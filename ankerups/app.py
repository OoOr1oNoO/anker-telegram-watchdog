"""Anker SOLIX power station watchdog via Anker cloud MQTT.

- Telegram alert when AC input is lost / restored, with durations, and a full-charge alert.
- Charge alerts per battery (default 75 / 50 / 25 %) and a critical charge alert.
- Planned outage schedule (Kyiv, Kyiv region) with alerts; the status as a live Telegram widget.
- Bot buttons: Status, Light / 12V, Stats (charts, outages, energy, months), Settings
  (notifications, station controls, users and access, language).
- Messages in English, Ukrainian, Russian, Polish, German or Spanish, per chat.
- Daily summary; samples, outages, users and settings are kept in SQLite.
- Switches Windows to the Power saver plan on outage, restores the previous plan after.

Config: environment variables or a .env file next to the script/exe (see .env.example).
"""

import argparse
import asyncio
import collections
import contextlib
import json
import logging
import logging.handlers
import os
import sys
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import ClientSession
from anker_solix_api.api import AnkerSolixApi
from anker_solix_api.mqtt_factory import SolixMqttDeviceFactory
from anker_solix_api.mqtt_pps import MODELS as PPS_MODELS

from .bot import BotHandlers, Telegram, card
from .common import (
    FULL_SOC,
    HEALTHCHECK_EVERY,
    IDLE_LOAD_W,
    IS_WINDOWS,
    REALTIME_TIMEOUT,
    as_int,
    data_dir,
    id_list,
    load_env,
    log,
)
from .i18n import Lang, lang
from .translations import LANGS
from .menus import STATION_CONTROLS, Controller, Ctx, in_window
from .plug import Plug
from .tv import Tv
from .api import Api
from .stats import Settings, Stats, chart_title, merge_slot
from .users import Users
from .schedmenu import ScheduleMenu, alert_note, day_text
from .schedule import Day, Schedule, hhmm, run_schedule, windows, windows_text
from .watchdog import LoadPeaks, PowerPlan, Watchdog, battery_state
from .extras import Extras
from .widget import MAX_OUTPUT, widget_data


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
    tg.send(lambda L: stats.summary_text(now, L), rich=lambda L: stats.summary_card(now, L),
            thread="daily", end="daily")

    def chart(L: Lang) -> bytes | None:
        with contextlib.suppress(Exception):
            return stats.chart_png(24, L, end=now.timestamp())
        return None

    tg.send_photo(chart, lambda L: "📈 " + chart_title(L, 24))


def charge_hint(stats: Stats, snapshot: dict, limit: int | None, limits: list[int],
                start: float) -> tuple[Callable[[Lang], str], Callable[[Lang], str] | None]:
    """Before a planned outage: the charge it will have by then at the current input, and the AC
    input limit that would fill it in time (a button; fast charging is left alone)."""
    levels = snapshot.get("levels") or {}
    if not levels:
        return (lambda L: ""), None
    soc = round(sum(levels.values()) / len(levels))
    if soc >= 99:
        return (lambda L: "\n" + L.t("ch_full")), None
    hours = max(0.1, (start - time.time()) / 3600)
    per_pct = stats.wh_per_percent()
    ac_in, out = snapshot.get("ac_in") or 0, snapshot.get("out") or 0
    if not per_pct:
        return (lambda L: "\n" + L.t("ch_unknown", w=ac_in)), None
    reach = min(100, round(soc + (ac_in - out) * hours / per_pct))
    need = round((100 - soc) * per_pct / hours + out)
    if reach >= 100:
        return (lambda L: "\n" + L.t("ch_reach", v=reach)), None
    best = next((w for w in sorted(limits) if w >= need), None)
    if best is None:
        return (lambda L: "\n" + L.t("ch_reach", v=reach) + "\n" + L.t("ch_too_much", w=need)), None
    text = (lambda L: "\n" + L.t("ch_reach", v=reach) + "\n" + L.t("ch_need", w=need, limit=limit or "?"))
    if limit is not None and limit >= best:
        return text, None  # the limit allows it: the station draws what it can
    return text, (lambda L: json.dumps({"inline_keyboard": [[
        {"text": L.t("ch_button", w=best), "callback_data": f"a:limit:{best}@alert", "style": "primary"}]]}))


