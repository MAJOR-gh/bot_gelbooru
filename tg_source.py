"""Telegram-источник: арты из каналов через userbot (Telethon).

Нужны TG_API_ID / TG_API_HASH и сессия: на хостинге — строка TG_SESSION_STRING,
локально — файл tg_userbot.session (оба даёт tools/tg_export_session.py).
"""
import json
import logging
import os
import time

from telethon import TelegramClient
from telethon.sessions import StringSession

import config

logger = logging.getLogger("gelbooru_bot.tg")

SESSION_NAME = "tg_userbot"
CHANNELS_FILE = os.path.join(config.BASE_DIR, "tg_channels.json")
HISTORY_LIMIT = 200           # сколько последних сообщений канала смотрим за запрос

# ── Эвристики отсева рекламы ───────────────────────────────────────────────────
# Подпись с любым из этих фрагментов считаем рекламой (регистр игнорируется).
AD_KEYWORDS = (
    "реклама", "рекламы", "спонсор", "партнёр", "партнер", "partner",
    "промокод", "промо", "розыгрыш", "конкурс", "вступай", "подпишись",
    "подписывайся", "переходи", "ставк", "казино", "betting", "промоакц",
    "t.me/+", "telega.in", "telega.io", "@admin", "по рекламе", "сотруднич",
    "vpn", "доход", "заработ",
)


# ── Клиент ─────────────────────────────────────────────────────────────────────
async def start_client(api_id: int, api_hash: str) -> TelegramClient:
    """Поднять Telethon-клиент на уже авторизованной сессии (в текущем loop)."""
    if config.TG_SESSION_STRING:
        logger.info("[tg] использую сессию из TG_SESSION_STRING (env)")
        client = TelegramClient(StringSession(config.TG_SESSION_STRING), api_id, api_hash)
    else:
        logger.info("[tg] использую файловую сессию tg_userbot.session")
        client = TelegramClient(SESSION_NAME, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "Сессия Telegram не авторизована: запусти `python tools/tg_export_session.py` "
            "и задай TG_SESSION_STRING (или положи tg_userbot.session рядом с ботом)."
        )
    # Прогреваем кэш диалогов: без этого приватные каналы (по числовому ID, без
    # @username) не резолвятся в iter_messages.
    try:
        await client.get_dialogs()
    except Exception as e:
        logger.warning("[tg] get_dialogs не удался (приватные каналы могут не открыться): %s", e)
    return client


def _peer_value(peer: str | int):
    """@username → строка; число (в т.ч. отрицательное) → int; иначе строка как есть."""
    s = str(peer).strip()
    if s.startswith("@"):
        return s
    try:
        return int(s)
    except ValueError:
        return s


