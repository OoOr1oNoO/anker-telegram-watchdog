"""📊 Status widget: size limits, data, sending, live updates and the text mode."""

import json
import urllib.error

import pytest

from ankerups import widget, widget_clock, widget_ios, widget_quick
from ankerups.i18n import lang
from ankerups.schedmenu import ScheduleMenu
from ankerups.translations import LANGS
from conftest import FakeBot, buttons, texts, toast
from test_schedule import fake, schedule  # noqa: F401 - fixtures

DATA = widget.widget_data(1794139367, 1794139364, False, 1794133607, {"main": 100, "expansion": 61}, 0, 54,
                          40.5, "0" * 30, "0" * 11 + "1" * 7 + "0" * 30, "1" * 48, (1794151800, "1"), "12.1", 802)


@pytest.mark.parametrize("code", LANGS)
@pytest.mark.parametrize("module", [widget, widget_ios, widget_clock, widget_quick])
def test_html_fits_telegram_limits(code, module):
    html = module.widget_html(lang(code), "Anker <SOLIX> C1000X")
    # Telegram refuses more than 4096 bytes after zlib level 9, measured the same way as here
    # (the widget-sandbox docs); keep ~50 bytes of margin (the station's real name is shorter than this one)
    assert len(html.encode()) <= 32768 and widget.compressed_size(html) <= 4040
    assert "&lt;SOLIX&gt;" in html and "__" not in html


def test_data_is_compact_json():
    data = json.loads(DATA)
    assert len(DATA.encode()) <= widget.MAX_DATA
    assert data["g"] == 0 and data["b"] == [100, 61] and data["x"] == [1794151800, "1"]
    assert "i" in data  # 0 W input is kept, only None / "" are dropped


@pytest.fixture
def wbot(bot: FakeBot):
    bot.handlers.widget = lambda: ("Anker C1000X", DATA)
    bot.controller.widgets = True
    return bot


def test_status_sends_widget_then_updates_it(wbot: FakeBot):
    calls = wbot.message(wbot.ADMIN, "📊 Status")
    (method, params), = calls
    assert method == "sendWidget" and params["chat_data"] == DATA and "Anker C1000X" in params["html"]
    wbot.tg.update_widgets('{"t":1}')
    assert wbot.calls == [("editMessageWidgetData", {"chat_id": wbot.ADMIN, "message_id": 101,
                                                     "chat_data": '{"t":1}'})]


def test_widget_that_is_gone_stops_updating(wbot: FakeBot):
    wbot.message(wbot.ADMIN, "📊 Status")

    def deleted(method, params, timeout=10):
        raise urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
    wbot.tg._call = deleted
    wbot.tg.update_widgets("{}")
    assert wbot.tg._widgets() == {}


def test_refused_widget_falls_back_to_text(wbot: FakeBot):
    real = wbot.tg._call

    def no_widgets(method, params, timeout=10):
        if method == "sendWidget":
            raise urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
        return real(method, params, timeout)
    wbot.tg._call = no_widgets
    assert texts(wbot.message(wbot.ADMIN, "📊 Status")) == ["📊 Station\nen"]


def test_status_style_per_chat(wbot: FakeBot):
    calls = wbot.message(wbot.ADMIN, "⚙️ Settings")
    assert buttons(calls[0][1])[-1] == ["📊 Status look: Classic"]
    calls = wbot.press(wbot.ADMIN, "m:style")
    assert buttons(calls[0][1])[:2] == [["• Classic", "Modern"], ["Clock", "Text"]]
    calls = wbot.press(wbot.ADMIN, "v:ios")
    assert toast(calls) == "Saved" and buttons(calls[0][1])[0] == ["Classic", "• Modern"]
    assert "CHARGE AT POWER-ON" in dict(wbot.message(wbot.ADMIN, "📊 Status"))["sendWidget"]["html"]
    wbot.press(wbot.ADMIN, "v:clock")
    assert "pathLength" in dict(wbot.message(wbot.ADMIN, "📊 Status"))["sendWidget"]["html"]  # the clock dial
    wbot.press(wbot.ADMIN, "v:text")
    assert texts(wbot.message(wbot.ADMIN, "📊 Status")) == ["📊 Station\nen"]
    assert wbot.message(wbot.GROUP, "/status", kind="group")[0][0] == "sendWidget"  # other chats keep it
    wbot.press(wbot.ADMIN, "v:bogus")
    assert wbot.users.status_style(wbot.ADMIN) == "text"


def test_old_text_mode_setting_is_kept(wbot: FakeBot):
    wbot.users.stats.meta(f"status_text:{wbot.ADMIN}", "1")
    assert wbot.users.status_style(wbot.ADMIN) == "text"


