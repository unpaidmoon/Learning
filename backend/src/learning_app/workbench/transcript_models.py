"""Original ASR and user revisions have separate identities and attribution."""

from typing import Literal

from pydantic import Field, model_validator

from .models import Contract, Digest, Identity


class SubtitleSegment(Contract):
    segment_id: Identity
    revision_id: Identity
    raw_revision_id: Identity
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    text: str = Field(max_length=100_000)
    raw_text: str = Field(max_length=100_000)
    origin: Literal["original", "user_note"]


class TranscriptView(Contract):
    track_id: Identity
    course_id: Identity
    asset_id: Identity
    source_version: Identity
    source_digest: Digest
    source_type: Literal["local_asr", "platform_cc", "platform_ai"] = "local_asr"
    edit_head_id: Identity | None
    audio_start_ms: int | None
    audio_end_ms: int | None
    units_total: int | None
    units_ready: list[int]
    missing_units: list[int]
    segments: list[SubtitleSegment]


class SubtitleChange(Contract):
    segment_id: Identity
    raw_revision_id: Identity
    text: str = Field(max_length=100_000)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def positive(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("subtitle end must follow its start")
        return self


class TranscriptEdit(Contract):
    operation_id: Identity
    expected_edit_head_id: Identity | None
    changes: list[SubtitleChange] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique(self):
        keys = [item.segment_id for item in self.changes]
        if len(set(keys)) != len(keys):
            raise ValueError("subtitle changes must have unique segments")
        return self


class TranscriptEditReceipt(Contract):
    revision_id: Identity
    head_revision_id: Identity | None
    disposition: Literal["published", "conflict"]


class SubtitleCapture(Contract):
    segment_id: Identity
    revision_id: Identity
