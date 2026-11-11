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


def test_remaining_time_tells_the_grid_at_zero_load():
    assert app.remaining_and_grid(6553.5) == (None, True)  # full on the grid: "no value"
    assert app.remaining_and_grid(1484.6) == (None, True)  # full on the grid: the battery is not used
    assert app.remaining_and_grid(137.6) == (137.6, False)  # on the battery at 0 W
    assert app.remaining_and_grid(None) == (None, None) and app.remaining_and_grid(0) == (0.0, None)
