"""Outage schedule: both sources, windows, next change, alerts and the Schedule menu."""

import json
from datetime import datetime, timedelta

import pytest

from ankerups import schedule as sch
from ankerups.i18n import lang
from ankerups.schedmenu import ScheduleMenu

TODAY = datetime(2026, 11, 8)
DATE, NEXT = TODAY.strftime("%Y-%m-%d"), (TODAY + timedelta(days=1)).strftime("%Y-%m-%d")

YASNO_SCHEDULE = {"12.1": {
    "today": {"slots": [{"start": 0, "end": 330, "type": "Definite"}, {"start": 330, "end": 540, "type": "NotPlanned"},
                        {"start": 540, "end": 1050, "type": "Definite"}, {"start": 1050, "end": 1140, "type": "NotPlanned"},
                        {"start": 1140, "end": 1440, "type": "Definite"}],
              "date": f"{DATE}T00:00:00+02:00", "status": "ScheduleApplies"},
    "tomorrow": {"slots": [], "date": f"{NEXT}T00:00:00+02:00", "status": "WaitingForSchedule"}}}
TOMORROW_OUT = {"slots": [{"start": 600, "end": 840, "type": "Definite"}], "date": f"{NEXT}T00:00:00+02:00",
                "status": "ScheduleApplies"}

OBLAST = {"fact": {"data": {str(int(TODAY.timestamp())): {
    "GPV3.1": {**{str(h): "yes" for h in range(1, 25)}, "1": "no", "2": "no", "6": "first", "13": "second",
               "20": "maybe"},
    "GPV1.1": {str(h): "yes" for h in range(1, 25)}}}}}


class FakeYasno:
    """The YASNO and outage-data-ua URLs the bot uses, answered from memory."""

    def __init__(self):
        self.schedule = json.loads(json.dumps(YASNO_SCHEDULE))
        self.calls: list[str] = []

    def __call__(self, url: str):
        self.calls.append(url)
        if url == sch.OBLAST_URL:
            return OBLAST
        if url == sch.KYIV_URL:  # outage-data-ua keeps the plan when YASNO shows emergencies
            return {"fact": {"data": {str(int(TODAY.timestamp())): {
                "GPV12.1": {**{str(h): "no" for h in range(1, 25)}, "8": "yes"}}}}}
        if "planned-outages" in url:
            return self.schedule
        if "/streets?" in url:
            return [{"id": 900, "value": "вул. Хрещатик"}] if "%D0%A5%D1%80%D0%B5" in url else \
                [{"id": 1, "value": "вул. А"}, {"id": 2, "value": "вул. Б"}] if "query=a" in url.lower() else []
        if "/houses?" in url:
            return [{"id": 9000, "value": "20/24"}, {"id": 9001, "value": "22/к1"}]
        if "/group?" in url:
            return {"group": 12, "subgroup": 1}
        raise AssertionError(url)


@pytest.fixture
def fake():
    return FakeYasno()


@pytest.fixture
def schedule(settings, stats, fake):
    for key in ("sched_source", "sched_group", "sched_label"):
        settings.defaults[key] = ""
    for key in ("alert_sched_soon", "alert_sched_tomorrow", "alert_sched_change"):
        settings.defaults[key] = "1"
    return sch.Schedule(settings, stats, fetch=fake)


def at(hour: int, minute: int = 0, day: int = 0) -> float:
    return (TODAY + timedelta(days=day, hours=hour, minutes=minute)).timestamp()


def test_yasno_day_slots_and_windows():
    today, tomorrow = sch.yasno_days(YASNO_SCHEDULE, "12.1")
    assert (today.date, today.status, tomorrow.status, tomorrow.slots) == (DATE, "ok", "waiting", "")
    assert sch.windows_text(today.slots) == "00:00–05:30, 09:00–17:30, 19:00–24:00"
    assert sch.yasno_days(YASNO_SCHEDULE, "1.1") == []


