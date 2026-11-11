"""Telegram: sending alerts and replies, long polling and button handling.
"""

import contextlib
import json
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .common import as_int, log
from .i18n import Lang, Render, lang
from .menus import TEXT_SETTINGS, Controller, Ctx, paint
from .stats import SESSION_CHOICES, SESSION_PERIODS, Settings, Stats, chart_title, parse_range, session_period
from .translations import LANG_NAMES, LANGS
from .users import ACCESS_MODES, ACTIVE_ROLES, Users, chat_name
from . import widget_clock, widget_ios, widget_quick
from .widget import widget_html

if TYPE_CHECKING:
    from .schedmenu import ScheduleMenu

WIDGET_UPDATE_EVERY = 15  # seconds between checks for new widget data
WIDGET_IDLE_EVERY = 60  # an unchanged widget still gets fresh data this often (times, minutes)
MAX_RETRY_WAIT = 300  # flood control longer than this holds the alert instead of waiting
RETRY_HELD_EVERY = 600  # Telegram's long holds often cover only some messages: try again this soon


def card(title: str, rows: list[tuple[str, str]] | tuple = (), note: str = "") -> str:
    """An alert as a rich message: a title, a two-column table, a note under it."""
    html = f"<h3>{_esc(title)}</h3>"
    if rows:
        html += "<table bordered>" + "".join(f"<tr><td>{_esc(a)}</td><td>{_esc(b)}</td></tr>"
                                            for a, b in rows) + "</table>"
    return html + (f"<footer>{_esc(note.strip())}</footer>" if note.strip() else "")


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _reply_keyboard(markup: str | None) -> bool:
    return bool(markup) and '"keyboard"' in markup


def retry_after(body: str) -> int | None:
    """Seconds Telegram asks to wait in a 429 answer."""
    try:
        return int(json.loads(body)["parameters"]["retry_after"])
    except (ValueError, KeyError, TypeError):
        return None


def widget_core(payload: str) -> str:
    """The widget data without the fields that change every time (timestamps, the minute)."""
    try:
        data = json.loads(payload)
    except ValueError:
        return payload
    core = {k: v for k, v in data.items() if k not in ("t", "u", "m")}
    for k, step in (("i", 10), ("o", 10), ("r", 0.5)):  # watts and hours wobble from reading to reading
        if isinstance(core.get(k), (int, float)):
            core[k] = round(core[k] / step) * step
    return json.dumps(core, sort_keys=True)
WIDGET_LIVE_FOR = 48 * 3600  # a status widget stops updating after this long


@dataclass
class BotHandlers:
    """Callbacks the Telegram thread uses to read state and act on the station."""

    status: Render
    controller: "Controller"
    stats: "Stats | None" = None
    widget: "Callable[[], tuple[str, str] | None] | None" = None  # (station name, widget data JSON)
    quick: "Callable[[], str] | None" = None  # data of the ⚙️ Quick settings widget
    sched: "ScheduleMenu | None" = None


KEYBOARD_ACTIONS = (("btn_status", "status"), ("btn_light", "light"), ("btn_12v", "light"),
                    ("btn_stats", "stats"), ("btn_settings", "settings"))


# button texts in every language -> action; in groups the bot sees every message (privacy mode
# off), so only exact button texts and commands are handled, not any text with "status" in it
BUTTON_ACTIONS = {lang(code).t(key).lower(): action for code in LANGS for key, action in KEYBOARD_ACTIONS}
# labels of keyboards sent before v1.4.0 ("🔦 Light" became "🔦 Light/12V")
BUTTON_ACTIONS |= {lang(code).t("btn_light").lower().removesuffix("/12v"): "light" for code in LANGS}


COMMANDS = {"start": "status", "status": "status", "light": "light", "stats": "stats", "settings": "settings"}


REMOVE_KEYBOARD = json.dumps({"remove_keyboard": True})
FORCE_REPLY = json.dumps({"force_reply": True, "selective": True})


