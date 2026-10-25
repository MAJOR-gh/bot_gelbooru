"""Подсказки тегов: локальный список, исправление опечаток, короткий бюджет на API."""
import asyncio
import difflib
import re
import time
from collections import OrderedDict

import content_filter as cf
import sources

COMMON_TAGS = (
    "1girl", "solo", "2girls", "multiple_girls", "breasts", "ass", "nude",
    "censored", "uncensored", "bikini", "school_uniform", "cat_ears", "animal_ears",
    "maid", "twintails", "blonde_hair", "blue_hair", "brown_hair", "pink_hair",
    "purple_hair", "red_hair", "white_hair", "grey_hair", "long_hair", "short_hair",
    "abs", "looking_at_viewer", "smile", "blush", "outdoors", "indoors", "bed",
    "forest", "city", "beach", "swimsuit", "lingerie", "underwear", "thighhighs",
    "pantyhose", "socks", "gloves", "hat", "hairband", "ribbon", "bow", "glasses",
    "comic", "scenery", "landscape", "sky", "cloud", "sunset", "mountain", "flower",
    "megumin", "hatsune_miku",
)
# Вне NSFW-каналов API не спрашиваем: сами названия тегов бывают NSFW.
SAFE_TAGS = (
    "solo", "cat_ears", "maid", "twintails", "blonde_hair", "blue_hair", "brown_hair",
    "pink_hair", "purple_hair", "red_hair", "white_hair", "long_hair", "short_hair",
    "looking_at_viewer", "smile", "blush", "outdoors", "indoors", "forest", "city",
    "beach", "gloves", "hat", "ribbon", "bow", "glasses", "scenery", "landscape",
    "sky", "cloud", "sunset", "mountain", "flower",
)
_cache = OrderedDict()
CACHE_TTL = 600.0
CACHE_MAX = 128
API_BUDGET = 1.0


def normalize(value: str) -> tuple[str, str]:
    cleaned = cf.clean_user_tags([value])
    value = cleaned[0] if cleaned else ""
    return ("-" if value.startswith("-") else "", value.lstrip("-"))


def permitted(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9_()\-]+", name)) and len(name) <= 99 and not (
        cf.tag_is_blocked(name) or cf.tag_triggers_venti(name)
        or cf.tag_triggers_lgbt_joke(name) or cf.tag_is_kanzaki_hideri(name))


def ranked(names, part: str, limit: int):
    unique = {n for n in names if isinstance(n, str) and permitted(n)}

    def key(name):
        return (not name.startswith(part), part not in name,
                -difflib.SequenceMatcher(None, part, name).ratio(), name)
    matches = [n for n in unique if not part or part in n
               or difflib.SequenceMatcher(None, part, n).ratio() >= .55]
    return sorted(matches, key=key)[:limit]


async def suggestions(source, value: str, *, nsfw: bool, limit: int = 25) -> list[str]:
    prefix, part = normalize(value)
    if len(part) > 99 or (part and not re.fullmatch(r"[a-z0-9_()\-]+", part)):
        return []
    local = ranked(COMMON_TAGS if nsfw else SAFE_TAGS, part, limit)
    key = (source.name, part, nsfw)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        _cache.move_to_end(key)
        return [prefix + n for n in hit[1][:limit]]
    names = list(local)
    # API — только с двух букв и только там, где у сайта есть ключи/доступ.
    remote = getattr(source, "similar_tags", None)
    if nsfw and len(part) >= 2 and callable(remote):
        if not isinstance(source, sources.Gelbooru) or (source.api_key and source.user_id):
            try:
                async with asyncio.timeout(API_BUDGET):
                    rows = await remote(part, 25, retries=1)
                names.extend(t.get("name") for t in rows if isinstance(t, dict)
                             and sources.to_int(t.get("count")) and str(t.get("type")) != "6")
            except (sources.SourceError, TimeoutError):
                pass  # сбой подсказок не должен ломать саму команду
    result = ranked(names, part, 25)
    _cache[key] = (time.monotonic(), result)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)
    return [prefix + n for n in result[:limit]]