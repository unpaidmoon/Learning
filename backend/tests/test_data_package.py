"""Whole-data format integrity, optional originals and new-stage-only extraction."""

import hashlib
import json
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from workbench_support import learning_course

from learning_app.data_package import DataPackage
from learning_app.errors import AppError


def export(repo, path, include_originals=False):
    stream, manifest = DataPackage(repo).build(include_originals=include_originals)
    with stream, path.open("wb") as output:
        while part := stream.read(1024 * 1024):
            output.write(part)
    return manifest


def rewrite(source, destination, member, content, *, update_hash=False, new_name=None):
    with ZipFile(source) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files["manifest.json"])
    files[member] = content
    if new_name:
        files[new_name] = files.pop(member)
    if update_hash:
        row = next(r for r in manifest["files"] if r["path"] == member)
        row.update(
            path=new_name or member, bytes=len(content), sha256=hashlib.sha256(content).hexdigest()
        )
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode()
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)


@pytest.mark.parametrize("originals", [False, True])
def test_default_and_optional_originals_roundtrip_are_complete_but_not_activated(
    tmp_path, originals
):
    repo, course, root, selected = learning_course(tmp_path)
    repo.create_chat(course, "chat")
    repo.publish_submission("chat", "保存当前问题", "pending")
    (root / "not-opened.txt").write_bytes(
        b"unopened original must be included only when explicitly selected"
    )
    (root / "duplicate.txt").write_bytes((root / "lesson.txt").read_bytes())
    path = tmp_path / "whole.zip"
    manifest = export(repo, path, originals)
    verified = DataPackage().verify(path)
    assert verified.manifest == manifest
    if originals:
        assert len(manifest["originals"]["course_files"]) == 3
        assert len([r for r in manifest["files"] if r["path"].startswith("originals/")]) == 2
        assert (
            manifest["originals"]["source_versions"][0]["source_version"]
            == selected.reference.source_version
        )
    else:
        assert not any(r["path"].startswith("originals/") for r in manifest["files"])
    target = tmp_path / "owned-stage"
    DataPackage().stage(path, target)
    assert (target / "data/business.sqlite3").is_file() and (
        target / "offline/index.html"
    ).is_file()
    assert not (target / "data/execution").exists()
    assert (
        json.loads((target / "manifest.json").read_text("utf-8"))["package_id"]
        == manifest["package_id"]
    )
    assert (
        repo.get_generation(repo.get_chat("chat")["generations"][0]["generation_id"])["state"]
        == "pending"
    )
    # Extracted data has not been opened by a service or given execution rights.
    repo.close()


def test_existing_target_is_not_merged_or_overwritten(tmp_path):
    repo, _, _, _ = learning_course(tmp_path)
    source = tmp_path / "whole.zip"
    export(repo, source)
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep.txt").write_text("existing user data", "utf-8")
    with pytest.raises(AppError) as error:
        DataPackage().stage(source, target)
    assert error.value.code == "DATA_IMPORT_TARGET_EXISTS"
    assert (target / "keep.txt").read_text("utf-8") == "existing user data"
    repo.close()


def test_old_original_version_cannot_be_replaced_by_same_name_new_bytes(tmp_path):
    repo, _, root, _ = learning_course(tmp_path)
    (root / "lesson.txt").write_bytes(b"replacement same name")
    with pytest.raises(AppError) as error:
        DataPackage(repo).build(include_originals=True)
    assert error.value.code == "DATA_ORIGINAL_VERSION_UNAVAILABLE"
    # Software facts still export without pretending the old original exists.
    stream, manifest = DataPackage(repo).build()
    stream.close()
    assert not manifest["include_originals"]
    assert (root / "lesson.txt").read_bytes() == b"replacement same name"
    repo.close()


@pytest.mark.parametrize("case", ["hash", "script", "html", "path", "undeclared"])
def test_tampered_members_or_unsafe_readers_are_rejected(tmp_path, case):
    repo, _, _, _ = learning_course(tmp_path)
    source = tmp_path / "source.zip"
    export(repo, source)
    bad = tmp_path / "bad.zip"
    if case == "hash":
        rewrite(source, bad, "data/business.sqlite3", b"bad DB bytes")
    elif case == "script":
        rewrite(source, bad, "offline/reader/reader.js", b"globalThis.BAD=true", update_hash=True)
    elif case == "html":
        rewrite(source, bad, "offline/index.html", b"<script>BAD()</script>", update_hash=True)
    elif case == "path":
        rewrite(
            source,
            bad,
            "data/business.sqlite3",
            b"data",
            update_hash=True,
            new_name="../escape.sqlite3",
        )
    else:
        with ZipFile(source) as incoming, ZipFile(bad, "w") as outgoing:
            for item in incoming.infolist():
                outgoing.writestr(item, incoming.read(item))
            outgoing.writestr("unlisted.key", b"unexpected")
    with pytest.raises(AppError):
        DataPackage().stage(bad, tmp_path / "stage")
    assert not (tmp_path / "stage").exists() and not (tmp_path / "escape.sqlite3").exists()
    repo.close()
