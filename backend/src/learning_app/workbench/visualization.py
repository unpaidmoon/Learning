"""Closed data renderers over the fixed DeepTutor envelope; no generated code or URLs."""

from __future__ import annotations

import ast
import math
from typing import Annotated, Literal

from deeptutor_reuse.visualization_models import (
    RendererRef,
    VisualizationEnvelope,
    VisualizationInteraction,
    VisualizationPayload,
    VisualizationPresentation,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

FUNCTIONS = frozenset({"sin", "cos", "tan", "exp", "log", "sqrt", "abs"})


class DataContract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AxisRange(DataContract):
    min: float = Field(ge=-10_000, le=10_000)
    max: float = Field(ge=-10_000, le=10_000)

    @model_validator(mode="after")
    def ordered(self):
        if self.max - self.min < 0.0001:
            raise ValueError("VISUALIZATION_AXIS_RANGE_INVALID")
        return self


class PlotParameter(DataContract):
    symbol: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,15}$")
    label: str = Field(min_length=1, max_length=80)
    min: float = Field(ge=-10_000, le=10_000)
    max: float = Field(ge=-10_000, le=10_000)
    step: float = Field(gt=0, le=10_000)
    initial: float = Field(ge=-10_000, le=10_000)

    @model_validator(mode="after")
    def usable(self):
        if self.symbol in FUNCTIONS | {"x", "pi", "e"}:
            raise ValueError("VISUALIZATION_PARAMETER_RESERVED")
        if not self.min < self.max or not self.min <= self.initial <= self.max:
            raise ValueError("VISUALIZATION_PARAMETER_RANGE_INVALID")
        if (self.max - self.min) / self.step > 20_000 or self.step > self.max - self.min:
            raise ValueError("VISUALIZATION_PARAMETER_STEPS_INVALID")
        return self


def compile_expression(expression: str, symbols: set[str]) -> dict:
    """Parse a bounded numeric grammar, never eval/compile/import a Python program."""
    try:
        parsed = ast.parse(expression.replace("^", "**"), mode="eval")
    except (SyntaxError, ValueError, RecursionError) as error:
        raise ValueError("VISUALIZATION_EXPRESSION_INVALID") from error
    count = 0
    used = set()
    binary = {ast.Add: "add", ast.Sub: "sub", ast.Mult: "mul", ast.Div: "div", ast.Pow: "pow"}

    def visit(node, depth=0):
        nonlocal count
        count += 1
        if count > 128 or depth > 24:
            raise ValueError("VISUALIZATION_EXPRESSION_LIMIT")
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            value = float(node.value)
            if not math.isfinite(value) or abs(value) > 1_000_000:
                raise ValueError("VISUALIZATION_NUMBER_LIMIT")
            return {"type": "number", "value": value}
        if isinstance(node, ast.Name):
            if node.id in {"pi", "e"}:
                return {"type": "number", "value": math.pi if node.id == "pi" else math.e}
            if node.id not in symbols | {"x"}:
                raise ValueError("VISUALIZATION_SYMBOL_DENIED")
            used.add(node.id)
            return {"type": "variable", "name": node.id}
        if isinstance(node, ast.BinOp) and type(node.op) in binary:
            return {
                "type": "binary",
                "op": binary[type(node.op)],
                "left": visit(node.left, depth + 1),
                "right": visit(node.right, depth + 1),
            }
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return {
                "type": "unary",
                "op": "positive" if isinstance(node.op, ast.UAdd) else "negative",
                "operand": visit(node.operand, depth + 1),
            }
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in FUNCTIONS
            and len(node.args) == 1
            and not node.keywords
        ):
            return {"type": "unary", "op": node.func.id, "operand": visit(node.args[0], depth + 1)}
        raise ValueError("VISUALIZATION_EXPRESSION_OPERATION_DENIED")

    result = visit(parsed.body)
    if not symbols.issubset(used):
        raise ValueError("VISUALIZATION_UNUSED_PARAMETER")
    return result


