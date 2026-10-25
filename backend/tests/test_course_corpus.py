"""Synthetic immutable course evidence; no provider or user files."""

import threading
from dataclasses import asdict

import pytest
from sqlalchemy import func, select
from test_native_business import call, prepare_chat, task_scope
from workbench_support import learning_course, note

from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.execution.native_tools import ToolRejected
from learning_app.storage import Repository
from learning_app.workbench import corpus_schema as c
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.corpus import CourseCorpus, concept_argument
from learning_app.workbench.corpus_models import CorpusPolicy
from learning_app.workbench.models import ArtifactUpdate, BlockChange
from learning_app.workbench.native_business import NativeBusiness
from learning_app.workbench.sources import SourceStore


def index_all(corpus, course):
    corpus.discover(course, force=True)
    for _ in range(200):
        status = corpus.status(course)
        if status.scan_state == "running":
            corpus.discover(course)
        elif not any(d.state in {"pending", "indexing"} for d in status.documents):
            return
        corpus.index_next(course)
    pytest.fail("synthetic index failed to settle")


def test_discovery_checkpoint_lossless_unicode_and_course_exclusions(tmp_path):
    repo, course, root, _ = learning_course(tmp_path)
    (root / "ignored").mkdir()
    (root / "ignored/private.md").write_text("矩形面积 Private sentinel", "utf-8")
    original = ("🙂 矩形面积 = 长 × 宽\n" * 250) + "末尾原样。\n"
    (root / "math.md").write_bytes(original.encode("utf-8"))
    (root / "captions.srt").write_text("矩形面积字幕不能全局检索", "utf-8")
    (root / "movie.mp4").write_bytes(b"matrix_area_video_forbidden")
    corpus = CourseCorpus(repo)
    value = CorpusPolicy(operation_id="exclude", expected_revision=0, exclusions=["ignored"])
    assert corpus.configure(course, value) == corpus.configure(course, value)
    index_all(corpus, course)
    status = corpus.status(course)
    assert {d.relative_path for d in status.documents} == {"lesson.txt", "math.md"}
    hits = corpus.search(course, "矩形面积")
    assert hits and all(h["snapshot"].content_role == "original" for h in hits)
    assert "Private sentinel" not in str(hits) and "字幕" not in str(hits)
    with repo.engine.connect() as conn:
        rows = conn.execute(
            select(s.snapshots.c.reference, s.snapshots.c.text)
            .join(c.chunks, c.chunks.c.snapshot_id == s.snapshots.c.snapshot_id)
            .join(c.documents)
            .where(c.documents.c.relative_path == "math.md")
            .order_by(s.snapshots.c.reference["start"].as_integer())
        ).all()
    assert "".join(row.text for row in rows) == original
    for row in rows:
        assert original[row.reference["start"] : row.reference["end"]] == row.text
    with pytest.raises(AppError) as conflict:
        corpus.configure(
            course, CorpusPolicy(operation_id="stale", expected_revision=0, exclusions=[])
        )
    assert conflict.value.code == "CORPUS_POLICY_CONFLICT"
    repo.close()


def test_directory_scan_resumes_after_restart_and_tracks_version_without_redirect(tmp_path):
    repo, course, root, _ = learning_course(tmp_path)
    for i in range(57):
        (root / f"d{i:02}.txt").write_text(f"概念守恒 unique{i}", "utf-8")
    corpus = CourseCorpus(repo)
    assert corpus.discover(course)
    assert corpus.status(course).scan_state == "running"
    repo.close()
    repo = Repository(tmp_path / "data")
    corpus = CourseCorpus(repo)
    index_all(corpus, course)
    assert len(corpus.status(course).documents) == 58
    original = next(h["snapshot"] for h in corpus.search(course, "unique12"))
    (root / "d12.txt").write_text("新版不同 concept", "utf-8")
    # On-disk version mismatch denies retrieval even before the next sweep.
    assert corpus.search(course, "unique12") == []
    with repo.engine.connect() as conn:
        assert corpus.availability(conn, course)["incomplete"]
    index_all(corpus, course)
    assert corpus.search(course, "unique12") == []
    assert corpus.search(course, "concept")
    assert SourceStore(repo).snapshot(course, original.snapshot_id) == original
    with pytest.raises(AppError):
        SourceStore(repo).require_current(original)
    (root / "d13.txt").unlink()
    index_all(corpus, course)
    assert corpus.search(course, "unique13") == []
    repo.close()


