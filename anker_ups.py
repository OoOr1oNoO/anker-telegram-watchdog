# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "anker-solix-api @ https://github.com/thomluther/anker-solix-api/archive/refs/tags/v3.9.1.tar.gz",
#     "matplotlib",
# ]
# ///
"""Anker SOLIX power station watchdog via Anker cloud MQTT.

- Telegram alert when AC input is lost / restored, with durations, and a full-charge alert.
- Charge alerts per battery (default 75 / 50 / 25 %) and a critical charge alert.
- Bot buttons: Status, Light, Stats (charts, outages, energy, months), Settings
  (notifications, station controls, users and access, language).
- Messages in English, Ukrainian, Russian, Polish, German or Spanish, per chat.
- Daily summary; samples, outages, users and settings are kept in SQLite.
- Switches Windows to the Power saver plan on outage, restores the previous plan after.

Config: environment variables or a .env file next to the script/exe (see .env.example).
"""

import argparse
import asyncio
import contextlib
import functools
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

sys.path.insert(0, str(Path(__file__).resolve().parent))  # translations.py next to the script
from translations import ENERGY_UNITS, LANG_NAMES, LANGS, MONTHS, SHORT_UNITS, TEXTS, UNITS  # noqa: E402

POWER_SAVER_GUID = "a1841308-3541-4fab-bc81-f71556f20b4a"
IS_WINDOWS = platform.system() == "Windows"
REALTIME_TIMEOUT = 300  # seconds the device streams 0405 messages before the poller re-triggers
HEALTHCHECK_EVERY = 300  # seconds between pings to HEALTHCHECK_URL

log = logging.getLogger("anker_ups")


