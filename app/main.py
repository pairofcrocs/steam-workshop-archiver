"""FastAPI application — Steam Workshop Archiver."""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, config, db
from .core import metadata
from .core.steam_api import is_id
from .jobs import manager
from .routes import archive, runs, system
from .security import SecurityMiddleware

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("swa")

BASE = Path(__file__).parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    os.makedirs(config.META_DIR, exist_ok=True)
    os.makedirs(config.DOWNLOADS_DIR, exist_ok=True)
    db.init(os.path.join(config.META_DIR, "archiver.db"))
    imported_jobs = runs.import_legacy_saved_jobs()
    imported_items = metadata.import_legacy(config.META_DIR)
    if imported_jobs or imported_items:
        log.info("Imported %d saved jobs and %d item records from v1 files", imported_jobs, imported_items)
    manager.start()
    log.info("Steam Workshop Archiver %s started (meta=%s, downloads=%s)", __version__, config.META_DIR, config.DOWNLOADS_DIR)
    yield
    manager.stop()


app = FastAPI(title="Steam Workshop Archiver", version=__version__, lifespan=lifespan)
app.add_middleware(SecurityMiddleware)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.include_router(system.router)
app.include_router(runs.router)
app.include_router(archive.router)
templates = Jinja2Templates(directory=BASE / "templates")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"version": __version__})


@app.get("/workshop/{appid}", response_class=HTMLResponse)
def workshop_page(request: Request, appid: str):
    if not is_id(appid):
        raise HTTPException(404, "Not found")
    return templates.TemplateResponse(request, "workshop.html", {"version": __version__, "appid": appid})
