"""Finite local preparation contracts, distinct from remote model executions."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, model_validator

from .models import Contract, Digest, Identity

PreparationState = Literal[
    "queued", "running", "paused", "needs_source", "failed", "completed", "cancelled"
]


class PreparationCreate(Contract):
    kind: Literal["pdf_ocr", "asr"]
    asset_id: Identity
    source_version: Identity
    scope: Literal["full", "unit"] = "full"
    unit_index: int | None = Field(default=None, ge=0)
    priority_unit: int | None = Field(default=None, ge=0)
    ocr_layout: Literal["auto", "text_block"] = "auto"
    language: Literal["zh", "en"] | None = None

    @model_validator(mode="after")
    def unit_scope(self) -> PreparationCreate:
        if (self.scope == "unit") != (self.unit_index is not None):
            raise ValueError("unit preparation requires exactly one unit index")
        return self


class PreparationControl(Contract):
    operation_id: Identity
    expected_revision: int = Field(ge=0)


class ReadingFocusInput(Contract):
    reader_client_id: Identity
    reader_epoch: Identity
    focus_sequence: int = Field(ge=0)
    request_id: Identity
    page_index: int = Field(ge=0)


class AudioFocusInput(Contract):
    reader_client_id: Identity
    reader_epoch: Identity
    focus_sequence: int = Field(ge=0)
    request_id: Identity
    position_ms: int = Field(ge=0)


class AudioFocusState(Contract):
    asset_id: Identity
    source_version: Identity
    reader_epoch: Identity


class PreparationUnit(Contract):
    unit_index: int = Field(ge=0)
    state: Literal["pending", "running", "completed", "failed", "source_missing"]
    priority: int
    error_code: str | None
    result_digest: Digest | None
    local_attempts: int = Field(ge=0)
    progress: dict[str, JsonValue] | None


class PreparationView(Contract):
    job_id: Identity
    course_id: Identity
    kind: Literal["pdf_ocr", "asr", "platform_download", "platform_subtitles"]
    asset_id: Identity
    source_version: Identity
    source_digest: Digest
    scope: Literal["full", "unit", "range"]
    parameters: dict[str, JsonValue]
    state: PreparationState
    revision: int
    epoch: int
    units: list[PreparationUnit]
    created_at: str
    updated_at: str


class PreparationProgress(Contract):
    job_id: Identity
    course_id: Identity
    asset_id: Identity
    source_version: Identity
    kind: Literal["pdf_ocr", "asr", "platform_download", "platform_subtitles"]
    scope: Literal["full", "unit", "range"]
    state: PreparationState
    revision: int
    units_total: int = Field(ge=0)
    units_completed: int = Field(ge=0)
    units_failed: int = Field(ge=0)
    units_missing: int = Field(ge=0)
    active_unit_index: int | None
    updated_at: str


class CurrentPagePreparation(Contract):
    batch: PreparationProgress | None
    current: PreparationProgress
    limitation_code: str | None = None
