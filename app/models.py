"""Request models."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .core.steam_api import SORT_KEYS, is_id

SourceType = Literal["workshop", "collection", "items"]


def _check_pages(v: str) -> str:
    v = str(v).strip().lower()
    if v == "all":
        return v
    if v.isdigit() and v.isascii() and 1 <= int(v) <= 1000:
        return str(int(v))
    raise ValueError("pages must be 'all' or a whole number from 1 to 1000")


class JobFields(BaseModel):
    appid: str = Field(default="", max_length=20)
    source_type: SourceType = "workshop"
    source_value: str = Field(default="", max_length=50_000)
    pages: str = "all"
    sort_key: str = "trend_week"
    delay_min: float = Field(default=1.0, ge=0, le=60)
    delay_max: float = Field(default=2.5, ge=0, le=60)
    scrape_only: bool = False
    download_only: bool = False

    @field_validator("appid")
    @classmethod
    def _appid(cls, v: str) -> str:
        v = v.strip()
        if v and not is_id(v):
            raise ValueError("App ID must be numeric")
        return v

    @field_validator("pages")
    @classmethod
    def _pages(cls, v: str) -> str:
        return _check_pages(v)

    @field_validator("sort_key")
    @classmethod
    def _sort(cls, v: str) -> str:
        if v not in SORT_KEYS:
            raise ValueError(f"sort_key must be one of {', '.join(SORT_KEYS)}")
        return v

    @model_validator(mode="after")
    def _consistent(self):
        if self.delay_min > self.delay_max:
            self.delay_min, self.delay_max = self.delay_max, self.delay_min
        if self.scrape_only and self.download_only:
            raise ValueError("scrape_only and download_only can't both be set")
        if self.source_type == "workshop" and not self.appid:
            raise ValueError("An App ID is required for a whole-Workshop job")
        if self.source_type != "workshop" and not self.source_value.strip():
            raise ValueError("A collection or item list is required")
        if self.source_type != "workshop" and (self.scrape_only or self.download_only):
            raise ValueError("Scrape-only / download-only apply to whole-Workshop jobs")
        return self


class RunRequest(JobFields):
    kind: Literal["archive"] = "archive"
    fetch_sizes: bool = False
    force: bool = False


class SavedJobRequest(JobFields):
    name: str = Field(default="", max_length=100)
    game_name: str = Field(default="", max_length=200)
    schedule_type: Literal["none", "interval", "cron"] = "none"
    schedule_value: str = Field(default="", max_length=100)
    enabled: bool = True


class SchedulePreview(BaseModel):
    schedule_type: Literal["none", "interval", "cron"]
    schedule_value: str = Field(default="", max_length=100)


class ResolveInput(BaseModel):
    text: str = Field(max_length=2000)


class ItemIds(BaseModel):
    ids: list[str] = Field(max_length=5000)

    @field_validator("ids")
    @classmethod
    def _ids(cls, v: list[str]) -> list[str]:
        bad = [i for i in v if not is_id(i)]
        if bad:
            raise ValueError("ids must be numeric")
        return list(dict.fromkeys(v))
