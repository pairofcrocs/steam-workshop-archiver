"""Run queue, worker and the archive pipeline."""

import csv
import json
import logging
import os
import random
import tempfile
import threading
import time
from collections import deque

from . import config, db, names, notify, schedule
from .core import archive, downloader, metadata, scraper, steam_api
from .events import bus

log = logging.getLogger(__name__)

TERMINAL = ("done", "partial", "error", "cancelled")
PARAM_DEFAULTS = {
    "pages": "all", "sort_key": "trend_week", "delay_min": 1.0, "delay_max": 2.5,
    "scrape_only": False, "download_only": False, "fetch_sizes": False, "force": False,
}
ACTIVE = ("running", "waiting_confirm")
LOG_TAIL = 2000

_RUN_FIELDS = (
    "status", "phase", "started_at", "finished_at", "total", "done", "downloaded", "updated",
    "skipped", "failed", "unavailable", "failed_ids", "bytes_estimate", "error", "hint",
    "appid", "game_name",
)


class Cancelled(Exception):
    pass


def run_to_dict(row) -> dict:
    d = dict(row)
    d["params"] = json.loads(d.get("params") or "{}")
    d["failed_ids"] = json.loads(d.get("failed_ids") or "[]")
    return d


def logs_dir() -> str:
    return os.path.join(config.META_DIR, "logs")


def log_path(run_id: int) -> str:
    return os.path.join(logs_dir(), f"run-{run_id}.log")