def test_oblast_half_hours():
    (day,) = sch.oblast_days(OBLAST, "3.1")
    assert day.slots[:4] == "0000" and day.slots[10:12] == "01" and day.slots[24:26] == "10"
    assert day.slots[38:40] == "??"
    assert sch.windows_text(day.slots) == "00:00–02:00, 05:00–05:30, 12:30–13:00"
    assert sch.oblast_groups(OBLAST) == ["1.1", "3.1"]


def test_next_change_and_warning(schedule):
    schedule.set("kyiv", "12.1", "вул. Хрещатик, 22/к1")
    schedule.refresh(at(8))
    assert schedule.next_change(at(8)) == (at(9), "0")
    assert schedule.next_change(at(18)) == (at(19), "0")
    assert schedule.upcoming_outage(at(8, 20)) is None  # 40 min before
    assert schedule.upcoming_outage(at(8, 35)) == (at(9), at(17, 30))
    assert schedule.upcoming_outage(at(10)) is None  # already off


def test_events_tomorrow_published_and_changed(schedule, fake):
    schedule.set("kyiv", "12.1", "x")
    assert schedule.refresh(at(8)) == []  # first download: nothing to compare with
    fake.schedule["12.1"]["tomorrow"] = TOMORROW_OUT
    events = schedule.refresh(at(8, 5))  # within FETCH_EVERY: not downloaded yet
    assert events == []
    events = schedule.refresh(at(8) + sch.FETCH_EVERY)
    assert [(k, d.date) for k, d in events] == [("tomorrow", NEXT)]
    slots = fake.schedule["12.1"]["today"]["slots"]
    slots[0]["end"] = slots[1]["start"] = 360  # the morning outage is 30 min longer
    events = schedule.refresh(at(12))
    assert [(k, d.date) for k, d in events] == [("changed", DATE)]
    assert schedule.refresh(at(13)) == []


def test_download_failure_is_retried_soon(schedule, fake):
    schedule.set("kyiv", "12.1", "x")
    fake.schedule = None  # .get on None fails like a broken answer
    assert schedule.refresh(at(8)) == []
    fake.schedule = json.loads(json.dumps(YASNO_SCHEDULE))
    assert schedule.refresh(at(8) + 60) == []
    assert not schedule.days
    schedule.refresh(at(8) + 130)
    assert DATE in schedule.days


def test_menu_finds_group_by_address(schedule, fake):
    menu, L = ScheduleMenu(schedule, fetch=fake), lang("uk")
    assert menu.callback("kyiv", L) == ("", None, "street")
    (text, markup), ask = menu.text_input("street", "Хрещатик", L)
    assert ask == "house:900" and "вул. Хрещатик" in text
    # "22/1" on the DTEK list is "22/к1"
    (text, markup), ask = menu.text_input(ask, "22/1", L)
    assert ask is None and text.startswith("📅 Графік відключень")
    assert (schedule.source, schedule.group, schedule.label) == ("kyiv", "12.1", "вул. Хрещатик, 22/к1")


def test_menu_lists_several_streets_and_houses(schedule, fake):
    menu, L = ScheduleMenu(schedule, fetch=fake), lang("en")
    (text, markup), ask = menu.text_input("street", "a", L)
    assert ask is None and [r[0]["callback_data"] for r in json.loads(markup)["inline_keyboard"]] == ["g:st:1", "g:st:2"]
    assert menu.callback("st:2", L)[2] == "house:2"
    (text, markup), ask = menu.text_input("house:2", "1", L)
    assert text == "Choose the house"
    toast, view, _ = menu.callback("h:2:9000", L)
    assert toast == "✅ Group 12.1: вул. Б, 20/24"
    assert menu.text_input("street", "zzz", L) == (("No such street in Kyiv, try part of the name in Ukrainian", None),
                                                   "street")


