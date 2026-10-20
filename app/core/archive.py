"""On-disk archive layout: scanning, sizes, ACF manifests, deletes and ZIP streaming."""

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import unicodedata
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from .. import config

log = logging.getLogger(__name__)

_acf_lock = threading.Lock()


@dataclass
class Entry:
    item_id: str
    path: str
    is_bin: bool


def workshop_root() -> str:
    return os.path.join(config.DOWNLOADS_DIR, "steamapps", "workshop")


def content_dir(appid: str) -> str:
    return os.path.join(workshop_root(), "content", appid)


def acf_path(appid: str) -> str:
    return os.path.join(workshop_root(), f"appworkshop_{appid}.acf")


def game_meta_dir(appid: str) -> str:
    return os.path.join(config.META_DIR, "games", appid)


def list_appids() -> list[str]:
    base = os.path.join(workshop_root(), "content")
    try:
        return sorted(
            e.name for e in os.scandir(base) if e.is_dir() and e.name.isdigit() and e.name.isascii()
        )
    except FileNotFoundError:
        return []


def scan(appid: str) -> dict[str, Entry]:
    """Downloaded items for an app: ``{item_id: Entry}``."""
    out: dict[str, Entry] = {}
    try:
        it = os.scandir(content_dir(appid))
    except FileNotFoundError:
        return out
    with it:
        for e in it:
            name = e.name
            if e.is_dir() and name.isdigit() and name.isascii():
                out[name] = Entry(name, e.path, False)
            elif name.endswith(".bin") and name[:-4].isdigit() and e.is_file():
                out[name[:-4]] = Entry(name[:-4], e.path, True)
    return out


def entry(appid: str, item_id: str) -> Entry | None:
    """The on-disk entry for one item, without scanning the whole app folder."""
    base = content_dir(appid)
    path = os.path.join(base, item_id)
    if os.path.isdir(path):
        return Entry(item_id, path, False)
    if os.path.isfile(path + ".bin"):
        return Entry(item_id, path + ".bin", True)
    return None


def item_disk_size(entry: Entry) -> int:
    if entry.is_bin:
        try:
            return os.path.getsize(entry.path)
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(entry.path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def disk_usage() -> dict:
    path = config.DOWNLOADS_DIR
    try:
        u = shutil.disk_usage(path)
        return {"path": path, "total": u.total, "used": u.used, "free": u.free}
    except OSError:
        return {"path": path, "total": 0, "used": 0, "free": 0}


# ---------------------------------------------------------------------------
# ACF (SteamCMD's appworkshop manifest)
# ---------------------------------------------------------------------------
_ACF_ITEM_RE = re.compile(r'"(\d+)"\s*\{([^{}]*)\}')
_ACF_SIZE_RE = re.compile(r'"size"\s+"(\d+)"')
_ACF_TIME_RE = re.compile(r'"timeupdated"\s+"(\d+)"')


def _vdf_block(text: str, key: str) -> str:
    idx = text.find(f'"{key}"')
    if idx == -1:
        return ""
    brace = text.find("{", idx)
    if brace == -1:
        return ""
    depth = 0
    for pos in range(brace, len(text)):
        if text[pos] == "{":
            depth += 1
        elif text[pos] == "}":
            depth -= 1
            if depth == 0:
                return text[brace + 1:pos]
    return ""


def parse_acf_text(text: str) -> dict[str, dict]:
    section = _vdf_block(text, "WorkshopItemsInstalled")
    out: dict[str, dict] = {}
    for m in _ACF_ITEM_RE.finditer(section):
        size_m = _ACF_SIZE_RE.search(m.group(2))
        time_m = _ACF_TIME_RE.search(m.group(2))
        out[m.group(1)] = {
            "size": int(size_m.group(1)) if size_m else 0,
            "timeupdated": int(time_m.group(1)) if time_m else 0,
        }
    return out


def parse_acf(appid: str) -> dict[str, dict]:
    """``{item_id: {size, timeupdated}}`` from the app's ACF, or {}."""
    try:
        with open(acf_path(appid), encoding="utf-8", errors="replace") as f:
            return parse_acf_text(f.read())
    except OSError:
        return {}


def remove_from_acf(appid: str, item_ids: Iterable[str]) -> None:
    """Drop item blocks from the ACF so SteamCMD and size totals forget them."""
    ids = set(item_ids)
    path = acf_path(appid)
    with _acf_lock:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return
        new = re.sub(
            r'\s*"(\d+)"\s*\{[^{}]*\}',
            lambda m: "" if m.group(1) in ids else m.group(0),
            text,
        )
        if new != text:
            _atomic_write_text(path, new)


def _atomic_write_text(path: str, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Deletes
# ---------------------------------------------------------------------------
def delete_items(appid: str, item_ids: Iterable[str]) -> list[str]:
    entries = scan(appid)
    deleted = []
    for iid in item_ids:
        entry = entries.get(iid)
        if not entry:
            continue
        if entry.is_bin:
            os.unlink(entry.path)
        else:
            shutil.rmtree(entry.path)
        deleted.append(iid)
    if deleted:
        remove_from_acf(appid, deleted)
    return deleted


def delete_app(appid: str, purge_metadata: bool) -> bool:
    deleted = False
    if os.path.isdir(content_dir(appid)):
        shutil.rmtree(content_dir(appid))
        deleted = True
    if os.path.isfile(acf_path(appid)):
        os.unlink(acf_path(appid))
        deleted = True
    if purge_metadata and os.path.isdir(game_meta_dir(appid)):
        shutil.rmtree(game_meta_dir(appid))
        deleted = True
    return deleted


# ---------------------------------------------------------------------------
# ZIP streaming
# ---------------------------------------------------------------------------
_SKIP_DIRS = {".DepotDownloader"}


def safe_name(name: str, fallback: str) -> str:
    """A single path component safe for ZIP entries and download filenames."""
    name = unicodedata.normalize("NFC", str(name or ""))
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", name).strip(" .")
    name = name.replace("..", "_")
    return name[:120] or fallback


def folder_name(entry: Entry) -> str:
    """Archive root name: workshop.json's FolderName when present."""
    if entry.is_bin:
        return entry.item_id
    try:
        with open(os.path.join(entry.path, "workshop.json"), encoding="utf-8") as f:
            name = json.load(f).get("FolderName")
        return safe_name(name, entry.item_id) if name else entry.item_id
    except (OSError, ValueError, AttributeError):
        return entry.item_id


def _zip_members(entry: Entry, root: str) -> Iterator[tuple[str, str]]:
    if entry.is_bin:
        yield entry.path, f"{root}/{entry.item_id}.bin"
        return
    for dirpath, dirnames, files in os.walk(entry.path):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for f in files:
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, entry.path).replace(os.sep, "/")
            yield full, f"{root}/{rel}"


def stream_zip(entries: list[Entry], roots: list[str]) -> Iterator[bytes]:
    """Stream a ZIP (stored, no compression — game assets rarely compress)."""
    read_fd, write_fd = os.pipe()

    def writer():
        try:
            with os.fdopen(write_fd, "wb") as out:
                with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
                    for entry, root in zip(entries, roots):
                        for full, arc in _zip_members(entry, root):
                            zf.write(full, arc)
        except BrokenPipeError:
            pass
        except Exception:
            log.exception("ZIP streaming failed")

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    with os.fdopen(read_fd, "rb") as inp:
        while chunk := inp.read(256 * 1024):
            yield chunk
    t.join()
