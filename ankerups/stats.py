"""Minute samples, outages and settings in SQLite; summaries and charts.
"""

import io
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .i18n import Lang


def chart_title(L: Lang, hours: int) -> str:
    return L.t("chart_hours", hours=hours) if hours < 48 else L.t("chart_days", days=hours // 24)


def merge_slot(slot: str, grid: bool) -> str:
    """Add a minute with or without grid to a half-hour cell of grid_slots()."""
    state = "1" if grid else "0"
    return state if slot in ("-", state) else "p"


DEFAULT_TARIFF = 4.32  # UAH per kWh, Ukrainian households


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def table(rows: list[list[str]], compact: bool = False) -> str:
    """A rich-message table; the first row is the header."""
    head = "".join(f"<th><b>{esc(c)}</b></th>" for c in rows[0])
    body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in rows[1:])
    return f"<table bordered striped{' compact' if compact else ''}><tr>{head}</tr>{body}</table>"


class Stats:
    """Minute samples and outages in SQLite next to the script, for charts and summaries.

    Telemetry arrives every 3-5 s; it is averaged per minute, so energy is the sum of
    minute averages (W * 60 s). Used from the asyncio loop and the Telegram thread.
    """

    def __init__(self, path: Path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY, ac_in REAL, out REAL,
                                                soc INTEGER, grid INTEGER);
            CREATE TABLE IF NOT EXISTS outages (id INTEGER PRIMARY KEY, start INTEGER,
                                                end INTEGER, soc_start INTEGER, soc_end INTEGER);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        self.minute: int | None = None
        self.acc: list[tuple[float, float, int, bool]] = []

    def _exec(self, sql: str, args: tuple = ()) -> list:
        with self.lock:
            rows = self.db.execute(sql, args).fetchall()
            self.db.commit()
            return rows

    def add_reading(self, ac_in: float, out: float, soc: int, grid: bool) -> None:
        minute = int(time.time() // 60)
        if self.minute is not None and minute != self.minute and self.acc:
            n = len(self.acc)
            self._exec("INSERT OR REPLACE INTO samples VALUES (?, ?, ?, ?, ?)", (
                self.minute * 60, sum(a[0] for a in self.acc) / n, sum(a[1] for a in self.acc) / n,
                self.acc[-1][2], int(all(a[3] for a in self.acc))))
            self.acc = []
        self.minute = minute
        self.acc.append((ac_in or 0, out or 0, soc, grid))

    def meta(self, key: str, value: str | None = None) -> str | None:
        if value is not None:
            self._exec("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
            return value
        rows = self._exec("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0][0] if rows else None

    def open_outage(self) -> int | None:
        rows = self._exec("SELECT start FROM outages WHERE end IS NULL ORDER BY start DESC LIMIT 1")
        return rows[0][0] if rows else None

    def outage_started(self, start: float, soc: int) -> None:
        if self.open_outage() is None:
            self._exec("INSERT INTO outages (start, soc_start) VALUES (?, ?)", (int(start), soc))

    def last_outage_end(self) -> int | None:
        rows = self._exec("SELECT MAX(end) FROM outages WHERE end IS NOT NULL")
        return rows[0][0] if rows else None

    def outage_ended(self, end: float, soc: int) -> None:
        self._exec("UPDATE outages SET end = ?, soc_end = ? WHERE end IS NULL", (int(end), soc))

    def grid_slots(self, midnight: float, now: float) -> str:
        """Half hours from midnight to now: "1" grid all the time, "0" no grid all the time, "p" part
        of it (an outage started or ended in it), "-" no data."""
        rows = self._exec("SELECT ts, grid FROM samples WHERE ts >= ? AND ts < ?", (int(midnight), int(now)))
        slots = ["-"] * min(48, int((now - midnight) // 1800) + 1)
        for ts, grid in rows:
            i = int((ts - midnight) // 1800)
            if 0 <= i < len(slots):
                slots[i] = merge_slot(slots[i], bool(grid))
        return "".join(slots)

    def wh_per_percent(self, now: float | None = None) -> float | None:
        """Battery energy per 1 % of the average charge, learned from the last 3 h of charging
        from the grid (net input W x minutes / change of %); None without enough data."""
        now = time.time() if now is None else now
        rows = self._exec("SELECT ac_in, out, soc FROM samples WHERE ts >= ? AND grid = 1 AND ac_in > out + 20 "
                          "ORDER BY ts", (int(now - 3 * 3600),))
        if len(rows) < 10:
            return None
        wh = sum(a - o for a, o, _ in rows) / 60
        gained = rows[-1][2] - rows[0][2]
        return wh / gained if gained >= 3 and wh > 0 else None

    def energy_kwh(self, since: float, until: float | None = None) -> tuple[float, float]:
        rows = self._exec("SELECT COALESCE(SUM(ac_in), 0), COALESCE(SUM(out), 0) FROM samples "
                          "WHERE ts >= ? AND ts < ?", (int(since), int(until or time.time() + 60)))
        return rows[0][0] * 60 / 3.6e6, rows[0][1] * 60 / 3.6e6

    def outages_between(self, start: float, end: float) -> list[tuple]:
        # outages overlapping the window; an open one counts until now
        return self._exec("SELECT start, COALESCE(end, CAST(strftime('%s','now') AS INTEGER)), "
                          "soc_start, soc_end FROM outages WHERE start < ? AND "
                          "(end IS NULL OR end > ?) ORDER BY start", (int(end), int(start)))

    def energy_text(self, L: Lang) -> str:
        now = datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        periods = [("p_today", today), ("p_7d", now - timedelta(days=7)),
                   ("p_30d", now - timedelta(days=30)), ("p_all", datetime.fromtimestamp(0))]
        lines = [L.t("en_title")]
        for label, since in periods:
            e_in, e_out = self.energy_kwh(since.timestamp())
            lines.append(f"{L.t(label)}: {L.energy(e_in)} / {L.energy(e_out)}")
        return "\n".join(lines)

    def outages_text(self, L: Lang, days: int = 30, limit: int = 10) -> str:
        now = time.time()
        rows = self.outages_between(now - days * 86400, now)
        if not rows:
            return L.t("out_none", days=days)
        total = sum(end - start for start, end, *_ in rows)
        lines = [L.t("out_title", days=days, n=len(rows), dur=L.dur(total))]
        for start, end, soc_start, soc_end in rows[-limit:][::-1]:
            when = datetime.fromtimestamp(start).strftime("%d.%m %H:%M")
            used = f", {soc_start}% → {soc_end}%" if soc_end is not None else L.t("out_ongoing")
            lines.append(f"{when}  {L.dur(end - start)}{used}")
        return "\n".join(lines)

    def months_text(self, L: Lang, count: int = 2) -> str:
        """This month and the previous ones: outages, time without grid, energy."""
        now = datetime.now()
        starts = [now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)]
        while len(starts) < count:
            starts.append((starts[-1] - timedelta(days=1)).replace(day=1))
        lines = [L.t("mo_title")]
        for i, start in enumerate(starts):
            s, e = start.timestamp(), (now if i == 0 else starts[i - 1]).timestamp()
            spans = [min(b, e) - max(a, s) for a, b, *_ in self.outages_between(s, e)]
            e_in, e_out = self.energy_kwh(s, e)
            lines += ["", f"{L.month(start.month)} {start.year}",
                      L.t("mo_outages", n=len(spans), dur=L.dur(sum(spans)), longest=L.dur(max(spans)))
                      if spans else L.t("mo_none"),
                      L.t("sum_energy", e_in=L.energy(e_in), e_out=L.energy(e_out))]
        first = self._exec("SELECT MIN(ts) FROM samples")[0][0]
        if first and first > starts[-1].timestamp():
            lines += ["", L.t("mo_since", date=datetime.fromtimestamp(first).strftime("%d.%m.%Y"))]
        return "\n".join(lines)

    # rich messages (Bot API tables); the *_text versions above are the fallback
    def energy_html(self, L: Lang, tariff: float) -> str:
        now = datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        periods = [("p_today", today), ("p_7d", now - timedelta(days=7)),
                   ("p_30d", now - timedelta(days=30)), ("p_all", datetime.fromtimestamp(0))]
        rows = [[L.t("t_period"), L.t("t_in"), L.t("t_out"), L.t("t_cost")]]
        for label, since in periods:
            e_in, e_out = self.energy_kwh(since.timestamp())
            rows.append([L.t(label), f"{e_in:.2f}", f"{e_out:.2f}", L.money(e_in * tariff)])
        return (f"<h3>{esc(L.t('en_title_short'))}</h3>" + table(rows)
                + f"<footer>{esc(L.t('t_tariff_note', v=L.money(tariff)))}</footer>")

    def outages_html(self, L: Lang, days: int = 30) -> str:
        """One row per day (by the start of the outage): time without grid, the outages, the lowest
        charge they ended with."""
        now = time.time()
        found = self.outages_between(now - days * 86400, now)
        if not found:
            return f"<p>{esc(L.t('out_none', days=days))}</p>"
        total = sum(end - start for start, end, *_ in found)
        by_day: dict[str, list] = {}
        for outage in found:
            by_day.setdefault(datetime.fromtimestamp(outage[0]).strftime("%d.%m"), []).append(outage)
        rows = [[L.t("t_day"), L.t("t_off"), L.t("t_when"), L.t("t_min")]]
        for day, outages in list(by_day.items())[::-1]:
            spans = ", ".join(f"{datetime.fromtimestamp(s):%H:%M}–" + (f"{datetime.fromtimestamp(e):%H:%M}"
                                                                         if soc_end is not None else "…")
                              for s, e, _, soc_end in outages)
            ends = [soc_end for *_, soc_end in outages if soc_end is not None]
            rows.append([day, L.dur(sum(e - s for s, e, *_ in outages), True), spans,
                         f"{min(ends)}%" if ends else "—"])
        return (f"<h3>{esc(L.t('out_title', days=days, n=len(found), dur=L.dur(total, True)))}</h3>"
                + table(rows, compact=True))

    def months_html(self, L: Lang, tariff: float, count: int = 3) -> str:
        now = datetime.now()
        starts = [now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)]
        while len(starts) < count:
            starts.append((starts[-1] - timedelta(days=1)).replace(day=1))
        first = self._exec("SELECT MIN(ts) FROM samples")[0][0]
        rows = [[L.t("t_month"), L.t("t_outages"), L.t("t_off"), L.t("t_in"), L.t("t_cost")]]
        for i, start in enumerate(starts):
            s, e = start.timestamp(), (now if i == 0 else starts[i - 1]).timestamp()
            if first and e <= first:
                break
            spans = [min(b, e) - max(a, s) for a, b, *_ in self.outages_between(s, e)]
            e_in, _ = self.energy_kwh(s, e)
            rows.append([f"{L.month(start.month)} {start.year}", str(len(spans)), L.dur(sum(spans), True),
                         f"{e_in:.2f}", L.money(e_in * tariff)])
        footer = (f"<footer>{esc(L.t('mo_since', date=datetime.fromtimestamp(first).strftime('%d.%m.%Y')))}</footer>"
                  if first and first > starts[-1].timestamp() else "")
        return f"<h3>{esc(L.t('mo_title'))}</h3>" + table(rows) + footer

    def summary_text(self, end: datetime, L: Lang, hours: int = 24) -> str:
        start = end - timedelta(hours=hours)
        rows = self._exec("SELECT AVG(out), MAX(out), SUM(ac_in), SUM(out) FROM samples "
                          "WHERE ts >= ? AND ts < ?", (int(start.timestamp()), int(end.timestamp())))
        avg_out, max_out, sum_in, sum_out = rows[0]
        outages = self.outages_between(start.timestamp(), end.timestamp())
        off = sum(min(e, end.timestamp()) - max(s, start.timestamp()) for s, e, *_ in outages)
        if avg_out is None:
            return L.t("sum_nodata", hours=hours)
        return "\n".join([
            L.t("sum_title", hours=hours, start=f"{start:%d.%m %H:%M}", end=f"{end:%d.%m %H:%M}"),
            L.t("sum_outages", n=len(outages), dur=L.dur(off)),
            L.t("sum_load", avg=f"{avg_out:.0f}", peak=f"{max_out:.0f}"),
            L.t("sum_energy", e_in=L.energy(sum_in * 60 / 3.6e6), e_out=L.energy(sum_out * 60 / 3.6e6))])

    def chart_png(self, hours: int, L: Lang, end: float | None = None) -> bytes | None:
        import matplotlib  # noqa: PLC0415 - heavy, only needed for charts
        matplotlib.use("Agg")
        import matplotlib.dates as mdates  # noqa: PLC0415
        import matplotlib.pyplot as plt  # noqa: PLC0415

        end = end or time.time()
        start = end - hours * 3600
        rows = self._exec("SELECT ts, ac_in, out, soc FROM samples WHERE ts >= ? AND ts < ? "
                          "ORDER BY ts", (int(start), int(end)))
        if len(rows) < 2:
            return None
        ts = [datetime.fromtimestamp(r[0]) for r in rows]
        fig, ax = plt.subplots(figsize=(10, 4.5), dpi=110)
        ax.plot(ts, [r[2] for r in rows], color="#d9480f", lw=1.2, label=L.t("legend_load"))
        ax.plot(ts, [r[1] for r in rows], color="#1971c2", lw=1.0, alpha=0.8, label=L.t("legend_input"))
        ax.set_ylabel("W")
        ax.set_ylim(bottom=0)
        soc_ax = ax.twinx()
        soc_ax.plot(ts, [r[3] for r in rows], color="#2f9e44", lw=1.5, label=L.t("legend_charge"))
        soc_ax.set_ylim(0, 105)
        soc_ax.set_ylabel("%")
        outages = self.outages_between(start, end)
        for s, e, *_ in outages:
            ax.axvspan(datetime.fromtimestamp(max(s, start)), datetime.fromtimestamp(min(e, end)),
                       color="#868e96", alpha=0.25, lw=0)
        # like a clock face: long labelled ticks every few hours (days), short ones in between
        if hours <= 48:
            ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3 if hours <= 24 else 6)))
            ax.xaxis.set_minor_locator(mdates.HourLocator())
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        else:
            ax.xaxis.set_major_locator(mdates.DayLocator())
            ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18]))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
        ax.tick_params(axis="x", which="major", length=8, width=1.2)
        ax.tick_params(axis="x", which="minor", length=3, width=0.8)
        ax.grid(axis="x", which="major", alpha=0.35)
        ax.grid(axis="x", which="minor", alpha=0.12)
        ax.grid(axis="y", alpha=0.3)
        handles = ax.get_legend_handles_labels()[0] + soc_ax.get_legend_handles_labels()[0]
        ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=3,
                  fontsize=8, frameon=False)
        ax.set_title(chart_title(L, hours) + (L.t("chart_grey") if outages else ""))
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        return buf.getvalue()


class Settings:
    """User settings changed from the bot; stored in the stats DB, .env/CLI give defaults."""

    def __init__(self, stats: "Stats | None", defaults: dict[str, str]):
        self.stats = stats
        self.defaults = defaults

    def get(self, key: str) -> str:
        value = self.stats.meta(f"setting:{key}") if self.stats else None
        return self.defaults.get(key, "") if value is None else value

    def set(self, key: str, value: str) -> None:
        if self.stats:
            self.stats.meta(f"setting:{key}", value)

    @property
    def thresholds(self) -> list[int]:
        return sorted((int(t) for t in self.get("thresholds").split(",") if t.strip()), reverse=True)

    @property
    def critical(self) -> int:
        return int(self.get("critical") or 0)

    @property
    def summary(self) -> str:
        return self.get("summary").strip()

    @property
    def tariff(self) -> float:
        """Grid price per kWh (UAH by default), for the cost in the stats."""
        try:
            return float(self.get("tariff").replace(",", ".") or DEFAULT_TARIFF)
        except ValueError:
            return DEFAULT_TARIFF

    def flag(self, key: str) -> bool:
        return self.get(key) == "1"
