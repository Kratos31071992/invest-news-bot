"""Перевод новостей EN→RU.

Бесплатный сервис MyMemory (без ключа), результаты кэшируются в SQLite,
чтобы не переводить одно и то же повторно и не упираться в лимиты.
Если задан OPENAI_API_KEY — можно было бы переводить через LLM,
но MyMemory полностью закрывает задачу.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

import storage

_API = "https://api.mymemory.translated.net/get"
_UA = "invest-news-bot/0.2"


def _looks_russian(text: str) -> bool:
    return any("а" <= ch <= "я" or ch == "ё" for ch in text.lower())


def translate(text: str, langpair: str = "en|ru") -> str:
    """Переводит текст. При любой ошибке возвращает оригинал."""
    text = (text or "").strip()
    if not text:
        return text
    if langpair == "en|ru" and _looks_russian(text):
        return text
    cached = storage.get_translation(text, langpair)
    if cached:
        return cached
    try:
        url = _API + "?" + urllib.parse.urlencode({"q": text[:450], "langpair": langpair})
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        result = (data.get("responseData") or {}).get("translatedText", "")
        if result and result.lower() != text.lower():
            storage.set_translation(text, langpair, result)
            return result
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] перевод недоступен: {exc}")
    return text


def translate_titles(items, langpair: str = "en|ru") -> dict[str, str]:
    """Переводит заголовки списка новостей.

    Возвращает {ключ новости: переведённый заголовок}.
    Ключ — link, если есть, иначе title.
    """
    out: dict[str, str] = {}
    for i, it in enumerate(items):
        key = it.link or it.title
        out[key] = translate(it.title, langpair)
        if i < len(items) - 1:
            time.sleep(0.3)  # не долбим бесплатный API
    return out
