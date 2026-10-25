"""Validated final intent, actor order, durable error receipts, and no old draft leak."""

import json

import pytest
from pydantic import ValidationError
from test_native_business import call, count, prepare_registry, stage_args, task_scope
from workbench_support import learning_course

from learning_app.errors import AppError
from learning_app.execution.native_tools import ToolRejected
from learning_app.execution.types import NormalizedEvent
from learning_app.workbench import schema as s
from learning_app.workbench.models import ArtifactBody, QuestionOption
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.stage_attempts import failures
from learning_app.workbench.stage_contract import source_details, validation_details


def terminal(repo, run, binding):
    TaskRunStore(repo).ingest(
        run.run_id,
        NormalizedEvent(
            "known_fixture_done",
            1,
            run.run_id,
            binding.epoch,
            "terminal",
            status="completed",
            native_seq=100,
        ),
        source="fixture",
    )


def test_later_rejected_stage_keeps_private_draft_and_prevents_publication(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    _, run, binding, port, _ = task_scope(repo, course, source)
    args = stage_args(repo, run, source, "early draft")
    receipt = port.invoke(call(binding, "stage_result", args), args)
    assert receipt["stage_revision"] == 1 and receipt["expected_version"] == 0
    wrong = {**args, "expected_version": receipt["stage_revision"]}
    rejected = call(binding, "stage_result", wrong, seq=5)
    with pytest.raises(ToolRejected) as error:
        port.authorize(rejected, wrong)
    assert error.value.code == "NATIVE_STAGE_TARGET_DENIED"
    assert error.value.details["expected_version"] == 0
    assert count(repo, failures) == 1 and count(repo, s.model_stages) == 1
    terminal(repo, run, binding)
    with pytest.raises(AppError) as unpublished:
        port.staged_body(run.run_id)
    assert unpublished.value.code == "NATIVE_FINAL_STAGE_REJECTED"
    assert repo.counts()["outbound_attempts"] == 0
    repo.close()


def test_reaffirming_old_body_after_a_rejection_uses_latest_effect_not_stage_creation(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    _, run, binding, port, _ = task_scope(repo, course, source)
    first = stage_args(repo, run, source, "first body")
    second = stage_args(repo, run, source, "second body")
    initial = port.invoke(call(binding, "stage_result", first), first)
    port.invoke(call(binding, "stage_result", second, seq=5), second)
    invalid = {**first, "body": "not JSON"}
    with pytest.raises(ToolRejected):
        port.invoke(call(binding, "stage_result", invalid, seq=7), invalid)
    last = port.invoke(call(binding, "stage_result", first, seq=9), first)
    assert last == initial and count(repo, s.model_stages) == 2
    terminal(repo, run, binding)
    assert port.staged_body(run.run_id).blocks[0].markdown == "first body"
    repo.close()


@pytest.mark.parametrize(
    "fault", ["native_stage_rejection_before_commit", "native_stage_rejection_after_commit"]
)
def test_rejected_stage_audit_failure_is_unknown_until_exact_readonly_receipt(tmp_path, fault):
    repo, course, _, source = learning_course(tmp_path)
    _, run, binding, port, scope = task_scope(repo, course, source)
    args = {**stage_args(repo, run, source), "expected_version": 1}
    native = call(binding, "stage_result", args)
    registry, envelope = prepare_registry(tmp_path, scope, native, args)

    def fail(point, _detail):
        if point == fault:
            raise RuntimeError("owned fault window")

    repo.fault_hook = fail
    with pytest.raises(ToolRejected) as unknown:
        registry.invoke(envelope, port)
    assert unknown.value.code == "NATIVE_OPERATION_UNKNOWN"
    assert not registry.effects_settled(run.run_id)
    repo.fault_hook = None
    recovered = registry.reconcile_effects(run.run_id, port)
    if fault.endswith("after_commit"):
        assert len(recovered) == 1 and registry.effects_settled(run.run_id)
        with pytest.raises(ToolRejected) as known:
            registry.reconcile_call(envelope, port)
        assert known.value.code == "NATIVE_STAGE_TARGET_DENIED"
        assert known.value.details["expected_version"] == 0
    else:
        assert recovered == [] and not registry.effects_settled(run.run_id)
    assert count(repo, s.model_stages) == 0
    repo.close()


def test_bound_stage_call_without_an_operation_cannot_be_assumed_settled(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    _, run, binding, port, scope = task_scope(repo, course, source)
    args = stage_args(repo, run, source)
    registry, envelope = prepare_registry(
        tmp_path, scope, call(binding, "stage_result", args), args
    )
    registry.validate_call(envelope)
    assert not registry.effects_settled(run.run_id)
    assert registry.reconcile_effects(run.run_id, port) == []
    assert not registry.effects_settled(run.run_id)
    repo.close()


def test_schema_feedback_is_bounded_and_does_not_echo_private_input(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    _, run, binding, port, _ = task_scope(repo, course, source)
    args = {**stage_args(repo, run, source), "body": '{"blocks":[{"kind":"PRIVATE_SENTINEL"}]}'}
    with pytest.raises(ToolRejected) as error:
        port.invoke(call(binding, "stage_result", args), args)
    assert error.value.code == "NATIVE_STAGE_BODY_INVALID"
    assert error.value.details["issues"][0]["type"] == "union_tag_invalid"
    assert "PRIVATE_SENTINEL" not in json.dumps(error.value.details)
    assert len(json.dumps(error.value.details)) < 4096
    repo.close()


def test_option_feedback_includes_server_pattern_without_echoing_values():
    with pytest.raises(ValidationError) as error:
        QuestionOption.model_validate({"option_id": "true", "text": "PRIVATE_OPTION_TEXT"})
    result = validation_details(error.value)
    assert result["issues"][0]["pattern"] == "^[A-Z]$"
    assert "PRIVATE_OPTION_TEXT" not in json.dumps(result)
    assert '"true"' not in json.dumps(result)


def test_source_feedback_only_lists_current_run_ids_and_rejection_stays_durable(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    _, run, binding, port, _ = task_scope(repo, course, source)
    args = stage_args(repo, run, source)
    body = json.loads(args["body"])
    body["blocks"][0]["source_snapshot_ids"] = ["private_ancestor_snapshot"]
    args["body"] = json.dumps(body)
    with pytest.raises(ToolRejected) as rejected:
        port.invoke(call(binding, "stage_result", args), args)
    assert rejected.value.code == "TASK_SOURCE_DENIED"
    details = rejected.value.details
    assert details["allowed_source_snapshot_ids"] == [source.snapshot_id]
    assert details["list_complete"] and details["source_count"] == 1
    assert "private_ancestor_snapshot" not in json.dumps(details)
    assert count(repo, s.model_stages) == 0
    large = source_details(
        0, ArtifactBody.model_validate(body).blocks[0], {"x" * 155 + str(i) for i in range(96)}
    )
    assert len(large["allowed_source_snapshot_ids"]) == 16 and not large["list_complete"]
    assert len(json.dumps(large)) < 4096
    repo.close()


def test_text_alias_and_inactive_duration_are_canonical_but_active_zero_is_rejected():
    chart = {
        "block_id": "figure",
        "kind": "visualization",
        "markdown": "plot",
        "attribution": "ai_supplement",
        "visualization": {
            "render_type": "cartesian",
            "renderer": {"native_renderer": "cartesian"},
            "payload": {
                "data": {
                    "kind": "cartesian",
                    "expression": "sqrt(x+1)+sqrt(3-x)-m",
                    "parameters": [
                        {
                            "symbol": "m",
                            "label": "m",
                            "min": -3,
                            "max": 3,
                            "initial": 2.5,
                            "step": 0.05,
                        }
                    ],
                    "x_range": {"min": -1, "max": 3},
                    "y_range": {"min": -3, "max": 3},
                    "probe_x": 1,
                    "animate_parameter": None,
                    "duration_seconds": 0,
                }
            },
        },
    }
    body = ArtifactBody.model_validate(
        {
            "blocks": [
                {
                    "block_id": "words",
                    "kind": "text",
                    "markdown": "Words",
                    "attribution": "ai_supplement",
                },
                chart,
            ]
        }
    )
    assert body.blocks[0].kind == "markdown"
    assert body.blocks[1].visualization.payload.data.duration_seconds == 8
    chart["visualization"]["payload"]["data"]["animate_parameter"] = "m"
    with pytest.raises(ValidationError):
        ArtifactBody.model_validate({"blocks": [chart]})
