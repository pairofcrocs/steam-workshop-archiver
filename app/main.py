"""FastAPI application — Steam Workshop Archiver web UI."""

import asyncio
import base64
import csv
import json
import os
import queue
import re
import secrets
import shutil
import time
import uuid
import zipfile
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import AsyncGenerator
from urllib.parse import quote as url_quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from . import __version__
from .core.downloader import download_workshop_items
from .core.scraper import scrape_workshop
from .core.utils import (
    SESSION,
    csv_path,
    extract_workshop_id,
    fetch_and_cache_metadata,
    format_bytes,
    get_game_name,
)

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
STEAMCMD_PATH = os.environ.get("STEAMCMD_PATH", "/opt/steamcmd/steamcmd.sh")
# META_DIR  — small files: CSV lists, HTML viewers (put on fast cache drive)
# DOWNLOADS_DIR — large files: SteamCMD workshop downloads (put on array)
# Falls back to OUTPUT_DIR (legacy) then /data so old deployments still work.
META_DIR        = os.environ.get("META_DIR", "/meta")
DOWNLOADS_DIR   = os.environ.get("DOWNLOADS_DIR", "/downloads")
SAVED_JOBS_FILE = os.path.join(META_DIR, "saved_jobs.json")
AUTH_PASSWORD   = os.environ.get("AUTH_PASSWORD", "")

# ---------------------------------------------------------------------------
# Saved-jobs helpers
# ---------------------------------------------------------------------------
def _load_saved_jobs() -> None:
    global _saved_jobs
    try:
        with open(SAVED_JOBS_FILE, "r", encoding="utf-8") as f:
            _saved_jobs = json.load(f)
    except (OSError, ValueError):
        _saved_jobs = []


def _save_saved_jobs() -> None:
    try:
        os.makedirs(META_DIR, exist_ok=True)
        with open(SAVED_JOBS_FILE, "w", encoding="utf-8") as f:
            json.dump(_saved_jobs, f, indent=2)
    except OSError:
        pass


def _validate_schedule(schedule_type: str, schedule_value: str) -> str | None:
    """Return an error message if the schedule is invalid, or None if valid."""
    if schedule_type == "interval":
        try:
            hours = float(schedule_value)
            if hours <= 0:
                return "Interval must be a positive number of hours."
        except (ValueError, TypeError):
            return f"Invalid interval value {schedule_value!r} — must be a positive number."
    elif schedule_type == "cron":
        try:
            from croniter import croniter
            if not croniter.is_valid(schedule_value):
                return f"Invalid cron expression: {schedule_value!r}"
        except Exception:
            return f"Invalid cron expression: {schedule_value!r}"
    return None


def _compute_next_run(
    schedule_type: str, schedule_value: str, from_time: float | None = None
) -> float | None:
    t = from_time if from_time is not None else time.time()
    if schedule_type == "interval":
        try:
            hours = float(schedule_value)
            if hours > 0:
                return t + hours * 3600
        except (ValueError, TypeError):
            pass
        return None
    if schedule_type == "cron":
        try:
            from croniter import croniter
            it = croniter(schedule_value, t)
            return it.get_next(float)
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    _load_saved_jobs()
    task = asyncio.create_task(_scheduler_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Steam Workshop Archiver", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

if AUTH_PASSWORD:
    class _BasicAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Basic "):
                try:
                    decoded = base64.b64decode(auth[6:]).decode("utf-8")
                    _, pwd = decoded.split(":", 1)
                    if secrets.compare_digest(pwd.encode(), AUTH_PASSWORD.encode()):
                        return await call_next(request)
                except Exception:
                    pass
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Steam Workshop Archiver"'},
                content="Unauthorized",
            )
    app.add_middleware(_BasicAuthMiddleware)


# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------
class JobStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass
class Job:
    status: JobStatus = JobStatus.IDLE
    log_queue: queue.Queue = field(default_factory=queue.Queue)
    thread: threading.Thread | None = None
    appid: str = ""
    game_name: str = ""
    error: str = ""
    cancelled: bool = False
    saved_job_id: str = ""
    waiting_confirm: bool = False
    confirm_event: threading.Event = field(default_factory=threading.Event)


