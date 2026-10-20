"""Steam Web API / store helpers and Workshop id parsing."""

import html
import logging
import re
import time
from collections.abc import Callable, Iterator
from urllib.parse import parse_qs, quote, urlparse

import requests

from .net import SESSION

log = logging.getLogger(__name__)

API = "https://api.steampowered.com"
DETAILS_URL = f"{API}/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
COLLECTION_URL = f"{API}/ISteamRemoteStorage/GetCollectionDetails/v1/"
QUERY_FILES_URL = f"{API}/IPublishedFileService/QueryFiles/v1/"

DETAILS_BATCH = 100
COLLECTION_CREATOR_APP = 766  # creator_app_id Steam uses for collections
_ID_RE = re.compile(r"[0-9]{1,20}")

# EPublishedFileQueryType values per sort key; "days" applies to trend sorts.
QUERY_SORTS: dict[str, tuple[int, int | None]] = {
    "trend_today": (3, 1),
    "trend_week": (3, 7),
    "trend_month": (3, 30),
    "trend_3months": (3, 90),
    "trend_6months": (3, 180),
    "trend_year": (3, 365),
    "trend_alltime": (0, None),
    "mostrecent": (1, None),
    "lastupdated": (19, None),
    "totaluniquesubscribers": (9, None),
    "random": (0, None),
}
SORT_KEYS = tuple(QUERY_SORTS)


class SteamError(Exception):
    pass


# ---------------------------------------------------------------------------
# Id parsing
# ---------------------------------------------------------------------------
def is_id(value: str) -> bool:
    return bool(_ID_RE.fullmatch(value or ""))


def extract_workshop_id(url: str) -> str | None:
    """Item id from a Workshop URL (``?id=``) or a bare numeric id."""
    url = (url or "").strip()
    if is_id(url):
        return url
    try:
        ids = parse_qs(urlparse(url).query).get("id")
    except ValueError:
        return None
    if ids and is_id(ids[0]):
        return ids[0]
    return None


def parse_item_list(text: str) -> list[str]:
    """Unique item ids from whitespace/comma separated ids or Workshop URLs."""
    out: list[str] = []
    for token in re.split(r"[\s,;]+", text or ""):
        iid = extract_workshop_id(token)
        if iid and iid not in out:
            out.append(iid)
    return out


def parse_app_ref(text: str) -> str | None:
    """App id from a bare number or a store / community / SteamDB URL."""
    text = (text or "").strip()
    if is_id(text) and len(text) <= 10:
        return text
    try:
        parsed = urlparse(text)
        q = parse_qs(parsed.query)
    except ValueError:
        return None
    if q.get("appid") and is_id(q["appid"][0]):
        return q["appid"][0]
    m = re.search(r"/(?:app|apps)/(\d{1,10})", parsed.path)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Published file details
# ---------------------------------------------------------------------------
def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def normalize_details(fd: dict) -> dict:
    """Map ISteamRemoteStorage / IPublishedFileService detail shapes to one dict."""
    tags = [t.get("tag") or t.get("display_name") for t in fd.get("tags", []) if isinstance(t, dict)]
    return {
        "item_id": str(fd.get("publishedfileid", "")),
        "result": _int(fd.get("result", 1)),
        "appid": str(fd.get("consumer_app_id") or fd.get("consumer_appid") or ""),
        "title": fd.get("title", "") or "",
        "description": fd.get("description", fd.get("file_description", "")) or "",
        "tags": [t for t in tags if t],
        "creator": str(fd.get("creator", "") or ""),
        "file_size": _int(fd.get("file_size")),
        "file_url": fd.get("file_url", "") or "",
        "filename": fd.get("filename", "") or "",
        "preview_url": fd.get("preview_url", "") or "",
        "time_created": _int(fd.get("time_created")),
        "time_updated": _int(fd.get("time_updated")),
        "is_collection": _int(fd.get("creator_app_id") or fd.get("creator_appid")) == COLLECTION_CREATOR_APP
        or _int(fd.get("file_type", 0)) == 2,
        "banned": bool(fd.get("banned", False)),
    }


def get_published_file_details(
    item_ids: list[str],
    log_fn: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, dict]:
    """Batch-fetch details (no API key needed). Items Steam doesn't return are absent;
    removed/private items come back with ``result != 1``."""
    out: dict[str, dict] = {}
    total = len(item_ids)
    for start in range(0, total, DETAILS_BATCH):
        if cancel_check and cancel_check():
            break
        batch = item_ids[start:start + DETAILS_BATCH]
        data = {"itemcount": len(batch)}
        data.update({f"publishedfileids[{i}]": iid for i, iid in enumerate(batch)})
        resp = None
        for attempt in range(3):
            try:
                r = SESSION.post(DETAILS_URL, data=data, timeout=30)
                r.raise_for_status()
                resp = r.json()
                break
            except (requests.RequestException, ValueError) as exc:
                if attempt == 2:
                    if log_fn:
                        log_fn(f"WARNING: Steam API details request failed ({exc}); {len(batch)} items skipped.")
                else:
                    time.sleep(2 * (attempt + 1))
        if resp is None:
            continue
        for fd in resp.get("response", {}).get("publishedfiledetails", []):
            d = normalize_details(fd)
            if d["item_id"]:
                out[d["item_id"]] = d
        if log_fn and total > DETAILS_BATCH:
            log_fn(f"  Metadata: {min(start + DETAILS_BATCH, total)}/{total}")
    return out