def start_schedule(tg: Telegram, schedule: Schedule, settings: Settings,
                   levels: Callable[[], dict[str, int] | None],
                   hint: Callable[[float], tuple[Callable, Callable | None]] | None = None) -> None:
    """Outage schedule thread: downloads it and sends the schedule alerts."""
    def on_event(kind: str, day: Day) -> None:
        flag = "alert_sched_tomorrow" if kind == "tomorrow" else "alert_sched_change"
        if not settings.flag(flag):
            return
        group = schedule.group

        def text(L: Lang) -> str:
            v = L.t("sc_outages", v=windows_text(day.slots)) if "0" in day.slots else L.t("sc_no_off")
            day_name = L.t("w_today" if day.date == time.strftime("%Y-%m-%d") else "w_tomorrow").lower()
            if kind == "tomorrow":
                return L.t("al_sched_tomorrow", group=group, v=v)
            if kind == "emergency":
                return L.t("al_sched_emergency", group=group)
            if kind == "restored":
                return L.t("al_sched_restored", day=day_name, group=group, v=v)
            return L.t("al_sched_change", day=day_name, group=group, v=v)

        def rich(L: Lang) -> str:  # the title, then the outages as a table: the window and how long
            title, _, rest = text(L).partition(": ")
            if kind == "emergency":
                return card(title, note=rest)
            rows = [(f"{mark} {hhmm(a)}–{hhmm(b)}", L.dur((b - a) * 60))
                    for state, mark in (("0", "🔴"), ("?", "🟡")) for a, b in windows(day.slots, state)]
            rows.sort(key=lambda r: r[0][2:])
            off = sum((b - a) * 60 for a, b in windows(day.slots))
            if not rows:
                return card(title, note=L.t("sc_no_off"))
            on = sum((b - a) * 60 for a, b in windows(day.slots, "1"))
            return card(title, rows + [("⏱ " + L.t("t_off"), L.dur(off)), ("⚡ " + L.t("t_on"), L.dur(on))])
        tg.send(text, rich=rich, thread="sched", end="sched")  # the newest schedule news only

    def on_soon(start: float, end: float) -> None:
        if not settings.flag("alert_sched_soon"):
            return
        lv = levels()
        mins = max(1, round((start - time.time()) / 60))
        at = time.strftime("%H:%M", time.localtime(start))
        until = time.strftime("%H:%M", time.localtime(end)) if end - start < 86400 else "?"
        extra, markup = hint(start) if hint else ((lambda L: ""), None)
        tg.send(lambda L: L.t("al_sched_soon", start=at, mins=mins, end=until,
                              charge=L.charge(lv) if lv else "?") + extra(L), markup, thread="outage",
                rich=lambda L: card(L.t("c_soon", mins=mins), [("🕐 " + L.t("r_window"), f"{at} – {until}"),
                                                               ("🔋 " + L.t("t_charge"), L.charge(lv) if lv else "?")],
                                    extra(L)))

    threading.Thread(target=run_schedule, args=(schedule, on_event, on_soon), daemon=True).start()


def autolight_step(watchdog: Watchdog, controller: Controller, settings: Settings, tg: Telegram,
                   meta: Callable, now: datetime) -> None:
    """Auto light: DC 12V (a USB lamp in a 12V adapter) on while an outage falls in the chosen
    hours, off again after it, but only if the bot turned it on (meta "autolight": on / skip)."""
    window = settings.get("autolight")  # an invalid one (e.g. 24 h from a test build) counts as off
    want = bool(window and watchdog.on_battery and controller._supported("dc")
                and in_window(window, now.hour * 60 + now.minute))
    state = meta("autolight") or ""
    ctx = Ctx(tg.default, True, "auto", "auto light")
    if want and not state:
        if controller.widget_controls()[1] == 1:
            meta("autolight", "skip")  # already on: leave it to whoever turned it on
            return
        if controller.act("dc", "1", ctx)[0] != tg.default.t("cmd_failed"):
            meta("autolight", "on")
            tg.send(lambda L: L.t("al_auto_on"), thread="outage", rich=lambda L: card(L.t("al_auto_on")))
    elif not want and state:
        meta("autolight", "")
        if state == "on" and controller.widget_controls()[1] == 1:
            controller.act("dc", "0", ctx)
            tg.send(lambda L: L.t("al_auto_off"), thread="outage", rich=lambda L: card(L.t("al_auto_off")))


