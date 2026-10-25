"""Paged course workspace and native-selected task admission, no model calls."""

from fastapi.testclient import TestClient
from sqlalchemy import update
from test_desktop import NativeFixture, native_envelope
from workbench_support import learning_course, note, settings

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.workspace import WorkspaceQuery, WorkspaceStore


def test_artifact_index_pages_equal_timestamps_without_bodies_and_course_leaks(tmp_path):
    repo, course, _, _ = learning_course(tmp_path)
    repo.create_course("隔离课程", "other")
    assets = ArtifactStore(repo)
    expected = []
    for index in range(5):
        request = note(f"note_{index}", "未泄漏正文" + "x" * 1000).model_copy(
            update={"title": f"笔记{index}"}
        )
        expected.append(assets.create(course, request).artifact.artifact_id)
    assets.create("other", note("other_note"))
    with repo.engine.begin() as conn:
        conn.execute(update(s.artifacts).values(updated_at="2026-10-23T00:00:00.000+00:00"))
    workspace, found, cursor = WorkspaceStore(repo), [], None
    for _ in range(3):
        page = workspace.artifacts(course, WorkspaceQuery(cursor=cursor, limit=2))
        found.extend(item.artifact_id for item in page.items)
        assert "未泄漏正文" not in page.model_dump_json() and "body" not in page.model_dump_json()
        cursor = page.next_cursor
    assert sorted(found) == sorted(expected) and len(set(found)) == 5 and cursor is None
    assert workspace.artifacts(course, WorkspaceQuery(query="不存在的词")).items == []
    assert len(workspace.artifacts(course, WorkspaceQuery(query="未泄漏正文")).items) == 5
    revision_page = workspace.revisions(course, expected[0], WorkspaceQuery(limit=2))
    assert len(revision_page.items) == 1 and "body" not in revision_page.model_dump_json()
    version = workspace.revision(course, expected[0], revision_page.items[0].revision_id)
    assert version.body.blocks[0].markdown.startswith("未泄漏正文")
    repo.close()


def test_workspace_http_requires_session_and_task_list_is_small_projection(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="task",
            kind="handout",
            instruction="完整整理" + "a" * 1000,
            sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
            target={"mode": "create", "title": "讲义标题"},
            settings=settings(),
        ),
    )
    config = AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "run")
    with TestClient(
        create_app(config, repository=repo), base_url="http://127.0.0.1:8765"
    ) as client:
        prefix = f"/api/v1/courses/{course}/learning"
        assert client.get(prefix + "/task-index").status_code == 403
        client.get("/api/v1/session")
        page = client.get(prefix + "/task-index").json()
        assert len(page["items"]) == 1 and page["items"][0]["task_id"] == task.task_id
        assert page["items"][0]["title"] == "讲义标题"
        assert len(page["items"][0]["instruction_preview"]) == 300
        assert "scope_spec" not in str(page) and "units" not in page["items"][0]
        assert client.get(prefix + "/artifact-index", params={"cursor": "bad"}).status_code == 422
        assert client.get(prefix + "/task-index", params={"limit": 101}).status_code == 422
        assert client.get("/api/v1/courses/missing/learning/task-index").status_code == 404


def test_desktop_intent_uses_actual_native_model_choice_and_stable_operation(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    execution = NativeFixture(tmp_path)
    config = AppConfig(
        mode="test", desktop_enabled=True, data_dir=repo.root, runtime_dir=tmp_path / "run"
    )
    app = create_app(config, repository=repo, execution=execution)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        csrf = client.get("/api/v1/session").json()["csrf_token"]
        headers = {"X-Learning-CSRF": csrf}
        opened = client.post(f"/api/v1/desktop/courses/{course}/open", headers=headers).json()
        sid = opened["session_id"]
        client.portal.call(
            app.state.desktop_gateway.rpc,
            "session/selectModel",
            native_envelope(
                "session/selectModel",
                {
                    "sessionId": sid,
                    "provider": "deepseek-official",
                    "model": "deepseek-v4-pro",
                    "reasoningEffort": "off",
                },
            ),
        )
        payload = {
            "operation_id": "native_full_task",
            "kind": "handout",
            "instruction": "整理这个选区",
            "sources": [{"kind": "selection", "snapshot_id": source.snapshot_id}],
            "target": {"mode": "create", "title": "课程讲义"},
            "coverage_mode": "selection",
        }
        endpoint = f"/api/v1/desktop/sessions/{sid}/learning-task-intents"
        assert client.post(endpoint, json=payload).status_code == 403
        response = client.post(endpoint, headers=headers, json=payload)
        assert response.status_code == 200, response.text
        task = response.json()
        assert (
            task["settings"]["model"] == "deepseek-v4-pro"
            and task["settings"]["thinking"] == "none"
        )
        assert task["chat_id"] == opened["chat_id"]
        assert (
            client.post(endpoint, headers=headers, json=payload).json()["task_id"]
            == task["task_id"]
        )
        assert (
            client.post(
                endpoint, headers=headers, json={**payload, "settings": settings().model_dump()}
            ).status_code
            == 422
        )
        denied = client.post(
            endpoint,
            headers=headers,
            json={**payload, "operation_id": "wrong_chat", "chat_id": "another_chat"},
        )
        assert denied.status_code == 403 and denied.json()["code"] == "TASK_CHAT_SCOPE_DENIED"
        assert execution.starts == [] and repo.counts()["outbound_attempts"] == 0
