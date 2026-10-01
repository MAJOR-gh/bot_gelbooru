"""Gelbooru Discord-бот: /gelbooru /konachan /safebooru /tg /tags /tagcheck /help.

Модули:
  content_filter — что можно показывать (блэклисты, нагота, фокус части тела);
  booru          — сайты, поиск по страницам, скачивание;
  selection      — анти-повтор и порядок кандидатов;
  tg_source      — Telegram-каналы (userbot на Telethon).
"""
import logging
import os
import random
import time
from collections import defaultdict, deque
from io import BytesIO

import nextcord
from dotenv import load_dotenv
from nextcord.errors import NotFound
from nextcord.ext import commands

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# .env рядом с ботом (панели-хостинги не всегда дают задать переменные).
# Уже заданные переменные окружения не перетираются.
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Эти модули читают env при импорте — поэтому после load_dotenv.
import booru  # noqa: E402
import content_filter as cf  # noqa: E402
import tg_source  # noqa: E402
from selection import ShownMemory, order_candidates, post_uid, recent_key  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('gelbooru_bot')

VERSION = "3.0"


def env_any(*names: str) -> str | None:
    """Первое непустое значение среди нескольких имён переменных окружения."""
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    return None


# ID серверов для МГНОВЕННОЙ регистрации слэш-команд (через запятую).
# Пусто → глобальная регистрация (Discord обновляет до часа).
GUILD_IDS = [
    int(g) for g in (os.environ.get("DISCORD_GUILD_IDS") or "").replace(";", ",").split(",")
    if g.strip().isdigit()
]

# Где хранить память показанных артов (на хостинге — постоянный диск).
DATA_DIR = os.environ.get("DATA_DIR") or BASE_DIR
memory = ShownMemory(os.path.join(DATA_DIR, "recent_shown.json"))

GELBOORU = booru.Gelbooru(env_any("GELBOORU_API_KEY"), env_any("GELBOORU_USER_ID"))
KONACHAN = booru.Konachan()
SAFEBOORU = booru.Safebooru()

TG_API_ID = env_any("TG_API_ID", "API_ID", "TELEGRAM_API_ID", "TG_ID")
TG_API_HASH = env_any("TG_API_HASH", "API_HASH", "TELEGRAM_API_HASH", "TG_HASH")
tg_client = None  # Telethon-клиент; поднимается в on_ready, None если ключи не заданы

bot = commands.Bot(intents=nextcord.Intents.default(), default_guild_ids=GUILD_IDS or None)


# ── Кулдауны (nextcord-слэш-команды НЕ поддерживают commands.cooldown) ─────────
class CooldownManager:
    """Sliding-window кулдаун: не более `rate` вызовов за `per` секунд."""

    def __init__(self, rate: int, per: float):
        self.rate = rate
        self.per = per
        self._calls: dict[int, deque] = defaultdict(deque)

    def retry_after(self, key: int) -> float:
        """0.0 если можно выполнить (вызов засчитан), иначе секунды до сброса."""
        now = time.monotonic()
        dq = self._calls[key]
        while dq and now - dq[0] >= self.per:
            dq.popleft()
        if len(dq) >= self.rate:
            return self.per - (now - dq[0])
        dq.append(now)
        return 0.0


# 5 поисков за 30 сек ≈ 1 поиск раз в 6 секунд на пользователя (с запасом на всплеск).
GELBOORU_CD = CooldownManager(rate=5, per=30.0)
KONACHAN_CD = CooldownManager(rate=5, per=30.0)
SAFEBOORU_CD = CooldownManager(rate=5, per=30.0)
TG_CD = CooldownManager(rate=3, per=30.0)
TAGS_CD = CooldownManager(rate=1, per=30.0)
TAGCHECK_CD = CooldownManager(rate=5, per=30.0)


async def reject_if_on_cooldown(interaction: nextcord.Interaction, cd: CooldownManager) -> bool:
    """Если пользователь на кулдауне — отвечает и возвращает True."""
    retry = cd.retry_after(interaction.user.id)
    if retry > 0:
        await interaction.response.send_message(
            f"⏳ Слишком часто! Попробуй снова через **{retry:.0f}с**.", ephemeral=True)
        return True
    return False


NSFW_ONLY = "🔞 Пиздуй в NSFW канал!"