class ActiveRun:
    """In-memory state of the run the worker is executing."""

    def __init__(self, row: dict):
        self.data = row
        self.cancel_event = threading.Event()
        self.confirm_event = threading.Event()
        self.tail: deque[str] = deque(maxlen=LOG_TAIL)
        self.confirm_info: dict | None = None
        os.makedirs(logs_dir(), exist_ok=True)
        self._log_file = open(log_path(row["id"]), "a", encoding="utf-8")
        self._last_publish = 0.0
        self._lock = threading.Lock()

    @property
    def id(self) -> int:
        return self.data["id"]

    def log(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self.tail.append(line)
            if not self._log_file.closed:
                self._log_file.write(f"[{stamp}] {line}\n")
                self._log_file.flush()
        bus.publish("log", run_id=self.id, line=line)

    def update(self, force: bool = False, **changes) -> None:
        with self._lock:
            self.data.update(changes)
        now = time.monotonic()
        if force or now - self._last_publish > 0.5:
            self._last_publish = now
            self.save()
            bus.publish("run_updated", run=self.public())

    def bump(self, **deltas) -> None:
        with self._lock:
            for k, v in deltas.items():
                self.data[k] += v
        self.update()

    def public(self) -> dict:
        with self._lock:
            return dict(self.data, failed_ids=list(self.data["failed_ids"]))

    def save(self) -> None:
        d = self.public()
        with db.transaction() as c:
            c.execute(
                f"UPDATE runs SET {', '.join(f'{f} = ?' for f in _RUN_FIELDS)} WHERE id = ?",
                [json.dumps(d[f]) if f == "failed_ids" else d[f] for f in _RUN_FIELDS] + [d["id"]],
            )

    def close(self) -> None:
        self._log_file.close()

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()


class JobManager:
    def __init__(self):
        self.current: ActiveRun | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._enqueue_lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        with db.transaction() as c:
            c.execute(
                "UPDATE runs SET status = 'error', error = 'Interrupted by a restart', finished_at = ? "
                "WHERE status IN ('running', 'waiting_confirm')",
                (time.time(),),
            )
        self._stop.clear()
        for target in (self._worker, self._scheduler):
            t = threading.Thread(target=target, daemon=True, name=target.__name__)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        cur = self.current
        if cur:
            cur.cancel_event.set()
            cur.confirm_event.set()
        for t in self._threads:
            t.join(timeout=15)
        self._threads.clear()

    # ------------------------------------------------------------------ queue API
    def queue(self) -> list[dict]:
        return [run_to_dict(r) for r in db.query("SELECT * FROM runs WHERE status = 'queued' ORDER BY id")]

    def get(self, run_id: int) -> dict | None:
        cur = self.current
        if cur and cur.id == run_id:
            return cur.public()
        row = db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return run_to_dict(row) if row else None

    def active_appid(self) -> str | None:
        cur = self.current
        return cur.data["appid"] if cur else None

    @staticmethod
    def _same_work(run: dict, spec: dict, params: dict, saved_job_id: str | None) -> bool:
        if (run["kind"], run["source_type"], run["source_value"], run["saved_job_id"], run["params"]) != (
            spec["kind"], spec.get("source_type", "workshop"), spec.get("source_value") or "", saved_job_id, params,
        ):
            return False
        # Collection/item runs resolve their appid while running; only compare when both are known.
        want = spec.get("appid") or ""
        return not want or not run["appid"] or run["appid"] == want

    def enqueue(self, spec: dict, trigger: str = "manual", saved_job_id: str | None = None) -> dict:
        """Queue a run. Returns an identical queued/running run instead of duplicating it."""
        params = {k: default if spec.get(k) is None else spec[k] for k, default in PARAM_DEFAULTS.items()}
        with self._enqueue_lock:
            cur = self.current
            if cur and self._same_work(cur.public(), spec, params, saved_job_id):
                return cur.public()
            for q in self.queue():
                if self._same_work(q, spec, params, saved_job_id):
                    return q
            appid = spec.get("appid") or ""
            game_name = names.get(appid, fetch=False) if appid else ""
            with db.transaction() as c:
                row = c.execute(
                    "INSERT INTO runs(kind, trigger, saved_job_id, appid, game_name, source_type, source_value, "
                    "params, status, queued_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                    (spec["kind"], trigger, saved_job_id, appid, game_name, spec.get("source_type", "workshop"),
                     spec.get("source_value") or "", json.dumps(params), time.time()),
                )
                run_id = row.lastrowid
        run = self.get(run_id)
        bus.publish("run_queued", run=run)
        self._wake.set()
        return run

    def cancel(self, run_id: int) -> dict | None:
        with self._enqueue_lock:
            cur = self.current
            if cur and cur.id == run_id:
                cur.cancel_event.set()
                cur.confirm_event.set()
            else:
                with db.transaction() as c:
                    c.execute(
                        "UPDATE runs SET status = 'cancelled', finished_at = ? WHERE id = ? AND status = 'queued'",
                        (time.time(), run_id),
                    )
                cur = None
        if cur:
            cur.log("Cancelling...")
            return cur.public()
        run = self.get(run_id)
        if run:
            bus.publish("run_finished", run=run)
        return run

    def confirm(self, run_id: int) -> bool:
        cur = self.current
        if cur and cur.id == run_id and cur.data["status"] == "waiting_confirm":
            cur.confirm_event.set()
            return True
        return False

    # ------------------------------------------------------------------ worker
    def _claim_next(self) -> ActiveRun | None:
        """Atomically move the oldest queued run to running and make it current."""
        with self._enqueue_lock:
            row = db.query_one("SELECT * FROM runs WHERE status = 'queued' ORDER BY id LIMIT 1")
            if row is None:
                return None
            with db.transaction() as c:
                claimed = c.execute(
                    "UPDATE runs SET status = 'running', phase = 'resolving', started_at = ? "
                    "WHERE id = ? AND status = 'queued'",
                    (time.time(), row["id"]),
                ).rowcount
            if not claimed:
                return None
            data = run_to_dict(db.query_one("SELECT * FROM runs WHERE id = ?", (row["id"],)))
            self.current = ActiveRun(data)
            return self.current

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                active = self._claim_next()
            except Exception:
                log.exception("Couldn't start the next queued run")
                active = None
                self._stop.wait(5)
            if active is None:
                self._wake.wait(timeout=5)
                self._wake.clear()
                continue
            try:
                self._execute(active)
            except Exception as exc:
                log.exception("Worker failed while finishing run %s", active.id)
                self.current = None
                with db.transaction() as c:
                    c.execute(
                        "UPDATE runs SET status = 'error', error = ?, finished_at = ? "
                        "WHERE id = ? AND status IN ('queued', 'running', 'waiting_confirm')",
                        (str(exc), time.time(), active.id),
                    )
                try:
                    active.close()
                except Exception:
                    pass
                run = self.get(active.id)
                if run:
                    bus.publish("run_finished", run=run)

    def _execute(self, active: ActiveRun) -> None:
        bus.publish("run_started", run=active.public())
        settings: dict = {}
        try:
            settings = config.get_settings()
            if active.data["kind"] == "metadata":
                status = _metadata_pipeline(active, settings)
            else:
                status = _archive_pipeline(active, settings)
        except Cancelled:
            status = "cancelled"
            active.log("--- Run cancelled ---")
        except Exception as exc:
            log.exception("Run %s failed", active.id)
            status = "error"
            active.data["error"] = str(exc) or exc.__class__.__name__
            active.log(f"FATAL ERROR: {active.data['error']}")
        if active.cancel_event.is_set() and status != "error":
            status = "cancelled"
        active.update(force=True, status=status, phase="finished", finished_at=time.time())
        active.close()
        run = active.public()
        self.current = None
        if run["saved_job_id"]:
            with db.transaction() as c:
                c.execute("UPDATE saved_jobs SET last_status = ? WHERE id = ?", (status, run["saved_job_id"]))
            bus.publish("saved_jobs_changed")
        bus.publish("run_finished", run=run)
        if run["appid"]:
            bus.publish("archive_changed", appid=run["appid"])
        notify.notify_run(settings, run)

    # ------------------------------------------------------------------ scheduler
    def _scheduler(self) -> None:
        while not self._stop.wait(timeout=30):
            try:
                self.run_due_jobs()
            except Exception:
                log.exception("Scheduler error")

    def run_due_jobs(self, now: float | None = None) -> None:
        now = now or time.time()
        due = db.query(
            "SELECT * FROM saved_jobs WHERE enabled = 1 AND schedule_type != 'none' "
            "AND next_run_at IS NOT NULL AND next_run_at <= ?",
            (now,),
        )
        for job in due:
            job = dict(job)
            nxt = schedule.next_run(job["schedule_type"], job["schedule_value"], after=now, prev_due=job["next_run_at"])
            with db.transaction() as c:
                c.execute(
                    "UPDATE saved_jobs SET last_run_at = ?, next_run_at = ? WHERE id = ?",
                    (now, nxt, job["id"]),
                )
            self.enqueue(saved_job_spec(job), trigger="schedule", saved_job_id=job["id"])
        if due:
            bus.publish("saved_jobs_changed")


