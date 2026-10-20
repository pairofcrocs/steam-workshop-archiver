"""Workshop browse-page scraping (used when no Steam Web API key is configured)."""

import html
import json
import math
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import requests

from .net import SESSION
from .steam_api import extract_workshop_id

# sort_key -> (browsesort, days). "random" scrapes all-time and shuffles afterwards.
_SORT_PARAMS: dict[str, tuple[str, int | None]] = {
    "trend_today": ("trend", 1),
    "trend_week": ("trend", 7),
    "trend_month": ("trend", 30),
    "trend_3months": ("trend", 90),
    "trend_6months": ("trend", 180),
    "trend_year": ("trend", 365),
    "trend_alltime": ("trend", -1),
    "mostrecent": ("mostrecent", None),
    "lastupdated": ("lastupdated", None),
    "totaluniquesubscribers": ("totaluniquesubscribers", None),
    "random": ("trend", -1),
}

# Steam caps workshop browse pagination at 1000 pages.
BROWSE_PAGE_CAP = 1000

_SSR_SCRIPT_RE = re.compile(r'<script[^>]*id="valve-ssr-data"[^>]*>(.*?)</script>', re.DOTALL)
_SSR_LOADER_RE = re.compile(r"window\.SSR\.loaderData = (\[.*?\]);", re.DOTALL)
_WORKSHOP_LINK_RE = re.compile(r'href="(https://steamcommunity\.com/sharedfiles/filedetails/\?id=\d+)"')
_WORKSHOP_AUTHOR_RE = re.compile(r">By ([^<]+)<")
_STEAM_WORKSHOP_TITLE_RE = re.compile(r"<title>The Steam Workshop for (.+?)</title>")
_LEGACY_GAME_NAME_RE = re.compile(r'<div class="apphub_AppName ellipsis">\s*(.*?)\s*</div>')


def build_workshop_url(appid: str, page: int, sort_key: str = "trend_week") -> str:
    browsesort, days = _SORT_PARAMS.get(sort_key, ("trend", 7))
    url = (
        "https://steamcommunity.com/workshop/browse/"
        f"?appid={appid}&browsesort={browsesort}&section=readytouseitems"
        "&created_date_range_filter_start=0&created_date_range_filter_end=0"
        "&updated_date_range_filter_start=0&updated_date_range_filter_end=0"
        f"&actualsort={browsesort}&p={page}"
    )
    if days is not None:
        url += f"&days={days}"
    return url


def _ssr_entries(html_text: str) -> list[dict]:
    """Decoded SSR loaderData entries. Handles the ``valve-ssr-data`` JSON script
    (current markup) and the older inline ``window.SSR.loaderData = [...]``."""
    raw = None
    m = _SSR_SCRIPT_RE.search(html_text)
    if m:
        try:
            raw = json.loads(m.group(1)).get("loaderData")
        except (json.JSONDecodeError, AttributeError):
            raw = None
    if raw is None:
        m = _SSR_LOADER_RE.search(html_text)
        if m:
            try:
                raw = json.loads(m.group(1))
            except json.JSONDecodeError:
                raw = None
    entries = []
    for part in raw or []:
        try:
            entry = json.loads(part) if isinstance(part, str) else part
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def game_name_from_browse_html(html_text: str) -> str:
    for entry in _ssr_entries(html_text):
        name = (entry.get("appHubHeader") or {}).get("name")
        if name:
            return html.unescape(str(name).strip())
    for pattern in (_STEAM_WORKSHOP_TITLE_RE, _LEGACY_GAME_NAME_RE):
        match = pattern.search(html_text)
        if match:
            return html.unescape(match.group(1).strip())
    return ""


def parse_workshop_browse_meta(html_text: str) -> dict:
    for entry in _ssr_entries(html_text):
        if "workshopNumbers" not in entry:
            continue
        try:
            total = int(entry["workshopNumbers"].get("total") or 0)
            per_page = int((entry.get("serverQuery") or {}).get("num_per_page") or 30)
        except (AttributeError, TypeError, ValueError):
            return {}
        if total <= 0 or per_page <= 0:
            return {}
        return {
            "total": total,
            "per_page": per_page,
            "max_pages": min(math.ceil(total / per_page), BROWSE_PAGE_CAP),
        }
    return {}


