"""Доставка в Discord: скачивание → проверка байтов → отправка → запись в историю.

send_first_fitting — единственная точка отправки артов; анти-повтор здесь строгий:
резерв ДО скачивания, проверка MD5/SHA256 реальных байтов, неясный исход
отправки оставляет резерв (лучше пропуск, чем дубль).
"""
import asyncio
import hashlib
import logging
from io import BytesIO

import nextcord
from nextcord.errors import NotFound

import config
import sources
import texts
import tg_source
from selection import ShownMemory, post_uid, post_uids

logger = logging.getLogger("gelbooru_bot.delivery")

MB = 1024 * 1024
UPLOAD_MARGIN = 512 * 1024    # запас на embed и обёртку multipart
MAX_DOWNLOAD_TRIES = 8        # попыток скачивания за вызов, НЕ глубина поиска
SEND_TIMEOUT = 180.0
DISCORD_MAX_FILES = 10        # Discord принимает максимум 10 вложений в сообщении


def max_upload_size(interaction) -> int:
    """Лимит файла: 10 МБ, на серверах с бустом 2+ уровня — 50/100 МБ."""
    limit = 10 * MB
    guild = getattr(interaction, "guild", None)
    if guild is not None and (guild.premium_tier or 0) >= 2:
        limit = guild.filesize_limit
    if config.UPLOAD_CAP_MB:
        limit = min(limit, config.UPLOAD_CAP_MB * MB)
    return max(MB, limit - UPLOAD_MARGIN)


def content_uids(data: bytes) -> set[str]:
    """MD5 совпадает с метаданными API разных сайтов; SHA256 — точная проверка байтов."""
    return {hashlib.md5(data, usedforsecurity=False).hexdigest(),
            "sha256:" + hashlib.sha256(data).hexdigest()}


async def safe_followup(interaction, content=None, **kwargs):
    try:
        return await interaction.followup.send(content=content, **kwargs)
    except NotFound:
        logger.warning("[safe_followup] интеракция протухла")
    except Exception as error:
        logger.error("[safe_followup] %s: %s", type(error).__name__, error)
    return None


def _send_kwargs(payload: dict) -> dict:
    """payload → аргументы followup.send без пустых и служебных полей."""
    out = {k: payload[k] for k in ("content", "files") if payload.get(k)}
    out.update({k: payload[k] for k in ("embed", "file", "view") if payload.get(k) is not None})
    return out


def close_payload(payload: dict | None) -> None:
    if payload:
        for file in [*(payload.get("files") or []), payload.get("file")]:
            if file is not None:
                file.close()


async def send_first_fitting(memory: ShownMemory, interaction, scope: str, candidates, make_payload):
    """Первый кандидат, который удалось скачать и отправить → (пост | None, причины)."""
    reasons: set[str] = set()
    tries = 0
    try:
        async with asyncio.timeout(SEND_TIMEOUT):
            for post in candidates:
                if tries >= MAX_DOWNLOAD_TRIES:
                    break
                ids = post_uids(post)
                token = memory.reserve(scope, ids)
                if token is None:
                    reasons.add("duplicate")
                    logger.info("[anti-repeat] заблокирован повтор %s в %s", post_uid(post), scope)
                    continue
                tries += 1
                payload, delivery_started = None, False
                try:
                    payload, reason = await make_payload(post)
                    if payload is None:
                        reasons.add(reason or "error")
                        memory.failed(scope, ids)
                        continue
                    fingerprints = payload.get("_uids") or set()
                    if fingerprints and not memory.extend(scope, token, fingerprints):
                        memory.mark_blocked(token)
                        reasons.add("duplicate")
                        logger.info("[anti-repeat] совпали байты файла %s в %s", post_uid(post), scope)
                        continue
                    delivery_started = True
                    try:
                        message = await interaction.followup.send(**_send_kwargs(payload))
                        if payload.get("view") is not None:
                            payload["view"].message = message
                    except nextcord.HTTPException as error:
                        if error.status < 500:
                            delivery_started = False   # явный отказ — не «неясная доставка»
                        if error.status == 413:
                            reasons.add("too_big")
                            memory.failed(scope, ids)
                            continue
                        raise
                    memory.mark_sent(token)
                    logger.info("[anti-repeat] отправлен %s в %s", post_uid(post), scope)
                    return post, reasons
                finally:
                    if not delivery_started:
                        memory.release(token)
                    close_payload(payload)   # таймаут/сеть после начала отправки оставляет pending
    except TimeoutError:
        reasons.add("timeout")
    return None, reasons


# ── Booru ─────────────────────────────────────────────────────────────────────
def format_tags_preview(tags_str: str, limit: int = 14, maxlen: int = 950) -> str:
    """Теги поста → `tag` `tag` … +N (с обрезкой по длине поля embed)."""
    tags = (tags_str or "").split()
    text = " ".join(f"`{t}`" for t in tags[:limit])
    if len(tags) > limit:
        text += f" … +{len(tags) - limit}"
    return text[:maxlen]


