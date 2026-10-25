"""Fixed real ASR + native skill + provider, on a self-generated audio timeline."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import re
import runpy
import wave

from dotenv import dotenv_values
from sqlalchemy import select

from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.storage import schema as business
from learning_app.workbench import corpus_schema as c
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.asr_resources import AsrResources
from learning_app.workbench.processing import PreparationService
from learning_app.workbench.processing_models import (
    PreparationControl,
    PreparationCreate,
)


def wav_bytes(pcm):
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        writer.writeframes(pcm)
    return output.getvalue()


def freeze():
    paths = [
        *sorted((PROJECT_ROOT / "backend/src/learning_app").rglob("*.py")),
        *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
        PROJECT_ROOT / "harness/runtime.lock.json",
        PROJECT_ROOT / "resources/workbench/engines.lock.json",
        PROJECT_ROOT / "backend/uv.lock",
        PROJECT_ROOT / "tests/fixtures/workbench/generate.py",
    ]
    return {
        str(path.relative_to(PROJECT_ROOT)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in paths
    }


async def run(args):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,100}", args.run_id):
        raise ValueError("invalid owned run identity")
    directory = PROJECT_ROOT / ".runtime/validation" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    config = AppConfig(
        mode="test",
        harness_enabled=True,
        credential_route="api_key",
        model=args.model,
        thinking=args.thinking,
        max_output_tokens=args.max_output_tokens,
        model_timeout_seconds=args.timeout,
        data_dir=directory / "data",
        runtime_dir=directory / "runtime",
    )
    record = {
        "layer": "actual_asr_native_harness_real_provider",
        "status": "not_run",
        "source_hashes_before": freeze(),
        "input": "self-generated Windows TTS embedded in 180s PCM timeline",
        "user_experience": "not_run",
    }
    if (
        not dotenv_values(config.secret_file).get("DEEPSEEK_API_KEY")
        or not AsrResources(PROJECT_ROOT).prepared()
    ):
        record["missing_prerequisite"] = "PRIVATE_CREDENTIAL_OR_FIXED_ASR_NOT_READY"
        (directory / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
        return 2
    original_speech = directory / "base.wav"
    runpy.run_path(str(PROJECT_ROOT / "tests/fixtures/workbench/generate.py"))[
        "synthetic_speech"
    ](original_speech)
    with wave.open(str(original_speech), "rb") as reader:
        assert (
            reader.getnchannels(),
            reader.getsampwidth(),
            reader.getframerate(),
        ) == (1, 2, 16000)
        speech = reader.readframes(reader.getnframes())
    assert len(speech) < 30 * 32000
    pcm = bytearray(180 * 32000)
    for second in (0, 60, 140):
        pcm[second * 32000 : second * 32000 + len(speech)] = speech
    material = directory / "synthetic-course"
    material.mkdir()
    audio = material / "synthetic-timeline.wav"
    audio.write_bytes(wav_bytes(pcm))
    original_hash = hashlib.sha256(audio.read_bytes()).hexdigest()
    repo = Repository(config.data_dir)
    course = CourseFileStore(repo).bind(None, str(material), "合成附近字幕真实验证")[
        "course_id"
    ]
    source = repo.register_source(course, audio)
    repo.create_chat(course, "real_nearby")
    service = GenerationService(repo, config)
    preparations, generation = None, None
    try:
        await service.open()
        if not service.question_answering_ready:
            raise RuntimeError(service.execution_error or "MODEL_ROUTE_NOT_READY")
        preparations = PreparationService(
            repo,
            PROJECT_ROOT,
            is_busy=lambda: any(not t.done() for t in service.tasks.values()),
        )
        batch = preparations.create_asr(
            course,
            PreparationCreate(
                kind="asr",
                asset_id=source["asset_id"],
                source_version=source["source_version"],
                language="en",
            ),
        )
        paused = preparations.store.control(
            course,
            batch.job_id,
            "pause",
            PreparationControl(
                operation_id="pause_full", expected_revision=batch.revision
            ),
        )
        await preparations.open()
        submitted = await service.submit(
            {
                "chat_id": "real_nearby",
                "submission_id": "real_nearby_question",
                "text": "请先使用原生course-read技能，按需读取本次75秒附近的字幕，说明这段课程音轨介绍了哪些内容，列三条并引用实际快照ID。只依据附近字幕，不使用外部资料，不保存为学习成果。",
                "question_video": {
                    "asset_id": source["asset_id"],
                    "source_version": source["source_version"],
                    "time_ms": 75000,
                },
                "model": args.model,
                "thinking": args.thinking,
            }
        )
        generation = submitted["generation_id"]
        await asyncio.wait_for(service.tasks[generation], args.timeout * 4 + 30)
        run = repo.get_generation(generation)
        record["generation"] = run
        with repo.engine.connect() as conn:
            queries = list(
                conn.execute(
                    select(c.query_receipts).where(
                        c.query_receipts.c.run_id == generation
                    )
                ).mappings()
            )
            units = list(conn.execute(select(s.transcript_units)).mappings())
        record["queries"] = [dict(row) for row in queries]
        record["units"] = [dict(row) for row in units]
        record["paused_batch_after"] = preparations.store.get(
            course, batch.job_id
        ).model_dump(mode="json")
        if run["state"] != "completed" or not queries:
            raise RuntimeError("NEARBY_REAL_NOT_COMPLETE")
        if {unit["unit_index"] for unit in units} != {1, 2, 3}:
            raise RuntimeError("NEARBY_REAL_AUDIO_SCOPE")
        for unit in units:
            i, metadata = unit["unit_index"], unit["payload"]["audio"]
            expected = wav_bytes(pcm[i * 30 * 32000 : (i + 1) * 30 * 32000])
            if (
                metadata["wav_sha256"] != hashlib.sha256(expected).hexdigest()
                or metadata["image_frames_requested"] != 0
            ):
                raise RuntimeError("NEARBY_REAL_SAMPLE_PROOF")
        read = next(
            (
                row["result"]
                for row in queries
                if row["native_call"]["tool_name"] == "read_nearby_subtitles"
            ),
            None,
        )
        if read is None or read["status"] != "matched" or read["missing_units"]:
            raise RuntimeError("NEARBY_REAL_SUBTITLE_PENDING")
        if not any(
            item["source"]["snapshot_id"] in run["body"] for item in read["results"]
        ):
            raise RuntimeError("NEARBY_REAL_CITATION_MISSING")
        if preparations.store.get(course, batch.job_id) != paused or ArtifactStore(
            repo
        ).list(course):
            raise RuntimeError("NEARBY_REAL_UNREQUESTED_EFFECT")
        requests = [
            json.loads(
                (
                    service.execution.boundary.private
                    / f"{attempt['request_id']}.request.json"
                ).read_text("utf-8")
            )
            for attempt in service.execution.boundary.records
            if attempt.get("generation_id") == generation
        ]
        record["provider_request_count"] = len(requests)
        if not requests or any(
            '"type": "image"' in json.dumps(request) for request in requests
        ):
            raise RuntimeError("NEARBY_REAL_INPUT_SCOPE")
        if "This is a synthetic lesson" in json.dumps(requests[0]):
            raise RuntimeError("NEARBY_REAL_AUTOMATIC_PREFETCH")
        record["status"] = "passed"
    except BaseException as error:
        record.update(status="failed", failure=f"{type(error).__name__}: {error}")
    finally:
        if preparations:
            await preparations.close()
        await service.close()
        record["cleanup"] = service.cleanup
        record["actual_outbound_attempts"] = (
            service.execution.boundary.actual_outbound_attempts
            if service.execution
            else None
        )
        with repo.engine.connect() as conn:
            record["raw_usage"] = [
                dict(row)
                for row in conn.execute(select(business.usage_records)).mappings()
            ]
        record["source_hashes_after"] = freeze()
        record["source_changed_during_run"] = (
            record["source_hashes_before"] != record["source_hashes_after"]
        )
        record["original_preserved"] = (
            hashlib.sha256(audio.read_bytes()).hexdigest() == original_hash
        )
        if record["source_changed_during_run"] or not record["original_preserved"]:
            record["status"] = "failed"
        if generation and repo.get_generation(generation)["state"] == "unknown":
            record["status"] = "unknown"
        (directory / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
        print(
            json.dumps(
                {
                    "status": record["status"],
                    "attempts": record["actual_outbound_attempts"],
                    "evidence": str(directory / "summary.json"),
                },
                ensure_ascii=False,
            )
        )
        repo.close()
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--thinking", default="high")
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--timeout", type=float, default=180)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
