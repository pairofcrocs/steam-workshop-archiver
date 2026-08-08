"""Tests for app.main: schedule helpers, job log buffer, and API surface."""

import os

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app.main import (
    Job,
    _compute_next_run,
    _validate_schedule,
    app,
)


@pytest.fixture()
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Schedule helpers
# ---------------------------------------------------------------------------
def test_validate_schedule_interval():
    assert _validate_schedule("interval", "24") is None
    assert _validate_schedule("interval", "0") is not None
    assert _validate_schedule("interval", "-3") is not None
    assert _validate_schedule("interval", "abc") is not None


def test_validate_schedule_cron():
    assert _validate_schedule("cron", "0 2 * * *") is None
    assert _validate_schedule("cron", "not a cron") is not None


def test_validate_schedule_none_is_valid():
    assert _validate_schedule("none", "") is None


def test_compute_next_run_interval():
    nxt = _compute_next_run("interval", "2", from_time=1000.0)
    assert nxt == 1000.0 + 2 * 3600
    assert _compute_next_run("interval", "junk") is None


def test_compute_next_run_cron():
    # 02:00 daily from midnight UTC-ish epoch time: just require a future time
    nxt = _compute_next_run("cron", "0 2 * * *", from_time=1000.0)
    assert nxt is not None and nxt > 1000.0
    assert _compute_next_run("cron", "bad expr") is None
    assert _compute_next_run("none", "") is None


# ---------------------------------------------------------------------------
# Job log buffer
# ---------------------------------------------------------------------------
def test_job_log_append_and_read():
    job = Job()
    for i in range(10):
        job.append_log(f"line {i}")
    lines, nxt = job.read_log(0)
    assert lines == [f"line {i}" for i in range(10)]
    assert nxt == 10
    # No new lines
    lines, nxt = job.read_log(nxt)
    assert lines == [] and nxt == 10


def test_job_log_trimming(monkeypatch):
    monkeypatch.setattr(app_main, "_LOG_BUFFER_MAX", 100)
    job = Job()
    for i in range(150):
        job.append_log(f"line {i}")
    # Oldest lines were trimmed; offset advanced
    assert job.log_offset > 0
    assert len(job.log_lines) <= 100

    # A reader positioned before the trim point snaps forward, no crash
    lines, nxt = job.read_log(0)
    assert lines[0] == f"line {job.log_offset}"
    assert nxt == job.log_offset + len(lines)


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------
def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_job_status_idle(client):
    r = client.get("/jobs/status")
    assert r.status_code == 200
    assert r.json()["status"] == "idle"


def test_workshop_list_empty(client):
    r = client.get("/api/workshop")
    assert r.status_code == 200
    assert r.json() == []


def test_numeric_id_validation(client):
    assert client.get("/api/workshop/not-a-number").status_code == 400
    assert client.get("/workshop-image/620/evil../previewimage.png").status_code == 400
    assert client.delete("/api/workshop/620/items/12abc").status_code == 400


def test_delete_missing_archive_404(client):
    assert client.delete("/api/workshop/999999").status_code == 404


def test_saved_job_crud(client):
    # Create
    r = client.post("/api/saved-jobs", json={
        "appid": "620",
        "game_name": "Portal 2",
        "pages": "2",
        "schedule_type": "interval",
        "schedule_value": "24",
    })
    assert r.status_code == 200
    job = r.json()
    assert job["appid"] == "620"
    assert job["next_run_at"] is not None

    # Invalid schedule rejected
    r = client.post("/api/saved-jobs", json={
        "appid": "620",
        "schedule_type": "interval",
        "schedule_value": "-1",
    })
    assert r.status_code == 422

    # Update
    r = client.put(f"/api/saved-jobs/{job['id']}", json={
        "appid": "620",
        "pages": "5",
        "schedule_type": "none",
        "schedule_value": "",
    })
    assert r.status_code == 200
    assert r.json()["pages"] == "5"
    assert r.json()["next_run_at"] is None

    # Delete
    assert client.delete(f"/api/saved-jobs/{job['id']}").status_code == 200
    assert client.delete(f"/api/saved-jobs/{job['id']}").status_code == 404


def test_cross_origin_writes_rejected(client):
    r = client.post(
        "/jobs/cancel",
        headers={"Origin": "https://evil.example", "Host": "archiver.local"},
    )
    assert r.status_code == 403

    # Same-origin (or no Origin) is allowed through
    r = client.post("/jobs/cancel")
    assert r.status_code == 200


def test_outcome_recorded_on_saved_job(client):
    r = client.post("/api/saved-jobs", json={"appid": "777", "game_name": "Test"})
    saved = r.json()
    job = Job(saved_job_id=saved["id"], appid="777")
    job.status = app_main.JobStatus.ERROR
    job.error = "boom"
    app_main._record_job_outcome(job)

    jobs = {j["id"]: j for j in client.get("/api/saved-jobs").json()}
    assert jobs[saved["id"]]["last_status"] == "error"
    assert jobs[saved["id"]]["last_error"] == "boom"
    client.delete(f"/api/saved-jobs/{saved['id']}")
