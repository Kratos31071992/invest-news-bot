"""Перевод новостей EN→RU.

Два бесплатных движка без ключа:
  1. MyMemory — основной, результаты кэшируются в SQLite.
  2. Google Translate (неофициальная конечная точка gtx) — запасной,
     включается, если MyMemory недоступен, сдох по лимиту или вернул мусор.

При любой ошибке возвращается оригинал — сообщение не должно пропадать.
"""
from __future__ import annotations

import html
import json
import time
import urllib.parse
import urllib.request

import storage

_MYMEMORY = "https://api.mymemory.translated.net/get"
_GOOGLE = "https://translate.googleapis.com/translate_a/single"
_UA = "invest-news-bot/0.3"

# Фрагменты, которыми MyMemory отвечает при исчерпании лимита — это не перевод.
_BAD_MARKERS = ("MYMEMORY WARNING", "LIMIT REACHED", "QUERY LENGTH LIMIT", "INVALID")


def _looks_russian(text: str) -> bool:
    return any("а" <= ch <= "я" or ch == "ё" for ch in text.lower())


def _http_get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    return urllib.request.urlopen(req, timeout=timeout).read()


def _ok(text: str, original: str) -> bool:
    t = (text or "").strip()
    if not t or t.lower() == original.lower():
        return False
    return not any(m in t.upper() for m in _BAD_MARKERS)


def _via_mymemory(text: str, langpair: str) -> str:
    url = _MYMEMORY + "?" + urllib.parse.urlencode({"q": text[:450], "langpair": langpair})
    data = json.loads(_http_get(url))
    result = (data.get("responseData") or {}).get("translatedText", "")
    return html.unescape(result) if _ok(result, text) else ""


def _via_google(text: str, langpair: str) -> str:
    src, dst = langpair.split("|", 1)
    url = _GOOGLE + "?" + urllib.parse.urlencode(
        {"client": "gtx", "sl": src, "tl": dst, "dt": "t", "q": text[:450]}
    )
    data = json.loads(_http_get(url))
    parts = [p[0] for p in (data[0] or []) if p and p[0]]
    result = html.unescape("".join(parts))
    return result if _ok(result, text) else ""


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
    result = ""
    try:
        result = _via_mymemory(text, langpair)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] MyMemory недоступен: {exc}", flush=True)
    if not result:
        try:
            result = _via_google(text, langpair)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] запасной переводчик недоступен: {exc}", flush=True)
    if result:
        storage.set_translation(text, langpair, result)
        return result
    print(f"[warn] перевод не получен, отправляю оригинал: {text[:60]}", flush=True)
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
            time.sleep(0.3)  # не долбим бесплатные API
    return out
