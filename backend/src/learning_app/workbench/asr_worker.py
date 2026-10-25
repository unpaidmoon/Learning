"""Private finite-audio worker, launched with Python -I -S and owned by a job.

Only explicit, hash-verified PCM WAV bytes enter recognition. This module has no
provider key, application import, course lookup, video decoder or network API.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import io
import json
import math
import os
import sys
import wave
from pathlib import Path

MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_AUDIO_MS = 35_000
PROTOCOL = 1


def _emit(value):
    print(json.dumps(value, ensure_ascii=True, allow_nan=False), flush=True)


def _read():
    line = sys.stdin.buffer.readline(MAX_LINE_BYTES + 1)
    if not line:
        return None
    if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
        raise ValueError("ASR_MESSAGE_SIZE_LIMIT")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("ASR_MESSAGE_INVALID")
    return value


def _finite(value):
    return value if isinstance(value, (float, int)) and math.isfinite(value) else None


def main():
    first = _read()
    if not first or set(first) != {"type", "spec"} or first["type"] != "initialize":
        raise ValueError("ASR_INITIALIZATION_REQUIRED")
    spec = first["spec"]
    runtime = Path(spec["runtime"]).resolve(strict=True)
    stamp = json.loads((runtime / ".prepared.json").read_text("utf-8"))
    if stamp["manifest_sha256"] != spec["manifest_sha256"]:
        raise ValueError("ASR_MANIFEST_CONFLICT")
    sys.path.insert(0, str(runtime / "python/Lib/site-packages"))
    directories, libraries = [], []
    for path in spec["native_directories"]:
        checked = Path(path).resolve(strict=True)
        if not checked.is_relative_to(runtime):
            raise ValueError("ASR_NATIVE_PATH_DENIED")
        if os.name == "nt":
            directories.append(os.add_dll_directory(str(checked)))
    for path in spec["preload"]:
        checked = Path(path).resolve(strict=True)
        if not checked.is_relative_to(runtime):
            raise ValueError("ASR_NATIVE_PATH_DENIED")
        libraries.append(
            ctypes.WinDLL(str(checked)) if os.name == "nt" else ctypes.CDLL(str(checked))
        )
    import numpy as np
    from faster_whisper import WhisperModel

    model = WhisperModel(spec["model"], **spec["model_options"])
    _emit(
        {
            "type": "ready",
            "protocol_version": PROTOCOL,
            "manifest_sha256": spec["manifest_sha256"],
            "engine": "faster-whisper-1.2.1/ctranslate2-4.8.0",
            "vad_enabled": True,
        }
    )
    while (request := _read()) is not None:
        request_id = request.get("request_id")
        try:
            if (
                set(request)
                != {
                    "type",
                    "request_id",
                    "epoch",
                    "audio_base64",
                    "audio_sha256",
                    "start_ms",
                    "language",
                }
                or request["type"] != "transcribe"
            ):
                raise ValueError("ASR_REQUEST_INVALID")
            if (
                not isinstance(request_id, str)
                or not 1 <= len(request_id) <= 160
                or type(request["epoch"]) is not int
                or request["epoch"] < 1
                or type(request["start_ms"]) is not int
                or request["start_ms"] < 0
                or request["language"] not in {None, "zh", "en"}
            ):
                raise ValueError("ASR_REQUEST_INVALID")
            data = base64.b64decode(request["audio_base64"], validate=True)
            if hashlib.sha256(data).hexdigest() != request["audio_sha256"]:
                raise ValueError("ASR_AUDIO_HASH_MISMATCH")
            with wave.open(io.BytesIO(data), "rb") as audio:
                if (
                    audio.getnchannels(),
                    audio.getsampwidth(),
                    audio.getframerate(),
                    audio.getcomptype(),
                ) != (1, 2, 16000, "NONE"):
                    raise ValueError("ASR_AUDIO_FORMAT_INVALID")
                count = audio.getnframes()
                if not 0 < count <= 16000 * MAX_AUDIO_MS // 1000:
                    raise ValueError("ASR_AUDIO_WINDOW_LIMIT")
                pcm = audio.readframes(count)
                if len(pcm) != count * 2:
                    raise ValueError("ASR_AUDIO_INCOMPLETE")
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            duration_ms = count * 1000 // 16000
            # A scalar temperature explicitly disables fallback temperatures.
            segments, info = model.transcribe(
                samples,
                language=request["language"],
                beam_size=5,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 2000, "speech_pad_ms": 400},
                condition_on_previous_text=False,
                word_timestamps=True,
                temperature=0.0,
            )
            output, text_size = [], 0
            for segment in segments:
                begin = max(0, round(segment.start * 1000))
                end = min(duration_ms, round(segment.end * 1000))
                text_size += len(segment.text)
                if len(output) >= 256 or text_size > 100_000:
                    raise ValueError("ASR_OUTPUT_LIMIT")
                if end <= begin:
                    raise ValueError("ASR_SEGMENT_TIME_INVALID")
                words = [
                    {
                        "text": word.word,
                        "start_ms": request["start_ms"] + max(0, round(word.start * 1000)),
                        "end_ms": request["start_ms"] + min(duration_ms, round(word.end * 1000)),
                        "probability": _finite(word.probability),
                        "raw_start_seconds": _finite(word.start),
                        "raw_end_seconds": _finite(word.end),
                    }
                    for word in (segment.words or [])
                ]
                output.append(
                    {
                        "text": segment.text,
                        "start_ms": request["start_ms"] + begin,
                        "end_ms": request["start_ms"] + end,
                        "words": words,
                        "avg_logprob": _finite(segment.avg_logprob),
                        "no_speech_prob": _finite(segment.no_speech_prob),
                        "raw_start_seconds": _finite(segment.start),
                        "raw_end_seconds": _finite(segment.end),
                        "raw_tokens": list(segment.tokens),
                        "raw_temperature": _finite(segment.temperature),
                        "raw_compression_ratio": _finite(segment.compression_ratio),
                    }
                )
                _emit(
                    {
                        "type": "progress",
                        "request_id": request_id,
                        "epoch": request["epoch"],
                        "status": "recognizing audio",
                        "progress": min(0.99, end / max(1, duration_ms)),
                    }
                )
            _emit(
                {
                    "type": "result",
                    "request_id": request_id,
                    "epoch": request["epoch"],
                    "audio_sha256": request["audio_sha256"],
                    "start_ms": request["start_ms"],
                    "duration_ms": duration_ms,
                    "duration_after_vad_ms": round(info.duration_after_vad * 1000),
                    "language": info.language,
                    "language_probability": _finite(info.language_probability),
                    "segments": output,
                    "parameters_version": "fw121-vad6-beam5-context0-temp0-raw-v2",
                }
            )
        except Exception as error:
            code = (
                str(error)
                if isinstance(error, ValueError) and str(error).startswith("ASR_")
                else "ASR_RECOGNITION_FAILED"
            )
            _emit(
                {
                    "type": "error",
                    "request_id": request_id,
                    "epoch": request.get("epoch"),
                    "code": code,
                }
            )
    del model
    del libraries
    for handle in directories:
        handle.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        _emit({"type": "error", "request_id": None, "code": "ASR_INITIALIZATION_FAILED"})
        raise SystemExit(1)
