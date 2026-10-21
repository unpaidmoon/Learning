"""Generate only the explicitly defined V3 synthetic images; no media acquisition."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
WIDTH, HEIGHT = 1440, 900
FONT_SIZE = 46


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wrapped_lines(text: str, font: ImageFont.FreeTypeFont, limit: int) -> list[str]:
    result, current = [], ""
    for char in text:
        candidate = current + char
        if current and font.getlength(candidate) > limit:
            result.append(current)
            current = char
        else:
            current = candidate
    if current:
        result.append(current)
    return result


def generate(output_dir: Path, font_path: Path = Path("C:/Windows/Fonts/msyh.ttc")) -> dict:
    dataset_path = HERE / "cases.json"
    dataset = json.loads(dataset_path.read_text("utf-8"))
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError("OUTPUT_ALREADY_EXISTS_USE_A_NEW_RUN_DIRECTORY")
    if not font_path.is_file():
        raise FileNotFoundError("FIXTURE_FONT_MISSING")
    font = ImageFont.truetype(str(font_path), FONT_SIZE)
    small = ImageFont.truetype(str(font_path), 28)
    exponent = ImageFont.truetype(str(font_path), 30)
    output_dir.mkdir(parents=True)
    samples = []
    for case in dataset["cases"]:
        if not case["executable"]:
            continue
        image = Image.new("RGB", (WIDTH, HEIGHT), "#ffffff")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((28, 28, WIDTH - 28, HEIGHT - 28), radius=20, outline="#cadaee", width=2)
        draw.text((66, 68), f"{case['id']} · 合成数学题", font=small, fill="#285a94")
        y = 180
        missing_symbol = None
        for line in case["image_lines"]:
            if case["id"] == "B01" and "无法辨认" in line:
                prefix = "lim (sin x − x cos x) / x"
                draw.text((66, y), prefix, font=font, fill="#142d47")
                x = round(66 + font.getlength(prefix)) + 5
                missing_symbol = [x, y - 15, x + 143, y + 30]
                draw.rectangle(tuple(missing_symbol), outline="#9a4b30", fill="#fff4ed", width=2)
                draw.text((x + 4, y - 10), "无法辨认", font=exponent, fill="#803c27")
                y += 86
                continue
            for part in wrapped_lines(line, font, WIDTH - 132):
                if y + 80 > HEIGHT - 100:
                    raise ValueError(f"FIXTURE_TEXT_OVERFLOW:{case['id']}")
                draw.text((66, y), part, font=font, fill="#142d47")
                y += 86
        draw.text((66, HEIGHT - 92), "本图为自生成题图，不含真实视频画面或手写。", font=small, fill="#667b91")
        destination = output_dir / f"{case['id']}.png"
        image.save(destination, format="PNG", optimize=False)
        sample = {
            "fixture_id": f"V3-{case['id']}",
            "case_id": case["id"],
            "revision": dataset["revision"],
            "source": "synthetic_generated_from_cases_json",
            "media_type": "image/png",
            "file": destination.name,
            "sha256": digest(destination),
            "width": WIDTH,
            "height": HEIGHT,
            "allowed_scope": {"x": 0, "y": 0, "width": WIDTH, "height": HEIGHT},
            "allowed_operations": ["local_view", "formal_application_confirmed_attachment", "real_model_input"],
            "question_sha256": hashlib.sha256(case["question"].encode("utf-8")).hexdigest(),
        }
        if missing_symbol is not None:
            sample["unreadable_exponent_bbox_xyxy"] = missing_symbol
            sample["unreadable_scope"] = "denominator_exponent_only_no_hidden_correct_exponent"
        samples.append(sample)
    manifest = {
        "revision": dataset["revision"],
        "source": "synthetic",
        "cases_sha256": digest(dataset_path),
        "generator_sha256": digest(Path(__file__)),
        "font": {"filename": font_path.name, "sha256": digest(font_path), "size_px": FONT_SIZE},
        "expected_review_status": dataset["expected_review_status"],
        "coordinate_system": "source_pixel_origin_top_left_xyxy_for_bbox",
        "samples": samples,
        "not_generated": [
            {"case_id": case["id"], "reason": case["untested_reason"]}
            for case in dataset["cases"]
            if not case["executable"]
        ],
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/msyh.ttc"))
    args = parser.parse_args()
    manifest = generate(args.output_dir, args.font)
    print(json.dumps({"generated": len(manifest["samples"]), "skipped": len(manifest["not_generated"])}))


if __name__ == "__main__":
    main()
