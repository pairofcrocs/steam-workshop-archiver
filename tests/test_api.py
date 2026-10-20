import base64
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from conftest import make_item

H = {"X-SWA": "1"}


@pytest.fixture
def client(env):
    with TestClient(app) as c:
        yield c


def test_csrf_header_required(client):
    r = client.post("/api/runs", json={"appid": "620"})
    assert r.status_code == 403
    r = client.post("/api/saved-jobs", json={"appid": "620"}, headers=H)
    assert r.status_code == 200


def test_security_headers(client):
    r = client.get("/healthz")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_basic_auth(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_PASSWORD", "hunter2")
    assert client.get("/api/system").status_code == 401
    assert client.get("/healthz").status_code == 200
    good = base64.b64encode(b"any:hunter2").decode()
    assert client.get("/api/system", headers={"Authorization": f"Basic {good}"}).status_code == 200
    bad = base64.b64encode(b"any:nope").decode()
    assert client.get("/api/system", headers={"Authorization": f"Basic {bad}"}).status_code == 401


def test_host_allowlist(client, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_HOSTS", ["archiver.lan"])
    assert client.get("/api/system").status_code == 400
    assert client.get("/api/system", headers={"Host": "archiver.lan:8080"}).status_code == 200
    assert client.get("/healthz").status_code == 200  # Docker healthcheck uses 127.0.0.1


def test_workshop_page_validates_appid(client):
    assert client.get("/workshop/abc").status_code == 404
    assert client.get("/workshop/620").status_code == 200


def test_run_validation(client):
    assert client.post("/api/runs", json={"appid": "620", "pages": "0"}, headers=H).status_code == 422
    assert client.post("/api/runs", json={"appid": "6x"}, headers=H).status_code == 422
    assert client.post("/api/runs", json={"source_type": "items"}, headers=H).status_code == 422
    r = client.post("/api/runs", json={"appid": "620", "delay_min": 5, "delay_max": 1}, headers=H)
    assert r.status_code == 200 and r.json()["params"]["delay_min"] == 1


def test_saved_job_rejects_infinite_interval(client):
    r = client.post("/api/saved-jobs", json={"appid": "620", "schedule_type": "interval", "schedule_value": "inf"}, headers=H)
    assert r.status_code == 422
    assert client.get("/api/saved-jobs").status_code == 200


def test_saved_job_crud(client):
    body = {"appid": "620", "schedule_type": "cron", "schedule_value": "0 3 * * *"}
    job = client.post("/api/saved-jobs", json=body, headers=H).json()
    assert job["next_run_at"] and job["game_name"] == "Test Game" and job["enabled"] is True
    body["enabled"] = False
    job = client.put(f"/api/saved-jobs/{job['id']}", json=body, headers=H).json()
    assert job["next_run_at"] is None
    assert len(client.get("/api/saved-jobs").json()) == 1
    assert client.delete(f"/api/saved-jobs/{job['id']}", headers=H).status_code == 200
    assert client.get("/api/saved-jobs").json() == []


def test_schedule_preview(client):
    r = client.post("/api/schedule/preview", json={"schedule_type": "cron", "schedule_value": "0 3 * * *"}, headers=H)
    assert r.json()["valid"] and len(r.json()["next_runs"]) == 3
    r = client.post("/api/schedule/preview", json={"schedule_type": "interval", "schedule_value": "nan"}, headers=H)
    assert not r.json()["valid"]


def test_settings_roundtrip(client):
    r = client.put("/api/settings", json={"steam_api_key": "ABC123", "max_retries": 5}, headers=H)
    assert r.status_code == 200
    s = client.get("/api/settings").json()
    assert s["steam_api_key_set"] and "steam_api_key" not in s and s["max_retries"] == 5
    assert client.put("/api/settings", json={"max_retries": 99}, headers=H).status_code == 422
    assert client.put("/api/settings", json={"bogus": 1}, headers=H).status_code == 422


def test_item_listing_and_zip_names(client, env):
    downloads = env / "downloads"
    make_item(downloads, "620", "111", folder_name="Карта ../../evil")
    make_item(downloads, "620", "222", files={"a.txt": b"hello"})
    r = client.get("/api/workshop/620/items?sort=title")
    assert r.json()["total"] == 2

    r = client.get("/workshop-download/620/111")
    assert r.status_code == 200
    assert "filename*=UTF-8''%D0%9A" in r.headers["content-disposition"]
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert all(".." not in n.split("/") for n in names)

    r = client.get("/api/workshop/620/zip?ids=111,222")
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "222/a.txt" in names and len(names) == 3

    assert client.get("/api/workshop/620/zip?ids=../1").status_code == 400


def test_bulk_delete(client, env):
    downloads = env / "downloads"
    for i in ("1", "2", "3"):
        make_item(downloads, "620", i)
    r = client.post("/api/workshop/620/items/delete", json={"ids": ["1", "2", "9"]}, headers=H)
    assert r.json() == {"deleted": 2}
    assert client.get("/api/workshop/620").json()["item_count"] == 1
    assert client.post("/api/workshop/620/items/delete", json={"ids": ["../x"]}, headers=H).status_code == 422


def test_overview_sizes_are_consistent(client, env):
    make_item(env / "downloads", "620", "5", files={"f": b"x" * 10})
    from app.core import metadata

    metadata.mark_downloaded("620", "5", 100, 10)
    overview = client.get("/api/workshop").json()
    summary = client.get("/api/workshop/620").json()
    assert overview["games"][0]["total_size"] == summary["total_size"] == 10


def test_host_auto_mode():
    from app.security import host_allowed, parse_host

    auto = ["auto"]
    for ok in ("192.168.1.10", "::1", "localhost", "tower", "nas.local", "tower.lan", "box.ts.net"):
        assert host_allowed(ok, auto), ok
    for bad in ("evil.com", "rebind.attacker.net", ""):
        assert not host_allowed(bad, auto), bad
    assert host_allowed("archive.example.com", ["auto", "archive.example.com"])
    assert parse_host("[::1]:8080") == "::1" and parse_host("[::1]") == "::1"
    assert parse_host("Tower.LAN:8080") == "tower.lan" and parse_host("::1") == "::1"


def test_impossible_cron_rejected(client):
    r = client.post("/api/schedule/preview", json={"schedule_type": "cron", "schedule_value": "0 0 31 2 *"}, headers=H)
    assert r.status_code == 200 and not r.json()["valid"]
    r = client.post("/api/saved-jobs", json={"appid": "620", "schedule_type": "cron", "schedule_value": "0 0 31 2 *"}, headers=H)
    assert r.status_code == 422


def test_huge_ids_are_400s_not_500s(client):
    assert client.get("/api/runs/" + "9" * 30).status_code == 422
    assert client.get("/api/runs?offset=" + "9" * 30).status_code == 200


def test_http_direct_string_values(client):
    r = client.put("/api/settings", json={"http_direct": "false"}, headers=H)
    assert r.json()["http_direct"] is False
