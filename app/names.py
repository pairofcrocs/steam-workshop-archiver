"""Game name lookup, cached in SQLite so pages never wait on Steam twice."""

import threading

from . import db
from .core import steam_api

_inflight: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def get(appid: str, fetch: bool = True) -> str:
    name = db.get_game_name(appid)
    if name or not fetch:
        return name or f"App {appid}"
    with _guard:
        lock = _inflight.setdefault(appid, threading.Lock())
    with lock:
        name = db.get_game_name(appid)
        if name:
            return name
        name = steam_api.fetch_app_name(appid)
        if name:
            db.set_game_name(appid, name)
            return name
    return f"App {appid}"


def remember(appid: str, name: str) -> None:
    if name and name != f"App {appid}" and db.get_game_name(appid) != name:
        db.set_game_name(appid, name)
