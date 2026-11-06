"""Shared fixtures: a database in a temp folder and a Telegram bot with a fake API."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ankerups.bot import BotHandlers, Telegram  # noqa: E402
from ankerups.i18n import lang  # noqa: E402
from ankerups.menus import Controller  # noqa: E402
from ankerups.stats import Settings, Stats  # noqa: E402
from ankerups.users import Users  # noqa: E402

DEFAULTS = {"thresholds": "75,50,25", "critical": "10", "summary": "", "alert_outage": "1",
            "alert_full": "1", "alert_expansion": "1", "alert_link": "1"}


@pytest.fixture
def stats(tmp_path):
    return Stats(tmp_path / "test.db")


@pytest.fixture
def settings(stats):
    return Settings(stats, dict(DEFAULTS))


class FakeBot:
    """Telegram with the HTTP calls recorded and the send queue drained synchronously."""

    ADMIN, GROUP = "397", "-100555"

    def __init__(self, stats, settings, station: dict):
        self.tg = Telegram(None, True, lang("en"))  # no token: no worker thread, we drain the queue
        self.tg.enabled = True
        self.calls: list[tuple[str, dict]] = []
        self.tg._call = self._call
        self.tg._send_photo = lambda chat, png, caption, markup: self.calls.append(
            ("sendPhoto", {"chat_id": chat, "caption": caption}))
        self.users = Users(stats, [self.ADMIN, self.GROUP], [self.ADMIN], lang("en"))
        self.tg.users = self.users
        self.station = station
        self.controller = Controller(settings, self.users, lambda: self.station, lambda coro: True, None)
        self.tg.has_light = self.controller.has_light
        self.handlers = BotHandlers(lambda L: f"📊 Station\n{L.code}", self.controller, stats)

    def _call(self, method, params, timeout=10):
        self.calls.append((method, params))
        if method == "getChat":
            return {"result": {"id": int(params["chat_id"]), "first_name": "Petro"}}
        return {"result": {}}

    def drain(self) -> list[tuple[str, dict]]:
        """Deliver queued messages like the worker does; returns and clears the recorded calls."""
        q = self.tg.queue.queue
        while q:
            kind, payload, extra, target, code, title = q.popleft()
            chats = [target] if target else self.users.recipients()
            for chat in chats:
                L = lang(code) if code else self.tg.lang_of(chat)
                if kind == "photo":
                    png = payload(L)
                    if png is not None:
                        self.tg._deliver(kind, chat, (png, extra(L), self.tg.keyboard(L)))
                    continue
                markup = extra(L) if callable(extra) else extra
                text = payload(L) if callable(payload) else payload
                self.tg._deliver(kind, chat, (text, markup or self.tg.keyboard(L), title))
        calls, self.calls = self.calls, []
        return calls

    def message(self, chat, text, user=None, lc="en", kind="private", name="Tester"):
        user = user or chat
        self.tg._handle_update({"message": {
            "chat": {"id": int(chat), "type": kind, "first_name": name, "title": name if kind != "private" else None},
            "from": {"id": int(user), "first_name": name, "language_code": lc}, "text": text}}, self.handlers)
        return self.drain()

    def press(self, chat, data, user=None):
        self.tg._handle_update({"callback_query": {"id": "q", "from": {"id": int(user or chat)}, "data": data,
                                                   "message": {"chat": {"id": int(chat)}, "message_id": 1}}},
                               self.handlers)
        return self.drain()


def texts(calls, method=None):
    return [p.get("text") or p.get("caption") for m, p in calls if method in (None, m) and m != "answerCallbackQuery"]


def toast(calls):
    return next(p["text"] for m, p in calls if m == "answerCallbackQuery")


def buttons(params) -> list[list[str]]:
    markup = json.loads(params.get("reply_markup") or "{}")
    return [[b["text"] for b in row] for row in markup.get("inline_keyboard", markup.get("keyboard", []))]


@pytest.fixture
def bot(stats, settings):
    station = {"main_battery_soc": 80, "light_mode": 0, "ac_output_power_switch": 1, "ac_output_mode": 1,
               "display_mode": 2, "display_timeout_seconds": 30, "device_timeout_minutes": 0,
               "ac_input_limit": 800, "ac_fast_charge_switch": 1}
    return FakeBot(stats, settings, station)
