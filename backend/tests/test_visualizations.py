"""Strict renderer data, safe mathematical grammar and immutable product versions."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError
from workbench_support import learning_course

from learning_app.errors import AppError
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.models import (
    ArtifactBody,
    ArtifactCreate,
    ArtifactUpdate,
    BlockChange,
    VisualizationBlock,
)
from learning_app.workbench.visualization import LocalVisualizationEnvelope, compile_expression


def plot():
    return {
        "render_type": "cartesian",
        "renderer": {"native_renderer": "cartesian"},
        "presentation": {"title": "二次函数", "alt_text": "系数改变时观察抛物线"},
        "payload": {
            "data": {
                "kind": "cartesian",
                "expression": "a*x^2",
                "x_range": {"min": -4, "max": 4},
                "y_range": {"min": -10, "max": 10},
                "probe_x": 1,
                "parameters": [
                    {
                        "symbol": "a",
                        "label": "系数a",
                        "min": -2,
                        "max": 2,
                        "initial": 1,
                        "step": 0.1,
                    }
                ],
                "animate_parameter": "a",
            }
        },
    }


def diagram():
    return {
        "render_type": "diagram",
        "renderer": {"native_renderer": "diagram"},
        "payload": {
            "data": {
                "kind": "diagram",
                "nodes": [
                    {"node_id": "a", "x": 20, "y": 50, "label": "输入"},
                    {"node_id": "b", "x": 80, "y": 50, "label": "结果"},
                ],
                "edges": [{"source": "a", "target": "b", "arrow": True, "label": "变换"}],
            }
        },
    }


def test_original_envelope_inheritance_compilation_and_roundtrip():
    from deeptutor_reuse.visualization_models import VisualizationEnvelope

    value = LocalVisualizationEnvelope.model_validate(plot())
    assert isinstance(value, VisualizationEnvelope)
    assert value.payload.data.expression_tree == {
        "type": "binary",
        "op": "mul",
        "left": {"type": "variable", "name": "a"},
        "right": {
            "type": "binary",
            "op": "pow",
            "left": {"type": "variable", "name": "x"},
            "right": {"type": "number", "value": 2.0},
        },
    }
    assert LocalVisualizationEnvelope.model_validate_json(value.model_dump_json()) == value
    assert LocalVisualizationEnvelope.model_validate(diagram()).payload.data.kind == "diagram"
    with pytest.raises(ValidationError):
        VisualizationBlock(
            block_id="plot",
            markdown="原文字句",
            attribution="source_quote",
            source_snapshot_ids=["original_text"],
            visualization=value,
        )


def test_browser_analytic_fixtures_use_the_actual_formal_compiler():
    from learning_app.workbench.visualization import CartesianData

    cases = json.loads(
        (
            Path(__file__).resolve().parents[2] / "tests/fixtures/workbench/visualizations.json"
        ).read_text("utf-8")
    )
    for case in cases:
        data = CartesianData.model_validate(case["data"])
        assert data.expression_tree == case["data"]["expression_tree"]


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('x')",
        "x.__class__",
        "[x for x in range(10)]",
        "open('/secret')",
        "(lambda:1)()",
        "a['x']",
        "sin(x, 1)",
        "sin(x, out=1)",
        "True",
        "1e999",
        "x//2",
        "x if a else 1",
    ],
)
def test_expression_is_data_not_an_executable_program(expression):
    with pytest.raises(ValueError):
        compile_expression(expression, {"a"})


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v.update(html="<script>fetch('/api/v1/session')</script>"),
        lambda v: v["renderer"].update(target="iframe", entry_url="https://example.invalid/x"),
        lambda v: v["renderer"].update(entry_url="/api/v1/session"),
        lambda v: v.update(interaction={"events": ["write_business_data"]}),
        lambda v: v.update(fallback={"html": "<iframe src='/api/v1/session'>"}),
        lambda v: v["payload"].update(data={"kind": "svg", "svg": "<svg onload='fetch(1)'/>"}),
        lambda v: v["payload"]["data"].update(**{"$ref": "https://example.invalid/schema"}),
        lambda v: v["payload"]["data"].update(
            expression_tree={"type": "variable", "name": "credential"}
        ),
        lambda v: v["payload"]["data"].update(sample_count=10_000_000),
        lambda v: v["payload"]["data"].update(animate_parameter="missing"),
        lambda v: v["payload"]["data"].update(probe_x=99),
        lambda v: v["payload"]["data"]["parameters"][0].update(symbol="x"),
        lambda v: v["payload"]["data"]["parameters"][0].update(step=1e-10),
        lambda v: v["payload"]["data"]["parameters"][0].update(initial=float("nan")),
    ],
)
def test_renderer_protocol_rejects_capability_and_resource_escapes(change):
    value = deepcopy(plot())
    change(value)
    with pytest.raises(ValidationError):
        LocalVisualizationEnvelope.model_validate(value)


def test_graph_scope_duplicate_nodes_and_expression_budgets():
    value = diagram()
    value["payload"]["data"]["edges"][0]["target"] = "not_in_graph"
    with pytest.raises(ValidationError):
        LocalVisualizationEnvelope.model_validate(value)
    value = diagram()
    value["payload"]["data"]["nodes"][1]["node_id"] = "a"
    with pytest.raises(ValidationError):
        LocalVisualizationEnvelope.model_validate(value)
    with pytest.raises(ValueError):
        compile_expression("+".join(["x"] * 80), set())
    with pytest.raises(ValueError):
        compile_expression("sin(" * 26 + "x" + ")" * 26, set())


def test_visualization_is_saved_indexed_and_conflicting_edits_keep_both(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    store = ArtifactStore(repo)
    block = VisualizationBlock(
        block_id="plot",
        markdown="系数改变时图形和函数一起改变。",
        attribution="source_paraphrase",
        source_snapshot_ids=[source.snapshot_id],
        visualization=LocalVisualizationEnvelope.model_validate(plot()),
    )
    artifact = store.create(
        course,
        ArtifactCreate(
            operation_id="explicit_plot",
            kind="visualization",
            title="可调抛物线",
            body=ArtifactBody(blocks=[block]),
        ),
    ).artifact
    assert store.search(course, "可调抛物线")
    assert (
        artifact.indexed
        and store.get(course, artifact.artifact_id).revision.body.blocks[0] == block
    )
    proposed = block.model_copy(update={"markdown": "用户修改说明。"})
    first = store.update(
        course,
        artifact.artifact_id,
        ArtifactUpdate(
            operation_id="first_edit",
            expected_revision_id=artifact.head_revision_id,
            changes=[BlockChange(block_id="plot", replacement=proposed)],
        ),
    )
    other = store.update(
        course,
        artifact.artifact_id,
        ArtifactUpdate(
            operation_id="second_edit",
            expected_revision_id=artifact.head_revision_id,
            changes=[BlockChange(block_id="plot", replacement=block)],
        ),
    )
    assert first.outcome == "applied" and other.outcome == "conflict"
    assert (
        store.get(course, artifact.artifact_id).head_revision_id == first.artifact.head_revision_id
    )
    with pytest.raises(AppError):
        store.create(
            course,
            ArtifactCreate(
                operation_id="wrong_kind",
                kind="visualization",
                title="缺图形",
                body=ArtifactBody(
                    blocks=[
                        {"block_id": "text", "markdown": "只有入口", "attribution": "user_authored"}
                    ]
                ),
            ),
        )
