"""Gelbooru Discord-бот: точка входа, слэш-команды, кулдауны, события.

Карта модулей — в README.md (раздел «Структура»).
"""
import asyncio
import logging
import math
import os
import random
import time
from collections import defaultdict, deque
from io import BytesIO

import nextcord
from nextcord.errors import NotFound
from nextcord.ext import commands

import config  # первым из своих модулей: загружает .env
import content_filter as cf
import delivery
import diagnostics
import search
import sources
import tag_help
import texts
import tg_source
from search_ui import NextView
from selection import ShownMemory, channel_scope, order_candidates

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("gelbooru_bot")

VERSION = "3.4.3"
KANZAKI_HIDERI_IMAGE = os.path.join(config.BASE_DIR, "assets", "kanzaki_hideri.jpg")

memory = ShownMemory(os.path.join(config.data_dir(), "shown.sqlite3"),
                     legacy_path=os.path.join(config.data_dir(), "recent_shown.json"))
GELBOORU = sources.Gelbooru(config.GELBOORU_API_KEY, config.GELBOORU_USER_ID)
KONACHAN = sources.Konachan()
SAFEBOORU = sources.Safebooru()
tg_client = None  # Telethon-клиент; поднимается в on_ready, None если ключи не заданы

# allowed_mentions=none: теги и названия каналов из запроса попадают в ответы —
# «`@everyone`» внутри тега не должен пинговать сервер.
bot = commands.Bot(intents=nextcord.Intents.default(), default_guild_ids=config.GUILD_IDS or None,
                   allowed_mentions=nextcord.AllowedMentions.none())


# ── Кулдауны (слэш-команды nextcord не поддерживают commands.cooldown) ─────────
class CooldownManager:
    """Sliding-window: не более `rate` вызовов за `per` секунд на пользователя."""

    def __init__(self, rate: int, per: float):
        self.rate, self.per = rate, per
        self._calls: dict[int, deque] = defaultdict(deque)

    def retry_after(self, key: int) -> float:
        """0.0 — можно (вызов засчитан), иначе секунды до освобождения слота."""
        now = time.monotonic()
        calls = self._calls[key]
        while calls and now - calls[0] >= self.per:
            calls.popleft()
        if len(calls) >= self.rate:
            return self.per - (now - calls[0])
        calls.append(now)
        return 0.0


# 5 поисков за 30 сек ≈ 1 поиск раз в 6 секунд на пользователя (с запасом на всплеск).
COOLDOWNS = {
    "gelbooru": CooldownManager(5, 30.0),
    "konachan": CooldownManager(5, 30.0),
    "safebooru": CooldownManager(5, 30.0),
    "tg": CooldownManager(3, 30.0),
    "tags": CooldownManager(1, 30.0),
    "tagcheck": CooldownManager(5, 30.0),
    "status": CooldownManager(1, 30.0),
}


async def reject_if_on_cooldown(interaction: nextcord.Interaction, cd: CooldownManager) -> bool:
    retry = cd.retry_after(interaction.user.id)
    if retry > 0:
        await interaction.response.send_message(texts.cooldown(retry), ephemeral=True)
        return True
    return False


def channel_allows_nsfw(interaction) -> bool:
    """NSFW — только в NSFW-каналах сервера (в ЛС нет возрастной проверки канала)."""
    if interaction.guild_id is None:
        return False
    is_nsfw = getattr(interaction.channel, "is_nsfw", None)
    try:
        return bool(is_nsfw()) if callable(is_nsfw) else False
    except Exception:
        return False


async def defer_or_log(interaction, label: str) -> bool:
    try:
        await interaction.response.defer(with_message=True)
        return True
    except (NotFound, nextcord.HTTPException) as error:
        logger.warning("[%s] defer не удался: %s", label, error)
        return False


# ── Особые теги («приколдес» и блокировки) ────────────────────────────────────
def tag_verdict(tags: list[str]) -> str | None:
    """'kanzaki' | 'venti' | 'lgbt' | 'blocked' | None. Kanzaki — раньше блокировок."""
    if any(cf.tag_is_kanzaki_hideri(t) for t in tags):
        return "kanzaki"
    for tag in tags:
        if cf.tag_triggers_venti(tag):
            return "venti"
        if cf.tag_triggers_lgbt_joke(tag):
            return "lgbt"
        if cf.tag_is_blocked(tag):
            return "blocked"
    return None


