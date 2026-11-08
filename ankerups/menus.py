"""Settings, light, language and user menus of the bot, and station commands.
"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from .common import as_int, log
from .i18n import Lang, lang
from .stats import Settings
from .translations import LANG_NAMES, LANGS
from .users import ACCESS_MODES, ACTIVE_ROLES, Users


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
        return json.dumps({"inline_keyboard": [r for r in rows if r]})

    def render(self, menu: str, ctx: Ctx) -> tuple[str, str]:
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
            return "\n".join(lines or [L.t("no_light")]), self._markup(L, rows, back=None)
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
            mode = L.t("st_text" if self.users.text_status(ctx.chat) else "st_widget")
            rows.append([{"text": L.t("st_mode", v=mode), "callback_data": "v:toggle"}])
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
        if key == "autolight" and value.partition("@")[0] in ("off", *AUTOLIGHT_CHOICES):
            choice = value.partition("@")[0]
            st.set("autolight", "" if choice == "off" else choice)
            return L.t("saved"), "light"
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
