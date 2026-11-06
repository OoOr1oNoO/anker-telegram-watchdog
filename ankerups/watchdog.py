"""Outage, charge and full-charge alerts; Windows power plans and popups.
"""

import os
import re
import subprocess
import threading
import time
from datetime import datetime

from .bot import Telegram
from .common import IDLE_LOAD_W, IS_WINDOWS, POWER_SAVER_GUID, as_int, data_dir, log
from .i18n import Lang
from .stats import Settings, Stats


class PowerPlan:
    """Windows power plan switching via powercfg; logs only on other OSes or in dry-run.

    The plan active before the outage is kept in a file next to the exe, so it is
    restored even if the program was restarted while running on battery.
    POWER_PLAN_BATTERY / POWER_PLAN_GRID in .env override the plan GUIDs; when
    POWER_PLAN_GRID is empty the plan that was active before the outage is restored.
    """

    def __init__(self, dry_run: bool):
        self.enabled = IS_WINDOWS and not dry_run
        self.state_file = data_dir() / "anker_ups.plan"
        self.battery_guid = (os.environ.get("POWER_PLAN_BATTERY") or POWER_SAVER_GUID).strip().lower()
        self.grid_guid = (os.environ.get("POWER_PLAN_GRID") or "").strip().lower()

    @staticmethod
    def _powercfg(*args: str) -> str:
        # stdin must be explicit: a --noconsole exe has no valid stdin handle to inherit
        result = subprocess.run(
            ["powercfg", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            raise RuntimeError(f"powercfg {' '.join(args)} failed ({result.returncode}): "
                               f"{(result.stdout + result.stderr).strip()}")
        return result.stdout

    def _active_guid(self) -> str | None:
        match = re.search(r"[0-9a-f]{8}-[0-9a-f-]{27}", self._powercfg("/getactivescheme"), re.I)
        return match.group(0).lower() if match else None

    def log_plans(self) -> None:
        if IS_WINDOWS:
            try:
                log.info("[power] plans:\n%s", self._powercfg("/list").strip())
                log.info("[power] battery plan %s, grid plan %s", self.battery_guid,
                         self.grid_guid or "previous")
            except Exception:
                log.exception("[power] cannot list plans")

    def power_saver(self) -> None:
        if not self.enabled:
            log.info("[power] would switch to %s", self.battery_guid)
            return
        try:
            active = self._active_guid()
            if not self.state_file.exists():
                self.state_file.write_text(
                    active if active and active != self.battery_guid else "", encoding="utf-8")
            if (self.battery_guid == POWER_SAVER_GUID
                    and POWER_SAVER_GUID not in self._powercfg("/list").lower()):
                # Windows 11 often hides the built-in plan; restore it under its original GUID
                self._powercfg("-duplicatescheme", POWER_SAVER_GUID, POWER_SAVER_GUID)
            self._powercfg("/setactive", self.battery_guid)
            log.info("[power] switched %s -> %s, now active %s",
                     active, self.battery_guid, self._active_guid())
        except Exception:
            log.exception("[power] failed to switch to battery plan")

    def restore(self) -> None:
        if not self.enabled:
            log.info("[power] would restore %s", self.grid_guid or "previous plan")
            return
        if not self.state_file.exists():
            return
        try:
            target = self.grid_guid or self.state_file.read_text(encoding="utf-8").strip()
            if target:
                self._powercfg("/setactive", target)
            self.state_file.unlink()
            log.info("[power] restored %s, now active %s", target or "nothing", self._active_guid())
        except Exception:
            log.exception("[power] failed to restore plan")


def show_popup(title: str, text: str) -> None:
    """Topmost Windows message box in a background thread; log only elsewhere."""
    if not IS_WINDOWS:
        log.info("[popup] %s: %s", title, text)
        return
    import ctypes  # noqa: PLC0415 - Windows only

    flags = 0x30 | 0x1000 | 0x40000  # MB_ICONWARNING | MB_SYSTEMMODAL | MB_TOPMOST
    threading.Thread(target=ctypes.windll.user32.MessageBoxW,
                     args=(None, text, title, flags), daemon=True).start()


class Watchdog:
    def __init__(self, tg: Telegram, power: PowerPlan, confirm_s: float, settings: Settings,
                 name: str = "Anker", stats: Stats | None = None, full_confirm_s: float = 180):
        self.name = name
        self.stats = stats
        self.tg = tg
        self.power = power
        self.confirm_s = confirm_s
        # A full station with (almost) no load tops itself up from the grid in short bursts,
        # so its AC input sits at 0 W for up to a minute at a time while the grid is up; in
        # that state an outage needs a longer run of 0 W (with a real load the input never drops)
        self.full_confirm_s = full_confirm_s
        self.settings = settings
        self.on_battery: bool | None = None  # None until the first telemetry
        self.no_grid_since: float | None = None
        self.outage_started: float | None = None  # wall clock, for the outage duration
        self.fired: dict[str, set[int]] = {}  # battery name -> thresholds already reported
        self.critical_fired = False
        # wall clock when the grid came back after an outage, until the battery is full;
        # kept in the DB so a restart while charging still sends the full-charge alert
        saved = stats.meta("charge_pending") if stats else None
        self.charge_pending: float | None = float(saved) if saved else None

    def _set_charge_pending(self, value: float | None) -> None:
        self.charge_pending = value
        if self.stats:
            self.stats.meta("charge_pending", "" if value is None else str(int(value)))

    def outage_seconds(self) -> float | None:
        return time.time() - self.outage_started if self.outage_started else None

    def evaluate(self, grid: bool, ac_in: int, levels: dict[str, int], remaining_h: float | None = None,
                 load: int | None = None) -> None:
        """levels ({"main": %, "expansion": %}) drive the per-battery thresholds and the messages."""
        now = time.monotonic()
        total = round(sum(levels.values()) / len(levels))
        name = self.name
        if grid:
            self.no_grid_since = None
            if self.on_battery is None and self.stats and (start := self.stats.open_outage()):
                # the outage ended while we were not running
                self.on_battery, self.outage_started = True, start
            if self.on_battery:
                if self.settings.flag("alert_outage"):
                    off = self.outage_seconds()
                    self.tg.send(lambda L: L.t("al_back", dur=L.dur(off) if off else "?", ac_in=ac_in,
                                               charge=L.charge(levels)))
                if self.stats:
                    self.stats.outage_ended(time.time(), total)
                if min(levels.values()) < 100:  # nothing to report after a short outage
                    self._set_charge_pending(time.time())
            elif self.charge_pending and min(levels.values()) >= 100:
                # once per outage: the station hovers 99-100 % when full, so no repeats
                if self.settings.flag("alert_full"):
                    took = time.time() - self.charge_pending
                    self.tg.send(lambda L: L.t("al_full", dur=L.dur(took), charge=L.charge(levels)))
                self._set_charge_pending(None)
            if self.on_battery is not False:
                # also on the first reading, in case we were restarted during an outage
                self.power.restore()
            self.on_battery = False
            self.outage_started = None
            self.fired.clear()
            self.critical_fired = False
            return

        def left(L: Lang) -> str:
            return L.t("al_left", dur=L.dur(remaining_h * 3600)) if remaining_h else ""

        if self.no_grid_since is None:
            self.no_grid_since = now
        idle_full = min(levels.values()) >= 100 and (load or 0) < IDLE_LOAD_W
        if not self.on_battery and now - self.no_grid_since >= (self.full_confirm_s if idle_full else self.confirm_s):
            self.on_battery = True
            resumed = self.stats.open_outage() if self.stats else None
            if resumed:
                # restarted during an outage: keep its start, do not repeat earlier alerts
                self.outage_started = resumed
                for key, pct in levels.items():
                    self.fired[key] = {t for t in self.settings.thresholds if pct <= t}
                self.critical_fired = bool(self.settings.critical and total <= self.settings.critical)
                log.info("Resumed outage started %s", datetime.fromtimestamp(resumed))
            else:
                self.outage_started = time.time() - (now - self.no_grid_since)
                self._set_charge_pending(None)  # charging did not finish before this outage
                # how long the grid was up: since the end of the previous outage
                last_end = self.stats.last_outage_end() if self.stats else None
                up = self.outage_started - last_end if last_end and self.outage_started > last_end else None
                # a full idle station confirms after minutes: then say when the input really dropped
                started = (time.strftime("%H:%M", time.localtime(self.outage_started))
                           if time.time() - self.outage_started >= 60 else None)
                if self.settings.flag("alert_outage"):
                    self.tg.send(lambda L: L.t("al_lost", name=name, charge=L.charge(levels), left=left(L),
                                               since=L.t("al_since", time=started) if started else "",
                                               uptime=L.t("al_uptime", dur=L.dur(up)) if up else ""))
                if self.stats:
                    self.stats.outage_started(self.outage_started, total)
            self.power.power_saver()

        if self.on_battery:
            # per battery: report only the lowest crossed threshold, mark the higher ones as done
            for key, pct in levels.items():
                fired = self.fired.setdefault(key, set())
                crossed = [t for t in self.settings.thresholds if pct <= t and t not in fired]
                if crossed:
                    t = min(crossed)
                    fired.update(x for x in self.settings.thresholds if x >= t)
                    self.tg.send(lambda L, key=key, pct=pct, t=t: L.t(
                        "al_threshold", name=L.battery(key), pct=pct, t=t, charge=L.charge(levels), left=left(L)))
            critical = self.settings.critical
            if critical and total <= critical and not self.critical_fired:
                self.critical_fired = True
                self.tg.send(lambda L: L.t("al_critical", charge=L.charge(levels), left=left(L)))
                L = self.tg.default
                show_popup(L.t("popup_title", name=name),
                           L.t("popup_text", charge=L.charge(levels), left=left(L)))


def battery_state(data: dict) -> tuple[dict[str, int] | None, int]:
    """Return (per-battery %, expansion pack count) from MQTT data.

    Field names differ a little between station models, so known variants are tried.
    """
    main = as_int(data.get("main_battery_soc", data.get("battery_soc")))
    if main is None:
        return None, 0
    exps = [as_int(data.get(f"exp_{i}_soc")) for i in range(1, 7)]
    packs = as_int(data.get("expansion_packs"))
    if packs is None:  # models without a pack counter report 0% for empty slots
        packs = sum(1 for e in exps if e)
    exps = [e for e in exps[:packs] if e is not None]
    if not exps:
        return {"main": main}, packs
    return {"main": main} | {("expansion" if len(exps) == 1 else f"expansion {i}"): e
                             for i, e in enumerate(exps, 1)}, packs
