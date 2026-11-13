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
    cards: list[str] = []

    class TG:
        default = lang("en")

        def send(self, render, markup=None, thread=None, rich=None, end=None):
            sent.append((int(clock.t - 1000), render(lang("en"))))
            if rich:  # the cards render too
                html = rich(lang("en"))
                assert html.startswith("<h3>")
                cards.append(html)

    class Power:
        def power_saver(self): ...
        def restore(self): ...

    w = Watchdog(TG(), Power(), 10, settings, "Station", stats, full_confirm_s=180, idle_confirm_s=180)

    def feed(seconds, ac_in, levels, load=0, step=4):
        for _ in range(int(seconds / step)):
            w.evaluate(ac_in > 0, ac_in, levels, None, load)
            clock.t += step

    feed.sent = sent
    feed.cards = cards
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


def test_full_station_with_load_waits_30_seconds(run):
    run(20, 120, FULL, load=120)
    run(12, 0, FULL, load=120)  # a few seconds without input while the grid is up: nothing
    run(20, 120, FULL, load=120)
    assert run.sent == []
    run(40, 0, FULL, load=120)
    assert [at for at, _ in run.sent] == [84]


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


def test_full_idle_station_waits_20_minutes_even_in_a_planned_outage(run):
    run.w.idle_confirm_s = 1200  # the default; a planned outage does not shorten it
    run(20, 16, FULL)
    run(860, 0, FULL)  # pauses of up to ~14 min between the pulses with the grid up
    assert run.sent == []
    run(400, 0, FULL)
    assert len(run.sent) == 1 and "lost AC input at " in run.sent[0][1]


def test_stations_with_a_plug_status_never_wait(run):
    run.w.plug_known = True
    run(20, 16, FULL)
    run(20, 0, FULL)
    assert len(run.sent) == 1


def test_threshold_and_back_cards_show_the_load(run, stats):
    run(20, 120, FULL, load=60)
    run(20, 0, FULL, load=60)  # outage confirmed
    start = time.time()
    for i in range(10):
        stats._exec("INSERT OR REPLACE INTO samples VALUES (?, 0, ?, 90, 0)", (int(start) + i, 40 + i * 10))
    run(20, 0, {"main": 45}, load=70)
    threshold = next(c for c in run.cards if "Threshold" in c)
    assert "<td>⚡ Load</td><td>70 W now, avg" in threshold
    run(20, 120, {"main": 45}, load=70)
    back = run.cards[-1]
    assert "Grid power is back" in back and "<td>⚡ Load</td><td>avg" in back and "⬆️ Used" in back


def test_full_at_99_percent_under_a_small_load_rides_out_long_gaps(run):
    # 13.11: one battery at 99 %, 10-30 W, the input dropped for up to ~100 s with the grid up
    almost = {"main": 99, "expansion": 100}
    run(20, 20, almost, load=20)
    for gap, load in [(40, 20), (60, 12), (100, 25), (36, 18)]:
        run(gap, 0, almost, load=load)
        run(8, 20, almost, load=20)
    assert run.sent == []


def test_small_load_outage_on_full_station_after_three_minutes(run):
    run(20, 25, FULL, load=25)
    run(240, 0, FULL, load=25)
    assert [at for at, _ in run.sent] == [200]