def main_keyboard(L: Lang, panel: str | None = "btn_light") -> str:
    """Persistent reply keyboard; Status gets its own row (Telegram makes a row's buttons equal).
    panel is the label key of the light / 12V button, None when the station has neither."""
    row = [{"text": L.t("btn_stats")}, {"text": L.t("btn_settings")}]
    return json.dumps({"keyboard": [[{"text": L.t("btn_status")}],
                                    [{"text": L.t(panel)}, *row] if panel else row],
                       "resize_keyboard": True, "is_persistent": True})


def stats_menu(L: Lang) -> str:
    return json.dumps({"inline_keyboard": [
        [{"text": L.t("chart24"), "callback_data": "stats:chart24"},
         {"text": L.t("chart7"), "callback_data": "stats:chart168"}],
        [{"text": L.t("outages"), "callback_data": "stats:outages"},
         {"text": L.t("energy"), "callback_data": "stats:energy"},
         {"text": L.t("months"), "callback_data": "stats:months"}],
        [{"text": L.t("sessions"), "callback_data": "stats:sessions"}]]})


def sessions_menu(L: Lang, period: str, w: int) -> str:
    """The periods and thresholds under the sessions table; a press sends it again in its place."""
    names = {"today": L.t("p_today"), "yday": L.t("p_yday"), "7": L.t("p_7d"), "30": L.t("p_30d")}
    return json.dumps({"inline_keyboard": [
        [{"text": ("•" if p == period else "") + names[p], "callback_data": f"stats:sessions:{p}:{w}"}
         for p in SESSION_PERIODS] + [{"text": ("•" if period not in names else "") + "✏️",
                                       "callback_data": f"stats:sessions:ask:{w}"}],
        [{"text": ("•" if c == w else "") + f"{c}W", "callback_data": f"stats:sessions:{period}:{c}"}
         for c in SESSION_CHOICES]]})


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
        self.panel_button: Callable[[], str | None] = lambda: "btn_light"  # set by listen_commands
        self.awaiting: dict[tuple[str, str], tuple[str, float]] = {}  # (chat, user) -> (input, time)
        self.widget_lock = threading.Lock()
        self.widget_last: dict[str, tuple[str, float]] = {}  # "chat|look" -> (data sent, when)
        self.widget_paused: dict[str, float] = {}  # "chat|look" -> until when its edits are refused
        self.blocked: dict[str, float] = {}  # chat -> when to try its held alerts again
        self.widgets_blocked: dict[str, float] = {}  # "chat|look" -> until when new widgets are refused
        self.cannot_delete: dict[str, float] = {}  # groups where the bot is not an admin: until when
        self.keyboard_blocked: dict[str, float] = {}  # the same for messages with the reply keyboard
        self.deferred: dict[str, list[str]] = {}  # texts held for a blocked chat
        self.temps: Callable[[], dict] | None = None  # daily temperatures for the stats, set by the app
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
        return main_keyboard(L, self.panel_button())

    def send(self, render: Render, markup: Render | None = None, thread: str | None = None,
             rich: Render | None = None, end: str | None = None) -> None:
        """Alert to all active chats, each in its language; markup: inline buttons under it.

        rich: the alert as a rich message (a card, see card()), the text is its fallback.
        Runs of alerts keep the chat clean: thread ("outage") adds the alert to a run, end deletes
        the run's earlier alerts once this one is sent (with the same thread: it replaces them)."""
        log.info("ALERT: %s", render(lang("en")))
        if self.enabled:
            stamp = time.strftime("%H:%M:%S")
            self.queue.put(("text", lambda L: f"{stamp} {render(L)}", markup, None, None, False,
                            {"thread": thread, "rich": rich, "end": end}))

    def reply(self, chat: str, text: str | Render, markup: str | Render | None = None,
              code: str | None = None, title: bool = False) -> None:
        """Message to one chat; code forces the language (people who are not users yet);
        title makes the first line bold (menus, status, stats)."""
        if self.enabled:
            self.queue.put(("text", text, markup, chat, code, title))

    def send_photo(self, png: Callable[[Lang], bytes | None], caption: Render, chat: str | None = None) -> None:
        if self.enabled:
            self.queue.put(("photo", png, caption, chat, None, False))

    def _worker(self) -> None:
        while True:
            self._process(*self.queue.get())

    def _process(self, kind: str, payload, extra, target: str | None, code: str | None, title: bool,
                 opts: dict | None = None) -> None:
        """Render one queued message for each chat in its language and send it."""
        opts = opts or {}
        thread, rich, end = opts.get("thread"), opts.get("rich"), opts.get("end")
        chats = [target] if target else (self.users.recipients() if self.users else [])
        photos: dict[str, bytes | None] = {}  # one chart per language
        for chat in chats:
            L = lang(code) if code else self.lang_of(chat)
            if kind == "rich":  # a rich message (tables); the text version if it is refused
                try:
                    fallback, markup = extra
                    self._deliver_rich(chat, payload(L), fallback(L), markup(L) if markup else self.keyboard(L))
                except Exception:
                    log.exception("Rich message failed")
                continue
            if kind == "widget":
                try:
                    name, data = payload()
                    fallback, style = extra
                    html = {"ios": widget_ios.widget_html, "clock": widget_clock.widget_html,
                            "quick": widget_quick.widget_html}.get(style, widget_html)(L, name)
                    self._deliver_widget(chat, html, data, L, fallback, style)
                except Exception:
                    log.exception("Status widget failed")
                continue
            try:
                if kind == "photo":
                    if L.code not in photos:
                        photos[L.code] = payload(L)
                    if photos[L.code] is None:
                        continue
                    args = (photos[L.code], extra(L), self.keyboard(L))
                else:
                    markup = extra(L) if callable(extra) else extra
                    args = (payload(L) if callable(payload) else payload, markup or self.keyboard(L), title)
            except Exception:
                log.exception("Telegram message rendering failed")
                continue
            sent = None
            if rich:  # tried even under flood control: Telegram lets some messages through
                try:
                    sent = self._call("sendRichMessage", {"chat_id": chat, "reply_markup": args[1],
                                                          "rich_message": json.dumps({"html": rich(L)})})["result"]
                except urllib.error.HTTPError as exc:
                    if exc.code == 403:  # the user blocked the bot or it left the group: text fails too
                        log.warning("Rich alert to %s refused (403), skipping", chat)
                        continue
                    log.warning("Rich alert to %s failed (%s), sending text", chat, exc)
                except Exception as exc:
                    log.warning("Rich alert to %s failed (%s), sending text", chat, exc)
            if sent is None:
                sent = self._deliver(kind, chat, args, alert=target is None)
            if sent is not None:
                if end:
                    self._thread(chat, end, end=True)
                if thread:
                    self._thread(chat, thread, add=sent.get("message_id"))

    def _thread(self, chat: str, name: str, add: int | None = None, end: bool = False) -> None:
        """Message ids of a run of alerts per chat (kept in the DB); end deletes them."""
        if not self.users:
            return
        key = f"thread_{name}"
        try:
            saved = json.loads(self.users.stats.meta(key) or "{}")
        except ValueError:
            saved = {}
        if add:
            saved.setdefault(chat, []).append(add)
        if end:
            for message_id in saved.pop(chat, []):
                with contextlib.suppress(Exception):  # older than 48 h or already gone
                    self._call("deleteMessage", {"chat_id": chat, "message_id": message_id})
        self.users.stats.meta(key, json.dumps(saved))

    def _blocked(self, chat: str, table: dict | None = None) -> bool:
        return time.time() < (self.blocked if table is None else table).get(chat, 0)

    def _defer(self, chat: str, kind: str, args: tuple, alert: bool) -> None:
        """Keep an alert for a chat Telegram holds back (long flood control); sent once it lifts.
        Replies to buttons are not kept: they would be stale by then."""
        if alert and kind == "text" and len(self.deferred.setdefault(chat, [])) < 30:
            self.deferred[chat].append(args[0])

    def _flush_deferred(self) -> None:
        for chat in [c for c, texts in self.deferred.items() if texts and not self._blocked(c)]:
            texts = self.deferred.pop(chat)
            L = self.lang_of(chat)
            body = L.t("tg_held") + "\n\n" + "\n".join(texts)
            log.info("Sending %d held messages to %s", len(texts), chat)
            self._deliver("text", chat, (body[:4000], self.keyboard(L), True))

    def _deliver(self, kind: str, chat: str, args: tuple, alert: bool = False) -> dict | None:
        """Send a message (retrying network errors); returns the sent message, None if not sent."""
        while True:
            try:
                if kind == "photo":
                    self._send_photo(chat, *args)
                    return None
                params = {"chat_id": chat, "text": args[0], "reply_markup": args[1]}
                if _reply_keyboard(args[1]) and self._blocked(chat, self.keyboard_blocked):
                    del params["reply_markup"]
                if args[2]:
                    params["entities"] = bold_first_line(args[0])
                return self._call("sendMessage", params).get("result")
            except urllib.error.HTTPError as exc:
                body = exc.read().decode(errors="replace")
                if exc.code == 429:  # flood control: wait as long as Telegram says
                    wait = retry_after(body) or 15
                    if wait > MAX_RETRY_WAIT and kind == "text" and _reply_keyboard(args[1]) \
                            and not self._blocked(chat, self.keyboard_blocked):
                        # the block may cover only the reply keyboard (it follows the widget one):
                        # the same message without it
                        log.warning("Telegram refuses the reply keyboard in %s for %d s, sending without it",
                                    chat, wait)
                        self.keyboard_blocked[chat] = time.time() + wait
                        continue
                    if wait > MAX_RETRY_WAIT:
                        log.error("Telegram holds a message to %s back for %d s, trying again later", chat, wait)
                        self.blocked[chat] = time.time() + min(wait, RETRY_HELD_EVERY)
                        self._defer(chat, kind, args, alert)
                        return
                    log.warning("Telegram send to %s: flood control, retrying in %d s", chat, wait)
                    time.sleep(wait + 1)
                    continue
                if exc.code < 500:
                    # wrong id, bot blocked or removed from the group: retrying won't help
                    log.warning("Telegram send to %s failed: %s %s", chat, exc.code, body[:200])
                    return
                log.warning("Telegram send failed (%s), retrying in 15s", exc)
            except Exception as exc:  # no internet during outage is expected
                log.warning("Telegram send failed (%s), retrying in 15s", exc)
            time.sleep(15)

    def reply_rich(self, chat: str, html: Render, fallback: Render, markup: Render | None = None) -> None:
        """A rich message (tables); markup: its inline buttons instead of the reply keyboard."""
        if self.enabled:
            self.queue.put(("rich", html, (fallback, markup), chat, None, False))

    def _deliver_rich(self, chat: str, html: str, fallback: str, markup: str) -> None:
        try:
            self._call("sendRichMessage", {"chat_id": chat, "rich_message": json.dumps({"html": html}),
                                           "reply_markup": markup})
        except urllib.error.HTTPError as exc:
            log.warning("sendRichMessage to %s failed: %s %s, sending text", chat, exc.code,
                        exc.read().decode(errors="replace")[:200])
            self._deliver("text", chat, (fallback, markup, True))

    def send_widget(self, chat: str, data: Callable[[], tuple[str, str]], fallback: Render,
                    style: str = "widget") -> None:
        """📊 Status as a live widget ("widget" or the app-like "ios"); fallback is the text
        status if the widget is refused."""
        if self.enabled:
            self.queue.put(("widget", data, (fallback, style), chat, None, False))

    def _deliver_widget(self, chat: str, html: str, data: str, L: Lang, fallback: Render,
                        style: str = "widget") -> None:
        params = {"chat_id": chat, "html": html, "chat_data": data, "reply_markup": self.keyboard(L),
                  "allow_queries": "true"}  # the light / 12V buttons inside the widget
        if self._blocked(f"{chat}|{style}", self.widgets_blocked):  # refused for now: the text status
            self._deliver("text", chat, (fallback(L), self.keyboard(L), True))
            return
        try:
            message = self._call("sendWidget", params, timeout=20)["result"]
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            log.warning("sendWidget to %s failed: %s %s, sending text", chat, exc.code, body[:200])
            if exc.code == 429:
                self.widgets_blocked[f"{chat}|{style}"] = time.time() + (retry_after(body) or 60)
            elif "WIDGET_NOT_ALLOWED" in body:  # groups: widgets are for private chats for now
                for look in ("widget", "ios", "clock", "quick"):
                    self.widgets_blocked[f"{chat}|{look}"] = time.time() + 86400
            self._deliver("text", chat, (fallback(L), self.keyboard(L), True))
            return
        with self.widget_lock:
            widgets = self._widgets()
            old = widgets.get(f"{chat}|{style}")
            widgets[f"{chat}|{style}"] = [message["message_id"], int(time.time())]
            self._save_widgets(widgets)
        if old:  # one live widget of each look per chat: the client keeps every live one running
            with contextlib.suppress(Exception):
                self._call("deleteMessage", {"chat_id": chat, "message_id": old[0]})

    def _widgets(self) -> dict[str, list[int]]:
        """Live status widgets: "chat|look" -> [message id, sent at]; kept in the DB across restarts."""
        try:
            saved = json.loads(self.users.stats.meta("widgets") or "{}") if self.users else {}
        except ValueError:
            return {}
        return {(k if "|" in k else f"{k}|widget"): v for k, v in saved.items()}  # before 2.3.2: by chat

    def _save_widgets(self, widgets: dict) -> None:
        if self.users:
            self.users.stats.meta("widgets", json.dumps(widgets))

    def update_widgets(self, data: str, quick: str | None = None, force: bool = False) -> None:
        """Push new data to every live widget: the status data, or the quick settings data.

        Telegram counts every edit against the chat's flood limit (too many and it holds back the
        alerts too), so a widget gets new data only when something it shows changed, or once a
        minute for the clock-like fields; force (after a button press) skips that check."""
        with self.widget_lock:
            widgets = self._widgets()
        gone = []
        for key, (message_id, sent) in widgets.items():
            chat, _, look = key.partition("|")
            payload = data if look != "quick" else (
                widget_quick.with_look(quick, self.users.status_style(chat)) if quick and self.users else quick)
            if payload is None:
                continue
            if time.time() - sent > WIDGET_LIVE_FOR:
                gone.append((key, message_id))
                continue
            if self._blocked(key, self.widget_paused):
                continue
            core = widget_core(payload)
            last = self.widget_last.get(key)
            if not force and last and last[0] == core and time.time() - last[1] < WIDGET_IDLE_EVERY:
                continue
            try:
                self._call("editMessageWidgetData", {"chat_id": chat, "message_id": message_id, "chat_data": payload})
                self.widget_last[key] = (core, time.time())
            except urllib.error.HTTPError as exc:
                text = exc.read().decode(errors="replace")
                if exc.code == 429:
                    wait = retry_after(text) or 60
                    self.widget_paused[key] = time.time() + wait
                    log.warning("Widget %s updates paused for %d s: flood control", key, wait)
                    continue
                if "not modified" not in text:
                    log.info("Status widget in %s no longer updated: %s %s", chat, exc.code, text[:120])
                    gone.append((key, message_id))
            except Exception as exc:  # network: try again next time
                log.debug("Widget update failed: %s", exc)
        if gone:
            with self.widget_lock:
                widgets = self._widgets()
                for key, message_id in gone:  # unless a newer widget was sent meanwhile
                    if widgets.get(key, [None])[0] == message_id:
                        widgets.pop(key)
                self._save_widgets(widgets)

    def _widget_loop(self, handlers: BotHandlers) -> None:
        while True:
            time.sleep(WIDGET_UPDATE_EVERY)
            with contextlib.suppress(Exception):
                self._flush_deferred()
            try:
                info = handlers.widget() if handlers.widget else None
                if info and self._widgets():
                    self.update_widgets(info[1], handlers.quick() if handlers.quick else None)
            except Exception:
                log.exception("Widget update loop failed")

    def listen_commands(self, handlers: BotHandlers) -> None:
        self.panel_button = handlers.controller.panel_button
        if self.enabled:
            threading.Thread(target=self._poll_commands, args=(handlers,), daemon=True).start()
            if handlers.widget:
                threading.Thread(target=self._widget_loop, args=(handlers,), daemon=True).start()

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
                params = {"timeout": 50, "allowed_updates": '["message", "callback_query", "my_chat_member", '
                                                            '"widget_query"]'}
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

    def _handle_stats(self, chat: str, action: str, stats: "Stats", L: Lang, settings: "Settings") -> str:
        tariff = settings.tariff
        if action.startswith("sessions"):
            _, period, choice = (action.split(":") + ["", ""])[:3]
            if choice.isdigit() and int(choice) in SESSION_CHOICES:
                settings.set("session_w", choice)
            if period == "ask":  # a period of its own: typed as dates
                self.awaiting[(chat, chat)] = ("sessions", time.time())
                self.reply(chat, L.t("ses_ask"), FORCE_REPLY)
                return ""
            self.send_sessions(chat, stats, settings, period or "today")
            return L.t("sent")
        if action.startswith("chart"):
            hours = int(action.removeprefix("chart"))
            if stats.chart_png(hours, L) is None:
                return L.t("not_enough")
            e_in, e_out = stats.energy_kwh(time.time() - hours * 3600)
            self.send_photo(lambda L: stats.chart_png(hours, L), lambda L: "📈 " + chart_title(L, hours) + "\n" + L.t(
                "chart_energy", e_in=L.energy(e_in), cost=L.money(e_in * tariff), e_out=L.energy(e_out)), chat)
            return L.t("chart_sent")
        temps = self.temps() if self.temps else None
        rich = {"outages": (lambda L: stats.outages_html(L, temps=temps), stats.outages_text),
                "energy": (lambda L: stats.energy_html(L, tariff), stats.energy_text),
                "months": (lambda L: stats.months_html(L, tariff), stats.months_text)}
        if action in rich:
            self.reply_rich(chat, *rich[action])
        return L.t("sent")

    def send_sessions(self, chat: str, stats: "Stats", settings: "Settings", period: str) -> None:
        w, tariff = as_int(settings.get("session_w")) or 80, settings.tariff

        def show(render):
            def text(L: Lang) -> str:
                start, end, label = session_period(period, L) or session_period("today", L)
                return render(L, tariff, w, start, end, label)
            return text
        self.reply_rich(chat, show(stats.sessions_html), show(stats.sessions_text),
                        lambda L: sessions_menu(L, period, w))

    def _edit(self, msg: dict, view: tuple[str, str | None], rich: str | None = None) -> None:
        """Show a menu in the message of its button; rich: as a card. A text message cannot surely
        become a rich one (and back) by an edit: when the edit is refused, a new message replaces it."""
        chat, mid = msg["chat"]["id"], msg.get("message_id")
        text = {"text": view[0], "entities": bold_first_line(view[0])}
        content = {"rich_message": json.dumps({"html": rich})} if rich else text
        markup = {"reply_markup": view[1]} if view[1] else {}
        try:
            self._call("editMessageText", {"chat_id": chat, "message_id": mid, **content, **markup})
            return
        except urllib.error.HTTPError as exc:
            if "not modified" in exc.read().decode(errors="replace"):  # the same view again
                return
        except Exception:
            return
        with contextlib.suppress(Exception):
            self._call("deleteMessage", {"chat_id": chat, "message_id": mid})
        try:
            self._call("sendRichMessage" if rich else "sendMessage", {"chat_id": chat, **content, **markup})
        except Exception:
            if rich:  # the card refused: the text menu
                with contextlib.suppress(Exception):
                    self._call("sendMessage", {"chat_id": chat, **text, **markup})

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
        if wq := upd.get("widget_query"):
            self._handle_widget_query(wq, h)
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
        if not action and self.awaiting.get((chat, chat), ("",))[0] == "sessions" and h.stats:
            _, asked = self.awaiting.pop((chat, chat))
            period = parse_range(text) if time.time() - asked < 600 else None
            if period:
                self.send_sessions(chat, h.stats, h.controller.settings, period)
            elif time.time() - asked < 600:
                self.awaiting[(chat, chat)] = ("sessions", time.time())
                self.reply(chat, L.t("ses_bad"), FORCE_REPLY)
            return
        if not action and (chat, user) in self.awaiting and self.awaiting[(chat, user)][0] in TEXT_SETTINGS:
            kind, asked = self.awaiting.pop((chat, user))
            if time.time() - asked < 600 and users.is_admin(user):
                view = h.controller.text_setting(kind, text, Ctx(L, True, chat, user))
                if view:
                    self.reply(chat, *view, title=True)
                else:
                    self.awaiting[(chat, user)] = (kind, time.time())
                    self.reply(chat, L.t(TEXT_SETTINGS[kind][1]), FORCE_REPLY)
            return
        if not action and (chat, user) in self.awaiting and h.sched:
            kind, asked = self.awaiting.pop((chat, user))
            if time.time() - asked < 600 and users.is_admin(user):
                (text_out, markup), again = h.sched.text_input(kind, text, L)
                if again:  # the answer is the next question ("no such street", "house number?")
                    self.awaiting[(chat, user)] = (again, time.time())
                    markup = FORCE_REPLY
                self.reply(chat, text_out, markup, title=text_out.startswith("📅"))
            return
        if not action and (chat, user) in users.awaiting_id:
            if time.time() - users.awaiting_id.pop((chat, user)) < 600 and users.is_admin(user):
                self._add_by_id(chat, text.strip(), L)
            return
        if action == "light":
            if self.panel_button() is None:  # also refreshes the keyboard without the button
                self.reply(chat, L.t("no_light"))
            else:
                ctx = Ctx(L, users.is_admin(user), chat, user)
                (text, markup), card = h.controller.render("light", ctx), h.controller.rich("light", ctx)
                if card:  # the panel as a card, the text menu if Telegram refuses it
                    self.reply_rich(chat, lambda L: card, lambda L: text, lambda L: markup)
                else:
                    self.reply(chat, text, markup, title=True)
        elif action == "settings":
            self.reply(chat, *h.controller.render("root", Ctx(L, users.is_admin(user), chat, user)), title=True)
        elif action == "stats":
            if h.stats:
                self.reply(chat, L.t("stats_title"), stats_menu(L), title=True)
        elif action == "status":
            style = users.status_style(chat)
            if h.widget and h.widget() and style != "text":
                self.send_widget(chat, h.widget, h.status, style)
            else:
                self.reply(chat, h.status(L), title=True)
        if action and msg.get("message_id") and not self._blocked(chat, self.cannot_delete):  # the press
            try:  # groups: only when the bot is an admin allowed to delete messages
                self._call("deleteMessage", {"chat_id": chat, "message_id": msg["message_id"]})
            except urllib.error.HTTPError as exc:
                log.info("Cannot delete button presses in %s: %s", chat, exc.read().decode(errors="replace")[:120])
                self.cannot_delete[chat] = time.time() + 3600  # it may be made an admin later
            except Exception:
                pass

    def _ask(self, chat: str, user: str, kind: str, h: BotHandlers) -> None:
        """Ask an admin for typed input (street, house) with a force-reply message."""
        self.awaiting[(chat, user)] = (kind, time.time())
        if kind in TEXT_SETTINGS:
            self.reply(chat, lambda L: L.t(TEXT_SETTINGS[kind][0]), FORCE_REPLY)
        else:
            self.reply(chat, lambda L: h.sched.prompt(kind, L), FORCE_REPLY)

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
                self.reply(admin, lambda L: L.t("rq_admin", who=who), lambda L: json.dumps({"inline_keyboard": paint([[
                    {"text": L.t("rq_allow"), "callback_data": f"u:ok:{chat}"},
                    {"text": L.t("rq_deny"), "callback_data": f"u:no:{chat}"}]])}))
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
            if kind in ("a", "u", "g") and not ctx.admin:
                toast = ctx.L.t("only_admins")
                log.info("Denied %s:%s from user %s", kind, rest, user)
            elif kind == "stats" and h.stats:
                toast = self._handle_stats(chat, rest, h.stats, ctx.L, h.controller.settings)
                if rest.count(":") == 2 and not rest.startswith("sessions:ask"):  # sent again: drop the old one
                    with contextlib.suppress(Exception):
                        self._call("deleteMessage", {"chat_id": chat, "message_id": msg.get("message_id")})
            elif kind == "m":
                self._edit(msg, h.controller.render(rest, ctx), h.controller.rich(rest, ctx))
            elif kind == "l":
                users.upsert(chat, lang=rest if rest in LANGS else "")
                ctx.L = users.lang(chat)
                self._edit(msg, h.controller.render("lang", ctx))
                self.reply(chat, ctx.L.t("lang_title", v=LANG_NAMES[ctx.L.code]))  # refreshes the keyboard
            elif kind == "a" and rest.endswith(":edit") and rest.split(":")[0] in TEXT_SETTINGS:  # ✏️
                self._ask(chat, user, rest.split(":")[0], h)
            elif kind == "a":
                key, _, value = rest.partition(":")
                toast, menu = h.controller.act(key, value, ctx)
                if menu != "alert":  # a button under an alert keeps the alert as it is
                    self._edit(msg, h.controller.render(menu, ctx), h.controller.rich(menu, ctx))
            elif kind == "u":
                toast = self._user_action(rest, ctx, msg, h)
            elif kind == "g" and h.sched:
                toast, view, ask = h.sched.callback(rest, ctx.L)
                if view:
                    self._edit(msg, view)
                if ask:
                    self._ask(chat, user, ask, h)
            elif kind == "v" and h.widget:  # the look of 📊 Status, per chat
                users.set_status_style(chat, rest)
                self._edit(msg, h.controller.render("style", ctx))
                toast = ctx.L.t("saved")
        with contextlib.suppress(Exception):
            self._call("answerCallbackQuery", {"callback_query_id": cb["id"], "text": toast})

    def _handle_widget_query(self, wq: dict, h: BotHandlers) -> None:
        """A button inside the status widget: light or DC 12V (admins); the answer is shown on
        the button for a moment, the new state follows as widget data."""
        users = self.users
        chat = str((wq.get("chat") or {}).get("id") or (wq.get("message") or {}).get("chat", {}).get("id"))
        user = str((wq.get("from") or {}).get("id"))
        action = wq.get("data") or ""
        result = ""
        if users.role(chat) in ACTIVE_ROLES:
            ctx = Ctx(users.lang(chat), users.is_admin(user), chat, user)
            if action.startswith("look:") or action == "status":  # anyone: a status (in a new look)
                if action != "status":
                    users.set_status_style(chat, action[5:])
                    result = ctx.L.t("saved")
                style = users.status_style(chat)
                if style != "text" and h.widget and h.widget():
                    self.send_widget(chat, h.widget, h.status, style)
                elif style == "text":
                    self.reply(chat, h.status(ctx.L), title=True)
            elif not ctx.admin:
                result = ctx.L.t("only_admins")
            elif action == "qs" and self._blocked(f"{chat}|quick", self.widgets_blocked):  # no new widgets for now
                self.reply(chat, *h.controller.render("root", ctx), title=True)
            elif action == "qs" and h.quick and h.widget and (info := h.widget()):  # open ⚙️ Quick settings
                name = info[0]
                self.send_widget(chat, lambda: (name, widget_quick.with_look(h.quick(), users.status_style(chat))),
                                 h.status, "quick")
            elif action:
                result = h.controller.quick_action(action, ctx)
        log.info("Widget button %s by %s in %s: %s", action, user, chat, result)
        try:
            self._call("answerWidgetQuery", {"widget_query_id": wq.get("id"), "data": result})
        except urllib.error.HTTPError as exc:
            log.warning("answerWidgetQuery failed: %s %s (update %s)", exc.code,
                        exc.read().decode(errors="replace")[:200], json.dumps(wq)[:300])
        except Exception as exc:
            log.warning("answerWidgetQuery failed: %s", exc)
        if h.widget and (info := h.widget()):
            self.update_widgets(info[1], h.quick() if h.quick else None, force=True)  # the new state now

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


def bold_first_line(text: str) -> str:
    """Telegram entities that make the first line bold; offsets count UTF-16 code units, and
    entities need no escaping, unlike parse_mode (names may contain < or &)."""
    first = text.split("\n", 1)[0].rstrip("\u2800")  # not the blank padding of menu titles
    return json.dumps([{"type": "bold", "offset": 0, "length": len(first.encode("utf-16-le")) // 2}])
