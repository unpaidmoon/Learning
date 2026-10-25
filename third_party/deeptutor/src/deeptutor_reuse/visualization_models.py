"""Original envelope DTOs without the optional jsonschema validation import."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class RendererRef(BaseModel):
    id: str
    version: str
    target: Literal["native", "iframe", "artifact"]
    native_renderer: str = ""
    entry_url: str = ""



class VisualizationPayload(BaseModel):
    format: str
    data: Any



class VisualizationPresentation(BaseModel):
    title: str = ""
    description: str = ""
    alt_text: str = ""
    aspect_ratio: str = ""



class VisualizationInteraction(BaseModel):
    events: list[str] = Field(default_factory=list)



class VisualizationEnvelope(BaseModel):
    """Versioned result understood by the generic frontend canvas."""

    schema_version: str = "deeptutor.visualization/v1"
    render_type: str
    renderer: RendererRef
    payload: VisualizationPayload
    presentation: VisualizationPresentation = Field(default_factory=VisualizationPresentation)
    interaction: VisualizationInteraction = Field(default_factory=VisualizationInteraction)
    fallback: dict[str, Any] = Field(default_factory=dict)
