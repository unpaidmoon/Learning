"""Real business transactions with explicit native-event fixtures, no provider."""

import json
from dataclasses import asdict, replace

import pytest
from sqlalchemy import func, select
from workbench_support import learning_course, settings

from learning_app.drafts import source_selection_conditions
from learning_app.errors import AppError
from learning_app.execution.native_tools import (
    NativeCall,
    NativeCallRegistry,
    ToolRejected,
    args_hash,
)
from learning_app.execution.types import ExecutionBinding, NormalizedEvent
from learning_app.storage import Repository
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.models import ArtifactCreate, TaskAction
from learning_app.workbench.native_business import NativeBusiness
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def task_scope(repo, course, source, operation="intent", *, kind="handout", dispatch=True):
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id=operation,
            kind=kind,
            instruction="完整整理这段资料",
            coverage_mode="selection",
            sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
            target={"mode": "create", "title": "课程成果"},
            settings=settings(),
            sources_only=True,
        ),
    )
    TaskCoverage(repo).refresh(course, task.task_id)
    TaskPlanner(repo).plan(course, task.task_id)
    task = TaskStore(repo).get(course, task.task_id)
    run = TaskRunStore(repo).claim(course, task.task_id, task.units[0].unit_id, "fixture prompt")
    binding = ExecutionBinding(
        task.task_id,
        f"request_{run.run_id}",
        run.run_id,
        7,
        f"sid_{run.run_id}",
        f"request_{run.run_id}",
        native_floor=0,
        native_turn=1,
    )
    TaskRunStore(repo).persist_binding(run.run_id, binding, "native_business_fixture")
    port = NativeBusiness(repo)
    scope = port.ensure_task_scope(run.run_id, binding)
    if dispatch:
        assert TaskRunStore(repo).mark_dispatching(run.run_id)
    return task, run, binding, port, scope


def call(binding, tool, arguments, *, seq=3):
    return NativeCall(
        binding.session_id,
        binding.generation_id,
        binding.request_id,
        binding.native_lease_epoch,
        "fixture_attempt",
        "00000000-0000-0000-0000-000000000001",
        0,
        binding.native_turn,
        1,
        seq,
        "fixture_call",
        tool,
        args_hash(arguments),
    )


def stage_args(repo, run, source, text="已经整理的来源内容"):
    target = TaskRunStore(repo).internal(run.run_id)["input_contract"]["target"]
    return {
        "task_id": run.task_id,
        "object_id": target["reserved_artifact_id"],
        "expected_version": target["structure_revision"],
        "stage": "content",
        "body": json.dumps(
            {
                "blocks": [
                    {
                        "block_id": "summary",
                        "markdown": text,
                        "attribution": "source_paraphrase",
                        "source_snapshot_ids": [source.snapshot_id],
                    }
                ]
            },
            ensure_ascii=False,
        ),
    }


