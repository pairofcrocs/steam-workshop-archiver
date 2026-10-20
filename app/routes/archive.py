"""Archive browsing, item detail, images, downloads and deletes."""

import os
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from .. import db, names
from ..core import archive, metadata
from ..core.steam_api import is_id
from ..events import bus
from ..jobs import manager
from ..models import ItemIds

router = APIRouter()

SORTS = ("updated_desc", "updated_asc", "title", "size_desc", "size_asc", "downloaded_desc")
FILTERS = ("", "removed", "no_metadata", "outdated")
MAX_ZIP_ITEMS = 500
_IMG_CACHE = {"Cache-Control": "private, max-age=86400"}


def _check_id(value: str, label: str = "ID") -> None:
    if not is_id(value):
        raise HTTPException(400, f"Invalid {label}: must be numeric.")


def _content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _ensure_idle(appid: str) -> None:
    if manager.active_appid() == appid:
        raise HTTPException(409, "A run for this game is in progress — cancel it or wait for it to finish.")


def _rows(appid: str) -> tuple[dict, dict, dict]:
    """(entries on disk, item rows, acf) for an app."""
    entries = archive.scan(appid)
    rows = metadata.get_items(appid, entries.keys()) if entries else {}
    return entries, rows, archive.parse_acf(appid)


def _size(iid: str, row: dict, acf: dict) -> int:
    return acf.get(iid, {}).get("size") or row.get("disk_size") or row.get("file_size") or 0


def _local_time(iid: str, row: dict, acf: dict) -> int:
    return row.get("downloaded_time_updated") or acf.get(iid, {}).get("timeupdated", 0)


def _outdated(iid: str, row: dict, acf: dict) -> bool:
    t = row.get("time_updated", 0)
    return bool(t) and not row.get("removed") and t > _local_time(iid, row, acf)


_BBCODE_TAG_RE = re.compile(r"\[/?[a-zA-Z0-9*]+(?:=[^\]]*)?\]")
_BBCODE_MEDIA_RE = re.compile(r"\[(img|previewyoutube|video)\b[^\]]*\].*?\[/\1\]", re.IGNORECASE | re.DOTALL)
_URL_RE = re.compile(r"https?://\S+")


def _excerpt(description: str, length: int = 160) -> str:
    text = _BBCODE_MEDIA_RE.sub(" ", description or "")
    text = " ".join(_URL_RE.sub(" ", _BBCODE_TAG_RE.sub(" ", text)).split())
    return text if len(text) <= length else text[: length - 1].rstrip() + "…"


def _has_preview(appid: str, entry: archive.Entry) -> bool:
    return os.path.isfile(metadata.preview_path(appid, entry.item_id)) or (
        not entry.is_bin and os.path.isfile(os.path.join(entry.path, "previewimage.png"))
    )


@router.get("/api/workshop")
def overview():
    games = []
    last_runs = {
        r["appid"]: {"id": r["id"], "status": r["status"], "finished_at": r["finished_at"]}
        for r in db.query(
            "SELECT id, appid, status, finished_at FROM runs WHERE id IN "
            "(SELECT MAX(id) FROM runs WHERE finished_at IS NOT NULL GROUP BY appid)"
        )
    }
    jobs: dict[str, list[str]] = {}
    for r in db.query("SELECT id, appid FROM saved_jobs"):
        jobs.setdefault(r["appid"], []).append(r["id"])
    for appid in archive.list_appids():
        entries, rows, acf = _rows(appid)
        games.append({
            "appid": appid,
            "game_name": names.get(appid),
            "item_count": len(entries),
            "total_size": sum(_size(i, rows.get(i, {}), acf) for i in entries),
            "removed_count": sum(1 for i in entries if rows.get(i, {}).get("removed")),
            "last_run": last_runs.get(appid),
            "saved_job_ids": jobs.get(appid, []),
        })
    games.sort(key=lambda g: g["game_name"].lower())
    return {
        "games": games,
        "total_items": sum(g["item_count"] for g in games),
        "total_size": sum(g["total_size"] for g in games),
        "disk": archive.disk_usage(),
    }


@router.get("/api/workshop/{appid}")
def game_summary(appid: str):
    _check_id(appid, "App ID")
    entries, rows, acf = _rows(appid)
    tags: dict[str, int] = {}
    for i in entries:
        for t in rows.get(i, {}).get("tags", []):
            tags[t] = tags.get(t, 0) + 1
    return {
        "appid": appid,
        "game_name": names.get(appid),
        "item_count": len(entries),
        "total_size": sum(_size(i, rows.get(i, {}), acf) for i in entries),
        "removed_count": sum(1 for i in entries if rows.get(i, {}).get("removed")),
        "no_metadata_count": sum(1 for i in entries if not rows.get(i, {}).get("fetched_at")),
        "outdated_count": sum(1 for i in entries if _outdated(i, rows.get(i, {}), acf)),
        "tags": [{"tag": t, "count": c} for t, c in sorted(tags.items(), key=lambda kv: (-kv[1], kv[0].lower()))],
    }