def channel_allows_nsfw(interaction: nextcord.Interaction) -> bool:
    """NSFW разрешён в личке и в NSFW-каналах (в ветке — по родительскому каналу)."""
    channel = interaction.channel
    if isinstance(channel, nextcord.DMChannel):
        return True
    is_nsfw = getattr(channel, "is_nsfw", None)
    if callable(is_nsfw):
        try:
            return bool(is_nsfw())
        except Exception:
            return False
    return False


# ── Лимит загрузки ────────────────────────────────────────────────────────────
MB = 1024 * 1024
UPLOAD_MARGIN = 512 * 1024  # запас на embed и обёртку multipart
_cap = env_any("MAX_UPLOAD_MB")
UPLOAD_CAP = int(_cap) * MB if _cap and _cap.isdigit() else None


def max_upload_size(interaction: nextcord.Interaction) -> int:
    """Лимит файла: 10 МБ, на серверах с бустом 2+ уровня — 50/100 МБ."""
    limit = 10 * MB
    guild = interaction.guild
    if guild is not None and (guild.premium_tier or 0) >= 2:
        limit = guild.filesize_limit
    if UPLOAD_CAP:
        limit = min(limit, UPLOAD_CAP)
    return max(1 * MB, limit - UPLOAD_MARGIN)


# ── «Приколдес» ───────────────────────────────────────────────────────────────
LGBT_JOKE_WARNING = (
    "⚠️ **ВНИМАНИЕ!** Запрос подобных материалов карается статьёй 6.21 УК ЧР "
    "«Хранение и распространение материалов содержащих ЛГБТ+ контент». "
    "В ближайшее время на вас будет заведено уголовное дело. "
    "Ваш IP адрес был передан МВД Чернарусской республики."
)
VENTI_WARNING = (
    "**СУДЕБНОЕ ПОСТАНОВЛЕНИЕ РЕСПУБЛИКИ ЧЕРНАРУСЬ**\n\n"
    "Настоящим уведомляем, что в отношении Вас вынесено Судебное постановление "
    "Республики Чернарусь на основании **повторного** выявления фактов нарушения "
    "**ст. 6.21 УК ЧР** («Хранение и распространение материалов, содержащих ЛГБТ+ "
    "контент») в сети Интернет.\n\n"
    "Согласно постановлению суда, в Вашем отношении задействованы следующие меры:\n"
    "🚷 Введён **полный запрет на выезд** за пределы Республики Чернарусь. "
    "Данные переданы в Пограничную службу государственной безопасности.\n"
    "🔒 Инициирована процедура **задержания и заключения под стражу**. "
    "Соответствующий ордер направлен в территориальные органы МВД по месту Вашего "
    "фактического нахождения.\n"
    "📡 Ваш IP-адрес, устройство и сетевая активность поставлены на оперативный учёт.\n\n"
    "⚠️ **Внимание:** Любые действия, направленные на уклонение от следственных "
    "органов или попытку пересечения государственной границы, будут расценены как "
    "побег из-под стражи и попытка скрыться от правосудия. В соответствии с "
    "законодательством ЧР в условиях особого положения, данные действия влекут за "
    "собой применение суровых мер наказания, **вплоть до расстрела**.\n\n"
    "_Оставайтесь на месте по месту фактического нахождения. Сотрудники уже выехали._"
)
# Для Kanzaki Hideri вместо поиска шлём заготовленную картинку.
KANZAKI_HIDERI_IMAGE = os.path.join(BASE_DIR, "assets", "kanzaki_hideri.jpg")
BLOCKED_TEXT = "🚫 Один из тегов заблокирован."


def tag_verdict(tags: list[str]) -> str | None:
    """Особая реакция на теги: 'kanzaki' | 'venti' | 'lgbt' | 'blocked' | None.

    Kanzaki проверяется раньше блокировок; остальное — по порядку тегов.
    """
    if any(cf.tag_is_kanzaki_hideri(t) for t in tags):
        return "kanzaki"
    for t in tags:
        if cf.tag_triggers_venti(t):
            return "venti"
        if cf.tag_triggers_lgbt_joke(t):
            return "lgbt"
        if cf.tag_is_blocked(t):
            return "blocked"
    return None


