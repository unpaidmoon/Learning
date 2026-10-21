"""Generate bounded synthetic course assets for the redesigned MVP-A.

This module reads no user course, screen or secret. The existing deterministic
video generator remains the single source for the colored coordinate fixture.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[3]


def generate(directory: Path, *, ffmpeg: Path | None = None) -> dict:
    directory.mkdir(parents=True, exist_ok=False)
    spec = importlib.util.spec_from_file_location(
        "redesign_video_fixture", ROOT / "tests/fixtures/mvp_a/generate.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("SYNTHETIC_GENERATOR_MISSING")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    media = module.generate(directory / "media", ffmpeg=ffmpeg)
    # Keep the deterministic source frame, but use a lesson-length copy so that
    # real CLI interaction latency cannot turn playback assertions into end-of-file tests.
    encoder = Path(media["encoder"]["path"])
    long_video = directory / "media/synthetic-lesson.webm"
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    subprocess.run(
        [str(encoder), "-hide_banner", "-loglevel", "error", "-y", "-stream_loop", "5",
         "-i", media["video"], "-t", "120", "-c", "copy", "-map_metadata", "-1",
         "-fflags", "+bitexact", str(long_video)],
        capture_output=True, check=True, timeout=30, **options,
    )

    course = directory / "合成数学课程 A"
    course.mkdir()
    (course / "子目录").mkdir()
    (course / "说明.txt").write_text("合成资料，仅验证文件及标签行为。\n", encoding="utf-8")
    (course / "公式.md").write_text(
        "# 合成数学资料\n\n已知 $x^2-5x+6=0$，求解。\n\n"
        "这份文件只用于操作验证，不表示模型已作答。\n",
        encoding="utf-8",
    )
    shutil.copyfile(long_video, course / "synthetic-course.webm")
    shutil.copyfile(long_video, course / "rename-source.webm")
    shutil.copyfile(media["frame"], course / "synthetic-frame.png")
    (course / "不支持的格式.fixture").write_bytes(b"SYNTHETIC-UNSUPPORTED-FORMAT\n")

    imports = directory / "external-imports"
    imports.mkdir()
    (imports / "move-me.txt").write_text("SYNTHETIC-MOVE\n", encoding="utf-8")
    (imports / "copy-me.txt").write_text("SYNTHETIC-COPY\n", encoding="utf-8")
    (imports / "ui-import.txt").write_text("SYNTHETIC-UI-IMPORT\n", encoding="utf-8")
    (course / "冲突.txt").write_text("SYNTHETIC-ORIGINAL\n", encoding="utf-8")
    collisions = directory / "external-collisions"
    collisions.mkdir()
    for name, text in (("冲突.txt", "SYNTHETIC-REPLACEMENT\n"),):
        (collisions / name).write_text(text, encoding="utf-8")
    outside = directory / "outside-sentinel.txt"
    outside.write_text("SYNTHETIC-OUTSIDE-DO-NOT-READ-OR-CHANGE\n", encoding="utf-8")
    course_b = directory / "合成课程 B"
    course_b.mkdir()
    (course_b / "说明.txt").write_text("第二门合成课程，不包含任何真实资料。\n", encoding="utf-8")

    files = {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }
    manifest = {
        "revision": "mvp-a-redesign-synthetic-r1",
        "scope": "all generated; no user files, real screen or secret read",
        "directory": str(directory.resolve()),
        "course_root": str(course.resolve()),
        "course_b_root": str(course_b.resolve()),
        "media": media,
        "course_video_duration_seconds": 120,
        "external_imports": str(imports.resolve()),
        "external_collisions": str(collisions.resolve()),
        "outside_sentinel": str(outside.resolve()),
        "files": files,
        "model_request_images": "only the later user-confirmed browser selection",
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--ffmpeg-path", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(generate(arguments.directory, ffmpeg=arguments.ffmpeg_path), ensure_ascii=False))
