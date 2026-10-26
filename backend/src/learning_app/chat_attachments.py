"""Verbatim chat files and bounded, explicitly invoked reads of submitted copies.

Uploading never parses a file or grants a course/filesystem. Every tool read is
bound to the immutable submission, actual native actor, and original byte hash.
"""

from __future__ import annotations

import base64
import hashlib
import io
import mimetypes
import os
import re
import tempfile
import zipfile
from dataclasses import asdict
from pathlib import Path
from xml.etree import ElementTree

from sqlalchemy import insert, select

from learning_app.documents import _UPLOAD_GATE, _document, attachment_metadata, documents
from learning_app.errors import AppError
from learning_app.storage.repository import _file_hash, _hash
from learning_app.workbench import corpus_schema as corpus
from learning_app.workbench import schema as native_schema
from learning_app.workbench.common import now

TOOL = "read_chat_attachment"
VERSION = "verbatim/1"
READ_CHARACTERS = 16_000


def validate_image_proof(grant, proof, scope):
    from learning_app.workbench.source_images import NATIVE_RUNTIME_SHA256

    expected = {key: value for key, value in grant.items() if key != "data"}
    expected.update({key: scope[key] for key in ("session_id", "run_id", "request_id")})
    expected["epoch"] = scope.get("native_epoch", scope.get("epoch"))
    expected.update(model=scope["settings"]["model"], runtime_sha256=NATIVE_RUNTIME_SHA256)
    if (not isinstance(proof, dict) or any(proof.get(k) != v for k, v in expected.items())
        or proof.get("provider") not in {"deepseek-official", "deepseek-account"}
        or any(not re.fullmatch(r"[a-f0-9]{64}", str(proof.get(k, "")))
               for k in ("normalized_sha256", "encoded_sha256"))
        or proof.get("normalized_ref", {}).get("attachmentId") != "sha256:" + proof["normalized_sha256"]
        or proof.get("encoded_media_type") not in {"image/png", "image/jpeg", "image/webp"}
        or any(type(proof.get(k)) is not int or proof[k] <= 0
               for k in ("encoded_bytes", "encoded_width", "encoded_height"))):
        raise AppError("CHAT_FILE_IMAGE_RECEIPT_INVALID", "页面图片编码与冻结原件或原执行不符", 403)


def wire_grants(conn, scope):
    """Only prior actual reads of files in this immutable chat context qualify."""
    ids = set(scope["grants"].get("chat_attachments", []))
    if not ids:
        return []
    result = []
    for row in conn.execute(select(corpus.query_receipts).where(
        corpus.query_receipts.c.result["attachment_id"].as_string().in_(ids)
    )).mappings():
        value = row["result"]
        if value.get("attachment_id") not in ids or not value.get("image_proof"):
            continue
        if row["native_call"].get("tool_name") != TOOL or _hash(value) != row["result_hash"]:
            raise AppError("CHAT_FILE_RECEIPT_INVALID", "附件页面历史回执不完整", 403)
        owner = conn.execute(select(native_schema.native_scopes).where(
            native_schema.native_scopes.c.run_id == row["run_id"]
        )).mappings().one()
        proof = value["image_proof"]
        grant = {k: proof[k] for k in ("kind", "source_id", "course_id", "source_sha256",
                                      "media_type", "source_version", "source_digest", "page_index")}
        original = conn.execute(select(documents).where(
            documents.c.attachment_id == value["attachment_id"]
        )).mappings().one()
        if (not is_verbatim(original) or value["sha256"] != original["payload"]["original_sha256"]
            or proof["source_digest"] != value["sha256"]
            or proof["page_index"] != value["page"] - 1):
            raise AppError("CHAT_FILE_RECEIPT_INVALID", "附件页面历史来源与原件不符", 403)
        validate_image_proof(grant, proof, {**owner, "epoch": owner["native_epoch"]})
        result.append((row, proof))
    return result


