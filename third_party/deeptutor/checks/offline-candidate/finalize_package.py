"""Freeze one local-assets-only reading artifact and a separately unpacked copy."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import zipfile

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[3]
PACKAGE = ROOT / "package"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    for relative in ["deeptutor/book/models.py", "deeptutor/book/compiler.py", "deeptutor/visualizers/protocol.py", "web/components/visualize/VisualizationViewer.tsx"]:
        target = ROOT / "upstream" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / "third_party/deeptutor/upstream" / relative, target)
    upstream_notice = """Selected DeepTutor reuse for the standalone offline candidate

Source: https://github.com/HKUDS/DeepTutor
Commit: a31d82624c27d75f4e771d4fcb78dce16470b383
License: Apache-2.0, original text in DeepTutor-LICENSE.

Build-time code retains the original Book/Page/Block models. The selected
BlockGenerator/BlockContext prefix, book/export.py and book/progress.py change
only model imports to the isolated reuse package. The default agent registry,
providers, storage, retries and live services are excluded.

runtime/upstream.js compiles unmodified quiz-question-type.ts and
book-reader-navigation.ts with TypeScript 5.9.3, using a classic-script exports
wrapper. The selected VisualizationViewer stripCodeFence/splitSvgBlocks bodies
are retained through the attributed visualizationParsers adapter; chart parsing
removes the upstream JSON-repair fallback and uses JSON.parse only.

runtime/reader.js and reader.css are new candidate implementations. They render
bounded data using DOM APIs and a fixed quadratic function, without evaluating
arbitrary scripts/expressions or embedding the upstream React/Next viewers.

KaTeX 0.16.22 script/CSS/fonts are copied unchanged from the project's pinned
package; its original license is in KaTeX-LICENSE. All material is synthetic.
"""
    (PACKAGE / "licenses/UPSTREAM.txt").write_text(upstream_notice, encoding="utf-8", newline="\n")
    (PACKAGE / "README.txt").write_text("先解压，再打开 index.html。\n无需启动本软件或联网。\n全部题文、公式、图片和参数数据均为合成示例。\n自测只保留本次打开期间的状态，不回传正式学习记录。\nbook.md 是文字投影，完整交互请使用 index.html。\n", encoding="utf-8", newline="\n")
    raw_model = ROOT / "evidence/python-model-compile.json"
    model_evidence = json.loads(raw_model.read_text("utf-8"))
    assert model_evidence["original_model_identity_passed"] and model_evidence["compiled_block_count"] == 6
    for script in [PACKAGE / "runtime/reader.js", PACKAGE / "runtime/upstream.js"]:
        text = script.read_text("utf-8")
        for pattern in [r"\beval\s*\(", r"\bnew\s+Function\b", r"\bfetch\s*\(", r"\bWebSocket\b", r"\bXMLHttpRequest\b", r"\blocalStorage\b", r"\bindexedDB\b", r"\bpostMessage\s*\("]:
            assert not re.search(pattern, text), (script.name, pattern)
    html = (PACKAGE / "index.html").read_text("utf-8")
    assert "connect-src 'none'" in html and "unsafe-eval" not in html
    files = []
    for path in sorted(PACKAGE.rglob("*")):
        if not path.is_file() or path.name == "asset-manifest.json":
            continue
        assert not path.is_symlink()
        relative = path.relative_to(PACKAGE).as_posix()
        assert relative in {"index.html", "book-data.json", "book.md", "README.txt"} or relative.startswith(("assets/", "sources/", "licenses/", "runtime/", "vendor/katex/"))
        assert not any(token in relative.lower() for token in ["secret", "cookie", ".env", ".sqlite", "profile"])
        files.append({"path": relative, "bytes": path.stat().st_size, "sha256": sha(path)})
    manifest = {"manifest_version": 1, "fixture_id": "offline-quadratic-book-r1", "source_commit": model_evidence["source_commit"],
                "katex_version": "0.16.22", "file_count_excluding_manifest": len(files),
                "runtime_dependency": "Browser with JavaScript; no application backend or network", "files": files}
    manifest_path = PACKAGE / "asset-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    archive_path = artifacts / "learning-book-candidate.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(p for p in PACKAGE.rglob("*") if p.is_file()):
            name = path.relative_to(PACKAGE).as_posix()
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 23, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    unpacked = ROOT / "verification/unpacked-r1"
    if unpacked.exists():
        raise RuntimeError("Unpacked test root already exists; retain it and choose a new revision")
    unpacked.mkdir(parents=True)
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            relative = PurePosixPath(member.filename)
            assert not relative.is_absolute() and ".." not in relative.parts and ":" not in member.filename and "\\" not in member.filename
            destination = unpacked.joinpath(*relative.parts)
            assert destination.resolve().is_relative_to(unpacked.resolve())
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(member))
    for record in files:
        actual = unpacked / record["path"]
        assert actual.stat().st_size == record["bytes"] and sha(actual) == record["sha256"]
    source_records = []
    for path in sorted(p for p in (ROOT / "upstream").rglob("*") if p.is_file()):
        source_records.append({"path": path.relative_to(ROOT).as_posix(), "sha256": sha(path), "bytes": path.stat().st_size})
    identity = {"fixture_id": manifest["fixture_id"], "zip_path": archive_path.relative_to(ROOT).as_posix(), "zip_sha256": sha(archive_path),
                "zip_bytes": archive_path.stat().st_size, "asset_manifest_sha256": sha(manifest_path),
                "entrypoint": "verification/unpacked-r1/index.html", "file_url": (unpacked / "index.html").as_uri(),
                "file_count_including_manifest": len(files) + 1, "all_unpacked_file_hashes_verified": True,
                "upstream_files": source_records}
    (ROOT / "evidence/package-identity.json").write_text(json.dumps(identity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in identity.items() if key != "upstream_files"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
