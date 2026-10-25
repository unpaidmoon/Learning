"""Assemble an allowlisted Windows closure; never include local user/run data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from packaging.markers import Marker
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".local/cache/desktop"


def digest(path):
    with path.open("rb") as incoming:
        return hashlib.file_digest(incoming, "sha256").hexdigest()


def child_environment():
    env = dict(os.environ)
    for name in list(env):
        if name.upper() in {
            "PYTHONHOME",
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "NODE_OPTIONS",
            "ELECTRON_RUN_AS_NODE",
            "DEEPSEEK_API_KEY",
            "DEEPSEEK_AUTH_TOKEN",
        }:
            env.pop(name, None)
    env["SOURCE_DATE_EPOCH"] = "1792713600"
    return env


def fetch(spec, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        temporary = path.with_suffix(path.suffix + ".partial")
        with (
            urllib.request.urlopen(spec["url"], timeout=180) as source,
            temporary.open("xb") as out,
        ):
            shutil.copyfileobj(source, out, 1024 * 1024)
        if digest(temporary) != spec["sha256"]:
            raise RuntimeError("PORTABLE_DOWNLOAD_HASH_MISMATCH: " + path.name)
        temporary.replace(path)
    if digest(path) != spec["sha256"] or (
        spec.get("bytes") is not None and path.stat().st_size != spec["bytes"]
    ):
        raise RuntimeError("PORTABLE_ARCHIVE_INVALID: " + path.name)
    return path


def archive_members(archive):
    for member in archive.infolist():
        parts = PurePosixPath(member.filename).parts
        if (
            not parts
            or ".." in parts
            or "\\" in member.filename
            or ":" in member.filename
            or PurePosixPath(member.filename).is_absolute()
            or (member.external_attr >> 16) & 0o170000 == 0o120000
        ):
            raise RuntimeError("PORTABLE_ARCHIVE_PATH_DENIED")
        if not member.is_dir():
            yield member, parts


class Bundle:
    def __init__(self, output):
        self.output = output.resolve()
        self.output.mkdir(parents=True, exist_ok=False)

    def write(self, name, content):
        target = self.output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as out:
            out.write(content)

    def copy(self, source, name=None):
        source = source.absolute()
        if any(p.is_symlink() or p.is_junction() for p in [source, *source.parents]):
            raise RuntimeError("PORTABLE_INPUT_LINK_DENIED: " + str(source))
        target = self.output / (name or source.relative_to(ROOT))
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if digest(source) != digest(target):
                raise RuntimeError("PORTABLE_INPUT_COLLISION: " + str(target))
            return
        shutil.copyfile(source, target)

    def tree(self, relative, *, source_only=False):
        root = ROOT / relative
        if not root.is_dir():
            raise RuntimeError("PORTABLE_INPUT_MISSING: " + relative)
        excluded = {"__pycache__", ".git", ".venv", ".pytest_cache"}
        if not relative.startswith(".runtime/"):
            excluded.update(
                {
                    "node_modules",
                    "target",
                    "verification",
                    "verification-r1",
                    "verification-r2",
                }
            )
        suffixes = {
            ".py",
            ".rs",
            ".ts",
            ".tsx",
            ".js",
            ".mjs",
            ".cjs",
            ".css",
            ".html",
            ".json",
            ".toml",
            ".lock",
            ".yaml",
            ".yml",
            ".md",
            ".txt",
            ".svg",
        }
        for directory, folders, names in os.walk(root, followlinks=False):
            folders[:] = sorted(name for name in folders if name not in excluded)
            for name in sorted(names):
                path = Path(directory) / name
                if path.suffix in {".pyc", ".pyo"} or name in {".DS_Store"}:
                    continue
                if (
                    source_only
                    and path.suffix not in suffixes
                    and not name.startswith(("LICENSE", "NOTICE", "COPYING"))
                ):
                    continue
                self.copy(path)


def wheels():
    """Resolve only edges already frozen by uv, then verify actual wheel bytes."""
    lock = tomllib.loads((ROOT / "backend/uv.lock").read_text("utf-8"))
    packages = {canonicalize_name(p["name"]): p for p in lock["package"]}
    queue, selected = ["learning-app"], {}
    while queue:
        name = canonicalize_name(queue.pop())
        if name in selected:
            continue
        package = packages[name]
        selected[name] = package
        for edge in package.get("dependencies", []):
            if not edge.get("marker") or Marker(edge["marker"]).evaluate({"extra": ""}):
                queue.append(edge["name"])
    wheel_dir = CACHE / "wheels"
    build = subprocess.run(
        [
            str(ROOT / ".tools/uv/uv.exe"),
            "build",
            str(ROOT / "third_party/deeptutor"),
            "--wheel",
            "--no-sources",
            "--no-python-downloads",
            "--python",
            sys.executable,
            "--out-dir",
            str(wheel_dir),
            "--no-create-gitignore",
            "--cache-dir",
            str(ROOT / ".local/cache/uv-build"),
        ],
        cwd=ROOT,
        env=child_environment(),
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if build.returncode:
        raise RuntimeError("PORTABLE_REUSE_WHEEL_BUILD_FAILED: " + build.stderr[-2000:])
    paths, records = [], []
    tags = {tag: order for order, tag in enumerate(sys_tags())}
    for name, package in sorted(selected.items()):
        if name == "learning-app":
            continue
        if name == "learning-deeptutor-reuse":
            path = (
                wheel_dir
                / f"learning_deeptutor_reuse-{package['version']}-py3-none-any.whl"
            )
            spec = {
                "name": name,
                "version": package["version"],
                "sha256": digest(path),
                "bytes": path.stat().st_size,
                "source": "third_party/deeptutor",
                "filename": path.name,
            }
        else:
            candidates = []
            for item in package.get("wheels", []):
                filename = item["url"].rsplit("/", 1)[-1]
                matching = parse_wheel_filename(filename)[3] & tags.keys()
                if matching:
                    candidates.append(
                        (min(tags[tag] for tag in matching), filename, item)
                    )
            if not candidates:
                raise RuntimeError("PORTABLE_FIXED_WHEEL_MISSING: " + name)
            _, filename, item = min(candidates)
            spec = {
                "name": name,
                "version": package["version"],
                "sha256": item["hash"].split(":", 1)[1],
                "bytes": item["size"],
                "url": item["url"],
                "filename": filename,
            }
            path = fetch(spec, wheel_dir / filename)
        paths.append(path)
        records.append(spec)
    return paths, records


def install_wheels(bundle, paths):
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            for member, parts in archive_members(archive):
                if parts[0].endswith(".data"):
                    if len(parts) > 2 and parts[1] in {"purelib", "platlib"}:
                        parts = parts[2:]
                    else:
                        # Console entry points are never executed by the app.
                        continue
                if member.filename.endswith(".pth"):
                    raise RuntimeError("PORTABLE_WHEEL_PATH_HOOK_DENIED")
                target = "runtime/python/site-packages/" + "/".join(parts)
                bundle.write(target, archive.read(member))


def video_preview_files():
    from learning_app.video_previews import TOOL_SHA256, TOOL_VERSION

    spec = json.loads((ROOT / "harness/portable.lock.json").read_text("utf-8"))[
        "video_preview"
    ]
    if (
        spec["version"] != TOOL_VERSION
        or spec["files"]["ffmpeg.exe"]["sha256"] != TOOL_SHA256
    ):
        raise RuntimeError("PORTABLE_VIDEO_PREVIEW_PIN_MISMATCH")
    if set(spec["files"]) != {"ffmpeg.exe", "LICENSE", "README.txt"}:
        raise RuntimeError("PORTABLE_VIDEO_PREVIEW_FILES_INVALID")
    installed = Path(os.environ.get("LOCALAPPDATA", "")) / (
        "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/"
        f"ffmpeg-{TOOL_VERSION}-full_build"
    )
    for directory, executable in [
        (ROOT / ".tools/ffmpeg", "ffmpeg.exe"),
        (installed, "bin/ffmpeg.exe"),
    ]:
        files = {
            name: directory / (executable if name == "ffmpeg.exe" else name)
            for name in spec["files"]
        }
        if all(
            path.is_file()
            and path.stat().st_size == spec["files"][name]["bytes"]
            and digest(path) == spec["files"][name]["sha256"]
            for name, path in files.items()
        ):
            return files
    raise RuntimeError("PORTABLE_VIDEO_PREVIEW_RESOURCES_MISSING_OR_CHANGED")


def validate_inputs():
    from learning_app.workbench.asr_resources import AsrResources
    from learning_app.workbench.content_processing import ContentProcessor
    from learning_app.workbench.essay_processing import EssayProcessor
    from learning_app.workbench.ocr_resources import OcrResources
    from learning_app.workbench.offline_resources import OfflineRuntime
    from learning_app.workbench.resources import ReadingResources
    from learning_app.workbench.scheduler import CalculationHelper

    ContentProcessor(ROOT).verified()
    EssayProcessor(ROOT).verified()
    OfflineRuntime(ROOT).read()
    OcrResources(ROOT).launch_spec()
    AsrResources(ROOT).launch_spec()
    video_preview_files()
    for name in ("question", "fsrs"):
        CalculationHelper(name, ROOT).verified()
    reading = ReadingResources(ROOT)
    for name, spec in reading.files.items():
        path = reading.path(reading.lock["version"], name)
        if path.stat().st_size != spec["bytes"] or digest(path) != spec["sha256"]:
            raise RuntimeError("PORTABLE_READING_RESOURCE_INVALID: " + name)
    for name in (
        "scripts/desktop/build-native.mjs",
        "scripts/desktop/build-learning.mjs",
        "scripts/desktop/build-video.mjs",
        "scripts/workbench/build-offline.mjs",
        "scripts/workbench/build-offline-chat.mjs",
        "scripts/workbench/build-content-helper.mjs",
    ):
        result = subprocess.run(
            [str(ROOT / ".tools/node/node.exe"), str(ROOT / name), "--check"],
            cwd=ROOT,
            env=child_environment(),
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            raise RuntimeError(
                "PORTABLE_BUILT_RESOURCE_STALE: " + name + ": " + result.stderr[-1000:]
            )


def main():
    parser = argparse.ArgumentParser(
        description="打包固定 Windows 运行闭包，不打包用户数据"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="必须为尚不存在的独立目录"
    )
    parser.add_argument("--release-id", required=True)
    parser.add_argument(
        "--rustc", default="rustc.exe", help="仅构建端使用的固定 Rust 编译器"
    )
    parser.add_argument(
        "--zip", action="store_true", help="另生成可解压运行的 ZIP64 包"
    )
    args = parser.parse_args()
    if os.name != "nt" or sys.version_info[:2] != (3, 12):
        raise RuntimeError("PORTABLE_BUILD_PLATFORM_INVALID")
    lock = json.loads((ROOT / "harness/portable.lock.json").read_text("utf-8"))
    if (
        subprocess.check_output([args.rustc, "--version"], text=True).strip()
        != lock["launcher_build"]["rustc"]
    ):
        raise RuntimeError("PORTABLE_LAUNCHER_TOOLCHAIN_MISMATCH")
    validate_inputs()
    paths, dependencies = wheels()
    bundle = Bundle(args.output)
    for relative in (
        "backend/src",
        "desktop",
        "frontend/dist",
        "frontend/src",
        "resources",
        "harness/profiles",
        "harness/plugins",
        "third_party/deeptutor/src",
        "third_party/deeptutor/dependency-licenses",
        "third_party/deepstudent/upstream",
        "third_party/deepstudent/web",
        "third_party/deepstudent/scripts",
        "third_party/deepstudent/runtime",
        "third_party/deepstudent/notices",
    ):
        bundle.tree(relative)
    for relative in (
        "scripts/desktop",
        "scripts/workbench",
        "third_party/deepstudent/checks",
    ):
        bundle.tree(relative, source_only=True)
    for name in (
        "scripts/app.py",
        "backend/pyproject.toml",
        "backend/uv.lock",
        "package.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "frontend/package.json",
        "frontend/tsconfig.json",
        "harness/runtime.lock.json",
        "harness/desktop.lock.json",
        "harness/portable.lock.json",
        "third_party/deeptutor/pyproject.toml",
        "third_party/deeptutor/reuse-manifest.json",
        "third_party/deeptutor/upstream-lock.json",
        "third_party/deeptutor/LICENSE",
        "third_party/deeptutor/UPSTREAM.md",
        "third_party/deepstudent/manifest.json",
        "third_party/deepstudent/dependency-manifest.json",
    ):
        bundle.copy(ROOT / name)
    for name, path in video_preview_files().items():
        bundle.copy(path.resolve(), ".tools/ffmpeg/" + name)
    bundle.tree(".runtime/desktop/client")
    bundle.tree(".runtime/desktop/resources")
    for family, lock_name in (
        ("content", "content"),
        ("essay", "essay"),
        ("offline-reader", "offline"),
        ("offline-chat-reader", "offline-chat"),
        ("schedulers", "fsrs"),
        ("schedulers", "question"),
        ("asr", "asr"),
    ):
        resource = json.loads(
            (ROOT / f"resources/workbench/{lock_name}-runtime.lock.json").read_text(
                "utf-8"
            )
        )
        bundle.tree(f".runtime/workbench/{family}/{resource['release_id']}")
    bundle.tree(".runtime/workbench/ocr")
    pdf_lock = json.loads(
        (ROOT / "resources/workbench/engines.lock.json").read_text("utf-8")
    )["pdfjs"]
    bundle.tree(f".runtime/workbench/resources/pdfjs/{pdf_lock['version']}")
    harness = json.loads((ROOT / "harness/runtime.lock.json").read_text("utf-8"))
    for key in ("executable", "ripgrep"):
        spec = harness["artifact"][key]
        source = ROOT / ".runtime/harness/install" / spec["filename"]
        if digest(source) != spec["sha256"]:
            raise RuntimeError("PORTABLE_HARNESS_HASH_MISMATCH")
        bundle.copy(source)
    bundle.tree(".runtime/harness/install/licenses")
    for name in ("deepseek-harness-runtime.json",):
        bundle.copy(ROOT / ".runtime/harness/install" / name)
    for key in ("python", "node"):
        spec = lock[key]
        path = fetch(spec, CACHE / spec["filename"])
        with zipfile.ZipFile(path) as archive:
            for member, parts in archive_members(archive):
                if key == "node":
                    if len(parts) != 2 or parts[-1] not in {"node.exe", "LICENSE"}:
                        continue
                    target = ".tools/node/" + parts[-1]
                else:
                    if parts[-1].endswith("._pth"):
                        continue
                    target = "runtime/python/" + "/".join(parts)
                bundle.write(target, archive.read(member))
    bundle.write(
        "runtime/python/python312._pth",
        b"python312.zip\n.\nsite-packages\n../../backend/src\nimport site\n",
    )
    install_wheels(bundle, paths)
    desktop = json.loads((ROOT / "harness/desktop.lock.json").read_text("utf-8"))
    spec = desktop["electron"]
    archive_path = fetch(
        spec, ROOT / ".local/cache/electron" / spec["url"].rsplit("/", 1)[-1]
    )
    with zipfile.ZipFile(archive_path) as archive:
        for member, _ in archive_members(archive):
            bundle.write(
                ".runtime/desktop/electron/" + member.filename, archive.read(member)
            )
        exe_hash = hashlib.sha256(archive.read("electron.exe")).hexdigest()
    bundle.write(
        ".runtime/desktop/electron/.learning-runtime.json",
        json.dumps({**spec, "executable_sha256": exe_hash}).encode(),
    )
    subprocess.run(
        [
            args.rustc,
            str(ROOT / "scripts/desktop/launcher.rs"),
            "--edition=2024",
            "-O",
            "-C",
            "target-feature=+crt-static",
            "-C",
            "debuginfo=0",
            "-C",
            "strip=debuginfo",
            "-o",
            str(bundle.output / "启动学习.exe"),
        ],
        check=True,
        cwd=ROOT,
        env=child_environment(),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    for suffix in (".pdb",):
        (bundle.output / ("启动学习" + suffix)).unlink(missing_ok=True)
    bundle.write(
        "使用说明.txt",
        (
            "AI 辅助学习 Windows 运行候选包\n\n完整解压到可写目录，双击“启动学习.exe”。无需安装 Python、Node、Rust 或开发依赖。\n"
            "软件数据默认保存在 %LOCALAPPDATA%\\LearningWorkbench，与运行包分开。原始课件、视频仍原地引用。\n"
            "关闭窗口后后台保留；从托盘或菜单选择“退出”请求保存并退出。再次双击恢复现有窗口。\n"
            "包内不包含密钥、账号、聊天、课程或用户素材。模型登录和凭据使用软件已有入口。\n"
            "这是技术验收候选包；全部学习场景、数据迁移和真实学习体验的统一验收尚未完成。\n"
            "源码、固定依赖、许可与通知随包保留；资源清单见 desktop-release.json。\n"
        ).encode("utf-8-sig"),
    )
    files = {}
    for directory, _, names in os.walk(bundle.output):
        for name in sorted(names):
            path = Path(directory) / name
            files[path.relative_to(bundle.output).as_posix()] = {
                "bytes": path.stat().st_size,
                "sha256": digest(path),
            }
    manifest = {
        "format": 1,
        "platform": "win32-x64",
        "release_id": args.release_id,
        "status": "candidate",
        "python": lock["python"],
        "node": lock["node"],
        "dependencies": dependencies,
        "files": files,
        "source_locks": {
            name: digest(ROOT / name)
            for name in (
                "backend/uv.lock",
                "pnpm-lock.yaml",
                "harness/runtime.lock.json",
                "harness/desktop.lock.json",
                "harness/portable.lock.json",
            )
        },
    }
    bundle.write(
        "desktop-release.json",
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    if args.zip:
        with zipfile.ZipFile(
            str(bundle.output) + ".zip",
            "x",
            zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            for name in [*files, "desktop-release.json"]:
                archive.write(bundle.output / name, args.release_id + "/" + name)
    print(
        json.dumps(
            {
                "output": str(bundle.output),
                "files": len(files),
                "bytes": sum(row["bytes"] for row in files.values()),
                "dependencies": len(dependencies),
                "manifest_sha256": digest(bundle.output / "desktop-release.json"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
