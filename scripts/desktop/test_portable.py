"""Integrity failures must stop a release before any application is started."""

import importlib.util
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest


def module(name):
    path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


portable = module("portable")
builder = module("build_portable")


def release(tmp_path):
    bundle = builder.Bundle(tmp_path / "release")
    names = {
        "runtime/python/python.exe",
        "runtime/python/pythonw.exe",
        "runtime/python/python312._pth",
        "scripts/app.py",
        "scripts/desktop/run.py",
        "desktop/main.cjs",
        "启动学习.exe",
    }
    for name in names:
        bundle.write(name, b"integrity fixture only")
    manifest = {
        "format": 1,
        "platform": "win32-x64",
        "release_id": "fixture",
        "files": {
            name: {
                "bytes": (bundle.output / name).stat().st_size,
                "sha256": builder.digest(bundle.output / name),
            }
            for name in names
        },
    }
    bundle.write("desktop-release.json", json.dumps(manifest).encode())
    return bundle.output, manifest


def test_tampered_runtime_and_missing_launch_input_stop_before_start(tmp_path):
    root, manifest = release(tmp_path)
    assert portable.verify_release(root)["release_id"] == "fixture"
    runtime = root / "runtime/python/python.exe"
    original = runtime.read_bytes()
    runtime.write_bytes(b"X" * len(original))
    with pytest.raises(RuntimeError, match="HASH_MISMATCH"):
        portable.verify_release(root)
    runtime.write_bytes(original)
    del manifest["files"]["scripts/desktop/run.py"]
    (root / "desktop-release.json").write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="RELEASE_INCOMPLETE"):
        portable.verify_release(root)


@pytest.mark.parametrize("name", ["../outside", "C:/outside", "folder\\outside"])
def test_release_rejects_paths_outside_its_root(tmp_path, name):
    root, manifest = release(tmp_path)
    manifest["files"][name] = {"bytes": 0, "sha256": "0" * 64}
    (root / "desktop-release.json").write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="PATH_DENIED"):
        portable.verify_release(root)


@pytest.mark.parametrize(
    "name", ["../outside", "/absolute", "C:/outside", "folder\\..\\outside"]
)
def test_fixed_archive_unpacking_rejects_traversal(tmp_path, name):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(name, b"synthetic archive")
    with (
        zipfile.ZipFile(data) as archive,
        pytest.raises(RuntimeError, match="ARCHIVE_PATH_DENIED"),
    ):
        list(builder.archive_members(archive))
    assert list(tmp_path.iterdir()) == []


def test_builder_does_not_overlay_an_existing_data_directory(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    marker = target / "user-asset.txt"
    marker.write_bytes(b"preserve existing data")
    with pytest.raises(FileExistsError):
        builder.Bundle(target)
    assert marker.read_bytes() == b"preserve existing data"


@pytest.mark.parametrize("damage", [None, "executable", "license", "pin"])
def test_preview_executable_and_notices_are_a_required_fixed_release_input(
    tmp_path, monkeypatch, damage
):
    import learning_app.video_previews as previews

    root = tmp_path / "root"
    source = root / ".tools/ffmpeg"
    source.mkdir(parents=True)
    files = {
        "ffmpeg.exe": b"owned executable fixture",
        "LICENSE": b"license fixture",
        "README.txt": b"notices fixture",
    }
    for name, content in files.items():
        (source / name).write_bytes(content)
    expected = hashlib.sha256(files["ffmpeg.exe"]).hexdigest()
    monkeypatch.setattr(previews, "TOOL_SHA256", expected)
    spec = {
        "video_preview": {
            "version": previews.TOOL_VERSION,
            "files": {
                name: {
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for name, content in files.items()
            },
        }
    }
    (root / "harness").mkdir()
    if damage == "pin":
        spec["video_preview"]["files"]["ffmpeg.exe"]["sha256"] = "0" * 64
    (root / "harness/portable.lock.json").write_text(json.dumps(spec), "utf-8")
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-global-runtime"))
    if damage == "executable":
        (source / "ffmpeg.exe").write_bytes(b"changed executable")
    if damage == "license":
        (source / "LICENSE").unlink()
    if damage:
        with pytest.raises(RuntimeError, match="VIDEO_PREVIEW"):
            builder.video_preview_files()
    else:
        assert builder.video_preview_files() == {name: source / name for name in files}
