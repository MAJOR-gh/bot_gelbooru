"""Вступить в каналы под твинком по инвайт-ссылкам (одноразово).

python tg_join.py
Вставляй ссылки по одной: приватные t.me/+xxxx (или t.me/joinchat/xxxx)
либо публичные @username / t.me/username. Пустая строка — выход.
"""
import os
import asyncio

from telethon import TelegramClient, errors
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.channels import JoinChannelRequest

API_ID = os.environ.get("TG_API_ID") or input("API ID (число): ").strip()
API_HASH = os.environ.get("TG_API_HASH") or input("API hash: ").strip()

_PRIVATE_MARKERS = ("t.me/+", "t.me/joinchat/", "telegram.me/+", "telegram.me/joinchat/")


async def join(client: TelegramClient, link: str):
    link = link.strip()
    for marker in _PRIVATE_MARKERS:
        if marker in link:
            invite_hash = link.split(marker, 1)[1].strip("/")
            await client(ImportChatInviteRequest(invite_hash))
            return
    username = link.rsplit("/", 1)[-1].lstrip("@")  # публичный канал
    await client(JoinChannelRequest(username))


async def main():
    async with TelegramClient("tg_userbot", int(API_ID), API_HASH) as client:
        while True:
            link = input("\nИнвайт-ссылка (пусто = выход): ").strip()
            if not link:
                break
            try:
                await join(client, link)
                print("  ✅ вступил")
            except errors.UserAlreadyParticipantError:
                print("  ℹ️ уже участник")
            except errors.InviteRequestSentError:
                print("  ⏳ заявка отправлена — ждёт одобрения админа канала")
            except Exception as e:
                print(f"  ❌ ошибка: {type(e).__name__}: {e}")


asyncio.run(main())
