"""Юнит-тесты логики бота (без подключения к Discord и без сети).

Запуск: python test_bot.py
"""
import asyncio
import os
import tempfile
import time
from collections import deque

import booru
import bot_gelbooru as b
import content_filter as cf
import selection as sel

# Never touch the production history during tests.
b.memory = sel.ShownMemory(":memory:")

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
check("json with post list", booru.parse_posts('{"post":[{"id":1},{"id":2}]}') == [{"id": 1}, {"id": 2}])
check("json no post key -> []", booru.parse_posts('{"@attributes":{"count":0}}') == [])
check("json single post dict -> wrapped", booru.parse_posts('{"post":{"id":7}}') == [{"id": 7}])
check("json top-level list", booru.parse_posts('[{"id":1}]') == [{"id": 1}])
check("xml parse", booru.parse_posts('<posts><post id="5" file_url="x"/></posts>')[0]["id"] == "5")
check("garbage -> None", booru.parse_posts("not json not xml") is None)
check("count from @attributes",
      booru.parse_posts_page('{"@attributes":{"count":250},"post":[]}')[1] == 250)
check("count from xml", booru.parse_posts_page('<posts count="42"></posts>')[1] == 42)

print("== post_is_clean ==")
check("clean post", cf.post_is_clean({"tags": "1girl blue_hair smile nude"}) is True)
check("clean post without nudity rejected", cf.post_is_clean({"tags": "1girl blue_hair smile"}) is False)
check("clean post without nudity allowed when flag off",
      cf.post_is_clean({"tags": "1girl blue_hair smile"}, require_nudity=False) is True)
check("dirty post (loli)", cf.post_is_clean({"tags": "1girl loli"}) is False)
check("dirty post (gore)", cf.post_is_clean({"tags": "blood gore wound"}) is False)
check("empty tags -> not clean when nudity required", cf.post_is_clean({}) is False)
check("empty tags -> clean when nudity not required", cf.post_is_clean({}, require_nudity=False) is True)
check("HARD exact (1boy) rejected", cf.post_is_clean({"tags": "1girl 1boy nude"}) is False)
check("HARD variant (yaoi_*) rejected", cf.post_is_clean({"tags": "yaoi_hand nude"}) is False)
check("futaba_sakura passes (token-exact HARD)", cf.post_is_clean({"tags": "futaba_sakura nude"}) is True)
check("strap-on variant rejected", cf.post_is_clean({"tags": "strap-on_harness nude"}) is False)
check("AI substring rejected", cf.post_is_clean({"tags": "nude ai-generated"}) is False)
check("dressed rejected", cf.post_is_clean({"tags": "nude fully_clothed"}) is False)

print("== ratings ==")
check("safebooru: questionable rejected",
      cf.post_is_allowed({"tags": "1girl", "rating": "questionable"}, cf.SAFE_RATINGS) is False)
check("safebooru: general ok",
      cf.post_is_allowed({"tags": "1girl", "rating": "general"}, cf.SAFE_RATINGS) is True)
check("nsfw: konachan safe rejected",
      cf.post_is_allowed({"tags": "nude", "rating": "safe"}, cf.NSFW_RATINGS) is False)
check("nsfw: explicit ok",
      cf.post_is_allowed({"tags": "nude", "rating": "explicit"}, cf.NSFW_RATINGS) is True)
check("no rating filter -> rating ignored", cf.post_is_allowed({"tags": "nude"}) is True)

print("== tag_is_blocked (exact token, no false positives) ==")
check("futa blocked", cf.tag_is_blocked("futa") is True)
check("futanari blocked", cf.tag_is_blocked("futanari") is True)
check("trap blocked", cf.tag_is_blocked("trap") is True)
check("futaba_sakura NOT blocked", cf.tag_is_blocked("futaba_sakura") is False)
check("strap NOT blocked", cf.tag_is_blocked("strap") is False)
check("contrast NOT blocked", cf.tag_is_blocked("contrast") is False)
check("1girl NOT blocked", cf.tag_is_blocked("1girl") is False)
check("cat_ears NOT blocked (cat is exact-only)", cf.tag_is_blocked("cat_ears") is False)
check("HARD exact collar blocked (was silent empty result)", cf.tag_is_blocked("collar") is True)
check("1boy blocked", cf.tag_is_blocked("1boy") is True)
check("exclusion -futa is allowed", cf.tag_is_blocked("-futa") is False)
check("loli substring blocked", cf.tag_is_blocked("loli_dominance") is True)
check("hyphen age variant blocked (user tag)", cf.tag_is_blocked("aged-down") is True)
check("hyphen age variant rejected (post)", cf.post_is_clean({"tags": "aged-down nude"}) is False)

