import os
import threading

import pytest

from app import db, jobs
from app.core import archive, downloader, metadata, steam_api
from app.events import EventBus
from conftest import details, make_item


@pytest.fixture
def fake_steam(env, monkeypatch):
    """Fake Steam: configurable item details; downloads just create folders."""
    catalog: dict[str, dict] = {}
    downloaded: list[str] = []

    def get_details(ids, **kw):
        return {i: catalog[i] for i in ids if i in catalog}

    def fake_download(tasks, ctx):
        for t in tasks:
            if catalog.get(t.item_id, {}).get("_fail"):
                ctx.on_result(t.item_id, False, "Access Denied")
                continue
            make_item(env / "downloads", ctx.appid, t.item_id)
            downloaded.append(t.item_id)
            ctx.on_result(t.item_id, True, "")

    monkeypatch.setattr(steam_api, "get_published_file_details", get_details)
    monkeypatch.setattr(downloader, "download", fake_download)
    monkeypatch.setattr(metadata, "download_images", lambda *a, **kw: None)
    return catalog, downloaded


def run_now(spec: dict) -> dict:
    run = jobs.manager.enqueue({"kind": "archive", **spec})
    active = jobs.manager._claim_next()
    assert active and active.id == run["id"]
    jobs.manager._execute(active)
    return jobs.manager.get(run["id"])


def test_items_run_then_skip_then_update(fake_steam):
    catalog, downloaded = fake_steam
    catalog.update({"1": details("1"), "2": details("2")})
    run = run_now({"source_type": "items", "source_value": "1 2"})
    assert (run["status"], run["downloaded"], run["appid"]) == ("done", 2, "620")

    run = run_now({"source_type": "items", "source_value": "1 2"})
    assert (run["downloaded"], run["skipped"]) == (0, 2)

    catalog["2"] = details("2", time_updated=200)
    run = run_now({"source_type": "items", "source_value": "1 2"})
    assert (run["updated"], run["skipped"]) == (1, 1)


def test_failures_make_run_partial(fake_steam):
    catalog, _ = fake_steam
    catalog.update({"1": details("1"), "2": details("2", _fail=True)})
    run = run_now({"source_type": "items", "source_value": "1 2"})
    assert run["status"] == "partial" and run["failed_ids"] == ["2"]


def test_removed_items_are_unavailable(fake_steam):
    catalog, downloaded = fake_steam
    catalog["1"] = details("1", result=9)
    run = run_now({"source_type": "items", "source_value": "1"})
    assert run["unavailable"] == 1 and downloaded == []


def test_foreign_items_skipped(fake_steam):
    catalog, downloaded = fake_steam
    catalog.update({"1": details("1"), "2": details("2"), "3": details("3", appid="440")})
    run = run_now({"source_type": "items", "source_value": "1 2 3"})
    assert run["appid"] == "620" and sorted(downloaded) == ["1", "2"]


def test_failed_scrape_keeps_manifest(fake_steam, monkeypatch):
    from app.core import scraper

    jobs.write_manifest("620", [{"id": "1", "title": "old"}, {"id": "2", "title": "old"}])
    monkeypatch.setattr(
        scraper, "scrape_workshop",
        lambda *a, **kw: scraper.ScrapeResult(items=[], complete=False, error="HTTP 503"),
    )
    run = run_now({"appid": "620", "source_type": "workshop"})
    assert run["status"] == "error" and "503" in run["error"]
    assert jobs.read_manifest("620") == ["1", "2"]


def test_partial_scrape_does_not_overwrite_manifest(fake_steam, monkeypatch):
    from app.core import scraper

    catalog, _ = fake_steam
    catalog["3"] = details("3")
    jobs.write_manifest("620", [{"id": "1"}, {"id": "2"}])
    monkeypatch.setattr(
        scraper, "scrape_workshop",
        lambda *a, **kw: scraper.ScrapeResult(items=[{"id": "3", "title": "", "author": ""}], complete=False),
    )
    run = run_now({"appid": "620", "source_type": "workshop"})
    assert run["status"] == "partial" and run["downloaded"] == 1
    assert jobs.read_manifest("620") == ["1", "2"]


