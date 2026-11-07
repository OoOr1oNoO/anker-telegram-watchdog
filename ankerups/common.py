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


def load_env() -> Path | None:
    """Load .env into os.environ (existing variables win); returns the file used, None if none.

    Windows Notepad often saves it as ".env.txt" (with extensions hidden) and may add a BOM,
    so both are accepted.
    """
    env_file = next((f for f in (data_dir() / ".env", data_dir() / ".env.txt") if f.is_file()), None)
    if env_file is None:
        return None
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return env_file


def id_list(value: str | None) -> list[str]:
    """Comma / space separated Telegram ids from .env."""
    return [x for x in re.split(r"[\s,;]+", value or "") if x]


def as_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