def run_autolight(watchdog: Watchdog, controller: Controller, settings: Settings, tg: Telegram,
                  stats: Stats | None) -> None:
    store: dict[str, str] = {}

    def meta(key: str, value: str | None = None) -> str | None:
        if stats:
            return stats.meta(key, value)
        if value is not None:
            store[key] = value
        return store.get(key)
    while True:
        time.sleep(30)
        try:
            autolight_step(watchdog, controller, settings, tg, meta, datetime.now())
        except Exception:
            log.exception("Auto light failed")


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
        "sched_source": "", "sched_group": "", "sched_label": "",
        "alert_sched_soon": "1", "alert_sched_tomorrow": "1", "alert_sched_change": "1",
        "autolight": "", "tariff": "4.32", "peak_mode": "both", "peak_w": "1000",
    })
    schedule = Schedule(settings, stats) if stats else None
    watchdog = Watchdog(tg, power, confirm_s=args.confirm, settings=settings, stats=stats,
                        full_confirm_s=args.full_confirm)
    watchdog.note = lambda L, kind, ts: alert_note(schedule, L, kind, ts)
    peaks = LoadPeaks(tg, settings)
    extras = Extras(stats) if stats else None  # weather and air-raid alerts for the widgets and stats
    if extras:
        extras.start()
        tg.temps = lambda: extras.daily
    healthcheck = os.environ.get("HEALTHCHECK_URL", "").strip()

    if args.test_telegram:
        tg.send(lambda L: L.t("test_msg"))
        await asyncio.sleep(5)
        return

    user, password = os.environ.get("ANKERUSER"), os.environ.get("ANKERPASSWORD")
    if not (user and password):
        raise SystemExit("ANKERUSER / ANKERPASSWORD are not set: put them into .env next to the program")

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
        last_flag: bool | None = None
        runtime = RuntimeGrid()
        last_draw_log = 0.0

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
            grid_now = None if watchdog.on_battery is None else not watchdog.on_battery
            if schedule and schedule.configured and (change := schedule.next_change(time.time(), grid_now)):
                at = time.strftime("%H:%M", time.localtime(change[0]))
                lines.append(f"📅 {L.t('w_on_at' if change[1] == '1' else 'w_off_at')} {at}")
            return "\n".join(lines)

        def widget_info() -> tuple[str, str] | None:
            """Station name and the status widget's data; None until the first telemetry."""
            if not snapshot or not stats:
                return None
            now_wall = time.time()
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            grid = None if watchdog.on_battery is None else not watchdog.on_battery
            since = watchdog.outage_started if watchdog.on_battery else stats.last_outage_end()
            fact = stats.grid_slots(today.timestamp(), now_wall)
            if grid is not None:  # the current half hour: the state right now counts too
                fact = fact[:-1] + merge_slot(fact[-1:] or "-", grid)
            plan_today = plan_tomorrow = group = ""
            change, emergency = None, False
            if schedule and schedule.configured:
                d_today, d_tomorrow = schedule.day(today), schedule.day(today + timedelta(days=1))
                plan_today = d_today.slots if d_today else ""
                emergency = bool(d_today and d_today.status == "emergency")
                plan_tomorrow = d_tomorrow.slots if d_tomorrow else ""
                group, change = schedule.group, schedule.next_change(now_wall, grid)
            data = widget_data(now_wall, snapshot.get("wall"), grid, since, snapshot["levels"],
                               snapshot["ac_in"], snapshot["out"],
                               snapshot.get("remaining_h") if watchdog.on_battery else None,
                               fact, plan_today, plan_tomorrow, change, group,
                               datetime.now().hour * 60 + datetime.now().minute, emergency,
                               *controller.widget_controls(), MAX_OUTPUT.get(device.get("device_pn")),
                               (extras.widget() if extras else {}) | plug_extra())
            return watchdog.name, data

        def status_card(L: Lang) -> str | None:
            """📊 Status as a rich message with everything the classic widget shows: the grid and for how
            long, the batteries, the load, the plug, the light / 12V, weather, the air-raid alert, the
            schedule with its next change and the outage windows (actual, today, tomorrow)."""
            try:
                return _status_card(L) if snapshot else None
            except Exception:  # the text status then
                log.exception("Status card failed")
                return None

        def _status_card(L: Lang) -> str:
            now = time.time()
            w = (L.t("pl_power", w="", v="").split() + ["W", "·", "V"])[1:4:2]  # the watt / volt units
            grid = None if watchdog.on_battery is None else not watchdog.on_battery
            since = watchdog.outage_started if watchdog.on_battery else (stats.last_outage_end() if stats else None)
            levels = snapshot["levels"]
            avg = round(sum(levels.values()) / len(levels)) if levels else None
            title = f"📊 {watchdog.name}: " + {True: L.t("w_on"), False: L.t("w_off")}.get(grid, "?")
            rows = []
            if since:
                rows.append(("⏱ " + (L.t("st_outage", dur="").split(":")[0] if watchdog.on_battery
                                     else L.t("st_grid", state="").split(":")[0]), L.dur(now - since)))
            charge = L.charge(levels)
            if watchdog.on_battery and snapshot.get("remaining_h"):
                charge += " · ~" + L.dur(snapshot["remaining_h"] * 3600)
            rows.append(("🔋 " + L.t("t_charge"), charge))
            rows.append(("⚡ " + L.t("r_load"), f"{snapshot['out'] or 0} {w[0]} · {L.t('w_input')} "
                                                f"{snapshot['ac_in'] or 0} {w[0]}"))
            plug = plug_extra()
            if plug:
                v = {1: L.t("on"), 0: L.t("off")}.get(plug.get("ps"), "?")
                if "pw" in plug:
                    v += f" · {plug['pw']} {w[0]} · {plug['pv']} {w[1]}"
                rows.append(("🔌 " + L.t("plug"), v))
            light, dc = controller.widget_controls()
            if light is not None:
                rows.append(tuple(L.t("light_title", v=controller._label("light", L)).split(": ", 1)))
            if dc is not None:
                rows.append(("🔌 12V", controller._label("dc", L)))
            if extras and extras.temp is not None:
                rows.append(("🌡", f"{round(extras.temp):+d}°" + (f" · AQI {extras.aqi}" if extras.aqi is not None else "")))
            if extras and extras.alert:
                rows.append(("⚠️ " + L.t("w_alert_near") if extras.alert == "n" else "🚨 " + L.t("w_alert"),
                             L.dur(now - (extras.alert_since or now))))
            if schedule and schedule.configured:
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                d_today, d_tomorrow = schedule.day(today), schedule.day(today + timedelta(days=1))
                emergency = bool(d_today and d_today.status == "emergency")
                v = L.t("w_emergency") if emergency else ""
                if change := schedule.next_change(now, grid):
                    nx = (f"{L.t('w_on_at' if change[1] == '1' else 'w_off_at')} "
                          f"{time.strftime('%H:%M', time.localtime(change[0]))} · {L.dur(change[0] - now)}")
                    if watchdog.on_battery and change[1] == "1" and snapshot.get("remaining_h") and avg is not None:
                        left, need = snapshot["remaining_h"] * 3600, change[0] - now
                        nx += f" · ✓ ≈{round(avg * (1 - need / left))}%" if need < left else " · ✗"
                    v = f"{v} · {nx}" if v else nx
                rows.append(("📅 " + L.t("w_group") + " " + schedule.group, v or "—"))
                if stats:
                    fact = stats.grid_slots(today.timestamp(), now)
                    if grid is not None:
                        fact = fact[:-1] + merge_slot(fact[-1:] or "-", grid)
                    rows.append(("🔴 " + L.t("w_fact"), windows_text(fact.replace("p", "0")) or "—"))
                rows.append(("📅 " + L.t("w_today"), day_text(L, d_today)))
                if d_tomorrow:
                    rows.append(("📅 " + L.t("w_tomorrow"), day_text(L, d_tomorrow)))
            note = L.t("st_age", age=int(time.monotonic() - snapshot["at"]))
            return card(title, rows, note)

        def plug_extra() -> dict:  # the Tuya plug for the widgets: load "pw", voltage "pv", switch "ps"
            if not controller.plug:
                return {}
            load, st = controller.plug.load(), controller.plug.status(max_age=60) or {}
            out = {} if load is None else {"pw": load[0], "pv": load[1]}
            if st.get("online") and "switch_1" in st:
                out["ps"] = int(bool(st["switch_1"]))
            return out

        mqtt_device = SolixMqttDeviceFactory(api_instance=api, device_sn=sn).create_device()
        loop = asyncio.get_running_loop()

        def run_on_loop(coro):
            # called from the Telegram thread; MQTT commands must run on the asyncio loop
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=15)

        controller = Controller(
            settings, users, lambda: api.mqttsession.mqtt_data.get(sn, {}) if api.mqttsession else {},
            run_on_loop, mqtt_device)
        controller.plug = Plug.from_env(os.environ)  # optional Tuya smart plug
        if controller.plug:
            log.info("Tuya plug %s…", controller.plug.device[:6])
        if controller.plug and stats:
            watch_voltage(controller.plug, stats)
        controller.tv = Tv.from_env(os.environ, data_dir())  # optional Android TV over ADB
        if controller.tv:
            controller.tv.start_timer(settings)
            log.info("TV over ADB at %s:%s", controller.tv.host, controller.tv.port)
        handlers = BotHandlers(status_text, controller, stats, card=status_card)
        siri = Api.from_env(os.environ, api_routes(controller, watchdog, snapshot, tg, schedule, extras))  # `api` is Anker's
        if siri:
            siri.start()
        if schedule:
            handlers.sched = controller.sched = ScheduleMenu(schedule)
            handlers.widget, controller.widgets = widget_info, True
            handlers.quick = controller.quick_data
            def hint(start: float):
                limit = as_int(controller._state("limit")) if controller._supported("limit") else None
                limits = [int(v) for v in STATION_CONTROLS["limit"][1]] if limit is not None else []
                return charge_hint(stats, snapshot, limit, limits, start)
            start_schedule(tg, schedule, settings, lambda: last_levels, hint)
        threading.Thread(target=run_autolight, args=(watchdog, controller, settings, tg, stats),
                         daemon=True).start()
        tg.listen_commands(handlers)
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
                    tg.send(lambda L: L.t("al_nodata", s=seconds), thread="data",
                            rich=lambda L: card(L.t("al_nodata", s=seconds)))
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
                if not isinstance(remaining_h, (int, float)) or not 0 < remaining_h < NO_VALUE_HOURS:
                    remaining_h = None
                if levels:
                    snapshot.update(at=now, wall=time.time(), levels=levels, ac_in=ac_in, remaining_h=remaining_h,
                                    out=as_int(data.get("output_power_total")))
                log.debug("ac_in=%sW charging_status=%s out=%sW in_total=%sW battery=%s",
                          ac_in, charging, data.get("output_power_total"),
                          data.get("photovoltaic_power"), levels)
                if levels and packs != expansion:
                    if expansion is not None and settings.flag("alert_expansion"):
                        key = "al_exp_on" if packs > expansion else "al_exp_off"
                        tg.send(lambda L, key=key, lv=levels: L.t(key, charge=L.charge(lv)), thread="exp", end="exp",
                                rich=lambda L, key=key, lv=levels: card(L.t(key, charge=L.charge(lv))))
                    expansion = packs
                if link_alerted:
                    if settings.flag("alert_link"):
                        tg.send(lambda L: L.t("al_data_back"), thread="data", end="data", rich=lambda L: card(L.t("al_data_back")))
                    link_alerted = False
                if levels and ac_in is not None:
                    last_levels = levels
                    # charging_status (where reported): 0 idle, 1 solar, 2 AC, 3 both.
                    # ac_input_plug_status (C1000 Gen 2, C2000 Gen 2, C300...): 1 = AC connected;
                    # these models have no charging_status, and with a full battery and a small
                    # load the AC input drops to 0 W while the grid is up
                    on_grid_flag = runtime.update(now, data.get("remaining_time_hours"),
                                                  as_int(data.get("output_power_total")), ac_in, levels)
                    if runtime.extra is not None and now - last_draw_log >= 60:  # to check SELF_DRAW_W
                        log.debug("own draw %.1f W (remaining %s h, out %s W, ac_in %s W)", runtime.extra,
                                 data.get("remaining_time_hours"), data.get("output_power_total"), ac_in)
                        last_draw_log = now
                    if on_grid_flag != last_flag:
                        log.info("remaining time says %s (own draw %s W, remaining %s h, ac_in %s W)",
                                 "grid" if on_grid_flag else "battery" if on_grid_flag is False else "nothing",
                                 None if runtime.extra is None else round(runtime.extra, 1),
                                 data.get("remaining_time_hours"), ac_in)
                        last_flag = on_grid_flag
                    # the remaining time tells the grid apart too (see RuntimeGrid), but only while
                    # it is sent: with the AC output off the station sends none, then the wait is long again
                    plug = as_int(data.get("ac_input_plug_status"))
                    watchdog.plug_known = plug is not None or on_grid_flag is not None
                    if plug != last_plug:
                        log.info("ac_input_plug_status %s -> %s (ac_in %s W)", last_plug, plug, ac_in)
                        last_plug = plug
                    grid = ac_in > 0 or charging in (2, 3) or plug == 1 or on_grid_flag is True
                    if stats:
                        stats.add_reading(ac_in, as_int(data.get("output_power_total")) or 0,
                                          round(sum(levels.values()) / len(levels)), grid)
                    last_load = as_int(data.get("output_power_total"))
                    watchdog.evaluate(grid, ac_in, levels, remaining_h, last_load)
                    peaks.update(last_load)
                    continue
            # keep re-evaluating so the outage confirm timer runs between messages
            if watchdog.no_grid_since is not None and not watchdog.on_battery and last_levels:
                watchdog.evaluate(False, 0, last_levels, load=last_load)


