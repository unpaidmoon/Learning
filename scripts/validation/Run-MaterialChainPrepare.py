"""Owned mixed PDF and two narrated videos, formal OCR/ASR and frozen A/B scope."""

import argparse
import asyncio
import hashlib
import importlib.util
import io
import json
import os
import re
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont

from learning_app.config import PROJECT_ROOT
from learning_app.course_files import CourseFileStore
from learning_app.processes import OwnedProcessGroup
from learning_app.storage import Repository
from learning_app.video_previews import TOOL_SHA256
from learning_app.workbench.models import ModelSettings
from learning_app.workbench.processing import PreparationService
from learning_app.workbench.processing_models import PreparationCreate
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.transcripts import TranscriptStore


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(arguments, directory, timeout=60):
    group = OwnedProcessGroup("material-chain-owned-fixture")
    try:
        process = group.spawn([str(x) for x in arguments], cwd=directory)
        stdout, stderr = process.communicate(timeout=timeout)
        if process.returncode:
            raise RuntimeError(
                "OWNED_FIXTURE_COMMAND_FAILED:" + str(process.returncode)
            )
    finally:
        group.close()


def fixtures(out):
    helper = PROJECT_ROOT / "backend/tests/pdf_graphics_support.py"
    spec = importlib.util.spec_from_file_location("graphics_fixture", helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    work, a, b = out / "fixture-inputs", out / "course-A", out / "course-B"
    for path in (work, a, b):
        path.mkdir()
    native = module.GraphicsPDF().write(
        work / "native.pdf",
        "",
        anchor=(
            "BT /F0 15 Tf 30 340 Td (Rectangle area = width times length.) Tj "
            "0 -40 Td (Width 3 cm, length 4 cm: area 12 square cm.) Tj "
            "0 -40 Td (Check units before calculating.) Tj ET\n"
        ),
    )
    with Image.new("RGB", (1600, 800), "white") as picture:
        draw = ImageDraw.Draw(picture)
        font = ImageFont.load_default(size=64)
        for y, line in enumerate(
            (
                "AREA AND UNITS",
                "Width 5 cm, length 2 cm.",
                "Area = 10 square centimetres.",
                "Length and area use different units.",
            )
        ):
            draw.text((65, 65 + y * 170), line, font=font, fill="black")
        buffer = io.BytesIO()
        picture.save(buffer, "JPEG", quality=100)
    scan = module.GraphicsPDF()
    image = scan.stream(
        "/Type /XObject /Subtype /Image /Width 1600 /Height 800 /ColorSpace /DeviceRGB "
        "/BitsPerComponent 8 /Filter /DCTDecode",
        buffer.getvalue(),
    )
    scan_path = scan.write(
        work / "scan.pdf",
        "q 344 0 0 172 28 110 cm /Im Do Q",
        f"/XObject << /Im {image} 0 R >>",
        anchor="",
    )
    mixed = a / "rectangle-mixed.pdf"
    with pdfium.PdfDocument.new() as merged:
        for source in (native, scan_path):
            with pdfium.PdfDocument(source) as doc:
                merged.import_pages(doc, [0])
        merged.save(mixed)
    installed = Path(os.environ.get("LOCALAPPDATA", "")) / (
        "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/"
        "ffmpeg-8.1-full_build/bin/ffmpeg.exe"
    )
    ffmpeg = next(
        (
            p
            for p in [PROJECT_ROOT / ".tools/ffmpeg/ffmpeg.exe", installed]
            if p.is_file() and digest(p) == TOOL_SHA256
        ),
        None,
    )
    if ffmpeg is None:
        raise RuntimeError("FIXED_FFMPEG_NOT_AVAILABLE")
    script = work / "speech.ps1"
    script.write_text(
        """param([string]$OutputPath,[string]$TextPath)
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $speaker.SelectVoice('Microsoft Zira Desktop')
  $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
  $speaker.SetOutputToWaveFile($OutputPath, $format)
  $speaker.Speak((Get-Content -LiteralPath $TextPath -Raw))
} finally { $speaker.Dispose() }
""",
        "utf-8",
    )
    powershell = (
        Path(os.environ.get("SYSTEMROOT", "C:/Windows"))
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    speeches = (
        "To calculate the area of a rectangle, multiply its width by its length. A width of three centimetres and a length of four centimetres give an area of twelve square centimetres. Check the units before calculating.",
        "Lengths are measured in centimetres. Areas are measured in square centimetres. A rectangle with width five centimetres and length two centimetres has area ten square centimetres. Do not confuse a length unit with an area unit.",
    )
    videos = []
    for index, speech in enumerate(speeches):
        text, audio = work / f"speech-{index}.txt", work / f"speech-{index}.wav"
        text.write_bytes(speech.encode("ascii"))
        command(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script,
                "-OutputPath",
                audio,
                "-TextPath",
                text,
            ],
            work,
            30,
        )
        target = b / f"rectangle-{index + 1}.mp4"
        command(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-n",
                "-i",
                audio,
                "-f",
                "lavfi",
                "-i",
                f"color=c={'red' if index == 0 else 'magenta'}:s=320x180:r=5",
                "-map",
                "0:a:0",
                "-map",
                "1:v:0",
                "-c:a",
                "aac",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-shortest",
                "-movflags",
                "+faststart",
                target,
            ],
            work,
        )
        videos.append(target)
    return a, b, mixed, videos


