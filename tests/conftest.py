import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, jobs  # noqa: E402
from app.core import steam_api  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated META/DOWNLOADS dirs and an initialised database; no network."""
    meta, downloads = tmp_path / "meta", tmp_path / "downloads"
    meta.mkdir()
    downloads.mkdir()
    monkeypatch.setattr(config, "META_DIR", str(meta))
    monkeypatch.setattr(config, "DOWNLOADS_DIR", str(downloads))
    monkeypatch.setattr(config, "STEAMCMD_PATH", str(tmp_path / "missing-steamcmd"))
    monkeypatch.setattr(config, "DEPOTDOWNLOADER_PATH", str(tmp_path / "missing-dd"))
    monkeypatch.setattr(config, "AUTH_PASSWORD", "")
    monkeypatch.setattr(config, "ALLOWED_HOSTS", ["*"])
    monkeypatch.setattr(steam_api, "fetch_app_name", lambda appid: "Test Game")
    monkeypatch.setattr(steam_api, "get_published_file_details", lambda ids, **kw: {})
    # No background worker/scheduler in tests: runs execute only when a test drives them.
    monkeypatch.setattr(jobs.manager, "start", lambda: None)
    monkeypatch.setattr(jobs.manager, "current", None)
    db.init(str(meta / "archiver.db"))
    yield tmp_path
    db.close()


def make_item(downloads, appid: str, item_id: str, files: dict | None = None, folder_name: str | None = None):
    d = downloads / "steamapps" / "workshop" / "content" / appid / item_id
    d.mkdir(parents=True, exist_ok=True)
    for name, data in (files or {"map.bsp": b"x" * 100}).items():
        (d / name).write_bytes(data)
    if folder_name is not None:
        (d / "workshop.json").write_text(json.dumps({"FolderName": folder_name}), encoding="utf-8")
    return d


def details(item_id: str, **kw) -> dict:
    base = {
        "item_id": item_id, "result": 1, "appid": "620", "title": f"Item {item_id}", "description": "",
        "tags": ["Map"], "creator": "1", "file_size": 1000, "file_url": "", "filename": "",
        "preview_url": "", "time_created": 1, "time_updated": 100, "is_collection": False, "banned": False,
    }
    base.update(kw)
    return base
