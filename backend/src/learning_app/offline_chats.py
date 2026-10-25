"""Offline chat projection from one immutable business cut, never live state."""

import html
import json
import re
from contextlib import closing
from datetime import UTC, datetime

from learning_app.data_snapshot import SnapshotCut, _readonly
from learning_app.drafts import message_selection_ids
from learning_app.errors import AppError
from learning_app.offline_chat_resources import OfflineChatRuntime

_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_SHA = re.compile(r"^[a-f0-9]{64}$")
_ROLES = {
    "original": "原始资料",
    "user_note": "用户笔记",
    "ai_generated": "AI生成",
    "mixed": "混合修订",
}


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise AppError("DATA_CHAT_ID_INVALID", "聊天记录身份无效")
    return value


def _data_html(document, prefix):
    data = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    for char, replacement in [
        ("&", "\\u0026"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ]:
        data = data.replace(char, replacement)
    csp = (
        "default-src 'none'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; "
        "img-src 'self' data:; font-src data:; connect-src 'none'; object-src 'none'; frame-src 'none'; "
        "base-uri 'none'; form-action 'none'"
    )
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f'<meta http-equiv="Content-Security-Policy" content="{html.escape(csp, quote=True)}">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">'
        f'<title>{html.escape(document.get("title", "聊天记录"))}</title><link rel="stylesheet" href="{prefix}reader/reader.css"></head>'
        '<body><div id="learning-offline-chat-root"></div><noscript>请启用JavaScript读取离线记录。</noscript>'
        f'<script type="application/json" id="learning-offline-chat-data">{data}</script>'
        f'<script src="{prefix}reader/reader.js"></script></body></html>'
    ).encode("utf-8")


