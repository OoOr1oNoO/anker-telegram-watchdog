# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "anker-solix-api @ https://github.com/thomluther/anker-solix-api/archive/refs/tags/v3.9.1.tar.gz",
# ]
# ///
"""Anker SOLIX power station watchdog via Anker cloud MQTT.

- Telegram alert when AC input is lost / restored.
- Switches Windows to the Power saver plan on outage, restores the previous plan after.
- Telegram alerts at 50% and 20% battery while on battery.

Config: environment variables or a .env file next to the script/exe (see .env.example).
"""

import argparse
import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import platform
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from aiohttp import ClientSession
from anker_solix_api.api import AnkerSolixApi
from anker_solix_api.mqtt_factory import SolixMqttDeviceFactory
from anker_solix_api.mqtt_pps import MODELS as PPS_MODELS

# persistent reply keyboard; button texts come back as normal messages.
# Telegram gives all buttons in a row the same width, so Status gets its own row.
MAIN_KEYBOARD = json.dumps({"keyboard": [[{"text": "📊 Status"}], [{"text": "🔦 Light"}]],
                            "resize_keyboard": True, "is_persistent": True})
LIGHT_MODES = {"off": "Off", "low": "Low", "medium": "Medium", "high": "High", "blinking": "Blink"}
POWER_SAVER_GUID = "a1841308-3541-4fab-bc81-f71556f20b4a"
IS_WINDOWS = platform.system() == "Windows"
REALTIME_TIMEOUT = 300  # seconds the device streams 0405 messages before the poller re-triggers

log = logging.getLogger("anker_ups")


def base_dir() -> Path:
    """Folder of the exe when packaged with PyInstaller, of the script otherwise."""
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def load_env() -> None:
    env_file = base_dir() / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Telegram:
    """Sends messages from a background thread and retries until the network is back.

    Also handles the bot buttons from the configured chat via long polling (getUpdates):
    📊 Status and 🔦 Light (an inline menu of light modes).
    """

    def __init__(self, token: str | None, chat_id: str | None):
        self.enabled = bool(token and chat_id)
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.queue: queue.Queue[tuple[str, str]] = queue.Queue()
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True).start()
        else:
            log.warning("TG_BOT_TOKEN/TG_CHAT_ID not set, Telegram disabled")

    def _call(self, method: str, params: dict, timeout: float = 10) -> dict:
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(f"{self.base}/{method}", data=data, timeout=timeout) as resp:
            return json.loads(resp.read())

    def send(self, text: str) -> None:
        log.info("ALERT: %s", text)
        if self.enabled:
            self.queue.put((f"{time.strftime('%H:%M:%S')} {text}", MAIN_KEYBOARD))

    def _worker(self) -> None:
        while True:
            text, markup = self.queue.get()
            while True:
                try:
                    self._call("sendMessage", {"chat_id": self.chat_id, "text": text,
                                               "reply_markup": markup})
                    break
                except Exception as exc:  # no internet during outage is expected
                    log.warning("Telegram send failed (%s), retrying in 15s", exc)
                    time.sleep(15)

    def listen_commands(self, status: Callable[[], str], light_state: Callable[[], str],
                        set_light: Callable[[str], str]) -> None:
        if self.enabled:
            threading.Thread(target=self._poll_commands, args=(status, light_state, set_light),
                             daemon=True).start()

    @staticmethod
    def _light_menu(current: str) -> tuple[str, str]:
        buttons = [{"text": ("• " if key == current else "") + label, "callback_data": f"light:{key}"}
                   for key, label in LIGHT_MODES.items()]
        markup = json.dumps({"inline_keyboard": [buttons[:3], buttons[3:]]})
        return f"🔦 Light: {LIGHT_MODES.get(current, '?')}", markup

    def _poll_commands(self, status: Callable[[], str], light_state: Callable[[], str],
                       set_light: Callable[[str], str]) -> None:
        with contextlib.suppress(Exception):
            self._call("setMyCommands", {"commands": json.dumps(
                [{"command": "status", "description": "Anker status"},
                 {"command": "light", "description": "Station light"}])})
        offset = None
        while True:
            try:
                params = {"timeout": 50, "allowed_updates": '["message", "callback_query"]'}
                if offset is not None:
                    params["offset"] = offset
                updates = self._call("getUpdates", params, timeout=60).get("result", [])
            except Exception as exc:
                log.warning("Telegram getUpdates failed (%s), retrying in 15s", exc)
                time.sleep(15)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    self._handle_update(upd, status, light_state, set_light)
                except Exception:
                    log.exception("Telegram update handling failed")

    def _handle_update(self, upd: dict, status: Callable[[], str], light_state: Callable[[], str],
                       set_light: Callable[[str], str]) -> None:
        if cb := upd.get("callback_query"):
            msg = cb.get("message") or {}
            # answer only the owner's chat
            if str(msg.get("chat", {}).get("id")) != self.chat_id:
                return
            mode = (cb.get("data") or "").removeprefix("light:")
            if mode not in LIGHT_MODES:
                return
            result = set_light(mode)
            self._call("answerCallbackQuery", {"callback_query_id": cb["id"], "text": result})
            if not result.startswith("⚠️"):
                text, markup = self._light_menu(mode)
                with contextlib.suppress(Exception):  # unchanged text makes Telegram return 400
                    self._call("editMessageText", {"chat_id": self.chat_id,
                                                   "message_id": msg.get("message_id"),
                                                   "text": text, "reply_markup": markup})
            return
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != self.chat_id:
            return
        text = (msg.get("text") or "").strip().lower()
        if text.startswith("/light") or "light" in text:
            self.queue.put(self._light_menu(light_state()))
        elif text.startswith(("/status", "/start")) or "status" in text:
            self.queue.put((status(), MAIN_KEYBOARD))


