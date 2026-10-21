"""Application-owned execution contracts; no Remote or provider objects leak out."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ExecutionBinding:
    conversation_id: str
    submission_id: str
    generation_id: str
    epoch: int
    session_id: str
    request_id: str
    native_floor: int = -1
    native_turn: int | None = None
    accepted: bool = False
    status: str = "prepared"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ExecutionBinding:
        return cls(**{key: value[key] for key in cls.__dataclass_fields__ if key in value})


@dataclass(frozen=True)
class ApprovedAttachment:
    attachment_id: str
    path: Path
    sha256: str
    media_type: str = "image/png"


@dataclass
class NormalizedEvent:
    event_id: str
    seq: int
    generation_id: str
    epoch: int
    kind: str
    text: str = ""
    status: str | None = None
    native_seq: int | None = None
    attempt_id: str | None = None
    raw_usage: dict[str, Any] | None = None
    usage_presence: dict[str, bool] | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExecutionError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
