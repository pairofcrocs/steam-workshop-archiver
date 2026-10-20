"""Environment configuration and user-editable settings."""

import os
import shutil

from . import db

META_DIR = os.environ.get("META_DIR", "/meta")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")
# Host header allowlist (DNS-rebinding guard). "auto" (default) accepts IPs, localhost
# and LAN-style names; add real domains (e.g. behind a reverse proxy) explicitly. "*" allows any.
ALLOWED_HOSTS = [h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "auto").split(",") if h.strip()] or ["auto"]

STEAMCMD_PATH = os.environ.get("STEAMCMD_PATH", "/opt/steamcmd/steamcmd.sh")
DEPOTDOWNLOADER_PATH = os.environ.get("DEPOTDOWNLOADER_PATH", "/opt/depotdownloader/DepotDownloader")

# Defaults for settings editable in the UI; env vars seed them, the DB overrides.
_DEFAULTS: dict = {
    "steam_api_key": os.environ.get("STEAM_API_KEY", ""),
    "download_backend": os.environ.get("DOWNLOAD_BACKEND", ""),
    "http_direct": os.environ.get("HTTP_DIRECT", "1").strip().lower() not in ("0", "false", "no", "off"),
    "steam_username": os.environ.get("STEAM_USERNAME", ""),
    "max_retries": int(os.environ.get("MAX_RETRIES", "3")),
    "stall_timeout_min": int(os.environ.get("STALL_TIMEOUT_MIN", "20")),
    "confirm_timeout_min": int(os.environ.get("CONFIRM_TIMEOUT_MIN", "60")),
    "metadata_ttl_hours": int(os.environ.get("METADATA_TTL_HOURS", "24")),
    "notify_urls": [u for u in os.environ.get("NOTIFY_URLS", "").split() if u],
    "notify_on": os.environ.get("NOTIFY_ON", "all"),
}

_LIMITS = {
    "max_retries": (0, 10),
    "stall_timeout_min": (1, 24 * 60),
    "confirm_timeout_min": (1, 24 * 60),
    "metadata_ttl_hours": (0, 24 * 365),
}


def steamcmd_available() -> bool:
    return os.path.isfile(STEAMCMD_PATH)


def depotdownloader_available() -> bool:
    return os.path.isfile(DEPOTDOWNLOADER_PATH) or bool(shutil.which(DEPOTDOWNLOADER_PATH))


def get_settings() -> dict:
    settings = dict(_DEFAULTS)
    settings.update(db.get_settings())
    if settings["download_backend"] not in ("steamcmd", "depotdownloader"):
        settings["download_backend"] = (
            "steamcmd" if steamcmd_available() or not depotdownloader_available() else "depotdownloader"
        )
    return settings


def public_settings() -> dict:
    s = get_settings()
    s["steam_api_key_set"] = bool(s.pop("steam_api_key"))
    return s


def update_settings(changes: dict) -> dict:
    """Validate and persist a partial settings update. Raises ValueError."""
    clean: dict = {}
    for key, value in changes.items():
        if value is None:
            continue
        if key == "steam_api_key":
            value = str(value).strip()
            if value and not value.isalnum():
                raise ValueError("Steam API key should be alphanumeric.")
        elif key == "download_backend":
            if value not in ("steamcmd", "depotdownloader"):
                raise ValueError("download_backend must be 'steamcmd' or 'depotdownloader'.")
        elif key == "http_direct":
            if isinstance(value, str):
                value = value.strip().lower() not in ("0", "false", "no", "off", "")
            value = bool(value)
        elif key == "steam_username":
            value = str(value).strip()
            if value and not all(c.isalnum() or c in "_-." for c in value):
                raise ValueError("Steam username contains invalid characters.")
        elif key in _LIMITS:
            lo, hi = _LIMITS[key]
            value = int(value)
            if not lo <= value <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}.")
        elif key == "notify_urls":
            if isinstance(value, str):
                value = value.split()
            value = [str(u).strip() for u in value if str(u).strip()]
        elif key == "notify_on":
            if value not in ("all", "failures", "never"):
                raise ValueError("notify_on must be 'all', 'failures' or 'never'.")
        else:
            raise ValueError(f"Unknown setting: {key}")
        clean[key] = value
    db.set_settings(clean)
    return public_settings()