def test_native_concept_grants_read_only_current_course_and_revocation(tmp_path):
    repo, course, root, selection = learning_course(tmp_path)
    (root / "math.md").write_text("矩形面积 等于长乘宽。", "utf-8")
    other = tmp_path / "other"
    other.mkdir()
    (other / "math.md").write_text("矩形面积 OTHER_COURSE_SENTINEL", "utf-8")
    second = CourseFileStore(repo).bind(None, str(other))["course_id"]
    corpus = CourseCorpus(repo)
    index_all(corpus, course)
    index_all(corpus, second)
    _, binding = prepare_chat(repo, course, selection, "concept")
    port = NativeBusiness(repo)
    scope = port.ensure_chat_scope(binding.generation_id, binding)
    assert "search_course_concept" in scope["allowed_tools"]
    args = {"concept": "矩形面积"}
    query = call(binding, "search_course_concept", args)
    result = port.invoke(query, args)
    assert result["status"] == "matched" and "OTHER_COURSE" not in str(result)
    assert port.reconcile_effect(query) == result
    hit = result["results"][0]
    read_args = {key: hit[key] for key in ("handle_id", "source_version")}
    assert port.invoke(call(binding, "read_source_handle", read_args, seq=5), read_args)[
        "source"
    ] == hit["source"] | {"label": "已授权来源"}
    corpus.configure(
        course, CorpusPolicy(operation_id="revoke", expected_revision=0, exclusions=["math.md"])
    )
    with pytest.raises(ToolRejected, match="COURSE_CORPUS_SOURCE_DENIED"):
        port.authorize(call(binding, "read_source_handle", read_args, seq=7), read_args)
    # Explicitly submitted selection is an independent permission.
    explicit = {"handle_id": selection.snapshot_id, "source_version": selection.content_hash}
    assert (
        port.invoke(call(binding, "read_source_handle", explicit, seq=9), explicit)["source"][
            "text"
        ]
        == selection.text
    )
    for denied in (
        args | {"course_id": second},
        {"concept": "第12题"},
        {"concept": "原题全文"},
        {"path": "math.md"},
    ):
        with pytest.raises(ToolRejected):
            port.invoke(call(binding, "search_course_concept", denied), denied)
    with repo.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(s.artifacts)).scalar_one() == 0
        assert conn.execute(select(c.query_receipts.c.native_call)).scalar_one() == asdict(query)
    repo.close()


def test_retrieved_assets_keep_user_identity_and_current_revision(tmp_path):
    repo, course, _, selection = learning_course(tmp_path)
    artifact = ArtifactStore(repo).create(course, note("user_note", "矩形面积 用户理解")).artifact
    corpus = CourseCorpus(repo)
    hits = corpus.search(course, "矩形面积")
    assert hits[0]["snapshot"].content_role == "user_note"
    snapshot = hits[0]["snapshot"]
    ArtifactStore(repo).update(
        course,
        artifact.artifact_id,
        ArtifactUpdate(
            operation_id="edit",
            expected_revision_id=artifact.head_revision_id,
            changes=[
                BlockChange(
                    block_id="paragraph_1",
                    replacement={
                        "block_id": "paragraph_1",
                        "markdown": "改为三角形",
                        "attribution": "user_authored",
                    },
                )
            ],
        ),
    )
    assert corpus.search(course, "矩形面积") == []
    assert SourceStore(repo).snapshot(course, snapshot.snapshot_id).text == "矩形面积 用户理解"
    task, _, binding, port, scope = task_scope(repo, course, selection, "no_course_rag")
    assert task and "search_course_concept" not in scope["allowed_tools"]
    with pytest.raises(ToolRejected, match="NATIVE_TOOL_NOT_ALLOWED"):
        port.authorize(
            call(binding, "search_course_concept", {"concept": "矩形面积"}), {"concept": "矩形面积"}
        )
    repo.close()


def test_index_yields_without_publishing_a_partial_unit(tmp_path):
    repo, course, _, _ = learning_course(tmp_path)
    corpus = CourseCorpus(repo)
    corpus.discover(course)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(AppError) as interrupted:
        corpus.index_next(course, cancelled=cancelled)
    assert interrupted.value.code == "PREPARATION_INTERRUPTED"
    assert corpus.status(course).documents[0].cursor == 0
    index_all(corpus, course)
    assert corpus.status(course).documents[0].state == "ready"
    repo.close()


