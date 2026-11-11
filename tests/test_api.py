"""The Siri / Shortcuts HTTP API: the key, the routes and what Siri reads out."""

from types import SimpleNamespace

from ankerups import app
from ankerups.api import Api
from ankerups.i18n import lang
from test_tv import make

KEY = "k" * 20


def test_from_env_needs_both():
    assert Api.from_env({}, {}) is None
    assert Api.from_env({"API_BIND": "100.1.2.3:8765", "API_KEY": "short"}, {}) is None
    api = Api.from_env({"API_BIND": "100.1.2.3:8765", "API_KEY": KEY}, {})
    assert api.addr == ("100.1.2.3", 8765)


def test_key_and_routes():
    api = Api("127.0.0.1:1", KEY, {"ping": lambda q: "pong " + q.get("x", "")})
    assert api.answer("/ping?x=1") == (403, "forbidden")
    assert api.answer(f"/ping?key=bad{KEY}") == (403, "forbidden")
    assert api.answer(f"/ping?key={KEY}&x=1") == (200, "pong 1")
    assert api.answer(f"/nope?key={KEY}")[0] == 404


def test_routes_status_and_tv(settings):
    c, dev, ctx = make(settings)
    watchdog = SimpleNamespace(on_battery=True, outage_seconds=lambda: 2 * 3600 + 300)
    snapshot = {"levels": {"main": 100, "expansion": 80}, "remaining_h": 40, "out": 41, "ac_in": 0}
    tg = SimpleNamespace(default=lang("ru"))
    api = Api("127.0.0.1:1", KEY, app.api_routes(c, watchdog, snapshot, tg))
    code, text = api.answer(f"/status?key={KEY}")
    assert code == 200 and text == ("Заряд 90% (основная 100%, доп. 80%). Света нет 2 ч 05 мин, хватит на 40 ч 00 мин. "
                                    "Нагрузка 41 Вт, вход 0 Вт.")
    watchdog.on_battery = False
    assert api.answer(f"/status?key={KEY}&lang=en")[1] == (
        "Charge 90% (main 100%, expansion 80%). The grid is on. Load 41 W, input 0 W.")
    assert api.answer(f"/tv/off?key={KEY}")[1] == "Готово" and dev.awake is False
    assert api.answer(f"/tv/toggle?key={KEY}")[1] == "Готово" and dev.awake is True
    assert api.answer(f"/tv/volume?key={KEY}&v=up")[1] == "Готово" and dev.vol == 54
    assert api.answer(f"/tv/timer?key={KEY}&min=60")[1] == "Готово" and settings.get("tv_off_at")
    assert api.answer(f"/tv/timer?key={KEY}&min=x")[1] == "Не получилось"
    dev.fail = True
    c.tv.down_until = 0
    assert api.answer(f"/tv/pause?key={KEY}")[1] == "Не получилось"
