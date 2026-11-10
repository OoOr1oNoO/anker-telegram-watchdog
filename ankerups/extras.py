"""Weather and air quality (Open-Meteo) and air-raid alerts (NEPTUN, with the yellow / red level;
the ubilling.net.ua feed as a fallback), all without keys.

They are context for the status widget and the stats: the temperature now and per day, and
whether an alert is on in the chosen region (or only in the nearby one). Alert periods of the
chosen region are kept in the stats DB.

.env: LATITUDE / LONGITUDE (default Kyiv), ALERT_REGION (default "м. Київ"), ALERT_NEAR
(default "Київська область"); WEATHER=0 or ALERT_REGION=- turns a part off.
"""

import contextlib
import json
import os
import threading
import time
import urllib.request
from datetime import datetime

from .common import log

WEATHER_URL = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
               "&current=temperature_2m,weather_code&daily=temperature_2m_max,temperature_2m_min"
               "&past_days=31&forecast_days=1&timezone=auto")
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}&current=european_aqi"
ALERTS_URL = "https://ubilling.net.ua/aerialalerts/"  # the fallback: on / off only
NEPTUN_URL = "https://neptun.in.ua/api/v1/alerts"  # with the level: yellow (drones) or red (missiles)
OWN = ("r", "y", "o")
WEATHER_EVERY, ALERTS_EVERY = 1800, 30


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={  # NEPTUN's CDN refuses Python's default User-Agent
        "User-Agent": "anker-telegram-watchdog (+https://github.com/OoOr1oNoO/anker-telegram-watchdog)",
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


# a thermometer: deep blue, light blue, white at 0°, yellow, orange, red
TEMP_STOPS = ((-30, (30, 90, 255)), (-15, (100, 190, 255)), (0, (255, 255, 255)), (10, (255, 214, 10)),
              (20, (255, 149, 0)), (30, (255, 59, 48)))


def temp_color(t: float) -> str:
    """The weather border color for a temperature, between the stops (the ends beyond them)."""
    t = max(TEMP_STOPS[0][0], min(TEMP_STOPS[-1][0], t))
    for (t0, c0), (t1, c1) in zip(TEMP_STOPS, TEMP_STOPS[1:]):
        if t <= t1:
            a = (t - t0) / (t1 - t0)
            return "#" + "".join(f"{round(x + (y - x) * a):02x}" for x, y in zip(c0, c1))
    return "#ff3b30"


def _since(rows: list[dict]) -> float | None:
    """The earliest start of NEPTUN alert rows (ISO time, UTC), None if none is given."""
    starts = []
    for r in rows:
        with contextlib.suppress(Exception):
            starts.append(datetime.fromisoformat(r["since"].replace("Z", "+00:00")).timestamp())
    return int(min(starts)) if starts else None


class Extras:
    def __init__(self, stats=None, fetch=fetch_json, env=None):
        env = os.environ if env is None else env
        self.stats = stats
        self.fetch = fetch
        self.weather_on = env.get("WEATHER", "1").strip() != "0"
        self.lat = float(env.get("LATITUDE") or 50.45)
        self.lon = float(env.get("LONGITUDE") or 30.52)
        self.region = (env.get("ALERT_REGION") or "м. Київ").strip()
        self.near = (env.get("ALERT_NEAR") or "Київська область").strip()
        self.temp: float | None = None
        self.code: int | None = None
        self.daily: dict[str, tuple[float, float]] = {}  # date -> (min, max)
        self.aqi: int | None = None  # European air quality index: 0-20 good ... 100+ extremely poor
        self.alert = ""  # see _alert_kind
        self.alert_since: float | None = None
        self._weather_at = self._alerts_at = 0.0
        self._started: float | None = None  # when the current alert began, as the source says
        if stats and self.region != "-":  # an alert still on from before a restart keeps its start
            try:
                self.alert, self.alert_since = json.loads(stats.meta("alert") or '["", null]')
            except (ValueError, TypeError):
                pass
            row = stats._exec("SELECT start FROM alerts WHERE end IS NULL ORDER BY start DESC LIMIT 1")
            if row and self.alert not in OWN:
                self.alert, self.alert_since = "o", row[0][0]

    def refresh(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if self.weather_on and now - self._weather_at >= WEATHER_EVERY:
            self._weather_at = now
            try:
                d = self.fetch(WEATHER_URL.format(lat=self.lat, lon=self.lon))
                self.temp, self.code = d["current"]["temperature_2m"], d["current"]["weather_code"]
                days = d["daily"]
                self.daily = {t: (lo, hi) for t, lo, hi in zip(days["time"], days["temperature_2m_min"],
                                                               days["temperature_2m_max"]) if lo is not None}
            except Exception as exc:
                log.warning("Weather download failed: %s", exc)
                self._weather_at = now - WEATHER_EVERY + 300
            try:
                self.aqi = round(self.fetch(AIR_URL.format(lat=self.lat, lon=self.lon))["current"]["european_aqi"])
            except Exception as exc:
                log.warning("Air quality download failed: %s", exc)
        if self.region != "-" and now - self._alerts_at >= ALERTS_EVERY:
            self._alerts_at = now
            kind = self._alert_kind()
            if kind is None:  # both sources failed: keep the last state, never a false all-clear
                return
            own, was_own = kind in OWN, self.alert in OWN
            if own != was_own and self.stats:  # the region's own alert periods
                if own:
                    self.stats._exec("INSERT INTO alerts (start) VALUES (?)", (int(now),))
                else:
                    self.stats._exec("UPDATE alerts SET end = ? WHERE end IS NULL", (int(now),))
            since = self.alert_since
            if kind != self.alert:
                log.info("Air-raid alert: %r -> %r", self.alert, kind)
                keep = own and was_own  # yellow <-> red: the same alert goes on
                since = since if keep else now if kind else None
            if kind and self._started:  # NEPTUN gives the start (the earliest one in the nearby region)
                since = min(since, self._started) if own and was_own and since else self._started
            if (kind, since) != (self.alert, self.alert_since):
                self.alert, self.alert_since = kind, since
                if self.stats:
                    self.stats.meta("alert", json.dumps([kind, since]))

    def _alert_kind(self) -> str | None:
        """"r" / "y": the region's red / yellow alert (NEPTUN), "o": the region's alert of an unknown
        level (the fallback feed), "n": only in the nearby region, "": none; None if no source works."""
        try:
            data = self.fetch(NEPTUN_URL)
            rows = data.get("oblasts", []) + data.get("raions", [])
            here = [r for r in data.get("oblasts", []) if r.get("name", "").lower() == self.region.lower()]
            near = [r for r in rows if r.get("oblast", "").lower() == self.near.lower()]
            self._started = _since(here or near)
            if here:
                return "r" if any(r.get("level", "red") == "red" for r in here) else "y"
            return "n" if near else ""
        except Exception as exc:
            log.warning("NEPTUN alerts download failed: %s", exc)
        self._started = None
        try:
            states = self.fetch(ALERTS_URL).get("states", {})
        except Exception as exc:
            log.warning("Air-raid alerts download failed: %s", exc)
            return None
        if states.get(self.region, {}).get("alertnow"):
            return "o"
        return "n" if states.get(self.near, {}).get("alertnow") else ""

    def widget(self) -> dict:
        """Fields for the status widget data: w = [temperature, WMO code, AQI or None, border color],
        al = [level, since]."""
        data: dict = {}
        if self.temp is not None:
            data["w"] = [round(self.temp), self.code, self.aqi, temp_color(self.temp)]
        if self.alert:
            data["al"] = [self.alert, int(self.alert_since or time.time())]  # a credit to NEPTUN follows
        return data

    def run(self) -> None:
        while True:
            try:
                self.refresh()
            except Exception:
                log.exception("Weather / alerts check failed")
            time.sleep(30)

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True).start()
