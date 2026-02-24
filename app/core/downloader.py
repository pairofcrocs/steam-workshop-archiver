"""SteamCMD download logic."""

import csv
import os
import subprocess
from typing import Callable

from .utils import extract_workshop_id


def download_workshop_items(
    workshop_list_file: str,
    steamcmd_path: str,
    appid: str,
    downloads_dir: str,
    log_fn: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> bool:
    """
    Download every item listed in the CSV via SteamCMD (anonymous login).

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

    total = len(workshop_items)
    log_fn(f"Download queue starting — {total} items")

    failed: list[tuple[str, str]] = []
    processed = 0

    for i, (file_name, item_url) in enumerate(workshop_items, start=1):
        if cancel_check and cancel_check():
            log_fn("Download cancelled by user.")
            break

        processed += 1
        workshop_id = extract_workshop_id(item_url)
        if not workshop_id:
            log_fn(f"WARNING: Could not extract workshop ID from: {item_url} — skipping.")
            failed.append((file_name, item_url))
            continue

        cmd = [
            steamcmd_path,
            "+force_install_dir", downloads_dir,
            "+login", "anonymous",
            "+workshop_download_item", appid, workshop_id,
            "+quit",
        ]

        log_fn(f"[{i}/{total}] Downloading {workshop_id} — {file_name}")

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )

            success = False
            try:
                for line in process.stdout:
                    line = line.strip()
                    if line:
                        log_fn(f"  steamcmd: {line}")
                    if "Success." in line:
                        success = True
                    # Check for cancellation mid-download and kill SteamCMD immediately
                    if cancel_check and cancel_check():
                        log_fn("Download cancelled — SteamCMD process terminated.")
                        break
            finally:
                # Always ensure the process is cleaned up, even on unexpected exceptions
                if process.poll() is None:
                    process.kill()
                process.wait()

            if process.returncode != 0 or not success:
                log_fn(
                    f"WARNING: Failed to download {workshop_id} ({file_name})"
                    f" — exit code {process.returncode}"
                )
                failed.append((file_name, item_url))
            else:
                log_fn(f"Downloaded: {file_name}")

        except FileNotFoundError:
            log_fn(f"ERROR: SteamCMD executable not found: {steamcmd_path}")
            return False
        except Exception as exc:
            log_fn(f"ERROR: Unexpected error downloading {workshop_id}: {exc}")
            failed.append((file_name, item_url))

    succeeded = processed - len(failed)
    remaining = total - processed
    if remaining > 0:
        log_fn(f"Download complete: {succeeded}/{processed} succeeded — {remaining} not started (cancelled)")
    else:
        log_fn(f"Download complete: {succeeded}/{total} succeeded")

    if failed:
        log_fn(f"WARNING: {len(failed)} item(s) failed:")
        for name, url in failed:
            log_fn(f"  - {name} ({url})")

    return len(failed) == 0
