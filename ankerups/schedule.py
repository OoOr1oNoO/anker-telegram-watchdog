"""Planned outage schedules in Ukraine ("графік відключень").

Sources:
- Kyiv city: the public YASNO API (DTEK Kyiv Electric Networks), searched by street and house.
- Kyiv region: the outage-data-ua project on GitHub (it scrapes DTEK), by group number.

A day is kept as 48 half-hour slots: "1" power on, "0" off, "?" maybe; "" until published.
"""

import json
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .common import log

YASNO = "https://app.yasno.ua/api/blackout-service/public/shutdowns"
KYIV_REGION, KYIV_DSO = 25, 902
OBLAST_URL = "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv-region.json"
SOURCES = ("kyiv", "oblast")
FETCH_EVERY = 600  # seconds between schedule downloads
WARN_BEFORE = 30 * 60  # outage warning this long before a planned outage
SLOT = 1800
EMERGENCY = "E"  # a day with emergency outages instead of a schedule, in the "seen" state

# outage-data-ua hour cell -> the two half hours
OBLAST_CELLS = {"yes": "11", "no": "00", "maybe": "??", "first": "01", "second": "10",
                "mfirst": "?1", "msecond": "1?"}


def fetch_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "anker-telegram-watchdog",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _addresses(path: str, **params) -> object:
    query = urllib.parse.urlencode({"regionId": KYIV_REGION, "dsoId": KYIV_DSO, **params})
    return fetch_json(f"{YASNO}/addresses/v2/{path}?{query}")


def search_streets(query: str, fetch=fetch_json) -> list[tuple[int, str]]:
    query = urllib.parse.urlencode({"regionId": KYIV_REGION, "dsoId": KYIV_DSO, "query": query})
    return [(s["id"], s["value"]) for s in fetch(f"{YASNO}/addresses/v2/streets?{query}")][:12]


def search_houses(street: int, query: str, fetch=fetch_json) -> list[tuple[int, str]]:
    """Houses of a street starting with the number typed; "22/1" also finds "22/к1"."""
    number = query.strip().split("/")[0].split()[0] if query.strip() else ""
    q = urllib.parse.urlencode({"regionId": KYIV_REGION, "dsoId": KYIV_DSO, "streetId": street,
                                "query": number})
    houses = [(h["id"], h["value"]) for h in fetch(f"{YASNO}/addresses/v2/houses?{q}")]
    wanted = query.strip().lower().replace(" ", "")
    exact = [h for h in houses if _house_key(h[1]) == _house_key(wanted)]
    return exact or houses[:24]


def _house_key(value: str) -> str:
    """22/к1, 22/1, 22 к1 and 22-К1 are the same house."""
    return "".join(ch for ch in value.lower() if ch.isalnum() and ch not in "кk")


def find_group(street: int, house: int, fetch=fetch_json) -> str:
    q = urllib.parse.urlencode({"regionId": KYIV_REGION, "dsoId": KYIV_DSO, "streetId": street,
                                "houseId": house})
    data = fetch(f"{YASNO}/addresses/v2/group?{q}")
    return f"{data['group']}.{data['subgroup']}"


@dataclass
class Day:
    date: str  # YYYY-MM-DD, local
    slots: str  # 48 x "1" / "0" / "?", "" when not published
    status: str  # ok, waiting, emergency


def yasno_days(data: dict, group: str) -> list[Day]:
    days = []
    entry = data.get(group) or {}
    for key in ("today", "tomorrow"):
        day = entry.get(key)
        if not day:
            continue
        status = {"ScheduleApplies": "ok", "WaitingForSchedule": "waiting",
                  "EmergencyShutdowns": "emergency"}.get(day.get("status"), "waiting")
        slots = ""
        if status == "ok" and day.get("slots"):
            cells = ["1"] * 48
            for s in day["slots"]:
                state = {"Definite": "0", "Possible": "?"}.get(s.get("type"), "1")
                for i in range(48):
                    if s["start"] <= i * 30 + 15 < s["end"]:
                        cells[i] = state
            slots = "".join(cells)
        days.append(Day(day["date"][:10], slots, status if slots or status != "ok" else "waiting"))
    return days


def oblast_days(data: dict, group: str) -> list[Day]:
    days = []
    for midnight, groups in sorted((data.get("fact") or {}).get("data", {}).items()):
        hours = groups.get(f"GPV{group}")
        if not hours:
            continue
        slots = "".join(OBLAST_CELLS.get(hours.get(str(h), "yes"), "11") for h in range(1, 25))
        days.append(Day(datetime.fromtimestamp(int(midnight)).strftime("%Y-%m-%d"), slots, "ok"))
    return days


def oblast_groups(data: dict) -> list[str]:
    groups: set[str] = set()
    for day in (data.get("fact") or {}).get("data", {}).values():
        groups |= {g.removeprefix("GPV") for g in day}
    return sorted(groups, key=lambda g: [int(x) for x in g.split(".") if x.isdigit()])


def windows(slots: str, state: str = "0") -> list[tuple[int, int]]:
    """(start, end) minutes of the runs of one state: [(0, 330), (540, 1050)]."""
    runs, start = [], None
    for i, s in enumerate(slots + "x"):
        if s == state and start is None:
            start = i
        elif s != state and start is not None:
            runs.append((start * 30, i * 30))
            start = None
    return runs


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def windows_text(slots: str) -> str:
    return ", ".join(f"{hhmm(a)}–{hhmm(b)}" for a, b in windows(slots))


