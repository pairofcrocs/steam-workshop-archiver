"""Download backends: SteamCMD (batched), DepotDownloader, and direct HTTP for
legacy single-file items. All share retry/backoff, cancellation and stall detection."""

import os
import queue
import random
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import config
from . import archive
from .net import DownloadError, safe_download

STEAMCMD_BATCH = 30
_STALL_CHECK_SECS = 30

_SC_SUCCESS_RE = re.compile(r"Success\. Downloaded item (\d+)")
_SC_ERROR_RE = re.compile(r"ERROR! Download item (\d+) failed \(([^)]*)\)")
_OWNERSHIP_REASONS = ("access denied", "no subscription", "accessdenied", "nosubscription")
_LOGIN_FAILURE_RE = re.compile(
    r"(Cached credentials not found|password:|Two-factor code|Steam Guard code|"
    r"Invalid Password|FAILED \(Invalid Login|Login Failure|InvalidPassword)",
    re.IGNORECASE,
)

OWNERSHIP_HINT = (
    "Steam refused these downloads. This game's Workshop probably requires a Steam "
    "account that owns the game: cache a login with `swa-steam-login <username>` "
    "and set the Steam username in Settings."
)
LOGIN_HINT = (
    "SteamCMD/DepotDownloader couldn't log in with the cached credentials. Run "
    "`docker exec -it <container> swa-steam-login <username>` to log in once (Steam Guard "
    "prompts are handled there), or clear the Steam username in Settings to use anonymous login."
)


@dataclass
class Task:
    item_id: str
    title: str = ""
    time_updated: int = 0
    file_url: str = ""
    filename: str = ""
    force: bool = False  # re-download: only an explicit success line counts


@dataclass
class Context:
    appid: str
    log: Callable[[str], None]
    cancel_event: threading.Event
    on_result: Callable[[str, bool, str], None]
    settings: dict
    delay_min: float = 1.0
    delay_max: float = 2.5
    hints: set[str] = field(default_factory=set)

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def sleep(self, seconds: float) -> bool:
        """Cancellable sleep; True if cancelled."""
        return self.cancel_event.wait(max(0.0, seconds))

    def polite_pause(self) -> bool:
        lo, hi = sorted((self.delay_min, self.delay_max))
        return self.sleep(random.uniform(lo, hi))


# ---------------------------------------------------------------------------
# Process runner
# ---------------------------------------------------------------------------
@dataclass
class ProcessOutcome:
    returncode: int | None
    cancelled: bool = False
    stalled: bool = False
    lines: list[str] = field(default_factory=list)


