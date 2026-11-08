"""📊 Status widget: size limits, data, sending, live updates and the text mode."""

import json
import urllib.error

import pytest

from ankerups import widget
from ankerups.i18n import lang
from ankerups.schedmenu import ScheduleMenu
from ankerups.translations import LANGS
from conftest import FakeBot, buttons, texts, toast
from test_schedule import fake, schedule  # noqa: F401 - fixtures

DATA = widget.widget_data(1794139367, 1794139364, False, 1794133607, {"main": 100, "expansion": 61}, 0, 54,
                          40.5, "0" * 30, "0" * 11 + "1" * 7 + "0" * 30, "1" * 48, (1794151800, "1"), "12.1", 802)


@pytest.mark.parametrize("code", LANGS)
def test_html_fits_telegram_limits(code):
    html = widget.widget_html(lang(code), "Anker <SOLIX> C1000X")
    assert len(html.encode()) <= 32768 and widget.compressed_size(html) <= 3800
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


def test_text_mode_per_chat(wbot: FakeBot):
    calls = wbot.message(wbot.ADMIN, "⚙️ Settings")
    assert buttons(calls[0][1])[-1] == ["📊 Status: widget"]
    calls = wbot.press(wbot.ADMIN, "v:toggle")
    assert toast(calls) == "Saved" and buttons(calls[0][1])[-1] == ["📊 Status: text"]
    assert texts(wbot.message(wbot.ADMIN, "📊 Status")) == ["📊 Station\nen"]
    assert wbot.message(wbot.GROUP, "/status", kind="group")[0][0] == "sendWidget"  # other chats keep it


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
