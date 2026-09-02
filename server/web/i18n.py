"""Tiny i18n: load server/i18n/<lang>.json, resolve a key, fall back to English.

Locale is chosen per-request from the `lang` cookie (set by the toggle) and
defaults to Indonesian.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

I18N_DIR = Path(__file__).resolve().parent.parent / "i18n"
DEFAULT_LANG = "id"
AVAILABLE = ("id", "en")


@functools.lru_cache(maxsize=len(AVAILABLE))
def _load(lang: str) -> dict[str, str]:
    path = I18N_DIR / f"{lang}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class Translator:
    def __init__(self, lang: str):
        self.lang = lang if lang in AVAILABLE else DEFAULT_LANG
        self._strings = _load(self.lang)
        self._fallback = _load("en")

    def __call__(self, key: str, **kw: object) -> str:
        raw = self._strings.get(key) or self._fallback.get(key) or key
        return raw.format(**kw) if kw else raw

    @property
    def other_lang(self) -> str:
        return "en" if self.lang == "id" else "id"


def translator_for(cookie_value: str | None) -> Translator:
    return Translator(cookie_value or DEFAULT_LANG)
