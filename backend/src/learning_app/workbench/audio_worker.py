"""Isolated fixed-PyAV worker. Only one authorized local audio window is decoded."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
from pathlib import Path


def emit(value):
    print(json.dumps(value, ensure_ascii=True, allow_nan=False), flush=True)


def main():
    line = sys.stdin.buffer.readline(64 * 1024 + 1)
    if len(line) > 64 * 1024 or not line.endswith(b"\n"):
        raise ValueError("ASR_MESSAGE_SIZE_LIMIT")
    request = json.loads(line)
    if set(request) != {"request_id", "epoch", "type", "spec", "source", "start_ms", "end_ms"}:
        raise ValueError("ASR_REQUEST_INVALID")
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", request["request_id"])
        or type(request["epoch"]) is not int
        or request["epoch"] < 1
        or request["type"] not in {"probe", "decode"}
    ):
        raise ValueError("ASR_REQUEST_INVALID")
    spec = request["spec"]
    runtime = Path(spec["runtime"]).resolve(strict=True)
    stamp = json.loads((runtime / ".prepared.json").read_text("utf-8"))
    if stamp["manifest_sha256"] != spec["manifest_sha256"]:
        raise ValueError("ASR_MANIFEST_CONFLICT")
    sys.path.insert(0, str(runtime / "python/Lib/site-packages"))
    module_spec = importlib.util.spec_from_file_location(
        "learning_audio_window", Path(__file__).with_name("audio_window.py")
    )
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    source = request["source"]
    if set(source) != {"path", "asset_id", "source_version", "bytes", "sha256"}:
        raise ValueError("ASR_SOURCE_IDENTITY_INVALID")
    identity = module.SourceIdentity(
        **{key: value for key, value in source.items() if key != "path"}
    )
    path = Path(source["path"])
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise ValueError("ASR_SOURCE_PATH_DENIED")

    def progress(value):
        amount = value.get("decoded_audio_samples", 0)
        rate = value.get("source_sample_rate", 1)
        duration = ((request["end_ms"] or 1) - (request["start_ms"] or 0)) / 1000
        emit(
            {
                "type": "progress",
                "request_id": request["request_id"],
                "epoch": request["epoch"],
                "status": "scanning audio metadata"
                if request["type"] == "probe"
                else "decoding audio",
                "progress": min(0.99, amount / max(1, rate * duration)) if amount else None,
                **{
                    key: value[key]
                    for key in ("audio_packets", "audio_frames", "decoded_audio_samples")
                    if key in value
                },
            }
        )

    with module.locked_source(path, identity) as handle:
        if request["type"] == "probe":
            result = {"probe": module.probe_audio(handle, identity, progress=progress)}
        else:
            wav, metadata = module.decode_window(
                handle, identity, request["start_ms"], request["end_ms"], progress=progress
            )
            result = {"audio_base64": base64.b64encode(wav).decode("ascii"), "audio": metadata}
    emit(
        {"type": "result", "request_id": request["request_id"], "epoch": request["epoch"], **result}
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        code = (
            str(error)
            if isinstance(error, ValueError) and re.fullmatch(r"ASR_[A-Z0-9_]+", str(error))
            else "ASR_AUDIO_DECODE_FAILED"
        )
        emit({"type": "error", "code": code})
        raise SystemExit(1)
