"""Deterministic migration fixtures; these do not certify Learning scenarios."""

# ruff: noqa: E402 -- Explicit bootstrap compares source imports and installed-wheel imports.

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if "--installed" in sys.argv:
    sys.argv.remove("--installed")
else:
    sys.path.insert(0, str(ROOT / "src"))

from pydantic import ValidationError

from deeptutor_reuse.book_compiler_state import finalize_terminal_page
from deeptutor_reuse.book_models import (
    Block,
    BlockStatus,
    BlockType,
    Book,
    Chapter,
    ConceptGraph,
    ConceptNode,
    Page,
    PageStatus,
    Spine,
)
from deeptutor_reuse.eval_dataset import EvalDataset, EvalDatasetError, load_dataset
from deeptutor_reuse.eval_metrics import (
    aggregate_query_metrics,
    evaluate_hits,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from deeptutor_reuse.file_io import atomic_write_json
from deeptutor_reuse.learning_transactions import (
    InteractionStatus,
    MasteryEvent,
    MasteryInteraction,
    PendingQuestion,
)
from deeptutor_reuse import parser_cache
import deeptutor_reuse
from deeptutor_reuse.research_similarity import normalize_topic, topic_similarity, topic_tokens
from deeptutor_reuse.source_digest import source_digest
from deeptutor_reuse.source_ref import SourceRef
from deeptutor_reuse.visualization_models import (
    RendererRef,
    VisualizationEnvelope,
    VisualizationPayload,
)
from deeptutor_reuse.visualization_refs import find_remote_schema_ref


class IdentityTests(unittest.TestCase):
    def test_all_archived_bytes_match_manifest(self):
        manifest = json.loads((ROOT / "reuse-manifest.json").read_text("utf-8"))
        for item in manifest["upstream_files"]:
            raw = (ROOT / "upstream" / item["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), item["sha256"], item["path"])
            blob = b"blob " + str(len(raw)).encode() + b"\0" + raw
            self.assertEqual(hashlib.sha1(blob).hexdigest(), item["git_blob_sha1"])
        for item in manifest["migrations"]:
            if item["mode"] == "R":
                self.assertEqual(
                    (ROOT / item["target"]).read_bytes(),
                    (ROOT / "upstream" / item["upstream"]).read_bytes(),
                    item["target"],
                )

    def test_active_imports_match_registered_migration_hashes(self):
        active_package = Path(deeptutor_reuse.__file__).parent
        manifest = json.loads((ROOT / "reuse-manifest.json").read_text("utf-8"))
        for item in manifest["migrations"]:
            if item["target"].startswith("src/deeptutor_reuse/"):
                target = active_package / Path(item["target"]).name
                self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), item["target_sha256"])


class SourceTests(unittest.TestCase):
    def test_multiblock_bytes_and_same_content_identity(self):
        raw = ("教材引用\n".encode("utf-8") * 110_000) + b"tail"
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "one.pdf"
            second = Path(directory) / "renamed.pdf"
            first.write_bytes(raw)
            second.write_bytes(raw)
            self.assertEqual(source_digest(first), hashlib.sha256(raw).hexdigest())
            self.assertEqual(source_digest(first), source_digest(second))
            second.write_bytes(raw + b"revision")
            self.assertNotEqual(source_digest(first), source_digest(second))

    def test_source_dto_rejects_malformed_identity(self):
        base = {"kb_ref": "fixture", "document_key": "one.pdf", "source_digest": "a" * 64, "page_index": 0}
        self.assertEqual(SourceRef.model_validate(base).page_index, 0)
        for change in [{"source_digest": "bad"}, {"page_index": -1}, {"unapproved": True}]:
            with self.assertRaises(ValidationError):
                SourceRef.model_validate({**base, **change})


class RetrievalMetricTests(unittest.TestCase):
    def test_repeated_gold_has_no_extra_recall_or_dcg(self):
        hits = [set(), {0}, {0}, {1}]
        result = evaluate_hits("fixture", hits, gold_count=2, k=4)
        self.assertEqual(result.recall, 1)
        self.assertEqual(result.precision, 0.75)
        self.assertEqual(result.mrr, 0.5)
        self.assertEqual(result.average_precision, 0.5)
        expected = (1 / math.log2(3) + 1 / math.log2(5)) / (1 + 1 / math.log2(3))
        self.assertAlmostEqual(result.ndcg, expected)
        self.assertEqual(recall_at_k([{0}, {0}], 2, 2), 0.5)
        self.assertLess(ndcg_at_k([{0}, {0}], 2, 2), 1)

    def test_short_precision_and_failed_case_separation_contract(self):
        self.assertEqual(precision_at_k([{0}], 5), 1)
        self.assertEqual(aggregate_query_metrics([], 5)["queries"], 0)
        with self.assertRaises(ValueError):
            precision_at_k([], 0)

    def test_dataset_loading_labels_duplicates_and_invalid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.jsonl"
            path.write_text('{"query":" What? ","gold":" Source. "}\n', encoding="utf-8")
            dataset = load_dataset(path)
            self.assertEqual(dataset.cases[0].id, "q1")
            self.assertEqual(dataset.cases[0].gold, ["Source."])
            dataset = EvalDataset.from_cases([{"query": "why", "gold": ["because"]}])
            self.assertEqual(len(dataset), 1)
            path.write_text('{"id":"same","query":"a","gold":"a"}\n{"id":"same","query":"b","gold":"b"}\n', encoding="utf-8")
            with self.assertRaises(EvalDatasetError):
                load_dataset(path)
            path.write_text('{"query":', encoding="utf-8")
            with self.assertRaises(EvalDatasetError):
                load_dataset(path)


class TransactionTests(unittest.TestCase):
    def question(self, identifier="q1"):
        return PendingQuestion(
            question_id=identifier,
            knowledge_point_id="point",
            question_type="choice",
            prompt=f"Choose for {identifier}",
            expected_answer="A",
            options=[{"label": "A", "body": "x - 1 = 0"}],
        )

    def test_typed_option_and_transaction_round_trip(self):
        question = self.question()
        self.assertEqual(question.choice_map, {"A": "x - 1 = 0"})
        interaction = MasteryInteraction(interaction_id="i1", path_id="p1", question=question)
        round_trip = MasteryInteraction.model_validate_json(interaction.model_dump_json())
        self.assertEqual(round_trip.status, InteractionStatus.REGISTERED)
        self.assertEqual(round_trip.question.expected_answer, "A")
        event = MasteryEvent(path_id="p1", revision=1, event_type="registered")
        self.assertEqual(event.revision, 1)

    def test_legacy_strings_and_duplicate_group_rejected(self):
        with self.assertRaises(ValidationError):
            PendingQuestion(question_id="q1", knowledge_point_id="point", options=["A: body"])
        with self.assertRaises(ValidationError):
            PendingQuestion(question_id="g", knowledge_point_id="point", question_type="group", questions=[self.question(), self.question()])
        group = PendingQuestion(question_id="g", knowledge_point_id="point", question_type="group", questions=[self.question(), self.question("q2")])
        self.assertEqual(len(group.questions), 2)


class BookTests(unittest.TestCase):
    def test_book_tree_lookup_and_roundtrip(self):
        book = Book(title="Fixture")
        chapter = Chapter(title="Intro")
        spine = Spine(book_id=book.id, chapters=[chapter])
        self.assertEqual(spine.chapter_by_id(chapter.id), chapter)
        self.assertIsNone(spine.chapter_by_id("missing"))
        graph = ConceptGraph(nodes=[ConceptNode(id="c1")])
        self.assertEqual(graph.node_by_id("c1").id, "c1")
        self.assertEqual(Book.model_validate_json(book.model_dump_json()).id, book.id)

    def test_terminal_aggregation_and_generation_guard(self):
        first = Block(type=BlockType.TEXT, status=BlockStatus.READY)
        second = Block(type=BlockType.QUIZ, status=BlockStatus.ERROR)
        page = Page(blocks=[first, second])
        finalize_terminal_page(page)
        self.assertEqual(page.status, PageStatus.PARTIAL)
        second.status = BlockStatus.READY
        finalize_terminal_page(page)
        self.assertEqual(page.status, PageStatus.READY)
        second.status = BlockStatus.GENERATING
        with self.assertRaisesRegex(ValueError, "PAGE_NOT_TERMINAL"):
            finalize_terminal_page(page)
        empty = Page()
        finalize_terminal_page(empty)
        self.assertEqual(empty.status, PageStatus.ERROR)


class VisualizationTests(unittest.TestCase):
    def test_envelope_roundtrip_and_nested_reference_detection(self):
        envelope = VisualizationEnvelope(render_type="svg", renderer=RendererRef(id="fixture", version="1", target="native"), payload=VisualizationPayload(format="svg", data="<svg/>"))
        self.assertEqual(VisualizationEnvelope.model_validate_json(envelope.model_dump_json()).render_type, "svg")
        self.assertEqual(find_remote_schema_ref({"$defs": [{"$dynamicRef": "https://invalid.example/schema"}]}), "https://invalid.example/schema")
        self.assertEqual(find_remote_schema_ref({"$ref": "#/$defs/local"}), "")


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "optional visualization extra not installed")
class OptionalSchemaTests(unittest.TestCase):
    def test_bounded_schema_and_strict_syntax(self):
        from deeptutor_reuse.visualization_protocol import VisualizerManifest, VisualizerPlugin

        base = {"id": "fixture", "display_name": "Fixture", "description": "Test", "payload_format": "json", "payload_kind": "json", "native_renderer": "fixed", "prompt": "Fixture"}
        manifest = VisualizerManifest(**base, payload_schema={"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]})
        plugin = VisualizerPlugin(manifest=manifest, origin="core")
        self.assertTrue(plugin.validate_payload('{"value": 2}')[0])
        self.assertFalse(plugin.validate_payload('{"value": "bad"}')[0])
        self.assertFalse(plugin.validate_payload("{value: 2}")[0])
        self.assertFalse(plugin.validate_payload("x" * 200_001)[0])
        with self.assertRaises(ValidationError):
            VisualizerManifest(**base, payload_schema={"$ref": "https://invalid.example/schema"})


class TopicTests(unittest.TestCase):
    def test_title_normalization_similarity_without_queue_runtime(self):
        self.assertEqual(normalize_topic("  Wave   BASICS  "), "wave basics")
        self.assertEqual(topic_tokens("The basics of waves"), {"basic", "wave"})
        self.assertEqual(topic_similarity("The basics of waves", "Wave basics"), 1)
        self.assertEqual(topic_similarity("", "Wave basics"), 0)


class CacheAndFileTests(unittest.TestCase):
    def test_partial_cache_retained_and_complete_ir_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key, signature = "a" * 64, "b" * 64
            workdir = parser_cache.reserve(root, key, signature)
            (workdir / "partial.md").write_text("partial", encoding="utf-8")
            self.assertIsNone(parser_cache.lookup(root, key, signature))
            workdir = parser_cache.reserve(root, key, signature)
            retained = list(workdir.parent.glob(f".{signature}.failed-*"))
            self.assertEqual(len(retained), 1)
            self.assertEqual((retained[0] / "partial.md").read_text("utf-8"), "partial")
            (workdir / "fixture.md").write_text("Complete fixture", encoding="utf-8")
            parser_cache.write_manifest(workdir, {"source_digest": key, "parser_signature": signature})
            self.assertEqual(parser_cache.lookup(root, key, signature), workdir)
            self.assertEqual(parser_cache.load_ir(workdir)[0], "Complete fixture")

    def test_failed_atomic_replace_keeps_previous_target(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            atomic_write_json(path, {"version": "previous"})
            with patch("deeptutor_reuse.file_io._atomic_replace", side_effect=OSError("fixture-replace-failure")):
                with self.assertRaises(OSError):
                    atomic_write_json(path, {"version": "candidate"})
            self.assertEqual(json.loads(path.read_text("utf-8")), {"version": "previous"})
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main(verbosity=2)
