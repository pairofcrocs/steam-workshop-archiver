"""Workshop item metadata (SQLite) plus cached preview / description images."""

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from urllib.parse import urlparse

from .. import db
from . import archive, steam_api
from .net import safe_download

log = logging.getLogger(__name__)

LogFn = Callable[[str], None]

_image_locks: dict[str, threading.Lock] = {}
_image_locks_guard = threading.Lock()

_IMG_BBCODE_RE = re.compile(r"\[img\]\s*(https?://[^\s\[\]]+?)\s*\[/img\]", re.IGNORECASE)
_SAFE_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
DESC_IMAGE_RE = re.compile(r"^[0-9a-f]{20}\.(jpg|jpeg|png|gif|webp)$")

_ITEM_COLUMNS = (
    "title", "description", "tags", "creator", "file_size", "file_url", "filename",
    "preview_url", "time_created", "time_updated", "result", "removed", "fetched_at",
)


def _lock_for(appid: str) -> threading.Lock:
    with _image_locks_guard:
        return _image_locks.setdefault(appid, threading.Lock())


# ---------------------------------------------------------------------------
# DB access
# ---------------------------------------------------------------------------
def upsert_details(appid: str, details: Iterable[dict], fetched_at: float | None = None) -> None:
    """Store normalized details. Removed/private items (result != 1) only flip the
    ``removed`` flag so previously fetched titles and descriptions survive."""
    now = fetched_at or time.time()
    ok_rows, gone_rows = [], []
    for d in details:
        if d.get("result", 1) == 1:
            ok_rows.append((
                appid, d["item_id"], d["title"], d["description"], json.dumps(d["tags"]),
                d["creator"], d["file_size"], d["file_url"], d["filename"], d["preview_url"],
                d["time_created"], d["time_updated"], 1, 1 if d.get("banned") else 0, now,
            ))
        else:
            gone_rows.append((appid, d["item_id"], d.get("result", 0), now))
    with db.transaction() as c:
        if ok_rows:
            c.executemany(
                f"INSERT INTO items(appid, item_id, {', '.join(_ITEM_COLUMNS)}) "
                f"VALUES({', '.join('?' * (len(_ITEM_COLUMNS) + 2))}) "
                "ON CONFLICT(appid, item_id) DO UPDATE SET "
                + ", ".join(f"{col} = excluded.{col}" for col in _ITEM_COLUMNS),
                ok_rows,
            )
        if gone_rows:
            c.executemany(
                "INSERT INTO items(appid, item_id, result, removed, fetched_at) VALUES(?, ?, ?, 1, ?) "
                "ON CONFLICT(appid, item_id) DO UPDATE SET result = excluded.result, removed = 1, "
                "fetched_at = excluded.fetched_at",
                gone_rows,
            )


def get_items(appid: str, ids: Iterable[str] | None = None, with_description: bool = False) -> dict[str, dict]:
    cols = "*" if with_description else (
        "item_id, title, tags, creator, file_size, file_url, filename, preview_url, time_created, "
        "time_updated, result, removed, fetched_at, downloaded_at, downloaded_time_updated, disk_size"
    )
    if ids is None:
        rows = db.query(f"SELECT {cols} FROM items WHERE appid = ?", (appid,))
    else:
        ids = list(ids)
        rows = []
        for start in range(0, len(ids), 900):
            chunk = ids[start:start + 900]
            rows += db.query(
                f"SELECT {cols} FROM items WHERE appid = ? AND item_id IN ({','.join('?' * len(chunk))})",
                (appid, *chunk),
            )
    out = {}
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d.get("tags") or "[]")
        out[d["item_id"]] = d
    return out


def mark_downloaded(appid: str, item_id: str, time_updated: int, disk_size: int) -> None:
    with db.transaction() as c:
        c.execute(
            "INSERT INTO items(appid, item_id, downloaded_at, downloaded_time_updated, disk_size) "
            "VALUES(?, ?, ?, ?, ?) ON CONFLICT(appid, item_id) DO UPDATE SET "
            "downloaded_at = excluded.downloaded_at, "
            "downloaded_time_updated = excluded.downloaded_time_updated, disk_size = excluded.disk_size",
            (appid, item_id, time.time(), time_updated, disk_size),
        )


def forget_downloads(appid: str, item_ids: Iterable[str] | None = None) -> None:
    with db.transaction() as c:
        if item_ids is None:
            c.execute(
                "UPDATE items SET downloaded_at = 0, downloaded_time_updated = 0, disk_size = 0 WHERE appid = ?",
                (appid,),
            )
        else:
            c.executemany(
                "UPDATE items SET downloaded_at = 0, downloaded_time_updated = 0, disk_size = 0 "
                "WHERE appid = ? AND item_id = ?",
                [(appid, i) for i in item_ids],
            )


def purge_app(appid: str) -> None:
    with db.transaction() as c:
        c.execute("DELETE FROM items WHERE appid = ?", (appid,))


