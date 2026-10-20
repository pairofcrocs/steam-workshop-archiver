# HTTP API (v2)

All JSON. Errors return `{"detail": "<message>"}` with a 4xx/5xx status.

**CSRF guard:** every non-GET request must send the header `X-SWA: 1`
(requests without it get `403`). This forces a CORS preflight, so other
sites can't drive the API from a visitor's browser.

**Auth:** when `AUTH_PASSWORD` is set, every route except `/healthz` needs
HTTP Basic auth (any username).

Times are Unix epoch seconds (float). Sizes are bytes (int).

---

## Objects

### Run
One execution of a job: queued, running or finished.

```json
{
  "id": 12,
  "kind": "archive",               // "archive" | "metadata"
  "trigger": "manual",             // "manual" | "schedule" | "retry"
  "saved_job_id": "uuid-or-null",
  "appid": "620",
  "game_name": "Portal 2",
  "source_type": "workshop",       // "workshop" | "collection" | "items"
  "source_value": "",              // collection id, or newline-separated item ids
  "params": {
    "pages": "all", "sort_key": "trend_week",
    "delay_min": 1.0, "delay_max": 2.5,
    "scrape_only": false, "download_only": false,
    "fetch_sizes": false, "force": false
  },
  "status": "running",             // queued | running | waiting_confirm | done | partial | error | cancelled
  "phase": "downloading",          // resolving | metadata | downloading | images | finished
  "queued_at": 0, "started_at": 0, "finished_at": null,
  "total": 120,        // items considered in this run
  "done": 40,          // items processed so far (any outcome)
  "downloaded": 30,    // newly downloaded
  "updated": 5,        // re-downloaded because the workshop copy changed
  "skipped": 4,        // already up to date
  "failed": 1,
  "unavailable": 0,    // removed / private on the Workshop (local copy kept)
  "failed_ids": ["123"],
  "bytes_estimate": 0,
  "error": "",         // fatal error message
  "hint": ""           // human hint, e.g. "This game probably needs a Steam account that owns it"
}
```

`done` / `partial` (finished with some failures) / `error` / `cancelled` are terminal.

### SavedJob

```json
{
  "id": "uuid", "name": "", "appid": "620", "game_name": "Portal 2",
  "source_type": "workshop", "source_value": "",
  "pages": "all", "sort_key": "trend_week",
  "delay_min": 1.0, "delay_max": 2.5,
  "scrape_only": false, "download_only": false,
  "schedule_type": "cron",         // "none" | "interval" | "cron"
  "schedule_value": "0 3 * * *",   // hours for interval, 5-field cron for cron
  "enabled": true,
  "next_run_at": 0, "last_run_at": null, "last_status": "done",
  "created_at": 0,
  "schedule_description": "Every day at 03:00"   // read-only
}
```

Several saved jobs may exist per game (e.g. one per collection).

### Sort keys
`trend_today`, `trend_week`, `trend_month`, `trend_3months`, `trend_6months`,
`trend_year`, `trend_alltime`, `mostrecent`, `lastupdated`,
`totaluniquesubscribers`, `random`.

`pages` is `"all"` or an integer string `>= 1`.

---

## System

| Method | Path | Notes |
|---|---|---|
| GET | `/healthz` | `{"ok": true}`; no auth |
| GET | `/api/system` | see below |
| GET | `/api/settings` | current settings (secrets masked) |
| PUT | `/api/settings` | partial update; returns settings |
| POST | `/api/settings/test-notification` | sends a test notification → `{"ok": true}` or 4xx with detail |

`GET /api/system`:
```json
{
  "version": "2.0.0", "tz": "America/New_York", "auth_enabled": false,
  "backends": {
    "steamcmd": {"available": true, "path": "/meta/steamcmd/steamcmd.sh"},
    "depotdownloader": {"available": true, "path": "/opt/depotdownloader/DepotDownloader"}
  },
  "disk": {"path": "/downloads", "total": 0, "used": 0, "free": 0}
}
```