def parse_workshop_browse_page(html_text: str) -> list[dict]:
    """[{id, title, author}] from one browse page."""
    links = list(dict.fromkeys(_WORKSHOP_LINK_RE.findall(html_text)))
    authors = [html.unescape(a.strip()) for a in _WORKSHOP_AUTHOR_RE.findall(html_text)]
    items = []
    for i, link in enumerate(links):
        iid = extract_workshop_id(link)
        if not iid:
            continue
        m = re.search(rf'href="{re.escape(link)}"[^>]*><img[^>]*alt="([^"]*)"', html_text)
        items.append({
            "id": iid,
            "title": html.unescape(m.group(1).strip()) if m else "",
            "author": authors[i] if i < len(authors) else "",
        })
    return items


@dataclass
class ScrapeResult:
    items: list[dict] = field(default_factory=list)
    complete: bool = True
    error: str = ""
    game_name: str = ""


def scrape_workshop(
    appid: str,
    num_pages: int | None,
    delay_min: float,
    delay_max: float,
    sort_key: str,
    log_fn: Callable[[str], None],
    cancel_check: Callable[[], bool] | None = None,
) -> ScrapeResult:
    """Scrape browse pages. ``num_pages=None`` means all pages.

    ``complete`` is False when the scrape stopped early (error or cancel), so callers
    can avoid replacing a previous full list with a partial one."""
    result = ScrapeResult()
    seen: set[str] = set()
    page = 1
    max_pages: int | None = num_pages
    prev_ids: list[str] | None = None
    known_pages: int | None = None
    lo, hi = sorted((delay_min, delay_max))

    log_fn(f"Scraping Workshop browse pages for App ID {appid}...")
    while max_pages is None or page <= max_pages:
        if cancel_check and cancel_check():
            result.complete = False
            log_fn("Scrape cancelled.")
            break
        try:
            r = SESSION.get(build_workshop_url(appid, page, sort_key), timeout=20)
            r.raise_for_status()
        except requests.RequestException as exc:
            result.complete = False
            result.error = f"Failed to fetch page {page}: {exc}"
            log_fn(f"ERROR: {result.error}")
            break

        if page == 1:
            result.game_name = game_name_from_browse_html(r.text)
            meta = parse_workshop_browse_meta(r.text)
            if meta.get("max_pages"):
                cap = meta["max_pages"]
                known_pages = cap
                max_pages = cap if max_pages is None else min(max_pages, cap)
                log_fn(f"Workshop has {meta['total']} items ({cap} pages of {meta['per_page']}).")
                if meta["total"] > BROWSE_PAGE_CAP * meta["per_page"]:
                    log_fn(
                        "NOTE: Steam only lets the browse pages reach the first "
                        f"{BROWSE_PAGE_CAP * meta['per_page']} items. Add a Steam Web API key "
                        "in Settings to archive the rest."
                    )

        page_items = parse_workshop_browse_page(r.text)
        if not page_items:
            if known_pages and page <= known_pages and page > 1:
                result.complete = False
                result.error = f"Page {page} came back empty although the Workshop reports {known_pages} pages."
                log_fn(f"WARNING: {result.error} Treating the list as incomplete.")
            else:
                log_fn(f"No more items after page {page - 1}.")
            break
        ids = [it["id"] for it in page_items]
        if prev_ids is not None and ids == prev_ids:
            log_fn(f"Page {page} repeated the previous page — stopping.")
            break
        prev_ids = ids

        for it in page_items:
            if it["id"] not in seen:
                seen.add(it["id"])
                result.items.append(it)
        log_fn(f"Scraped page {page} — {len(result.items)} items so far")
        page += 1
        if max_pages is None or page <= max_pages:
            time.sleep(random.uniform(lo, hi))

    if sort_key == "random" and result.items:
        random.shuffle(result.items)
        log_fn("Shuffled items into random order.")
    return result