def test_schedule_set_up_from_the_bot(wbot: FakeBot, schedule, fake):  # noqa: F811
    wbot.handlers.sched = wbot.controller.sched = ScheduleMenu(schedule, fetch=fake)
    calls = wbot.press(wbot.ADMIN, "m:sched")
    assert texts(calls)[0].startswith("📅 Outage schedule\nNot set up")
    assert toast(wbot.press(wbot.GROUP, "g:kyiv", user="555")) == "Only admins can change this"
    calls = wbot.press(wbot.ADMIN, "g:kyiv")
    assert texts(calls) == ["Send the street name, e.g. Хрещатик"]
    calls = wbot.message(wbot.ADMIN, "Хрещатик")
    assert texts(calls) == ["вул. Хрещатик: send the house number, e.g. 22/1"]
    calls = wbot.message(wbot.ADMIN, "22/1")
    assert "📍 вул. Хрещатик, 22/к1, group 12.1" in texts(calls)[0]
    assert schedule.group == "12.1"
    assert wbot.message(wbot.ADMIN, "hello") == []  # no more input expected


def test_emergency_flag_in_data():
    data = json.loads(widget.widget_data(1, 1, True, None, {"main": 50}, 0, 0, None, "", "", "", None, "12.1", 0,
                                         emergency=True))
    assert data["e"] == 1 and "p" not in data


def test_widget_buttons_switch_light_and_dc(wbot: FakeBot):
    from types import SimpleNamespace
    sent = []
    wbot.station["dc_output_power_switch"] = 0
    wbot.controller.device = SimpleNamespace(set_light=lambda **kw: sent.append(("light", kw)),
                                             set_dc_output=lambda **kw: sent.append(("dc", kw)))
    assert wbot.controller.widget_controls() == (0, 0)
    wbot.message(wbot.ADMIN, "📊 Status")

    def tap(data, user=None):
        wbot.tg._handle_update({"widget_query": {"id": "w1", "from": {"id": int(user or wbot.ADMIN)},
                                                 "chat": {"id": int(wbot.ADMIN)}, "message_id": 101,
                                                 "data": data}}, wbot.handlers)
        return wbot.drain()

    calls = tap("light")
    assert ("answerWidgetQuery", {"widget_query_id": "w1", "data": "High ✓"}) in calls
    assert any(m == "editMessageWidgetData" for m, _ in calls)  # the new state at once
    assert wbot.controller.widget_controls() == (3, 0)  # pending until telemetry catches up
    tap("light")
    tap("dc")
    assert sent == [("light", {"mode": 3}), ("light", {"mode": 0}), ("dc", {"enabled": True})]


def test_widget_buttons_admins_only_and_only_when_reported(wbot: FakeBot):
    wbot.users.upsert("555", role="user")
    calls = wbot.tg._handle_update({"widget_query": {"id": "w2", "from": {"id": 555}, "chat": {"id": 555},
                                                     "data": "light"}}, wbot.handlers) or wbot.drain()
    assert ("answerWidgetQuery", {"widget_query_id": "w2", "data": "Only admins can change this"}) in calls
    del wbot.station["light_mode"]
    assert wbot.controller.widget_controls() == (None, None)  # no buttons in the widget
    assert "allow_queries" in dict(wbot.message(wbot.ADMIN, "📊 Status"))["sendWidget"]


def test_new_status_replaces_the_old_widget_of_the_same_look(wbot: FakeBot):
    wbot.message(wbot.ADMIN, "📊 Status")  # message 101, classic
    calls = wbot.message(wbot.ADMIN, "📊 Status")  # 102 replaces 101
    assert ("deleteMessage", {"chat_id": wbot.ADMIN, "message_id": 101}) in calls
    wbot.press(wbot.ADMIN, "v:clock")
    calls = wbot.message(wbot.ADMIN, "📊 Status")  # another look: the classic one stays
    assert "deleteMessage" not in dict(calls)
    assert sorted(m for m, _ in wbot.tg._widgets().values()) == [102, 103]


def test_widgets_saved_by_older_versions_count_as_classic(wbot: FakeBot):
    wbot.users.stats.meta("widgets", json.dumps({wbot.ADMIN: [55, int(__import__("time").time())]}))
    calls = wbot.message(wbot.ADMIN, "📊 Status")
    assert ("deleteMessage", {"chat_id": wbot.ADMIN, "message_id": 55}) in calls


def tap(bot: FakeBot, data: str, user=None):
    bot.tg._handle_update({"widget_query": {"id": "q1", "from": {"id": int(user or bot.ADMIN)},
                                            "chat": {"id": int(bot.ADMIN)}, "data": data}}, bot.handlers)
    return bot.drain()


