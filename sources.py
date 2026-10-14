"""Booru-сайты (Gelbooru, Safebooru, Konachan): HTTP, разбор ответов, страницы, медиа.

Здесь нет логики выбора арта — только «спросить сайт и честно разобрать ответ».
Обход всей выдачи живёт в search.py.
"""
import asyncio
import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import aiohttp

import config
import content_filter as cf

logger = logging.getLogger("gelbooru_bot.sources")

API_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=6)
MEDIA_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=6, sock_read=20)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Accept": "application/json, text/xml, */*"}
API_CONCURRENCY = 3          # одновременных запросов к одному сайту
PAGE_CACHE_TTL = 600.0
PAGE_CACHE_MAX = 80


# ── HTTP ──────────────────────────────────────────────────────────────────────
class SourceError(Exception):
    """Сайт не ответил. kind: auth (401/403), rate (429), down (5xx/сеть), bad, deep."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind


_session: aiohttp.ClientSession | None = None


async def get_session() -> aiohttp.ClientSession:
    """Общая HTTP-сессия (создаётся лениво, внутри event loop)."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=40, ttl_dns_cache=300), headers=HEADERS)
    return _session


async def close_session() -> None:
    if _session is not None and not _session.closed:
        await _session.close()


async def fetch_text(url: str, params: dict, *, retries: int = 2) -> str:
    """GET → текст ответа. Ретраи на таймаут, обрыв, 429 и 5xx."""
    kind = "down"
    for attempt in range(1, retries + 1):
        try:
            session = await get_session()
            async with session.get(url, params=params, timeout=API_TIMEOUT, proxy=config.PROXY) as resp:
                if resp.status == 200:
                    return await resp.text()
                if resp.status in (401, 403):
                    raise SourceError("auth", f"HTTP {resp.status}")
                # Safebooru при всплеске запросов временно отвечает 404 — это лимит, а не «нет API».
                throttled = resp.status in (404, 429)
                if not throttled and resp.status < 500:
                    raise SourceError("bad", f"HTTP {resp.status}")
                kind = "rate" if throttled else "down"
                logger.warning("[http] %s → HTTP %s (попытка %s/%s)", url, resp.status, attempt, retries)
        except SourceError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            kind = "down"
            logger.warning("[http] %s → %s (попытка %s/%s)", url, type(error).__name__, attempt, retries)
        if attempt < retries:
            await asyncio.sleep(1.5 * attempt)
    raise SourceError(kind, url)


# ── Разбор ответов ────────────────────────────────────────────────────────────
def to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_posts_page(text: str) -> tuple[list[dict] | None, int | None]:
    """JSON/XML ответ booru → (посты или None, если это не выдача; count или None)."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        data = None
    else:
        if isinstance(data, list):
            return data, None
        if not isinstance(data, dict):
            return None, None
        count = to_int((data.get("@attributes") or {}).get("count"))
        if "post" not in data and count is None:
            return None, None
        posts = data.get("post", [])
        if isinstance(posts, dict):              # один пост приходит словарём
            posts = [posts]
        return (posts if isinstance(posts, list) else []), count
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None, None
    if root.tag != "posts":
        return None, None
    return [p.attrib for p in root.iter("post")], to_int(root.attrib.get("count"))


def parse_posts(text: str) -> list[dict] | None:
    return parse_posts_page(text)[0]


def parse_tag_list(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        try:
            root = ET.fromstring(text)
        except (ET.ParseError, TypeError):
            return []
        if root.tag != "tags":
            return []
        return [dict(t.attrib) for t in root.findall("tag") if t.get("name")]
    tags = data.get("tag", []) if isinstance(data, dict) else data
    if isinstance(tags, dict):
        tags = [tags]
    return [t for t in tags if isinstance(t, dict)] if isinstance(tags, list) else []


_FIELDS = ("id", "md5", "tags", "score", "rating", "file_url", "jpeg_url",
           "sample_url", "width", "height", "owner", "source", "file_size")


def slim(post: dict, **override) -> dict:
    """Оставляем только нужные поля — кэш и индекс не раздуваются."""
    out = {k: post.get(k) for k in _FIELDS}
    out.update(override)
    return out


# ── Курсор глубокой пагинации ─────────────────────────────────────────────────
@dataclass(frozen=True)
class Cursor:
    """Где читать выдачу, когда обычные страницы (pid) упираются в потолок сайта.

    below=S          → `score:<S`, сортировка по score, страницы pid с нуля;
    bucket=S         → все посты ровно с score S, листание по `id:<before_id`.
    Так обходится ВСЯ выдача в порядке убывания score, без потолка pid.
    """
    below: int | None = None
    bucket: int | None = None
    before_id: int | None = None

    def as_dict(self) -> dict:
        return {"below": self.below, "bucket": self.bucket, "before_id": self.before_id}


# ── Источники ─────────────────────────────────────────────────────────────────
class Source:
    name = ""
    referer = ""
    page_size = 100
    ratings: frozenset = cf.NSFW_RATINGS
    max_offset: int | None = None     # потолок pid*limit у сайта (None — нет)
    supports_cursor = False           # умеет ли сайт score:/id: метатеги без лимита тегов

    def request(self, tags: list[str], page: int, cursor: Cursor | None = None) -> tuple[str, dict]:
        raise NotImplementedError

    def normalize(self, post: dict) -> dict:
        raise NotImplementedError

    def post_url(self, post: dict) -> str:
        raise NotImplementedError


class DapiSource(Source):
    """Движок Gelbooru (Gelbooru, Safebooru): page=dapi, pid с нуля."""
    URL = ""
    sort_score = "sort:score"
    sort_id = "sort:id:desc"
    server_tags: tuple[str, ...] = ()
    supports_cursor = True

    def params(self, **extra) -> dict:
        return {"page": "dapi", "q": "index", "json": "1", **extra}

    def query_tags(self, tags: list[str], cursor: Cursor | None) -> list[str]:
        q = list(tags)
        if cursor and cursor.bucket is not None:
            q.append(f"score:{cursor.bucket}")
            if cursor.before_id is not None:
                q.append(f"id:<{cursor.before_id}")
            q.append(self.sort_id)
        else:
            if cursor and cursor.below is not None:
                q.append(f"score:<{cursor.below}")
            q.append(self.sort_score)
        # Блэклисты — короткие (URL не раздувается); полный список режется локально.
        return q + list(self.server_tags) + cf.SERVER_EXCLUDES

    def request(self, tags, page, cursor=None):
        return self.URL, self.params(s="post", limit=str(self.page_size), pid=str(page),
                                     tags=" ".join(self.query_tags(tags, cursor)))

    def post_url(self, post):
        return f"{self.URL}?page=post&s=view&id={post.get('id')}"

    async def tag_info(self, names: list[str]) -> dict[str, dict]:
        async with _limiter(self):
            text = await fetch_text(self.URL, self.params(s="tag", limit="100", names=" ".join(names)))
        return {t["name"]: t for t in parse_tag_list(text) if t.get("name")}

    async def similar_tags(self, part: str, limit: int = 5, *, retries: int = 2) -> list[dict]:
        async with _limiter(self):
            text = await fetch_text(self.URL, self.params(
                s="tag", limit=str(limit), orderby="count", order="DESC", name_pattern=f"%{part}%"),
                retries=retries)
        return parse_tag_list(text)


class Gelbooru(DapiSource):
    name = "Gelbooru"
    referer = "https://gelbooru.com/"
    URL = "https://gelbooru.com/index.php"
    server_tags = ("-rating:general",)
    max_offset = 20_000          # глубже pid*limit Gelbooru отвечает «Too deep»

    def __init__(self, api_key: str | None = None, user_id: str | None = None):
        self.api_key = api_key
        self.user_id = user_id

    def params(self, **extra) -> dict:
        p = super().params(**extra)
        if self.api_key and self.user_id:
            p.update(api_key=self.api_key, user_id=self.user_id)
        return p

    def normalize(self, post):
        return slim(post, rating=(post.get("rating") or "").lower(), _site=self.name)


class Safebooru(DapiSource):
    name = "Safebooru"
    referer = "https://safebooru.org/"
    URL = "https://safebooru.org/index.php"
    ratings = cf.SAFE_RATINGS          # на Safebooru есть ~17k questionable — режем
    server_tags = ("-rating:questionable",)

    def normalize(self, post):
        return slim(post, md5=(post.get("hash") or post.get("md5") or "").lower(),
                    rating=(post.get("rating") or "").lower(), _site=self.name)


class Konachan(Source):
    name = "Konachan"
    referer = "https://konachan.com/"
    URL = "https://konachan.com/post.json"
    MAX_TAGS = 6                # анонимам Konachan режет >6 тегов (HTTP 500)
    _RATING = {"s": "safe", "q": "questionable", "e": "explicit"}

    def request(self, tags, page, cursor=None):
        # Блэклисты не влезают в лимит тегов — фильтрация целиком локальная.
        # order:score важнее серверного -rating:s (safe и так режется локально).
        q = list(tags) + ["order:score"]
        if len(q) < self.MAX_TAGS:
            q.append("-rating:s")
        return self.URL, {"tags": " ".join(q[:self.MAX_TAGS]), "limit": str(self.page_size),
                          "page": str(page + 1)}          # Moebooru считает страницы с 1

    def normalize(self, post):
        rating = (post.get("rating") or "").lower()
        return slim(post, owner=post.get("author"), rating=self._RATING.get(rating, rating),
                    _site=self.name)

    def post_url(self, post):
        return f"https://konachan.com/post/show/{post.get('id')}"


# ── Страница выдачи (с кэшем) ─────────────────────────────────────────────────
_page_cache: "OrderedDict[tuple, tuple[float, list[dict], int | None]]" = OrderedDict()
_api_limits: WeakKeyDictionary = WeakKeyDictionary()


def _limiter(source: Source) -> asyncio.Semaphore:
    by_source = _api_limits.setdefault(asyncio.get_running_loop(), {})
    return by_source.setdefault(source.name, asyncio.Semaphore(API_CONCURRENCY))


def _validate(raw: list | None, text: str) -> list[dict]:
    if raw is None:
        if "too deep" in (text or "").lower():
            raise SourceError("deep", "Сайт не отдаёт такие глубокие страницы")
        raise SourceError("bad", "Ответ API не является списком постов")
    for post in raw:
        if not isinstance(post, dict):
            raise SourceError("bad", "Ответ API не является списком постов")
        for name in ("tags", "rating", "md5", "hash"):
            if post.get(name) is not None and not isinstance(post[name], str):
                raise SourceError("bad", f"Некорректное поле API: {name}")
        if post.get("id") is None and not (post.get("md5") or post.get("hash")):
            raise SourceError("bad", "Пост API без идентификатора")
    return raw


async def fetch_page(source: Source, tags: list[str], page: int,
                     cursor: Cursor | None = None) -> tuple[list[dict], int | None]:
    """Одна страница выдачи → (нормализованные посты, count-подсказка API)."""
    key = (source.name, tuple(tags), page, cursor)
    hit = _page_cache.get(key)
    if hit and time.monotonic() - hit[0] < PAGE_CACHE_TTL:
        _page_cache.move_to_end(key)
        return hit[1], hit[2]
    url, params = source.request(tags, page, cursor)
    async with _limiter(source):
        text = await fetch_text(url, params)
    raw, count = parse_posts_page(text)
    posts = [source.normalize(p) for p in _validate(raw, text)]
    _page_cache[key] = (time.monotonic(), posts, count)
    _page_cache.move_to_end(key)
    while len(_page_cache) > PAGE_CACHE_MAX:
        _page_cache.popitem(last=False)
    return posts, count


def clear_page_cache() -> None:
    _page_cache.clear()


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
            session = await get_session()
            async with session.get(url, headers=headers, timeout=MEDIA_TIMEOUT, proxy=config.PROXY) as resp:
                if resp.status != 200:
                    logger.warning("[download] HTTP %s: %s", resp.status, url)
                    return None, "error"
                ctype = resp.headers.get("Content-Type", "")
                if ctype.startswith("text/"):   # хотлинк-заглушка вместо картинки
                    logger.warning("[download] вместо файла пришёл %s: %s", ctype, url)
                    return None, "error"
                if resp.content_length and resp.content_length > max_size:
                    return None, "too_big"
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    buf.extend(chunk)
                    if len(buf) > max_size:
                        return None, "too_big"
                return bytes(buf), None
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            logger.warning("[download] %s (попытка %s/2): %s", type(error).__name__, attempt, url)
            if attempt == 1:
                await asyncio.sleep(1.5)
    return None, "error"


async def fetch_media(source: Source, post: dict, max_size: int) -> tuple[Media | None, str]:
    """Оригинал поста, а если не влезает — jpeg/sample-версия.

    → (Media, "") или (None, причина: 'too_big' | 'error' | 'no_file').
    """
    urls = list(dict.fromkeys(
        u for u in (post.get(k) for k in ("file_url", "jpeg_url", "sample_url"))
        if isinstance(u, str) and u.startswith("http")))
    if not urls:
        return None, "no_file"
    reason = "error"
    for i, url in enumerate(urls):
        known = to_int(post.get("file_size")) if i == 0 else None
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
