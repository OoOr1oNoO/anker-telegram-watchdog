"""Translations, number formatting, button widths and stats texts in every language."""

import json
import re
import time

import pytest

from ankerups.i18n import lang
from ankerups.menus import Ctx
from ankerups.translations import LANGS, TEXTS

# a phone shows about six characters on each of five buttons in a row
MAX_LABEL_IN_ROW = {5: 7, 4: 9}


def test_every_text_has_all_languages_and_same_placeholders():
    for key, row in TEXTS.items():
        assert len(row) == len(LANGS), key
        names = {tuple(sorted(set(re.findall(r"\{(\w+)\}", text)))) for text in row if text}
        assert len(names) == 1, key


@pytest.mark.parametrize(("code", "seconds", "expected"), [
    ("en", 36 * 60, "36m"), ("en", 5 * 3600 + 180, "5h 03m"), ("en", 266 * 3600 + 42 * 60, "11d 2h (266h 42m)"),
    ("ru", 90 * 60, "1 ч 30 мин"), ("uk", 3600, "1 год 00 хв")])
def test_durations(code, seconds, expected):
    assert lang(code).dur(seconds) == expected


def test_charge_text():
    assert lang("en").charge({"main": 100, "expansion": 51}) == "76% (main 100%, expansion 51%)"
    assert lang("de").charge({"main": 40}) == "40%"


@pytest.mark.parametrize("code", LANGS)
@pytest.mark.parametrize("menu", ["notif", "charging", "outputs", "light", "root", "lang", "users"])
def test_buttons_fit_on_a_phone(bot, code, menu):
    ctx = Ctx(lang(code), True, bot.ADMIN, bot.ADMIN)
    _, markup = bot.controller.render(menu, ctx)
    for row in json.loads(markup)["inline_keyboard"]:
        limit = MAX_LABEL_IN_ROW.get(len(row))
        if limit:
            for button in row:
                # an emoji takes about two characters
                width = sum(2 if ord(ch) > 0x2000 else 1 for ch in button["text"] if ord(ch) != 0xFE0F)
                assert width <= limit, (menu, button["text"])


@pytest.mark.parametrize("code", LANGS)
def test_stats_texts_and_chart(stats, code):
    now = int(time.time())
    for minute in range(200):
        ts = (now - minute * 60) // 60 * 60
        stats._exec("INSERT OR REPLACE INTO samples VALUES (?, ?, ?, ?, ?)", (ts, 300, 150, 80, 1))
    stats._exec("INSERT INTO outages (start, end, soc_start, soc_end) VALUES (?, ?, ?, ?)",
                (now - 7200, now - 3600, 100, 70))
    L = lang(code)
    for text in (stats.energy_text(L), stats.outages_text(L), stats.months_text(L)):
        assert text and "{" not in text
    assert stats.chart_png(24, L).startswith(b"\x89PNG")