def test_large_text_yields_between_slices_and_resumes_with_global_codepoint_offsets(
    tmp_path, monkeypatch
):
    repo, course, root, _ = learning_course(tmp_path)
    (root / "lesson.txt").unlink()
    original = "\n🙂开头\n" + "段🙂字 " * 14_000 + "\n结尾\n"
    (root / "large.md").write_bytes(original.encode("utf-8"))
    corpus = CourseCorpus(repo)
    corpus.discover(course)
    cancelled = threading.Event()
    saved, calls = corpus.sources._save_verified, []

    def foreground_arrives(*args, **kwargs):
        result = saved(*args, **kwargs)
        calls.append(result.snapshot_id)
        cancelled.set()
        return result

    monkeypatch.setattr(corpus.sources, "_save_verified", foreground_arrives)
    with pytest.raises(AppError) as interrupted:
        corpus.index_next(course, cancelled=cancelled)
    assert interrupted.value.code == "PREPARATION_INTERRUPTED"
    assert len(calls) == 1, "foreground input must preempt before capturing another slice"
    with repo.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(c.chunks)).scalar_one() == 0
    monkeypatch.setattr(corpus.sources, "_save_verified", saved)
    cancelled.clear()
    index_all(corpus, course)
    large = next(doc for doc in corpus.status(course).documents if doc.relative_path == "large.md")
    assert large.total == large.cursor == 3
    with repo.engine.connect() as conn:
        rows = conn.execute(
            select(s.snapshots.c.reference, s.snapshots.c.text)
            .join(c.chunks, c.chunks.c.snapshot_id == s.snapshots.c.snapshot_id)
            .where(c.chunks.c.document_id == large.document_id)
            .order_by(s.snapshots.c.reference["start"].as_integer())
        ).all()
    assert "".join(row.text for row in rows) == original
    assert all(
        row.reference["page_index"] == 0
        and row.text == original[row.reference["start"] : row.reference["end"]]
        for row in rows
    )
    repo.close()


@pytest.mark.parametrize(
    "fault_point,committed",
    [("native_query_before_commit", False), ("native_query_after_commit", True)],
)
def test_query_receipt_loss_is_readonly_reconciled(tmp_path, fault_point, committed):
    repo, course, root, source = learning_course(tmp_path)
    (root / "math.txt").write_text("矩形面积 = 长乘宽", "utf-8")
    index_all(CourseCorpus(repo), course)
    _, binding = prepare_chat(repo, course, source, "query_fault")
    port = NativeBusiness(repo)
    port.ensure_chat_scope(binding.generation_id, binding)
    args = {"concept": "矩形面积"}
    native_call = call(binding, "search_course_concept", args)
    observed = []

    def fail(name, _):
        if name == fault_point:
            observed.append(name)
            raise OSError("synthetic receipt lost")

    repo.fault_hook = fail
    with pytest.raises(OSError, match="synthetic receipt lost"):
        port.invoke(native_call, args)
    repo.fault_hook = None
    original_search = CourseCorpus.search

    def no_search(*args, **kwargs):
        pytest.fail("recovery must not query or create handles again")

    CourseCorpus.search = no_search
    try:
        receipt = port.reconcile_effect(native_call)
        assert bool(receipt) is committed
        if committed:
            assert receipt["results"][0]["source"]["text"] == "矩形面积 = 长乘宽"
    finally:
        CourseCorpus.search = original_search
    assert observed == [fault_point]
    repo.close()


def test_native_pdf_page_index_reuses_reading_cache_and_keeps_page_locator(tmp_path, monkeypatch):
    from test_workbench_reading import course_pdf

    from learning_app.workbench.reading import DocumentReader

    repo, course, opened, _ = course_pdf(tmp_path, page_count=3)
    reader = DocumentReader(repo)
    for page in range(3):
        reader.page(course, opened["asset_id"], opened["source_version"], page)

    def no_parser(*args, **kwargs):
        pytest.fail("index must reuse the actual reading cache")

    monkeypatch.setattr(DocumentReader, "_run", no_parser)
    corpus = CourseCorpus(repo)
    index_all(corpus, course)
    hits = corpus.search(course, "Learning")
    assert {hit["snapshot"].reference.page_index for hit in hits} == {0, 1, 2}
    assert all(hit["snapshot"].reference.asset_id == opened["asset_id"] for hit in hits)
    assert corpus.status(course).documents[0].state == "ready"
    repo.close()


