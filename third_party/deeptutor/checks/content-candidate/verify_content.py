"""Self-made text/eval/research snapshots; no live retrieval/provider/Host."""

# ruff: noqa: E402 -- Explicit source-vs-built-wheel import selection.
from pathlib import Path
import argparse
from dataclasses import asdict, replace
import hashlib
import importlib.util
import json
import math
import sys
import types

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument("--installed", action="store_true")
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=False)
if not args.installed:
    sys.path.insert(0, str(ROOT / "src"))

import deeptutor_reuse
from deeptutor_reuse.source_windows import (
    TextUnit,
    split_text_unit,
    reassemble_text_unit,
    text_digest,
)
from deeptutor_reuse.eval_dataset import EvalDataset, EvalDatasetError, load_dataset
from deeptutor_reuse.eval_matching import RelevanceMatcher, MatchPolicy, source_text
from deeptutor_reuse.eval_metrics import evaluate_hits, aggregate_query_metrics
from deeptutor_reuse.research_records import (
    TopicBlock,
    TopicStatus,
    TraceReference,
    ResearchTopicView,
)
from deeptutor_reuse.research_recovery import review_research_recovery

report = {
    "layer": "installed pure wheel" if args.installed else "pure source candidate",
    "package": str(Path(deeptutor_reuse.__file__).resolve()),
    "checks": [],
    "actual_retriever_calls": 0,
    "model_calls": 0,
    "host_started": False,
    "user_data_read": False,
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


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


def expect(code, fn):
    try:
        fn()
    except ValueError as error:
        assert code in str(error), (code, str(error))
        return str(error)
    raise AssertionError("missing rejection " + code)


def source_windows():
    fixtures = [
        ("long-one-line", "  " + ("字幕😀e\u0301👩‍💻\t" * 30001) + "  "),
        (
            "line-boundaries",
            "\n\n"
            + "\r\n".join(f"第{i}行：数学 $x_{i}^2$ 😀" for i in range(10001))
            + "\r\n\n",
        ),
        ("blank", ""),
        ("whitespace", " \t\r\n  \n"),
    ]
    results = []
    for name, text in fixtures:
        unit = TextUnit(
            name,
            "synthetic-source",
            "media-v1",
            "effective-subtitle-r3",
            "source_content",
            text,
            segment_id="segment-7",
            start_ms=1234,
            end_ms=91234,
        )
        windows = split_text_unit(unit)
        assert reassemble_text_unit(unit, windows) == text
        assert all(len(w.text) <= 24000 for w in windows)
        assert all(
            (w.segment_id, w.start_ms, w.end_ms, w.content_revision)
            == ("segment-7", 1234, 91234, "effective-subtitle-r3")
            for w in windows
        )
        for w in windows:
            w.text.encode("utf-8", errors="strict")
        results.append(
            {
                "name": name,
                "codepoints": len(text),
                "utf8_bytes": len(text.encode()),
                "source_text_sha256": text_digest(text),
                "windows": len(windows),
                "reassembled_sha256": text_digest(reassemble_text_unit(unit, windows)),
                "max_window_codepoints": max([len(w.text) for w in windows], default=0),
            }
        )
        if name == "long-one-line":
            (args.output / "long-subtitle-window-map.json").write_text(
                json.dumps([asdict(w) for w in windows], ensure_ascii=False, indent=2),
                "utf-8",
            )
    edge = TextUnit(
        "edge", "synthetic", "v1", "r1", "source_content", "a\r\n😀e\u0301👩‍💻\n\nend"
    )
    for budget in range(1, 32):
        windows = split_text_unit(edge, budget)
        assert reassemble_text_unit(edge, windows) == edge.text
        assert all(len(w.text) <= budget for w in windows)
    return {
        "fixtures": results,
        "small_budgets": 31,
        "timestamps_not_interpolated": True,
        "unicode_normalization": "none; codepoint offsets, not grapheme or token offsets",
    }


check("lossless_long_subtitle_and_unicode_line_boundaries", source_windows)


def coverage_negatives():
    unit = TextUnit(
        "leaf", "asset", "v1", "r1", "source_content", "abc😀def\r\nghi", page_index=0
    )
    windows = split_text_unit(unit, 3, prefer_lines=False)
    expect(
        "SOURCE_WINDOW_COVERAGE_INVALID",
        lambda: reassemble_text_unit(unit, [windows[0], windows[0], *windows[1:]]),
    )
    expect(
        "SOURCE_WINDOW_COVERAGE_INVALID",
        lambda: reassemble_text_unit(unit, windows[1:]),
    )
    expect(
        "SOURCE_WINDOW_COVERAGE_INVALID",
        lambda: reassemble_text_unit(unit, list(reversed(windows))),
    )
    expect(
        "SOURCE_WINDOW_VERSION_CONFLICT",
        lambda: reassemble_text_unit(replace(unit, source_version="v2"), windows),
    )
    expect(
        "SOURCE_WINDOW_VERSION_CONFLICT",
        lambda: reassemble_text_unit(replace(unit, content_revision="r2"), windows),
    )
    expect(
        "SOURCE_WINDOW_VERSION_CONFLICT",
        lambda: reassemble_text_unit(
            replace(unit, content_role="ai_generated"), windows
        ),
    )
    expect(
        "SOURCE_WINDOW_HASH_CONFLICT",
        lambda: reassemble_text_unit(
            unit, [replace(windows[0], text="XYZ"), *windows[1:]]
        ),
    )
    for budget in (0, -1, True):
        expect(
            "SOURCE_WINDOW_BUDGET_INVALID",
            lambda budget=budget: split_text_unit(unit, budget),
        )
    expect(
        "SOURCE_TEXT_INVALID_UTF8",
        lambda: TextUnit("x", "a", "v1", "r1", "source_content", "\ud800"),
    )
    return {
        "duplicates_gaps_reordering_revision_role_hash_rejected": True,
        "invalid_budgets_rejected": 3,
        "lone_surrogate_rejected": True,
    }


check("window_tamper_and_scope_negative_cases", coverage_negatives)


def evaluation():
    dataset = EvalDataset.from_cases(
        [
            {
                "id": "ranked",
                "query": "synthetic ranking",
                "gold": ["alpha beta gamma delta", "theta lambda omega sigma"],
            },
            {
                "id": "short",
                "query": "short retrieval",
                "gold": "alpha beta gamma delta",
            },
            {
                "id": "failed",
                "query": "failed retrieval",
                "gold": "epsilon zeta eta iota",
            },
        ],
        name="synthetic-eval",
    )
    payload = {"name": dataset.name, "cases": [c.model_dump() for c in dataset.cases]}
    path = args.output / "qa-set.json"
    path.write_text(json.dumps(payload), "utf-8")
    loaded = load_dataset(path)
    assert [c.id for c in loaded.cases] == ["ranked", "short", "failed"]
    ranked = [
        {"content": "irrelevant words"},
        {"text": "alpha beta gamma delta"},
        {"snippet": "alpha beta gamma delta"},
        {"excerpt": "theta lambda omega sigma"},
    ]
    matcher = RelevanceMatcher(loaded.cases[0].gold)
    hits = [set(matcher.match(source_text(row)).indices) for row in ranked]
    assert hits == [set(), {0}, {0}, {1}]
    metric = evaluate_hits("ranked", hits, matcher.gold_count, 4)
    assert (
        metric.recall == 1
        and metric.precision == 0.75
        and metric.mrr == 0.5
        and metric.average_precision == 0.5
    )
    assert math.isclose(
        metric.ndcg,
        ((1 / math.log2(3)) + (1 / math.log2(5))) / (1 + (1 / math.log2(3))),
    )
    short = evaluate_hits("short", [{0}], 1, 4)
    assert short.precision == 1
    macro = aggregate_query_metrics([metric, short], 4)
    rows = [
        {"status": "passed", "metrics": metric.to_dict()},
        {"status": "passed", "metrics": short.to_dict()},
        {
            "id": "failed",
            "status": "failed",
            "error_code": "SYNTHETIC_RETRIEVER_UNAVAILABLE",
            "metrics": None,
        },
    ]
    assert macro["queries"] == 2
    empty = {"status": "not_run", "metrics": aggregate_query_metrics([], 4)}
    assert empty["metrics"]["queries"] == 0
    # Observable false positive: matching is lexical, not mathematical entailment.
    math_gold = "Assume x is positive and x > 0 then the result is positive."
    opposite = "Assume x is negative and x < 0 then the result is negative."
    lexical_false_positive = bool(RelevanceMatcher([math_gold]).match(opposite))
    assert lexical_false_positive
    expect("min_ratio", lambda: MatchPolicy(min_ratio=0))
    expect("k must be", lambda: evaluate_hits("bad", [], 1, 0))
    try:
        EvalDataset.from_cases([{"query": "blank gold", "gold": []}])
    except ValueError:
        pass
    else:
        raise AssertionError("empty gold accepted")
    duplicate = args.output / "duplicate.json"
    duplicate.write_text(
        json.dumps(
            [
                {"id": "dup", "query": "q1", "gold": "a"},
                {"id": "dup", "query": "q2", "gold": "b"},
            ]
        ),
        "utf-8",
    )
    try:
        load_dataset(duplicate)
    except EvalDatasetError:
        pass
    else:
        raise AssertionError("duplicate query ids accepted")
    result = {
        "macro_success_only": macro,
        "rows": rows,
        "empty_run": empty,
        "known_math_lexical_false_positive": lexical_false_positive,
        "warning": "No source/version authorization, mathematical equivalence, coverage or production retrieval has been validated by lexical matching.",
    }
    (args.output / "retrieval-eval.json").write_text(
        json.dumps(result, indent=2), "utf-8"
    )
    return result


check(
    "original_C08_dataset_matching_and_metrics_with_failed_query_separation", evaluation
)


def original_research_probe():
    # Trusted fixed-code import, test-only namespace: do not install the upstream app.
    package = types.ModuleType("deeptutor")
    package.__path__ = []
    utils = types.ModuleType("deeptutor.utils")
    utils.__path__ = []
    sys.modules["deeptutor"] = package
    sys.modules["deeptutor.utils"] = utils

    def module(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        loaded = importlib.util.module_from_spec(spec)
        sys.modules[name] = loaded
        spec.loader.exec_module(loaded)
        return loaded

    module(
        "deeptutor.utils.json_parser", ROOT / "upstream/deeptutor/utils/json_parser.py"
    )
    original = module(
        "dt_original_research_probe",
        ROOT / "upstream/deeptutor/agents/research/data_structures.py",
    )
    queue = original.DynamicTopicQueue("synthetic-original", max_length=2)
    first = queue.add_block("Synthetic A", "")
    second = queue.add_block("Synthetic B", "")
    queue.mark_researching(first.block_id)
    queue.mark_researching(second.block_id)
    original_path = args.output / "original-queue.json"
    queue.save_to_json(str(original_path))
    restored = original.DynamicTopicQueue.load_from_json(str(original_path))
    assert (
        restored.max_length is None
        and restored.get_pending_block() is None
        and not restored.is_all_completed()
    )
    body = "汉" * 60000
    trace = original.ToolTrace(
        "tool-1", "citation-1", "rag", "synthetic", body, "summary"
    )
    assert trace.raw_answer_truncated and trace.raw_answer != body
    result = {
        "original_saved_cap": 2,
        "restored_cap": restored.max_length,
        "researching_after_load": restored.get_statistics()["researching"],
        "pending_after_load": restored.get_statistics()["pending"],
        "automatic_recovery": False,
        "raw_answer_original_size_field": trace.raw_answer_original_size,
        "actual_original_utf8_bytes": len(body.encode()),
        "truncated_string_codepoints": len(trace.raw_answer),
        "truncated_original_is_not_complete_evidence": True,
    }
    (args.output / "original-recovery-limitations.json").write_text(
        json.dumps(result, indent=2), "utf-8"
    )
    return result


check("actual_original_queue_load_and_truncation_limitations", original_research_probe)


def read_only_recovery():
    full = "汉" * 60000
    full_hash = text_digest(full)
    (args.output / "synthetic-full-evidence.txt").write_text(full, "utf-8")
    trace = TraceReference(
        "tool",
        "CIT-1",
        "rag",
        "synthetic query",
        "synthetic summary",
        "evidence",
        "ev1",
        full_hash,
        "operation-1",
    )
    refs = [
        {"asset_id": "source", "source_version": "v1", "content_role": "source_content"}
    ]
    topics = [
        TopicBlock(
            f"block_{i}",
            f"Synthetic {i}",
            "",
            status=status,
            tool_traces=[trace] if status == TopicStatus.COMPLETED else [],
            created_at="2026-10-23T00:00:00+00:00",
            updated_at="2026-10-23T00:00:00+00:00",
            metadata={"source_refs": refs},
        )
        for i, status in enumerate(
            (
                TopicStatus.COMPLETED,
                TopicStatus.RESEARCHING,
                TopicStatus.FAILED,
                TopicStatus.PENDING,
            ),
            1,
        )
    ]
    topics.append(
        TopicBlock(
            "block_5",
            "missing source",
            "",
            status=TopicStatus.COMPLETED,
            tool_traces=[trace],
            created_at="2026-10-23T00:00:00Z",
            updated_at="2026-10-23T00:00:00Z",
            metadata={"source_refs": [{**refs[0], "source_version": "v2"}]},
        )
    )
    topics.append(
        TopicBlock(
            "block_6",
            "missing evidence",
            "",
            status=TopicStatus.COMPLETED,
            created_at="2026-10-23T00:00:00Z",
            updated_at="2026-10-23T00:00:00Z",
            metadata={"source_refs": refs},
        )
    )
    topics.append(
        TopicBlock(
            "block_7",
            "naive legacy clock",
            "",
            created_at="2026-10-23T00:00:00",
            updated_at="2026-10-23T00:00:00",
            metadata={"source_refs": refs},
        )
    )
    path = args.output / "topic-records.json"
    path.write_text(json.dumps([t.to_dict() for t in topics]), "utf-8")
    loaded = tuple(
        TopicBlock.from_dict(row) for row in json.loads(path.read_text("utf-8"))
    )
    before = json.dumps([b.to_dict() for b in loaded], sort_keys=True)
    view = ResearchTopicView(loaded)
    assert loaded[0].get_latest_trace().evidence_sha256 == full_hash
    assert loaded[0].get_all_summaries() == "[rag] synthetic summary"
    assert view.find_similar(" SYNTHETIC 1 ").block_id == "block_1"
    assert view.get_pending_block().block_id == "block_4"
    assert not hasattr(view, "add_block") and not hasattr(view, "save_to_json")
    review = review_research_recovery(
        loaded,
        available_sources=frozenset({("source", "v1", "source_content")}),
        available_evidence={("evidence", "ev1"): full_hash},
    )
    assert review.completed == ("block_1",) and review.reconciliation_required == (
        "block_2",
    )
    assert review.failed_decision_required == ("block_3",) and review.pending == (
        "block_4",
    )
    blocked = dict(review.blocked)
    assert "RESEARCH_SOURCE_VERSION_UNAVAILABLE" in blocked["block_5"]
    assert "RESEARCH_EVIDENCE_MISSING" in blocked["block_6"]
    assert "RESEARCH_TIMESTAMP_REQUIRES_MIGRATION" in blocked["block_7"]
    assert before == json.dumps([b.to_dict() for b in loaded], sort_keys=True)
    assert (
        review_research_recovery(
            loaded,
            available_sources=frozenset({("source", "v1", "source_content")}),
            available_evidence={},
        ).completed
        == ()
    )
    expect(
        "RESEARCH_TOPIC_ID_CONFLICT",
        lambda: review_research_recovery(
            [loaded[0], loaded[0]], available_sources=frozenset(), available_evidence={}
        ),
    )
    expect(
        "RESEARCH_LEGACY_RAW_ANSWER",
        lambda: TraceReference.from_dict({"raw_answer": "legacy"}),
    )
    result = {
        "review": asdict(review),
        "declared_original_statistics": view.get_statistics(),
        "record_states_unchanged": True,
        "full_evidence_reference_sha256": full_hash,
        "full_evidence_bytes": len(full.encode()),
        "has_authoritative_queue_or_dispatch": False,
    }
    (args.output / "recovery-review.json").write_text(
        json.dumps(result, indent=2), "utf-8"
    )
    return result


check(
    "read_only_original_topic_queries_and_application_owned_recovery_decisions",
    read_only_recovery,
)


def identity():
    extraction = json.loads(
        (ROOT / "checks/content-candidate/extraction-manifest.json").read_text("utf-8")
    )
    for source in extraction["C06_sources"]:
        raw = (ROOT / "checks/content-candidate" / source["path"]).read_bytes()
        assert sha(raw) == source["sha256"]
        assert (
            hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            == source["git_blob_sha1"]
        )
    actual = Path(deeptutor_reuse.__file__).parent
    assert (actual / "eval_matching.py").read_bytes() == (
        ROOT / "upstream/deeptutor/services/rag/eval/matching.py"
    ).read_bytes()
    assert (
        sha((actual / "source_digest.py").read_bytes())
        == "2b37b793c25a7ebf0174cac2a21048aafb29e2fbc2c789463112fe78876c8d95"
    )
    return {
        "C06_original_files": len(extraction["C06_sources"]),
        "C08_matching_original_bytes": True,
        "source_digest_unchanged": True,
        "active_modules": {
            name: sha((actual / (name + ".py")).read_bytes())
            for name in (
                "source_windows",
                "eval_matching",
                "eval_metrics",
                "eval_dataset",
                "research_records",
                "research_recovery",
            )
        },
    }


check("fixed_source_and_active_package_identity", identity)

report["passed"] = sum(c["status"] == "passed" for c in report["checks"])
report["failed"] = sum(c["status"] == "failed" for c in report["checks"])
report["status"] = "passed" if not report["failed"] else "failed"
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
raise SystemExit(0 if not report["failed"] else 1)
