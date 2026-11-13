"""An Android TV over ADB (network debugging): on, off and an off timer from the bot.

.env: TV_ADB=host[:port] (e.g. its Tailscale address, port 5555 by default), TV_NAME (shown in the
menus, "TV" by default). The bot keeps its own ADB key next to the database (.adb/adbkey); the TV
asks once to allow it ("Always allow from this computer"). In standby the TV keeps its network and
ADB, so KEYCODE_WAKEUP turns it on again; KEYCODE_SLEEP is the remote's power button.
"""

import base64
import contextlib
import os
import re
import shlex
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
# the same buttons as Linux key codes, written straight into the TV's IR receiver (sendevent, ~0.2 s): `input`
# starts a Java tool for each press, ~1 s on this TV. The shell user is in the "input" group.
SCANS = {"home": 102, "up": 103, "back": 158, "left": 105, "ok": 28, "right": 106, "vdown": 114, "down": 108,
         "vup": 115, "play": 164, "mute": 113}
IR_FIND = "getevent -pl 2>/dev/null | grep -B1 -i 'ir receiver' | grep -o '/dev/input/event[0-9]*'"
# the same remote as buttons, for chats without widgets (groups)
PAD = [[("home", "🏠"), ("up", "⬆️"), ("back", "↩️")], [("left", "⬅️"), ("ok", "OK"), ("right", "➡️")],
       [("vdown", "🔉 −10"), ("down", "⬇️"), ("vup", "🔊 +10")], [("play", "⏯"), ("mute", "🔇")]]
REZKA = "com.falcofemoralis.hdrezkaapp/.views.MainActivity"  # the HDrezka app (its links do not search)
# taps in HDrezka on a fresh start (1920x1080), each with an extra pause in s (an `input` itself takes ~1 s):
# the menu's search icon opens the menu, then its "Search" item, then the search field
REZKA_TAPS = [(53, 540, 0), (150, 540, 0.5), (900, 75, 0)]
# the focus stays in HDrezka's search field, where the remote's arrows do nothing: TAB moves it to the field's
# clear button, down then onto the results once they have loaded; checked (a screen dump, ~2 s) and repeated
FOCUS_RESULTS = ("sleep 1.5; input keyevent KEYCODE_TAB; input keyevent KEYCODE_DPAD_DOWN; for i in 1 2 3; do "
                 "uiautomator dump /sdcard/.rz.xml >/dev/null 2>&1; "
                 "f=$(grep -o 'id/[a-z_]*\" class=[^>]*focused=\"true\"' /sdcard/.rz.xml | head -1); "
                 "case $f in *film_layout*) break;; *act_suggest*) input keyevent KEYCODE_TAB;; esac; "
                 "input keyevent KEYCODE_DPAD_DOWN; done")