Settings object (GET shape; PUT accepts any subset, `steam_api_key: ""` clears it,
omitting it leaves it unchanged):
```json
{
  "steam_api_key_set": true,          // GET only
  "steam_api_key": "",                // PUT only (write-only)
  "download_backend": "steamcmd",     // "steamcmd" | "depotdownloader"
  "http_direct": true,                // fetch legacy single-file items straight from Steam's CDN
  "steam_username": "",               // empty = anonymous; credentials must be cached first (see README)
  "max_retries": 3,
  "stall_timeout_min": 20,
  "confirm_timeout_min": 60,
  "metadata_ttl_hours": 24,
  "notify_urls": ["discord://..."],   // Apprise URLs
  "notify_on": "all"                  // "all" | "failures" | "never"
}
```

## Search / input resolution

| Method | Path | Notes |
|---|---|---|
| GET | `/api/search?q=portal` | `[{"appid","name","logo"}]`; `502` if Steam is unreachable |
| POST | `/api/resolve-input` | body `{"text": "..."}` |

`/api/resolve-input` accepts an App ID, a store/workshop/app URL, a Workshop item
URL/ID, or a collection URL. Returns one of:
```json
{"kind": "app", "appid": "620", "name": "Portal 2"}
{"kind": "item", "appid": "620", "name": "Portal 2", "item_id": "123", "title": "..."}
{"kind": "collection", "appid": "620", "name": "Portal 2", "collection_id": "456", "title": "...", "item_count": 42}
```
`400` when it can't be understood.

## Runs (queue + history)

| Method | Path | Notes |
|---|---|---|
| POST | `/api/runs` | enqueue; body below → Run |
| GET | `/api/runs?limit=50&offset=0&appid=620` | `{"runs": [Run], "total": n}`, newest first |
| GET | `/api/runs/{id}` | Run |
| POST | `/api/runs/{id}/cancel` | queued → cancelled immediately; running → stops |
| POST | `/api/runs/{id}/confirm` | continue a run in `waiting_confirm` |
| POST | `/api/runs/{id}/retry` | enqueue a new run for `failed_ids` → Run |
| GET | `/api/runs/{id}/log` | `text/plain` full log; `?download=1` adds attachment header |
| DELETE | `/api/runs/{id}` | delete a finished run from history |

`POST /api/runs` body:
```json
{
  "kind": "archive",
  "appid": "620",                 // optional for collection/items (resolved server-side)
  "source_type": "workshop",
  "source_value": "",             // collection id/URL, or item ids/URLs separated by whitespace/commas
  "pages": "all", "sort_key": "trend_week",
  "delay_min": 1.0, "delay_max": 2.5,
  "scrape_only": false, "download_only": false,
  "fetch_sizes": false,           // pause for confirmation with a size estimate
  "force": false                  // re-download even if up to date
}
```
Identical work already queued/running returns the existing Run (no duplicate).

## Live events — `GET /api/events` (Server-Sent Events)

Each event has an `id:` (global sequence number) and JSON `data:` with a `type`.
Browsers' `EventSource` resends `Last-Event-ID` on reconnect; the server replays
missed events, or sends a fresh `snapshot` if they're too old.

| type | payload |
|---|---|
| `snapshot` | `{"current": Run\|null, "queue": [Run], "log": ["line", ...], "confirm": {...}\|null}` (log = tail of current run; confirm = same payload as the `confirm` event while waiting) |
| `run_queued` | `{"run": Run}` |
| `run_started` | `{"run": Run}` |
| `run_updated` | `{"run": Run}` (status/phase/counters changed; throttled) |
| `log` | `{"run_id": 12, "line": "..."}` |
| `confirm` | `{"run_id": 12, "items": 80, "bytes": 123456, "timeout_at": 0}` |
| `run_finished` | `{"run": Run}` |
| `saved_jobs_changed` | `{}` |
| `archive_changed` | `{"appid": "620"}` |