async def answer_verdict(interaction, verdict: str):
    if verdict == "kanzaki" and os.path.isfile(KANZAKI_HIDERI_IMAGE):
        await interaction.response.defer()

        async def asset_payload(_post):
            with open(KANZAKI_HIDERI_IMAGE, "rb") as stream:
                data = stream.read()
            return {"file": nextcord.File(BytesIO(data), filename="kanzaki_hideri.jpg"),
                    "_uids": delivery.content_uids(data)}, ""

        sent, reasons = await delivery.send_first_fitting(
            memory, interaction, channel_scope(interaction), [{"_uid": "asset:kanzaki_hideri"}], asset_payload)
        if sent is None:
            await delivery.safe_followup(interaction, texts.nothing_sent("kanzaki_hideri", "бота", reasons))
        return
    if verdict == "venti":
        return await interaction.response.send_message(texts.VENTI_WARNING)
    if verdict == "lgbt":
        return await interaction.response.send_message(texts.LGBT_JOKE_WARNING)
    return await interaction.response.send_message(texts.BLOCKED, ephemeral=True)


# ── Поиск по booru ────────────────────────────────────────────────────────────
def empty_result_text(res: search.SearchResult, display_tag: str, site: str) -> str:
    if res.stop_reason in {"api_end_before_count", "repeated_page"}:
        return texts.pagination_stopped(site, res.fetched)
    if res.allowed:
        return texts.ALL_SHOWN
    if not res.fetched:
        return texts.no_site_results(display_tag, site)
    return texts.nothing_found(display_tag, site)


def next_view(interaction, raw_tags, source, cooldown, nsfw):
    async def advance(next_interaction):
        return await run_booru_search(next_interaction, raw_tags, source, cooldown, nsfw=nsfw)
    return NextView(interaction.user.id, interaction.channel_id, advance)


async def run_booru_search(interaction, raw_tags: tuple, source, cooldown: CooldownManager,
                           *, nsfw: bool = True):
    """Общая логика /gelbooru, /konachan, /safebooru (nsfw=False — без NSFW и наготы)."""
    if nsfw and not channel_allows_nsfw(interaction):
        await interaction.response.send_message(texts.NSFW_ONLY, ephemeral=True)
        return False
    tags = cf.clean_user_tags(raw_tags)
    if not tags:
        await interaction.response.send_message(texts.NO_TAGS, ephemeral=True)
        return False
    verdict = tag_verdict(tags)
    if verdict == "blocked":                 # тихо, только автору, без траты кулдауна
        await answer_verdict(interaction, verdict)
        return False
    if await reject_if_on_cooldown(interaction, cooldown):
        return False
    if verdict:
        await answer_verdict(interaction, verdict)
        return False
    if not await defer_or_log(interaction, source.name):
        return False

    display_tag = " + ".join(tags)
    scope = channel_scope(interaction)
    try:
        started = time.monotonic()
        res = await search.search(source, tags, memory.search_history(scope), require_nudity=nsfw)
        diagnostics.record_search(scope, source, res, (time.monotonic() - started) * 1000)
        if res.errors:
            pending = "scan_pending" in res.errors
            text = (texts.scan_pending(res.fetched, res.total, priority=res.priority_pending) if pending
                    else texts.source_error(source.name, res.errors))
            kwargs = {"view": next_view(interaction, tuple(tags), source, cooldown, nsfw)} if pending else {}
            message = await interaction.followup.send(text, **kwargs)
            if pending:
                kwargs["view"].message = message
            return pending
        if not res.candidates:
            message = empty_result_text(res, display_tag, source.name)
            # Подсказка только показывается — сам запрос не меняется и не расширяется.
            if not res.fetched and res.complete and len(tags) == 1 and not tags[0].startswith("-"):
                hints = await tag_help.suggestions(source, tags[0], nsfw=channel_allows_nsfw(interaction),
                                                  limit=5)
                hints = [h for h in hints if h != tags[0]]
                if hints:
                    message += "\n💡 Возможно, ты имел в виду: " + ", ".join(f"`{h}`" for h in hints)
            await interaction.followup.send(message)
            return True
        max_size = delivery.max_upload_size(interaction)
        view = next_view(interaction, tuple(tags), source, cooldown, nsfw)

        async def make_payload(post):
            payload, reason = await delivery.build_booru_payload(source, post, display_tag, max_size)
            if payload is not None:
                payload["view"] = view
            return payload, reason
        sent, reasons = await delivery.send_first_fitting(
            memory, interaction, scope, res.candidates, make_payload)
        if sent is None:
            view.message = await delivery.safe_followup(
                interaction, texts.nothing_sent(display_tag, source.name, reasons), view=view)
        return True
    except NotFound:
        logger.warning("[%s] интеракция протухла", source.name)
    except Exception:
        logger.exception("[%s] ошибка поиска по `%s`", source.name, display_tag)
        await delivery.safe_followup(interaction, texts.INTERNAL_ERROR)
    return False


