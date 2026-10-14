"""Обход ВСЕЙ выдачи сайта по запросу и выбор лучших непоказанных артов.

Как это работает:
  • Сайт отдаёт выдачу по убыванию score. Читаем страницы по порядку и кладём
    посты в дисковый индекс (search_index.py) с готовыми ключами ранжирования.
  • Ранжирование: strict (есть нагота) → tier (фокус запрошенной части тела) → score.
  • Ранняя остановка только с ДОКАЗАТЕЛЬСТВОМ: у непрочитанных постов score не выше
    последнего прочитанного (frontier). Если есть NEED_FRESH непоказанных постов
    strict=1, с максимальным tier и score ≥ frontier — лучше них в выдаче нет.
    Иначе читаем дальше (до PAGES_PER_REQUEST за вызов, курсор сохраняется).
  • Потолок пагинации (Gelbooru: pid*limit ≤ 20000) обходится курсором:
    все посты с score = frontier листаются по `id:<…`, затем `score:<frontier`.
    Так доступна вся выдача тега, хоть 5 тысяч, хоть 500 тысяч постов.
  • Ошибки API никогда не подменяются повтором уже показанного.
"""
import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field

import config
import content_filter as cf
from search_index import SearchIndex, prune_stale
from selection import post_score, post_uid
from sources import Cursor, Source, SourceError, fetch_page, clear_page_cache

logger = logging.getLogger("gelbooru_bot.search")

SCAN_CONCURRENCY = 3      # страниц параллельно
PAGES_PER_REQUEST = 6     # страниц за ОДИН вызов, НЕ глубина поиска
NEED_FRESH = 5            # сколько «доказанно лучших» кандидатов достаточно
SCAN_TIMEOUT = config.SEARCH_TIMEOUT
REFRESH_AFTER = 600.0     # законченный обход перечитывается не чаще раза в 10 минут
OPEN_INDEXES = 4          # одновременно открытых индексов в памяти
KEY_VERSION = "3.3"

with open(cf.__file__, "rb") as _policy:
    POLICY_HASH = hashlib.sha256(_policy.read()).hexdigest()


@dataclass
class SearchResult:
    candidates: Sequence[dict]
    errors: list[str] = field(default_factory=list)
    fetched: int = 0
    total: int | None = None
    complete: bool = False
    allowed: int = 0
    stop_reason: str | None = None
    ranking_proven: bool = False
    priority_pending: bool = False


_PERSISTED = ("next_page", "page_size", "total", "fetched", "complete", "stop_reason",
              "cursor", "frontier", "seq")


@dataclass
class ScanState:
    index: SearchIndex
    page_size: int = 100
    next_page: int = 0               # pid внутри текущего сегмента
    cursor: dict | None = None       # Cursor.as_dict() или None (обычные страницы)
    frontier: int | None = None      # score последнего прочитанного поста
    seq: int = 0                     # сколько страниц прочитано (ключ журнала страниц)
    total: int | None = None         # count-подсказка API
    fetched: int = 0                 # уникальных постов прочитано
    allowed: int = 0                 # из них прошли фильтры
    complete: bool = False
    stop_reason: str | None = None
    updated: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    def restore(cls, key, page_size: int) -> "ScanState":
        index = SearchIndex(key)
        state = cls(index=index, page_size=page_size, allowed=index.allowed_count())
        saved = index.progress()
        for name in _PERSISTED:
            if name in saved:
                setattr(state, name, saved[name])
        state.updated -= max(0.0, time.time() - saved.get("saved_at", time.time()))
        return state

    def save(self, signature: str | None = None) -> None:
        self.updated = time.monotonic()
        payload = {name: getattr(self, name) for name in _PERSISTED}
        payload["saved_at"] = time.time()
        self.index.save_progress(payload, self.seq if signature else None, signature)

    def restart(self) -> None:
        self.index.reset_progress()
        self.next_page, self.cursor, self.frontier, self.seq = 0, None, None, 0
        self.fetched, self.total, self.complete, self.stop_reason = 0, None, False, None
        self.save()

    def finish(self, reason: str) -> None:
        self.complete, self.stop_reason = True, reason
        self.save()

    @property
    def in_bucket(self) -> bool:
        return bool(self.cursor) and self.cursor.get("bucket") is not None

    def enter_bucket(self) -> None:
        """Потолок pid: дочитываем всех с score = frontier, дальше `score:<frontier`."""
        self.cursor = Cursor(bucket=self.frontier).as_dict()
        self.next_page = 0
        self.save()


