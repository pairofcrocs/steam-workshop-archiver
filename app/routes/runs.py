"""Run queue/history and saved (scheduled) jobs."""

import json
import logging
import os
import time
import uuid

from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import Response

from .. import config, db, names, schedule
from ..core import steam_api
from ..events import bus
from ..jobs import TERMINAL, log_path, manager, run_to_dict, saved_job_spec
from ..models import RunRequest, SavedJobRequest, _check_pages

log = logging.getLogger(__name__)
router = APIRouter()
RunId = Path(ge=1, le=2**62)


def _run_or_404(run_id: int) -> dict:
    run = manager.get(run_id)
    if not run:
        raise HTTPException(404, "Run not found.")
    return run


@router.post("/api/runs")
def create_run(req: RunRequest):
    return manager.enqueue(req.model_dump())


@router.get("/api/runs")
def list_runs(limit: int = 50, offset: int = 0, appid: str = ""):
    limit = max(1, min(limit, 200))
    offset = max(0, min(offset, 10**9))
    where, params = "", []
    if appid:
        if not steam_api.is_id(appid):
            raise HTTPException(400, "Invalid App ID.")
        where, params = "WHERE appid = ?", [appid]
    total = db.query_one(f"SELECT COUNT(*) AS n FROM runs {where}", params)["n"]
    rows = db.query(f"SELECT * FROM runs {where} ORDER BY id DESC LIMIT ? OFFSET ?", [*params, limit, offset])
    runs = [run_to_dict(r) for r in rows]
    cur = manager.current
    if cur:
        runs = [cur.public() if r["id"] == cur.id else r for r in runs]
    return {"runs": runs, "total": total}


@router.get("/api/runs/{run_id}")
def get_run(run_id: int = RunId):
    return _run_or_404(run_id)


@router.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: int = RunId):
    _run_or_404(run_id)
    return manager.cancel(run_id)


@router.post("/api/runs/{run_id}/confirm")
def confirm_run(run_id: int = RunId):
    if not manager.confirm(run_id):
        raise HTTPException(409, "That run isn't waiting for confirmation.")
    return {"status": "confirmed"}


@router.post("/api/runs/{run_id}/retry")
def retry_run(run_id: int = RunId):
    run = _run_or_404(run_id)
    if not run["failed_ids"]:
        raise HTTPException(409, "That run has no failed items.")
    return manager.enqueue(
        {
            "kind": "archive", "appid": run["appid"], "source_type": "items",
            "source_value": "\n".join(run["failed_ids"]),
            **{k: run["params"].get(k) for k in ("delay_min", "delay_max")},
            "pages": "all", "sort_key": "trend_week", "scrape_only": False, "download_only": False,
            "fetch_sizes": False, "force": False,
        },
        trigger="retry",
    )


@router.get("/api/runs/{run_id}/log")
def run_log(run_id: int = RunId, download: int = 0):
    _run_or_404(run_id)
    path = log_path(run_id)
    try:
        with open(path, "rb") as f:  # snapshot: the log may still be growing
            body = f.read()
    except OSError:
        body = b""
    headers = {"Content-Disposition": f'attachment; filename="run-{run_id}.log"'} if download else {}
    return Response(body, media_type="text/plain; charset=utf-8", headers=headers)


@router.delete("/api/runs/{run_id}")
def delete_run(run_id: int = RunId):
    run = _run_or_404(run_id)
    if run["status"] not in TERMINAL:
        raise HTTPException(409, "Only finished runs can be deleted.")
    with db.transaction() as c:
        c.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    try:
        os.unlink(log_path(run_id))
    except OSError:
        pass
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Saved jobs
# ---------------------------------------------------------------------------
_JOB_FIELDS = (
    "name", "appid", "game_name", "source_type", "source_value", "pages", "sort_key", "delay_min",
    "delay_max", "scrape_only", "download_only", "schedule_type", "schedule_value", "enabled", "next_run_at",
)


def job_to_dict(row) -> dict:
    d = dict(row)
    for k in ("scrape_only", "download_only", "enabled"):
        d[k] = bool(d[k])
    try:
        d["schedule_description"] = schedule.describe(d["schedule_type"], d["schedule_value"])
    except (ValueError, IndexError):
        d["schedule_description"] = d["schedule_value"]
    return d


def _job_values(req: SavedJobRequest) -> dict:
    err = schedule.validate(req.schedule_type, req.schedule_value)
    if err:
        raise HTTPException(422, err)
    values = req.model_dump()
    values["schedule_value"] = values["schedule_value"].strip()
    if not values["appid"]:
        values["appid"] = _appid_for_source(req)
    values["game_name"] = values["game_name"] or names.get(values["appid"])
    values["next_run_at"] = (
        schedule.next_run(req.schedule_type, values["schedule_value"])
        if req.enabled and req.schedule_type != "none" else None
    )
    return values


