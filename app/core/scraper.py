"""Workshop scraping logic."""

import csv
import os
import random
import re
import time
from typing import Callable

import requests

from .utils import (
    SESSION,
    build_workshop_url,
    csv_path,
    fetch_file_size,
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

    title_pat = re.compile(
        r'<div class="workshopItemTitle ellipsis">(.*?)<\/div>', re.DOTALL
    )
    author_pat = re.compile(
        r'<div class="workshopItemAuthorName ellipsis">by&nbsp;'
        r'<a class="workshop_author_link" href=".*?">(.*?)<\/a><\/div>',
        re.DOTALL,
    )
    link_pat = re.compile(
        r'<a data-panel="{&quot;focusable&quot;:false}" href="(.*?)" class="item_link">',
        re.DOTALL,
    )

    log_fn(f"Starting scrape for App ID {appid}...")

    while pages_remaining != 0:
        if cancel_check and cancel_check():
            log_fn("Scrape cancelled.")
            break

        page_url = build_workshop_url(appid=appid, page=page, sort_key=sort_key)
        try:
            response = SESSION.get(page_url, timeout=15)
            response.raise_for_status()
        except requests.RequestException as exc:
            log_fn(f"ERROR: Failed to fetch page {page}: {exc}")
            break

        html = response.text

        if "No items matching your search criteria were found." in html:
            log_fn(f"No more items found after page {page - 1}.")
            break

        page_titles = title_pat.findall(html)
        page_authors = author_pat.findall(html)
        page_links = link_pat.findall(html)

        if not page_titles:
            log_fn(f"WARNING: Page {page} returned no items — stopping.")
            break

        count = max(len(page_titles), len(page_authors), len(page_links))
        page_titles  += [""] * (count - len(page_titles))
        page_authors += [""] * (count - len(page_authors))
        page_links   += [""] * (count - len(page_links))

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


def scrape_file_sizes(
    links: list[str],
    delay_min: float = 1.0,
    delay_max: float = 2.5,
    log_fn: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[str]:
    """Fetch the file size for every workshop item link."""
    if log_fn is None:
        import logging
        log_fn = logging.getLogger(__name__).info

    sizes: list[str] = []
    total = len(links)
    log_fn(f"Fetching file sizes for {total} items...")

    for i, url in enumerate(links, start=1):
        if cancel_check and cancel_check():
            log_fn("File size fetch cancelled.")
            sizes.extend([""] * (total - len(sizes)))
            break

        size = fetch_file_size(url)
        sizes.append(size)
        log_fn(f"[{i}/{total}]  {size if size else '(unknown)'}")
        if i < total:
            time.sleep(random.uniform(min(delay_min, delay_max), max(delay_min, delay_max)))

    fetched = sum(1 for s in sizes if s)
    log_fn(f"File sizes fetched: {fetched}/{total} retrieved.")
    return sizes