def count(repo, table):
    with repo.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def test_stage_is_draft_and_only_completed_original_run_can_publish(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    _, run, binding, port, _ = task_scope(repo, course, source)
    args = stage_args(repo, run, source)
    first = port.invoke(call(binding, "stage_result", args), args)
    second = port.invoke(call(binding, "stage_result", args, seq=5), args)
    assert first == second and first["disposition"] == "staged"
    assert count(repo, s.model_stages) == 1 and count(repo, s.native_effects) == 2
    assert ArtifactStore(repo).list(course) == [] and port.staged_body(run.run_id) is None
    TaskRunStore(repo).ingest(
        run.run_id,
        NormalizedEvent(
            "fixture_done",
            1,
            run.run_id,
            binding.epoch,
            "terminal",
            status="completed",
            native_seq=10,
        ),
        source="fixture",
    )
    body = port.staged_body(run.run_id)
    assert body.blocks[0].markdown == "已经整理的来源内容"
    receipt = ArtifactStore(repo).publish_run_result(
        run.run_id,
        ArtifactCreate(operation_id="ignored", kind="handout", title="课程成果", body=body),
    )
    assert receipt.artifact.artifact_id == args["object_id"]
    assert receipt.artifact.revision.author == "model"
    assert receipt.artifact.revision.task_id == run.task_id
    assert len(ArtifactStore(repo).list(course)) == 1
    repo.close()


@pytest.mark.parametrize(
    "change,code",
    [
        ({"object_id": "another_asset"}, "NATIVE_STAGE_TARGET_DENIED"),
        ({"task_id": "another_task"}, "NATIVE_STAGE_TARGET_DENIED"),
        ({"expected_version": 999}, "NATIVE_STAGE_TARGET_DENIED"),
        ({"stage": "submit_answer"}, "NATIVE_STAGE_TARGET_DENIED"),
        ({"expected_version": True}, "NATIVE_STAGE_ARGUMENTS"),
        ({"body": "not json"}, "NATIVE_STAGE_BODY_INVALID"),
    ],
)
def test_stage_rejects_ungranted_target_version_and_phase(tmp_path, change, code):
    repo, course, _, source = learning_course(tmp_path)
    _, run, binding, port, _ = task_scope(repo, course, source)
    args = {**stage_args(repo, run, source), **change}
    with pytest.raises(ToolRejected, match=code):
        port.invoke(call(binding, "stage_result", args), args)
    assert count(repo, s.model_stages) == 0 and ArtifactStore(repo).list(course) == []
    repo.close()


@pytest.mark.parametrize(
    "role,ids,code",
    [
        ("user_authored", [], "TASK_USER_AUTHOR_DENIED"),
        ("ai_supplement", [], "TASK_SUPPLEMENT_DENIED"),
        ("source_paraphrase", ["selection_ungranted"], "TASK_SOURCE_DENIED"),
        ("source_quote", None, "SOURCE_QUOTE_MISMATCH"),
    ],
)
def test_stage_uses_same_attribution_guard_as_publisher(tmp_path, role, ids, code):
    repo, course, _, source = learning_course(tmp_path)
    _, run, binding, port, _ = task_scope(repo, course, source)
    args = stage_args(repo, run, source, "not an original literal quote")
    body = json.loads(args["body"])
    body["blocks"][0].update(
        attribution=role, source_snapshot_ids=[source.snapshot_id] if ids is None else ids
    )
    args["body"] = json.dumps(body)
    with pytest.raises(ToolRejected, match=code):
        port.invoke(call(binding, "stage_result", args), args)
    assert count(repo, s.model_stages) == 0
    repo.close()


def test_source_handle_is_exact_readonly_and_rechecked_after_pause(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    task, run, binding, port, _ = task_scope(repo, course, source)
    args = {"handle_id": source.snapshot_id, "source_version": source.content_hash}
    read = call(binding, "read_source_handle", args)
    result = port.invoke(read, args)
    assert result["source"]["text"] == source.text
    assert "不应补发前后文" not in json.dumps(result, ensure_ascii=False)
    for denied in (
        {**args, "source_version": "f" * 64},
        {**args, "handle_id": "course_all"},
        {**args, "path": "lesson.txt"},
    ):
        with pytest.raises(ToolRejected):
            port.invoke(call(binding, "read_source_handle", denied), denied)
    current = TaskStore(repo).get(course, task.task_id)
    TaskStore(repo).control(
        course,
        task.task_id,
        "pause",
        TaskAction(operation_id="pause", expected_revision=current.revision),
    )
    with pytest.raises(ToolRejected, match="NATIVE_RUN_CANCEL_REQUESTED"):
        port.authorize(read, args)
    assert port.reconcile_effect(read) == result
    repo.close()


def prepare_registry(tmp_path, scope, native_call, arguments):
    registry = NativeCallRegistry(tmp_path / "native-call-facts.jsonl")
    registry.bind(scope)
    registry.begin_attempt(
        {
            "request_id": native_call.attempt_id,
            "generation_id": native_call.run_id,
            "native_session_id": native_call.session_id,
        },
        native_call.dispatch_id,
    )
    events = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": native_call.call_id,
                "name": native_call.tool_name,
                "input": arguments,
            },
        },
        {"type": "content_block_stop", "index": 0},
    ]
    registry.feed(
        {"request_id": native_call.attempt_id},
        "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode(),
    )
    return registry, {**asdict(native_call), "arguments": arguments}


@pytest.mark.parametrize(
    "fault_point,committed",
    [("native_stage_before_commit", False), ("native_stage_after_commit", True)],
)
def test_unknown_stage_receipt_reconciles_database_only_without_reinvoke(
    tmp_path, fault_point, committed
):
    repo, course, _, source = learning_course(tmp_path)
    _, run, binding, port, scope = task_scope(repo, course, source)
    args = stage_args(repo, run, source)
    native_call = call(binding, "stage_result", args)
    registry, envelope = prepare_registry(tmp_path, scope, native_call, args)
    observed = []

    def fault(point, _context):
        if point == fault_point:
            observed.append(point)
            raise RuntimeError("known fixture receipt loss")

    repo.fault_hook = fault
    with pytest.raises(ToolRejected, match="NATIVE_OPERATION_UNKNOWN"):
        registry.invoke(envelope, port)
    repo.fault_hook = None
    key = f"{binding.session_id}:{native_call.native_call_seq}"
    assert registry.operations[key]["state"] == "unknown"
    assert observed == [fault_point]
    assert registry.reconcile_effects(run.run_id, port) == ([key] if committed else [])
    assert count(repo, s.model_stages) == int(committed)
    assert count(repo, s.native_effects) == int(committed)
    if committed:
        assert registry.operations[key]["reconciled"]
        assert registry.invoke(envelope, port) == port.reconcile_effect(native_call)
    else:
        with pytest.raises(ToolRejected, match="NATIVE_OPERATION_UNKNOWN"):
            registry.invoke(envelope, port)
    assert observed == [fault_point] and ArtifactStore(repo).list(course) == []
    repo.close()