def register_booru_command(name: str, description: str, source, *, nsfw: bool = True):
    """Одна фабрика вместо трёх одинаковых команд /gelbooru, /konachan, /safebooru."""
    async def command(
        interaction: nextcord.Interaction,
        tag: str = nextcord.SlashOption(description=texts.TAG_OPTION, required=True, autocomplete=True),
        tag2: str = nextcord.SlashOption(description=texts.EXTRA_OPTION, required=False, default=None,
                                        autocomplete=True),
        tag3: str = nextcord.SlashOption(description=texts.EXTRA_OPTION, required=False, default=None,
                                        autocomplete=True),
        tag4: str = nextcord.SlashOption(description=texts.EXTRA_OPTION, required=False, default=None,
                                        autocomplete=True),
    ):
        await run_booru_search(interaction, (tag, tag2, tag3, tag4), source, COOLDOWNS[name], nsfw=nsfw)

    command.__name__ = name
    registered = bot.slash_command(name=name, description=description)(command)

    async def autocomplete(interaction, value):
        choices = await tag_help.suggestions(source, value, nsfw=channel_allows_nsfw(interaction))
        await interaction.response.send_autocomplete(choices)
    for option in ("tag", "tag2", "tag3", "tag4"):
        registered.on_autocomplete(option)(autocomplete)
    return registered


register_booru_command("gelbooru", "🔞 Арт по тегам с Gelbooru — приоритет твоему запросу (до 4 тегов)", GELBOORU)
register_booru_command("konachan", "🔞 Арт по тегам с Konachan — аниме-арт высокого качества (до 4 тегов)", KONACHAN)
register_booru_command("safebooru", "🟢 Safe-арт по тегам с Safebooru — без NSFW, в любом канале (до 4 тегов)",
                       SAFEBOORU, nsfw=False)


# ── /tags ─────────────────────────────────────────────────────────────────────
# Подборка популярных тегов; разрешённый 1boy не навязывается каждому запросу.
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


@bot.slash_command(name="tags", description="🏷️ Популярные теги и сколько по ним артов")
async def tags_list(interaction: nextcord.Interaction):
    if await reject_if_on_cooldown(interaction, COOLDOWNS["tags"]):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        info = await GELBOORU.tag_info(POPULAR_TAGS)
    except sources.SourceError as error:
        return await delivery.safe_followup(interaction, texts.source_error("Gelbooru", [error.kind]),
                                            ephemeral=True)
    counts = ((t, int(info[t].get("count") or 0)) for t in POPULAR_TAGS
              if t in info and not cf.tag_is_blocked(t))
    alive = sorted(((t, c) for t, c in counts if c > 0), key=lambda x: -x[1])
    lines = " • ".join(f"`{t}` {texts.short_count(c)}" for t, c in alive)
    embed = nextcord.Embed(title="🏷️ Популярные теги",
                           description=lines[:4000] or "Не удалось получить список тегов.", color=0x2ecc71)
    embed.set_footer(text="💡 Число — сколько артов на Gelbooru. Свой тег проверь через /tagcheck")
    await delivery.safe_followup(interaction, embed=embed, ephemeral=True)