async def answer_verdict(interaction: nextcord.Interaction, verdict: str):
    if verdict == "kanzaki" and os.path.isfile(KANZAKI_HIDERI_IMAGE):
        return await interaction.response.send_message(file=nextcord.File(KANZAKI_HIDERI_IMAGE))
    if verdict == "venti":
        return await interaction.response.send_message(VENTI_WARNING)
    if verdict == "lgbt":
        return await interaction.response.send_message(LGBT_JOKE_WARNING)
    return await interaction.response.send_message(BLOCKED_TEXT, ephemeral=True)


# ── Утилиты отправки ──────────────────────────────────────────────────────────
async def safe_followup(interaction: nextcord.Interaction, content=None, **kwargs):
    try:
        return await interaction.followup.send(content=content, **kwargs)
    except NotFound:
        logger.warning("[safe_followup] интеракция протухла")
    except Exception as e:
        logger.error(f"[safe_followup] {type(e).__name__}: {e}")
    return None


def _send_kwargs(payload: dict) -> dict:
    """payload → аргументы followup.send без пустых полей."""
    out = {}
    if payload.get("content"):
        out["content"] = payload["content"]
    if payload.get("embed") is not None:
        out["embed"] = payload["embed"]
    if payload.get("file") is not None:
        out["file"] = payload["file"]
    if payload.get("files"):
        out["files"] = payload["files"]
    return out


MAX_DOWNLOAD_TRIES = 8                # сколько постов пробуем скачать за запрос
_inflight: set[tuple[str, str]] = set()  # (запрос, арт), которые прямо сейчас отправляются


async def send_first_fitting(interaction, rkey: str, candidates: list[dict], make_payload):
    """Отправляет первого кандидата, который скачался и влез в лимит Discord.

    → (отправленный пост | None, множество причин неудач).
    Посты, которые в эту секунду отправляет параллельный запрос с тем же
    тегом, пропускаются — иначе двое одновременно получат один и тот же арт.
    """
    reasons: set[str] = set()
    tries = 0
    for post in candidates:
        key = (rkey, post_uid(post))
        if key in _inflight:
            continue
        if tries >= MAX_DOWNLOAD_TRIES:
            break
        tries += 1
        _inflight.add(key)
        try:
            payload, reason = await make_payload(post)
            if payload is None:
                reasons.add(reason or "error")
                continue
            try:
                await interaction.followup.send(**_send_kwargs(payload))
            except nextcord.HTTPException as e:
                if e.status == 413:
                    logger.warning("413 от Discord — пробую следующего кандидата")
                    reasons.add("too_big")
                    continue
                raise
            memory.remember(rkey, key[1])
            return post, reasons
        finally:
            _inflight.discard(key)
    return None, reasons


def nothing_sent_text(display_tag: str, label: str, reasons: set[str]) -> str:
    if reasons and reasons <= {"too_big"}:
        return f"❌ По тегу `{display_tag}` все подходящие файлы слишком большие для загрузки."
    return f"❌ Не удалось скачать арты с {label}. Попробуй ещё раз."


def source_error_text(label: str, errors: list[str]) -> str:
    if "auth" in errors:
        return f"❌ {label} отказал в доступе (нужен API-ключ или сайт заблокирован)."
    if "rate" in errors:
        return f"⏳ {label} ограничил частоту запросов — попробуй через минуту."
    return f"❌ {label} сейчас не отвечает. Попробуй позже."


# ── Embed ─────────────────────────────────────────────────────────────────────
RATING_LABELS = {
    "general": "🟢 General",
    "safe": "🟢 Safe",
    "sensitive": "🟡 Sensitive",
    "questionable": "🟠 Questionable",
    "explicit": "🔴 Explicit",
}


def rating_label(post: dict) -> str:
    return RATING_LABELS.get((post.get("rating") or "").lower(), "—")


def format_tags_preview(tags_str: str, limit: int = 14, maxlen: int = 950) -> str:
    """Теги поста → `tag` `tag` … +N (с обрезкой по длине поля embed)."""
    tags = (tags_str or "").split()
    if not tags:
        return ""
    text = " ".join(f"`{t}`" for t in tags[:limit])
    if len(tags) > limit:
        text += f" … +{len(tags) - limit}"
    return text[:maxlen]


