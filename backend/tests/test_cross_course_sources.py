"""Explicit query courses and exact foreign evidence, never inferred authority."""

import pytest
from sqlalchemy import select, update
from test_course_corpus import index_all
from test_native_business import call
from workbench_support import learning_course, note

from learning_app.course_files import CourseFileStore
from learning_app.execution.native_tools import ToolRejected
from learning_app.execution.types import ExecutionBinding
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.corpus import CourseCorpus
from learning_app.workbench.native_business import NativeBusiness
from learning_app.workbench.source_grants import snapshot_grants
from learning_app.workbench.sources import SourceStore


def courses(tmp_path):
    repo, first, root, _ = learning_course(tmp_path)
    (root / "definition.md").write_text("共同概念 FIRST_COURSE_SENTINEL", "utf-8")
    other = tmp_path / "second-course"
    other.mkdir()
    (other / "definition.md").write_text("共同概念 SECOND_COURSE_SENTINEL", "utf-8")
    second = CourseFileStore(repo).bind(None, str(other), "对照课程")["course_id"]
    index_all(CourseCorpus(repo), first)
    index_all(CourseCorpus(repo), second)
    return repo, first, second


def scope(repo, owner, read_courses=(), conditions=()):
    repo.create_chat(owner, "cross_course_chat")
    result = repo.publish_submission(
        "cross_course_chat",
        "比较明确课程内的共同概念",
        "cross_course_submission",
        read_course_ids=read_courses,
        conditions=conditions,
    )
    binding = ExecutionBinding(
        "cross_course_chat",
        "cross_course_submission",
        result["generation_id"],
        1,
        "cross_course_sid",
        "cross_course_submission",
        native_floor=0,
        native_turn=1,
    )
    stored = repo.bind_execution(
        "cross_course_chat", binding.session_id, adapter_metadata=binding.to_dict()
    )
    assert repo.claim_dispatch(
        binding.generation_id,
        stored["binding_id"],
        1,
        request_id=binding.request_id,
        adapter_metadata=binding.to_dict(),
    )
    port = NativeBusiness(repo)
    frozen = port.ensure_chat_scope(binding.generation_id, binding)
    return port, binding, frozen


@pytest.mark.parametrize("mode", ["default", "second_only", "both"])
def test_only_explicit_courses_are_queried_and_origin_is_preserved(tmp_path, mode):
    repo, first, second = courses(tmp_path)
    selected = [] if mode == "default" else [second] if mode == "second_only" else [first, second]
    port, binding, frozen = scope(repo, first, selected)
    args = {"concept": "共同概念"}
    found = port.invoke(call(binding, "search_course_concept", args), args)
    text = str(found)
    assert ("FIRST_COURSE_SENTINEL" in text) is (mode != "second_only")
    assert ("SECOND_COURSE_SENTINEL" in text) is (mode != "default")
    assert frozen["course_id"] == first
    assert ArtifactStore(repo).list(first) == []
    if mode != "default":
        hit = next(
            item for item in found["results"] if item["source"]["source_course_id"] == second
        )
        snapshot = SourceStore(repo).snapshot(first, hit["source"]["snapshot_id"])
        assert snapshot.course_id == second and snapshot.content_role == "original"
        asset = ArtifactStore(repo).create(first, note(snapshot_id=snapshot.snapshot_id)).artifact
        assert asset.course_id == first and asset.revision.body.blocks[0].source_snapshot_ids == [
            snapshot.snapshot_id
        ]
        with repo.engine.connect() as conn:
            grant = (
                conn.execute(
                    select(snapshot_grants).where(
                        snapshot_grants.c.snapshot_id == snapshot.snapshot_id
                    )
                )
                .mappings()
                .one()
            )
            assert grant["owner_course_id"] == first and grant["source_course_id"] == second
    with pytest.raises(ToolRejected):
        invalid = {"concept": "共同概念", "course_id": second}
        port.invoke(call(binding, "search_course_concept", invalid, seq=7), invalid)
    repo.close()


def test_foreign_snapshot_without_explicit_selection_or_scoped_query_stays_denied(tmp_path):
    repo, first, second = courses(tmp_path)
    hit = CourseCorpus(repo).search(second, "共同概念")[0]["snapshot"]
    from learning_app.errors import AppError

    with pytest.raises(AppError) as rejected:
        SourceStore(repo).snapshot(first, hit.snapshot_id)
    assert rejected.value.code == "SOURCE_SELECTION_DENIED"
    repo.close()


def test_one_manually_referenced_foreign_snapshot_does_not_enable_its_course_search(tmp_path):
    from learning_app.drafts import source_selection_conditions

    repo, first, second = courses(tmp_path)
    hit = CourseCorpus(repo).search(second, "共同概念")[0]["snapshot"]
    with repo.engine.begin() as conn:
        conditions = source_selection_conditions(
            conn, first, [hit.snapshot_id], declare_foreign=True
        )
    port, binding, frozen = scope(repo, first, conditions=conditions)
    args = {"handle_id": hit.snapshot_id, "source_version": hit.content_hash}
    actual = port.invoke(call(binding, "read_source_handle", args), args)
    assert actual["source"]["source_course_id"] == second
    assert "corpus_course_ids" not in frozen.get("grants", {})
    query = {"concept": "共同概念"}
    results = port.invoke(call(binding, "search_course_concept", query, seq=6), query)
    assert "SECOND_COURSE_SENTINEL" not in str(results)
    assert "FIRST_COURSE_SENTINEL" in str(results)
    repo.close()


def test_scope_is_preserved_in_input_revision_and_new_scope_prevents_unsafe_downgrade(tmp_path):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from learning_app.storage import repository as storage

    repo, first, second = courses(tmp_path)
    _, binding, _ = scope(repo, first, [second])
    original = repo.get_generation(binding.generation_id)
    repo.revise_input(original["user_message_id"], "只修订问题文字，不改变既定课程范围")
    message = next(
        item for item in repo.get_chat("cross_course_chat")["messages"] if item["role"] == "user"
    )
    assert message["read_course_ids"] == [second]
    assert repo.get_generation(binding.generation_id)["context_snapshot"]["messages"][0][
        "read_course_ids"
    ] == [second]
    config = Config()
    config.set_main_option("script_location", str(Path(storage.__file__).parent / "migrations"))
    with pytest.raises(RuntimeError, match="CROSS_COURSE_DOWNGRADE_DENIED"):
        with repo.engine.begin() as conn:
            config.attributes["connection"] = conn
            command.downgrade(config, "0029_document_image_reads")
    assert repo.runtime_info()["schema_revision"] == storage.SCHEMA_REVISION
    repo.close()


def test_exact_foreign_reference_detects_changed_grant_hash(tmp_path):
    from learning_app.errors import AppError

    repo, first, second = courses(tmp_path)
    port, binding, _ = scope(repo, first, [second])
    args = {"concept": "共同概念"}
    hit = port.invoke(call(binding, "search_course_concept", args), args)["results"][0]
    with repo.engine.begin() as conn:
        conn.execute(
            update(snapshot_grants)
            .where(snapshot_grants.c.snapshot_id == hit["source"]["snapshot_id"])
            .values(source_hash="0" * 64)
        )
    with pytest.raises(AppError) as error:
        SourceStore(repo).snapshot(first, hit["source"]["snapshot_id"])
    assert error.value.code == "SOURCE_GRANT_CONFLICT"
    repo.close()
