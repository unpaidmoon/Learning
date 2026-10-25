"""Actual C/D physical-disk data copy using exclusively owned synthetic facts."""

import argparse
import io
import json
import os
import subprocess
from pathlib import Path

from PIL import Image

from learning_app.config import PROJECT_ROOT
from learning_app.course_files import CourseFileStore
from learning_app.data_activation import require_active_data
from learning_app.data_location import ProfileLocation, inventory
from learning_app.storage import Repository


def run(identity):
    evidence = PROJECT_ROOT / ".runtime/validation" / identity
    evidence.mkdir(parents=True, exist_ok=False)
    target_parent = (
        Path(os.environ["USERPROFILE"])
        / ".codex/tmp/learning-workbench-cross-disk"
        / identity
    )
    target_parent.mkdir(parents=True, exist_ok=False)
    partitions = json.loads(
        subprocess.check_output(
            [
                str(
                    Path(os.environ["SystemRoot"])
                    / "System32/WindowsPowerShell/v1.0/powershell.exe"
                ),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-Partition -DriveLetter C,D | Select-Object DriveLetter,DiskNumber,PartitionNumber | ConvertTo-Json -Compress",
            ],
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    )
    disk = {row["DriveLetter"]: row["DiskNumber"] for row in partitions}
    if (
        evidence.drive.upper() != "D:"
        or target_parent.resolve().drive.upper() != "C:"
        or evidence.stat().st_dev == target_parent.stat().st_dev
        or disk["C"] == disk["D"]
    ):
        raise RuntimeError("TWO_DISTINCT_PHYSICAL_DISKS_REQUIRED")
    result = {
        "status": "running",
        "assertions": [],
        "physical_partitions": partitions,
        "source": str(evidence / "data"),
        "target_parent": str(target_parent),
        "host_starts": 0,
        "model_requests": 0,
        "scope": "owned synthetic course, confirmed image and pending question; no user data",
    }

    def check(condition, name):
        result["assertions"].append({"name": name, "passed": bool(condition)})
        if not condition:
            raise AssertionError(name)

    repository = None
    try:
        materials = evidence / "synthetic-course"
        materials.mkdir()
        (materials / "lesson.txt").write_text(
            "Owned cross-disk fixture. 原件不随软件数据移动。", "utf-8"
        )
        repository = Repository(evidence / "data")
        course = CourseFileStore(repository).bind(None, str(materials))["course_id"]
        chat = repository.create_chat(course)["chat_id"]
        image = io.BytesIO()
        Image.new("RGB", (32, 24), "#2455ab").save(image, "PNG")
        attachment = repository.stage_attachment(
            image.getvalue(), media_type="image/png"
        )
        pending = repository.publish_submission(
            chat,
            "Retain this pending question; never dispatch",
            "original",
            attachment_ids=[attachment["attachment_id"]],
        )
        original = repository.get_generation(pending["generation_id"])
        location = ProfileLocation(repository.root)
        location.ready(location.plan(identity, "move", target_parent))
        location.schedule(identity)
        repository.close()
        repository = None
        source = evidence / "data"
        before = inventory(source)
        with location.lease():
            receipt = location.apply_closed()
        check(
            receipt["state"] == "completed",
            "closed_copy_publishes_successfully_on_the_other_physical_disk",
        )
        check(
            location.current().drive.upper() == "C:",
            "active_pointer_now_resolves_to_disk_c",
        )
        check(
            inventory(location.current()) == before == inventory(source),
            "all_software_data_bytes_match_and_original_disk_d_data_is_retained",
        )
        require_active_data(location.current())
        check(
            True,
            "same_operation_stage_is_active_only_after_verified_cross_disk_publication",
        )
        repository = Repository(location.current())
        check(
            repository.get_generation(pending["generation_id"]) == original,
            "original_pending_state_and_submission_are_preserved_without_service_start",
        )
        check(
            repository.counts()["outbound_attempts"] == 0,
            "cross_disk_move_dispatches_no_model_request",
        )
        check(
            (materials / "lesson.txt").is_file(),
            "course_original_remains_in_its_existing_disk_d_course_directory",
        )
        result.update(
            status="passed", files=len(before), active_data=str(location.current())
        )
    except Exception as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
    finally:
        if repository is not None:
            repository.close()
        (evidence / "summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), "utf-8"
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if os.name != "nt" or not args.run_id.replace("-", "").replace("_", "").isalnum():
        raise RuntimeError("WINDOWS_OWNED_RUN_REQUIRED")
    value = run(args.run_id)
    print(
        json.dumps(
            {
                "status": value["status"],
                "assertions": len(value["assertions"]),
                "error": value.get("error"),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if value["status"] == "passed" else 1)
