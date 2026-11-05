# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "anker-solix-api @ https://github.com/thomluther/anker-solix-api/archive/refs/tags/v3.9.1.tar.gz",
#     "matplotlib",
# ]
# ///
"""Anker SOLIX power station watchdog via Anker cloud MQTT.

- Telegram alert when AC input is lost / restored, with the outage duration.
- Charge alerts per battery (default 75 / 50 / 25 %) and a critical charge alert.
- Bot buttons: Status, Light (station light modes), Stats (charts, outages, energy).
- Daily summary; samples and outages are kept in SQLite next to the script.
- Switches Windows to the Power saver plan on outage, restores the previous plan after.

Config: environment variables or a .env file next to the script/exe (see .env.example).
"""

import argparse
import asyncio
import contextlib
import io
import json
import logging
import logging.handlers
import os
import platform
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import ClientSession
from anker_solix_api.api import AnkerSolixApi
from anker_solix_api.mqtt_factory import SolixMqttDeviceFactory
from anker_solix_api.mqtt_pps import MODELS as PPS_MODELS

# persistent reply keyboard; button texts come back as normal messages.
# Telegram gives all buttons in a row the same width, so Status gets its own row.
def main_keyboard(light: bool = True) -> str:
    """Light is left out once the station is known to have none (no light_mode field)."""
    row = [{"text": "📈 Stats"}, {"text": "⚙️ Settings"}]
    return json.dumps({"keyboard": [[{"text": "📊 Status"}], [{"text": "🔦 Light"}, *row] if light else row],
                       "resize_keyboard": True, "is_persistent": True})
STATS_MENU = json.dumps({"inline_keyboard": [
    [{"text": "Chart 24 h", "callback_data": "stats:chart24"},
     {"text": "Chart 7 days", "callback_data": "stats:chart168"}],
    [{"text": "Outages", "callback_data": "stats:outages"},
     {"text": "Energy", "callback_data": "stats:energy"}]]})
POWER_SAVER_GUID = "a1841308-3541-4fab-bc81-f71556f20b4a"
IS_WINDOWS = platform.system() == "Windows"
REALTIME_TIMEOUT = 300  # seconds the device streams 0405 messages before the poller re-triggers

log = logging.getLogger("anker_ups")


def base_dir() -> Path:
    """Folder of the exe when packaged with PyInstaller, of the script otherwise."""
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def load_env() -> None:
    env_file = base_dir() / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class BotHandlers:
    """Callbacks the Telegram thread uses to read state and act on the station."""

    status: Callable[[], str]
    controller: "Controller"
    stats: "Stats | None" = None


# button texts and commands -> action; in groups the bot sees every message (privacy mode
# off), so only exact button texts and commands are handled, not any text with "status" in it
BUTTON_ACTIONS = {"📊 status": "status", "🔦 light": "light", "📈 stats": "stats",
                  "⚙️ settings": "settings", "start": "status"}


def id_list(value: str | None) -> list[str]:
    """Comma / space separated Telegram ids from .env."""
    return [x for x in re.split(r"[\s,;]+", value or "") if x]


