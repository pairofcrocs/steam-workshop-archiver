"""Shared utilities: HTTP session, helpers, HTML/CSV writers."""

import csv
import hashlib
import json
import os
import re
import tempfile
import threading
from urllib.parse import urlparse, parse_qs

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Workshop URL builder
# ---------------------------------------------------------------------------

# Maps sort_key -> (browsesort value, days value or None)
# "random" uses all-time trend as the base; results are shuffled in the scraper.
_SORT_PARAMS: dict[str, tuple[str, int | None]] = {
    "trend_today":            ("trend", 1),
    "trend_week":             ("trend", 7),
    "trend_month":            ("trend", 30),
    "trend_3months":          ("trend", 90),
    "trend_6months":          ("trend", 180),
    "trend_year":             ("trend", 365),
    "trend_alltime":          ("trend", -1),
    "mostrecent":             ("mostrecent", None),
    "lastupdated":            ("lastupdated", None),
    "totaluniquesubscribers": ("totaluniquesubscribers", None),
    "random":                 ("trend", -1),
}


def build_workshop_url(appid: str, page: int, sort_key: str = "trend_week") -> str:
    """Build a Steam Workshop browse URL for the given app, page, and sort."""
    browsesort, days = _SORT_PARAMS.get(sort_key, ("trend", 7))
    url = (
        "https://steamcommunity.com/workshop/browse/"
        f"?appid={appid}"
        f"&browsesort={browsesort}"
        "&section=readytouseitems"
        "&created_date_range_filter_start=0"
        "&created_date_range_filter_end=0"
        "&updated_date_range_filter_start=0"
        "&updated_date_range_filter_end=0"
        f"&actualsort={browsesort}"
        f"&p={page}"
    )
    if days is not None:
        url += f"&days={days}"
    return url

# Steam Web API — batch workshop item details (no API key required, 100 items/request)
STEAM_API_DETAILS_URL = (
    "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
)

# Patterns for extracting file-size metadata from individual item pages
_DETAILS_BLOCK_PAT = re.compile(
    r'<div class="rightDetailsBlock">(.*?)<div style="clear:left">',
    re.DOTALL,
)
_STAT_LABEL_PAT = re.compile(r'<div class="detailsStatLeft">(.*?)</div>', re.DOTALL)
_STAT_VALUE_PAT = re.compile(r'<div class="detailsStatRight">(.*?)</div>', re.DOTALL)


# ---------------------------------------------------------------------------
# Per-appid metadata file locks (prevents concurrent read-modify-write races)
# ---------------------------------------------------------------------------
_meta_locks: dict[str, threading.Lock] = {}
_meta_locks_mutex = threading.Lock()


def _meta_lock(metadata_file: str) -> threading.Lock:
    """Return (creating if necessary) a per-file lock for *metadata_file*."""
    with _meta_locks_mutex:
        if metadata_file not in _meta_locks:
            _meta_locks[metadata_file] = threading.Lock()
        return _meta_locks[metadata_file]


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------
def make_session(retries: int = 3, backoff: float = 1.0) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    })
    return session


SESSION = make_session()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def csv_path(meta_dir: str, appid: str) -> str:
    return os.path.join(meta_dir, "games", appid, "data.csv")


# ---------------------------------------------------------------------------
# Steam helpers
# ---------------------------------------------------------------------------
def extract_workshop_id(url: str) -> str | None:
    """Extract the workshop item ID from a Steam URL. Returns None if not numeric."""
    parsed = parse_qs(urlparse(url).query)
    ids = parsed.get("id")
    if ids and ids[0].isdigit():
        return ids[0]
    start = url.find("id=") + 3
    if start < 3:
        return None
    end = url.find("&", start)
    iid = url[start:] if end == -1 else url[start:end]
    return iid if iid.isdigit() else None


def get_game_name(appid: str) -> str:
    """Fetch the human-readable game name for the given App ID."""
    url = build_workshop_url(appid, page=1)
    try:
        r = SESSION.get(url, timeout=15)
        r.raise_for_status()
        match = re.search(
            r'<div class="apphub_AppName ellipsis">\s*(.*?)\s*</div>',
            r.text,
        )
        return match.group(1) if match else "Unknown Game"
    except requests.RequestException:
        return "Unknown Game"


