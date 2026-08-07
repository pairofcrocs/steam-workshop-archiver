"""SteamCMD download logic."""

import csv
import os
import random
import subprocess
import time
from typing import Callable

from .utils import (
    DownloadVerifySnapshot,
    extract_workshop_id,
    load_workshop_metadata_times,
    take_verify_snapshot,
    verify_downloaded_items,
    workshop_item_satisfied,
)

# One workshop browse page = 30 items; batch SteamCMD invocations accordingly.
_BATCH_SIZE = 30


def _build_steamcmd_command(
    steamcmd_path: str,
    downloads_dir: str,
    appid: str,
    workshop_ids: list[str],
) -> list[str]:
    """Build a single SteamCMD invocation for one or more workshop items."""
    cmd = [
        steamcmd_path,
        "+force_install_dir", downloads_dir,
        "+login", "anonymous",
    ]
    for workshop_id in workshop_ids:
        cmd.extend(["+workshop_download_item", appid, workshop_id])
    cmd.append("+quit")
    return cmd


def _run_steamcmd(
    cmd: list[str],
    log_fn: Callable[[str], None],
    cancel_check: Callable[[], bool] | None,
) -> bool:
    """Run SteamCMD, streaming stdout to the log. Returns True if cancelled."""
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )

    cancelled = False
    try:
        for line in process.stdout:
            line = line.rstrip("\n")
            if line.strip():
                log_fn(f"  steamcmd: {line.strip()}")
            if cancel_check and cancel_check():
                log_fn("Download cancelled — SteamCMD process terminated.")
                cancelled = True
                break
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()

    return cancelled


def _sleep_between_requests(
    pause: float,
    resume: float,
    log_fn: Callable[[str], None],
    cancel_check: Callable[[], bool] | None,
) -> bool:
    """Sleep for a random delay. Returns True if cancelled before/during sleep."""
    if cancel_check and cancel_check():
        return True
    delay = random.uniform(pause, resume)
    log_fn(f"Waiting {delay:.1f}s before next request...")
    time.sleep(delay)
    return bool(cancel_check and cancel_check())


def _retry_items_individually(
    items: list[tuple[str, str, str]],
    steamcmd_path: str,
    downloads_dir: str,
    appid: str,
    api_times: dict[str, int],
    pause: float,
    resume: float,
    log_fn: Callable[[str], None],
    cancel_check: Callable[[], bool] | None,
) -> tuple[set[str], bool]:
    """Retry unsatisfied items one at a time. Returns (succeeded_ids, cancelled)."""
    succeeded: set[str] = set()

    for index, (file_name, _item_url, workshop_id) in enumerate(items):
        if cancel_check and cancel_check():
            return succeeded, True

        log_fn(f"Retrying individually: {workshop_id} — {file_name}")
        snapshot = take_verify_snapshot(downloads_dir, appid, [workshop_id])
        cmd = _build_steamcmd_command(steamcmd_path, downloads_dir, appid, [workshop_id])
        cancelled = _run_steamcmd(cmd, log_fn, cancel_check)
        if cancelled:
            return succeeded, True

        if workshop_item_satisfied(
            workshop_id,
            snapshot,
            downloads_dir,
            appid,
            api_times.get(workshop_id, 0),
        ):
            succeeded.add(workshop_id)
        else:
            log_fn(f"  Item not satisfied after individual retry: {workshop_id}")

        if index < len(items) - 1:
            if _sleep_between_requests(pause, resume, log_fn, cancel_check):
                return succeeded, True

    return succeeded, False