def _dir_size(path: str) -> int:
    total = 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def run_process(
    cmd: list[str],
    ctx: Context,
    stall_timeout: float,
    activity_paths: list[str],
    prefix: str,
    env: dict | None = None,
) -> ProcessOutcome:
    """Run *cmd* in its own process group, streaming output to the log. Killed on
    cancel, or when there's no output and no growth in *activity_paths* for
    *stall_timeout* seconds."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def reader():
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            lines.put(raw.decode("utf-8", errors="replace").rstrip())
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    outcome = ProcessOutcome(returncode=None)
    last_activity = time.monotonic()
    last_check = last_activity
    last_size = sum(_dir_size(p) for p in activity_paths)
    try:
        while True:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                line = ""
            if line is None:
                break
            if line:
                last_activity = time.monotonic()
                outcome.lines.append(line)
                ctx.log(f"  {prefix}: {line}")
                if _LOGIN_FAILURE_RE.search(line):
                    ctx.hints.add(LOGIN_HINT)
            if ctx.cancelled():
                outcome.cancelled = True
                break
            now = time.monotonic()
            if now - last_check >= _STALL_CHECK_SECS:
                last_check = now
                size = sum(_dir_size(p) for p in activity_paths)
                if size != last_size:
                    last_size = size
                    last_activity = now
            if now - last_activity > stall_timeout:
                outcome.stalled = True
                ctx.log(f"  {prefix}: no progress for {int(stall_timeout // 60)} min — stopping it.")
                break
    finally:
        if proc.poll() is None:
            _kill_group(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    outcome.returncode = proc.returncode
    return outcome


# ---------------------------------------------------------------------------
# Retry orchestration
# ---------------------------------------------------------------------------
AttemptFn = Callable[[list[Task]], dict[str, tuple[bool, str]]]


def _with_retries(tasks: list[Task], attempt: AttemptFn, batch_size: int, ctx: Context) -> None:
    """First pass in batches, then up to max_retries individual attempts with backoff."""
    max_retries = int(ctx.settings.get("max_retries", 3))
    pending = list(tasks)
    for round_no in range(max_retries + 1):
        if not pending or ctx.cancelled():
            return
        size = batch_size if round_no == 0 else 1
        if round_no > 0:
            backoff = min(300, 10 * 2 ** (round_no - 1))
            ctx.log(f"Retry round {round_no}/{max_retries} for {len(pending)} item(s) in {backoff}s...")
            if ctx.sleep(backoff):
                return
        failed: list[Task] = []
        for start in range(0, len(pending), size):
            if ctx.cancelled():
                return
            batch = pending[start:start + size]
            results = attempt(batch)
            for t in batch:
                ok, reason = results.get(t.item_id, (False, "no result"))
                if ok:
                    ctx.on_result(t.item_id, True, "")
                elif ctx.cancelled():
                    continue
                elif round_no == max_retries or _is_permanent(reason):
                    ctx.on_result(t.item_id, False, reason)
                else:
                    failed.append(t)
            if ctx.cancelled():
                return
            if start + size < len(pending) and ctx.polite_pause():
                return
        pending = failed


def _is_permanent(reason: str) -> bool:
    r = reason.lower()
    return any(k in r for k in _OWNERSHIP_REASONS) or "file not found" in r


# ---------------------------------------------------------------------------
# SteamCMD
# ---------------------------------------------------------------------------
def _steamcmd_attempt(ctx: Context) -> AttemptFn:
    username = ctx.settings.get("steam_username") or "anonymous"
    stall = int(ctx.settings.get("stall_timeout_min", 20)) * 60
    activity = [
        os.path.join(archive.workshop_root(), "downloads", ctx.appid),
        os.path.join(archive.workshop_root(), "temp", ctx.appid),
    ]

    def attempt(batch: list[Task]) -> dict[str, tuple[bool, str]]:
        cmd = [config.STEAMCMD_PATH, "+force_install_dir", config.DOWNLOADS_DIR, "+login", username]
        for t in batch:
            cmd += ["+workshop_download_item", ctx.appid, t.item_id]
        cmd.append("+quit")
        ctx.log(
            f"SteamCMD: {len(batch)} item(s) — "
            + ", ".join(t.item_id for t in batch[:5]) + ("…" if len(batch) > 5 else "")
        )
        try:
            outcome = run_process(cmd, ctx, stall, activity, "steamcmd")
        except OSError as exc:
            ctx.log(f"ERROR: could not start SteamCMD: {exc}")
            return {t.item_id: (False, str(exc)) for t in batch}

        succeeded = set(_SC_SUCCESS_RE.findall("\n".join(outcome.lines)))
        errors = {m.group(1): m.group(2) for m in map(_SC_ERROR_RE.search, outcome.lines) if m}
        acf = archive.parse_acf(ctx.appid)
        on_disk = archive.scan(ctx.appid)
        results: dict[str, tuple[bool, str]] = {}
        for t in batch:
            present = t.item_id in on_disk
            acf_time = acf.get(t.item_id, {}).get("timeupdated", 0)
            fresh = t.time_updated == 0 or acf_time >= t.time_updated
            inferred = fresh and not t.force and t.item_id not in errors and not outcome.cancelled and not outcome.stalled
            if present and (t.item_id in succeeded or inferred):
                results[t.item_id] = (True, "")
            else:
                reason = errors.get(t.item_id) or (
                    "stalled" if outcome.stalled else "cancelled" if outcome.cancelled else "not downloaded"
                )
                results[t.item_id] = (False, reason)
                if any(k in reason.lower() for k in _OWNERSHIP_REASONS) and username == "anonymous":
                    ctx.hints.add(OWNERSHIP_HINT)
        return results

    return attempt


# ---------------------------------------------------------------------------
# DepotDownloader
# ---------------------------------------------------------------------------
_DD_ERROR_RE = re.compile(r"(AccessDenied|NoSubscription|Access Denied|Unable to download|Error|error)[^\n]*")


def _depot_attempt(ctx: Context) -> AttemptFn:
    username = ctx.settings.get("steam_username") or ""
    stall = int(ctx.settings.get("stall_timeout_min", 20)) * 60

    def attempt(batch: list[Task]) -> dict[str, tuple[bool, str]]:
        results = {}
        for t in batch:
            if ctx.cancelled():
                break
            dest = os.path.join(archive.content_dir(ctx.appid), t.item_id)
            existed = os.path.isdir(dest)
            os.makedirs(dest, exist_ok=True)
            cmd = [
                config.DEPOTDOWNLOADER_PATH, "-app", ctx.appid, "-pubfile", t.item_id,
                "-dir", dest, "-max-downloads", "8",
            ]
            if username:
                cmd += ["-username", username, "-remember-password"]
            ctx.log(f"DepotDownloader: {t.item_id} {t.title}".rstrip())
            try:
                outcome = run_process(cmd, ctx, stall, [dest], "depotdownloader")
            except OSError as exc:
                results[t.item_id] = (False, str(exc))
                continue
            has_files = any(n != ".DepotDownloader" for n in os.listdir(dest))
            if outcome.returncode == 0 and has_files:
                results[t.item_id] = (True, "")
            else:
                text = "\n".join(outcome.lines[-20:])
                m = _DD_ERROR_RE.search(text)
                reason = m.group(0).strip() if m else f"exit code {outcome.returncode}"
                if outcome.stalled:
                    reason = "stalled"
                if not existed:  # don't leave a partial new item that looks archived
                    shutil.rmtree(dest, ignore_errors=True)
                if any(k in reason.lower() for k in _OWNERSHIP_REASONS) and not username:
                    ctx.hints.add(OWNERSHIP_HINT)
                results[t.item_id] = (False, reason)
        return results

    return attempt


# ---------------------------------------------------------------------------
# Direct HTTP (legacy single-file items that expose a file_url)
# ---------------------------------------------------------------------------
def _http_attempt(ctx: Context) -> AttemptFn:
    def attempt(batch: list[Task]) -> dict[str, tuple[bool, str]]:
        results = {}
        for t in batch:
            if ctx.cancelled():
                break
            base = archive.content_dir(ctx.appid)
            os.makedirs(base, exist_ok=True)
            fname = archive.safe_name(os.path.basename(t.filename.replace("\\", "/")), f"{t.item_id}.bin")
            staging = tempfile.mkdtemp(prefix=f".{t.item_id}-", dir=base)
            try:
                ctx.log(f"HTTP: {t.item_id} {fname}")
                safe_download(
                    t.file_url, os.path.join(staging, fname), max_bytes=None, timeout=60,
                    cancel_check=ctx.cancelled,
                )
                final = os.path.join(base, t.item_id)
                if os.path.isdir(final):
                    shutil.rmtree(final)
                os.replace(staging, final)
                results[t.item_id] = (True, "")
            except (DownloadError, OSError) as exc:
                results[t.item_id] = (False, str(exc))
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        return results

    return attempt


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def download(tasks: list[Task], ctx: Context) -> None:
    """Download *tasks*, reporting each final outcome via ``ctx.on_result``."""
    settings = ctx.settings
    http_tasks = [t for t in tasks if settings.get("http_direct") and t.file_url]
    rest = [t for t in tasks if t not in http_tasks]

    if http_tasks:
        ctx.log(f"Fetching {len(http_tasks)} legacy item(s) directly from Steam's CDN.")
        _with_retries(http_tasks, _http_attempt(ctx), 1, ctx)
    if not rest or ctx.cancelled():
        return

    backend = settings.get("download_backend", "steamcmd")
    if backend == "depotdownloader":
        if not config.depotdownloader_available():
            _fail_all(rest, ctx, f"DepotDownloader not found at {config.DEPOTDOWNLOADER_PATH}")
            return
        _with_retries(rest, _depot_attempt(ctx), 1, ctx)
    else:
        if not config.steamcmd_available():
            _fail_all(rest, ctx, f"SteamCMD not found at {config.STEAMCMD_PATH}")
            return
        _with_retries(rest, _steamcmd_attempt(ctx), STEAMCMD_BATCH, ctx)


def _fail_all(tasks: list[Task], ctx: Context, reason: str) -> None:
    ctx.log(f"ERROR: {reason}")
    for t in tasks:
        ctx.on_result(t.item_id, False, reason)
