"""Bot flows: languages, access modes, roles, groups and message formatting."""

import json
from types import SimpleNamespace

from conftest import FakeBot, buttons, texts, toast


def test_language_follows_telegram_app(bot: FakeBot):
    calls = bot.message(bot.ADMIN, "/start", lc="uk")
    assert texts(calls) == ["📊 Station\nuk"]
    assert buttons(calls[0][1])[0] == ["📊 Статус"]


def test_buttons_work_in_any_language(bot: FakeBot):
    assert texts(bot.message(bot.ADMIN, "📊 Статус", lc="ru")) == ["📊 Station\nru"]
    assert texts(bot.message(bot.ADMIN, "⚙️ Einstellungen", lc="de")) == ["⚙️ Einstellungen"]


def test_group_ignores_chatter_and_commands_for_other_bots(bot: FakeBot):
    bot.tg.username = "my_bot"
    assert bot.message(bot.GROUP, "what is the status", user=bot.ADMIN, kind="group") == []
    assert bot.message(bot.GROUP, "/status@other_bot", user=bot.ADMIN, kind="group") == []
    assert texts(bot.message(bot.GROUP, "/status@my_bot", user=bot.ADMIN, kind="group"))


def test_closed_mode_tells_strangers_their_id(bot: FakeBot):
    calls = bot.message("111", "/start", lc="ru")
    assert texts(calls) == ["⛔ Это приватный бот. Ваш id: 111. Отправьте его владельцу, чтобы получить доступ."]
    assert bot.users.role("111") is None


def test_request_approve_and_member_cannot_control(bot: FakeBot):
    bot.press(bot.ADMIN, "u:acc:requests")
    calls = bot.message("111", "/start", name="Vasya")
    assert {p["chat_id"] for m, p in calls} == {"111", bot.ADMIN}
    request = next(p for m, p in calls if p["chat_id"] == bot.ADMIN)
    assert buttons(request) == [["✅ Allow", "⛔ Deny"]]
    assert texts(bot.message("111", "/start")) == ["⏳ Your request is still waiting"]

    calls = bot.press(bot.ADMIN, "u:ok:111")
    assert bot.users.role("111") == "user"
    assert any(p.get("chat_id") == "111" for m, p in calls)  # the keyboard goes to the new user

    assert toast(bot.press("111", "a:light:1")) == "Only admins can change this"
    assert toast(bot.press("111", "u:acc:open")) == "Only admins can change this"


def test_denied_user_is_ignored(bot: FakeBot):
    bot.press(bot.ADMIN, "u:acc:requests")
    bot.message("111", "/start")
    bot.press(bot.ADMIN, "u:no:111")
    assert bot.users.role("111") == "blocked"
    assert bot.message("111", "/start") == []


def test_open_mode_adds_user_and_tells_admins(bot: FakeBot):
    bot.press(bot.ADMIN, "u:acc:open")
    calls = bot.message("999", "/start", lc="pl", name="Ola")
    assert bot.users.role("999") == "user"
    assert "👋 Ola (999) joined the bot" in texts(calls)


def test_add_by_id_and_env_users_are_protected(bot: FakeBot):
    bot.press(bot.ADMIN, "u:add")
    calls = bot.message(bot.ADMIN, "222333444")
    assert "✅ Added: Petro (222333444)" in texts(calls)
    assert toast(bot.press(bot.ADMIN, f"u:rm:{bot.ADMIN}")) == "Listed in .env, change it there"
    bot.press(bot.ADMIN, "u:adm:222333444")
    assert bot.users.role("222333444") == "admin"
    bot.press(bot.ADMIN, "u:rm:222333444")
    assert bot.users.role("222333444") is None


def test_language_menu_changes_chat_language(bot: FakeBot):
    calls = bot.press(bot.ADMIN, "l:es")
    assert "🌐 Idioma: Español" in texts(calls)
    assert bot.users.lang(bot.ADMIN).code == "es"


def test_menu_titles_are_bold(bot: FakeBot):
    calls = bot.message(bot.ADMIN, "⚙️ Settings")
    entities = json.loads(calls[0][1]["entities"])
    assert entities == [{"type": "bold", "offset": 0, "length": len("⚙️ Settings".encode("utf-16-le")) // 2}]


def test_alerts_reach_every_chat_in_its_language(bot: FakeBot):
    bot.message(bot.ADMIN, "/start", lc="uk")
    bot.tg.send(lambda L: L.t("al_data_back"))
    sent = {p["chat_id"]: p["text"][9:] for m, p in bot.drain()}
    assert sent == {bot.ADMIN: "📶 Дані від Anker знову надходять", bot.GROUP: "📶 Anker data is flowing again"}


def test_light_button_hidden_without_light_and_dc(bot: FakeBot):
    del bot.station["light_mode"]
    calls = bot.message(bot.ADMIN, "🔦 Light/12V")
    assert texts(calls) == ["This station has no light and no 12V output"]
    assert ["🔦 Light/12V"] not in [row[:1] for row in buttons(calls[0][1])]


def test_light_panel_has_dc_switch(bot: FakeBot):
    bot.station["dc_output_power_switch"] = 0
    calls = bot.message(bot.ADMIN, "🔦 Light")  # the label of keyboards from older versions
    assert texts(calls)[0].splitlines() == ["🔦 Light: Off", "🔌 12V: Off", "💡 Auto 12V in an outage: off"]
    assert buttons(calls[0][1])[-2:] == [["🔌 12V On", "• 🔌 12V Off"], ["•💡✗", "17–23", "18–24", "18–01", "✏️"]]
    calls = bot.press(bot.ADMIN, "a:autolight:18-01@light")
    assert toast(calls) == "Saved" and "💡 Auto 12V in an outage: 18:00–01:00" in texts(calls)[0]
    assert texts(bot.press(bot.ADMIN, "a:autolight:edit")) == ["Send the hours for the auto light, e.g. 18:30-00:30"]
    assert texts(bot.message(bot.ADMIN, "late")) == [
        "Could not read it: send the start and the end, e.g. 18:30-00:30 (at most 16 h)"]
    calls = bot.message(bot.ADMIN, "18.30 - 0.30")
    assert "💡 Auto 12V in an outage: 18:30–00:30" in texts(calls)[0]
    assert buttons(calls[0][1])[-1][-1] == "•✏️"


def test_light_panel_dc_only_and_stays_open(bot: FakeBot):
    del bot.station["light_mode"]
    bot.station["dc_output_power_switch"] = 0
    calls = bot.message(bot.ADMIN, "🔦 Light/12V")
    assert texts(calls)[0].splitlines()[0] == "🔌 12V: Off"
    calls = bot.message(bot.ADMIN, "/start")
    assert buttons(calls[0][1])[1][0] == "🔌 12V"  # the keyboard button without the light
    sent = []
    bot.controller.device = SimpleNamespace(set_dc_output=lambda **kw: sent.append(kw))
    calls = bot.press(bot.ADMIN, "a:dc:1@light")
    assert sent == [{"enabled": True}]
    assert toast(calls) == "On ✓"
    assert texts(calls)[-1].splitlines()[0] == "🔌 12V: On"  # the panel is shown again, not Outputs
