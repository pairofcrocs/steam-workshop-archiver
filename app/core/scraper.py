"""Workshop scraping logic."""

import csv
import os
import random
import time
from typing import Callable

import requests

from .utils import (
    SESSION,
    build_workshop_url,
    csv_path,
    extract_workshop_id,
    parse_workshop_browse_meta,
    parse_workshop_browse_page,
)


def scrape_workshop(
    appid: str,
    output_dir: str,
    game_name: str = "Unknown Game",
    num_pages: int = -1,
    delay_min: float = 1.0,
    delay_max: float = 2.5,
    sort_key: str = "trend_week",
    log_fn: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> str:
    """
    Scrape workshop items and save to CSV + HTML viewer.

    Args:
        appid:        Steam App ID.
        output_dir:   Directory to write output files.
        game_name:    Human-readable game name for the HTML title.
        num_pages:    Pages to scrape; -1 means all.
        delay_min:    Minimum seconds to wait between page requests.
        delay_max:    Maximum seconds to wait between page requests.
        log_fn:       Callback that receives log message strings.
        cancel_check: Callable that returns True when the job is cancelled.

    Returns:
        Path to the written CSV file.
    """
    if log_fn is None:
        import logging
        log_fn = logging.getLogger(__name__).info

    titles: list[str] = []
    authors: list[str] = []
    links: list[str] = []
    page = 1
    pages_remaining = num_pages  # -1 = unlimited
    max_pages: int | None = None
    prev_page_ids: list[str] | None = None

    log_fn(f"Starting scrape for App ID {appid}...")

    while pages_remaining != 0:
        if cancel_check and cancel_check():
            log_fn("Scrape cancelled.")
            break

        if max_pages is not None and page > max_pages:
            log_fn(f"Reached workshop browse page limit ({max_pages}).")
            break

        page_url = build_workshop_url(appid=appid, page=page, sort_key=sort_key)
        try:
            response = SESSION.get(page_url, timeout=15)
            response.raise_for_status()
        except requests.RequestException as exc:
            log_fn(f"ERROR: Failed to fetch page {page}: {exc}")
            break

        html = response.text

        if page == 1 and pages_remaining == -1:
            meta = parse_workshop_browse_meta(html)
            if meta.get("max_pages"):
                max_pages = meta["max_pages"]
                log_fn(
                    f"Workshop has {meta['total']} items "
                    f"(up to {max_pages} pages at {meta['per_page']} per page)."
                )

        page_titles, page_links, page_authors = parse_workshop_browse_page(html)

        if not page_links:
            log_fn(f"No more items found after page {page - 1}.")
            break

        page_ids = [iid for iid in (extract_workshop_id(link) for link in page_links) if iid]
        if prev_page_ids is not None and page_ids == prev_page_ids:
            log_fn(f"Page {page} duplicated previous results — stopping.")
            break
        prev_page_ids = page_ids

        titles.extend(page_titles)
        authors.extend(page_authors)
        links.extend(page_links)

        log_fn(f"Scraped page {page} — {len(titles)} items so far")

        if pages_remaining > 0:
            pages_remaining -= 1

        page += 1
        time.sleep(random.uniform(min(delay_min, delay_max), max(delay_min, delay_max)))

    if sort_key == "random" and titles:
        combined = list(zip(titles, links, authors))
        random.shuffle(combined)
        titles, links, authors = zip(*combined)
        titles, links, authors = list(titles), list(links), list(authors)
        log_fn("Shuffled items into random order.")

    out_path = csv_path(output_dir, appid)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Title", "Link", "Author"])
        writer.writerows(zip(titles, links, authors))

    log_fn(f"Saved {len(titles)} items to CSV: {out_path}")
    return out_path