class PowerPlan:
    """Windows power plan switching via powercfg; logs only on other OSes or in dry-run.

    The plan active before the outage is kept in a file next to the exe, so it is
    restored even if the program was restarted while running on battery.
    POWER_PLAN_BATTERY / POWER_PLAN_GRID in .env override the plan GUIDs; when
    POWER_PLAN_GRID is empty the plan that was active before the outage is restored.
    """

    def __init__(self, dry_run: bool):
        self.enabled = IS_WINDOWS and not dry_run
        self.state_file = base_dir() / "anker_ups.plan"
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


def fmt_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


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
    def __init__(self, tg: Telegram, power: PowerPlan, confirm_s: float, thresholds: list[int],
                 critical: int, name: str = "Anker"):
        self.name = name
        self.tg = tg
        self.power = power
        self.confirm_s = confirm_s
        self.thresholds = sorted(thresholds, reverse=True)
        self.critical = critical
        self.on_battery: bool | None = None  # None until the first telemetry
        self.no_grid_since: float | None = None
        self.outage_started: float | None = None  # wall clock, for the outage duration
        self.fired: dict[str, set[int]] = {}  # battery name -> thresholds already reported
        self.critical_fired = False

    def outage_duration(self) -> str:
        return fmt_duration(time.time() - self.outage_started) if self.outage_started else "?"

    def evaluate(self, grid: bool, ac_in: int, levels: dict[str, int], charge: str,
                 remaining_h: float | None = None) -> None:
        """levels ({"main": %, "expansion": %}) drive the per-battery thresholds;
        charge is the human-readable breakdown for messages."""
        now = time.monotonic()
        if grid:
            self.no_grid_since = None
            if self.on_battery:
                self.tg.send(f"⚡ Grid power is back after {self.outage_duration()}: "
                             f"input {ac_in} W, charge {charge}")
            if self.on_battery is not False:
                # also on the first reading, in case we were restarted during an outage
                self.power.restore()
            self.on_battery = False
            self.outage_started = None
            self.fired.clear()
            self.critical_fired = False
            return

        left = f", ~{fmt_duration(remaining_h * 3600)} left" if remaining_h else ""
        if self.no_grid_since is None:
            self.no_grid_since = now
        if not self.on_battery and now - self.no_grid_since >= self.confirm_s:
            self.on_battery = True
            self.outage_started = time.time() - (now - self.no_grid_since)
            self.tg.send(f"🔌 {self.name} lost AC input, running on battery: {charge}{left}")
            self.power.power_saver()

        if self.on_battery:
            # per battery: report only the lowest crossed threshold, mark the higher ones as done
            for name, pct in levels.items():
                fired = self.fired.setdefault(name, set())
                crossed = [t for t in self.thresholds if pct <= t and t not in fired]
                if crossed:
                    t = min(crossed)
                    fired.update(x for x in self.thresholds if x >= t)
                    self.tg.send(f"🔋 {name.capitalize()} battery {pct}% (threshold {t}%), "
                                 f"charge {charge}{left}")
            total = round(sum(levels.values()) / len(levels))
            if self.critical and total <= self.critical and not self.critical_fired:
                self.critical_fired = True
                self.tg.send(f"🪫 CRITICAL: charge {charge}{left}. Save your work and shut down the PC")
                show_popup(f"{self.name}: critical battery",
                           f"Battery charge is {charge}{left}.\n\n"
                           "Save your work and shut down the computer.")