print("== clean_user_tags ==")
check("spaces -> underscore + lower", cf.clean_user_tags(("Large  Breasts", None, " ")) == ["large_breasts"])
check("dedupe keeps order", cf.clean_user_tags(("ass", "breasts", "ASS")) == ["ass", "breasts"])

print("== tag_verdict ==")
check("kanzaki wins over block", b.tag_verdict(["futa", "kanzaki_hideri"]) == "kanzaki")
check("venti", b.tag_verdict(["venti_(genshin_impact)"]) == "venti")
check("felix -> lgbt joke (before block)", b.tag_verdict(["felix_argyle"]) == "lgbt")
check("blocked", b.tag_verdict(["1girl", "yaoi"]) == "blocked")
check("first tag decides", b.tag_verdict(["yaoi", "venti"]) == "blocked")
check("normal -> None", b.tag_verdict(["hatsune_miku"]) is None)
check("excluded venti -> None", b.tag_verdict(["1girl", "-venti"]) is None)

print("== server excludes ==")
check("excludes have -loli", "-loli" in cf.SERVER_EXCLUDES)
check("excludes have -1boy", "-1boy" in cf.SERVER_EXCLUDES)
check("excludes unique", len(cf.SERVER_EXCLUDES) == len(set(cf.SERVER_EXCLUDES)))
check("BLACKLIST_SET no dashes", all(not t.startswith("-") for t in cf.BLACKLIST_SET))

print("== CooldownManager ==")
cd = b.CooldownManager(rate=2, per=1.0)
check("1st call ok", cd.retry_after(1) == 0.0)
check("2nd call ok", cd.retry_after(1) == 0.0)
check("3rd call blocked", cd.retry_after(1) > 0)
check("different user ok", cd.retry_after(2) == 0.0)
time.sleep(1.05)
check("after window resets", cd.retry_after(1) == 0.0)

print("== Gelbooru params ==")
g = booru.Gelbooru("K", "U")
url, p = g.request(["1girl"], 2)
check("includes api_key/user_id when set", p.get("api_key") == "K" and p.get("user_id") == "U")
check("static fields", p["page"] == "dapi" and p["q"] == "index" and p["json"] == "1")
check("page -> pid", p["pid"] == "2")
check("query has sort + rating + excludes",
      p["tags"].startswith("1girl sort:score -rating:general") and "-loli" in p["tags"])
check("omits api_key when empty", "api_key" not in booru.Gelbooru("", "").params(s="post"))

print("== Konachan params ==")
k = booru.Konachan()
_, kp = k.request(["a", "b", "c", "d"], 0)
check("page is 1-based", kp["page"] == "1")
check("4 tags + order + rating = 6", kp["tags"].split() == ["a", "b", "c", "d", "order:score", "-rating:s"])
_, kp = k.request(["a", "b", "c", "d", "nude"], 0)
check("never more than 6 tags, order:score kept",
      len(kp["tags"].split()) == 6 and "order:score" in kp["tags"].split())
check("konachan rating normalized", k.normalize({"rating": "e", "author": "x"})["rating"] == "explicit")
check("safebooru md5 from hash", booru.Safebooru().normalize({"hash": "ABC"})["md5"] == "abc")