async def run(args):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        raise ValueError("OWNED_RUN_ID_INVALID")
    out = PROJECT_ROOT / ".runtime/validation" / args.run_id
    out.mkdir(exist_ok=False)
    record = {
        "status": "not_run",
        "layer": "actual local OCR/ASR and frozen source scope; no Host/provider",
        "actual_outbound_attempts": 0,
        "host_calls": 0,
        "user_material_reads": 0,
    }
    repo = preparations = None
    try:
        a, b, mixed, videos = fixtures(out)
        originals = {str(p): digest(p) for p in [mixed, *videos]}
        repo = Repository(out / "data")
        files = CourseFileStore(repo)
        owner = files.bind(None, str(a), "混合文档与成果所在课A")["course_id"]
        origin = files.bind(None, str(b), "明确视频来源课B")["course_id"]
        pdf = files.open(owner, mixed.name)
        media = [files.open(origin, p.name) for p in videos]
        preparations = PreparationService(repo, PROJECT_ROOT)
        jobs = [
            preparations.create_pdf(
                owner,
                PreparationCreate(
                    kind="pdf_ocr",
                    asset_id=pdf["asset_id"],
                    source_version=pdf["source_version"],
                ),
            )
        ]
        audio_jobs = [
            preparations.create_asr(
                origin,
                PreparationCreate(
                    kind="asr",
                    asset_id=value["asset_id"],
                    source_version=value["source_version"],
                    language="en",
                ),
            )
            for value in media
        ]
        jobs.extend(audio_jobs)
        task = TaskCoverage(repo).admit(
            owner,
            TaskAdmission(
                operation_id="complete_mixed_materials",
                kind="handout",
                coverage_mode="complete",
                sources_only=True,
                instruction="完整整理指定混合PDF两页及另一个来源课程的两段完整视频有效字幕，形成中文矩形面积与单位讲义。3×4=12、5×2=10，区分长度和面积单位，原资料与AI补充身份分开；不得补视频画面。",
                sources=[
                    {
                        "kind": "document",
                        "asset_id": pdf["asset_id"],
                        "source_version": pdf["source_version"],
                    },
                    *[
                        {
                            "kind": "transcript",
                            "source_course_id": origin,
                            "asset_id": value["asset_id"],
                            "source_version": value["source_version"],
                            "track_id": job.parameters["track_id"],
                        }
                        for value, job in zip(media, audio_jobs, strict=True)
                    ],
                ],
                target={"mode": "create", "title": "矩形面积与单位完整讲义"},
                settings=ModelSettings(
                    model="deepseek-flash",
                    thinking="high",
                    max_output_tokens=16384,
                    timeout_seconds=300,
                ),
            ),
        )
        record.update(
            owner_course_id=owner,
            origin_course_id=origin,
            task_id=task.task_id,
            coverage_before=TaskCoverage(repo)
            .get(owner, task.task_id)
            .model_dump(mode="json"),
        )
        await preparations.open()
        deadline = asyncio.get_running_loop().time() + 600
        last = None
        while True:
            states = [preparations.store.get(job.course_id, job.job_id) for job in jobs]
            current = [
                (job.kind, job.state, [(u.unit_index, u.state) for u in job.units])
                for job in states
            ]
            if current != last:
                print(
                    json.dumps({"preparation": current}, ensure_ascii=False), flush=True
                )
                last = current
            if all(job.state == "completed" for job in states):
                break
            if any(
                job.state in {"failed", "needs_source", "cancelled"} for job in states
            ):
                raise RuntimeError("LOCAL_PREPARATION_NOT_COMPLETED")
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("LOCAL_PREPARATION_DEADLINE")
            await asyncio.sleep(0.2)
        TaskCoverage(repo).refresh(owner, task.task_id)
        record["jobs"] = [job.model_dump(mode="json") for job in states]
        record["transcripts"] = [
            TranscriptStore(repo)
            .view(origin, job.parameters["track_id"])
            .model_dump(mode="json")
            for job in audio_jobs
        ]
        record["coverage_after"] = (
            TaskCoverage(repo).get(owner, task.task_id).model_dump(mode="json")
        )
        record["originals"] = originals
        record["originals_unchanged"] = all(
            digest(Path(p)) == value for p, value in originals.items()
        )
        record["status"] = "passed" if record["originals_unchanged"] else "failed"
    except Exception as error:
        record.update(
            status="failed",
            error=getattr(error, "code", type(error).__name__) + ": " + str(error),
        )
    finally:
        if preparations:
            await preparations.close()
        if repo:
            repo.close()
        (out / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
    print(
        json.dumps(
            {
                k: record.get(k)
                for k in ["status", "error", "actual_outbound_attempts", "task_id"]
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
