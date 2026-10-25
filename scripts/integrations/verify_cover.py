"""Verify one explicitly selected isolated sample's cover without exposing its pixels."""

import argparse
import hashlib
import json
import time
from pathlib import Path

from sqlalchemy import func, select

from learning_app.integrations import schema as s
from learning_app.integrations.host import IntegrationHost
from learning_app.storage import Repository

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--item", type=Path, required=True)
    args = parser.parse_args()
    path = args.item.resolve(strict=True)
    if not path.is_relative_to(ROOT / ".runtime/platforms"):
        raise RuntimeError("VERIFY_SCOPE_DENIED")
    item = json.loads(path.read_text("utf-8"))
    repo = Repository(ROOT / ".runtime/platforms/desktop/data")
    try:
        host = IntegrationHost(repo, ROOT)
        repo.integrations = host
        scope = item["course_id"], item["asset_id"], item["source_version"]
        before = host.source(scope[1], scope[2], scope[0])
        with repo.engine.connect() as conn:
            dispatched = conn.execute(
                select(func.count()).select_from(s.requests)
            ).scalar_one()
        response = host.covers.response(*scope)
        after = host.source(scope[1], scope[2], scope[0])
        if before != after:
            raise RuntimeError("VERIFY_SOURCE_CHANGED")
        reopened = IntegrationHost(repo, ROOT)
        if (
            reopened.covers.response(*scope).body != response.body
            or reopened._provider is not None
        ):
            raise RuntimeError("VERIFY_COVER_CACHE_NOT_REUSED")
        with repo.engine.connect() as conn:
            requests = (
                conn.execute(select(func.count()).select_from(s.requests)).scalar_one()
                - dispatched
            )
        summary = {
            "actual_platform_cover": True,
            "bytes": len(response.body),
            "sha256": hashlib.sha256(response.body).hexdigest(),
            "media_type": response.media_type,
            "platform_requests": requests,
            "cache_reused_after_host_reopen": True,
            "source_unchanged": True,
            "image_pixels_exposed_to_model": False,
            "video_media_requested": False,
        }
        evidence = ROOT / ".runtime/platforms/evidence" / f"cover-{time.time_ns()}.json"
        evidence.write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
        print(json.dumps(summary, ensure_ascii=False))
    finally:
        repo.close()


if __name__ == "__main__":
    main()
