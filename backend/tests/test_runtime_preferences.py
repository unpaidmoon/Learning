"""Local execution settings, real owned worker limits and simulated power API.

No current-user power settings, startup entry, model choice or credential is
changed. Power behavior uses an injected platform; workers are owned fixtures.
"""

import json
import os
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_source_preparations import setup

from learning_app.app import create_app
from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.data_package import DataPackage
from learning_app.errors import AppError
from learning_app.runtime_preferences import (
    RuntimePowerKeeper,
    RuntimePreferenceInput,
    RuntimePreferences,
    RuntimePreferenceValues,
    resource_policy,
)
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.workbench.local_workers import run_json_worker
from learning_app.workbench.ocr_resources import OcrResources
from learning_app.workbench.preparations import PreparationStore
from learning_app.workbench.processing import PreparationService


def change(identity="set_resources", revision=0, mode="accelerated", keep=False):
    return RuntimePreferenceInput(
        operation_id=identity,
        expected_revision=revision,
        values=RuntimePreferenceValues(resource_mode=mode, keep_awake=keep),
    )


def test_preferences_default_cas_idempotency_and_model_parameters_are_outside_interface(tmp_path):
    preferences = RuntimePreferences(tmp_path)
    assert preferences.read().values == RuntimePreferenceValues()
    assert not preferences.path.exists()
    saved = preferences.set(change())
    assert saved.revision == 1
    assert RuntimePreferences(tmp_path).read() == saved
    assert preferences.set(change()) == saved
    with pytest.raises(AppError) as error:
        preferences.set(change(mode="gentle"))
    assert error.value.code == "RUNTIME_OPERATION_CONFLICT"
    with pytest.raises(AppError) as error:
        preferences.set(change(identity="stale"))
    assert error.value.code == "RUNTIME_PREFERENCE_CONFLICT"
    with pytest.raises(ValidationError):
        RuntimePreferenceValues.model_validate(
            {"resource_mode": "gentle", "model": "different-model"}
        )


def test_published_preference_with_lost_receipt_reconciles_without_second_save(
    tmp_path, monkeypatch
):
    from learning_app import runtime_preferences as module

    preferences = RuntimePreferences(tmp_path)
    write = module.atomic_record

    def fault(path, value):
        if path.parent == preferences.operations and value.get("state") == "completed":
            raise OSError("owned lost acknowledgment")
        write(path, value)

    monkeypatch.setattr(module, "atomic_record", fault)
    with pytest.raises(OSError):
        preferences.set(change())
    assert preferences.read().revision == 1
    monkeypatch.setattr(module, "atomic_record", write)
    receipt = preferences.reconcile("set_resources")
    assert receipt["state"] == "completed"
    assert preferences.set(change()).revision == 1


def test_preferences_are_whitelisted_in_whole_package_and_import_does_not_apply_power(tmp_path):
    repo = Repository(tmp_path / "data")
    preferences = RuntimePreferences(repo.root)
    preferences.set(change(keep=True))
    stream, manifest = DataPackage(repo).build()
    archive = tmp_path / "whole.zip"
    with stream, archive.open("wb") as output:
        output.write(stream.read())
    assert "data/desktop-ui/runtime.json" in {r["path"] for r in manifest["files"]}
    assert not any("runtime-operations" in r["path"] for r in manifest["files"])
    stage = tmp_path / "stage"
    DataPackage().stage(archive, stage)
    assert RuntimePreferences(stage / "data").read() == preferences.read()
    repo.close()


def test_power_is_requested_only_during_active_work_and_released_on_same_thread(tmp_path):
    preferences = RuntimePreferences(tmp_path)
    busy = threading.Event()

    class Platform:
        available = True
        calls = []

        def set(self, active):
            self.calls.append((active, threading.get_ident()))

    platform = Platform()
    keeper = RuntimePowerKeeper(preferences, busy.is_set, platform=platform)
    keeper.start()

    def wait_for(predicate):
        deadline = time.monotonic() + 3
        while not predicate():
            assert time.monotonic() < deadline
            time.sleep(0.01)

    try:
        busy.set()
        keeper.wake()
        time.sleep(0.03)
        assert not platform.calls
        preferences.set(change(keep=True))
        keeper.wake()
        wait_for(lambda: len(platform.calls) == 1)
        busy.clear()
        keeper.wake()
        wait_for(lambda: len(platform.calls) == 2)
        busy.set()
        keeper.wake()
        wait_for(lambda: len(platform.calls) == 3)
    finally:
        keeper.close()
    assert [value for value, _ in platform.calls] == [True, False, True, False]
    assert len({identity for _, identity in platform.calls}) == 1
    assert not keeper.thread.is_alive() and not keeper.status()["held"]


@pytest.mark.parametrize("mode", ["gentle", "accelerated"])
def test_actual_owned_worker_observes_selected_thread_limits_and_windows_priority(tmp_path, mode):
    policy = resource_policy(mode)
    script = tmp_path / "worker.py"
    script.write_text(
        """import json,os,sys
r=json.loads(sys.stdin.readline()); priority=0
if os.name=='nt':
 import ctypes
 k=ctypes.WinDLL('kernel32');k.GetCurrentProcess.restype=ctypes.c_void_p;k.GetPriorityClass.argtypes=[ctypes.c_void_p];k.GetPriorityClass.restype=ctypes.c_uint32
 priority=k.GetPriorityClass(k.GetCurrentProcess())
print(json.dumps({'type':'result','request_id':r['request_id'],'epoch':r['epoch'],'threads':os.environ['OMP_NUM_THREADS'],'priority':priority}),flush=True)
""",
        "utf-8",
    )
    result = run_json_worker(
        [sys.executable, "-I", str(script)],
        {"request_id": "owned_worker", "epoch": 1},
        directory=tmp_path,
        cancelled=threading.Event(),
        on_progress=lambda _: None,
        log_path=tmp_path / "worker.jsonl",
        resource_policy=policy,
    )
    assert int(result["threads"]) == policy["cpu_threads"]
    if os.name == "nt":
        assert result["priority"] == (0x20 if mode == "accelerated" else 0x4000)
    assert json.loads((tmp_path / "worker.jsonl.limits.json").read_text("utf-8")) == policy


def test_release_action_pauses_local_jobs_without_cancelling_existing_question(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(OcrResources, "prepared", lambda self: True)

    async def no_worker(self):
        return None

    async def no_model_cancel(self, *args, **kwargs):
        raise AssertionError("local release must not cancel a question")

    monkeypatch.setattr(PreparationService, "open", no_worker)
    monkeypatch.setattr(GenerationService, "cancel", no_model_cancel)
    repo, course, _, request = setup(tmp_path)
    job = PreparationStore(repo).create_pdf(course, request, PROJECT_ROOT)
    repo.create_chat(course, "chat")
    pending = repo.publish_submission("chat", "existing question", "pending")
    original = repo.get_generation(pending["generation_id"])
    app = create_app(AppConfig(mode="test", data_dir=repo.root), repository=repo)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        csrf = client.get("/api/v1/session").json()["csrf_token"]
        response = client.post(
            "/api/v1/runtime/release-local-resources", headers={"X-Learning-CSRF": csrf}, json={}
        )
        assert response.status_code == 200 and response.json()["paused_local_jobs"] == [job.job_id]
        assert PreparationStore(repo).get(course, job.job_id).state == "paused"
        assert repo.get_generation(pending["generation_id"]) == original