class Schedule:
    """Downloads the schedule of the chosen group and raises alerts about it.

    Settings (in the bot DB): sched_source (kyiv / oblast), sched_group ("12.1"), sched_label
    (the address or the group to show). Runs in its own thread; the alert callbacks get the
    event and the day.
    """

    def __init__(self, settings, stats=None, fetch: Callable = fetch_json):
        self.settings = settings
        self.stats = stats
        self.fetch = fetch
        self.days: dict[str, Day] = {}
        self.updated = 0.0
        self.fetched_for = ""
        self.lock = threading.Lock()

    @property
    def group(self) -> str:
        return self.settings.get("sched_group")

    @property
    def source(self) -> str:
        return self.settings.get("sched_source")

    @property
    def label(self) -> str:
        return self.settings.get("sched_label") or self.group

    @property
    def configured(self) -> bool:
        return bool(self.group and self.source in SOURCES)

    def set(self, source: str, group: str, label: str) -> None:
        self.settings.set("sched_source", source)
        self.settings.set("sched_group", group)
        self.settings.set("sched_label", label)
        with self.lock:
            self.days, self.updated = {}, 0.0
        if self.stats:
            self.stats.meta("sched_seen", "")  # new group: no "changed" alerts for the old one

    def download(self) -> list[Day]:
        if self.source == "kyiv":
            return yasno_days(self.fetch(f"{YASNO}/regions/{KYIV_REGION}/dsos/{KYIV_DSO}/planned-outages"),
                              self.group)
        return oblast_days(self.fetch(OBLAST_URL), self.group)

    def refresh(self, now: float | None = None) -> list[tuple[str, Day]]:
        """Download when due; returns events: ("tomorrow", day) when the next day's schedule is
        published, ("changed", day) when a published day changes, ("emergency", day) when
        emergency outages replace the schedule and ("restored", day) when it applies again."""
        now = time.time() if now is None else now
        key = f"{self.source}:{self.group}"
        if not self.configured or (now - self.updated < FETCH_EVERY and self.fetched_for == key):
            return []
        try:
            days = self.download()
        except Exception as exc:
            log.warning("Schedule download failed: %s", exc)
            self.updated, self.fetched_for = now - FETCH_EVERY + 120, key  # try again in 2 minutes
            return []
        with self.lock:
            self.days = {d.date: d for d in days}
            self.updated, self.fetched_for = now, key
        return self._events(days, now)

    def _events(self, days: list[Day], now: float) -> list[tuple[str, Day]]:
        seen = json.loads(self.stats.meta("sched_seen") or "{}") if self.stats else {}
        first = not seen
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        events: list[tuple[str, Day]] = []
        for day in days:
            mark = EMERGENCY if day.status == "emergency" else day.slots  # "" = not published
            if not mark or day.date < today:
                continue
            before = seen.get(day.date)
            if not first and before != mark:
                if mark == EMERGENCY:
                    kind = "emergency"
                elif before == EMERGENCY:
                    kind = "restored"
                else:
                    kind = "tomorrow" if before is None and day.date > today else "changed"
                if not (kind == "emergency" and any(k == kind for k, _ in events)):  # one per download
                    events.append((kind, day))
            seen[day.date] = mark
        seen = {d: s for d, s in seen.items() if d >= today}
        if self.stats:
            self.stats.meta("sched_seen", json.dumps(seen))
        return events

    def day(self, when: datetime) -> Day | None:
        with self.lock:
            return self.days.get(when.strftime("%Y-%m-%d"))

    def slots_from(self, now: float) -> tuple[str, int]:
        """Slots of today and tomorrow joined ("" for an unknown day: stops the scan) and the
        index of the current slot."""
        dt = datetime.fromtimestamp(now)
        today, tomorrow = self.day(dt), self.day(dt + timedelta(days=1))
        joined = (today.slots if today else "")
        if len(joined) == 48 and tomorrow and tomorrow.slots:
            joined += tomorrow.slots
        return joined, (dt.hour * 60 + dt.minute) // 30

    def next_change(self, now: float) -> tuple[float, str] | None:
        """(time, new state) of the next change of the planned state, within today and tomorrow."""
        slots, i = self.slots_from(now)
        if i >= len(slots):
            return None
        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        for j in range(i + 1, len(slots)):
            if slots[j] != slots[i]:
                return (midnight + timedelta(minutes=30 * j)).timestamp(), slots[j]
        return None

    def upcoming_outage(self, now: float) -> tuple[float, float] | None:
        """(start, end) of a planned outage starting within WARN_BEFORE, warned once."""
        slots, i = self.slots_from(now)
        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        for j in range(i + 1, len(slots)):
            start = (midnight + timedelta(minutes=30 * j)).timestamp()
            if start - now > WARN_BEFORE:
                break
            if slots[j] == "0" and slots[j - 1] != "0":
                end_j = next((k for k in range(j, len(slots)) if slots[k] != "0"), len(slots))
                return start, (midnight + timedelta(minutes=30 * end_j)).timestamp()
        return None


def run_schedule(schedule: Schedule, on_event: Callable[[str, Day], None],
                 on_soon: Callable[[float, float], None], stop: threading.Event | None = None) -> None:
    """Thread body: refresh the schedule and fire the alerts."""
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            for kind, day in schedule.refresh():
                on_event(kind, day)
            now = time.time()
            soon = schedule.upcoming_outage(now) if schedule.configured else None
            meta = schedule.stats.meta if schedule.stats else None
            if soon and meta and meta("sched_warned") != str(int(soon[0])):
                meta("sched_warned", str(int(soon[0])))
                on_soon(*soon)
        except Exception:
            log.exception("Schedule check failed")
        stop.wait(30)
