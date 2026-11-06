"""Paths, .env loading and small helpers shared by all modules.
"""

import logging
import os
import platform
import re
import sys
from pathlib import Path

POWER_SAVER_GUID = "a1841308-3541-4fab-bc81-f71556f20b4a"


IS_WINDOWS = platform.system() == "Windows"


REALTIME_TIMEOUT = 300  # seconds the device streams 0405 messages before the poller re-triggers


HEALTHCHECK_EVERY = 300  # seconds between pings to HEALTHCHECK_URL


IDLE_LOAD_W = 30  # output below this counts as "no load" for the full-battery outage confirm


log = logging.getLogger("anker_ups")


def base_dir() -> Path:
    """Folder of the exe when packaged with PyInstaller, of anker_ups.py otherwise."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Where .env, the database and the log live; DATA_DIR overrides it (Docker: /data)."""
    path = Path(os.environ.get("DATA_DIR") or base_dir())
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_env() -> None:
    env_file = data_dir() / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def id_list(value: str | None) -> list[str]:
    """Comma / space separated Telegram ids from .env."""
    return [x for x in re.split(r"[\s,;]+", value or "") if x]


def as_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
