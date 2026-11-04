# anker-telegram-watchdog

Telegram alerts for Anker SOLIX portable power stations used as a UPS.

- Alert when the station loses AC input and when grid power comes back (with outage duration)
- Charge alerts for the main battery and each expansion battery (default 75 / 50 / 25 %)
- Critical charge alert (Telegram + topmost Windows popup) at a configurable level
- **📊 Status** button in the bot: grid state, charge, remaining time, input and load
- **🔦 Light** button: switch the station light (off / low / medium / high / blink)
- **📈 Stats** button: load / grid input / charge charts for 24 h and 7 days, outage history,
  energy through the station (kWh in / out) and a daily summary with a chart
  (data is kept in `anker_ups.db` next to the script)
- **⚙️ Settings** button: charge alert thresholds, critical level, daily summary time and which
  alerts to send; station outputs (AC / DC 12V on-off, Normal / Smart mode; turning AC off asks
  for confirmation), AC charge limit, fast charging, display brightness and timeout, auto-off.
  Only the controls the station reports are shown. Settings are stored in the database;
  `.env` values are the defaults
- Windows: switches to the Power saver plan during an outage and restores the previous plan afterwards

Data comes from the Anker cloud (MQTT) through the unofficial
[anker-solix-api](https://github.com/thomluther/anker-solix-api) library, so the script can run
on any machine with internet access: the PC on the UPS, a small cloud VM or a Raspberry Pi.

## Supported stations

Any power station that anker-solix-api supports over MQTT, e.g. SOLIX C300, C800, C1000(X),
C1000 Gen 2, C2000 Gen 2, F2000, F2600, F3800. Tested on a **SOLIX C1000X (A1761) with an
expansion battery**; other models use the same field names but have not been tested.

Grid loss is detected from the AC input power (and the charging status where the model reports
it). With a full battery and no load on the outputs some models may report 0 W input while the
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

Options: `--thresholds 75 50 25`, `--confirm 10` (seconds without input before an outage),
`--link-timeout 120`, `--test-telegram`. A log is written to `anker_ups.log` next to the script.

### Linux server (systemd)

Put `anker_ups.py`, `install_linux.sh` and `.env` into one folder and run:

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

### Windows exe

Ready-made exe files are attached to every [release](../../releases/latest); they are built from
this repository by GitHub Actions ([workflow](.github/workflows/release.yml)), so no Python is needed:

- `anker_ups.exe`: full version, including the charts in 📈 Stats
- `anker_ups_lite.exe`: smaller, without charts; enough for a PC that only switches power plans
  while the bot runs on a server
- `env.example`: save it next to the exe as `.env` and fill it in

The exe has no window; it writes `anker_ups.log` next to itself. Stop it with
`taskkill /im anker_ups.exe /f`. The files are not code-signed, so Windows SmartScreen or Defender
may warn about them (a known false positive for PyInstaller builds). If you prefer not to
allow an unsigned exe, run the script with `uv` instead.

To build it yourself, run `build_exe.bat` (lite variant) on Windows.

Autostart at logon:

```bat
schtasks /create /tn AnkerUPS /tr "\"C:\Tools\AnkerUPS\anker_ups.exe\"" /sc onlogon /rl limited
```

### One bot, one instance

Run only one instance per bot: two instances duplicate alerts and break the Status button.
To use both a server and the PC, let the server handle Telegram and leave `TG_BOT_TOKEN` empty
in the PC's `.env`, so the PC only switches power plans.

## Disclaimer

Not affiliated with Anker. The cloud API is unofficial and may change at any time.
