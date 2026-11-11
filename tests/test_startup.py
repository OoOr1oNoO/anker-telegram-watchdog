"""Startup: .env loading and errors that a windowless exe would otherwise swallow."""

import logging
import sys

import pytest

pytest.importorskip("anker_solix_api")  # installed in CI; skipped by the short local command

from ankerups import app, common  # noqa: E402


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for key in ("ANKERUSER", "ANKERPASSWORD", "TG_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_env_with_bom_and_txt_extension(data, monkeypatch):
    # Windows Notepad: "Save as .env" with hidden extensions gives .env.txt, sometimes with a BOM
    (data / ".env.txt").write_text("ANKERUSER=a@b.c\nANKERPASSWORD=x\n", encoding="utf-8-sig")
    assert common.load_env() == data / ".env.txt"
    assert common.os.environ["ANKERUSER"] == "a@b.c"
    monkeypatch.delenv("ANKERUSER")
    monkeypatch.delenv("ANKERPASSWORD")


def test_missing_credentials_are_logged(data, monkeypatch, caplog):
    monkeypatch.setattr(sys, "argv", ["anker_ups", "--dry-run"])
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: None)  # keep pytest's log capture
    with caplog.at_level(logging.INFO, "anker_ups"), pytest.raises(SystemExit) as exc:
        app.main()
    assert exc.value.code == 1
    assert "no .env in" in caplog.text
    assert "Stopped: ANKERUSER / ANKERPASSWORD are not set" in caplog.text


class FakeWinreg:
    HKEY_CURRENT_USER, KEY_SET_VALUE, REG_SZ = "HKCU", 2, 1
    values: dict = {}

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(self, root, path, reserved, access):
        assert (root, path) == ("HKCU", app.AUTOSTART_KEY)
        return self._Key()

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        del self.values[name]


def test_autostart_on_and_off(data, monkeypatch, tmp_path):
    reg = FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", reg)
    monkeypatch.setattr(app, "IS_WINDOWS", True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Tools\AnkerUPS\anker_ups_lite.exe")
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: None)
    monkeypatch.setattr(sys, "argv", ["anker_ups", "--autostart"])
    app.main()  # returns instead of starting the watchdog
    assert reg.values == {"AnkerUPS": r'"C:\Tools\AnkerUPS\anker_ups_lite.exe"'}
    monkeypatch.setattr(sys, "argv", ["anker_ups", "--autostart", "off"])
    app.main()
    app.main()  # already off: no error
    assert reg.values == {}


def test_autostart_elsewhere_is_an_error(data, monkeypatch):
    monkeypatch.setattr(app, "IS_WINDOWS", False)
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: None)
    monkeypatch.setattr(sys, "argv", ["anker_ups", "--autostart"])
    with pytest.raises(SystemExit) as exc:
        app.main()
    assert exc.value.code == 1


def test_remaining_time_tells_the_grid_by_the_own_draw():
    full = {"main": 100, "expansion": 100}  # 2112 Wh

    def feed(samples, out=0, ac_in=0, levels=full):
        g = app.RuntimeGrid()
        result = None
        for i, hours in enumerate(samples):
            result = g.update(i * 3, hours, out, ac_in, levels)
        return result, g.extra

    assert feed([6553.5, 1484.6, 400, 1604.9])[0] is True  # full on the grid, no load (dips included)
    assert feed([600, 520, 700], out=4)[0] is True  # the grid, 12V from the battery
    assert feed([116.8, 53.5, 42.1, 109.5], out=4)[0] is False  # the battery, 12V on: 22-31 W own draw
    assert feed([56.5, 42.5, 64.9], out=30)[0] is None  # a load: the input tells, the estimate is off
    assert feed([6553.5, 1484.6, 1604.9], levels={"main": 100, "expansion": 96})[0] is None  # not full
    assert feed([137.6, 81.8, 94.3, 51.5, 177.8])[0] is False  # on the battery at 0 W (the inverter)
    assert feed([6553.5, 1484.6])[0] is None  # too few readings yet
    assert feed([None, 0, None])[0] is None  # AC output off: no remaining time at all
