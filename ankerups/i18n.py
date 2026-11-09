"""Texts and number formatting in the chat's language (see translations.py).
"""

import functools
from collections.abc import Callable

from .translations import ENERGY_UNITS, LANGS, MONTHS, SHORT_UNITS, TEXTS, UNITS


class Lang:
    """Texts and number formatting in one language; falls back to English."""

    def __init__(self, code: str | None):
        code = (code or "").lower().split("-")[0]
        self.code = code if code in LANGS else "en"
        self.i = LANGS.index(self.code)

    def t(self, key: str, **kw) -> str:
        row = TEXTS[key]
        text = row[self.i] or row[0]
        return text.format(**kw) if kw else text

    def dur(self, seconds: float, compact: bool = False) -> str:
        """36m, 5h 03m, or 11d 2h (266h 42m) for long ones (266h 42m when compact, for tables)."""
        d_u, h_u, m_u, _ = UNITS[self.code]
        minutes = max(0, int(seconds // 60))
        hours, m = divmod(minutes, 60)
        if minutes < 60:
            return f"{minutes}{m_u}"
        if hours < 24 or compact:
            return f"{hours}{h_u} {m:02d}{m_u}"
        d, h = divmod(hours, 24)
        return f"{d}{d_u} {h}{h_u} ({hours}{h_u} {m:02d}{m_u})"

    def short(self, seconds: int, compact: bool = False) -> str:
        """20 s, 5 min, 2 h for settings; compact (20s, 5min) for narrow buttons."""
        s_u, m_u, h_u = SHORT_UNITS[self.code] if compact else UNITS[self.code][:0:-1]
        if seconds < 60:
            return f"{seconds}{s_u}"
        return f"{seconds // 60}{m_u}" if seconds < 3600 else f"{seconds // 3600}{h_u}"

    def energy(self, kwh: float) -> str:
        k_u, w_u = ENERGY_UNITS.get(self.code, ("kWh", "Wh"))
        return f"{kwh:.2f} {k_u}" if kwh >= 1 else f"{kwh * 1000:.0f} {w_u}"

    def battery(self, key: str) -> str:
        if key == "main":
            return self.t("bat_main")
        return self.t("bat_expansion") if key == "expansion" else self.t("bat_expansion_n", n=key.split()[-1])

    def charge(self, levels: dict[str, int]) -> str:
        """76% (main 100%, expansion 51%); expansion packs are assumed to match the main capacity."""
        if len(levels) == 1:
            return f"{next(iter(levels.values()))}%"
        total = round(sum(levels.values()) / len(levels))
        return f"{total}% ({', '.join(f'{self.battery(k)} {v}%' for k, v in levels.items())})"

    def money(self, amount: float) -> str:
        return f"{amount:.2f} {self.t('currency')}"

    def month(self, number: int) -> str:
        return MONTHS[self.code][number - 1]


@functools.cache
def lang(code: str | None) -> Lang:
    return Lang(code)


Render = Callable[[Lang], str]