# Single global job; only one job runs at a time.
_current_job = Job()
_job_lock = threading.Lock()

# Saved jobs (persisted to SAVED_JOBS_FILE).
_saved_jobs: list[dict] = []
_saved_jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Job request model
# ---------------------------------------------------------------------------
class StartJobRequest(BaseModel):
    appid: str = Field(max_length=20)
    pages: str = Field(default="all", max_length=10)
    delay_min: float = Field(default=1.0, ge=0.0)
    delay_max: float = Field(default=2.5, ge=0.0)
    fetch_sizes: bool = False
    scrape_only: bool = False
    download_only: bool = False
    sort_key: str = Field(default="trend_week", max_length=30)


class SavedJobRequest(BaseModel):
    appid: str = Field(max_length=20)
    game_name: str = Field(default="", max_length=200)
    pages: str = Field(default="all", max_length=10)
    delay_min: float = Field(default=1.0, ge=0.0)
    delay_max: float = Field(default=2.5, ge=0.0)
    scrape_only: bool = False
    download_only: bool = False
    schedule_type: str = "none"   # "none", "interval", "cron"
    schedule_value: str = Field(default="", max_length=100)
    sort_key: str = Field(default="trend_week", max_length=30)


# ---------------------------------------------------------------------------
# Job launcher & scheduler (defined after models so type annotations resolve)
# ---------------------------------------------------------------------------
def _launch_job(req: StartJobRequest, saved_job_id: str = "") -> bool:
    """Start the pipeline in a background thread. Returns False if already running."""
    global _current_job
    with _job_lock:
        if _current_job.status == JobStatus.RUNNING:
            return False
        _current_job = Job(appid=req.appid.strip(), saved_job_id=saved_job_id)
        _current_job.status = JobStatus.RUNNING
    thread = threading.Thread(
        target=_run_pipeline, args=(_current_job, req), daemon=True
    )
    _current_job.thread = thread
    thread.start()
    return True


async def _scheduler_loop() -> None:
    """Background task: trigger saved jobs whose next_run_at is due."""
    import logging
    _sched_log = logging.getLogger(__name__ + ".scheduler")

    while True:
        await asyncio.sleep(60)
        try:
            now = time.time()
            # Collect due jobs without mutating their timestamps yet —
            # we only advance next_run_at for the job we actually launch.
            with _saved_jobs_lock:
                due_jobs = [
                    dict(jd) for jd in _saved_jobs
                    if jd.get("next_run_at") and jd["next_run_at"] <= now
                ]
            for jd in due_jobs:
                req = StartJobRequest(
                    appid=jd["appid"],
                    pages=jd["pages"],
                    delay_min=jd["delay_min"],
                    delay_max=jd["delay_max"],
                    scrape_only=jd["scrape_only"],
                    download_only=jd["download_only"],
                    sort_key=jd.get("sort_key", "trend_week"),
                )
                if _launch_job(req, saved_job_id=jd["id"]):
                    # Advance only the job we launched; remaining due jobs keep
                    # their timestamps and will be picked up on the next cycle.
                    with _saved_jobs_lock:
                        for saved in _saved_jobs:
                            if saved["id"] == jd["id"]:
                                saved["last_run_at"] = now
                                saved["next_run_at"] = _compute_next_run(
                                    jd["schedule_type"], jd["schedule_value"], from_time=now
                                )
                                break
                        _save_saved_jobs()
                    break  # only one job at a time
        except Exception:
            _sched_log.exception("Scheduler encountered an unexpected error — will retry next cycle.")


