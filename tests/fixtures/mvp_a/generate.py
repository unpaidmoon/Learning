"""Generate synthetic media only; no user file or screen is read."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from PIL import Image, ImageDraw


def generate(directory: Path, *, ffmpeg: Path | None = None) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (640, 360), "#ff00ff")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 639, 44), fill="#00ffff")
    draw.rectangle((160, 90, 479, 269), fill="#d9f99d")
    draw.text((182, 110), "SYNTHETIC MATH ONLY", fill="#102510")
    draw.text((182, 145), "x^2 - 5x + 6 = 0", fill="#102510")
    draw.text((182, 180), "Expected roots: 2, 3", fill="#102510")
    draw.text((10, 315), "OUTSIDE CONFIRMED CROP - MUST NOT BE SENT", fill="#000000")
    frame = directory / "synthetic-frame.png"
    image.save(frame)
    for index, color in enumerate(("#93c5fd", "#fde68a"), 1):
        extra = Image.new("RGB", (240, 120), color)
        ImageDraw.Draw(extra).text((15, 40), f"SYNTHETIC PASTE {index}", fill="black")
        extra.save(directory / f"paste-{index}.png")
    command = ffmpeg or (Path(shutil.which("ffmpeg")) if shutil.which("ffmpeg") else None)
    if command is None or not command.is_file():
        raise RuntimeError("FIXTURE_ENCODER_MISSING: 用 --ffmpeg-path 指定现有编码器")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    version = subprocess.run(
        [str(command), "-version"], capture_output=True, text=True, check=True, **options
    ).stdout.splitlines()[0]
    video = directory / "synthetic-course.webm"
    subprocess.run(
        [
            str(command), "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+bitexact",
            "-loop", "1",
            "-framerate", "5", "-i", str(frame), "-t", "20", "-c:v", "libvpx",
            "-deadline", "realtime", "-threads", "1", "-b:v", "180k", "-flags:v", "+bitexact",
            "-map_metadata", "-1", "-fflags", "+bitexact", "-an", str(video),
        ],
        capture_output=True, check=True, timeout=30, **options,
    )
    record = {
        "scope": "generated synthetic colored coordinate fixture; no user media",
        "video": str(video.resolve()), "frame": str(frame.resolve()),
        "width": 640, "height": 360, "duration_seconds": 20,
        "crop": {"x": 160, "y": 90, "width": 320, "height": 180},
        "encoder": {"path": str(command), "version": version,
                    "sha256": hashlib.sha256(command.read_bytes()).hexdigest()},
        "files": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in directory.iterdir() if path.is_file()},
    }
    (directory / "manifest.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return record


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--ffmpeg-path", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(generate(arguments.directory, ffmpeg=arguments.ffmpeg_path),
                     ensure_ascii=False))
