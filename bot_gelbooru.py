import nextcord
from nextcord.ext import commands
import aiohttp
import random
import os
import json
import time
import asyncio
import logging
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from io import BytesIO
from nextcord.errors import NotFound

import tg_source

# Настройка логов
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('gelbooru_bot')

intents = nextcord.Intents.default()
bot = commands.Bot(intents=intents)

# Глобальная сессия для HTTP запросов
session: aiohttp.ClientSession = None

# Таймауты
API_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)
IMG_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=5)
HEAD_TIMEOUT = aiohttp.ClientTimeout(total=5, connect=5)

# Семафор для /tags (параллельные запросы, не более 5 одновременно)
TAGS_SEMAPHORE = asyncio.Semaphore(5)


async def get_session() -> aiohttp.ClientSession:
    """Получить или создать глобальную сессию (создаётся внутри event loop)."""
    global session
    if session is None or session.closed:
        connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
        session = aiohttp.ClientSession(connector=connector)
    return session


# ── Кулдауны (nextcord-слэш-команды НЕ поддерживают commands.cooldown) ─────────
class CooldownManager:
    """Простой sliding-window кулдаун: не более `rate` вызовов за `per` секунд."""

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


GELBOORU_CD = CooldownManager(rate=3, per=30.0)
KONACHAN_CD = CooldownManager(rate=3, per=30.0)
TAGS_CD = CooldownManager(rate=1, per=30.0)
TAGCHECK_CD = CooldownManager(rate=5, per=30.0)


async def reject_if_on_cooldown(interaction: nextcord.Interaction, cd: CooldownManager) -> bool:
    """Если пользователь на кулдауне — отправляет сообщение и возвращает True."""
    retry = cd.retry_after(interaction.user.id)
    if retry > 0:
        await interaction.response.send_message(
            f"⏳ Слишком часто! Попробуй снова через **{retry:.0f}с**.",
            ephemeral=True,
        )
        return True
    return False


def channel_allows_nsfw(interaction: nextcord.Interaction) -> bool:
    """NSFW разрешён в личке и в NSFW-каналах."""
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


# ── Блэклист ────────────────────────────────────────────────────────────────
_BLACKLIST_TAGS = [
    "loli", "shota", "underage", "young", "child", "minor", "aged_down",
    "scat", "gore", "blood", "snuff", "rape", "abuse", "feces", "vore",
    "torture", "mutilation",
    "tentacles", "bestiality", "zoophilia", "inflation", "watersports",
    "piss", "pee", "peeing", "urine", "omorashi",
    "fart", "toilet", "diaper", "pregnancy", "pregnant", "birth", "group_sex",
    "furry", "anthro", "animal", "dog", "cat", "horse", "fox",
    "the_simpsons", "bart_simpson", "homer_simpson",
    "pokemon", "pikachu", "my_little_pony", "mlp", "steven_universe",
    "family_guy", "south_park", "rugrats", "disney", "cartoon",
    "3d",
    "futa", "futanari", "trap", "crossdressing", "femboy",
    "netorare", "ntr", "cheating", "cuckold",
    "mindbreak", "mind_control",
    "ryona", "bdsm", "bondage", "gag",
    "ai_generated", "ai_art", "stable_diffusion", "novelai", "midjourney",
    "nai_diffusion",
    "armpit_hair", "pubic_hair", "body_hair", "chest_hair", "leg_hair", "hairy",
    "smegma",
    "ugly", "ugly_man", "ugly_bastard", "old_man", "old_guy", "oyaji",
    "fat", "obese", "overweight", "chubby_male",
    "stubble",
    "vomit", "puke", "crying", "tears_of_pain",
    "forced", "non-consensual", "dubcon",
    "muscle_female", "extremely_muscular",
    "huge_belly", "saggy_breasts", "wrinkles",
    "bad_anatomy", "bad_hands", "bad_feet", "bad_face",
    "cuntboy", "gay", "lesbian", "shit", "dark-skinned_male",
    "male/male", "same_size_vore",
    "orc",
    # ── низкое качество / трешак (локальный отсев, не навязывает типаж) ──
    "lowres", "sketch", "wip", "unfinished", "jpeg_artifacts",
    "bad_proportions", "poorly_drawn", "ms_paint", "scan", "what",
    "old_woman", "granny",
]

# Строка отрицательных тегов для API-запроса ("-tag -tag2 ...")
BLACKLIST = " ".join(f"-{t}" for t in _BLACKLIST_TAGS)

# Set для быстрой локальной фильтрации — O(1) lookup
BLACKLIST_SET: set[str] = set(_BLACKLIST_TAGS)

# ── КРИТИЧЕСКИЙ блэклист (возрастные теги) ────────────────────────────────────
# Эти теги ВСЕГДА уходят в запрос к API (короткий список — НЕ вызывает 413),
# чтобы Gelbooru отсекал их на сервере, а не только локально.
_CRITICAL_TAGS = [
    "loli", "shota", "lolicon", "shotacon", "toddlercon",
    "underage", "child", "minor", "aged_down", "young",
]
CRITICAL_BLACKLIST = " ".join(f"-{t}" for t in _CRITICAL_TAGS)
# Локальная подстраховка: ловим и подстроки (loli внутри loli_dominance и т.п.)
CRITICAL_SET: set[str] = set(_CRITICAL_TAGS)

# ── AI / нейроарты ────────────────────────────────────────────────────────────
# Основной тег Gelbooru пишется через ДЕФИС ("ai-generated"). Шлём в API всегда
# (короткий список, 413 не вызовет) + локальная подстраховка по подстрокам.
_AI_TAGS = [
    "ai-generated", "ai-created", "ai-assisted", "ai_generated", "ai_art",
    "ai_art_(generation)", "stable_diffusion", "novelai", "nai_diffusion",
    "midjourney", "dall-e",
]
AI_BLACKLIST = " ".join(f"-{t}" for t in _AI_TAGS)
# Для локальной фильтрации сводим к корням, чтобы ловить любые вариации
AI_SUBSTRINGS = ("ai-generated", "ai_generated", "ai-created", "ai_art",
                 "stable_diffusion", "novelai", "nai_diffusion", "midjourney",
                 "dall-e", "dalle")

