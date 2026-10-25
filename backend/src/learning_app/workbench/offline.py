"""An exact artifact revision as a portable, read-only standalone ZIP."""

import base64
import hashlib
import html
import json
from tempfile import SpooledTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import select

from learning_app.errors import AppError
from learning_app.media import _read_locked
from learning_app.storage import schema as business
from learning_app.storage.repository import _hash

from . import schema as s
from .artifact_scope import ArtifactScopeStore
from .artifacts import ArtifactStore
from .books import BookStore
from .common import now
from .models import Contract, Identity
from .offline_resources import OfflineRuntime
from .sources import SourceStore

MAX_CONTENT_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_BYTES = 256 * 1024 * 1024


class OfflineExportInput(Contract):
    revision_id: Identity


class OfflineExporter:
    def __init__(self, repository, runtime=None):
        self.repository = repository
        self.runtime = runtime or OfflineRuntime()

    @staticmethod
    def source(conn, course_id, identity, local_id):
        snapshot = current = SourceStore.in_transaction(conn, course_id, identity)
        offset = 0
        for depth in range(17):
            if current.reference.kind != "snapshot_slice":
                break
            if depth == 16:
                raise AppError("SOURCE_REFERENCE_DEPTH", "来源片段层次无法可靠导出")
            reference = current.reference
            parent = SourceStore.in_transaction(conn, current.course_id, reference.parent_snapshot_id)
            if (
                parent.content_hash != reference.parent_hash
                or current.text != parent.text[reference.start : reference.end]
            ):
                raise AppError("SOURCE_SLICE_CORRUPT", "来源片段与冻结原文不符")
            offset += reference.start
            current = parent
        raw = current.reference.model_dump(mode="json")
        allowed = {
            "kind",
            "source_version",
            "source_digest",
            "page_index",
            "page_label",
            "start",
            "end",
            "offset_unit",
            "parts",
            "parser_version",
            "transcript_revision_id",
            "start_ms",
            "end_ms",
            "url",
            "final_url",
            "title",
            "retrieved_at",
            "published_at",
            "response_sha256",
            "evidence_kind",
            "target_http_status",
            "region",
            "pixel_rect",
            "image_to_pdf",
            "region_parser_version",
            "parent_text_hash",
        }
        location = {key: value for key, value in raw.items() if key in allowed}
        if "parts" in location:
            location["parts"] = [
                {key: value for key, value in part.items() if key != "regions"}
                for part in location["parts"]
            ]
        location["snapshot_hash"] = snapshot.content_hash
        if offset or snapshot.snapshot_id != current.snapshot_id:
            location["range_in_frozen_source"] = {
                "start": offset,
                "end": offset + len(snapshot.text),
            }
        label = "已保存来源"
        if raw.get("asset_id"):
            label = (
                conn.execute(
                    select(business.assets.c.display_name).where(
                        business.assets.c.asset_id == raw["asset_id"],
                    )
                ).scalar_one_or_none()
                or label
            )
        elif raw.get("artifact_id"):
            revision = ArtifactStore._revision(conn, raw["artifact_id"], raw["revision_id"])
            label = revision.title
            location["revision_hash"] = _hash(revision.model_dump(mode="json"))
        elif raw.get("kind") == "web_evidence":
            label = raw.get("title") or raw["url"]
        elif raw.get("kind") == "message":
            label = "已提交的对话文字"
        elif raw.get("kind") == "submitted_image":
            label = "用户已确认提交的图片范围"
            location["image_sha256"] = raw["image_sha256"]
        elif raw.get("kind") == "practice_answer":
            label = "已引用的作答文字"
        result = {
            "id": local_id,
            "label": label,
            "role": snapshot.content_role,
            "text": snapshot.text,
            "location": location,
        }
        image = None
        if raw["kind"] in {"document_image", "document_region_image", "submitted_image"}:
            image = (raw["image_sha256"], raw.get("media_type", "image/png").split("/")[1])
        return result, image

    def snapshot(self, course_id, artifact_id, revision_id):
        with self.repository.engine.connect() as conn:
            artifact = ArtifactStore._view(conn, course_id, artifact_id)
            revision = ArtifactStore._revision(conn, artifact_id, revision_id)
            if revision.disposition != "published":
                raise AppError(
                    "OFFLINE_REVISION_NOT_PUBLISHED", "请先采用或合并此候选，再导出该版本", 409
                )
            source_ids, images, block_ids = {}, {}, {}
            sources, blocks, linked = [], [], {}

            def export_block(block, namespace=revision.revision_id):
                local_id = "b" + str(len(block_ids))
                key = (namespace, block.block_id)
                block_ids.setdefault(key, local_id)
                local_id = block_ids[key]
                value = block.model_dump(mode="json")
                value["block_id"] = local_id
                value["source_snapshot_ids"] = []
                for identity in block.source_snapshot_ids:
                    if identity not in source_ids:
                        if len(source_ids) >= 10_000:
                            raise AppError(
                                "OFFLINE_SOURCE_LIMIT", "来源数量超过当前单包上限，未截断导出"
                            )
                        source_id = "s" + str(len(source_ids))
                        source_ids[identity] = source_id
                        source, image = self.source(conn, course_id, identity, source_id)
                        if image:
                            digest, extension = image
                            name = f"assets/{digest}.{extension}"
                            source["image"] = name
                            images[name] = digest
                        sources.append(source)
                    value["source_snapshot_ids"].append(source_ids[identity])
                if block.kind == "translation":
                    value["translation"]["source_parts"] = [
                        {**part, "snapshot_id": source_ids[part["snapshot_id"]]}
                        for part in value["translation"]["source_parts"]
                    ]
                if block.kind == "assessment":
                    value = {
                        key: value[key]
                        for key in ("block_id", "markdown", "attribution", "source_snapshot_ids")
                    }
                    value["kind"] = "markdown"
                elif block.kind == "essay_feedback":
                    value["feedback"] = {
                        "round_id": "offline",
                        "draft_revision_id": "offline",
                        "draft_content_hash": block.feedback.draft_content_hash,
                        "annotated_text": block.feedback.annotated_text,
                    }
                elif block.kind == "path_step":
                    value["step"]["questions"] = []
                    linked[local_id] = []
                    for link in block.step.questions:
                        ArtifactStore._view(conn, course_id, link.artifact_id)
                        question_revision = ArtifactStore._revision(
                            conn, link.artifact_id, link.revision_id
                        )
                        question = next(
                            (
                                item
                                for item in question_revision.body.blocks
                                if item.block_id == link.block_id and item.kind == "question"
                            ),
                            None,
                        )
                        if not question or question_revision.disposition != "published":
                            raise AppError(
                                "OFFLINE_PATH_QUESTION_UNAVAILABLE", "路线关联的题目版本无法核对"
                            )
                        linked[local_id].append(export_block(question, link.revision_id))
                return value

            blocks = [export_block(block) for block in revision.body.blocks]
            chapters = []
            origin_task = revision.task_id
            if artifact.kind == "book":
                layout = BookStore.layout(conn, artifact_id, revision_id, required=False)
                if layout:
                    origin_task = layout.origin_task_id
                    for index, chapter in enumerate(layout.chapters):
                        pages = []
                        for page in sorted(layout.pages, key=lambda item: item.order_key):
                            if page.chapter_id != chapter.chapter_id:
                                continue
                            ids = [
                                block_ids[(revision_id, identity)]
                                for identity in page.block_ids
                                if (revision_id, identity) in block_ids
                            ]
                            if ids:
                                pages.append(
                                    {
                                        "id": "p" + str(len(pages)) + "c" + str(index),
                                        "title": page.title,
                                        "block_ids": ids,
                                    }
                                )
                        chapters.append(
                            {
                                "id": "c" + str(index),
                                "title": chapter.title,
                                "objective": chapter.objective,
                                "pages": pages,
                            }
                        )
                    assigned = {
                        identity
                        for chapter in chapters
                        for page in chapter["pages"]
                        for identity in page["block_ids"]
                    }
                    remaining = [
                        block["block_id"] for block in blocks if block["block_id"] not in assigned
                    ]
                    if remaining:
                        chapters.append(
                            {
                                "id": "c_extra",
                                "title": "本版补充内容",
                                "objective": "",
                                "pages": [
                                    {
                                        "id": "p_extra",
                                        "title": "本版补充内容",
                                        "block_ids": remaining,
                                    }
                                ],
                            }
                        )
            historical = revision_id != artifact.head_revision_id
            coverage = "导出本次保存的成果修订。"
            if historical:
                coverage = "历史成果修订：仅包含本版已保存的内容，不代表后续任务覆盖状态。"
            elif origin_task:
                task = (
                    conn.execute(
                        select(s.tasks).where(
                            s.tasks.c.task_id == origin_task, s.tasks.c.course_id == course_id
                        )
                    )
                    .mappings()
                    .first()
                )
                if task:
                    coverage = (
                        "任务已完成，导出本版已保存内容。"
                        if task["state"] == "completed"
                        else "此成果尚非完整任务结果；导出仅含本版已有内容，未完成或排除范围不在包中。"
                    )
            scope = ArtifactScopeStore.in_transaction(conn, course_id, artifact_id, revision_id)
            document = {
                "format": "learning-offline-v1",
                "title": revision.title,
                "kind": artifact.kind,
                "role": revision.content_role,
                "exported_at": now(),
                "revision_hash": _hash(revision.model_dump(mode="json")),
                "coverage": coverage,
                "notices": scope.notices,
                "block_notices": {
                    block_ids[(revision_id, identity)]: text
                    for identity, text in scope.block_notices.items()
                    if (revision_id, identity) in block_ids
                },
                "blocks": blocks,
                "sources": sources,
                "linked_questions": linked,
                "chapters": chapters,
            }
            data = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(data) > MAX_CONTENT_BYTES:
                raise AppError("OFFLINE_CONTENT_LIMIT", "单包内容超过64MiB，未截断导出")
            return document, images

    def build(self, course_id, artifact_id, revision_id):
        runtime = self.runtime.read()
        document, images = self.snapshot(course_id, artifact_id, revision_id)
        data = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        for character, replacement in [
            ("&", "\\u0026"),
            ("<", "\\u003c"),
            (">", "\\u003e"),
            ("\u2028", "\\u2028"),
            ("\u2029", "\\u2029"),
        ]:
            data = data.replace(character, replacement)
        javascript, css = (
            runtime["reader.js"].decode("utf-8"),
            runtime["reader.css"].decode("utf-8"),
        )
        if "</script" in javascript.lower() or "</style" in css.lower():
            raise AppError("OFFLINE_RUNTIME_EMBED_DENIED", "固定阅读资源不能安全内嵌")
        js_hash = base64.b64encode(hashlib.sha256(runtime["reader.js"]).digest()).decode("ascii")
        css_hash = base64.b64encode(hashlib.sha256(runtime["reader.css"]).digest()).decode("ascii")
        csp = (
            f"default-src 'none'; script-src 'sha256-{js_hash}'; style-src 'sha256-{css_hash}'; "
            "style-src-attr 'unsafe-inline'; img-src 'self' data:; font-src data:; worker-src blob:; "
            "connect-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'"
        )
        page = (
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{html.escape(csp, quote=True)}">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="referrer" content="no-referrer">'
            f"<title>{html.escape(document['title'])}</title><style>{css}</style></head><body>"
            '<div id="learning-offline-root"></div><noscript>请启用浏览器JavaScript读取交互内容。</noscript>'
            f'<script type="application/json" id="learning-offline-data">{data}</script>'
            f"<script>{javascript}</script></body></html>"
        ).encode("utf-8")
        spool = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        try:
            manifest = {
                "format": "learning-offline-package-v1",
                "revision_hash": document["revision_hash"],
                "runtime_release": self.runtime.lock["release_id"],
                "files": [],
            }
            size = 0
            with ZipFile(spool, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:

                def add(name, content):
                    nonlocal size
                    size += len(content)
                    if size > MAX_PACKAGE_BYTES:
                        raise AppError(
                            "OFFLINE_PACKAGE_LIMIT", "离线资源超过256MiB，未生成不完整包"
                        )
                    archive.writestr(name, content)
                    manifest["files"].append(
                        {
                            "path": name,
                            "bytes": len(content),
                            "sha256": hashlib.sha256(content).hexdigest(),
                        }
                    )

                add("index.html", page)
                add("notices.txt", runtime["notices.txt"])
                add(
                    "使用说明.txt",
                    "解压整个文件夹后双击index.html。可断网、关闭学习软件后阅读。\n自测仅在当前页面，不回传正式成绩或复习安排。\n保留整个文件夹以免图片丢失。\n".encode(),
                )
                for name, digest in images.items():
                    path = SourceStore(self.repository).image_path(digest)
                    if any(
                        p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
                        for p in [path, *path.parents]
                    ):
                        raise AppError("OFFLINE_IMAGE_PATH_DENIED", "授权图像目录被重定向，未读取")
                    try:
                        with _read_locked(path) as stream:
                            content = stream.read(MAX_PACKAGE_BYTES + 1)
                    except OSError as error:
                        raise AppError(
                            "OFFLINE_IMAGE_UNAVAILABLE", "成果引用的授权图像已缺失"
                        ) from error
                    valid_image = (
                        name.endswith(".png")
                        and content.startswith(b"\x89PNG\r\n\x1a\n")
                        or name.endswith(".jpeg")
                        and content.startswith(b"\xff\xd8\xff")
                        or name.endswith(".webp")
                        and content[:4] == b"RIFF"
                        and content[8:12] == b"WEBP"
                    )
                    if not valid_image or hashlib.sha256(content).hexdigest() != digest:
                        raise AppError(
                            "OFFLINE_IMAGE_HASH_MISMATCH", "授权图像摘要不符，未换用其他图片"
                        )
                    add(name, content)
                self.repository._hook("learning_offline_before_finish", artifact_id=artifact_id)
                archive.writestr(
                    "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode()
                )
            spool.seek(0)
            return spool
        except BaseException:
            spool.close()
            raise
