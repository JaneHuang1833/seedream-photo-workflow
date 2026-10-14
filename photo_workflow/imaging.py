"""Conservative, deterministic photo finishing without generative pixel changes.

Recipe values are deliberately small. All output pixels come from the source
photo; color adjustment, mild noise reduction, and sharpening are the only
operations. Originals are never overwritten.
"""

from __future__ import annotations

import io
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

import numpy as np
from PIL import ExifTags, Image, ImageCms, ImageFilter, ImageOps


_PARAMS = {
    "exposure_ev": (-0.6, 0.6, 0.0),
    "temperature": (-1.0, 1.0, 0.0),
    "tint": (-1.0, 1.0, 0.0),
    "contrast": (-0.18, 0.18, 0.0),
    "highlights": (-0.6, 0.6, 0.0),
    "shadows": (-0.6, 0.6, 0.0),
    "saturation": (-0.18, 0.18, 0.0),
    "vibrance": (-0.3, 0.3, 0.0),
    "sharpen": (0.0, 1.0, 0.12),
    "denoise": (0.0, 1.0, 0.0),
    "subject_exposure_ev": (-0.35, 0.35, 0.0),
    "face_exposure_ev": (0.0, 0.35, 0.0),
    "vignette": (0.0, 0.25, 0.0),
    "autumn_boost": (0.0, 0.3, 0.0),
}
_CATEGORIES = {"landscape", "portrait", "environmental_portrait", "detail"}
_PORTRAITS = {"portrait", "environmental_portrait"}
_PROFILES = {"natural", "autumn_landscape", "poetic_detail", "bright_portrait", "story_documentary"}
_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
_SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
_SRGB_BYTES = _SRGB.tobytes()
_SRGB_LEVELS = np.arange(256, dtype=np.float32) / np.float32(255.0)
_TO_LINEAR = np.where(
    _SRGB_LEVELS <= 0.04045,
    _SRGB_LEVELS / 12.92,
    ((_SRGB_LEVELS + 0.055) / 1.055) ** 2.4,
).astype(np.float32)


