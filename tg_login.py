"""Одноразовый логин Telegram-твинка для userbot'а.

Запусти ОДИН раз:  python tg_login.py
Скрипт спросит api_hash, номер телефона и код из Telegram, после чего создаст
файл сессии `tg_userbot.session` рядом. Дальше бот будет использовать его сам —
логиниться повторно не нужно.

Ничего из этого в чат вставлять НЕ надо: всё вводится локально в твоём терминале.
"""
import os
from telethon import TelegramClient

API_ID = os.environ.get("TG_API_ID") or input("API ID (число): ").strip()
API_HASH = os.environ.get("TG_API_HASH") or input("API hash: ").strip()

SESSION_NAME = "tg_userbot"  # → создаст файл tg_userbot.session

with TelegramClient(SESSION_NAME, int(API_ID), API_HASH) as client:
    me = client.loop.run_until_complete(client.get_me())
    uname = me.username or me.first_name
    print(f"\n✅ Успешный вход как: {uname} (id {me.id})")
    print(f"📁 Сессия сохранена в {SESSION_NAME}.session — храни этот файл локально.")
