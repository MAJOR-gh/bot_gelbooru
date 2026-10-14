"""Sanitized /status metrics and bounded, cached API checks (never reveal credentials)."""
import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass

import sources

STARTED = time.monotonic()
HEALTH_TTL = 60.0
PROBE_TIMEOUT = 5.0
_health = {}
_locks = {}
_searches = OrderedDict()


@dataclass(frozen=True)
class Health:
    state: str
    elapsed_ms: float
    checked_at: float


async def probe(source) -> Health:
    if isinstance(source, sources.Gelbooru) and not (source.api_key and source.user_id):
        return Health("not_configured", 0, time.monotonic())
    lock = _locks.setdefault((asyncio.get_running_loop(), source.name), asyncio.Lock())
    async with lock:
        hit = _health.get(source.name)
        if hit and time.monotonic() - hit.checked_at < HEALTH_TTL:
            return hit
        started = time.monotonic()
        state = "ok"
        try:
            async with asyncio.timeout(PROBE_TIMEOUT):
                # Safebooru answers an empty body for id:0; use a known benign tag.
                # Only metadata is fetched, never images.
                url, params = source.request(["scenery"], 0)
                params["limit"] = "1"
                async with sources._limiter(source):
                    text = await sources.fetch_text(url, params, retries=1)
                rows, _count = sources.parse_posts_page(text)
                sources._validate(rows, text)
        except sources.SourceError as error:
            state = error.kind  # raw errors/URLs can contain sensitive connection data
        except TimeoutError:
            state = "timeout"
        except Exception:
            state = "down"
        result = Health(state, (time.monotonic() - started) * 1000, time.monotonic())
        _health[source.name] = result
        return result


def record_search(scope, source, result, elapsed_ms):
    _searches[scope] = {
        "site": source.name, "elapsed_ms": elapsed_ms, "fetched": result.fetched,
        "allowed": result.allowed, "complete": result.complete,
        "proven": result.ranking_proven, "pending": result.priority_pending,
        "errors": tuple(result.errors), "at": time.monotonic(),
    }
    _searches.move_to_end(scope)
    while len(_searches) > 128:
        _searches.popitem(last=False)


def last_search(scope):
    return _searches.get(scope)


def health_text(health: Health) -> str:
    labels = {"ok": "✅ API отвечает", "not_configured": "⚙️ Ключ Gelbooru не настроен",
              "auth": "🚫 Доступ отклонён (ключ/защита сайта)", "rate": "⏳ Лимит запросов",
              "timeout": "⏳ Таймаут", "bad": "⚠️ Некорректный ответ API",
              "down": "❌ API не отвечает"}
    label = labels.get(health.state, "⚠️ API недоступен")
    age = max(0, time.monotonic() - health.checked_at)
    return f"{label} · {health.elapsed_ms:.0f} мс · проверено {age:.0f} с назад"