PACK_WH = 1056  # Wh of a C1000(X) and of its expansion battery
SELF_DRAW_W = 10  # W the battery spends beyond the load: on the battery 22-31 at a small load, on the grid 0-5
VOLT_LOW, VOLT_HIGH, VOLT_JUMP = 205, 245, 10  # V: logged when outside these or after a jump


def watch_voltage(plug, stats) -> None:
    """The grid voltage from the plug once a minute into the stats; odd values go to the log too
    (to tell a real drop from a false outage alert afterwards)."""
    def loop() -> None:
        last = None
        while True:
            try:
                load = plug.load()
                if load and load[1]:
                    volts = load[1]
                    stats.add_voltage(volts)
                    if not VOLT_LOW <= volts <= VOLT_HIGH or (last and abs(volts - last) >= VOLT_JUMP):
                        log.info("Grid voltage %s V (was %s V)", volts, last)
                    last = volts
            except Exception as exc:
                log.debug("Voltage reading failed: %s", exc)
            time.sleep(60)
    threading.Thread(target=loop, daemon=True, name="voltage").start()


def api_routes(controller: Controller, watchdog: Watchdog, snapshot: dict, tg: Telegram,
               schedule=None, extras=None) -> dict:
    """The routes of the Siri / Shortcuts API (api.Api): station switches, the plug, the TV, a status
    line to read out. ?lang=uk|ru|en... picks the language (the bot's default otherwise)."""
    def ctx(q: dict) -> Ctx:
        return Ctx(lang(q["lang"]) if q.get("lang") in LANGS else tg.default, True, "api", "api")

    def done(q: dict, ok: bool) -> str:
        return ctx(q).L.t("api_ok" if ok else "api_fail")

    def station(key: str, value: str) -> Callable[[dict], str]:
        return lambda q: done(q, controller.act(key, value, ctx(q))[0].endswith("✓"))

    def status(q: dict) -> str:
        """Everything about the station in a few sentences for Siri to read out."""
        L, now = ctx(q).L, time.time()
        if not snapshot:
            return L.t("no_data_yet")
        levels = snapshot["levels"]
        avg = round(sum(levels.values()) / len(levels))
        parts = [L.t("t_charge") + " " + L.charge(levels)]
        if watchdog.on_battery:
            off = watchdog.outage_seconds()
            line = L.t("api_off", dur=L.dur(off, True)) if off else L.t("grid_down")
            if snapshot.get("remaining_h"):  # compact durations read better than "1 d 16 h (...)"
                line += ", " + L.t("api_left", dur=L.dur(snapshot["remaining_h"] * 3600, True))
            parts.append(line)
        elif watchdog.on_battery is False:
            end = watchdog.stats.last_outage_end() if getattr(watchdog, "stats", None) else None
            parts.append(L.t("api_on_for", dur=L.dur(now - end, True)) if end else L.t("api_on"))
        grid_now = None if watchdog.on_battery is None else not watchdog.on_battery
        if schedule and schedule.configured and (change := schedule.next_change(now, grid_now)):
            at, wait = time.strftime("%H:%M", time.localtime(change[0])), L.dur(change[0] - now, True)
            line = L.t("api_next_on" if change[1] == "1" else "api_next_off", t=at, dur=wait)
            if watchdog.on_battery and change[1] == "1" and snapshot.get("remaining_h"):
                left, need = snapshot["remaining_h"] * 3600, change[0] - now
                line += ", " + (L.t("api_then", pct=round(avg * (1 - need / left))) if need < left else L.t("api_not_enough"))
            parts.append(line)
        parts.append(L.t("api_load", out=snapshot.get("out") or 0, ac_in=snapshot.get("ac_in") or 0))
        if extras and getattr(extras, "alert", "") in ("r", "y", "o"):
            parts.append(L.t("api_alert"))
        return ". ".join(p[0].upper() + p[1:] for p in parts) + "."

    routes = {"status": status, "12v/on": station("dc", "1"), "12v/off": station("dc", "0"),
              "light/on": station("light", "3"), "light/off": station("light", "0")}
    if controller.plug:
        routes |= {"plug/on": lambda q: done(q, controller.plug.send(switch_1=True)),
                   "plug/off": lambda q: done(q, controller.plug.send(switch_1=False))}
    if tv := controller.tv:
        def timer(q: dict) -> str:
            m = q.get("min", "")
            if not m.isdigit() or int(m) > 720:
                return done(q, False)
            controller.settings.set("tv_off_at", str(int(time.time() + int(m) * 60)) if int(m) else "")
            return done(q, True)

        def volume(q: dict) -> str:
            v = q.get("v", "")
            if v in ("up", "down"):
                return done(q, tv.step_volume(25 if v == "up" else -25))
            return done(q, v.isdigit() and tv.set_volume(int(v)))

        def toggle(q: dict) -> str:
            state = tv.state(fresh=True)
            return done(q, state is not None and tv.power(not state))
        routes |= {"tv/on": lambda q: done(q, tv.power(True)), "tv/off": lambda q: done(q, tv.power(False)),
                   "tv/toggle": toggle, "tv/pause": lambda q: done(q, tv.press("play")),
                   "tv/timer": timer, "tv/volume": volume,
                   "tv/backlight": lambda q: done(q, q.get("v", "").isdigit() and tv.set_backlight(int(q["v"])))}
    return routes


