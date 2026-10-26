"""Actual text receipts -> fixed PDF region -> native encoded image receipt.

Encoding is a private, synchronous callback used only outside DB transactions.
Its implementation schedules the existing Host attachment service, not a model.
"""

import base64
import hashlib
import json
import threading
from dataclasses import asdict

from sqlalchemy import insert, select, update

from learning_app.errors import AppError
from learning_app.storage import schema as business
from learning_app.storage.repository import _hash

from . import corpus_schema as corpus
from . import document_image_schema as images
from . import schema as s
from .common import now
from .document_regions import DocumentRegionCandidate, DocumentRegions
from .source_images import image_grant, validate_encoding
from .sources import SourceStore
from .task_materials import material

TOOL = "read_document_region"
MAX_IMAGES = 6


def rendered_metadata(result):
    if result.get("render_metadata_version") == 2:
        return result["render_metadata"]
    return {key: result[key] for key in ("handle_id", "source_version", "source")}


class NativeDocumentImages:
    def __init__(self, repository):
        self.repository = repository
        self.prepare = None
        self.supports_images = lambda _model: False
        self._inflight = set()
        self._lock = threading.Lock()

    def _encode(self, scope, snapshots):
        requests = []
        for snapshot in snapshots:
            grant = image_grant(snapshot)
            raw = SourceStore(self.repository).image_path(grant["source_sha256"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != grant["source_sha256"]:
                raise AppError("SOURCE_IMAGE_CORRUPT", "冻结局部图字节校验失败，未读取新原件")
            requests.append({**grant, "data": base64.b64encode(raw).decode("ascii")})
        proofs = self.prepare(scope, requests)
        if not isinstance(proofs, list) or len(proofs) != len(snapshots):
            raise AppError("DOCUMENT_IMAGE_PREPARATION_SCOPE", "图片编码回执数量与冻结范围不符")
        return proofs

    @staticmethod
    def _text_receipts(conn, run_id, snapshot_id):
        found = set()
        for row in conn.execute(
            select(s.native_read_receipts).where(
                s.native_read_receipts.c.run_id == run_id,
                s.native_read_receipts.c.snapshot_id == snapshot_id,
            )
        ).mappings():
            if _hash(row["result"]) != row["result_hash"]:
                raise AppError("DOCUMENT_IMAGE_TEXT_RECEIPT_CONFLICT", "原文字读取回执不完整")
            if row["result"].get("source", {}).get("text", "").strip():
                found.add(row["effect_id"])
        for row in conn.execute(
            select(corpus.query_receipts).where(
                corpus.query_receipts.c.run_id == run_id,
            )
        ).mappings():
            if _hash(row["result"]) != row["result_hash"]:
                raise AppError("DOCUMENT_IMAGE_TEXT_RECEIPT_CONFLICT", "原检索读取回执不完整")
            if any(
                item["source"]["snapshot_id"] == snapshot_id
                and item["source"].get("text", "").strip()
                for item in row["result"].get("results", [])
            ):
                found.add(row["effect_id"])
        return found

    def authorize(self, port, conn, scope, arguments):
        if (
            scope["owner_kind"] != "chat"
            or scope["purpose"] != "qa"
            or not isinstance(arguments, dict)
            or set(arguments) != {"handle_id", "source_version", "region_id"}
            or not all(isinstance(arguments[key], str) for key in ("handle_id", "source_version"))
            or (arguments["region_id"] is not None and not isinstance(arguments["region_id"], str))
        ):
            raise AppError(
                "DOCUMENT_IMAGE_ARGUMENTS_DENIED", "局部图仅允许本轮文字句柄与程序候选", 403
            )
        parent = port._source(
            conn, scope, {key: arguments[key] for key in ("handle_id", "source_version")}
        )
        handle = (
            conn.execute(
                select(s.native_source_handles).where(
                    s.native_source_handles.c.run_id == scope["run_id"],
                    s.native_source_handles.c.handle_id == arguments["handle_id"],
                )
            )
            .mappings()
            .one()
        )
        if not handle["locator"].get("corpus_document_id") or not parent.text.strip():
            raise AppError(
                "DOCUMENT_IMAGE_DOCUMENT_GRANT_REQUIRED", "此文字引用没有独立的文档检索权限", 403
            )
        receipts = self._text_receipts(conn, scope["run_id"], parent.snapshot_id)
        if not receipts:
            raise AppError(
                "DOCUMENT_IMAGE_TEXT_NOT_READ", "先实际读取本轮文档文字，再定位局部图", 403
            )
        region = None
        if arguments["region_id"] is not None:
            region = (
                conn.execute(
                    select(s.native_source_handles).where(
                        s.native_source_handles.c.run_id == scope["run_id"],
                        s.native_source_handles.c.handle_id == arguments["region_id"],
                        s.native_source_handles.c.kind == "document_region",
                        s.native_source_handles.c.source_version == parent.content_hash,
                    )
                )
                .mappings()
                .first()
            )
            if (
                not region
                or region["parent_handle_id"] != arguments["handle_id"]
                or region["locator"].get("parent_handle_id") != arguments["handle_id"]
                or region["locator"].get("parent_effect_id") not in receipts
            ):
                raise AppError(
                    "DOCUMENT_IMAGE_REGION_DENIED", "局部图候选不属于本轮实际读取的文字", 403
                )
        return parent, receipts, region

    def invoke(self, port, call, arguments):
        identity = _hash(asdict(call))
        with self._lock:
            self._inflight.add(identity)
        try:
            return self._invoke(port, call, arguments)
        except AppError as error:
            result = {
                "status": "not_ready",
                "error_code": error.code,
                "regions": [],
                "results": [],
                "whole_page_fallback": False,
            }
            with self.repository._lock, self.repository.engine.begin() as conn:
                conn.execute(
                    update(images.image_reads)
                    .where(
                        images.image_reads.c.effect_id == identity,
                        images.image_reads.c.state == "pending_encoding",
                    )
                    .values(
                        state="failed", result=result, result_hash=_hash(result), updated_at=now()
                    )
                )
            raise
        finally:
            with self._lock:
                self._inflight.discard(identity)

    def _invoke(self, port, call, arguments):
        identity = _hash(asdict(call))
        with self.repository.engine.connect() as conn:
            scope = port._authorize(conn, call, arguments)
            parent, receipts, region = self.authorize(port, conn, scope, arguments)
            if arguments["region_id"] is None:
                prior = self.metadata_receipt(conn, call)
                if prior is not None:
                    return prior
            else:
                prior = (
                    conn.execute(
                        select(images.image_reads).where(
                            images.image_reads.c.effect_id == identity,
                        )
                    )
                    .mappings()
                    .first()
                )
                if prior and prior["state"] in {"completed", "failed"}:
                    return self._result(prior, call)
        SourceStore(self.repository).require_current(parent)
        if arguments["region_id"] is None:
            candidates = DocumentRegions(self.repository).candidates(
                scope["course_id"], parent.snapshot_id
            )
            with self.repository._lock, self.repository.engine.begin() as conn:
                scope = port._authorize(conn, call, arguments)
                self.authorize(port, conn, scope, arguments)
                descriptors = []
                for candidate in candidates:
                    parent_effect = min(receipts)
                    locator = {
                        "candidate": candidate.model_dump(mode="json"),
                        "parent_handle_id": arguments["handle_id"],
                        "parent_effect_id": parent_effect,
                    }
                    handle_id = "figure_" + _hash({"run_id": call.run_id, **locator})
                    existing = conn.execute(
                        select(s.native_source_handles).where(
                            s.native_source_handles.c.run_id == call.run_id,
                            s.native_source_handles.c.handle_id == handle_id,
                        )
                    ).first()
                    if existing is None:
                        conn.execute(
                            insert(s.native_source_handles).values(
                                run_id=call.run_id,
                                handle_id=handle_id,
                                kind="document_region",
                                source_version=parent.content_hash,
                                locator=locator,
                                parent_handle_id=arguments["handle_id"],
                                created_at=now(),
                            )
                        )
                    descriptors.append(
                        {
                            "region_id": handle_id,
                            "kind": candidate.kind,
                            "page_index": candidate.page_index,
                            "region": candidate.region.model_dump(mode="json"),
                        }
                    )
                result = {
                    "handle_id": arguments["handle_id"],
                    "source_version": parent.content_hash,
                    "status": "located" if descriptors else "unresolved",
                    "regions": descriptors,
                    "results": [],
                    "whole_page_fallback": False,
                }
                conn.execute(
                    insert(corpus.query_receipts).values(
                        effect_id=identity,
                        run_id=call.run_id,
                        native_call=asdict(call),
                        result=result,
                        result_hash=_hash(result),
                        created_at=now(),
                    )
                )
                return result
        if self.prepare is None or not self.supports_images(scope["settings"]["model"]):
            raise AppError(
                "MODEL_IMAGE_UNSUPPORTED", "所选模型或运行时尚不能读取此局部图；未更换模型", 422
            )
        # Repeated calls to this same frozen candidate reuse the original crop.
        with self.repository.engine.connect() as conn:
            known = (
                conn.execute(
                    select(images.image_reads).where(
                        images.image_reads.c.run_id == call.run_id,
                        images.image_reads.c.handle_id == arguments["region_id"],
                        images.image_reads.c.state == "completed",
                    )
                )
                .mappings()
                .first()
            )
        snapshot = (
            SourceStore(self.repository).snapshot(scope["course_id"], known["snapshot_id"])
            if known
            else SourceStore(self.repository).capture_document_region(
                scope["course_id"],
                DocumentRegionCandidate.model_validate(region["locator"]["candidate"]),
            )
        )
        with self.repository._lock, self.repository.engine.begin() as conn:
            scope = port._authorize(conn, call, arguments)
            self.authorize(port, conn, scope, arguments)
            if not conn.execute(
                select(images.image_reads).where(images.image_reads.c.effect_id == identity)
            ).first():
                conn.execute(
                    insert(images.image_reads).values(
                        effect_id=identity,
                        run_id=call.run_id,
                        handle_id=arguments["region_id"],
                        parent_effect_id=region["locator"]["parent_effect_id"],
                        native_call=asdict(call),
                        snapshot_id=snapshot.snapshot_id,
                        state="pending_encoding",
                        created_at=now(),
                        updated_at=now(),
                    )
                )
        self.repository._hook("document_image_before_encoding", run_id=call.run_id)
        # A local crop or foreground wait may outlive the original question.
        # Recheck before invoking the native codec, as well as before commit.
        with self.repository.engine.connect() as conn:
            scope = port._authorize(conn, call, arguments)
            self.authorize(port, conn, scope, arguments)
        proofs = self._encode(scope, [snapshot])
        self.repository._hook("document_image_after_encoding", run_id=call.run_id)
        with self.repository._lock, self.repository.engine.begin() as conn:
            scope = port._authorize(conn, call, arguments)
            self.authorize(port, conn, scope, arguments)
            result = self._complete(conn, scope, call, identity, snapshot, proofs[0])
            self.repository._hook("document_image_before_commit", run_id=call.run_id)
        self.repository._hook("document_image_after_commit", run_id=call.run_id)
        return result

    @staticmethod
    def _result(row, call):
        if row["native_call"] != asdict(call) or _hash(row["result"]) != row["result_hash"]:
            raise AppError("DOCUMENT_IMAGE_RECEIPT_CONFLICT", "原局部图读取回执不符")
        return row["result"]

    @staticmethod
    def _owner(scope):
        return {
            "session_id": scope["session_id"],
            "run_id": scope["run_id"],
            "request_id": scope["request_id"],
            "epoch": scope["native_epoch"],
        }

    def _allow(self, conn, scope, effect_id, snapshot, proof):
        validate_encoding(snapshot, proof, self._owner(scope), scope["settings"]["model"])
        entries = list(
            conn.execute(
                select(images.image_allowances).where(
                    images.image_allowances.c.run_id == scope["run_id"],
                )
            ).mappings()
        )
        unique = {row["proof"]["source_id"]: row["proof"]["encoded_bytes"] for row in entries}
        unique[snapshot.snapshot_id] = proof["encoded_bytes"]
        if len(unique) > MAX_IMAGES or sum(unique.values()) > 14 * 1024 * 1024:
            raise AppError(
                "DOCUMENT_IMAGE_INPUT_LIMIT", "本轮局部图片超过输入预算，未追加或扩大范围", 422
            )
        prior = next((row for row in entries if row["effect_id"] == effect_id), None)
        if prior and (prior["proof"] != proof or prior["proof_hash"] != _hash(proof)):
            raise AppError("DOCUMENT_IMAGE_ENCODING_CONFLICT", "同一执行的局部图片编码回执不能替换")
        if not prior:
            conn.execute(
                insert(images.image_allowances).values(
                    run_id=scope["run_id"],
                    effect_id=effect_id,
                    proof=proof,
                    proof_hash=_hash(proof),
                    created_at=now(),
                )
            )

    def _complete(self, conn, scope, call, identity, snapshot, proof):
        row = (
            conn.execute(
                select(images.image_reads).where(images.image_reads.c.effect_id == identity)
            )
            .mappings()
            .one()
        )
        if row["state"] == "completed":
            return self._result(row, call)
        if row["state"] != "pending_encoding" or row["native_call"] != asdict(call):
            raise AppError("DOCUMENT_IMAGE_READ_CLOSED", "原局部图读取不再接受编码回执", 409)
        self._allow(conn, scope, identity, snapshot, proof)
        result = {
            "handle_id": snapshot.snapshot_id,
            "source_version": snapshot.content_hash,
            "source": material(
                {"representation": "image", "leaf_id": None, "label": "已读取的文档局部图"},
                snapshot,
            ),
            "image_attachment": proof["normalized_ref"],
            "encoding_proof": proof,
        }
        label = conn.execute(
            select(business.assets.c.display_name).where(
                business.assets.c.asset_id == snapshot.reference.asset_id,
            )
        ).scalar_one()
        if snapshot.course_id != scope["course_id"]:
            course_label = conn.execute(
                select(business.courses.c.title).where(
                    business.courses.c.course_id == snapshot.course_id
                )
            ).scalar_one()
            label = course_label + " · " + label
        result["source"]["label"] = label + " · 文档局部图"
        result["render_metadata_version"] = 2
        result["render_metadata"] = {
            "handle_id": snapshot.snapshot_id,
            "source_version": snapshot.content_hash,
            "source": {
                "snapshot_id": snapshot.snapshot_id,
                "content_role": snapshot.content_role,
                "source_course_id": snapshot.course_id,
                "label": label + " · 文档局部图",
                "page": snapshot.reference.page_index + 1,
            },
        }
        conn.execute(
            update(images.image_reads)
            .where(images.image_reads.c.effect_id == identity)
            .values(
                state="completed",
                result=result,
                result_hash=_hash(result),
                updated_at=now(),
            )
        )
        conn.execute(
            insert(s.native_read_receipts).values(
                effect_id=identity,
                run_id=call.run_id,
                snapshot_id=snapshot.snapshot_id,
                native_call=asdict(call),
                result=result,
                result_hash=_hash(result),
                created_at=now(),
            )
        )
        return result

    @staticmethod
    def metadata_receipt(conn, call):
        row = (
            conn.execute(
                select(corpus.query_receipts).where(
                    corpus.query_receipts.c.effect_id == _hash(asdict(call)),
                    corpus.query_receipts.c.run_id == call.run_id,
                )
            )
            .mappings()
            .first()
        )
        if not row:
            return None
        if row["native_call"] != asdict(call) or _hash(row["result"]) != row["result_hash"]:
            raise AppError("DOCUMENT_IMAGE_RECEIPT_CONFLICT", "原局部图定位回执不符")
        return row["result"]

    def receipt(self, conn, call):
        metadata = self.metadata_receipt(conn, call)
        if metadata is not None:
            return metadata
        row = (
            conn.execute(
                select(images.image_reads).where(
                    images.image_reads.c.effect_id == _hash(asdict(call)),
                    images.image_reads.c.run_id == call.run_id,
                )
            )
            .mappings()
            .first()
        )
        if not row:
            return None
        if row["native_call"] != asdict(call):
            raise AppError("DOCUMENT_IMAGE_RECEIPT_CONFLICT", "原局部图读取身份不符")
        if row["state"] in {"completed", "failed"}:
            return self._result(row, call)
        # No allowance can leave a pending transaction: an interrupted local
        # encoding never returned an ImageBlock. It is a known read failure,
        # not permission to replay a model/tool or reread the original PDF.
        with self._lock:
            if row["effect_id"] in self._inflight:
                return None
        if conn.execute(
            select(images.image_allowances).where(
                images.image_allowances.c.effect_id == row["effect_id"],
            )
        ).first():
            return None
        result = {
            "status": "not_ready",
            "error_code": "DOCUMENT_IMAGE_ENCODING_INTERRUPTED",
            "regions": [],
            "results": [],
            "whole_page_fallback": False,
        }
        conn.execute(
            update(images.image_reads)
            .where(images.image_reads.c.effect_id == row["effect_id"])
            .values(
                state="failed",
                result=result,
                result_hash=_hash(result),
                updated_at=now(),
            )
        )
        return result

    @staticmethod
    def freeze_history(conn, run, chat, context):
        frozen = {}
        for message in context["messages"]:
            generation_id = message.get("generation_id")
            if message.get("role") != "assistant" or not generation_id:
                continue
            generation = conn.execute(
                select(business.generations.c.generation_id)
                .join(
                    business.submissions,
                    business.submissions.c.submission_id == business.generations.c.submission_id,
                )
                .where(
                    business.generations.c.generation_id == generation_id,
                    business.generations.c.generation_id != run["generation_id"],
                    business.generations.c.state.in_(
                        ["completed", "failed", "cancelled", "truncated"]
                    ),
                    business.generations.c.assistant_message_id == message["message_id"],
                    business.submissions.c.chat_id == chat["chat_id"],
                )
            ).first()
            if not generation:
                continue
            for row in conn.execute(
                select(images.image_reads)
                .join(s.native_scopes)
                .where(
                    images.image_reads.c.run_id == generation_id,
                    images.image_reads.c.state == "completed",
                    s.native_scopes.c.course_id == chat["course_id"],
                    s.native_scopes.c.owner_kind == "chat",
                )
            ).mappings():
                if _hash(row["result"]) != row["result_hash"]:
                    raise AppError("DOCUMENT_IMAGE_HISTORY_CONFLICT", "原文档图片历史回执不完整")
                frozen[row["effect_id"]] = {
                    "effect_id": row["effect_id"],
                    "result_hash": row["result_hash"],
                    "snapshot_id": row["snapshot_id"],
                    "source_sha256": row["result"]["encoding_proof"]["source_sha256"],
                }
        if len({row["snapshot_id"] for row in frozen.values()}) > MAX_IMAGES:
            raise AppError(
                "DOCUMENT_IMAGE_HISTORY_LIMIT",
                "本对话文档图片历史超过本轮输入预算，请使用明确的新对话范围",
                422,
            )
        return list(frozen.values())

    def prepare_history(self, port, run_id, rebuild=False):
        with self.repository.engine.connect() as conn:
            scope = port._scope(conn, run_id)
            declarations = scope["grants"].get("document_image_history", [])
            pending = []
            for declaration in declarations:
                row = (
                    conn.execute(
                        select(images.image_reads).where(
                            images.image_reads.c.effect_id == declaration["effect_id"]
                        )
                    )
                    .mappings()
                    .one()
                )
                if (
                    row["state"] != "completed"
                    or row["result_hash"] != declaration["result_hash"]
                    or _hash(row["result"]) != declaration["result_hash"]
                    or row["snapshot_id"] != declaration["snapshot_id"]
                ):
                    raise AppError(
                        "DOCUMENT_IMAGE_HISTORY_CONFLICT", "冻结文档图片历史已变化，未扩大读取"
                    )
                if (
                    not rebuild
                    and conn.execute(
                        select(images.image_allowances).where(
                            images.image_allowances.c.run_id == run_id,
                            images.image_allowances.c.effect_id == row["effect_id"],
                        )
                    ).first()
                ):
                    continue
                snapshot = SourceStore.in_transaction(conn, scope["course_id"], row["snapshot_id"])
                if snapshot.reference.image_sha256 != declaration["source_sha256"]:
                    raise AppError("DOCUMENT_IMAGE_HISTORY_CONFLICT", "冻结历史裁图摘要不符")
                pending.append((row, snapshot))
        if not pending:
            return
        if self.prepare is None or not self.supports_images(scope["settings"]["model"]):
            raise AppError(
                "MODEL_IMAGE_UNSUPPORTED", "所选模型不能读取本次合法文档图片历史，未更换模型", 422
            )
        unique = {snapshot.snapshot_id: snapshot for _, snapshot in pending}
        encoded = self._encode(scope, list(unique.values()))
        proofs = dict(zip(unique, encoded, strict=True))
        with self.repository._lock, self.repository.engine.begin() as conn:
            current = port._scope(conn, run_id)
            if current["state"] != "active" or current["grants"] != scope["grants"]:
                raise AppError("DOCUMENT_IMAGE_HISTORY_STALE", "原执行范围已变化，未追加图片历史")
            for row, snapshot in pending:
                proof = proofs[snapshot.snapshot_id]
                if (
                    proof["normalized_sha256"]
                    != row["result"]["encoding_proof"]["normalized_sha256"]
                ):
                    raise AppError(
                        "DOCUMENT_IMAGE_HISTORY_CODEC_CHANGED", "历史图片规范化身份改变，未静默替换"
                    )
                self._allow(conn, current, row["effect_id"], snapshot, proof)

    def history_content(self, port, run_id):
        """Rebuild model material from exact frozen crop bytes, never original PDFs."""
        material = {}
        with self.repository.engine.connect() as conn:
            scope = port._scope(conn, run_id)
            for declaration in scope["grants"].get("document_image_history", []):
                row = (
                    conn.execute(
                        select(images.image_reads).where(
                            images.image_reads.c.effect_id == declaration["effect_id"],
                        )
                    )
                    .mappings()
                    .one()
                )
                if (
                    row["result_hash"] != declaration["result_hash"]
                    or _hash(row["result"]) != declaration["result_hash"]
                ):
                    raise AppError("DOCUMENT_IMAGE_HISTORY_CONFLICT", "冻结图片历史回执改变")
                snapshot = SourceStore.in_transaction(conn, scope["course_id"], row["snapshot_id"])
                allowance = (
                    conn.execute(
                        select(images.image_allowances).where(
                            images.image_allowances.c.run_id == run_id,
                            images.image_allowances.c.effect_id == row["effect_id"],
                        )
                    )
                    .mappings()
                    .one()
                )
                if _hash(allowance["proof"]) != allowance["proof_hash"]:
                    raise AppError("DOCUMENT_IMAGE_ALLOWANCE_CONFLICT", "图片历史编码回执改变")
                validate_encoding(
                    snapshot, allowance["proof"], self._owner(scope), scope["settings"]["model"]
                )
                raw = SourceStore(self.repository).cached_image(snapshot)
                material[snapshot.snapshot_id] = {
                    "metadata": rendered_metadata(row["result"]),
                    "data": base64.b64encode(raw).decode("ascii"),
                    "mediaType": "image/png",
                }
        return list(material.values())

    def validate_request(self, run_id, request, summarize):
        with self.repository.engine.connect() as conn:
            scope = (
                conn.execute(select(s.native_scopes).where(s.native_scopes.c.run_id == run_id))
                .mappings()
                .one()
            )
            declared = {
                item["effect_id"] for item in scope["grants"].get("document_image_history", [])
            }
            grants = []
            for allowance in conn.execute(
                select(images.image_allowances).where(
                    images.image_allowances.c.run_id == run_id,
                )
            ).mappings():
                row = (
                    conn.execute(
                        select(images.image_reads).where(
                            images.image_reads.c.effect_id == allowance["effect_id"],
                        )
                    )
                    .mappings()
                    .one()
                )
                if (
                    row["state"] != "completed"
                    or _hash(row["result"]) != row["result_hash"]
                    or _hash(allowance["proof"]) != allowance["proof_hash"]
                    or (row["run_id"] != run_id and row["effect_id"] not in declared)
                ):
                    raise AppError(
                        "DOCUMENT_IMAGE_ALLOWANCE_CONFLICT", "图片出站回执或历史名单不符", 403
                    )
                snapshot = SourceStore.in_transaction(conn, scope["course_id"], row["snapshot_id"])
                validate_encoding(
                    snapshot, allowance["proof"], self._owner(scope), scope["settings"]["model"]
                )
                grants.append((row, allowance["proof"]))
            from learning_app.chat_attachments import wire_grants

            file_grants = wire_grants(conn, scope)
            grants.extend(file_grants)
        occurrences, file_occurrences = 0, 0
        for message in request.get("messages", []):
            content = message.get("content", [])
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "tool_result":
                    continue
                pictures = summarize(part.get("content", []))
                if not pictures:
                    continue
                occurrences += len(pictures)
                blocks = part.get("content", [])
                try:
                    text = next(
                        block["text"]
                        for block in blocks
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                    metadata = json.loads(text)
                except (StopIteration, KeyError, ValueError, TypeError):
                    raise AppError(
                        "DOCUMENT_IMAGE_WIRE_METADATA_DENIED",
                        "图片工具结果缺少可核对的原始来源",
                        403,
                    ) from None
                matches = [
                    (row, proof)
                    for row, proof in grants
                    if row["native_call"]["call_id"] == part.get("tool_use_id")
                    and metadata == rendered_metadata(row["result"])
                ]
                if any(row["native_call"].get("tool_name") == "read_chat_attachment"
                       for row, _ in matches):
                    file_occurrences += len(pictures)
                if len(pictures) != 1 or not any(
                    pictures[0]["sha256"] == proof["encoded_sha256"]
                    and pictures[0].get("mime") == proof["encoded_media_type"]
                    and pictures[0].get("kind") == "image"
                    for _, proof in matches
                ):
                    raise AppError(
                        "DOCUMENT_IMAGE_WIRE_SCOPE_DENIED",
                        "实际工具图片与本轮已提交编码回执不符",
                        403,
                    )
            for index, part in enumerate(content):
                prefix = "READ_ONLY_DOCUMENT_IMAGE_HISTORY:"
                if (
                    not isinstance(part, dict)
                    or part.get("type") != "text"
                    or not part.get("text", "").startswith(prefix)
                ):
                    continue
                try:
                    metadata = json.loads(part["text"][len(prefix) :])
                except ValueError:
                    raise AppError(
                        "DOCUMENT_IMAGE_HISTORY_WIRE_DENIED", "图片历史来源标记无效", 403
                    ) from None
                pictures = [
                    item
                    for block in content[index + 1 :]
                    if isinstance(block, dict) and block.get("type") == "image"
                    for item in summarize(block)
                ]
                occurrences += len(pictures)
                if not isinstance(metadata, list) or not metadata or len(metadata) != len(pictures):
                    raise AppError(
                        "DOCUMENT_IMAGE_HISTORY_WIRE_DENIED", "图片历史与冻结来源数量不符", 403
                    )
                for meta, picture in zip(metadata, pictures, strict=True):
                    if not any(
                        row["effect_id"] in declared
                        and meta == rendered_metadata(row["result"])
                        and picture["sha256"] == proof["encoded_sha256"]
                        and picture.get("mime") == proof["encoded_media_type"]
                        and picture.get("kind") == "image"
                        for row, proof in grants
                    ):
                        raise AppError(
                            "DOCUMENT_IMAGE_HISTORY_WIRE_DENIED",
                            "实际历史图片不属于本次冻结来源和编码",
                            403,
                        )
        if occurrences - file_occurrences > MAX_IMAGES:
            raise AppError(
                "DOCUMENT_IMAGE_INPUT_LIMIT", "工具图片历史超过本次输入预算，未发送", 403
            )
