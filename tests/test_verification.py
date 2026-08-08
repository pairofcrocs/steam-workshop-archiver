"""Tests for the download-verification logic (snapshot / satisfied rules)."""

import json
import os

from app.core.utils import (
    load_workshop_metadata_times,
    resolve_workshop_item_size,
    take_verify_snapshot,
    verify_downloaded_items,
    workshop_item_satisfied,
)

APPID = "620"


def _write_acf(downloads_dir, items: dict[str, tuple[int, int]]) -> None:
    """items: {item_id: (size, timeupdated)}"""
    workshop_dir = os.path.join(downloads_dir, "steamapps", "workshop")
    os.makedirs(workshop_dir, exist_ok=True)
    blocks = "".join(
        f'\t\t"{iid}"\n\t\t{{\n\t\t\t"size"\t\t"{size}"\n'
        f'\t\t\t"timeupdated"\t\t"{tu}"\n\t\t}}\n'
        for iid, (size, tu) in items.items()
    )
    text = (
        '"AppWorkshop"\n{\n\t"WorkshopItemsInstalled"\n\t{\n'
        + blocks
        + "\t}\n}\n"
    )
    with open(os.path.join(workshop_dir, f"appworkshop_{APPID}.acf"), "w") as f:
        f.write(text)


def _make_item_dir(downloads_dir, item_id) -> str:
    path = os.path.join(downloads_dir, "steamapps", "workshop", "content", APPID, item_id)
    os.makedirs(path, exist_ok=True)
    return path


def test_new_item_must_appear_on_disk(tmp_path):
    downloads = str(tmp_path)
    _write_acf(downloads, {})
    snap = take_verify_snapshot(downloads, APPID, ["111"])

    # Not downloaded -> not satisfied
    assert not workshop_item_satisfied("111", snap, downloads, APPID)

    # Appears on disk -> satisfied
    _make_item_dir(downloads, "111")
    assert workshop_item_satisfied("111", snap, downloads, APPID)


def test_preexisting_item_updated_this_run(tmp_path):
    downloads = str(tmp_path)
    _make_item_dir(downloads, "111")
    _write_acf(downloads, {"111": (1000, 1_600_000_000)})
    snap = take_verify_snapshot(downloads, APPID, ["111"])

    # ACF timeupdated advanced during the run -> satisfied
    _write_acf(downloads, {"111": (1000, 1_700_000_000)})
    assert workshop_item_satisfied("111", snap, downloads, APPID)


def test_preexisting_item_stale_against_api_time(tmp_path):
    downloads = str(tmp_path)
    _make_item_dir(downloads, "111")
    _write_acf(downloads, {"111": (1000, 1_600_000_000)})
    snap = take_verify_snapshot(downloads, APPID, ["111"])

    # Nothing changed and the workshop has a newer version -> not satisfied
    assert not workshop_item_satisfied(
        "111", snap, downloads, APPID, api_time_updated=1_650_000_000
    )
    # Nothing changed but local copy is current -> satisfied
    assert workshop_item_satisfied(
        "111", snap, downloads, APPID, api_time_updated=1_600_000_000
    )


def test_preexisting_item_without_api_reference_is_accepted(tmp_path):
    """Re-running over an existing archive with no metadata cache must not
    report false failures (regression test for the batching PR)."""
    downloads = str(tmp_path)
    _make_item_dir(downloads, "111")
    _write_acf(downloads, {"111": (1000, 1_600_000_000)})
    snap = take_verify_snapshot(downloads, APPID, ["111"])

    assert workshop_item_satisfied("111", snap, downloads, APPID, api_time_updated=0)


def test_verify_downloaded_items_mixed_batch(tmp_path):
    downloads = str(tmp_path)
    _make_item_dir(downloads, "111")  # pre-existing, will stay unchanged
    _write_acf(downloads, {"111": (1000, 1_600_000_000)})
    snap = take_verify_snapshot(downloads, APPID, ["111", "222", "333"])

    _make_item_dir(downloads, "222")  # new item that appeared
    api_times = {"111": 1_600_000_000}

    satisfied = verify_downloaded_items(
        ["111", "222", "333"], snap, downloads, APPID, api_times
    )
    assert satisfied == {"111", "222"}  # 333 never appeared


def test_bin_item_mtime_bump(tmp_path):
    downloads = str(tmp_path)
    content = os.path.join(downloads, "steamapps", "workshop", "content", APPID)
    os.makedirs(content, exist_ok=True)
    bin_path = os.path.join(content, "555.bin")
    with open(bin_path, "wb") as f:
        f.write(b"old")
    os.utime(bin_path, (1000, 1000))
    _write_acf(downloads, {})
    snap = take_verify_snapshot(downloads, APPID, ["555"])

    # Rewritten with a newer mtime -> satisfied even with a newer api time
    with open(bin_path, "wb") as f:
        f.write(b"new content")
    assert workshop_item_satisfied(
        "555", snap, downloads, APPID, api_time_updated=1_900_000_000
    )


def test_resolve_workshop_item_size_priority(tmp_path):
    item_dir = tmp_path / "item"
    item_dir.mkdir()
    (item_dir / "a.bin").write_bytes(b"x" * 10)

    # ACF wins
    assert resolve_workshop_item_size(
        str(item_dir), is_dir=True, is_bin=False,
        acf_item={"size": 999}, api_meta={"file_size": 5},
    ) == 999
    # API next
    assert resolve_workshop_item_size(
        str(item_dir), is_dir=True, is_bin=False,
        acf_item={}, api_meta={"file_size": 5},
    ) == 5
    # Disk walk as last resort
    assert resolve_workshop_item_size(
        str(item_dir), is_dir=True, is_bin=False, acf_item={}, api_meta={},
    ) == 10


def test_load_workshop_metadata_times(tmp_path):
    game_dir = tmp_path / "games" / APPID
    game_dir.mkdir(parents=True)
    (game_dir / "metadata.json").write_text(json.dumps({
        "111": {"time_updated": 123},
        "222": {"time_updated": "456"},
        "333": "garbage",
    }))
    times = load_workshop_metadata_times(str(tmp_path), APPID)
    assert times == {"111": 123, "222": 456}

    assert load_workshop_metadata_times(str(tmp_path / "missing"), APPID) == {}
