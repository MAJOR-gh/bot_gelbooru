"""Контент-политика бота: что показываем, что режем, что считаем «наготой».

Все списки тегов живут здесь — блэклисты правятся в одном месте.
Чистые функции без сети и Discord, покрыты test_bot.py.
"""

# ── Общий блэклист (точное совпадение тега поста) ──────────────────────────────
BLACKLIST_TAGS = [
    "loli", "shota", "underage", "young", "child", "aged_down",
    "gore", "blood", "snuff", "rape", "abuse", "vore",
    "torture", "mutilation",
    "tentacles", "bestiality", "zoophilia", "inflation",
    "piss", "pee", "peeing", "urine",
    "fart", "toilet", "diaper", "pregnancy", "pregnant", "birth", "group_sex",
    "furry", "anthro", "animal", "dog", "cat", "horse", "fox",
    "the_simpsons", "bart_simpson", "homer_simpson",
    "pokemon", "pikachu", "my_little_pony", "mlp", "steven_universe",
    "family_guy", "south_park", "rugrats", "disney", "cartoon",
    "3d",
    "futa", "futanari", "trap", "crossdressing", "femboy",
    "netorare", "ntr", "cheating", "cuckold",
    "mindbreak", "mind_control",
    "ryona", "bdsm", "bondage", "gag", "dildo",
    "penetration", "anal_object_insertion", "anal_fingering", "anal_fisting",
    "foot_focus", "goblin",
    "ai_generated",
    "armpit_hair", "pubic_hair", "body_hair", "chest_hair", "leg_hair", "hairy",
    "smegma",
    "ugly", "ugly_man", "ugly_bastard", "old_man",
    "fat", "obese", "overweight",
    "stubble",
    "vomit", "puke", "crying",
    "forced",
    "huge_belly", "saggy_breasts", "wrinkles",
    "bad_anatomy", "bad_hands", "bad_feet", "bad_face",
    "cuntboy", "gay", "lesbian", "dark-skinned_male",
    "male/male",
    "orc",
    # ── низкое качество / трешак (локальный отсев, не навязывает типаж) ──
    "lowres", "sketch", "wip", "unfinished", "jpeg_artifacts",
    "bad_proportions", "poorly_drawn", "scan", "what",
    "old_woman", "granny",
]
BLACKLIST_SET = frozenset(BLACKLIST_TAGS)

# ── КРИТИЧЕСКИЙ блэклист (возраст) — ловим подстрокой, НЕотключаем ────────────
CRITICAL_TAGS = [
    "loli", "shota", "lolicon", "shotacon", "toddlercon",
    "underage", "child", "aged_down", "young",
]

# ── AI / нейроарты — ловим подстрокой ─────────────────────────────────────────
# Основной тег Gelbooru пишется через ДЕФИС ("ai-generated").
AI_TAGS = ["ai-generated", "ai-created", "ai-assisted", "ai_generated"]
AI_SUBSTRINGS = ("ai-generated", "ai_generated", "ai-created", "ai_art",
                 "stable_diffusion", "novelai", "nai_diffusion", "midjourney",
                 "dall-e", "dalle")

# ── HARD-блок: яой / мужской контент / фембои / бондаж / БДСМ ──────────────────
# Строго запрещено и НЕотключаемо, даже если юзер укажет такой тег явно.
HARD_TAGS = [
    # яой / мужской контент
    "yaoi", "bara", "gay", "male_only", "multiple_boys", "2boys", "3boys",
    "male/male", "boy_on_top", "male_focus", "1boy", "cum_on_male",
    # фембои / трапы / переодевание
    "femboy", "trap", "crossdressing", "otoko_no_ko", "cuntboy", "tomgirl",
    "astolfo_(fate)", "astolfo", "felix_argyle",
    # футанари
    "futanari", "futa", "futa_on_male", "newhalf", "dickgirl",
    # бондаж / БДСМ / насилие в кадре
    "bdsm", "bondage", "shibari", "rope_bondage", "gag", "ball_gag",
    "ring_gag", "tape_gag", "collar", "leash", "chained", "shackles",
    "spanking", "whip", "flogger", "torture", "ryona",
    # страпон / пеггинг
    "strap-on", "strapon",
]
HARD_SET = frozenset(HARD_TAGS)
# Вариации (yaoi_*, *_bondage, ball_gag…). Одиночное слово сравнивается с
# каждым словом тега ТОЧНО (futa ≠ futaba_sakura), фраза из нескольких слов —
# подстрокой.
HARD_SUBSTRINGS = (
    "yaoi", "bara", "femboy", "futanari", "futa", "crossdress",
    "otoko_no_ko", "cuntboy", "bondage", "bdsm", "shibari", "_gag", "gag_",
    "ball_gag", "ring_gag", "leash", "shackle", "ryona",
    "strap-on", "strapon",
)


