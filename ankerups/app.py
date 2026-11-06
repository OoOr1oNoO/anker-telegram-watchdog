"""Anker SOLIX power station watchdog via Anker cloud MQTT.

- Telegram alert when AC input is lost / restored, with durations, and a full-charge alert.
- Charge alerts per battery (default 75 / 50 / 25 %) and a critical charge alert.
- Bot buttons: Status, Light, Stats (charts, outages, energy, months), Settings
  (notifications, station controls, users and access, language).
- Messages in English, Ukrainian, Russian, Polish, German or Spanish, per chat.
- Daily summary; samples, outages, users and settings are kept in SQLite.
- Switches Windows to the Power saver plan on outage, restores the previous plan after.

Config: environment variables or a .env file next to the script/exe (see .env.example).
"""

import argparse
import asyncio
import contextlib
import logging
import logging.handlers
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from aiohttp import ClientSession
from anker_solix_api.api import AnkerSolixApi
from anker_solix_api.mqtt_factory import SolixMqttDeviceFactory
from anker_solix_api.mqtt_pps import MODELS as PPS_MODELS

from .bot import BotHandlers, Telegram
from .common import (
    HEALTHCHECK_EVERY,
    REALTIME_TIMEOUT,
    as_int,
    data_dir,
    id_list,
    load_env,
    log,
)
from .i18n import Lang, lang
from .menus import Controller
from .stats import Settings, Stats, chart_title
from .users import Users
from .watchdog import PowerPlan, Watchdog, battery_state


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


def send_daily_summary(tg: Telegram, stats: Stats | None, at: str) -> None:
    """Once a day after the summary time (HH:MM, local), send the last 24 h summary and chart."""
    if not (tg.enabled and stats and at):
        return
    now = datetime.now()
    if now.strftime("%H:%M") < at or stats.meta("last_summary") == now.strftime("%Y-%m-%d"):
        return
    stats.meta("last_summary", now.strftime("%Y-%m-%d"))
    tg.send(lambda L: stats.summary_text(now, L))

    def chart(L: Lang) -> bytes | None:
        with contextlib.suppress(Exception):
            return stats.chart_png(24, L, end=now.timestamp())
        return None

    tg.send_photo(chart, lambda L: "📈 " + chart_title(L, 24))


def ping_healthcheck(url: str) -> None:
    """Dead man's switch (e.g. healthchecks.io): the service alerts when the pings stop."""
    def ping() -> None:
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                resp.read()
        except Exception as exc:
            log.warning("Healthcheck ping failed: %s", exc)

    threading.Thread(target=ping, daemon=True).start()


