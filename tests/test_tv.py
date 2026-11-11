"""The Android TV over ADB: on, off, the off timer and its menu."""

import json
import time

from ankerups.i18n import lang
from ankerups.menus import Controller, Ctx
from ankerups.tv import Tv, parse_timer

EN = lang("en")


class FakeTv:
    """adb_shell's device: answers dumpsys power and key events."""

    def __init__(self):
        self.awake, self.cmds, self.fail, self.vol = True, [], False, 29

    def shell(self, cmd, read_timeout_s=None):
        if self.fail:
            raise ConnectionError("gone")
        self.cmds.append(cmd)
        if "KEYCODE_SLEEP" in cmd:
            self.awake = False
        if "KEYCODE_WAKEUP" in cmd:
            self.awake = True
        if "--set" in cmd:
            self.vol = int(cmd.split()[-1])
        if "--get" in cmd:
            return f"[v] Connecting to AudioService\n[v] volume is {self.vol} in range [0..100]\n"
        return f"  mWakefulness={'Awake' if self.awake else 'Asleep'}\n" if "dumpsys" in cmd else ""

    def close(self):
        pass


def make(settings):
    dev = FakeTv()
    c = Controller(settings, None, lambda: {"dc_output_power_switch": 1, "light_mode": 0}, lambda coro: None, None)
    c.tv = Tv("100.1.2.3", connect=lambda host, port, key: dev)
    return c, dev, Ctx(EN, True, "1", "1")


def test_from_env(tmp_path):
    assert Tv.from_env({}, tmp_path) is None
    tv = Tv.from_env({"TV_ADB": "100.64.0.10", "TV_NAME": "Living room"}, tmp_path)
    assert (tv.host, tv.port, tv.name, tv.key) == ("100.64.0.10", 5555, "Living room", tmp_path / ".adb" / "adbkey")
    assert Tv.from_env({"TV_ADB": "tv.lan:5556"}, tmp_path).port == 5556
    assert Tv.from_env({"TV_ADB": "100.1.1.1:5555, 192.168.0.2"}, tmp_path).targets == [("100.1.1.1", 5555), ("192.168.0.2", 5555)]


def test_second_address_when_the_first_is_down(settings):
    dev, tried = FakeTv(), []

    def connect(host, port, key):
        tried.append(host)
        if host == "100.1.1.1":
            raise TimeoutError("asleep")
        return dev
    tv = Tv("100.1.1.1", connect=connect)
    tv.targets = [("100.1.1.1", 5555), ("192.168.0.2", 5555)]
    assert tv.power(False) and tried == ["100.1.1.1", "192.168.0.2"]


def test_on_off_and_the_menu(settings):
    c, dev, ctx = make(settings)
    assert c.tv.state() is True and "<h3>📺 TV: on</h3>" in c.rich("tv", ctx)
    assert c.act("tv", "off", ctx) == ("Saved", "tv") and dev.awake is False and "input keyevent KEYCODE_SLEEP" in dev.cmds
    assert "📺 TV: off" in c.render("tv", ctx)[0]
    c.act("tv", "on", ctx)
    assert dev.awake is True and "<td>📺 TV</td><td>on</td>" in c.rich("light", ctx)
    assert '"a:tv:remote"' in c.render("light", ctx)[1]  # the panel opens the remote widget


def test_timers(settings):
    c, dev, ctx = make(settings)
    c.act("tv", "t60", ctx)
    at = float(settings.get("tv_off_at"))
    assert 3590 < at - time.time() <= 3600 and "off at" in c.rich("tv", ctx) and "a:tv:t0" in c.render("tv", ctx)[1]
    c.act("tv", "t0", ctx)
    assert settings.get("tv_off_at") == "" and "<td>⏲ Timer</td><td>off</td>" in c.rich("tv", ctx)
    assert c.text_setting("tvtimer", "nope", ctx) is None
    assert c.text_setting("tvtimer", "90", ctx) and 5390 < float(settings.get("tv_off_at")) - time.time() <= 5400


