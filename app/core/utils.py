"""Shared utilities: HTTP session, helpers, HTML/CSV writers."""

import csv
import hashlib
import html
import json
import math
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
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

# Workshop browse page (React SSR redesign)
_SSR_LOADER_RE = re.compile(r"window\.SSR\.loaderData = (\[.*?\]);", re.DOTALL)
_WORKSHOP_LINK_RE = re.compile(
    r'href="(https://steamcommunity\.com/sharedfiles/filedetails/\?id=\d+)"'
)
_WORKSHOP_AUTHOR_RE = re.compile(r">By ([^<]+)<")
_STEAM_WORKSHOP_TITLE_RE = re.compile(
    r"<title>The Steam Workshop for (.+?)</title>"
)
_LEGACY_GAME_NAME_RE = re.compile(
    r'<div class="apphub_AppName ellipsis">\s*(.*?)\s*</div>'
)
# Steam caps workshop browse pagination at 1000 pages.
_WORKSHOP_BROWSE_PAGE_CAP = 1000


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


def workshop_content_dir(downloads_dir: str, appid: str) -> str:
    """Path to SteamCMD workshop content for an app."""
    return os.path.join(downloads_dir, "steamapps", "workshop", "content", appid)


def is_item_downloaded(downloads_dir: str, appid: str, item_id: str) -> bool:
    """True if workshop item content already exists locally."""
    content_dir = workshop_content_dir(downloads_dir, appid)
    return (
        os.path.isdir(os.path.join(content_dir, item_id))
        or os.path.isfile(os.path.join(content_dir, f"{item_id}.bin"))
    )


def appworkshop_acf_path(downloads_dir: str, appid: str) -> str:
    """Path to the SteamCMD appworkshop ACF manifest for an app."""
    return os.path.join(downloads_dir, "steamapps", "workshop", f"appworkshop_{appid}.acf")


_ACF_ITEM_RE = re.compile(r'"(\d+)"\s*\{([^}]+)\}')
_ACF_SIZE_RE = re.compile(r'"size"\s+"(\d+)"')
_ACF_TIME_RE = re.compile(r'"timeupdated"\s+"(\d+)"')