def _norm(tag: str) -> str:
    """Единый вид тега: нижний регистр, дефис = подчёркивание."""
    return tag.lower().replace("-", "_")


_HARD_PARTS = [p for p in (_norm(h).strip("_") for h in HARD_SUBSTRINGS) if p]
_HARD_WORDS = frozenset(p for p in _HARD_PARTS if "_" not in p)
_HARD_PHRASES = tuple(p for p in _HARD_PARTS if "_" in p)

# Отрицательные теги, которые уходят в КАЖДЫЙ API-запрос (короткие списки —
# URL не раздувается до HTTP 413). Полный BLACKLIST режется только локально.
SERVER_EXCLUDES: list[str] = list(dict.fromkeys(
    f"-{t}" for t in CRITICAL_TAGS + AI_TAGS + HARD_TAGS
))


def _is_hard_variant(tag_norm: str) -> bool:
    if any(p in tag_norm for p in _HARD_PHRASES):
        return True
    return not _HARD_WORDS.isdisjoint(tag_norm.split("_"))


# ── Нагота ────────────────────────────────────────────────────────────────────
# «Строгий» пост должен содержать хотя бы один из этих тегов (полная или
# частичная нагота) — так отсекаются полностью одетые арты.
NUDITY_TAGS = frozenset({
    "nude", "completely_nude", "topless", "bottomless", "naked",
    "nipples", "breasts_out", "no_bra", "no_panties", "pussy",
    "uncensored", "exposed_breasts", "bare_breasts", "areola_slip",
    "nipple_slip", "covered_nipples", "clothing_aside", "bottomless_female",
    "open_clothes", "undressing", "partially_undressed", "clothes_lift",
    "skirt_lift", "shirt_lift", "bra", "panties", "lingerie", "underwear",
    "see-through", "wardrobe_malfunction", "cum", "sex", "vaginal", "anal",
    "fellatio", "paizuri", "cameltoe", "ass", "thong",
})
# Явная «полная одежда» — такие посты отбрасываем всегда.
DRESSED_TAGS = frozenset({"fully_clothed", "fully_dressed", "dressed"})

# ── Рейтинги ──────────────────────────────────────────────────────────────────
NSFW_RATINGS = frozenset({"sensitive", "questionable", "explicit"})
SAFE_RATINGS = frozenset({"general", "safe", "sensitive"})


# ── Фокус по части тела: приоритет запрошенного ───────────────────────────────
# Юзер ищет breasts, но в топе по score часто арт, где сюжет — ass/pussy, а тег
# breasts лишь присутствует. Поднимаем посты, где сюжет = запрошенная часть, и
# опускаем те, где сюжет — часть, которую НЕ просили.

# «Конкурирующие» телесные nudity-теги: голая жопа/писька сама по себе НЕ
# засчитывает наготу для запроса breasts.
_BODYPART_NUDITY = frozenset({"pussy", "cameltoe", "ass", "thong", "anus"})
GENERAL_NUDITY = NUDITY_TAGS - _BODYPART_NUDITY

