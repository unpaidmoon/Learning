"""Read native document text on request, without a corpus or OCR worker."""

import os
import re

from learning_app.course_files import CourseFileStore, _is_link
from learning_app.errors import AppError

from .models import DocumentRef
from .reading import DocumentReader
from .sources import SourceStore


def current_selection(repository, value, question):
    if not re.search(r"这[页份篇个段章]|当前|本页|本文|这份资料", question):
        return None
    if not isinstance(value, dict) or set(value) - {
        "course_id",
        "asset_id",
        "source_version",
        "page_index",
    }:
        raise AppError("CURRENT_DOCUMENT_INVALID", "当前资料范围无效", 422)
    course, asset, version = (value.get(key) for key in ("course_id", "asset_id", "source_version"))
    if not all(
        isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", item)
        for item in (course, asset, version)
    ):
        raise AppError("CURRENT_DOCUMENT_INVALID", "当前资料身份无效", 422)
    sources = SourceStore(repository)
    source = sources._source(course, asset, version)
    if source["path"].suffix.lower() == ".pdf":
        page_index = value.get("page_index", 0)
        if not isinstance(page_index, int) or page_index < 0:
            raise AppError("CURRENT_DOCUMENT_INVALID", "当前页码无效", 422)
        page = DocumentReader(repository).page(course, asset, version, page_index)
        if not page.text.strip():
            raise AppError("PDF_TEXT_UNAVAILABLE", "当前页没有文字层，请截图提问；不会进行OCR", 422)
        if len(page.text) > 96_000:
            raise AppError("NATIVE_SOURCE_SIZE_LIMIT", "当前页过长，请选择具体文字", 422)
        return sources._save_verified(
            course,
            DocumentRef(
                asset_id=asset,
                source_version=version,
                source_digest=page.source_digest,
                page_index=page_index,
                page_label=page.page_label,
                parser_version=page.parser_version,
                start=0,
                end=len(page.text),
            ),
            page.text,
            "original",
        )
    text, _ = sources.text(course, asset, version)
    if len(text) > 96_000:
        raise AppError("NATIVE_SOURCE_SIZE_LIMIT", "资料过长，请选择具体文字", 422)
    return sources.capture_text(course, asset, version, 0, len(text))


def search(repository, course, query):
    files, sources = CourseFileStore(repository), SourceStore(repository)
    root = files._root(course)
    results, pages_read, files_read = [], 0, 0
    for directory, folders, names in os.walk(root, followlinks=False):
        from pathlib import Path

        directory = Path(directory)
        folders[:] = [name for name in folders if not _is_link(directory / name)]
        for name in sorted(names):
            path = directory / name
            if _is_link(path) or path.suffix.lower() not in {".pdf", ".txt", ".md", ".markdown"}:
                continue
            if files_read >= 64 or pages_read >= 64 or len(results) >= 4:
                return results
            checked = files._path(root, path.relative_to(root).as_posix())
            item = files._register(course, root, checked)
            files_read += 1
            asset, version = item["asset_id"], item["source_version"]
            reader = DocumentReader(repository)
            total = (
                reader.info(course, asset, version).page_count
                if path.suffix.lower() == ".pdf"
                else 1
            )
            for index in range(total):
                if pages_read >= 64 or len(results) >= 4:
                    return results
                pages_read += 1
                if path.suffix.lower() == ".pdf":
                    page = reader.page(course, asset, version, index)
                    body, parser, label = page.text, page.parser_version, page.page_label
                else:
                    from .sources import TEXT_PARSER_VERSION

                    body, _ = sources.text(course, asset, version)
                    parser, label = TEXT_PARSER_VERSION, None
                offset = body.casefold().find(query.casefold())
                if offset < 0 and query.casefold() not in name.casefold():
                    continue
                start = max(0, offset - 300)
                end = min(len(body), start + 2400)
                if not body[start:end].strip():
                    continue
                snapshot = sources._save_verified(
                    course,
                    DocumentRef(
                        asset_id=asset,
                        source_version=version,
                        source_digest=item["content_hash"],
                        page_index=index,
                        page_label=label,
                        start=start,
                        end=end,
                        parser_version=parser,
                    ),
                    body[start:end],
                    "original",
                )
                results.append(
                    {
                        "snapshot": snapshot,
                        "label": f"{name} · 第{index + 1}页",
                        "live_document": True,
                    }
                )
    return results