# ---------------------------------------------------------------------------
# Size helpers
# ---------------------------------------------------------------------------
def parse_size_bytes(size_str: str) -> float:
    """Convert a human-readable size string (e.g. '3.602 GB') to bytes."""
    units = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
    m = re.match(r"([\d,.]+)\s*([KMGT]?B)", size_str.strip(), re.IGNORECASE)
    if not m:
        return 0.0
    number = float(m.group(1).replace(",", ""))
    return number * units.get(m.group(2).upper(), 0.0)


def format_bytes(total: float) -> str:
    """Format a byte count as a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if total < 1024.0 or unit == "TB":
            return f"{total:.2f} {unit}"
        total /= 1024.0
    return str(total)


# ---------------------------------------------------------------------------
# File-size fetching
# ---------------------------------------------------------------------------
def fetch_file_size(url: str) -> str:
    """Fetch a workshop item page and return its file size string."""
    clean_url = url.split("&")[0] if "&" in url else url
    try:
        r = SESSION.get(clean_url, timeout=15)
        r.raise_for_status()
    except requests.RequestException:
        return ""
    block_match = _DETAILS_BLOCK_PAT.search(r.text)
    if not block_match:
        return ""
    block = block_match.group(1)
    labels = _STAT_LABEL_PAT.findall(block)
    values = _STAT_VALUE_PAT.findall(block)
    for label, value in zip(labels, values):
        if "File Size" in label:
            return value.strip()
    return ""


# ---------------------------------------------------------------------------
# Description-image helpers
# ---------------------------------------------------------------------------
_IMG_BBCODE_RE = re.compile(r'\[img\](https?://\S+?)\[/img\]', re.IGNORECASE)
_SAFE_IMG_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}


def _url_to_desc_filename(url: str) -> str:
    """Return a stable local filename derived from an image URL."""
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext not in _SAFE_IMG_EXTS:
        ext = '.jpg'
    return hashlib.sha256(url.encode()).hexdigest()[:20] + ext


def _download_desc_images(metadata: dict, desc_images_base: str, log) -> None:
    """Download images embedded in BBCode descriptions; maintain per-item map.json."""
    to_download: list[tuple[str, str]] = []  # (dest_path, url)

    for iid, meta in metadata.items():
        desc = meta.get("description", "")
        if not desc:
            continue
        # Unique URLs in order of appearance
        urls = list(dict.fromkeys(_IMG_BBCODE_RE.findall(desc)))
        if not urls:
            continue

        item_dir = os.path.join(desc_images_base, iid)
        map_path = os.path.join(item_dir, "map.json")

        try:
            with open(map_path, "r", encoding="utf-8") as f:
                url_map: dict = json.load(f)
        except (OSError, ValueError):
            url_map = {}

        map_changed = False
        for url in urls:
            filename = _url_to_desc_filename(url)
            if url not in url_map:
                url_map[url] = filename
                map_changed = True
            dest = os.path.join(item_dir, filename)
            if not os.path.isfile(dest):
                to_download.append((dest, url))

        if map_changed:
            os.makedirs(item_dir, exist_ok=True)
            with open(map_path, "w", encoding="utf-8") as f:
                json.dump(url_map, f)

    if not to_download:
        return

    total = len(to_download)
    log(f"Downloading {total} description image{'s' if total != 1 else ''}...")
    ok = 0
    for dest, url in to_download:
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            r = SESSION.get(url, timeout=15)
            r.raise_for_status()
            with open(dest, "wb") as f:
                f.write(r.content)
            ok += 1
        except Exception:
            pass
    log(f"Description images complete: {ok}/{total} downloaded.")


# ---------------------------------------------------------------------------
# Steam Web API metadata fetch
# ---------------------------------------------------------------------------
def fetch_and_cache_metadata(
    appid: str,
    item_ids: list[str],
    meta_dir: str,
    log_fn=None,
    download_previews: bool = True,
) -> dict:
    """Batch-fetch workshop item metadata from the Steam Web API.

    Saves preview images to ``{meta_dir}/games/{appid}/previews/{item_id}.jpg``
    and caches metadata to ``{meta_dir}/games/{appid}/metadata.json``.
    Returns the full metadata dict keyed by item_id.
    Only fetches items not already present in the cache.
    """
    def log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    game_dir = os.path.join(meta_dir, "games", appid)
    previews_dir = os.path.join(game_dir, "previews")
    os.makedirs(previews_dir, exist_ok=True)
    metadata_file = os.path.join(game_dir, "metadata.json")

    lock = _meta_lock(metadata_file)
    with lock:
        # Load existing cache
        cached: dict = {}
        try:
            with open(metadata_file, "r", encoding="utf-8") as f:
                cached = json.load(f)
        except (OSError, ValueError):
            pass

        # Only hit the API for items not already cached
        to_fetch = [i for i in item_ids if i not in cached]
        if not to_fetch:
            log(f"Metadata already cached for all {len(item_ids)} items.")
        else:
            log(f"Fetching metadata for {len(to_fetch)} workshop items from Steam API...")
            # Fetch into a separate dict so that a failed disk write does not
            # leave `cached` in a mutated-but-unpersisted state.
            new_items: dict = {}
            _batch_fetch_metadata(to_fetch, new_items, log)
            merged = {**cached, **new_items}
            # Atomic write: write to a temp file then rename so readers never
            # see a partially-written JSON file.
            try:
                tmp_fd, tmp_path = tempfile.mkstemp(dir=game_dir, suffix=".tmp")
                try:
                    with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                        json.dump(merged, f, indent=2)
                    os.replace(tmp_path, metadata_file)
                    cached.update(new_items)  # only update in-memory after successful write
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
            except OSError as exc:
                log(f"WARNING: Could not write metadata cache: {exc}")

    # Download any missing preview + description images
    # (skipped for pre-download size estimates via download_previews=False)
    if download_previews:
        _download_previews(cached, previews_dir, log)
        _download_desc_images(cached, os.path.join(game_dir, "desc_images"), log)
    return cached


def _batch_fetch_metadata(item_ids: list[str], out: dict, log) -> None:
    """POST to Steam Web API in batches of 100; merge results into *out*."""
    BATCH = 100
    total = len(item_ids)
    for start in range(0, total, BATCH):
        batch = item_ids[start: start + BATCH]
        post_data: dict = {"itemcount": len(batch)}
        for idx, iid in enumerate(batch):
            post_data[f"publishedfileids[{idx}]"] = iid
        try:
            r = SESSION.post(STEAM_API_DETAILS_URL, data=post_data, timeout=30)
            r.raise_for_status()
            resp = r.json()
        except Exception as exc:
            log(f"WARNING: Steam API request failed (batch {start}–{start + len(batch) - 1}): {exc}")
            continue
        for fd in resp.get("response", {}).get("publishedfiledetails", []):
            iid = str(fd.get("publishedfileid", ""))
            if not iid:
                continue
            tags_raw = fd.get("tags", [])
            tags = [t["tag"] for t in tags_raw if isinstance(t, dict) and "tag" in t]
            out[iid] = {
                "title": fd.get("title", iid),
                "description": fd.get("description", ""),
                "tags": tags,
                "preview_url": fd.get("preview_url", ""),
                "time_updated": fd.get("time_updated", 0),
                "file_size": int(fd.get("file_size", 0) or 0),
                "creator": fd.get("creator", ""),
            }
        fetched = min(start + BATCH, total)
        log(f"  Metadata: {fetched}/{total}")


def _download_previews(metadata: dict, previews_dir: str, log) -> None:
    """Download preview images for items that don't yet have a local copy."""
    needed = [
        (iid, meta["preview_url"])
        for iid, meta in metadata.items()
        if meta.get("preview_url")
        and not os.path.isfile(os.path.join(previews_dir, f"{iid}.jpg"))
    ]
    if not needed:
        return
    total = len(needed)
    log(f"Downloading {total} preview images...")
    BAR_WIDTH = 28
    # Log at most every 5% progress, but always on the last item
    step = max(1, total // 20)
    ok = 0
    last_logged = -1

    def _progress(done: int) -> None:
        pct = done / total
        filled = int(BAR_WIDTH * pct)
        bar = "█" * filled + "░" * (BAR_WIDTH - filled)
        log(f"  [{bar}] {done}/{total} ({int(pct * 100)}%)")

    for i, (iid, url) in enumerate(needed, start=1):
        dest = os.path.join(previews_dir, f"{iid}.jpg")
        try:
            r = SESSION.get(url, timeout=15)
            r.raise_for_status()
            with open(dest, "wb") as f:
                f.write(r.content)
            ok += 1
        except Exception:
            pass
        if i % step == 0 or i == total:
            if i != last_logged:
                _progress(i)
                last_logged = i

    log(f"Previews complete: {ok}/{total} downloaded.")


