"""Fixed local PyAV worker: remux only, with audio/video duration checks."""

import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from learning_app.errors import AppError


def av_runtime(root):
    lock = json.loads((root / "resources/integrations/av.lock.json").read_text())
    archive = root / ".local/platform-engine" / lock["filename"]
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != lock["sha256"]:
        raise AppError("BILI_MUX_NOT_PREPARED", "保存本地需要先准备固定媒体组件", 503)
    target = root / ".runtime/platforms/av" / lock["sha256"]
    # Verify installed bytes against the immutable archive on every launch.
    with zipfile.ZipFile(archive) as wheel:
        for item in wheel.infolist():
            path = target / item.filename
            if not path.resolve().is_relative_to(target.resolve()):
                raise AppError("BILI_MUX_PATH_DENIED", "媒体组件包含无效路径", 503)
            if item.is_dir():
                continue
            data = wheel.read(item)
            if path.exists():
                if path.read_bytes() != data:
                    raise AppError("BILI_MUX_INTEGRITY", "媒体组件内容已变化，请核对后修复", 503)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
    return target


def mux(root, tracks, target, duration_ms, cancelled):
    runtime = av_runtime(root)
    command = [
        sys.executable,
        str(Path(__file__).with_name("mux_worker.py")),
        str(runtime),
        str(target),
        str(duration_ms),
        *map(str, tracks),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    deadline = time.monotonic() + 300
    try:
        while process.poll() is None:
            if cancelled.is_set() or time.monotonic() > deadline:
                process.terminate()
                raise AppError("PREPARATION_INTERRUPTED", "媒体整理已暂停，原轨道保留", 409)
            try:
                output, _ = process.communicate(timeout=0.1)
            except subprocess.TimeoutExpired:
                continue
        output, _ = process.communicate()
        if process.returncode or not target.is_file():
            raise AppError("BILI_MUX_FAILED", "音视频完整性或合并验证未通过，未发布副本", 409)
        return json.loads(output)
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)
