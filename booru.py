"""Booru-источники (Gelbooru, Konachan, Safebooru): HTTP, разбор ответов, поиск.

Возвращаем лучшие новые результаты из просмотренной части, затем постепенно
обходим следующие страницы без общего потолка. Индекс и курсор переживают
перезапуск. Ошибки API никогда не подменяются возвратом показанного.
"""
import asyncio
import json
import hashlib
import logging
import os
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import aiohttp

import content_filter as cf
from selection import post_uid, post_score
from search_index import SearchIndex

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
            if "post" not in data and count is None:
                return None, count
            post = data.get("post", [])
            if isinstance(post, dict):           # один пост приходит словарём
                post = [post]
            return (post if isinstance(post, list) else []), count
        return (data if isinstance(data, list) else None), None
    try:
        root = ET.fromstring(text)
        if root.tag != "posts":
            return None, None
        return [p.attrib for p in root.iter("post")], _to_int(root.attrib.get("count"))
    except ET.ParseError:
        pass
    return None, None


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
_api_limits = WeakKeyDictionary()


def api_limiter(source: Source) -> asyncio.Semaphore:
    by_source = _api_limits.setdefault(asyncio.get_running_loop(), {})
    return by_source.setdefault(source.name, asyncio.Semaphore(3))


async def fetch_page(source: Source, tags: list[str], page: int) -> tuple[list[dict], int | None]:
    key = (source.name, tuple(tags), page)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        _cache.move_to_end(key)
        return hit[1], hit[2]
    url, params = source.request(tags, page)
    async with api_limiter(source):
        raw, count = parse_posts_page(await fetch_text(url, params))
    if raw is None or any(not isinstance(p, dict) for p in raw):
        raise SourceError("bad", "Ответ API не является списком постов")
    for post in raw:
        for name in ("tags", "rating", "md5", "hash"):
            if post.get(name) is not None and not isinstance(post[name], str):
                raise SourceError("bad", f"Некорректное поле API: {name}")
        if post.get("id") is None and not (post.get("md5") or post.get("hash")):
            raise SourceError("bad", "Пост API без идентификатора")
    posts = [source.normalize(p) for p in raw]
    _cache[key] = (time.monotonic(), posts, count)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)
    return posts, count


# ── Полный, возобновляемый обход выдачи ───────────────────────────────────────
# Нет потолка страниц/постов. Временной бюджет ограничивает ОДИН вызов,
# а не область поиска: незавершённый обход продолжается следующим запросом.
SCAN_CONCURRENCY = 3
_requested_timeout = float(os.environ.get("SEARCH_TIMEOUT_SECONDS", "20"))
if not 0 < _requested_timeout <= 600:
    raise ValueError("SEARCH_TIMEOUT_SECONDS должен быть положительным числом до 600")
SCAN_TIMEOUT = min(_requested_timeout, 30.0)  # old env=120 must not restore long waits
PAGES_PER_REQUEST = 6  # per invocation, NOT a maximum search depth
NEED_FRESH = 5
with open(cf.__file__, "rb") as _policy_file:
    POLICY_HASH = hashlib.sha256(_policy_file.read()).hexdigest()
SCAN_CACHE_MAX = 4


@dataclass
class SearchResult:
    candidates: Sequence[dict]
    errors: list[str] = field(default_factory=list)
    fetched: int = 0
    total: int | None = None
    complete: bool = False
    allowed: int = 0
    stop_reason: str | None = None


@dataclass
class ScanState:
    index: SearchIndex = field(default_factory=SearchIndex)
    allowed: int = 0
    next_page: int = 0
    page_size: int = 100
    total: int | None = None
    fetched: int = 0
    complete: bool = False
    updated: float = field(default_factory=time.monotonic)
    stop_reason: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def save(self, page=None, signature=None):
        self.updated = time.monotonic()
        self.index.save_progress({"next_page": self.next_page, "page_size": self.page_size,
                                  "total": self.total, "fetched": self.fetched,
                                  "complete": self.complete, "stop_reason": self.stop_reason,
                                  "saved_at": time.time()}, page, signature)

    @classmethod
    def restore(cls, key, page_size):
        index = SearchIndex(cache_key=key)
        saved = index.progress()
        state = cls(index=index, page_size=page_size, allowed=index.allowed_count())
        for field_name in ('next_page', 'page_size', 'total', 'fetched', 'complete', 'stop_reason'):
            if field_name in saved:
                setattr(state, field_name, saved[field_name])
        state.updated -= max(0, time.time() - saved.get('saved_at', time.time()))
        return state


_scans: "OrderedDict[tuple, ScanState]" = OrderedDict()


def clear_search_cache() -> None:
    _cache.clear()
    _scans.clear()


