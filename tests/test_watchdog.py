"""Outage detection timing, charge thresholds and the full-charge alert."""

import time

import pytest

from ankerups.i18n import lang
from ankerups.watchdog import Watchdog


class Clock:
    def __init__(self):
        self.t = 1000.0


@pytest.fixture
def run(monkeypatch, stats, settings):
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", lambda: clock.t)
    sent: list[tuple[int, str]] = []

    class TG:
        default = lang("en")

        def send(self, render, markup=None, thread=None, rich=None, end=None):
            sent.append((int(clock.t - 1000), render(lang("en"))))
            if rich:  # the cards render too
                assert rich(lang("en")).startswith("<h3>")

    class Power:
        def power_saver(self): ...
        def restore(self): ...

    w = Watchdog(TG(), Power(), 10, settings, "Station", stats, full_confirm_s=180)
    w.planned_off = lambda ts: True  # the 3-minute wait; the schedule-aware one is tested below

    def feed(seconds, ac_in, levels, load=0, step=4):
        for _ in range(int(seconds / step)):
            w.evaluate(ac_in > 0, ac_in, levels, None, load)
            clock.t += step

    feed.sent = sent
    feed.w = w
    return feed


FULL = {"main": 100, "expansion": 100}


def test_pulsing_input_on_full_idle_station_is_not_an_outage(run):
    run(20, 16, FULL)
    for _ in range(10):
        run(60, 0, FULL)
        run(12, 16, FULL)
    assert run.sent == []


def test_outage_on_full_idle_station_after_three_minutes_with_start_time(run):
    run(20, 16, FULL)
    run(240, 0, FULL)
    assert len(run.sent) == 1
    at, text = run.sent[0]
    assert at == 200  # 20 s of grid, then 180 s of 0 W
    assert text.startswith("🔌 Station lost AC input at ")


def test_outage_with_load_after_ten_seconds(run):
    run(20, 120, FULL, load=120)
    run(20, 0, FULL, load=120)
    assert [at for at, _ in run.sent] == [32]


def test_outage_below_full_after_ten_seconds(run):
    run(20, 500, {"main": 95})
    run(20, 0, {"main": 95})
    assert run.sent and run.sent[0][1].startswith("🔌 Station lost AC input, ")


def test_only_lowest_crossed_threshold_is_reported(run):
    run(20, 500, {"main": 90, "expansion": 90})
    run(20, 0, {"main": 90, "expansion": 40})
    alerts = [text for _, text in run.sent if text.startswith("🔋")]
    assert alerts == ["🔋 Battery (expansion) 40% (threshold 50%), charge 65% (main 90%, expansion 40%)"]


def test_full_charge_alert_once_after_outage(run):
    run(20, 500, {"main": 60})
    run(20, 0, {"main": 60})
    run(8, 600, {"main": 60})
    run(8, 600, {"main": 100})
    run(8, 600, {"main": 99})
    run(8, 600, {"main": 100})
    assert [text for _, text in run.sent if "Fully charged" in text] == [
        "🔋 Fully charged in 0m after the outage: 100%"]


def test_no_full_charge_alert_after_short_outage_at_full(run):
    run(20, 120, FULL, load=120)
    run(20, 0, FULL, load=120)
    run(20, 120, FULL, load=120)
    assert not [text for _, text in run.sent if "Fully charged" in text]


def test_full_idle_station_waits_10_minutes_unless_an_outage_is_planned(run):
    run.w.planned_off = lambda ts: False  # the schedule says the power is on
    run(20, 16, FULL)
    run(400, 0, FULL)  # night pauses of up to ~6 min with the grid up
    assert run.sent == []
    run(260, 0, FULL)
    assert len(run.sent) == 1 and "lost AC input at " in run.sent[0][1]


def test_stations_with_a_plug_status_never_wait(run):
    run.w.plug_known, run.w.planned_off = True, (lambda ts: None)
    run(20, 16, FULL)
    run(20, 0, FULL)
    assert len(run.sent) == 1