FOCUS_GROUPS: dict[str, dict[str, frozenset]] = {
    "breasts": {
        # тег юзера → активирует эту группу как «запрошенную»
        "triggers": frozenset({"breasts", "large_breasts", "huge_breasts", "gigantic_breasts",
                               "medium_breasts", "small_breasts", "cleavage", "underboob",
                               "sideboob", "paizuri", "oppai", "boobs"}),
        # сюжет поста = грудь (emphasis / close-up / захват)
        "focus": frozenset({"large_breasts", "huge_breasts", "gigantic_breasts", "medium_breasts",
                            "cleavage", "underboob", "sideboob", "paizuri", "breasts_out",
                            "between_breasts", "breast_focus", "cleavage_cutout", "breast_grab",
                            "breast_press", "breast_hold", "nipples", "bare_breasts",
                            "exposed_breasts", "oppai"}),
        # засчитывается как нагота при запросе груди
        "nudity": frozenset({"nipples", "breasts_out", "bare_breasts", "exposed_breasts",
                             "no_bra", "paizuri", "areola_slip", "nipple_slip"}),
    },
    "ass": {
        "triggers": frozenset({"ass", "huge_ass", "big_ass", "large_ass", "ass_focus",
                               "anus", "butt", "booty"}),
        "focus": frozenset({"huge_ass", "big_ass", "large_ass", "ass_focus", "from_behind",
                            "bent_over", "spread_ass", "top-down_bottom-up", "backboob",
                            "ass_visible_through_thighs", "anus", "ass_grab"}),
        "nudity": frozenset({"ass", "anus", "thong"}),
    },
    "pussy": {
        "triggers": frozenset({"pussy", "vagina", "vaginal", "spread_pussy", "clitoris",
                               "cameltoe"}),
        "focus": frozenset({"spread_pussy", "pussy_focus", "clitoris", "pussy_juice",
                            "cameltoe", "female_ejaculation", "after_vaginal", "gaping"}),
        "nudity": frozenset({"pussy", "cameltoe"}),
    },
    "feet": {
        "triggers": frozenset({"feet", "foot", "feet_focus", "foot_focus", "soles", "toes",
                               "footjob", "barefoot"}),
        "focus": frozenset({"feet_focus", "foot_focus", "soles", "toes", "footjob",
                            "foot_worship"}),
        "nudity": frozenset(),
    },
}
_TAG_TO_GROUP = {t: g for g, d in FOCUS_GROUPS.items() for t in d["triggers"]}
_ALL_GROUPS = frozenset(FOCUS_GROUPS)
TOP_TIER = 3


def post_tags(post: dict) -> set[str]:
    return {t.lower() for t in (post.get("tags") or "").split()}


def focus_groups_for(tags: list[str]) -> set[str]:
    """Какие телесные группы запросил юзер (по совпадению тега с triggers)."""
    return {_TAG_TO_GROUP[t] for t in (x.lower() for x in tags) if t in _TAG_TO_GROUP}


def requested_nudity_tags(groups: set[str]) -> frozenset | None:
    """Теги, засчитываемые как «нагота» под конкретный запрос.

    Ищешь часть тела → нагота = общая нагота или нагота ИМЕННО этой части.
    Без телесного запроса → None (полный NUDITY_TAGS).
    """
    if not groups:
        return None
    nud = set(GENERAL_NUDITY)
    for g in groups:
        nud |= FOCUS_GROUPS[g]["nudity"]
    return frozenset(nud)


def focus_tier(post: dict, groups: set[str] | None) -> int:
    """Релевантность поста запрошенной части тела (больше = выше в выдаче).

      3 — сюжет = запрошенная часть (и только она);
      2 — запрошенная часть в фокусе, но в кадре и другая;
      1 — нейтрально (явного фокуса ни на чём нет);
      0 — сюжет = часть, которую НЕ просили. Без телесного запроса — 0 у всех.
    """
    if not groups:
        return 0
    low = post_tags(post)
    on = any(low & FOCUS_GROUPS[g]["focus"] for g in groups)
    off = any(low & FOCUS_GROUPS[g]["focus"] for g in (_ALL_GROUPS - groups))
    if on:
        return 3 if not off else 2
    return 1 if not off else 0


