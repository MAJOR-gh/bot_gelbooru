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
check("clean post", b.post_is_clean({"tags": "1girl blue_hair smile"}) is True)
check("dirty post (loli)", b.post_is_clean({"tags": "1girl loli"}) is False)
check("dirty post (gore)", b.post_is_clean({"tags": "blood gore wound"}) is False)
check("empty tags -> clean", b.post_is_clean({}) is True)

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

print(f"\n==== {passed} passed, {failed} failed ====")
raise SystemExit(1 if failed else 0)
