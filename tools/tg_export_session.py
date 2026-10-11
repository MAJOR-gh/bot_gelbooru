"""Экспорт Telegram-сессии в строку для хостинга (env TG_SESSION_STRING).

Зачем: файл `tg_userbot.session` — секрет, он в .gitignore и НЕ доезжает до
хостинга через git/Docker. Поэтому переводим сессию в строку и кладём её на
хостинг как обычную переменную окружения (рядом с DISCORD_BOT_TOKEN).

Запусти ОДИН раз локально:  python tg_export_session.py

Поведение:
  • Если рядом есть рабочая `tg_userbot.session` — просто сконвертирует её в
    строку (телефон/код вводить НЕ нужно).
  • Если файла нет или сессия протухла — telethon попросит номер телефона и код
    из Telegram и создаст новую сессию.

Вывод — длинная строка. Скопируй её В ОДНУ СТРОКУ в переменную окружения
TG_SESSION_STRING на хостинге. Никому не показывай: это полный доступ к аккаунту.
"""
import os

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = int(os.environ.get("TG_API_ID") or input("API ID (число): ").strip())
API_HASH = os.environ.get("TG_API_HASH") or input("API hash: ").strip()

SESSION_NAME = "tg_userbot"  # существующий файл tg_userbot.session, если есть

# Контекст-менеджер сам поднимет сессию, а при необходимости проведёт интерактивный
# логин (телефон + код). client.session здесь — уже авторизованная файловая сессия.
with TelegramClient(SESSION_NAME, API_ID, API_HASH) as client:
    me = client.get_me()
    src = client.session

    # Переносим DC и auth_key из текущей сессии в StringSession и сериализуем.
    ss = StringSession()
    ss.set_dc(src.dc_id, src.server_address, src.port)
    ss.auth_key = src.auth_key
    session_string = ss.save()

print("\n" + "=" * 70)
print("✅ Аккаунт:", (me.username or me.first_name), f"(id {me.id})")
print("=" * 70)
print("\nСкопируй строку ниже в env хостинга как TG_SESSION_STRING (целиком, одной строкой):\n")
print(session_string)
print("\n⚠️  Это полный доступ к аккаунту — храни как пароль, в чат/репозиторий не вставляй.")