# ── HARD-блок: яой / мужской контент / фембои / бондаж / БДСМ ──────────────────
# СТРОГО запрещено и НЕотключаемо (даже если юзер укажет такой тег явно).
# Уходит в каждый API-запрос + ловится локально (точные теги и подстроки).
_HARD_TAGS = [
    # яой / мужской контент
    "yaoi", "bara", "gay", "male_only", "multiple_boys", "2boys", "3boys",
    "male/male", "boy_on_top", "male_focus", "1boy", "cum_on_male",
    # фембои / трапы / переодевание
    "femboy", "trap", "crossdressing", "otoko_no_ko", "cuntboy", "tomgirl",
    "astolfo_(fate)", "astolfo", "felix", "felix_argyle", "felix_argyle_(re:zero)",
    # футанари
    "futanari", "futa", "futa_on_male", "newhalf", "dickgirl",
    # бондаж / БДСМ / насилие в кадре
    "bdsm", "bondage", "shibari", "rope_bondage", "gag", "ball_gag",
    "ring_gag", "tape_gag", "collar", "leash", "chained", "shackles",
    "spanking", "whip", "flogger", "torture", "ryona",
    # страпон / пеггинг
    "strap-on", "strapon",
]
HARD_BLACKLIST = " ".join(f"-{t}" for t in _HARD_TAGS)
HARD_SET: set[str] = set(_HARD_TAGS)
# Подстроки для локальной ловли вариаций (yaoi_*, *_bondage, ball_gag и т.п.)
HARD_SUBSTRINGS = (
    "yaoi", "bara", "femboy", "futanari", "futa", "crossdress",
    "otoko_no_ko", "cuntboy", "bondage", "bdsm", "shibari", "_gag", "gag_",
    "ball_gag", "ring_gag", "leash", "shackle", "ryona",
    "strap-on", "strapon",
)

# ── Требование наготы ─────────────────────────────────────────────────────────
# Пост проходит, ТОЛЬКО если содержит хотя бы один из этих тегов (полная или
# частичная нагота). Так отсекаются полностью одетые арты.
NUDITY_TAGS: set[str] = {
    "nude", "completely_nude", "topless", "bottomless", "naked",
    "nipples", "breasts_out", "no_bra", "no_panties", "pussy",
    "uncensored", "exposed_breasts", "bare_breasts", "areola_slip",
    "nipple_slip", "covered_nipples", "clothing_aside", "bottomless_female",
    "open_clothes", "undressing", "partially_undressed", "clothes_lift",
    "skirt_lift", "shirt_lift", "bra", "panties", "lingerie", "underwear",
    "see-through", "wardrobe_malfunction", "cum", "sex", "vaginal", "anal",
    "fellatio", "paizuri", "cameltoe", "ass", "thong",
}
# Явная «полная одежда» — локально отбрасываем такие посты.
DRESSED_TAGS: set[str] = {"fully_clothed", "fully_dressed", "dressed"}


def post_is_clean(post: dict, allowed: set[str] | None = None,
                  require_nudity: bool = True) -> bool:
    """True если пост допустим к показу.

    Правила (по приоритету):
      1. HARD-блок (яой/фембои/бондаж/БДСМ/фута) — НЕотключаем, даже явным тегом.
      2. Возрастные и AI теги — НЕотключаемы (подстроки).
      3. Общий блэклист — можно обойти, если юзер сам запросил такой тег (allowed).
      4. Явно одетые (fully_clothed и т.п.) — отбрасываем.
      5. Требование наготы: должен быть хотя бы один nudity-тег.
    """
    allowed = allowed or set()
    raw = post.get("tags", "") or ""
    post_tags = set(raw.split())
    low_tags = {t.lower() for t in post_tags}

    # 1. HARD-блок — точные теги
    if not low_tags.isdisjoint(HARD_SET):
        return False
    # 1b + 2. HARD/возрастные/AI — по подстрокам
    for tag in low_tags:
        if any(h in tag for h in HARD_SUBSTRINGS):
            return False
        if any(crit in tag for crit in CRITICAL_SET):
            return False
        if any(ai in tag for ai in AI_SUBSTRINGS):
            return False

    # 3. Общий блэклист — АБСОЛЮТНЫЙ. Никаких исключений «юзер сам запросил».
    if not low_tags.isdisjoint(BLACKLIST_SET):
        return False

    # 4. Явно одетые — вон
    if not low_tags.isdisjoint(DRESSED_TAGS):
        return False

    # 5. Требование наготы (полная или частичная). В «умном» ослаблении выключается:
    #    тогда проходит любой NSFW-результат (safe/general уже отсечены на сервере).
    if require_nudity and low_tags.isdisjoint(NUDITY_TAGS):
        return False

    return True


# ── Блокированные пользовательские теги (точное совпадение токена) ────────────
BLOCKED_USER_TAGS = frozenset([
    "futa", "futanari", "femboy", "trap", "crossdressing", "yaoi", "bara",
    "gay", "bdsm", "bondage", "shibari", "otoko_no_ko", "cuntboy", "ryona",
    "astolfo", "felix",
])

# ── «Приколдес»: теги, при запросе которых выдаём грозное предупреждение ───────
LGBT_JOKE_TAGS = frozenset([
    "felix", "astolfo",
])
LGBT_JOKE_WARNING = (
    "⚠️ **ВНИМАНИЕ!** Запрос подобных материалов карается статьёй 6.21 УК ЧР "
    "«Хранение и распространение материалов содержащих ЛГБТ+ контент». "
    "В ближайшее время на вас будет заведено уголовное дело. "
    "Ваш IP адрес был передан МВД Чернарусской республики."
)


def tag_triggers_lgbt_joke(clean_tag: str) -> bool:
    """True если тег пользователя совпадает с «приколдес»-тегами (felix/astolfo)."""
    low = clean_tag.lower()
    tokens = set(low.split("_"))
    return low in LGBT_JOKE_TAGS or not tokens.isdisjoint(LGBT_JOKE_TAGS)


# ── «Судебное постановление» по запросу Venti ─────────────────────────────────
VENTI_TAGS = frozenset(["venti"])
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


def tag_triggers_venti(clean_tag: str) -> bool:
    """True если тег пользователя — Venti."""
    low = clean_tag.lower()
    tokens = set(low.split("_"))
    return low in VENTI_TAGS or not tokens.isdisjoint(VENTI_TAGS)


# ── Спец-картинка для Kanzaki Hideri ──────────────────────────────────────────
# Если юзер запросил этого персонажа — вместо поиска шлём заранее заготовленную
# локальную картинку (проверка идёт ДО блокировок).
KANZAKI_HIDERI_IMAGE = r"C:\Users\MAJOR\Downloads\Секретные файлы ЧДКЗ\3244c5e5-a79b-4718-9b89-4c8cfc008db8.jpg"
KANZAKI_HIDERI_TAGS = frozenset(["kanzaki_hideri", "kanzaki", "hideri"])


def tag_is_kanzaki_hideri(clean_tag: str) -> bool:
    """True если тег указывает на персонажа Kanzaki Hideri."""
    low = clean_tag.lower()
    if low in KANZAKI_HIDERI_TAGS:
        return True
    # «kanzaki hideri» / «kanzaki_hideri_(...)» и т.п. — ловим по обоим словам
    return "kanzaki" in low and "hideri" in low


