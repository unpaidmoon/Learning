"""Bounded, local document preparation. PDFium runs only in an owned worker.

The whole selected PDF is represented by all rendered pages; any bound failure
rejects the document rather than accepting a silently truncated representation.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import subprocess
import sys
import threading
from pathlib import Path

from learning_app.errors import AppError
from learning_app.processes import OwnedProcessGroup

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
MAX_TEXT_CHARACTERS = 200_000
MAX_PDF_PAGES = 32
MAX_PDF_PIXELS = 80_000_000
MAX_RENDERED_BYTES = 12 * 1024 * 1024
PDF_EDGE = 1600
PDF_TIMEOUT_SECONDS = 60
DOCUMENT_TYPES = {"application/pdf", "text/plain", "text/markdown"}
_gate = threading.Lock()


def validate_original_bytes(data: bytes, media_type: str) -> None:
    if media_type not in DOCUMENT_TYPES:
        raise AppError("DOCUMENT_TYPE_UNSUPPORTED", "仅支持PDF、TXT和Markdown文档", 422)
    maximum = MAX_PDF_BYTES if media_type == "application/pdf" else MAX_TEXT_BYTES
    if not data or len(data) > maximum:
        raise AppError("DOCUMENT_SIZE_LIMIT", "文件为空或超过当前文档大小上限", 422)
    if media_type == "application/pdf":
        if not data.startswith(b"%PDF-"):
            raise AppError("DOCUMENT_INVALID", "文件内容不是有效PDF", 422)
    else:
        try:
            text = data.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            raise AppError("DOCUMENT_ENCODING_UNSUPPORTED", "文本文件须为UTF-8编码", 422) from exc
        if "\x00" in text:
            raise AppError("DOCUMENT_INVALID", "文本中包含二进制内容", 422)
        if len(text) > MAX_TEXT_CHARACTERS:
            raise AppError("DOCUMENT_TEXT_LIMIT", "文本超过200000字符，未截断或发送", 422)


def _render_pdf(source: Path, directory: Path) -> dict:
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(source)
    except pdfium.PdfiumError as exc:
        raise AppError("DOCUMENT_PDF_UNREADABLE", "PDF无法读取或需要密码", 422) from exc
    pages = []
    pixels, byte_count = 0, 0
    try:
        count = len(document)
        if not 0 < count <= MAX_PDF_PAGES:
            raise AppError("DOCUMENT_PAGE_LIMIT", "PDF须为1至32页；未截取部分页面", 422)
        for index in range(count):
            page = document[index]
            try:
                width, height = page.get_size()
                if not all(
                    math.isfinite(value) and 0 < value <= 14400 for value in (width, height)
                ):
                    raise AppError("DOCUMENT_PAGE_INVALID", "PDF页面尺寸无效", 422)
                scale = min(2.0, PDF_EDGE / max(width, height))
                estimate = math.ceil(width * scale) * math.ceil(height * scale)
                if pixels + estimate > MAX_PDF_PIXELS:
                    raise AppError("DOCUMENT_RENDER_LIMIT", "整份PDF的页面像素超过处理上限", 422)
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil().convert("RGB")
                    pixels += image.width * image.height
                    output = io.BytesIO()
                    image.save(output, "JPEG", quality=90, optimize=True)
                    content = output.getvalue()
                    byte_count += len(content)
                    if byte_count > MAX_RENDERED_BYTES:
                        raise AppError(
                            "DOCUMENT_RENDER_LIMIT", "整份PDF的页面图片超过处理上限", 422
                        )
                    name = f"page-{index + 1:04d}.jpg"
                    (directory / name).write_bytes(content)
                    pages.append(
                        {
                            "page": index + 1,
                            "file": name,
                            "width": image.width,
                            "height": image.height,
                            "sha256": hashlib.sha256(content).hexdigest(),
                        }
                    )
                    image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()
        return {
            "kind": "pdf",
            "page_count": count,
            "character_count": None,
            "text": "",
            "pages": pages,
        }
    finally:
        document.close()


def prepare_document(data: bytes, media_type: str, filename: str, directory: Path) -> dict:
    validate_original_bytes(data, media_type)
    if media_type != "application/pdf":
        text = data.decode("utf-8-sig", errors="strict")
        return {
            "kind": "markdown" if media_type == "text/markdown" else "text",
            "page_count": None,
            "character_count": len(text),
            "text": text,
            "pages": [],
        }
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "selected.pdf"
    source.write_bytes(data)
    group = OwnedProcessGroup("document-pdf")
    with _gate:
        try:
            process = group.spawn(
                [
                    sys.executable,
                    "-m",
                    "learning_app.document_processing",
                    "--worker",
                    "--source",
                    str(source),
                    "--directory",
                    str(directory),
                ],
                cwd=Path(__file__).resolve().parents[3],
            )
            try:
                stdout, _stderr = process.communicate(timeout=PDF_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                raise AppError(
                    "DOCUMENT_PROCESSING_TIMEOUT", "PDF处理超时，未加入或发送部分内容", 422
                ) from exc
            try:
                value = json.loads(stdout)
            except (ValueError, TypeError) as exc:
                raise AppError(
                    "DOCUMENT_PROCESSING_FAILED", "PDF处理未完成，请检查文件", 422
                ) from exc
            if process.returncode or not value.get("ok"):
                code = value.get("code", "DOCUMENT_PROCESSING_FAILED")
                messages = {
                    "DOCUMENT_PAGE_LIMIT": "PDF须为1至32页；未截取部分页面",
                    "DOCUMENT_RENDER_LIMIT": "整份PDF超过页面渲染上限，未截断或发送",
                    "DOCUMENT_PDF_UNREADABLE": "PDF无法读取或需要密码",
                    "DOCUMENT_PAGE_INVALID": "PDF页面尺寸无效",
                }
                raise AppError(
                    code if code in messages else "DOCUMENT_PROCESSING_FAILED",
                    messages.get(code, "PDF处理未完成，请检查文件"),
                    422,
                )
            result = value["result"]
            if result.get("page_count") != len(result.get("pages", [])):
                raise AppError("DOCUMENT_PROCESSING_FAILED", "PDF页面处理不完整", 422)
            for number, page in enumerate(result["pages"], 1):
                path = directory / page["file"]
                if (
                    page["page"] != number
                    or path.name != f"page-{number:04d}.jpg"
                    or path.parent != directory
                ):
                    raise AppError("DOCUMENT_PROCESSING_FAILED", "PDF页面处理结果无效", 422)
                if (
                    not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != page["sha256"]
                ):
                    raise AppError("DOCUMENT_PROCESSING_FAILED", "PDF页面完整性校验失败", 422)
                page["path"] = str(path)
            return result
        finally:
            group.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = _render_pdf(args.source, args.directory)
    except AppError as exc:
        print(json.dumps({"ok": False, "code": exc.code}), flush=True)
        return 1
    except Exception:
        print(json.dumps({"ok": False, "code": "DOCUMENT_PROCESSING_FAILED"}), flush=True)
        return 1
    print(json.dumps({"ok": True, "result": result}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