def build_post_embed(source, post: dict, display_tag: str, size: int, filename: str,
                     reduced: bool) -> nextcord.Embed:
    """Embed: рейтинг, score, размер, разрешение, аффтор, теги, ссылки."""
    embed = nextcord.Embed(title="🖼 Результат по тегу", description=f"`{display_tag}`",
                           color=0x00ff00)
    score = post.get("score")
    embed.add_field(name="📊 Score", value=str(score) if score is not None else "N/A", inline=True)
    embed.add_field(name="🔞 Рейтинг", value=rating_label(post), inline=True)
    embed.add_field(name="📏 Размер", value=f"{size / MB:.1f} MB", inline=True)
    width, height = post.get("width"), post.get("height")
    if width and height:
        embed.add_field(name="📐 Разрешение", value=f"{width}×{height}", inline=True)
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
    media, reason = await booru.fetch_media(source, post, max_size)
    if media is None:
        return None, reason
    size = len(media.data)
    filename = f"{source.name.lower()}_{post.get('id') or 'art'}.{media.ext}"
    file = nextcord.File(BytesIO(media.data), filename=filename)
    if media.is_video:
        content = (f"🎬 **Видео** `{display_tag}` • 📊 {post.get('score', 'N/A')} • "
                   f"🔞 {rating_label(post)} • {size / MB:.1f} MB • "
                   f"[Открыть пост](<{source.post_url(post)}>)")
        return {"content": content, "file": file}, ""
    embed = build_post_embed(source, post, display_tag, size, filename, media.reduced)
    return {"embed": embed, "file": file}, ""


# ── Поиск по booru ────────────────────────────────────────────────────────────
async def run_booru_search(interaction: nextcord.Interaction, raw_tags: tuple, source,
                           cooldown: CooldownManager, *, nsfw: bool = True):
    """Общая логика /gelbooru, /konachan, /safebooru.

    nsfw=True — только NSFW-каналы/личка и требование наготы (для Safebooru оба
    выключены; система блокировки тегов та же).
    """
    if nsfw and not channel_allows_nsfw(interaction):
        return await interaction.response.send_message(NSFW_ONLY, ephemeral=True)
    tags = cf.clean_user_tags(raw_tags)
    if not tags:
        return await interaction.response.send_message("❌ Укажи хотя бы один тег.", ephemeral=True)

    # Заблокированный тег — тихий ответ только автору и без траты кулдауна.
    verdict = tag_verdict(tags)
    if verdict == "blocked":
        return await answer_verdict(interaction, verdict)
    if await reject_if_on_cooldown(interaction, cooldown):
        return
    if verdict:
        return await answer_verdict(interaction, verdict)

    try:
        await interaction.response.defer()
    except (NotFound, nextcord.HTTPException) as e:
        logger.warning(f"[{source.name}] defer не удался: {e}")
        return

    display_tag = " + ".join(tags)
    rkey = recent_key(source.name, tags)
    try:
        res = await booru.search(source, tags, memory.recent(rkey), require_nudity=nsfw)
        if not res.candidates:
            if res.errors and not res.fetched:
                text = source_error_text(source.name, res.errors)
            else:
                text = f"❌ По тегу `{display_tag}` на {source.name} ничего не найдено."
            return await interaction.followup.send(text)

        max_size = max_upload_size(interaction)
        sent, reasons = await send_first_fitting(
            interaction, rkey, res.candidates,
            lambda p: build_booru_payload(source, p, display_tag, max_size))
        if sent is None:
            await safe_followup(interaction, nothing_sent_text(display_tag, source.name, reasons))
    except NotFound:
        logger.warning(f"[{source.name}] интеракция протухла")
    except Exception:
        logger.exception(f"[{source.name}] ошибка поиска по `{display_tag}`")
        await safe_followup(interaction, "❌ Произошла внутренняя ошибка. Проверь консоль бота.")


TAG_OPTION = "Главный тег для поиска"
EXTRA_OPTION = "Доп. тег для сужения (необязательно)"


@bot.slash_command(name='gelbooru',
                   description="🔞 Арт по тегам с Gelbooru — приоритет твоему запросу (до 4 тегов)")
async def gelbooru(
    interaction: nextcord.Interaction,
    tag: str = nextcord.SlashOption(description=TAG_OPTION, required=True),
    tag2: str = nextcord.SlashOption(description=EXTRA_OPTION, required=False, default=None),
    tag3: str = nextcord.SlashOption(description=EXTRA_OPTION, required=False, default=None),
    tag4: str = nextcord.SlashOption(description=EXTRA_OPTION, required=False, default=None),
):
    await run_booru_search(interaction, (tag, tag2, tag3, tag4), GELBOORU, GELBOORU_CD)