@router.get("/api/workshop/{appid}/items")
def list_items(
    appid: str, q: str = "", tags: str = "", tag_mode: str = "all", sort: str = "updated_desc",
    filter: str = "", offset: int = 0, limit: int = 60,
):
    _check_id(appid, "App ID")
    if sort not in SORTS:
        raise HTTPException(400, f"sort must be one of {', '.join(SORTS)}")
    if filter not in FILTERS:
        raise HTTPException(400, f"filter must be one of {', '.join(f for f in FILTERS if f)}")
    limit = max(1, min(limit, 500))
    offset = max(0, min(offset, 10**9))
    entries, rows, acf = _rows(appid)
    wanted_tags = {t.strip().lower() for t in tags.split(",") if t.strip()}
    needle = q.strip().lower()

    out = []
    for iid, entry in entries.items():
        row = rows.get(iid, {})
        title = row.get("title") or iid
        if needle and needle not in title.lower() and needle not in iid:
            continue
        item_tags = row.get("tags", [])
        if wanted_tags:
            have = {t.lower() for t in item_tags}
            if (tag_mode == "any" and not wanted_tags & have) or (tag_mode != "any" and not wanted_tags <= have):
                continue
        outdated = _outdated(iid, row, acf)
        if (filter == "removed" and not row.get("removed")) or \
           (filter == "no_metadata" and row.get("fetched_at")) or \
           (filter == "outdated" and not outdated):
            continue
        out.append({
            "id": iid, "title": title, "tags": item_tags, "size": _size(iid, row, acf),
            "time_updated": row.get("time_updated", 0), "downloaded_at": row.get("downloaded_at", 0),
            "is_bin": entry.is_bin, "removed": bool(row.get("removed")),
            "has_metadata": bool(row.get("fetched_at")), "outdated": outdated,
        })

    keys = {
        "updated_desc": (lambda i: i["time_updated"], True),
        "updated_asc": (lambda i: i["time_updated"], False),
        "title": (lambda i: i["title"].casefold(), False),
        "size_desc": (lambda i: i["size"], True),
        "size_asc": (lambda i: i["size"], False),
        "downloaded_desc": (lambda i: i["downloaded_at"], True),
    }
    key, reverse = keys[sort]
    out.sort(key=key, reverse=reverse)
    page = out[offset:offset + limit]
    descs = metadata.get_items(appid, [i["id"] for i in page], with_description=True)
    for item in page:
        item["has_preview"] = _has_preview(appid, entries[item["id"]])
        item["excerpt"] = _excerpt(descs.get(item["id"], {}).get("description", ""))
    return {"total": len(out), "offset": offset, "limit": limit, "items": page}


@router.get("/api/workshop/{appid}/items/{item_id}")
def item_detail(appid: str, item_id: str):
    _check_id(appid, "App ID")
    _check_id(item_id, "Item ID")
    entry = archive.scan(appid).get(item_id)
    if not entry:
        raise HTTPException(404, "Item not found.")
    row = metadata.get_items(appid, [item_id], with_description=True).get(item_id, {})
    acf = archive.parse_acf(appid)
    return {
        "id": item_id, "title": row.get("title") or item_id, "tags": row.get("tags", []),
        "size": _size(item_id, row, acf), "time_updated": row.get("time_updated", 0),
        "downloaded_at": row.get("downloaded_at", 0), "has_preview": _has_preview(appid, entry),
        "is_bin": entry.is_bin, "removed": bool(row.get("removed")),
        "has_metadata": bool(row.get("fetched_at")), "outdated": _outdated(item_id, row, acf),
        "description": row.get("description", ""), "desc_image_map": metadata.desc_image_map(appid, item_id),
        "creator": row.get("creator", ""), "time_created": row.get("time_created", 0),
        "file_size": row.get("file_size", 0),
        "steam_url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}",
    }


@router.delete("/api/workshop/{appid}")
def delete_game(appid: str, purge_metadata: int = 0, delete_saved_jobs: int = 0):
    _check_id(appid, "App ID")
    _ensure_idle(appid)
    if not archive.delete_app(appid, bool(purge_metadata)):
        raise HTTPException(404, "No archive found for this app.")
    if purge_metadata:
        metadata.purge_app(appid)
    else:
        metadata.forget_downloads(appid)
    if delete_saved_jobs:
        with db.transaction() as c:
            c.execute("DELETE FROM saved_jobs WHERE appid = ?", (appid,))
        bus.publish("saved_jobs_changed")
    bus.publish("archive_changed", appid=appid)
    return {"status": "deleted"}