def battery_state(data: dict) -> tuple[dict[str, int] | None, int, str]:
    """Return (per-battery %, expansion pack count, text) from MQTT data.

    Field names differ a little between station models, so known variants are tried.
    Expansion packs are assumed to match the main battery capacity, so the total
    charge shown in the text is the plain average.
    """
    main = as_int(data.get("main_battery_soc", data.get("battery_soc")))
    if main is None:
        return None, 0, ""
    exps = [as_int(data.get(f"exp_{i}_soc")) for i in range(1, 7)]
    packs = as_int(data.get("expansion_packs"))
    if packs is None:  # models without a pack counter report 0% for empty slots
        packs = sum(1 for e in exps if e)
    exps = [e for e in exps[:packs] if e is not None]
    if not exps:
        return {"main": main}, packs, f"{main}%"
    levels = {"main": main} | {
        ("expansion" if len(exps) == 1 else f"expansion {i}"): e for i, e in enumerate(exps, 1)}
    total = round(sum(levels.values()) / len(levels))
    parts = ", ".join(f"{k} {v}%" for k, v in levels.items())
    return levels, packs, f"{total}% ({parts})"


def as_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


async def find_device(api: AnkerSolixApi, wanted_sn: str | None) -> dict:
    await api.update_sites()
    await api.get_bind_devices()
    for sn, dev in api.devices.items():
        log.info("  account device %s %s (%s)", sn, dev.get("device_pn"), dev.get("name") or dev.get("alias"))
    for sn, dev in api.devices.items():
        if (wanted_sn and sn == wanted_sn) or (not wanted_sn and dev.get("device_pn") in PPS_MODELS):
            return dev
    raise SystemExit("No power station with MQTT support found in this Anker account; "
                     "set ANKER_DEVICE_SN or check the supported models in anker-solix-api")


async def start_poller(api: AnkerSolixApi, device: dict) -> asyncio.Task:
    session = await api.startMqttSession()
    if not (session and session.is_connected()):
        raise ConnectionError("cannot connect to Anker MQTT server")
    topics = {f"{session.get_topic_prefix(deviceDict=device)}#"}
    # The poller subscribes the device topic and keeps re-sending the realtime trigger
    return asyncio.create_task(session.message_poller(
        topics=topics, trigger_devices={device["device_sn"]}, timeout=REALTIME_TIMEOUT,
    ))


async def restart_poller(api: AnkerSolixApi, device: dict, poller: asyncio.Task) -> asyncio.Task:
    # Wait for the old poller to finish: its CancelledError handler calls session.cleanup(),
    # which would otherwise tear down the freshly connected client and its message callback.
    poller.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await poller
    api.stopMqttSession()
    return await start_poller(api, device)


