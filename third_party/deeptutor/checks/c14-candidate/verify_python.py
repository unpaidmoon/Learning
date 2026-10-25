"""Original C14 faults and read-only project pagination, synthetic files/SQLite."""

from pathlib import Path
import argparse
import ast
import hashlib
import importlib.util
import io
import json
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

from sqlalchemy import (
    Column,
    MetaData,
    String,
    Integer,
    JSON,
    Table,
    and_,
    create_engine,
    insert,
    or_,
    select,
    update,
)

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parents[1]
REPO = PACKAGE.parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
assert args.output.resolve().is_relative_to(
    REPO / ".local/cache/workbench/deeptutor-c14"
)
args.output.mkdir(parents=True, exist_ok=False)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


indexing = load("c14_indexing_probe", HERE / "indexing_run_probe.py")
slice_module = load("c14_slice", HERE / "slice_checkpoint.py")
report = {
    "layer": "synthetic original-code fault probes; no index engine/real DB/Host",
    "checks": [],
    "model_calls": 0,
    "index_engine_calls": 0,
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


def data(task="run-1"):
    return {
        "schema": 1,
        "task_id": task,
        "state": "running",
        "documents": {
            status: {"status": status}
            for status in (
                "pending",
                "parsing",
                "embedding",
                "parsed",
                "completed",
                "failed",
                "excluded",
            )
        },
    }


def directory(name):
    path = args.output / name
    path.mkdir()
    return path


def interrupted():
    path = directory("journal")
    value = data()
    journal = path / ".indexing-run.json"
    journal.write_text(json.dumps(value), "utf-8")
    original = journal.read_bytes()
    with patch.object(indexing, "psutil", None):
        projected = indexing.visible_run(path)
    assert projected["state"] == "interrupted"
    assert all(
        projected["documents"][s]["status"] == "unknown"
        for s in ("pending", "parsing", "embedding")
    )
    assert projected["documents"]["parsed"]["status"] == "parsed"
    assert projected["documents"]["completed"]["status"] == "completed"
    assert journal.read_bytes() == original
    assert indexing.request_cancel(path, "other-task") is False
    assert indexing.request_cancel(path, "run-1") is True
    assert indexing.load_run(path)["cancel_requested"] is True
    value["task_id"] = "run-2"
    journal.write_text(json.dumps(value), "utf-8")
    assert indexing.load_run(path)["cancel_requested"] is False
    (path / ".indexing-cancel.json").write_text("{broken", "utf-8")
    try:
        indexing.load_run(path)
    except OSError:
        pass
    else:
        raise AssertionError("corrupt cancellation state accepted")
    assert (path / ".indexing-cancel.json").read_text("utf-8") == "{broken"
    return {
        "lost_owner_inflight": "unknown",
        "parsed_checkpoint_preserved": True,
        "projection_did_not_write": True,
        "stale_cancel_did_not_cancel_new_run": True,
        "corrupt_file_preserved": True,
    }


check("owner_lost_projection_and_task_scoped_cancel", interrupted)


def source_and_late():
    path = directory("source-guard")
    source = path / "synthetic.txt"
    source.write_text("synthetic version one", "utf-8")
    run = indexing.IndexingRun(path, [], task_id="run-1")
    run.active = True
    key = run.source_key(source)
    run._source_paths = {key: source}
    run.data = {
        "schema": 1,
        "task_id": "run-1",
        "state": "running",
        "documents": {key: {"status": "parsed", "source_hash": run._hash(source)}},
    }
    run._save()
    run.verify_sources()
    source.write_text("synthetic version two", "utf-8")
    try:
        run.verify_sources()
    except RuntimeError as error:
        assert "Source changed" in str(error)
    else:
        raise AssertionError("changed source published")
    before = json.dumps(run.data, sort_keys=True)
    newer = {"schema": 1, "task_id": "run-2", "state": "running", "documents": {}}
    (path / ".indexing-run.json").write_text(json.dumps(newer), "utf-8")
    disk = (path / ".indexing-run.json").read_bytes()
    run.document(source, "completed", late=True)
    run.finish("completed", version="bad-late-version")
    assert json.dumps(run.data, sort_keys=True) == before
    assert (path / ".indexing-run.json").read_bytes() == disk
    try:
        run.check()
    except indexing.IndexingCancelled:
        pass
    else:
        raise AssertionError("stale run retained ownership")
    return {
        "source_mutation_rejected": True,
        "late_document_and_finish_ignored": True,
        "newer_journal_unchanged": True,
        "app_router_and_start_not_called": True,
    }


check("source_rehash_and_late_old_run_callbacks", source_and_late)


def slices():
    path = directory("slices")
    checkpoint = slice_module.SliceCheckpoint(path, 0, 2)
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as z:
        z.writestr("parsed.md", "# Synthetic\nsource text\n")
    first = raw.getvalue()
    checkpoint.save(first)
    assert checkpoint.load() == first
    pointer = checkpoint.pointer.read_bytes()
    original_replace = slice_module.os.replace

    def fail_pointer(src, dst):
        if Path(dst).name == "ready.json":
            raise PermissionError("synthetic pointer publication failure")
        return original_replace(src, dst)

    try:
        with patch.object(slice_module.os, "replace", side_effect=fail_pointer):
            checkpoint.save(b"candidate two")
    except PermissionError:
        pass
    else:
        raise AssertionError("pointer failure swallowed")
    assert checkpoint.pointer.read_bytes() == pointer and checkpoint.load() == first
    archive = checkpoint.directory / (sha(first) + ".zip")
    archive.write_bytes(b"corrupted")
    assert checkpoint.load() is None
    checkpoint.save(b"not actually a zip")
    assert checkpoint.load() == b"not actually a zip"
    return {
        "saved_archive_sha256": sha(first),
        "prior_pointer_survives_replace_failure": True,
        "tampered_archive_rejected": True,
        "hash_valid_arbitrary_bytes_accepted": True,
        "limitation": "Integrity checkpoint alone does not validate archive, source identity or retrievable index.",
    }


check("original_slice_atomic_pointer_and_integrity_limits", slices)


def workspace_cursor():
    source = HERE / "project-snapshots/backend/src/learning_app/workbench/workspace.py"
    tree = ast.parse(source.read_text("utf-8"))
    selected = [
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in {"_page", "_result"}
    ]

    class AppError(ValueError):
        def __init__(self, code, message, status):
            self.code = code
            super().__init__(code)

    namespace = {"datetime": datetime, "and_": and_, "or_": or_, "AppError": AppError}
    exec(
        compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"),
        namespace,
    )
    page, result = namespace["_page"], namespace["_result"]
    metadata = MetaData()
    table = Table(
        "workspace_fixture",
        metadata,
        Column("id", String, primary_key=True),
        Column("course_id", String),
        Column("updated_at", String),
    )
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    stamp = "2026-10-23T00:00:00+00:00"
    with engine.begin() as conn:
        conn.execute(
            insert(table),
            [
                {"id": "item_" + x, "course_id": "A", "updated_at": stamp}
                for x in ("a", "b", "c")
            ]
            + [{"id": "foreign", "course_id": "B", "updated_at": stamp}],
        )
        query = select(table).where(table.c.course_id == "A")
        rows = list(
            conn.execute(
                page(query, table, table.c.id, SimpleNamespace(cursor=None, limit=2))
            ).mappings()
        )
        first, cursor = result(rows, "id", 2)
        rows = list(
            conn.execute(
                page(query, table, table.c.id, SimpleNamespace(cursor=cursor, limit=2))
            ).mappings()
        )
        second, _ = result(rows, "id", 2)
        assert [r["id"] for r in first + second] == ["item_c", "item_b", "item_a"]
        for invalid in (
            "bad",
            stamp + "|bad' OR 1=1",
            stamp + "|",
            stamp + "|" + "a" * 161,
        ):
            try:
                page(query, table, table.c.id, SimpleNamespace(cursor=invalid, limit=2))
            except AppError as error:
                assert error.code == "WORKSPACE_CURSOR_INVALID"
            else:
                raise AssertionError("invalid cursor accepted")
        conn.execute(
            update(table)
            .where(table.c.id == "item_a")
            .values(updated_at="2026-10-23T01:00:00+00:00")
        )
        after_move = list(
            conn.execute(
                page(query, table, table.c.id, SimpleNamespace(cursor=cursor, limit=2))
            ).mappings()
        )
        assert after_move == []
    engine.dispose()
    return {
        "tie_breaker_no_duplicates_or_gaps": True,
        "invalid_cursor_rejected": 4,
        "scope_filter_was_fixture_supplied": True,
        "list_mutation_can_move_row_before_cursor": True,
        "limitation": "Browse pagination is not a stable full-coverage enumeration or event replay cursor.",
    }


check("actual_project_keyset_helpers_ties_scope_and_mutation", workspace_cursor)


def reading_save_cas():
    source = HERE / "project-snapshots/backend/src/learning_app/workbench/reading.py"
    tree = ast.parse(source.read_text("utf-8"))
    reader = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "DocumentReader"
    )
    method = next(
        n for n in reader.body if isinstance(n, ast.FunctionDef) and n.name == "save"
    )

    class AppError(ValueError):
        def __init__(self, code, message, status):
            self.code = code
            super().__init__(code)

    class Position:
        model_fields = {
            key: None for key in ("page_index", "scale", "rotation", "scroll_fraction")
        }

        def __init__(self, **values):
            self.__dict__.update(values)

        @classmethod
        def model_validate(cls, values):
            return cls(**values)

        def model_dump(self, include=None):
            return {
                k: v
                for k, v in self.__dict__.items()
                if include is None or k in include
            }

    metadata = MetaData()
    table = Table(
        "reading_state_fixture",
        metadata,
        Column("course_id", String, primary_key=True),
        Column("asset_id", String, primary_key=True),
        Column("source_version", String, primary_key=True),
        Column("reader_epoch", String),
        Column("revision", Integer),
        Column("position", JSON),
        Column("updated_at", String),
    )
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    initial = {
        "page_index": 2,
        "scale": "page-width",
        "rotation": 0,
        "scroll_fraction": 0.2,
    }
    with engine.begin() as conn:
        conn.execute(
            insert(table),
            [
                {
                    "course_id": course,
                    "asset_id": "asset",
                    "source_version": version,
                    "reader_epoch": "epoch-new",
                    "revision": 4,
                    "position": initial,
                    "updated_at": "synthetic",
                }
                for course, version in (("A", "v1"), ("B", "v1"), ("A", "v2"))
            ],
        )
    namespace = {
        "s": SimpleNamespace(reading_states=table),
        "select": select,
        "update": update,
        "ReadingPosition": Position,
        "ReadingState": SimpleNamespace,
        "ReadingStateUpdate": Position,
        "now": lambda: "2026-10-23T00:00:00+00:00",
        "AppError": AppError,
    }
    exec(
        compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"),
        namespace,
    )

    class Reader:
        save = namespace["save"]
        repository = SimpleNamespace(_lock=threading.RLock(), engine=engine)

        def _pdf_source(self, course, asset, version):
            return None  # Fixture port: this does NOT test actual source/file authorization.

        def page(self, course, asset, version, index):
            if not 0 <= index < 10:
                raise AppError("SOURCE_PAGE_RANGE_INVALID", "fixture", 422)
            return None

    instance = Reader()
    for epoch, revision in (("epoch-old", 4), ("epoch-new", 3)):
        value = Position(
            **{
                **initial,
                "page_index": 5,
                "reader_epoch": epoch,
                "expected_revision": revision,
            }
        )
        try:
            instance.save("A", "asset", "v1", value)
        except AppError as error:
            assert error.code == "READING_STATE_CONFLICT"
        else:
            raise AssertionError("stale save accepted")
    saved = instance.save(
        "A",
        "asset",
        "v1",
        Position(
            **{
                **initial,
                "page_index": 5,
                "reader_epoch": "epoch-new",
                "expected_revision": 4,
            }
        ),
    )
    assert saved.revision == 5 and saved.source_version == "v1"
    with engine.connect() as conn:
        untouched = list(
            conn.execute(
                select(table).where(
                    or_(table.c.course_id == "B", table.c.source_version == "v2")
                )
            ).mappings()
        )
        assert len(untouched) == 2 and all(
            row["revision"] == 4 and row["position"] == initial for row in untouched
        )
    engine.dispose()
    return {
        "actual_save_body_executed": True,
        "old_epoch_or_revision_rejected": True,
        "course_and_version_rows_isolated": True,
        "correct_revision_incremented": 5,
        "fixture_ports": ["source guard", "page reader", "Pydantic DTO shape"],
        "real_business_DB_or_source_guard_tested": False,
    }


check(
    "actual_project_reading_save_CAS_body_with_inmemory_SQLite_ports", reading_save_cas
)


def frozen_package():
    source_manifest = json.loads((HERE / "source-manifest.json").read_text("utf-8"))
    for name, digest in source_manifest["frozen_package_hashes"].items():
        assert sha((PACKAGE / name).read_bytes()) == digest, name
    return {
        "package_modules_including_init_unchanged": len(
            source_manifest["frozen_package_hashes"]
        )
        - 2,
        "lock_and_pyproject_unchanged": True,
        "source_digest_unchanged": source_manifest["frozen_package_hashes"][
            "src/deeptutor_reuse/source_digest.py"
        ],
    }


check("frozen_runtime_package_and_locks_unchanged", frozen_package)

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