async def _scan_all(source: Source, tags: list[str], state: ScanState,
                    require_nudity: bool, recent=(), exhaustive=False) -> None:
    groups = cf.focus_groups_for(tags)
    nudity = cf.requested_nudity_tags(groups)
    async with state.lock:
        read = 0
        while not state.complete:
            if not exhaustive:
                candidates = state.index.candidates(recent)
                first = candidates[:NEED_FRESH]
                if len(first) >= NEED_FRESH and (not require_nudity or all(
                        cf.has_nudity(p, nudity) for p in first)):
                    return
                if read >= PAGES_PER_REQUEST:
                    return
            remaining = SCAN_CONCURRENCY if exhaustive else min(SCAN_CONCURRENCY, PAGES_PER_REQUEST - read)
            pages = [0] if state.next_page == 0 else list(range(state.next_page, state.next_page + remaining))
            results = await asyncio.gather(*(fetch_page(source, tags, n) for n in pages),
                                           return_exceptions=True)
            for page, result in zip(pages, results):
                if isinstance(result, BaseException):
                    if isinstance(result, asyncio.CancelledError):
                        raise result
                    error = result if isinstance(result, SourceError) else SourceError('down', type(result).__name__)
                    logger.warning('[%s] page=%s next_page=%s fetched=%s total_hint=%s: %s',
                                   source.name, page, state.next_page, state.fetched, state.total, error)
                    raise error
                posts, count = result
                if page == 0:
                    state.total = count if count is not None and count >= 0 else None
                    if posts and state.total is not None and state.total > len(posts):
                        state.page_size = min(source.page_size, len(posts))
                if not posts:
                    # count is only a hint. Empty pages are a valid API response.
                    state.complete = True
                    state.stop_reason = ('api_end_before_count' if state.total is not None
                                         and state.fetched < state.total else 'api_end')
                    state.save()
                    logger.info('[%s] pagination ended: page=%s fetched=%s total_hint=%s reason=%s',
                                source.name, page, state.fetched, state.total, state.stop_reason)
                    return
                ids = sorted(str(p.get('id') or post_uid(p)) for p in posts)
                signature = hashlib.sha256("\0".join(ids).encode()).hexdigest()
                if state.index.repeated_page(page, signature):
                    state.complete = True
                    state.stop_reason = 'repeated_page'
                    state.save()
                    logger.warning('[%s] API repeats page %s; cached NEW candidates remain usable', source.name, page)
                    return
                records = []
                for offset, post in enumerate(posts):
                    site_id = f"{post.get('_site')}:{post['id']}" if post.get('id') is not None else ''
                    allowed = int(cf.post_is_allowed(post, source.ratings))
                    strict = int(not require_nudity or cf.has_nudity(post, nudity))
                    records.append((post_uid(post), site_id, json.dumps(post), allowed, strict,
                                    cf.focus_tier(post, groups), post_score(post), state.fetched + offset))
                state.allowed += state.index.add(records)
                state.fetched += len(posts)
                state.next_page = page + 1
                state.save(page, signature)
                read += 1



async def search(source: Source, tags: list[str], recent, *,
                 require_nudity: bool = True, exhaustive: bool = False) -> SearchResult:
    """Fast response from NEW indexed posts, progressively traversing the API.

    No global page ceiling. No need to index 445k posts before returning one art.
    exhaustive is a diagnostic/test option, not used by Discord commands.
    """
    key = ('3.2', POLICY_HASH, source.name, tuple(sorted(tags)), require_nudity)
    state = _scans.get(key)
    if state is None:
        state = ScanState.restore(key, source.page_size)
        _scans[key] = state
    if state.complete and not state.lock.locked() and time.monotonic() - state.updated >= CACHE_TTL:
        state.index.reset_progress()
        state.next_page, state.fetched, state.total = 0, 0, None
        state.complete, state.stop_reason = False, None
        state.save()
    _scans.move_to_end(key)
    for old_key in list(_scans):
        if len(_scans) <= SCAN_CACHE_MAX:
            break
        if old_key != key and not _scans[old_key].lock.locked():
            del _scans[old_key]
    errors = []
    try:
        async with asyncio.timeout(SCAN_TIMEOUT):
            await _scan_all(source, tags, state, require_nudity, recent, exhaustive)
    except TimeoutError:
        errors = ['scan_pending']
    except SourceError as error:
        errors = [error.kind]
    candidates = state.index.candidates(recent)
    if exhaustive and state.stop_reason in {'api_end_before_count', 'repeated_page'}:
        errors = ['incomplete']
    # Network/count failures must not suppress already indexed, genuinely NEW art.
    # This is NEVER a replay fallback. Auth/rate denials still return explicitly.
    if candidates and not exhaustive and not set(errors) & {'auth', 'rate'}:
        errors = []
    elif exhaustive and errors:
        candidates = []
    elif errors:
        candidates = []
    if not candidates and not errors and not state.complete:
        errors = ['scan_pending']
    return SearchResult(candidates, errors, state.fetched, state.total,
                        state.complete and state.stop_reason == 'api_end',
                        state.allowed, state.stop_reason)


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
