"""Deterministic local assets for the second workspace interaction pass.

Every file is generated here or by the reviewed synthetic fixture. No user
course, real video, screen, clipboard or secret is read.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import zlib

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[3]


def text_pdf(path: Path) -> None:
    """Two small uncompressed PDF pages, each with a distinct text sentinel."""
    page_one = b"BT /F1 20 Tf 30 430 Td (SYNTHETIC PDF PAGE ONE - ALPHA 1729) Tj ET\n1 0 0 rg 30 100 180 200 re f\n"
    page_two = b"BT /F1 20 Tf 30 430 Td (SYNTHETIC PDF PAGE TWO - OMEGA 2718) Tj ET\n0 0 1 rg 300 100 180 200 re f\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 640 480] /Resources << /Font << /F1 7 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(page_one)).encode() + b" >>\nstream\n" + page_one + b"endstream",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 640 480] /Resources << /Font << /F1 7 0 R >> >> /Contents 6 0 R >>",
        b"<< /Length " + str(len(page_two)).encode() + b" >>\nstream\n" + page_two + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(content))
        content.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(content)
    content.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        content.extend(f"{offset:010} 00000 n \n".encode())
    content.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    path.write_bytes(content)


def image_pdf(path: Path, pages: list[Image.Image]) -> None:
    """Write image-only pages without depending on optional JPEG encoders."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    page_refs = []
    for image in pages:
        page_number = len(objects) + 1
        stream_number, image_number = page_number + 1, page_number + 2
        page_refs.append(f"{page_number} 0 R")
        content = b"q 640 0 0 480 0 0 cm /Synthetic Do Q\n"
        pixels = zlib.compress(image.convert("RGB").tobytes())
        objects.extend([
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 640 480] /Resources << /XObject << /Synthetic {image_number} 0 R >> >> /Contents {stream_number} 0 R >>".encode(),
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream",
            f"<< /Type /XObject /Subtype /Image /Width {image.width} /Height {image.height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length {len(pixels)} >>\nstream\n".encode() + pixels + b"\nendstream",
        ])
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(page_refs)}] /Count {len(pages)} >>".encode()
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(pdf)


def generate(directory: Path, *, ffmpeg: Path | None = None) -> dict:
    spec = importlib.util.spec_from_file_location(
        "workspace_v2_base", ROOT / "tests/fixtures/redesign/generate.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("SYNTHETIC_GENERATOR_MISSING")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = module.generate(directory, ffmpeg=ffmpeg)
    course = Path(manifest["course_root"])
    external = Path(manifest["external_imports"])
    documents = directory / "attachments"
    documents.mkdir()
    (external / "copy-collision.txt").write_text("SYNTHETIC EXTERNAL COPY 20261021\n", encoding="utf-8")
    (course / "copy-collision.txt").write_text("SYNTHETIC ORIGINAL MUST REMAIN\n", encoding="utf-8")
    (external / "drag-copy.txt").write_text("SYNTHETIC DRAG ONLY COPY\n", encoding="utf-8")
    (external / "clipboard-copy.txt").write_text("SYNTHETIC CLIPBOARD ONLY COPY\n", encoding="utf-8")
    (course / "internal-copy.txt").write_text("SYNTHETIC INTERNAL TRANSFER\n", encoding="utf-8")
    (course / "recycle-only.txt").write_text("SYNTHETIC RECYCLE BIN CASE\n", encoding="utf-8")
    (course / "子目录" / "nested-search.txt").write_text("SYNTHETIC NESTED FILE\n", encoding="utf-8")
    (documents / "whole-document.txt").write_text(
        "TEXT_START_1729\n合成整份文本附件。\n" + "middle line\n" * 30 + "TEXT_END_2718\n",
        encoding="utf-8",
    )
    (documents / "whole-document.md").write_text(
        "# MARKDOWN_START_3141\n\n矩阵 $M=\\begin{pmatrix}0&A\\\\B&E\\end{pmatrix}$。\n\nMARKDOWN_END_5926\n",
        encoding="utf-8",
    )
    text_pdf(documents / "two-page-text.pdf")
    pages = []
    for index, color in enumerate(("#ffe4e6", "#dbeafe"), 1):
        page = Image.new("RGB", (640, 480), "white")
        draw = ImageDraw.Draw(page)
        draw.rectangle((20, 50, 620, 450), fill=color)
        draw.text((40, 80), f"SYNTHETIC SCANNED PAGE {index}", fill="black")
        draw.text((40, 120), "ALPHA 1729" if index == 1 else "OMEGA 2718", fill="black")
        page.save(documents / f"scanned-source-{index}.png")
        pages.append(page)
    image_pdf(documents / "two-page-scanned.pdf", pages)
    image_pdf(documents / "thirty-three-pages.pdf", [pages[0], *([pages[1]] * 32)])
    (documents / "unsupported.docx").write_bytes(b"SYNTHETIC-UNSUPPORTED-OFFICE\n")
    encoder = Path(manifest["media"]["encoder"]["path"])
    frames = []
    for index, color in enumerate(("#dc2626", "#16a34a", "#2563eb"), 1):
        image = Image.new("RGB", (640, 360), color)
        draw = ImageDraw.Draw(image)
        draw.text((30, 40), f"SYNTHETIC SEEK SEGMENT {index}", fill="white")
        draw.text((30, 80), f"TIME RANGE {(index - 1) * 40} - {index * 40}", fill="white")
        frame = directory / "media" / f"seek-segment-{index}.png"
        image.save(frame)
        frames.append(frame)
    seek_video = course / "seek-preview.webm"
    command = [str(encoder), "-hide_banner", "-loglevel", "error", "-y"]
    for frame in frames:
        command.extend(["-loop", "1", "-framerate", "2", "-t", "40", "-i", str(frame)])
    command.extend([
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0,format=yuv420p[out]",
        "-map", "[out]", "-c:v", "libvpx", "-deadline", "realtime", "-threads", "1",
        "-b:v", "120k", "-map_metadata", "-1", "-an", str(seek_video),
    ])
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    subprocess.run(command, capture_output=True, check=True, timeout=45, **options)
    manifest.update({
        "revision": "workspace-interactions-v2-synthetic-r1",
        "attachment_directory": str(documents.resolve()),
        "seek_video": str(seek_video.resolve()),
        "seek_segments": [
            {"from": 0, "to": 40, "dominant_channel": "red"},
            {"from": 40, "to": 80, "dominant_channel": "green"},
            {"from": 80, "to": 120, "dominant_channel": "blue"},
        ],
    })
    manifest["files"] = {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*")) if path.is_file() and path.name != "manifest.json"
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
