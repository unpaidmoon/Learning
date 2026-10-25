"""Synthetic proof of retained APIs; not product/Harness/user acceptance.

--installed never inserts src, so it tests the actual built wheel. All restart
fixtures are written under the caller's exclusive output directory.
"""

# ruff: noqa: E402 -- Deliberate source-vs-wheel bootstrap before library imports.
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument("--installed", action="store_true")
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
if not args.installed:
    sys.path.insert(0, str(ROOT / "src"))
args.output.mkdir(parents=True, exist_ok=False)

from pydantic import ValidationError
import deeptutor_reuse
from deeptutor_reuse.book_models import (
    Block,
    BlockStatus,
    BlockType,
    Book,
    BookInputs,
    BookStatus,
    Chapter,
    Page,
    Progress,
    QuizAttempt,
    SourceAnchor,
    Spine,
)
from deeptutor_reuse.book_block_compiler import BlockGenerator, GenerationFailure
from deeptutor_reuse.book_export import render_book_markdown
from deeptutor_reuse.book_progress import record_attempt, completion_ratio
from deeptutor_reuse.book_resume import (
    BookCompileSnapshot,
    outline_digest,
    confirm_outline,
    next_block,
    begin_block,
    complete_block,
    interrupt_block,
    resume_book,
    reconcile_not_started,
    readable_chapters,
    readable_pages,
    make_block_context,
)
from deeptutor_reuse.learning_actions import (
    apply_learning_action,
    independent_grade_eligible,
)
from deeptutor_reuse.learning_transactions import (
    PendingQuestion,
    MasteryInteraction,
    InteractionStatus,
    MasteryEvent,
    MasteryPathLease,
    PendingOption,
)
from deeptutor_reuse.offline_data import (
    FigurePayload,
    Inline,
    TextPayload,
    PlotData,
    validate_offline_visualization,
)
from deeptutor_reuse.visualization_models import (
    RendererRef,
    VisualizationEnvelope,
    VisualizationPayload,
    VisualizationPresentation,
    VisualizationInteraction,
)
from deeptutor_reuse.research_similarity import topic_similarity

