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

from .common import log
from .i18n import Lang, Render, lang
from .menus import Controller, Ctx
from .stats import Stats, chart_title
from .translations import LANG_NAMES, LANGS
from .users import ACCESS_MODES, ACTIVE_ROLES, Users, chat_name


@dataclass
class BotHandlers:
    """Callbacks the Telegram thread uses to read state and act on the station."""

    status: Render
    controller: "Controller"
    stats: "Stats | None" = None


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
            self.queue.put(("text", lambda L: f"{stamp} {render(L)}", None, None, None, False))

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
            kind, payload, extra, target, code, title = self.queue.get()
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
                        args = (payload(L) if callable(payload) else payload, markup or self.keyboard(L), title)
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
                    params = {"chat_id": chat, "text": args[0], "reply_markup": args[1]}
                    if args[2]:
                        params["entities"] = bold_first_line(args[0])
                    self._call("sendMessage", params)
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
            self.reply(chat, texts[action], title=True)
        return L.t("sent")

    def _edit(self, msg: dict, view: tuple[str, str | None]) -> None:
        params = {"chat_id": msg["chat"]["id"], "message_id": msg.get("message_id"), "text": view[0],
                  "entities": bold_first_line(view[0])}
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
                self.reply(chat, *h.controller.render("light", Ctx(L, users.is_admin(user), chat, user)), title=True)
        elif action == "settings":
            self.reply(chat, *h.controller.render("root", Ctx(L, users.is_admin(user), chat, user)), title=True)
        elif action == "stats":
            if h.stats:
                self.reply(chat, L.t("stats_title"), stats_menu(L), title=True)
        elif action == "status":
            self.reply(chat, h.status(L), title=True)

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


def bold_first_line(text: str) -> str:
    """Telegram entities that make the first line bold; offsets count UTF-16 code units, and
    entities need no escaping, unlike parse_mode (names may contain < or &)."""
    first = text.split("\n", 1)[0]
    return json.dumps([{"type": "bold", "offset": 0, "length": len(first.encode("utf-16-le")) // 2}])
