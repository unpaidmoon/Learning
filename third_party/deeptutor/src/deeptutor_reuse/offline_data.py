"""Bounded project data policy over the original visualization envelope.

No HTML/JS execution, URLs, business bridge, storage, provider or file IO.
The hosting renderer must also enforce the package CSP and local asset manifest.
"""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .visualization_models import VisualizationEnvelope
from .visualization_refs import find_remote_schema_ref


class StrictData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def validate_math(value: str) -> str:
    if any(token in value.lower() for token in ("\\href", "\\url", "\\includegraphics", "\\html")):
        raise ValueError("MATH_EXTERNAL_OR_HTML_COMMAND")
    return value


class Inline(StrictData):
    kind: Literal["text", "math"]
    value: str = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def safe_math(self):
        if self.kind == "math":
            validate_math(self.value)
        return self


class TextPayload(StrictData):
    body: str = Field(max_length=100_000)
    paragraphs: list[list[Inline]] = Field(max_length=1000)
    display_math: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("display_math")
    @classmethod
    def safe_displays(cls, values):
        return [validate_math(v) for v in values]


class FigurePayload(StrictData):
    description: str
    asset_path: str
    asset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    alt: str
    caption: str

    @field_validator("asset_path")
    @classmethod
    def local_asset(cls, value):
        if (not re.fullmatch(r"assets/[A-Za-z0-9._/-]+", value)
                or any(p in {"", ".", ".."} for p in value.split("/"))):
            raise ValueError("ASSET_OUTSIDE_PACKAGE")
        return value


class RangeParameter(StrictData):
    label: str = Field(max_length=160)
    min: float = Field(ge=-1000, le=1000)
    max: float = Field(ge=-1000, le=1000)
    step: float = Field(gt=0, le=1000)
    initial: float = Field(ge=-1000, le=1000)

    @model_validator(mode="after")
    def valid_range(self):
        if not self.min < self.max or not self.min <= self.initial <= self.max:
            raise ValueError("PARAMETER_RANGE_INVALID")
        if (self.max - self.min) / self.step > 10_000:
            raise ValueError("PARAMETER_RANGE_TOO_LARGE")
        return self


class PlotData(StrictData):
    kind: Literal["quadratic_tangent"]
    a: RangeParameter
    x0: RangeParameter


def validate_offline_visualization(value: dict | VisualizationEnvelope) -> VisualizationEnvelope:
    raw = value.model_dump(mode="json") if isinstance(value, VisualizationEnvelope) else value
    allowed = set(VisualizationEnvelope.model_fields)
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError("VISUALIZATION_FIELDS_NOT_ALLOWED")
    if not isinstance(raw.get("renderer"), dict) or set(raw["renderer"]) - {"id", "version", "target", "native_renderer", "entry_url"}:
        raise ValueError("RENDERER_FIELDS_NOT_ALLOWED")
    if not isinstance(raw.get("payload"), dict) or set(raw["payload"]) != {"format", "data"}:
        raise ValueError("VISUALIZATION_PAYLOAD_INVALID")
    result = VisualizationEnvelope.model_validate(raw)
    if (result.schema_version != "deeptutor.visualization/v1" or result.render_type != "quadratic_tangent"
            or result.renderer.id != "offline.function-plot" or result.renderer.version != "1.0.0"
            or result.renderer.target != "native" or result.renderer.native_renderer != "quadratic_tangent"
            or result.renderer.entry_url or result.payload.format != "learning.parameter-plot/v1"
            or result.interaction.events or result.fallback):
        raise ValueError("RENDERER_NOT_ALLOWED")
    if find_remote_schema_ref(result.payload.data):
        raise ValueError("REMOTE_SCHEMA_REF_NOT_ALLOWED")
    data = json.loads(result.payload.data) if isinstance(result.payload.data, str) else result.payload.data
    result.payload.data = PlotData.model_validate(data).model_dump(mode="json")
    return result