# ---------------------------------------------------------------------------
# Refresh from Steam
# ---------------------------------------------------------------------------
def refresh(
    appid: str,
    item_ids: list[str],
    ttl_hours: float,
    force: bool = False,
    log_fn: LogFn | None = None,
    cancel_check=None,
) -> dict[str, dict]:
    """Fetch details for items that are missing or older than *ttl_hours*."""
    log_fn = log_fn or (lambda _m: None)
    existing = get_items(appid, item_ids)
    cutoff = time.time() - ttl_hours * 3600
    stale = [i for i in item_ids if force or i not in existing or existing[i]["fetched_at"] < cutoff]
    if stale:
        log_fn(f"Fetching Workshop metadata for {len(stale)} item{'s' if len(stale) != 1 else ''}...")
        details = steam_api.get_published_file_details(stale, log_fn=log_fn, cancel_check=cancel_check)
        upsert_details(appid, details.values())
        missing = len(stale) - len(details)
        if missing:
            log_fn(f"WARNING: Steam returned no data for {missing} item(s); will retry next run.")
    else:
        log_fn(f"Metadata is fresh for all {len(item_ids)} items.")
    return get_items(appid, item_ids)


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
def preview_path(appid: str, item_id: str) -> str:
    return os.path.join(archive.game_meta_dir(appid), "previews", f"{item_id}.jpg")


def desc_dir(appid: str, item_id: str) -> str:
    return os.path.join(archive.game_meta_dir(appid), "desc_images", item_id)


def desc_filename(url: str) -> str:
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext not in _SAFE_IMG_EXTS:
        ext = ".jpg"
    return hashlib.sha256(url.encode()).hexdigest()[:20] + ext


def desc_image_map(appid: str, item_id: str) -> dict[str, str]:
    """{original_url: local serving URL} for cached description images."""
    d = desc_dir(appid, item_id)
    try:
        with open(os.path.join(d, "map.json"), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    return {
        url: f"/api/workshop/{appid}/desc-images/{item_id}/{fname}"
        for url, fname in raw.items()
        if isinstance(fname, str) and DESC_IMAGE_RE.match(fname) and os.path.isfile(os.path.join(d, fname))
    }


def download_images(
    appid: str,
    item_ids: list[str],
    log_fn: LogFn | None = None,
    cancel_check=None,
) -> None:
    """Cache preview images and description images for downloaded items."""
    log_fn = log_fn or (lambda _m: None)
    items = get_items(appid, item_ids, with_description=True)
    with _lock_for(appid):
        previews = [
            (iid, it["preview_url"]) for iid, it in items.items()
            if it.get("preview_url") and not os.path.isfile(preview_path(appid, iid))
        ]
        if previews:
            log_fn(f"Caching {len(previews)} preview image(s)...")
        ok = 0
        for iid, url in previews:
            if cancel_check and cancel_check():
                return
            try:
                safe_download(url, preview_path(appid, iid), expect_image=True)
                ok += 1
            except Exception as exc:  # one bad image must never fail the run
                log.debug("preview %s failed: %s", iid, exc)
        if previews:
            log_fn(f"Preview images: {ok}/{len(previews)} cached.")

        pending = 0
        ok = 0
        for iid, it in items.items():
            urls = list(dict.fromkeys(_IMG_BBCODE_RE.findall(it.get("description") or "")))
            if not urls:
                continue
            d = desc_dir(appid, iid)
            map_path = os.path.join(d, "map.json")
            try:
                with open(map_path, encoding="utf-8") as f:
                    url_map = json.load(f)
            except (OSError, ValueError):
                url_map = {}
            changed = False
            for url in urls[:50]:
                if cancel_check and cancel_check():
                    break
                fname = desc_filename(url)
                dest = os.path.join(d, fname)
                if os.path.isfile(dest):
                    if url_map.get(url) != fname:
                        url_map[url] = fname
                        changed = True
                    continue
                pending += 1
                try:
                    safe_download(url, dest, expect_image=True, allow_public=True)
                    url_map[url] = fname
                    changed = True
                    ok += 1
                except Exception as exc:
                    log.debug("desc image %s failed: %s", url, exc)
            if changed:
                os.makedirs(d, exist_ok=True)
                archive._atomic_write_text(map_path, json.dumps(url_map))
        if pending:
            log_fn(f"Description images: {ok}/{pending} cached.")


# ---------------------------------------------------------------------------
# Legacy import (v1 metadata.json)
# ---------------------------------------------------------------------------
def import_legacy(meta_dir: str) -> int:
    """Import v1 ``games/*/metadata.json`` caches into SQLite (once)."""
    imported = 0
    base = os.path.join(meta_dir, "games")
    try:
        appids = [e.name for e in os.scandir(base) if e.is_dir() and e.name.isdigit()]
    except FileNotFoundError:
        return 0
    for appid in appids:
        path = os.path.join(base, appid, "metadata.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            log.warning("Skipping unreadable %s", path)
            continue
        details = []
        for iid, m in data.items():
            if not isinstance(m, dict) or not iid.isdigit():
                continue
            details.append({
                "item_id": iid, "result": 1, "title": m.get("title", "") or "",
                "description": m.get("description", "") or "", "tags": m.get("tags", []) or [],
                "creator": str(m.get("creator", "") or ""), "file_size": int(m.get("file_size", 0) or 0),
                "file_url": "", "filename": "", "preview_url": m.get("preview_url", "") or "",
                "time_created": 0, "time_updated": int(m.get("time_updated", 0) or 0),
            })
        # fetched_at=1 marks them stale so the next run refreshes them.
        upsert_details(appid, details, fetched_at=1)
        os.replace(path, path + ".imported")
        imported += len(details)
    return imported