async def run(args: argparse.Namespace) -> None:
    tg = Telegram(os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID"))
    power = PowerPlan(dry_run=args.dry_run)
    power.log_plans()
    watchdog = Watchdog(tg, power, confirm_s=args.confirm, thresholds=args.thresholds,
                        critical=int(os.environ.get("CRITICAL_LEVEL") or 10))

    if args.test_telegram:
        tg.send("Test message from anker_ups")
        await asyncio.sleep(5)
        return

    user, password = os.environ.get("ANKERUSER"), os.environ.get("ANKERPASSWORD")
    if not (user and password):
        raise SystemExit("ANKERUSER/ANKERPASSWORD not set")

    async with ClientSession() as websession:
        api = AnkerSolixApi(user, password, os.environ.get("ANKERCOUNTRY", "DE"), websession,
                            logging.getLogger("anker_solix_api"))
        await api.async_authenticate()
        device = await find_device(api, os.environ.get("ANKER_DEVICE_SN"))
        sn = device["device_sn"]
        log.info("Monitoring %s (%s)", sn, device.get("device_pn"))
        watchdog.name = device.get("alias") or device.get("name") or f"Anker {device.get('device_pn')}"

        poller = await start_poller(api, device)
        log.info("MQTT connected, waiting for telemetry...")

        last_message, last_change = None, time.monotonic()
        last_levels: dict[str, int] | None = None
        last_charge = ""
        expansion: int | None = None  # pack count, None until the first telemetry
        disconnected_since: float | None = None
        last_restart = last_retrigger = time.monotonic()
        link_alerted = False
        snapshot: dict = {}

        def status_text() -> str:
            if not snapshot:
                return "⏳ No data from Anker yet"
            age = int(time.monotonic() - snapshot["at"])
            grid = {True: "down, running on battery", False: "up"}.get(watchdog.on_battery, "?")
            lines = [f"📊 {watchdog.name}", f"Grid: {grid}", f"Charge: {snapshot['charge']}"]
            if watchdog.on_battery:
                lines.append(f"Outage: {watchdog.outage_duration()}")
                if snapshot.get("remaining_h"):
                    lines.append(f"Remaining: ~{fmt_duration(snapshot['remaining_h'] * 3600)}")
            lines += [f"Input: {snapshot['ac_in']} W, load: {snapshot['out']} W",
                      f"Last data: {age} s ago"]
            return "\n".join(lines)

        mqtt_device = SolixMqttDeviceFactory(api_instance=api, device_sn=sn).create_device()
        loop = asyncio.get_running_loop()
        light_keys = list(LIGHT_MODES)  # index matches the device value 0..4

        def light_state() -> str:
            session_data = api.mqttsession.mqtt_data.get(sn, {}) if api.mqttsession else {}
            value = as_int(session_data.get("light_mode"))
            return light_keys[value] if value is not None and 0 <= value < len(light_keys) else ""

        def set_light(mode: str) -> str:
            # called from the Telegram thread; the MQTT command must run on the asyncio loop
            future = asyncio.run_coroutine_threadsafe(mqtt_device.set_light(mode=mode), loop)
            try:
                ok = future.result(timeout=15) is not None
            except Exception:
                log.exception("set_light(%s) failed", mode)
                ok = False
            log.info("Light set to %s: %s", mode, "ok" if ok else "failed")
            return f"Light: {LIGHT_MODES[mode]}" if ok else "⚠️ Light command failed"

        tg.listen_commands(status_text, light_state, set_light)
        while True:
            await asyncio.sleep(1)
            session = api.mqttsession
            now = time.monotonic()

            if not (session and session.is_connected()) or poller.done():
                disconnected_since = disconnected_since or now
            else:
                disconnected_since = None
            silent = now - last_change
            if last_message and not link_alerted and silent > args.link_timeout:
                link_alerted = True
                tg.send(f"⚠️ No data from Anker for {int(silent)} s")
            # The device stops streaming after some state changes (e.g. grid coming back),
            # so ask for realtime data again as soon as the usual 3-5 s cadence breaks.
            if (session and session.is_connected() and silent > args.retrigger
                    and now - last_retrigger > args.retrigger):
                last_retrigger = now
                log.info("No data for %d s, re-sending realtime trigger", silent)
                with contextlib.suppress(Exception):
                    session.realtime_trigger(deviceDict=device, timeout=REALTIME_TIMEOUT)
            # restart on a dead connection, or on a live one that stays silent
            if (now - last_restart > 60
                    and ((disconnected_since and now - disconnected_since > 60) or silent > 60)):
                last_restart = now
                log.warning("MQTT %s, restarting session",
                            "disconnected" if disconnected_since else "silent")
                try:
                    poller = await restart_poller(api, device, poller)
                    session = api.mqttsession
                    log.info("MQTT session restarted")
                except Exception as exc:
                    log.warning("MQTT restart failed: %s", exc)

            data = (session.mqtt_data.get(sn) if session else None) or {}
            if data.get("last_message") != last_message:
                last_message, last_change = data.get("last_message"), now
                levels, packs, charge = battery_state(data)
                ac_in = as_int(data.get("ac_input_power", data.get("ac_input_power_total")))
                charging = as_int(data.get("charging_status"))
                remaining_h = data.get("remaining_time_hours")
                if levels:
                    snapshot.update(at=now, charge=charge, ac_in=ac_in, remaining_h=remaining_h,
                                    out=as_int(data.get("output_power_total")))
                log.debug("ac_in=%sW charging_status=%s out=%sW in_total=%sW battery=%s",
                          ac_in, charging, data.get("output_power_total"),
                          data.get("photovoltaic_power"), charge or None)
                if levels and packs != expansion:
                    if expansion is not None and packs > expansion:
                        tg.send(f"🔋 Expansion battery connected, charge {charge}")
                    elif expansion is not None:
                        tg.send(f"🔋 Expansion battery disconnected, charge {charge}")
                    expansion = packs
                if link_alerted:
                    tg.send("📶 Anker data is flowing again")
                    link_alerted = False
                if levels and ac_in is not None:
                    last_levels, last_charge = levels, charge
                    # charging_status (where reported): 0 idle, 1 solar, 2 AC, 3 both
                    grid = ac_in > 0 or charging in (2, 3)
                    watchdog.evaluate(grid, ac_in, levels, charge, remaining_h)
                    continue
            # keep re-evaluating so the outage confirm timer runs between messages
            if watchdog.no_grid_since is not None and not watchdog.on_battery and last_levels:
                watchdog.evaluate(False, 0, last_levels, last_charge)


def main() -> None:
    load_env()
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="do not touch the power plan")
    p.add_argument("--confirm", type=float, default=10, help="seconds without AC input before outage (10)")
    p.add_argument("--thresholds", type=int, nargs="+", default=[75, 50, 25])
    p.add_argument("--link-timeout", type=float, default=120, help="seconds without telemetry before alert")
    p.add_argument("--retrigger", type=float, default=15,
                   help="seconds without telemetry before re-requesting realtime data (15)")
    p.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="print every telemetry update")
    args = p.parse_args()
    # a windowless exe has no stderr, so always keep a rotating log next to it
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        base_dir() / "anker_ups.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    for noisy in ("anker_solix_api", "paho", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