class Lang:
    """Texts and number formatting in one language; falls back to English."""

    def __init__(self, code: str | None):
        code = (code or "").lower().split("-")[0]
        self.code = code if code in LANGS else "en"
        self.i = LANGS.index(self.code)

    def t(self, key: str, **kw) -> str:
        row = TEXTS[key]
        text = row[self.i] or row[0]
        return text.format(**kw) if kw else text

    def dur(self, seconds: float) -> str:
        """36m, 5h 03m, or 11d 2h (266h 42m) for long ones."""
        d_u, h_u, m_u, _ = UNITS[self.code]
        minutes = max(0, int(seconds // 60))
        hours, m = divmod(minutes, 60)
        if minutes < 60:
            return f"{minutes}{m_u}"
        if hours < 24:
            return f"{hours}{h_u} {m:02d}{m_u}"
        d, h = divmod(hours, 24)
        return f"{d}{d_u} {h}{h_u} ({hours}{h_u} {m:02d}{m_u})"

    def short(self, seconds: int, compact: bool = False) -> str:
        """20 s, 5 min, 2 h for settings; compact (20s, 5min) for narrow buttons."""
        s_u, m_u, h_u = SHORT_UNITS[self.code] if compact else UNITS[self.code][:0:-1]
        if seconds < 60:
            return f"{seconds}{s_u}"
        return f"{seconds // 60}{m_u}" if seconds < 3600 else f"{seconds // 3600}{h_u}"

    def energy(self, kwh: float) -> str:
        k_u, w_u = ENERGY_UNITS.get(self.code, ("kWh", "Wh"))
        return f"{kwh:.2f} {k_u}" if kwh >= 1 else f"{kwh * 1000:.0f} {w_u}"

    def battery(self, key: str) -> str:
        if key == "main":
            return self.t("bat_main")
        return self.t("bat_expansion") if key == "expansion" else self.t("bat_expansion_n", n=key.split()[-1])

    def charge(self, levels: dict[str, int]) -> str:
        """76% (main 100%, expansion 51%); expansion packs are assumed to match the main capacity."""
        if len(levels) == 1:
            return f"{next(iter(levels.values()))}%"
        total = round(sum(levels.values()) / len(levels))
        return f"{total}% ({', '.join(f'{self.battery(k)} {v}%' for k, v in levels.items())})"

    def month(self, number: int) -> str:
        return MONTHS[self.code][number - 1]


@functools.cache
def lang(code: str | None) -> Lang:
    return Lang(code)


Render = Callable[[Lang], str]


def base_dir() -> Path:
    """Folder of the exe when packaged with PyInstaller, of the script otherwise."""
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


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


ACTIVE_ROLES = ("admin", "user")
ACCESS_MODES = {"closed": "acc_closed", "requests": "acc_requests", "open": "acc_open"}


class Users:
    """Chats that get alerts and may use the bot, with roles and languages, in the stats DB.

    Roles: admin (controls the station and the bot), user (alerts and viewing), pending
    (asked for access), blocked (denied). TG_CHAT_ID / TG_ADMIN_IDS from .env are re-applied on
    every start and cannot be changed from the bot; when TG_ADMIN_IDS is empty, the people in
    TG_CHAT_ID are admins (as in older versions, where everyone could control the station).
    """

    def __init__(self, stats: "Stats", env_chats: list[str], env_admins: list[str], default: Lang):
        self.stats = stats
        self.default = default
        self.awaiting_id: dict[tuple[str, str], float] = {}  # (chat, admin) -> time of "Add by ID"
        stats._exec("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT, role TEXT, "
                    "lang TEXT, lang_auto TEXT)")
        admins = env_admins or [c for c in env_chats if not c.startswith("-")]
        self.env_ids = set(env_chats) | set(admins)
        for chat in dict.fromkeys(env_chats + admins):
            if chat in admins:
                self.upsert(chat, role="admin")
            elif self.role(chat) not in ACTIVE_ROLES:
                self.upsert(chat, role="user")

    def _row(self, chat: str) -> tuple | None:
        rows = self.stats._exec("SELECT name, role, lang, lang_auto FROM users WHERE id = ?", (chat,))
        return rows[0] if rows else None

    def role(self, chat: str) -> str | None:
        row = self._row(chat)
        return row[1] if row else None

    def who(self, chat: str) -> str:
        row = self._row(chat)
        return f"{row[0]} ({chat})" if row and row[0] else chat

    def upsert(self, chat: str, **fields) -> None:
        if self._row(chat) is None:
            self.stats._exec("INSERT INTO users (id) VALUES (?)", (chat,))
        for key, value in fields.items():
            if key in ("name", "role", "lang", "lang_auto") and value is not None:
                self.stats._exec(f"UPDATE users SET {key} = ? WHERE id = ?", (value, chat))

    def remove(self, chat: str) -> None:
        self.stats._exec("DELETE FROM users WHERE id = ?", (chat,))

    def all(self) -> list[tuple[str, str, str]]:
        """(id, name, role), admins first."""
        rows = self.stats._exec("SELECT id, COALESCE(name, ''), role FROM users")
        order = {"admin": 0, "user": 1, "pending": 2, "blocked": 3}
        return sorted(rows, key=lambda r: (order.get(r[2], 9), r[1].lower(), r[0]))

    def recipients(self) -> list[str]:
        return [r[0] for r in self.all() if r[2] in ACTIVE_ROLES]

    def admins(self) -> list[str]:
        return [r[0] for r in self.all() if r[2] == "admin"]

    def is_admin(self, user: str) -> bool:
        return self.role(user) == "admin"

    def lang(self, chat: str) -> Lang:
        row = self._row(chat)
        return lang((row[2] or row[3]) if row and (row[2] or row[3]) else self.default.code)

    def note(self, chat: str, name: str, code: str | None, private: bool) -> None:
        """Remember the chat name and the Telegram app language (groups keep the first one)."""
        row = self._row(chat)
        if row is None:
            return
        auto = Lang(code).code if code else None
        self.upsert(chat, name=name if name != row[0] else None,
                    lang_auto=auto if auto and (private or not row[3]) and auto != row[3] else None)

    @property
    def access(self) -> str:
        mode = self.stats.meta("setting:access")
        return mode if mode in ACCESS_MODES else "closed"

    @access.setter
    def access(self, mode: str) -> None:
        self.stats.meta("setting:access", mode)


def chat_name(chat: dict, user: dict | None = None) -> str:
    if chat.get("title"):
        return chat["title"]
    user = user or chat
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    if user.get("username"):
        name = f"{name} @{user['username']}".strip()
    return name or "?"


@dataclass
class BotHandlers:
    """Callbacks the Telegram thread uses to read state and act on the station."""

    status: Render
    controller: "Controller"
    stats: "Stats | None" = None


@dataclass
class Ctx:
    """Who pressed a button: the chat language and whether the person is an admin."""

    L: Lang
    admin: bool
    chat: str
    user: str


KEYBOARD_ACTIONS = (("btn_status", "status"), ("btn_light", "light"), ("btn_stats", "stats"),
                    ("btn_settings", "settings"))
# button texts in every language -> action; in groups the bot sees every message (privacy mode
# off), so only exact button texts and commands are handled, not any text with "status" in it
BUTTON_ACTIONS = {lang(code).t(key).lower(): action for code in LANGS for key, action in KEYBOARD_ACTIONS}
COMMANDS = {"start": "status", "status": "status", "light": "light", "stats": "stats", "settings": "settings"}
REMOVE_KEYBOARD = json.dumps({"remove_keyboard": True})


def main_keyboard(L: Lang, light: bool = True) -> str:
    """Persistent reply keyboard; Status gets its own row (Telegram makes a row's buttons equal).
    Light is left out once the station is known to have none (no light_mode field)."""
    row = [{"text": L.t("btn_stats")}, {"text": L.t("btn_settings")}]
    return json.dumps({"keyboard": [[{"text": L.t("btn_status")}],
                                    [{"text": L.t("btn_light")}, *row] if light else row],
                       "resize_keyboard": True, "is_persistent": True})


def stats_menu(L: Lang) -> str:
    return json.dumps({"inline_keyboard": [
        [{"text": L.t("chart24"), "callback_data": "stats:chart24"},
         {"text": L.t("chart7"), "callback_data": "stats:chart168"}],
        [{"text": L.t("outages"), "callback_data": "stats:outages"},
         {"text": L.t("energy"), "callback_data": "stats:energy"},
         {"text": L.t("months"), "callback_data": "stats:months"}]]})


class Telegram:
    """Sends messages from a background thread and retries until the network is back.

    Alerts go to every active chat (people and groups) in its own language. The bot buttons
    work in each of these chats via long polling (getUpdates); replies go to the chat that
    asked. Strangers get access according to the access mode (closed / requests / open).
    """

    def __init__(self, token: str | None, configured: bool, default: Lang):
        self.enabled = bool(token and configured)
        self.base = f"https://api.telegram.org/bot{token}"
        self.default = default
        self.users: Users | None = None
        self.username = ""  # bot username, to skip /commands@other_bot in groups
        # (kind, payload, markup, chat, lang code); chat None = all active chats; payload and
        # markup are strings or functions of the language, rendered per chat in the worker
        self.queue: queue.Queue[tuple] = queue.Queue()
        self.has_light: Callable[[], bool | None] = lambda: None  # set by listen_commands
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True).start()
        elif not token:
            log.warning("TG_BOT_TOKEN not set, Telegram disabled")
        else:
            log.warning("TG_CHAT_ID / TG_ADMIN_IDS not set, Telegram disabled")

    def _call(self, method: str, params: dict, timeout: float = 10) -> dict:
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(f"{self.base}/{method}", data=data, timeout=timeout) as resp:
            return json.loads(resp.read())

    def _send_photo(self, chat: str, png: bytes, caption: str, markup: str) -> None:
        boundary = f"----anker{int(time.time() * 1000)}"
        parts = []
        for name, value in (("chat_id", chat), ("caption", caption), ("reply_markup", markup)):
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

    def lang_of(self, chat: str) -> Lang:
        return self.users.lang(chat) if self.users else self.default

    def keyboard(self, L: Lang) -> str:
        return main_keyboard(L, self.has_light() is not False)

    def send(self, render: Render) -> None:
        """Alert to all active chats, each in its language."""
        log.info("ALERT: %s", render(lang("en")))
        if self.enabled:
            stamp = time.strftime("%H:%M:%S")
            self.queue.put(("text", lambda L: f"{stamp} {render(L)}", None, None, None))

    def reply(self, chat: str, text: str | Render, markup: str | Render | None = None,
              code: str | None = None) -> None:
        """Message to one chat; code forces the language (people who are not users yet)."""
        if self.enabled:
            self.queue.put(("text", text, markup, chat, code))

    def send_photo(self, png: Callable[[Lang], bytes | None], caption: Render, chat: str | None = None) -> None:
        if self.enabled:
            self.queue.put(("photo", png, caption, chat, None))

    def _worker(self) -> None:
        while True:
            kind, payload, extra, target, code = self.queue.get()
            chats = [target] if target else (self.users.recipients() if self.users else [])
            photos: dict[str, bytes | None] = {}  # one chart per language
            for chat in chats:
                L = lang(code) if code else self.lang_of(chat)
                try:
                    if kind == "photo":
                        if L.code not in photos:
                            photos[L.code] = payload(L)
                        if photos[L.code] is None:
                            continue
                        args = (photos[L.code], extra(L), self.keyboard(L))
                    else:
                        markup = extra(L) if callable(extra) else extra
                        args = (payload(L) if callable(payload) else payload, markup or self.keyboard(L))
                except Exception:
                    log.exception("Telegram message rendering failed")
                    continue
                self._deliver(kind, chat, args)

    def _deliver(self, kind: str, chat: str, args: tuple) -> None:
        while True:
            try:
                if kind == "photo":
                    self._send_photo(chat, *args)
                else:
                    self._call("sendMessage", {"chat_id": chat, "text": args[0], "reply_markup": args[1]})
                return
            except urllib.error.HTTPError as exc:
                if exc.code != 429 and exc.code < 500:
                    # wrong id, bot blocked or removed from the group: retrying won't help
                    log.warning("Telegram send to %s failed: %s %s", chat, exc.code,
                                exc.read().decode(errors="replace")[:200])
                    return
                log.warning("Telegram send failed (%s), retrying in 15s", exc)
            except Exception as exc:  # no internet during outage is expected
                log.warning("Telegram send failed (%s), retrying in 15s", exc)
            time.sleep(15)

    def listen_commands(self, handlers: BotHandlers) -> None:
        self.has_light = handlers.controller.has_light
        if self.enabled:
            threading.Thread(target=self._poll_commands, args=(handlers,), daemon=True).start()

    def _set_commands(self) -> None:
        for code in LANGS:
            L = lang(code)
            params = {"commands": json.dumps([{"command": c, "description": L.t(f"cmd_{c}")}
                                              for c in ("status", "light", "stats", "settings")])}
            if code != "en":
                params["language_code"] = code
            with contextlib.suppress(Exception):
                self._call("setMyCommands", params)

    def _poll_commands(self, handlers: BotHandlers) -> None:
        with contextlib.suppress(Exception):
            self.username = self._call("getMe", {})["result"]["username"].lower()
        self._set_commands()
        offset = None
        while True:
            try:
                params = {"timeout": 50, "allowed_updates": '["message", "callback_query", "my_chat_member"]'}
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

    def _handle_stats(self, chat: str, action: str, stats: "Stats", L: Lang) -> str:
        if action.startswith("chart"):
            hours = int(action.removeprefix("chart"))
            if stats.chart_png(hours, L) is None:
                return L.t("not_enough")
            self.send_photo(lambda L: stats.chart_png(hours, L), lambda L: "📈 " + chart_title(L, hours), chat)
            return L.t("chart_sent")
        texts = {"outages": stats.outages_text, "energy": stats.energy_text, "months": stats.months_text}
        if action in texts:
            self.reply(chat, texts[action])
        return L.t("sent")

    def _edit(self, msg: dict, view: tuple[str, str | None]) -> None:
        params = {"chat_id": msg["chat"]["id"], "message_id": msg.get("message_id"), "text": view[0]}
        if view[1]:
            params["reply_markup"] = view[1]
        with contextlib.suppress(Exception):  # unchanged text makes Telegram return 400
            self._call("editMessageText", params)

    def _action(self, text: str) -> str | None:
        text = text.strip().lower()
        if text.startswith("/"):
            cmd, _, bot = text[1:].split()[0].partition("@") if len(text) > 1 else ("", "", "")
            if bot and self.username and bot != self.username:
                return None  # command for another bot in the group
            return COMMANDS.get(cmd)
        return BUTTON_ACTIONS.get(text)

    def _handle_update(self, upd: dict, h: BotHandlers) -> None:
        users = self.users
        if users is None:
            return
        if mcm := upd.get("my_chat_member"):
            # the bot was added to a group: treat it like /start from that group
            chat = mcm.get("chat", {})
            if (chat.get("type") in ("group", "supergroup")
                    and mcm.get("new_chat_member", {}).get("status") in ("member", "administrator")
                    and users.role(str(chat["id"])) not in ACTIVE_ROLES):
                self._stranger(str(chat["id"]), chat_name(chat), mcm.get("from", {}).get("language_code"))
            return
        if cb := upd.get("callback_query"):
            self._handle_callback(cb, h)
            return
        msg = upd.get("message") or {}
        chat_obj, sender = msg.get("chat", {}), msg.get("from", {})
        chat, user = str(chat_obj.get("id")), str(sender.get("id"))
        text = msg.get("text") or ""
        action = self._action(text)
        if users.role(chat) not in ACTIVE_ROLES:
            if action:
                self._stranger(chat, chat_name(chat_obj, sender), sender.get("language_code"))
            return
        users.note(chat, chat_name(chat_obj, sender), sender.get("language_code"),
                   chat_obj.get("type") == "private")
        L = users.lang(chat)
        if not action and (chat, user) in users.awaiting_id:
            if time.time() - users.awaiting_id.pop((chat, user)) < 600 and users.is_admin(user):
                self._add_by_id(chat, text.strip(), L)
            return
        if action == "light":
            if self.has_light() is False:  # also refreshes the keyboard without the button
                self.reply(chat, L.t("no_light"))
            else:
                self.reply(chat, *h.controller.render("light", Ctx(L, users.is_admin(user), chat, user)))
        elif action == "settings":
            self.reply(chat, *h.controller.render("root", Ctx(L, users.is_admin(user), chat, user)))
        elif action == "stats":
            if h.stats:
                self.reply(chat, L.t("stats_title"), stats_menu(L))
        elif action == "status":
            self.reply(chat, h.status(L))

    def _stranger(self, chat: str, name: str, code: str | None) -> None:
        """Someone who is not a user pressed /start (or added the bot to a group)."""
        users = self.users
        role, L = users.role(chat), lang(code or self.default.code)
        log.info("Telegram chat %s (%s) is not a user (%s), access %s", chat, name, role, users.access)
        if role == "blocked":
            return
        if role == "pending":
            self.reply(chat, L.t("rq_wait"), REMOVE_KEYBOARD, L.code)
            return
        who = f"{name} ({chat})"
        if users.access == "open":
            users.upsert(chat, name=name, role="user", lang_auto=L.code)
            self.reply(chat, L.t("rq_granted"))
            for admin in users.admins():
                self.reply(admin, lambda L: L.t("rq_joined", who=who))
        elif users.access == "requests":
            users.upsert(chat, name=name, role="pending", lang_auto=L.code)
            self.reply(chat, L.t("rq_sent"), REMOVE_KEYBOARD, L.code)
            for admin in users.admins():
                self.reply(admin, lambda L: L.t("rq_admin", who=who), lambda L: json.dumps({"inline_keyboard": [[
                    {"text": L.t("rq_allow"), "callback_data": f"u:ok:{chat}"},
                    {"text": L.t("rq_deny"), "callback_data": f"u:no:{chat}"}]]}))
        else:
            self.reply(chat, L.t("closed_reply", id=chat), REMOVE_KEYBOARD, L.code)

    def _add_by_id(self, chat: str, text: str, L: Lang) -> None:
        users = self.users
        if not re.fullmatch(r"-?\d{5,20}", text):
            self.reply(chat, L.t("u_bad_id"))
            return
        if users.role(text) in ACTIVE_ROLES:
            self.reply(chat, L.t("u_exists", who=users.who(text)))
            return
        name = None
        with contextlib.suppress(Exception):  # works once the person has started the bot
            name = chat_name(self._call("getChat", {"chat_id": text})["result"])
        users.upsert(text, role="user", name=name)
        log.info("User %s added by id from chat %s", text, chat)
        self.reply(chat, L.t("u_added", who=users.who(text)))
        if name:  # they can be messaged: send them the keyboard
            self.reply(text, lambda L: L.t("rq_granted"))

    def _handle_callback(self, cb: dict, h: BotHandlers) -> None:
        users = self.users
        msg = cb.get("message") or {}
        chat, user = str(msg.get("chat", {}).get("id")), str(cb.get("from", {}).get("id"))
        kind, _, rest = (cb.get("data") or "").partition(":")
        toast = ""
        if users.role(chat) in ACTIVE_ROLES:
            ctx = Ctx(users.lang(chat), users.is_admin(user), chat, user)
            if kind in ("a", "u") and not ctx.admin:
                toast = ctx.L.t("only_admins")
                log.info("Denied %s:%s from user %s", kind, rest, user)
            elif kind == "stats" and h.stats:
                toast = self._handle_stats(chat, rest, h.stats, ctx.L)
            elif kind == "m":
                self._edit(msg, h.controller.render(rest, ctx))
            elif kind == "l":
                users.upsert(chat, lang=rest if rest in LANGS else "")
                ctx.L = users.lang(chat)
                self._edit(msg, h.controller.render("lang", ctx))
                self.reply(chat, ctx.L.t("lang_title", v=LANG_NAMES[ctx.L.code]))  # refreshes the keyboard
            elif kind == "a":
                key, _, value = rest.partition(":")
                toast, menu = h.controller.act(key, value, ctx)
                self._edit(msg, h.controller.render(menu, ctx))
            elif kind == "u":
                toast = self._user_action(rest, ctx, msg, h)
        with contextlib.suppress(Exception):
            self._call("answerCallbackQuery", {"callback_query_id": cb["id"], "text": toast})

    def _user_action(self, rest: str, ctx: Ctx, msg: dict, h: BotHandlers) -> str:
        """u:<action>:<id>[:m] from a request message or the Users menu (":m")."""
        users, L = self.users, ctx.L
        action, _, target = rest.partition(":")
        target, _, from_menu = target.partition(":")
        if action == "acc" and target in ACCESS_MODES:
            users.access = target
            self._edit(msg, h.controller.render("users", ctx))
            return L.t("saved")
        if action == "add":
            users.awaiting_id[(ctx.chat, ctx.user)] = time.time()
            self.reply(ctx.chat, L.t("u_send_id"), json.dumps({"force_reply": True, "selective": True}))
            return ""
        role, who = users.role(target), users.who(target)
        if role is None:
            return L.t("unknown")
        if action in ("ok", "no") and role in ("pending", "blocked"):
            users.upsert(target, role="user" if action == "ok" else "blocked")
            log.info("Access %s for %s by %s", "granted" if action == "ok" else "denied", target, ctx.user)
            if action == "ok":
                self.reply(target, lambda L: L.t("rq_granted"))
            result = L.t("rq_allowed" if action == "ok" else "rq_denied", who=who)
            self._edit(msg, h.controller.render("users", ctx) if from_menu else (result, None))
            return result
        if action == "unb" and role == "blocked":
            users.remove(target)
            self._edit(msg, h.controller.render("users", ctx))
            return L.t("u_removed")
        if action in ("adm", "usr", "rm"):
            if target in users.env_ids:
                return L.t("u_env")
            if role == "admin" and action != "adm" and len(users.admins()) <= 1:
                return L.t("u_last_admin")
            if action == "rm":
                users.remove(target)
                self._edit(msg, h.controller.render("users", ctx))
                return L.t("u_removed")
            users.upsert(target, role="admin" if action == "adm" else "user")
            self._edit(msg, h.controller.render(f"user:{target}", ctx))
            return L.t("saved")
        return L.t("unknown")


def chart_title(L: Lang, hours: int) -> str:
    return L.t("chart_hours", hours=hours) if hours < 48 else L.t("chart_days", days=hours // 24)


class PowerPlan:
    """Windows power plan switching via powercfg; logs only on other OSes or in dry-run.

    The plan active before the outage is kept in a file next to the exe, so it is
    restored even if the program was restarted while running on battery.
    POWER_PLAN_BATTERY / POWER_PLAN_GRID in .env override the plan GUIDs; when
    POWER_PLAN_GRID is empty the plan that was active before the outage is restored.
    """

    def __init__(self, dry_run: bool):
        self.enabled = IS_WINDOWS and not dry_run
        self.state_file = data_dir() / "anker_ups.plan"
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

    def last_outage_end(self) -> int | None:
        rows = self._exec("SELECT MAX(end) FROM outages WHERE end IS NOT NULL")
        return rows[0][0] if rows else None

    def outage_ended(self, end: float, soc: int) -> None:
        self._exec("UPDATE outages SET end = ?, soc_end = ? WHERE end IS NULL", (int(end), soc))

    def energy_kwh(self, since: float, until: float | None = None) -> tuple[float, float]:
        rows = self._exec("SELECT COALESCE(SUM(ac_in), 0), COALESCE(SUM(out), 0) FROM samples "
                          "WHERE ts >= ? AND ts < ?", (int(since), int(until or time.time() + 60)))
        return rows[0][0] * 60 / 3.6e6, rows[0][1] * 60 / 3.6e6

    def outages_between(self, start: float, end: float) -> list[tuple]:
        # outages overlapping the window; an open one counts until now
        return self._exec("SELECT start, COALESCE(end, CAST(strftime('%s','now') AS INTEGER)), "
                          "soc_start, soc_end FROM outages WHERE start < ? AND "
                          "(end IS NULL OR end > ?) ORDER BY start", (int(end), int(start)))

    def energy_text(self, L: Lang) -> str:
        now = datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        periods = [("p_today", today), ("p_7d", now - timedelta(days=7)),
                   ("p_30d", now - timedelta(days=30)), ("p_all", datetime.fromtimestamp(0))]
        lines = [L.t("en_title")]
        for label, since in periods:
            e_in, e_out = self.energy_kwh(since.timestamp())
            lines.append(f"{L.t(label)}: {L.energy(e_in)} / {L.energy(e_out)}")
        return "\n".join(lines)

    def outages_text(self, L: Lang, days: int = 30, limit: int = 10) -> str:
        now = time.time()
        rows = self.outages_between(now - days * 86400, now)
        if not rows:
            return L.t("out_none", days=days)
        total = sum(end - start for start, end, *_ in rows)
        lines = [L.t("out_title", days=days, n=len(rows), dur=L.dur(total))]
        for start, end, soc_start, soc_end in rows[-limit:][::-1]:
            when = datetime.fromtimestamp(start).strftime("%d.%m %H:%M")
            used = f", {soc_start}% → {soc_end}%" if soc_end is not None else L.t("out_ongoing")
            lines.append(f"{when}  {L.dur(end - start)}{used}")
        return "\n".join(lines)

    def months_text(self, L: Lang, count: int = 2) -> str:
        """This month and the previous ones: outages, time without grid, energy."""
        now = datetime.now()
        starts = [now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)]
        while len(starts) < count:
            starts.append((starts[-1] - timedelta(days=1)).replace(day=1))
        lines = [L.t("mo_title")]
        for i, start in enumerate(starts):
            s, e = start.timestamp(), (now if i == 0 else starts[i - 1]).timestamp()
            spans = [min(b, e) - max(a, s) for a, b, *_ in self.outages_between(s, e)]
            e_in, e_out = self.energy_kwh(s, e)
            lines += ["", f"{L.month(start.month)} {start.year}",
                      L.t("mo_outages", n=len(spans), dur=L.dur(sum(spans)), longest=L.dur(max(spans)))
                      if spans else L.t("mo_none"),
                      L.t("sum_energy", e_in=L.energy(e_in), e_out=L.energy(e_out))]
        first = self._exec("SELECT MIN(ts) FROM samples")[0][0]
        if first and first > starts[-1].timestamp():
            lines += ["", L.t("mo_since", date=datetime.fromtimestamp(first).strftime("%d.%m.%Y"))]
        return "\n".join(lines)

    def summary_text(self, end: datetime, L: Lang, hours: int = 24) -> str:
        start = end - timedelta(hours=hours)
        rows = self._exec("SELECT AVG(out), MAX(out), SUM(ac_in), SUM(out) FROM samples "
                          "WHERE ts >= ? AND ts < ?", (int(start.timestamp()), int(end.timestamp())))
        avg_out, max_out, sum_in, sum_out = rows[0]
        outages = self.outages_between(start.timestamp(), end.timestamp())
        off = sum(min(e, end.timestamp()) - max(s, start.timestamp()) for s, e, *_ in outages)
        if avg_out is None:
            return L.t("sum_nodata", hours=hours)
        return "\n".join([
            L.t("sum_title", hours=hours, start=f"{start:%d.%m %H:%M}", end=f"{end:%d.%m %H:%M}"),
            L.t("sum_outages", n=len(outages), dur=L.dur(off)),
            L.t("sum_load", avg=f"{avg_out:.0f}", peak=f"{max_out:.0f}"),
            L.t("sum_energy", e_in=L.energy(sum_in * 60 / 3.6e6), e_out=L.energy(sum_out * 60 / 3.6e6))])

    def chart_png(self, hours: int, L: Lang, end: float | None = None) -> bytes | None:
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
        ax.plot(ts, [r[2] for r in rows], color="#d9480f", lw=1.2, label=L.t("legend_load"))
        ax.plot(ts, [r[1] for r in rows], color="#1971c2", lw=1.0, alpha=0.8, label=L.t("legend_input"))
        ax.set_ylabel("W")
        ax.set_ylim(bottom=0)
        soc_ax = ax.twinx()
        soc_ax.plot(ts, [r[3] for r in rows], color="#2f9e44", lw=1.5, label=L.t("legend_charge"))
        soc_ax.set_ylim(0, 105)
        soc_ax.set_ylabel("%")
        outages = self.outages_between(start, end)
        for s, e, *_ in outages:
            ax.axvspan(datetime.fromtimestamp(max(s, start)), datetime.fromtimestamp(min(e, end)),
                       color="#868e96", alpha=0.25, lw=0)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M" if hours <= 48 else "%d.%m"))
        ax.grid(alpha=0.3)
        handles = ax.get_legend_handles_labels()[0] + soc_ax.get_legend_handles_labels()[0]
        ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=3,
                  fontsize=8, frameon=False)
        ax.set_title(chart_title(L, hours) + (L.t("chart_grey") if outages else ""))
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
ALERT_FLAGS = {"alert_outage": "f_outage", "alert_full": "f_full", "alert_expansion": "f_expansion",
               "alert_link": "f_link"}