def saved_job_spec(job: dict) -> dict:
    return {
        "kind": "archive",
        "appid": job["appid"],
        "source_type": job["source_type"],
        "source_value": job["source_value"],
        "pages": job["pages"],
        "sort_key": job["sort_key"],
        "delay_min": job["delay_min"],
        "delay_max": job["delay_max"],
        "scrape_only": bool(job["scrape_only"]),
        "download_only": bool(job["download_only"]),
        "fetch_sizes": False,
        "force": False,
    }


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------
def manifest_path(appid: str) -> str:
    return os.path.join(archive.game_meta_dir(appid), "data.csv")


def write_manifest(appid: str, items: list[dict]) -> None:
    path = manifest_path(appid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Title", "Link", "Author"])
            for it in items:
                w.writerow([
                    it.get("title", ""),
                    f"https://steamcommunity.com/sharedfiles/filedetails/?id={it['id']}",
                    it.get("author", ""),
                ])
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_manifest(appid: str) -> list[str]:
    with open(manifest_path(appid), encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        return [iid for row in reader if len(row) > 1 and (iid := steam_api.extract_workshop_id(row[1]))]


def _pages(value) -> int | None:
    return None if str(value).strip().lower() == "all" else int(value)


def _resolve_workshop(run: ActiveRun, settings: dict, appid: str, params: dict) -> tuple[list[str], bool]:
    """Item ids for a whole-Workshop source. Returns (ids, complete)."""
    if params.get("download_only"):
        try:
            ids = read_manifest(appid)
        except OSError as exc:
            raise RuntimeError("No saved item list yet — run a scrape first (download-only mode).") from exc
        run.log(f"Using the saved item list ({len(ids)} items).")
        return ids, True

    pages = _pages(params.get("pages", "all"))
    sort_key = params.get("sort_key") or "trend_week"
    key = settings.get("steam_api_key")
    if key:
        run.log("Listing items via the Steam Web API...")
        ids: list[str] = []
        seen: set[str] = set()
        items: list[dict] = []
        max_items = pages * 30 if pages else None
        for page in steam_api.query_files(key, appid, sort_key, max_items, run.log, run.cancel_event.is_set):
            run.check_cancel()
            metadata.upsert_details(appid, page)
            for d in page:
                if d["item_id"] not in seen:
                    seen.add(d["item_id"])
                    ids.append(d["item_id"])
                    items.append({"id": d["item_id"], "title": d["title"], "author": d["creator"]})
            run.log(f"Listed {len(ids)} items...")
        if sort_key == "random":
            random.shuffle(ids)
        run.check_cancel()
        write_manifest(appid, items)
        return ids, True

    result = scraper.scrape_workshop(
        appid, pages, params.get("delay_min", 1.0), params.get("delay_max", 2.5), sort_key,
        run.log, run.cancel_event.is_set,
    )
    run.check_cancel()
    if result.game_name:
        names.remember(appid, result.game_name)
    if not result.items:
        raise RuntimeError(result.error or "No Workshop items found.")
    if result.complete:
        write_manifest(appid, result.items)
        run.log(f"Saved item list ({len(result.items)} items).")
    else:
        run.log("Scrape stopped early — keeping the previous saved item list; continuing with what was found.")
    return [it["id"] for it in result.items], result.complete


def _resolve_source(run: ActiveRun, settings: dict) -> tuple[str, list[str], bool]:
    """(appid, item ids, complete) for the run's source."""
    d = run.data
    params = d["params"]
    appid = d["appid"]
    if d["source_type"] == "workshop":
        if not steam_api.is_id(appid):
            raise RuntimeError("A valid App ID is required.")
        ids, complete = _resolve_workshop(run, settings, appid, params)
        return appid, ids, complete

    if d["source_type"] == "collection":
        cid = steam_api.extract_workshop_id(d["source_value"])
        if not cid:
            raise RuntimeError("Invalid collection id or URL.")
        run.log(f"Expanding collection {cid}...")
        ids = steam_api.expand_collection(cid)
        run.log(f"Collection has {len(ids)} items.")
    else:
        ids = steam_api.parse_item_list(d["source_value"])
        run.log(f"{len(ids)} item(s) requested.")
    if not ids:
        raise RuntimeError("No items to download.")

    run.log("Looking up item details...")
    details = steam_api.get_published_file_details(ids, log_fn=run.log, cancel_check=run.cancel_event.is_set)
    run.check_cancel()
    nested = [i for i in ids if details.get(i, {}).get("is_collection")]
    if nested:
        run.log(f"Expanding {len(nested)} collection(s) in the list...")
        expanded = [c for cid in nested for c in steam_api.expand_collection(cid)]
        ids = list(dict.fromkeys([i for i in ids if i not in nested] + expanded))
        missing = [i for i in ids if i not in details]
        details.update(steam_api.get_published_file_details(missing, log_fn=run.log))
    if not appid:
        counts: dict[str, int] = {}
        for x in details.values():
            if x["appid"]:
                counts[x["appid"]] = counts.get(x["appid"], 0) + 1
        if not counts:
            raise RuntimeError("Couldn't determine which game these items belong to.")
        appid = max(counts, key=counts.get)
        run.log(f"Items belong to App ID {appid}.")
    foreign = [i for i in ids if details.get(i, {}).get("appid") not in ("", None, appid)]
    if foreign:
        run.log(f"WARNING: skipping {len(foreign)} item(s) from a different game: {', '.join(foreign[:10])}")
        ids = [i for i in ids if i not in set(foreign)]
    metadata.upsert_details(appid, [details[i] for i in ids if i in details])
    return appid, ids, True


def _archive_pipeline(run: ActiveRun, settings: dict) -> str:
    params = run.data["params"]
    appid, ids, complete = _resolve_source(run, settings)
    game_name = names.get(appid)
    run.update(force=True, appid=appid, game_name=game_name)
    run.log(f"Game: {game_name} ({appid})")
    run.check_cancel()

    if params.get("scrape_only"):
        run.update(force=True, total=len(ids), done=len(ids))
        run.log("--- Scrape complete ---")
        return "done" if complete else "partial"

    # Metadata drives update detection, removed-item flags and size estimates.
    run.update(force=True, phase="metadata", total=len(ids))
    items = metadata.refresh(
        appid, ids, settings["metadata_ttl_hours"], log_fn=run.log, cancel_check=run.cancel_event.is_set
    )
    run.check_cancel()

    on_disk = archive.scan(appid)
    acf = archive.parse_acf(appid)
    tasks: list[downloader.Task] = []
    skipped = unavailable = 0
    for iid in ids:
        meta = items.get(iid, {})
        present = iid in on_disk
        if meta.get("removed"):
            unavailable += 1
            continue
        api_time = meta.get("time_updated", 0)
        local_time = meta.get("downloaded_time_updated") or acf.get(iid, {}).get("timeupdated", 0)
        if present and not params.get("force") and (api_time == 0 or local_time >= api_time):
            skipped += 1
            continue
        tasks.append(downloader.Task(
            iid, meta.get("title", ""), api_time, meta.get("file_url", ""), meta.get("filename", ""),
            force=bool(params.get("force")),
        ))
    if unavailable:
        run.log(f"{unavailable} item(s) are removed or private on the Workshop (local copies are kept).")
    run.update(force=True, skipped=skipped, unavailable=unavailable, done=skipped + unavailable)
    updates = sum(1 for t in tasks if t.item_id in on_disk)
    run.log(f"Plan: {len(tasks) - updates} new, {updates} updated, {skipped} already up to date.")

    if tasks and params.get("fetch_sizes"):
        _confirm_gate(run, settings, tasks, items)

    if tasks:
        run.update(force=True, phase="downloading")
        _download(run, settings, appid, tasks, on_disk)
    run.check_cancel()

    run.update(force=True, phase="images")
    on_disk_now = archive.scan(appid)
    present_ids = [i for i in ids if i in on_disk_now]
    metadata.download_images(appid, present_ids, run.log, run.cancel_event.is_set)
    run.check_cancel()

    d = run.public()
    run.log(
        f"--- Finished: {d['downloaded']} downloaded, {d['updated']} updated, {d['skipped']} up to date, "
        f"{d['failed']} failed, {d['unavailable']} unavailable ---"
    )
    return "partial" if d["failed"] or not complete else "done"


def _confirm_gate(run: ActiveRun, settings: dict, tasks: list, items: dict) -> None:
    total_bytes = sum(items.get(t.item_id, {}).get("file_size", 0) for t in tasks)
    timeout = settings["confirm_timeout_min"] * 60
    run.update(force=True, status="waiting_confirm", bytes_estimate=total_bytes)
    run.log(f"About to download {len(tasks)} item(s), roughly {_fmt_bytes(total_bytes)}. Waiting for confirmation...")
    run.confirm_info = {"run_id": run.id, "items": len(tasks), "bytes": total_bytes, "timeout_at": time.time() + timeout}
    bus.publish("confirm", **run.confirm_info)
    try:
        confirmed = run.confirm_event.wait(timeout)
    finally:
        run.confirm_info = None
    run.check_cancel()
    if not confirmed:
        run.log("No confirmation received in time — cancelling.")
        run.cancel_event.set()
        raise Cancelled()
    run.update(force=True, status="running")
    run.log("Confirmed — starting downloads.")


def _download(run: ActiveRun, settings: dict, appid: str, tasks: list, on_disk: dict) -> None:
    by_id = {t.item_id: t for t in tasks}
    failed_ids: list[str] = []

    def on_result(item_id: str, ok: bool, reason: str) -> None:
        t = by_id[item_id]
        if ok:
            entry = archive.entry(appid, item_id)
            size = archive.item_disk_size(entry) if entry else 0
            metadata.mark_downloaded(appid, item_id, t.time_updated, size)
            was_update = item_id in on_disk
            run.log(f"{'Updated' if was_update else 'Downloaded'}: {t.title or item_id}")
            run.bump(done=1, **{"updated" if was_update else "downloaded": 1})
        else:
            failed_ids.append(item_id)
            run.log(f"FAILED: {t.title or item_id} ({item_id}) — {reason}")
            with run._lock:
                run.data["failed_ids"] = list(failed_ids)
            run.bump(done=1, failed=1)

    ctx = downloader.Context(
        appid=appid, log=run.log, cancel_event=run.cancel_event, on_result=on_result, settings=settings,
        delay_min=run.data["params"].get("delay_min", 1.0), delay_max=run.data["params"].get("delay_max", 2.5),
    )
    downloader.download(tasks, ctx)
    if ctx.hints:
        hint = " ".join(sorted(ctx.hints))
        run.update(force=True, hint=hint)
        run.log(f"HINT: {hint}")


def _metadata_pipeline(run: ActiveRun, settings: dict) -> str:
    appid = run.data["appid"]
    run.update(force=True, game_name=names.get(appid), phase="metadata")
    ids = sorted(archive.scan(appid))
    run.update(force=True, total=len(ids))
    if not ids:
        run.log("No downloaded items to refresh.")
        return "done"
    metadata.refresh(appid, ids, 0, force=True, log_fn=run.log, cancel_check=run.cancel_event.is_set)
    run.check_cancel()
    run.update(force=True, phase="images")
    metadata.download_images(appid, ids, run.log, run.cancel_event.is_set)
    run.update(force=True, done=len(ids))
    run.log("--- Metadata refresh complete ---")
    return "done"


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


manager = JobManager()