NO_VALUE_HOURS = 6000  # remaining_time_hours of 6553.5: "none", the battery is not used


class RuntimeGrid:
    """Tells the grid from the battery by the remaining time the station reports.

    A full C1000(X) with no load takes nothing from the grid for hours, so its input says nothing.
    Its remaining time does: battery energy / remaining time is what the battery spends, and beyond
    the load it powers (the output not covered by the AC input) that is the station's own draw:
    12-55 W on the battery (the inverter), 0-5 W on the grid. The median over a minute, since the
    value jumps from one message to the next. With the AC output off the station sends no remaining
    time: then None (nothing known)."""

    def __init__(self, window: float = 60):
        self.window = window
        self.samples: collections.deque[tuple[float, float]] = collections.deque()
        self.extra: float | None = None  # the median own draw, W

    def update(self, now: float, hours, out: int | None, ac_in: int | None, levels: dict[str, int]) -> bool | None:
        try:
            hours = float(hours)
        except (TypeError, ValueError):
            hours = 0.0
        if hours <= 0 or not levels:
            self.samples.clear()
            self.extra = None
            return None
        energy = PACK_WH * sum(levels.values()) / 100
        drain = 0.0 if hours >= NO_VALUE_HOURS else energy / hours
        self.samples.append((now, drain - max(0, (out or 0) - (ac_in or 0))))
        while self.samples and now - self.samples[0][0] > self.window:
            self.samples.popleft()
        if len(self.samples) < 3 or min(levels.values()) < FULL_SOC or (out or 0) >= IDLE_LOAD_W:
            # only a full station with a small load needs it (else the grid shows in the input), and
            # under a load the estimate is off (the capacity is approximate): not used then
            self.extra = statistics.median(x for _, x in self.samples) if self.samples else None
            return None
        self.extra = statistics.median(x for _, x in self.samples)
        return self.extra < SELF_DRAW_W


