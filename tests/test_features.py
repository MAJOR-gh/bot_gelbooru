"""Next/status/tag-help regressions. All tests offline; no Discord or bot tokens."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot_gelbooru as bot
import content_filter as cf
import diagnostics
import search
import sources
import tag_help
from search_index import SearchIndex
from search_ui import NextView
from selection import HistoryView, ShownMemory, LEGACY_SCOPE


def post(i, tags="1girl nude", tier_score=None):
    return {"id": i, "_site": "Gelbooru", "md5": f"m{i}", "tags": tags,
            "rating": "explicit", "score": 20000-i if tier_score is None else tier_score}


class Interaction:
    def __init__(self, owner=1, channel=100, nsfw=True):
        self.user = SimpleNamespace(id=owner)
        self.channel_id, self.guild_id = channel, 1
        self.guild = None
        self.channel = SimpleNamespace(is_nsfw=lambda: nsfw)
        self.response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(),
                                        send_autocomplete=AsyncMock(), is_done=lambda: True)
        self.followup = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())))
        self.message = SimpleNamespace(edit=AsyncMock())


class FeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"DATA_DIR": self.tmp.name})
        self.env.start()
        search.clear_search_cache()
        tag_help._cache.clear()
        diagnostics._health.clear()
        diagnostics._locks.clear()
        diagnostics._searches.clear()
        self.previous_memory = bot.memory
        bot.memory = ShownMemory(os.path.join(self.tmp.name, "shown.sqlite3"))
        self.source = sources.Gelbooru(None, None)

    async def asyncTearDown(self):
        search.clear_search_cache()
        bot.memory.close()
        bot.memory = self.previous_memory
        self.env.stop()
        self.tmp.cleanup()

    async def test_next_rejects_other_user(self):
        advance = AsyncMock(return_value=True)
        view = NextView(1, 100, advance)
        await view.next_button.callback(Interaction(owner=2))
        advance.assert_not_awaited()
        self.assertFalse(view.consumed)
        view.stop()

    async def test_next_rejects_other_channel(self):
        advance = AsyncMock(return_value=True)
        view = NextView(1, 100, advance)
        await view.next_button.callback(Interaction(channel=101))
        advance.assert_not_awaited()
        view.stop()

    async def test_next_success_is_single_use(self):
        advance = AsyncMock(return_value=True)
        view = NextView(1, 100, advance)
        await view.next_button.callback(Interaction())
        await view.next_button.callback(Interaction())
        advance.assert_awaited_once()
        self.assertTrue(view.consumed)
        self.assertTrue(view.next_button.disabled)

    async def test_next_failed_cooldown_does_not_consume(self):
        view = NextView(1, 100, AsyncMock(return_value=False))
        await view.next_button.callback(Interaction())
        self.assertFalse(view.consumed)
        self.assertFalse(view.next_button.disabled)
        view.stop()

    async def test_double_click_only_one_search(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def advance(interaction):
            entered.set()
            await release.wait()
            return True
        mock = AsyncMock(side_effect=advance)
        view = NextView(1, 100, mock)
        first = asyncio.create_task(view.next_button.callback(Interaction()))
        await entered.wait()
        second = Interaction()
        await view.next_button.callback(second)
        second.response.send_message.assert_awaited_once()
        release.set()
        await first
        mock.assert_awaited_once()

    async def test_timeout_disables_button(self):
        view = NextView(1, 100, AsyncMock())
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.on_timeout()
        self.assertTrue(view.next_button.disabled)
        self.assertTrue(view.consumed)
        view.message.edit.assert_awaited_once_with(view=view)
        view.stop()

    async def test_next_has_absolute_expiry_even_after_cooldown_clicks(self):
        advance = AsyncMock(return_value=True)
        view = NextView(1, 100, advance)
        view.expires_at = 0
        await view.next_button.callback(Interaction())
        advance.assert_not_awaited()
        self.assertTrue(view.consumed)
        self.assertTrue(view.next_button.disabled)

    async def test_next_rechecks_nsfw_gate(self):
        interaction = Interaction(nsfw=False)
        view = bot.next_view(interaction, ("1girl",), self.source, bot.CooldownManager(5, 30), True)
        with patch("search.search", AsyncMock()) as find:
            await view.next_button.callback(interaction)
            find.assert_not_awaited()
        self.assertFalse(view.consumed)
        view.stop()

    async def test_next_real_cooldown_is_shared(self):
        interaction = Interaction()
        cooldown = bot.CooldownManager(1, 30)
        cooldown.retry_after(1)
        view = bot.next_view(interaction, ("1girl",), self.source, cooldown, True)
        with patch("search.search", AsyncMock()) as find:
            await view.next_button.callback(interaction)
            find.assert_not_awaited()
        self.assertFalse(view.consumed)
        view.stop()

    async def test_next_sends_different_art_and_keeps_query(self):
        rows = [post(i) for i in range(100)]
        async def fetch(source, tags, page, cursor=None):
            self.assertIs(source, self.source)
            self.assertEqual(tags, ["1girl", "-glasses"])
            return rows if page == 0 else [], 100
        async def payload(source, p, display, max_size):
            return {"content": f"art-{p['id']}"}, ""
        first, second = Interaction(), Interaction()
        with patch("search.fetch_page", fetch), patch("delivery.build_booru_payload", payload):
            await bot.run_booru_search(first, ("1girl", "-glasses"), self.source,
                                      bot.CooldownManager(5, 30))
            kwargs = first.followup.send.call_args.kwargs
            self.assertEqual(kwargs["content"], "art-0")
            view = kwargs["view"]
            await view.next_button.callback(second)
        self.assertEqual(second.followup.send.call_args.kwargs["content"], "art-1")
        second.followup.send.call_args.kwargs["view"].stop()

    async def test_priority_after_six_pages_waits_then_next_finds_it(self):
        rows = [post(i, "1girl nude large_breasts ass_focus" if i < 700
                     else "1girl nude large_breasts") for i in range(1000)]
        async def fetch(source, tags, page, cursor=None):
            return rows[page*100:(page+1)*100], 1000
        with patch("search.fetch_page", fetch):
            result = await search.search(self.source, ["breasts"], set())
            self.assertEqual(result.errors, ["scan_pending"])
            self.assertTrue(result.priority_pending)
            self.assertFalse(result.candidates)
            self.assertEqual(result.fetched, 600)
            result = await search.search(self.source, ["breasts"], set())
        self.assertTrue(result.ranking_proven)
        self.assertEqual(result.candidates[0]["id"], 700)
        self.assertTrue(all(cf.focus_tier(p, {"breasts"}) == cf.TOP_TIER for p in result.candidates))

    async def test_pending_search_has_next_and_no_download(self):
        rows = [post(i, "1girl nude large_breasts ass_focus") for i in range(1000)]
        async def fetch(source, tags, page, cursor=None):
            return rows[page*100:(page+1)*100], 1000
        interaction = Interaction()
        with patch("search.fetch_page", fetch), patch("delivery.build_booru_payload", AsyncMock()) as download:
            await bot.run_booru_search(interaction, ("breasts",), self.source, bot.CooldownManager(5, 30))
            download.assert_not_awaited()
        kwargs = interaction.followup.send.call_args.kwargs
        self.assertIn("Приоритет", interaction.followup.send.call_args.args[0])
        self.assertIsInstance(kwargs["view"], NextView)
        self.assertIsNotNone(kwargs["view"].message)
        kwargs["view"].stop()

    async def test_complete_focus_search_can_fall_back_if_no_top_tier_exists(self):
        rows = [post(i, "1girl nude large_breasts ass_focus") for i in range(100)]
        async def fetch(source, tags, page, cursor=None):
            return rows if page == 0 else [], 100
        with patch("search.fetch_page", fetch):
            result = await search.search(self.source, ["breasts"], set())
        self.assertTrue(result.complete)
        self.assertFalse(result.priority_pending)
        self.assertTrue(result.candidates)

    async def test_incomplete_focus_pagination_does_not_offer_endless_next(self):
        rows = [post(i, "1girl nude large_breasts ass_focus") for i in range(100)]
        async def fetch(source, tags, page, cursor=None):
            return rows if page == 0 else [], 10000
        with patch("search.fetch_page", fetch):
            result = await search.search(self.source, ["breasts"], set())
        self.assertFalse(result.priority_pending)
        self.assertEqual(result.errors, ["incomplete"])
        self.assertFalse(result.candidates)

    async def test_fuzzy_local_spelling(self):
        hints = await tag_help.suggestions(self.source, "smille", nsfw=True)
        self.assertEqual(hints[0], "smile")

    async def test_negative_tag_suggestions_keep_minus(self):
        hints = await tag_help.suggestions(self.source, "-long ha", nsfw=True)
        self.assertIn("-long_hair", hints)
        self.assertTrue(all(h.startswith("-") for h in hints))

    async def test_safe_channel_never_calls_tag_api(self):
        source = sources.Safebooru()
        with patch.object(source, "similar_tags", AsyncMock()) as remote:
            hints = await tag_help.suggestions(source, "bre", nsfw=False)
        remote.assert_not_awaited()
        self.assertNotIn("breasts", hints)

    async def test_live_tag_cache_and_blocked_suggestions(self):
        source = sources.Gelbooru("secret", "private")
        remote = AsyncMock(return_value=[
            {"name": "megumin_(character)", "count": 10},
            {"name": "megumin_loli", "count": 100},
            {"name": "megumin_fake", "count": 0}])
        with patch.object(source, "similar_tags", remote):
            first = await tag_help.suggestions(source, "megu", nsfw=True)
            second = await tag_help.suggestions(source, "megu", nsfw=True)
        remote.assert_awaited_once()
        self.assertEqual(first, second)
        self.assertIn("megumin_(character)", first)
        self.assertNotIn("megumin_loli", first)
        self.assertNotIn("megumin_fake", first)

    async def test_tag_api_timeout_falls_back(self):
        source = sources.Gelbooru("secret", "private")
        async def slow(*args, **kwargs):
            await asyncio.sleep(1)
        with patch.object(source, "similar_tags", slow), patch("tag_help.API_BUDGET", .01):
            hints = await tag_help.suggestions(source, "smille", nsfw=True)
        self.assertEqual(hints[0], "smile")

    async def test_tag_api_auth_error_falls_back(self):
        source = sources.Gelbooru("secret", "private")
        with patch.object(source, "similar_tags", AsyncMock(side_effect=sources.SourceError("auth", "secret"))):
            hints = await tag_help.suggestions(source, "smille", nsfw=True)
        self.assertEqual(hints[0], "smile")

    async def test_tag_hint_limit_and_input_bound(self):
        self.assertLessEqual(len(await tag_help.suggestions(self.source, "", nsfw=True)), 25)
        self.assertEqual(await tag_help.suggestions(self.source, "a"*101, nsfw=True), [])
        self.assertEqual(await tag_help.suggestions(self.source, "@everyone", nsfw=True), [])

    async def test_status_missing_credentials_skips_network(self):
        with patch("sources.fetch_text", AsyncMock()) as fetch:
            result = await diagnostics.probe(self.source)
        fetch.assert_not_awaited()
        self.assertEqual(result.state, "not_configured")

    async def test_status_probe_is_cached_and_sanitized(self):
        source = sources.Gelbooru("API_SUPER_SECRET", "PRIVATE_ID")
        with patch("sources.fetch_text", AsyncMock(return_value='{"@attributes":{"count":0}}')) as fetch:
            first = await diagnostics.probe(source)
            second = await diagnostics.probe(source)
        self.assertEqual(first.state, "ok")
        self.assertEqual(first, second)
        fetch.assert_awaited_once()
        self.assertNotIn("API_SUPER_SECRET", diagnostics.health_text(first))
        self.assertNotIn("PRIVATE_ID", diagnostics.health_text(first))

    async def test_status_probe_rejects_html(self):
        with patch("sources.fetch_text", AsyncMock(return_value="<html>challenge</html>")):
            result = await diagnostics.probe(sources.Safebooru())
        self.assertEqual(result.state, "bad")

    async def test_tag_parser_accepts_safebooru_xml(self):
        xml = ('<?xml version="1.0"?><tags type="array">'
               '<tag name="landscape" count="300" type="0"/></tags>')
        self.assertEqual(sources.parse_tag_list(xml),
                         [{"name": "landscape", "count": "300", "type": "0"}])
        self.assertEqual(sources.parse_tag_list("<html>blocked</html>"), [])

    async def test_status_probe_uses_nonempty_known_query(self):
        async def fetch(url, params, **kwargs):
            self.assertIn("scenery", params["tags"])
            self.assertEqual(params["limit"], "1")
            return json.dumps([{"id": 123, "tags": "scenery", "rating": "safe"}])
        with patch("sources.fetch_text", fetch):
            self.assertEqual((await diagnostics.probe(sources.Safebooru())).state, "ok")

    async def test_pending_next_reaches_late_focus_end_to_end(self):
        rows = [post(i, "1girl nude large_breasts ass_focus" if i < 700
                     else "1girl nude large_breasts") for i in range(1000)]
        async def fetch(source, tags, page, cursor=None):
            return rows[page*100:(page+1)*100], 1000
        async def payload(source, p, display, max_size):
            return {"content": f"art-{p['id']}"}, ""
        first, second = Interaction(), Interaction()
        with patch("search.fetch_page", fetch), patch("delivery.build_booru_payload", payload):
            await bot.run_booru_search(first, ("breasts",), self.source, bot.CooldownManager(5, 30))
            view = first.followup.send.call_args.kwargs["view"]
            await view.next_button.callback(second)
        self.assertEqual(second.followup.send.call_args.kwargs["content"], "art-700")
        self.assertNotIn("m0", bot.memory.recent("discord:1:100"))
        self.assertIn("m700", bot.memory.recent("discord:1:100"))
        second.followup.send.call_args.kwargs["view"].stop()

    async def test_status_probe_has_timeout(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(1)
        with patch("sources.fetch_text", slow), patch("diagnostics.PROBE_TIMEOUT", .01):
            result = await diagnostics.probe(sources.Safebooru())
        self.assertEqual(result.state, "timeout")

    async def test_status_data_is_scoped_to_channel(self):
        diagnostics.record_search("one", self.source, search.SearchResult([], fetched=10), 4)
        self.assertIsNone(diagnostics.last_search("two"))
        self.assertEqual(diagnostics.last_search("one")["fetched"], 10)

    async def test_new_commands_have_autocomplete(self):
        commands = {c.name: c for c in bot.bot._application_commands_to_add}
        self.assertIn("status", commands)
        for name in ("gelbooru", "konachan", "safebooru"):
            commands[name].from_callback(commands[name].callback)
            for option in commands[name].options.values():
                self.assertTrue(option.autocomplete)

    async def test_status_command_reply_is_private(self):
        interaction = Interaction(owner=99)
        states = diagnostics.Health("ok", 1, time.monotonic())
        with patch("diagnostics.probe", AsyncMock(return_value=states)):
            await bot.status_command.callback(interaction)
        self.assertTrue(interaction.response.defer.call_args.kwargs["ephemeral"])
        self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])


class HistorySQLTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"DATA_DIR": self.tmp.name})
        self.env.start()
        self.memory = ShownMemory(os.path.join(self.tmp.name, "history with space.sqlite3"))
        self.memory.load()
        self.index = SearchIndex(("history-test",))
        self.index.add([(f"m{i}", f"Gelbooru:{i}", json.dumps(post(i)), 1, 1, 0, 100-i, i)
                        for i in range(20)])

    def tearDown(self):
        self.index.close()
        self.memory.close()
        self.env.stop()
        self.tmp.cleanup()

    def test_sql_reference_avoids_temporary_history_copy(self):
        self.memory.remember("channel", "m0")
        seen = self.memory.search_history("channel")
        self.assertIsInstance(seen, HistoryView)
        candidates = self.index.candidates(seen)
        self.assertEqual(candidates[0]["id"], 1)
        self.assertEqual(self.index.db.execute("SELECT COUNT(*) FROM excluded").fetchone()[0], 0)

    def test_sql_history_matches_set_history(self):
        self.memory.remember("channel", "m0")
        self.memory.remember("channel", "Gelbooru:1")
        self.memory.failed("channel", {"m2"})
        self.memory.reserve("channel", {"m3"})
        self.memory.remember(LEGACY_SCOPE, "m4")
        self.memory.remember("other-channel", "m5")
        sql = self.index.candidates(self.memory.search_history("channel"))
        snapshot = self.index.candidates(self.memory.recent("channel"))
        self.assertEqual(list(sql), list(snapshot))
        self.assertEqual(sql[0]["id"], 5)
        self.assertEqual(sql.proven_count(0, 0, 5), 5)
        self.assertEqual(len(sql), 15)

    def test_sql_history_sees_new_reservations(self):
        candidates = self.index.candidates(self.memory.search_history("channel"))
        self.assertEqual(candidates[0]["id"], 0)
        self.memory.reserve("channel", {"m0"})
        self.assertEqual(candidates[0]["id"], 1)

    def test_failed_quarantine_expiry_is_respected(self):
        self.memory.failed("channel", {"m0"}, ttl=-1)
        candidates = self.index.candidates(self.memory.search_history("channel"))
        self.assertEqual(candidates[0]["id"], 0)

    def test_attached_history_is_read_only(self):
        self.index.candidates(self.memory.search_history("channel"))
        alias = next(iter(self.index.histories.values()))
        with self.assertRaises(sqlite3.OperationalError):
            self.index.db.execute(f'DELETE FROM "{alias}".shown')

    def test_in_memory_history_keeps_compatibility(self):
        memory = ShownMemory(":memory:")
        memory.remember("channel", "m0")
        self.assertEqual(memory.search_history("channel"), {"m0"})
        memory.close()

    def test_proven_candidate_subset_excludes_fallback(self):
        candidates = self.index.candidates(set())
        candidates.restrict_to_proven(0, 98)
        self.assertEqual([p["id"] for p in candidates], [0, 1, 2])


if __name__ == "__main__":
    unittest.main()