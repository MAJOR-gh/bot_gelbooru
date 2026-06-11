"""Telegram-источник для бота: чтение артов из каналов через userbot (Telethon).

Тяжёлая логика вынесена сюда, чтобы не раздувать основной файл бота.
Зависит от уже созданной сессии `tg_userbot.session` (см. tg_login.py) и ключей
TG_API_ID / TG_API_HASH из переменных окружения.
"""
import json
import os
import logging

from telethon import TelegramClient
from telethon.sessions import StringSession

logger = logging.getLogger("gelbooru_bot.tg")

SESSION_NAME = "tg_userbot"


def _env_any(*names: str) -> str | None:
    """Первое непустое значение среди нескольких имён env (с обрезкой пробелов)."""
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    return None


# Сессия строкой (для хостинга/Docker — файл *.session не доезжает через git).
# Ловим несколько частых имён переменной; если задана — используем её, иначе
# падаем на файловую сессию (локалка).
SESSION_STRING = _env_any("TG_SESSION_STRING", "SESSION_STRING",
                          "TG_SESSION", "STRING_SESSION", "TG_STRING_SESSION")
CHANNELS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tg_channels.json")

# Сколько последних сообщений канала просматриваем за один запрос.
HISTORY_LIMIT = 200

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
    """Поднять Telethon-клиент на уже авторизованной сессии (в текущем loop).

    На хостинге сессия берётся из env TG_SESSION_STRING (StringSession), локально —
    из файла tg_userbot.session. Так секрет-сессия не лежит в git/Docker-образе.
    """
    if SESSION_STRING:
        logger.info("[tg] использую сессию из TG_SESSION_STRING (env)")
        client = TelegramClient(StringSession(SESSION_STRING), api_id, api_hash)
    else:
        logger.info("[tg] использую файловую сессию tg_userbot.session")
        client = TelegramClient(SESSION_NAME, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "Сессия Telegram не авторизована. Локально — запусти `python tg_login.py`; "
            "на хостинге — задай env TG_SESSION_STRING (см. `python tg_export_session.py`)."
        )
    # Прогреваем кэш диалогов: без этого приватные каналы (по числовому ID, без
    # @username) не резолвятся в iter_messages.
    try:
        await client.get_dialogs()
    except Exception as e:
        logger.warning(f"[tg] get_dialogs не удался (приватные каналы могут не открыться): {e}")
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
        logger.error(f"[tg] не удалось прочитать {CHANNELS_FILE}: {e}")
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


def post_is_ad(msg) -> bool:
    """Мягкая эвристика рекламы: нет картинки / кнопки-ссылки / рекламные слова."""
    if not has_visual_media(msg):
        return True
    if getattr(msg, "reply_markup", None) is not None:  # инлайн-кнопки
        return True
    text = (getattr(msg, "message", None) or "").lower()
    if any(kw in text for kw in AD_KEYWORDS):
        return True
    return False


async def fetch_channel_arts(client: TelegramClient, alias: str, peer: str,
                             limit: int = HISTORY_LIMIT) -> list[dict]:
    """Просмотреть историю канала → список арт-постов (реклама отсеяна).

    Каждый пост: {id, score(=реакции), _site, _alias, _username, _msg, caption}.
    """
    peer_val = _peer_value(peer)
    username = peer_val[1:] if isinstance(peer_val, str) and peer_val.startswith("@") else None
    peer_id = peer_val if isinstance(peer_val, int) else None
    posts: list[dict] = []
    try:
        async for msg in client.iter_messages(peer_val, limit=limit):
            if post_is_ad(msg):
                continue
            posts.append({
                "id": msg.id,
                "score": reaction_count(msg),
                "_site": f"TG:{alias}",
                "_alias": alias,
                "_username": username,
                "_peer_id": peer_id,
                "_msg": msg,
                "caption": (getattr(msg, "message", None) or "")[:200],
            })
    except Exception as e:
        logger.error(f"[tg] ошибка чтения канала {alias}: {e}")
        return []
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
        logger.error(f"[tg] download error: {e}")
        return None, size, "error"
    if not data:
        return None, size, "error"
    if len(data) > max_size:
        return None, len(data), "too_big"
    return data, len(data), None


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