def apply_timezone() -> None:
    """Use TIMEZONE from .env (e.g. Europe/Kyiv) for message and log timestamps.

    Servers usually run in UTC; Windows keeps the PC's own time zone (no tzset there).
    """
    tz = os.environ.get("TIMEZONE", "").strip()
    if tz and hasattr(time, "tzset"):
        os.environ["TZ"] = tz
        time.tzset()


def tell(text: str, error: bool = False) -> None:
    """Show a message to whoever started the program: a message box for the windowless exe."""
    if IS_WINDOWS and not sys.stderr:
        import ctypes  # noqa: PLC0415 - Windows only

        icon = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, text, "Anker UPS watchdog", icon | 0x40000)  # MB_TOPMOST
    else:
        print(text, file=sys.stderr if error else sys.stdout)


def fatal(text: str) -> None:
    """Log a startup error and show it; a windowless exe would otherwise just disappear."""
    log.error("Stopped: %s", text)
    tell(f"{text}\n\nLog: {data_dir() / 'anker_ups.log'}", error=True)


AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "AnkerUPS"


def set_autostart(on: bool) -> str:
    """Start the exe at Windows logon (current user, no admin rights): HKCU ... Run registry value."""
    if not IS_WINDOWS:
        raise SystemExit("--autostart is for Windows; on Linux use install_linux.sh (systemd)")
    import winreg  # noqa: PLC0415 - Windows only

    if getattr(sys, "frozen", False):
        command = f'"{sys.executable}"'
    else:  # the script: a windowless pythonw next to python, if there is one
        exe = Path(sys.executable)
        pythonw = exe.with_name("pythonw.exe")
        command = f'"{pythonw if pythonw.exists() else exe}" "{Path(sys.argv[0]).resolve()}"'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, command)
            return f"Autostart at logon is on:\n{command}\n\nTurn it off with --autostart off."
        try:
            winreg.DeleteValue(key, AUTOSTART_NAME)
        except FileNotFoundError:
            pass
        return "Autostart at logon is off."


def main() -> None:
    env_file = load_env()
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
    p.add_argument("--autostart", choices=["on", "off"], nargs="?", const="on",
                   help="Windows: start this exe at logon (on, the default) or stop doing it (off), then exit")
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
    if args.autostart:
        try:
            text = set_autostart(args.autostart == "on")
        except SystemExit as exc:
            fatal(str(exc.code))
            raise SystemExit(1) from None
        except OSError as exc:
            fatal(f"Cannot change autostart: {exc}")
            raise SystemExit(1) from None
        log.info(text.replace("\n", " "))
        tell(text)
        return
    log.info("Settings: %s", env_file or f"no .env in {data_dir()}, using environment variables")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except SystemExit as exc:
        if isinstance(exc.code, str):
            fatal(exc.code)
            raise SystemExit(1) from None
        raise
    except Exception as exc:
        log.exception("Unexpected error")
        fatal(f"{type(exc).__name__}: {exc}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