def test_parse_timer():
    now = time.mktime((2026, 11, 11, 21, 0, 0, 0, 0, -1))
    assert parse_timer("90", now) == now + 5400
    assert parse_timer("23:30", now) == now + 9000
    assert parse_timer("01.15", now) == now + 4 * 3600 + 900  # tonight, after midnight
    assert parse_timer("20:00", now) is None and parse_timer("900", now) is None  # over 12 h ahead
    assert parse_timer("25:00", now) is None and parse_timer("soon", now) is None


def test_unreachable_tv_says_so_and_waits(settings):
    c, dev, ctx = make(settings)
    dev.fail = True
    assert c.tv.state(fresh=True) is None and "no connection" in c.render("tv", ctx)[0]
    dev.fail = False
    assert c.tv.state(fresh=True) is None  # no new attempt for a minute: the menus would wait
    assert c.act("tv", "on", ctx)[0] == "no connection ✗"


def test_controls_button_with_a_tv(settings):
    from ankerups.bot import BUTTON_ACTIONS
    c, dev, ctx = make(settings)
    assert c.panel_button() == "btn_controls"
    assert BUTTON_ACTIONS["🎛 управление"] == "light" and BUTTON_ACTIONS["🔦 фонарь/12v"] == "light"  # old keyboards too


def test_remote_pad(settings, bot):
    c, dev, ctx = make(settings)
    markup = c.render("tv", ctx)[1]
    assert '"a:tv:kok"' in markup and '"a:tv:khome"' in markup and '"a:tv:kvup"' in markup  # the card's remote
    assert c.act("tv", "kup", ctx) == ("", "alert") and dev.cmds[-1] == "input keyevent KEYCODE_DPAD_UP"
    assert c.act("tv", "kok", ctx)[1] == "alert" and dev.cmds[-1] == "input keyevent KEYCODE_DPAD_CENTER"
    assert c.act("tv", "knope", ctx)[0] == "no connection ✗"
    assert c.act("tv", "kvup", ctx)[0] == "" and dev.vol == 39 and c.act("tv", "kvdown", ctx)[0] == "" and dev.vol == 29
    bot.controller.tv = c.tv
    calls = bot.press(bot.ADMIN, "a:tv:kback")
    assert dev.cmds[-1] == "input keyevent KEYCODE_BACK" and "editMessageText" not in dict(calls)  # the menu stays


def test_remote_widget(settings, bot):
    from ankerups import widget, widget_remote
    html = widget_remote.widget_html(EN, "")
    assert "📺 TV" in html and 'data-k=ok' in html and '"tv:k"+k' in html and "__" not in html
    assert widget.compressed_size(html) <= 4000
    c, dev, ctx = make(settings)
    assert c.quick_action("tv:kmute", ctx) == "" and dev.cmds[-1] == "input keyevent KEYCODE_VOLUME_MUTE"
    assert c.quick_action("tv:power", ctx) == "" and dev.awake is False  # on -> off
    assert c.quick_action("tv:power", ctx) == "" and dev.awake is True
    bot.controller.tv = c.tv
    calls = bot.press(bot.ADMIN, "a:tv:remote")
    sent = dict(calls)["sendWidget"]
    assert "data-k=ok" in sent["html"] and sent["chat_data"] == '{"at":0,"v":29,"vm":100}'
    assert c.tv.volume() == (29, 100) and c.quick_action("tv:vol:40", ctx) == "" and dev.vol == 40
    assert c.quick_action("tv:timer:40", ctx) == "" and 2390 < float(settings.get("tv_off_at")) - time.time() <= 2400
    assert '"at":' in dict(bot.press(bot.ADMIN, "a:tv:remote"))["sendWidget"]["chat_data"]
    assert c.quick_action("tv:timer:0", ctx) == "" and settings.get("tv_off_at") == ""
    before = len(bot.calls)
    bot.tg.update_widgets('{"t":1}', force=True)  # status updates leave the remote alone
    assert not [p for m, p in bot.calls[before:] if m == "editMessageWidgetData"]



def test_remote_in_a_group_shows_the_card(settings, bot):
    c, dev, ctx = make(settings)
    bot.controller.tv = c.tv
    calls = bot.press(bot.GROUP, "a:tv:remote", user=bot.ADMIN)
    assert "sendWidget" not in dict(calls) and "📺 TV: on" in json.loads(dict(calls)["editMessageText"]["rich_message"])["html"]