class OfflineChats:
    def __init__(self, runtime=None):
        self.runtime = runtime or OfflineChatRuntime()

    @staticmethod
    def _source_label(connection, identity, assets):
        seen = set()
        for _ in range(17):
            if identity in seen:
                raise AppError("DATA_CHAT_SOURCE_CYCLE", "聊天来源链不能可靠导出")
            seen.add(identity)
            row = connection.execute(
                "SELECT reference,content_role FROM learning_snapshots WHERE snapshot_id=?",
                (identity,),
            ).fetchone()
            if row is None:
                return "已保存引用（原来源不可用）"
            ref = json.loads(row["reference"])
            if ref.get("kind") == "snapshot_slice":
                identity = ref["parent_snapshot_id"]
                continue
            label = assets.get(ref.get("asset_id"), "已保存引用")
            if ref.get("kind") == "artifact":
                revision = connection.execute(
                    "SELECT title FROM learning_revisions WHERE revision_id=?",
                    (ref.get("revision_id"),),
                ).fetchone()
                label = revision[0] if revision else "学习成果"
            elif ref.get("kind") == "web_evidence":
                label = ref.get("title") or ref["url"]
            if "page_index" in ref:
                label += f" · PDF第{ref['page_index'] + 1}页"
            elif ref.get("parts"):
                pages = ", ".join(str(p["page_index"] + 1) for p in ref["parts"])
                label += f" · PDF第{pages}页"
            if "start_ms" in ref:
                label += f" · {ref['start_ms'] / 1000:g}—{ref['end_ms'] / 1000:g}秒"
            return label + " · " + _ROLES.get(row["content_role"], "已保存来源")
        raise AppError("DATA_CHAT_SOURCE_DEPTH", "聊天来源链层数超限")

    @classmethod
    def _message(cls, connection, row, assets, cut_files):
        payload = connection.execute(
            "SELECT payload FROM input_revisions WHERE revision_id=?", (row["revision_id"],)
        ).fetchone()
        if payload is None:
            raise AppError("DATA_CHAT_REVISION_MISSING", "消息版本不完整，未导出不完整记录")
        payload = json.loads(payload[0])
        attachments = []
        labels = []
        for aid in payload.get("attachment_ids", []):
            attachment = connection.execute(
                "SELECT * FROM attachments WHERE attachment_id=?", (aid,)
            ).fetchone()
            if attachment is None or not _SHA.fullmatch(attachment["content_hash"]):
                raise AppError("DATA_CHAT_ATTACHMENT_MISSING", "消息附件没有可核对的已保存字节")
            digest = attachment["content_hash"]
            name = f"attachments/objects/{digest[:2]}/{digest}"
            if name not in cut_files:
                raise AppError("DATA_CHAT_ATTACHMENT_MISSING", "消息附件不在一致快照中")
            original = connection.execute(
                "SELECT filename FROM document_attachments WHERE attachment_id=?", (aid,)
            ).fetchone()
            filename = original[0] if original else "已提交图片"
            attachments.append(
                {
                    "filename": filename,
                    "media_type": attachment["media_type"],
                    "path": "../../data/" + name,
                }
            )
        for locator in payload.get("source_locators", []):
            if locator.get("asset_id"):
                label = assets.get(locator["asset_id"], "原始资料")
                if "time_ms" in locator:
                    label += f" · {locator['time_ms'] / 1000:g}秒"
                labels.append(label)
        identities = list(message_selection_ids(payload.get("conditions", [])))
        if row["generation_id"]:
            identities.extend(
                r[0]
                for r in connection.execute(
                    "SELECT snapshot_id FROM learning_native_read_receipts WHERE run_id=?",
                    (row["generation_id"],),
                )
            )
            identities.extend(
                r[0]
                for r in connection.execute(
                    "SELECT snapshot_id FROM learning_document_image_reads WHERE run_id=? AND state='completed'",
                    (row["generation_id"],),
                )
            )
            for receipt in connection.execute(
                "SELECT result FROM learning_native_query_receipts WHERE run_id=?",
                (row["generation_id"],),
            ):
                value = json.loads(receipt[0])
                identities.extend(
                    result["source"]["snapshot_id"]
                    for result in value.get("results", [])
                    if isinstance(result, dict)
                    and isinstance(result.get("source"), dict)
                    and isinstance(result["source"].get("snapshot_id"), str)
                )
        labels.extend(
            cls._source_label(connection, sid, assets) for sid in dict.fromkeys(identities)
        )
        paths = {a["path"] for a in attachments}
        for identity in dict.fromkeys(identities):
            source = connection.execute(
                "SELECT reference FROM learning_snapshots WHERE snapshot_id=?", (identity,)
            ).fetchone()
            reference = json.loads(source[0]) if source else {}
            if reference.get("kind") in {"document_image", "document_region_image"}:
                name = f"learning-source-images/{reference['image_sha256']}.png"
                path = "../../data/" + name
                if name not in cut_files:
                    raise AppError("DATA_CHAT_SOURCE_IMAGE_MISSING", "已保存的来源图不在一致快照中")
                if path not in paths:
                    attachments.append(
                        {
                            "filename": cls._source_label(connection, identity, assets),
                            "media_type": "image/png",
                            "path": path,
                        }
                    )
                    paths.add(path)
        generation = (
            connection.execute(
                "SELECT state,reasoning FROM generations WHERE generation_id=?",
                (row["generation_id"],),
            ).fetchone()
            if row["generation_id"]
            else None
        )
        return {
            "id": row["message_id"],
            "role": row["role"],
            "text": row["text"],
            "status": generation["state"] if generation else row["status"],
            "created_at": row["created_at"],
            "reasoning": generation["reasoning"] if generation else "",
            "attachments": attachments,
            "sources": list(dict.fromkeys(labels)),
        }

    def write(self, cut: SnapshotCut, destination):
        destination.mkdir(exist_ok=False)
        (destination / "reader").mkdir()
        (destination / "chats").mkdir()
        for name, content in self.runtime.read().items():
            (destination / "reader" / name).write_bytes(content)
        index = {
            "format": "learning-offline-chat-index-v1",
            "exported_at": datetime.now(UTC).isoformat(),
            "courses": [],
        }
        cut_files = {row["path"] for row in cut.files}
        with closing(_readonly(cut.database)) as connection:
            assets = {
                row[0]: row[1]
                for row in connection.execute("SELECT asset_id,display_name FROM assets")
            }
            for course in connection.execute("SELECT * FROM courses ORDER BY created_at,course_id"):
                item = {"id": course["course_id"], "title": course["title"], "chats": []}
                index["courses"].append(item)
                for chat in connection.execute(
                    "SELECT * FROM chats WHERE course_id=? ORDER BY created_at,chat_id",
                    (course["course_id"],),
                ):
                    identity = _identity(chat["chat_id"])
                    first = connection.execute(
                        "SELECT text FROM messages WHERE chat_id=? AND role='user' ORDER BY sequence LIMIT 1",
                        (identity,),
                    ).fetchone()
                    title = " ".join(first[0].split())[:80] if first else "新对话"
                    binding = connection.execute(
                        "SELECT adapter_metadata FROM execution_bindings WHERE chat_id=? ORDER BY epoch DESC LIMIT 1",
                        (identity,),
                    ).fetchone()
                    manual = (
                        json.loads(binding[0]).get("desktop_title_override") if binding else None
                    )
                    if isinstance(manual, str) and manual.strip():
                        title = manual
                    item["chats"].append(
                        {"title": title or "图片提问", "path": f"chats/{identity}.html"}
                    )
                    total = connection.execute(
                        "SELECT count(*) FROM messages WHERE chat_id=?", (identity,)
                    ).fetchone()[0]
                    pages = max(1, (total + 99) // 100)
                    cursor = connection.execute(
                        "SELECT * FROM messages WHERE chat_id=? ORDER BY sequence", (identity,)
                    )
                    for part in range(pages):

                        def filename(number):
                            return (
                                f"{identity}.html" if number == 0 else f"{identity}-{number}.html"
                            )

                        document = {
                            "format": "learning-offline-chat-v1",
                            "title": title or "图片提问",
                            "course": course["title"],
                            "messages": [
                                self._message(connection, r, assets, cut_files)
                                for r in cursor.fetchmany(100)
                            ],
                            "navigation": {
                                "first": part * 100 + 1 if total else 0,
                                "last": min((part + 1) * 100, total),
                                "total": total,
                                "previous": filename(part - 1) if part else None,
                                "next": filename(part + 1) if part + 1 < pages else None,
                            },
                        }
                        (destination / "chats" / filename(part)).write_bytes(
                            _data_html(document, "../")
                        )
        (destination / "index.html").write_bytes(_data_html(index, ""))
        return index