print("== focus ==")
check("breasts -> {breasts}", cf.focus_groups_for(["breasts"]) == {"breasts"})
check("large_breasts -> {breasts}", cf.focus_groups_for(["large_breasts"]) == {"breasts"})
check("breasts+ass -> both", cf.focus_groups_for(["breasts", "ass"]) == {"breasts", "ass"})
check("character -> empty", cf.focus_groups_for(["hatsune_miku"]) == set())
check("case-insensitive", cf.focus_groups_for(["Breasts"]) == {"breasts"})
nb = cf.requested_nudity_tags({"breasts"})
check("no group -> None", cf.requested_nudity_tags(set()) is None)
check("breasts: nipples counts", "nipples" in nb)
check("breasts: general nude counts", "nude" in nb)
check("breasts: bare ass does NOT count", "ass" not in nb)
na = cf.requested_nudity_tags({"ass"})
check("ass: bare ass counts", "ass" in na)
check("ass: bare pussy does NOT count", "pussy" not in na)
G = {"breasts"}
check("on-only -> tier 3", cf.focus_tier({"tags": "1girl large_breasts nude"}, G) == 3)
check("on+off -> tier 2", cf.focus_tier({"tags": "large_breasts ass_focus"}, G) == 2)
check("neutral -> tier 1", cf.focus_tier({"tags": "1girl smile standing"}, G) == 1)
check("off-only -> tier 0", cf.focus_tier({"tags": "1girl ass_focus from_behind"}, G) == 0)
check("no group -> tier 0", cf.focus_tier({"tags": "large_breasts"}, set()) == 0)
check("breasts req: bare-ass post rejected",
      cf.post_is_clean({"tags": "1girl ass"}, nudity_tags=nb) is False)
check("breasts req: nipples post passes",
      cf.post_is_clean({"tags": "1girl nipples"}, nudity_tags=nb) is True)
check("legacy: bare-ass passes", cf.post_is_clean({"tags": "1girl ass"}) is True)

print("== order_candidates ==")
P = [
    {"md5": "x", "tags": "1girl", "score": 1},
    {"md5": "y", "tags": "1girl", "score": 100},
    {"md5": "z", "tags": "1girl", "score": 50},
]
check("all unseen -> best score first", sel.order_candidates(P, deque())[0]["md5"] == "y")
oc = sel.order_candidates(P, deque(["y"]))
check("seen art excluded", all(p["md5"] != "y" for p in oc))
check("best of remaining leads", oc[0]["md5"] == "z")
oc2 = sel.order_candidates(P, deque(["x", "y", "z"]))
check("exhausted -> repeat forbidden", oc2 == [])
F = [
    {"md5": "on_lo", "tags": "huge_breasts", "score": 5},
    {"md5": "on_hi", "tags": "large_breasts", "score": 40},
    {"md5": "off_hi", "tags": "ass_focus", "score": 999},
]
check("champion = best score within top focus tier",
      sel.order_candidates(F, deque(), {"breasts"})[0]["md5"] == "on_hi")
check("off-focus mega-score goes last",
      sel.order_candidates(F, deque(), {"breasts"})[-1]["md5"] == "off_hi")
dressed = [{"md5": "d", "tags": "1girl", "score": 5000}]
naked = [{"md5": "n", "tags": "nude", "score": 10}]
check("strict beats higher-score dressed (was a bug)",
      sel.order_candidates(naked, deque(), None, dressed)[0]["md5"] == "n")
check("dressed used when strict all seen",
      sel.order_candidates(naked, deque(["n"]), None, dressed)[0]["md5"] == "d")
check("string/None scores safe",
      sel.post_score({"score": None}) == 0 and sel.post_score({"score": "12"}) == 12)

print("== ShownMemory ==")
with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "sub", "shown.sqlite3")
    m = sel.ShownMemory(path)
    m.remember("channel1", "a")
    m.remember("channel1", "b")
    m.remember("channel2", "c")
    m.remember("channel1", "a")
    check("history is a set", m.recent("channel1") == {"a", "b"})
    check("unknown channel -> empty", not m.recent("unknown"))
    m.close()
    m2 = sel.ShownMemory(path)
    m2.load()
    check("immediate durable roundtrip", m2.recent("channel1") == {"a", "b"}
          and m2.recent("channel2") == {"c"})
    for i in range(600):
        m2.remember("channel1", str(i))
    check("history never evicts oldest", "a" in m2.recent("channel1")
          and len(m2.recent("channel1")) == 602)
    m2.close()