# ---------------------------------------------------------------------------
# Pipeline (runs in a background thread)
# ---------------------------------------------------------------------------
def _run_pipeline(job: Job, req: StartJobRequest) -> None:
    def log(msg: str) -> None:
        job.log_queue.put(msg)

    def cancelled() -> bool:
        return job.cancelled

    try:
        os.makedirs(META_DIR, exist_ok=True)
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        appid = req.appid.strip()
        if not _NUMERIC_RE.match(appid):
            log(f"ERROR: Invalid App ID '{appid}' — must be numeric.")
            job.status = JobStatus.ERROR
            return

        log(f"Fetching game name for App ID {appid}...")
        game_name = get_game_name(appid)
        job.game_name = game_name
        log(f"Game: {game_name}")

        list_file = csv_path(META_DIR, appid)

        # ---- Scrape ----
        if not req.download_only:
            if cancelled():
                job.status = JobStatus.DONE
                log("--- Job cancelled ---")
                return

            pages_arg = req.pages.strip()
            if pages_arg.lower() == "all":
                num_pages = -1
            else:
                try:
                    num_pages = int(pages_arg)
                except ValueError:
                    log(f"ERROR: pages must be an integer or 'all', got: {pages_arg!r}")
                    job.status = JobStatus.ERROR
                    return

            scrape_workshop(
                appid=appid,
                output_dir=META_DIR,
                game_name=game_name,
                num_pages=num_pages,
                delay_min=req.delay_min,
                delay_max=req.delay_max,
                sort_key=req.sort_key,
                log_fn=log,
                cancel_check=cancelled,
            )
        else:
            if not os.path.exists(list_file):
                log(f"ERROR: No existing CSV found for download-only mode: {list_file}")
                job.status = JobStatus.ERROR
                return

        if cancelled():
            job.status = JobStatus.DONE
            log("--- Job cancelled ---")
            return

        # ---- File sizes + confirmation gate ----
        if req.fetch_sizes and os.path.exists(list_file):
            _links: list[str] = []
            with open(list_file, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                next(reader, None)
                for row in reader:
                    _links.append(row[1] if len(row) > 1 else "")

            csv_ids = [extract_workshop_id(lnk) for lnk in _links if lnk]
            csv_ids = [iid for iid in csv_ids if iid]

            if csv_ids:
                meta = fetch_and_cache_metadata(appid, csv_ids, META_DIR, log_fn=log, download_previews=False)
                total_bytes = sum(
                    _safe_int(meta.get(iid, {}).get("file_size", 0)) for iid in csv_ids
                )
                if total_bytes:
                    log(f"Estimated download size: {format_bytes(total_bytes)} across {len(csv_ids)} items")
                else:
                    log(f"Found {len(csv_ids)} items (file sizes not available from Steam API)")

            # Pause here so the user can review the size before committing to the download.
            # In scrape-only mode there is nothing to confirm, so we skip the gate.
            if not req.scrape_only:
                log("__CONFIRM_NEEDED__")
                job.waiting_confirm = True
                job.confirm_event.wait(timeout=3600)
                job.waiting_confirm = False

        if cancelled():
            job.status = JobStatus.DONE
            log("--- Job cancelled ---")
            return

        # ---- Download ----
        if not req.scrape_only:
            if not os.path.exists(list_file):
                log(f"ERROR: Workshop list not found: {list_file}")
                job.status = JobStatus.ERROR
                return

            download_workshop_items(
                workshop_list_file=list_file,
                steamcmd_path=STEAMCMD_PATH,
                appid=appid,
                downloads_dir=DOWNLOADS_DIR,
                log_fn=log,
                cancel_check=cancelled,
            )

            # ---- Fetch Steam API metadata (enriches .bin items and items without workshop.json) ----
            if not cancelled():
                content_dir = os.path.join(
                    DOWNLOADS_DIR, "steamapps", "workshop", "content", appid
                )
                item_ids: list[str] = []
                try:
                    for entry in os.scandir(content_dir):
                        if entry.is_dir():
                            item_ids.append(entry.name)
                        elif entry.name.endswith(".bin"):
                            item_ids.append(entry.name[:-4])
                except OSError:
                    pass
                if item_ids:
                    fetch_and_cache_metadata(appid, item_ids, META_DIR, log_fn=log)

        job.status = JobStatus.DONE
        log("--- Job complete ---")

    except Exception as exc:
        job.status = JobStatus.ERROR
        job.error = str(exc)
        job.log_queue.put(f"FATAL ERROR: {exc}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "version": __version__,
        "steamcmd_path": STEAMCMD_PATH,
        "meta_dir": META_DIR,
        "downloads_dir": DOWNLOADS_DIR,
    })