report = {
    "layer": "installed-wheel synthetic candidate"
    if args.installed
    else "source synthetic candidate",
    "package": str(Path(deeptutor_reuse.__file__).resolve()),
    "checks": [],
    "model_calls": 0,
    "application_host_started": False,
    "user_material_read": False,
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def check(name, fn):
    try:
        report["checks"].append({"name": name, "status": "passed", "detail": fn()})
    except Exception as error:
        report["checks"].append(
            {
                "name": name,
                "status": "failed",
                "error": type(error).__name__ + ":" + str(error),
            }
        )
        import traceback

        traceback.print_exc()
    (args.output / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
    )


def expect(code, call):
    try:
        call()
    except ValueError as error:
        assert code in str(error), (code, str(error))
        return str(error)
    raise AssertionError("expected " + code)


def initial():
    book = Book(id="book-fixture", title="合成函数学习书", created_at=0, updated_at=0)
    chapters, pages = [], []
    for index in range(1, 4):
        chapter = Chapter(
            id=f"chapter-{index}",
            title=f"第{index}章",
            order=index,
            page_ids=[f"page-{index}"],
            prerequisites=[f"chapter-{index - 1}"] if index > 1 else [],
        )
        blocks = [
            Block(
                id=f"block-{index}-{j}",
                type=BlockType.TEXT,
                title=f"合成块{index}/{j}",
                params={"fixture_text": f"合成内容 {index}/{j}"},
                created_at=0,
                updated_at=0,
            )
            for j in range(1, 3)
        ]
        pages.append(
            Page(
                id=f"page-{index}",
                book_id=book.id,
                chapter_id=chapter.id,
                title=chapter.title,
                order=index,
                blocks=blocks,
                created_at=0,
                updated_at=0,
            )
        )
        chapters.append(chapter)
    return BookCompileSnapshot(
        book=book,
        spine=Spine(book_id=book.id, chapters=chapters, updated_at=0),
        pages=pages,
        inputs=BookInputs(
            source_refs=[
                {
                    "asset_id": "synthetic-source",
                    "source_version": "fixture-v1",
                    "role": "source_content",
                }
            ],
            user_intent="自造三章内容",
            captured_at=0,
        ),
    )


calls = []


class FixtureGenerator(BlockGenerator):
    block_type = BlockType.TEXT

    async def _generate(self, ctx):
        calls.append(ctx.block.id)
        return (
            {"body": ctx.block.params["fixture_text"]},
            [SourceAnchor(kind="manual", ref="synthetic-source:line-1")],
            {"content_role": "synthetic_example"},
        )


def run_inline(coroutine):
    # This synthetic async implementation has no await/IO: no event loop or
    # socket is needed to actually execute the retained generate wrapper.
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("fixture unexpectedly yielded work")


def generate(snapshot, item, generator=None):
    before = snapshot.model_dump_json()
    result = run_inline(
        (generator or FixtureGenerator()).generate(make_block_context(snapshot, item))
    )
    assert snapshot.model_dump_json() == before, (
        "generator mutated uncommitted checkpoint"
    )
    return result


def restart_proof():
    calls.clear()
    state = initial()
    digest = outline_digest(state)
    state = confirm_outline(state, digest)
    assert confirm_outline(state, digest).confirmation_count == 1
    for i in range(1, 4):
        item = next_block(state)
        started = begin_block(state, item, f"operation-{i}")
        assert started.dispatch
        state = started.snapshot
        assert not begin_block(state, item, f"operation-{i}").dispatch
        block = generate(state, item)
        state = complete_block(state, f"operation-{i}", block)
    assert readable_chapters(state) == ["chapter-1"]
    assert [p.id for p in readable_pages(state)] == ["page-1"]
    completed_first = readable_pages(state)[0].model_dump_json()
    (args.output / "early-readable-page.json").write_text(completed_first, "utf-8")
    item = next_block(state)
    state = begin_block(state, item, "operation-4").snapshot
    # Receipt was produced, but the process disappeared before repository commit.
    receipt = generate(state, item).model_copy(deep=True)
    (args.output / "uncommitted-receipt.json").write_text(
        receipt.model_dump_json(), "utf-8"
    )
    state = interrupt_block(state, "operation-4")
    (args.output / "paused-checkpoint.json").write_text(
        state.model_dump_json(), "utf-8"
    )
    state = BookCompileSnapshot.model_validate_json(
        (args.output / "paused-checkpoint.json").read_text("utf-8")
    )
    assert readable_chapters(state) == ["chapter-1"]
    expect("BOOK_RESULT_UNKNOWN", lambda: resume_book(state))
    assert not begin_block(state, item, "operation-4").dispatch
    state = complete_block(
        state,
        "operation-4",
        Block.model_validate_json(
            (args.output / "uncommitted-receipt.json").read_text("utf-8")
        ),
    )
    assert state.book.status == BookStatus.PAUSED
    state = resume_book(state)
    for i in range(5, 7):
        item = next_block(state)
        state = begin_block(state, item, f"operation-{i}").snapshot
        state = complete_block(state, f"operation-{i}", generate(state, item))
    assert next_block(state) is None and state.book.status == BookStatus.READY
    assert state.confirmation_count == 1
    assert len(calls) == len(set(calls)) == 6
    assert readable_pages(state)[0].model_dump_json() == completed_first
    assert readable_chapters(state) == ["chapter-1", "chapter-2", "chapter-3"]
    (args.output / "ready-checkpoint.json").write_text(state.model_dump_json(), "utf-8")
    markdown = render_book_markdown(state.book, state.spine, state.pages)
    (args.output / "retained-export.md").write_text(markdown, "utf-8")
    assert "合成内容 3/2" in markdown
    return {
        "confirmation_count": 1,
        "generator_calls": calls.copy(),
        "early_readable": ["chapter-1"],
        "final_readable": readable_chapters(state),
        "first_page_unchanged_sha256": sha(completed_first.encode()),
        "uncommitted_receipt_reconciled_without_dispatch": True,
    }


check("one_confirmation_continuous_chapters_and_real_json_restart", restart_proof)


def conflicts():
    state = initial()
    expect("BOOK_OUTLINE_CONFIRMATION_REQUIRED", lambda: next_block(state))
    state = confirm_outline(state, outline_digest(state))
    changed = state.model_copy(deep=True)
    changed.inputs.source_refs[0]["source_version"] = "fixture-v2"
    expect("BOOK_OUTLINE_VERSION_CONFLICT", lambda: resume_book(changed))
    reordered = state.model_copy(deep=True)
    reordered.pages.reverse()
    assert outline_digest(reordered) == outline_digest(state)
    item = next_block(state)
    state = begin_block(state, item, "idempotent").snapshot
    result = generate(state, item)
    state = complete_block(state, "idempotent", result)
    assert complete_block(state, "idempotent", result) == state
    conflict = result.model_copy(deep=True)
    conflict.payload["body"] = "changed"
    expect(
        "BOOK_OPERATION_RESULT_CONFLICT",
        lambda: complete_block(state, "idempotent", conflict),
    )
    expect(
        "BOOK_OPERATION_ID_CONFLICT",
        lambda: begin_block(state, next_block(state), "idempotent"),
    )
    return {
        "source_version_change_rejected": True,
        "same_receipt_idempotent": True,
        "conflicting_receipt_rejected": True,
        "page_storage_order_irrelevant": True,
    }


check("source_plan_and_operation_conflicts", conflicts)


def not_started():
    state = initial()
    state = confirm_outline(state, outline_digest(state))
    item = next_block(state)
    state = interrupt_block(begin_block(state, item, "not-sent").snapshot, "not-sent")
    expect(
        "BOOK_RECONCILIATION_REQUIRED",
        lambda: reconcile_not_started(state, "not-sent", ""),
    )
    state = reconcile_not_started(state, "not-sent", "synthetic-ledger:not-dispatched")
    expect("BOOK_EXPLICIT_RESUME_REQUIRED", lambda: next_block(state))
    state = resume_book(state)
    assert begin_block(state, item, "new-explicit-operation").dispatch
    expect(
        "BOOK_OPERATION_NOT_ACTIVE",
        lambda: complete_block(
            state, "not-sent", Block(id=item.block_id, type=BlockType.TEXT)
        ),
    )
    return {
        "explicit_reconciliation_ref": state.operations["not-sent"].reconciliation_ref,
        "old_receipt_rejected": True,
    }


check("unknown_not_started_requires_explicit_ledger_decision", not_started)


def failures_and_cancel():
    class Failed(FixtureGenerator):
        async def _generate(self, ctx):
            raise GenerationFailure("rate limit 429")

    class Cancelled(FixtureGenerator):
        async def _generate(self, ctx):
            raise asyncio.CancelledError()

    state = initial()
    state = confirm_outline(state, outline_digest(state))
    item = next_block(state)
    state = begin_block(state, item, "failed").snapshot
    failed = generate(state, item, Failed())
    assert (
        failed.status == BlockStatus.ERROR
        and failed.metadata["failure"]["kind"] == "rate_limit"
    )
    state = complete_block(state, "failed", failed)
    expect("BOOK_BLOCK_ERROR_REQUIRES_DECISION", lambda: resume_book(state))
    state = initial()
    state = confirm_outline(state, outline_digest(state))
    item = next_block(state)
    state = begin_block(state, item, "cancelled").snapshot
    try:
        generate(state, item, Cancelled())
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancel swallowed")
    state = interrupt_block(state, "cancelled")
    assert state.pages[0].blocks[0].status == BlockStatus.GENERATING
    expect("BOOK_RESULT_UNKNOWN", lambda: resume_book(state))
    return {
        "original_failure_metadata_preserved": True,
        "no_automatic_retry": True,
        "original_cancel_propagates": True,
        "cancelled_state_unknown": True,
    }


check(
    "original_generator_failure_and_cancel_are_not_silently_replayed",
    failures_and_cancel,
)


def free_actions():
    progress = Progress(book_id="book-fixture", updated_at=0)
    record_attempt(
        progress,
        page_id="page-1",
        block_id="quiz",
        question_id="q",
        user_answer="wrong",
        is_correct=False,
        page_to_chapter={"page-1": "chapter-1"},
        submission_id="answer-1",
    )
    before = progress.model_dump_json()
    interaction = MasteryInteraction(
        interaction_id="interaction-1",
        path_id="path-fixture",
        status=InteractionStatus.AWAITING_INPUT,
        question=PendingQuestion(
            question_id="q", knowledge_point_id="kp", prompt="1+1?", expected_answer="2"
        ),
        created_at=0,
        updated_at=0,
    )
    skipped = apply_learning_action(
        progress,
        interaction,
        action="skip",
        event_id=1,
        revision=1,
        now=1,
        target_page_id="page-3",
    )
    assert (
        skipped.progress.current_page_id == "page-3"
        and skipped.interaction.status == InteractionStatus.ABANDONED
    )
    assert not independent_grade_eligible(skipped.interaction, adjudicated_correct=True)
    revealed = apply_learning_action(
        progress, interaction, action="view_explanation", event_id=2, revision=2, now=2
    )
    assert revealed.interaction.question.assistance_used
    revealed.interaction.status = InteractionStatus.GRADED
    assert not independent_grade_eligible(
        revealed.interaction, adjudicated_correct=True
    )
    for changed in (skipped, revealed):
        assert changed.progress.quiz_attempts == progress.quiz_attempts
        assert changed.progress.score == 0 and changed.progress.weak_chapters == [
            "chapter-1"
        ]
        assert changed.event.payload["grants_mastery"] is False
    assert progress.model_dump_json() == before
    correct = interaction.model_copy(deep=True)
    correct.status = InteractionStatus.GRADED
    assert independent_grade_eligible(correct, adjudicated_correct=True)
    assert not independent_grade_eligible(correct, adjudicated_correct=None)
    after = apply_learning_action(
        progress, correct, action="view_explanation", event_id=3, revision=3, now=3
    )
    assert independent_grade_eligible(after.interaction, adjudicated_correct=True)
    (args.output / "learning-actions.json").write_text(
        json.dumps(
            {
                "skip": skipped.model_dump(mode="json"),
                "reveal": revealed.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )
    return {
        "free_jump_to": "page-3",
        "wrong_attempt_preserved": True,
        "revealed_answer_not_independent": True,
        "past_independent_grade_not_invalidated_by_later_reveal": True,
        "read_ratio_is_not_mastery": completion_ratio(skipped.progress, 3),
    }


check("skip_and_explanation_are_events_not_mastery_or_new_quiz_grades", free_actions)


def visualization_boundary():
    raw = {
        "render_type": "quadratic_tangent",
        "renderer": {
            "id": "offline.function-plot",
            "version": "1.0.0",
            "target": "native",
            "native_renderer": "quadratic_tangent",
        },
        "payload": {
            "format": "learning.parameter-plot/v1",
            "data": {
                "kind": "quadratic_tangent",
                "a": {
                    "label": "a",
                    "min": 0.5,
                    "max": 3.0,
                    "step": 0.5,
                    "initial": 1.0,
                },
                "x0": {
                    "label": "x0",
                    "min": -2.0,
                    "max": 2.0,
                    "step": 0.25,
                    "initial": 1.0,
                },
            },
        },
    }
    result = validate_offline_visualization(raw)
    assert isinstance(result, VisualizationEnvelope)
    denied = []

    def reject(name, change):
        case = json.loads(json.dumps(raw))
        change(case)
        try:
            validate_offline_visualization(case)
        except (ValueError, ValidationError):
            denied.append(name)
        else:
            raise AssertionError("accepted " + name)

    reject("iframe", lambda v: v["renderer"].update(target="iframe"))
    reject(
        "entry_url_https",
        lambda v: v["renderer"].update(entry_url="https://invalid.example/renderer"),
    )
    reject(
        "entry_url_local",
        lambda v: v["renderer"].update(entry_url="file:///C:/outside.html"),
    )
    reject(
        "business_capability", lambda v: v.update(business_interface={"write": True})
    )
    reject(
        "automatic_model_event",
        lambda v: v.update(interaction={"events": ["model.prompt"]}),
    )
    reject(
        "html_fallback",
        lambda v: v.update(fallback={"html": "<script>fetch('/api')</script>"}),
    )
    reject(
        "code_expression",
        lambda v: v["payload"]["data"].update(expression="fetch('/api')"),
    )
    reject(
        "prototype_payload",
        lambda v: v["payload"]["data"].update(__proto__={"polluted": True}),
    )
    reject(
        "remote_schema",
        lambda v: v["payload"]["data"].update(
            **{"$ref": "https://invalid.example/schema"}
        ),
    )
    reject(
        "infinite_parameter",
        lambda v: v["payload"]["data"]["a"].update(max=float("inf")),
    )
    reject("extreme_parameter", lambda v: v["payload"]["data"]["a"].update(max=1e100))
    reject("expression_string", lambda v: v["payload"].update(data="eval(1)"))
    for path in (
        "https://invalid.example/a.svg",
        "assets/../a.svg",
        "assets/%2e%2e/a.svg",
        "assets/a.svg?file=outside",
        "assets/a.svg#fragment",
    ):
        try:
            FigurePayload(
                description="",
                asset_path=path,
                asset_sha256="a" * 64,
                alt="",
                caption="",
            )
        except ValidationError:
            denied.append("asset:" + path)
        else:
            raise AssertionError("unsafe path accepted")
    expect(
        "MATH_EXTERNAL_OR_HTML_COMMAND",
        lambda: TextPayload(
            body="", paragraphs=[], display_math=[r"\href{https://invalid.example}{x}"]
        ),
    )
    (args.output / "native-envelope.json").write_text(result.model_dump_json(), "utf-8")
    return {
        "actual_original_envelope": result.__class__.__module__
        + "."
        + result.__class__.__name__,
        "negative_cases": denied,
        "backend_or_model_calls": 0,
    }


check(
    "closed_data_visualization_rejects_network_code_and_business_authority",
    visualization_boundary,
)


def frozen_offline():
    offline = ROOT / "checks/offline-candidate"
    final = json.loads(
        (offline / "evidence/final-verification.json").read_text("utf-8")
    )
    artifact = offline / "artifacts/learning-book-candidate.zip"
    assert sha(artifact.read_bytes()) == final["zip_sha256"]
    identity = json.loads(
        (offline / "evidence/package-identity.json").read_text("utf-8")
    )
    manifest_bytes = (offline / "package/asset-manifest.json").read_bytes()
    unpacked = offline / "verification/unpacked-r1"
    assert sha(manifest_bytes) == identity["asset_manifest_sha256"]
    assert (unpacked / "asset-manifest.json").read_bytes() == manifest_bytes
    manifest = json.loads(manifest_bytes)
    records = manifest["files"]
    for item in records:
        candidate = offline / "verification/unpacked-r1" / item["path"]
        assert sha(candidate.read_bytes()) == item["sha256"]
    expected = {row["path"] for row in records} | {"asset-manifest.json"}
    actual = {
        p.relative_to(unpacked).as_posix() for p in unpacked.rglob("*") if p.is_file()
    }
    assert actual == expected
    html = (offline / "verification/unpacked-r1/index.html").read_text("utf-8")
    assert (
        "connect-src 'none'" in html
        and "frame-src 'none'" in html
        and "form-action 'none'" in html
    )
    js = (offline / "verification/unpacked-r1/runtime/reader.js").read_text("utf-8")
    for forbidden in (
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "localStorage",
        "indexedDB",
        "postMessage(",
        "eval(",
        "new Function(",
    ):
        assert forbidden not in js, forbidden
    assert (
        final["http_or_websocket_requests_observed"]
        == final["storage_writes_observed"]
        == final["model_requests"]
        == 0
    )
    assert final["application_backend_started"] is False
    return {
        "status": "frozen_previous_real_browser_evidence_reverified",
        "zip_sha256": final["zip_sha256"],
        "resource_files_rehashed": len(records) + 1,
        "browser": final["browser"],
        "http_or_ws": 0,
        "business_storage_writes": 0,
        "automatic_model_calls": 0,
        "browser_relaunched_in_this_check": False,
        "previous_browser_evidence": "checks/offline-candidate/evidence/browser-verification.json",
    }


check(
    "file_offline_package_frozen_bytes_and_prior_real_browser_evidence", frozen_offline
)


def api_fields():
    classes = (
        Book,
        BookInputs,
        Spine,
        Chapter,
        Page,
        Block,
        Progress,
        QuizAttempt,
        SourceAnchor,
        PendingOption,
        PendingQuestion,
        MasteryInteraction,
        MasteryEvent,
        MasteryPathLease,
        RendererRef,
        VisualizationPayload,
        VisualizationPresentation,
        VisualizationInteraction,
        VisualizationEnvelope,
        BookCompileSnapshot,
        Inline,
        TextPayload,
        FigurePayload,
        PlotData,
    )
    result = {
        cls.__module__ + "." + cls.__name__: {
            key: str(field.annotation) for key, field in cls.model_fields.items()
        }
        for cls in classes
    }
    (args.output / "actual-api-fields.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), "utf-8"
    )
    modules = (
        "book_models",
        "book_compiler_state",
        "learning_transactions",
        "visualization_models",
        "source_digest",
        "book_block_compiler",
        "book_progress",
        "book_export",
        "book_resume",
        "learning_actions",
        "offline_data",
    )
    package_dir = Path(deeptutor_reuse.__file__).parent
    hashes = {
        name: sha((package_dir / (name + ".py")).read_bytes()) for name in modules
    }
    (args.output / "active-module-hashes.json").write_text(
        json.dumps(hashes, indent=2), "utf-8"
    )
    assert topic_similarity("Fourier basis", "fourier basis") == 1.0
    assert topic_similarity("", "unrelated") == 0.0
    return {
        "classes": list(result),
        "package_path": str(package_dir),
        "research_similarity_is_not_a_queue": True,
        "module_hashes": hashes,
    }


check("actual_import_fields_and_C13_similarity_boundary", api_fields)

report["passed"] = sum(c["status"] == "passed" for c in report["checks"])
report["failed"] = sum(c["status"] == "failed" for c in report["checks"])
report["status"] = "passed" if report["failed"] == 0 else "failed"
(args.output / "summary.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
)
print(
    json.dumps(
        {
            "status": report["status"],
            "passed": report["passed"],
            "failed": [c for c in report["checks"] if c["status"] == "failed"],
            "output": str(args.output),
        },
        ensure_ascii=False,
    )
)
raise SystemExit(0 if report["status"] == "passed" else 1)
