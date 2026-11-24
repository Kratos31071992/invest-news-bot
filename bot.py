"""Телеграм-бот для анализа инвестиционных новостей.

Интерфейс построен на кнопках:
  📰 Новости   — свежие новости (RU + переведённые зарубежные)
  🔍 Анализ    — выбор темы кнопками или свой запрос
  🔮 Прогноз   — краткосрочный прогноз по новостному фону
  🔔 Подписки  — подписка/отписка на алерты по темам
  🌅 Дайджест  — ежедневная утренняя сводка рынка
  📡 Источники — откуда берутся новости

Зарубежные новости автоматически переводятся на русский (translator.py),
поиск по темам идёт одновременно по русскому и английскому запросам.

Команды дублируют кнопки: /latest /analyze /forecast /watch /unwatch /watchlist /digest /sources
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import config
from analyzer import analyze_items, llm_analysis, make_forecast, summarize
from sources import EN_SOURCES, RU_SOURCES, fetch_news, fresh_since, search_news
from storage import (
    add_watch,
    all_watchers,
    digest_enabled,
    digest_users,
    is_seen,
    list_watch,
    mark_seen,
    remove_watch,
    set_digest,
)
from translator import translate, translate_titles

bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

DISCLAIMER = "\n\n<i>⚠️ Это аналитическая сводка, а не инвестиционная рекомендация.</i>"
FORECAST_DISCLAIMER = (
    "\n\n<i>⚠️ Прогноз построен автоматически по новостному фону и отражает только "
    "настроение новостей, а не реальное движение цены. Это не инвестиционная рекомендация.</i>"
)
_SENTIMENT_ICON = {"бычья": "🟢", "медвежья": "🔴", "нейтральная": "⚪"}

# Темы для кнопок: код → (название на кнопке, русский запрос, английский запрос)
TOPICS: dict[str, tuple[str, str, str]] = {
    "oil": ("🛢 Нефть", "нефт", "oil"),
    "gas": ("🔥 Газ", "газ", "natural gas"),
    "gold": ("🥇 Золото", "золот", "gold"),
    "ruble": ("💱 Курс рубля", "рубл", "ruble"),
    "rate": ("🏦 Ставка ЦБ", "ставк", "interest rate"),
    "sber": ("🟢 Сбербанк", "сбербанк", "Sberbank"),
    "gazprom": ("🔵 Газпром", "газпром", "Gazprom"),
    "crypto": ("₿ Криптовалюта", "биткоин", "bitcoin"),
}

# Последний проанализированный запрос на чат — для кнопки «Прогноз по этой теме»
_LAST_ANALYSIS: dict[int, tuple[str, str | None, str]] = {}


class Form(StatesGroup):
    waiting_analyze_query = State()
    waiting_watch_query = State()
    waiting_forecast_query = State()


# ---------- Клавиатуры ----------

def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📰 Новости"), KeyboardButton(text="🔍 Анализ"), KeyboardButton(text="🔮 Прогноз")],
            [KeyboardButton(text="🔔 Подписки"), KeyboardButton(text="🌅 Дайджест"), KeyboardButton(text="📡 Источники")],
        ],
        resize_keyboard=True,
    )


def topics_keyboard(action: str) -> InlineKeyboardMarkup:
    """action: 'analyze', 'forecast' или 'watch' — что делать с выбранной темой."""
    buttons = [
        [InlineKeyboardButton(text=label, callback_data=f"{action}:{key}")]
        for key, (label, _, _) in TOPICS.items()
    ]
    custom = {"analyze": "✏️ Своя тема", "watch": "✏️ Своя подписка", "forecast": "✏️ Своя тема"}[action]
    buttons.append([InlineKeyboardButton(text=custom, callback_data=f"{action}:custom")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def unsub_keyboard(queries: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"❌ Отписаться: {q.split('|')[0]}",
                callback_data=f"unwatch:{q}",
            )]
            for q in queries
        ]
    )


def digest_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    toggle = (
        InlineKeyboardButton(text="🔕 Выключить", callback_data="digest:off")
        if enabled
        else InlineKeyboardButton(text="✅ Включить", callback_data="digest:on")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [toggle],
            [InlineKeyboardButton(text="👀 Посмотреть пример сейчас", callback_data="digest:now")],
        ]
    )


def forecast_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔮 Прогноз по этой теме", callback_data="forecast:last")]]
    )


# ---------- Форматирование ----------

def _fmt_analysis(a, ru_title: str | None = None) -> str:
    icon = _SENTIMENT_ICON[a.sentiment]
    stars = "★" * a.impact if a.impact else "·"
    title = escape(ru_title or a.item.title)
    foreign = " 🌐" if a.item.source in EN_SOURCES else ""
    return (
        f"{icon} <b>{title}</b> [{stars}]\n"
        f"{escape(a.item.source)}{foreign} — <a href=\"{escape(a.item.link, quote=True)}\">читать</a>"
    )


async def _translate_for_display(analyses) -> dict[str, str]:
    """Переводит заголовки зарубежных новостей из списка (в отдельном потоке)."""
    en_items = [a.item for a in analyses if a.item.source in EN_SOURCES]
    if not en_items:
        return {}
    return await asyncio.to_thread(translate_titles, en_items)


HELP = (
    "📊 <b>Инвест Аналитик</b>\n\n"
    "Читаю свежие новости из российских и зарубежных финансовых СМИ "
    "(зарубежные перевожу на русский), оцениваю их влияние на рынок "
    "и строю краткосрочный прогноз по новостному фону.\n\n"
    "Пользуйтесь кнопками внизу экрана 👇\n\n"
    "Или командами: /latest /analyze /forecast /watch /watchlist /digest /sources"
)


async def make_digest() -> str | None:
    """Формирует текст утреннего дайджеста по новостям за последние 24 часа."""
    items = await asyncio.to_thread(fresh_since, 24.0)
    analyses = analyze_items(items)
    if not analyses:
        return None
    top = analyses[:6]
    titles = await _translate_for_display(top)
    today = datetime.now().strftime("%d.%m.%Y")

    deep = await asyncio.to_thread(llm_analysis, [a.item for a in analyses], "главные события дня на рынках")
    intro = escape(deep) if deep else summarize(analyses)

    return (
        f"🌅 <b>Утренний дайджест рынка — {today}</b>\n\n"
        + intro
        + "\n\n📌 <b>Главные новости:</b>\n"
        + "\n\n".join(_fmt_analysis(a, titles.get(a.item.link or a.item.title)) for a in top)
        + DISCLAIMER
    )


async def show_news(target: Message, count: int = 6):
    wait = await target.answer("⏳ Собираю и анализирую новости (зарубежные перевожу)…")
    items = await asyncio.to_thread(fetch_news, None, 10)
    analyses = analyze_items(items)[:count]
    if not analyses:
        await wait.edit_text("Не удалось получить новости, попробуйте позже.")
        return
    titles = await _translate_for_display(analyses)
    text = "🗞 <b>Свежие новости и оценка:</b>\n\n" + "\n\n".join(
        _fmt_analysis(a, titles.get(a.item.link or a.item.title)) for a in analyses
    )
    await wait.edit_text(text + DISCLAIMER)


async def _search_bilingual(query: str, en_query: str | None):
    """Ищет новости по русскому и английскому вариантам запроса."""
    extra = [en_query] if en_query else None
    if not en_query and not query.isascii():
        en_tr = await asyncio.to_thread(translate, query, "ru|en")
        if en_tr.lower() != query.lower():
            extra = [en_tr]
    return await asyncio.to_thread(search_news, query, 25, True, extra)


async def run_analysis(target: Message, query: str, en_query: str | None = None, display: str | None = None):
    shown = display or query
    wait = await target.answer(f"⏳ Анализирую новости по теме «{escape(shown)}»…")

    items = await _search_bilingual(query, en_query)
    analyses = analyze_items(items)

    deep = await asyncio.to_thread(llm_analysis, [a.item for a in analyses], query)
    if deep:
        titles = await _translate_for_display(analyses[:3])
        header = f"🔍 <b>Анализ по теме «{escape(shown)}»</b>\n\n{escape(deep)}"
        top = "\n\n📌 Топ-новостей:\n" + "\n".join(
            _fmt_analysis(a, titles.get(a.item.link or a.item.title)) for a in analyses[:3]
        )
        await wait.edit_text(header + top + DISCLAIMER)
        _LAST_ANALYSIS[target.chat.id] = (query, en_query, shown)
        await target.answer("Хотите прогноз по этой теме?", reply_markup=forecast_button())
        return

    if not analyses:
        await wait.edit_text(
            f"За последние сутки новостей по теме «{escape(shown)}» не нашлось.\n"
            "Попробуйте другую тему или посмотрите 📰 Новости."
        )
        return
    top5 = analyses[:5]
    titles = await _translate_for_display(top5)
    text = (
        f"🔍 <b>Анализ по теме «{escape(shown)}»</b>\n\n"
        + summarize(analyses)
        + "\n\n📌 Наиболее значимые новости:\n"
        + "\n\n".join(_fmt_analysis(a, titles.get(a.item.link or a.item.title)) for a in top5)
    )
    await wait.edit_text(text + DISCLAIMER)
    _LAST_ANALYSIS[target.chat.id] = (query, en_query, shown)
    await target.answer("Хотите прогноз по этой теме?", reply_markup=forecast_button())


async def run_forecast(target: Message, query: str, en_query: str | None = None, display: str | None = None):
    shown = display or query
    wait = await target.answer(f"⏳ Строю прогноз по теме «{escape(shown)}» на основе свежих новостей…")

    items = await _search_bilingual(query, en_query)
    analyses = analyze_items(items)
    fc = make_forecast(analyses)

    if not fc or fc.news_count < 3:
        await wait.edit_text(
            f"По теме «{escape(shown)}» слишком мало свежих новостей ({fc.news_count if fc else 0}) "
            "для прогноза.\nПопробуйте более широкую тему или загляните позже."
        )
        return

    titles = await _translate_for_display(fc.factors)
    factors = "\n".join(
        f"{_SENTIMENT_ICON[a.sentiment]} {escape(titles.get(a.item.link or a.item.title, a.item.title))}"
        for a in fc.factors
    )
    text = (
        f"🔮 <b>Прогноз по теме «{escape(shown)}»</b>\n\n"
        f"Направление: {fc.emoji} <b>{fc.direction}</b>\n"
        f"Уверенность: <b>{fc.confidence}%</b> ({fc.confidence_word})\n"
        f"Горизонт: {fc.horizon}\n"
        f"На основе {fc.news_count} свежих новостей\n\n"
        f"📌 <b>Ключевые факторы:</b>\n{factors}"
        + FORECAST_DISCLAIMER
    )
    await wait.edit_text(text)


async def show_subscriptions(target: Message, chat_id: int):
    subs = list_watch(chat_id)
    text = (
        "🔔 <b>Ваши подписки</b>\n\n"
        + ("\n".join(f"• {escape(q.split('|')[0])}" for q in subs) if subs else "Пока пусто.")
        + "\n\nВыберите тему, чтобы подписаться:"
    )
    kb_rows = [
        [InlineKeyboardButton(text=label, callback_data=f"watch:{key}")]
        for key, (label, _, _) in TOPICS.items()
    ]
    kb_rows.append([InlineKeyboardButton(text="✏️ Своя подписка", callback_data="watch:custom")])
    markup = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await target.answer(text, reply_markup=markup)
    if subs:
        await target.answer("Отписаться от темы:", reply_markup=unsub_keyboard(subs))


# ---------- Обработчики кнопок главного меню ----------

@dp.message(Command("start", "help"))
@dp.message(F.text == "ℹ️ Помощь")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(HELP, reply_markup=main_keyboard())


@dp.message(Command("latest"))
@dp.message(F.text == "📰 Новости")
async def cmd_latest(message: Message, command: CommandObject = None):
    count = 6
    if command and command.args and command.args.strip().isdigit():
        count = max(1, min(10, int(command.args.strip())))
    await show_news(message, count)


@dp.message(Command("analyze"))
@dp.message(F.text == "🔍 Анализ")
async def cmd_analyze(message: Message, state: FSMContext, command: CommandObject = None):
    await state.clear()
    if command and command.args:
        await run_analysis(message, command.args.strip())
        return
    await message.answer(
        "🔍 <b>Что анализируем?</b> Выберите тему кнопкой или введите свою:",
        reply_markup=topics_keyboard("analyze"),
    )


@dp.message(Command("forecast"))
@dp.message(F.text == "🔮 Прогноз")
async def cmd_forecast(message: Message, state: FSMContext, command: CommandObject = None):
    await state.clear()
    if command and command.args:
        await run_forecast(message, command.args.strip())
        return
    await message.answer(
        "🔮 <b>Прогноз по какой теме построить?</b>\n"
        "Оценю новостной фон и дам направление на ближайшие дни:",
        reply_markup=topics_keyboard("forecast"),
    )


@dp.message(Command("watchlist"))
@dp.message(F.text == "🔔 Подписки")
async def cmd_watchlist(message: Message, state: FSMContext):
    await state.clear()
    await show_subscriptions(message, message.chat.id)


@dp.message(Command("digest"))
@dp.message(F.text == "🌅 Дайджест")
async def cmd_digest(message: Message, state: FSMContext):
    await state.clear()
    enabled = digest_enabled(message.chat.id)
    status = "включён ✅" if enabled else "выключен 🔕"
    await message.answer(
        f"🌅 <b>Утренний дайджест</b>\n\n"
        f"Каждое утро в <b>{escape(config.DIGEST_TIME)}</b> присылаю сводку: "
        f"общий фон рынка за сутки и главные новости.\n\n"
        f"Статус: <b>{status}</b>",
        reply_markup=digest_keyboard(enabled),
    )


@dp.message(Command("sources"))
@dp.message(F.text == "📡 Источники")
async def cmd_sources(message: Message):
    ru = "\n".join(f"• {escape(n)}" for n in RU_SOURCES)
    en = "\n".join(f"• {escape(n)} 🌐 (переводится)" for n in EN_SOURCES)
    await message.answer(
        f"📡 <b>Источники новостей:</b>\n\n<b>Российские:</b>\n{ru}\n\n<b>Зарубежные:</b>\n{en}"
    )


@dp.message(Command("watch"))
async def cmd_watch(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Укажите тему, например: /watch нефть")
        return
    query = command.args.strip()
    stored = await _dual_query(query)
    if add_watch(message.chat.id, stored):
        await message.answer(f"✅ Подписка на «{escape(query)}» оформлена!")
    else:
        await message.answer(f"Вы уже подписаны на «{escape(query)}».")


@dp.message(Command("unwatch"))
async def cmd_unwatch(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Укажите тему, например: /unwatch нефть")
        return
    removed = False
    for q in list_watch(message.chat.id):
        if q.split("|")[0] == command.args.strip().lower():
            removed = remove_watch(message.chat.id, q) or removed
    if removed:
        await message.answer(f"🗑 Подписка на «{escape(command.args.strip())}» удалена.")
    else:
        await message.answer("Такой подписки не найдено.")


# ---------- Инлайн-кнопки ----------

@dp.callback_query(F.data.startswith("analyze:"))
async def cb_analyze(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":", 1)[1]
    if key == "custom":
        await state.set_state(Form.waiting_analyze_query)
        await cb.message.answer("✏️ Введите свою тему для анализа (например: алюминий, Норникель, IPO):")
        await cb.answer()
        return
    label, ru_q, en_q = TOPICS[key]
    await cb.answer(f"Анализирую: {label}")
    await run_analysis(cb.message, ru_q, en_q, display=label.split(" ", 1)[1])


@dp.callback_query(F.data == "forecast:last")
async def cb_forecast_last(cb: CallbackQuery):
    saved = _LAST_ANALYSIS.get(cb.message.chat.id)
    if not saved:
        await cb.message.answer("Сначала сделайте анализ темы (кнопка 🔍 Анализ).")
        await cb.answer()
        return
    ru_q, en_q, shown = saved
    await cb.answer(f"Прогноз: {shown}")
    await run_forecast(cb.message, ru_q, en_q, display=shown)


@dp.callback_query(F.data.startswith("forecast:"))
async def cb_forecast(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":", 1)[1]
    if key == "custom":
        await state.set_state(Form.waiting_forecast_query)
        await cb.message.answer("✏️ Введите тему для прогноза (например: золото, Мосбиржа, нефть Brent):")
        await cb.answer()
        return
    if key == "last":  # обработан отдельным хендлером выше
        await cb.answer()
        return
    label, ru_q, en_q = TOPICS[key]
    await cb.answer(f"Прогноз: {label}")
    await run_forecast(cb.message, ru_q, en_q, display=label.split(" ", 1)[1])


@dp.callback_query(F.data.startswith("watch:"))
async def cb_watch(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":", 1)[1]
    if key == "custom":
        await state.set_state(Form.waiting_watch_query)
        await cb.message.answer("✏️ Введите тему подписки (например: Сбербанк):")
        await cb.answer()
        return
    label, ru_q, en_q = TOPICS[key]
    if add_watch(cb.message.chat.id, f"{ru_q}|{en_q}"):
        await cb.message.answer(f"✅ Подписка на «{label}» оформлена! Пришлю алерт о значимых новостях (🇷🇺 и 🌐).")
    else:
        await cb.message.answer(f"Вы уже подписаны на «{label}».")
    await cb.answer()


@dp.callback_query(F.data.startswith("unwatch:"))
async def cb_unwatch(cb: CallbackQuery):
    query = cb.data.split(":", 1)[1]
    if remove_watch(cb.message.chat.id, query):
        await cb.message.edit_text(f"🗑 Подписка на «{escape(query.split('|')[0])}» удалена.")
    else:
        await cb.message.edit_text("Подписка уже удалена.")
    await cb.answer()


@dp.callback_query(F.data == "digest:on")
async def cb_digest_on(cb: CallbackQuery):
    set_digest(cb.message.chat.id, True)
    await cb.message.edit_text(
        f"✅ Утренний дайджест включён! Буду присылать сводку каждый день в {escape(config.DIGEST_TIME)}.",
        reply_markup=digest_keyboard(True),
    )
    await cb.answer()


@dp.callback_query(F.data == "digest:off")
async def cb_digest_off(cb: CallbackQuery):
    set_digest(cb.message.chat.id, False)
    await cb.message.edit_text(
        "🔕 Утренний дайджест выключен.",
        reply_markup=digest_keyboard(False),
    )
    await cb.answer()


@dp.callback_query(F.data == "digest:now")
async def cb_digest_now(cb: CallbackQuery):
    await cb.answer("Готовлю дайджест…")
    text = await make_digest()
    if text:
        await cb.message.answer(text)
    else:
        await cb.message.answer("Не удалось собрать новости, попробуйте позже.")


# ---------- Свободный ввод (своя тема) ----------

async def _dual_query(query: str) -> str:
    """Для русского запроса добавляет английский вариант: 'нефт|oil'."""
    if query.isascii():
        return query
    en = await asyncio.to_thread(translate, query, "ru|en")
    if en and en.lower() != query.lower() and en.isascii():
        return f"{query}|{en}"
    return query


@dp.message(Form.waiting_analyze_query)
async def fsm_analyze(message: Message, state: FSMContext):
    await state.clear()
    await run_analysis(message, message.text.strip())


@dp.message(Form.waiting_forecast_query)
async def fsm_forecast(message: Message, state: FSMContext):
    await state.clear()
    await run_forecast(message, message.text.strip())


@dp.message(Form.waiting_watch_query)
async def fsm_watch(message: Message, state: FSMContext):
    await state.clear()
    query = message.text.strip()
    stored = await _dual_query(query)
    if add_watch(message.chat.id, stored):
        await message.answer(f"✅ Подписка на «{escape(query)}» оформлена!")
    else:
        await message.answer(f"Вы уже подписаны на «{escape(query)}».")


# ---------- Фоновые задачи ----------

async def alert_loop():
    """Проверяет новые новости и шлёт алерты подписчикам."""
    await asyncio.sleep(10)
    while True:
        try:
            watchers = all_watchers()
            if watchers:
                items = await asyncio.to_thread(fetch_news, None, 25)
                fresh = [i for i in items if i.link and not is_seen(i.link)]
                for chat_id, queries in watchers.items():
                    for q in queries:
                        variants = [v for v in q.lower().split("|") if v]
                        hits = [
                            i for i in fresh
                            if any(v in i.text.lower() for v in variants)
                        ]
                        analyses = [a for a in analyze_items(hits) if a.impact >= 2]
                        titles = await _translate_for_display(analyses[:3])
                        for a in analyses[:3]:
                            await bot.send_message(
                                chat_id,
                                "🚨 <b>Алерт по подписке «" + escape(variants[0]) + "»</b>\n\n"
                                + _fmt_analysis(a, titles.get(a.item.link or a.item.title)),
                            )
                mark_seen([i.link for i in fresh])
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] alert_loop: {exc}")
        await asyncio.sleep(config.ALERT_INTERVAL_SEC)


async def digest_loop():
    """Каждый день в DIGEST_TIME рассылает утренний дайджест подписчикам."""
    last_sent_date: str | None = None
    await asyncio.sleep(15)
    while True:
        try:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if now.strftime("%H:%M") == config.DIGEST_TIME and last_sent_date != today:
                users = digest_users()
                if users:
                    text = await make_digest()
                    if text:
                        for chat_id in users:
                            try:
                                await bot.send_message(chat_id, text)
                            except Exception as exc:  # noqa: BLE001
                                print(f"[warn] дайджест не доставлен {chat_id}: {exc}")
                    last_sent_date = today
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] digest_loop: {exc}")
        await asyncio.sleep(30)  # проверяем время дважды в минуту


async def main():
    config.validate()
    await bot.set_my_commands([
        BotCommand(command="latest", description="📰 Свежие новости"),
        BotCommand(command="analyze", description="🔍 Анализ по теме"),
        BotCommand(command="forecast", description="🔮 Прогноз по теме"),
        BotCommand(command="watchlist", description="🔔 Мои подписки"),
        BotCommand(command="digest", description="🌅 Утренний дайджест"),
        BotCommand(command="sources", description="📡 Источники"),
        BotCommand(command="help", description="ℹ️ Помощь"),
    ])
    asyncio.create_task(alert_loop())
    asyncio.create_task(digest_loop())
    print("Бот запущен. Ctrl+C для остановки.", flush=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