def _modes(*keys: str) -> Callable[[Lang, str], str]:
    return lambda L, v: L.t(keys[int(v)])


# station controls: key -> (state field, button values, label(L, value), setter); values are
# strings from callback data, the setter turns them into an MQTT command on the device object
STATION_CONTROLS: dict[str, tuple[str, list[str], Callable, Callable]] = {
    "light": ("light_mode", ["0", "1", "2", "3", "4"], _modes("m_off", "m_low", "m_medium", "m_high", "m_blink"),
              lambda d, v: d.set_light(mode=int(v))),
    "ac": ("ac_output_power_switch", ["1", "0"], _modes("m_off", "m_on"),
           lambda d, v: d.set_ac_output(enabled=v == "1")),
    "acmode": ("ac_output_mode", ["1", "2"], lambda L, v: L.t("m_normal" if v == "1" else "m_smart"),
               lambda d, v: d.set_ac_output(mode="normal" if v == "1" else "smart")),
    "dc": ("dc_output_power_switch", ["1", "0"], _modes("m_off", "m_on"),
           lambda d, v: d.set_dc_output(enabled=v == "1")),
    "dcmode": ("dc_12v_output_mode", ["1", "2"], lambda L, v: L.t("m_normal" if v == "1" else "m_smart"),
               lambda d, v: d.set_dc_output(mode="normal" if v == "1" else "smart")),
    "limit": ("ac_input_limit", ["200", "400", "600", "800", "1000"], lambda L, v: f"{v}W",
              lambda d, v: d.set_charge_limit(max_watts=int(v))),
    "fast": ("ac_fast_charge_switch", ["1", "0"], _modes("m_off", "m_on"),
             lambda d, v: d.set_fast_charging(enabled=v == "1")),
    "disp": ("display_mode", ["0", "1", "2", "3"], _modes("m_off", "m_low", "m_medium", "m_high"),
             lambda d, v: d.set_display(mode=int(v))),
    "dispto": ("display_timeout_seconds", ["20", "30", "60", "300", "1800"], lambda L, v: L.short(int(v)),
               lambda d, v: d.set_display(timeout_seconds=int(v))),
    "autooff": ("device_timeout_minutes", ["0", "30", "60", "120", "240", "360", "720", "1440"],
                lambda L, v: L.short(int(v) * 60) if v != "0" else L.t("m_never"),
                lambda d, v: d.set_device_timeout(timeout_minutes=int(v))),
}