_scans: "OrderedDict[tuple, ScanState]" = OrderedDict()


def clear_search_cache() -> None:
    clear_page_cache()
    _scans.clear()


def _get_state(source: Source, tags: list[str], require_nudity: bool) -> ScanState:
    key = (KEY_VERSION, POLICY_HASH, source.name, tuple(sorted(tags)), require_nudity)
    state = _scans.get(key)
    if state is None:
        if not _scans:
            prune_stale()
        state = _scans[key] = ScanState.restore(key, source.page_size)
    _scans.move_to_end(key)
    for old in list(_scans):
        if len(_scans) <= OPEN_INDEXES:
            break
        if old != key and not _scans[old].lock.locked():
            del _scans[old]
    if (state.complete and not state.lock.locked()
            and time.monotonic() - state.updated >= REFRESH_AFTER):
        state.restart()
    return state


def _next_pages(source: Source, state: ScanState, budget: int) -> list[int]:
    """Какие pid читать следующими (пусто — пора переходить к курсору)."""
    if state.in_bucket or state.next_page == 0:
        return [0] if state.in_bucket else [state.next_page]   # первая страница — одна
    last = state.next_page + budget
    if source.max_offset:
        last = min(last, source.max_offset // max(1, state.page_size))
    return list(range(state.next_page, last))


def _records(posts, state: ScanState, source: Source, groups, nudity, require_nudity):
    for offset, post in enumerate(posts):
        site_id = f"{post.get('_site')}:{post['id']}" if post.get("id") is not None else ""
        yield (post_uid(post), site_id, json.dumps(post),
               int(cf.post_is_allowed(post, source.ratings)),
               int(not require_nudity or cf.has_nudity(post, nudity)),
               cf.focus_tier(post, groups), post_score(post), state.seq * 1000 + offset)


async def _scan(source: Source, tags: list[str], state: ScanState, require_nudity: bool,
                fresh, exhaustive: bool) -> None:
    groups = cf.focus_groups_for(tags)
    nudity = cf.requested_nudity_tags(groups)
    best_tier = cf.TOP_TIER if groups else 0
    async with state.lock:
        read = 0
        while not state.complete:
            if not exhaustive:
                if (state.frontier is not None
                        and fresh.proven_count(best_tier, state.frontier, NEED_FRESH) >= NEED_FRESH):
                    return
                if read >= PAGES_PER_REQUEST:
                    return
            budget = SCAN_CONCURRENCY if exhaustive else min(SCAN_CONCURRENCY, PAGES_PER_REQUEST - read)
            pages = _next_pages(source, state, budget)
            if not pages:
                state.enter_bucket()
                continue
            cursor = Cursor(**state.cursor) if state.cursor else None
            results = await asyncio.gather(
                *(fetch_page(source, tags, n, cursor) if cursor else fetch_page(source, tags, n)
                  for n in pages),
                return_exceptions=True)
            for page, result in zip(pages, results):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, BaseException):
                    error = result if isinstance(result, SourceError) else SourceError("down", type(result).__name__)
                    if (error.kind == "deep" and source.supports_cursor and not state.in_bucket
                            and state.frontier is not None):
                        logger.info("[%s] сайт не отдаёт pid=%s — перехожу на курсор score", source.name, page)
                        state.enter_bucket()
                        break
                    logger.warning("[%s] page=%s cursor=%s fetched=%s total_hint=%s: %s",
                                   source.name, page, state.cursor, state.fetched, state.total, error)
                    raise error
                read += 1
                if _ingest(source, state, page, *result, groups, nudity, require_nudity):
                    break