def tag_is_blocked(clean_tag: str) -> bool:
    """True если тег пользователя запрещён — такой контент не показываем вообще."""
    low = clean_tag.lower()
    tokens = set(low.split("_"))
    # Явные пользовательские блок-теги (точное совпадение токена)
    if low in BLOCKED_USER_TAGS or not tokens.isdisjoint(BLOCKED_USER_TAGS):
        return True
    # Общий блэклист (furry, fat, zoophilia, pregnant, peeing и т.д.)
    # — точное совпадение (без токен-сплита, чтобы не ловить cat_ears/animal_ears).
    if low in BLACKLIST_SET:
        return True
    # Возрастные / AI / HARD — по подстрокам, ловим любые вариации
    if any(c in low for c in CRITICAL_SET):
        return True
    if any(a in low for a in AI_SUBSTRINGS):
        return True
    return any(h in low for h in HARD_SUBSTRINGS)


# ── Популярные теги ───────────────────────────────────────────────────────────
POPULAR_TAGS = [
    "1girl", "1boy", "2girls", "solo", "couple", "multiple_girls", "group",
    "breasts", "ass", "nude", "censored", "uncensored", "bikini", "school_uniform",
    "cat_ears", "neko", "kemonomimi", "maid", "twintails", "blonde", "blue_hair",
    "brown_hair", "pink_hair", "purple_hair", "red_hair", "white_hair", "silver_hair",
    "long_hair", "short_hair", "curly_hair", "twisted_torso", "highres",
    "masterpiece", "abs", "flexible", "looking_at_viewer", "smile", "blush",
    "outdoor", "indoor", "bed", "forest", "city", "beach", "swimsuit",
    "lingerie", "underwear", "thighhighs", "stockings", "socks", "gloves",
    "hat", "hairband", "ribbons", "bow", "glasses", "eyes", "hetero",
    "wallpaper", "comic", "game_cg", "western",
]

# Вшитые данные Gelbooru (API Access Credentials).
# Можно переопределить через переменные окружения GELBOORU_API_KEY / GELBOORU_USER_ID.
DEFAULT_API_KEY = "6b1df28500ac0cd4d984c238ef37f48e9f154f8ef5c353f739a9462b5c1018cce06df5903b39d1a3595bd6ca53e43d79ecbd6d4b38407bea3625bb5ec45f1827"
DEFAULT_USER_ID = "1748928"

API_KEY = os.environ.get("GELBOORU_API_KEY") or DEFAULT_API_KEY
USER_ID = os.environ.get("GELBOORU_USER_ID") or DEFAULT_USER_ID

# ── Telegram userbot (источник /tg) ────────────────────────────────────────────
TG_API_ID = os.environ.get("TG_API_ID")
TG_API_HASH = os.environ.get("TG_API_HASH")
tg_client = None  # Telethon-клиент; поднимается в on_ready, None если ключи не заданы
TG_CD = CooldownManager(rate=3, per=30.0)
# Пороги реакций для отсева слабых постов (реакций меньше, чем score Gelbooru).
TG_REACTION_FLOORS = [20, 5, 0]

# Прокси для всех запросов к Gelbooru (если сайт заблокирован у провайдера).
# Поддерживается http/https-прокси, напр. "http://user:pass@host:port".
# Берётся из GELBOORU_PROXY, иначе из стандартных HTTPS_PROXY/HTTP_PROXY.
PROXY = (
    os.environ.get("GELBOORU_PROXY")
    or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    or None
)

