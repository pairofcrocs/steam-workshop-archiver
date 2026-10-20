import os
import time
from datetime import datetime

import pytest

from app import schedule
from app.core import net


@pytest.mark.parametrize("value", ["inf", "nan", "-1", "0", "abc", "100000"])
def test_bad_intervals(value):
    assert schedule.validate("interval", value)


def test_cron_validation():
    assert schedule.validate("cron", "0 3 * * *") is None
    assert schedule.validate("cron", "0 3 * *")
    assert schedule.validate("cron", "99 3 * * *")
    assert schedule.validate("weekly", "x")


@pytest.fixture
def new_york(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("tzset unavailable")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_cron_uses_local_timezone(new_york):
    after = datetime(2026, 10, 20, 12, 0).timestamp()  # noon local
    nxt = datetime.fromtimestamp(schedule.next_run("cron", "0 3 * * *", after=after))
    assert (nxt.hour, nxt.minute, nxt.day) == (3, 0, 21)


def test_cron_across_dst_change(new_york):
    after = datetime(2026, 10, 20, 12, 0).timestamp()
    runs, t = [], after
    for _ in range(3):
        t = schedule.next_run("cron", "0 3 * * 0", after=t)
        runs.append(datetime.fromtimestamp(t))
    # DST ends 2026-11-01: every run stays at 03:00 local, one per Sunday.
    assert [(r.month, r.day, r.hour) for r in runs] == [(10, 25, 3), (11, 1, 3), (11, 8, 3)]


def test_interval_is_anchored():
    due = 1000.0
    period = 2 * 3600
    assert schedule.next_run("interval", "2", after=due + 5, prev_due=due) == due + period
    # Missed several periods while the app was down: skip to the next future slot.
    assert schedule.next_run("interval", "2", after=due + 2.5 * period, prev_due=due) == due + 3 * period


def test_describe():
    assert schedule.describe("cron", "0 3 * * *") == "Every day at 03:00"
    assert schedule.describe("cron", "30 4 * * 0") == "Every Sunday at 04:30"
    assert schedule.describe("interval", "24") == "Every day"
    assert schedule.describe("interval", "6") == "Every 6 hours"


def test_upcoming_interval():
    runs = schedule.upcoming("interval", "1", count=3)
    assert len(runs) == 3 and runs[1] - runs[0] == pytest.approx(3600)


def test_steam_content_urls():
    assert net.is_steam_content_url("https://images.steamusercontent.com/ugc/1/2/")
    assert net.is_steam_content_url("https://shared.fastly.steamstatic.com/x.jpg")
    assert not net.is_steam_content_url("https://evil.com/steamusercontent.com")
    assert not net.is_steam_content_url("https://steamusercontent.com.evil.com/x")
    assert not net.is_steam_content_url("https://images.steamusercontent.com:8080/x")
    assert not net.is_steam_content_url("file:///etc/passwd")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x.png", "http://localhost/x.png", "http://10.0.0.5/x.png",
    "http://192.168.1.1/x.png", "http://169.254.169.254/latest/meta-data", "http://[::1]/x.png",
    "ftp://example.com/x.png",
])
def test_private_urls_rejected(url):
    assert not net.is_public_url(url)


def test_safe_download_refuses_internal(tmp_path):
    with pytest.raises(net.DownloadError):
        net.safe_download("http://127.0.0.1:8080/api/workshop", str(tmp_path / "x"), allow_public=True)
    assert not os.path.exists(tmp_path / "x")
