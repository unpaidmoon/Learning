"""One bounded local indexing step and immutable concept evidence.

PDF parsing/OCR caches are shared with reading. No video, subtitle, shell,
provider, embedding service or automatic question matching is reachable here.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from deeptutor_reuse.source_windows import TextUnit, split_text_unit
from sqlalchemy import column, func, insert, or_, select, table, text, update

from learning_app.course_files import CourseFileStore, _is_link
from learning_app.errors import AppError
from learning_app.storage.repository import _hash

from . import corpus_schema as c
from . import schema as s
from .common import new_id, now, prior_operation, save_operation
from .corpus_models import CorpusPolicy, CorpusPolicyReceipt, CorpusStatus
from .models import ArtifactBody, DocumentRef, block_text
from .reading import DocumentReader
from .sources import TEXT_PARSER_VERSION, SourceStore

SUPPORTED = frozenset({".pdf", ".txt", ".md", ".markdown"})
WINDOW = 2400
TEXT_UNIT = 24_000
RESCAN_SECONDS = 30
INDEX_VERSION = "corpus-v2-codepoint-units"


def excluded(path, exclusions):
    folded = path.replace("\\", "/").casefold()
    return any(
        folded == part.casefold() or folded.startswith(part.casefold() + "/") for part in exclusions
    )


def concept_argument(arguments):
    if (
        not isinstance(arguments, dict)
        or set(arguments) != {"concept"}
        or not isinstance(arguments["concept"], str)
    ):
        raise AppError("COURSE_CONCEPT_ARGUMENTS", "只接受一个课程概念检索词", 422)
    concept = arguments["concept"].strip()
    if (
        not 2 <= len(concept) <= 80
        or any(ord(ch) < 32 for ch in concept)
        or re.search(
            r"第\s*[0-9一二三四五六七八九十]+\s*题|题号|原题|question\s*#?\s*\d+", concept, re.I
        )
    ):
        raise AppError(
            "COURSE_CONCEPT_ARGUMENTS", "请输入简短概念，不支持按题号或题意寻找原题", 422
        )
    return concept


class CourseCorpus:
    def __init__(self, repository):
        self.repository = repository
        self.sources = SourceStore(repository)
        self.files = CourseFileStore(repository)

    @staticmethod
    def profile(conn, course):
        row = (
            conn.execute(select(c.profiles).where(c.profiles.c.course_id == course))
            .mappings()
            .first()
        )
        return dict(row) if row else {"revision": 0, "exclusions": []}

    def status(self, course, *, after="", limit=100):
        self.files.folder(course)
        with self.repository.engine.connect() as conn:
            profile = self.profile(conn, course)
            scan = (
                conn.execute(select(c.scans).where(c.scans.c.course_id == course))
                .mappings()
                .first()
            )
            docs = list(
                conn.execute(
                    select(c.documents)
                    .where(
                        c.documents.c.course_id == course,
                        c.documents.c.current.is_(True),
                        c.documents.c.relative_path > after,
                    )
                    .order_by(c.documents.c.relative_path)
                    .limit(limit + 1)
                ).mappings()
            )
            return CorpusStatus(
                revision=profile["revision"],
                exclusions=profile["exclusions"],
                scan_state=scan["state"] if scan else "pending",
                scan_error=scan["error_code"] if scan else None,
                documents=[
                    {
                        k: row[k]
                        for k in (
                            "document_id",
                            "relative_path",
                            "asset_id",
                            "source_version",
                            "cursor",
                            "total",
                            "state",
                            "error_code",
                        )
                    }
                    for row in docs[:limit]
                ],
                has_more=len(docs) > limit,
            )

    def configure(self, course, value: CorpusPolicy):
        self.files.folder(course)
        payload = value.model_dump(mode="json")
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(conn, course, "corpus_policy", value.operation_id, payload)
            if prior is not None:
                return prior
            profile = self.profile(conn, course)
            if profile["revision"] != value.expected_revision:
                raise AppError("CORPUS_POLICY_CONFLICT", "索引排除项已被另一窗口修改", 409)
            result = {"revision": profile["revision"] + 1, "exclusions": value.exclusions}
            change = {**result, "updated_at": now()}
            if profile["revision"]:
                conn.execute(
                    update(c.profiles).where(c.profiles.c.course_id == course).values(**change)
                )
            else:
                conn.execute(insert(c.profiles).values(course_id=course, **change))
            # Exclusions take effect at query/read authorization immediately.
            # The scan scope hash restarts discovery without deleting assets.
            save_operation(
                conn,
                course,
                "corpus_policy",
                value.operation_id,
                payload,
                CorpusPolicyReceipt(**result),
            )
            return result

    @staticmethod
    def _cancel(cancelled):
        if cancelled and cancelled.is_set():
            raise AppError("PREPARATION_INTERRUPTED", "前台操作优先，索引已保留检查点")

    def discover(self, course, *, force=False, cancelled=None):
        """Persist a directory queue, rather than trusting a mutable timestamp cursor."""
        root = self.files._root(course)
        with self.repository.engine.connect() as conn:
            profile = self.profile(conn, course)
            old = (
                conn.execute(select(c.scans).where(c.scans.c.course_id == course))
                .mappings()
                .first()
            )
        scope_hash = _hash(
            {"root": str(root), "revision": profile["revision"], "indexer": INDEX_VERSION}
        )
        if old and old["scope_hash"] == scope_hash and old["state"] == "running":
            scan_id, pending = old["scan_id"], list(old["pending"])
        elif (
            old
            and old["scope_hash"] == scope_hash
            and not force
            and old["updated_at"]
            > (datetime.now(UTC) - timedelta(seconds=RESCAN_SECONDS)).isoformat()
        ):
            return False
        else:
            scan_id, pending = new_id("corpus_scan"), [{"directory": "", "after": ""}]
        directory = pending[0]
        folder = self.files._path(root, directory["directory"])
        entries = sorted(folder.iterdir(), key=lambda p: p.name)
        if len(entries) > 50_000:
            raise AppError("CORPUS_DIRECTORY_LIMIT", "单目录条目过多，保留缺口而不截断")
        page = [entry for entry in entries if entry.name > directory["after"]][:50]
        found = []
        for entry in page:
            self._cancel(cancelled)
            relative = entry.relative_to(root).as_posix()
            if _is_link(entry) or excluded(relative, profile["exclusions"]):
                continue
            checked = self.files._path(root, relative)
            if checked.is_dir():
                pending.append({"directory": relative, "after": ""})
            elif checked.suffix.lower() in SUPPORTED:
                source = self.files._register(course, root, checked)
                found.append((relative, source))
        if len(page) == 50:
            pending[0] = {**directory, "after": page[-1].name}
        else:
            pending.pop(0)
        self._cancel(cancelled)
        with self.repository._lock, self.repository.engine.begin() as conn:
            if self.profile(conn, course)["revision"] != profile["revision"]:
                return False
            for relative, source in found:
                document_id = "corpus_" + _hash(
                    {
                        "course": course,
                        "path": relative,
                        "version": source["source_version"],
                        "indexer": INDEX_VERSION,
                    }
                )
                conn.execute(
                    update(c.documents)
                    .where(
                        c.documents.c.course_id == course,
                        c.documents.c.relative_path == relative,
                        c.documents.c.document_id != document_id,
                    )
                    .values(current=False)
                )
                prior = conn.execute(
                    select(c.documents.c.state).where(c.documents.c.document_id == document_id)
                ).first()
                if prior:
                    changes = {"seen_scan": scan_id, "current": True}
                    if prior.state in {"missing", "needs_source"}:
                        changes.update(state="pending", error_code=None, cursor=0)
                    conn.execute(
                        update(c.documents)
                        .where(c.documents.c.document_id == document_id)
                        .values(**changes)
                    )
                else:
                    conn.execute(
                        insert(c.documents).values(
                            document_id=document_id,
                            course_id=course,
                            relative_path=relative,
                            asset_id=source["asset_id"],
                            source_version=source["source_version"],
                            source_digest=source["content_hash"],
                            seen_scan=scan_id,
                            current=True,
                            cursor=0,
                            total=None,
                            state="pending",
                            updated_at=now(),
                        )
                    )
            value = dict(
                scan_id=scan_id,
                scope_hash=scope_hash,
                pending=pending,
                state="running" if pending else "completed",
                error_code=None,
                updated_at=now(),
            )
            if old:
                conn.execute(update(c.scans).where(c.scans.c.course_id == course).values(**value))
            else:
                conn.execute(insert(c.scans).values(course_id=course, **value))
            if not pending:
                conn.execute(
                    update(c.documents)
                    .where(
                        c.documents.c.course_id == course,
                        c.documents.c.seen_scan != scan_id,
                        c.documents.c.current.is_(True),
                    )
                    .values(current=False, state="missing")
                )
        return True

    def index_next(self, course, *, cancelled=None):
        with self.repository.engine.connect() as conn:
            profile = self.profile(conn, course)
            docs = (
                conn.execute(
                    select(c.documents)
                    .where(
                        c.documents.c.course_id == course,
                        c.documents.c.current.is_(True),
                        c.documents.c.state.in_(["pending", "indexing", "needs_ocr"]),
                    )
                    .order_by(c.documents.c.updated_at, c.documents.c.document_id)
                )
                .mappings()
                .all()
            )
            candidates = [
                dict(row)
                for row in docs
                if not excluded(row["relative_path"], profile["exclusions"])
            ]
        if not candidates:
            return False
        doc = candidates[0]
        try:
            self._index_unit(doc, profile, cancelled)
        except AppError as error:
            if error.code == "PREPARATION_INTERRUPTED":
                raise
            with self.repository._lock, self.repository.engine.begin() as conn:
                conn.execute(
                    update(c.documents)
                    .where(c.documents.c.document_id == doc["document_id"])
                    .values(state="failed", error_code=error.code, updated_at=now())
                )
        return True

    def _index_unit(self, doc, profile, cancelled):
        course, asset, version = doc["course_id"], doc["asset_id"], doc["source_version"]
        index = doc["cursor"]
        if doc["state"] == "needs_ocr":
            with self.repository.engine.connect() as conn:
                missing = (
                    conn.execute(
                        select(c.units.c.unit_index)
                        .where(
                            c.units.c.document_id == doc["document_id"],
                            c.units.c.state == "needs_ocr",
                        )
                        .order_by(c.units.c.unit_index)
                    )
                    .scalars()
                    .all()
                )
                ready = set(
                    conn.execute(
                        select(s.document_ocr.c.page_index).where(
                            s.document_ocr.c.course_id == course,
                            s.document_ocr.c.asset_id == asset,
                            s.document_ocr.c.source_version == version,
                        )
                    ).scalars()
                )
            indexes = [page for page in missing if page in ready]
            if not indexes:
                # Other documents must not be starved by an unready scan page.
                with self.repository.engine.begin() as conn:
                    conn.execute(
                        update(c.documents)
                        .where(c.documents.c.document_id == doc["document_id"])
                        .values(updated_at=now())
                    )
                return
            index = indexes[0]
        self._cancel(cancelled)
        base_offset = 0
        page_index = index
        if Path(doc["relative_path"]).suffix.lower() == ".pdf":
            page = DocumentReader(self.repository).page(
                course, asset, version, index, cancelled=cancelled
            )
            body, parser, total, label = (
                page.text,
                page.parser_version,
                page.page_count,
                page.page_label,
            )
            missing = page.text_status == "needs_ocr"
        else:
            body, _ = self.sources.text(course, asset, version)
            total = max(1, (len(body) + TEXT_UNIT - 1) // TEXT_UNIT)
            base_offset, page_index = index * TEXT_UNIT, 0
            body = body[base_offset : base_offset + TEXT_UNIT]
            parser, label, missing = TEXT_PARSER_VERSION, None, False
        unit = TextUnit(
            doc["document_id"] + f":{index}",
            asset,
            version,
            parser,
            "original",
            body,
            page_index=page_index,
        )
        snapshots = []
        for window in split_text_unit(unit, WINDOW):
            self._cancel(cancelled)
            if not window.text.strip():
                continue
            snapshots.append(
                self.sources._save_verified(
                    course,
                    DocumentRef(
                        asset_id=asset,
                        source_version=version,
                        source_digest=doc["source_digest"],
                        page_index=page_index,
                        page_label=label,
                        start=base_offset + window.start_codepoint,
                        end=base_offset + window.end_codepoint,
                        parser_version=parser,
                    ),
                    window.text,
                    "original",
                )
            )
        self._cancel(cancelled)
        # Verify the actual original again before publishing an immutable slice.
        self.sources._source(course, asset, version)
        with self.repository._lock, self.repository.engine.begin() as conn:
            current = (
                conn.execute(
                    select(c.documents).where(c.documents.c.document_id == doc["document_id"])
                )
                .mappings()
                .one()
            )
            if (
                not current["current"]
                or self.profile(conn, course)["revision"] != profile["revision"]
            ):
                return
            conn.execute(
                update(c.chunks)
                .where(c.chunks.c.document_id == doc["document_id"], c.chunks.c.unit_index == index)
                .values(active=False)
            )
            for snapshot in snapshots:
                chunk_id = "chunk_" + _hash(
                    {"document": doc["document_id"], "snapshot": snapshot.content_hash}
                )
                if conn.execute(
                    select(c.chunks.c.chunk_id).where(c.chunks.c.chunk_id == chunk_id)
                ).first():
                    conn.execute(
                        update(c.chunks)
                        .where(c.chunks.c.chunk_id == chunk_id)
                        .values(active=True, unit_index=index)
                    )
                else:
                    conn.execute(
                        insert(c.chunks).values(
                            chunk_id=chunk_id,
                            document_id=doc["document_id"],
                            unit_index=index,
                            snapshot_id=snapshot.snapshot_id,
                            active=True,
                        )
                    )
                    conn.execute(
                        text("INSERT INTO learning_corpus_fts(chunk_id,text) VALUES (:id,:text)"),
                        {"id": chunk_id, "text": snapshot.text},
                    )
            values = dict(
                text_revision=_hash({"parser": parser, "text": body}),
                state="needs_ocr" if missing else "ready",
            )
            condition = (c.units.c.document_id == doc["document_id"], c.units.c.unit_index == index)
            if conn.execute(select(c.units.c.state).where(*condition)).first():
                conn.execute(update(c.units).where(*condition).values(**values))
            else:
                conn.execute(
                    insert(c.units).values(
                        document_id=doc["document_id"], unit_index=index, **values
                    )
                )
            cursor = max(doc["cursor"], index + 1)
            gaps = conn.execute(
                select(func.count())
                .select_from(c.units)
                .where(c.units.c.document_id == doc["document_id"], c.units.c.state == "needs_ocr")
            ).scalar_one()
            state = "indexing" if cursor < total else "needs_ocr" if gaps else "ready"
            conn.execute(
                update(c.documents)
                .where(c.documents.c.document_id == doc["document_id"])
                .values(
                    cursor=cursor,
                    total=total,
                    state=state,
                    error_code="OCR_PAGE_NOT_READY" if gaps else None,
                    updated_at=now(),
                )
            )

    @classmethod
    def document_allowed(cls, conn, course, document_id):
        doc = (
            conn.execute(
                select(c.documents).where(
                    c.documents.c.document_id == document_id,
                    c.documents.c.course_id == course,
                    c.documents.c.current.is_(True),
                )
            )
            .mappings()
            .first()
        )
        if doc is None or excluded(doc["relative_path"], cls.profile(conn, course)["exclusions"]):
            raise AppError("COURSE_CORPUS_SOURCE_DENIED", "资料已排除或原索引版本不再当前", 403)
        return doc

    @classmethod
    def availability(cls, conn, course):
        exclusions = cls.profile(conn, course)["exclusions"]
        counts = {}
        for row in conn.execute(
            select(c.documents.c.relative_path, c.documents.c.state).where(
                c.documents.c.course_id == course, c.documents.c.current.is_(True)
            )
        ).mappings():
            if not excluded(row["relative_path"], exclusions):
                counts[row["state"]] = counts.get(row["state"], 0) + 1
        scan = conn.execute(
            select(c.scans.c.state).where(c.scans.c.course_id == course)
        ).scalar_one_or_none()
        return {
            "scan_state": scan or "pending",
            "document_states": counts,
            "incomplete": scan != "completed"
            or any(state != "ready" and count for state, count in counts.items()),
        }

    def search(self, course, concept):
        """Lexical concept retrieval, never a claim of semantic or mathematical equivalence."""
        self.files._root(course)
        escaped = concept.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self.repository.engine.connect() as conn:
            exclusions = self.profile(conn, course)["exclusions"]
            allowed = [
                c.documents.c.course_id == course,
                c.documents.c.current.is_(True),
                c.chunks.c.active.is_(True),
            ]
            for path in exclusions:
                escaped_path = path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                allowed.append(
                    ~or_(
                        c.documents.c.relative_path.ilike(escaped_path, escape="\\"),
                        c.documents.c.relative_path.ilike(escaped_path + "/%", escape="\\"),
                    )
                )
            if len(concept) >= 3:
                # Quoted phrase, no user-controlled FTS syntax or SQL identifiers.
                fts = table("learning_corpus_fts", column("chunk_id"), column("rank"))
                ids = list(
                    conn.execute(
                        select(fts.c.chunk_id)
                        .where(
                            text("learning_corpus_fts MATCH :q"),
                            fts.c.chunk_id.in_(
                                select(c.chunks.c.chunk_id).join(c.documents).where(*allowed)
                            ),
                        )
                        .order_by(fts.c.rank)
                        .limit(100),
                        {"q": '"' + concept.replace('"', '""') + '"'},
                    ).scalars()
                )
                condition = c.chunks.c.chunk_id.in_(ids)
            else:
                condition = s.snapshots.c.text.like(f"%{escaped}%", escape="\\")
            rows = (
                conn.execute(
                    select(c.chunks, c.documents.c.relative_path)
                    .join(c.documents)
                    .join(s.snapshots, s.snapshots.c.snapshot_id == c.chunks.c.snapshot_id)
                    .where(
                        *allowed,
                        condition,
                    )
                    .order_by(
                        c.documents.c.relative_path, c.chunks.c.unit_index, c.chunks.c.chunk_id
                    )
                    .limit(100)
                )
                .mappings()
                .all()
            )
            if len(concept) >= 3:
                rows.sort(key=lambda row: ids.index(row["chunk_id"]))
        results = []
        for row in rows:
            if excluded(row["relative_path"], exclusions):
                continue
            snapshot = self.sources.snapshot(course, row["snapshot_id"])
            try:
                self.sources.require_current(snapshot)
            except AppError as error:
                if error.code == "SOURCE_VERSION_UNAVAILABLE":
                    with self.repository.engine.begin() as conn:
                        conn.execute(
                            update(c.documents)
                            .where(c.documents.c.document_id == row["document_id"])
                            .values(state="needs_source", error_code=error.code, updated_at=now())
                        )
                continue
            results.append(
                {
                    "snapshot": snapshot,
                    "document_id": row["document_id"],
                    "label": row["relative_path"],
                }
            )
            if len(results) == 4:
                break
        # Editable assets keep their actual author role; never promote AI to textbook evidence.
        with self.repository.engine.connect() as conn:
            assets = (
                conn.execute(
                    select(s.artifacts, s.revisions.c.body)
                    .join(s.revisions, s.revisions.c.revision_id == s.artifacts.c.head_revision_id)
                    .join(
                        s.search_entries,
                        s.search_entries.c.artifact_id == s.artifacts.c.artifact_id,
                    )
                    .where(
                        s.artifacts.c.course_id == course,
                        s.artifacts.c.indexed.is_(True),
                        s.search_entries.c.text.like(f"%{escaped}%", escape="\\"),
                    )
                    .order_by(s.artifacts.c.artifact_id)
                    .limit(4)
                )
                .mappings()
                .all()
            )
        for asset in assets:
            for block in ArtifactBody.model_validate(asset["body"]).blocks:
                body = block_text(block)
                match = body.casefold().find(concept.casefold())
                if match < 0:
                    continue
                snapshot = self.sources.capture_artifact(
                    course, asset["artifact_id"], asset["head_revision_id"], block.block_id
                )
                if len(snapshot.text) > WINDOW:
                    start = max(0, match - 500)
                    snapshot = self.sources.slice(
                        snapshot, start, min(len(snapshot.text), start + WINDOW)
                    )
                results.append(
                    {
                        "snapshot": snapshot,
                        "artifact_id": asset["artifact_id"],
                        "revision_id": asset["head_revision_id"],
                        "label": asset["title"],
                    }
                )
                break
        return results[:8]
