from fastapi.testclient import TestClient
from workbench_support import learning_course, note, settings

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.workbench.models import TaskCreate, TaskUnitInput


def test_authenticated_course_product_edit_conflict_receipt_and_task(tmp_path):
    repo, course_id, _, snapshot = learning_course(tmp_path)
    config = AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "run")
    prefix = f"/api/v1/courses/{course_id}/learning"
    with TestClient(
        create_app(config, repository=repo), base_url="http://127.0.0.1:8765"
    ) as client:
        assert client.get(prefix + "/artifacts").status_code == 403
        session = client.get("/api/v1/session").json()
        headers = {"X-Learning-CSRF": session["csrf_token"]}
        payload = note().model_dump(mode="json")
        assert client.post(prefix + "/artifacts", json=payload).status_code == 403
        response = client.post(prefix + "/artifacts", json=payload, headers=headers)
        assert response.status_code == 200
        original = response.json()["artifact"]
        assert (
            client.post(prefix + "/artifacts", json=payload, headers=headers).json()
            == response.json()
        )
        receipt = client.get(prefix + "/operations/create_note").json()
        assert receipt["result"] == response.json()
        path = prefix + f"/artifacts/{original['artifact_id']}"
        update = {
            "operation_id": "manual",
            "expected_revision_id": original["head_revision_id"],
            "title": "手改标题",
            "changes": [],
        }
        assert (
            client.post(path + "/update", headers=headers, json=update).json()["outcome"]
            == "applied"
        )
        update["operation_id"], update["title"] = "parallel", "并发标题"
        conflict = client.post(path + "/update", headers=headers, json=update).json()
        assert conflict["outcome"] == "conflict"
        assert client.get(path).json()["title"] == "手改标题"
        assert len(client.get(path + "/revisions").json()) == 3
        assert (
            client.get(prefix + "/search", params={"query": "手改标题"}).json()[0]["content_role"]
            == "user_note"
        )
        forged = {**payload, "operation_id": "forged_origin", "content_role": "original"}
        assert client.post(prefix + "/artifacts", headers=headers, json=forged).status_code == 422
        task_input = TaskCreate(
            operation_id="task",
            kind="handout",
            instruction="仅处理所选文字",
            settings=settings(),
            units=[
                TaskUnitInput(
                    unit_id="selected", label="精确选区", source_snapshot_id=snapshot.snapshot_id
                )
            ],
        ).model_dump(mode="json")
        task = client.post(prefix + "/tasks", headers=headers, json=task_input).json()
        pause = client.post(
            prefix + f"/tasks/{task['task_id']}/control/pause",
            headers=headers,
            json={"operation_id": "pause", "expected_revision": 0},
        )
        assert pause.json()["state"] == "paused"
        assert (
            client.get(prefix + "/selections/" + snapshot.snapshot_id).json()["text"]
            == snapshot.text
        )
        assert repo.counts()["outbound_attempts"] == 0


def test_generated_openapi_has_typed_learning_contracts_without_lifespan(tmp_path):
    config = AppConfig(mode="test", data_dir=tmp_path / "not_created", runtime_dir=tmp_path / "run")
    app = create_app(config)
    schema = app.openapi()
    assert "/api/v1/courses/{course_id}/learning/tasks" in schema["paths"]
    assert schema["components"]["schemas"]["PageRegion"]["additionalProperties"] is False
    assert schema["components"]["schemas"]["ArtifactCreate"]["additionalProperties"] is False
    assert not config.data_dir.exists()
