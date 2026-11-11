"""Settings, light, language and user menus of the bot, and station commands.
"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from .common import as_int, log
from .i18n import Lang, lang
from .plug import decode_cycle, parse_cycle
from .stats import Settings, esc
from .translations import LANG_NAMES, LANGS
from .users import ACCESS_MODES, ACTIVE_ROLES, STATUS_STYLES, Users
from .widget_quick import quick_data


@dataclass
class Ctx:
    """Who pressed a button: the chat language and whether the person is an admin."""

    L: Lang
    admin: bool
    chat: str
    user: str


THRESHOLD_CHOICES = [90, 75, 50, 25, 10]


CRITICAL_CHOICES = [0, 5, 10, 15, 20]


SUMMARY_CHOICES = ["", "07:00", "10:00", "19:00", "23:00"]


# auto light: DC 12V on during an outage in these local hours, off again at the end of them;
# "" = off. The ready choices end by 01:00 (later nobody needs the light); ✏️ sets any time.
AUTOLIGHT_CHOICES = ["", "17-23", "18-24", "18-01"]
AUTOLIGHT_MAX_HOURS = 16


def parse_window(text: str) -> tuple[int, int] | None:
    """"18:30-00:30", "18-1", "18.30 – 0.30" -> (start, end) minutes of the day; None if invalid."""
    parts = re.split(r"\s*[-–—]\s*", text.strip())
    if len(parts) != 2:
        return None
    minutes = []
    for part in parts:
        m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?", part)
        if not m or int(m[1]) > 24 or int(m[2] or 0) > 59 or (int(m[1]) == 24 and m[2] not in (None, "00")):
            return None
        minutes.append(int(m[1]) * 60 + int(m[2] or 0))
    start, end = minutes[0] % 1440, minutes[1]
    length = (end - start) % 1440 or (1440 if end != start else 0)
    if not 0 < length <= AUTOLIGHT_MAX_HOURS * 60:
        return None
    return start, end


def in_window(window: str, minute: float) -> bool:
    """Whether a minute of the day is within the window (it may cross midnight)."""
    span = parse_window(window) if window else None
    if not span:
        return False
    start, end = span
    return start <= minute < end if start < end else minute >= start or minute < end % 1440


def window_text(L: Lang, window: str) -> str:
    span = parse_window(window) if window else None
    if not span:
        return L.t("off")
    return "–".join(f"{m // 60:02d}:{m % 60:02d}" for m in span)


MENU_WIDTH = 34  # characters of the padded title line: about as wide as the English menus, not wider than a phone

# inline button colours (Bot API "style"): dangerous actions red, the main way forward blue;
# a chosen option ("•", "✓") green
DANGER = ("a:ac:0!", "u:rm:", "u:no:", "g:off")
PRIMARY = ("u:ok:", "u:add")


def paint(rows: list[list[dict]]) -> list[list[dict]]:
    for row in rows:
        for button in row:
            data = button.get("callback_data", "")
            if data.startswith(DANGER):
                button["style"] = "danger"
            elif data.startswith(PRIMARY):
                button["style"] = "primary"
            elif button["text"].startswith(("•", "✓")):
                button["style"] = "success"
    return rows


# settings typed in by an admin after ✏️: key -> (prompt, "could not read it", menu to show)
TEXT_SETTINGS = {"autolight": ("al_auto_ask", "al_auto_bad", "light"),
                 "tariff": ("tf_ask", "tf_bad", "notif"),
                 "plugcycle": ("pl_ask", "pl_bad", "plug")}
PLUG_TIMERS = [30, 60, 120, 240]  # minutes: the plug switches over when the countdown ends
PLUG_CYCLES = ["10/10", "15/15", "20/10", "30/30"]
PEAK_MODES = ["", "both", "end"]  # high load alerts: off, start and end, only the end
PEAK_CHOICES = [500, 1000, 1500, 2000]

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
        self.sched = None  # ScheduleMenu, set by the app when Telegram is on
        self.widgets = False  # 📊 Status can be a widget: show the widget / text switch
        self.pending: dict[str, tuple[str, float]] = {}  # field -> (value, time) until telemetry catches up
        self.plug = None  # plug.Plug (Tuya smart plug), set by the app when .env has it

    def _state(self, key: str) -> str | None:
        field = STATION_CONTROLS[key][0]
        if field in self.pending and time.time() - self.pending[field][1] < 20:
            return self.pending[field][0]
        value = as_int(self.data().get(field))
        return None if value is None else str(value)

    def _label(self, key: str, L: Lang) -> str:
        state = self._state(key)
        return STATION_CONTROLS[key][2](L, state) if state in STATION_CONTROLS[key][1] else "?"

    def panel_button(self) -> str | None:
        """Label key of the light / DC 12V panel button: "btn_light" with a light (also before the
        first telemetry), "btn_12v" with only the DC 12V output, None when the station has neither."""
        data = self.data()
        if "light_mode" in data:
            self.light_seen = True
        if self.light_seen:
            return "btn_light"
        if "dc_output_power_switch" in data:
            return "btn_12v"
        return None if ("main_battery_soc" in data or "battery_soc" in data) else "btn_light"

    def widget_controls(self) -> tuple[int | None, int | None]:
        """Light mode and DC 12V switch for the status widget's buttons; None when not reported."""
        light = as_int(self._state("light")) if self._supported("light") else None
        dc = as_int(self._state("dc")) if self._supported("dc") else None
        return light, dc

    def quick_data(self) -> str:
        """Chat data of the ⚙️ Quick settings widget: the controls the station reports."""
        def state(key: str) -> int | None:
            return as_int(self._state(key)) if self._supported(key) else None
        light, dc = self.widget_controls()
        limits = [int(v) for v in STATION_CONTROLS["limit"][1]]
        return quick_data({"lm": light, "dc": dc, "fast": state("fast"), "disp": state("disp"), "lim": state("limit")},
                          limits, self.settings.get("autolight") if dc is not None else None)

    def quick_action(self, action: str, ctx: Ctx) -> str:
        """A control of the Quick settings widget; returns a short result for the widget."""
        key, _, value = action.partition(":")
        if key in ("light", "dc"):
            return self.toggle(key, ctx)
        if key == "plug" and self.plug:  # the Tuya plug on <-> off
            return self._plug_act("off" if (self.plug.status() or {}).get("switch_1") else "on", ctx)[0]
        if key == "fast" and self._supported("fast"):
            return self.act("fast", "0" if self._state("fast") == "1" else "1", ctx)[0]
        if key == "disp" and self._supported("disp"):
            return self.act("disp", str((as_int(self._state("disp")) or 0) + 1 & 3), ctx)[0]
        if key == "limit" and value in STATION_CONTROLS["limit"][1]:
            return self.act("limit", value, ctx)[0]
        if key == "auto" and value == "off":
            self.settings.set("autolight", "")
            return ctx.L.t("saved")
        if key == "auto" and self.text_setting("autolight", value, ctx):
            return ctx.L.t("saved")
        return ctx.L.t("unknown")

    def toggle(self, key: str, ctx: Ctx) -> str:
        """A widget button: the light off <-> high (any other mode counts as on), DC 12V on <-> off.
        Returns the text the button shows for a moment."""
        light, dc = self.widget_controls()
        if key == "light" and light is not None:
            value = "0" if light else "3"
        elif key == "dc" and dc is not None:
            value = "0" if dc else "1"
        else:
            return ctx.L.t("unknown")
        return self.act(key, value, ctx)[0]

    def text_setting(self, kind: str, text: str, ctx: Ctx) -> tuple[str, str] | None:
        """A typed setting (TEXT_SETTINGS); returns the menu to show, None if it is not valid."""
        if kind == "autolight":
            span = parse_window(text)
            if not span:
                return None
            value = "-".join(f"{m // 60:02d}:{m % 60:02d}" for m in span)
        elif kind == "plugcycle":
            cycle = parse_cycle(text) if self.plug else None
            if not cycle:
                return None
            work, rest, start, end = cycle
            ok = self.plug.set_cycle(True, work, rest, start, end)
            log.info("Plug cycle %s by %s: %s", text.strip(), ctx.user, "ok" if ok else "failed")
            return self.render("plug", ctx)
        elif kind == "tariff":
            try:
                price = float(text.strip().replace(",", ".").split()[0])
            except (ValueError, IndexError):
                return None
            if not 0 < price < 1000:
                return None
            value = f"{price:g}"
        else:
            return None
        self.settings.set(kind, value)
        log.info("Setting %s = %s by %s", kind, value, ctx.user)
        return self.render(TEXT_SETTINGS[kind][2], ctx)

    def _supported(self, key: str) -> bool:
        return STATION_CONTROLS[key][0] in self.data()

    def _row(self, key: str, L: Lang, prefix: str = "", back: str = "") -> list[dict]:
        current = self._state(key)
        _, values, label, _ = STATION_CONTROLS[key]
        label = BUTTON_LABELS.get(key, label)
        # rows of 4-5 buttons have ~6 characters each on a phone: no space after the marker
        mark = "•" if len(values) >= 4 else "• "
        return [{"text": (mark if v == current else "") + prefix + label(L, v), "callback_data": f"a:{key}:{v}{back}"}
                for v in values]

    @staticmethod
    def _markup(L: Lang, rows: list[list[dict]], back: str | None = "m:root") -> str:
        if back:
            rows = [*rows, [{"text": L.t("back"), "callback_data": back}]]
        return json.dumps({"inline_keyboard": paint([r for r in rows if r])})

    def render(self, menu: str, ctx: Ctx) -> tuple[str, str]:
        """A menu: text and inline keyboard. Telegram desktop makes the keyboard as wide as the
        message, and short texts (most languages but English) cut the labels of 4-5 button rows,
        so the title line is padded with blank Braille characters to a common width."""
        text, markup = self._render(menu, ctx)
        first, _, rest = text.partition("\n")
        first += "\u2800" * max(0, MENU_WIDTH - len(first))
        return (first + ("\n" + rest if rest else "")), markup

    def _render(self, menu: str, ctx: Ctx) -> tuple[str, str]:
        L, st = ctx.L, self.settings
        if menu == "light":
            # the light and the DC 12V socket (e.g. a USB lamp in a 12V adapter) in one panel
            lines, rows = [], []
            if self._supported("light"):
                row = self._row("light", L)
                lines.append(L.t("light_title", v=self._label("light", L)))
                rows += [row[:3], row[3:]]
            if self._supported("dc"):
                lines.append(f"🔌 12V: {self._label('dc', L)}")
                rows.append(self._row("dc", L, "🔌 12V ", back="@light"))
                auto = self.settings.get("autolight")
                lines.append(L.t("al_auto_line", v=window_text(L, auto)))
                rows.append([{"text": ("•" if c == auto else "") + (c.replace("-", "–") if c else "💡✗"),
                              "callback_data": f"a:autolight:{c or 'off'}@light"} for c in AUTOLIGHT_CHOICES]
                            + [{"text": ("•" if auto not in AUTOLIGHT_CHOICES else "") + "✏️",
                                "callback_data": "a:autolight:edit"}])
            if self.plug:
                rows.append([{"text": "🔌 " + L.t("plug"), "callback_data": "m:plug"}])
                lines = lines or ["🔌 " + L.t("plug")]
            return "\n".join(lines or [L.t("no_light")]), self._markup(L, rows, back=None)
        if menu == "plug" and self.plug:
            return self._render_plug(L)
        if menu == "notif":
            th = st.thresholds
            on_off = {True: L.t("on"), False: L.t("off")}
            text = "\n".join([
                L.t("n_title"),
                # the icons match the button rows below
                "🔋 " + L.t("n_charge", v=", ".join(f"{t}%" for t in th) or L.t("off")),
                "🪫 " + L.t("n_critical", v=f"{st.critical}%" if st.critical else L.t("off")),
                "📅 " + L.t("n_summary", v=L.t("n_summary_v", t=st.summary) if st.summary else L.t("off")),
                *(L.t("n_flag", label=L.t(label), v=on_off[st.flag(k)]) for k, label in ALERT_FLAGS.items()),
                L.t("n_peak", v=L.t(f"peak_{st.get('peak_mode') or 'off'}"), w=st.get("peak_w") or "1000"),
                L.t("n_tariff", v=L.money(st.tariff))])
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
                [{"text": ("•" if m == st.get("peak_mode") else "") + L.t(f"peak_btn_{m or 'off'}"),
                  "callback_data": f"a:peak:{m or 'off'}"} for m in PEAK_MODES],
                [{"text": ("•" if str(w) == (st.get("peak_w") or "1000") else "") + f"{w}W",
                  "callback_data": f"a:peakw:{w}"} for w in PEAK_CHOICES]
                + [{"text": "💰✏️", "callback_data": "a:tariff:edit"}],
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
        if menu == "style" and self.users:
            current = self.users.status_style(ctx.chat)
            return L.t("st_title", v=L.t(f"st_{current}")), self._markup(L, [
                [{"text": ("• " if s == current else "") + L.t(f"st_{s}"), "callback_data": f"v:{s}"}
                 for s in STATUS_STYLES[:2]],
                [{"text": ("• " if s == current else "") + L.t(f"st_{s}"), "callback_data": f"v:{s}"}
                 for s in STATUS_STYLES[2:]]])
        if menu == "sched" and self.sched:
            return self.sched.render(L, ctx.admin)
        if menu == "users" and ctx.admin and self.users:
            return self._render_users(L)
        if menu.startswith("user:") and ctx.admin and self.users:
            return self._render_user(L, menu.removeprefix("user:"))
        rows = [[{"text": L.t("s_notif"), "callback_data": "m:notif"},
                 {"text": L.t("s_outputs"), "callback_data": "m:outputs"}],
                [{"text": L.t("s_charging"), "callback_data": "m:charging"}],
                [{"text": L.t("s_lang"), "callback_data": "m:lang"}]]
        if self.sched:
            rows[1].append({"text": L.t("s_sched"), "callback_data": "m:sched"})
        if ctx.admin and self.users:
            rows[2].append({"text": L.t("s_users"), "callback_data": "m:users"})
        if self.widgets and self.users:
            mode = L.t(f"st_{self.users.status_style(ctx.chat)}")
            rows.append([{"text": L.t("st_mode", v=mode), "callback_data": "m:style"}])
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

    def rich(self, menu: str, ctx: Ctx) -> str | None:
        """A menu shown as a rich message (a card with a table), None for the plain text ones."""
        if menu == "plug" and self.plug:
            return self._plug_card(ctx.L)
        if menu == "light":
            return self._light_card(ctx.L)
        return None

    def _light_card(self, L: Lang) -> str | None:
        """The light, DC 12V, auto light and the plug in one table."""
        rows = []
        if self._supported("light"):
            rows.append(L.t("light_title", v=self._label("light", L)).split(": ", 1))
        if self._supported("dc"):
            rows.append(("🔌 12V", self._label("dc", L)))
            rows.append(L.t("al_auto_line", v=window_text(L, self.settings.get("autolight"))).split(": ", 1))
        if self.plug and (st := self.plug.status(max_age=60)):
            v = L.t("on") if st.get("switch_1") else L.t("off")
            if st.get("online") and st.get("cur_power") is not None and st.get("switch_1"):
                v += f" · {st['cur_power'] / 10:g} W"
            rows.append(("🔌 " + L.t("plug"), v))
        if not rows:
            return None
        return ("<table bordered>" + "".join(f"<tr><td>{esc(a)}</td><td>{esc(b)}</td></tr>" for a, b in rows)
                + "</table>")

    def _plug_card(self, L: Lang) -> str | None:
        st = self.plug.status()
        if st is None:
            return None
        on = bool(st.get("switch_1"))
        rows = []
        if st.get("cur_power") is not None:
            rows.append(("⚡ " + L.t("r_load"), L.t("pl_power", w=f"{st['cur_power'] / 10:g}",
                                                    v=f"{(st.get('cur_voltage') or 0) / 10:g}").removeprefix("⚡ ")))
        if left := int(st.get("countdown_1") or 0):
            rows.append(("⏲ " + L.t("pl_r_timer"), L.t("pl_timer_off" if on else "pl_timer_on",
                                                      dur=L.dur(left)).removeprefix("⏲ ")))
        cycle = decode_cycle(st.get("cycle_time", ""))
        if cycle and cycle["on"]:
            window = (L.t("pl_allday") if cycle["start"] == cycle["end"] or (cycle["end"] - cycle["start"]) % 1440 >= 1439
                      else f"{cycle['start'] // 60:02d}:{cycle['start'] % 60:02d}–{cycle['end'] // 60:02d}:{cycle['end'] % 60:02d}")
            label, _, value = L.t("pl_cycle", c=f"{cycle['work']}/{cycle['rest']}", w=window).partition(": ")
        else:
            label, _, value = L.t("pl_cycle_off").partition(": ")
        rows.append((label, value))
        html = (f"<h3>{esc(L.t('pl_title', v=L.t('on') if on else L.t('off')))}</h3><table bordered>"
                + "".join(f"<tr><td>{esc(a)}</td><td>{esc(b)}</td></tr>" for a, b in rows) + "</table>")
        return html + (f"<footer>{esc(L.t('pl_cloud_off'))}</footer>" if not st.get("online") else "")

    def _render_plug(self, L: Lang) -> tuple[str, str]:
        st = self.plug.status()
        if st is None:
            return L.t("pl_offline"), self._markup(L, [[{"text": "🔄", "callback_data": "m:plug"}]], back="m:light")
        on = bool(st.get("switch_1"))
        lines = [L.t("pl_title", v=L.t("on") if on else L.t("off"))]
        if not st.get("online"):
            lines.append(L.t("pl_cloud_off"))
        if st.get("cur_power") is not None:
            lines.append(L.t("pl_power", w=f"{st['cur_power'] / 10:g}", v=f"{(st.get('cur_voltage') or 0) / 10:g}"))
        left = int(st.get("countdown_1") or 0)
        if left:
            lines.append(L.t("pl_timer_off" if on else "pl_timer_on", dur=L.dur(left)))
        cycle = decode_cycle(st.get("cycle_time", ""))
        current = f"{cycle['work']}/{cycle['rest']}" if cycle and cycle["on"] else ""
        if current:
            window = (L.t("pl_allday") if cycle["start"] == cycle["end"] or (cycle["end"] - cycle["start"]) % 1440 >= 1439
                      else f"{cycle['start'] // 60:02d}:{cycle['start'] % 60:02d}–{cycle['end'] // 60:02d}:{cycle['end'] % 60:02d}")
            lines.append(L.t("pl_cycle", c=current, w=window))
        else:
            lines.append(L.t("pl_cycle_off"))
        rows = [
            [{"text": ("• " if on else "") + "✅ " + L.t("on"), "callback_data": "a:plug:on"},
             {"text": ("• " if not on else "") + "⛔ " + L.t("off"), "callback_data": "a:plug:off"},
             {"text": "🔄", "callback_data": "m:plug"}],
            [{"text": "⏲" + L.dur(m * 60), "callback_data": f"a:plug:t{m}"} for m in PLUG_TIMERS]
            + ([{"text": "⏲✗", "callback_data": "a:plug:t0"}] if left else []),
            [{"text": ("•" if not current else "") + "🔁✗", "callback_data": "a:plug:c0"}]
            + [{"text": ("•" if c == current else "") + c, "callback_data": f"a:plug:c{c}"} for c in PLUG_CYCLES]
            + [{"text": ("•" if current and current not in PLUG_CYCLES else "") + "✏️",
                "callback_data": "a:plugcycle:edit"}],
        ]
        return "\n".join(lines), self._markup(L, rows, back="m:light")

    def _plug_act(self, value: str, ctx: Ctx) -> tuple[str, str]:
        L, plug = ctx.L, self.plug
        if value in ("on", "off"):
            ok = plug.send(switch_1=value == "on")
        elif value.startswith("t") and value[1:].isdigit() and (int(value[1:]) in PLUG_TIMERS or value == "t0"):
            ok = plug.send(countdown_1=int(value[1:]) * 60)
        elif value == "c0":
            ok = plug.set_cycle(False)
        elif value[1:] in PLUG_CYCLES:
            work, rest = (int(x) for x in value[1:].split("/"))
            ok = plug.set_cycle(True, work, rest)
        else:
            return L.t("unknown"), "plug"
        log.info("Plug %s by %s: %s", value, ctx.user, "ok" if ok else "failed")
        if not ok and not (plug.status() or {}).get("online"):
            return L.t("pl_cloud_off_short"), "plug"
        return (L.t("saved") if ok else L.t("cmd_failed")), "plug"

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
        if key == "peak" and (value == "off" or value in PEAK_MODES):
            st.set("peak_mode", "" if value == "off" else value)
            return L.t("saved"), "notif"
        if key == "peakw" and value.isdigit() and int(value) in PEAK_CHOICES:
            st.set("peak_w", value)
            return L.t("saved"), "notif"
        if key == "flag" and value in ALERT_FLAGS:
            st.set(value, "0" if st.flag(value) else "1")
            return L.t("saved"), "notif"
        if key == "autolight" and value.partition("@")[0] in ("off", *AUTOLIGHT_CHOICES):
            choice = value.partition("@")[0]
            st.set("autolight", "" if choice == "off" else choice)
            return L.t("saved"), "light"
        if key == "plug" and self.plug:
            return self._plug_act(value, ctx)
        if key not in STATION_CONTROLS:
            return L.t("unknown"), "root"
        value, _, back = value.partition("@")  # "@light": pressed in the Light / 12V panel
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
        menu = back or {"light": "light", "ac": "outputs", "acmode": "outputs", "dc": "outputs",
                "dcmode": "outputs"}.get(key, "charging")
        return (f"{label(L, value)} ✓" if ok else L.t("cmd_failed")), menu