def post_is_allowed(post: dict, allowed_ratings: frozenset | None = None) -> bool:
    """Пост не нарушает ни одного запрета (нагота здесь НЕ проверяется).

      1. Рейтинг из списка источника (safe-команда не пропустит questionable).
      2. HARD-блок (яой/фембои/бондаж/БДСМ/фута) — точные теги и вариации.
      3. Возраст и AI — подстрокой, любые вариации.
      4. Общий блэклист — точное совпадение.
      5. Явно одетые (fully_clothed и т.п.).
    """
    if allowed_ratings is not None:
        if (post.get("rating") or "").lower() not in allowed_ratings:
            return False
    low = post_tags(post)
    if not low.isdisjoint(HARD_SET):
        return False
    for tag in low:
        n = _norm(tag)
        if _is_hard_variant(n):
            return False
        if any(c in tag for c in CRITICAL_TAGS):
            return False
        if any(a in tag for a in AI_SUBSTRINGS):
            return False
    if not low.isdisjoint(BLACKLIST_SET):
        return False
    if not low.isdisjoint(DRESSED_TAGS):
        return False
    return True


def has_nudity(post: dict, nudity_tags: frozenset | None = None) -> bool:
    nud = nudity_tags if nudity_tags is not None else NUDITY_TAGS
    return not post_tags(post).isdisjoint(nud)


def post_is_clean(post: dict, require_nudity: bool = True,
                  nudity_tags: frozenset | None = None,
                  allowed_ratings: frozenset | None = None) -> bool:
    """Пост допустим к показу: без запретов и (если нужно) с наготой."""
    if not post_is_allowed(post, allowed_ratings):
        return False
    return not require_nudity or has_nudity(post, nudity_tags)


# ── Пользовательские теги ─────────────────────────────────────────────────────
BLOCKED_USER_TAGS = frozenset([
    "futa", "futanari", "femboy", "trap", "crossdressing", "yaoi", "bara",
    "gay", "bdsm", "bondage", "shibari", "otoko_no_ko", "cuntboy", "ryona",
    "astolfo", "felix",
])


def clean_user_tags(raw: tuple | list) -> list[str]:
    """Сырые поля команды → теги: обрезка, пробелы → «_», нижний регистр, без дублей."""
    out: list[str] = []
    for t in raw:
        if not t or not t.strip():
            continue
        tag = "_".join(t.strip().lower().split())
        if tag not in out:
            out.append(tag)
    return out


def is_exclusion(tag: str) -> bool:
    """«-тег» только сужает выдачу — блокировки и приколы на него не срабатывают."""
    return tag.startswith("-")


def tag_is_blocked(tag: str) -> bool:
    """True, если тег пользователя запрещён — такой контент не показываем вообще."""
    if is_exclusion(tag):
        return False
    low = tag.lower()
    n = _norm(low)
    words = set(n.split("_"))
    if low in BLOCKED_USER_TAGS or not words.isdisjoint(BLOCKED_USER_TAGS):
        return True
    # Общий блэклист и HARD — точное совпадение (без деления на слова, чтобы не
    # ловить cat_ears/animal_ears). Иначе запрос «collar» уходил бы к API вместе
    # с «-collar» и молча возвращал пустоту.
    if low in BLACKLIST_SET or low in HARD_SET:
        return True
    if any(c in low for c in CRITICAL_TAGS):
        return True
    if any(a in low for a in AI_SUBSTRINGS):
        return True
    return _is_hard_variant(n)


def _tag_matches(tag: str, names: frozenset) -> bool:
    low = tag.lower()
    return low in names or not names.isdisjoint(low.split("_"))


# «Приколдес»-теги: вместо поиска — грозное предупреждение / картинка.
LGBT_JOKE_TAGS = frozenset(["felix", "astolfo"])
VENTI_TAGS = frozenset(["venti"])
KANZAKI_HIDERI_TAGS = frozenset(["kanzaki_hideri", "kanzaki", "hideri"])


def tag_triggers_lgbt_joke(tag: str) -> bool:
    return not is_exclusion(tag) and _tag_matches(tag, LGBT_JOKE_TAGS)


def tag_triggers_venti(tag: str) -> bool:
    return not is_exclusion(tag) and _tag_matches(tag, VENTI_TAGS)


def tag_is_kanzaki_hideri(tag: str) -> bool:
    if is_exclusion(tag):
        return False
    low = tag.lower()
    return low in KANZAKI_HIDERI_TAGS or ("kanzaki" in low and "hideri" in low)
