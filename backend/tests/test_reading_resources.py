import hashlib
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.errors import AppError
from learning_app.workbench.resources import ReadingResources


def fixture_resources(tmp_path):
    root = tmp_path / "project"
    manifests = root / "resources/workbench"
    manifests.mkdir(parents=True)
    entries = {
        "LICENSE": b"synthetic resource fixture",
        "web/viewer.html": b"<!doctype html><html>fixture</html>",
        "build/pdf.mjs": b"export const fixture = true;",
    }
    archive = tmp_path / "synthetic-resources.zip"
    with zipfile.ZipFile(archive, "w") as packed:
        for name, data in entries.items():
            packed.writestr(name, data)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = manifests / "pdfjs-5.4.624.files.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "5.4.624",
                "artifact_sha256": digest,
                "files": [
                    {"path": key, "sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
                    for key, value in entries.items()
                ],
            }
        ),
        "utf-8",
    )
    (manifests / "engines.lock.json").write_text(
        json.dumps(
            {
                "pdfjs": {
                    "version": "5.4.624",
                    "archive_url": "https://github.com/mozilla/pdf.js/releases/download/v5.4.624/pdfjs-5.4.624-dist.zip",
                    "archive_sha256": digest,
                    "archive_bytes": archive.stat().st_size,
                    "files_manifest": manifest.name,
                    "files_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                }
            }
        ),
        "utf-8",
    )
    return root, archive


def test_product_resources_are_fixed_prepared_and_never_an_arbitrary_file_route(tmp_path):
    root, archive = fixture_resources(tmp_path)
    resources = ReadingResources(root)
    assert not resources.prepared()
    resources.prepare(archive)
    assert resources.prepared() and resources.prepare() == resources.directory
    with pytest.raises(AppError):
        resources.path("5.4.624", "../../secrets.txt")
    with pytest.raises(AppError):
        resources.path("latest", "build/pdf.mjs")
    with pytest.raises(AppError):
        resources.path("5.4.624", "C:/Windows/file")
    archive.write_bytes(b"corrupt archive")
    other = ReadingResources(root)
    other.directory = resources.base / "new-synthetic-test-only"
    with pytest.raises(AppError) as error:
        other.prepare(archive)
    assert error.value.code == "READING_RESOURCE_ARCHIVE_INVALID"


def test_start_mode_preserves_only_fixed_reader_csp_and_rejects_modified_script(tmp_path):
    root, archive = fixture_resources(tmp_path)
    resources = ReadingResources(root)
    resources.prepare(archive)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>fixture</title>", "utf-8")
    app = create_app(
        AppConfig(
            mode="start",
            project_root=root,
            data_dir=tmp_path / "data",
            runtime_dir=tmp_path / "run",
            static_dir=static,
        )
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        route = "/api/v1/reading/pdfjs/5.4.624/"
        assert client.get(route + "web/viewer.html").status_code == 403
        client.get("/api/v1/session")
        response = client.get(route + "web/viewer.html")
        assert (
            response.status_code == 200
            and "frame-ancestors 'self'" in response.headers["Content-Security-Policy"]
        )
        assert "'unsafe-eval'" not in response.headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in client.get("/").headers["Content-Security-Policy"]
        script = client.get(route + "build/pdf.mjs")
        assert script.status_code == 200 and script.headers["content-type"].startswith(
            "text/javascript"
        )
        assert client.get(route + "../unknown.txt").status_code == 404
        (resources.directory / "build/pdf.mjs").write_text("untrusted modified code", "utf-8")
        response = client.get(route + "build/pdf.mjs")
        assert (
            response.status_code == 503
            and response.json()["code"] == "READING_RESOURCE_CONTENT_INVALID"
        )
