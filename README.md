# anker-telegram-watchdog

Telegram alerts for Anker SOLIX portable power stations used as a UPS.

- Alert when the station loses AC input (with how long the grid was up) and when grid power
  comes back (with the outage duration)
- Full-charge alert once the station has recharged to 100 % after an outage (once per outage)
- Charge alerts for the main battery and each expansion battery (default 75 / 50 / 25 %)
- Critical charge alert (Telegram + topmost Windows popup) at a configurable level
- **📊 Status** button in the bot: a live dashboard right in the chat (a Telegram widget the bot
  keeps updating): grid state and for how long, charge, load, remaining time, today's grid
  history next to the planned outage schedule, the next planned change and whether the battery
  lasts until then, plus 🔦 light and 🔌 12V on/off buttons for admins. Telegram apps without widgets (e.g. older desktop versions) can switch the
  chat to a text status in **⚙️ Settings**
- **📅 Outage schedule** (Ukraine): Kyiv by street and house (YASNO), Kyiv region by group
  ([outage-data-ua](https://github.com/Baskerville42/outage-data-ua)); alerts 30 minutes
  before a planned outage, when tomorrow's schedule is published, when a schedule changes and
  when emergency outages are announced (the last schedule stays visible, marked as emergency).
  The outage and grid-back alerts say how they compare with the schedule ("20 min earlier
  than the schedule", "not in the schedule")
- **🔦 Light/12V** button: switch the station light (off / low / medium / high / blink) and the
  DC 12V output, e.g. for a USB lamp in a 12V car adapter (**🔌 12V** on stations without a light).
  **💡 Auto light**: during an outage in the evening (17–23, 18–24, 18–01 or your own hours)
  the bot turns the 12V output on, and off again when the power is back or the hours end
- **📈 Stats** button: load / grid input / charge charts for 24 h and 7 days (with the energy and
  its cost), and tables: outages by day, outages and energy by month, energy through the station
  (kWh in / out and the cost at your tariff, 4.32 UAH/kWh by default); a daily summary with a
  chart (data is kept in `anker_ups.db` next to the script)
- **🔥 High load** alerts (a kettle, a heater): when it starts and / or a summary when it ends,
  with the energy and its cost; threshold and mode in **⚙️ Settings → 🔔 Notifications**
- Before a planned outage: the charge the battery will have by then and, if it would not be
  full, the AC input limit that fills it in time (a button; fast charging is left alone)
- **⚙️ Settings** button: charge alert thresholds, critical level, daily summary time and which
  alerts to send; station outputs (AC / DC 12V on-off, Normal / Smart mode; turning AC off asks
  for confirmation), AC charge limit, fast charging, display brightness and timeout, auto-off.
  Only the controls the station reports are shown. Settings are stored in the database;
  `.env` values are the defaults
- Several people and / or Telegram groups; admins and users; access closed, on request or open,
  managed from the bot (**⚙️ Settings → 👥 Users**)
- Messages in English, Ukrainian, Russian, Polish, German or Spanish, chosen per chat
  automatically from the Telegram app language or in **⚙️ Settings → 🌐 Language**
- Optional dead man's switch: pings a [healthchecks.io](https://healthchecks.io) check, which
  alerts you when the server or the bot stops
- Windows: switches to the Power saver plan during an outage and restores the previous plan afterwards

Data comes from the Anker cloud (MQTT) through the unofficial
[anker-solix-api](https://github.com/thomluther/anker-solix-api) library, so the script can run
on any machine with internet access: the PC on the UPS, a small cloud VM or a Raspberry Pi.

## Supported stations

Any power station that anker-solix-api supports over MQTT, e.g. SOLIX C300, C800, C1000(X),
C1000 Gen 2, C2000 Gen 2, F2000, F2600, F3800. Tested on a **SOLIX C1000X (A1761) with an
expansion battery**; other models use the same field names but have not been tested.

Grid loss is detected from the AC input power, plus the charging status or the AC input plug
status where the model reports them (e.g. C1000 Gen 2 and C2000 Gen 2 report the plug status).
Models that report neither may show 0 W input with a full battery and a small load while the
grid is up; keep a load on the station or check the log before relying on it.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and get your chat id
   (e.g. from [@userinfobot](https://t.me/userinfobot)). Send the bot `/start` once.
2. Copy `.env.example` to `.env` and fill in the Anker account (owner account, email + password)
   and the Telegram settings.
3. Run it with [uv](https://docs.astral.sh/uv/) (it downloads Python and dependencies):

   ```bash
   uv run anker_ups.py -v --dry-run   # test: prints telemetry, does not touch power plans
   uv run anker_ups.py                # normal run
   ```

Keep the `ankerups` folder next to `anker_ups.py`. Options: `--thresholds 75 50 25`, `--confirm 10`
(seconds without input before an outage), `--link-timeout 120`, `--test-telegram`, `--autostart` (Windows). A log is
written to `anker_ups.log` next to the script.

### Users, groups and access

The people and groups in `TG_CHAT_ID` get alerts and can use the buttons; group ids start with
`-100`. Admins (`TG_ADMIN_IDS`; when it is empty, the people in `TG_CHAT_ID`) can also change
settings and control the station. Everything else is managed in **⚙️ Settings → 👥 Users**
(admins only):

- access mode: **Closed** (default: only people you add; strangers get their id to send you),
  **Requests** (a stranger presses `/start`, admins get Allow / Deny buttons) or **Open**
  (anyone who starts the bot gets alerts and viewing, without control);
- add a person or group by id, make someone an admin, remove them.

Users are stored in the database. The ids from `.env` are applied again on every start and can
only be changed in `.env`, so you cannot lock yourself out from the bot.

For a group: add the bot to the group and, in @BotFather, turn privacy mode off
(`/setprivacy` → Disable) or make the bot a group admin, otherwise it does not see the keyboard
buttons. Adding the bot to a group counts as a request in the Requests mode; otherwise add the
group id in 👥 Users (the bot logs the id of every chat it ignores).

### Healthcheck

If the server or the bot dies, nobody is left to send an alert. Create a free check on
[healthchecks.io](https://healthchecks.io) (period 5 minutes, grace 10 minutes), add its
Telegram or e-mail integration and put the ping URL into `HEALTHCHECK_URL`; the bot pings it
every 5 minutes and healthchecks.io alerts you when the pings stop.

### Linux server (systemd)

Put `anker_ups.py`, the `ankerups` folder, `install_linux.sh` and `.env` into one folder and run:

```bash
sudo bash install_linux.sh
journalctl -u anker-ups -f
```

Works on Ubuntu and Oracle Linux (x86_64 and ARM), e.g. an Oracle Cloud Always Free VM.
Re-run the installer to update the script or `.env`. Set `TIMEZONE` in `.env` (servers usually
run in UTC).

On a 1 GB / 0.5 GB VM such as the Oracle `VM.Standard.E2.1.Micro`, background package-cache
updates can push the machine into swap until it stops responding. Disabling them helps:

```bash
sudo systemctl disable --now dnf-makecache.timer oracle-cloud-agent oracle-cloud-agent-updater
```

### Docker

Every release also publishes an image for amd64 and arm64 (e.g. Raspberry Pi, Oracle ARM):

```bash
docker run -d --name anker-ups --restart unless-stopped \
  --env-file .env -v anker-data:/data ghcr.io/ooor1onoo/anker-telegram-watchdog:latest
docker logs -f anker-ups
```

The database and the log are kept in the `/data` volume (a bind-mounted folder must be writable
by uid 1000). Windows power plans do not apply in a container.

### Windows exe

Ready-made exe files are attached to every [release](../../releases/latest); they are built from
this repository by GitHub Actions ([workflow](.github/workflows/release.yml)), so no Python is needed:

- `anker_ups.exe`: full version, including the charts in 📈 Stats
- `anker_ups_lite.exe`: smaller, without charts; enough for a PC that only switches power plans
  while the bot runs on a server
- `env.example`: save it next to the exe as `.env` and fill it in

The exe has no window; it writes `anker_ups.log` next to itself, and a startup error (e.g. no
Anker login in `.env`) is also shown in a message box. `.env.txt` (Notepad with hidden file
extensions) works too. Stop it with
`taskkill /im anker_ups.exe /f`. The files are not code-signed, so Windows SmartScreen or Defender
may warn about them (a known false positive for PyInstaller builds). If you prefer not to
allow an unsigned exe, run the script with `uv` instead.

To build it yourself, run `build_exe.bat` (lite variant) on Windows.

Autostart at logon: put the exe and `.env` into their final folder and run the exe once with
`--autostart` (no admin rights needed; it adds the exe to `HKCU\...\CurrentVersion\Run` and
shows a message box). `--autostart off` removes it.

```bat
C:\Tools\AnkerUPS\anker_ups_lite.exe --autostart
```

Downloaded exe files carry a "from the internet" mark, so SmartScreen asks once on the first
start; to avoid it, tick **Unblock** in the file properties (or run `Unblock-File` in PowerShell).

### One bot, one instance

Run only one instance per bot: two instances duplicate alerts and break the Status button.
To use both a server and the PC, let the server handle Telegram and leave `TG_BOT_TOKEN` empty
in the PC's `.env`, so the PC only switches power plans.

## Development

`anker_ups.py` only starts the `ankerups` package:

| Module | What it does |
|---|---|
| `app.py` | startup, Anker MQTT loop, daily summary, healthcheck |
| `watchdog.py` | outage, charge and full-charge alerts; Windows power plans |
| `bot.py` | Telegram: sending, long polling, buttons, access requests |
| `menus.py` | settings, light, language and user menus; station commands |
| `users.py` | users, roles and access mode |
| `stats.py` | SQLite samples, outages and settings; summaries and charts |
| `widget.py` | the 📊 Status widget: HTML and its data |
| `schedule.py`, `schedmenu.py` | planned outage schedules (YASNO, outage-data-ua) and their menu |
| `i18n.py`, `translations.py` | languages and number formatting |

Tests use a fake Telegram API and a simulated clock, so they need no account or station:

```bash
uv run --with pytest --with matplotlib pytest -q tests
```

They run in GitHub Actions on every push, together with a lint check.

## Disclaimer

Not affiliated with Anker. The cloud API is unofficial and may change at any time.
