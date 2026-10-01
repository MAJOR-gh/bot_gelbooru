"""Выбор арта: память показанного (анти-повтор) и порядок кандидатов.

Принцип (как в Lawliet): показываем ЛУЧШИЙ ещё не показанный арт.
Порядок важности: раздетый > одетый, фокус на запрошенной части тела, score.
Когда по запросу показано всё — сначала то, что показывали давнее всего.
"""
import asyncio
import json
import logging
import os
from collections import deque

from content_filter import focus_tier

logger = logging.getLogger("gelbooru_bot.selection")

RECENT_MAX = 500        # сколько последних артов помним на каждый запрос
RECENT_MAX_KEYS = 500   # сколько разных запросов держим (старые вытесняются)


def post_score(post: dict) -> int:
    try:
        return int(post.get("score") or 0)
    except (TypeError, ValueError):
        return 0


def post_uid(post: dict) -> str:
    """Стабильный id арта: общий _uid (альбом TG), иначе md5, иначе сайт+id."""
    uid = post.get("_uid")
    if uid:
        return uid
    md5 = (post.get("md5") or "").lower()
    return md5 or f"{post.get('_site')}:{post.get('id')}"


def recent_key(label: str, tags: list[str]) -> str:
    """Ключ памяти: источник + набор тегов (порядок тегов не важен)."""
    return label + "|" + ",".join(sorted(tags))


def order_candidates(strict: list[dict], recent, groups: set[str] | None = None,
                     broad: list[dict] = ()) -> list[dict]:
    """Финальный порядок кандидатов к показу.

    strict — посты, прошедшие строгий фильтр (с наготой), broad — чистые, но
    одетые (запасной вариант). Свежие (не из recent) идут первыми: сначала все
    strict, потом broad, внутри — по фокусу и score. Если свежих нет —
    давно показанные раньше недавних, чтобы не повторять только что отправленное.
    """
    seen = set(recent)

    def rank(p: dict) -> tuple[int, int]:
        return focus_tier(p, groups), post_score(p)

    def fresh(posts) -> list[dict]:
        return sorted((p for p in posts if post_uid(p) not in seen), key=rank, reverse=True)

    out = fresh(strict) + fresh(broad)
    if out:
        return out
    age = {uid: i for i, uid in enumerate(recent)}   # меньше = показывали давнее
    strict_ids = {id(p) for p in strict}
    pool = list(strict) + list(broad)
    return sorted(pool, key=lambda p: (age.get(post_uid(p), -1),
                                       id(p) not in strict_ids,
                                       -post_score(p)))


class ShownMemory:
    """Что уже показывали по каждому запросу. Переживает рестарт (JSON на диске).

    Запись на диск — отложенная (раз в autosave-интервал и при выключении), а не
    на каждый показ: файл бывает в мегабайты, синхронная запись тормозила бота.
    """

    def __init__(self, path: str):
        self.path = path
        self._data: dict[str, deque] = {}
        self._dirty = False

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Память показанных артов не прочитана ({e}) — начинаю с нуля")
            return
        if not isinstance(data, dict):
            return
        for key, uids in data.items():
            if isinstance(uids, list):
                self._data[key] = deque((str(u) for u in uids[-RECENT_MAX:]), maxlen=RECENT_MAX)
        self._trim()
        logger.info(f"🗂 Память показанных артов загружена: {len(self._data)} запросов")

    def recent(self, key: str) -> deque:
        return self._data.get(key) or deque()

    def remember(self, key: str, uid: str) -> None:
        dq = self._data.pop(key, None) or deque(maxlen=RECENT_MAX)
        if uid in dq:          # повтор при исчерпании — переносим в «свежие»
            dq.remove(uid)
        dq.append(uid)
        self._data[key] = dq   # в конец словаря = недавно использованный (LRU)
        self._trim()
        self._dirty = True

    def _trim(self) -> None:
        while len(self._data) > RECENT_MAX_KEYS:
            self._data.pop(next(iter(self._data)))

    def save(self) -> None:
        """Атомарная запись на диск (если есть что сохранять)."""
        if not self._dirty:
            return
        snapshot = {k: list(v) for k, v in self._data.items() if v}
        self._dirty = False
        try:
            self._write(snapshot)
        except OSError as e:
            self._dirty = True
            logger.warning(f"Не удалось сохранить память показанных артов: {e}")

    def _write(self, snapshot: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    async def autosave_loop(self, interval: float = 30.0) -> None:
        while True:
            await asyncio.sleep(interval)
            if not self._dirty:
                continue
            snapshot = {k: list(v) for k, v in self._data.items() if v}
            self._dirty = False
            try:
                await asyncio.to_thread(self._write, snapshot)
            except OSError as e:
                self._dirty = True
                logger.warning(f"Не удалось сохранить память показанных артов: {e}")