# shorter button labels where a row has many buttons; the menu text keeps the full words
BUTTON_LABELS: dict[str, Callable[[Lang, str], str]] = {
    "disp": lambda L, v: "✗" if v == "0" else v,  # brightness levels 1-3
    "dispto": lambda L, v: L.short(int(v), compact=True),
    "autooff": lambda L, v: "✗" if v == "0" else L.short(int(v) * 60, compact=True),
}


class Controller:
    """Renders the Settings / Light / Users / Language menus and applies button presses.

    Runs in the Telegram thread; device commands are executed on the asyncio loop.
    """

    def __init__(self, settings: Settings, users: Users | None, data: Callable[[], dict], run: Callable,
                 device):
        self.settings = settings
        self.users = users
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

    def _label(self, key: str, L: Lang) -> str:
        state = self._state(key)
        return STATION_CONTROLS[key][2](L, state) if state in STATION_CONTROLS[key][1] else "?"

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

    def _row(self, key: str, L: Lang, prefix: str = "") -> list[dict]:
        current = self._state(key)
        _, values, label, _ = STATION_CONTROLS[key]
        label = BUTTON_LABELS.get(key, label)
        # rows of 4-5 buttons have ~6 characters each on a phone: no space after the marker
        mark = "•" if len(values) >= 4 else "• "
        return [{"text": (mark if v == current else "") + prefix + label(L, v), "callback_data": f"a:{key}:{v}"}
                for v in values]

    @staticmethod
    def _markup(L: Lang, rows: list[list[dict]], back: str | None = "m:root") -> str:
        if back:
            rows = [*rows, [{"text": L.t("back"), "callback_data": back}]]
        return json.dumps({"inline_keyboard": [r for r in rows if r]})

    def render(self, menu: str, ctx: Ctx) -> tuple[str, str]:
        L, st = ctx.L, self.settings
        if menu == "light":
            row = self._row("light", L)
            return L.t("light_title", v=self._label("light", L)), self._markup(L, [row[:3], row[3:]], back=None)
        if menu == "notif":
            th = st.thresholds
            on_off = {True: L.t("on"), False: L.t("off")}
            text = "\n".join([
                L.t("n_title"),
                # the icons match the button rows below
                "🔋 " + L.t("n_charge", v=", ".join(f"{t}%" for t in th) or L.t("off")),
                "🪫 " + L.t("n_critical", v=f"{st.critical}%" if st.critical else L.t("off")),
                "📅 " + L.t("n_summary", v=L.t("n_summary_v", t=st.summary) if st.summary else L.t("off")),
                *(L.t("n_flag", label=L.t(label), v=on_off[st.flag(k)]) for k, label in ALERT_FLAGS.items())])
            rows = [
                [{"text": ("✓" if t in th else "") + f"{t}%", "callback_data": f"a:th:{t}"} for t in THRESHOLD_CHOICES],
                # icons instead of words: five buttons in a row leave ~6 characters on a phone,
                # so the icon is on the first ("off") button only and marks the whole row
                [{"text": ("•" if c == st.critical else "") + (f"{c}%" if c else "🪫✗"),
                  "callback_data": f"a:crit:{c}"} for c in CRITICAL_CHOICES],
                [{"text": ("•" if t == st.summary else "") + (t or "📅✗"),
                  "callback_data": f"a:sum:{t or 'off'}"}
                 # a custom time from .env is shown too, so the current choice is visible
                 for t in SUMMARY_CHOICES + ([st.summary] if st.summary not in SUMMARY_CHOICES else [])],
                *([{"text": ("✓ " if st.flag(k) else "✗ ") + L.t(label), "callback_data": f"a:flag:{k}"}
                   for k, label in list(ALERT_FLAGS.items())[i:i + 2]] for i in range(0, len(ALERT_FLAGS), 2)),
            ]
            return text, self._markup(L, rows)
        if menu == "outputs":
            lines, rows = [L.t("s_outputs")], []
            for key, mode_key, name in (("ac", "acmode", "AC"), ("dc", "dcmode", "DC 12V")):
                if self._supported(key):
                    mode = f" ({self._label(mode_key, L)})" if self._supported(mode_key) else ""
                    lines.append(f"{name}: {self._label(key, L)}{mode}")
                    rows.append(self._row(key, L, f"{name} "))
                    if self._supported(mode_key):
                        rows.append(self._row(mode_key, L, f"{name} "))
            return "\n".join(lines if rows else [*lines, L.t("o_unsupported")]), self._markup(L, rows)
        if menu == "charging":
            lines, rows = [L.t("s_charging")], []
            for key, name in (("limit", "c_limit"), ("fast", "c_fast"), ("disp", "c_disp"),
                              ("dispto", "c_dispto"), ("autooff", "c_autooff")):
                if self._supported(key):
                    # the same icon starts the line and the buttons of that setting
                    icon = {"limit": "🔌", "fast": "⚡", "disp": "☀️", "dispto": "⏱", "autooff": "⏻"}[key]
                    lines.append(f"{icon} {L.t(name)}: {self._label(key, L)}")
                    prefix = {"fast": "⚡ ", "disp": "☀️", "dispto": "⏱", "autooff": "⏻"}.get(key, "")
                    row = self._row(key, L, prefix)
                    rows += [row[:4], row[4:]] if len(row) > 5 else [row]
            return "\n".join(lines), self._markup(L, rows)
        if menu == "acoff":
            return L.t("acoff_text"), self._markup(L, [[{"text": L.t("acoff_yes"), "callback_data": "a:ac:0!"},
                                                        {"text": L.t("cancel"), "callback_data": "m:outputs"}]],
                                                   back=None)
        if menu == "lang":
            row = self.users._row(ctx.chat) if self.users else None
            manual = row[2] if row else ""
            auto = lang(row[3] if row and row[3] else (self.users.default.code if self.users else "en"))
            buttons = [{"text": ("• " if not manual else "") + L.t("lang_auto", v=LANG_NAMES[auto.code]),
                        "callback_data": "l:auto"}]
            buttons += [{"text": ("• " if manual == code else "") + LANG_NAMES[code], "callback_data": f"l:{code}"}
                        for code in LANGS]
            return (L.t("lang_title", v=LANG_NAMES[L.code]),
                    self._markup(L, [buttons[:1], buttons[1:4], buttons[4:]]))
        if menu == "users" and ctx.admin and self.users:
            return self._render_users(L)
        if menu.startswith("user:") and ctx.admin and self.users:
            return self._render_user(L, menu.removeprefix("user:"))
        rows = [[{"text": L.t("s_notif"), "callback_data": "m:notif"},
                 {"text": L.t("s_outputs"), "callback_data": "m:outputs"}],
                [{"text": L.t("s_charging"), "callback_data": "m:charging"}],
                [{"text": L.t("s_lang"), "callback_data": "m:lang"}]]
        if ctx.admin and self.users:
            rows[2].append({"text": L.t("s_users"), "callback_data": "m:users"})
        return L.t("s_title"), self._markup(L, rows, back=None)

    @staticmethod
    def _icon(chat: str, role: str) -> str:
        if role == "pending":
            return "⏳"
        if role == "blocked":
            return "⛔"
        return "👑" if role == "admin" else ("👥" if chat.startswith("-") else "👤")

    def _render_users(self, L: Lang) -> tuple[str, str]:
        users = self.users
        lines = [L.t("s_users"), L.t("u_access", v=L.t(ACCESS_MODES[users.access])), L.t("u_hint"), ""]
        rows = [[{"text": ("• " if mode == users.access else "") + L.t(key), "callback_data": f"u:acc:{mode}"}
                 for mode, key in ACCESS_MODES.items()]]
        for chat, name, role in users.all():
            lines.append(f"{self._icon(chat, role)} {name or '?'} ({chat})")
            rows.append([{"text": f"{self._icon(chat, role)} {(name or chat)[:40]}", "callback_data": f"m:user:{chat}"}])
        rows.append([{"text": L.t("u_add"), "callback_data": "u:add"}])
        return "\n".join(lines), self._markup(L, rows)

    def _render_user(self, L: Lang, chat: str) -> tuple[str, str]:
        users = self.users
        role = users.role(chat)
        if role is None:
            return self._render_users(L)
        kind = "r_group" if chat.startswith("-") and role in ACTIVE_ROLES else f"r_{role}"
        text = f"{self._icon(chat, role)} {users.who(chat)}\n{L.t('u_role', v=L.t(kind))}"
        if role == "pending":
            rows = [[{"text": L.t("rq_allow"), "callback_data": f"u:ok:{chat}:m"},
                     {"text": L.t("rq_deny"), "callback_data": f"u:no:{chat}:m"}]]
        elif role == "blocked":
            rows = [[{"text": L.t("rq_allow"), "callback_data": f"u:ok:{chat}:m"},
                     {"text": L.t("u_remove"), "callback_data": f"u:unb:{chat}"}]]
        else:
            rows = [[{"text": L.t("u_remove"), "callback_data": f"u:rm:{chat}"}]]
            if not chat.startswith("-"):  # groups cannot be admins: admin rights are per person
                toggle = ("u_make_user", "usr") if role == "admin" else ("u_make_admin", "adm")
                rows[0].insert(0, {"text": L.t(toggle[0]), "callback_data": f"u:{toggle[1]}:{chat}"})
        return text, self._markup(L, rows, back="m:users")

    def act(self, key: str, value: str, ctx: Ctx) -> tuple[str, str]:
        """Apply a button press; returns (toast text, menu to show next)."""
        L, st = ctx.L, self.settings
        if key == "th" and int(value) in THRESHOLD_CHOICES:
            th = set(st.thresholds) ^ {int(value)}
            st.set("thresholds", ",".join(str(t) for t in sorted(th, reverse=True)))
            return L.t("saved"), "notif"
        if key == "crit" and int(value) in CRITICAL_CHOICES:
            st.set("critical", value)
            return L.t("saved"), "notif"
        if key == "sum" and (value == "off" or value in SUMMARY_CHOICES):
            st.set("summary", "" if value == "off" else value)
            return L.t("saved"), "notif"
        if key == "flag" and value in ALERT_FLAGS:
            st.set(value, "0" if st.flag(value) else "1")
            return L.t("saved"), "notif"
        if key not in STATION_CONTROLS:
            return L.t("unknown"), "root"
        confirmed = value.endswith("!")
        value = value.rstrip("!")
        field, values, label, setter = STATION_CONTROLS[key]
        if value not in values:
            return L.t("unknown"), "root"
        if key == "ac" and value == "0" and not confirmed:
            return "", "acoff"
        try:
            ok = self.run(setter(self.device, value)) is not None
        except Exception:
            log.exception("command %s=%s failed", key, value)
            ok = False
        log.info("Command %s=%s by %s: %s", key, value, ctx.user, "ok" if ok else "failed")
        if ok:
            self.pending[field] = (value, time.time())
        menu = {"light": "light", "ac": "outputs", "acmode": "outputs", "dc": "outputs",
                "dcmode": "outputs"}.get(key, "charging")
        return (f"{label(L, value)} ✓" if ok else L.t("cmd_failed")), menu


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
        # wall clock when the grid came back after an outage, until the battery is full;
        # kept in the DB so a restart while charging still sends the full-charge alert
        saved = stats.meta("charge_pending") if stats else None
        self.charge_pending: float | None = float(saved) if saved else None

    def _set_charge_pending(self, value: float | None) -> None:
        self.charge_pending = value
        if self.stats:
            self.stats.meta("charge_pending", "" if value is None else str(int(value)))

    def outage_seconds(self) -> float | None:
        return time.time() - self.outage_started if self.outage_started else None

    def evaluate(self, grid: bool, ac_in: int, levels: dict[str, int], remaining_h: float | None = None) -> None:
        """levels ({"main": %, "expansion": %}) drive the per-battery thresholds and the messages."""
        now = time.monotonic()
        total = round(sum(levels.values()) / len(levels))
        name = self.name
        if grid:
            self.no_grid_since = None
            if self.on_battery is None and self.stats and (start := self.stats.open_outage()):
                # the outage ended while we were not running
                self.on_battery, self.outage_started = True, start
            if self.on_battery:
                if self.settings.flag("alert_outage"):
                    off = self.outage_seconds()
                    self.tg.send(lambda L: L.t("al_back", dur=L.dur(off) if off else "?", ac_in=ac_in,
                                               charge=L.charge(levels)))
                if self.stats:
                    self.stats.outage_ended(time.time(), total)
                if min(levels.values()) < 100:  # nothing to report after a short outage
                    self._set_charge_pending(time.time())
            elif self.charge_pending and min(levels.values()) >= 100:
                # once per outage: the station hovers 99-100 % when full, so no repeats
                if self.settings.flag("alert_full"):
                    took = time.time() - self.charge_pending
                    self.tg.send(lambda L: L.t("al_full", dur=L.dur(took), charge=L.charge(levels)))
                self._set_charge_pending(None)
            if self.on_battery is not False:
                # also on the first reading, in case we were restarted during an outage
                self.power.restore()
            self.on_battery = False
            self.outage_started = None
            self.fired.clear()
            self.critical_fired = False
            return

        def left(L: Lang) -> str:
            return L.t("al_left", dur=L.dur(remaining_h * 3600)) if remaining_h else ""

        if self.no_grid_since is None:
            self.no_grid_since = now
        if not self.on_battery and now - self.no_grid_since >= self.confirm_s:
            self.on_battery = True
            resumed = self.stats.open_outage() if self.stats else None
            if resumed:
                # restarted during an outage: keep its start, do not repeat earlier alerts
                self.outage_started = resumed
                for key, pct in levels.items():
                    self.fired[key] = {t for t in self.settings.thresholds if pct <= t}
                self.critical_fired = bool(self.settings.critical and total <= self.settings.critical)
                log.info("Resumed outage started %s", datetime.fromtimestamp(resumed))
            else:
                self.outage_started = time.time() - (now - self.no_grid_since)
                self._set_charge_pending(None)  # charging did not finish before this outage
                # how long the grid was up: since the end of the previous outage
                last_end = self.stats.last_outage_end() if self.stats else None
                up = self.outage_started - last_end if last_end and self.outage_started > last_end else None
                if self.settings.flag("alert_outage"):
                    self.tg.send(lambda L: L.t("al_lost", name=name, charge=L.charge(levels), left=left(L),
                                               uptime=L.t("al_uptime", dur=L.dur(up)) if up else ""))
                if self.stats:
                    self.stats.outage_started(self.outage_started, total)
            self.power.power_saver()

        if self.on_battery:
            # per battery: report only the lowest crossed threshold, mark the higher ones as done
            for key, pct in levels.items():
                fired = self.fired.setdefault(key, set())
                crossed = [t for t in self.settings.thresholds if pct <= t and t not in fired]
                if crossed:
                    t = min(crossed)
                    fired.update(x for x in self.settings.thresholds if x >= t)
                    self.tg.send(lambda L, key=key, pct=pct, t=t: L.t(
                        "al_threshold", name=L.battery(key), pct=pct, t=t, charge=L.charge(levels), left=left(L)))
            critical = self.settings.critical
            if critical and total <= critical and not self.critical_fired:
                self.critical_fired = True
                self.tg.send(lambda L: L.t("al_critical", charge=L.charge(levels), left=left(L)))
                L = self.tg.default
                show_popup(L.t("popup_title", name=name),
                           L.t("popup_text", charge=L.charge(levels), left=left(L)))


