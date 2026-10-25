"""Explicit, bounded authorized ordinary-video verification; never print CDN URLs."""

import json
import argparse
import threading
import time

from learning_app.config import PROJECT_ROOT
from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.integrations.host import IntegrationHost
from learning_app.integrations.models import ImportSelection, ResolveInput
from learning_app.storage import Repository


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url", required=True, help="本次明确授权的视频链接；仅首个返回分P读取媒体"
    )
    parser.add_argument("--save-local", action="store_true")
    args = parser.parse_args()
    root = PROJECT_ROOT / ".runtime/platforms/authorized-sample"
    root.mkdir(parents=True, exist_ok=True)
    repo = Repository(root / "data")
    host = IntegrationHost(repo, PROJECT_ROOT)
    repo.integrations = host
    course_root = root / "course"
    course_root.mkdir(exist_ok=True)
    course_id = "bili_sample"
    if not any(course["course_id"] == course_id for course in repo.list_courses()):
        repo.create_course("B站指定样例验证", course_id)
    CourseFileStore(repo).bind(course_id, str(course_root))
    host.access.change("bilibili", True)
    connections = host.access.modules()[0].connection_id
    connection_id = connections or host.access.connect().connection_id
    result = {
        "scope": "metadata: specified URL; media/subtitle: first returned part only"
    }
    try:
        plan = host.resolve(
            course_id, ResolveInput(connection_id=connection_id, url=args.url)
        )
        result["plan"] = plan.model_dump()
        imported = host.import_selected(
            course_id,
            ImportSelection(
                operation_id="sample_" + plan.plan_id,
                plan_id=plan.plan_id,
                object_ids=[plan.parts[0].object_id],
            ),
        )
        result["import"] = imported.model_dump()
        item = imported.items[0]
        media = host.media.session(host.source(item.asset_id, item.source_version))
        result["media"] = {
            "state": "resolved",
            "duration_ms": media["duration_ms"],
            "split_audio": bool(media["audio"]),
            "video_codec": media["video"].get("vcodec"),
        }
        for name in ["video", "audio"]:
            if media[name]:
                client, response = host.media.upstream(
                    media["grant"],
                    media[name]["url"],
                    "bytes=0-1023",
                    expected_cid=media["cid"],
                )
                try:
                    data = response.read()
                    result[name + "_range"] = {
                        "http": response.status_code,
                        "bytes": len(data),
                        "range": response.headers.get("content-range"),
                    }
                finally:
                    response.close()
                    client.close()
        try:
            result["subtitle"] = host.subtitles(
                course_id, item.asset_id, item.source_version
            ).model_dump()
        except AppError as exc:
            result["subtitle"] = {"state": "failed", "code": exc.code}
        if args.save_local:
            from learning_app.workbench.preparations import PreparationStore

            store = PreparationStore(repo)
            job = host.queue(
                course_id, item.asset_id, item.source_version, "platform_download"
            )
            lease = store.claim()
            if lease:
                outcome = host.prepare(lease, threading.Event(), lambda *values: None)
                store.finish(lease, outcome)
            result["download"] = store.get(course_id, job.job_id).model_dump()
            row = host.source(item.asset_id, item.source_version)
            path = host.media.local(row)
            result["local_copy"] = {
                "bytes": path.stat().st_size if path else None,
                "asset_id": item.asset_id,
                "source_version": item.source_version,
            }
    except AppError as exc:
        result["failure"] = {"code": exc.code, "message": exc.message}
    finally:
        repo.close()
        output = root / f"result-{time.time_ns()}.json"
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
