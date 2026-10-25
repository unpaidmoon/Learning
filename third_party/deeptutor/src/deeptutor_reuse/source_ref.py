"""Unmodified upstream DTO excerpt; project authorization is external."""

from pydantic import BaseModel, ConfigDict, Field


class SourcePageError(ValueError):
    """A safe, user-facing source resolution failure."""



class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kb_ref: str
    document_key: str
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_index: int = Field(ge=0)
    page_label: str = ""
    parser_signature: str = ""
    node_id: str = ""
    crop: tuple[float, float, float, float] | None = None
