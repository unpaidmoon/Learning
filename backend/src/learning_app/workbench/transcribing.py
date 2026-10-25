"""Finite audio-only decoding and recognition for one persisted local lease."""

import json
import sys
import tempfile
from pathlib import Path

from sqlalchemy import select

from learning_app.errors import AppError
from learning_app.storage.repository import _hash

from . import schema as s
from .asr_resources import AsrResources
from .audio_sources import AudioSources
from .local_workers import run_json_worker
from .transcripts import WINDOW_MS, TranscriptStore


class LocalTranscriber:
    def __init__(self, repository, project_root, progress, *, resource_policy=None):
        self.repository, self.project_root, self.progress = repository, project_root, progress
        self.resource_policy = resource_policy

    def run(self, lease, cancelled):
        source = AudioSources(self.repository).current(
            lease["course_id"], lease["asset_id"], lease["source_version"]
        )
        if source["content_hash"] != lease["source_digest"]:
            raise AppError("SOURCE_VERSION_UNAVAILABLE", "音轨与冻结任务版本不符")
        resources = AsrResources(self.project_root)
        engine = lease["parameters"]["engine"]
        if resources.manifest_hash != engine["manifest_sha256"]:
            raise AppError("ASR_RESOURCE_VERSION_CONFLICT", "当前语音资源与冻结任务不同")
        track_id = lease["parameters"]["track_id"]
        with self.repository.engine.connect() as conn:
            track = TranscriptStore._track(conn, lease["course_id"], track_id)
            if lease["unit_index"] == 0 and track["probe"]:
                return {
                    "kind": "audio_probe",
                    "track_id": track_id,
                    "unit_index": 0,
                    "source_digest": lease["source_digest"],
                    "probe": track["probe"],
                    "engine_invocations": 0,
                }
            prior = (
                conn.execute(
                    select(s.transcript_units).where(
                        s.transcript_units.c.track_id == track_id,
                        s.transcript_units.c.unit_index == lease["unit_index"] - 1,
                    )
                )
                .mappings()
                .first()
            )
            if prior:
                TranscriptStore._payload(prior)
                return {
                    "kind": "cached_transcript",
                    "track_id": track_id,
                    "unit_index": lease["unit_index"],
                    "source_digest": lease["source_digest"],
                    "raw_revision_id": prior["revision_id"],
                    "engine_invocations": 0,
                }
        evidence = (
            self.repository.root
            / "preparation-evidence"
            / lease["job_id"]
            / str(lease["unit_index"])
        )
        if not evidence.resolve().is_relative_to(self.repository.root.resolve()):
            raise AppError("ASR_EVIDENCE_PATH_INVALID", "音频处理结果目录不属于当前实例")
        evidence.mkdir(parents=True, exist_ok=True)
        input_hash = _hash(
            {
                "track_id": track_id,
                "unit_index": lease["unit_index"],
                "source_digest": lease["source_digest"],
                "engine": engine,
            }
        )
        for checkpoint in sorted(evidence.glob("*.result.json")):
            value = json.loads(checkpoint.read_text("utf-8"))
            if checkpoint.name.split(".")[-3] != _hash(value):
                # name is lease.<digest>.result.json
                raise AppError("ASR_CHECKPOINT_INTEGRITY", "音频处理检查点校验失败，未自动识别")
            if value.get("input_hash") == input_hash:
                return value
        spec = resources.launch_spec()
        if self.resource_policy:
            spec["model_options"] = {**spec["model_options"], "cpu_threads": self.resource_policy["cpu_threads"]}
        with tempfile.TemporaryDirectory(
            prefix="asr-unit-", dir=self.repository.staging
        ) as temporary:
            directory = Path(temporary)
            request_id, epoch = lease["lease_id"], lease["lease_epoch"]
            base = dict(
                request_id=request_id,
                epoch=epoch,
                spec={"runtime": spec["runtime"], "manifest_sha256": spec["manifest_sha256"]},
                source={
                    "path": str(source["path"]),
                    "asset_id": lease["asset_id"],
                    "source_version": lease["source_version"],
                    "bytes": source["byte_length"],
                    "sha256": source["content_hash"],
                },
            )
            if lease["unit_index"] == 0:
                decoded = run_json_worker(
                    [sys.executable, "-I", "-S", str(Path(__file__).with_name("audio_worker.py"))],
                    {**base, "type": "probe", "start_ms": None, "end_ms": None},
                    directory=directory,
                    cancelled=cancelled,
                    on_progress=lambda value: self.progress(lease, value),
                    log_path=evidence / f"{request_id}.probe.jsonl",
                    timeout_seconds=120,
                    resource_policy=self.resource_policy,
                )
                result = {
                    "kind": "audio_probe",
                    "track_id": track_id,
                    "unit_index": 0,
                    "source_digest": lease["source_digest"],
                    "probe": decoded["probe"],
                    "engine_invocations": 0,
                }
            else:
                index = lease["unit_index"] - 1
                probe = lease["parameters"]["audio_probe"]
                start, end = index * WINDOW_MS, min((index + 1) * WINDOW_MS, probe["audio_end_ms"])
                decoded = run_json_worker(
                    [sys.executable, "-I", "-S", str(Path(__file__).with_name("audio_worker.py"))],
                    {**base, "type": "decode", "start_ms": start, "end_ms": end},
                    directory=directory,
                    cancelled=cancelled,
                    on_progress=lambda value: self.progress(lease, value),
                    log_path=evidence / f"{request_id}.decode.jsonl",
                    omit_result_fields=("audio_base64",),
                    timeout_seconds=120,
                    resource_policy=self.resource_policy,
                )
                metadata = decoded["audio"]
                if (
                    metadata["source"]["sha256"] != lease["source_digest"]
                    or metadata["requested_start_ms"] != start
                    or metadata["requested_end_ms"] != end
                ):
                    raise AppError("ASR_RESULT_SCOPE_CONFLICT", "解码结果不属于指定音轨时间片")
                raw = run_json_worker(
                    [sys.executable, "-I", "-S", str(Path(__file__).with_name("asr_worker.py"))],
                    {
                        "type": "transcribe",
                        "request_id": request_id,
                        "epoch": epoch,
                        "audio_base64": decoded["audio_base64"],
                        "audio_sha256": metadata["wav_sha256"],
                        "start_ms": metadata["start_ms"],
                        "language": engine["language"],
                    },
                    initialization={"type": "initialize", "spec": spec},
                    directory=directory,
                    cancelled=cancelled,
                    on_progress=lambda value: self.progress(lease, value),
                    log_path=evidence / f"{request_id}.asr.jsonl",
                    timeout_seconds=180,
                    resource_policy=self.resource_policy,
                )
                if (
                    raw["audio_sha256"] != metadata["wav_sha256"]
                    or raw["start_ms"] != metadata["start_ms"]
                    or raw["duration_ms"] != metadata["duration_ms"]
                ):
                    raise AppError("ASR_RESULT_SCOPE_CONFLICT", "转写与本次有限音频片段不符")
                result = {
                    "kind": "asr",
                    "track_id": track_id,
                    "unit_index": lease["unit_index"],
                    "source_digest": lease["source_digest"],
                    "audio": metadata,
                    "raw": {
                        key: value
                        for key, value in raw.items()
                        if key not in {"request_id", "epoch"}
                    },
                    "engine_invocations": 1,
                }
        result["input_hash"] = input_hash
        (evidence / f"{lease['lease_id']}.{_hash(result)}.result.json").write_text(
            json.dumps(result, ensure_ascii=False, allow_nan=False), "utf-8"
        )
        return result