@router.delete("/api/workshop/{appid}/items/{item_id}")
def delete_item(appid: str, item_id: str):
    _check_id(appid, "App ID")
    _check_id(item_id, "Item ID")
    _ensure_idle(appid)
    if not archive.delete_items(appid, [item_id]):
        raise HTTPException(404, "Item not found.")
    metadata.forget_downloads(appid, [item_id])
    bus.publish("archive_changed", appid=appid)
    return {"status": "deleted"}


@router.post("/api/workshop/{appid}/items/delete")
def delete_items(appid: str, body: ItemIds):
    _check_id(appid, "App ID")
    _ensure_idle(appid)
    deleted = archive.delete_items(appid, body.ids)
    metadata.forget_downloads(appid, deleted)
    if deleted:
        bus.publish("archive_changed", appid=appid)
    return {"deleted": len(deleted)}


@router.post("/api/workshop/{appid}/refresh-metadata")
def refresh_metadata(appid: str):
    _check_id(appid, "App ID")
    if not archive.scan(appid):
        raise HTTPException(404, "No downloaded items found for this app.")
    return manager.enqueue({"kind": "metadata", "appid": appid, "source_type": "workshop"})


@router.get("/workshop-download/{appid}/{item_id}")
def download_item(appid: str, item_id: str):
    _check_id(appid, "App ID")
    _check_id(item_id, "Item ID")
    entry = archive.scan(appid).get(item_id)
    if not entry:
        raise HTTPException(404, "Item not found.")
    if entry.is_bin:
        return FileResponse(
            entry.path, media_type="application/octet-stream",
            headers={"Content-Disposition": _content_disposition(f"{item_id}.bin")},
        )
    root = archive.folder_name(entry)
    return StreamingResponse(
        archive.stream_zip([entry], [root]), media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(f"{root}_{item_id}.zip")},
    )


@router.get("/api/workshop/{appid}/zip")
def download_many(appid: str, ids: str):
    _check_id(appid, "App ID")
    wanted = list(dict.fromkeys(i for i in ids.split(",") if i))
    if not wanted or len(wanted) > MAX_ZIP_ITEMS or not all(is_id(i) for i in wanted):
        raise HTTPException(400, f"ids must be 1–{MAX_ZIP_ITEMS} comma-separated item ids.")
    entries = archive.scan(appid)
    chosen = [entries[i] for i in wanted if i in entries]
    if not chosen:
        raise HTTPException(404, "None of those items are downloaded.")
    roots, used = [], set()
    for e in chosen:
        root = archive.folder_name(e)
        if root in used:
            root = f"{root}_{e.item_id}"
        used.add(root)
        roots.append(root)
    game = archive.safe_name(names.get(appid, fetch=False), appid)
    return StreamingResponse(
        archive.stream_zip(chosen, roots), media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(f"{game}_{len(chosen)}_items.zip")},
    )


@router.get("/workshop-image/{appid}/{item_id}")
@router.get("/workshop-image/{appid}/{item_id}/{filename}")
def preview_image(appid: str, item_id: str, filename: str = "previewimage.png"):
    _check_id(appid, "App ID")
    _check_id(item_id, "Item ID")
    if filename not in ("previewimage.png", "loadingimage.png"):
        raise HTTPException(400, "Invalid filename.")
    cached = metadata.preview_path(appid, item_id)
    if filename == "previewimage.png" and os.path.isfile(cached):
        return FileResponse(cached, media_type="image/jpeg", headers=_IMG_CACHE)
    local = os.path.join(archive.content_dir(appid), item_id, filename)
    if os.path.isfile(local):
        return FileResponse(local, media_type="image/png", headers=_IMG_CACHE)
    raise HTTPException(404, "Image not found.")


@router.get("/api/workshop/{appid}/desc-images/{item_id}/{filename}")
def desc_image(appid: str, item_id: str, filename: str):
    _check_id(appid, "App ID")
    _check_id(item_id, "Item ID")
    if not metadata.DESC_IMAGE_RE.match(filename):
        raise HTTPException(400, "Invalid filename.")
    path = os.path.join(metadata.desc_dir(appid, item_id), filename)
    if not os.path.isfile(path):
        raise HTTPException(404, "Image not found.")
    return FileResponse(path, headers=_IMG_CACHE)
