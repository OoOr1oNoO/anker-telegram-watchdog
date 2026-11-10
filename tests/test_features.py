"""Schedule notes in the grid alerts, the auto light and charge thresholds across restarts."""

from datetime import datetime
from types import SimpleNamespace

import pytest

pytest.importorskip("anker_solix_api")  # ankerups.app needs it; installed in CI

from ankerups.app import autolight_step  # noqa: E402
from ankerups.i18n import lang  # noqa: E402
from ankerups.menus import in_window, parse_window  # noqa: E402
from ankerups.schedmenu import alert_note  # noqa: E402
from ankerups.watchdog import Watchdog  # noqa: E402
from test_schedule import at, fake, schedule  # noqa: E402, F401 - fixtures

EN = lang("en")


@pytest.fixture
def planned(schedule):  # noqa: F811
    schedule.set("kyiv", "12.1", "x")  # off 00:00-05:30, 09:00-17:30, 19:00-24:00
    schedule.refresh(at(8))
    return schedule


def test_notes_for_outage_alert(planned):
    assert alert_note(planned, EN, "lost", at(9, 2)) == "\n📅 Planned outage until 17:30"
    assert alert_note(planned, EN, "lost", at(8, 40)) == "\n📅 20m earlier than the schedule (09:00)"
    assert alert_note(planned, EN, "lost", at(6)) == "\n📅 Not in the schedule"
    assert alert_note(None, EN, "lost", at(6)) == ""


def test_notes_for_grid_back_alert(planned):
    assert alert_note(planned, EN, "back", at(16, 30)) == "\n📅 1h 00m earlier than the schedule (planned until 17:30)"
    assert alert_note(planned, EN, "back", at(17, 32)) == "\n📅 On schedule"
    assert alert_note(planned, lang("ru"), "back", at(17, 55)) == "\n📅 На 25 мин позже графика (по графику с 17:30)"
    assert alert_note(planned, EN, "back", at(5, 40)) == "\n📅 10m later than the schedule (planned from 05:30)"


def test_autolight_window():
    def h(hour, minute=0):
        return hour * 60 + minute
    assert in_window("18-01", h(23, 30)) and in_window("18-01", h(0, 59)) and not in_window("18-01", h(1))
    assert in_window("18-24", h(23, 59)) and not in_window("18-24", h(0)) and not in_window("17-23", h(12))
    assert in_window("18:30-00:30", h(0, 15)) and not in_window("18:30-00:30", h(18, 29))
    assert not in_window("", h(20)) and not in_window("0-24", h(12))  # too long: off


@pytest.mark.parametrize("text, span", [("18:30-00:30", (1110, 30)), ("18 - 1", (1080, 60)), ("17.45–23", (1065, 1380)),
                                        ("20-24", (1200, 1440)), ("0-24", None), ("18-18", None), ("25-1", None),
                                        ("18:61-1", None), ("evening", None), ("6-23", None)])
def test_parse_window(text, span):
    assert parse_window(text) == span


class Station:
    """Controller stand-in: the DC 12V switch and the commands sent to it."""

    def __init__(self, dc=0):
        self.dc, self.commands = dc, []

    def _supported(self, key):
        return key == "dc"

    def widget_controls(self):
        return None, self.dc

    def act(self, key, value, ctx):
        self.commands.append(value)
        self.dc = int(value)
        return "ok", "light"


def autolight(settings, station, on_battery, hour, meta, sent):
    tg = SimpleNamespace(default=EN, send=lambda render, *a, **k: sent.append(render(EN)))
    autolight_step(SimpleNamespace(on_battery=on_battery), station, settings, tg, lambda k, v=None: (
        meta.__setitem__(k, v) if v is not None else meta.get(k)), datetime(2026, 11, 8, hour))


def test_autolight_turns_12v_on_and_off(settings):
    settings.set("autolight", "18-01")
    station, meta, sent = Station(), {}, []
    autolight(settings, station, True, 14, meta, sent)  # outage in the day: nothing
    autolight(settings, station, True, 19, meta, sent)  # evening: on
    autolight(settings, station, True, 20, meta, sent)  # still: no repeats
    autolight(settings, station, False, 21, meta, sent)  # grid back: off
    autolight(settings, station, True, 23, meta, sent)  # the next outage: on again
    autolight(settings, station, True, 1, meta, sent)  # 01:00, still no power: off for the night
    autolight(settings, station, True, 2, meta, sent)
    assert station.commands == ["1", "0", "1", "0"]
    assert sent[:2] == ["💡 Auto light: 12V turned on for the outage", "💡 Auto light: 12V turned off"]


def test_autolight_leaves_12v_turned_on_by_hand(settings):
    settings.set("autolight", "17-23")
    station, meta, sent = Station(dc=1), {}, []
    autolight(settings, station, True, 18, meta, sent)
    autolight(settings, station, False, 19, meta, sent)
    assert station.commands == [] and station.dc == 1 and sent == []
    settings.set("autolight", "")
    autolight(settings, Station(), True, 12, meta, sent)
    assert sent == []


def test_thresholds_survive_a_restart_in_an_outage(stats, settings, monkeypatch):
    import time
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    sent = []
    tg = SimpleNamespace(default=EN, send=lambda render, *a, **k: sent.append(render(EN)))
    power = SimpleNamespace(power_saver=lambda: None, restore=lambda: None)

    def feed(w, levels, seconds=40):
        for _ in range(seconds // 4):
            w.evaluate(False, 0, levels, None, 80)
            clock[0] += 4

    w = Watchdog(tg, power, 10, settings, "Station", stats)
    feed(w, {"main": 100, "expansion": 80})
    feed(w, {"main": 100, "expansion": 74})  # 75 % reported
    # restart while the expansion battery sits at exactly 50 %: 50 % is still due
    w = Watchdog(tg, power, 10, settings, "Station", stats)
    feed(w, {"main": 100, "expansion": 50})
    assert [s.split(",")[0] for s in sent if s.startswith("🔋")] == ["🔋 Battery (expansion) 74% (threshold 75%)",
                                                                       "🔋 Battery (expansion) 50% (threshold 50%)"]


def test_actual_row_marks_partial_half_hours(stats, monkeypatch):
    import time as t
    midnight = 1794088800
    for minute, grid in ((0, True), (1, True), (30, True), (31, False), (60, False), (61, False)):
        monkeypatch.setattr(t, "time", lambda m=minute: midnight + m * 60 + 1)
        stats.add_reading(100 if grid else 0, 50, 80, grid)
    monkeypatch.setattr(t, "time", lambda: midnight + 62 * 60)
    stats.add_reading(0, 50, 80, False)  # flushes the last minute
    assert stats.grid_slots(midnight, midnight + 62 * 60) == "1p0"
