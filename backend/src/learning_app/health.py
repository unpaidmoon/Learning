from typing import Literal

from pydantic import BaseModel

from learning_app import __version__


class Capabilities(BaseModel):
    harness: Literal["ready", "not_configured", "unavailable"] = "not_configured"
    question_answering: Literal["ready", "not_configured", "unavailable"] = "not_configured"
    persistence: Literal["ready", "unavailable"] = "unavailable"
    video: Literal["ready", "unavailable", "not_implemented"] = "not_implemented"
    transcription: Literal["available", "unavailable", "not_implemented"] = "not_implemented"
    knowledge_base: Literal["available", "unavailable", "not_implemented"] = "not_implemented"


class HealthResponse(BaseModel):
    contract_version: Literal["1"] = "1"
    app_version: str = __version__
    instance_id: str
    ready: bool
    configured_model: str = "deepseek-flash"
    configured_thinking: str = "high"
    capabilities: Capabilities