BASE_URL = "https://gelbooru.com/index.php"

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json, text/xml, */*",
    "Referer": "https://gelbooru.com/",
}


def base_params(**extra) -> dict:
    """Общие параметры запроса + ключи API (если заданы)."""
    params = {"page": "dapi", "q": "index", "json": "1"}
    params.update(extra)
    if API_KEY:
        params["api_key"] = API_KEY
    if USER_ID:
        params["user_id"] = USER_ID
    return params


# ── Утилиты ──────────────────────────────────────────────────────────────────

async def safe_followup(interaction: nextcord.Interaction, content=None, embed=None, **kwargs):
    try:
        return await interaction.followup.send(content=content, embed=embed, **kwargs)
    except NotFound:
        logger.warning("[safe_followup] Interaction expired")
    except Exception as e:
        logger.error(f"[safe_followup] error: {e}")
    return None


async def fetch_json(
    http_session: aiohttp.ClientSession,
    params: dict,
    timeout: aiohttp.ClientTimeout = None,
    retries: int = 3,
    backoff: float = 1.5,
) -> str | None:
    """GET-запрос с авторетраями на таймаут/ошибку соединения."""
    t = timeout or API_TIMEOUT
    for attempt in range(1, retries + 1):
        try:
            async with http_session.get(BASE_URL, params=params, headers=headers, timeout=t, proxy=PROXY) as resp:
                if resp.status == 200:
                    return await resp.text()
                logger.warning(f"[fetch_json] status={resp.status} attempt={attempt}")
                return None  # не таймаут — повторять бессмысленно
        except (aiohttp.ServerTimeoutError, asyncio.TimeoutError) as e:
            logger.warning(f"[fetch_json] timeout attempt={attempt}/{retries}: {e}")
            if attempt < retries:
                await asyncio.sleep(backoff * attempt)
        except aiohttp.ClientConnectionError as e:
            logger.warning(f"[fetch_json] connection error attempt={attempt}/{retries}: {e}")
            if attempt < retries:
                await asyncio.sleep(backoff * attempt)
        except Exception as e:
            logger.error(f"[fetch_json] unexpected error: {e}")
            return None
    return None


def parse_posts(text: str) -> list[dict] | None:
    """Разобрать JSON или XML ответ Gelbooru → список постов."""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            post = data.get("post", [])
            if isinstance(post, dict):  # один пост может прийти словарём
                return [post]
            return post or []
        return data or []
    except json.JSONDecodeError:
        pass
    try:
        root = ET.fromstring(text)
        return [p.attrib for p in root.findall(".//post")]
    except Exception:
        pass
    import re
    urls = re.findall(r'file_url="([^"]+)"', text)
    return [{"file_url": u} for u in urls] if urls else None


# Discord нередко принимает меньше, чем сообщает guild.filesize_limit, а в
# multipart к файлу добавляется ещё JSON эмбеда — берём безопасный потолок и запас.
UPLOAD_HARD_CAP = int(os.environ.get("MAX_UPLOAD_MB", "10")) * 1024 * 1024
UPLOAD_MARGIN = 512 * 1024  # запас на embed + overhead мультипарта
# Сколько «строгих» (раздетых) постов считаем достаточным, чтобы НЕ ослаблять фильтр.
MIN_POOL = 12


def max_upload_size(interaction: nextcord.Interaction) -> int:
    """Безопасный лимит загрузки: min(лимит сервера, потолок) минус запас."""
    guild = interaction.guild
    base = guild.filesize_limit if guild is not None else 10 * 1024 * 1024
    base = min(base, UPLOAD_HARD_CAP)
    return max(1 * 1024 * 1024, base - UPLOAD_MARGIN)


async def download_media(
    http_session: aiohttp.ClientSession, url: str, max_size: int
) -> tuple[bytes | None, int | None, str | None]:
    """Скачать файл с проверкой размера через HEAD.

    Возвращает (data, size, reason). data=None если не скачан;
    reason='too_big' если файл превышает лимит, иначе 'error'.
    """
    # HEAD — чтобы не качать огромный файл зря
    file_size = None
    try:
        async with http_session.head(url, headers=headers, timeout=HEAD_TIMEOUT, proxy=PROXY) as head_resp:
            cl = head_resp.headers.get("Content-Length")
            if cl:
                file_size = int(cl)
    except Exception:
        pass  # HEAD не поддерживается — продолжим без него

    if file_size is not None and file_size > max_size:
        return None, file_size, "too_big"

    for attempt in range(1, 4):
        try:
            async with http_session.get(url, headers=headers, timeout=IMG_TIMEOUT, proxy=PROXY) as resp:
                if resp.status != 200:
                    logger.warning(f"[download_media] status={resp.status}")
                    return None, file_size, "error"
                data = await resp.read()
                if len(data) > max_size:
                    return None, len(data), "too_big"
                return data, len(data), None
        except (aiohttp.ServerTimeoutError, asyncio.TimeoutError,
                aiohttp.ClientConnectionError):
            # таймаут или обрыв связи (частое через туннель) — ретраим
            logger.warning(f"[download_media] timeout/disconnect attempt={attempt}")
            if attempt < 3:
                await asyncio.sleep(1.5 * attempt)
        except Exception as e:
            logger.error(f"[download_media] error: {e}")
            return None, file_size, "error"
    return None, file_size, "error"


# ── Events ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    await get_session()  # создаём общую сессию (внутри event loop)
    load_recent_shown()  # восстанавливаем память показанных артов после рестарта
    logger.info(f"✅ Бот онлайн: {bot.user.name}")
    logger.info("📋 Версия: 1.5 (исправленная)")
    if not API_KEY or not USER_ID:
        logger.warning("⚠️ GELBOORU_API_KEY / GELBOORU_USER_ID не заданы — "
                       "возможны ограничения API Gelbooru.")
    if PROXY:
        logger.info(f"🌐 Запросы к Gelbooru идут через прокси: {PROXY}")

    # Telegram userbot — поднимаем один раз, переиспользуем общий event loop.
    global tg_client
    if tg_client is None and TG_API_ID and TG_API_HASH:
        try:
            tg_client = await tg_source.start_client(int(TG_API_ID), TG_API_HASH)
            chans = tg_source.load_channels()
            logger.info(f"✅ Telegram userbot подключён, каналов в списке: {len(chans)}")
        except Exception as e:
            logger.error(f"⚠️ Telegram userbot не запущен: {e} — /tg будет недоступна.")
    elif not (TG_API_ID and TG_API_HASH):
        logger.warning("ℹ️ TG_API_ID / TG_API_HASH не заданы — команда /tg отключена.")

    await bot.sync_application_commands()


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


# ── /help ─────────────────────────────────────────────────────────────────────

@bot.slash_command(name='help', description="📖 Справка по командам бота")
async def help_command(interaction: nextcord.Interaction):
    embed = nextcord.Embed(
        title="📖 Справка по боту Gelbooru v1.5",
        description="Список всех доступных команд:",
        color=0x3498db
    )
    embed.add_field(name="🔞 /gelbooru <тег> [тег2] [тег3] [тег4]", value="Случайный арт/гиф/видео по 1-4 тегам с **Gelbooru**", inline=False)
    embed.add_field(name="🔞 /konachan <тег> [тег2] [тег3] [тег4]", value="Случайный арт/гиф/видео по 1-4 тегам с **Konachan**", inline=False)
    embed.add_field(name="🔞 /tg [канал]", value="Топовый по реакциям арт из **Telegram**-канала (отсев рекламы)", inline=False)
    embed.add_field(name="🏷️ /tags", value="Показать доступные теги и их статус", inline=False)
    embed.add_field(name="🔍 /tagcheck <тег>", value="Проверить наличие картинок по тегу", inline=False)
    embed.add_field(name="📖 /help", value="Показать эту справку", inline=False)
    embed.set_footer(text="💡 Используй /tags чтобы увидеть популярные теги!")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── /tags ─────────────────────────────────────────────────────────────────────

async def _check_tag(http_session: aiohttp.ClientSession, tag: str) -> bool:
    """Проверить есть ли хотя бы 1 пост по тегу. Возвращает True/False."""
    async with TAGS_SEMAPHORE:
        params = base_params(s="post", limit="1", tags=tag)
        text = await fetch_json(http_session, params, retries=2)
        if not text:
            return False
        posts = parse_posts(text)
        return bool(posts)


@bot.slash_command(name='tags', description="🏷️ Показать доступные теги")
async def tags_list(interaction: nextcord.Interaction):
    if await reject_if_on_cooldown(interaction, TAGS_CD):
        return
    embed = nextcord.Embed(
        title="🏷️ Доступные теги",
        description="Проверяю наличие картинок по популярным тегам...",
        color=0x3498db
    )
    embed.set_footer(text="⏳ Пожалуйста, подождите...",
                     icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed, ephemeral=True)

    http_session = await get_session()
    tags_to_check = POPULAR_TAGS[:20]

    # Все запросы параллельно (ограничены семафором до 5 одновременно)
    results = await asyncio.gather(
        *[_check_tag(http_session, tag) for tag in tags_to_check],
        return_exceptions=True
    )

    tags_with_images = [t for t, ok in zip(tags_to_check, results) if ok is True]
    tags_without_images = [t for t, ok in zip(tags_to_check, results) if ok is not True]

    embed_result = nextcord.Embed(
        title="🏷️ Статус тегов",
        description=f"Проверено {len(tags_to_check)} тегов из популярных",
        color=0x2ecc71
    )
    if tags_with_images:
        embed_result.add_field(
            name=f"✅ Есть картинки ({len(tags_with_images)})",
            value=", ".join(f"`{t}`" for t in tags_with_images),
            inline=False
        )
    if tags_without_images:
        shown = tags_without_images[:15]
        tail = f" ... и ещё {len(tags_without_images) - 15}" if len(tags_without_images) > 15 else ""
        embed_result.add_field(
            name=f"❌ Нет картинок ({len(tags_without_images)})",
            value=", ".join(f"`{t}`" for t in shown) + tail,
            inline=False
        )
    embed_result.set_footer(text="💡 Найди свой тег и используй /gelbooru <тег>")

    try:
        await interaction.edit_original_message(embed=embed_result)
    except NotFound:
        try:
            await interaction.followup.send(embed=embed_result, ephemeral=True)
        except Exception as e:
            logger.error(f"[tags] Не удалось отправить результат: {e}")


# ── /tagcheck ─────────────────────────────────────────────────────────────────

@bot.slash_command(name='tagcheck', description="🔍 Проверить наличие картинок по тегу")
async def tag_check(interaction: nextcord.Interaction, tag: str):
    if not channel_allows_nsfw(interaction):
        return await interaction.response.send_message("🔞 Пиздуй в NSFW канал!", ephemeral=True)
    if await reject_if_on_cooldown(interaction, TAGCHECK_CD):
        return

    await interaction.response.defer()
    clean_tag = tag.strip().replace(" ", "_")
    http_session = await get_session()

    # Два запроса параллельно
    posts_params = base_params(s="post", limit="10", tags=clean_tag)
    tag_info_params = base_params(s="tag", names=clean_tag)

    posts_text, tag_text = await asyncio.gather(
        fetch_json(http_session, posts_params),
        fetch_json(http_session, tag_info_params),
        return_exceptions=True
    )

    # Разбираем посты
    count = 0
    if isinstance(posts_text, str):
        posts = parse_posts(posts_text)
        count = len(posts) if posts else 0

    # Разбираем инфо о теге
    tag_count = "N/A"
    if isinstance(tag_text, str):
        try:
            tag_data = json.loads(tag_text)
            if isinstance(tag_data, dict):
                tag_data = tag_data.get("tag", [])
            tag_info = tag_data[0] if isinstance(tag_data, list) and tag_data else {}
            tag_count = tag_info.get("count", "N/A")
        except Exception:
            pass

    if count > 0:
        embed = nextcord.Embed(
            title=f"✅ Тег `{clean_tag}` активен!",
            description=f"Найдено последних постов: **{count}** (всего: ~{tag_count})",
            color=0x2ecc71
        )
        embed.add_field(name="💡 Используй", value=f"`/gelbooru {clean_tag}` для поиска картинки", inline=False)
    else:
        embed = nextcord.Embed(
            title=f"❌ Тег `{clean_tag}` не найден",
            description="По этому тегу нет изображений или он заблокирован.",
            color=0xe74c3c
        )
        embed.add_field(
            name="💡 Попробуй",
            value="• Проверь правильность написания тега\n• Используй `/tags` для списка доступных тегов",
            inline=False
        )
    await interaction.followup.send(embed=embed)


# ── /gelbooru ─────────────────────────────────────────────────────────────────

# ── Богатый embed ─────────────────────────────────────────────────────────────
RATING_LABELS = {
    "general": "🟢 General",
    "safe": "🟢 Safe",
    "sensitive": "🟡 Sensitive",
    "questionable": "🟠 Questionable",
    "explicit": "🔴 Explicit",
}


def _rating_label(post: dict) -> str:
    return RATING_LABELS.get((post.get("rating") or "").lower(), "—")


def format_tags_preview(tags_str: str, limit: int = 14, maxlen: int = 950) -> str:
    """Список тегов поста → компактная строка `tag` `tag` … +N (обрезка по длине)."""
    tags = (tags_str or "").split()
    if not tags:
        return ""
    shown = tags[:limit]
    text = " ".join(f"`{t}`" for t in shown)
    if len(tags) > limit:
        text += f" … +{len(tags) - limit}"
    return text[:maxlen]


def post_page_url(post: dict) -> str:
    """Ссылка на страницу поста на его сайте-источнике."""
    pid = post.get("id", "")
    if post.get("_site") == "Konachan":
        return f"https://konachan.com/post/show/{pid}"
    return f"https://gelbooru.com/index.php?page=post&s=view&id={pid}"


def build_post_embed(post: dict, display_tag: str, size: int, filename: str,
                     is_sample: bool) -> nextcord.Embed:
    """Подробный embed: рейтинг, score, размер, разрешение, аффтор, теги, ссылки."""
    post_id = post.get("id", "")
    post_link = post_page_url(post)
    width, height = post.get("width"), post.get("height")
    source = (post.get("source") or "").strip()

    embed = nextcord.Embed(
        title="🖼 Результат по тегу",
        description=f"`{display_tag}`",
        color=0x00ff00,
    )
    embed.add_field(name="📊 Score", value=str(post.get("score", "N/A")), inline=True)
    embed.add_field(name="🔞 Рейтинг", value=_rating_label(post), inline=True)
    embed.add_field(name="📏 Размер", value=f"{size / (1024 * 1024):.1f} MB", inline=True)
    if width and height:
        embed.add_field(name="📐 Разрешение", value=f"{width}×{height}", inline=True)
    embed.add_field(name="👤 Аффтор", value=str(post.get("owner") or "—"), inline=True)
    if is_sample:
        embed.add_field(name="🗜 Версия", value="сжатый sample", inline=True)

    links = f"[Открыть пост]({post_link})"
    if source.startswith("http"):
        links += f" • [Источник]({source})"
    embed.add_field(name="🔗 Ссылки", value=links, inline=False)

    tags_preview = format_tags_preview(post.get("tags", ""))
    if tags_preview:
        embed.add_field(name="🏷️ Теги", value=tags_preview, inline=False)

    embed.set_image(url=f"attachment://{filename}")
    embed.set_footer(text=f"{post.get('_site', 'Gelbooru')} • ID {post_id}")
    return embed


async def build_candidate_payload(
    http_session: aiohttp.ClientSession,
    post: dict,
    display_tag: str,
    max_size: int,
) -> dict | None:
    """Скачивает медиа поста и готовит payload для отправки/редактирования.

    Пробует original → sample_url (если оригинал слишком большой).
    Возвращает {"content", "embed", "file"} или None (не влез / ошибка скачивания).
    """
    post_id = post.get("id", "")
    original = post.get("file_url") or post.get("image")
    sample = post.get("sample_url")
    url_options = [u for u in (original, sample) if u]

    for idx, url in enumerate(url_options):
        is_sample = idx > 0
        file_ext = url.split(".")[-1].split("?")[0].lower()
        is_video = file_ext in ("webm", "mp4")

        data, size, reason = await download_media(http_session, url, max_size)
        if not data:
            if reason == "too_big":
                continue  # пробуем следующий URL (sample)
            break  # ошибка скачивания — этот пост пропускаем

        if is_video:
            filename = f"gelbooru_{post_id or 'vid'}.{file_ext}"
            file = nextcord.File(BytesIO(data), filename=filename)
            post_link = post_page_url(post)
            content = (
                f"🎬 **Видео** `{display_tag}` • 📊 {post.get('score', 'N/A')} • "
                f"🔞 {_rating_label(post)} • {size / (1024 * 1024):.1f} MB • "
                f"[Открыть пост]({post_link})"
            )
            return {"content": content, "embed": None, "file": file, "_post": post}

        if file_ext not in ("jpg", "jpeg", "png", "gif", "webp"):
            file_ext = "png"
        filename = f"gelbooru_{post_id or 'img'}.{file_ext}".replace("..", ".")
        bio = BytesIO(data)
        bio.seek(0)
        file = nextcord.File(bio, filename=filename)
        embed = build_post_embed(post, display_tag, size, filename, is_sample)
        return {"content": None, "embed": embed, "file": file, "_post": post}

    return None


# ── Перебор кандидатов: следующий влезающий по размеру пост ────────────────────
async def pop_next_payload(
    http_session: aiohttp.ClientSession,
    candidates: list[dict],
    display_tag: str,
    max_size: int,
) -> dict | None:
    """Достаёт из списка следующего кандидата, который успешно скачался и влез.

    Мутирует candidates (pop слева). Возвращает None, если подходящих не осталось.
    """
    while candidates:
        post = candidates.pop(0)
        payload = await build_candidate_payload(http_session, post, display_tag, max_size)
        if payload:
            return payload
    return None


# ── Источники: Gelbooru + Konachan (Moebooru) ─────────────────────────────────
KONACHAN_URL = "https://konachan.com/post.json"
KONACHAN_MAX_TAGS = 6  # Konachan режет анонимов на ≤6 тегах (7 → HTTP 500)
_MOEBOORU_RATING = {"s": "safe", "q": "questionable", "e": "explicit"}


def normalize_konachan(post: dict) -> dict:
    """Пост Konachan → общий вид (owner, словесный rating, метка сайта)."""
    p = dict(post)
    p["owner"] = post.get("author") or "—"
    p["rating"] = _MOEBOORU_RATING.get((post.get("rating") or "").lower(),
                                       post.get("rating"))
    p["_site"] = "Konachan"
    return p


def by_score(posts: list[dict]) -> list[dict]:
    return sorted(posts, key=lambda x: int(x.get("score", 0) or 0), reverse=True)


# Балансный порог качества: держим самый высокий floor, при котором остаётся
# достаточно постов; для нишевых тегов плавно ослабляем вплоть до 0.
# Планка поднята (100/30/0) — отсекаем посредственное, в приоритете то, что
# набрало много лайков. Для нишевых тегов floor плавно опускается до 0.
QUALITY_FLOORS = [100, 30, 0]
MIN_GOOD = 6


def quality_floor(posts: list[dict], floors: list[int] = QUALITY_FLOORS) -> list[dict]:
    """Отсекаем низкорейтинговый хвост, но не уходим ниже MIN_GOOD результатов.

    floors — пороги score по убыванию. Для Gelbooru score измеряется сотнями,
    для реакций Telegram — десятками, поэтому источник передаёт свои пороги.
    """
    ranked = by_score(posts)
    for floor in floors:
        good = [p for p in ranked if int(p.get("score", 0) or 0) >= floor]
        if len(good) >= MIN_GOOD or floor == 0:
            return good
    return ranked


# Потолок веса при взвешенном перемешивании. √score достигает 12 уже при
# score≈144, поэтому все «хорошие» арты (после quality_floor) получают почти
# равный шанс — это и даёт разнообразие, не пуская мусор вперёд залайканного.
WEIGHT_CAP = 12.0


def weighted_score_shuffle(posts: list[dict]) -> list[dict]:
    """Перемешивание с приоритетом по score: чем больше лайков, тем выше шанс
    оказаться в начале — но порядок остаётся случайным, выдача не приедается.

    Алгоритм Эфраимидиса–Спиракиса (взвешенная выборка без возврата):
    ключ = u**(1/weight), сортировка по убыванию. Вес = √score с потолком
    WEIGHT_CAP: приоритет лайкам сохраняется, но один мега-залайканный арт уже
    не монополизирует топ — внутри «хорошего» тира выбор почти равномерный, и
    выдача перестаёт приедаться.
    """
    def sort_key(p: dict) -> float:
        score = int(p.get("score", 0) or 0)
        weight = min(WEIGHT_CAP, max(1.0, score) ** 0.5) + 1.0  # √score с потолком, вес ≥ 1
        u = random.random()
        return u ** (1.0 / weight)

    return sorted(posts, key=sort_key, reverse=True)


# ── Память недавно показанных артов (чтобы выдача не повторялась) ──────────────
# Переживает рестарт: пишется в JSON на диск и грузится при старте. Иначе на
# хостинге каждый редеплой/краш обнулял бы память и повторы возвращались с нуля.
RECENT_MAX = 60         # сколько последних артов помним на каждый тег-запрос
RECENT_MAX_KEYS = 500   # сколько тег-запросов держим, прежде чем вытеснять старые
RECENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recent_shown.json")
_recent_shown: dict[str, deque] = defaultdict(lambda: deque(maxlen=RECENT_MAX))


def load_recent_shown() -> None:
    """Грузит память показанных артов с диска (один раз при старте бота)."""
    try:
        with open(RECENT_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    for key, uids in data.items():
        if isinstance(uids, list):
            _recent_shown[key] = deque(uids[-RECENT_MAX:], maxlen=RECENT_MAX)
    logger.info(f"🗂 Память показанных артов загружена: {len(_recent_shown)} тег-запросов")


def save_recent_shown() -> None:
    """Атомарно сохраняет память на диск; при переполнении вытесняет старые ключи.

    Словари Python хранят порядок вставки, а свежеиспользованный ключ мы двигаем
    в конец (см. run_booru_search), поэтому срез с начала = выкидываем давно не
    запрашиваемые теги (псевдо-LRU).
    """
    try:
        items = list(_recent_shown.items())
        if len(items) > RECENT_MAX_KEYS:
            for key, _ in items[:len(items) - RECENT_MAX_KEYS]:
                _recent_shown.pop(key, None)
            items = items[-RECENT_MAX_KEYS:]
        data = {key: list(dq) for key, dq in items if dq}
        tmp = RECENT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, RECENT_FILE)
    except OSError as e:
        logger.warning(f"Не удалось сохранить память показанных артов: {e}")


def post_uid(post: dict) -> str:
    """Стабильный идентификатор арта (md5, иначе сайт+id)."""
    md5 = (post.get("md5") or "").lower()
    return md5 or f"{post.get('_site')}:{post.get('id')}"


def recent_key(label: str, tags_clean: list[str]) -> str:
    """Ключ памяти: источник + набор тегов (порядок тегов не важен)."""
    return label + "|" + ",".join(sorted(tags_clean))


def dedup_posts(posts: list[dict]) -> list[dict]:
    """Убираем повторы: один арт = один md5 (работает и между сайтами)."""
    seen, out = set(), []
    for p in posts:
        md5 = (p.get("md5") or "").lower()
        key = md5 if md5 else (p.get("_site"), p.get("id"))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


async def fetch_gelbooru(http_session: aiohttp.ClientSession,
                         tags_clean: list[str], extra_tags: list[str]) -> list[dict]:
    """Запрос к Gelbooru → сырые посты (без локального фильтра контента)."""
    # sort:score — берём самые заплюсованные арты (качество), а не случайные.
    # safe/general режем на сервере. Короткие блэклисты (возраст/AI/HARD)
    # уходят в запрос; полный BLACKLIST не шлём (раздувал URL → HTTP 413).
    parts = (tags_clean + extra_tags
             + ["sort:score", "-rating:safe", "-rating:general"])
    tags_query = (" ".join(parts) + " "
                  + CRITICAL_BLACKLIST + " " + AI_BLACKLIST + " " + HARD_BLACKLIST)
    params = base_params(s="post", limit="100", tags=tags_query)
    text = await fetch_json(http_session, params, retries=3)
    if not text:
        return []
    posts = parse_posts(text) or []
    for p in posts:
        p["_site"] = "Gelbooru"
    return posts


async def fetch_konachan(http_session: aiohttp.ClientSession,
                         tags_clean: list[str], extra_tags: list[str]) -> list[dict]:
    """Запрос к Konachan (Moebooru) → нормализованные посты.

    Из-за лимита ≤6 тегов блэклисты НЕ шлём — контент фильтрует post_is_clean
    локально. На сервере отсекаем только safe (`-rating:s`).
    """
    base = list(tags_clean) + list(extra_tags)
    query_tags = base + ["-rating:s", "order:score"]
    if len(query_tags) > KONACHAN_MAX_TAGS:
        query_tags = (base + ["-rating:s"])[:KONACHAN_MAX_TAGS]
    params = {"tags": " ".join(query_tags), "limit": "100"}

    text = None
    for attempt in range(1, 3):
        try:
            async with http_session.get(KONACHAN_URL, params=params, headers=headers,
                                        timeout=API_TIMEOUT, proxy=PROXY) as resp:
                if resp.status != 200:
                    logger.warning(f"[konachan] status={resp.status}")
                    return []
                text = await resp.text()
                break
        except (aiohttp.ServerTimeoutError, asyncio.TimeoutError,
                aiohttp.ClientConnectionError) as e:
            logger.warning(f"[konachan] timeout/disconnect attempt={attempt}: {e}")
            if attempt < 2:
                await asyncio.sleep(1.0)
            else:
                return []
        except Exception as e:
            logger.warning(f"[konachan] error: {e}")
            return []

    try:
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [normalize_konachan(p) for p in data]


async def run_booru_search(
    interaction: nextcord.Interaction,
    tags: tuple,
    *,
    fetch_fn,
    cooldown: CooldownManager,
    label: str,
):
    """Общая логика поиска. Отличие /gelbooru и /konachan — только источник fetch_fn."""
    if not channel_allows_nsfw(interaction):
        return await interaction.response.send_message("🔞 Пиздуй в NSFW канал!", ephemeral=True)
    if await reject_if_on_cooldown(interaction, cooldown):
        return

    try:
        await interaction.response.defer()
    except (NotFound, nextcord.HTTPException, aiohttp.ClientError,
            asyncio.TimeoutError) as e:
        # интеракция протухла / сеть моргнула во время реконнекта — продолжать нечего
        logger.warning(f"[{label}] defer не удался, отмена: {e}")
        return

    # Обработка тегов
    def clean(t):
        return t.strip().replace(" ", "_") if t and t.strip() else None

    tags_clean = [t for t in (clean(x) for x in tags) if t]
    display_tag = " + ".join([x for x in tags if x])

    if not tags_clean:
        return await interaction.followup.send("❌ Укажи хотя бы один тег.")

    # Спец-обработка Kanzaki Hideri — шлём заготовленную картинку (до блокировок)
    if any(tag_is_kanzaki_hideri(t) for t in tags_clean):
        if os.path.isfile(KANZAKI_HIDERI_IMAGE):
            return await interaction.followup.send(
                file=nextcord.File(KANZAKI_HIDERI_IMAGE)
            )
        return await interaction.followup.send("🚫 Один из тегов заблокирован.", ephemeral=True)

    # Проверка блокированных тегов (точное совпадение, без ложных срабатываний)
    for t in tags_clean:
        if tag_triggers_venti(t):
            return await interaction.followup.send(VENTI_WARNING)
        if tag_triggers_lgbt_joke(t):
            return await interaction.followup.send(LGBT_JOKE_WARNING)
        if tag_is_blocked(t):
            return await interaction.followup.send("🚫 Один из тегов заблокирован.", ephemeral=True)

    allowed = set(tags_clean)
    http_session = await get_session()

    try:
        # 1) Сырые посты по тегу + строгий фильтр (требуется нагота).
        raw = dedup_posts(await fetch_fn(http_session, tags_clean, []))
        strict = [p for p in raw if post_is_clean(p, allowed, require_nudity=True)]

        # 2) Мало строгих — добираем целевым запросом с nude и снова фильтруем.
        if len(strict) < MIN_POOL:
            raw = dedup_posts(raw + await fetch_fn(http_session, tags_clean, ["nude"]))
            strict = [p for p in raw if post_is_clean(p, allowed, require_nudity=True)]

        # 3) Порог качества + умное ослабление. Посты уже приходят с sort:score,
        #    здесь отсекаем низкорейтинговый хвост и перемешиваем взвешенно
        #    (по score) — самые залайканные в приоритете, но выдача не приедается.
        #    Раздетое (strict) всегда идёт первым; одетое добираем, только если мало.
        strict_q = weighted_score_shuffle(quality_floor(strict))
        strict_count = len(strict_q)

        if strict_count >= MIN_POOL:
            pool = strict_q
        else:
            broad = [p for p in raw if post_is_clean(p, allowed, require_nudity=False)]
            strict_obj = {id(p) for p in strict}
            extra = weighted_score_shuffle(
                quality_floor([p for p in broad if id(p) not in strict_obj])
            )
            pool = strict_q + extra

        if not pool:
            return await interaction.followup.send(
                f"❌ По тегу `{display_tag}` на {label} ничего не найдено."
            )

        # Исключаем недавно показанные арты по этому тегу. Если так пул опустел
        # (всё уже видели) — сбрасываем фильтр, чтобы не отвечать «не найдено».
        rkey = recent_key(label, tags_clean)
        recent = _recent_shown[rkey]
        fresh = [p for p in pool if post_uid(p) not in recent]
        pool = fresh or pool

        candidates = pool[:50]

        max_size = max_upload_size(interaction)

        # 4) Отправка с самоисцелением: при 413 от Discord — следующий кандидат.
        payload = await pop_next_payload(http_session, candidates, display_tag, max_size)
        sent_msg = None
        attempts = 0
        while payload is not None and attempts < 8:
            attempts += 1
            try:
                sent_msg = await interaction.followup.send(
                    content=payload["content"],
                    embed=payload["embed"],
                    file=payload["file"],
                )
                recent.append(post_uid(payload["_post"]))  # запомнили показанное
                _recent_shown.pop(rkey, None)  # двигаем ключ в конец словаря (LRU)
                _recent_shown[rkey] = recent
                save_recent_shown()            # переживёт рестарт хостинга
                break
            except nextcord.HTTPException as e:
                if getattr(e, "status", None) == 413:
                    logger.warning(f"[{label}] 413 на отправке — пробую следующего кандидата")
                    payload = await pop_next_payload(http_session, candidates, display_tag, max_size)
                    continue
                raise

        if sent_msg is None:
            return await safe_followup(
                interaction,
                f"❌ По тегу `{display_tag}` все подходящие файлы слишком большие для загрузки."
            )

    except NotFound as nf:
        logger.warning(f"[{label}] Interaction expired: {nf}")
    except Exception as e:
        logger.error(f"--- КРИТИЧЕСКАЯ ОШИБКА ({label}) ---\nТип: {type(e).__name__}\nОписание: {e}\n")
        await safe_followup(interaction, "❌ Произошла внутренняя ошибка. Проверь консоль бота.")


@bot.slash_command(name='gelbooru', description="🔞 Поиск артов на Gelbooru (до 4 тегов)")
async def gelbooru(
    interaction: nextcord.Interaction,
    tag: str,
    tag2: str = None,
    tag3: str = None,
    tag4: str = None,
):
    await run_booru_search(
        interaction, (tag, tag2, tag3, tag4),
        fetch_fn=fetch_gelbooru, cooldown=GELBOORU_CD, label="Gelbooru",
    )


@bot.slash_command(name='konachan', description="🔞 Поиск артов на Konachan (до 4 тегов)")
async def konachan(
    interaction: nextcord.Interaction,
    tag: str,
    tag2: str = None,
    tag3: str = None,
    tag4: str = None,
):
    await run_booru_search(
        interaction, (tag, tag2, tag3, tag4),
        fetch_fn=fetch_konachan, cooldown=KONACHAN_CD, label="Konachan",
    )


# ── /tg — арты из Telegram-каналов ─────────────────────────────────────────────

async def build_tg_payload(post: dict, max_size: int) -> dict | None:
    """Скачивает медиа TG-поста и готовит payload для отправки в Discord."""
    msg = post["_msg"]
    data, size, reason = await tg_source.download_media(tg_client, msg, max_size)
    if not data:
        return None  # слишком большой или ошибка — пробуем следующего

    ext = tg_source.media_ext(msg)
    is_video = ext in ("mp4", "webm", "gif")
    filename = f"tg_{post['_alias']}_{post['id']}.{ext}".replace("..", ".")
    bio = BytesIO(data)
    bio.seek(0)
    file = nextcord.File(bio, filename=filename)

    link = tg_source.post_link(post)
    reactions = post.get("score", 0)
    if is_video:
        parts = [f"🎬 **{post['_alias']}** • ❤️ {reactions} • {size / (1024*1024):.1f} MB"]
        if link:
            parts.append(f"[Открыть пост]({link})")
        return {"content": " • ".join(parts), "embed": None, "file": file, "_post": post}

    embed = nextcord.Embed(title="🖼 Арт из Telegram", color=0x229ED9)
    embed.add_field(name="📡 Канал", value=str(post["_alias"]), inline=True)
    embed.add_field(name="❤️ Реакции", value=str(reactions), inline=True)
    embed.add_field(name="📏 Размер", value=f"{size / (1024*1024):.1f} MB", inline=True)
    if link:
        embed.add_field(name="🔗 Ссылка", value=f"[Открыть пост]({link})", inline=False)
    embed.set_image(url=f"attachment://{filename}")
    embed.set_footer(text=f"Telegram • {post['_alias']} • ID {post['id']}")
    return {"content": None, "embed": embed, "file": file, "_post": post}


async def run_tg_search(interaction: nextcord.Interaction, alias: str | None):
    """Достать топовый по реакциям арт из выбранного (или случайного) канала."""
    label = "Telegram"
    if not channel_allows_nsfw(interaction):
        return await interaction.response.send_message("🔞 Пиздуй в NSFW канал!", ephemeral=True)
    if await reject_if_on_cooldown(interaction, TG_CD):
        return

    if tg_client is None:
        return await interaction.response.send_message(
            "⚠️ Telegram-источник не настроен (нет TG_API_ID/TG_API_HASH или сессии).",
            ephemeral=True,
        )

    channels = tg_source.load_channels()
    if not channels:
        return await interaction.response.send_message(
            "⚠️ Список каналов пуст — заполни `tg_channels.json`.", ephemeral=True
        )

    if alias:
        chosen = next((c for c in channels if c["alias"].lower() == alias.lower()), None)
        if chosen is None:
            avail = ", ".join(f"`{c['alias']}`" for c in channels)
            return await interaction.response.send_message(
                f"❌ Канал `{alias}` не найден. Доступны: {avail}", ephemeral=True
            )
    else:
        chosen = random.choice(channels)

    try:
        await interaction.response.defer()
    except (NotFound, nextcord.HTTPException) as e:
        logger.warning(f"[{label}] defer не удался: {e}")
        return

    try:
        raw = await tg_source.fetch_channel_arts(tg_client, chosen["alias"], chosen["peer"])
        if not raw:
            return await interaction.followup.send(
                f"❌ В канале `{chosen['alias']}` не нашлось подходящих артов."
            )

        # Порог реакций (мягко опускается) → взвешенный рандом → память показанных.
        pool = weighted_score_shuffle(quality_floor(raw, TG_REACTION_FLOORS))
        rkey = recent_key(label, [chosen["alias"]])
        recent = _recent_shown[rkey]
        fresh = [p for p in pool if post_uid(p) not in recent]
        pool = fresh or pool

        candidates = pool[:50]
        max_size = max_upload_size(interaction)

        payload = None
        while candidates and payload is None:
            payload = await build_tg_payload(candidates.pop(0), max_size)

        sent_msg = None
        attempts = 0
        while payload is not None and attempts < 8:
            attempts += 1
            try:
                sent_msg = await interaction.followup.send(
                    content=payload["content"], embed=payload["embed"], file=payload["file"]
                )
                recent.append(post_uid(payload["_post"]))
                break
            except nextcord.HTTPException as e:
                if getattr(e, "status", None) == 413:
                    payload = None
                    while candidates and payload is None:
                        payload = await build_tg_payload(candidates.pop(0), max_size)
                    continue
                raise

        if sent_msg is None:
            return await safe_followup(
                interaction,
                f"❌ В канале `{chosen['alias']}` подходящие файлы слишком большие для загрузки."
            )
    except NotFound:
        logger.warning(f"[{label}] Interaction expired")
    except Exception as e:
        logger.error(f"--- КРИТИЧЕСКАЯ ОШИБКА ({label}) ---\nТип: {type(e).__name__}\nОписание: {e}\n")
        await safe_followup(interaction, "❌ Произошла внутренняя ошибка. Проверь консоль бота.")


@bot.slash_command(name="tg", description="🔞 Топовый по реакциям арт из Telegram-канала")
async def tg_command(
    interaction: nextcord.Interaction,
    channel: str = nextcord.SlashOption(
        name="channel",
        description="Канал из списка (пусто — случайный)",
        required=False,
        default=None,
        autocomplete=True,
    ),
):
    await run_tg_search(interaction, channel)


@tg_command.on_autocomplete("channel")
async def tg_command_autocomplete(interaction: nextcord.Interaction, value: str):
    aliases = [c["alias"] for c in tg_source.load_channels()]
    if value:
        aliases = [a for a in aliases if value.lower() in a.lower()]
    await interaction.response.send_autocomplete(aliases[:25])


if __name__ == "__main__":
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "❌ Не задан токен бота. Установи переменную окружения DISCORD_BOT_TOKEN.\n"
            "   Пример: export DISCORD_BOT_TOKEN='ваш_токен'"
        )
    bot.run(token)