def battery_state(data: dict) -> tuple[dict[str, int] | None, int]:
    """Return (per-battery %, expansion pack count) from MQTT data.

    Field names differ a little between station models, so known variants are tried.
    """
    main = as_int(data.get("main_battery_soc", data.get("battery_soc")))
    if main is None:
        return None, 0
    exps = [as_int(data.get(f"exp_{i}_soc")) for i in range(1, 7)]
    packs = as_int(data.get("expansion_packs"))
    if packs is None:  # models without a pack counter report 0% for empty slots
        packs = sum(1 for e in exps if e)
    exps = [e for e in exps[:packs] if e is not None]
    if not exps:
        return {"main": main}, packs
    return {"main": main} | {("expansion" if len(exps) == 1 else f"expansion {i}"): e
                             for i, e in enumerate(exps, 1)}, packs


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
    tg.send(lambda L: stats.summary_text(now, L))

    def chart(L: Lang) -> bytes | None:
        with contextlib.suppress(Exception):
            return stats.chart_png(24, L, end=now.timestamp())
        return None

    tg.send_photo(chart, lambda L: "📈 " + chart_title(L, 24))


def ping_healthcheck(url: str) -> None:
    """Dead man's switch (e.g. healthchecks.io): the service alerts when the pings stop."""
    def ping() -> None:
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                resp.read()
        except Exception as exc:
            log.warning("Healthcheck ping failed: %s", exc)

    threading.Thread(target=ping, daemon=True).start()


