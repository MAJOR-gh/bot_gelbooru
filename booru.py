"""Booru-источники (Gelbooru, Konachan, Safebooru): HTTP, разбор ответов, поиск.

Как ищем: страницы идут по убыванию score (стр. 0 = топ-100 по лайкам).
Берём стр. 0; если свежих (ещё не показанных) лучших артов мало — догружаем
следующие страницы, потом запрос с `nude`. Страницы кэшируются на 10 минут,
поэтому повторные запросы того же тега почти не дёргают API.
"""
import asyncio
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import aiohttp

import content_filter as cf
from selection import order_candidates, post_uid

logger = logging.getLogger("gelbooru_bot.booru")

API_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=6)
MEDIA_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=6, sock_read=20)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json, text/xml, */*",
}

# Прокси для всех запросов (если сайты заблокированы у провайдера),
# напр. "http://user:pass@host:port".
PROXY = (os.environ.get("GELBOORU_PROXY")
         or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
         or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
         or None)


def masked_proxy() -> str | None:
    """Прокси для логов — без логина/пароля."""
    if not PROXY:
        return None
    parts = urlsplit(PROXY)
    host = parts.hostname or "?"
    return f"{parts.scheme}://{'***@' if parts.username else ''}{host}:{parts.port or ''}"


_session: aiohttp.ClientSession | None = None


async def get_session() -> aiohttp.ClientSession:
    """Общая HTTP-сессия (создаётся лениво, внутри event loop)."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=40, ttl_dns_cache=300),
            headers=HEADERS,
        )
    return _session


async def close_session() -> None:
    if _session is not None and not _session.closed:
        await _session.close()


class SourceError(Exception):
    """Источник не ответил. kind: auth (401/403), rate (429), down (5xx/сеть), bad."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind


async def fetch_text(url: str, params: dict, *, retries: int = 2) -> str:
    """GET → текст ответа. Ретраи на таймаут, обрыв, 429 и 5xx."""
    kind = "down"
    for attempt in range(1, retries + 1):
        try:
            s = await get_session()
            async with s.get(url, params=params, timeout=API_TIMEOUT, proxy=PROXY) as resp:
                if resp.status == 200:
                    return await resp.text()
                if resp.status in (401, 403):
                    raise SourceError("auth", f"HTTP {resp.status}")
                if resp.status != 429 and resp.status < 500:
                    raise SourceError("bad", f"HTTP {resp.status}")
                kind = "rate" if resp.status == 429 else "down"
                logger.warning(f"[http] {url} → HTTP {resp.status} (попытка {attempt}/{retries})")
        except SourceError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            kind = "down"
            logger.warning(f"[http] {url} → {type(e).__name__} (попытка {attempt}/{retries})")
        if attempt < retries:
            await asyncio.sleep(1.5 * attempt)
    raise SourceError(kind, url)


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_posts_page(text: str) -> tuple[list[dict] | None, int | None]:
    """JSON/XML ответ booru → (посты, всего постов по запросу или None)."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    else:
        if isinstance(data, dict):
            count = _to_int((data.get("@attributes") or {}).get("count"))
            post = data.get("post", [])
            if isinstance(post, dict):           # один пост приходит словарём
                post = [post]
            return (post if isinstance(post, list) else []), count
        return (data if isinstance(data, list) else []), None
    try:
        root = ET.fromstring(text)
        return [p.attrib for p in root.iter("post")], _to_int(root.attrib.get("count"))
    except ET.ParseError:
        pass
    urls = re.findall(r'file_url="([^"]+)"', text or "")
    return ([{"file_url": u} for u in urls] if urls else None), None


def parse_posts(text: str) -> list[dict] | None:
    return parse_posts_page(text)[0]


_FIELDS = ("id", "md5", "tags", "score", "rating", "file_url", "jpeg_url",
           "sample_url", "width", "height", "owner", "source", "file_size")


def _slim(post: dict, **override) -> dict:
    """Оставляем только нужные поля — кэш страниц не раздувает память."""
    out = {k: post.get(k) for k in _FIELDS}
    out.update(override)
    return out


# ── Источники ─────────────────────────────────────────────────────────────────
class Source:
    name = ""
    referer = ""
    page_size = 100
    max_pages = 6               # глубже стр. 5 (ранг ~600 по лайкам) не лезем
    ratings: frozenset = cf.NSFW_RATINGS

    def request(self, tags: list[str], page: int) -> tuple[str, dict]:
        raise NotImplementedError

    def normalize(self, post: dict) -> dict:
        raise NotImplementedError

    def post_url(self, post: dict) -> str:
        raise NotImplementedError


class Gelbooru(Source):
    name = "Gelbooru"
    referer = "https://gelbooru.com/"
    URL = "https://gelbooru.com/index.php"

    def __init__(self, api_key: str | None, user_id: str | None):
        self.api_key = api_key
        self.user_id = user_id

    def params(self, **extra) -> dict:
        p = {"page": "dapi", "q": "index", "json": "1", **extra}
        if self.api_key and self.user_id:
            p["api_key"] = self.api_key
            p["user_id"] = self.user_id
        return p

    def request(self, tags, page):
        # sort:score — самые залайканные. general режем на сервере, короткие
        # блэклисты (возраст/AI/HARD) тоже; полный блэклист — только локально.
        q = list(tags) + ["sort:score", "-rating:general"] + cf.SERVER_EXCLUDES
        return self.URL, self.params(s="post", limit=str(self.page_size),
                                     pid=str(page), tags=" ".join(q))

    def normalize(self, post):
        return _slim(post, rating=(post.get("rating") or "").lower(), _site=self.name)

    def post_url(self, post):
        return f"https://gelbooru.com/index.php?page=post&s=view&id={post.get('id')}"

    async def tag_info(self, names: list[str]) -> dict[str, dict]:
        """Инфо о тегах одним запросом: {имя: {"count": int, "type": int}}."""
        text = await fetch_text(self.URL, self.params(s="tag", limit="100", names=" ".join(names)))
        return {t["name"]: t for t in _tag_list(text) if t.get("name")}

    async def similar_tags(self, part: str, limit: int = 5) -> list[dict]:
        """Самые популярные теги, содержащие part (подсказка при опечатке)."""
        text = await fetch_text(self.URL, self.params(
            s="tag", limit=str(limit), orderby="count", order="DESC",
            name_pattern=f"%{part}%"))
        return _tag_list(text)


def _tag_list(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    tags = data.get("tag", []) if isinstance(data, dict) else data
    if isinstance(tags, dict):
        tags = [tags]
    return [t for t in tags if isinstance(t, dict)] if isinstance(tags, list) else []


class Konachan(Source):
    name = "Konachan"
    referer = "https://konachan.com/"
    URL = "https://konachan.com/post.json"
    MAX_TAGS = 6                # анонимам Konachan режет >6 тегов (HTTP 500)
    _RATING = {"s": "safe", "q": "questionable", "e": "explicit"}

    def request(self, tags, page):
        # Блэклисты не влезают в лимит тегов — фильтрация целиком локальная.
        # order:score важнее серверного -rating:s (safe и так режется локально).
        q = list(tags) + ["order:score"]
        if len(q) < self.MAX_TAGS:
            q.append("-rating:s")
        params = {"tags": " ".join(q[:self.MAX_TAGS]), "limit": str(self.page_size),
                  "page": str(page + 1)}          # Moebooru считает страницы с 1
        return self.URL, params

    def normalize(self, post):
        r = (post.get("rating") or "").lower()
        return _slim(post, owner=post.get("author"), rating=self._RATING.get(r, r),
                     _site=self.name)

    def post_url(self, post):
        return f"https://konachan.com/post/show/{post.get('id')}"


class Safebooru(Source):
    name = "Safebooru"
    referer = "https://safebooru.org/"
    URL = "https://safebooru.org/index.php"
    ratings = cf.SAFE_RATINGS   # на Safebooru есть ~17k questionable — режем

    def request(self, tags, page):
        q = list(tags) + ["sort:score", "-rating:questionable"] + cf.SERVER_EXCLUDES
        return self.URL, {"page": "dapi", "s": "post", "q": "index", "json": "1",
                          "limit": str(self.page_size), "pid": str(page), "tags": " ".join(q)}

    def normalize(self, post):
        return _slim(post, md5=(post.get("hash") or post.get("md5") or "").lower(),
                     rating=(post.get("rating") or "").lower(), _site=self.name)

    def post_url(self, post):
        return f"https://safebooru.org/index.php?page=post&s=view&id={post.get('id')}"


# ── Кэш страниц ───────────────────────────────────────────────────────────────
CACHE_TTL = 600.0
CACHE_MAX = 80
_cache: "OrderedDict[tuple, tuple[float, list[dict], int | None]]" = OrderedDict()


async def fetch_page(source: Source, tags: list[str], page: int) -> tuple[list[dict], int | None]:
    key = (source.name, tuple(tags), page)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        _cache.move_to_end(key)
        return hit[1], hit[2]
    url, params = source.request(tags, page)
    raw, count = parse_posts_page(await fetch_text(url, params))
    posts = [source.normalize(p) for p in (raw or []) if isinstance(p, dict)]
    _cache[key] = (time.monotonic(), posts, count)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)
    return posts, count


# ── Поиск кандидатов ──────────────────────────────────────────────────────────
NEED_FRESH = 5          # столько свежих лучших артов хватает, чтобы не листать дальше
NUDE_PAGES = 3          # сколько страниц добирать запросом «… nude»
CANDIDATE_LIMIT = 30


@dataclass
class SearchResult:
    candidates: list[dict]
    errors: list[str] = field(default_factory=list)   # kind'ы SourceError
    fetched: int = 0                                   # сколько постов пришло всего


async def search(source: Source, tags: list[str], recent, *,
                 require_nudity: bool = True) -> SearchResult:
    """Кандидаты к показу: лучшие свежие сначала (см. selection.order_candidates)."""
    groups = cf.focus_groups_for(tags)
    nudity = cf.requested_nudity_tags(groups)
    best_tier = cf.TOP_TIER if groups else 0
    seen = set(recent)
    strict: list[dict] = []
    broad: list[dict] = []
    uids: set[str] = set()
    res = SearchResult(candidates=[])

    def absorb(posts: list[dict]) -> None:
        res.fetched += len(posts)
        for p in posts:
            uid = post_uid(p)
            if uid in uids:
                continue
            uids.add(uid)
            if not cf.post_is_allowed(p, source.ratings):
                continue
            if not require_nudity or cf.has_nudity(p, nudity):
                strict.append(p)
            else:
                broad.append(p)

    def fresh_strict(min_tier: int = 0) -> int:
        return sum(1 for p in strict
                   if post_uid(p) not in seen and cf.focus_tier(p, groups) >= min_tier)

    async def load(extra: list[str], pages) -> bool:
        """Грузит страницы параллельно. True — дальше страниц по запросу нет."""
        pages = list(pages)
        if not pages:
            return True
        results = await asyncio.gather(*(fetch_page(source, tags + extra, n) for n in pages),
                                       return_exceptions=True)
        ended = False
        for n, r in zip(pages, results):
            if isinstance(r, BaseException):
                res.errors.append(getattr(r, "kind", "down"))
                if not isinstance(r, SourceError):
                    logger.error(f"[{source.name}] стр. {n}: {type(r).__name__}: {r}")
                ended = True
                continue
            posts, count = r
            absorb(posts)
            if len(posts) < source.page_size or (count is not None
                                                 and (n + 1) * source.page_size >= count):
                ended = True
        return ended

    def next_pages(first_count_hint: int | None, limit: int) -> range:
        last = limit - 1
        if first_count_hint is not None:
            last = min(last, (first_count_hint - 1) // source.page_size)
        return range(1, last + 1)

    # 1) Топ по score; не хватает свежего топ-фокуса — листаем глубже.
    try:
        first, count = await fetch_page(source, tags, 0)
    except SourceError as e:
        res.errors.append(e.kind)
        return res
    absorb(first)
    if len(first) >= source.page_size and fresh_strict(best_tier) < NEED_FRESH:
        await load([], next_pages(count, source.max_pages))

    # 2) Мало свежего раздетого — запрос с nude: сервер сам отдаст раздетые.
    #    Если по тегу вообще ничего нет — с nude тоже не будет, не дёргаем API.
    if require_nudity and first and fresh_strict() < NEED_FRESH and "nude" not in tags:
        if not await load(["nude"], [0]) and fresh_strict() < NEED_FRESH:
            await load(["nude"], range(1, NUDE_PAGES))

    res.candidates = order_candidates(strict, recent, groups, broad)[:CANDIDATE_LIMIT]
    return res


# ── Скачивание медиа ──────────────────────────────────────────────────────────
VIDEO_EXTS = frozenset({"mp4", "webm", "mov", "m4v"})
IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "gif", "webp"})


def url_ext(url: str) -> str:
    return os.path.splitext(urlsplit(url).path)[1].lstrip(".").lower()


@dataclass
class Media:
    data: bytes
    ext: str
    is_video: bool
    reduced: bool       # не оригинал, а сжатая версия (jpeg/sample)


async def download(url: str, max_size: int, referer: str = "") -> tuple[bytes | None, str | None]:
    """Скачать файл не больше max_size. → (данные, None) или (None, 'too_big'|'error')."""
    headers = {"Referer": referer} if referer else None
    for attempt in (1, 2):
        try:
            s = await get_session()
            async with s.get(url, headers=headers, timeout=MEDIA_TIMEOUT, proxy=PROXY) as resp:
                if resp.status != 200:
                    logger.warning(f"[download] HTTP {resp.status}: {url}")
                    return None, "error"
                ctype = resp.headers.get("Content-Type", "")
                if ctype.startswith("text/"):   # хотлинк-заглушка вместо картинки
                    logger.warning(f"[download] вместо файла пришёл {ctype}: {url}")
                    return None, "error"
                if resp.content_length and resp.content_length > max_size:
                    return None, "too_big"
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    buf.extend(chunk)
                    if len(buf) > max_size:
                        return None, "too_big"
                return bytes(buf), None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"[download] {type(e).__name__} (попытка {attempt}/2): {url}")
            if attempt == 1:
                await asyncio.sleep(1.5)
    return None, "error"


async def fetch_media(source: Source, post: dict, max_size: int) -> tuple[Media | None, str]:
    """Оригинал поста, а если не влезает — jpeg/sample-версия.

    → (Media, "") или (None, причина: 'too_big' | 'error' | 'no_file').
    """
    urls = []
    for key in ("file_url", "jpeg_url", "sample_url"):
        u = post.get(key)
        if u and isinstance(u, str) and u.startswith("http") and u not in urls:
            urls.append(u)
    if not urls:
        return None, "no_file"
    reason = "error"
    for i, url in enumerate(urls):
        known = _to_int(post.get("file_size")) if i == 0 else None
        if known and known > max_size:          # размер известен заранее (Konachan)
            reason = "too_big"
            continue
        data, why = await download(url, max_size, source.referer)
        if data is None:
            reason = why or "error"
            if why == "too_big":
                continue                         # пробуем версию поменьше
            return None, reason                  # битая ссылка — следующий пост
        ext = url_ext(url)
        is_video = ext in VIDEO_EXTS
        if not is_video and ext not in IMAGE_EXTS:
            ext = "png"
        return Media(data, ext, is_video, reduced=i > 0), ""
    return None, reason