def is_verbatim(document):
    return bool(document and document["payload"].get("preparation_version") == VERSION)


def upload_file(repository, chat_id, name, source, upload_id, *, existing_document=None):
    repository.get_chat(chat_id)
    if (not isinstance(name, str) or not name or len(name) > 240
        or any(c in name for c in "/\\\x00\r\n") or any(ord(c) < 32 for c in name)):
        raise AppError("FILE_NAME_INVALID", "请选择带有效文件名的附件", 422)
    if not isinstance(upload_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", upload_id):
        raise AppError("FILE_UPLOAD_ID_INVALID", "附件上传身份无效", 422)
    aid = existing_document or "document_" + upload_id
    digest = _file_hash(source)
    with _UPLOAD_GATE:
        previous = _document(repository, aid)
        if previous:
            original = repository.resolve_attachment(aid)
            if (previous["chat_id"] != chat_id or previous["filename"] != name
                or original["content_hash"] != digest or not original["confirmed"]):
                raise AppError("DOCUMENT_UPLOAD_CONFLICT", "同一上传身份的附件不属于原对话或内容已变化", 409)
            return attachment_metadata(repository, aid)
        if existing_document:
            raise AppError("DOCUMENT_NOT_FOUND", "已保存的附件不存在，未重新创建", 404)
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        stored = repository.stage_file(source, attachment_id=aid, media_type=media_type)
        with repository._lock, repository.engine.begin() as conn:
            conn.execute(insert(documents).values(
                attachment_id=aid, chat_id=chat_id, filename=name, kind="file",
                payload={"preparation_version": VERSION,
                         "original_sha256": stored["content_hash"], "whole_document": True},
                created_at=now(),
            ))
        return attachment_metadata(repository, aid)


def freeze(conn, context):
    ids = list(dict.fromkeys(aid for message in context["messages"]
                            for aid in message.get("attachment_ids", ())))
    if not ids:
        return []
    return [row["attachment_id"] for row in conn.execute(select(documents).where(
        documents.c.attachment_id.in_(ids)
    )).mappings() if is_verbatim(row)]


def authorize(scope, arguments):
    if (not isinstance(arguments, dict) or set(arguments) != {
        "attachment_id", "sha256", "page", "offset", "mode"
    } or arguments["attachment_id"] not in scope["grants"].get("chat_attachments", [])
        or not isinstance(arguments["sha256"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", arguments["sha256"])
        or type(arguments["offset"]) is not int or arguments["offset"] < 0
        or (arguments["page"] is not None
            and (type(arguments["page"]) is not int or arguments["page"] < 1))
        or arguments["mode"] not in {"text", "image", "info"}):
        raise AppError("CHAT_ATTACHMENT_SCOPE_DENIED", "只允许读取本次提交及合法历史中的附件", 403)


def _text_slice(path, offset):
    with path.open("rb") as incoming:
        prefix = incoming.read(4096)
        encoding = "utf-16" if prefix.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        if b"\x00" in prefix and encoding != "utf-16":
            raise AppError("CHAT_FILE_READER_UNAVAILABLE", "当前读取工具不能解析此二进制格式", 422)
        incoming.seek(0)
        text = io.TextIOWrapper(incoming, encoding=encoding, errors="strict", newline="")
        remaining = offset
        while remaining:
            chunk = text.read(min(remaining, READ_CHARACTERS))
            if not chunk:
                break
            remaining -= len(chunk)
        result = text.read(READ_CHARACTERS + 1)
        return result[:READ_CHARACTERS], len(result) > READ_CHARACTERS


def _office_text(path, extension, part):
    # Read one package part, never unzip user-controlled names onto disk or
    # execute embedded code. Bounds apply to reads, not original file intake.
    with zipfile.ZipFile(path) as archive:
        if extension == ".docx":
            parts = ["word/document.xml"]
        elif extension == ".pptx":
            parts = sorted((n for n in archive.namelist()
                            if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                           key=lambda n: int(re.search(r"(\d+)\.xml$", n)[1]))
        else:
            parts = sorted(n for n in archive.namelist()
                           if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        if not 1 <= part <= len(parts):
            raise AppError("CHAT_FILE_PAGE_INVALID", "附件分段超出范围", 422)
        def xml(name):
            if archive.getinfo(name).file_size > 16 * 1024 * 1024:
                raise AppError("CHAT_FILE_PART_LIMIT", "此文档分段过大，当前读取工具无法完整处理", 422)
            with archive.open(name) as source:
                return ElementTree.parse(source).getroot()
        root = xml(parts[part - 1])
        if extension == ".xlsx":
            strings = []
            if "xl/sharedStrings.xml" in archive.namelist():
                strings = ["".join(item.itertext()) for item in xml("xl/sharedStrings.xml")]
            rows = []
            for row in root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
                cells = []
                for cell in row:
                    value = "".join(cell.itertext())
                    if cell.get("t") == "s":
                        value = strings[int(value)]
                    cells.append(value)
                rows.append("\t".join(cells))
            return "\n".join(rows), len(parts)
        paragraphs = []
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] == "p":
                paragraphs.append("".join(n.text or "" for n in node.iter()
                                          if n.tag.rsplit("}", 1)[-1] == "t"))
        return "\n".join(paragraphs), len(parts)


def _pdf_read(repository, original, page_index, *, render_to=None):
    from learning_app.workbench.reading import DocumentReader

    # The immutable object is extensionless. Only an owned alias is named PDF;
    # the existing worker still verifies the exact original hash and source.
    with tempfile.TemporaryDirectory(dir=repository.staging) as directory:
        alias = Path(directory) / "submitted.pdf"
        os.link(original["path"], alias)
        return DocumentReader._run({**original, "path": alias}, page_index, render_to=render_to)


def read_file(repository, document, original, arguments, *, encode_image=None):
    result = {"attachment_id": original["attachment_id"], "sha256": original["content_hash"],
              "filename": document["filename"], "size_bytes": original["byte_length"],
              "offset": arguments["offset"], "page": arguments["page"], "content_read": False}
    if arguments["mode"] == "info":
        return {**result, "status": "metadata", "text": ""}
    extension = Path(document["filename"]).suffix.lower()
    if arguments["mode"] == "image" and extension != ".pdf":
        raise AppError("CHAT_FILE_IMAGE_UNSUPPORTED", "页面图片读取只支持用户提交的PDF，不提取音视频画面", 422)
    if extension in {".mp4", ".mkv", ".mov", ".avi", ".webm", ".mp3", ".wav", ".m4a", ".ogg", ".flac"}:
        raise AppError("CHAT_FILE_READER_UNAVAILABLE", "音视频原件已保存；附件读取不会自动抽帧或转写", 422)
    offset = arguments["offset"]
    if extension == ".pdf":
        page = arguments["page"]
        if page is None:
            info = _pdf_read(repository, original, None)
            return {**result, **info, "status": "metadata", "text": "请指定1起算页码按需读取；尚未读取页面内容"}
        if arguments["mode"] == "image":
            if encode_image is None:
                raise AppError("MODEL_IMAGE_UNSUPPORTED", "所选模型不能读取页面图片，未更换模型", 422)
            with tempfile.TemporaryDirectory(dir=repository.staging) as directory:
                target = Path(directory) / "page.png"
                info = _pdf_read(repository, original, page - 1, render_to=target)
                raw = target.read_bytes()
                proof = encode_image(page - 1, raw)
            return {**result, "status": "image", "content_read": True,
                    "page_count": info["page_count"], "image_attachment": proof["normalized_ref"],
                    "image_proof": proof}
        info = _pdf_read(repository, original, page - 1)
        text = info["text"]
        result["page_count"] = info["page_count"]
        result["representation"] = "native PDF text layer only; page image not read"
        result["text_status"] = info["text_status"]
    elif extension in {".docx", ".pptx", ".xlsx"}:
        try:
            text, result["part_count"] = _office_text(original["path"], extension, arguments["page"] or 1)
        except (KeyError, ValueError, IndexError, RuntimeError, NotImplementedError, OSError) as exc:
            raise AppError("CHAT_FILE_READER_UNAVAILABLE", "Office文件分段无法读取，原件仍保留", 422) from exc
        result["page"] = arguments["page"] or 1
        result["representation"] = "text_only; layout, embedded images and macros not read"
    else:
        if arguments["page"] is not None:
            raise AppError("CHAT_FILE_PAGE_INVALID", "文本附件请使用offset续读", 422)
        text, more = _text_slice(original["path"], offset)
        return {**result, "status": "text", "content_read": True, "text": text,
                "has_more": more, "next_offset": offset + len(text) if more else None}
    return {**result, "status": "text", "content_read": True,
            "text": text[offset:offset + READ_CHARACTERS],
            "has_more": len(text) > offset + READ_CHARACTERS,
            "next_offset": offset + READ_CHARACTERS if len(text) > offset + READ_CHARACTERS else None}


def invoke(port, call, arguments):
    repository = port.repository
    identity = _hash(asdict(call))
    with repository.engine.connect() as conn:
        scope = port._authorize(conn, call, arguments)
        prior = conn.execute(select(corpus.query_receipts).where(
            corpus.query_receipts.c.effect_id == identity
        )).mappings().first()
        if prior:
            if prior["native_call"] != asdict(call) or _hash(prior["result"]) != prior["result_hash"]:
                raise AppError("CHAT_FILE_RECEIPT_INVALID", "附件读取回执校验失败", 409)
            return prior["result"]
    original = repository.resolve_attachment(arguments["attachment_id"])
    document = _document(repository, original["attachment_id"])
    if (not is_verbatim(document) or original["content_hash"] != arguments["sha256"]
        or document["payload"]["original_sha256"] != arguments["sha256"]):
        raise AppError("CHAT_FILE_VERSION_MISMATCH", "附件与本次提交的原件版本不一致", 409)
    def encode(page, raw):
        port.authorize(call, arguments)
        grant = {"source_id": f"{original['attachment_id']}_page_{page}", "kind": "document_page",
                 "course_id": scope["course_id"],
                 "source_version": original["content_hash"], "source_digest": original["content_hash"],
                 "page_index": page, "media_type": "image/png",
                 "source_sha256": hashlib.sha256(raw).hexdigest(),
                 "data": base64.b64encode(raw).decode("ascii")}
        proofs = port.document_images.prepare(scope, [grant])
        if not isinstance(proofs, list) or len(proofs) != 1:
            raise AppError("CHAT_FILE_IMAGE_RECEIPT_INVALID", "页面图片编码未返回完整回执", 409)
        validate_image_proof(grant, proofs[0], scope)
        return proofs[0]
    can_image = port.document_images.prepare and port.document_images.supports_images(scope["settings"]["model"])
    try:
        result = read_file(repository, document, original, arguments,
                           encode_image=encode if can_image else None)
    except (UnicodeError, zipfile.BadZipFile, ElementTree.ParseError):
        result = {"attachment_id": original["attachment_id"], "sha256": original["content_hash"],
                  "filename": document["filename"], "status": "unreadable", "content_read": False,
                  "message": "当前工具无法解析此文件或编码；原件仍保留，不能声称已读取内容"}
    if result.get("image_attachment"):
        result["render_metadata_version"] = 2
        result["render_metadata"] = {key: value for key, value in result.items()
                                     if key not in {"image_attachment", "image_proof", "render_metadata_version"}}
    with repository._lock, repository.engine.begin() as conn:
        port._authorize(conn, call, arguments)
        conn.execute(insert(corpus.query_receipts).values(
            effect_id=identity, run_id=call.run_id, native_call=asdict(call),
            result=result, result_hash=_hash(result), created_at=now(),
        ))
    return result