class CartesianData(DataContract):
    kind: Literal["cartesian"] = "cartesian"
    expression: str = Field(min_length=1, max_length=500)
    parameters: list[PlotParameter] = Field(default_factory=list, max_length=8)
    x_range: AxisRange
    y_range: AxisRange
    sample_count: int = Field(default=256, ge=32, le=512)
    probe_x: float = Field(default=0, ge=-10_000, le=10_000)
    animate_parameter: str | None = Field(default=None, max_length=16)
    duration_seconds: float = Field(default=8, ge=1, le=120)
    expression_tree: dict | None = None

    @model_validator(mode="before")
    @classmethod
    def inactive_duration(cls, value):
        if (
            isinstance(value, dict)
            and value.get("animate_parameter") is None
            and type(value.get("duration_seconds")) in {int, float}
            and value["duration_seconds"] == 0
        ):
            return {**value, "duration_seconds": 8}
        return value

    @model_validator(mode="after")
    def compile_data(self):
        symbols = {p.symbol for p in self.parameters}
        if len(symbols) != len(self.parameters):
            raise ValueError("VISUALIZATION_PARAMETER_DUPLICATE")
        if not self.x_range.min <= self.probe_x <= self.x_range.max:
            raise ValueError("VISUALIZATION_PROBE_OUTSIDE_AXIS")
        if self.animate_parameter is not None and self.animate_parameter not in symbols:
            raise ValueError("VISUALIZATION_ANIMATION_PARAMETER_DENIED")
        expected = compile_expression(self.expression, symbols)
        if self.expression_tree is not None and self.expression_tree != expected:
            raise ValueError("VISUALIZATION_EXPRESSION_TREE_CONFLICT")
        self.expression_tree = expected
        return self


class DiagramNode(DataContract):
    node_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    x: float = Field(ge=0, le=100)
    y: float = Field(ge=0, le=100)
    label: str = Field(default="", max_length=160)
    shape: Literal["point", "circle", "box"] = "point"
    size: float = Field(default=3, ge=0.5, le=15)
    color: Literal["blue", "orange", "green", "gray", "red"] = "blue"


class DiagramEdge(DataContract):
    source: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    target: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    label: str = Field(default="", max_length=160)
    arrow: bool = False


class DiagramData(DataContract):
    kind: Literal["diagram"] = "diagram"
    nodes: list[DiagramNode] = Field(min_length=1, max_length=128)
    edges: list[DiagramEdge] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def graph(self):
        identities = {n.node_id for n in self.nodes}
        if len(identities) != len(self.nodes):
            raise ValueError("VISUALIZATION_NODE_DUPLICATE")
        if any(
            e.source not in identities or e.target not in identities or e.source == e.target
            for e in self.edges
        ):
            raise ValueError("VISUALIZATION_EDGE_INVALID")
        return self


class LocalRenderer(RendererRef):
    model_config = ConfigDict(extra="forbid")
    id: Literal["learning.svg-data"] = "learning.svg-data"
    version: Literal["1.0.0"] = "1.0.0"
    target: Literal["native"] = "native"
    native_renderer: Literal["cartesian", "diagram"]
    entry_url: Literal[""] = ""


class LocalPayload(VisualizationPayload):
    model_config = ConfigDict(extra="forbid")
    format: Literal["learning.svg-data/v1"] = "learning.svg-data/v1"
    data: Annotated[CartesianData | DiagramData, Field(discriminator="kind")]


class LocalPresentation(VisualizationPresentation):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=3000)
    alt_text: str = Field(default="", max_length=3000)
    aspect_ratio: Literal["4:3"] = "4:3"


class LocalInteraction(VisualizationInteraction):
    model_config = ConfigDict(extra="forbid")
    events: list[str] = Field(default_factory=list, max_length=0)


class LocalVisualizationEnvelope(VisualizationEnvelope):
    """Strict adaptation of the actual upstream DTO, not its unsafe fallback renderer."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["deeptutor.visualization/v1"] = "deeptutor.visualization/v1"
    render_type: Literal["cartesian", "diagram"]
    renderer: LocalRenderer
    payload: LocalPayload
    presentation: LocalPresentation = Field(default_factory=LocalPresentation)
    interaction: LocalInteraction = Field(default_factory=LocalInteraction)
    fallback: dict = Field(default_factory=dict, max_length=0)

    @model_validator(mode="after")
    def consistent(self):
        if (
            self.render_type != self.payload.data.kind
            or self.renderer.native_renderer != self.render_type
        ):
            raise ValueError("VISUALIZATION_RENDERER_DATA_CONFLICT")
        return self
