"""Run notifications via Apprise (Discord, ntfy, Gotify, email, webhooks, ...)."""

import logging

log = logging.getLogger(__name__)


class NotifyError(Exception):
    pass


def send(urls: list[str], title: str, body: str) -> None:
    """Send to every Apprise URL. Raises NotifyError if none succeed."""
    if not urls:
        raise NotifyError("No notification URLs configured.")
    try:
        import apprise
    except ImportError as exc:
        raise NotifyError("Apprise is not installed.") from exc
    ap = apprise.Apprise()
    valid = [u for u in urls if ap.add(u)]
    if not valid:
        raise NotifyError("None of the notification URLs are valid Apprise URLs.")
    if not ap.notify(title=title, body=body):
        raise NotifyError("Sending failed for every notification URL.")


def notify_run(settings: dict, run: dict) -> None:
    when = settings.get("notify_on", "all")
    failed = run["status"] in ("error", "partial")
    if when == "never" or not settings.get("notify_urls") or (when == "failures" and not failed):
        return
    if run["status"] == "cancelled":
        return
    label = {"done": "finished", "partial": "finished with failures", "error": "failed"}.get(
        run["status"], run["status"]
    )
    title = f"Workshop archive {label}: {run['game_name'] or run['appid']}"
    lines = [
        f"Downloaded {run['downloaded']}, updated {run['updated']}, "
        f"up to date {run['skipped']}, failed {run['failed']}, unavailable {run['unavailable']}."
    ]
    if run.get("error"):
        lines.append(f"Error: {run['error']}")
    if run.get("hint"):
        lines.append(run["hint"])
    try:
        send(settings["notify_urls"], title, "\n".join(lines))
    except NotifyError as exc:
        log.warning("Notification failed: %s", exc)
