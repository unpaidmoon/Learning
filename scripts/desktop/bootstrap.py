"""Prepare verified desktop inputs without installing global tools or a second UI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def verified_executable() -> Path:
    """Verify an installed carrier without downloading or mutating anything."""
    lock = json.loads((ROOT / "harness/desktop.lock.json").read_text("utf-8"))
    runtime = ROOT / ".runtime/desktop/electron"
    marker = runtime / ".learning-runtime.json"
    executable = runtime / "electron.exe"
    if not marker.is_file() or not executable.is_file():
        raise RuntimeError("DESKTOP_ELECTRON_MISSING")
    installed = json.loads(marker.read_text("utf-8"))
    if {key: installed.get(key) for key in lock["electron"]} != lock["electron"]:
        raise RuntimeError("DESKTOP_ELECTRON_VERSION_MISMATCH")
    if installed.get("executable_sha256") != digest(executable):
        raise RuntimeError("DESKTOP_ELECTRON_EXECUTABLE_HASH_MISMATCH")
    return executable


def fetch_verified(spec: dict, cached: Path) -> Path:
    cached.parent.mkdir(parents=True, exist_ok=True)
    if cached.is_file():
        if digest(cached) != spec["sha256"]:
            raise RuntimeError(f"DESKTOP_CACHE_HASH_MISMATCH: {cached.name}")
        return cached
    temporary = cached.with_name(f"{cached.name}.{uuid.uuid4().hex}.partial")
    try:
        with urllib.request.urlopen(spec["url"], timeout=180) as source:
            with temporary.open("wb") as output:
                shutil.copyfileobj(source, output)
        if digest(temporary) != spec["sha256"]:
            raise RuntimeError(f"DESKTOP_DOWNLOAD_HASH_MISMATCH: {cached.name}")
        temporary.replace(cached)
    finally:
        temporary.unlink(missing_ok=True)
    return cached


def unpack(archive: Path, destination: Path, *, strip_root: bool = False) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            parts = Path(member.filename).parts[1:] if strip_root else Path(member.filename).parts
            if not parts:
                continue
            target = destination.joinpath(*parts).resolve()
            if not target.is_relative_to(destination):
                raise RuntimeError("DESKTOP_UNSAFE_ARCHIVE_PATH")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as input_file, target.open("wb") as output:
                    shutil.copyfileobj(input_file, output)


def prepare(*, source_only: bool = False, build_native: bool = False) -> dict:
    from learning_app.workbench.resources import ReadingResources

    lock = json.loads((ROOT / "harness/desktop.lock.json").read_text("utf-8"))
    source = lock["reference_source"]
    archive = fetch_verified(
        {**source, "url": source["archive_url"]},
        ROOT / ".local/cache/harness-source" / f"{source['commit']}.zip",
    )
    upstream = ROOT / ".runtime/desktop/upstream"
    source_marker = upstream / ".learning-source.json"
    if not source_marker.is_file():
        unpack(archive, upstream, strip_root=True)
        source_marker.write_text(json.dumps(source, indent=2), "utf-8")
    elif json.loads(source_marker.read_text("utf-8")) != source:
        raise RuntimeError("DESKTOP_SOURCE_VERSION_MISMATCH")
    assets = ROOT / ".runtime/desktop/resources"
    assets.mkdir(parents=True, exist_ok=True)
    for name in ("icon-windows.png", "tray-windows.ico"):
        shutil.copyfile(upstream / "apps/desktop/resources" / name, assets / name)
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(upstream / name, assets / name)
    result = {"source": str(upstream), "reference_commit": source["commit"]}
    result["pdfjs"] = str(ReadingResources(ROOT).prepare())
    native_manifest = json.loads((ROOT / "desktop/native-workspace.sources.json").read_text("utf-8"))
    native_output = ROOT / native_manifest["output"]
    if build_native or not native_output.is_file():
        node = ROOT / ".tools/node/node.exe"
        environment = {key: value for key, value in os.environ.items() if key not in {"NODE_OPTIONS", "ELECTRON_RUN_AS_NODE"}}
        subprocess.run(
            [str(node), str(ROOT / "scripts/desktop/build-native.mjs")],
            cwd=ROOT, env=environment, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    if digest(native_output) != native_manifest["output_sha256"]:
        raise RuntimeError("DESKTOP_NATIVE_ARTIFACT_HASH_MISMATCH")
    result["native_workspace_sha256"] = native_manifest["output_sha256"]
    if not source_only:
        # These are project learning modules compiled against the original
        # Harness React/primitives; generated bundles stay in the runtime cache.
        for script in ("build-learning.mjs", "build-video.mjs"):
            subprocess.run(
                [str(ROOT / ".tools/node/node.exe"), str(ROOT / "scripts/desktop" / script)],
                cwd=ROOT,
                env={key: value for key, value in os.environ.items()
                     if key not in {"NODE_OPTIONS", "ELECTRON_RUN_AS_NODE"}},
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        subprocess.run(
            [str(ROOT / ".tools/node/node.exe"), str(ROOT / "scripts/workbench/build-offline.mjs")],
            cwd=ROOT, check=True,
            env={key: value for key, value in os.environ.items()
                 if key not in {"NODE_OPTIONS", "ELECTRON_RUN_AS_NODE"}},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        subprocess.run(
            [str(ROOT / ".tools/node/node.exe"), str(ROOT / "scripts/workbench/build-content-helper.mjs")],
            cwd=ROOT, check=True,
            env={key: value for key, value in os.environ.items()
                 if key not in {"NODE_OPTIONS", "ELECTRON_RUN_AS_NODE"}},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        electron = lock["electron"]
        bundle = fetch_verified(
            electron,
            ROOT / ".local/cache/electron" / Path(electron["url"]).name,
        )
        runtime = ROOT / ".runtime/desktop/electron"
        marker = runtime / ".learning-runtime.json"
        executable = runtime / "electron.exe"
        if not marker.is_file():
            unpack(bundle, runtime)
            marker.write_text(json.dumps({**electron, "executable_sha256": digest(executable)}, indent=2), "utf-8")
        else:
            installed = json.loads(marker.read_text("utf-8"))
            if {key: installed.get(key) for key in electron} != electron:
                raise RuntimeError("DESKTOP_ELECTRON_VERSION_MISMATCH")
            if installed.get("executable_sha256") != digest(executable):
                # Older preparation markers are upgraded only after checking
                # the actual executable against the verified archive bytes.
                with zipfile.ZipFile(bundle) as source:
                    expected = hashlib.sha256(source.read("electron.exe")).hexdigest()
                if digest(executable) != expected:
                    raise RuntimeError("DESKTOP_ELECTRON_EXECUTABLE_HASH_MISMATCH")
                marker.write_text(json.dumps({**electron, "executable_sha256": expected}, indent=2), "utf-8")
        if not executable.is_file():
            raise RuntimeError("DESKTOP_ELECTRON_MISSING")
        result["electron"] = str(executable)
        result["electron_version"] = electron["version"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="准备固定版本的官方桌面素材与 Electron")
    parser.add_argument("--source-only", action="store_true")
    parser.add_argument("--build-native", action="store_true", help="使用项目固定 rolldown 重建官方 Workspace 浏览器模块")
    args = parser.parse_args()
    print(json.dumps(prepare(source_only=args.source_only, build_native=args.build_native), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
