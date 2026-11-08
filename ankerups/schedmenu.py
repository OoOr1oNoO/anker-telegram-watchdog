"""The ⚙️ Settings → 📅 Schedule menu: choosing the address or group, schedule alert switches.

Callbacks are "g:<action>[:args]" (admins only). Typed input (street, house) is asked with a
force-reply message; the bot passes the answer to text_input().
"""

import json
from collections.abc import Callable
from datetime import datetime, timedelta

from .common import log
from .i18n import Lang
from .schedule import Day, Schedule, fetch_json, find_group, oblast_groups, search_houses, search_streets
from .schedule import OBLAST_URL, windows_text

SCHED_FLAGS = {"alert_sched_soon": "sc_f_soon", "alert_sched_tomorrow": "sc_f_tomorrow",
               "alert_sched_change": "sc_f_change"}

View = tuple[str, str | None]  # text, inline markup


def day_text(L: Lang, day: Day | None) -> str:
    if day is None or day.status == "waiting" or (day.status == "ok" and not day.slots):
        return L.t("sc_waiting")
    if day.status == "emergency":
        return L.t("sc_emergency")
    return windows_text(day.slots) or L.t("sc_no_off")


def markup(rows: list[list[dict]]) -> str:
    return json.dumps({"inline_keyboard": [r for r in rows if r]})


class ScheduleMenu:
    def __init__(self, schedule: Schedule, fetch: Callable = fetch_json):
        self.schedule = schedule
        self.fetch = fetch
        self.streets: dict[int, str] = {}  # names of streets found, for the labels
        self.labels: dict[tuple[int, int], str] = {}  # (street, house) -> "вул. Хрещатик, 22/к1"

    def render(self, L: Lang, admin: bool) -> View:
        sc, st = self.schedule, self.schedule.settings
        lines = [L.t("sc_title")]
        if sc.configured:
            sc.refresh()
            now = datetime.now()
            lines += [L.t("sc_where", label=sc.label, group=sc.group),
                      L.t("sc_today", v=day_text(L, sc.day(now))),
                      L.t("sc_tomorrow", v=day_text(L, sc.day(now + timedelta(days=1))))]
        else:
            lines.append(L.t("sc_none"))
        on_off = {True: L.t("on"), False: L.t("off")}
        lines += [f"{L.t(label)}: {on_off[st.flag(k)]}" for k, label in SCHED_FLAGS.items()]
        rows = []
        if admin:
            rows = [[{"text": L.t("sc_kyiv"), "callback_data": "g:kyiv"},
                     {"text": L.t("sc_oblast"), "callback_data": "g:obl"}],
                    [{"text": ("✓ " if st.flag(k) else "✗ ") + L.t(label).split(" ", 1)[0],
                      "callback_data": f"g:f:{k}"} for k, label in SCHED_FLAGS.items()]]
            if sc.configured:
                rows.append([{"text": L.t("sc_off"), "callback_data": "g:off"}])
        rows.append([{"text": L.t("back"), "callback_data": "m:root"}])
        return "\n".join(lines), markup(rows)

    def callback(self, rest: str, L: Lang) -> tuple[str, View | None, str | None]:
        """Returns (toast, view to show in the menu message or None, input to ask for or None)."""
        action, _, arg = rest.partition(":")
        st = self.schedule.settings
        if action == "f" and arg in SCHED_FLAGS:
            st.set(arg, "0" if st.flag(arg) else "1")
            return L.t("saved"), self.render(L, True), None
        if action == "off":
            self.schedule.set("", "", "")
            return L.t("saved"), self.render(L, True), None
        if action == "kyiv":
            return "", None, "street"
        if action == "obl":
            try:
                groups = oblast_groups(self.fetch(OBLAST_URL))
            except Exception as exc:
                log.warning("Oblast groups download failed: %s", exc)
                return L.t("sc_failed"), None, None
            rows = [[{"text": g, "callback_data": f"g:og:{g}"} for g in groups[i:i + 4]]
                    for i in range(0, len(groups), 4)]
            return "", (L.t("sc_pick_group"), markup([*rows, [{"text": L.t("back"),
                                                                 "callback_data": "m:sched"}]])), None
        if action == "og" and arg:
            self.schedule.set("oblast", arg, L.t("sc_region"))
            return L.t("sc_set", group=arg, label=L.t("sc_region")), self.render(L, True), None
        if action == "st" and arg:
            return "", None, f"house:{arg}"
        if action == "h" and arg:
            street, _, house = arg.partition(":")
            return self._choose(L, int(street), int(house))
        return L.t("unknown"), None, None

    def _choose(self, L: Lang, street: int, house: int, label: str | None = None) -> tuple[str, View, None]:
        try:
            group = find_group(street, house, self.fetch)
        except Exception as exc:
            log.warning("Group lookup failed: %s", exc)
            return L.t("sc_failed"), (L.t("sc_failed"), None), None
        label = label or self.labels.get((street, house), f"{street}/{house}")
        self.schedule.set("kyiv", group, label)
        log.info("Outage schedule: group %s (%s)", group, label)
        return L.t("sc_set", group=group, label=label), self.render(L, True), None

    def text_input(self, kind: str, text: str, L: Lang) -> tuple[View, str | None]:
        """Answer to a force-reply prompt: (message to send, the next input to ask for or None)."""
        text = text.strip()
        try:
            if kind == "street":
                found = search_streets(text, self.fetch) if text else []
                if not found:
                    return (L.t("sc_no_street"), None), "street"
                self.streets.update(found)
                if len(found) == 1:
                    return (L.t("sc_ask_house", street=found[0][1]), None), f"house:{found[0][0]}"
                return (L.t("sc_pick_street"), markup([[{"text": name, "callback_data": f"g:st:{sid}"}]
                                                       for sid, name in found])), None
            if kind.startswith("house:"):
                street = int(kind.split(":")[1])
                found = search_houses(street, text, self.fetch) if text else []
                if not found:
                    return (L.t("sc_no_house"), None), kind
                name = self.streets.get(street, "")
                for hid, number in found:
                    self.labels[(street, hid)] = f"{name}, {number}" if name else number
                if len(found) == 1:
                    toast, view, _ = self._choose(L, street, found[0][0])
                    return view, None
                rows = [[{"text": number, "callback_data": f"g:h:{street}:{hid}"}
                         for hid, number in found[i:i + 4]] for i in range(0, len(found), 4)]
                return (L.t("sc_pick_house"), markup(rows)), None
        except Exception as exc:
            log.warning("Address search failed: %s", exc)
            return (L.t("sc_failed"), None), None
        return (L.t("unknown"), None), None

    def prompt(self, kind: str, L: Lang) -> str:
        if kind.startswith("house:"):
            return L.t("sc_ask_house", street=self.streets.get(int(kind.split(":")[1]), ""))
        return L.t("sc_ask_street")