# ── /tagcheck ─────────────────────────────────────────────────────────────────
@bot.slash_command(name="tagcheck", description="🔍 Проверить, есть ли на Gelbooru арты по тегу")
async def tag_check(interaction: nextcord.Interaction,
                    tag: str = nextcord.SlashOption(description="Тег для проверки", required=True,
                                                    autocomplete=True)):
    if not channel_allows_nsfw(interaction):
        return await interaction.response.send_message(texts.NSFW_ONLY, ephemeral=True)
    cleaned = cf.clean_user_tags([tag])
    if not cleaned:
        return await interaction.response.send_message("❌ Укажи тег.", ephemeral=True)
    clean_tag = cleaned[0].lstrip("-")
    if cf.tag_is_blocked(clean_tag):
        return await interaction.response.send_message(f"🚫 Тег `{clean_tag}` заблокирован в боте.",
                                                       ephemeral=True)
    if await reject_if_on_cooldown(interaction, COOLDOWNS["tagcheck"]):
        return
    await interaction.response.defer()
    try:
        info = (await GELBOORU.tag_info([clean_tag])).get(clean_tag)
        count = int(info.get("count") or 0) if info else 0
        deprecated = bool(info) and str(info.get("type")) == "6"
        similar = []
        part = clean_tag.strip("*%_")
        if (count == 0 or deprecated) and part:
            similar = [t for t in await GELBOORU.similar_tags(part, 8)
                       if t.get("name") != clean_tag and int(t.get("count") or 0) > 0
                       and not cf.tag_is_blocked(t["name"])][:5]
            if not similar:
                hints = await tag_help.suggestions(GELBOORU, clean_tag, nsfw=True, limit=5)
                similar = [{"name": name, "count": None} for name in hints if name != clean_tag]
    except sources.SourceError as error:
        return await delivery.safe_followup(interaction, texts.source_error("Gelbooru", [error.kind]))

    if count > 0:
        embed = nextcord.Embed(title=f"✅ Тег `{clean_tag}` активен!",
                               description=f"Артов на Gelbooru: **{count:,}**".replace(",", " "),
                               color=0x2ecc71)
        embed.add_field(name="💡 Используй", value=f"`/gelbooru {clean_tag}`", inline=False)
        if deprecated:
            embed.add_field(name="⚠️ Тег устаревший", value="Новые арты могут быть под другим именем.",
                            inline=False)
    else:
        embed = nextcord.Embed(title=f"❌ Тег `{clean_tag}` не найден",
                               description="На Gelbooru нет артов с таким тегом.", color=0xe74c3c)
    if similar:
        embed.add_field(name="🔎 Похожие теги", inline=False, value="\n".join(
            f"`{t['name']}`" + (f" — {texts.short_count(int(t['count']))}"
                               if t.get("count") is not None else " — вариант написания")
            for t in similar))
    elif count == 0:
        embed.add_field(name="💡 Попробуй", inline=False,
                        value="• Проверь написание (теги на английском, через `_`)\n• Посмотри `/tags`")
    await delivery.safe_followup(interaction, embed=embed)


@tag_check.on_autocomplete("tag")
async def tag_check_autocomplete(interaction, value):
    choices = await tag_help.suggestions(GELBOORU, value, nsfw=channel_allows_nsfw(interaction))
    await interaction.response.send_autocomplete(choices)


