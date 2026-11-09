"""Synthetic-image regressions; no network or production history."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import random
import tempfile
import unittest
from io import BytesIO
from PIL import Image, ImageDraw
from selection import ShownMemory
from visual_duplicates import fingerprint, similar
import content_filter as cf


def sample(seed=1):
    r = random.Random(seed)
    im = Image.new("RGB", (320, 240), "white")
    draw = ImageDraw.Draw(im)
    for _ in range(45):
        x, y = r.randrange(260), r.randrange(180)
        draw.rectangle((x, y, x+60, y+60), fill=tuple(r.randrange(256) for _ in range(3)))
    return im


def encode(im, fmt="PNG", **kwargs):
    out = BytesIO()
    im.save(out, fmt, **kwargs)
    return out.getvalue()


class VisualTests(unittest.TestCase):
    def test_resize_compression_and_different_picture(self):
        original = fingerprint(encode(sample()))
        smaller = fingerprint(encode(sample().resize((640, 480)), "JPEG", quality=85))
        self.assertIsNotNone(original)
        self.assertTrue(similar(original, smaller))
        self.assertFalse(similar(original, fingerprint(encode(sample(7)))))
        self.assertIsNone(fingerprint(b"not an image"))

    def test_reservations_release_persistence_and_channel_scope(self):
        sig = fingerprint(encode(sample()))
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "history.sqlite3")
            one, two = ShownMemory(path), ShownMemory(path)
            try:
                a = one.reserve("channel", {"first-id"})
                b = two.reserve("channel", {"different-id"})
                self.assertTrue(one.claim_visual("channel", a, [sig]))
                self.assertFalse(two.claim_visual("channel", b, [sig]))
                one.release(a)
                self.assertTrue(two.claim_visual("channel", b, [sig]))
                two.mark_sent(b)
                one.close()
                self.assertFalse(one.claim_visual("channel", "new-token", [sig]))
                self.assertTrue(one.claim_visual("other-channel", "other-token", [sig]))
            finally:
                one.close()
                two.close()

    def test_aliases_required_tags_and_exclusions(self):
        self.assertEqual(cf.clean_user_tags(["Boobs", "-BUTT"]), ["breasts", "-ass"])
        self.assertTrue(cf.matches_query({"tags": "landscape sky"}, ["landscape", "-indoors"]))
        self.assertFalse(cf.matches_query({"tags": "landscape sky"}, ["landscape", "indoors"]))
        self.assertFalse(cf.matches_query({"tags": "landscape sky"}, ["landscape", "-sky"]))
        self.assertTrue(cf.matches_query({"tags": "landscape sky"}, ["land*", "rating:safe"]))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_visual_duplicate_never_sends_and_uncertain_send_keeps_reservation(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        import delivery
        memory = ShownMemory(":memory:")
        sig = fingerprint(encode(sample()))
        interaction = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))
        async def payload(post):
            return {"content": "synthetic image", "_visuals": [sig]}, ""
        try:
            sent, _ = await delivery.send_first_fitting(memory, interaction, "channel",
                                                       [{"_uid": "a"}], payload)
            self.assertIsNotNone(sent)
            sent, reasons = await delivery.send_first_fitting(memory, interaction, "channel",
                                                             [{"_uid": "b"}], payload)
            self.assertIsNone(sent)
            self.assertIn("duplicate", reasons)
            interaction.followup.send.assert_awaited_once()
            interaction.followup.send.side_effect = OSError("connection lost")
            with self.assertRaises(OSError):
                await delivery.send_first_fitting(memory, interaction, "other",
                                                  [{"_uid": "c"}], payload)
            self.assertFalse(memory.claim_visual("other", "retry", [sig]))
        finally:
            memory.close()
