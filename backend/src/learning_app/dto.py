"""Public application DTOs; internal protocol envelopes never cross this boundary."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class InputDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorResponse(BaseModel):
    code: str
    message: str


class SessionResponse(BaseModel):
    csrf_token: str


class CourseInput(InputDTO):
    title: str = Field(min_length=1, max_length=200)


class CourseResponse(BaseModel):
    course_id: str
    title: str


class ChatResponse(BaseModel):
    chat_id: str
    course_id: str
    messages: list["MessageResponse"] = Field(default_factory=list)
    generations: list["GenerationResponse"] = Field(default_factory=list)


class MessageResponse(BaseModel):
    message_id: str
    role: Literal["user", "assistant"]
    text: str = ""
    reasoning: str = ""
    status: str = "completed"
    attachment_ids: list[str] = Field(default_factory=list)
    generation_id: str | None = None


class AttachmentInput(InputDTO):
    data_base64: str = Field(max_length=16_000_000)
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    confirmed: Literal[True]


class AttachmentResponse(BaseModel):
    attachment_id: str
    sha256: str
    media_type: str
    size_bytes: int
    confirmed: bool


class SubmissionInput(InputDTO):
    chat_id: str
    submission_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(max_length=100_000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=8)
    conditions: list[str] = Field(default_factory=list)
    corrections: list[str] = Field(default_factory=list)
    model: str = "deepseek-flash"
    thinking: Literal["none", "low", "medium", "high", "xhigh", "max"] = "high"


class SubmissionResponse(BaseModel):
    submission_id: str
    generation_id: str
    user_message_id: str
    assistant_message_id: str
    state: str
    created: bool


class UsageResponse(BaseModel):
    status: Literal["known", "unknown"] = "unknown"
    raw: dict | None = None
    fields: dict[str, bool] = Field(default_factory=dict)
    input_tokens: int | None = None
    output_tokens: int | None = None


class GenerationResponse(BaseModel):
    generation_id: str
    submission_id: str
    chat_id: str
    state: str
    body: str = ""
    reasoning: str = ""
    cancel_requested: bool = False
    event_watermark: int = 0
    usage: UsageResponse = Field(default_factory=UsageResponse)
    error_code: str | None = None


class BusinessEvent(BaseModel):
    generation_id: str
    event_watermark: int
    state: str
    body: str
    reasoning: str
