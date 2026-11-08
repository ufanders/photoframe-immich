"""Settings loaded from config.yaml, with IMMICH_URL / IMMICH_API_KEY env overrides."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

# The six inks, in the order used for palette indices everywhere in the app.
INK_NAMES = ("black", "white", "yellow", "red", "blue", "green")

# What the ESPHome driver expects to receive for each ink.
PURE_COLORS: dict[str, tuple[int, int, int]] = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "yellow": (255, 255, 0),
    "red": (255, 0, 0),
    "blue": (0, 0, 255),
    "green": (0, 255, 0),
}

# Approximate on-panel appearance of each ink. Error diffusion against these
# (rather than pure RGB) gives far more natural results on Spectra 6.
# Tune these by photographing your panel if colours look off.
DEFAULT_MEASURED: dict[str, tuple[int, int, int]] = {
    "black": (25, 30, 33),
    "white": (232, 232, 224),
    "yellow": (239, 222, 68),
    "red": (178, 30, 32),
    "blue": (33, 87, 186),
    "green": (55, 105, 75),
}


class Source(BaseModel):
    """One pool of photos. Filters within a source are ANDed together."""

    name: str
    weight: float = 1.0
    albums: list[str] = []
    people: list[str] = []
    people_match: Literal["any", "all"] = "any"
    tags: list[str] = []
    favorites: bool | None = None
    smart_query: str | None = None
    taken_after: date | None = None
    taken_before: date | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    min_rating: int | None = Field(default=None, ge=0, le=5)
    max_candidates: int = Field(default=1000, ge=1, le=10000)


class RenderSettings(BaseModel):
    width: int = 800
    height: int = 480
    brightness: float = 1.0
    contrast: float = 1.15
    saturation: float = 1.5
    sharpness: float = 1.0
    dither: Literal["floyd-steinberg", "none"] = "floyd-steinberg"
    portrait_mode: Literal["blur_fill", "crop"] = "blur_fill"
    face_aware_crop: bool = True
    measured_palette: dict[str, tuple[int, int, int]] = Field(
        default_factory=lambda: dict(DEFAULT_MEASURED)
    )

    @field_validator("measured_palette")
    @classmethod
    def _all_inks(cls, v: dict[str, tuple[int, int, int]]):
        merged = dict(DEFAULT_MEASURED)
        unknown = set(v) - set(INK_NAMES)
        if unknown:
            raise ValueError(f"unknown ink(s) {sorted(unknown)}; expected {INK_NAMES}")
        merged.update(v)
        return merged


class DeviceSettings(BaseModel):
    sleep_minutes: int = Field(default=60, ge=1)
    retry_minutes: int = Field(default=15, ge=1)
    stay_awake: bool = False
    low_battery_percent: int = 15
    low_battery_sleep_minutes: int = 360


class Settings(BaseModel):
    immich_url: str = ""
    immich_api_key: str = ""
    state_dir: Path = Path("/data")
    orientation: Literal["landscape_only", "landscape_preferred", "any"] = "landscape_preferred"
    portrait_weight: float = Field(default=0.25, ge=0)
    history_size: int = Field(default=100, ge=0)
    candidate_cache_minutes: int = Field(default=60, ge=1)
    sources: list[Source] = []
    render: RenderSettings = RenderSettings()
    device: DeviceSettings = DeviceSettings()


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    path = Path(path or os.environ.get("CONFIG_PATH", "config.yaml"))
    raw: dict = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
    if url := os.environ.get("IMMICH_URL"):
        raw["immich_url"] = url
    if key := os.environ.get("IMMICH_API_KEY"):
        raw["immich_api_key"] = key
    if state := os.environ.get("STATE_DIR"):
        raw["state_dir"] = state
    settings = Settings.model_validate(raw)
    if not settings.immich_url or not settings.immich_api_key:
        raise ValueError("immich_url and immich_api_key must be set (config.yaml or env)")
    if not settings.sources:
        settings.sources = [Source(name="favorites", favorites=True)]
    return settings
