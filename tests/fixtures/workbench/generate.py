"""Tiny deterministic PDF, standard Helvetica, no real course or embedded font."""

from pathlib import Path


def document_regions_pdf(path: Path, images: list, texts: list) -> None:
    """Synthetic object layouts for local figure versus scan-cover boundaries."""
    import io
    from PIL import Image

    objects = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for _x, _y, _w, _h, color in images:
        with Image.new("RGB", (32, 32), color) as image:
            output = io.BytesIO()
            image.save(output, "JPEG", quality=100)
        data = output.getvalue()
        objects.append(
            b"<< /Type /XObject /Subtype /Image /Width 32 /Height 32 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length "
            + str(len(data)).encode()
            + b" >>\nstream\n"
            + data
            + b"\nendstream"
        )
    page_id = len(objects) + 1
    stream_id = page_id + 1
    stream = "".join(
        f"q {w} 0 0 {h} {x} {y} cm /Im{i} Do Q\n"
        for i, (x, y, w, h, _color) in enumerate(images)
    )
    stream += "".join(
        f"BT /F1 16 Tf {x} {y} Td ({text}) Tj ET\n" for x, y, text in texts
    )
    stream = stream.encode("ascii")
    resources = " ".join(f"/Im{i} {i + 4} 0 R" for i in range(len(images)))
    objects.extend(
        [
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> /XObject << {resources} >> >> /Contents {stream_id} 0 R >>".encode(),
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"endstream",
        ]
    )
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Kids [{page_id} 0 R] /Count 1 >>".encode()
    data, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    start = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    )
    path.write_bytes(data)


def reading_pdf(
    path: Path,
    page_count: int = 34,
    *,
    rotation: int = 0,
    crop_box: tuple[int, int, int, int] | None = None,
    blank_pages: tuple[int, ...] = (),
) -> None:
    objects = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    pages = []
    for index in range(page_count):
        page_id = len(objects) + 1
        stream_id = page_id + 1
        pages.append(page_id)
        stream = f"BT /F1 20 Tf 50 730 Td (Learning page {index + 1} - immutable synthetic source.) Tj 0 -45 Td (Selection on this page only.) Tj ET\n".encode(
            "ascii"
        )
        if index in blank_pages:
            stream = b""
        geometry = f" /Rotate {rotation}"
        if crop_box:
            geometry += " /CropBox [" + " ".join(map(str, crop_box)) + "]"
        objects.extend(
            [
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]{geometry} /Resources << /Font << /F1 3 0 R >> >> /Contents {stream_id} 0 R >>".encode(),
                b"<< /Length "
                + str(len(stream)).encode()
                + b" >>\nstream\n"
                + stream
                + b"endstream",
            ]
        )
    outline_id = len(objects) + 1
    objects.append(
        f"<< /Type /Outlines /First {outline_id + 1} 0 R /Last {outline_id + 3} 0 R /Count 3 >>".encode()
    )
    destinations = [0, page_count // 2, page_count - 1]
    for ordinal, target in enumerate(destinations):
        previous = f" /Prev {outline_id + ordinal} 0 R" if ordinal else ""
        following = f" /Next {outline_id + ordinal + 2} 0 R" if ordinal < 2 else ""
        objects.append(
            f"<< /Title (Chapter {ordinal + 1}) /Parent {outline_id} 0 R{previous}{following} /Dest [{pages[target]} 0 R /Fit] >>".encode()
        )
    objects[0] = (
        f"<< /Type /Catalog /Pages 2 0 R /Outlines {outline_id} 0 R >>".encode()
    )
    objects[1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{value} 0 R' for value in pages)}] /Count {page_count} >>".encode()
    )
    data, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for identity, body in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{identity} 0 obj\n".encode() + body + b"\nendobj\n")
    start = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def scanned_pdf(path: Path, page_count: int = 1, *, dense: bool = False) -> None:
    """Only a locally drawn synthetic bitmap; PDF has no native text layer."""
    import io
    from PIL import Image, ImageDraw, ImageFont

    height = 1800 if dense else 900
    image = Image.new("RGB", (1600, height), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=64)
    draw.text((90, 130), "LEARNING WORKBENCH", font=font, fill="black")
    draw.text((90, 300), "Only selected text. 12345", font=font, fill="black")
    if dense:
        small = ImageFont.load_default(size=28)
        for index in range(45):
            draw.text(
                (70, 390 + index * 30),
                f"SYNTHETIC LINE {index:02d}: local OCR cancellation and checkpoint 12345.",
                font=small,
                fill="black",
            )
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=96)
    image.close()
    jpeg = buffer.getvalue()
    objects = [
        b"",
        b"",
        f"<< /Type /XObject /Subtype /Image /Width 1600 /Height {height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ".encode()
        + str(len(jpeg)).encode()
        + b" >>\nstream\n"
        + jpeg
        + b"\nendstream",
    ]
    pages = []
    for _ in range(page_count):
        page_id = len(objects) + 1
        pages.append(page_id)
        stream = f"q 516 0 0 {height / 1600 * 516} 48 {100 if dense else 400} cm /Im0 Do Q\n".encode()
        objects.extend(
            [
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject << /Im0 3 0 R >> >> /Contents {page_id + 1} 0 R >>".encode(),
                b"<< /Length "
                + str(len(stream)).encode()
                + b" >>\nstream\n"
                + stream
                + b"endstream",
            ]
        )
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in pages)}] /Count {page_count} >>".encode()
    )
    data, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for identity, body in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{identity} 0 obj\n".encode() + body + b"\nendobj\n")
    start = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def synthetic_speech(path: Path) -> None:
    """Owned local Windows TTS; no user recordings or downloaded media."""
    import os
    from learning_app.processes import OwnedProcessGroup

    script = path.parent.parent / (path.stem + ".tts-fixture.ps1")
    script.write_text(
        """param([string]$OutputPath)
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $speaker.SelectVoice('Microsoft Zira Desktop')
  $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
  $speaker.SetOutputToWaveFile($OutputPath, $format)
  $speaker.Speak('This is a synthetic lesson for local transcription. We keep original audio words and timestamps. A student may correct the subtitles without changing the original recording. Selected audio stays on this computer. No video images are read by transcription.')
} finally { $speaker.Dispose() }
""",
        encoding="utf-8",
    )
    group = OwnedProcessGroup("synthetic-speech-fixture")
    try:
        powershell = (
            Path(os.environ.get("SYSTEMROOT", "C:/Windows"))
            / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        process = group.spawn(
            [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-OutputPath",
                str(path.resolve()),
            ],
            cwd=path.parent,
        )
        _out, _err = process.communicate(timeout=30)
        if process.returncode or not path.is_file():
            raise RuntimeError("SYNTHETIC_SPEECH_GENERATION_FAILED")
    finally:
        group.close()