@bot.slash_command(name='konachan',
                   description="🔞 Арт по тегам с Konachan — аниме-арт высокого качества (до 4 тегов)")
async def konachan(
    interaction: nextcord.Interaction,
    tag: str = nextcord.SlashOption(description=TAG_OPTION, required=True),
    tag2: str = nextcord.SlashOption(description=EXTRA_OPTION, required=False, default=None),
    tag3: str = nextcord.SlashOption(description=EXTRA_OPTION, required=False, default=None),
    tag4: str = nextcord.SlashOption(description=EXTRA_OPTION, required=False, default=None),
):
    await run_booru_search(interaction, (tag, tag2, tag3, tag4), KONACHAN, KONACHAN_CD)


@bot.slash_command(name='safebooru',
                   description="🟢 Safe-арт по тегам с Safebooru — без NSFW, в любом канале (до 4 тегов)")
async def safebooru(
    interaction: nextcord.Interaction,
    tag: str = nextcord.SlashOption(description=TAG_OPTION, required=True),
    tag2: str = nextcord.SlashOption(description=EXTRA_OPTION, required=False, default=None),
    tag3: str = nextcord.SlashOption(description=EXTRA_OPTION, required=False, default=None),
    tag4: str = nextcord.SlashOption(description=EXTRA_OPTION, required=False, default=None),
):
    await run_booru_search(interaction, (tag, tag2, tag3, tag4), SAFEBOORU, SAFEBOORU_CD,
                           nsfw=False)


# ── /tags ─────────────────────────────────────────────────────────────────────
# Только живые теги Gelbooru (устаревшие blonde/outdoor/kemonomimi заменены, а
# 1boy/hetero убраны — такие арты всё равно режет HARD-блок).
POPULAR_TAGS = [
    "1girl", "solo", "2girls", "multiple_girls",
    "breasts", "ass", "nude", "censored", "uncensored", "bikini", "school_uniform",
    "cat_ears", "animal_ears", "maid", "twintails", "blonde_hair", "blue_hair",
    "brown_hair", "pink_hair", "purple_hair", "red_hair", "white_hair", "grey_hair",
    "long_hair", "short_hair", "curly_hair", "twisted_torso",
    "abs", "flexible", "looking_at_viewer", "smile", "blush",
    "outdoors", "indoors", "bed", "forest", "city", "beach", "swimsuit",
    "lingerie", "underwear", "thighhighs", "pantyhose", "socks", "gloves",
    "hat", "hairband", "ribbon", "bow", "glasses", "comic",
]


def short_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


@bot.slash_command(name='tags', description="🏷️ Популярные теги и сколько по ним артов")
async def tags_list(interaction: nextcord.Interaction):
    if await reject_if_on_cooldown(interaction, TAGS_CD):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        info = await GELBOORU.tag_info(POPULAR_TAGS)
    except booru.SourceError as e:
        return await safe_followup(interaction, source_error_text("Gelbooru", [e.kind]), ephemeral=True)

    alive = sorted(((t, int(info[t].get("count") or 0)) for t in POPULAR_TAGS
                    if t in info and not cf.tag_is_blocked(t)),
                   key=lambda x: -x[1])
    alive = [(t, c) for t, c in alive if c > 0]
    lines = " • ".join(f"`{t}` {short_count(c)}" for t, c in alive)
    embed = nextcord.Embed(title="🏷️ Популярные теги",
                           description=lines[:4000] or "Не удалось получить список тегов.",
                           color=0x2ecc71)
    embed.set_footer(text="💡 Число — сколько артов на Gelbooru. Свой тег проверь через /tagcheck")
    await safe_followup(interaction, embed=embed, ephemeral=True)