def _ingest(source, state: ScanState, page: int, posts: list[dict], count: int | None,
            groups, nudity, require_nudity) -> bool:
    """Учесть одну страницу. True — дальше эту пачку страниц не обрабатывать."""
    if page == 0 and not state.cursor:
        state.total = count if count is not None and count >= 0 else None
        if posts and state.total is not None and state.total > len(posts):
            state.page_size = min(source.page_size, len(posts))   # сервер урезал limit
    if not posts:
        if state.in_bucket:          # все посты с этим score прочитаны → ниже по score
            state.cursor = Cursor(below=state.cursor["bucket"]).as_dict()
            state.next_page = 0
            state.save()
            return True
        # count — только подсказка: пустая страница = честный конец выдачи.
        reason = ("api_end_before_count" if state.total is not None and state.fetched < state.total
                  else "api_end")
        state.finish(reason)
        logger.info("[%s] выдача закончилась: cursor=%s fetched=%s total_hint=%s reason=%s",
                    source.name, state.cursor, state.fetched, state.total, reason)
        return True
    ids = sorted(str(p.get("id") or post_uid(p)) for p in posts)
    signature = hashlib.sha256(json.dumps([state.cursor, ids]).encode()).hexdigest()
    if state.index.repeated_page(signature):
        state.finish("repeated_page")
        logger.warning("[%s] API повторяет страницу %s; уже найденные НОВЫЕ посты доступны",
                       source.name, page)
        return True
    new_allowed, new_total = state.index.add(list(_records(posts, state, source, groups,
                                                            nudity, require_nudity)))
    state.allowed += new_allowed
    state.fetched += new_total if state.in_bucket else len(posts)  # в курсоре бывают уже прочитанные
    state.seq += 1
    if state.in_bucket:
        state.cursor = dict(state.cursor, before_id=min(int(p["id"]) for p in posts))
        if len(posts) < state.page_size:     # неполная страница — score-группа кончилась
            state.cursor = Cursor(below=state.cursor["bucket"]).as_dict()
    else:
        low = min(post_score(p) for p in posts)
        state.frontier = low if state.frontier is None else min(state.frontier, low)
        state.next_page = page + 1
    state.save(signature)
    return False


async def search(source: Source, tags: list[str], recent, *,
                 require_nudity: bool = True, exhaustive: bool = False) -> SearchResult:
    """Лучшие НОВЫЕ посты по запросу; обход продолжается с сохранённого курсора.

    exhaustive=True — прочитать всю выдачу (тесты/диагностика, не для Discord).
    """
    state = _get_state(source, tags, require_nudity)
    candidates = state.index.candidates(recent)   # живой запрос: видит и новые страницы
    errors: list[str] = []
    try:
        async with asyncio.timeout(SCAN_TIMEOUT):
            await _scan(source, tags, state, require_nudity, candidates, exhaustive)
    except TimeoutError:
        errors = ["scan_pending"]
    except SourceError as error:
        errors = [error.kind]
    groups = cf.focus_groups_for(tags)
    best_tier = cf.TOP_TIER if groups else 0
    proven = (state.complete and state.stop_reason == "api_end") or (
        state.frontier is not None
        and candidates.proven_count(best_tier, state.frontier, NEED_FRESH) >= NEED_FRESH)
    priority_unproven = bool(groups and not proven and not exhaustive)
    priority_pending = priority_unproven and not state.complete
    if priority_unproven:
        # The page/time budget is never evidence that the priority is optimal.
        candidates = []
        if not errors:
            errors = ["scan_pending" if priority_pending else "incomplete"]
    elif groups and proven and not state.complete and not exhaustive:
        candidates.restrict_to_proven(best_tier, state.frontier)
    if exhaustive:
        if state.stop_reason in {"api_end_before_count", "repeated_page"}:
            errors = ["incomplete"]
        if errors:
            candidates = []
    elif candidates and not {"auth", "rate"} & set(errors):
        errors = []        # уже найденные НОВЫЕ посты важнее сбоя поздней страницы
    elif errors:
        candidates = []
    if not candidates and not errors and not state.complete:
        errors = ["scan_pending"]
    return SearchResult(candidates, errors, state.fetched, state.total,
                        state.complete and state.stop_reason == "api_end",
                        state.allowed, state.stop_reason, proven, priority_pending)
