"""An Android TV over ADB (network debugging): on, off and an off timer from the bot.

.env: TV_ADB=host[:port] (e.g. its Tailscale address, port 5555 by default), TV_NAME (shown in the
menus, "TV" by default). The bot keeps its own ADB key next to the database (.adb/adbkey); the TV
asks once to allow it ("Always allow from this computer"). In standby the TV keeps its network and
ADB, so KEYCODE_WAKEUP turns it on again; KEYCODE_SLEEP is the remote's power button.
"""

import contextlib
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .common import log

STATE_TTL = 15  # s: how long a read state is reused (menus open often)
DOWN_TTL = 15  # s: after a failed connection, do not try again sooner (the menus would wait)
TIMER_EVERY = 20  # s: how often the off timer is checked
# the remote widget's buttons -> Android key codes
KEYS = {"home": "HOME", "up": "DPAD_UP", "back": "BACK", "left": "DPAD_LEFT", "ok": "DPAD_CENTER",
        "right": "DPAD_RIGHT", "vdown": "VOLUME_DOWN", "down": "DPAD_DOWN", "vup": "VOLUME_UP",
        "play": "MEDIA_PLAY_PAUSE", "mute": "VOLUME_MUTE"}
# the same remote as buttons, for chats without widgets (groups)
PAD = [[("home", "🏠"), ("up", "⬆️"), ("back", "↩️")], [("left", "⬅️"), ("ok", "OK"), ("right", "➡️")],
       [("vdown", "🔉 −10"), ("down", "⬇️"), ("vup", "🔊 +10")], [("play", "⏯"), ("mute", "🔇")]]


def _adb_connect(host: str, port: int, key: Path):
    """A connected adb_shell device (the optional dependency is imported only when a TV is set up)."""
    from adb_shell.adb_device import AdbDeviceTcp  # noqa: PLC0415
    from adb_shell.auth.keygen import keygen  # noqa: PLC0415
    from adb_shell.auth.sign_pythonrsa import PythonRSASigner  # noqa: PLC0415

    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True)
        keygen(str(key))
        os.chmod(key, 0o600)
    signer = PythonRSASigner(Path(str(key) + ".pub").read_text(), key.read_text())
    device = AdbDeviceTcp(host, port, default_transport_timeout_s=8)
    device.connect(rsa_keys=[signer], auth_timeout_s=30)
    return device


class Tv:
    def __init__(self, host: str, port: int = 5555, key: Path | None = None, name: str = "",
                 connect: Callable | None = None):
        self.host, self.port, self.name = host, port, name
        self.targets = [(host, port)]
        self.key = key or Path(".adb/adbkey")
        self.connect = connect or _adb_connect
        self.device = None
        self.lock = threading.Lock()
        self.cache: tuple[float, bool | None] = (0.0, None)
        self.down_until = 0.0

    @classmethod
    def from_env(cls, env, data: Path) -> "Tv | None":
        target = (env.get("TV_ADB") or "").strip()
        if not target:
            return None
        targets = []  # several addresses, tried in order: e.g. its Tailscale one, then the home one
        for item in target.split(","):
            host, _, port = item.strip().rpartition(":") if item.count(":") == 1 else (item.strip(), "", "")
            targets.append((host or item.strip(), int(port) if port.isdigit() else 5555))
        tv = cls(*targets[0], data / ".adb" / "adbkey", (env.get("TV_NAME") or "").strip())
        tv.targets = targets
        return tv

    def _shell(self, cmd: str) -> str | None:
        """Run a shell command on the TV; None when it is not reachable (logged once a minute)."""
        with self.lock:
            if time.time() < self.down_until:
                return None
            # the open connection first, then each address (a dropped connection is opened again)
            for target in [None, *self.targets]:
                try:
                    if self.device is None:
                        if target is None:
                            continue
                        self.device = self.connect(*target, self.key)
                    return self.device.shell(cmd, read_timeout_s=8) or ""
                except Exception as exc:
                    old, self.device = self.device, None
                    if old is not None:
                        with contextlib.suppress(Exception):
                            old.close()
                    error = f"{target[0] if target else 'the open connection'}: {exc}"
            log.warning("TV not reachable (%s)", error)
            self.down_until = time.time() + DOWN_TTL
            return None

    def state(self, fresh: bool = False) -> bool | None:
        """True on, False in standby, None not reachable."""
        if not fresh and time.time() - self.cache[0] < STATE_TTL:
            return self.cache[1]
        out = self._shell("dumpsys power | grep mWakefulness=")
        state = None if out is None else "Awake" in out
        self.cache = (time.time(), state)
        return state

    def power(self, on: bool) -> bool:
        ok = self._shell("input keyevent " + ("KEYCODE_WAKEUP" if on else "KEYCODE_SLEEP")) is not None
        self.cache = (time.time(), on) if ok else (0.0, None)
        return ok

    def press(self, name: str) -> bool:
        """Press a remote button (KEYS)."""
        return name in KEYS and self._shell("input keyevent KEYCODE_" + KEYS[name]) is not None

    def volume(self) -> tuple[int, int] | None:
        """(the media volume, its maximum), None if not known."""
        out = self._shell("media volume --stream 3 --get")
        m = re.search(r"volume is (\d+) in range \[\d+\.\.(\d+)\]", out or "")
        return (int(m[1]), int(m[2])) if m else None

    def step_volume(self, delta: int) -> bool:
        """Volume up or down by delta (a button in chats without widgets: one key press is too small)."""
        vol = self.volume()
        return vol is not None and self.set_volume(max(0, min(vol[1], vol[0] + delta)))

    def set_volume(self, value: int) -> bool:
        """Set the media volume (the TV shows its volume bar)."""
        return self._shell(f"media volume --show --stream 3 --set {int(value)}") is not None

    def start_timer(self, settings) -> None:
        """Turns the TV off at settings "tv_off_at" (a Unix time); runs in its own thread so a slow
        network to the TV never holds up the bot."""
        def loop() -> None:
            while True:
                time.sleep(TIMER_EVERY)
                try:
                    at = float(settings.get("tv_off_at") or 0)
                except ValueError:
                    at = 0
                if at and time.time() >= at:
                    ok = self.power(False)
                    log.info("TV off timer: %s", "ok" if ok else "failed, the TV is not reachable")
                    settings.set("tv_off_at", "")
        threading.Thread(target=loop, daemon=True, name="tv-timer").start()


def parse_timer(text: str, now: float | None = None) -> float | None:
    """"90" (minutes from now) or "23:30" (a time of day, tomorrow's if it has passed) -> the Unix
    time to turn off at; None if it is neither or more than 12 hours ahead."""
    text, now = text.strip().replace(".", ":"), now or time.time()
    if text.isdigit():
        at = now + int(text) * 60
    elif text.count(":") == 1 and all(p.isdigit() for p in text.split(":")):
        h, m = (int(p) for p in text.split(":"))
        if h > 23 or m > 59:
            return None
        lt = time.localtime(now)
        at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
        if at <= now:
            at += 86400
    else:
        return None
    return at if 0 < at - now <= 12 * 3600 else None
