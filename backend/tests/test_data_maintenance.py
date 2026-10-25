"""A directory move resumes eligible local work, preserving manual pause intent."""

import json

from test_source_preparations import setup
from workbench_support import learning_course, settings

from learning_app.config import PROJECT_ROOT
from learning_app.data_location import ProfileLocation
from learning_app.data_maintenance import DataMaintenance
from learning_app.data_package import DataPackage
from learning_app.storage import Repository
from learning_app.workbench.models import TaskAction, TaskCreate, TaskUnitInput
from learning_app.workbench.ocr_resources import OcrResources
from learning_app.workbench.preparations import PreparationStore
from learning_app.workbench.processing_models import PreparationControl
from learning_app.workbench.tasks import TaskStore


def test_move_recovers_only_previously_runnable_local_queue_after_all_services_closed(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(OcrResources, "prepared", lambda self: True)
    repo, course, _, request = setup(tmp_path)
    store = PreparationStore(repo)
    manual = store.create_pdf(
        course, request.model_copy(update={"scope": "unit", "unit_index": 2}), PROJECT_ROOT
    )
    store.control(
        course,
        manual.job_id,
        "pause",
        PreparationControl(operation_id="user_pause", expected_revision=manual.revision),
    )
    automatic = store.create_pdf(course, request, PROJECT_ROOT)
    lease = store.claim()
    assert lease["job_id"] == automatic.job_id
    maintenance = DataMaintenance(repo)
    intended = maintenance.freeze_intent("move")
    assert maintenance.freeze_intent("move") == intended
    assert [row["job_id"] for row in intended["local_jobs"]] == [automatic.job_id]
    store.freeze_for_exit()
    assert not store.finish(lease, {"late": "ignored"})
    location = ProfileLocation(repo.root)
    target = tmp_path / "other drive"
    target.mkdir()
    location.ready(location.plan("move", "move", target))
    location.schedule("move")
    repo.close()
    with location.lease():
        location.apply_closed()
    relocated = Repository(location.current())
    DataMaintenance(relocated).restore_local_jobs()
    restored = PreparationStore(relocated)
    assert restored.get(course, automatic.job_id).state == "queued"
    assert restored.get(course, manual.job_id).state == "paused"
    before = restored.get(course, automatic.job_id).revision
    DataMaintenance(relocated).restore_local_jobs()
    assert restored.get(course, automatic.job_id).revision == before
    assert restored.claim()["job_id"] == automatic.job_id
    relocated.close()
    original = Repository(repo.root)
    assert PreparationStore(original).get(course, automatic.job_id).state == "paused"
    assert json.loads(maintenance.path.read_text("utf-8"))["state"] == "pending"
    original.close()


def test_move_pauses_model_tasks_without_turning_them_into_cancelled_or_automatically_resuming(
    tmp_path,
):
    repo, course, _, source = learning_course(tmp_path)
    tasks = TaskStore(repo)
    values = []
    for index in range(2):
        values.append(
            tasks.create(
                course,
                TaskCreate(
                    operation_id="task_" + str(index),
                    kind="note",
                    instruction="explicit fixture note",
                    coverage_mode="selection",
                    units=[
                        TaskUnitInput(
                            unit_id="one", label="one", source_snapshot_id=source.snapshot_id
                        )
                    ],
                    settings=settings(),
                ),
            )
        )
    manual = tasks.control(
        course,
        values[1].task_id,
        "pause",
        TaskAction(operation_id="manual_pause", expected_revision=values[1].revision),
    )
    maintenance = DataMaintenance(repo)
    record = maintenance.freeze_intent("move")
    assert [row["task_id"] for row in record["paused_tasks"]] == [values[0].task_id]
    assert tasks.get(course, values[0].task_id).state == "paused"
    assert tasks.get(course, manual.task_id).revision == manual.revision
    maintenance.restore_local_jobs()
    assert tasks.get(course, values[0].task_id).state == "paused"
    assert repo.counts()["outbound_attempts"] == 0
    stream, _ = DataPackage(repo).build()
    with stream:
        DataPackage().verify(stream)
    repo.close()
