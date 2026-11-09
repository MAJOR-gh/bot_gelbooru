"""Conservative visual fingerprints for still images; versioned for persistent history."""
from io import BytesIO
import warnings
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_PIXELS = 20_000_000
CROP_PERCENTAGES = (2, 4, 6)


def fingerprint(data: bytes) -> dict | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as original:
                if getattr(original, "is_animated", False) or original.width * original.height > MAX_PIXELS:
                    return None
                image = ImageOps.exif_transpose(original).convert("RGB")
                result = _signature(image)
                if result is not None:
                    mirrored = _signature(ImageOps.mirror(image))
                    if mirrored is not None:
                        result["mirror"] = mirrored
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


def _similar_direct(a: dict, b: dict) -> bool:
    if a["version"] != b["version"] or abs(a["ratio"] / b["ratio"] - 1) > .02:
        return False
    if (int(a["h"], 16) ^ int(b["h"], 16)).bit_count() > 6:
        return False
    if (int(a["v"], 16) ^ int(b["v"], 16)).bit_count() > 6:
        return False
    aa, bb = bytes.fromhex(a["color"]), bytes.fromhex(b["color"])
    return sum((x-y)**2 for x, y in zip(aa, bb)) / len(aa) <= 100


def similar(a: dict, b: dict) -> bool:
    # Optional mirror keeps existing v1 history comparable without reindexing.
    return (_similar_direct(a, b)
            or ("mirror" in a and _similar_direct(a["mirror"], b))
            or ("mirror" in b and _similar_direct(a, b["mirror"]))
            or any(_similar_direct(crop, b) for crop in a.get("crops", ()))
            or any(_similar_direct(a, crop) for crop in b.get("crops", ())))
