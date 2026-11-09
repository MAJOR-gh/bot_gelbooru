"""Conservative visual fingerprints for still images; versioned for persistent history."""
import hashlib
import warnings
from io import BytesIO
from math import cos, pi
from statistics import median

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_PIXELS = 20_000_000
CROP_PERCENTAGES = (2, 4, 6)
PHASH_SIZE = 32
PHASH_LOW_FREQUENCIES = 8
PHASH_MAX_DISTANCE = 6
_DCT_BASIS = tuple(
    tuple(cos(pi * frequency * (position + 0.5) / PHASH_SIZE)
          for position in range(PHASH_SIZE))
    for frequency in range(PHASH_LOW_FREQUENCIES)
)


def fingerprint(data: bytes) -> dict | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as original:
                if getattr(original, "is_animated", False) or original.width * original.height > MAX_PIXELS:
                    return None
                ImageOps.exif_transpose(original, in_place=True)
                result = {"version": 1, "pixels": _pixel_digest(original)}
                image = original.convert("RGB")
                base = _signature(image)
                if base is not None:
                    result.update(base)
                    result["phash"] = _phash(image)
                    mirror_image = ImageOps.mirror(image)
                    mirrored = _signature(mirror_image)
                    if mirrored is not None:
                        mirrored["phash"] = _phash(mirror_image)
                        result["mirror"] = mirrored
                    del mirror_image
                    crops = []
                    for percentage in CROP_PERCENTAGES:
                        x = round(image.width * percentage / 100)
                        y = round(image.height * percentage / 100)
                        if x or y:
                            crop = _signature(image.crop((x, y, image.width - x, image.height - y)))
                            if crop is not None:
                                crops.append(crop)
                    if crops:
                        result["crops"] = crops
                return result
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        return None


def _pixel_digest(image: Image.Image) -> str:
    digest = hashlib.sha256(f"{image.width}x{image.height}:".encode("ascii"))
    # Hash RGBA in strips to avoid an additional full-size pixel buffer.
    rows_per_strip = max(1, 262_144 // image.width)
    for top in range(0, image.height, rows_per_strip):
        strip = image.crop((0, top, image.width, min(image.height, top + rows_per_strip)))
        digest.update(strip.convert("RGBA").tobytes())
    return digest.hexdigest()


def _signature(image: Image.Image) -> dict | None:
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


def _phash(image: Image.Image) -> str:
    gray = image.convert("L").resize((PHASH_SIZE, PHASH_SIZE), Image.Resampling.LANCZOS)
    pixels = gray.tobytes()
    rows = [pixels[start:start + PHASH_SIZE] for start in range(0, len(pixels), PHASH_SIZE)]
    columns = [
        [sum(pixel * weight for pixel, weight in zip(row, basis)) for row in rows]
        for basis in _DCT_BASIS
    ]
    # Exclude the DC coefficient, which represents average brightness.
    coefficients = [sum(value * weight for value, weight in zip(column, basis))
                    for column in columns for basis in _DCT_BASIS][1:]
    midpoint = median(coefficients)
    return hex(sum(int(value > midpoint) << i for i, value in enumerate(coefficients)))


def _distance(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def _similar_direct(a: dict, b: dict) -> bool:
    if "h" not in a or "h" not in b or abs(a["ratio"] / b["ratio"] - 1) > .02:
        return False
    matches_dhash = _distance(a["h"], b["h"]) <= 6 and _distance(a["v"], b["v"]) <= 6
    if not matches_dhash and not (
            "phash" in a and "phash" in b
            and _distance(a["phash"], b["phash"]) <= PHASH_MAX_DISTANCE):
        return False
    aa, bb = bytes.fromhex(a["color"]), bytes.fromhex(b["color"])
    return sum((x-y)**2 for x, y in zip(aa, bb)) / len(aa) <= 100


def similar(a: dict, b: dict) -> bool:
    if a["version"] != b["version"]:
        return False
    if "pixels" in a and a["pixels"] == b.get("pixels"):
        return True
    return (_similar_direct(a, b)
            or ("mirror" in a and _similar_direct(a["mirror"], b))
            or ("mirror" in b and _similar_direct(a, b["mirror"]))
            or any(_similar_direct(crop, b) for crop in a.get("crops", ()))
            or any(_similar_direct(a, crop) for crop in b.get("crops", ())))