_SEARCH_APPID_RE = re.compile(r'data-ds-appid="(\d+)"')
_SEARCH_NAME_RE  = re.compile(r'<span class="title">(.*?)</span>')
_SEARCH_IMG_RE   = re.compile(r'search_capsule"><img src="([^"]+)"')

# Simple TTL cache for game search results — avoids hammering Steam on every keystroke.
_search_cache: dict[str, tuple[float, list]] = {}
_SEARCH_CACHE_TTL = 300.0  # seconds


def _do_search(q: str) -> list:
    """Synchronous Steam store search — call via asyncio.to_thread."""
    url = (
        "https://store.steampowered.com/search/results/"
        f"?term={url_quote(q)}"
        "&category2=30&ndl=1&count=15&start=0&infinite=1"
    )
    try:
        r = SESSION.get(url, timeout=10)
        r.raise_for_status()
        html = r.json().get("results_html", "")
    except Exception:
        return []
    results = []
    for appid, name, logo in zip(
        _SEARCH_APPID_RE.findall(html),
        _SEARCH_NAME_RE.findall(html),
        _SEARCH_IMG_RE.findall(html),
    ):
        results.append({"appid": appid, "name": name, "logo": logo})
    return results[:10]


@app.get("/api/search")
async def search_games(q: str = ""):
    """Search Steam store for games that support Workshop (category2=30).

    Uses /search/results/ with infinite=1 which actually enforces the
    category2 filter, unlike /search/suggest which ignores it entirely.
    """
    if not q.strip():
        return []
    key = q.strip().lower()
    cached = _search_cache.get(key)
    if cached and time.time() - cached[0] < _SEARCH_CACHE_TTL:
        return cached[1]
    results = await asyncio.to_thread(_do_search, q.strip())
    _search_cache[key] = (time.time(), results)
    return results


@app.post("/jobs/start")
async def start_job(req: StartJobRequest):
    if not _launch_job(req):
        raise HTTPException(status_code=409, detail="A job is already running.")
    return {"status": "started", "appid": req.appid.strip()}


@app.post("/jobs/cancel")
async def cancel_job():
    if _current_job.status == JobStatus.RUNNING:
        _current_job.cancelled = True
        if _current_job.waiting_confirm:
            _current_job.confirm_event.set()
        return {"status": "cancelling"}
    return {"status": "no_job_running"}


@app.post("/jobs/confirm")
async def confirm_job():
    """Unblock a pipeline that is paused waiting for download confirmation."""
    if not _current_job.waiting_confirm:
        raise HTTPException(status_code=409, detail="No job is waiting for confirmation.")
    _current_job.confirm_event.set()
    return {"status": "confirmed"}


@app.get("/jobs/status")
async def job_status():
    return {
        "status": _current_job.status,
        "appid": _current_job.appid,
        "game_name": _current_job.game_name,
        "error": _current_job.error,
        "waiting_confirm": _current_job.waiting_confirm,
    }


@app.get("/jobs/stream")
async def job_stream():
    """Server-Sent Events endpoint that streams log lines from the current job."""

    async def generate() -> AsyncGenerator[str, None]:
        # Send a heartbeat comment immediately so the browser knows the stream is open
        yield ": connected\n\n"

        while True:
            try:
                message = _current_job.log_queue.get_nowait()
                if message == "__CONFIRM_NEEDED__":
                    payload = json.dumps({"type": "confirm"})
                else:
                    payload = json.dumps({"type": "log", "message": message})
                yield f"data: {payload}\n\n"
            except queue.Empty:
                if _current_job.status not in (JobStatus.RUNNING,):
                    # Drain any remaining messages before closing
                    while True:
                        try:
                            message = _current_job.log_queue.get_nowait()
                            if message == "__CONFIRM_NEEDED__":
                                payload = json.dumps({"type": "confirm"})
                            else:
                                payload = json.dumps({"type": "log", "message": message})
                            yield f"data: {payload}\n\n"
                        except queue.Empty:
                            break
                    payload = json.dumps({
                        "type": "done",
                        "status": _current_job.status,
                        "appid": _current_job.appid,
                        "game_name": _current_job.game_name,
                    })
                    yield f"data: {payload}\n\n"
                    break
                await asyncio.sleep(0.05)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# ID validation helper