# ── search() на фейковом источнике ────────────────────────────────────────────
class FakeSource(booru.Source):
    name = "Fake"
    page_size = 4
    ratings = cf.NSFW_RATINGS

    def post_url(self, post):
        return "x"


def fake_pages(pages: dict, count=None):
    """pages: {(tuple(tags), page): [posts]} → подмена booru.fetch_page + журнал вызовов."""
    calls = []
    booru.clear_search_cache()

    async def fetch(source, tags, page):
        calls.append((tuple(tags), page))
        return list(pages.get((tuple(tags), page), [])), count
    return fetch, calls


def nude(i, score=None):
    return {"md5": f"n{i}", "tags": "1girl nude", "score": score if score is not None else 100 - i,
            "rating": "explicit"}


def dressed_post(i):
    return {"md5": f"d{i}", "tags": "1girl", "score": 500 - i, "rating": "explicit"}


async def search_tests():
    orig = booru.fetch_page
    try:
        print("== search: paging ==")
        pages = {(("t",), 0): [nude(i) for i in range(4)],
                 (("t",), 1): [nude(i) for i in range(4, 8)],
                 (("t",), 2): [nude(i) for i in range(8, 12)],
                 (("t",), 3): [nude(i) for i in range(12, 14)]}
        booru.fetch_page, calls = fake_pages(pages)
        r = await booru.search(FakeSource(), ["t"], deque())
        check("full scan even with fresh top", all((("t",), n) in calls for n in range(4)))
        check("best first", r.candidates[0]["md5"] == "n0")

        booru.fetch_page, calls = fake_pages(pages)
        r = await booru.search(FakeSource(), ["t"], deque(["n0", "n1", "n2", "n3"]))
        check("page 0 seen -> deeper pages fetched", (("t",), 1) in calls and (("t",), 3) in calls)
        check("best unseen leads", r.candidates[0]["md5"] == "n4")

        booru.fetch_page, calls = fake_pages(pages, count=6)
        r = await booru.search(FakeSource(), ["t"], deque(["n0", "n1", "n2", "n3"]))
        check("count limits pages (count=6 -> pages 0,1)",
              sorted(c[1] for c in calls if c[0] == ("t",)) == [0, 1])

        print("== search: nude phase & dressed fallback ==")
        pages2 = {(("t",), 0): [dressed_post(i) for i in range(3)] + [nude(1)],
                  (("t", "nude"), 0): [nude(1)]}
        booru.fetch_page, calls = fake_pages(pages2)
        r = await booru.search(FakeSource(), ["t"], deque())
        check("no narrow nude supplement needed after full scan", all(c[0] == ("t",) for c in calls))
        check("strict (nude) first, dressed after",
              [p["md5"] for p in r.candidates] == ["n1", "d0", "d1", "d2"])

        booru.fetch_page, calls = fake_pages(pages2)
        await booru.search(FakeSource(), ["t", "nude"], deque())
        check("no nude phase when user already asked nude", all(c[0] == ("t", "nude") for c in calls))

        booru.fetch_page, calls = fake_pages(pages2)
        r = await booru.search(FakeSource(), ["t"], deque(), require_nudity=False)
        check("safe mode: no nude phase, dressed are candidates",
              all(c[0] == ("t",) for c in calls) and r.candidates[0]["md5"] == "d0")

        print("== search: filters & errors ==")
        bad = {(("t",), 0): [{"md5": "l", "tags": "loli nude", "score": 9, "rating": "explicit"},
                             {"md5": "s", "tags": "nude", "score": 9, "rating": "safe"},
                             nude(1)]}
        booru.fetch_page, _ = fake_pages(bad)
        r = await booru.search(FakeSource(), ["t"], deque())
        check("blacklisted and wrong-rating posts dropped", [p["md5"] for p in r.candidates] == ["n1"])

        async def broken(source, tags, page):
            raise booru.SourceError("auth", "401")
        booru.clear_search_cache()
        booru.fetch_page = broken
        r = await booru.search(FakeSource(), ["t"], deque())
        check("source error reported, no candidates", r.errors == ["auth"] and not r.candidates and r.fetched == 0)
    finally:
        booru.fetch_page = orig

    print("== fetch_media ==")
    calls = []
    orig_dl = booru.download

    async def fake_dl(url, max_size, referer=""):
        calls.append(url)
        if "big" in url:
            return None, "too_big"
        return b"data", None
    booru.download = fake_dl
    try:
        m, why = await booru.fetch_media(FakeSource(), {"file_url": "http://h/big.png",
                                                        "jpeg_url": "http://h/ok.jpg"}, 100)
        check("too big original -> jpeg version", m is not None and m.reduced and m.ext == "jpg")
        calls.clear()
        m, why = await booru.fetch_media(FakeSource(), {"file_url": "http://h/a.png", "file_size": 999,
                                                        "sample_url": "http://h/s.jpg"}, 100)
        check("known file_size skips original without request", calls == ["http://h/s.jpg"])
        m, why = await booru.fetch_media(FakeSource(), {"file_url": "http://h/v.mp4"}, 100)
        check("video detected", m.is_video and m.ext == "mp4")
        m, why = await booru.fetch_media(FakeSource(), {"file_url": "http://h/big.mp4"}, 100)
        check("only too-big file -> too_big", m is None and why == "too_big")
        m, why = await booru.fetch_media(FakeSource(), {"file_url": ""}, 100)
        check("no url -> no_file", m is None and why == "no_file")
    finally:
        booru.download = orig_dl

    print("== send_first_fitting ==")

    class Resp:
        status = 413
        reason = "Payload Too Large"

    class Followup:
        def __init__(self, fail_first=False):
            self.sent = []
            self.fail_first = fail_first

        async def send(self, **kw):
            if self.fail_first:
                self.fail_first = False
                import nextcord
                raise nextcord.HTTPException(Resp(), "too large")
            self.sent.append(kw)

    class Inter:
        def __init__(self, fu):
            self.followup = fu

    async def payload_ok(post):
        return {"content": post["md5"]}, ""

    fu = Followup(fail_first=True)
    cands = [{"md5": "a"}, {"md5": "b"}]
    sent, reasons = await b.send_first_fitting(Inter(fu), "T|x", cands, payload_ok)
    check("413 -> next candidate sent", sent["md5"] == "b" and fu.sent == [{"content": "b"}])
    check("sent art remembered", "b" in b.memory.recent("T|x"))

    reserved = b.memory.reserve("T|y", {"a"})
    fu = Followup()
    sent, _ = await b.send_first_fitting(Inter(fu), "T|y", cands, payload_ok)
    check("in-flight art skipped (no double send)", sent["md5"] == "b")
    b.memory.release(reserved)

    async def payload_none(post):
        return None, "too_big"
    fu = Followup()
    sent, reasons = await b.send_first_fitting(Inter(fu), "T|z", cands, payload_none)
    check("nothing fits -> None + reasons", sent is None and reasons == {"too_big"})
    check("too_big text", "слишком большие" in b.nothing_sent_text("x", "G", reasons))
    check("error text", "Не удалось скачать" in b.nothing_sent_text("x", "G", {"error"}))
    check("failed downloads release pending", not b.memory._connection().execute(
        "SELECT 1 FROM shown WHERE scope='T|z' AND status='pending'").fetchone())


asyncio.run(search_tests())

print("== channel_allows_nsfw ==")


class _Chan:
    def __init__(self, nsfw):
        self._nsfw = nsfw

    def is_nsfw(self):
        return self._nsfw


class _ChanInter:
    def __init__(self, guild_id, channel):
        self.guild_id = guild_id
        self.channel = channel


check("NSFW channel allowed", b.channel_allows_nsfw(_ChanInter(1, _Chan(True))) is True)
check("plain channel refused", b.channel_allows_nsfw(_ChanInter(1, _Chan(False))) is False)
check("DM refused (no age gate)", b.channel_allows_nsfw(_ChanInter(None, _Chan(True))) is False)
check("channel without is_nsfw refused", b.channel_allows_nsfw(_ChanInter(1, object())) is False)

b.memory.close()
print(f"\n==== {passed} passed, {failed} failed ====")
raise SystemExit(1 if failed else 0)
