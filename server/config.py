"""Environment-driven settings plus loaders for the YAML config files.

Everything the system needs to run is resolved here so the rest of the code never
touches os.environ or reads a YAML file directly.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value is not None and value != "" else default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    database_url: str
    timezone: ZoneInfo
    timezone_name: str
    export_dir: Path
    ingest_api_keys: frozenset[str]
    short_shift_hours: float
    long_shift_hours: float
    chatter_window_seconds: int


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    tz_name = _env("HR_TIMEZONE", "Asia/Makassar")  # site is WITA, not WIB
    keys = {
        k.strip()
        for k in _env("HR_INGEST_API_KEYS", "dev-local-key").split(",")
        if k.strip()
    }
    export_dir = Path(_env("HR_EXPORT_DIR", "data/exports"))
    if not export_dir.is_absolute():
        export_dir = REPO_ROOT / export_dir
    return Settings(
        database_url=_env(
            "DATABASE_URL", "postgresql+psycopg://hr_system:hr_system@localhost:5434/hr_system"
        ),
        timezone=ZoneInfo(tz_name),
        timezone_name=tz_name,
        export_dir=export_dir,
        ingest_api_keys=frozenset(keys),
        short_shift_hours=_env_float("HR_SHORT_SHIFT_HOURS", 6.0),
        long_shift_hours=_env_float("HR_LONG_SHIFT_HOURS", 16.0),
        chatter_window_seconds=_env_int("HR_CHATTER_WINDOW_SECONDS", 90),
    )


# --------------------------------------------------------------------------- #
# YAML config: shifts, roster patterns, holidays, wage mapping                 #
# --------------------------------------------------------------------------- #


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


@dataclass(frozen=True)
class ShiftDef:
    key: str
    name: str
    start: time
    end: time
    window_start: time
    window_end: time
    crosses_midnight: bool
    short_hours: float
    long_hours: float


@dataclass(frozen=True)
class ShiftConfig:
    shifts: dict[str, ShiftDef]
    roster_patterns: dict[str, str]

    def get(self, key: str) -> ShiftDef:
        try:
            return self.shifts[key]
        except KeyError:
            raise KeyError(
                f"Unknown shift {key!r}. Defined: {sorted(self.shifts)}"
            ) from None


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@functools.lru_cache(maxsize=1)
def get_shift_config(path: str | None = None) -> ShiftConfig:
    raw = _load_yaml(Path(path) if path else CONFIG_DIR / "shifts.yaml")
    shifts: dict[str, ShiftDef] = {}
    for key, s in (raw.get("shifts") or {}).items():
        shifts[key] = ShiftDef(
            key=key,
            name=s.get("name", key),
            start=_parse_hhmm(s["start"]),
            end=_parse_hhmm(s["end"]),
            window_start=_parse_hhmm(s["window_start"]),
            window_end=_parse_hhmm(s["window_end"]),
            crosses_midnight=bool(s.get("crosses_midnight", False)),
            short_hours=float(s.get("short_hours", get_settings().short_shift_hours)),
            long_hours=float(s.get("long_hours", get_settings().long_shift_hours)),
        )
    patterns = dict(raw.get("roster_patterns") or {})
    return ShiftConfig(shifts=shifts, roster_patterns=patterns)


@functools.lru_cache(maxsize=1)
def get_holidays(path: str | None = None) -> frozenset[date]:
    raw = _load_yaml(Path(path) if path else CONFIG_DIR / "holidays.yaml")
    out: set[date] = set()
    for item in raw.get("dates") or []:
        if isinstance(item, date):
            out.add(item)
        else:
            out.add(date.fromisoformat(str(item)))
    return frozenset(out)


@dataclass(frozen=True)
class WageMapping:
    codes: dict[str, dict]
    exception_flags: dict[str, dict]

    def component(self, code: str) -> str:
        return (self.codes.get(code) or {}).get("component", "")

    def multiplier(self, code: str):
        return (self.codes.get(code) or {}).get("multiplier")

    def code_blocks_export(self, code: str) -> bool:
        return bool((self.codes.get(code) or {}).get("blocks_export", False))

    def flag_blocks_export(self, flag: str) -> bool:
        return bool((self.exception_flags.get(flag) or {}).get("blocks_export", False))


@functools.lru_cache(maxsize=1)
def get_wage_mapping(path: str | None = None) -> WageMapping:
    raw = _load_yaml(Path(path) if path else CONFIG_DIR / "wage_mapping.yaml")
    return WageMapping(
        codes=dict(raw.get("codes") or {}),
        exception_flags=dict(raw.get("exception_flags") or {}),
    )


@functools.lru_cache(maxsize=1)
def get_export_mapping(path: str | None = None) -> dict:
    return _load_yaml(Path(path) if path else CONFIG_DIR / "export_mapping.yaml")


def reset_caches() -> None:
    """Test helper — drop every cached config so a fresh env/YAML is picked up."""
    for fn in (
        get_settings,
        get_shift_config,
        get_holidays,
        get_wage_mapping,
        get_export_mapping,
    ):
        fn.cache_clear()