# ── /tagcheck ─────────────────────────────────────────────────────────────────
@bot.slash_command(name='tagcheck', description="🔍 Проверить, есть ли на Gelbooru арты по тегу")
async def tag_check(
    interaction: nextcord.Interaction,
    tag: str = nextcord.SlashOption(description="Тег для проверки", required=True),
):
    if not channel_allows_nsfw(interaction):
        return await interaction.response.send_message(NSFW_ONLY, ephemeral=True)
    cleaned = cf.clean_user_tags([tag])
    if not cleaned:
        return await interaction.response.send_message("❌ Укажи тег.", ephemeral=True)
    clean_tag = cleaned[0].lstrip("-")
    if cf.tag_is_blocked(clean_tag):
        return await interaction.response.send_message(
            f"🚫 Тег `{clean_tag}` заблокирован в боте.", ephemeral=True)
    if await reject_if_on_cooldown(interaction, TAGCHECK_CD):
        return
    await interaction.response.defer()

    try:
        info = (await GELBOORU.tag_info([clean_tag])).get(clean_tag)
        count = int(info.get("count") or 0) if info else 0
        deprecated = bool(info) and str(info.get("type")) == "6"
        similar = []
        if count == 0 or deprecated:
            part = clean_tag.strip("*%_")
            if part:
                similar = [t for t in await GELBOORU.similar_tags(part, 8)
                           if t.get("name") != clean_tag and int(t.get("count") or 0) > 0
                           and not cf.tag_is_blocked(t["name"])][:5]
    except booru.SourceError as e:
        return await safe_followup(interaction, source_error_text("Gelbooru", [e.kind]))

    if count > 0:
        embed = nextcord.Embed(title=f"✅ Тег `{clean_tag}` активен!",
                               description=f"Артов на Gelbooru: **{count:,}**".replace(",", " "),
                               color=0x2ecc71)
        embed.add_field(name="💡 Используй", value=f"`/gelbooru {clean_tag}`", inline=False)
        if deprecated:
            embed.add_field(name="⚠️ Тег устаревший",
                            value="Новые арты могут быть под другим именем.", inline=False)
    else:
        embed = nextcord.Embed(title=f"❌ Тег `{clean_tag}` не найден",
                               description="На Gelbooru нет артов с таким тегом.",
                               color=0xe74c3c)
    if similar:
        embed.add_field(name="🔎 Похожие теги",
                        value="\n".join(f"`{t['name']}` — {short_count(int(t.get('count') or 0))}"
                                        for t in similar),
                        inline=False)
    elif count == 0:
        embed.add_field(name="💡 Попробуй",
                        value="• Проверь написание (теги на английском, через `_`)\n"
                              "• Посмотри `/tags`", inline=False)
    await safe_followup(interaction, embed=embed)


# ── /tg — арты из Telegram-каналов ────────────────────────────────────────────
DISCORD_MAX_FILES = 10  # Discord принимает максимум 10 вложений в сообщении


def _is_video_msg(m) -> bool:
    return tg_source.media_ext(m) in ("mp4", "webm", "mov", "m4v", "gif")


async def build_tg_payload(post: dict, max_size: int):
    """Пост целиком: все картинки и видео в исходном порядке, пока влезают в
    лимит Discord на ВСЁ сообщение. Не влезшее — ссылкой «Открыть пост».
    → (payload | None, причина).
    """
    msg = post["_msg"]
    album = None
    if post.get("_peer") is not None:
        album = await tg_source.fetch_album_messages(tg_client, post["_peer"], msg)
    media_msgs = [m for m in (album or post.get("_msgs") or [msg]) if tg_source.has_visual_media(m)]
    if not media_msgs:
        return None, "no_file"

    has_video = any(_is_video_msg(m) for m in media_msgs)
    files, total, skipped = [], 0, 0
    for m in media_msgs:
        remaining = max_size - total
        f = getattr(m, "file", None)
        approx = getattr(f, "size", None) if f else None
        if len(files) >= DISCORD_MAX_FILES or (approx and approx > remaining):
            skipped += 1
            continue
        data, size, _ = await tg_source.download_media(tg_client, m, remaining)
        if not data:
            skipped += 1
            continue
        filename = f"tg_{post['_alias']}_{m.id}.{tg_source.media_ext(m)}"
        files.append(nextcord.File(BytesIO(data), filename=filename))
        total += size

    link = tg_source.post_link(post)
    head = f"{'🎬' if has_video else '🖼'} **{post['_alias']}** • ❤️ {post.get('score', 0)}"
    if not files:
        if not link:
            return None, "too_big"
        # Медиа было, но не влезло (тяжёлое видео/альбом) — отдаём ссылку.
        parts = [head, "⬆️ файлы слишком большие — смотри в источнике", f"[Открыть пост]({link})"]
        return {"content": " • ".join(parts)}, ""
    parts = [head]
    if len(files) > 1:
        parts.append(f"📎 {len(files)} файлов")
    if skipped:
        parts.append(f"➕ ещё {skipped} в посте")
    if link:
        parts.append(f"[Открыть пост](<{link}>)")
    return {"content": " • ".join(parts), "files": files}, ""


