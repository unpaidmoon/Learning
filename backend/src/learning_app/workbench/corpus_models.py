"""Course corpus controls belong to the user, never to a model tool."""

from pydantic import Field, field_validator

from .models import Contract, Identity


class CorpusPolicy(Contract):
    operation_id: Identity
    expected_revision: int = Field(ge=0)
    exclusions: list[str] = Field(max_length=256)

    @field_validator("exclusions")
    @classmethod
    def relative_paths(cls, paths):
        from pathlib import PureWindowsPath

        result = []
        for raw in paths:
            path = raw.replace("\\", "/").strip().rstrip("/")
            if (
                not path
                or len(path) > 1000
                or path.startswith("/")
                or PureWindowsPath(path).drive
                or any(p in {".", "..", ""} for p in path.split("/"))
            ):
                raise ValueError("exclusions must be relative file or directory paths")
            result.append(path)
        return sorted(set(result), key=str.casefold)


class CorpusDocument(Contract):
    document_id: Identity
    relative_path: str
    asset_id: Identity
    source_version: Identity
    cursor: int
    total: int | None
    state: str
    error_code: str | None


class CorpusPolicyReceipt(Contract):
    revision: int
    exclusions: list[str]


class CorpusStatus(Contract):
    revision: int
    exclusions: list[str]
    scan_state: str
    scan_error: str | None
    documents: list[CorpusDocument]
    has_more: bool