# ── /status ───────────────────────────────────────────────────────────────────
@bot.slash_command(name="status", description="🩺 Состояние бота, API и последнего поиска")
async def status_command(interaction: nextcord.Interaction):
    if await reject_if_on_cooldown(interaction, COOLDOWNS["status"]):
        return
    await interaction.response.defer(ephemeral=True, with_message=True)
    health = await asyncio.gather(*(diagnostics.probe(s) for s in (GELBOORU, KONACHAN, SAFEBOORU)))
    embed = nextcord.Embed(title=f"🩺 Бот v{VERSION}", color=0x3498db)
    uptime = max(0, int(time.monotonic() - diagnostics.STARTED))
    latency = bot.latency
    ping = f"{latency * 1000:.0f} мс" if math.isfinite(latency) else "ещё не измерена"
    embed.add_field(name="Discord", value=f"Задержка: {ping} · работает {uptime // 60} мин", inline=False)
    for source, result in zip((GELBOORU, KONACHAN, SAFEBOORU), health):
        embed.add_field(name=source.name, value=diagnostics.health_text(result), inline=False)
    embed.add_field(name="Кэш в памяти",
                    value=f"Страниц: {len(sources._page_cache)} · индексов: {len(search._scans)}",
                    inline=False)
    last = diagnostics.last_search(channel_scope(interaction))
    if last:
        result = ("приоритет ещё проверяется" if last["pending"] else
                  "порядок подтверждён" if last["proven"] else "частичная выдача")
        value = (f'{last["site"]} · {last["elapsed_ms"]:.0f} мс\n'
                 f'Просмотрено: {last["fetched"]:,} · после фильтров: {last["allowed"]:,}\n{result}')
        if last["errors"]:
            value += "\nСостояние: " + ", ".join(last["errors"])
    else:
        value = "После запуска в этом канале ещё не искали."
    embed.add_field(name="Последний booru-поиск в этом канале", value=value, inline=False)
    embed.set_footer(text="API-проверки кэшируются на 60 с. Успешный API не гарантирует доставку файла.")
    await delivery.safe_followup(interaction, embed=embed, ephemeral=True)


# ── /tg — арты из Telegram-каналов ────────────────────────────────────────────
async def run_tg_search(interaction, alias: str | None):
    """Топовый по реакциям ещё не показанный пост из выбранного/случайного канала."""
    label = "Telegram"
    if not channel_allows_nsfw(interaction):
        return await interaction.response.send_message(texts.NSFW_ONLY, ephemeral=True)
    if tg_client is None:
        return await interaction.response.send_message(
            "⚠️ Telegram-источник не настроен (нет TG_API_ID/TG_API_HASH или сессии).", ephemeral=True)
    channels = tg_source.load_channels()
    if not channels:
        return await interaction.response.send_message("⚠️ Список каналов пуст — заполни `tg_channels.json`.",
                                                       ephemeral=True)
    if alias:
        chosen = next((c for c in channels if c["alias"].lower() == alias.lower()), None)
        if chosen is None:
            available = ", ".join(f"`{c['alias']}`" for c in channels)
            return await interaction.response.send_message(f"❌ Канал `{alias}` не найден. Доступны: {available}",
                                                           ephemeral=True)
    else:
        chosen = random.choice(channels)
    if await reject_if_on_cooldown(interaction, COOLDOWNS["tg"]):
        return
    if not await defer_or_log(interaction, label):
        return

    scope = channel_scope(interaction)
    try:
        raw = await tg_source.fetch_channel_arts(tg_client, chosen["alias"], chosen["peer"])
        if not raw:
            return await interaction.followup.send(f"❌ В канале `{chosen['alias']}` не нашлось подходящих артов.")
        candidates = order_candidates(raw, memory.recent(scope))
        if not candidates:
            return await interaction.followup.send(texts.TG_ALL_SHOWN)
        max_size = delivery.max_upload_size(interaction)
        sent, reasons = await delivery.send_first_fitting(
            memory, interaction, scope, candidates, lambda p: delivery.build_tg_payload(tg_client, p, max_size))
        if sent is None:
            await delivery.safe_followup(interaction, texts.nothing_sent(chosen["alias"], label, reasons))
    except NotFound:
        logger.warning("[%s] интеракция протухла", label)
    except Exception:
        logger.exception("[%s] ошибка в канале %s", label, chosen["alias"])
        await delivery.safe_followup(interaction, texts.INTERNAL_ERROR)


@bot.slash_command(name="tg", description="🔞 Арт из телеграм канала")
async def tg_command(interaction: nextcord.Interaction,
                     channel: str = nextcord.SlashOption(name="channel", required=False, default=None,
                                                         description="Канал из списка (пусто — случайный)",
                                                         autocomplete=True)):
    await run_tg_search(interaction, channel)


@tg_command.on_autocomplete("channel")
async def tg_command_autocomplete(interaction: nextcord.Interaction, value: str):
    aliases = [c["alias"] for c in tg_source.load_channels()]
    if value:
        aliases = [a for a in aliases if value.lower() in a.lower()]
    await interaction.response.send_autocomplete(aliases[:25])


