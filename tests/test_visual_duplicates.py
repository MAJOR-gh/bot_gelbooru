"""Synthetic-image regressions; no network or production history."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import random
import tempfile
import unittest
from io import BytesIO
from PIL import Image, ImageDraw, ImageOps
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
    def test_normalized_pixels_match_across_formats_without_perceptual_hash(self):
        flat = Image.new("RGB", (40, 30), "blue")
        png, bmp = encode(flat), encode(flat, "BMP")
        self.assertNotEqual(png, bmp)
        original, converted = fingerprint(png), fingerprint(bmp)
        self.assertNotIn("phash", original)
        self.assertTrue(similar(original, converted))
        self.assertFalse(similar(original, fingerprint(encode(flat.resize((30, 40))))))
        self.assertFalse(similar(original, fingerprint(encode(Image.new("RGB", flat.size, "red")))))

    def test_phash_catches_recompressed_copies_missed_by_legacy_signature(self):
        original = fingerprint(encode(sample()))
        legacy = {key: original[key] for key in ("version", "h", "v", "ratio", "color")}
        for transform in (lambda im: im, ImageOps.mirror):
            with self.subTest(transform=transform):
                resized = transform(sample().resize((160, 120)))
                compressed = fingerprint(encode(resized, "JPEG", quality=30))
                self.assertFalse(similar(legacy, compressed))
                self.assertTrue(similar(original, compressed))
                self.assertTrue(similar(compressed, original))
        other = fingerprint(encode(sample(7).resize((160, 120)), "JPEG", quality=30))
        self.assertFalse(similar(original, other))

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

    def test_center_crop_matches_full_image_but_not_unrelated_image(self):
        full = fingerprint(encode(sample()))
        for percentage in (2, 4, 6):
            with self.subTest(percentage=percentage):
                x, y = round(320 * percentage / 100), round(240 * percentage / 100)
                cropped = fingerprint(encode(sample().crop((x, y, 320-x, 240-y)),
                                             "JPEG", quality=85))
                cropped.pop("crops", None)  # Can compare with old stored crop signatures.
                self.assertTrue(similar(full, cropped))
                self.assertTrue(similar(cropped, full))
                self.assertFalse(similar(full, fingerprint(encode(sample(7)))))

    def test_mirror_compares_with_existing_history(self):
        original = fingerprint(encode(sample()))
        original.pop("mirror", None)  # history saved by v3.5.0/3.5.1
        mirrored = fingerprint(encode(ImageOps.mirror(sample()), "JPEG", quality=85))
        self.assertTrue(similar(original, mirrored))
        self.assertTrue(similar(mirrored, original))
        self.assertFalse(similar(original, fingerprint(encode(ImageOps.mirror(sample(7))))))

    def test_two_hundred_restarts_keep_history_and_database_marker(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "shown.sqlite3")
            marker = None
            for i in range(200):
                memory = ShownMemory(path)
                try:
                    status = memory.history_status("channel")
                    if marker is None:
                        marker = status["database_id"]
                    self.assertEqual(status["database_id"], marker)
                    for old in range(i):
                        self.assertIsNone(memory.reserve("channel", {f"id-{old}"}))
                    token = memory.reserve("channel", {f"id-{i}"})
                    self.assertIsNotNone(token)
                    memory.mark_sent(token)
                    self.assertEqual(memory.history_status("channel")["sent_ids"], i+1)
                finally:
                    memory.close()

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
