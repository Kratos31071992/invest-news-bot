"""Конфигурация: читаем переменные окружения / файл .env"""
from __future__ import annotations

import os
from pathlib import Path

# Подхватываем .env без внешних зависимостей
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
# Как часто проверять новые новости для подписчиков (секунды)
ALERT_INTERVAL_SEC = int(os.environ.get("ALERT_INTERVAL_SEC", "900"))  # 15 минут
# Время утреннего дайджеста (локальное время компьютера), формат ЧЧ:ММ
DIGEST_TIME = os.environ.get("DIGEST_TIME", "09:00")


def validate() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "❌ Не задан BOT_TOKEN.\n"
            "1. Создайте бота у @BotFather в Telegram и получите токен.\n"
            "2. Скопируйте .env.example в .env и впишите токен,\n"
            "   либо запустите: BOT_TOKEN=123:abc python bot.py"
        )