def test_program_evidence_retains_snapshot_and_file_version_after_file_changes(tmp_path):
    from learning_app.workbench.evidence import chat_evidence

    repo, course, root, source = learning_course(tmp_path)
    (root / "definition.md").write_text("矩形面积定义证据", "utf-8")
    corpus = CourseCorpus(repo)
    index_all(corpus, course)
    _, binding = prepare_chat(repo, course, source, "program_citation")
    port = NativeBusiness(repo)
    port.ensure_chat_scope(binding.generation_id, binding)
    args = {"concept": "矩形面积"}
    result = port.invoke(call(binding, "search_course_concept", args), args)
    card = next(
        card
        for card in chat_evidence(repo, course, "native_chat").items
        if card.label == "definition.md"
    )
    original = SourceStore(repo).snapshot(course, result["results"][0]["source"]["snapshot_id"])
    assert card.source == original
    assert card.source.reference.source_version != card.source.content_hash
    (root / "definition.md").write_text("NEW_FILE_CONTENT_SENTINEL", "utf-8")
    assert (
        next(
            card
            for card in chat_evidence(repo, course, "native_chat").items
            if card.label == "definition.md"
        ).source
        == original
    )
    with pytest.raises(AppError) as wrong_course:
        chat_evidence(repo, "wrong_course", "native_chat")
    assert wrong_course.value.code == "SOURCE_CHAT_DENIED"
    repo.close()


def test_original_deeptutor_metrics_evaluate_the_actual_retrieval_results(tmp_path):
    from deeptutor_reuse.eval_dataset import EvalDataset
    from deeptutor_reuse.eval_matching import RelevanceMatcher
    from deeptutor_reuse.eval_metrics import aggregate_query_metrics, evaluate_hits

    repo, course, root, _ = learning_course(tmp_path)
    facts = [
        ("动量守恒", "动量守恒：孤立系统的总动量不变。"),
        ("导数", "导数描述函数在一点的局部变化率。"),
        ("rectangle", "A rectangle has four right angles."),
    ]
    for i, (_, fact) in enumerate(facts):
        (root / f"concept-{i}.md").write_text(fact, "utf-8")
    corpus = CourseCorpus(repo)
    index_all(corpus, course)
    dataset = EvalDataset.from_cases(
        [{"id": str(i), "query": query, "gold": [gold]} for i, (query, gold) in enumerate(facts)],
        name="synthetic-literal-concepts",
    )
    metrics = []
    details = []
    for case in dataset.iter_cases():
        found = corpus.search(course, case.query)
        matcher = RelevanceMatcher(case.gold)
        hits = [set(matcher.match(item["snapshot"].text).indices) for item in found]
        score = evaluate_hits(case.id, hits, len(case.gold), 4)
        metrics.append(score)
        details.append(score.to_dict())
    report = {
        "dataset": dataset.name,
        "cases": details,
        "metrics": aggregate_query_metrics(metrics, 4),
        "scope": "three synthetic literal concepts only; no semantic, mathematical or authority accuracy claim",
    }
    import json

    (tmp_path / "retrieval-metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
    )
    assert report["metrics"]["recall@4"] == 1 and report["metrics"]["queries"] == 3
    repo.close()


def test_unready_index_is_not_reported_as_a_complete_miss_and_evidence_hash_is_checked(tmp_path):
    from sqlalchemy import update

    from learning_app.workbench.evidence import chat_evidence

    repo, course, root, source = learning_course(tmp_path)
    (root / "concept.md").write_text("尚未索引的材料", "utf-8")
    _, binding = prepare_chat(repo, course, source, "index_pending")
    port = NativeBusiness(repo)
    port.ensure_chat_scope(binding.generation_id, binding)
    args = {"concept": "不会命中"}
    result = port.invoke(call(binding, "search_course_concept", args), args)
    assert result["status"] == "not_ready" and result["external_fallback"] is False
    with repo.engine.begin() as conn:
        conn.execute(update(c.query_receipts).values(result={**result, "concept": "tampered"}))
    with pytest.raises(AppError) as corrupt:
        chat_evidence(repo, course, "native_chat")
    assert corrupt.value.code == "SOURCE_EVIDENCE_INTEGRITY"
    repo.close()


@pytest.mark.parametrize(
    "args",
    [
        {"concept": "a"},
        {"concept": "矩形\n面积"},
        {"concept": "x" * 81},
        {"concept": "ok", "path": "x"},
    ],
)
def test_concept_arguments_are_bounded(args):
    with pytest.raises(AppError) as rejected:
        concept_argument(args)
    assert rejected.value.code == "COURSE_CONCEPT_ARGUMENTS"
