"""The optional Tuya smart plug: cycle encoding, the cloud client and the menu."""

import hashlib
import hmac
import json

from ankerups.i18n import lang
from ankerups.menus import Controller, Ctx
from ankerups import widget
from ankerups.plug import Plug, decode_cycle, encode_cycle, parse_cycle

EN = lang("en")
REAL = "AX8DhAODAA8ADw=="  # read from a plug: on, every day, 15:00-14:59, 15 min on / 15 min off


def test_cycle_round_trip_and_parsing():
    c = decode_cycle(REAL)
    assert c == {"on": True, "days": 0x7F, "start": 900, "end": 899, "work": 15, "rest": 15}
    assert encode_cycle(True, 15, 15, 900, 899) == REAL
    assert decode_cycle("") is None and decode_cycle("!!") is None
    assert parse_cycle("15/15") == (15, 15, 0, 0)
    assert parse_cycle(" 20 / 10 22:00-08:00 ") == (20, 10, 1320, 480)
    assert parse_cycle("0/10") is None and parse_cycle("15/15 25:00-08:00") is None and parse_cycle("x") is None


class Cloud:
    """Tuya's open API: checks the signature, keeps the plug state."""

    def __init__(self):
        self.state = {"switch_1": True, "countdown_1": 0, "cur_power": 119, "cur_voltage": 2327,
                      "cycle_time": REAL}
        self.calls = []
        self.online = True

    def __call__(self, method, url, headers, body):
        path = url.split(".com", 1)[1]
        data = body or b""
        to_sign = "\n".join([method, hashlib.sha256(data).hexdigest(), "", path])
        msg = "id" + headers.get("access_token", "") + headers["t"] + headers["nonce"] + to_sign
        assert headers["sign"] == hmac.new(b"secret", msg.encode(), hashlib.sha256).hexdigest().upper()
        self.calls.append((method, path))
        if path.startswith("/v1.0/token"):
            return {"success": True, "result": {"access_token": "tok", "expire_time": 7200}}
        if path == "/v1.0/devices/dev1":
            return {"success": True, "result": {"online": self.online}}
        if path.endswith("/status"):
            return {"success": True, "result": [{"code": k, "value": v} for k, v in self.state.items()]}
        for cmd in json.loads(body)["commands"]:
            self.state[cmd["code"]] = cmd["value"]
        return {"success": True, "result": True}


def make(settings):
    cloud = Cloud()
    c = Controller(settings, None, lambda: {"dc_output_power_switch": 1, "light_mode": 0}, lambda coro: None, None)
    c.plug = Plug("id", "secret", "dev1", call=cloud)
    return c, cloud, Ctx(EN, True, "1", "1")


def test_plug_menu_shows_state_and_changes_it(settings):
    c, cloud, ctx = make(settings)
    assert "callback_data\": \"m:plug\"" in c.render("light", ctx)[1]
    text, markup = c.render("plug", ctx)
    assert "Plug: on" in text and "11.9 W · 232.7 V" in text and "Cycle: 15/15 min on/off, around the clock" in text
    assert "\\u202215/15" in markup  # the current cycle is marked
    assert c.act("plug", "off", ctx)[1] == "plug"
    assert cloud.state["switch_1"] is False
    c.act("plug", "t60", ctx)
    assert cloud.state["countdown_1"] == 3600 and "Turns on in 1h 00m" in c.render("plug", ctx)[0]
    c.act("plug", "c20/10", ctx)
    assert decode_cycle(cloud.state["cycle_time"])["work"] == 20 and decode_cycle(cloud.state["cycle_time"])["start"] == 900
    c.act("plug", "c0", ctx)
    assert decode_cycle(cloud.state["cycle_time"])["on"] is False and "Cycle: off" in c.render("plug", ctx)[0]
    c.text_setting("plugcycle", "30/5 22:00-08:00", ctx)
    assert decode_cycle(cloud.state["cycle_time"]) == {"on": True, "days": 0x7F, "start": 1320, "end": 480,
                                                       "work": 30, "rest": 5}
    assert c.text_setting("plugcycle", "nonsense", ctx) is None
    assert sum(1 for m, p in cloud.calls if p.startswith("/v1.0/token")) == 1  # the token is kept


def test_plug_cloud_down_and_no_plug(settings):
    c, cloud, ctx = make(settings)
    c.plug._http = lambda *a: {"success": False, "code": 28841001, "msg": "IoT Core service is not subscribed"}
    assert "does not answer" in c.render("plug", ctx)[0]
    c.plug = None
    assert "m:plug" not in c.render("light", ctx)[1]
    assert Plug.from_env({"TUYA_ACCESS_ID": "a"}) is None


def test_offline_plug_says_so_and_sends_nothing(settings):
    c, cloud, ctx = make(settings)
    cloud.online = False
    assert "Offline in the Tuya cloud" in c.render("plug", ctx)[0]
    assert c.act("plug", "off", ctx)[0] == "Offline in the Tuya cloud ✗"
    assert cloud.state["switch_1"] is True and not any(m == "POST" for m, _ in cloud.calls)


def test_menu_shows_a_press_before_the_cloud_catches_up_and_power_for_widgets(settings):
    c, cloud, ctx = make(settings)
    real = cloud.__call__

    def lagging(method, url, headers, body):  # the cloud reports the old state for a while
        if url.endswith("/status"):
            return {"success": True, "result": [{"code": "switch_1", "value": True}, {"code": "cur_power", "value": 119},
                                                   {"code": "cur_voltage", "value": 2327}]}
        return real(method, url, headers, body)
    c.plug._http = lagging
    assert c.plug.load() == (12, 233)
    c.act("plug", "off", ctx)
    text = c.render("plug", ctx)[0]
    assert "Plug: off" in text and "0 W" in text and c.plug.load()[0] == 0


def test_widgets_have_the_socket_line():
    from ankerups import widget, widget_clock, widget_ios
    for mod in (widget, widget_ios, widget_clock):
        html = mod.widget_html(EN, "Station")
        assert "M10 11v2M14 11v2" in html and '" · "+d.pv+" V"' in html


def test_widget_plug_button_toggles_it(settings):
    c, cloud, ctx = make(settings)
    assert c.quick_action("plug", ctx) == "Saved" and cloud.state["switch_1"] is False
    assert c.quick_action("plug", ctx) == "Saved" and cloud.state["switch_1"] is True
    html = widget.widget_html(EN, "Station")
    assert 'add(c,"plug",SV+SK' in html and "d.ps" in html


def test_plug_menu_card(settings):
    c, cloud, ctx = make(settings)
    html = c.rich("plug", ctx)
    assert html.startswith("<h3>🔌 Plug: on</h3><table bordered>") and "<td>⚡ Load</td><td>11.9 W · 232.7 V</td>" in html
    assert "<td>🔁 Cycle</td><td>15/15 min on/off, around the clock</td>" in html and "<footer>" not in html
    c.act("plug", "t30", ctx)
    assert "<td>⏲ Timer</td><td>Turns off in 30m</td>" in c.rich("plug", ctx)
    cloud.online = False
    c.plug.cache = (0.0, None)
    assert "<footer>⚠️" in c.rich("plug", ctx) and "<td>🔌 Plug</td><td>on</td>" in c.rich("light", ctx)