class Telegram:
    """Sends messages from a background thread and retries until the network is back.

    Alerts go to every chat in TG_CHAT_ID (people and / or groups). The bot buttons work in
    each of these chats via long polling (getUpdates): 📊 Status, 🔦 Light, 📈 Stats and
    ⚙️ Settings; replies go to the chat that asked. When TG_ADMIN_IDS is set, only those
    users can change settings or control the station; everyone else can only look.
    """

    def __init__(self, token: str | None, chat_ids: str | None, admin_ids: str | None = None):
        self.chats = id_list(chat_ids)
        self.admins = set(id_list(admin_ids))
        self.enabled = bool(token and self.chats)
        self.base = f"https://api.telegram.org/bot{token}"
        self.username = ""  # bot username, to skip /commands@other_bot in groups
        # (kind, payload, extra, chat): ("text", text, reply_markup, chat) or
        # ("photo", png bytes, caption, chat); chat None = all configured chats
        self.queue: queue.Queue[tuple[str, str | bytes, str, str | None]] = queue.Queue()
        self.has_light: Callable[[], bool | None] = lambda: None  # set by listen_commands
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True).start()
        else:
            log.warning("TG_BOT_TOKEN/TG_CHAT_ID not set, Telegram disabled")

    def _call(self, method: str, params: dict, timeout: float = 10) -> dict:
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(f"{self.base}/{method}", data=data, timeout=timeout) as resp:
            return json.loads(resp.read())

    def _send_photo(self, chat: str, png: bytes, caption: str) -> None:
        boundary = f"----anker{int(time.time() * 1000)}"
        parts = []
        for name, value in (("chat_id", chat), ("caption", caption),
                            ("reply_markup", self.keyboard())):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                         f"{value}\r\n".encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; '
                     f'filename="chart.png"\r\nContent-Type: image/png\r\n\r\n'.encode()
                     + png + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(f"{self.base}/sendPhoto", data=b"".join(parts),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()

    def keyboard(self) -> str:
        return main_keyboard(self.has_light() is not False)

    def send(self, text: str) -> None:
        """Alert to all configured chats."""
        log.info("ALERT: %s", text)
        if self.enabled:
            self.queue.put(("text", f"{time.strftime('%H:%M:%S')} {text}", self.keyboard(), None))

    def reply(self, chat: str, text: str, markup: str | None = None) -> None:
        self.queue.put(("text", text, markup or self.keyboard(), chat))

    def send_photo(self, png: bytes, caption: str, chat: str | None = None) -> None:
        if self.enabled:
            self.queue.put(("photo", png, caption, chat))

    def _worker(self) -> None:
        while True:
            kind, payload, extra, target = self.queue.get()
            for chat in [target] if target else self.chats:
                while True:
                    try:
                        if kind == "photo":
                            self._send_photo(chat, payload, extra)
                        else:
                            self._call("sendMessage", {"chat_id": chat, "text": payload,
                                                       "reply_markup": extra})
                        break
                    except urllib.error.HTTPError as exc:
                        if exc.code != 429 and exc.code < 500:
                            # wrong id, bot blocked or removed from the group: retrying won't help
                            log.warning("Telegram send to %s failed: %s %s", chat, exc.code,
                                        exc.read().decode(errors="replace")[:200])
                            break
                        log.warning("Telegram send failed (%s), retrying in 15s", exc)
                        time.sleep(15)
                    except Exception as exc:  # no internet during outage is expected
                        log.warning("Telegram send failed (%s), retrying in 15s", exc)
                        time.sleep(15)

    def listen_commands(self, handlers: BotHandlers) -> None:
        self.has_light = handlers.controller.has_light
        if self.enabled:
            threading.Thread(target=self._poll_commands, args=(handlers,), daemon=True).start()

    def _poll_commands(self, handlers: BotHandlers) -> None:
        with contextlib.suppress(Exception):
            self.username = self._call("getMe", {})["result"]["username"].lower()
        with contextlib.suppress(Exception):
            self._call("setMyCommands", {"commands": json.dumps(
                [{"command": "status", "description": "Anker status"},
                 {"command": "light", "description": "Station light"},
                 {"command": "stats", "description": "Charts, outages, energy"},
                 {"command": "settings", "description": "Notifications and station settings"}])})
        offset = None
        while True:
            try:
                params = {"timeout": 50, "allowed_updates": '["message", "callback_query"]'}
                if offset is not None:
                    params["offset"] = offset
                updates = self._call("getUpdates", params, timeout=60).get("result", [])
            except Exception as exc:
                log.warning("Telegram getUpdates failed (%s), retrying in 15s", exc)
                time.sleep(15)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    self._handle_update(upd, handlers)
                except Exception:
                    log.exception("Telegram update handling failed")

    def _handle_stats(self, chat: str, action: str, stats: "Stats") -> str:
        if action.startswith("chart"):
            hours = int(action.removeprefix("chart"))
            png = stats.chart_png(hours)
            if png is None:
                return "Not enough data yet"
            self.send_photo(png, f"📈 Last {hours} h" if hours < 48 else f"📈 Last {hours // 24} days", chat)
            return "Chart sent"
        self.reply(chat, stats.outages_text() if action == "outages" else stats.energy_text())
        return "Sent"

    def _edit(self, msg: dict, view: tuple[str, str]) -> None:
        with contextlib.suppress(Exception):  # unchanged text makes Telegram return 400
            self._call("editMessageText", {"chat_id": msg["chat"]["id"], "message_id": msg.get("message_id"),
                                           "text": view[0], "reply_markup": view[1]})

    def _allowed_chat(self, msg: dict) -> str | None:
        chat = str(msg.get("chat", {}).get("id"))
        if chat in self.chats:
            return chat
        # logged so the id of a new group or person can be copied into TG_CHAT_ID
        log.info("Ignoring Telegram chat %s (%s), not in TG_CHAT_ID", chat,
                 msg.get("chat", {}).get("title") or msg.get("chat", {}).get("username") or "?")
        return None

    def _action(self, text: str) -> str | None:
        text = text.strip().lower()
        if text.startswith("/"):
            cmd, _, bot = text[1:].split()[0].partition("@") if len(text) > 1 else ("", "", "")
            if bot and self.username and bot != self.username:
                return None  # command for another bot in the group
            return BUTTON_ACTIONS.get(cmd, cmd if cmd in BUTTON_ACTIONS.values() else None)
        return BUTTON_ACTIONS.get(text)

    def _handle_update(self, upd: dict, h: BotHandlers) -> None:
        if cb := upd.get("callback_query"):
            msg = cb.get("message") or {}
            if not (chat := self._allowed_chat(msg)):
                return
            kind, _, rest = (cb.get("data") or "").partition(":")
            toast = ""
            if kind == "stats" and h.stats:
                toast = self._handle_stats(chat, rest, h.stats)
            elif kind == "m":
                self._edit(msg, h.controller.render(rest))
            elif kind == "a":
                user = str(cb.get("from", {}).get("id"))
                if self.admins and user not in self.admins:
                    toast = "Only admins can change this"
                    log.info("Denied %s from user %s", rest, user)
                else:
                    key, _, value = rest.partition(":")
                    toast, menu = h.controller.act(key, value)
                    self._edit(msg, h.controller.render(menu))
            self._call("answerCallbackQuery", {"callback_query_id": cb["id"], "text": toast})
            return
        msg = upd.get("message") or {}
        if not (action := self._action(msg.get("text") or "")) or not (chat := self._allowed_chat(msg)):
            return
        if action == "light":
            if self.has_light() is False:  # also refreshes the keyboard without the button
                self.reply(chat, "This station has no light")
            else:
                self.reply(chat, *h.controller.render("light"))
        elif action == "settings":
            self.reply(chat, *h.controller.render("root"))
        elif action == "stats":
            if h.stats:
                self.reply(chat, "📈 Statistics", STATS_MENU)
        else:
            self.reply(chat, h.status())


class PowerPlan:
    """Windows power plan switching via powercfg; logs only on other OSes or in dry-run.

    The plan active before the outage is kept in a file next to the exe, so it is
    restored even if the program was restarted while running on battery.
    POWER_PLAN_BATTERY / POWER_PLAN_GRID in .env override the plan GUIDs; when
    POWER_PLAN_GRID is empty the plan that was active before the outage is restored.
    """

    def __init__(self, dry_run: bool):
        self.enabled = IS_WINDOWS and not dry_run
        self.state_file = base_dir() / "anker_ups.plan"
        self.battery_guid = (os.environ.get("POWER_PLAN_BATTERY") or POWER_SAVER_GUID).strip().lower()
        self.grid_guid = (os.environ.get("POWER_PLAN_GRID") or "").strip().lower()

    @staticmethod
    def _powercfg(*args: str) -> str:
        # stdin must be explicit: a --noconsole exe has no valid stdin handle to inherit
        result = subprocess.run(
            ["powercfg", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            raise RuntimeError(f"powercfg {' '.join(args)} failed ({result.returncode}): "
                               f"{(result.stdout + result.stderr).strip()}")
        return result.stdout

    def _active_guid(self) -> str | None:
        match = re.search(r"[0-9a-f]{8}-[0-9a-f-]{27}", self._powercfg("/getactivescheme"), re.I)
        return match.group(0).lower() if match else None

    def log_plans(self) -> None:
        if IS_WINDOWS:
            try:
                log.info("[power] plans:\n%s", self._powercfg("/list").strip())
                log.info("[power] battery plan %s, grid plan %s", self.battery_guid,
                         self.grid_guid or "previous")
            except Exception:
                log.exception("[power] cannot list plans")

    def power_saver(self) -> None:
        if not self.enabled:
            log.info("[power] would switch to %s", self.battery_guid)
            return
        try:
            active = self._active_guid()
            if not self.state_file.exists():
                self.state_file.write_text(
                    active if active and active != self.battery_guid else "", encoding="utf-8")
            if (self.battery_guid == POWER_SAVER_GUID
                    and POWER_SAVER_GUID not in self._powercfg("/list").lower()):
                # Windows 11 often hides the built-in plan; restore it under its original GUID
                self._powercfg("-duplicatescheme", POWER_SAVER_GUID, POWER_SAVER_GUID)
            self._powercfg("/setactive", self.battery_guid)
            log.info("[power] switched %s -> %s, now active %s",
                     active, self.battery_guid, self._active_guid())
        except Exception:
            log.exception("[power] failed to switch to battery plan")

    def restore(self) -> None:
        if not self.enabled:
            log.info("[power] would restore %s", self.grid_guid or "previous plan")
            return
        if not self.state_file.exists():
            return
        try:
            target = self.grid_guid or self.state_file.read_text(encoding="utf-8").strip()
            if target:
                self._powercfg("/setactive", target)
            self.state_file.unlink()
            log.info("[power] restored %s, now active %s", target or "nothing", self._active_guid())
        except Exception:
            log.exception("[power] failed to restore plan")


def fmt_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def fmt_energy(kwh: float) -> str:
    return f"{kwh:.2f} kWh" if kwh >= 1 else f"{kwh * 1000:.0f} Wh"


def show_popup(title: str, text: str) -> None:
    """Topmost Windows message box in a background thread; log only elsewhere."""
    if not IS_WINDOWS:
        log.info("[popup] %s: %s", title, text)
        return
    import ctypes  # noqa: PLC0415 - Windows only

    flags = 0x30 | 0x1000 | 0x40000  # MB_ICONWARNING | MB_SYSTEMMODAL | MB_TOPMOST
    threading.Thread(target=ctypes.windll.user32.MessageBoxW,
                     args=(None, text, title, flags), daemon=True).start()


class Stats:
    """Minute samples and outages in SQLite next to the script, for charts and summaries.

    Telemetry arrives every 3-5 s; it is averaged per minute, so energy is the sum of
    minute averages (W * 60 s). Used from the asyncio loop and the Telegram thread.
    """

    def __init__(self, path: Path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY, ac_in REAL, out REAL,
                                                soc INTEGER, grid INTEGER);
            CREATE TABLE IF NOT EXISTS outages (id INTEGER PRIMARY KEY, start INTEGER,
                                                end INTEGER, soc_start INTEGER, soc_end INTEGER);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        self.minute: int | None = None
        self.acc: list[tuple[float, float, int, bool]] = []

    def _exec(self, sql: str, args: tuple = ()) -> list:
        with self.lock:
            rows = self.db.execute(sql, args).fetchall()
            self.db.commit()
            return rows

    def add_reading(self, ac_in: float, out: float, soc: int, grid: bool) -> None:
        minute = int(time.time() // 60)
        if self.minute is not None and minute != self.minute and self.acc:
            n = len(self.acc)
            self._exec("INSERT OR REPLACE INTO samples VALUES (?, ?, ?, ?, ?)", (
                self.minute * 60, sum(a[0] for a in self.acc) / n, sum(a[1] for a in self.acc) / n,
                self.acc[-1][2], int(all(a[3] for a in self.acc))))
            self.acc = []
        self.minute = minute
        self.acc.append((ac_in or 0, out or 0, soc, grid))

    def meta(self, key: str, value: str | None = None) -> str | None:
        if value is not None:
            self._exec("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
            return value
        rows = self._exec("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0][0] if rows else None

    def open_outage(self) -> int | None:
        rows = self._exec("SELECT start FROM outages WHERE end IS NULL ORDER BY start DESC LIMIT 1")
        return rows[0][0] if rows else None

    def outage_started(self, start: float, soc: int) -> None:
        if self.open_outage() is None:
            self._exec("INSERT INTO outages (start, soc_start) VALUES (?, ?)", (int(start), soc))

    def outage_ended(self, end: float, soc: int) -> None:
        self._exec("UPDATE outages SET end = ?, soc_end = ? WHERE end IS NULL", (int(end), soc))

    def energy_kwh(self, since: float) -> tuple[float, float]:
        rows = self._exec("SELECT COALESCE(SUM(ac_in), 0), COALESCE(SUM(out), 0) FROM samples "
                          "WHERE ts >= ?", (int(since),))
        return rows[0][0] * 60 / 3.6e6, rows[0][1] * 60 / 3.6e6

    def outages_between(self, start: float, end: float) -> list[tuple]:
        # outages overlapping the window; an open one counts until now
        return self._exec("SELECT start, COALESCE(end, CAST(strftime('%s','now') AS INTEGER)), "
                          "soc_start, soc_end FROM outages WHERE start < ? AND "
                          "(end IS NULL OR end > ?) ORDER BY start", (int(end), int(start)))

    def energy_text(self) -> str:
        now = datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        periods = [("Today", today), ("7 days", now - timedelta(days=7)),
                   ("30 days", now - timedelta(days=30)), ("All time", datetime.fromtimestamp(0))]
        lines = ["⚡ Energy through the station (in / out)"]
        for label, since in periods:
            e_in, e_out = self.energy_kwh(since.timestamp())
            lines.append(f"{label}: {fmt_energy(e_in)} / {fmt_energy(e_out)}")
        return "\n".join(lines)

    def outages_text(self, days: int = 30, limit: int = 10) -> str:
        now = time.time()
        rows = self.outages_between(now - days * 86400, now)
        if not rows:
            return f"🔌 No outages in the last {days} days"
        total = sum(end - start for start, end, *_ in rows)
        lines = [f"🔌 Outages, last {days} days: {len(rows)}, total {fmt_duration(total)}"]
        for start, end, soc_start, soc_end in rows[-limit:][::-1]:
            when = datetime.fromtimestamp(start).strftime("%d.%m %H:%M")
            used = f", {soc_start}% → {soc_end}%" if soc_end is not None else ", ongoing"
            lines.append(f"{when}  {fmt_duration(end - start)}{used}")
        return "\n".join(lines)

    def summary_text(self, end: datetime, hours: int = 24) -> str:
        start = end - timedelta(hours=hours)
        rows = self._exec("SELECT AVG(out), MAX(out), SUM(ac_in), SUM(out) FROM samples "
                          "WHERE ts >= ? AND ts < ?", (int(start.timestamp()), int(end.timestamp())))
        avg_out, max_out, sum_in, sum_out = rows[0]
        outages = self.outages_between(start.timestamp(), end.timestamp())
        off = sum(min(e, end.timestamp()) - max(s, start.timestamp()) for s, e, *_ in outages)
        if avg_out is None:
            return f"📅 Last {hours} h: no data"
        return (f"📅 Summary, last {hours} h ({start:%d.%m %H:%M} – {end:%d.%m %H:%M})\n"
                f"Outages: {len(outages)}, without grid {fmt_duration(off)}\n"
                f"Load: avg {avg_out:.0f} W, peak {max_out:.0f} W\n"
                f"Energy: in {fmt_energy(sum_in * 60 / 3.6e6)}, out {fmt_energy(sum_out * 60 / 3.6e6)}")

    def chart_png(self, hours: int, end: float | None = None) -> bytes | None:
        import matplotlib  # noqa: PLC0415 - heavy, only needed for charts
        matplotlib.use("Agg")
        import matplotlib.dates as mdates  # noqa: PLC0415
        import matplotlib.pyplot as plt  # noqa: PLC0415

        end = end or time.time()
        start = end - hours * 3600
        rows = self._exec("SELECT ts, ac_in, out, soc FROM samples WHERE ts >= ? AND ts < ? "
                          "ORDER BY ts", (int(start), int(end)))
        if len(rows) < 2:
            return None
        ts = [datetime.fromtimestamp(r[0]) for r in rows]
        fig, ax = plt.subplots(figsize=(10, 4.5), dpi=110)
        ax.plot(ts, [r[2] for r in rows], color="#d9480f", lw=1.2, label="Load, W")
        ax.plot(ts, [r[1] for r in rows], color="#1971c2", lw=1.0, alpha=0.8, label="Grid input, W")
        ax.set_ylabel("W")
        ax.set_ylim(bottom=0)
        soc_ax = ax.twinx()
        soc_ax.plot(ts, [r[3] for r in rows], color="#2f9e44", lw=1.5, label="Charge, %")
        soc_ax.set_ylim(0, 105)
        soc_ax.set_ylabel("%")
        for s, e, *_ in self.outages_between(start, end):
            ax.axvspan(datetime.fromtimestamp(max(s, start)), datetime.fromtimestamp(min(e, end)),
                       color="#868e96", alpha=0.25, lw=0)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M" if hours <= 48 else "%d.%m"))
        ax.grid(alpha=0.3)
        handles = ax.get_legend_handles_labels()[0] + soc_ax.get_legend_handles_labels()[0]
        ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=3,
                  fontsize=8, frameon=False)
        ax.set_title(f"Last {hours} h" + (" (grey: outages)" if self.outages_between(start, end) else ""))
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        return buf.getvalue()


class Settings:
    """User settings changed from the bot; stored in the stats DB, .env/CLI give defaults."""

    def __init__(self, stats: "Stats | None", defaults: dict[str, str]):
        self.stats = stats
        self.defaults = defaults

    def get(self, key: str) -> str:
        value = self.stats.meta(f"setting:{key}") if self.stats else None
        return self.defaults[key] if value is None else value

    def set(self, key: str, value: str) -> None:
        if self.stats:
            self.stats.meta(f"setting:{key}", value)

    @property
    def thresholds(self) -> list[int]:
        return sorted((int(t) for t in self.get("thresholds").split(",") if t.strip()), reverse=True)

    @property
    def critical(self) -> int:
        return int(self.get("critical") or 0)

    @property
    def summary(self) -> str:
        return self.get("summary").strip()

    def flag(self, key: str) -> bool:
        return self.get(key) == "1"


THRESHOLD_CHOICES = [90, 75, 50, 25, 10]
CRITICAL_CHOICES = [0, 5, 10, 15, 20]
SUMMARY_CHOICES = ["", "07:00", "10:00", "19:00", "23:00"]
ALERT_FLAGS = {"alert_outage": "Outage", "alert_expansion": "Expansion", "alert_link": "No data"}

# station controls: key -> (state field, {button value: label}, setter); values are strings
# from callback data, the setter turns them into an MQTT command on the device object
STATION_CONTROLS: dict[str, tuple[str, dict[str, str], Callable]] = {
    "light": ("light_mode", {"0": "Off", "1": "Low", "2": "Medium", "3": "High", "4": "Blink"},
              lambda d, v: d.set_light(mode=int(v))),
    "ac": ("ac_output_power_switch", {"1": "On", "0": "Off"},
           lambda d, v: d.set_ac_output(enabled=v == "1")),
    "acmode": ("ac_output_mode", {"1": "Normal", "2": "Smart"},
               lambda d, v: d.set_ac_output(mode="normal" if v == "1" else "smart")),
    "dc": ("dc_output_power_switch", {"1": "On", "0": "Off"},
           lambda d, v: d.set_dc_output(enabled=v == "1")),
    "dcmode": ("dc_12v_output_mode", {"1": "Normal", "2": "Smart"},
               lambda d, v: d.set_dc_output(mode="normal" if v == "1" else "smart")),
    "limit": ("ac_input_limit", {str(w): f"{w}W" for w in (200, 400, 600, 800, 1000)},
              lambda d, v: d.set_charge_limit(max_watts=int(v))),
    "fast": ("ac_fast_charge_switch", {"1": "On", "0": "Off"},
             lambda d, v: d.set_fast_charging(enabled=v == "1")),
    "disp": ("display_mode", {"0": "Off", "1": "Low", "2": "Medium", "3": "High"},
             lambda d, v: d.set_display(mode=int(v))),
    "dispto": ("display_timeout_seconds", {"20": "20s", "30": "30s", "60": "1m", "300": "5m", "1800": "30m"},
               lambda d, v: d.set_display(timeout_seconds=int(v))),
    "autooff": ("device_timeout_minutes", {"0": "Never", "30": "30m", "60": "1h", "120": "2h", "240": "4h",
                                           "360": "6h", "720": "12h", "1440": "24h"},
                lambda d, v: d.set_device_timeout(timeout_minutes=int(v))),
}


class Controller:
    """Renders the Settings / Light inline menus and applies button presses.

    Runs in the Telegram thread; device commands are executed on the asyncio loop.
    """

    def __init__(self, settings: Settings, data: Callable[[], dict], run: Callable, device):
        self.settings = settings
        self.data = data  # latest MQTT values of the station
        self.run = run  # run(coroutine) -> result, on the asyncio loop
        self.device = device
        self.light_seen = False
        self.pending: dict[str, tuple[str, float]] = {}  # field -> (value, time) until telemetry catches up

    def _state(self, key: str) -> str | None:
        field = STATION_CONTROLS[key][0]
        if field in self.pending and time.time() - self.pending[field][1] < 20:
            return self.pending[field][0]
        value = as_int(self.data().get(field))
        return None if value is None else str(value)

    def _label(self, key: str) -> str:
        return STATION_CONTROLS[key][1].get(self._state(key) or "", "?")

    def has_light(self) -> bool | None:
        """None until the first battery telemetry, then whether the station reports a light."""
        data = self.data()
        if "light_mode" in data:
            self.light_seen = True
        if self.light_seen:
            return True
        return False if ("main_battery_soc" in data or "battery_soc" in data) else None

    def _supported(self, key: str) -> bool:
        return STATION_CONTROLS[key][0] in self.data()

    def _row(self, key: str, prefix: str = "") -> list[dict]:
        current = self._state(key)
        return [{"text": ("• " if value == current else "") + prefix + label, "callback_data": f"a:{key}:{value}"}
                for value, label in STATION_CONTROLS[key][1].items()]

    @staticmethod
    def _markup(rows: list[list[dict]], back: bool = True) -> str:
        if back:
            rows = [*rows, [{"text": "« Back", "callback_data": "m:root"}]]
        return json.dumps({"inline_keyboard": [r for r in rows if r]})

    def render(self, menu: str) -> tuple[str, str]:
        st = self.settings
        if menu == "light":
            row = self._row("light")
            return f"🔦 Light: {self._label('light')}", self._markup([row[:3], row[3:]], back=False)
        if menu == "notif":
            th = st.thresholds
            text = (f"🔔 Notifications\nCharge alerts at: {', '.join(f'{t}%' for t in th) or 'off'}\n"
                    f"Critical alert: {f'{st.critical}%' if st.critical else 'off'}\n"
                    f"Daily summary: {st.summary + ' (last 24 h)' if st.summary else 'off'}\n"
                    + "\n".join(f"{label} alerts: {'on' if st.flag(k) else 'off'}" for k, label in ALERT_FLAGS.items()))
            rows = [
                [{"text": ("✓" if t in th else "") + f"{t}%", "callback_data": f"a:th:{t}"} for t in THRESHOLD_CHOICES],
                [{"text": ("• " if c == st.critical else "") + (f"Crit {c}%" if c else "Crit off"),
                  "callback_data": f"a:crit:{c}"} for c in CRITICAL_CHOICES],
                [{"text": ("• " if t == st.summary else "") + (t or "Summary off"), "callback_data": f"a:sum:{t or 'off'}"}
                 # a custom time from .env is shown too, so the current choice is visible
                 for t in SUMMARY_CHOICES + ([st.summary] if st.summary not in SUMMARY_CHOICES else [])],
                [{"text": ("✓ " if st.flag(k) else "✗ ") + label, "callback_data": f"a:flag:{k}"}
                 for k, label in ALERT_FLAGS.items()],
            ]
            return text, self._markup(rows)
        if menu == "outputs":
            lines, rows = ["🔌 Outputs"], []
            for key, mode_key, name in (("ac", "acmode", "AC"), ("dc", "dcmode", "DC 12V")):
                if self._supported(key):
                    mode = f" ({self._label(mode_key)})" if self._supported(mode_key) else ""
                    lines.append(f"{name}: {self._label(key)}{mode}")
                    rows.append(self._row(key, f"{name} "))
                    if self._supported(mode_key):
                        rows.append(self._row(mode_key, f"{name} "))
            return "\n".join(lines if rows else [*lines, "Not supported by this station"]), self._markup(rows)
        if menu == "charging":
            lines, rows = ["⚡ Charging & display"], []
            for key, name in (("limit", "AC charge limit"), ("fast", "Fast charging"), ("disp", "Display"),
                              ("dispto", "Screen timeout"), ("autooff", "Station auto-off")):
                if self._supported(key):
                    lines.append(f"{name}: {self._label(key)}")
                    prefix = {"fast": "Fast ", "disp": "☀️ ", "dispto": "⏱ ", "autooff": "⏻ "}.get(key, "")
                    row = self._row(key, prefix)
                    rows += [row[:4], row[4:]] if len(row) > 5 else [row]
            return "\n".join(lines), self._markup(rows)
        if menu == "acoff":
            return ("⚠️ Turn the AC output OFF?\nEverything plugged into the station outlets loses power.",
                    self._markup([[{"text": "Yes, turn off", "callback_data": "a:ac:0!"},
                                   {"text": "Cancel", "callback_data": "m:outputs"}]], back=False))
        return "⚙️ Settings", self._markup([
            [{"text": "🔔 Notifications", "callback_data": "m:notif"},
             {"text": "🔌 Outputs", "callback_data": "m:outputs"}],
            [{"text": "⚡ Charging & display", "callback_data": "m:charging"}]], back=False)

    def act(self, key: str, value: str) -> tuple[str, str]:
        """Apply a button press; returns (toast text, menu to show next)."""
        st = self.settings
        if key == "th" and int(value) in THRESHOLD_CHOICES:
            th = set(st.thresholds) ^ {int(value)}
            st.set("thresholds", ",".join(str(t) for t in sorted(th, reverse=True)))
            return "Saved", "notif"
        if key == "crit" and int(value) in CRITICAL_CHOICES:
            st.set("critical", value)
            return "Saved", "notif"
        if key == "sum" and (value == "off" or value in SUMMARY_CHOICES):
            st.set("summary", "" if value == "off" else value)
            return "Saved", "notif"
        if key == "flag" and value in ALERT_FLAGS:
            st.set(value, "0" if st.flag(value) else "1")
            return "Saved", "notif"
        if key not in STATION_CONTROLS:
            return "Unknown action", "root"
        confirmed = value.endswith("!")
        value = value.rstrip("!")
        field, options, setter = STATION_CONTROLS[key]
        if value not in options:
            return "Unknown value", "root"
        if key == "ac" and value == "0" and not confirmed:
            return "Confirm", "acoff"
        try:
            ok = self.run(setter(self.device, value)) is not None
        except Exception:
            log.exception("command %s=%s failed", key, value)
            ok = False
        log.info("Command %s=%s: %s", key, value, "ok" if ok else "failed")
        if ok:
            self.pending[field] = (value, time.time())
        menu = {"light": "light", "ac": "outputs", "acmode": "outputs", "dc": "outputs",
                "dcmode": "outputs"}.get(key, "charging")
        return (f"{options[value]} ✓" if ok else "⚠️ Command failed"), menu


class Watchdog:
    def __init__(self, tg: Telegram, power: PowerPlan, confirm_s: float, settings: Settings,
                 name: str = "Anker", stats: Stats | None = None):
        self.name = name
        self.stats = stats
        self.tg = tg
        self.power = power
        self.confirm_s = confirm_s
        self.settings = settings
        self.on_battery: bool | None = None  # None until the first telemetry
        self.no_grid_since: float | None = None
        self.outage_started: float | None = None  # wall clock, for the outage duration
        self.fired: dict[str, set[int]] = {}  # battery name -> thresholds already reported
        self.critical_fired = False

    def outage_duration(self) -> str:
        return fmt_duration(time.time() - self.outage_started) if self.outage_started else "?"

    def evaluate(self, grid: bool, ac_in: int, levels: dict[str, int], charge: str,
                 remaining_h: float | None = None) -> None:
        """levels ({"main": %, "expansion": %}) drive the per-battery thresholds;
        charge is the human-readable breakdown for messages."""
        now = time.monotonic()
        total = round(sum(levels.values()) / len(levels))
        if grid:
            self.no_grid_since = None
            if self.on_battery is None and self.stats and (start := self.stats.open_outage()):
                # the outage ended while we were not running
                self.on_battery, self.outage_started = True, start
            if self.on_battery:
                if self.settings.flag("alert_outage"):
                    self.tg.send(f"⚡ Grid power is back after {self.outage_duration()}: "
                                 f"input {ac_in} W, charge {charge}")
                if self.stats:
                    self.stats.outage_ended(time.time(), total)
            if self.on_battery is not False:
                # also on the first reading, in case we were restarted during an outage
                self.power.restore()
            self.on_battery = False
            self.outage_started = None
            self.fired.clear()
            self.critical_fired = False
            return

        left = f", ~{fmt_duration(remaining_h * 3600)} left" if remaining_h else ""
        if self.no_grid_since is None:
            self.no_grid_since = now
        if not self.on_battery and now - self.no_grid_since >= self.confirm_s:
            self.on_battery = True
            resumed = self.stats.open_outage() if self.stats else None
            if resumed:
                # restarted during an outage: keep its start, do not repeat earlier alerts
                self.outage_started = resumed
                for name, pct in levels.items():
                    self.fired[name] = {t for t in self.settings.thresholds if pct <= t}
                self.critical_fired = bool(self.settings.critical and total <= self.settings.critical)
                log.info("Resumed outage started %s", datetime.fromtimestamp(resumed))
            else:
                self.outage_started = time.time() - (now - self.no_grid_since)
                if self.settings.flag("alert_outage"):
                    self.tg.send(f"🔌 {self.name} lost AC input, running on battery: {charge}{left}")
                if self.stats:
                    self.stats.outage_started(self.outage_started, total)
            self.power.power_saver()

        if self.on_battery:
            # per battery: report only the lowest crossed threshold, mark the higher ones as done
            for name, pct in levels.items():
                fired = self.fired.setdefault(name, set())
                crossed = [t for t in self.settings.thresholds if pct <= t and t not in fired]
                if crossed:
                    t = min(crossed)
                    fired.update(x for x in self.settings.thresholds if x >= t)
                    self.tg.send(f"🔋 {name.capitalize()} battery {pct}% (threshold {t}%), "
                                 f"charge {charge}{left}")
            critical = self.settings.critical
            if critical and total <= critical and not self.critical_fired:
                self.critical_fired = True
                self.tg.send(f"🪫 CRITICAL: charge {charge}{left}. Save your work and shut down the PC")
                show_popup(f"{self.name}: critical battery",
                           f"Battery charge is {charge}{left}.\n\n"
                           "Save your work and shut down the computer.")


def battery_state(data: dict) -> tuple[dict[str, int] | None, int, str]:
    """Return (per-battery %, expansion pack count, text) from MQTT data.

    Field names differ a little between station models, so known variants are tried.
    Expansion packs are assumed to match the main battery capacity, so the total
    charge shown in the text is the plain average.
    """
    main = as_int(data.get("main_battery_soc", data.get("battery_soc")))
    if main is None:
        return None, 0, ""
    exps = [as_int(data.get(f"exp_{i}_soc")) for i in range(1, 7)]
    packs = as_int(data.get("expansion_packs"))
    if packs is None:  # models without a pack counter report 0% for empty slots
        packs = sum(1 for e in exps if e)
    exps = [e for e in exps[:packs] if e is not None]
    if not exps:
        return {"main": main}, packs, f"{main}%"
    levels = {"main": main} | {
        ("expansion" if len(exps) == 1 else f"expansion {i}"): e for i, e in enumerate(exps, 1)}
    total = round(sum(levels.values()) / len(levels))
    parts = ", ".join(f"{k} {v}%" for k, v in levels.items())
    return levels, packs, f"{total}% ({parts})"


def as_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


async def find_device(api: AnkerSolixApi, wanted_sn: str | None) -> dict:
    await api.update_sites()
    await api.get_bind_devices()
    for sn, dev in api.devices.items():
        log.info("  account device %s %s (%s)", sn, dev.get("device_pn"), dev.get("name") or dev.get("alias"))
    for sn, dev in api.devices.items():
        if (wanted_sn and sn == wanted_sn) or (not wanted_sn and dev.get("device_pn") in PPS_MODELS):
            return dev
    raise SystemExit("No power station with MQTT support found in this Anker account; "
                     "set ANKER_DEVICE_SN or check the supported models in anker-solix-api")


async def start_poller(api: AnkerSolixApi, device: dict) -> asyncio.Task:
    session = await api.startMqttSession()
    if not (session and session.is_connected()):
        raise ConnectionError("cannot connect to Anker MQTT server")
    topics = {f"{session.get_topic_prefix(deviceDict=device)}#"}
    # The poller subscribes the device topic and keeps re-sending the realtime trigger
    return asyncio.create_task(session.message_poller(
        topics=topics, trigger_devices={device["device_sn"]}, timeout=REALTIME_TIMEOUT,
    ))


async def restart_poller(api: AnkerSolixApi, device: dict, poller: asyncio.Task) -> asyncio.Task:
    # Wait for the old poller to finish: its CancelledError handler calls session.cleanup(),
    # which would otherwise tear down the freshly connected client and its message callback.
    poller.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await poller
    api.stopMqttSession()
    return await start_poller(api, device)


def send_daily_summary(tg: Telegram, stats: Stats | None, at: str) -> None:
    """Once a day after the summary time (HH:MM, local), send the last 24 h summary and chart."""
    if not (tg.enabled and stats and at):
        return
    now = datetime.now()
    if now.strftime("%H:%M") < at or stats.meta("last_summary") == now.strftime("%Y-%m-%d"):
        return
    stats.meta("last_summary", now.strftime("%Y-%m-%d"))
    tg.send(stats.summary_text(now))
    with contextlib.suppress(Exception):
        if png := stats.chart_png(24, end=now.timestamp()):
            tg.send_photo(png, "📈 Last 24 h")


async def run(args: argparse.Namespace) -> None:
    tg = Telegram(os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID"),
                  os.environ.get("TG_ADMIN_IDS"))
    power = PowerPlan(dry_run=args.dry_run)
    power.log_plans()
    # statistics and bot settings only matter with Telegram; a PC that just switches
    # power plans (empty TG_BOT_TOKEN) runs without the database
    stats = Stats(base_dir() / "anker_ups.db") if tg.enabled else None
    settings = Settings(stats, {
        "thresholds": ",".join(str(t) for t in args.thresholds),
        "critical": os.environ.get("CRITICAL_LEVEL") or "10",
        "summary": os.environ.get("SUMMARY_TIME", "09:00").strip(),
        "alert_outage": "1", "alert_expansion": "1", "alert_link": "1",
    })
    watchdog = Watchdog(tg, power, confirm_s=args.confirm, settings=settings, stats=stats)

    if args.test_telegram:
        tg.send("Test message from anker_ups")
        await asyncio.sleep(5)
        return

    user, password = os.environ.get("ANKERUSER"), os.environ.get("ANKERPASSWORD")
    if not (user and password):
        raise SystemExit("ANKERUSER/ANKERPASSWORD not set")

    async with ClientSession() as websession:
        api = AnkerSolixApi(user, password, os.environ.get("ANKERCOUNTRY", "DE"), websession,
                            logging.getLogger("anker_solix_api"))
        await api.async_authenticate()
        device = await find_device(api, os.environ.get("ANKER_DEVICE_SN"))
        sn = device["device_sn"]
        log.info("Monitoring %s (%s)", sn, device.get("device_pn"))
        watchdog.name = device.get("alias") or device.get("name") or f"Anker {device.get('device_pn')}"

        poller = await start_poller(api, device)
        log.info("MQTT connected, waiting for telemetry...")

        last_message, last_change = None, time.monotonic()
        last_levels: dict[str, int] | None = None
        last_charge = ""
        expansion: int | None = None  # pack count, None until the first telemetry
        disconnected_since: float | None = None
        last_restart = last_retrigger = time.monotonic()
        link_alerted = False
        snapshot: dict = {}

        def status_text() -> str:
            if not snapshot:
                return "⏳ No data from Anker yet"
            age = int(time.monotonic() - snapshot["at"])
            grid = {True: "down, running on battery", False: "up"}.get(watchdog.on_battery, "?")
            lines = [f"📊 {watchdog.name}", f"Grid: {grid}", f"Charge: {snapshot['charge']}"]
            if watchdog.on_battery:
                lines.append(f"Outage: {watchdog.outage_duration()}")
                if snapshot.get("remaining_h"):
                    lines.append(f"Remaining: ~{fmt_duration(snapshot['remaining_h'] * 3600)}")
            lines += [f"Input: {snapshot['ac_in']} W, load: {snapshot['out']} W",
                      f"Last data: {age} s ago"]
            return "\n".join(lines)

        mqtt_device = SolixMqttDeviceFactory(api_instance=api, device_sn=sn).create_device()
        loop = asyncio.get_running_loop()
        def run_on_loop(coro):
            # called from the Telegram thread; MQTT commands must run on the asyncio loop
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=15)

        controller = Controller(
            settings, lambda: api.mqttsession.mqtt_data.get(sn, {}) if api.mqttsession else {},
            run_on_loop, mqtt_device)
        tg.listen_commands(BotHandlers(status_text, controller, stats))
        while True:
            await asyncio.sleep(1)
            session = api.mqttsession
            now = time.monotonic()
            send_daily_summary(tg, stats, settings.summary)

            if not (session and session.is_connected()) or poller.done():
                disconnected_since = disconnected_since or now
            else:
                disconnected_since = None
            silent = now - last_change
            if last_message and not link_alerted and silent > args.link_timeout:
                link_alerted = True
                if settings.flag("alert_link"):
                    tg.send(f"⚠️ No data from Anker for {int(silent)} s")
            # The device stops streaming after some state changes (e.g. grid coming back),
            # so ask for realtime data again as soon as the usual 3-5 s cadence breaks.
            if (session and session.is_connected() and silent > args.retrigger
                    and now - last_retrigger > args.retrigger):
                last_retrigger = now
                log.info("No data for %d s, re-sending realtime trigger", silent)
                with contextlib.suppress(Exception):
                    session.realtime_trigger(deviceDict=device, timeout=REALTIME_TIMEOUT)
            # restart on a dead connection, or on a live one that stays silent
            if (now - last_restart > 60
                    and ((disconnected_since and now - disconnected_since > 60) or silent > 60)):
                last_restart = now
                log.warning("MQTT %s, restarting session",
                            "disconnected" if disconnected_since else "silent")
                try:
                    poller = await restart_poller(api, device, poller)
                    session = api.mqttsession
                    log.info("MQTT session restarted")
                except Exception as exc:
                    log.warning("MQTT restart failed: %s", exc)

            data = (session.mqtt_data.get(sn) if session else None) or {}
            if data.get("last_message") != last_message:
                last_message, last_change = data.get("last_message"), now
                levels, packs, charge = battery_state(data)
                ac_in = as_int(data.get("ac_input_power", data.get("ac_input_power_total")))
                charging = as_int(data.get("charging_status"))
                remaining_h = data.get("remaining_time_hours")
                if levels:
                    snapshot.update(at=now, charge=charge, ac_in=ac_in, remaining_h=remaining_h,
                                    out=as_int(data.get("output_power_total")))
                log.debug("ac_in=%sW charging_status=%s out=%sW in_total=%sW battery=%s",
                          ac_in, charging, data.get("output_power_total"),
                          data.get("photovoltaic_power"), charge or None)
                if levels and packs != expansion:
                    if expansion is None or not settings.flag("alert_expansion"):
                        pass
                    elif packs > expansion:
                        tg.send(f"🔋 Expansion battery connected, charge {charge}")
                    else:
                        tg.send(f"🔋 Expansion battery disconnected, charge {charge}")
                    expansion = packs
                if link_alerted:
                    if settings.flag("alert_link"):
                        tg.send("📶 Anker data is flowing again")
                    link_alerted = False
                if levels and ac_in is not None:
                    last_levels, last_charge = levels, charge
                    # charging_status (where reported): 0 idle, 1 solar, 2 AC, 3 both
                    grid = ac_in > 0 or charging in (2, 3)
                    if stats:
                        stats.add_reading(ac_in, as_int(data.get("output_power_total")) or 0,
                                          round(sum(levels.values()) / len(levels)), grid)
                    watchdog.evaluate(grid, ac_in, levels, charge, remaining_h)
                    continue
            # keep re-evaluating so the outage confirm timer runs between messages
            if watchdog.no_grid_since is not None and not watchdog.on_battery and last_levels:
                watchdog.evaluate(False, 0, last_levels, last_charge)


def apply_timezone() -> None:
    """Use TIMEZONE from .env (e.g. Europe/Kyiv) for message and log timestamps.

    Servers usually run in UTC; Windows keeps the PC's own time zone (no tzset there).
    """
    tz = os.environ.get("TIMEZONE", "").strip()
    if tz and hasattr(time, "tzset"):
        os.environ["TZ"] = tz
        time.tzset()


def main() -> None:
    load_env()
    apply_timezone()
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="do not touch the power plan")
    p.add_argument("--confirm", type=float, default=10, help="seconds without AC input before outage (10)")
    p.add_argument("--thresholds", type=int, nargs="+", default=[75, 50, 25])
    p.add_argument("--link-timeout", type=float, default=120, help="seconds without telemetry before alert")
    p.add_argument("--retrigger", type=float, default=15,
                   help="seconds without telemetry before re-requesting realtime data (15)")
    p.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="print every telemetry update")
    args = p.parse_args()
    # a windowless exe has no stderr, so always keep a rotating log next to it
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        base_dir() / "anker_ups.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    for noisy in ("anker_solix_api", "paho", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
