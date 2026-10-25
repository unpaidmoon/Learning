"""Make a self-contained operator kit from a verified release and owned examples."""

import argparse
import hashlib
import json
import re
import runpy
import shutil
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def document(archive):
    value = archive.read("index.html").decode("utf-8")
    match = re.search(
        r'<script type="application/json" id="learning-offline-data">(.*?)</script>',
        value,
        re.S,
    )
    if not match:
        raise RuntimeError("ACTUAL_OFFLINE_DOCUMENT_MISSING")
    return json.loads(match.group(1))


def extract_example(source, kind, destination):
    found = []
    for candidate in source.glob("*.zip"):
        with ZipFile(candidate) as archive:
            value = document(archive)
            if value["kind"] != kind:
                continue
            manifest = json.loads(archive.read("manifest.json"))
            for row in manifest["files"]:
                path = Path(row["path"])
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in row["path"]
                    or ":" in row["path"]
                ):
                    raise RuntimeError("EXAMPLE_MEMBER_PATH_DENIED")
                content = archive.read(row["path"])
                if (
                    len(content) != row["bytes"]
                    or hashlib.sha256(content).hexdigest() != row["sha256"]
                ):
                    raise RuntimeError("ACTUAL_EXAMPLE_HASH_MISMATCH")
                output = destination / row["path"]
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("xb") as stream:
                    stream.write(content)
            (destination / "manifest.json").write_bytes(archive.read("manifest.json"))
            found.append(
                {
                    "kind": kind,
                    "directory": destination.name,
                    "title": value["title"],
                    "source_zip_sha256": digest(candidate),
                    "revision_hash": value["revision_hash"],
                }
            )
    if len(found) != 1:
        raise RuntimeError("ACTUAL_EXAMPLE_NOT_UNIQUE:" + kind)
    return found[0]


def build(candidate, archive_path, output, zip_path):
    sys.path.insert(0, str(ROOT / "scripts/desktop"))
    from portable import verify_release

    manifest = verify_release(candidate)
    if (
        output.exists()
        or zip_path.exists()
        or archive_path.resolve() == zip_path.resolve()
    ):
        raise RuntimeError("ACCEPTANCE_OUTPUT_ALREADY_EXISTS")
    output.mkdir(parents=True, exist_ok=False)
    release_id = manifest["release_id"]
    copied = output / release_id
    # Runtime-created caches and private profiles never enter the kit.
    for name in [*manifest["files"], "desktop-release.json"]:
        path = copied / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(candidate / name, path)
    verify_release(copied)
    tools = output / "验收工具"
    tools.mkdir()
    for name in [
        "entry.ps1",
        "manual_launch.py",
        "offline_check.cjs",
        "offline_probe.py",
    ]:
        source = ROOT / "scripts/validation/windows_acceptance" / name
        # Windows PowerShell 5.1 needs the BOM for Chinese source text.
        (tools / name).write_text(
            source.read_text("utf-8"), "utf-8-sig" if name.endswith(".ps1") else "utf-8"
        )
    shutil.copyfile(
        ROOT / "scripts/validation/Run-PortableRecovery.py",
        tools / "Run-PortableRecovery.py",
    )
    shutil.copyfile(
        ROOT / "scripts/validation/windows_acceptance/验收说明.txt",
        output / "验收说明.txt",
    )
    assets = tools / "验收素材"
    assets.mkdir()
    runpy.run_path(str(ROOT / "tests/fixtures/workbench/generate.py"))["reading_pdf"](
        assets / "示例.pdf", page_count=3
    )
    (assets / "阅读说明.txt").write_text(
        "这是独立验收的自造资料。\n请检查文件阅读、视频全屏、确认截图和草稿恢复。\n真实学习资料与账号没有加入验收包。\n",
        "utf-8",
    )
    samples = tools / "离线示例"
    samples.mkdir()
    records = [
        extract_example(
            ROOT
            / "output/playwright/material-chain-output-browser-20261025-r2/downloads",
            "book",
            samples / "交互书",
        ),
        extract_example(
            ROOT
            / "output/playwright/deep-solution-output-browser-20261024-r3/downloads",
            "visualization",
            samples / "参数图",
        ),
        extract_example(
            ROOT
            / "output/playwright/material-chain-output-browser-20261025-r2/downloads",
            "card_set",
            samples / "回忆卡片",
        ),
    ]
    (samples / "samples.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), "utf-8"
    )
    for title, mode in {
        "开始验收": "Auto",
        "继续手动验收": "Manual",
        "查看验收结果": "Report",
        "汇总验收结果": "Collect",
    }.items():
        (output / (title + ".cmd")).write_bytes(
            (
                "@echo off\r\nchcp 65001 >nul\r\n"
                'powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0验收工具\\entry.ps1" -Mode '
                + mode
                + "\r\n"
                "echo.\r\npause\r\n"
            ).encode("utf-8")
        )
    config = {
        "format": 1,
        "release_id": release_id,
        "release_manifest_sha256": digest(copied / "desktop-release.json"),
        "python_sha256": manifest["python"]["executable_sha256"],
        "tools": [
            {
                "path": p.relative_to(tools).as_posix(),
                "bytes": p.stat().st_size,
                "sha256": digest(p),
            }
            for p in sorted(tools.rglob("*"))
            if p.is_file()
        ],
    }
    (tools / "kit.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), "utf-8"
    )
    additions = [
        path
        for path in output.rglob("*")
        if path.is_file() and not path.is_relative_to(copied)
    ]
    # Keep the verified, already-compressed release bytes. Append only the
    # reviewed kit; never rewrite or include a user's later acceptance profile.
    shutil.copyfile(archive_path, zip_path)
    with ZipFile(zip_path, "a", ZIP_DEFLATED, compresslevel=6) as archive:
        for path in additions:
            archive.write(path, path.relative_to(output).as_posix())
    expected = {
        release_id + "/" + name for name in [*manifest["files"], "desktop-release.json"]
    }
    expected.update(path.relative_to(output).as_posix() for path in additions)
    with ZipFile(zip_path) as archive:
        if set(archive.namelist()) != expected or len(archive.namelist()) != len(
            expected
        ):
            raise RuntimeError("ACCEPTANCE_ARCHIVE_MEMBERS_INVALID")
        for name, row in manifest["files"].items():
            with archive.open(release_id + "/" + name) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != row["sha256"]:
                    raise RuntimeError("ACCEPTANCE_ARCHIVE_HASH_MISMATCH:" + name)
    result = {
        "status": "passed",
        "release_id": release_id,
        "archive": str(zip_path.resolve()),
        "archive_bytes": zip_path.stat().st_size,
        "archive_sha256": digest(zip_path),
        "release_files": len(manifest["files"]),
        "kit_files": len(additions),
        "contains_user_materials": False,
        "contains_profile": False,
        "examples": records,
    }
    zip_path.with_suffix(".zip.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), "utf-8"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--release-archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--zip", type=Path, required=True)
    args = parser.parse_args()
    result = build(
        args.candidate.resolve(),
        args.release_archive.resolve(),
        args.output.resolve(),
        args.zip.resolve(),
    )
    print(
        json.dumps(
            {
                key: result[key]
                for key in [
                    "status",
                    "archive",
                    "archive_bytes",
                    "archive_sha256",
                    "release_files",
                    "kit_files",
                ]
            },
            ensure_ascii=False,
        )
    )
