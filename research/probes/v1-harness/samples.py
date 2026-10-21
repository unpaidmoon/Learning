"""Deterministic, synthetic V1 image fixtures; never reads video or screens.

Only the M01/L02 images and IMG-SCOPE's confirmed crop are model inputs.
The IMG-SCOPE source is retained locally to test the crop boundary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


REVISION = "v1-synthetic-2026-10-21-r1"
TEXT_FONT = Path("C:/Windows/Fonts/msyh.ttc")
MATH_FONT = Path("C:/Windows/Fonts/cambria.ttc")
INK = "#151b26"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    if not path.is_file():
        raise FileNotFoundError(f"Required fixture font is absent: {path}")
    return ImageFont.truetype(str(path), size=size, index=0)


def _text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str,
          font: ImageFont.FreeTypeFont, fill: str = INK) -> None:
    # Anchor at the ink box top so the formula layout does not depend on ascent.
    draw.text(xy, text, font=font, fill=fill, anchor="lt")


def _center(draw: ImageDraw.ImageDraw, center_x: int, top: int, text: str,
            font: ImageFont.FreeTypeFont) -> None:
    width = draw.textlength(text, font=font)
    _text(draw, (round(center_x - width / 2), top), text, font)


def _metadata(path: Path, image: Image.Image, fixture_id: str,
              confirmed_scope: dict, expected: dict | None,
              model_input_allowed: bool = True) -> dict:
    image.save(path, format="PNG", optimize=False, compress_level=9)
    return {
        "fixture_id": fixture_id,
        "revision": REVISION,
        "path": str(path.resolve()),
        "sha256": _digest(path),
        "size": list(image.size),
        "media_type": "image/png",
        "file_bytes": path.stat().st_size,
        "source": "Deterministic Pillow drawing from the approved sample text",
        "synthetic": True,
        "model_input_allowed": model_input_allowed,
        "confirmed_scope": confirmed_scope,
        "expected": expected,
        "assistant_review": {
            "review_type": "independent_symbolic_derivation",
            "reviewed_by": "assistant",
            "human_review_ref": None,
        } if expected else None,
        "human_ref": None,
    }


def generate(output_dir: str | Path) -> dict:
    """Write fixtures below output_dir/private/samples and return a manifest.

    Re-running against unchanged Pillow/fonts produces identical PNG bytes.
    Expected answers remain metadata; they are not drawn into model inputs.
    Human review fields deliberately remain null until the runner records one.
    """
    folder = Path(output_dir).resolve() / "private" / "samples"
    folder.mkdir(parents=True, exist_ok=True)
    title = _font(TEXT_FONT, 44)
    body = _font(TEXT_FONT, 38)
    label = _font(TEXT_FONT, 24)
    math = _font(MATH_FONT, 76)
    small_math = _font(MATH_FONT, 42)

    m01 = Image.new("RGB", (1400, 620), "white")
    draw = ImageDraw.Draw(m01)
    _text(draw, (70, 52), "M01 · 极限", title)
    _text(draw, (70, 135), "求下列极限，并说明推导过程。", body)
    _text(draw, (210, 298), "lim", math)
    _center(draw, 263, 384, "x → 0", small_math)
    _center(draw, 800, 269, "sin x − x cos x", math)
    draw.line((490, 357, 1110, 357), fill=INK, width=4)
    _center(draw, 800, 385, "x³", math)
    _text(draw, (70, 558), "合成试题 · 无真实视频或屏幕内容", label, "#626c7a")
    m01_expected = {
        "limit": "1/3",
        "corrected_denominator": "2*x^3",
        "corrected_limit": "1/6",
        "derivation": "sin(x)-x*cos(x)=x^3/3+O(x^5) as x approaches 0",
    }
    m01_meta = _metadata(folder / "M01.png", m01, "M01",
                         {"kind": "full_synthetic_image", "bounds": [0, 0, 1400, 620],
                          "authorization_ref": "user V1 authorization 2026-10-21, M01/L02"},
                         m01_expected)

    l02 = Image.new("RGB", (1400, 620), "white")
    draw = ImageDraw.Draw(l02)
    _text(draw, (70, 52), "L02 · 线性代数", title)
    _text(draw, (70, 135), "求矩阵 A 的特征值及其相应的特征向量。", body)
    _text(draw, (360, 330), "A =", math)
    left, right, top, bottom = 600, 960, 255, 480
    draw.line([(left + 24, top), (left, top), (left, bottom),
               (left + 24, bottom)], fill=INK, width=5)
    draw.line([(right - 24, top), (right, top), (right, bottom),
               (right - 24, bottom)], fill=INK, width=5)
    for x, y, number in [(690, 278, "2"), (865, 278, "1"),
                         (690, 390, "1"), (865, 390, "2")]:
        _center(draw, x, y, number, math)
    _text(draw, (70, 558), "合成试题 · 无真实视频或屏幕内容", label, "#626c7a")
    l02_meta = _metadata(folder / "L02.png", l02, "L02",
                         {"kind": "full_synthetic_image", "bounds": [0, 0, 1400, 620],
                          "authorization_ref": "user V1 authorization 2026-10-21, M01/L02"},
                         {"eigenpairs": [{"value": 3, "direction": [1, 1]},
                                         {"value": 1, "direction": [1, -1]}],
                          "allow_nonzero_scalar_multiple": True,
                          "derivation": "det(A-lambda*I)=(lambda-3)*(lambda-1)"})

    scope = Image.new("RGB", (1400, 900), "#b92d42")
    draw = ImageDraw.Draw(scope)
    _text(draw, (45, 55), "OUTSIDE-UNCONFIRMED-R42", _font(MATH_FONT, 54), "white")
    _text(draw, (45, 790), "OUTSIDE-UNCONFIRMED-R42", _font(MATH_FONT, 54), "white")
    bounds = (260, 220, 1140, 680)
    draw.rectangle(bounds, fill="white")
    draw.rectangle((bounds[0] + 8, bounds[1] + 8, bounds[2] - 9, bounds[3] - 9),
                   outline="#1269bf", width=7)
    _text(draw, (310, 286), "INSIDE-CONFIRMED-A17", _font(MATH_FONT, 52))
    _text(draw, (310, 390), "x² + 1", math)
    _text(draw, (310, 540), "仅蓝框内的确认选区可提交", _font(TEXT_FONT, 34))
    source_meta = _metadata(folder / "IMG-SCOPE-source.png", scope, "IMG-SCOPE-source",
                           {"kind": "local_only_unconfirmed_source",
                            "authorized_model_crop": list(bounds)}, None, False)
    crop = scope.crop(bounds)
    crop_meta = _metadata(folder / "IMG-SCOPE-confirmed.png", crop, "IMG-SCOPE",
                         {"kind": "synthetic_confirmed_crop",
                          "source_fixture": "IMG-SCOPE-source",
                          "source_bounds": list(bounds),
                          "local_bounds": [0, 0, 880, 460],
                          "allowed_marker": "INSIDE-CONFIRMED-A17",
                          "forbidden_marker": "OUTSIDE-UNCONFIRMED-R42"}, None)
    crop_meta["source"] = source_meta
    manifest = {
        "revision": REVISION,
        "generator": "samples.py / Pillow",
        "fonts": [{"path": str(path), "sha256": _digest(path), "collection_index": 0}
                  for path in (TEXT_FONT, MATH_FONT)],
        "M01": m01_meta,
        "L02": l02_meta,
        "IMG-SCOPE": crop_meta,
        "H02": {"status": "not_available", "reason": "No user-provided real handwriting image"},
    }
    (folder / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(generate(args.output_dir), ensure_ascii=False, indent=2))