def _acf_vdf_block(text: str, key: str) -> str:
    """Return the inner body of a brace-delimited VDF section, or ""."""
    marker = f'"{key}"'
    idx = text.find(marker)
    if idx == -1:
        return ""
    brace = text.find("{", idx)
    if brace == -1:
        return ""
    depth = 0
    for pos in range(brace, len(text)):
        ch = text[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[brace + 1: pos]
    return ""


def parse_acf_items(acf_path: str) -> dict[str, dict]:
    """Return {item_id: {size, timeupdated}} from a SteamCMD appworkshop .acf file."""
    try:
        with open(acf_path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}

    section = _acf_vdf_block(text, "WorkshopItemsInstalled")
    if not section:
        return {}

    result: dict[str, dict] = {}
    for m in _ACF_ITEM_RE.finditer(section):
        item_id, block = m.group(1), m.group(2)
        size_m = _ACF_SIZE_RE.search(block)
        time_m = _ACF_TIME_RE.search(block)
        result[item_id] = {
            "size": int(size_m.group(1)) if size_m else 0,
            "timeupdated": int(time_m.group(1)) if time_m else 0,
        }
    return result


def workshop_item_disk_size(entry_path: str, *, is_dir: bool, is_bin: bool) -> int:
    """Return on-disk byte size for a workshop item directory or .bin file."""
    if is_bin:
        try:
            return os.path.getsize(entry_path)
        except OSError:
            return 0
    if not is_dir:
        return 0
    total = 0
    for root, _, files in os.walk(entry_path):
        for fname in files:
            try:
                total += os.path.getsize(os.path.join(root, fname))
            except OSError:
                pass
    return total


def resolve_workshop_item_size(
    entry_path: str,
    *,
    is_dir: bool,
    is_bin: bool,
    acf_item: dict,
    api_meta: dict,
) -> int:
    """Resolve display/download size: ACF, then API metadata, then disk."""
    size = int(acf_item.get("size", 0) or 0)
    if size:
        return size
    size = int(api_meta.get("file_size", 0) or 0)
    if size:
        return size
    return workshop_item_disk_size(entry_path, is_dir=is_dir, is_bin=is_bin)


def load_workshop_metadata_times(meta_dir: str, appid: str) -> dict[str, int]:
    """Return {item_id: time_updated} from the cached Steam API metadata file."""
    metadata_file = os.path.join(meta_dir, "games", appid, "metadata.json")
    try:
        with open(metadata_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    times: dict[str, int] = {}
    for item_id, entry in data.items():
        if not isinstance(entry, dict):
            continue
        try:
            times[item_id] = int(entry.get("time_updated", 0) or 0)
        except (TypeError, ValueError):
            times[item_id] = 0
    return times


@dataclass
class DownloadVerifySnapshot:
    """Filesystem state captured before a SteamCMD download attempt."""

    preexisting: set[str] = field(default_factory=set)
    acf_state: dict[str, dict] = field(default_factory=dict)
    bin_mtime: dict[str, float] = field(default_factory=dict)


def take_verify_snapshot(
    downloads_dir: str,
    appid: str,
    item_ids: list[str],
) -> DownloadVerifySnapshot:
    """Record which items already exist and their ACF / .bin timestamps."""
    content_dir = workshop_content_dir(downloads_dir, appid)
    preexisting: set[str] = set()
    bin_mtime: dict[str, float] = {}

    for item_id in item_ids:
        item_dir = os.path.join(content_dir, item_id)
        bin_path = os.path.join(content_dir, f"{item_id}.bin")
        if os.path.isdir(item_dir):
            preexisting.add(item_id)
        elif os.path.isfile(bin_path):
            preexisting.add(item_id)
            bin_mtime[item_id] = os.path.getmtime(bin_path)

    return DownloadVerifySnapshot(
        preexisting=preexisting,
        acf_state=parse_acf_items(appworkshop_acf_path(downloads_dir, appid)),
        bin_mtime=bin_mtime,
    )


def workshop_item_satisfied(
    workshop_id: str,
    snapshot: DownloadVerifySnapshot,
    downloads_dir: str,
    appid: str,
    api_time_updated: int = 0,
) -> bool:
    """
    Return True when a download attempt can be considered successful.

    - New items must appear on disk.
    - Pre-existing items must have been updated this run (ACF or .bin mtime),
      or already match the workshop's published time_updated from API metadata.
    - When no API timestamp is available for a pre-existing, unchanged item, it
      is treated as satisfied: the archive already holds a copy and there is no
      reference to prove it stale (avoids false failures and pointless retries
      on re-runs without a metadata cache).
    """
    if not is_item_downloaded(downloads_dir, appid, workshop_id):
        return False

    if workshop_id not in snapshot.preexisting:
        return True

    acf_after = parse_acf_items(appworkshop_acf_path(downloads_dir, appid))
    before = snapshot.acf_state.get(workshop_id, {})
    after = acf_after.get(workshop_id, {})

    before_time = int(before.get("timeupdated", 0) or 0)
    after_time = int(after.get("timeupdated", 0) or 0)
    before_size = int(before.get("size", 0) or 0)
    after_size = int(after.get("size", 0) or 0)

    if after_time > before_time or after_size != before_size:
        return True

    bin_path = os.path.join(workshop_content_dir(downloads_dir, appid), f"{workshop_id}.bin")
    if workshop_id in snapshot.bin_mtime and os.path.isfile(bin_path):
        if os.path.getmtime(bin_path) > snapshot.bin_mtime[workshop_id]:
            return True

    if api_time_updated > 0:
        return after_time >= api_time_updated

    # No API reference timestamp: item exists locally and SteamCMD left it
    # unchanged — accept the existing copy.
    return True


def verify_downloaded_items(
    item_ids: list[str],
    snapshot: DownloadVerifySnapshot,
    downloads_dir: str,
    appid: str,
    api_times: dict[str, int],
) -> set[str]:
    """Return workshop IDs whose local state satisfies the download attempt."""
    return {
        workshop_id
        for workshop_id in item_ids
        if workshop_item_satisfied(
            workshop_id,
            snapshot,
            downloads_dir,
            appid,
            api_times.get(workshop_id, 0),
        )
    }


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


def _parse_ssr_loader_parts(html_text: str) -> list | None:
    """Return parsed SSR loaderData entries, or None if unavailable."""
    match = _SSR_LOADER_RE.search(html_text)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def game_name_from_browse_html(html_text: str) -> str:
    """Extract the game name from a workshop browse page."""
    parts = _parse_ssr_loader_parts(html_text)
    if parts and len(parts) >= 2:
        try:
            header = json.loads(parts[1])
            name = header.get("appHubHeader", {}).get("name")
            if name:
                return html.unescape(str(name).strip())
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    match = _STEAM_WORKSHOP_TITLE_RE.search(html_text)
    if match:
        return html.unescape(match.group(1).strip())

    match = _LEGACY_GAME_NAME_RE.search(html_text)
    if match:
        return html.unescape(match.group(1).strip())

    return ""


def parse_workshop_browse_meta(html_text: str) -> dict:
    """Return browse pagination metadata from SSR loader data."""
    parts = _parse_ssr_loader_parts(html_text)
    if not parts or len(parts) < 3:
        return {}

    try:
        query = json.loads(parts[2])
    except (json.JSONDecodeError, TypeError):
        return {}

    total = int(query.get("workshopNumbers", {}).get("total") or 0)
    per_page = int(query.get("serverQuery", {}).get("num_per_page") or 30)
    if total <= 0 or per_page <= 0:
        return {}

    max_pages = min(math.ceil(total / per_page), _WORKSHOP_BROWSE_PAGE_CAP)
    return {"total": total, "per_page": per_page, "max_pages": max_pages}


def parse_workshop_browse_page(html_text: str) -> tuple[list[str], list[str], list[str]]:
    """Return (titles, links, authors) from a workshop browse page."""
    links = list(dict.fromkeys(_WORKSHOP_LINK_RE.findall(html_text)))
    titles: list[str] = []
    for link in links:
        title_match = re.search(
            rf'href="{re.escape(link)}"[^>]*><img[^>]*alt="([^"]*)"',
            html_text,
        )
        titles.append(
            html.unescape(title_match.group(1).strip()) if title_match else ""
        )

    authors = [
        html.unescape(author.strip())
        for author in _WORKSHOP_AUTHOR_RE.findall(html_text)
    ]
    if len(authors) < len(links):
        authors.extend([""] * (len(links) - len(authors)))
    elif len(authors) > len(links):
        authors = authors[: len(links)]

    return titles, links, authors


# Game names effectively never change, so cache them forever: in memory for
# this process and (when cache_dir is given) in a JSON file across restarts.
# Without this, every dashboard load fetched a full Steam browse page per app.
_game_name_cache: dict[str, str] = {}
_game_name_cache_lock = threading.Lock()


def _game_names_file(cache_dir: str) -> str:
    return os.path.join(cache_dir, "game_names.json")


def get_game_name(appid: str, cache_dir: str = "") -> str:
    """Fetch the human-readable game name for the given App ID (cached)."""
    with _game_name_cache_lock:
        name = _game_name_cache.get(appid)
    if name:
        return name

    if cache_dir:
        try:
            with open(_game_names_file(cache_dir), "r", encoding="utf-8") as f:
                name = json.load(f).get(appid)
        except (OSError, ValueError):
            name = None
        if name:
            with _game_name_cache_lock:
                _game_name_cache[appid] = name
            return name

    url = build_workshop_url(appid, page=1)
    try:
        r = SESSION.get(url, timeout=15)
        r.raise_for_status()
        name = game_name_from_browse_html(r.text)
    except requests.RequestException:
        name = ""

    if not name:
        # Failed lookups are not cached so the next call can retry.
        return "Unknown Game"

    with _game_name_cache_lock:
        _game_name_cache[appid] = name
    if cache_dir:
        path = _game_names_file(cache_dir)
        with _meta_lock(path):
            try:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        stored = json.load(f)
                except (OSError, ValueError):
                    stored = {}
                stored[appid] = name
                os.makedirs(cache_dir, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(stored, f, indent=2)
            except OSError:
                pass
    return name


# ---------------------------------------------------------------------------
# Size helpers
# ---------------------------------------------------------------------------
def format_bytes(total: float) -> str:
    """Format a byte count as a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if total < 1024.0 or unit == "TB":
            return f"{total:.2f} {unit}"
        total /= 1024.0
    return str(total)


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
    force_refresh: bool = False,
) -> dict:
    """Batch-fetch workshop item metadata from the Steam Web API.

    Saves preview images to ``{meta_dir}/games/{appid}/previews/{item_id}.jpg``
    and caches metadata to ``{meta_dir}/games/{appid}/metadata.json``.
    Returns the full metadata dict keyed by item_id.
    Only fetches items not already present in the cache, unless *force_refresh*
    is set — then every requested item is re-fetched so stale entries
    (time_updated, descriptions, previews) get updated.
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

        # Only hit the API for items not already cached (or everything on refresh)
        if force_refresh:
            to_fetch = list(item_ids)
        else:
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