def test_menu_oblast_group_and_flags(schedule, fake):
    menu, L = ScheduleMenu(schedule, fetch=fake), lang("en")
    _, (text, markup), _ = menu.callback("obl", L)
    assert [b["text"] for b in json.loads(markup)["inline_keyboard"][0]] == ["1.1", "3.1"]
    toast, (text, markup), _ = menu.callback("og:3.1", L)
    assert toast == "✅ Group 3.1: Kyiv region" and "Kyiv region, group 3.1" in text
    toast, (text, _), _ = menu.callback("f:alert_sched_soon", L)
    assert "⏰ 30 min before: off" in text
    menu.callback("off", L)
    assert not schedule.configured


def test_run_schedule_warns_once(schedule, fake, monkeypatch):
    schedule.set("kyiv", "12.1", "x")
    monkeypatch.setattr(sch.time, "time", lambda: at(8, 40))

    class Stop:
        n = 0

        def is_set(self):
            return self.n >= 2

        def wait(self, s):
            self.n += 1

    warned = []
    sch.run_schedule(schedule, lambda k, d: None, lambda s, e: warned.append((s, e)), Stop())
    assert warned == [(at(9), at(17, 30))]


def test_emergency_outages_announced_and_restored(schedule, fake):
    schedule.set("kyiv", "12.1", "x")
    schedule.refresh(at(8))
    saved = fake.schedule["12.1"]["today"]
    for key in ("today", "tomorrow"):
        fake.schedule["12.1"][key] = {"slots": [], "date": (DATE if key == "today" else NEXT) + "T00:00:00+02:00",
                                      "status": "EmergencyShutdowns"}
    events = schedule.refresh(at(9))
    assert [(k, d.date) for k, d in events] == [("emergency", DATE)]  # one alert for both days
    today = schedule.day(TODAY)
    assert today.status == "emergency" and sch.windows_text(today.slots).startswith("00:00–05:30")  # plan kept
    assert schedule.next_change(at(9)) == (at(17, 30), "1")
    assert schedule.refresh(at(10)) == []
    fake.schedule["12.1"]["today"] = saved
    events = schedule.refresh(at(11))
    assert [(k, d.date) for k, d in events] == [("restored", DATE)]


def test_emergency_after_restart_takes_the_plan_from_outage_data(schedule, fake):
    for key in ("today", "tomorrow"):
        fake.schedule["12.1"][key] = {"slots": [], "date": (DATE if key == "today" else NEXT) + "T00:00:00+02:00",
                                      "status": "EmergencyShutdowns"}
    schedule.set("kyiv", "12.1", "x")
    schedule.refresh(at(8))
    assert sch.windows_text(schedule.day(TODAY).slots) == "00:00–07:00, 08:00–24:00"
    assert schedule.day(TODAY + timedelta(days=1)).slots == ""  # nothing known for tomorrow


def test_next_change_follows_the_actual_grid(schedule):
    schedule.set("kyiv", "12.1", "x")  # off 00:00-05:30, 09:00-17:30, 19:00-24:00
    schedule.refresh(at(8))
    assert schedule.next_change(at(17, 15), grid=True) == (at(19), "0")  # back before 17:30
    assert schedule.next_change(at(17, 15), grid=False) == (at(17, 30), "1")
    assert schedule.next_change(at(18), grid=False) is None  # an outage the plan did not have, no "on" ahead today
    assert schedule.next_change(at(8), grid=True) == (at(9), "0")


def test_planned_off_now_or_soon(schedule):
    assert schedule.planned_off(at(8)) is None  # not set up
    schedule.set("kyiv", "12.1", "x")  # off 00:00-05:30, 09:00-17:30, 19:00-24:00
    schedule.refresh(at(8))
    assert schedule.planned_off(at(10)) is True
    assert schedule.planned_off(at(8, 45)) is True  # 15 min before
    assert schedule.planned_off(at(7)) is False