def test_restore_retains_native_mapping_and_same_source_can_belong_to_two_runs(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    _, run, binding, port, scope = task_scope(repo, course, source)
    original = binding.to_dict()
    assert binding.epoch == 7 and binding.native_lease_epoch == 1
    assert scope["epoch"] == 1
    repo.close()
    repo = Repository(tmp_path / "data")
    restored = ExecutionBinding.from_dict(original)
    assert NativeBusiness(repo).restore_scope(run.run_id, restored) == scope
    assert restored.epoch == 7 and restored.native_lease_epoch == 1
    _, second, _, _, _ = task_scope(repo, course, source, operation="second")
    assert second.run_id != run.run_id and count(repo, s.native_source_handles) == 2
    with pytest.raises(AppError) as rejected:
        NativeBusiness(repo).restore_scope(run.run_id, replace(restored, epoch=8))
    assert rejected.value.code == "NATIVE_BINDING_MISMATCH"
    repo.close()


def prepare_chat(repo, course, source, submission, *, turn=1, source_locators=()):
    if not repo.list_chats(course):
        repo.create_chat(course, "native_chat")
    with repo.engine.connect() as conn:
        conditions = source_selection_conditions(conn, course, [source.snapshot_id])
    result = repo.publish_submission(
        "native_chat",
        "解释这段资料",
        submission,
        conditions=conditions,
        source_locators=source_locators,
    )
    binding = ExecutionBinding(
        "native_chat",
        submission,
        result["generation_id"],
        1,
        "same_native_sid",
        submission,
        native_floor=(turn - 1) * 10,
        native_turn=turn,
    )
    known = repo.bind_execution(
        "native_chat", binding.session_id, adapter_metadata=binding.to_dict()
    )
    assert known["epoch"] == 1
    assert repo.claim_dispatch(
        binding.generation_id,
        known["binding_id"],
        1,
        request_id=submission,
        adapter_metadata=binding.to_dict(),
    )
    return result, binding


def test_same_chat_native_epochs_increase_without_changing_business_epoch(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    _, binding = prepare_chat(repo, course, source, "first_chat")
    port = NativeBusiness(repo)
    scope = port.ensure_chat_scope(binding.generation_id, binding)
    assert scope["purpose"] == "qa" and "stage_result" not in scope["allowed_tools"]
    with repo.engine.connect() as conn:
        row = port._scope(conn, binding.generation_id)
    assert row["raw_user_text"] == "解释这段资料"
    assert source.text not in row["raw_user_text"]
    with pytest.raises(ToolRejected, match="NATIVE_TOOL_NOT_ALLOWED"):
        args = {"body": "{}"}
        port.authorize(call(binding, "stage_result", args), args)
    event = NormalizedEvent(
        "chat_done", 1, binding.generation_id, 1, "terminal", status="completed", native_seq=10
    )
    repo.ingest_event(
        binding.generation_id,
        binding_id=repo.current_binding("native_chat")["binding_id"],
        epoch=event.epoch,
        event_id=event.event_id,
        seq=event.seq,
        kind=event.kind,
        status=event.status,
        data={"native_seq": event.native_seq, "reason": {"kind": "completed"}},
    )
    terminal = {"turn": 1, "seq": 10, "reason": {"kind": "completed"}}
    port.remember_terminal(binding.generation_id, terminal)
    lease = {key: scope[key] for key in ("session_id", "run_id", "request_id", "epoch")}
    port.assert_releasable(lease)
    port.mark_released(
        {
            **lease,
            "released": True,
            "durable": True,
            "native_turn": 1,
            "terminal_seq": 10,
            "native_terminal_kind": "completed",
        }
    )
    _, next_binding = prepare_chat(repo, course, source, "next_chat", turn=2)
    next_scope = port.ensure_chat_scope(next_binding.generation_id, next_binding)
    assert next_scope["epoch"] == 2 and next_binding.native_lease_epoch == 2
    assert next_binding.epoch == binding.epoch == 1
    assert repo.current_binding("native_chat")["epoch"] == 1
    assert port.restore_scope(binding.generation_id, binding) == scope
    assert count(repo, s.native_source_handles) == 2
    repo.close()


def test_translation_has_no_native_tool_permissions(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    _, _, binding, port, scope = task_scope(repo, course, source, kind="translation")
    assert scope["purpose"] == "translation" and scope["allowed_tools"] == []
    args = {"handle_id": source.snapshot_id, "source_version": source.content_hash}
    with pytest.raises(ToolRejected, match="NATIVE_TOOL_NOT_ALLOWED"):
        port.authorize(call(binding, "read_source_handle", args), args)
    repo.close()


@pytest.mark.parametrize(
    "point,committed", [("native_read_before_commit", False), ("native_read_after_commit", True)]
)
def test_read_receipt_loss_reconciles_original_immutable_response_only(tmp_path, point, committed):
    repo, course, _, source = learning_course(tmp_path)
    _, run, binding, port, scope = task_scope(repo, course, source)
    args = {"handle_id": source.snapshot_id, "source_version": source.content_hash}
    native_call = call(binding, "read_source_handle", args)
    registry, envelope = prepare_registry(tmp_path, scope, native_call, args)
    seen = []

    def fault(name, _detail):
        if name == point:
            seen.append(name)
            raise RuntimeError("synthetic immutable read receipt loss")

    repo.fault_hook = fault
    with pytest.raises(ToolRejected, match="NATIVE_OPERATION_UNKNOWN"):
        registry.invoke(envelope, port)
    repo.fault_hook = None
    assert count(repo, s.native_read_receipts) == int(committed)
    keys = registry.reconcile_effects(run.run_id, port)
    assert len(keys) == int(committed) and seen == [point]
    if committed:
        saved = port.reconcile_effect(native_call)
        assert saved["source"]["text"] == source.text
        assert registry.invoke(envelope, port) == saved
        assert count(repo, s.native_read_receipts) == 1
    else:
        assert port.reconcile_effect(native_call) is None
    repo.close()


@pytest.mark.parametrize("action", ["local_failure", "cancel"])
def test_known_pre_dispatch_closure_does_not_invent_native_terminal(tmp_path, action):
    repo, course, _, source = learning_course(tmp_path)
    _, run, binding, port, scope = task_scope(repo, course, source, dispatch=False)
    registry = NativeCallRegistry(tmp_path / "native-facts.jsonl")
    registry.bind(scope)
    lease = {key: scope[key] for key in ("session_id", "run_id", "request_id", "epoch")}
    assert not registry.retire_unstarted(lease, port)
    if action == "local_failure":
        TaskRunStore(repo).fail_prepared(run.run_id, "KNOWN_LOCAL_PREFLIGHT_FAILURE")
    else:
        TaskRunStore(repo).request_cancel(run.run_id)
    assert registry.retire_unstarted(lease, port)
    assert registry.retire_unstarted(lease, port)
    assert run.run_id not in registry.released and run.run_id in registry.retired_unstarted
    with repo.engine.connect() as conn:
        stored = port._scope(conn, run.run_id)
    assert stored["state"] == "not_dispatched" and stored["native_terminal"] is None
    assert port.unreleased_scopes() == [] and ArtifactStore(repo).list(course) == []
    with pytest.raises(ToolRejected, match="NATIVE_LEASE_NOT_ACTIVE"):
        registry.owner(scope)
    repo.close()


def test_unknown_effect_fences_new_call_identity_and_next_model_attempt(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    _, run, binding, port, scope = task_scope(repo, course, source)
    args = stage_args(repo, run, source)
    first = call(binding, "stage_result", args)
    registry, envelope = prepare_registry(tmp_path, scope, first, args)
    second = replace(first, call_id="different_call_id", native_call_seq=5, tool_ordinal=1)
    events = [
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": second.call_id,
                "name": second.tool_name,
                "input": args,
            },
        },
        {"type": "content_block_stop", "index": 1},
    ]
    registry.feed(
        {"request_id": first.attempt_id},
        "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode(),
    )

    def fault(point, _detail):
        if point == "native_stage_before_commit":
            raise RuntimeError("synthetic unknown effect")

    repo.fault_hook = fault
    with pytest.raises(ToolRejected, match="NATIVE_OPERATION_UNKNOWN"):
        registry.invoke(envelope, port)
    repo.fault_hook = None
    with pytest.raises(ToolRejected, match="NATIVE_OPERATION_UNKNOWN"):
        registry.invoke({**asdict(second), "arguments": args}, port)
    with pytest.raises(ToolRejected, match="NATIVE_OPERATION_UNKNOWN"):
        registry.begin_attempt(
            {
                "request_id": "new_attempt",
                "generation_id": run.run_id,
                "native_session_id": binding.session_id,
            },
            "00000000-0000-0000-0000-000000000002",
        )
    assert count(repo, s.model_stages) == 0 and count(repo, s.native_effects) == 0
    assert sorted(value["state"] for value in registry.operations.values()) == [
        "not_dispatched",
        "unknown",
    ]
    assert len(registry.attempts) == 1
    repo.close()
