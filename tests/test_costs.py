"""Rich stats tables with cost, the tariff, high load peaks and the charge hint before an outage."""

import json
import time
import urllib.error
from types import SimpleNamespace

import pytest

from ankerups.i18n import lang
from ankerups.watchdog import LoadPeaks
from conftest import FakeBot, buttons, texts, toast

EN = lang("en")


def fill(stats, minutes: int, ac_in: float, out: float, soc_from: int, soc_to: int, grid=True, end=None):
    end = end or time.time()
    for i in range(minutes):
        soc = round(soc_from + (soc_to - soc_from) * i / max(1, minutes - 1))
        stats._exec("INSERT OR REPLACE INTO samples VALUES (?, ?, ?, ?, ?)",
                    (int(end - (minutes - i) * 60), ac_in, out, soc, int(grid)))


def test_energy_table_with_cost(stats):
    fill(stats, 60, 1000, 500, 50, 60)  # 1 kWh in, 0.5 kWh out
    html = stats.energy_html(lang("uk"), 4.32)
    assert "<table" in html and "<th><b>Вартість</b></th>" in html and "<td>1.00</td>" in html and "4.32 грн" in html


def test_outages_and_months_tables(stats):
    now = time.time()
    stats.outage_started(now - 90 * 3600, 100)
    stats.outage_ended(now - 31 * 3600, 60)
    fill(stats, 5, 100, 50, 60, 60)
    html = stats.outages_html(EN)
    assert "59h 00m" in html and "<table bordered striped compact>" in html  # one row per day
    assert "<td>60%</td>" in html
    months = stats.months_html(EN, 4.32)
    assert months.count("<tr>") >= 2 and "UAH" in months and ">Grid<" in months and "%</td>" in months


def test_stats_buttons_send_rich_tables_or_text(bot: FakeBot, stats):
    bot.handlers.stats = stats
    calls = bot.press(bot.ADMIN, "stats:energy")
    rich = dict(calls)["sendRichMessage"]
    assert "<table" in json.loads(rich["rich_message"])["html"]
    real = bot.tg._call

    def no_rich(method, params, timeout=10):
        if method == "sendRichMessage":
            raise urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
        return real(method, params, timeout)
    bot.tg._call = no_rich
    calls = bot.press(bot.ADMIN, "stats:energy")
    assert texts(calls)[0].startswith("⚡ Energy through the station")


def test_tariff_and_peak_settings(bot: FakeBot, settings):
    calls = bot.press(bot.ADMIN, "m:notif")
    assert "💰 Tariff: 4.32 UAH per kWh" in texts(calls)[0]
    assert buttons(calls[0][1])[-2] == ["500W", "•1000W", "1500W", "2000W", "💰✏️"]
    assert texts(bot.press(bot.ADMIN, "a:tariff:edit")) == ["Send the grid price per kWh, e.g. 4.32"]
    assert texts(bot.message(bot.ADMIN, "abc")) == ["Could not read it: send a number, e.g. 4.32"]
    calls = bot.message(bot.ADMIN, "7,5")
    assert "💰 Tariff: 7.50 UAH per kWh" in texts(calls)[0] and settings.tariff == 7.5
    assert toast(bot.press(bot.ADMIN, "a:peak:end")) == "Saved" and settings.get("peak_mode") == "end"
    assert toast(bot.press(bot.ADMIN, "a:peakw:1500")) == "Saved" and settings.get("peak_w") == "1500"


@pytest.fixture
def peaks(settings):
    sent = []
    tg = SimpleNamespace(send=lambda render, markup=None: sent.append(render(EN)))
    settings.set("peak_mode", "both")
    settings.set("peak_w", "1000")
    p = LoadPeaks(tg, settings)
    p.sent = sent
    return p


def run_load(peaks, start, seconds, watts, step=5):
    for t in range(0, seconds, step):
        peaks.update(watts(t) if callable(watts) else watts, start + t)
    return start + seconds


def test_peak_start_and_end_with_energy(peaks):
    t0 = 1_800_000_000
    t = run_load(peaks, t0, 60, 100)
    t = run_load(peaks, t, 6 * 60, lambda s: 2000 if s % 60 < 50 else 300)  # a kettle element cycling
    run_load(peaks, t, 60, 100)
    assert len(peaks.sent) == 2
    assert peaks.sent[0].startswith("🔥 High load 2000 W since")
    assert "up to 2000 W: " in peaks.sent[1] and "Wh ≈ " in peaks.sent[1] and "UAH" in peaks.sent[1]


def test_peak_summary_only_and_short_spikes_ignored(peaks, settings):
    settings.set("peak_mode", "end")
    t0 = 1_800_000_000
    t = run_load(peaks, t0, 5, 3000)  # a 5 s spike: no peak
    t = run_load(peaks, t, 60, 100)
    assert peaks.sent == []
    t = run_load(peaks, t, 120, 1500)
    run_load(peaks, t, 60, 100)
    assert len(peaks.sent) == 1 and peaks.sent[0].startswith("🔥 High load ")


def test_charge_hint_before_outage(stats):
    pytest.importorskip("anker_solix_api")
    from ankerups.app import charge_hint
    fill(stats, 120, 600, 100, 40, 70)  # 500 W net for 2 h = 1000 Wh for 30 % -> ~33 Wh per %
    per_pct = stats.wh_per_percent()
    assert 30 < per_pct < 37
    snap = {"levels": {"main": 80, "expansion": 60}, "ac_in": 300, "out": 100}
    # 2 h before the outage: 30 % x ~33 Wh = 1 kWh needs ~500 W net + 100 W load
    text, markup = charge_hint(stats, snap, 400, [200, 400, 600, 800, 1000], time.time() + 7200)
    assert "By then about 82%" in text(EN) and "input limit now 400 W" in text(EN)
    assert json.loads(markup(EN))["inline_keyboard"][0][0]["callback_data"] == "a:limit:600@alert"
    text, markup = charge_hint(stats, snap, 400, [200, 400, 600, 800, 1000], time.time() + 1800)
    assert "Even the highest input limit" in text(EN) and markup is None
    text, markup = charge_hint(stats, {"levels": {"main": 100}, "ac_in": 0, "out": 50}, 400, [400], time.time())
    assert text(EN) == "\n🔋 The battery is full" and markup is None


def test_limit_button_under_an_alert_keeps_the_alert(bot: FakeBot):
    sent = []
    bot.controller.device = SimpleNamespace(set_charge_limit=lambda **kw: sent.append(kw))
    calls = bot.press(bot.ADMIN, "a:limit:600@alert")
    assert toast(calls) == "600W ✓" and sent == [{"max_watts": 600}]
    assert "editMessageText" not in dict(calls)
