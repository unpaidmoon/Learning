"""Project lossless codepoint windows, not an upstream transcript serializer.

No retrieval or model execution. Unit/version/time authority belongs to the
application. Text offsets never imply an interpolated word timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib


@dataclass(frozen=True)
class TextUnit:
    unit_id: str
    asset_id: str
    source_version: str
    content_revision: str
    content_role: str
    text: str
    page_index: int | None = None
    segment_id: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None

    def __post_init__(self):
        if not all(
            (
                self.unit_id,
                self.asset_id,
                self.source_version,
                self.content_revision,
                self.content_role,
            )
        ):
            raise ValueError("SOURCE_UNIT_IDENTITY_INVALID")
        if self.page_index is not None and (
            type(self.page_index) is not int or self.page_index < 0
        ):
            raise ValueError("SOURCE_LOCATOR_INVALID")
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("SOURCE_TIME_INVALID")
        if self.start_ms is not None and (
            type(self.start_ms) is not int
            or type(self.end_ms) is not int
            or not 0 <= self.start_ms < self.end_ms
        ):
            raise ValueError("SOURCE_TIME_INVALID")
        if self.segment_id is not None and self.start_ms is None:
            raise ValueError("SOURCE_TIME_INVALID")
        text_digest(self.text)


@dataclass(frozen=True)
class TextWindow:
    unit_id: str
    asset_id: str
    source_version: str
    content_revision: str
    content_role: str
    start_codepoint: int
    end_codepoint: int
    text: str
    source_text_sha256: str
    window_sha256: str
    page_index: int | None
    segment_id: str | None
    start_ms: int | None
    end_ms: int | None


def text_digest(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("SOURCE_TEXT_INVALID")
    try:
        return hashlib.sha256(text.encode("utf-8", errors="strict")).hexdigest()
    except UnicodeEncodeError as error:
        raise ValueError("SOURCE_TEXT_INVALID_UTF8") from error


def split_text_unit(
    unit: TextUnit, max_codepoints: int = 24_000, *, prefer_lines=True
) -> tuple[TextWindow, ...]:
    if type(max_codepoints) is not int or max_codepoints < 1:
        raise ValueError("SOURCE_WINDOW_BUDGET_INVALID")
    source_hash = text_digest(unit.text)
    windows = []
    start = 0
    while start < len(unit.text):
        end = min(len(unit.text), start + max_codepoints)
        if prefer_lines and end < len(unit.text):
            # Include the chosen newline in exactly one owning window.
            boundary = unit.text.rfind("\n", start + max_codepoints // 2, end)
            if boundary >= start:
                end = boundary + 1
        text = unit.text[start:end]
        windows.append(
            TextWindow(
                unit.unit_id,
                unit.asset_id,
                unit.source_version,
                unit.content_revision,
                unit.content_role,
                start,
                end,
                text,
                source_hash,
                text_digest(text),
                unit.page_index,
                unit.segment_id,
                unit.start_ms,
                unit.end_ms,
            )
        )
        start = end
    return tuple(windows)


def reassemble_text_unit(
    unit: TextUnit, windows: tuple[TextWindow, ...] | list[TextWindow]
) -> str:
    identity = (
        unit.unit_id,
        unit.asset_id,
        unit.source_version,
        unit.content_revision,
        unit.content_role,
        unit.page_index,
        unit.segment_id,
        unit.start_ms,
        unit.end_ms,
    )
    source_hash = text_digest(unit.text)
    cursor = 0
    parts = []
    for window in windows:
        actual = (
            window.unit_id,
            window.asset_id,
            window.source_version,
            window.content_revision,
            window.content_role,
            window.page_index,
            window.segment_id,
            window.start_ms,
            window.end_ms,
        )
        if actual != identity or window.source_text_sha256 != source_hash:
            raise ValueError("SOURCE_WINDOW_VERSION_CONFLICT")
        if window.start_codepoint != cursor or not cursor < window.end_codepoint <= len(
            unit.text
        ):
            raise ValueError("SOURCE_WINDOW_COVERAGE_INVALID")
        if (
            window.text != unit.text[cursor : window.end_codepoint]
            or text_digest(window.text) != window.window_sha256
        ):
            raise ValueError("SOURCE_WINDOW_HASH_CONFLICT")
        parts.append(window.text)
        cursor = window.end_codepoint
    if cursor != len(unit.text):
        raise ValueError("SOURCE_WINDOW_COVERAGE_INVALID")
    result = "".join(parts)
    if text_digest(result) != source_hash:
        raise ValueError("SOURCE_WINDOW_HASH_CONFLICT")
    return result