async def run(args: argparse.Namespace) -> None:
    env_chats, env_admins = id_list(os.environ.get("TG_CHAT_ID")), id_list(os.environ.get("TG_ADMIN_IDS"))
    default_lang = lang(os.environ.get("BOT_LANGUAGE") or "en")
    tg = Telegram(os.environ.get("TG_BOT_TOKEN"), bool(env_chats or env_admins), default_lang)
    power = PowerPlan(dry_run=args.dry_run)
    power.log_plans()
    # statistics, users and bot settings only matter with Telegram; a PC that just switches
    # power plans (empty TG_BOT_TOKEN) runs without the database
    stats = Stats(data_dir() / "anker_ups.db") if tg.enabled else None
    users = Users(stats, env_chats, env_admins, default_lang) if stats else None
    tg.users = users
    settings = Settings(stats, {
        "thresholds": ",".join(str(t) for t in args.thresholds),
        "critical": os.environ.get("CRITICAL_LEVEL") or "10",
        "summary": os.environ.get("SUMMARY_TIME", "09:00").strip(),
        "alert_outage": "1", "alert_full": "1", "alert_expansion": "1", "alert_link": "1",
    })
    watchdog = Watchdog(tg, power, confirm_s=args.confirm, settings=settings, stats=stats)
    healthcheck = os.environ.get("HEALTHCHECK_URL", "").strip()

    if args.test_telegram:
        tg.send(lambda L: L.t("test_msg"))
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
        expansion: int | None = None  # pack count, None until the first telemetry
        disconnected_since: float | None = None
        last_restart = last_retrigger = time.monotonic()
        last_ping = 0.0
        link_alerted = False
        snapshot: dict = {}
        last_plug: int | None = None

        def status_text(L: Lang) -> str:
            if not snapshot:
                return L.t("no_data_yet")
            age = int(time.monotonic() - snapshot["at"])
            grid = {True: L.t("grid_down"), False: L.t("grid_up")}.get(watchdog.on_battery, "?")
            lines = [f"📊 {watchdog.name}", L.t("st_grid", state=grid), L.t("st_charge", charge=L.charge(snapshot["levels"]))]
            if watchdog.on_battery:
                off = watchdog.outage_seconds()
                lines.append(L.t("st_outage", dur=L.dur(off) if off else "?"))
                if snapshot.get("remaining_h"):
                    lines.append(L.t("st_remaining", dur=L.dur(snapshot["remaining_h"] * 3600)))
            lines += [L.t("st_power", ac_in=snapshot["ac_in"], out=snapshot["out"]), L.t("st_age", age=age)]
            return "\n".join(lines)

        mqtt_device = SolixMqttDeviceFactory(api_instance=api, device_sn=sn).create_device()
        loop = asyncio.get_running_loop()

        def run_on_loop(coro):
            # called from the Telegram thread; MQTT commands must run on the asyncio loop
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=15)

        controller = Controller(
            settings, users, lambda: api.mqttsession.mqtt_data.get(sn, {}) if api.mqttsession else {},
            run_on_loop, mqtt_device)
        tg.listen_commands(BotHandlers(status_text, controller, stats))
        while True:
            await asyncio.sleep(1)
            session = api.mqttsession
            now = time.monotonic()
            send_daily_summary(tg, stats, settings.summary)
            if healthcheck and now - last_ping >= HEALTHCHECK_EVERY:
                last_ping = now
                ping_healthcheck(healthcheck)

            if not (session and session.is_connected()) or poller.done():
                disconnected_since = disconnected_since or now
            else:
                disconnected_since = None
            silent = now - last_change
            if last_message and not link_alerted and silent > args.link_timeout:
                link_alerted = True
                if settings.flag("alert_link"):
                    seconds = int(silent)
                    tg.send(lambda L: L.t("al_nodata", s=seconds))
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
                levels, packs = battery_state(data)
                ac_in = as_int(data.get("ac_input_power", data.get("ac_input_power_total")))
                charging = as_int(data.get("charging_status"))
                remaining_h = data.get("remaining_time_hours")
                if levels:
                    snapshot.update(at=now, levels=levels, ac_in=ac_in, remaining_h=remaining_h,
                                    out=as_int(data.get("output_power_total")))
                log.debug("ac_in=%sW charging_status=%s out=%sW in_total=%sW battery=%s",
                          ac_in, charging, data.get("output_power_total"),
                          data.get("photovoltaic_power"), levels)
                if levels and packs != expansion:
                    if expansion is not None and settings.flag("alert_expansion"):
                        key = "al_exp_on" if packs > expansion else "al_exp_off"
                        tg.send(lambda L, key=key, lv=levels: L.t(key, charge=L.charge(lv)))
                    expansion = packs
                if link_alerted:
                    if settings.flag("alert_link"):
                        tg.send(lambda L: L.t("al_data_back"))
                    link_alerted = False
                if levels and ac_in is not None:
                    last_levels = levels
                    # charging_status (where reported): 0 idle, 1 solar, 2 AC, 3 both.
                    # ac_input_plug_status (C1000 Gen 2, C2000 Gen 2, C300...): 1 = AC connected;
                    # these models have no charging_status, and with a full battery and a small
                    # load the AC input drops to 0 W while the grid is up
                    plug = as_int(data.get("ac_input_plug_status"))
                    if plug != last_plug:
                        log.info("ac_input_plug_status %s -> %s (ac_in %s W)", last_plug, plug, ac_in)
                        last_plug = plug
                    grid = ac_in > 0 or charging in (2, 3) or plug == 1
                    if stats:
                        stats.add_reading(ac_in, as_int(data.get("output_power_total")) or 0,
                                          round(sum(levels.values()) / len(levels)), grid)
                    watchdog.evaluate(grid, ac_in, levels, remaining_h)
                    continue
            # keep re-evaluating so the outage confirm timer runs between messages
            if watchdog.no_grid_since is not None and not watchdog.on_battery and last_levels:
                watchdog.evaluate(False, 0, last_levels)


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
        data_dir() / "anker_ups.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
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