# ---------------------------------------------------------------------------
_NUMERIC_RE = re.compile(r'^\d+$')


def _require_numeric_id(value: str, label: str = "ID") -> None:
    """Raise HTTP 400 if *value* is not a non-empty string of digits."""
    if not _NUMERIC_RE.match(value):
        raise HTTPException(status_code=400, detail=f"Invalid {label}: must be numeric.")


# ---------------------------------------------------------------------------
# Workshop browser helpers
# ---------------------------------------------------------------------------
_ACF_ITEM_RE = re.compile(r'"(\d+)"\s*\{([^}]+)\}')
_ACF_SIZE_RE = re.compile(r'"size"\s+"(\d+)"')
_ACF_TIME_RE = re.compile(r'"timeupdated"\s+"(\d+)"')


def _safe_int(v) -> int:
    """Convert v to int safely; returns 0 on any failure."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _parse_acf_items(acf_path: str) -> dict[str, dict]:
    """Return {item_id: {size, timeupdated}} from a SteamCMD appworkshop .acf file."""
    try:
        with open(acf_path, "r", encoding="utf-8") as f:
            text = f.read()
        installed = re.search(r'"WorkshopItemsInstalled"\s*\{(.*?)\n\t\}', text, re.DOTALL)
        if not installed:
            return {}
        result: dict[str, dict] = {}
        for m in _ACF_ITEM_RE.finditer(installed.group(1)):
            item_id, block = m.group(1), m.group(2)
            size_m = _ACF_SIZE_RE.search(block)
            time_m = _ACF_TIME_RE.search(block)
            result[item_id] = {
                "size": int(size_m.group(1)) if size_m else 0,
                "timeupdated": int(time_m.group(1)) if time_m else 0,
            }
        return result
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Workshop browser routes
# ---------------------------------------------------------------------------
@app.get("/api/workshop")
async def list_workshop_appids():
    """List all appids that have downloaded content in DOWNLOADS_DIR."""
    content_base = os.path.join(DOWNLOADS_DIR, "steamapps", "workshop", "content")
    result = []
    try:
        for appid in sorted(os.listdir(content_base)):
            appid_dir = os.path.join(content_base, appid)
            if not os.path.isdir(appid_dir):
                continue
            item_count = sum(
                1 for e in os.scandir(appid_dir)
                if e.is_dir() or e.name.endswith(".bin")
            )
            acf_path = os.path.join(DOWNLOADS_DIR, "steamapps", "workshop", f"appworkshop_{appid}.acf")
            acf_data = _parse_acf_items(acf_path)
            total_size = sum(v["size"] for v in acf_data.values())
            result.append({
                "appid": appid,
                "game_name": get_game_name(appid),
                "item_count": item_count,
                "total_size": total_size,
                "total_size_formatted": format_bytes(total_size),
            })
    except FileNotFoundError:
        pass
    return result


@app.get("/api/workshop/{appid}")
async def workshop_items(appid: str):
    """Return all downloaded workshop items for an appid with full metadata."""
    _require_numeric_id(appid, "App ID")
    content_dir = os.path.join(DOWNLOADS_DIR, "steamapps", "workshop", "content", appid)
    acf_path = os.path.join(DOWNLOADS_DIR, "steamapps", "workshop", f"appworkshop_{appid}.acf")
    acf_data = _parse_acf_items(acf_path)
    game_name = get_game_name(appid)

    # Load API metadata cache — single source for title/description/tags/time_updated
    metadata_file = os.path.join(META_DIR, "games", appid, "metadata.json")
    api_meta: dict = {}
    try:
        with open(metadata_file, "r", encoding="utf-8") as f:
            api_meta = json.load(f)
    except (OSError, ValueError):
        pass

    items = []
    try:
        for entry_name in sorted(os.listdir(content_dir)):
            entry_path = os.path.join(content_dir, entry_name)
            is_dir = os.path.isdir(entry_path)
            is_bin = entry_name.endswith(".bin") and os.path.isfile(entry_path)
            if not is_dir and not is_bin:
                continue

            item_id = entry_name[:-4] if is_bin else entry_name
            meta = api_meta.get(item_id, {})

            # Size: ACF for directories (SteamCMD-tracked), disk for .bin files
            acf_item = acf_data.get(item_id, {})
            size = acf_item.get("size", 0)
            if not size and is_bin:
                size = os.path.getsize(entry_path)

            # Preview: check both the local directory image and the API-fetched cache
            # so existing archives without a metadata run still show thumbnails
            has_preview = (
                (is_dir and os.path.isfile(os.path.join(entry_path, "previewimage.png")))
                or os.path.isfile(os.path.join(META_DIR, "games", appid, "previews", f"{item_id}.jpg"))
            )

            # Build desc_image_map: original URL → local serving URL
            desc_image_map: dict[str, str] = {}
            map_path = os.path.join(META_DIR, "games", appid, "desc_images", item_id, "map.json")
            try:
                with open(map_path, "r", encoding="utf-8") as f:
                    raw_map: dict = json.load(f)
                for orig_url, fname in raw_map.items():
                    local = os.path.join(META_DIR, "games", appid, "desc_images", item_id, fname)
                    if os.path.isfile(local):
                        desc_image_map[orig_url] = (
                            f"/api/workshop/{appid}/desc-images/{item_id}/{fname}"
                        )
            except (OSError, ValueError):
                pass

            items.append({
                "id": item_id,
                "title": meta.get("title") or item_id,
                "description": meta.get("description", ""),
                "tags": meta.get("tags", []),
                "size": size,
                "size_formatted": format_bytes(size),
                "time_updated": meta.get("time_updated", 0),
                "has_preview": has_preview,
                "is_bin": is_bin,
                "desc_image_map": desc_image_map,
                "has_metadata": item_id in api_meta,
            })
    except FileNotFoundError:
        pass

    total_size = sum(i["size"] for i in items)
    return {
        "appid": appid,
        "game_name": game_name,
        "items": items,
        "total_size": total_size,
        "total_size_formatted": format_bytes(total_size),
    }


@app.get("/workshop/{appid}", response_class=HTMLResponse)
async def workshop_page(request: Request, appid: str):
    return templates.TemplateResponse("workshop.html", {"request": request, "appid": appid})


@app.get("/workshop-image/{appid}/{item_id}/{filename}")
async def workshop_image(appid: str, item_id: str, filename: str):
    _require_numeric_id(appid, "App ID")
    _require_numeric_id(item_id, "Item ID")
    if filename not in ("previewimage.png", "loadingimage.png"):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    # Primary: local file inside the downloaded item directory
    path = os.path.join(DOWNLOADS_DIR, "steamapps", "workshop", "content", appid, item_id, filename)
    if os.path.isfile(path):
        return FileResponse(path, media_type="image/png")
    # Fallback: API-fetched preview image (covers .bin items)
    if filename == "previewimage.png":
        preview_path = os.path.join(META_DIR, "games", appid, "previews", f"{item_id}.jpg")
        if os.path.isfile(preview_path):
            return FileResponse(preview_path, media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Image not found.")


_DESC_IMG_RE = re.compile(r'^[0-9a-f]{20}\.(jpg|jpeg|png|gif|webp)$')


@app.get("/api/workshop/{appid}/desc-images/{item_id}/{filename}")
async def serve_desc_image(appid: str, item_id: str, filename: str):
    """Serve a locally-cached description image."""
    _require_numeric_id(appid, "App ID")
    _require_numeric_id(item_id, "Item ID")
    if not _DESC_IMG_RE.match(filename):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    path = os.path.join(META_DIR, "games", appid, "desc_images", item_id, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Image not found.")
    return FileResponse(path)


@app.get("/workshop-download/{appid}/{item_id}")
async def download_item(appid: str, item_id: str):
    """Stream the workshop item as a .zip archive (directories) or raw .bin file."""
    _require_numeric_id(appid, "App ID")
    _require_numeric_id(item_id, "Item ID")
    base = os.path.join(DOWNLOADS_DIR, "steamapps", "workshop", "content", appid)
    item_dir = os.path.join(base, item_id)
    bin_path = os.path.join(base, f"{item_id}.bin")

    # Serve .bin items directly
    if not os.path.isdir(item_dir) and os.path.isfile(bin_path):
        return FileResponse(
            bin_path,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{item_id}.bin"'},
        )

    if not os.path.isdir(item_dir):
        raise HTTPException(status_code=404, detail="Item not found.")

    # Use folder_name from workshop.json as the archive root name
    archive_name = item_id
    try:
        with open(os.path.join(item_dir, "workshop.json"), "r", encoding="utf-8") as f:
            wj = json.load(f)
        archive_name = wj.get("FolderName", item_id) or item_id
    except (OSError, ValueError):
        pass

    def generate():
        read_fd, write_fd = os.pipe()

        def write_zip():
            try:
                with os.fdopen(write_fd, "wb") as out:
                    with zipfile.ZipFile(out, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                        for root, _, files in os.walk(item_dir):
                            for fname in files:
                                full_path = os.path.join(root, fname)
                                arcpath = os.path.join(
                                    archive_name,
                                    os.path.relpath(full_path, item_dir),
                                )
                                zf.write(full_path, arcpath)
            except Exception:
                pass  # Broken pipe or other error — reader will get EOF

        t = threading.Thread(target=write_zip, daemon=True)
        t.start()
        with os.fdopen(read_fd, "rb") as inp:
            while chunk := inp.read(65536):
                yield chunk
        t.join()

    filename = f"{archive_name}_{item_id}.zip"
    return StreamingResponse(
        generate(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/workshop/{appid}")
async def delete_workshop_archive(appid: str):
    """Delete all downloaded files for an appid (content dir + ACF file)."""
    _require_numeric_id(appid, "App ID")
    content_dir = os.path.join(DOWNLOADS_DIR, "steamapps", "workshop", "content", appid)
    acf_path = os.path.join(DOWNLOADS_DIR, "steamapps", "workshop", f"appworkshop_{appid}.acf")
    deleted = False
    if os.path.isdir(content_dir):
        shutil.rmtree(content_dir)
        deleted = True
    if os.path.isfile(acf_path):
        os.unlink(acf_path)
        deleted = True
    if not deleted:
        raise HTTPException(status_code=404, detail="No archive found for this app.")
    return {"status": "deleted"}


@app.delete("/api/workshop/{appid}/items/{item_id}")
async def delete_workshop_item(appid: str, item_id: str):
    """Delete a single downloaded workshop item (directory or .bin file)."""
    _require_numeric_id(appid, "App ID")
    _require_numeric_id(item_id, "Item ID")
    base = os.path.join(DOWNLOADS_DIR, "steamapps", "workshop", "content", appid)
    item_dir = os.path.join(base, item_id)
    bin_path = os.path.join(base, f"{item_id}.bin")
    if os.path.isdir(item_dir):
        shutil.rmtree(item_dir)
        return {"status": "deleted"}
    if os.path.isfile(bin_path):
        os.unlink(bin_path)
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Item not found.")


@app.post("/api/workshop/{appid}/fetch-metadata")
async def fetch_workshop_metadata(appid: str):
    """Trigger a background Steam API metadata fetch for all downloaded items."""
    _require_numeric_id(appid, "App ID")
    content_dir = os.path.join(DOWNLOADS_DIR, "steamapps", "workshop", "content", appid)
    item_ids: list[str] = []
    try:
        for entry in os.scandir(content_dir):
            if entry.is_dir():
                item_ids.append(entry.name)
            elif entry.name.endswith(".bin"):
                item_ids.append(entry.name[:-4])
    except OSError:
        pass
    if not item_ids:
        raise HTTPException(status_code=404, detail="No downloaded items found for this app.")

    def _do_fetch() -> None:
        fetch_and_cache_metadata(appid, item_ids, META_DIR)

    thread = threading.Thread(target=_do_fetch, daemon=True)
    thread.start()
    return {"status": "started", "item_count": len(item_ids)}


# ---------------------------------------------------------------------------
# Saved jobs routes
# ---------------------------------------------------------------------------
@app.get("/api/saved-jobs")
async def list_saved_jobs():
    return _saved_jobs


@app.post("/api/saved-jobs")
async def create_saved_job(req: SavedJobRequest):
    """Create or update the saved job for this appid (one per appid)."""
    err = _validate_schedule(req.schedule_type, req.schedule_value)
    if err:
        raise HTTPException(status_code=422, detail=err)
    with _saved_jobs_lock:
        existing = next((j for j in _saved_jobs if j["appid"] == req.appid.strip()), None)
        now = time.time()
        next_run = _compute_next_run(req.schedule_type, req.schedule_value)
        if existing:
            existing.update({
                "game_name": req.game_name or existing.get("game_name", ""),
                "pages": req.pages,
                "delay_min": req.delay_min,
                "delay_max": req.delay_max,
                "scrape_only": req.scrape_only,
                "download_only": req.download_only,
                "schedule_type": req.schedule_type,
                "schedule_value": req.schedule_value,
                "sort_key": req.sort_key,
                "next_run_at": next_run,
            })
            _save_saved_jobs()
            return existing
        else:
            job: dict = {
                "id": str(uuid.uuid4()),
                "appid": req.appid.strip(),
                "game_name": req.game_name or req.appid.strip(),
                "pages": req.pages,
                "delay_min": req.delay_min,
                "delay_max": req.delay_max,
                "scrape_only": req.scrape_only,
                "download_only": req.download_only,
                "schedule_type": req.schedule_type,
                "schedule_value": req.schedule_value,
                "sort_key": req.sort_key,
                "next_run_at": next_run,
                "last_run_at": None,
                "created_at": now,
            }
            _saved_jobs.append(job)
            _save_saved_jobs()
            return job


@app.put("/api/saved-jobs/{job_id}")
async def update_saved_job(job_id: str, req: SavedJobRequest):
    err = _validate_schedule(req.schedule_type, req.schedule_value)
    if err:
        raise HTTPException(status_code=422, detail=err)
    with _saved_jobs_lock:
        job = next((j for j in _saved_jobs if j["id"] == job_id), None)
        if not job:
            raise HTTPException(status_code=404, detail="Saved job not found.")
        next_run = _compute_next_run(req.schedule_type, req.schedule_value)
        job.update({
            "game_name": req.game_name or job.get("game_name", ""),
            "pages": req.pages,
            "delay_min": req.delay_min,
            "delay_max": req.delay_max,
            "scrape_only": req.scrape_only,
            "download_only": req.download_only,
            "schedule_type": req.schedule_type,
            "schedule_value": req.schedule_value,
            "sort_key": req.sort_key,
            "next_run_at": next_run,
        })
        _save_saved_jobs()
        return job


@app.delete("/api/saved-jobs/{job_id}")
async def delete_saved_job(job_id: str):
    with _saved_jobs_lock:
        before = len(_saved_jobs)
        _saved_jobs[:] = [j for j in _saved_jobs if j["id"] != job_id]
        if len(_saved_jobs) == before:
            raise HTTPException(status_code=404, detail="Saved job not found.")
        _save_saved_jobs()
    return {"status": "deleted"}


@app.post("/api/saved-jobs/{job_id}/run")
async def run_saved_job(job_id: str):
    """Trigger a saved job to run immediately."""
    with _saved_jobs_lock:
        job_data = next((j for j in _saved_jobs if j["id"] == job_id), None)
        if not job_data:
            raise HTTPException(status_code=404, detail="Saved job not found.")
        now = time.time()
        job_data["last_run_at"] = now
        job_data["next_run_at"] = _compute_next_run(
            job_data["schedule_type"], job_data["schedule_value"], from_time=now
        )
        _save_saved_jobs()
        req = StartJobRequest(
            appid=job_data["appid"],
            pages=job_data["pages"],
            delay_min=job_data["delay_min"],
            delay_max=job_data["delay_max"],
            scrape_only=job_data["scrape_only"],
            download_only=job_data["download_only"],
            sort_key=job_data.get("sort_key", "trend_week"),
        )
        saved_job_id = job_data["id"]
    if not _launch_job(req, saved_job_id=saved_job_id):
        raise HTTPException(status_code=409, detail="A job is already running.")
    return {"status": "started", "appid": req.appid}