# HDrezka on a fresh start with the remote's arrows: left to the menu (New), down twice to Search, OK opens it,
# right to its field
REZKA_KEYS = ["left", "down", "down", "ok", "right"]
IR_PAUSE = 0  # s between those presses: none needed, the app queues them
# the remote's keyboard up over the field after `input text`: BACK hides it (only while it is up: with the focus
# on the results BACK puts it back into the field)
BACK_IF_KEYBOARD = "sleep 1; dumpsys input_method | grep -q mInputShown=true && input keyevent KEYCODE_BACK"
ADB_IME = "com.android.adbkeyboard/.AdbIME"  # ADBKeyBoard: types any text (input text is ASCII only)


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
        self.ir: str | None = None  # the IR receiver's /dev/input/event*, "" when there is none

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

    def _shell(self, cmd: str, timeout: float = 8) -> str | None:
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
                    return self.device.shell(cmd, read_timeout_s=timeout) or ""
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
        """Press a remote button (KEYS): as the IR remote would when the TV has an IR receiver, else (or when
        that fails) with `input keyevent`."""
        if name not in KEYS or self._find_ir() is None:
            return False
        if self.ir and name in SCANS:
            out = self._shell(self._ir_keys([name]) + " && echo ok")
            if out is None:
                return False
            if "ok" in out:
                return True
            self.ir = ""  # no rights to it after all: the slow way from now on
        return self._shell("input keyevent KEYCODE_" + KEYS[name]) is not None

    def _find_ir(self) -> str | None:
        """The IR receiver's device ("" when there is none), looked up once; None if the TV is not reachable."""
        if self.ir is None:
            found = self._shell(IR_FIND)
            if found is None:
                return None
            self.ir = found.strip() if found.strip().startswith("/dev/input/event") else ""
        return self.ir

    def _ir_keys(self, names: list[str], pause: float = 0) -> str:
        """Shell commands pressing these remote buttons through the IR receiver, with a pause after each."""
        ev = self.ir
        return " && ".join(f"sendevent {ev} 1 {SCANS[n]} 1 && sendevent {ev} 0 0 0 && sendevent {ev} 1 {SCANS[n]} 0 "
                           f"&& sendevent {ev} 0 0 0" + (f" && sleep {pause}" if pause else "") for n in names)

    def volume(self) -> tuple[int, int] | None:
        """(the media volume, its maximum), None if not known."""
        out = self._shell("media volume --stream 3 --get")
        m = re.search(r"volume is (\d+) in range \[\d+\.\.(\d+)\]", out or "")
        return (int(m[1]), int(m[2])) if m else None

    def backlight(self) -> int | None:
        """The panel backlight 0-100 (Xiaomi's picture_backlight), None if not known."""
        out = (self._shell("settings get global picture_backlight") or "").strip()
        return int(out) if out.isdigit() else None

    def set_backlight(self, value: int) -> bool:
        """0 leaves the screen practically dark with the sound on."""
        return self._shell(f"settings put global picture_backlight {max(0, min(100, int(value)))}") is not None

    def step_volume(self, delta: int) -> bool:
        """Volume up or down by delta (a button in chats without widgets: one key press is too small)."""
        vol = self.volume()
        return vol is not None and self.set_volume(max(0, min(vol[1], vol[0] + delta)))

    def set_volume(self, value: int) -> bool:
        """Set the media volume (the TV shows its volume bar)."""
        return self._shell(f"media volume --show --stream 3 --set {int(value)}") is not None

    def can_type(self, text: str) -> bool:
        """Whether the TV is on and can take this text: ASCII always, other text with ADBKeyBoard."""
        if self.state() is not True:
            return False
        return text.isascii() or "adbkeyboard" in (self._shell("pm list packages com.android.adbkeyboard") or "")

    def search(self, query: str) -> bool:
        """Search HDrezka on the TV: opens the app afresh, its search and types the query (~5 s with the remote's
        arrows through the IR receiver); without it taps and `input` (~1 s each here), Enter and the focus moved
        onto the results (~12 s)."""
        query = " ".join(query.replace("%", " ").split())[:100]
        if not query:
            return False
        # ADBKeyBoard (when installed) types any text; the remote's keyboard comes back after
        adb_ime = "adbkeyboard" in (self._shell("pm list packages com.android.adbkeyboard") or "")
        if not adb_ime and not query.isascii():
            return False
        ime = (self._shell("settings get secure default_input_method") or "").strip()
        swap = adb_ime and ime != ADB_IME
        taps = "; ".join(f"input tap {x} {y}" + (f"; sleep {pause}" if pause else "") for x, y, pause in REZKA_TAPS)
        if adb_ime:
            typing = f"am broadcast -a ADB_INPUT_B64 --es msg {base64.b64encode(query.encode()).decode()} >/dev/null"
        else:
            typing = "input text " + shlex.quote(query.replace(" ", "%s"))
        start = f"am start -S -W -n {REZKA} >/dev/null" + (f"; ime set {ADB_IME} >/dev/null" if swap else "")
        if self._find_ir():  # the remote's arrows (~0.2 s each, `input` takes ~1 s): the menu, its Search, the
            # field; HDrezka searches as it is typed in, the focus stays in the field (the remote's down
            # goes on to the results)
            cmds = [start + "; " + self._ir_keys(REZKA_KEYS, IR_PAUSE), typing] + (
                [] if adb_ime else [BACK_IF_KEYBOARD])
        else:  # taps and key events
            cmds = [start + "; sleep 1", taps, f"{typing}; input keyevent KEYCODE_ENTER"] + (
                [] if adb_ime else [BACK_IF_KEYBOARD]) + [FOCUS_RESULTS]
        ok = all(self._shell(cmd, timeout=30) is not None for cmd in cmds)
        if swap and ime:  # the remote's keyboard back (also after a failed step) once no text field has the
            # focus: with the field focused it pops up over the results
            self._shell(f"ime set {shlex.quote(ime)} >/dev/null")
        return ok

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