def download_workshop_items(
    workshop_list_file: str,
    steamcmd_path: str,
    appid: str,
    downloads_dir: str,
    meta_dir: str = "",
    delay_min: float = 1.0,
    delay_max: float = 2.5,
    log_fn: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> bool:
    """
    Download every item listed in the CSV via SteamCMD (anonymous login).

    Items are downloaded in batches of up to 30 per SteamCMD invocation. Success
    is verified from disk state: new items must appear locally; pre-existing items
    must be updated this run or already match cached workshop time_updated metadata.
    Unsatisfied items are retried individually. A random delay between batches and
    retries matches the scrape hammering-protection settings.

    Returns True if all items succeeded, False if any failed.
    """
    if log_fn is None:
        import logging
        log_fn = logging.getLogger(__name__).info

    if not os.path.isfile(workshop_list_file):
        log_fn(f"ERROR: Workshop list not found: {workshop_list_file}")
        return False

    if not os.path.isfile(steamcmd_path):
        log_fn(f"ERROR: SteamCMD not found at: {steamcmd_path}")
        return False

    os.makedirs(downloads_dir, exist_ok=True)

    with open(workshop_list_file, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # skip header
        workshop_items = [(row[0], row[1]) for row in reader if len(row) >= 2]

    if not workshop_items:
        log_fn(f"WARNING: No workshop items found in {workshop_list_file}")
        return True

    failed: list[tuple[str, str]] = []
    queue: list[tuple[str, str, str]] = []

    for file_name, item_url in workshop_items:
        workshop_id = extract_workshop_id(item_url)
        if not workshop_id:
            log_fn(f"WARNING: Could not extract workshop ID from: {item_url} — skipping.")
            failed.append((file_name, item_url))
            continue
        queue.append((file_name, item_url, workshop_id))

    total = len(queue) + len(failed)
    if not queue:
        log_fn("No valid workshop items to download.")
        return len(failed) == 0

    api_times = load_workshop_metadata_times(meta_dir, appid) if meta_dir else {}

    batch_count = (len(queue) + _BATCH_SIZE - 1) // _BATCH_SIZE
    log_fn(
        f"Download queue starting — {len(queue)} items "
        f"in {batch_count} batch{'es' if batch_count != 1 else ''} "
        f"(up to {_BATCH_SIZE} per SteamCMD call)"
    )

    processed = len(failed)
    pause = min(delay_min, delay_max)
    resume = max(delay_min, delay_max)
    cancelled = False

    for batch_index in range(batch_count):
        if cancel_check and cancel_check():
            log_fn("Download cancelled by user.")
            cancelled = True
            break

        batch_start = batch_index * _BATCH_SIZE
        batch = queue[batch_start: batch_start + _BATCH_SIZE]
        batch_ids = [workshop_id for _, _, workshop_id in batch]
        batch_end = batch_start + len(batch)

        log_fn(
            f"Batch {batch_index + 1}/{batch_count}: "
            f"downloading {len(batch)} items [{batch_start + 1}–{batch_end}/{len(queue)}]"
        )
        for offset, (file_name, _, workshop_id) in enumerate(batch, start=1):
            log_fn(f"  [{batch_start + offset}/{len(queue)}] {workshop_id} — {file_name}")

        snapshot: DownloadVerifySnapshot = take_verify_snapshot(
            downloads_dir, appid, batch_ids
        )
        cmd = _build_steamcmd_command(steamcmd_path, downloads_dir, appid, batch_ids)

        try:
            batch_cancelled = _run_steamcmd(cmd, log_fn, cancel_check)
        except FileNotFoundError:
            log_fn(f"ERROR: SteamCMD executable not found: {steamcmd_path}")
            return False
        except Exception as exc:
            log_fn(f"ERROR: Unexpected error running SteamCMD batch: {exc}")
            for file_name, item_url, _workshop_id in batch:
                processed += 1
                failed.append((file_name, item_url))
            if cancel_check and cancel_check():
                cancelled = True
                break
            continue

        if batch_cancelled:
            cancelled = True

        satisfied = verify_downloaded_items(
            batch_ids, snapshot, downloads_dir, appid, api_times
        )

        retry_items = [
            (file_name, item_url, workshop_id)
            for file_name, item_url, workshop_id in batch
            if workshop_id not in satisfied
        ]

        if retry_items and not cancelled:
            log_fn(
                f"Verified: {len(satisfied)}/{len(batch)} satisfied — "
                f"retrying {len(retry_items)} item(s) individually"
            )
            retried_ids, retry_cancelled = _retry_items_individually(
                retry_items,
                steamcmd_path,
                downloads_dir,
                appid,
                api_times,
                pause,
                resume,
                log_fn,
                cancel_check,
            )
            satisfied |= retried_ids
            if retry_cancelled:
                cancelled = True

        for file_name, item_url, workshop_id in batch:
            if workshop_id in satisfied:
                processed += 1
                if workshop_id in snapshot.preexisting:
                    log_fn(f"Up to date: {file_name}")
                else:
                    log_fn(f"Downloaded: {file_name}")
            elif cancelled:
                break
            else:
                processed += 1
                log_fn(f"WARNING: Failed to download {workshop_id} ({file_name})")
                failed.append((file_name, item_url))

        if cancelled:
            break

        if batch_index < batch_count - 1:
            if _sleep_between_requests(pause, resume, log_fn, cancel_check):
                log_fn("Download cancelled by user.")
                cancelled = True
                break

    succeeded = processed - len(failed)
    remaining = total - processed
    if remaining > 0:
        log_fn(
            f"Download complete: {succeeded}/{processed} succeeded — "
            f"{remaining} not started (cancelled)"
        )
    else:
        log_fn(f"Download complete: {succeeded}/{total} succeeded")

    if failed:
        log_fn(f"WARNING: {len(failed)} item(s) failed:")
        for name, url in failed:
            log_fn(f"  - {name} ({url})")

    return len(failed) == 0
