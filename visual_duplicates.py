"""Conservative visual fingerprints for still images; versioned for persistent history."""
from io import BytesIO
import warnings
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_PIXELS = 20_000_000


def fingerprint(data: bytes) -> dict | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as original:
                if getattr(original, "is_animated", False) or original.width * original.height > MAX_PIXELS:
                    return None
                image = ImageOps.exif_transpose(original).convert("RGB")
                gray = image.convert("L")
                values = list(gray.resize((17, 16), Image.Resampling.LANCZOS).tobytes())
                bits = [values[y*17+x] > values[y*17+x+1] for y in range(16) for x in range(16)]
                if not 24 <= sum(bits) <= 232:
                    return None  # Flat/simple images have unreliable perceptual hashes.
                h = sum(int(b) << i for i, b in enumerate(bits))
                values = list(gray.resize((16, 17), Image.Resampling.LANCZOS).tobytes())
                v = sum(int(values[y*16+x] > values[(y+1)*16+x]) << (y*16+x)
                        for y in range(16) for x in range(16))
                color = image.resize((8, 8), Image.Resampling.LANCZOS).tobytes().hex()
                return {"version": 1, "h": hex(h), "v": hex(v),
                        "ratio": image.width / image.height, "color": color}
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        return None


def similar(a: dict, b: dict) -> bool:
    if a["version"] != b["version"] or abs(a["ratio"] / b["ratio"] - 1) > .02:
        return False
    if (int(a["h"], 16) ^ int(b["h"], 16)).bit_count() > 6:
        return False
    if (int(a["v"], 16) ^ int(b["v"], 16)).bit_count() > 6:
        return False
    aa, bb = bytes.fromhex(a["color"]), bytes.fromhex(b["color"])
    return sum((x-y)**2 for x, y in zip(aa, bb)) / len(aa) <= 100