A `: ping` comment is sent every 15 s.

## Saved jobs / schedules

| Method | Path | Notes |
|---|---|---|
| GET | `/api/saved-jobs` | `[SavedJob]` |
| POST | `/api/saved-jobs` | create → SavedJob |
| PUT | `/api/saved-jobs/{id}` | full update → SavedJob |
| DELETE | `/api/saved-jobs/{id}` | |
| POST | `/api/saved-jobs/{id}/run` | enqueue now (doesn't move the schedule) → Run |
| POST | `/api/schedule/preview` | `{"schedule_type","schedule_value"}` → `{"valid": true, "error": "", "description": "Every day at 03:00", "next_runs": [epoch, epoch, epoch]}` |

Create/update body = SavedJob fields minus `id`, `next_run_at`, `last_*`, `created_at`.

## Archive

| Method | Path | Notes |
|---|---|---|
| GET | `/api/workshop` | overview, below |
| GET | `/api/workshop/{appid}` | game summary, below |
| GET | `/api/workshop/{appid}/items` | paged list, below |
| GET | `/api/workshop/{appid}/items/{item_id}` | item detail, below |
| DELETE | `/api/workshop/{appid}?purge_metadata=1&delete_saved_jobs=1` | delete a game's archive (`409` while a run for it is active) |
| DELETE | `/api/workshop/{appid}/items/{item_id}` | delete one item |
| POST | `/api/workshop/{appid}/items/delete` | body `{"ids": [...]}` → `{"deleted": n}` |
| POST | `/api/workshop/{appid}/refresh-metadata` | enqueue a `metadata` run → Run |
| GET | `/workshop-download/{appid}/{item_id}` | one item as `.zip` (or raw `.bin`) |
| GET | `/api/workshop/{appid}/zip?ids=1,2,3` | several items in one `.zip` (max 500 ids) |
| GET | `/workshop-image/{appid}/{item_id}` | preview image |
| GET | `/api/workshop/{appid}/desc-images/{item_id}/{filename}` | cached description image |

`GET /api/workshop`:
```json
{
  "games": [{
    "appid": "620", "game_name": "Portal 2",
    "item_count": 30, "total_size": 0, "removed_count": 0,
    "last_run": {"id": 12, "status": "done", "finished_at": 0} ,
    "saved_job_ids": ["uuid"]
  }],
  "total_items": 30, "total_size": 0,
  "disk": {"path": "/downloads", "total": 0, "used": 0, "free": 0}
}
```

`GET /api/workshop/{appid}`:
```json
{"appid": "620", "game_name": "Portal 2", "item_count": 30, "total_size": 0,
 "removed_count": 0, "no_metadata_count": 0, "outdated_count": 0,
 "tags": [{"tag": "Singleplayer", "count": 12}]}
```

`GET /api/workshop/{appid}/items` query params:
- `q` — title / item-id substring
- `tags` — comma-separated; `tag_mode=all` (default) or `any`
- `sort` — `updated_desc` (default), `updated_asc`, `title`, `size_desc`, `size_asc`, `downloaded_desc`
- `filter` — `removed`, `no_metadata`, `outdated` (empty = all)
- `offset` (default 0), `limit` (default 60, max 500)

```json
{"total": 30, "offset": 0, "limit": 60, "items": [{
  "id": "123", "title": "Map", "tags": ["Singleplayer"], "size": 1024,
  "time_updated": 0, "downloaded_at": 0, "has_preview": true, "is_bin": false,
  "removed": false, "has_metadata": true, "outdated": false,
  "excerpt": "First ~160 characters of the description, BBCode stripped"
}]}
```
`outdated` = the Workshop has a newer version than the local copy.

Item detail adds: `description` (raw BBCode), `desc_image_map` (`{original_url: local_url}`),
`creator`, `time_created`, `file_size`, `steam_url`.