# ── /help ─────────────────────────────────────────────────────────────────────
@bot.slash_command(name="help", description="📖 Справка: список команд и как ими пользоваться")
async def help_command(interaction: nextcord.Interaction):
    embed = nextcord.Embed(title=f"📖 Справка по боту Gelbooru v{VERSION}",
                           description="Список всех доступных команд:", color=0x3498db)
    for name, value in texts.HELP_COMMANDS:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text=texts.HELP_FOOTER)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── События ───────────────────────────────────────────────────────────────────
_started = False


async def start_telegram() -> None:
    global tg_client
    logger.info("[tg] env: TG_API_ID=%s | TG_API_HASH=%s | TG_SESSION_STRING=%s",
                "есть" if config.TG_API_ID else "НЕТ", "есть" if config.TG_API_HASH else "НЕТ",
                "есть" if config.TG_SESSION_STRING else "НЕТ (будет искать файл сессии)")
    if not (config.TG_API_ID and config.TG_API_HASH):
        logger.warning("ℹ️ TG_API_ID / TG_API_HASH не заданы — команда /tg отключена.")
        return
    try:
        tg_client = await tg_source.start_client(int(config.TG_API_ID), config.TG_API_HASH)
        logger.info("✅ Telegram userbot подключён, каналов: %s", len(tg_source.load_channels()))
    except Exception as error:
        logger.error("⚠️ Telegram userbot не запущен: %s: %s — /tg недоступна.", type(error).__name__, error)


@bot.event
async def on_ready():
    """Срабатывает и после каждого переподключения — разовая настройка под флагом."""
    global _started
    logger.info("✅ Бот онлайн: %s • v%s • серверов: %s", bot.user, VERSION, len(bot.guilds))
    if _started:
        return
    _started = True
    if not (GELBOORU.api_key and GELBOORU.user_id):
        logger.warning("⚠️ GELBOORU_API_KEY / GELBOORU_USER_ID не заданы — /gelbooru будет получать 401.")
    if config.PROXY:
        logger.info("🌐 Запросы к сайтам идут через прокси: %s", config.masked_proxy())
    await start_telegram()
    # nextcord сам синхронизирует команды. В глобальном режиме сносим серверные
    # копии от прежнего guild-режима — иначе команды двоятся.
    if config.GUILD_IDS:
        logger.info("⚡ Слэш-команды для серверов: %s", config.GUILD_IDS)
        return
    for guild in list(bot.guilds):
        try:
            await bot.http.bulk_upsert_guild_commands(bot.application_id, guild.id, [])
        except Exception as error:
            logger.warning("Не удалось снести серверные команды на %s: %s", guild.id, error)
    logger.info("🌐 Слэш-команды глобальные (обновление до ~1 часа).")


@bot.event
async def on_application_command_error(interaction: nextcord.Interaction, error: Exception):
    name = getattr(interaction.application_command, "name", "?")
    logger.error("[command_error] /%s: %s: %s", name, type(error).__name__, error, exc_info=error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(texts.COMMAND_ERROR, ephemeral=True)
        else:
            await interaction.response.send_message(texts.COMMAND_ERROR, ephemeral=True)
    except Exception:
        pass


@bot.event
async def on_close():
    await sources.close_session()
    search.clear_search_cache()
    if tg_client is not None:
        try:
            await tg_client.disconnect()
        except Exception:
            pass


async def start_health_server() -> None:
    """HTTP-«пульс» для хостингов, которым нужен открытый порт (если задан PORT)."""
    if not config.HEALTH_PORT:
        return
    from aiohttp import web

    async def health(_request):
        return web.Response(text=f"ok, ready={bot.is_ready()}")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.HEALTH_PORT).start()
    logger.info("💓 Health-сервер слушает порт %s", config.HEALTH_PORT)


def main() -> None:
    if not config.DISCORD_TOKEN:
        raise SystemExit("❌ Не задан токен бота: переменная DISCORD_BOT_TOKEN "
                         "(в окружении или в файле .env рядом с ботом).")
    memory.load()
    bot.loop.create_task(start_health_server())
    try:
        bot.run(config.DISCORD_TOKEN)
    finally:
        memory.close()


if __name__ == "__main__":
    main()