@pytest.fixture
def qbot(wbot: FakeBot):
    from types import SimpleNamespace
    wbot.handlers.quick = wbot.controller.quick_data
    wbot.station["dc_output_power_switch"] = 0
    wbot.sent = []
    wbot.controller.device = SimpleNamespace(
        set_fast_charging=lambda **kw: wbot.sent.append(("fast", kw)), set_display=lambda **kw: wbot.sent.append(("disp", kw)),
        set_charge_limit=lambda **kw: wbot.sent.append(("limit", kw)), set_light=lambda **kw: wbot.sent.append(("light", kw)),
        set_dc_output=lambda **kw: wbot.sent.append(("dc", kw)))
    return wbot


def test_quick_settings_open_from_the_status_widget(qbot: FakeBot):
    calls = tap(qbot, "qs")
    params = dict(calls)["sendWidget"]
    assert "Quick settings" in params["html"]
    data = json.loads(params["chat_data"])
    assert data == {"lm": 0, "dc": 0, "fast": 1, "disp": 2, "lim": 800, "lims": [200, 400, 600, 800, 1000],
                    "a": 13, "b": 25, "on": 0, "st": "widget"}
    assert "|quick" in "".join(qbot.tg._widgets())


def test_quick_settings_controls(qbot: FakeBot, settings):
    tap(qbot, "fast")
    tap(qbot, "disp")
    tap(qbot, "limit:600")
    assert qbot.sent == [("fast", {"enabled": False}), ("disp", {"mode": 3}), ("limit", {"max_watts": 600})]
    tap(qbot, "auto:18:30-00:30")
    assert settings.get("autolight") == "18:30-00:30"
    assert json.loads(qbot.controller.quick_data())["on"] == 1
    tap(qbot, "auto:off")
    assert settings.get("autolight") == ""
    assert ("answerWidgetQuery", {"widget_query_id": "q1", "data": "Unknown action"}) in tap(qbot, "limit:123")


def test_quick_settings_look_for_anyone_and_admin_only_controls(qbot: FakeBot):
    qbot.users.upsert("555", role="user")
    calls = qbot.tg._handle_update({"widget_query": {"id": "q2", "from": {"id": 555}, "chat": {"id": 555},
                                                     "data": "look:clock"}}, qbot.handlers) or qbot.drain()
    assert qbot.users.status_style("555") == "clock" and "pathLength" in dict(calls)["sendWidget"]["html"]
    calls = qbot.tg._handle_update({"widget_query": {"id": "q3", "from": {"id": 555}, "chat": {"id": 555},
                                                     "data": "fast"}}, qbot.handlers) or qbot.drain()
    assert ("answerWidgetQuery", {"widget_query_id": "q3", "data": "Only admins can change this"}) in calls
    assert qbot.sent == []


def test_auto_light_window_on_the_range():
    from ankerups.widget_quick import auto_slots, with_look
    assert auto_slots("18:30-00:30") == (13, 25, 1)
    assert auto_slots("") == (13, 25, 0)
    assert auto_slots("20-06") == (16, 32, 1)  # past 04:00: to the end of the range
    assert with_look("{}", "ios") == '{"st":"ios"}' and with_look('{"a":1}', "text") == '{"a":1,"st":"text"}'


def test_back_to_status_from_quick_settings(qbot: FakeBot):
    qbot.press(qbot.ADMIN, "v:ios")
    calls = tap(qbot, "status")
    assert "CHARGE AT POWER-ON" in dict(calls)["sendWidget"]["html"]


def test_widget_edits_only_on_change_and_pause_on_flood_control(wbot: FakeBot):
    import urllib.error
    from io import BytesIO
    wbot.message(wbot.ADMIN, "📊 Status")
    base = json.loads(DATA)
    same = json.dumps(base | {"t": base["t"] + 15, "o": base["o"] - 2})  # time and a few watts only
    wbot.tg.update_widgets(DATA)
    wbot.tg.update_widgets(same)
    assert [m for m, _ in wbot.calls].count("editMessageWidgetData") == 1
    wbot.calls.clear()
    wbot.tg.update_widgets(json.dumps(base | {"g": 1}))  # the grid came back: sent at once
    assert [m for m, _ in wbot.calls] == ["editMessageWidgetData"]

    def flood(method, params, timeout=10):
        raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, BytesIO(
            b'{"ok":false,"error_code":429,"parameters":{"retry_after":120}}'))
    wbot.tg._call = flood
    wbot.tg.update_widgets(json.dumps(base | {"g": 0}), force=True)
    assert min(wbot.tg.widget_paused.values()) > __import__("time").time() + 100
    assert wbot.tg._widgets()  # the widget is kept for later


def test_status_press_is_deleted_with_a_widget(wbot: FakeBot):
    wbot.tg._handle_update({"message": {"message_id": 77, "chat": {"id": int(wbot.ADMIN), "type": "private"},
                                        "from": {"id": int(wbot.ADMIN)}, "text": "📊 Status"}}, wbot.handlers)
    calls = wbot.drain()
    assert ("deleteMessage", {"chat_id": wbot.ADMIN, "message_id": 77}) in calls
