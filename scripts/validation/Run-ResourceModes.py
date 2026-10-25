"""Real local ASR modes, active pause and owned-process cleanup; synthetic audio."""

import argparse
import asyncio
import base64
import hashlib
import io
import json
import re
import shutil
import sys
import threading
import time
import wave
from pathlib import Path

from learning_app.config import PROJECT_ROOT
from learning_app.course_files import CourseFileStore
from learning_app.processes import OwnedProcessGroup
from learning_app.runtime_preferences import (
    RuntimePreferenceInput,
    RuntimePreferences,
    RuntimePreferenceValues,
    resource_policy,
)
from learning_app.storage import Repository
from learning_app.workbench import local_workers
from learning_app.workbench.asr_resources import AsrResources
from learning_app.workbench.local_workers import run_json_worker
from learning_app.workbench.processing import PreparationService
from learning_app.workbench.processing_models import (
    PreparationControl,
    PreparationCreate,
)
from learning_app.workbench.transcripts import TranscriptStore


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")


def freeze():
    paths = [
        *PROJECT_ROOT.joinpath("backend/src/learning_app").rglob("*.py"),
        Path(__file__).resolve(),
    ]
    return {
        p.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in paths
    }


async def run(args):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        raise ValueError("OWNED_RUN_ID_INVALID")
    output = PROJECT_ROOT / ".runtime/validation" / args.run_id
    output.mkdir(exist_ok=False)
    (output / "runner-frozen.py").write_bytes(Path(__file__).read_bytes())
    record = {
        "status": "running",
        "layer": "actual installed CUDA ASR + formal preparation queue + owned-process observation",
        "cloud_model_calls": 0,
        "user_material_reads": 0,
        "video_frame_reads": 0,
        "source_before": freeze(),
        "cases": [],
        "groups": [],
    }
    original = (
        PROJECT_ROOT
        / ".runtime/validation/material-chain-prepare-20261024-r1/course-B/rectangle-1.mp4"
    ).resolve()
    assert (
        original.is_relative_to(
            (
                PROJECT_ROOT / ".runtime/validation/material-chain-prepare-20261024-r1"
            ).resolve()
        )
        and original.is_file()
    )
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    materials = output / "synthetic-materials"
    materials.mkdir()
    base = materials / "base.mp4"
    shutil.copyfile(original, base)
    spec = AsrResources(PROJECT_ROOT).launch_spec()
    decoder_dir = output / "decode"
    decoder_dir.mkdir()
    worker = PROJECT_ROOT / "backend/src/learning_app/workbench/audio_worker.py"
    request = {
        "request_id": "own_audio_decode",
        "epoch": 1,
        "spec": {
            "runtime": spec["runtime"],
            "manifest_sha256": spec["manifest_sha256"],
        },
        "source": {
            "path": str(base),
            "asset_id": "synthetic_audio",
            "source_version": original_hash,
            "bytes": base.stat().st_size,
            "sha256": original_hash,
        },
    }
    probe = run_json_worker(
        [sys.executable, "-I", "-S", str(worker)],
        {**request, "type": "probe", "start_ms": None, "end_ms": None},
        directory=decoder_dir,
        cancelled=threading.Event(),
        on_progress=lambda _: None,
        log_path=decoder_dir / "probe.jsonl",
    )
    decoded = run_json_worker(
        [sys.executable, "-I", "-S", str(worker)],
        {
            **request,
            "type": "decode",
            "start_ms": 0,
            "end_ms": min(30_000, probe["probe"]["audio_end_ms"]),
        },
        directory=decoder_dir,
        cancelled=threading.Event(),
        on_progress=lambda _: None,
        log_path=decoder_dir / "decode.jsonl",
        omit_result_fields=("audio_base64",),
    )
    with wave.open(
        io.BytesIO(base64.b64decode(decoded["audio_base64"])), "rb"
    ) as source:
        rate, channels, width = (
            source.getframerate(),
            source.getnchannels(),
            source.getsampwidth(),
        )
        frames = source.readframes(source.getnframes())
    assert rate == 16000 and channels == 1 and width == 2 and frames
    target_size = 90 * rate * channels * width
    pcm = (frames * (target_size // len(frames) + 1))[:target_size]
    speech = materials / "speech.wav"
    with wave.open(str(speech), "wb") as target:
        target.setframerate(rate)
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.writeframes(pcm)

    children = []

    class ObservedGroup(OwnedProcessGroup):
        def spawn(self, arguments, **options):
            child = super().spawn(arguments, **options)
            children.append((Path(arguments[-1]).name, child))
            record["groups"].append(
                {"pid": child.pid, "worker": Path(arguments[-1]).name, "closed": None}
            )
            self.observation = record["groups"][-1]
            return child

        def close(self, **options):
            result = super().close(**options)
            if hasattr(self, "observation"):
                self.observation["closed"] = result
            return result

    previous_group = local_workers.OwnedProcessGroup
    local_workers.OwnedProcessGroup = ObservedGroup
    try:
        for mode in ["gentle", "accelerated"]:
            repository = Repository(output / mode / "data")
            service = None
            try:
                course = CourseFileStore(repository).bind(
                    None, str(materials), "自造语音资源验证"
                )["course_id"]
                source = CourseFileStore(repository).open(course, "speech.wav")
                preferences = RuntimePreferences(repository.root)
                preferences.set(
                    RuntimePreferenceInput(
                        operation_id="select_mode",
                        expected_revision=0,
                        values=RuntimePreferenceValues(resource_mode=mode),
                    )
                )
                service = PreparationService(
                    repository,
                    PROJECT_ROOT,
                    get_resource_policy=lambda: resource_policy(
                        preferences.effective().resource_mode
                    ),
                )
                task = service.create_asr(
                    course,
                    PreparationCreate(
                        kind="asr",
                        asset_id=source["asset_id"],
                        source_version=source["source_version"],
                        language="en",
                    ),
                )
                item = {
                    "mode": mode,
                    "job_id": task.job_id,
                    "active_pause": "not_run",
                    "status": "running",
                }
                record["cases"].append(item)
                await service.open()
                deadline = time.monotonic() + 900
                while time.monotonic() < deadline:
                    current = service.store.get(course, task.job_id)
                    logs = (
                        list(
                            (repository.root / "preparation-evidence").rglob(
                                "*.asr.jsonl"
                            )
                        )
                        if (repository.root / "preparation-evidence").exists()
                        else []
                    )
                    if (
                        mode == "accelerated"
                        and item["active_pause"] == "not_run"
                        and current.state == "running"
                        and service._active
                        and service._active["unit_index"] > 0
                        and any(
                            name == "asr_worker.py" and child.poll() is None
                            for name, child in children
                        )
                        and any(
                            '"type": "ready"' in path.read_text("utf-8")
                            and '"type": "result"' not in path.read_text("utf-8")
                            for path in logs
                            if path.name == service._active["lease_id"] + ".asr.jsonl"
                        )
                    ):
                        service.store.control(
                            course,
                            task.job_id,
                            "pause",
                            PreparationControl(
                                operation_id="explicit_active_pause",
                                expected_revision=current.revision,
                            ),
                        )
                        service.interrupt(task.job_id)
                        while (
                            service._active is not None and time.monotonic() < deadline
                        ):
                            await asyncio.sleep(0.05)
                        assert (
                            service._active is None
                            and service.store.get(course, task.job_id).state == "paused"
                        )
                        assert all(
                            row["closed"] is not None
                            and not row["closed"]["remaining_pids"]
                            for row in record["groups"]
                        )
                        item["active_pause"] = (
                            "passed; initialized actual ASR process closed"
                        )
                        latest = service.store.get(course, task.job_id)
                        service.store.control(
                            course,
                            task.job_id,
                            "resume",
                            PreparationControl(
                                operation_id="explicit_resume_after_release",
                                expected_revision=latest.revision,
                            ),
                        )
                        service.wake()
                    if current.state in {
                        "completed",
                        "failed",
                        "needs_source",
                        "cancelled",
                    }:
                        break
                    await asyncio.sleep(0.05)
                final = service.store.get(course, task.job_id)
                if final.state != "completed" or (
                    mode == "accelerated" and item["active_pause"] == "not_run"
                ):
                    raise RuntimeError(
                        "RESOURCE_MODE_NOT_COMPLETE:" + mode + ":" + final.state
                    )
                limits = [
                    json.loads(path.read_text("utf-8"))
                    for path in (repository.root / "preparation-evidence").rglob(
                        "*.limits.json"
                    )
                ]
                assert limits and all(row == resource_policy(mode) for row in limits)
                tracks = TranscriptStore(repository).list(
                    course, source["asset_id"], source["source_version"]
                )
                item.update(
                    status="passed",
                    preparation=final.model_dump(mode="json"),
                    limits=limits,
                    track_ids=tracks,
                )
                write(output / "progress.json", record["cases"])
                print(
                    json.dumps(
                        {"mode": mode, "state": "passed", "pause": item["active_pause"]}
                    ),
                    flush=True,
                )
            finally:
                if service:
                    await service.close()
                repository.close()
        record["status"] = "passed"
    except Exception as error:
        record.update(status="failed", error=getattr(error, "code", str(error)))
    finally:
        local_workers.OwnedProcessGroup = previous_group
        record["original_unchanged"] = (
            hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
        )
        record["source_after"] = freeze()
        record["source_unchanged"] = record["source_before"] == record["source_after"]
        if not record["original_unchanged"] or not record["source_unchanged"]:
            record["status"] = "failed"
        write(output / "summary.json", record)
    print(
        json.dumps({"status": record["status"], "error": record.get("error")}),
        flush=True,
    )
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