def _finite_number(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _short_text(value: Any, limit: int = 1200) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _normalize_box(value: Any, *, face: bool = False) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(isinstance(number, bool) or not isinstance(number, (float, int)) for number in value):
        return None
    try:
        box = [float(number) for number in value]
    except (OverflowError, ValueError):
        return None
    if any(not math.isfinite(number) or number < 0.0 or number > 1.0 for number in box):
        return None
    x0, y0, x1, y1 = box
    area = (x1 - x0) * (y1 - y0)
    if x1 <= x0 or y1 <= y0 or area < (0.0001 if face else 0.0004):
        return None
    if face and area > 0.45:
        return None
    return [round(number, 6) for number in box]


def normalize_recipe(raw: Any, style: str = "natural") -> dict[str, Any]:
    """Validate untrusted model output, replacing invalid values with defaults.

    Natural portraits limit positive saturation, vibrance, and contrast. Missing
    tone parameters are neutral; missing sharpening is a subtle 0.12. Supply an
    explicit ``sharpen: 0`` for a completely neutral recipe.
    """
    if not isinstance(raw, Mapping):
        raw = {}
    params_raw = raw.get("params", {})
    if not isinstance(params_raw, Mapping):
        params_raw = {}
    category = raw.get("category", "landscape")
    if not isinstance(category, str) or category not in _CATEGORIES:
        category = "landscape"
    profile = raw.get("profile", "natural")
    if not isinstance(profile, str) or profile not in _PROFILES:
        profile = "natural"
    params = {}
    for name, (minimum, maximum, default) in _PARAMS.items():
        value = _finite_number(params_raw.get(name, default), default)
        params[name] = round(min(maximum, max(minimum, value)), 6)
    if style == "natural" and category in _PORTRAITS:
        params["saturation"] = min(params["saturation"], 0.06)
        params["vibrance"] = min(params["vibrance"], 0.12)
        params["contrast"] = min(params["contrast"], 0.10)
    if category in _PORTRAITS:
        # The simple warm-color detector cannot distinguish autumn leaves from
        # every skin tone; portrait recipes therefore omit autumn enhancement.
        params["autumn_boost"] = 0.0
    subject_box = _normalize_box(raw.get("subject_box"))
    boxes_raw = raw.get("face_boxes", [])
    face_boxes = []
    if isinstance(boxes_raw, (list, tuple)):
        for value in boxes_raw[:32]:
            box = _normalize_box(value, face=True)
            if box is not None and box not in face_boxes:
                face_boxes.append(box)
            if len(face_boxes) == 8:
                break
    issues = raw.get("issues", [])
    if not isinstance(issues, (list, tuple)):
        issues = []
    return {
        "category": category,
        "profile": profile,
        "style": _short_text(style, 60) or "natural",
        "subject_box": subject_box,
        "face_boxes": face_boxes,
        "summary": _short_text(raw.get("summary")),
        "issues": [_short_text(item, 200) for item in issues[:20] if isinstance(item, str)],
        "reasoning": _short_text(raw.get("reasoning")),
        "params": params,
    }


def inspect_image(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Return image metadata; width and height include EXIF orientation."""
    path = Path(path)
    with Image.open(path) as image:
        exif = image.getexif()
        orientation = int(_finite_number(exif.get(274, 1), 1))
        width, height = image.size
        if orientation in (5, 6, 7, 8):
            width, height = height, width
        return {
            "path": str(path.resolve()),
            "width": width,
            "height": height,
            "source_width": image.width,
            "source_height": image.height,
            "format": image.format,
            "mode": image.mode,
            "size_bytes": path.stat().st_size,
            "has_icc": bool(image.info.get("icc_profile")),
            "orientation": orientation,
            "camera": " ".join(
                _short_text(exif.get(tag), 120).strip() for tag in (271, 272)
                if isinstance(exif.get(tag), str)
            ),
            "date": _short_text(exif.get(306), 60),
        }


def _clean_exif(original: Image.Exif, size: tuple[int, int]) -> bytes:
    """Rebuild EXIF without GPS, orientation, or an obsolete thumbnail IFD."""
    clean = Image.Exif()
    # Building a fresh IFD discards original IFD1 thumbnail offsets and data.
    excluded = {274, 34853, 513, 514, 330}
    for tag, value in original.items():
        if tag in excluded or tag == 34665:
            continue
        clean[tag] = value
    if 256 in clean:
        clean[256] = size[0]
    if 257 in clean:
        clean[257] = size[1]
    if 34665 in original:
        inner = dict(original.get_ifd(ExifTags.IFD.Exif))
        inner.pop(34853, None)
        inner.pop(274, None)
        inner.pop(513, None)
        inner.pop(514, None)
        if 40962 in inner:
            inner[40962] = size[0]
        if 40963 in inner:
            inner[40963] = size[1]
        # Pixels have been transformed into sRGB regardless of source profile.
        if 40961 in inner:
            inner[40961] = 1
        if 40965 in inner:
            interop = dict(original.get_ifd(ExifTags.IFD.Interop))
            if interop:
                inner[40965] = interop
            else:
                inner.pop(40965, None)
        clean[34665] = inner
    return clean.tobytes() if clean else b""


def _load_rgb(path: str | os.PathLike[str]) -> tuple[Image.Image, bytes, list[str]]:
    warnings = []
    with Image.open(path) as original:
        original.load()
        original_exif = original.getexif()
        image = ImageOps.exif_transpose(original)
        profile = original.info.get("icc_profile")
        if profile:
            try:
                input_profile = ImageCms.ImageCmsProfile(io.BytesIO(profile))
                if image.mode not in ("RGB", "RGBA", "CMYK", "LAB"):
                    image = image.convert("RGB")
                image = ImageCms.profileToProfile(image, input_profile, _SRGB, outputMode="RGB")
            except (ImageCms.PyCMSError, OSError, ValueError, TypeError):
                warnings.append("Unreadable or incompatible ICC profile; interpreted as sRGB.")
                if image.mode != "RGB":
                    image = image.convert("RGB")
        elif image.mode in ("RGBA", "LA") or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            image = Image.alpha_composite(background, rgba).convert("RGB")
        elif image.mode != "RGB":
            image = image.convert("RGB")
        try:
            exif = _clean_exif(original_exif, image.size)
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            warnings.append("Malformed EXIF could not be preserved; output contains no EXIF.")
            exif = b""
    return image, exif, warnings


def _edge_limit(max_edge: int) -> int:
    if isinstance(max_edge, bool) or not isinstance(max_edge, int) or max_edge < 2:
        raise ValueError("max_edge must be an integer of at least 2")
    return max_edge


def make_preview(path: str | os.PathLike[str], max_edge: int = 1280) -> bytes:
    """Return an oriented sRGB JPEG with no source EXIF or GPS metadata."""
    max_edge = _edge_limit(max_edge)
    image, _, _ = _load_rgb(path)
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, "JPEG", quality=95, subsampling=0, icc_profile=_SRGB_BYTES)
    return output.getvalue()


def _guard_destination(source: Path, destination: Path) -> None:
    if source.resolve() == destination.resolve():
        raise ValueError("The destination must be different from the original photo")
    if destination.exists() and os.path.samefile(source, destination):
        raise ValueError("The destination points to the original photo")


def _save_jpeg(image: Image.Image, destination: Path, exif: bytes = b"") -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=".photo-", suffix=".jpg", dir=destination.parent)
    os.close(handle)
    try:
        image.save(
            temporary_name, "JPEG", quality=95, subsampling=0,
            icc_profile=_SRGB_BYTES, exif=exif,
        )
        os.replace(temporary_name, destination)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _to_srgb(linear: np.ndarray) -> np.ndarray:
    np.clip(linear, 0.0, 1.0, out=linear)
    return np.where(
        linear <= 0.0031308,
        linear * np.float32(12.92),
        np.float32(1.055) * np.power(linear, np.float32(1.0 / 2.4)) - np.float32(0.055),
    ).astype(np.float32, copy=False)


def _ellipse_mask(
    box: list[float], x: np.ndarray, y: np.ndarray,
) -> np.ndarray:
    x0, y0, x1, y1 = box
    distance = ((x - (x0 + x1) / 2) / ((x1 - x0) / 2)) ** 2
    distance = distance + ((y - (y0 + y1) / 2) / ((y1 - y0) / 2)) ** 2
    mask = np.exp(-np.float32(2.0) * distance).astype(np.float32, copy=False)
    mask[distance > 4.0] = 0.0
    return mask


def _skin_weight(encoded: np.ndarray) -> np.ndarray:
    r, g, b = encoded[..., 0], encoded[..., 1], encoded[..., 2]
    return (
        np.clip((r - b - 0.025) / 0.14, 0.0, 1.0)
        * np.clip((r - g + 0.025) / 0.10, 0.0, 1.0)
        * np.clip((g - b + 0.025) / 0.08, 0.0, 1.0)
    )


def _finish_pixels(image: Image.Image, recipe: dict[str, Any]) -> Image.Image:
    params = recipe["params"]
    tone_names = set(_PARAMS) - {"sharpen", "denoise"}
    has_tone = any(params[name] != 0 for name in tone_names)
    denoise = params["denoise"]
    if not has_tone and denoise == 0:
        finished = image
    else:
        source = np.asarray(image, dtype=np.uint8)
        output = np.empty_like(source)
        median = np.asarray(image.filter(ImageFilter.MedianFilter(3))) if denoise else None
        # Bounded tiles keep 20 MP images from needing several full float copies.
        rows_per_tile = max(1, min(512, 250_000 // image.width))
        temperature, tint = params["temperature"], params["tint"]
        white_balance = np.exp(np.array([
            0.08 * temperature + 0.02 * tint,
            -0.04 * tint,
            -0.08 * temperature + 0.02 * tint,
        ], dtype=np.float32))
        white_balance /= np.dot(white_balance, _LUMA)
        portrait = recipe["category"] in _PORTRAITS
        x_coordinates = ((np.arange(image.width, dtype=np.float32) + 0.5) / image.width)[None, :]
        for y in range(0, image.height, rows_per_tile):
            tile = source[y:y + rows_per_tile]
            y_coordinates = ((np.arange(y, min(y + rows_per_tile, image.height), dtype=np.float32) + 0.5) / image.height)[:, None]
            if median is not None:
                # At maximum strength blend 40% median; no noise generation.
                encoded = tile.astype(np.float32) / np.float32(255.0)
                encoded += np.float32(denoise * 0.4) * (
                    median[y:y + rows_per_tile].astype(np.float32) / np.float32(255.0) - encoded
                )
                linear = np.where(
                    encoded <= 0.04045, encoded / 12.92,
                    ((encoded + 0.055) / 1.055) ** 2.4,
                ).astype(np.float32, copy=False)
            else:
                linear = _TO_LINEAR[tile]
            if has_tone:
                linear *= white_balance
                luminance = linear @ _LUMA
                exposed_luminance = luminance * np.float32(2.0 ** params["exposure_ev"])
                shadow_weight = np.clip(1.0 - exposed_luminance / 0.55, 0.0, 1.0) ** 2
                highlight_weight = np.clip((exposed_luminance - 0.35) / 0.65, 0.0, 1.0) ** 2
                gain_ev = (
                    np.float32(params["exposure_ev"])
                    + np.float32(params["shadows"]) * shadow_weight
                    + np.float32(params["highlights"] * 0.85) * highlight_weight
                )
                subject_mask = None
                if recipe["subject_box"] is not None:
                    subject_mask = _ellipse_mask(recipe["subject_box"], x_coordinates, y_coordinates)
                highlight_protection = np.clip((0.95 - exposed_luminance) / 0.55, 0.0, 1.0)
                if params["subject_exposure_ev"] and subject_mask is not None:
                    local = np.float32(params["subject_exposure_ev"]) * subject_mask
                    if params["subject_exposure_ev"] > 0:
                        local *= highlight_protection
                    gain_ev += local
                if params["face_exposure_ev"] and recipe["face_boxes"]:
                    face_mask = np.zeros(tile.shape[:2], dtype=np.float32)
                    for box in recipe["face_boxes"]:
                        np.maximum(face_mask, _ellipse_mask(box, x_coordinates, y_coordinates), out=face_mask)
                    encoded_source = tile.astype(np.float32) / np.float32(255.0)
                    gain_ev += np.float32(params["face_exposure_ev"]) * face_mask * _skin_weight(encoded_source) * highlight_protection
                if params["vignette"]:
                    radius = ((x_coordinates - 0.5) * 2.0) ** 2 + ((y_coordinates - 0.5) * 2.0) ** 2
                    vignette_weight = np.clip((radius - 0.2) / 1.8, 0.0, 1.0) ** 1.2
                    if subject_mask is not None:
                        vignette_weight = np.maximum(vignette_weight * 0.65, 1.0 - subject_mask)
                    gain_ev -= np.float32(params["vignette"] * 1.2) * vignette_weight
                gain = np.exp2(gain_ev)
                linear *= gain[..., None]
            encoded = _to_srgb(linear)
            if params["contrast"]:
                luminance = encoded @ _LUMA
                power = np.float32(1.0 + params["contrast"])
                lo = np.power(luminance, power)
                hi = np.power(1.0 - luminance, power)
                target = lo / np.maximum(lo + hi, np.float32(1e-8))
                encoded *= (target / np.maximum(luminance, np.float32(1e-8)))[..., None]
            if params["saturation"] or params["vibrance"] or params["autumn_boost"]:
                maximum = encoded.max(axis=-1)
                minimum = encoded.min(axis=-1)
                saturation = (maximum - minimum) / np.maximum(maximum, np.float32(1e-8))
                change = np.float32(params["saturation"]) + (
                    np.float32(params["vibrance"] * 0.65) * (1.0 - saturation) ** 2
                )
                if portrait:
                    r, g, b = encoded[..., 0], encoded[..., 1], encoded[..., 2]
                    warm_pixels = (r > g) & (g >= b * 0.9) & ((r - b) > 0.03)
                    # Protect warm skin hues from added color intensity.
                    change = np.where(warm_pixels & (change > 0), change * 0.65, change)
                if params["autumn_boost"]:
                    r, g, b = encoded[..., 0], encoded[..., 1], encoded[..., 2]
                    warm_leaves = (
                        np.clip((r - b - 0.03) / 0.15, 0.0, 1.0)
                        * np.clip((r - g + 0.03) / 0.08, 0.0, 1.0)
                        * np.clip((g - b + 0.02) / 0.08, 0.0, 1.0)
                        * np.clip(saturation / 0.20, 0.0, 1.0)
                        * np.clip((1.0 - saturation) / 0.35, 0.0, 1.0)
                    )
                    change += np.float32(params["autumn_boost"] * 0.35) * warm_leaves
                luminance = encoded @ _LUMA
                encoded = luminance[..., None] + (encoded - luminance[..., None]) * (1.0 + change[..., None])
            np.clip(encoded, 0.0, 1.0, out=encoded)
            output[y:y + rows_per_tile] = np.rint(encoded * 255.0).astype(np.uint8)
        finished = Image.fromarray(output)
    if params["sharpen"]:
        percent = round(params["sharpen"] * 65)
        if percent:
            finished = finished.filter(ImageFilter.UnsharpMask(radius=0.9, percent=percent, threshold=3))
    return finished


def apply_recipe(
    source: str | os.PathLike[str], dest: str | os.PathLike[str], recipe: Any,
) -> dict[str, Any]:
    """Write a full-resolution, oriented sRGB JPEG and return processing facts."""
    source, destination = Path(source), Path(dest)
    _guard_destination(source, destination)
    style = recipe.get("style", "natural") if isinstance(recipe, Mapping) else "natural"
    normalized = normalize_recipe(recipe, style=style if isinstance(style, str) else "natural")
    image, exif, warnings = _load_rgb(source)
    finished = _finish_pixels(image, normalized)
    _save_jpeg(finished, destination, exif)
    return {
        "destination": str(destination.resolve()),
        "width": finished.width,
        "height": finished.height,
        "size_bytes": destination.stat().st_size,
        "recipe": normalized,
        "processing": {
            "output_format": "JPEG", "quality": 95, "color_space": "sRGB",
            "orientation_applied": True, "gps_removed": True,
            "generative_edit": False, "warnings": warnings,
        },
    }


def make_comparison(
    source: str | os.PathLike[str], edited: str | os.PathLike[str],
    dest: str | os.PathLike[str], max_edge: int = 1200,
) -> None:
    """Create original-left / edited-right JPEG, with no photo metadata."""
    max_edge = _edge_limit(max_edge)
    destination = Path(dest)
    _guard_destination(Path(source), destination)
    _guard_destination(Path(edited), destination)
    original, _, _ = _load_rgb(source)
    result, _, _ = _load_rgb(edited)
    gap = min(8, max_edge // 4)
    # Compare at equal display height even when the generated photo has fewer
    # source pixels. A shared pixel scale would make the lower-resolution edit
    # look like a tiny thumbnail beside its full-resolution original.
    aspect_ratios = (original.width / original.height, result.width / result.height)
    display_height = max(1, int(min(
        original.height, result.height, max_edge,
        (max_edge - gap) / sum(aspect_ratios),
    )))
    resized = []
    for image, ratio in zip((original, result), aspect_ratios):
        size = (max(1, int(display_height * ratio)), display_height)
        resized.append(image.resize(size, Image.Resampling.LANCZOS))
    canvas = Image.new("RGB", (sum(image.width for image in resized) + gap, max(image.height for image in resized)), "white")
    canvas.paste(resized[0], (0, (canvas.height - resized[0].height) // 2))
    canvas.paste(resized[1], (resized[0].width + gap, (canvas.height - resized[1].height) // 2))
    _save_jpeg(canvas, destination)
