"""Reversibly exercise file operations on one explicitly selected isolated sample."""

import argparse
import hashlib
import json
import time
from pathlib import Path, PurePosixPath
from uuid import uuid4

from learning_app.course_files import (
    CourseFileStore,
    CourseMoveInput,
    CourseRenameInput,
)
from learning_app.integrations.host import IntegrationHost
from learning_app.media import MediaStore
from learning_app.storage import Repository

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--item", type=Path, required=True)
    args = parser.parse_args()
    item_path = args.item.resolve(strict=True)
    if not item_path.is_relative_to(ROOT / ".runtime/platforms"):
        raise RuntimeError("VERIFY_SCOPE_DENIED")
    item = json.loads(item_path.read_text("utf-8"))
    repo = Repository(ROOT / ".runtime/platforms/desktop/data")
    host = IntegrationHost(repo, ROOT)
    repo.integrations = host
    files, media = CourseFileStore(repo), MediaStore(repo)
    course, asset, version = item["course_id"], item["asset_id"], item["source_version"]
    evidence = (
        ROOT / ".runtime/platforms/evidence" / f"file-identity-{time.time_ns()}.json"
    )
    summary = {
        "passed": False,
        "restored": False,
        "needs_review": False,
        "operations": [],
    }

    def position():
        return next(v for v in media.list(course) if v["asset_id"] == asset)[
            "position_ms"
        ]

    def history():
        data = json.dumps(
            [repo.get_chat(c["chat_id"]) for c in repo.list_chats(course)],
            ensure_ascii=False,
            sort_keys=True,
        )
        if asset not in data or version not in data:
            raise RuntimeError("VERIFY_NO_EXISTING_QUESTION_FOR_SAMPLE")
        return hashlib.sha256(data.encode()).hexdigest()

    def mutate(method, value):
        summary["operations"].append(value.operation_id)
        try:
            return method(course, value)
        except Exception:
            # Reconcile the original receipt only; never dispatch the effect again.
            summary["needs_review"] = True
            status = files.operation(course, value.operation_id)
            if status["state"] == "completed" and status["result"]:
                summary["needs_review"] = False
                return status["result"]
            raise

    current = None
    try:
        source = repo.resolve_source(asset, version)
        root = Path(files.folder(course)["root_path"])
        if not root.resolve().is_relative_to(ROOT / ".runtime/platforms"):
            raise RuntimeError("VERIFY_COURSE_ROOT_SCOPE_DENIED")
        original = Path(source["local_path"]).relative_to(root).as_posix()
        if original != item["path"] or PurePosixPath(original).parent.as_posix() == ".":
            raise RuntimeError("VERIFY_EXPECTED_NESTED_SAMPLE_PATH")
        prior_position, prior_history = position(), history()
        if prior_position <= 0 or not host.source(asset, version, course)["local_path"]:
            raise RuntimeError("VERIFY_NEEDS_EXISTING_POSITION_AND_LOCAL_COPY")
        token = uuid4().hex
        current = original
        renamed = mutate(
            files.rename,
            CourseRenameInput(
                operation_id="verify_rename_" + token,
                path=current,
                name="核验-" + token[:8] + ".bili",
            ),
        )
        current = renamed["entry"]["path"]
        moved = mutate(
            files.move,
            CourseMoveInput(
                operation_id="verify_move_" + token, path=current, destination=""
            ),
        )
        current = moved["entry"]["path"]
        opened = files.open(course, current)
        if (
            opened["video"]["asset_id"] != asset
            or opened["video"]["source_version"] != version
        ):
            raise RuntimeError("VERIFY_IDENTITY_CHANGED")
        if position() != prior_position or history() != prior_history:
            raise RuntimeError("VERIFY_HISTORY_OR_POSITION_CHANGED")
        summary.update(
            passed=True,
            actual_existing_question_preserved=True,
            position_ms=prior_position,
            local_copy_kept=True,
        )
    finally:
        try:
            if current is not None and not summary["needs_review"]:
                parent = PurePosixPath(original).parent.as_posix()
                if PurePosixPath(current).parent.as_posix() != parent:
                    restored = mutate(
                        files.move,
                        CourseMoveInput(
                            operation_id="verify_restore_move_" + token,
                            path=current,
                            destination=parent,
                        ),
                    )
                    current = restored["entry"]["path"]
                if current != original:
                    restored = mutate(
                        files.rename,
                        CourseRenameInput(
                            operation_id="verify_restore_name_" + token,
                            path=current,
                            name=PurePosixPath(original).name,
                        ),
                    )
                    current = restored["entry"]["path"]
                summary["restored"] = (
                    current == original
                    and position() == prior_position
                    and history() == prior_history
                )
            if not summary["restored"]:
                summary["passed"] = False
        except Exception:
            summary.update(passed=False, needs_review=True)
            raise
        finally:
            try:
                evidence.write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2), "utf-8"
                )
                print(json.dumps(summary, ensure_ascii=False))
            finally:
                repo.close()
    if not summary["passed"]:
        raise RuntimeError("VERIFY_FILE_IDENTITY_FAILED")


if __name__ == "__main__":
    main()