async def run_tg_search(interaction: nextcord.Interaction, alias: str | None):
    """Топовый по реакциям ещё не показанный пост из выбранного/случайного канала."""
    label = "Telegram"
    if not channel_allows_nsfw(interaction):
        return await interaction.response.send_message(NSFW_ONLY, ephemeral=True)
    if tg_client is None:
        return await interaction.response.send_message(
            "⚠️ Telegram-источник не настроен (нет TG_API_ID/TG_API_HASH или сессии).",
            ephemeral=True)
    channels = tg_source.load_channels()
    if not channels:
        return await interaction.response.send_message(
            "⚠️ Список каналов пуст — заполни `tg_channels.json`.", ephemeral=True)
    if alias:
        chosen = next((c for c in channels if c["alias"].lower() == alias.lower()), None)
        if chosen is None:
            avail = ", ".join(f"`{c['alias']}`" for c in channels)
            return await interaction.response.send_message(
                f"❌ Канал `{alias}` не найден. Доступны: {avail}", ephemeral=True)
    else:
        chosen = random.choice(channels)
    if await reject_if_on_cooldown(interaction, TG_CD):
        return
    try:
        await interaction.response.defer()
    except (NotFound, nextcord.HTTPException) as e:
        logger.warning(f"[{label}] defer не удался: {e}")
        return

    rkey = recent_key(label, [chosen["alias"]])
    try:
        raw = await tg_source.fetch_channel_arts(tg_client, chosen["alias"], chosen["peer"])
        if not raw:
            return await interaction.followup.send(
                f"❌ В канале `{chosen['alias']}` не нашлось подходящих артов.")
        candidates = order_candidates(raw, memory.recent(rkey))[:booru.CANDIDATE_LIMIT]
        max_size = max_upload_size(interaction)
        sent, reasons = await send_first_fitting(
            interaction, rkey, candidates, lambda p: build_tg_payload(p, max_size))
        if sent is None:
            await safe_followup(interaction, nothing_sent_text(chosen["alias"], label, reasons))
    except NotFound:
        logger.warning(f"[{label}] интеракция протухла")
    except Exception:
        logger.exception(f"[{label}] ошибка в канале {chosen['alias']}")
        await safe_followup(interaction, "❌ Произошла внутренняя ошибка. Проверь консоль бота.")


@bot.slash_command(name="tg", description="🔞 Арт из телеграм канала")
async def tg_command(
    interaction: nextcord.Interaction,
    channel: str = nextcord.SlashOption(name="channel", description="Канал из списка (пусто — случайный)",
                                        required=False, default=None, autocomplete=True),
):
    await run_tg_search(interaction, channel)


@tg_command.on_autocomplete("channel")
async def tg_command_autocomplete(interaction: nextcord.Interaction, value: str):
    aliases = [c["alias"] for c in tg_source.load_channels()]
    if value:
        aliases = [a for a in aliases if value.lower() in a.lower()]
    await interaction.response.send_autocomplete(aliases[:25])


