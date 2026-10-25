from fastapi.testclient import TestClient
from test_accounts import AccountExecution
from workbench_support import learning_course, settings

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.execution.types import ExecutionBinding, NormalizedEvent
from learning_app.workbench.models import TaskCreate, TaskUnitInput
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.tasks import TaskStore


def explicit_task(repo, course_id, selection):
    return TaskStore(repo).create(
        course_id,
        TaskCreate(
            operation_id="explicit",
            kind="translation",
            instruction="翻译所选内容",
            settings=settings(),
            units=[
                TaskUnitInput(
                    unit_id="unit", label="选区", source_snapshot_id=selection.snapshot_id
                )
            ],
        ),
    )


def connect(client):
    return {"X-Learning-CSRF": client.get("/api/v1/session").json()["csrf_token"]}


def test_queued_task_requires_exit_confirmation_and_exit_blocks_new_tasks(tmp_path):
    repo, course_id, _, selection = learning_course(tmp_path)
    task = explicit_task(repo, course_id, selection)
    config = AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "run")
    with TestClient(
        create_app(config, repository=repo), base_url="http://127.0.0.1:8765"
    ) as client:
        headers = connect(client)
        status = client.get("/api/v1/runtime").json()
        assert (
            status["active_generations"] == []
            and status["active_tasks"][0]["task_id"] == task.task_id
        )
        response = client.post(
            "/api/v1/runtime/prepare-exit",
            headers=headers,
            json={"instance_id": config.instance_id},
        )
        assert (
            response.status_code == 409
            and not client.get("/api/v1/runtime").json()["exit_prepared"]
        )
        response = client.post(
            "/api/v1/runtime/prepare-exit",
            headers=headers,
            json={"instance_id": config.instance_id, "confirm_active": True},
        )
        assert response.status_code == 200 and response.json()["exit_prepared"]
        create = TaskCreate(
            operation_id="too_late",
            kind="translation",
            instruction="不会新派发",
            settings=settings(),
            units=[
                TaskUnitInput(
                    unit_id="unit", label="选区", source_snapshot_id=selection.snapshot_id
                )
            ],
        )
        result = client.post(
            f"/api/v1/courses/{course_id}/learning/tasks",
            headers=headers,
            json=create.model_dump(mode="json"),
        )
        assert result.status_code == 409 and result.json()["code"] == "APPLICATION_EXIT_PREPARED"
        assert len(TaskStore(repo).list(course_id)) == 1 and repo.counts()["outbound_attempts"] == 0


def test_unknown_task_exit_persists_cancel_and_partial_output_without_host_kill(tmp_path):
    repo, course_id, _, selection = learning_course(tmp_path)
    task = explicit_task(repo, course_id, selection)
    runs = TaskRunStore(repo)
    run = runs.claim(course_id, task.task_id, "unit", selection.text)
    runs.persist_binding(
        run.run_id,
        ExecutionBinding(task.task_id, "request", run.run_id, 1, "session", "request"),
        "fixture_host",
    )
    runs.mark_dispatching(run.run_id)
    runs.ingest(
        run.run_id,
        NormalizedEvent("partial", 1, run.run_id, 1, "text_delta", text="已保存部分译文"),
        source="fixture",
    )
    config = AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "run")
    with TestClient(
        create_app(config, repository=repo), base_url="http://127.0.0.1:8765"
    ) as client:
        headers = connect(client)
        assert client.get("/api/v1/runtime").json()["active_tasks"][0]["state"] == "unknown"
        response = client.post(
            "/api/v1/runtime/prepare-exit",
            headers=headers,
            json={"instance_id": config.instance_id, "confirm_active": True},
        )
        assert response.status_code == 200
        retained = runs.get(course_id, task.task_id, run.run_id)
        assert (
            retained.state == "unknown"
            and retained.cancel_requested
            and retained.body == "已保存部分译文"
        )
        assert retained.usage_status == "unknown" and retained.actual_outbound_attempts == 0


def test_task_run_blocks_credential_switch_and_signout_until_execution_is_known_final(tmp_path):
    repo, course_id, _, selection = learning_course(tmp_path)
    task = explicit_task(repo, course_id, selection)
    runs = TaskRunStore(repo)
    run = runs.claim(course_id, task.task_id, "unit", selection.text)
    execution = AccountExecution(api_key="synthetic-only-credential")
    config = AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "run")
    with TestClient(
        create_app(config, repository=repo, execution=execution), base_url="http://127.0.0.1:8765"
    ) as client:
        headers = connect(client)
        for path, payload in (("route", {"route": "api_key"}), ("sign-out", None)):
            response = client.post("/api/v1/account/" + path, headers=headers, json=payload)
            assert (
                response.status_code == 409
                and response.json()["code"] == "ACCOUNT_ACTIVE_GENERATIONS"
            )
        assert not any(method == "account/signOut" for method, _args in execution.calls)
        assert execution.credential_route == "account"
        runs.fail_prepared(run.run_id, "KNOWN_NO_DISPATCH")
        response = client.post("/api/v1/account/route", headers=headers, json={"route": "api_key"})
        assert response.status_code == 200 and execution.credential_route == "api_key"