# ── Каналы ─────────────────────────────────────────────────────────────────────
def load_channels() -> list[dict]:
    """Список каналов из tg_channels.json: [{'alias':..., 'peer':...}, ...]."""
    if not os.path.isfile(CHANNELS_FILE):
        return []
    try:
        with open(CHANNELS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error("[tg] не удалось прочитать %s: %s", CHANNELS_FILE, e)
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        alias = str(item.get("alias", "")).strip()
        peer = str(item.get("peer", "")).strip()
        if alias and peer:
            out.append({"alias": alias, "peer": peer})
    return out


# ── Разбор сообщения ───────────────────────────────────────────────────────────
def reaction_count(msg) -> int:
    """Суммарное число реакций под постом (наш суррогат score)."""
    r = getattr(msg, "reactions", None)
    results = getattr(r, "results", None) if r else None
    if not results:
        return 0
    return sum(int(getattr(x, "count", 0) or 0) for x in results)


def has_visual_media(msg) -> bool:
    """True, если в посте есть картинка/гиф/видео (а не просто текст/файл)."""
    if getattr(msg, "photo", None):
        return True
    f = getattr(msg, "file", None)
    mime = (getattr(f, "mime_type", None) or "") if f else ""
    return mime.startswith("image/") or mime.startswith("video/")


def post_is_ad(msgs) -> bool:
    """Мягкая эвристика рекламы для поста (одиночного или альбома целиком).

    Реклама: ни одной картинки/видео, инлайн-кнопки или рекламные слова в
    подписи. У альбома подпись висит лишь на одном сообщении — поэтому решение
    принимается по всему альбому, а не по каждой картинке отдельно.
    """
    if not isinstance(msgs, (list, tuple)):
        msgs = [msgs]
    if not any(has_visual_media(m) for m in msgs):
        return True
    for m in msgs:
        if getattr(m, "reply_markup", None) is not None:  # инлайн-кнопки
            return True
        text = (getattr(m, "message", None) or "").lower()
        if any(kw in text for kw in AD_KEYWORDS):
            return True
    return False


# Скан канала кэшируется: повторный /tg по тому же каналу не листает историю заново.
SCAN_TTL = 600.0
_scan_cache: dict[str, tuple[float, list[dict]]] = {}


async def fetch_channel_arts(client: TelegramClient, alias: str, peer: str,
                             limit: int = HISTORY_LIMIT) -> list[dict]:
    """Просмотреть историю канала → список арт-постов (реклама отсеяна).

    Альбом = один пост. Каждый пост: {id, score(=реакции), _site, _alias,
    _username, _peer_id, _peer, _uid, _legacy_uid, _msg, _msgs, _edge}.
    """
    hit = _scan_cache.get(alias)
    if hit and time.monotonic() - hit[0] < SCAN_TTL:
        return hit[1]

    peer_val = _peer_value(peer)
    username = peer_val[1:] if isinstance(peer_val, str) and peer_val.startswith("@") else None
    peer_id = peer_val if isinstance(peer_val, int) else None
    groups: dict[str, list] = {}
    try:
        async for msg in client.iter_messages(peer_val, limit=limit):
            gid = getattr(msg, "grouped_id", None)
            groups.setdefault(f"g{gid}" if gid else str(msg.id), []).append(msg)
    except Exception as e:
        logger.error("[tg] ошибка чтения канала %s: %s: %s", alias, type(e).__name__, e)
        return []

    oldest = min((m.id for msgs in groups.values() for m in msgs), default=None)
    posts: list[dict] = []
    for key, msgs in groups.items():
        if post_is_ad(msgs):
            continue
        msgs.sort(key=lambda m: m.id)
        head = msgs[0]
        canonical_peer = getattr(getattr(head, "peer_id", None), "channel_id", None) or peer_val
        posts.append({
            "id": head.id,
            # реакции у альбома висят на одном из сообщений — берём максимум
            "score": max(reaction_count(m) for m in msgs),
            "_site": f"TG:{canonical_peer}",
            "_alias": alias,
            "_username": username,
            "_peer_id": peer_id,
            "_peer": peer_val,
            # общий uid альбома: анти-повтор не покажет его же через другую картинку
            "_uid": f"TG:{canonical_peer}:{key}",
            "_legacy_uid": f"TG:{alias}:{key}",
            "_msg": head,
            "_msgs": msgs,
            # Альбом на самом краю просмотренной истории мог попасть в скан не целиком.
            "_edge": bool(getattr(head, "grouped_id", None)) and head.id == oldest,
        })
    _scan_cache[alias] = (time.monotonic(), posts)
    return posts


async def download_media(client: TelegramClient, msg, max_size: int):
    """Скачать медиа поста в байты с учётом лимита размера.

    Возвращает (data, size, reason); reason='too_big' если файл слишком большой.
    """
    f = getattr(msg, "file", None)
    size = getattr(f, "size", None) if f else None
    if size and size > max_size:
        return None, size, "too_big"
    try:
        data = await client.download_media(msg, file=bytes)
    except Exception as e:
        logger.error("[tg] не скачалось медиа: %s", e)
        return None, size, "error"
    if not data:
        return None, size, "error"
    if len(data) > max_size:
        return None, len(data), "too_big"
    return data, len(data), None


async def fetch_album_messages(client: TelegramClient, peer, msg) -> list:
    """Вернуть все сообщения альбома (grouped_id), или [msg] если пост одиночный.

    Нужно для альбома на границе скана (post["_edge"]): дочитываем соседние
    сообщения. При ошибке сети — None (возьмём то, что уже есть).
    """
    grouped_id = getattr(msg, "grouped_id", None)
    if not grouped_id:
        return [msg]
    msgs = []
    try:
        async for m in client.iter_messages(peer, min_id=msg.id - 20, max_id=msg.id + 20):
            if getattr(m, "grouped_id", None) == grouped_id:
                msgs.append(m)
    except Exception as e:
        logger.warning("[tg] альбом не дочитан: %s: %s", type(e).__name__, e)
        return None
    msgs.sort(key=lambda m: m.id)  # хронологический порядок, как в исходном посте
    return msgs or [msg]


VIDEO_EXTS = frozenset({"mp4", "webm", "mov", "m4v", "gif"})


def is_video(msg) -> bool:
    return media_ext(msg) in VIDEO_EXTS


def media_ext(msg) -> str:
    """Расширение файла поста (без точки), по умолчанию jpg."""
    f = getattr(msg, "file", None)
    ext = (getattr(f, "ext", None) or "").lstrip(".").lower() if f else ""
    return ext or "jpg"


def post_link(post: dict) -> str | None:
    """Ссылка на пост: публичная по @username, либо t.me/c/... для приватных."""
    if post.get("_username"):
        return f"https://t.me/{post['_username']}/{post['id']}"
    pid = post.get("_peer_id")
    if isinstance(pid, int):
        short = str(pid)
        short = short[4:] if short.startswith("-100") else short.lstrip("-")
        return f"https://t.me/c/{short}/{post['id']}"  # откроется только у участников
    return None