def _appid_for_source(req: SavedJobRequest) -> str:
    if req.source_type == "collection":
        ids = [steam_api.extract_workshop_id(req.source_value) or ""]
    else:
        ids = steam_api.parse_item_list(req.source_value)[:5]
    details = steam_api.get_published_file_details([i for i in ids if i])
    apps = [d["appid"] for d in details.values() if d["appid"]]
    if not apps:
        raise HTTPException(422, "Couldn't determine the game for that collection / item list.")
    return apps[0]


def _get_job(job_id: str) -> dict:
    row = db.query_one("SELECT * FROM saved_jobs WHERE id = ?", (job_id,))
    if not row:
        raise HTTPException(404, "Saved job not found.")
    return job_to_dict(row)


@router.get("/api/saved-jobs")
def list_saved_jobs():
    return [job_to_dict(r) for r in db.query("SELECT * FROM saved_jobs ORDER BY game_name, created_at")]


@router.post("/api/saved-jobs")
def create_saved_job(req: SavedJobRequest):
    values = _job_values(req)
    job_id = str(uuid.uuid4())
    with db.transaction() as c:
        c.execute(
            f"INSERT INTO saved_jobs(id, created_at, {', '.join(_JOB_FIELDS)}) "
            f"VALUES(?, ?, {', '.join('?' * len(_JOB_FIELDS))})",
            [job_id, time.time(), *(values[f] for f in _JOB_FIELDS)],
        )
    bus.publish("saved_jobs_changed")
    return _get_job(job_id)


@router.put("/api/saved-jobs/{job_id}")
def update_saved_job(job_id: str, req: SavedJobRequest):
    _get_job(job_id)
    values = _job_values(req)
    with db.transaction() as c:
        c.execute(
            f"UPDATE saved_jobs SET {', '.join(f'{f} = ?' for f in _JOB_FIELDS)} WHERE id = ?",
            [*(values[f] for f in _JOB_FIELDS), job_id],
        )
    bus.publish("saved_jobs_changed")
    return _get_job(job_id)


@router.delete("/api/saved-jobs/{job_id}")
def delete_saved_job(job_id: str):
    _get_job(job_id)
    with db.transaction() as c:
        c.execute("DELETE FROM saved_jobs WHERE id = ?", (job_id,))
    bus.publish("saved_jobs_changed")
    return {"status": "deleted"}


@router.post("/api/saved-jobs/{job_id}/run")
def run_saved_job(job_id: str):
    job = _get_job(job_id)
    with db.transaction() as c:
        c.execute("UPDATE saved_jobs SET last_run_at = ? WHERE id = ?", (time.time(), job_id))
    bus.publish("saved_jobs_changed")
    return manager.enqueue(saved_job_spec(job), trigger="manual", saved_job_id=job_id)


def _legacy_pages(value) -> str:
    try:
        return _check_pages(str(value if value is not None else "all"))
    except ValueError:
        return "all"


def import_legacy_saved_jobs() -> int:
    """Import v1 saved_jobs.json once."""
    path = os.path.join(config.META_DIR, "saved_jobs.json")
    if not os.path.isfile(path):
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            jobs = json.load(f)
    except (OSError, ValueError):
        log.warning("Couldn't read %s; leaving it in place", path)
        return 0
    count = 0
    with db.transaction() as c:
        for j in jobs if isinstance(jobs, list) else []:
            if not isinstance(j, dict):
                continue
            try:
                stype = j.get("schedule_type", "none")
                svalue = str(j.get("schedule_value", ""))
                if schedule.validate(stype, svalue):
                    stype, svalue = "none", ""
                appid = str(j["appid"]).strip()
                if not steam_api.is_id(appid):
                    continue
                c.execute(
                    f"INSERT OR IGNORE INTO saved_jobs(id, created_at, {', '.join(_JOB_FIELDS)}) "
                    f"VALUES(?, ?, {', '.join('?' * len(_JOB_FIELDS))})",
                    [
                        j.get("id") or str(uuid.uuid4()), j.get("created_at") or time.time(),
                        "", appid, j.get("game_name", ""), "workshop", "", _legacy_pages(j.get("pages")),
                        j.get("sort_key") if j.get("sort_key") in steam_api.SORT_KEYS else "trend_week",
                        float(j.get("delay_min", 1.0)),
                        float(j.get("delay_max", 2.5)), int(bool(j.get("scrape_only"))),
                        int(bool(j.get("download_only"))), stype, svalue, 1,
                        schedule.next_run(stype, svalue) if stype != "none" else None,
                    ],
                )
                count += 1
            except (KeyError, TypeError, ValueError):
                log.warning("Skipping malformed saved job: %r", j)
    os.replace(path, path + ".imported")
    return count
