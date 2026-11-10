"""Weather and air-raid alerts: polling, alert periods in the DB, the stats columns."""

import time

from ankerups import extras
from ankerups.i18n import lang


class Feed:
    def __init__(self):
        self.here = self.near = False
        self.level = "yellow"
        self.neptun_up = True

    def __call__(self, url):
        if "air-quality" in url:
            return {"current": {"european_aqi": 21.4}}
        if url == extras.NEPTUN_URL:
            if not self.neptun_up:
                raise OSError("403")
            return {"oblasts": [{"key": "м. київ", "name": "м. Київ", "oblast": "м. Київ", "level": self.level}]
                    if self.here else [],
                    "raions": [{"name": "Броварський район", "oblast": "Київська область", "level": "yellow"}]
                    if self.near else []}
        if url == extras.ALERTS_URL:
            return {"states": {"м. Київ": {"alertnow": self.here}, "Київська область": {"alertnow": self.near}}}
        return {"current": {"temperature_2m": 4.6, "weather_code": 3},
                "daily": {"time": ["2026-11-09", "2026-11-10"], "temperature_2m_min": [1.2, 2.0],
                          "temperature_2m_max": [7.9, 6.1]}}


def test_weather_and_alert_levels(stats):
    feed = Feed()
    x = extras.Extras(stats, fetch=feed, env={})
    t = 1_800_000_000
    x.refresh(t)
    assert x.widget() == {"w": [5, 3, 21, "#ffec8e"]} and x.daily["2026-11-09"] == (1.2, 7.9)
    feed.near = True
    x.refresh(t + 60)
    assert x.widget()["al"] == ["n", t + 60]  # only nearby: grey, not counted
    feed.here = True
    x.refresh(t + 120)
    assert x.widget()["al"] == ["y", t + 120]  # drones
    feed.level = "red"
    x.refresh(t + 180)
    assert x.widget()["al"] == ["r", t + 120]  # missiles: the same alert goes on
    feed.neptun_up = False
    x.refresh(t + 240)
    assert x.widget()["al"] == ["o", t + 120]  # the fallback feed: on, level unknown
    feed.here = feed.near = False
    x.refresh(t + 1920)
    assert "al" not in x.widget()
    assert stats.alert_seconds(t, t + 86400) == 1800


def test_alert_open_across_restart_and_off_switches(stats):
    feed = Feed()
    feed.here = True
    extras.Extras(stats, fetch=feed, env={}).refresh(1_800_000_000)
    x = extras.Extras(stats, fetch=feed, env={})  # restarted during the alert
    assert x.alert == "y" and x.alert_since == 1_800_000_000
    x.refresh(1_800_000_060)
    assert x.alert == "y" and x.alert_since == 1_800_000_000
    off = extras.Extras(stats, fetch=feed, env={"WEATHER": "0", "ALERT_REGION": "-"})
    off.refresh(1_800_000_000)
    assert off.widget() == {}


def test_outages_table_with_temperature_and_alerts(stats):
    now = time.time()
    stats.outage_started(now - 3 * 3600, 100)
    stats.outage_ended(now - 2 * 3600, 90)
    stats._exec("INSERT INTO alerts (start, end) VALUES (?, ?)", (int(now - 3 * 3600), int(now - 3600)))
    day = time.strftime("%Y-%m-%d", time.localtime(now - 3 * 3600))
    html = stats.outages_html(lang("en"), temps={day: (1.2, 7.9)})
    assert "<th><b>t°</b></th>" in html and "<td>1…8°</td>" in html and "<td>2h 00m</td>" in html


def test_nearby_alert_keeps_its_start_across_restart(stats):
    feed = Feed()
    feed.near = True
    extras.Extras(stats, fetch=feed, env={}).refresh(1_800_000_000)
    x = extras.Extras(stats, fetch=feed, env={})
    x.refresh(1_800_000_600)
    assert x.widget()["al"] == ["n", 1_800_000_000]


def test_alert_start_comes_from_neptun(stats):
    feed = Feed()
    feed.near = True
    rows = feed.__call__

    def with_since(url):
        d = rows(url)
        for r in d.get("raions", []):
            r["since"] = "2027-01-15T08:00:00.5Z"
        return d
    x = extras.Extras(stats, fetch=with_since, env={})
    x.refresh(1_800_000_000)
    assert x.widget()["al"] == ["n", 1_800_000_000]  # 2027-01-15 08:00 UTC
