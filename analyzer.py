"""Анализатор новостей.

Два режима:
1. Встроенный (всегда работает): лексический анализ тональности по
   словарям «бычьих» и «медвежьих» слов + оценка значимости новости.
2. LLM-режим (опционально): если задан OPENAI_API_KEY, анализ делается
   через OpenAI-совместимый API (gpt-4o-mini по умолчанию).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from sources import NewsItem

BULL_WORDS = {
    "рост", "вырос", "растёт", "повышение", "прибыль", "рекорд", "дивиденды",
    "уверенно", "прогноз повышен", "инвестиции", "выкуп", "байбек",
    "growth", "surge", "rally", "record", "profit", "beat", "upgrade",
    "raise", "dividend", "buyback", "bullish", "soar", "gain", "outperform",
}
BEAR_WORDS = {
    "падение", "упал", "снижение", "убыток", "кризис", "санкции", "дефолт",
    "штраф", "обвал", "отток", "дефицит", "риск", "прогноз понижен",
    "fall", "drop", "plunge", "loss", "crisis", "sanction", "default",
    "downgrade", "miss", "bearish", "slump", "recession", "fear", "layoff",
}
# Слова, повышающие значимость новости для рынка
IMPACT_WORDS = {
    "цб", "ставк", "ключев", "инфляц", "ввп", "нефть", "газ", "рубл",
    "доллар", "санкц", "дивиденд", "отчётност", "ipo", "офз",
    "fed", "rate", "inflation", "gdp", "oil", "treasury", "tariff",
    "earnings", "opec",
}


@dataclass
class NewsAnalysis:
    item: NewsItem
    sentiment: str            # "бычья" | "медвежья" | "нейтральная"
    sentiment_score: float    # от -1 (медвежья) до +1 (бычья)
    impact: int               # значимость 0..5
    bull_hits: list[str]
    bear_hits: list[str]


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zа-яё]+", text.lower())


def analyze_item(item: NewsItem) -> NewsAnalysis:
    tokens = _tokenize(item.text)
    text_l = item.text.lower()
    bull = sorted({w for w in BULL_WORDS if any(t.startswith(w) for t in tokens)})
    bear = sorted({w for w in BEAR_WORDS if any(t.startswith(w) for t in tokens)})
    impact = min(5, sum(1 for w in IMPACT_WORDS if w in text_l))

    total = len(bull) + len(bear)
    if total == 0:
        sentiment, score = "нейтральная", 0.0
    else:
        score = round((len(bull) - len(bear)) / total, 2)
        sentiment = "бычья" if score > 0.2 else "медвежья" if score < -0.2 else "нейтральная"
    return NewsAnalysis(item, sentiment, score, impact, bull, bear)


def analyze_items(items: list[NewsItem]) -> list[NewsAnalysis]:
    return sorted(
        (analyze_item(i) for i in items),
        key=lambda a: (a.impact, abs(a.sentiment_score)),
        reverse=True,
    )


def summarize(analyses: list[NewsAnalysis]) -> str:
    """Сводный вердикт по набору новостей."""
    if not analyses:
        return "Новостей по запросу не найдено за последние сутки."
    bulls = sum(1 for a in analyses if a.sentiment == "бычья")
    bears = sum(1 for a in analyses if a.sentiment == "медвежья")
    neutral = len(analyses) - bulls - bears
    avg = sum(a.sentiment_score for a in analyses) / len(analyses)

    if avg > 0.15:
        verdict = "📈 Фон скорее позитивный (бычий)"
    elif avg < -0.15:
        verdict = "📉 Фон скорее негативный (медвежий)"
    else:
        verdict = "➖ Фон нейтральный / смешанный"

    return (
        f"{verdict}\n"
        f"Новостей: {len(analyses)} — бычьих {bulls}, медвежьих {bears}, нейтральных {neutral}.\n"
        f"Средний скор тональности: {avg:+.2f} (от -1 до +1)."
    )


# ---------- Опциональный LLM-режим ----------

def llm_available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def llm_analysis(items: list[NewsItem], query: str) -> str | None:
    """Глубокий анализ через OpenAI-совместимый API. None, если не настроен."""
    if not llm_available():
        return None
    try:
        from openai import OpenAI  # импорт только при наличии ключа

        client = OpenAI(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ.get("OPENAI_BASE_URL") or None,
        )
        digest = "\n\n".join(
            f"[{i.source}] {i.title}\n{i.summary[:400]}" for i in items[:10]
        )
        resp = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты — аналитик инвестиционного рынка. По новостям дай краткий "
                        "структурированный разбор: 1) ключевые события, 2) настроение "
                        "рынка (бычье/медвежье/смешанное), 3) какие активы/сектора "
                        "затронуты и как, 4) риски. Пиши по-русски, сжато, маркированным "
                        "списком. Добавь дисклеймер, что это не инвестиционная совет."
                    ),
                },
                {"role": "user", "content": f"Запрос: {query}\n\nНовости:\n{digest}"},
            ],
            temperature=0.3,
        )
        return resp.choices[0].message.content
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] LLM-анализ недоступен: {exc}")
        return None


# ---------- Прогноз на основе новостного фона ----------

@dataclass
class Forecast:
    direction: str        # "рост" | "снижение" | "боковик"
    emoji: str            # 📈 | 📉 | ➡️
    confidence: int       # 0..90 %
    confidence_word: str  # "низкая" | "средняя" | "высокая"
    horizon: str          # ориентировочный горизонт
    news_count: int
    factors: list[NewsAnalysis]  # ключевые новости-факторы


def make_forecast(analyses: list[NewsAnalysis]) -> Forecast | None:
    """Строит краткосрочный прогноз по набору проанализированных новостей.

    Логика: взвешенная тональность (важные новости весят больше) +
    согласованность сигналов (доля новостей в ведущем направлении).
    """
    if not analyses:
        return None

    n = len(analyses)
    bulls = sum(1 for a in analyses if a.sentiment == "бычья")
    bears = sum(1 for a in analyses if a.sentiment == "медвежья")

    weight = sum(1 + a.impact for a in analyses)
    score = sum(a.sentiment_score * (1 + a.impact) for a in analyses) / weight if weight else 0

    if score > 0.08:
        direction, emoji = "рост", "📈"
    elif score < -0.08:
        direction, emoji = "снижение", "📉"
    else:
        direction, emoji = "боковик", "➡️"

    consistency = max(bulls, bears, n - bulls - bears) / n
    confidence = round(min(90, (25 + n * 4) * (0.4 + 0.6 * consistency)))
    confidence_word = "высокая" if confidence >= 65 else "средняя" if confidence >= 45 else "низкая"
    horizon = "1–3 дня" if n < 8 else "до недели"

    return Forecast(
        direction=direction,
        emoji=emoji,
        confidence=confidence,
        confidence_word=confidence_word,
        horizon=horizon,
        news_count=n,
        factors=analyses[:4],
    )
