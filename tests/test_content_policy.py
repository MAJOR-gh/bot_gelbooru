"""Точечные исключения: 1boy разрешён, а целевые фильтры контента продолжают работать."""
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import content_filter as cf
import search
import sources
import tag_help


class ContentPolicyTests(unittest.TestCase):
    def test_1boy_is_not_a_blocked_content_category(self):
        self.assertNotIn("1boy", cf.HARD_SET)
        self.assertNotIn("1boy", cf.BLACKLIST_SET)
        self.assertNotIn("-1boy", cf.SERVER_EXCLUDES)
        self.assertFalse(cf.tag_is_blocked("1boy"))

    def test_mixed_scene_is_allowed_without_a_mandatory_hetero_tag(self):
        for tags in ("1girl 1boy nude", "1girl 1boy hetero nude"):
            with self.subTest(tags=tags):
                self.assertTrue(cf.post_is_clean({"tags": tags, "rating": "explicit"},
                                                 allowed_ratings=cf.NSFW_RATINGS))

    def test_solo_female_art_is_not_lost(self):
        self.assertTrue(cf.post_is_clean({"tags": "1girl solo nude", "rating": "explicit"},
                                         allowed_ratings=cf.NSFW_RATINGS))
        for source in (sources.Gelbooru(), sources.Safebooru(), sources.Konachan()):
            with self.subTest(source=source.name):
                _url, params = source.request(["solo"], 0)
                self.assertNotIn("hetero", params["tags"].split())

    def test_targeted_exclusions_still_apply_locally_and_on_server(self):
        for tag in ("yaoi", "bara", "male/male", "male_only", "male_focus",
                    "femboy", "trap", "crossdressing", "otoko_no_ko", "otokonoko",
                    "futanari", "futa"):
            with self.subTest(tag=tag):
                self.assertFalse(cf.post_is_allowed({"tags": f"1girl 1boy nude {tag}"}))
                self.assertTrue(cf.tag_is_blocked(tag))
                self.assertIn("-" + tag, cf.SERVER_EXCLUDES)

    def test_crossdressing_compounds_are_not_missed(self):
        for tag in ("male_crossdressing", "crossdressing_male", "CROSSDRESSING_MALE",
                    "male-crossdressing", "crossdress", "male_crossdress"):
            with self.subTest(tag=tag):
                self.assertFalse(cf.post_is_allowed({"tags": f"1boy nude {tag}"}))
                self.assertTrue(cf.tag_is_blocked(tag))

    def test_otokonoko_spelling_and_variants_are_blocked(self):
        for tag in ("otokonoko", "otokonoko_(character)", "otoko_no_ko", "otoko-no-ko"):
            with self.subTest(tag=tag):
                self.assertFalse(cf.post_is_allowed({"tags": f"1boy nude {tag}"}))
                self.assertTrue(cf.tag_is_blocked(tag))

    def test_exclusion_tags_remain_usable(self):
        for tag in ("-yaoi", "-femboy", "-crossdressing", "-otokonoko", "-1boy"):
            with self.subTest(tag=tag):
                self.assertFalse(cf.tag_is_blocked(tag))
                self.assertIn(tag, cf.clean_user_tags([tag]))

    def test_dapi_requests_allow_1boy_without_cancelling_it(self):
        for source in (sources.Gelbooru(), sources.Safebooru()):
            with self.subTest(source=source.name):
                _url, params = source.request(["1boy"], 0)
                tags = params["tags"].split()
                self.assertIn("1boy", tags)
                self.assertNotIn("-1boy", tags)
                self.assertIn("-yaoi", tags)
                self.assertIn("-otokonoko", tags)

    def test_konachan_keeps_targeted_filter_without_server_tag_overflow(self):
        source = sources.Konachan()
        _url, params = source.request(["1girl", "1boy"], 0)
        self.assertIn("1boy", params["tags"].split())
        self.assertNotIn("-1boy", params["tags"].split())
        post = source.normalize({"id": 1, "tags": "1girl 1boy nude", "rating": "e"})
        self.assertTrue(cf.post_is_allowed(post, source.ratings))
        post["tags"] += " crossdressing_male"
        self.assertFalse(cf.post_is_allowed(post, source.ratings))

    def test_targeted_tag_variants_are_not_suggested(self):
        for tag in ("otokonoko", "male_crossdressing", "yaoi"):
            with self.subTest(tag=tag):
                self.assertFalse(tag_help.permitted(tag))

    def test_no_broad_substring_false_positives(self):
        for tag in ("futaba_sakura", "barbara", "contrast", "cat_ears", "feminine_female"):
            with self.subTest(tag=tag):
                self.assertTrue(cf.post_is_clean({"tags": f"1girl nude {tag}"}))
                self.assertFalse(cf.tag_is_blocked(tag))

    def test_age_ai_and_rating_guards_are_unchanged(self):
        for forbidden in ("child", "aged-down", "ai-generated"):
            with self.subTest(tag=forbidden):
                self.assertFalse(cf.post_is_allowed({"tags": f"1girl 1boy nude {forbidden}"}))
        self.assertFalse(cf.post_is_allowed(
            {"tags": "1girl 1boy", "rating": "explicit"}, cf.SAFE_RATINGS))

    def test_search_cache_key_tracks_content_policy_change(self):
        # Смена политики меняет ключ индекса: метаданные, собранные со старыми
        # серверными исключениями, не выдаются за полную выдачу по новым правилам.
        with open(cf.__file__, "rb") as stream:
            self.assertEqual(search.POLICY_HASH, hashlib.sha256(stream.read()).hexdigest())


if __name__ == "__main__":
    unittest.main()