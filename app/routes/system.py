"""System, settings, search, input resolution, schedules and the live event stream."""

import asyncio
import json
import os
import time
from collections import OrderedDict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .. import __version__, config, names, notify, schedule
from ..core import archive, steam_api
from ..events import bus
from ..jobs import manager
from ..models import ResolveInput, SchedulePreview

router = APIRouter()

_search_cache: "OrderedDict[str, tuple[float, list]]" = OrderedDict()
_SEARCH_TTL = 300
_SEARCH_MAX = 256


@router.get("/healthz")
def healthz():
    return {"ok": True}


@router.get("/api/system")
def system():
    return {
        "version": __version__,
        "tz": os.environ.get("TZ") or time.tzname[0],
        "auth_enabled": bool(config.AUTH_PASSWORD),
        "backends": {
            "steamcmd": {"available": config.steamcmd_available(), "path": config.STEAMCMD_PATH},
            "depotdownloader": {"available": config.depotdownloader_available(), "path": config.DEPOTDOWNLOADER_PATH},
        },
        "disk": archive.disk_usage(),
    }


@router.get("/api/settings")
def get_settings():
    return config.public_settings()


@router.put("/api/settings")
def put_settings(changes: dict):
    changes.pop("steam_api_key_set", None)
    try:
        return config.update_settings(changes)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/settings/test-notification")
def test_notification():
    urls = config.get_settings().get("notify_urls", [])
    try:
        notify.send(urls, "Steam Workshop Archiver", "Test notification — it works!")
    except notify.NotifyError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@router.get("/api/search")
def search(q: str = ""):
    key = q.strip().lower()[:100]
    if not key:
        return []
    cached = _search_cache.get(key)
    if cached and time.time() - cached[0] < _SEARCH_TTL:
        _search_cache.move_to_end(key)
        return cached[1]
    try:
        results = steam_api.search_workshop_games(key)
    except steam_api.SteamError as exc:
        raise HTTPException(502, "Steam store search is unavailable right now.") from exc
    for r in results:
        names.remember(r["appid"], r["name"])
    _search_cache[key] = (time.time(), results)
    while len(_search_cache) > _SEARCH_MAX:
        _search_cache.popitem(last=False)
    return results


@router.post("/api/resolve-input")
def resolve_input(body: ResolveInput):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Nothing to resolve.")
    is_item_url = "filedetails" in text or "sharedfiles" in text
    appid = None if is_item_url else steam_api.parse_app_ref(text)
    if appid and ("store.steampowered.com" in text or "/app/" in text or "appid=" in text or len(text) <= 7):
        return {"kind": "app", "appid": appid, "name": names.get(appid)}

    iid = steam_api.extract_workshop_id(text)
    if iid:
        details = steam_api.get_published_file_details([iid]).get(iid)
        if details and details["result"] == 1:
            app = details["appid"]
            game = names.get(app) if app else ""
            if details["is_collection"]:
                try:
                    count = len(steam_api.expand_collection(iid))
                except steam_api.SteamError:
                    count = 0
                return {"kind": "collection", "appid": app, "name": game, "collection_id": iid,
                        "title": details["title"], "item_count": count}
            return {"kind": "item", "appid": app, "name": game, "item_id": iid, "title": details["title"]}
        if appid:
            return {"kind": "app", "appid": appid, "name": names.get(appid)}
        raise HTTPException(404, "No public Workshop item or collection with that id.")
    raise HTTPException(400, "Paste an App ID, a store/Workshop URL, an item URL or a collection URL.")


@router.post("/api/schedule/preview")
def schedule_preview(body: SchedulePreview):
    err = schedule.validate(body.schedule_type, body.schedule_value)
    if err:
        return {"valid": False, "error": err, "description": "", "next_runs": []}
    return {
        "valid": True,
        "error": "",
        "description": schedule.describe(body.schedule_type, body.schedule_value),
        "next_runs": schedule.upcoming(body.schedule_type, body.schedule_value),
    }


def _snapshot() -> dict:
    cur = manager.current
    return {
        "type": "snapshot",
        "current": cur.public() if cur else None,
        "queue": manager.queue(),
        "log": list(cur.tail) if cur else [],
        "confirm": cur.confirm_info if cur else None,
    }


def _sse(seq: int, payload: dict) -> str:
    return f"id: {seq}\ndata: {json.dumps(payload)}\n\n"


@router.get("/api/events")
async def events(request: Request):
    last = request.headers.get("last-event-id", "")

    async def gen():
        seq = int(last) if last.isdigit() else -1
        if seq >= 0:
            pending, ok = bus.since(seq)
        else:
            ok = False
        if not ok:
            seq = bus.seq
            yield "retry: 3000\n" + _sse(seq, _snapshot())
        last_ping = time.monotonic()
        while True:
            if await request.is_disconnected():
                return
            pending, ok = bus.since(seq)
            if not ok:
                seq = bus.seq
                yield _sse(seq, _snapshot())
                continue
            for s, payload in pending:
                seq = s
                yield _sse(s, payload)
            if time.monotonic() - last_ping > 15:
                last_ping = time.monotonic()
                yield ": ping\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
