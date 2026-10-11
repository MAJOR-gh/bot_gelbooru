"""Показать каналы/группы, на которые подписан твинк, с их ID.

Запусти:  python tg_list_channels.py
Скопируй нужные `peer=<число>` в tg_channels.json (для приватных каналов
именно число — это и есть идентификатор; @username им не нужен).

Список также сохраняется в tg_found_channels.json — на случай, если консоль
Windows не может напечатать эмодзи в названии канала.
"""
import os
import sys
import json
import asyncio

# Windows-консоль по умолчанию cp1251 и падает на эмодзи в названиях каналов.
# Принудительно переключаем вывод на UTF-8, а непечатаемое заменяем на «?».
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from telethon import TelegramClient
from telethon.tl.types import Channel

API_ID = os.environ.get("TG_API_ID") or input("API ID (число): ").strip()
API_HASH = os.environ.get("TG_API_HASH") or input("API hash: ").strip()


async def main():
    found = []
    async with TelegramClient("tg_userbot", int(API_ID), API_HASH) as client:
        print("\n=== Каналы и супергруппы аккаунта ===\n")
        async for dialog in client.iter_dialogs():
            ent = dialog.entity
            if isinstance(ent, Channel):  # каналы + супергруппы (не личка/боты)
                uname = f"@{ent.username}" if getattr(ent, "username", None) else "—"
                found.append({"peer": dialog.id, "username": ent.username, "name": dialog.name})
                # Печатаем безопасно: даже если консоль не вытянет символ — не упадём.
                line = f"  peer={dialog.id:<16} username={uname:<22} {dialog.name}"
                sys.stdout.buffer.write(line.encode("utf-8", "replace"))
                sys.stdout.buffer.write(b"\n")
        print("\nДля приватных каналов в tg_channels.json в поле \"peer\" ставь число "
              "(например -1001234567890).")

    # Дублируем в файл — кодировка тут уже не помеха.
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tg_found_channels.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(found, f, ensure_ascii=False, indent=2)
    print(f"\n📄 Полный список сохранён в {out} ({len(found)} шт.)")


asyncio.run(main())