def build_post_embed(source, post: dict, display_tag: str, size: int, filename: str,
                     reduced: bool) -> nextcord.Embed:
    """Embed: рейтинг, score, размер, разрешение, аффтор, теги, ссылки."""
    embed = nextcord.Embed(title="🖼 Результат по тегу", description=f"`{display_tag}`", color=0x00ff00)
    score = post.get("score")
    embed.add_field(name="📊 Score", value=str(score) if score is not None else "N/A", inline=True)
    embed.add_field(name="🔞 Рейтинг", value=texts.rating_label(post), inline=True)
    embed.add_field(name="📏 Размер", value=f"{size / MB:.1f} MB", inline=True)
    if post.get("width") and post.get("height"):
        embed.add_field(name="📐 Разрешение", value=f"{post['width']}×{post['height']}", inline=True)
    embed.add_field(name="👤 Аффтор", value=str(post.get("owner") or "—")[:100], inline=True)
    if reduced:
        embed.add_field(name="🗜 Версия", value="сжатая (оригинал не влез)", inline=True)
    links = f"[Открыть пост]({source.post_url(post)})"
    src = (post.get("source") or "").strip()
    if src.startswith("http") and " " not in src and len(src) < 500:
        links += f" • [Источник]({src})"
    embed.add_field(name="🔗 Ссылки", value=links, inline=False)
    preview = format_tags_preview(post.get("tags") or "")
    if preview:
        embed.add_field(name="🏷️ Теги", value=preview, inline=False)
    embed.set_image(url=f"attachment://{filename}")
    embed.set_footer(text=f"{source.name} • ID {post.get('id', '')}")
    return embed


async def build_booru_payload(source, post: dict, display_tag: str, max_size: int):
    """Скачивает медиа поста → (payload для отправки | None, причина неудачи)."""
    media, reason = await sources.fetch_media(source, post, max_size)
    if media is None:
        return None, reason
    size = len(media.data)
    filename = f"{source.name.lower()}_{post.get('id') or 'art'}.{media.ext}"
    payload = {"file": nextcord.File(BytesIO(media.data), filename=filename),
               "_uids": content_uids(media.data)}
    if media.is_video:
        payload["content"] = (f"🎬 **Видео** `{display_tag}` • 📊 {post.get('score', 'N/A')} • "
                              f"🔞 {texts.rating_label(post)} • {size / MB:.1f} MB • "
                              f"[Открыть пост](<{source.post_url(post)}>)")
    else:
        payload["embed"] = build_post_embed(source, post, display_tag, size, filename, media.reduced)
    return payload, ""


# ── Telegram ──────────────────────────────────────────────────────────────────
async def build_tg_payload(client, post: dict, max_size: int):
    """Пост целиком: все картинки и видео по порядку, пока влезают в лимит сообщения.

    Не влезшее — ссылкой «Открыть пост». → (payload | None, причина).
    """
    msg = post["_msg"]
    album = None
    if post.get("_peer") is not None:
        album = await tg_source.fetch_album_messages(client, post["_peer"], msg)
    media_msgs = [m for m in (album or post.get("_msgs") or [msg]) if tg_source.has_visual_media(m)]
    if not media_msgs:
        return None, "no_file"

    has_video = any(tg_source.is_video(m) for m in media_msgs)
    files, total, skipped = [], 0, 0
    fingerprints: set[str] = set()
    for m in media_msgs:
        remaining = max_size - total
        approx = getattr(getattr(m, "file", None), "size", None)
        if len(files) >= DISCORD_MAX_FILES or (approx and approx > remaining):
            skipped += 1
            continue
        data, size, _ = await tg_source.download_media(client, m, remaining)
        if not data:
            skipped += 1
            continue
        filename = f"tg_{post['_alias']}_{m.id}.{tg_source.media_ext(m)}"
        files.append(nextcord.File(BytesIO(data), filename=filename))
        total += size
        fingerprints.update(content_uids(data))

    link = tg_source.post_link(post)
    parts = [f"{'🎬' if has_video else '🖼'} **{post['_alias']}** • ❤️ {post.get('score', 0)}"]
    if not files:
        if not link:
            return None, "too_big"
        parts += ["⬆️ файлы слишком большие — смотри в источнике", f"[Открыть пост]({link})"]
        return {"content": " • ".join(parts)}, ""
    if len(files) > 1:
        parts.append(f"📎 {len(files)} файлов")
    if skipped:
        parts.append(f"➕ ещё {skipped} в посте")
    if link:
        parts.append(f"[Открыть пост](<{link}>)")
    return {"content": " • ".join(parts), "files": files, "_uids": fingerprints}, ""
