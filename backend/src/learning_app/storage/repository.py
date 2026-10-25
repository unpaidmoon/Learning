"""Project-owned SQLite and attachment publication behind a small repository.

No method calls Harness or a provider. In particular recovery and history merge
return facts; only the execution service may decide whether a *new* dispatch is
safe. SQLite/file/Host are separate durable domains with explicit fault hooks.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from alembic import command
from alembic.config import Config
from deeptutor_reuse.source_digest import source_digest
from PIL import Image, UnidentifiedImageError
from sqlalchemy import and_, create_engine, event, exists, func, insert, or_, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from learning_app.execution.usage import usage_is_settled, usage_projection

from . import schema as s

FaultHook = Callable[[str, dict[str, Any]], None]
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "truncated"})
ACTIVE_STATES = frozenset({"dispatching", "accepted", "running", "unknown"})
SCHEMA_REVISION = "0035_chat_workspaces"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


class StorageError(RuntimeError):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _checked_id(value: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise StorageError("INVALID_ID")
    return value


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _file_hash(path: Path) -> str:
    # Fixed DeepTutor C07 excerpt; the project still owns authorization/version rules.
    return source_digest(path)


def _one(conn: Connection, table: Any, condition: Any, code: str) -> dict[str, Any]:
    row = conn.execute(select(table).where(condition)).mappings().first()
    if row is None:
        raise StorageError(code)
    return dict(row)


def _atomic_publish(source: Path, target: Path) -> None:
    # Both paths are inside one data root/volume. On Windows retain the native
    # write-through flag; no globally installed DLL is touched.
    if os.name == "nt":
        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move.restype = ctypes.c_int
        if not move(str(source), str(target), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


class Repository:
    def __init__(self, root: Path, *, fault_hook: FaultHook | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "business.sqlite3"
        self.objects = self.root / "attachments" / "objects"
        self.staging = self.root / "attachments" / "staging"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        if not self.objects.resolve().is_relative_to(
            self.root
        ) or not self.staging.resolve().is_relative_to(self.root):
            raise StorageError("ATTACHMENT_PATH_ESCAPE")
        self.fault_hook = fault_hook
        self._lock = threading.RLock()
        self.engine = create_engine(
            f"sqlite:///{self.db_path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": 15},
            poolclass=NullPool,
        )

        @event.listens_for(self.engine, "connect")
        def configure(dbapi_connection: Any, _record: Any) -> None:
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=15000")
            # Python 3.12.5's SQLite 3.45.3 lacks the WAL-reset fix. The
            # explicitly supported rollback journal is safer than replacing a
            # global DLL or claiming that a venv upgrades SQLite.
            cursor.execute("PRAGMA journal_mode=DELETE")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.close()

        @event.listens_for(self.engine, "begin")
        def begin(conn: Connection) -> None:
            # Explicit BEGIN also makes DDL transactional under sqlite3's
            # otherwise legacy transaction handling.
            conn.exec_driver_sql("BEGIN IMMEDIATE")

        self._migrate()

    def _hook(self, name: str, **detail: Any) -> None:
        if self.fault_hook:
            self.fault_hook(name, detail)

    def _migrate(self) -> None:
        previous: str | None = None
        existing = self.db_path.exists() and self.db_path.stat().st_size > 0
        if existing:
            with closing(sqlite3.connect(self.db_path)) as conn:
                has_version = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='alembic_version'"
                ).fetchone()
                if has_version:
                    row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
                    previous = row[0] if row else None
            if previous == SCHEMA_REVISION:
                return
        backup: Path | None = None
        if existing:
            backups = self.root / "migration-recovery"
            backups.mkdir(exist_ok=True)
            backup = backups / f"before-{SCHEMA_REVISION}-{uuid4().hex}.sqlite3"
            with closing(sqlite3.connect(self.db_path)) as source, closing(sqlite3.connect(backup)) as dest:
                source.backup(dest)
                if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise StorageError("MIGRATION_BACKUP_INVALID")
            with backup.open("r+b") as stream:
                os.fsync(stream.fileno())
        self._hook("migration_before_upgrade", previous=previous, backup=str(backup))
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        with self.engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
            self._hook("migration_before_commit", previous=previous)
        self._hook("migration_after_commit", revision=SCHEMA_REVISION)

    def close(self) -> None:
        self.engine.dispose()

    def runtime_info(self) -> dict[str, Any]:
        with self.engine.connect() as conn:
            return {
                "sqlite_version": conn.exec_driver_sql("SELECT sqlite_version()").scalar_one(),
                "journal_mode": conn.exec_driver_sql("PRAGMA journal_mode").scalar_one(),
                "synchronous": conn.exec_driver_sql("PRAGMA synchronous").scalar_one(),
                "foreign_keys": conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one(),
                "schema_revision": conn.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalar_one(),
                "attachment_publish": "fsync+MoveFileExW_WRITE_THROUGH"
                if os.name == "nt"
                else "fsync+replace+directory_fsync",
            }

    def create_course(self, title: str, course_id: str | None = None) -> dict[str, Any]:
        values = {
            "course_id": _checked_id(course_id or _id("course")),
            "title": title,
            "created_at": _now(),
        }
        with self._lock, self.engine.begin() as conn:
            conn.execute(insert(s.courses).values(**values))
        return values

    def list_courses(self) -> list[dict[str, Any]]:
        from learning_app.course_files_schema import course_roots, course_visibility

        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    select(s.courses)
                    .outerjoin(course_roots, course_roots.c.course_id == s.courses.c.course_id)
                    .outerjoin(
                        course_visibility, course_visibility.c.course_id == s.courses.c.course_id
                    )
                    .where(
                        or_(course_roots.c.state.is_(None), course_roots.c.state != "detached"),
                        or_(
                            course_visibility.c.hidden.is_(None),
                            course_visibility.c.hidden.is_(False),
                        ),
                    )
                ).mappings()
            ]

    def create_chat(self, course_id: str, chat_id: str | None = None) -> dict[str, Any]:
        values = {
            "chat_id": _checked_id(chat_id or _id("chat")),
            "course_id": course_id,
            "history_revision": 0,
            "current_question_id": None,
            "created_at": _now(),
        }
        with self._lock, self.engine.begin() as conn:
            _one(conn, s.courses, s.courses.c.course_id == course_id, "COURSE_NOT_FOUND")
            conn.execute(insert(s.chats).values(**values))
        return values

    def list_chats(self, course_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    select(s.chats).where(s.chats.c.course_id == course_id)
                ).mappings()
            ]

    def register_source(
        self,
        course_id: str,
        path: Path,
        *,
        asset_id: str | None = None,
        display_name: str | None = None,
        source_version: str | None = None,
    ) -> dict[str, Any]:
        path = Path(path).resolve(strict=True)
        asset_id = _checked_id(asset_id or _id("asset"))
        with self._lock, self.engine.begin() as conn:
            _one(conn, s.courses, s.courses.c.course_id == course_id, "COURSE_NOT_FOUND")
            conn.execute(
                insert(s.assets).values(
                    asset_id=asset_id, display_name=display_name or path.name, created_at=_now()
                )
            )
            conn.execute(insert(s.course_assets).values(course_id=course_id, asset_id=asset_id))
        return self.add_source_version(asset_id, path, source_version=source_version)

    def associate_source(self, course_id: str, asset_id: str) -> None:
        with self._lock, self.engine.begin() as conn:
            _one(conn, s.courses, s.courses.c.course_id == course_id, "COURSE_NOT_FOUND")
            _one(conn, s.assets, s.assets.c.asset_id == asset_id, "ASSET_NOT_FOUND")
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            conn.execute(
                sqlite_insert(s.course_assets)
                .values(course_id=course_id, asset_id=asset_id)
                .on_conflict_do_nothing()
            )

    def add_source_version(
        self, asset_id: str, path: Path, *, source_version: str | None = None
    ) -> dict[str, Any]:
        path = Path(path).resolve(strict=True)
        digest = _file_hash(path)
        values = {
            "asset_id": asset_id,
            "source_version": source_version or digest,
            "content_hash": digest,
            "byte_length": path.stat().st_size,
            "local_path": str(path),
            "created_at": _now(),
        }
        with self._lock, self.engine.begin() as conn:
            _one(conn, s.assets, s.assets.c.asset_id == asset_id, "ASSET_NOT_FOUND")
            existing = (
                conn.execute(
                    select(s.source_versions).where(
                        s.source_versions.c.asset_id == asset_id,
                        s.source_versions.c.source_version == values["source_version"],
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                if existing["content_hash"] != digest:
                    raise StorageError("SOURCE_VERSION_CONFLICT")
                return dict(existing)
            conn.execute(insert(s.source_versions).values(**values))
        return values

    def resolve_source(self, asset_id: str, source_version: str) -> dict[str, Any]:
        with self.engine.connect() as conn:
            row = _one(
                conn,
                s.source_versions,
                (s.source_versions.c.asset_id == asset_id)
                & (s.source_versions.c.source_version == source_version),
                "SOURCE_VERSION_NOT_FOUND",
            )
        path = Path(row["local_path"])
        if not path.is_file():
            row["status"] = "missing"
        elif _file_hash(path) != row["content_hash"]:
            row["status"] = "version_mismatch"
        else:
            row["status"] = "available"
        return row

    def relocate_source(self, asset_id: str, source_version: str, path: Path) -> dict[str, Any]:
        path = Path(path).resolve(strict=True)
        previous = self.resolve_source(asset_id, source_version)
        if _file_hash(path) != previous["content_hash"]:
            raise StorageError("SOURCE_VERSION_MISMATCH")
        with self._lock, self.engine.begin() as conn:
            conn.execute(
                update(s.source_versions)
                .where(
                    s.source_versions.c.asset_id == asset_id,
                    s.source_versions.c.source_version == source_version,
                )
                .values(local_path=str(path))
            )
        return self.resolve_source(asset_id, source_version)

    def _object_path(self, digest: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise StorageError("INVALID_ATTACHMENT_HASH")
        path = self.objects / digest[:2] / digest
        if not path.resolve().is_relative_to(self.objects.resolve()):
            raise StorageError("ATTACHMENT_PATH_ESCAPE")
        return path

    def stage_attachment(
        self,
        data: bytes,
        media_type: str = "image/png",
        *,
        confirmed: bool = True,
        attachment_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(data, bytes) or not data or len(data) > 20 * 1024 * 1024:
            raise StorageError("ATTACHMENT_SIZE_INVALID")
        if media_type in {"application/pdf", "text/plain", "text/markdown"}:
            from learning_app.document_processing import validate_original_bytes
            from learning_app.errors import AppError

            try:
                validate_original_bytes(data, media_type)
            except AppError as exc:
                raise StorageError(exc.code) from exc
        else:
            if media_type not in {"image/png", "image/jpeg", "image/webp"}:
                raise StorageError("ATTACHMENT_TYPE_UNSUPPORTED")
            valid = (
                (media_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
                or (media_type == "image/jpeg" and data.startswith(b"\xff\xd8\xff"))
                or (media_type == "image/webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP")
            )
            if not valid:
                raise StorageError("ATTACHMENT_CONTENT_INVALID")
            try:
                with Image.open(io.BytesIO(data)) as image:
                    if image.width * image.height > 40_000_000:
                        raise StorageError("ATTACHMENT_DIMENSIONS_INVALID")
                    if Image.MIME.get(image.format) != media_type:
                        raise StorageError("ATTACHMENT_CONTENT_INVALID")
                    image.verify()
            except (
                UnidentifiedImageError,
                OSError,
                SyntaxError,
                ValueError,
                Image.DecompressionBombError,
            ) as exc:
                raise StorageError("ATTACHMENT_CONTENT_INVALID") from exc
        attachment_id = _checked_id(attachment_id or _id("attachment"))
        digest = hashlib.sha256(data).hexdigest()
        final = self._object_path(digest)
        final.parent.mkdir(exist_ok=True)
        temporary = self.staging / f"{uuid4().hex}.partial"
        self._hook("attachment_before_write", attachment_id=attachment_id, content_hash=digest)
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._hook("attachment_after_fsync", attachment_id=attachment_id, content_hash=digest)
            if final.exists():
                if _file_hash(final) != digest:
                    raise StorageError("ATTACHMENT_INTEGRITY_ERROR")
                temporary.unlink()
            else:
                _atomic_publish(temporary, final)
            self._hook("attachment_after_publish", attachment_id=attachment_id, content_hash=digest)
            values = {
                "attachment_id": attachment_id,
                "content_hash": digest,
                "media_type": media_type,
                "byte_length": len(data),
                "confirmed": confirmed,
                "state": "staged",
                "created_at": _now(),
            }
            with self._lock, self.engine.begin() as conn:
                existing = (
                    conn.execute(
                        select(s.attachments).where(s.attachments.c.attachment_id == attachment_id)
                    )
                    .mappings()
                    .first()
                )
                if existing:
                    if existing["content_hash"] != digest or existing["confirmed"] != confirmed:
                        raise StorageError("ATTACHMENT_ID_CONFLICT")
                    return dict(existing)
                conn.execute(insert(s.attachments).values(**values))
            return values
        except OSError as exc:
            raise StorageError("ATTACHMENT_WRITE_FAILED") from exc
        finally:
            if temporary.exists():
                temporary.unlink()

    def resolve_attachment(self, attachment_id: str) -> dict[str, Any]:
        _checked_id(attachment_id)
        with self.engine.connect() as conn:
            return self._attachment(conn, attachment_id)

    def _attachment(self, conn: Connection, attachment_id: str) -> dict[str, Any]:
        row = _one(
            conn,
            s.attachments,
            s.attachments.c.attachment_id == attachment_id,
            "ATTACHMENT_NOT_FOUND",
        )
        path = self._object_path(row["content_hash"])
        if not path.is_file():
            raise StorageError("ATTACHMENT_MISSING")
        if path.stat().st_size != row["byte_length"] or _file_hash(path) != row["content_hash"]:
            raise StorageError("ATTACHMENT_INTEGRITY_ERROR")
        row["path"] = path
        return row

    def list_attachments(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(select(s.attachments)).mappings()]

    def attachment_inventory(self) -> dict[str, Any]:
        """Identify crash leftovers. Never automatically delete confirmed assets."""
        with self.engine.connect() as conn:
            referenced = set(conn.execute(select(s.message_attachments.c.attachment_id)).scalars())
            rows = [dict(row) for row in conn.execute(select(s.attachments)).mappings()]
        registered_hashes = {row["content_hash"] for row in rows}
        return {
            "unreferenced_attachment_ids": [
                row["attachment_id"] for row in rows if row["attachment_id"] not in referenced
            ],
            "unregistered_object_hashes": sorted(
                path.name
                for path in self.objects.glob("*/*")
                if path.is_file() and path.name not in registered_hashes
            ),
            "partial_file_count": len(list(self.staging.glob("*.partial"))),
            "automatic_deletions": 0,
        }

    def _validate_payload(
        self, conn: Connection, chat: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> None:
        if payload.get("read_course_ids"):
            from learning_app.workbench.source_grants import course_scope

            course_scope(conn, chat["course_id"], payload["read_course_ids"])
        for attachment_id in payload["attachment_ids"]:
            attachment = self._attachment(conn, attachment_id)
            if not attachment["confirmed"]:
                raise StorageError("ATTACHMENT_NOT_CONFIRMED")
        for locator in payload["source_locators"]:
            asset_id, version = locator.get("asset_id"), locator.get("source_version")
            if not asset_id or not version:
                raise StorageError("SOURCE_LOCATOR_INVALID")
            _one(
                conn,
                s.course_assets,
                (s.course_assets.c.course_id == (locator.get("source_course_id") or chat["course_id"]))
                & (s.course_assets.c.asset_id == asset_id),
                "SOURCE_NOT_IN_COURSE",
            )
            _one(
                conn,
                s.source_versions,
                (s.source_versions.c.asset_id == asset_id)
                & (s.source_versions.c.source_version == version),
                "SOURCE_VERSION_NOT_FOUND",
            )

    def _put_message(
        self,
        conn: Connection,
        *,
        message_id: str,
        chat_id: str,
        role: str,
        payload: dict[str, Any],
        status: str,
        origin: str,
        question_root_id: str,
        in_reply_to: str | None = None,
        generation_id: str | None = None,
        revision_id: str | None = None,
        parent_ids: Iterable[str] = (),
    ) -> None:
        revision_id = _checked_id(revision_id or _id("revision"))
        conn.execute(
            insert(s.messages).values(
                message_id=message_id,
                chat_id=chat_id,
                role=role,
                text=payload["text"],
                revision_id=revision_id,
                question_root_id=question_root_id,
                in_reply_to=in_reply_to,
                parent_ids=list(parent_ids),
                generation_id=generation_id,
                status=status,
                origin=origin,
                created_at=_now(),
            )
        )
        conn.execute(
            insert(s.input_revisions).values(
                revision_id=revision_id,
                message_id=message_id,
                supersedes=None,
                content_hash=_hash(payload),
                payload=payload,
                created_at=_now(),
            )
        )
        for attachment_id in payload["attachment_ids"]:
            conn.execute(
                insert(s.message_attachments).values(
                    revision_id=revision_id, attachment_id=attachment_id
                )
            )
            conn.execute(
                update(s.attachments)
                .where(s.attachments.c.attachment_id == attachment_id)
                .values(state="published")
            )

    @staticmethod
    def _payload(
        text: str,
        attachment_ids: Iterable[str] = (),
        conditions: Iterable[str] = (),
        corrections: Iterable[str] = (),
        source_locators: Iterable[dict[str, Any]] = (),
        read_course_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        result = {
            "text": text,
            "attachment_ids": list(dict.fromkeys(attachment_ids)),
            "conditions": list(conditions),
            "corrections": list(corrections),
            "source_locators": list(source_locators),
        }
        result["read_course_ids"] = list(read_course_ids)
        return result

    def _messages(self, conn: Connection, chat_id: str) -> list[dict[str, Any]]:
        result = []
        for row in conn.execute(
            select(s.messages)
            .where(s.messages.c.chat_id == chat_id)
            .order_by(s.messages.c.sequence)
        ).mappings():
            message = dict(row)
            revision = _one(
                conn,
                s.input_revisions,
                s.input_revisions.c.revision_id == row["revision_id"],
                "REVISION_NOT_FOUND",
            )
            message.update(revision["payload"])
            # Generation text is maintained transactionally; a streamed answer
            # need not rewrite the immutable initial input-revision payload.
            message["text"] = row["text"]
            result.append(message)
        return result

    def get_chat(self, chat_id: str) -> dict[str, Any]:
        with self.engine.connect() as conn:
            chat = _one(conn, s.chats, s.chats.c.chat_id == chat_id, "CHAT_NOT_FOUND")
            chat["messages"] = self._messages(conn, chat_id)
            ids = (
                conn.execute(
                    select(s.generations.c.generation_id)
                    .join(
                        s.submissions,
                        s.generations.c.submission_id == s.submissions.c.submission_id,
                    )
                    .where(s.submissions.c.chat_id == chat_id)
                )
                .scalars()
                .all()
            )
        chat["generations"] = [self.get_generation(generation_id) for generation_id in ids]
        return chat

    def _snapshot(
        self, conn: Connection, chat_id: str, exclude_message_id: str | None = None
    ) -> dict[str, Any]:
        chat = _one(conn, s.chats, s.chats.c.chat_id == chat_id, "CHAT_NOT_FOUND")
        messages = [
            m
            for m in self._messages(conn, chat_id)
            if m["message_id"] != exclude_message_id
            and (m["role"] == "user" or m["status"] in TERMINAL_STATES)
        ]
        required = list(dict.fromkeys(a for m in messages for a in m["attachment_ids"]))
        for attachment_id in required:
            self._attachment(conn, attachment_id)
        protected = [m for m in messages if m["question_root_id"] == chat["current_question_id"]]
        payload = {
            "contract_version": "context/1",
            "chat_id": chat_id,
            "history_revision": chat["history_revision"],
            "policy_version": "p0-lossless-1",
            "messages": messages,
            "required_attachment_ids": required,
            "protected_message_ids": [m["message_id"] for m in protected],
            "protected_revision_ids": [m["revision_id"] for m in protected],
            "conditions": list(dict.fromkeys(c for m in protected for c in m["conditions"])),
            "corrections": list(dict.fromkeys(c for m in protected for c in m["corrections"])),
            "frontier": [messages[-1]["message_id"]] if messages else [],
        }
        digest = _hash(payload)
        snapshot_id = _id("snapshot")
        conn.execute(
            insert(s.context_snapshots).values(
                snapshot_id=snapshot_id,
                chat_id=chat_id,
                context_hash=digest,
                payload=payload,
                created_at=_now(),
            )
        )
        return {"snapshot_id": snapshot_id, "context_hash": digest, **payload}

    def build_context(self, chat_id: str, exclude_message_id: str | None = None) -> dict[str, Any]:
        with self._lock, self.engine.begin() as conn:
            return self._snapshot(conn, chat_id, exclude_message_id)

    def publish_submission(
        self,
        chat_id: str,
        text: str,
        submission_id: str,
        *,
        attachment_ids: Iterable[str] = (),
        conditions: Iterable[str] = (),
        corrections: Iterable[str] = (),
        source_locators: Iterable[dict[str, Any]] = (),
        read_course_ids: Iterable[str] = (),
        model: str = "deepseek-flash",
        thinking: str = "high",
        max_output_tokens: int = 8192,
        timeout_seconds: float = 180,
        question_root_id: str | None = None,
        draft_content_hash: str | None = None,
    ) -> dict[str, Any]:
        _checked_id(submission_id)
        if max_output_tokens < 1 or timeout_seconds <= 0:
            raise StorageError("MODEL_LIMITS_INVALID")
        payload = self._payload(
            text, attachment_ids, conditions, corrections, source_locators, read_course_ids
        )
        if draft_content_hash is not None:
            if not re.fullmatch(r"[a-f0-9]{64}", draft_content_hash):
                raise StorageError("DRAFT_HASH_INVALID")
            payload["draft_content_hash"] = draft_content_hash
        payload_hash = _hash(
            {
                "chat_id": chat_id,
                **payload,
                "model": model,
                "thinking": thinking,
                "max_output_tokens": max_output_tokens,
                "timeout_seconds": timeout_seconds,
                "question_root_id": question_root_id,
            }
        )
        with self._lock, self.engine.begin() as conn:
            existing = (
                conn.execute(
                    select(s.submissions).where(s.submissions.c.submission_id == submission_id)
                )
                .mappings()
                .first()
            )
            if existing:
                if existing["payload_hash"] != payload_hash:
                    raise StorageError("SUBMISSION_CONFLICT")
                generation = _one(
                    conn,
                    s.generations,
                    s.generations.c.submission_id == submission_id,
                    "GENERATION_NOT_FOUND",
                )
                return self._submission_result(existing, generation, False)
            chat = _one(conn, s.chats, s.chats.c.chat_id == chat_id, "CHAT_NOT_FOUND")
            active = conn.execute(
                select(s.generations.c.generation_id)
                .join(s.submissions, s.generations.c.submission_id == s.submissions.c.submission_id)
                .where(
                    s.submissions.c.chat_id == chat_id,
                    s.generations.c.state.in_({"pending", *ACTIVE_STATES}),
                )
            ).first()
            if active:
                raise StorageError("CHAT_EXECUTION_ACTIVE")
            self._validate_payload(conn, chat, payload)
            previous_message_id = conn.execute(
                select(s.messages.c.message_id)
                .where(s.messages.c.chat_id == chat_id)
                .order_by(s.messages.c.sequence.desc())
                .limit(1)
            ).scalar_one_or_none()
            user_id, assistant_id, generation_id = _id("message"), _id("message"), _id("generation")
            root_id = question_root_id or chat["current_question_id"] or user_id
            if question_root_id:
                _one(
                    conn,
                    s.messages,
                    (s.messages.c.message_id == question_root_id)
                    & (s.messages.c.chat_id == chat_id),
                    "QUESTION_ROOT_NOT_FOUND",
                )
            self._put_message(
                conn,
                message_id=user_id,
                chat_id=chat_id,
                role="user",
                payload=payload,
                status="completed",
                origin="local",
                question_root_id=root_id,
                parent_ids=[previous_message_id] if previous_message_id else [],
            )
            conn.execute(
                update(s.chats)
                .where(s.chats.c.chat_id == chat_id)
                .values(current_question_id=root_id, history_revision=chat["history_revision"] + 1)
            )
            snapshot = self._snapshot(conn, chat_id)
            self._put_message(
                conn,
                message_id=assistant_id,
                chat_id=chat_id,
                role="assistant",
                payload=self._payload(""),
                status="pending",
                origin="local",
                question_root_id=root_id,
                in_reply_to=user_id,
                generation_id=generation_id,
                parent_ids=[user_id],
            )
            submission = {
                "submission_id": submission_id,
                "chat_id": chat_id,
                "user_message_id": user_id,
                "payload_hash": payload_hash,
                "state": "pending",
                "created_at": _now(),
            }
            conn.execute(insert(s.submissions).values(**submission))
            generation = {
                "generation_id": generation_id,
                "submission_id": submission_id,
                "assistant_message_id": assistant_id,
                "snapshot_id": snapshot["snapshot_id"],
                "state": "pending",
                "body": "",
                "reasoning": "",
                "cancel_requested": False,
                "adapter_metadata": {},
                "model": model,
                "thinking": thinking,
                "max_output_tokens": max_output_tokens,
                "timeout_seconds": timeout_seconds,
                "created_at": _now(),
                "updated_at": _now(),
            }
            conn.execute(insert(s.generations).values(**generation))
            self._hook(
                "submission_before_commit", submission_id=submission_id, generation_id=generation_id
            )
        self._hook(
            "submission_after_commit", submission_id=submission_id, generation_id=generation_id
        )
        return self._submission_result(submission, generation, True)

    @staticmethod
    def _submission_result(
        submission: Mapping[str, Any], generation: Mapping[str, Any], created: bool
    ) -> dict[str, Any]:
        return {
            "submission_id": submission["submission_id"],
            "generation_id": generation["generation_id"],
            "user_message_id": submission["user_message_id"],
            "assistant_message_id": generation["assistant_message_id"],
            "state": generation["state"],
            "created": created,
        }

    def get_generation(self, generation_id: str) -> dict[str, Any]:
        with self.engine.connect() as conn:
            generation = _one(
                conn,
                s.generations,
                s.generations.c.generation_id == generation_id,
                "GENERATION_NOT_FOUND",
            )
            submission = _one(
                conn,
                s.submissions,
                s.submissions.c.submission_id == generation["submission_id"],
                "SUBMISSION_NOT_FOUND",
            )
            snapshot = _one(
                conn,
                s.context_snapshots,
                s.context_snapshots.c.snapshot_id == generation["snapshot_id"],
                "SNAPSHOT_NOT_FOUND",
            )
            generation["chat_id"] = submission["chat_id"]
            generation["user_message_id"] = submission["user_message_id"]
            generation["text"] = _one(
                conn,
                s.messages,
                s.messages.c.message_id == submission["user_message_id"],
                "MESSAGE_NOT_FOUND",
            )["text"]
            generation["context_snapshot"] = {
                "snapshot_id": snapshot["snapshot_id"],
                "context_hash": snapshot["context_hash"],
                **snapshot["payload"],
            }
            generation["usage"] = [
                dict(row)
                for row in conn.execute(
                    select(s.usage_records).where(s.usage_records.c.generation_id == generation_id)
                ).mappings()
            ]
            generation["outbound_attempt_count"] = conn.execute(
                select(func.count())
                .select_from(s.outbound_attempts)
                .where(s.outbound_attempts.c.generation_id == generation_id)
            ).scalar_one()
            generation["real_outbound_attempt_count"] = conn.execute(
                select(func.count())
                .select_from(s.outbound_attempts)
                .where(
                    s.outbound_attempts.c.generation_id == generation_id,
                    s.outbound_attempts.c.actual_outbound.is_(True),
                )
            ).scalar_one()
            generation["event_watermark"] = (
                conn.execute(
                    select(func.max(s.execution_events.c.seq)).where(
                        s.execution_events.c.generation_id == generation_id
                    )
                ).scalar_one()
                or 0
            )
            return generation

    def get_submission(self, submission_id: str) -> dict[str, Any]:
        with self.engine.connect() as conn:
            submission = _one(
                conn,
                s.submissions,
                s.submissions.c.submission_id == submission_id,
                "SUBMISSION_NOT_FOUND",
            )
            generation = _one(
                conn,
                s.generations,
                s.generations.c.submission_id == submission_id,
                "GENERATION_NOT_FOUND",
            )
            return self._submission_result(submission, generation, False)

    def execution_context(self, generation_id: str) -> dict[str, Any]:
        """Private adapter input, frozen when this submission was committed."""
        from learning_app.drafts import REFERENCE_CONDITION_PREFIX

        generation = self.get_generation(generation_id)
        snapshot = generation["context_snapshot"]
        current = next(
            (
                message
                for message in snapshot["messages"]
                if message["message_id"] == generation["user_message_id"]
            ),
            None,
        )
        if current is None:
            raise StorageError("CURRENT_INPUT_NOT_IN_SNAPSHOT")

        def message_text(message):
            # Old condition/correction assets are kept losslessly, but no
            # longer acquire a separate priority channel in new requests.
            ordinary = [
                value
                for value in message.get("conditions", ())
                if not value.startswith(REFERENCE_CONDITION_PREFIX)
            ]
            ordinary.extend(message.get("corrections", ()))
            text = message["text"]
            if ordinary:
                text += "\n\n" + "\n".join(ordinary)
            references = [
                value
                for value in message.get("conditions", ())
                if value.startswith(REFERENCE_CONDITION_PREFIX)
            ]
            if references:
                text += "\n\n用户选取的引用：\n" + "\n".join(references)
            return text

        prompt = message_text(current)
        history = [
            {**message, "text": message_text(message), "conditions": [], "corrections": []}
            for message in snapshot["messages"]
            if message["message_id"] != generation["user_message_id"]
        ]
        return {
            "snapshot_id": snapshot["snapshot_id"],
            "context_hash": snapshot["context_hash"],
            "prompt": prompt,
            "history_context": {
                "notice": "以下为已经完成的历史问答，仅供本次新问题参考；不要重新执行旧问题。",
                "contract_version": "history/1",
                "messages": history,
            },
            "attachments": [
                self.resolve_attachment(attachment_id)
                for attachment_id in snapshot["required_attachment_ids"]
            ],
        }

    def bind_execution(
        self,
        chat_id: str,
        session_id: str,
        *,
        binding_id: str | None = None,
        adapter_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock, self.engine.begin() as conn:
            _one(conn, s.chats, s.chats.c.chat_id == chat_id, "CHAT_NOT_FOUND")
            current = (
                conn.execute(
                    select(s.execution_bindings)
                    .where(s.execution_bindings.c.chat_id == chat_id)
                    .order_by(s.execution_bindings.c.epoch.desc())
                )
                .mappings()
                .first()
            )
            if current and current["session_id"] == session_id:
                if current["state"] in {"retired", "stale"}:
                    raise StorageError("BINDING_STALE")
                if adapter_metadata is not None:
                    conn.execute(
                        update(s.execution_bindings)
                        .where(s.execution_bindings.c.binding_id == current["binding_id"])
                        .values(adapter_metadata=adapter_metadata)
                    )
                return {
                    **dict(current),
                    "adapter_metadata": adapter_metadata
                    if adapter_metadata is not None
                    else current["adapter_metadata"],
                }
            if current and current["active_generation_id"]:
                raise StorageError("CHAT_EXECUTION_ACTIVE")
            epoch = int(current["epoch"]) + 1 if current else 1
            if current:
                conn.execute(
                    update(s.execution_bindings)
                    .where(s.execution_bindings.c.binding_id == current["binding_id"])
                    .values(state="retired")
                )
            values = {
                "binding_id": _checked_id(binding_id or _id("binding")),
                "chat_id": chat_id,
                "session_id": session_id,
                "epoch": epoch,
                "state": "ready",
                "active_generation_id": None,
                "last_event_watermark": 0,
                "adapter_metadata": adapter_metadata or {},
                "created_at": _now(),
            }
            conn.execute(insert(s.execution_bindings).values(**values))
            return values

    def get_binding(self, binding_id: str) -> dict[str, Any]:
        with self.engine.connect() as conn:
            return _one(
                conn,
                s.execution_bindings,
                s.execution_bindings.c.binding_id == binding_id,
                "BINDING_NOT_FOUND",
            )

    def current_binding(self, chat_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(s.execution_bindings)
                    .where(s.execution_bindings.c.chat_id == chat_id)
                    .order_by(s.execution_bindings.c.epoch.desc())
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def chat_for_session(self, session_id: str) -> str:
        """Resolve native desktop identity without promoting it to business identity."""
        with self.engine.connect() as conn:
            row = conn.execute(
                select(s.execution_bindings.c.chat_id)
                .where(
                    s.execution_bindings.c.session_id == session_id,
                    s.execution_bindings.c.state.not_in(["stale", "retired"]),
                )
                .order_by(s.execution_bindings.c.epoch.desc())
            ).first()
            if row is None:
                raise StorageError("CHAT_NOT_FOUND")
            return row[0]

    def desktop_creation_for_seed(self, session_id: str) -> dict[str, Any] | None:
        """Durable receipt for a named native Session creation replay."""
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(s.execution_bindings)
                    .where(
                        s.execution_bindings.c.adapter_metadata["desktop_create_seed"].as_string()
                        == session_id,
                        s.execution_bindings.c.state.not_in(["stale", "retired"]),
                    )
                    .order_by(s.execution_bindings.c.epoch.desc())
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def update_binding_metadata(self, binding_id: str, metadata: dict[str, Any]) -> None:
        with self._lock, self.engine.begin() as conn:
            _one(
                conn,
                s.execution_bindings,
                s.execution_bindings.c.binding_id == binding_id,
                "BINDING_NOT_FOUND",
            )
            conn.execute(
                update(s.execution_bindings)
                .where(s.execution_bindings.c.binding_id == binding_id)
                .values(adapter_metadata=metadata)
            )

    def save_adapter_metadata(self, generation_id: str, metadata: dict[str, Any]) -> None:
        with self._lock, self.engine.begin() as conn:
            generation = _one(
                conn,
                s.generations,
                s.generations.c.generation_id == generation_id,
                "GENERATION_NOT_FOUND",
            )
            if metadata.get("generation_id", generation_id) != generation_id:
                raise StorageError("ADAPTER_GENERATION_MISMATCH")
            if (
                generation["epoch"] is not None
                and metadata.get("epoch", generation["epoch"]) != generation["epoch"]
            ):
                raise StorageError("ADAPTER_EPOCH_MISMATCH")
            conn.execute(
                update(s.generations)
                .where(s.generations.c.generation_id == generation_id)
                .values(adapter_metadata=metadata)
            )
            if generation["binding_id"]:
                binding = _one(
                    conn,
                    s.execution_bindings,
                    s.execution_bindings.c.binding_id == generation["binding_id"],
                    "BINDING_NOT_FOUND",
                )
                if metadata.get("session_id", binding["session_id"]) != binding["session_id"]:
                    raise StorageError("ADAPTER_SESSION_MISMATCH")
                if binding["active_generation_id"] == generation_id:
                    conn.execute(
                        update(s.execution_bindings)
                        .where(s.execution_bindings.c.binding_id == generation["binding_id"])
                        .values(adapter_metadata={**binding["adapter_metadata"], **metadata})
                    )

    def get_execution_for_generation(self, generation_id: str) -> dict[str, Any] | None:
        generation = self.get_generation(generation_id)
        if not generation["binding_id"]:
            return None
        return {
            **self.get_binding(generation["binding_id"]),
            "adapter_metadata": generation["adapter_metadata"],
        }

    def claim_dispatch(
        self,
        generation_id: str,
        binding_id: str,
        epoch: int,
        *,
        request_id: str | None = None,
        host_instance_id: str | None = None,
        adapter_metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Claim before IO. A crashed claim is uncertain, never reset to pending."""
        with self._lock, self.engine.begin() as conn:
            generation = _one(
                conn,
                s.generations,
                s.generations.c.generation_id == generation_id,
                "GENERATION_NOT_FOUND",
            )
            if generation["state"] != "pending" or generation["cancel_requested"]:
                return False
            submission = _one(
                conn,
                s.submissions,
                s.submissions.c.submission_id == generation["submission_id"],
                "SUBMISSION_NOT_FOUND",
            )
            binding = _one(
                conn,
                s.execution_bindings,
                s.execution_bindings.c.binding_id == binding_id,
                "BINDING_NOT_FOUND",
            )
            if binding["chat_id"] != submission["chat_id"] or binding["epoch"] != epoch:
                raise StorageError("BINDING_OWNERSHIP_MISMATCH")
            if binding["state"] in {"retired", "stale"}:
                raise StorageError("BINDING_STALE")
            # Conditional UPDATE performs the serialization, including across
            # independent Repository objects/processes; a Python lock alone
            # would not protect a restarted instance.
            claimed = conn.execute(
                update(s.execution_bindings)
                .where(
                    s.execution_bindings.c.binding_id == binding_id,
                    s.execution_bindings.c.active_generation_id.is_(None),
                )
                .values(active_generation_id=generation_id, state="running")
            )
            if claimed.rowcount != 1:
                raise StorageError("CHAT_EXECUTION_ACTIVE")
            changed = conn.execute(
                update(s.generations)
                .where(
                    s.generations.c.generation_id == generation_id,
                    s.generations.c.state == "pending",
                    s.generations.c.cancel_requested.is_(False),
                )
                .values(
                    state="dispatching",
                    binding_id=binding_id,
                    epoch=epoch,
                    request_id=request_id,
                    host_instance_id=host_instance_id,
                    adapter_metadata=adapter_metadata or {},
                    updated_at=_now(),
                )
            )
            if changed.rowcount != 1:
                raise StorageError("DISPATCH_CLAIM_CONFLICT")
            conn.execute(
                update(s.submissions)
                .where(s.submissions.c.submission_id == generation["submission_id"])
                .values(state="dispatching")
            )
        self._hook(
            "dispatch_claimed", generation_id=generation_id, binding_id=binding_id, epoch=epoch
        )
        return True

    def mark_dispatch_started(
        self, generation_id: str, binding_id: str, epoch: int, **kwargs: Any
    ) -> bool:
        return self.claim_dispatch(generation_id, binding_id, epoch, **kwargs)

    def record_receipt(self, generation_id: str, harness_message_id: str) -> None:
        with self._lock, self.engine.begin() as conn:
            generation = _one(
                conn,
                s.generations,
                s.generations.c.generation_id == generation_id,
                "GENERATION_NOT_FOUND",
            )
            if generation["harness_message_id"] not in {None, harness_message_id}:
                raise StorageError("RECEIPT_IDENTITY_CONFLICT")
            values: dict[str, Any] = {
                "harness_message_id": harness_message_id,
                "updated_at": _now(),
            }
            if generation["state"] in {"dispatching", "unknown"}:
                values["state"] = "accepted"
            conn.execute(
                update(s.generations)
                .where(s.generations.c.generation_id == generation_id)
                .values(**values)
            )

    def record_outbound_attempt(
        self,
        generation_id: str,
        attempt_id: str,
        *,
        kind: str = "answer",
        request_id: str | None = None,
        model: str | None = None,
        thinking: str | None = None,
        actual_outbound: bool = True,
    ) -> bool:
        """Only call from evidence at the actual provider outbound edge."""
        with self._lock, self.engine.begin() as conn:
            generation = _one(
                conn,
                s.generations,
                s.generations.c.generation_id == generation_id,
                "GENERATION_NOT_FOUND",
            )
            existing = (
                conn.execute(
                    select(s.outbound_attempts).where(
                        s.outbound_attempts.c.attempt_id == attempt_id
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                if existing["generation_id"] != generation_id:
                    raise StorageError("ATTEMPT_IDENTITY_CONFLICT")
                # The boundary may first expose 'received', then cross the
                # provider send edge. Upgrade that same fact without creating
                # a second attempt; an observed send can never become unsent.
                if actual_outbound and not existing["actual_outbound"]:
                    conn.execute(
                        update(s.outbound_attempts)
                        .where(s.outbound_attempts.c.attempt_id == attempt_id)
                        .values(actual_outbound=True)
                    )
                return False
            conn.execute(
                insert(s.outbound_attempts).values(
                    attempt_id=attempt_id,
                    generation_id=generation_id,
                    kind=kind,
                    request_id=request_id,
                    model=model or generation["model"],
                    thinking=thinking or generation["thinking"],
                    actual_outbound=actual_outbound,
                    created_at=_now(),
                )
            )
            return True

    def _usage(
        self,
        conn: Connection,
        generation_id: str,
        settlement_id: str,
        raw_usage: dict[str, Any] | None,
        source: str,
        fields: Mapping[str, bool] | None,
    ) -> bool:
        existing = (
            conn.execute(
                select(s.usage_records).where(
                    s.usage_records.c.generation_id == generation_id,
                    s.usage_records.c.settlement_id == settlement_id,
                )
            )
            .mappings()
            .first()
        )
        raw = raw_usage
        presence, normalized = usage_projection(raw, fields)
        if existing:
            if existing["source"] != source:
                raise StorageError("USAGE_SETTLEMENT_CONFLICT")
            if existing["raw_usage"] == raw and existing["field_presence"] == presence:
                return False

            if usage_is_settled(existing["raw_usage"]):
                if usage_is_settled(raw):
                    raise StorageError("USAGE_SETTLEMENT_CONFLICT")
                # An earlier, unsettled observation may arrive after the final
                # record. Its immutable execution_event is still retained.
                return False
            conn.execute(
                update(s.usage_records)
                .where(
                    s.usage_records.c.generation_id == generation_id,
                    s.usage_records.c.settlement_id == settlement_id,
                )
                .values(raw_usage=raw, field_presence=presence, normalized=normalized)
            )
            return False
        conn.execute(
            insert(s.usage_records).values(
                generation_id=generation_id,
                settlement_id=settlement_id,
                source=source,
                raw_usage=raw,
                field_presence=presence,
                normalized=normalized,
                created_at=_now(),
            )
        )
        return True

    def ingest_event(
        self,
        generation_id: str,
        *,
        binding_id: str,
        epoch: int,
        event_id: str,
        seq: int,
        kind: str,
        text: str = "",
        status: str | None = None,
        raw_usage: dict[str, Any] | None = None,
        usage_source: str | None = None,
        usage_fields: Mapping[str, bool] | None = None,
        settlement_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.ingest_events(
            [
                {
                    "generation_id": generation_id,
                    "binding_id": binding_id,
                    "epoch": epoch,
                    "event_id": event_id,
                    "seq": seq,
                    "kind": kind,
                    "text": text,
                    "status": status,
                    "raw_usage": raw_usage,
                    "usage_source": usage_source,
                    "usage_fields": usage_fields,
                    "settlement_id": settlement_id,
                    "data": data,
                }
            ]
        )[0]

    def ingest_events(self, events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Atomically persist a bounded stream burst, retaining every event identity.

        Before-commit fault hooks remain inside the shared transaction. Each
        accepted event's after-commit hook runs only after the whole batch is
        durable; a crash before commit replays the original events by identity.
        """
        items = list(events)
        if not items:
            return []
        if len(items) > 128:
            raise StorageError("EVENT_BATCH_TOO_LARGE")
        with self._lock, self.engine.begin() as conn:
            results = [self._ingest_event(conn, **item) for item in items]
        aliases = {"text_delta": "body", "reasoning_delta": "reasoning", "activity": "running"}
        for item, result in zip(items, results, strict=True):
            if result["accepted"]:
                self._hook(
                    "event_after_commit",
                    generation_id=item["generation_id"],
                    event_id=item["event_id"],
                    kind=aliases.get(item["kind"], item["kind"]),
                )
        return results

    def _ingest_event(
        self,
        conn: Connection,
        generation_id: str,
        *,
        binding_id: str,
        epoch: int,
        event_id: str,
        seq: int,
        kind: str,
        text: str = "",
        status: str | None = None,
        raw_usage: dict[str, Any] | None = None,
        usage_source: str | None = None,
        usage_fields: Mapping[str, bool] | None = None,
        settlement_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        aliases = {"text_delta": "body", "reasoning_delta": "reasoning", "activity": "running"}
        kind = aliases.get(kind, kind)
        if (
            kind
            not in {
                "body",
                "reasoning",
                "terminal",
                "snapshot",
                "accepted",
                "running",
                "receipt",
                "usage",
                "unknown",
            }
            or seq < 0
        ):
            raise StorageError("EVENT_INVALID")
        payload = {
            "text": text,
            "status": status,
            "data": data or {},
            "raw_usage": raw_usage,
            "usage_fields": dict(usage_fields or {}),
            "usage_source": usage_source,
            "settlement_id": settlement_id,
        }
        with self._lock:
            generation = _one(
                conn,
                s.generations,
                s.generations.c.generation_id == generation_id,
                "GENERATION_NOT_FOUND",
            )
            binding = _one(
                conn,
                s.execution_bindings,
                s.execution_bindings.c.binding_id == binding_id,
                "BINDING_NOT_FOUND",
            )
            existing = (
                conn.execute(
                    select(s.execution_events).where(
                        s.execution_events.c.binding_id == binding_id,
                        s.execution_events.c.event_id == event_id,
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                if (
                    existing["generation_id"] != generation_id
                    or existing["epoch"] != epoch
                    or existing["kind"] != kind
                    or existing["payload"] != payload
                    or existing["event_id"] != event_id
                ):
                    raise StorageError("EVENT_IDENTITY_CONFLICT")
                return {
                    "accepted": False,
                    "duplicate": True,
                    "stale": False,
                    "state": generation["state"],
                    "watermark": binding["last_event_watermark"],
                }
            latest_epoch = conn.execute(
                select(func.max(s.execution_bindings.c.epoch)).where(
                    s.execution_bindings.c.chat_id == binding["chat_id"]
                )
            ).scalar_one()
            if (
                binding["epoch"] != epoch
                or generation["epoch"] != epoch
                or generation["binding_id"] != binding_id
                or epoch != latest_epoch
                or binding["state"] == "retired"
            ):
                conn.execute(
                    insert(s.rejected_events).values(
                        generation_id=generation_id,
                        binding_id=binding_id,
                        epoch=epoch,
                        event_id=event_id,
                        reason="stale_epoch_or_binding",
                        created_at=_now(),
                    )
                )
                return {
                    "accepted": False,
                    "duplicate": False,
                    "stale": True,
                    "state": generation["state"],
                    "watermark": binding["last_event_watermark"],
                }
            # A new generation on the same session has the same epoch. Reject
            # old non-idempotent callbacks after that generation was settled.
            if generation["state"] in TERMINAL_STATES and kind not in {
                "usage",
                "terminal",
                "snapshot",
            }:
                conn.execute(
                    insert(s.rejected_events).values(
                        generation_id=generation_id,
                        binding_id=binding_id,
                        epoch=epoch,
                        event_id=event_id,
                        reason="generation_already_terminal",
                        created_at=_now(),
                    )
                )
                return {
                    "accepted": False,
                    "duplicate": False,
                    "stale": True,
                    "state": generation["state"],
                    "watermark": binding["last_event_watermark"],
                }
            values: dict[str, Any] = {"updated_at": _now()}
            if kind == "body":
                values.update(body=generation["body"] + text, state="running")
            elif kind == "reasoning":
                values.update(reasoning=generation["reasoning"] + text, state="running")
            elif kind == "snapshot":
                native_seq = (data or {}).get("native_seq")
                if not isinstance(native_seq, int) or native_seq >= binding["last_event_watermark"]:
                    body, reasoning = (data or {}).get("body", text), (data or {}).get("reasoning")
                    if body is not None:
                        values["body"] = str(body)
                    if reasoning is not None:
                        values["reasoning"] = str(reasoning)
                    if generation["state"] not in TERMINAL_STATES:
                        values["state"] = "running"
            elif kind in {"accepted", "running", "receipt"}:
                if generation["state"] not in TERMINAL_STATES:
                    values["state"] = "running" if kind == "running" else "accepted"
                message_id = (data or {}).get("harness_message_id")
                if message_id:
                    if generation["harness_message_id"] not in {None, message_id}:
                        raise StorageError("RECEIPT_IDENTITY_CONFLICT")
                    values["harness_message_id"] = message_id
            elif kind == "terminal":
                if status not in TERMINAL_STATES:
                    raise StorageError("TERMINAL_STATUS_INVALID")
                if generation["state"] in TERMINAL_STATES and status != generation["state"]:
                    raise StorageError("TERMINAL_STATUS_CONFLICT")
                values["state"] = status
                self._hook(
                    "terminal_before_commit",
                    generation_id=generation_id,
                    binding_id=binding_id,
                    event_id=event_id,
                )
            elif kind == "unknown":
                if generation["state"] not in TERMINAL_STATES:
                    values.update(state="unknown", unknown_reason=(data or {}).get("reason", text))
            conn.execute(
                insert(s.execution_events).values(
                    binding_id=binding_id,
                    event_id=event_id,
                    seq=seq,
                    generation_id=generation_id,
                    epoch=epoch,
                    kind=kind,
                    payload=payload,
                    created_at=_now(),
                )
            )
            conn.execute(
                update(s.generations)
                .where(s.generations.c.generation_id == generation_id)
                .values(**values)
            )
            state = values.get("state", generation["state"])
            body = values.get("body", generation["body"])
            conn.execute(
                update(s.messages)
                .where(s.messages.c.message_id == generation["assistant_message_id"])
                .values(text=body, status=state)
            )
            conn.execute(
                update(s.submissions)
                .where(s.submissions.c.submission_id == generation["submission_id"])
                .values(state=state)
            )
            binding_values: dict[str, Any] = {
                "last_event_watermark": max(
                    binding["last_event_watermark"], (data or {}).get("native_seq") or 0
                )
            }
            if state in TERMINAL_STATES and binding["active_generation_id"] == generation_id:
                binding_values.update(
                    active_generation_id=None,
                    state="stale" if binding["state"] == "stale" else "ready",
                )
            conn.execute(
                update(s.execution_bindings)
                .where(s.execution_bindings.c.binding_id == binding_id)
                .values(**binding_values)
            )
            if kind == "usage" or raw_usage is not None or usage_fields is not None:
                self._usage(
                    conn,
                    generation_id,
                    settlement_id or event_id,
                    raw_usage,
                    usage_source or "unavailable",
                    usage_fields,
                )
            self._hook(
                "event_before_commit",
                generation_id=generation_id,
                binding_id=binding_id,
                event_id=event_id,
                kind=kind,
            )
        return {
            "accepted": True,
            "duplicate": False,
            "stale": False,
            "state": state,
            "watermark": binding_values["last_event_watermark"],
        }

    def mark_unknown(self, generation_id: str, reason: str) -> None:
        with self._lock, self.engine.begin() as conn:
            generation = _one(
                conn,
                s.generations,
                s.generations.c.generation_id == generation_id,
                "GENERATION_NOT_FOUND",
            )
            if generation["state"] in TERMINAL_STATES:
                return
            conn.execute(
                update(s.generations)
                .where(s.generations.c.generation_id == generation_id)
                .values(state="unknown", unknown_reason=reason, updated_at=_now())
            )
            conn.execute(
                update(s.messages)
                .where(s.messages.c.message_id == generation["assistant_message_id"])
                .values(status="unknown")
            )
            conn.execute(
                update(s.submissions)
                .where(s.submissions.c.submission_id == generation["submission_id"])
                .values(state="unknown")
            )

    def request_cancel(self, generation_id: str) -> None:
        with self._lock, self.engine.begin() as conn:
            generation = _one(
                conn,
                s.generations,
                s.generations.c.generation_id == generation_id,
                "GENERATION_NOT_FOUND",
            )
            if generation["state"] not in TERMINAL_STATES:
                conn.execute(
                    update(s.generations)
                    .where(s.generations.c.generation_id == generation_id)
                    .values(cancel_requested=True, updated_at=_now())
                )
                if generation["state"] == "pending":
                    conn.execute(
                        update(s.generations)
                        .where(s.generations.c.generation_id == generation_id)
                        .values(state="cancelled")
                    )
                    conn.execute(
                        update(s.submissions)
                        .where(s.submissions.c.submission_id == generation["submission_id"])
                        .values(state="cancelled")
                    )
                    conn.execute(
                        update(s.messages)
                        .where(s.messages.c.message_id == generation["assistant_message_id"])
                        .values(status="cancelled")
                    )

    def fail_pending(self, generation_id: str, code: str) -> bool:
        """An independently known pre-dispatch failure cannot imply a paid call."""
        with self._lock, self.engine.begin() as conn:
            generation = _one(
                conn,
                s.generations,
                s.generations.c.generation_id == generation_id,
                "GENERATION_NOT_FOUND",
            )
            if generation["state"] != "pending":
                return False
            conn.execute(
                update(s.generations)
                .where(s.generations.c.generation_id == generation_id)
                .values(state="failed", unknown_reason=code, updated_at=_now())
            )
            conn.execute(
                update(s.submissions)
                .where(s.submissions.c.submission_id == generation["submission_id"])
                .values(state="failed")
            )
            conn.execute(
                update(s.messages)
                .where(s.messages.c.message_id == generation["assistant_message_id"])
                .values(status="failed")
            )
            return True

    def recoverable_generations(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            ids = (
                conn.execute(
                    select(s.generations.c.generation_id).where(
                        or_(
                            s.generations.c.state.in_({"pending", *ACTIVE_STATES}),
                            and_(
                                s.generations.c.state.in_(TERMINAL_STATES),
                                s.generations.c.binding_id.is_not(None),
                                ~exists(
                                    select(s.usage_records.c.generation_id).where(
                                        s.usage_records.c.generation_id
                                        == s.generations.c.generation_id
                                    )
                                ),
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [self.get_generation(generation_id) for generation_id in ids]

    def list_recoverable_generations(self) -> list[dict[str, Any]]:
        return self.recoverable_generations()

    def merge_completed_history(
        self, chat_id: str, messages: list[dict[str, Any]]
    ) -> dict[str, int]:
        """Business-only import. No pending submission or provider event is made."""
        inserted = duplicates = 0
        with self._lock, self.engine.begin() as conn:
            chat = _one(conn, s.chats, s.chats.c.chat_id == chat_id, "CHAT_NOT_FOUND")
            root_id = chat["current_question_id"]
            for message in messages:
                message_id = _checked_id(message["message_id"])
                if message["role"] not in {"user", "assistant"}:
                    raise StorageError("HISTORY_ROLE_INVALID")
                if message.get("status", "completed") != "completed":
                    raise StorageError("HISTORY_NOT_COMPLETED")
                payload = self._payload(
                    message["text"],
                    message.get("attachment_ids", ()),
                    message.get("conditions", ()),
                    message.get("corrections", ()),
                    message.get("source_locators", ()),
                )
                self._validate_payload(conn, chat, payload)
                existing = (
                    conn.execute(select(s.messages).where(s.messages.c.message_id == message_id))
                    .mappings()
                    .first()
                )
                if existing:
                    revision = _one(
                        conn,
                        s.input_revisions,
                        s.input_revisions.c.revision_id == existing["revision_id"],
                        "REVISION_NOT_FOUND",
                    )
                    if (
                        existing["chat_id"] != chat_id
                        or existing["role"] != message["role"]
                        or revision["content_hash"] != _hash(payload)
                        or existing["in_reply_to"] != message.get("in_reply_to")
                        or existing["parent_ids"] != message.get("parent_ids", [])
                        or (
                            message.get("revision_id") is not None
                            and existing["revision_id"] != message["revision_id"]
                        )
                    ):
                        raise StorageError("HISTORY_MESSAGE_CONFLICT")
                    duplicates += 1
                    continue
                if message.get("in_reply_to"):
                    _one(
                        conn,
                        s.messages,
                        (s.messages.c.message_id == message["in_reply_to"])
                        & (s.messages.c.chat_id == chat_id),
                        "HISTORY_PARENT_NOT_FOUND",
                    )
                for parent_id in message.get("parent_ids", ()):
                    _one(
                        conn,
                        s.messages,
                        (s.messages.c.message_id == parent_id) & (s.messages.c.chat_id == chat_id),
                        "HISTORY_PARENT_NOT_FOUND",
                    )
                if message.get("revision_id"):
                    previous_revision = (
                        conn.execute(
                            select(s.input_revisions).where(
                                s.input_revisions.c.revision_id == message["revision_id"]
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if previous_revision is not None:
                        raise StorageError("HISTORY_REVISION_CONFLICT")
                root_id = root_id or message_id
                self._put_message(
                    conn,
                    message_id=message_id,
                    chat_id=chat_id,
                    role=message["role"],
                    payload=payload,
                    status="completed",
                    origin=message.get("device_id", "external"),
                    question_root_id=message.get("question_root_id", root_id),
                    in_reply_to=message.get("in_reply_to"),
                    revision_id=message.get("revision_id"),
                    parent_ids=message.get("parent_ids", ()),
                )
                inserted += 1
            if inserted:
                conn.execute(
                    update(s.chats)
                    .where(s.chats.c.chat_id == chat_id)
                    .values(
                        history_revision=chat["history_revision"] + inserted,
                        current_question_id=root_id,
                    )
                )
                # Do not interrupt an active generation. It remains tied to its
                # immutable snapshot, and the next new question rebuilds.
                conn.execute(
                    update(s.execution_bindings)
                    .where(
                        s.execution_bindings.c.chat_id == chat_id,
                        s.execution_bindings.c.state != "retired",
                    )
                    .values(state="stale")
                )
        return {"inserted": inserted, "duplicates": duplicates}

    def revise_input(
        self,
        message_id: str,
        text: str,
        *,
        attachment_ids: Iterable[str] = (),
        conditions: Iterable[str] = (),
        corrections: Iterable[str] = (),
        source_locators: Iterable[dict[str, Any]] = (),
        read_course_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Append a new revision; an existing generation's snapshot stays fixed."""
        with self._lock, self.engine.begin() as conn:
            message = _one(
                conn, s.messages, s.messages.c.message_id == message_id, "MESSAGE_NOT_FOUND"
            )
            if message["role"] != "user":
                raise StorageError("INPUT_REVISION_ROLE_INVALID")
            original = _one(
                conn,
                s.input_revisions,
                s.input_revisions.c.revision_id == message["revision_id"],
                "REVISION_NOT_FOUND",
            )
            selected_courses = (
                original["payload"].get("read_course_ids", ())
                if read_course_ids is None
                else read_course_ids
            )
            payload = self._payload(
                text, attachment_ids, conditions, corrections, source_locators, selected_courses
            )
            chat = _one(conn, s.chats, s.chats.c.chat_id == message["chat_id"], "CHAT_NOT_FOUND")
            self._validate_payload(conn, chat, payload)
            revision_id = _id("revision")
            conn.execute(
                insert(s.input_revisions).values(
                    revision_id=revision_id,
                    message_id=message_id,
                    supersedes=message["revision_id"],
                    content_hash=_hash(payload),
                    payload=payload,
                    created_at=_now(),
                )
            )
            for attachment_id in payload["attachment_ids"]:
                conn.execute(
                    insert(s.message_attachments).values(
                        revision_id=revision_id, attachment_id=attachment_id
                    )
                )
                conn.execute(
                    update(s.attachments)
                    .where(s.attachments.c.attachment_id == attachment_id)
                    .values(state="published")
                )
            conn.execute(
                update(s.messages)
                .where(s.messages.c.message_id == message_id)
                .values(text=text, revision_id=revision_id)
            )
            conn.execute(
                update(s.chats)
                .where(s.chats.c.chat_id == message["chat_id"])
                .values(history_revision=chat["history_revision"] + 1)
            )
            conn.execute(
                update(s.execution_bindings)
                .where(
                    s.execution_bindings.c.chat_id == message["chat_id"],
                    s.execution_bindings.c.state != "retired",
                )
                .values(state="stale")
            )
            return {
                "message_id": message_id,
                "revision_id": revision_id,
                "supersedes": message["revision_id"],
                **payload,
            }

    def evidence(self, generation_id: str) -> dict[str, Any]:
        generation = self.get_generation(generation_id)
        with self.engine.connect() as conn:
            events = [
                dict(row)
                for row in conn.execute(
                    select(s.execution_events)
                    .where(s.execution_events.c.generation_id == generation_id)
                    .order_by(s.execution_events.c.seq)
                ).mappings()
            ]
            attempts = [
                dict(row)
                for row in conn.execute(
                    select(s.outbound_attempts).where(
                        s.outbound_attempts.c.generation_id == generation_id
                    )
                ).mappings()
            ]
        return {
            "generation": generation,
            "binding": self.get_binding(generation["binding_id"])
            if generation["binding_id"]
            else None,
            "events": events,
            "attempts": attempts,
        }

    def counts(self) -> dict[str, int]:
        with self.engine.connect() as conn:
            return {
                table.name: conn.execute(select(func.count()).select_from(table)).scalar_one()
                for table in s.metadata.sorted_tables
            }