async def run(args: argparse.Namespace) -> None:
    env_chats, env_admins = id_list(os.environ.get("TG_CHAT_ID")), id_list(os.environ.get("TG_ADMIN_IDS"))
    default_lang = lang(os.environ.get("BOT_LANGUAGE") or "en")
    tg = Telegram(os.environ.get("TG_BOT_TOKEN"), bool(env_chats or env_admins), default_lang)
    power = PowerPlan(dry_run=args.dry_run)
    power.log_plans()
    # statistics, users and bot settings only matter with Telegram; a PC that just switches
    # power plans (empty TG_BOT_TOKEN) runs without the database
    stats = Stats(data_dir() / "anker_ups.db") if tg.enabled else None
    users = Users(stats, env_chats, env_admins, default_lang) if stats else None
    tg.users = users
    settings = Settings(stats, {
        "thresholds": ",".join(str(t) for t in args.thresholds),
        "critical": os.environ.get("CRITICAL_LEVEL") or "10",
        "summary": os.environ.get("SUMMARY_TIME", "09:00").strip(),
        "alert_outage": "1", "alert_full": "1", "alert_expansion": "1", "alert_link": "1",
    })
    watchdog = Watchdog(tg, power, confirm_s=args.confirm, settings=settings, stats=stats,
                        full_confirm_s=args.full_confirm)
    healthcheck = os.environ.get("HEALTHCHECK_URL", "").strip()

    if args.test_telegram:
        tg.send(lambda L: L.t("test_msg"))
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
        last_load: int | None = None
        expansion: int | None = None  # pack count, None until the first telemetry
        disconnected_since: float | None = None
        last_restart = last_retrigger = time.monotonic()
        last_ping = 0.0
        link_alerted = False
        snapshot: dict = {}
        last_plug: int | None = None

        def status_text(L: Lang) -> str:
            if not snapshot:
                return L.t("no_data_yet")
            age = int(time.monotonic() - snapshot["at"])
            grid = {True: L.t("grid_down"), False: L.t("grid_up")}.get(watchdog.on_battery, "?")
            lines = [f"📊 {watchdog.name}", L.t("st_grid", state=grid), L.t("st_charge", charge=L.charge(snapshot["levels"]))]
            if watchdog.on_battery:
                off = watchdog.outage_seconds()
                lines.append(L.t("st_outage", dur=L.dur(off) if off else "?"))
                if snapshot.get("remaining_h"):
                    lines.append(L.t("st_remaining", dur=L.dur(snapshot["remaining_h"] * 3600)))
            lines += [L.t("st_power", ac_in=snapshot["ac_in"], out=snapshot["out"]), L.t("st_age", age=age)]
            return "\n".join(lines)

        mqtt_device = SolixMqttDeviceFactory(api_instance=api, device_sn=sn).create_device()
        loop = asyncio.get_running_loop()

        def run_on_loop(coro):
            # called from the Telegram thread; MQTT commands must run on the asyncio loop
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=15)

        controller = Controller(
            settings, users, lambda: api.mqttsession.mqtt_data.get(sn, {}) if api.mqttsession else {},
            run_on_loop, mqtt_device)
        tg.listen_commands(BotHandlers(status_text, controller, stats))
        while True:
            await asyncio.sleep(1)
            session = api.mqttsession
            now = time.monotonic()
            send_daily_summary(tg, stats, settings.summary)
            if healthcheck and now - last_ping >= HEALTHCHECK_EVERY:
                last_ping = now
                ping_healthcheck(healthcheck)

            if not (session and session.is_connected()) or poller.done():
                disconnected_since = disconnected_since or now
            else:
                disconnected_since = None
            silent = now - last_change
            if last_message and not link_alerted and silent > args.link_timeout:
                link_alerted = True
                if settings.flag("alert_link"):
                    seconds = int(silent)
                    tg.send(lambda L: L.t("al_nodata", s=seconds))
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
                levels, packs = battery_state(data)
                ac_in = as_int(data.get("ac_input_power", data.get("ac_input_power_total")))
                charging = as_int(data.get("charging_status"))
                remaining_h = data.get("remaining_time_hours")
                if levels:
                    snapshot.update(at=now, levels=levels, ac_in=ac_in, remaining_h=remaining_h,
                                    out=as_int(data.get("output_power_total")))
                log.debug("ac_in=%sW charging_status=%s out=%sW in_total=%sW battery=%s",
                          ac_in, charging, data.get("output_power_total"),
                          data.get("photovoltaic_power"), levels)
                if levels and packs != expansion:
                    if expansion is not None and settings.flag("alert_expansion"):
                        key = "al_exp_on" if packs > expansion else "al_exp_off"
                        tg.send(lambda L, key=key, lv=levels: L.t(key, charge=L.charge(lv)))
                    expansion = packs
                if link_alerted:
                    if settings.flag("alert_link"):
                        tg.send(lambda L: L.t("al_data_back"))
                    link_alerted = False
                if levels and ac_in is not None:
                    last_levels = levels
                    # charging_status (where reported): 0 idle, 1 solar, 2 AC, 3 both.
                    # ac_input_plug_status (C1000 Gen 2, C2000 Gen 2, C300...): 1 = AC connected;
                    # these models have no charging_status, and with a full battery and a small
                    # load the AC input drops to 0 W while the grid is up
                    plug = as_int(data.get("ac_input_plug_status"))
                    if plug != last_plug:
                        log.info("ac_input_plug_status %s -> %s (ac_in %s W)", last_plug, plug, ac_in)
                        last_plug = plug
                    grid = ac_in > 0 or charging in (2, 3) or plug == 1
                    if stats:
                        stats.add_reading(ac_in, as_int(data.get("output_power_total")) or 0,
                                          round(sum(levels.values()) / len(levels)), grid)
                    last_load = as_int(data.get("output_power_total"))
                    watchdog.evaluate(grid, ac_in, levels, remaining_h, last_load)
                    continue
            # keep re-evaluating so the outage confirm timer runs between messages
            if watchdog.no_grid_since is not None and not watchdog.on_battery and last_levels:
                watchdog.evaluate(False, 0, last_levels, load=last_load)


def apply_timezone() -> None:
    """Use TIMEZONE from .env (e.g. Europe/Kyiv) for message and log timestamps.

    Servers usually run in UTC; Windows keeps the PC's own time zone (no tzset there).
    """
    tz = os.environ.get("TIMEZONE", "").strip()
    if tz and hasattr(time, "tzset"):
        os.environ["TZ"] = tz
        time.tzset()


def main() -> None:
    load_env()
    apply_timezone()
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="do not touch the power plan")
    p.add_argument("--confirm", type=float, default=10, help="seconds without AC input before outage (10)")
    p.add_argument("--full-confirm", type=float, default=180,
                   help="the same with a full battery and no load, when the input pulses (180)")
    p.add_argument("--thresholds", type=int, nargs="+", default=[75, 50, 25])
    p.add_argument("--link-timeout", type=float, default=120, help="seconds without telemetry before alert")
    p.add_argument("--retrigger", type=float, default=15,
                   help="seconds without telemetry before re-requesting realtime data (15)")
    p.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="print every telemetry update")
    args = p.parse_args()
    # a windowless exe has no stderr, so always keep a rotating log next to it
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        data_dir() / "anker_ups.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
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