# ── /help ─────────────────────────────────────────────────────────────────────
@bot.slash_command(name='help', description="📖 Справка: список команд и как ими пользоваться")
async def help_command(interaction: nextcord.Interaction):
    embed = nextcord.Embed(title=f"📖 Справка по боту Gelbooru v{VERSION}",
                           description="Список всех доступных команд:", color=0x3498db)
    embed.add_field(name="🔞 /gelbooru <тег> [тег2] [тег3] [тег4]", value="Арт/гиф/видео по 1-4 тегам с **Gelbooru** — приоритет твоему запросу", inline=False)
    embed.add_field(name="🔞 /konachan <тег> [тег2] [тег3] [тег4]", value="Арт по 1-4 тегам с **Konachan** (аниме-арт)", inline=False)
    embed.add_field(name="🟢 /safebooru <тег> [тег2] [тег3] [тег4]", value="Safe-арт по 1-4 тегам с **Safebooru** — без NSFW, работает в любом канале", inline=False)
    embed.add_field(name="🔞 /tg [канал]", value="Арт из **Telegram**-канала — весь пост (все картинки и видео)", inline=False)
    embed.add_field(name="🏷️ /tags", value="Популярные теги и сколько по ним артов", inline=False)
    embed.add_field(name="🔍 /tagcheck <тег>", value="Проверить тег и подсказать похожие, если опечатка", inline=False)
    embed.add_field(name="📖 /help", value="Показать эту справку", inline=False)
    embed.set_footer(text="💡 NSFW-команды работают только в NSFW-каналах или в ЛС. Тег с минусом (-tag) исключает его.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── События ───────────────────────────────────────────────────────────────────
_started = False


@bot.event
async def on_ready():
    """Срабатывает и после каждого переподключения — разовая настройка под флагом."""
    global _started, tg_client
    logger.info(f"✅ Бот онлайн: {bot.user} • v{VERSION} • серверов: {len(bot.guilds)}")
    if _started:
        return
    _started = True

    if not (GELBOORU.api_key and GELBOORU.user_id):
        logger.warning("⚠️ GELBOORU_API_KEY / GELBOORU_USER_ID не заданы — Gelbooru "
                       "без ключа отвечает 401, /gelbooru работать не будет.")
    if booru.PROXY:
        logger.info(f"🌐 Запросы к сайтам идут через прокси: {booru.masked_proxy()}")

    # Telegram userbot — поднимаем один раз в общем event loop.
    logger.info("[tg] env: TG_API_ID=%s | TG_API_HASH=%s | TG_SESSION_STRING=%s",
                "есть" if TG_API_ID else "НЕТ", "есть" if TG_API_HASH else "НЕТ",
                "есть" if tg_source.SESSION_STRING else "НЕТ (будет искать файл сессии)")
    if TG_API_ID and TG_API_HASH:
        try:
            tg_client = await tg_source.start_client(int(TG_API_ID), TG_API_HASH)
            logger.info(f"✅ Telegram userbot подключён, каналов: {len(tg_source.load_channels())}")
        except Exception as e:
            logger.error(f"⚠️ Telegram userbot не запущен: {type(e).__name__}: {e} — /tg недоступна.")
    else:
        logger.warning("ℹ️ TG_API_ID / TG_API_HASH не заданы — команда /tg отключена.")

    # Слэш-команды nextcord синхронизирует сам при подключении (глобальные — в
    # on_connect, серверные — в on_guild_available; лишние глобальные при
    # guild-режиме он же и удаляет). В глобальном режиме остаётся снести
    # серверные копии от прежнего guild-режима — иначе команды двоятся.
    if not GUILD_IDS:
        for g in list(bot.guilds):
            try:
                await bot.http.bulk_upsert_guild_commands(bot.application_id, g.id, [])
            except Exception as e:
                logger.warning(f"Не удалось снести серверные команды на {g.id}: {e}")
        logger.info("🌐 Слэш-команды глобальные (обновление до ~1 часа).")
    else:
        logger.info(f"⚡ Слэш-команды для серверов: {GUILD_IDS}")


@bot.event
async def on_application_command_error(interaction: nextcord.Interaction, error: Exception):
    logger.error(f"[command_error] {type(error).__name__}: {error}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send("❌ Произошла ошибка при выполнении команды.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Произошла ошибка при выполнении команды.", ephemeral=True)
    except Exception:
        pass


@bot.event
async def on_close():
    memory.save()
    await booru.close_session()
    if tg_client is not None:
        try:
            await tg_client.disconnect()
        except Exception:
            pass


async def start_health_server() -> None:
    """HTTP-«пульс» для хостингов, которым нужен открытый порт (Render и т.п.).

    Включается только если хостинг задал переменную PORT.
    """
    port = os.environ.get("PORT")
    if not (port and port.isdigit()):
        return
    from aiohttp import web

    async def health(_request):
        return web.Response(text=f"ok, ready={bot.is_ready()}")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    logger.info(f"💓 Health-сервер слушает порт {port}")


def main() -> None:
    token = env_any("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("❌ Не задан токен бота: переменная DISCORD_BOT_TOKEN "
                         "(в окружении или в файле .env рядом с ботом).")
    memory.load()
    bot.loop.create_task(memory.autosave_loop())
    bot.loop.create_task(start_health_server())
    try:
        bot.run(token)
    finally:
        memory.save()


if __name__ == "__main__":
    main()