def test_confirm_timeout_cancels(fake_steam):
    run = jobs.manager.enqueue({"kind": "archive", "appid": "620", "source_type": "workshop"})
    active = jobs.ActiveRun(jobs.run_to_dict(db.query_one("SELECT * FROM runs WHERE id = ?", (run["id"],))))
    tasks = [downloader.Task("1")]
    with pytest.raises(jobs.Cancelled):
        jobs._confirm_gate(active, {"confirm_timeout_min": 0.0005}, tasks, {"1": {"file_size": 5}})
    assert active.cancel_event.is_set()
    active.close()


def test_duplicate_run_is_deduped(env):
    a = jobs.manager.enqueue({"kind": "archive", "appid": "999", "source_type": "workshop"})
    b = jobs.manager.enqueue({"kind": "archive", "appid": "999", "source_type": "workshop"})
    assert a["id"] == b["id"]


def test_cancel_queued_run(env):
    run = jobs.manager.enqueue({"kind": "archive", "appid": "77", "source_type": "workshop"})
    assert jobs.manager.cancel(run["id"])["status"] == "cancelled"
    assert jobs.manager._claim_next() is None  # a cancelled run can't be revived by the worker


def test_dedupe_considers_params(env):
    a = jobs.manager.enqueue({"kind": "archive", "appid": "5", "source_type": "workshop"})
    b = jobs.manager.enqueue({"kind": "archive", "appid": "5", "source_type": "workshop", "scrape_only": True})
    assert a["id"] != b["id"]


def test_metadata_import_legacy(env):
    import json

    game = env / "meta" / "games" / "620"
    game.mkdir(parents=True)
    (game / "metadata.json").write_text(json.dumps({"5": {"title": "Old", "tags": ["A"], "time_updated": 3}}))
    assert metadata.import_legacy(str(env / "meta")) == 1
    assert metadata.get_items("620", ["5"])["5"]["title"] == "Old"
    assert os.path.exists(game / "metadata.json.imported")


def test_removed_result_keeps_old_metadata(env):
    metadata.upsert_details("620", [details("1", title="Keep me")])
    metadata.upsert_details("620", [details("1", result=9, title="")])
    row = metadata.get_items("620", ["1"])["1"]
    assert row["title"] == "Keep me" and row["removed"] == 1


def test_event_bus_replay():
    bus = EventBus(maxlen=3)
    for i in range(5):
        bus.publish("log", line=str(i))
    events, ok = bus.since(3)
    assert ok and [e[1]["line"] for e in events] == ["3", "4"]
    _, ok = bus.since(0)
    assert not ok  # fell out of the ring → client must resync


def test_with_retries_backoff_and_permanent(env):
    calls = []
    results = {}
    ctx = downloader.Context(
        appid="620", log=lambda m: None, cancel_event=threading.Event(),
        on_result=lambda i, ok, r: results.__setitem__(i, (ok, r)),
        settings={"max_retries": 2}, delay_min=0, delay_max=0,
    )
    ctx.sleep = lambda s: False  # skip backoff waits

    def attempt(batch):
        calls.append([t.item_id for t in batch])
        out = {}
        for t in batch:
            if t.item_id == "denied":
                out[t.item_id] = (False, "Access Denied")
            elif t.item_id == "flaky" and len(calls) < 3:
                out[t.item_id] = (False, "Timeout")
            else:
                out[t.item_id] = (True, "")
        return out

    downloader._with_retries([downloader.Task(i) for i in ("ok", "flaky", "denied")], attempt, 30, ctx)
    assert results == {"ok": (True, ""), "flaky": (True, ""), "denied": (False, "Access Denied")}
    assert calls[0] == ["ok", "flaky", "denied"] and calls[1] == ["flaky"]


def test_delete_items_updates_acf(env):
    make_item(env / "downloads", "620", "111")
    path = archive.acf_path("620")
    with open(path, "w") as f:
        f.write('"AppWorkshop"\n{\n\t"WorkshopItemsInstalled"\n\t{\n\t\t"111"\n\t\t{\n\t\t\t"size"\t\t"5"\n\t\t}\n\t}\n}\n')
    assert archive.delete_items("620", ["111"]) == ["111"]
    assert archive.parse_acf("620") == {}
