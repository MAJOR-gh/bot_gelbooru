"""Unit tests for the pure logic of the Gelbooru bot (no Discord connection)."""
import time
import bot_gelbooru as b

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name}")


print("== parse_posts ==")
check("json with post list", b.parse_posts('{"post":[{"id":1},{"id":2}]}') == [{"id": 1}, {"id": 2}])
check("json no post key -> []", b.parse_posts('{"@attributes":{"count":0}}') == [])
check("json single post dict -> wrapped", b.parse_posts('{"post":{"id":7}}') == [{"id": 7}])
check("json top-level list", b.parse_posts('[{"id":1}]') == [{"id": 1}])
check("xml parse", b.parse_posts('<posts><post id="5" file_url="x"/></posts>')[0]["id"] == "5")
check("garbage -> None", b.parse_posts("not json not xml") is None)

print("== post_is_clean ==")
check("clean post", b.post_is_clean({"tags": "1girl blue_hair smile nude"}) is True)
check("clean post without nudity rejected", b.post_is_clean({"tags": "1girl blue_hair smile"}) is False)
check("clean post without nudity allowed when flag off", b.post_is_clean({"tags": "1girl blue_hair smile"}, require_nudity=False) is True)
check("dirty post (loli)", b.post_is_clean({"tags": "1girl loli"}) is False)
check("dirty post (gore)", b.post_is_clean({"tags": "blood gore wound"}) is False)
check("empty tags -> not clean when nudity required", b.post_is_clean({}) is False)
check("empty tags -> clean when nudity not required", b.post_is_clean({}, require_nudity=False) is True)

print("== tag_is_blocked (exact token, no false positives) ==")
check("futa blocked", b.tag_is_blocked("futa") is True)
check("futanari blocked", b.tag_is_blocked("futanari") is True)
check("trap blocked", b.tag_is_blocked("trap") is True)
check("futaba_sakura NOT blocked (was a substring bug)", b.tag_is_blocked("futaba_sakura") is False)
check("strap NOT blocked (was a substring bug)", b.tag_is_blocked("strap") is False)
check("contrast NOT blocked", b.tag_is_blocked("contrast") is False)
check("1girl NOT blocked", b.tag_is_blocked("1girl") is False)

print("== blacklist string built correctly ==")
check("BLACKLIST starts with -loli", b.BLACKLIST.startswith("-loli "))
check("BLACKLIST has -futanari", " -futanari" in b.BLACKLIST)
check("BLACKLIST_SET has loli", "loli" in b.BLACKLIST_SET)
check("BLACKLIST_SET no dashes", all(not t.startswith("-") for t in b.BLACKLIST_SET))

print("== CooldownManager ==")
cd = b.CooldownManager(rate=2, per=1.0)
check("1st call ok", cd.retry_after(1) == 0.0)
check("2nd call ok", cd.retry_after(1) == 0.0)
check("3rd call blocked", cd.retry_after(1) > 0)
check("different user ok", cd.retry_after(2) == 0.0)
time.sleep(1.05)
check("after window resets", cd.retry_after(1) == 0.0)

print("== base_params ==")
b.API_KEY = "K"
b.USER_ID = "U"
p = b.base_params(s="post", tags="1girl")
check("includes api_key/user_id when set", p.get("api_key") == "K" and p.get("user_id") == "U")
check("includes static fields", p["page"] == "dapi" and p["q"] == "index" and p["json"] == "1")
b.API_KEY = ""
b.USER_ID = ""
p2 = b.base_params(s="post")
check("omits api_key when empty", "api_key" not in p2)

print("== focus_groups_for ==")
check("breasts -> {breasts}", b.focus_groups_for(["breasts"]) == {"breasts"})
check("large_breasts -> {breasts}", b.focus_groups_for(["large_breasts"]) == {"breasts"})
check("ass -> {ass}", b.focus_groups_for(["ass"]) == {"ass"})
check("breasts+ass -> both", b.focus_groups_for(["breasts", "ass"]) == {"breasts", "ass"})
check("character -> empty", b.focus_groups_for(["hatsune_miku"]) == set())
check("case-insensitive", b.focus_groups_for(["Breasts"]) == {"breasts"})

print("== requested_nudity_tags ==")
check("no group -> None (legacy full set)", b.requested_nudity_tags(set()) is None)
nb = b.requested_nudity_tags({"breasts"})
check("breasts: nipples counts", "nipples" in nb)
check("breasts: general nude counts", "nude" in nb)
check("breasts: bare ass does NOT count", "ass" not in nb)
check("breasts: bare pussy does NOT count", "pussy" not in nb)
na = b.requested_nudity_tags({"ass"})
check("ass: bare ass counts", "ass" in na)
check("ass: bare pussy does NOT count", "pussy" not in na)

