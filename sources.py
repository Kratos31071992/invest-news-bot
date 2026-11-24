"""Источники новостей: RSS-ленты финансовых изданий.

Каждая лента возвращает список NewsItem. Работает только на стандартной
библиотеке Python (urllib + xml.etree) — внешних зависимостей не нужно.
"""
from __future__ import annotations

import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from html import unescape

# Русскоязычные источники (используются по умолчанию)
RU_SOURCES: dict[str, str] = {
    "РБК Экономика": "https://rssexport.rbc.ru/rbcnews/news/30/full.rss",
    "Коммерсантъ Деньги": "https://www.kommersant.ru/RSS/money.xml",
    "Интерфакс": "https://www.interfax.ru/rss.asp",
    "ТАСС Экономика": "https://tass.ru/rss/v2.xml",
    "Ведомости": "https://www.vedomosti.ru/rss/news",
}

# Англоязычные (по желанию: fetch_news(include_en=True))
EN_SOURCES: dict[str, str] = {
    "CNBC Finance": "https://www.cnbc.com/id/10000664/device/rss/rss.html",
    "MarketWatch": "https://feeds.marketwatch.com/marketwatch/topstories/",
    "FT Markets": "https://www.ft.com/rss/home",
    "Investing.com": "https://www.investing.com/rss/news.rss",
}

# Полный список (для команды /sources)
RSS_SOURCES: dict[str, str] = {**RU_SOURCES, **EN_SOURCES}

USER_AGENT = "invest-news-bot/0.1 (+https://t.me/)"


@dataclass
class NewsItem:
    title: str
    summary: str
    link: str
    source: str
    published_ts: float = 0.0  # unix-время публикации, 0 если неизвестно
    tags: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.summary}"


def _strip_html(raw: str) -> str:
    raw = re.sub(r"<[^>]+>", " ", raw or "")
    return re.sub(r"\s+", " ", unescape(raw)).strip()


def _parse_date(el: ET.Element | None) -> float:
    if el is None or not el.text:
        return 0.0
    try:
        return parsedate_to_datetime(el.text.strip()).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _fetch_url(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse_rss(xml_bytes: bytes, source_name: str, limit: int) -> list[NewsItem]:
    root = ET.fromstring(xml_bytes)
    items: list[NewsItem] = []
    # Стандартный RSS 2.0
    for item in root.iter("item"):
        title = _strip_html(item.findtext("title", ""))
        if not title:
            continue
        items.append(
            NewsItem(
                title=title,
                summary=_strip_html(item.findtext("description", "")),
                link=(item.findtext("link", "") or "").strip(),
                source=source_name,
                published_ts=_parse_date(item.find("pubDate")),
            )
        )
        if len(items) >= limit:
            return items
    # Atom-лента (на случай источников без RSS 2.0)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
        title = _strip_html(entry.findtext("a:title", "", ns))
        if not title:
            continue
        link_el = entry.find("a:link", ns)
        items.append(
            NewsItem(
                title=title,
                summary=_strip_html(entry.findtext("a:summary", "", ns)),
                link=(link_el.get("href", "") if link_el is not None else ""),
                source=source_name,
                published_ts=_parse_date(entry.find("a:updated", ns)),
            )
        )
        if len(items) >= limit:
            break
    return items


def fetch_news(
    source_names: list[str] | None = None,
    per_source: int = 15,
    include_en: bool = True,
) -> list[NewsItem]:
    """Скачивает свежие новости из указанных (или всех) источников.

    Сбой одного источника не ломает остальные — он просто пропускается.
    """
    if source_names is not None:
        names = source_names
    else:
        names = list(RU_SOURCES) + (list(EN_SOURCES) if include_en else [])
    collected: list[NewsItem] = []
    for name in names:
        url = RSS_SOURCES.get(name)
        if not url:
            continue
        try:
            collected.extend(_parse_rss(_fetch_url(url), name, per_source))
        except Exception as exc:  # noqa: BLE001 — логируем и идём дальше
            print(f"[warn] источник '{name}' недоступен: {exc}")
    collected.sort(key=lambda n: n.published_ts or 0, reverse=True)
    return collected


def search_news(
    query: str,
    per_source: int = 25,
    include_en: bool = True,
    extra_queries: list[str] | None = None,
) -> list[NewsItem]:
    """Ищет свежие новости по запросу (и дополнительным вариантам, напр. переводу)."""
    variants = {query.lower()}
    variants.update(q.lower() for q in (extra_queries or []) if q)
    hits = [
        n
        for n in fetch_news(per_source=per_source, include_en=include_en)
        if any(v in n.text.lower() for v in variants)
    ]
    return hits


def fresh_since(hours: float = 24.0) -> list[NewsItem]:
    """Все новости за последние N часов."""
    cutoff = time.time() - hours * 3600
    return [n for n in fetch_news(per_source=40) if n.published_ts >= cutoff]