def get_collection_children(collection_ids: list[str]) -> dict[str, list[tuple[str, int]]]:
    """{collection_id: [(child_id, filetype), ...]} for the given collections."""
    data = {"collectioncount": len(collection_ids)}
    data.update({f"publishedfileids[{i}]": cid for i, cid in enumerate(collection_ids)})
    try:
        r = SESSION.post(COLLECTION_URL, data=data, timeout=30)
        r.raise_for_status()
        resp = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise SteamError(f"collection lookup failed: {exc}") from exc
    out: dict[str, list[tuple[str, int]]] = {}
    for col in resp.get("response", {}).get("collectiondetails", []):
        if _int(col.get("result")) != 1:
            continue
        children = sorted(col.get("children", []), key=lambda c: _int(c.get("sortorder")))
        out[str(col.get("publishedfileid"))] = [
            (str(c.get("publishedfileid")), _int(c.get("filetype"))) for c in children
        ]
    return out


def expand_collection(collection_id: str, max_depth: int = 5) -> list[str]:
    """All item ids in a collection, following nested collections (filetype 2)."""
    seen_cols: set[str] = set()
    items: list[str] = []
    frontier = [collection_id]
    for _ in range(max_depth):
        frontier = [c for c in frontier if c not in seen_cols]
        if not frontier:
            break
        seen_cols.update(frontier)
        children = get_collection_children(frontier)
        if not children and collection_id in frontier:
            raise SteamError(f"{collection_id} is not a public collection")
        nxt: list[str] = []
        for cid in frontier:
            for child, ftype in children.get(cid, []):
                if ftype == 2:
                    nxt.append(child)
                elif child not in items:
                    items.append(child)
        frontier = nxt
    return items


# ---------------------------------------------------------------------------
# QueryFiles (needs a Web API key)
# ---------------------------------------------------------------------------
def query_files(
    api_key: str,
    appid: str,
    sort_key: str,
    max_items: int | None,
    log_fn: Callable[[str], None],
    cancel_check: Callable[[], bool] | None = None,
) -> Iterator[list[dict]]:
    """Yield pages of normalized item details using cursor pagination."""
    query_type, days = QUERY_SORTS.get(sort_key, QUERY_SORTS["trend_week"])
    cursor = "*"
    fetched = 0
    total_logged = False
    while True:
        if cancel_check and cancel_check():
            return
        params = {
            "key": api_key,
            "query_type": query_type,
            "cursor": cursor,
            "numperpage": 100,
            "appid": appid,
            "creator_appid": appid,
            "filetype": 0,
            "return_details": "true",
            "return_tags": "true",
            "strip_description_bbcode": "false",
        }
        if days:
            params["days"] = days
            params["include_recent_votes_only"] = "false"
        r = None
        for attempt in range(3):
            try:
                r = SESSION.get(QUERY_FILES_URL, params=params, timeout=30)
                if r.status_code in (401, 403):
                    raise SteamError(f"Steam rejected the Web API key (HTTP {r.status_code})")
                r.raise_for_status()
                break
            except requests.RequestException as exc:
                if attempt == 2:
                    raise SteamError(f"QueryFiles failed: {exc}") from exc
                time.sleep(2 * (attempt + 1))
        resp = r.json().get("response", {})
        if not total_logged:
            log_fn(f"Workshop reports {resp.get('total', 0)} items.")
            total_logged = True
        details = [normalize_details(fd) for fd in resp.get("publishedfiledetails", []) or []]
        if max_items is not None:
            details = details[: max(0, max_items - fetched)]
        if not details:
            return
        fetched += len(details)
        yield details
        next_cursor = resp.get("next_cursor")
        if not next_cursor or next_cursor == cursor or (max_items is not None and fetched >= max_items):
            return
        cursor = next_cursor


# ---------------------------------------------------------------------------
# Apps / store
# ---------------------------------------------------------------------------
_TITLE_RE = re.compile(r"<title>(?:Steam Workshop::|The Steam Workshop for )(.+?)</title>")


def fetch_app_name(appid: str) -> str | None:
    """Name from the store API, falling back to the Workshop page title."""
    try:
        r = SESSION.get(
            "https://store.steampowered.com/api/appdetails",
            params={"appids": appid, "filters": "basic"},
            timeout=15,
        )
        entry = r.json().get(appid, {})
        if entry.get("success") and entry.get("data", {}).get("name"):
            return entry["data"]["name"]
    except (requests.RequestException, ValueError, AttributeError):
        pass
    try:
        r = SESSION.get(f"https://steamcommunity.com/app/{appid}/workshop/", timeout=15)
        m = _TITLE_RE.search(r.text)
        if m:
            return html.unescape(m.group(1).strip())
    except requests.RequestException:
        pass
    return None


_SEARCH_APPID_RE = re.compile(r'data-ds-appid="(\d+)"')
_SEARCH_NAME_RE = re.compile(r'<span class="title">(.*?)</span>')
_SEARCH_IMG_RE = re.compile(r'search_capsule"><img src="([^"]+)"')


def search_workshop_games(q: str) -> list[dict]:
    """Store search limited to Workshop-enabled games (category2=30). Raises SteamError."""
    url = (
        "https://store.steampowered.com/search/results/"
        f"?term={quote(q)}&category2=30&ndl=1&count=15&start=0&infinite=1"
    )
    try:
        r = SESSION.get(url, timeout=10)
        r.raise_for_status()
        page = r.json().get("results_html", "")
    except (requests.RequestException, ValueError) as exc:
        raise SteamError(str(exc)) from exc
    results = []
    for appid, name, logo in zip(
        _SEARCH_APPID_RE.findall(page), _SEARCH_NAME_RE.findall(page), _SEARCH_IMG_RE.findall(page)
    ):
        results.append({"appid": appid, "name": html.unescape(name), "logo": logo})
    return results[:10]