print("== _focus_tier (relevance) ==")
G = {"breasts"}
check("on-only -> tier 3", b._focus_tier({"tags": "1girl large_breasts nude"}, G) == 3)
check("on+off -> tier 2", b._focus_tier({"tags": "large_breasts ass_focus"}, G) == 2)
check("neutral -> tier 1", b._focus_tier({"tags": "1girl smile standing"}, G) == 1)
check("off-only -> tier 0", b._focus_tier({"tags": "1girl ass_focus from_behind"}, G) == 0)
check("no group -> tier 0 for all", b._focus_tier({"tags": "large_breasts"}, set()) == 0)

print("== focus_rerank (soft priority, stable within tier) ==")
posts = [
    {"id": "off", "tags": "ass_focus from_behind"},      # tier 0
    {"id": "on", "tags": "huge_breasts cleavage"},       # tier 3
    {"id": "mix", "tags": "large_breasts spread_pussy"}, # tier 2
    {"id": "neu", "tags": "1girl smile"},                # tier 1
]
ranked = [p["id"] for p in b.focus_rerank(posts, {"breasts"})]
check("order on>mix>neu>off", ranked == ["on", "mix", "neu", "off"])
check("off-focus kept (fallback, not dropped)", "off" in ranked)
check("empty group -> unchanged order",
      [p["id"] for p in b.focus_rerank(posts, set())] == ["off", "on", "mix", "neu"])

print("== post_is_clean: nudity tied to focus ==")
breasts_nud = b.requested_nudity_tags({"breasts"})
ass_nud = b.requested_nudity_tags({"ass"})
check("breasts req: bare-ass post rejected",
      b.post_is_clean({"tags": "1girl ass"}, nudity_tags=breasts_nud) is False)
check("breasts req: nipples post passes",
      b.post_is_clean({"tags": "1girl nipples"}, nudity_tags=breasts_nud) is True)
check("ass req: bare-ass post passes",
      b.post_is_clean({"tags": "1girl ass"}, nudity_tags=ass_nud) is True)
check("legacy (no override): bare-ass still passes",
      b.post_is_clean({"tags": "1girl ass"}) is True)

print("== lead_by_score (Lawliet-style best-first) ==")
LB = [
    {"md5": "a", "tags": "1girl", "score": 10},
    {"md5": "b", "tags": "1girl", "score": 99},
    {"md5": "c", "tags": "1girl", "score": 50},
]
check("top score leads", b.lead_by_score(list(LB))[0]["md5"] == "b")
check("keeps every art", len(b.lead_by_score(list(LB))) == 3)
check("short list unchanged", b.lead_by_score(LB[:1]) == LB[:1])
GF = {"breasts"}
LBF = [
    {"md5": "on_lo", "tags": "huge_breasts", "score": 5},     # tier 3, low score
    {"md5": "on_hi", "tags": "large_breasts", "score": 40},   # tier 3, high score
    {"md5": "off_hi", "tags": "ass_focus", "score": 999},     # tier 0, huge score
]
led = b.lead_by_score(b.focus_rerank(LBF, GF), GF)
check("champion = best score within top focus tier", led[0]["md5"] == "on_hi")
check("off-focus mega-score does NOT lead", led[0]["md5"] != "off_hi")

print("== order_candidates (unseen-first, graceful LRS fallback) ==")
from collections import deque as _dq
P = [
    {"md5": "x", "tags": "1girl", "score": 1},
    {"md5": "y", "tags": "1girl", "score": 100},
    {"md5": "z", "tags": "1girl", "score": 50},
]
check("all unseen -> champion first", b.order_candidates(list(P), _dq())[0]["md5"] == "y")
oc = b.order_candidates(list(P), _dq(["y"]))
check("seen art excluded", all(p["md5"] != "y" for p in oc))
check("best of remaining leads", oc[0]["md5"] == "z")
oc2 = b.order_candidates(list(P), _dq(["x", "y", "z"]))  # x appended first = oldest
check("exhausted -> oldest-shown leads", oc2[0]["md5"] == "x")
check("exhausted -> full pool returned", len(oc2) == 3)

print(f"\n==== {passed} passed, {failed} failed ====")
raise SystemExit(1 if failed else 0)
