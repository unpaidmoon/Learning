"""Verify selected metadata pages in the isolated account; never import or open media."""

import argparse
import json
import time
from pathlib import Path

from sqlalchemy import func, select

from learning_app.course_files import CourseFileStore
from learning_app.integrations import schema as s
from learning_app.integrations.bilibili.favorites import Favorites
from learning_app.integrations.host import IntegrationHost
from learning_app.integrations.routes import FavoriteBrowse, FavoriteFolderBrowse
from learning_app.storage import Repository

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--item", type=Path, required=True)
    args = parser.parse_args()
    item_path = args.item.resolve(strict=True)
    if not item_path.is_relative_to(ROOT / ".runtime/platforms"):
        raise RuntimeError("VERIFY_ITEM_SCOPE_DENIED")
    item = json.loads(item_path.read_text("utf-8"))
    repo = Repository(ROOT / ".runtime/platforms/desktop/data")
    evidence = ROOT / ".runtime/platforms/evidence" / f"favorites-{time.time_ns()}.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    result = {"passed": False, "media_opened": False, "model_called": False}
    try:
        course_root = Path(CourseFileStore(repo).folder(item["course_id"])["root_path"])
        if not course_root.resolve().is_relative_to(ROOT / ".runtime/platforms"):
            raise RuntimeError("VERIFY_COURSE_SCOPE_DENIED")
        host = IntegrationHost(repo, ROOT)
        module = next(m for m in host.access.modules() if m.platform == "bilibili")
        if not module.enabled or module.connection_state != "connected":
            raise RuntimeError("VERIFY_CONNECTED_ACCOUNT_REQUIRED")
        connection = module.connection_id
        with repo.engine.connect() as conn:
            before = conn.execute(
                select(func.count()).select_from(s.sources)
            ).scalar_one()
        adapter = Favorites(host)
        created = adapter.folder_page(FavoriteFolderBrowse(connection_id=connection))
        collected = adapter.folder_page(
            FavoriteFolderBrowse(connection_id=connection, kind="collected")
        )
        result["created_folder_page_valid"] = True
        result["collected_folder_page_valid"] = True
        result["collected_directory_empty"] = not collected["items"]
        if not created["items"]:
            raise RuntimeError("VERIFY_NO_CREATED_FOLDER_SAMPLE")
        folder = created["items"][0]
        page = adapter.browse(
            FavoriteBrowse(connection_id=connection, folder_id=folder["folder_id"])
        )
        last = max(1, (page["total"] + 19) // 20)
        selected = [page]
        for number in dict.fromkeys([2 if last > 1 else 1, last]):
            if number != 1:
                selected.append(
                    adapter.browse(
                        FavoriteBrowse(
                            connection_id=connection,
                            folder_id=folder["folder_id"],
                            page=number,
                        )
                    )
                )
        followed = []
        seen_types = set()
        for row in collected["items"]:
            if row["resource_type"] in seen_types:
                continue
            seen_types.add(row["resource_type"])
            value = FavoriteBrowse(
                connection_id=connection,
                kind="collected",
                folder_id=row["folder_id"],
                resource_type=row["resource_type"],
            )
            first = adapter.browse(value)
            count = max(1, (first["total"] + 19) // 20)
            pages = [first]
            for number in dict.fromkeys([2 if count > 1 else 1, count]):
                if number != 1:
                    pages.append(
                        adapter.browse(value.model_copy(update={"page": number}))
                    )
            if pages[-1]["has_more"]:
                raise RuntimeError("VERIFY_FOLLOWED_LAST_PAGE_INVALID")
            followed.append(
                {
                    "resource_type": row["resource_type"],
                    "first_second_last_verified": count > 2,
                }
            )
        result["followed_types_verified"] = followed
        with repo.engine.connect() as conn:
            after = conn.execute(
                select(func.count()).select_from(s.sources)
            ).scalar_one()
        result.update(
            passed=before == after,
            source_count_unchanged=before == after,
            multi_page_sample=last > 1,
            first_second_last_verified=last > 2,
            last_page_has_more=selected[-1]["has_more"],
            pages=[
                {
                    "page": p["page"],
                    "total": p["total"],
                    "rows": len(p["items"]),
                    "has_more": p["has_more"],
                    "unavailable": sum(not v["available"] for v in p["items"]),
                }
                for p in selected
            ],
        )
        if not result["passed"] or selected[-1]["has_more"]:
            raise RuntimeError("VERIFY_FAVORITE_PAGE_INVARIANT_FAILED")
    finally:
        evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
        print(
            json.dumps(
                {k: v for k, v in result.items() if k != "pages"}, ensure_ascii=False
            )
        )
        print(json.dumps({"private_evidence": str(evidence)}, ensure_ascii=False))
        repo.close()


if __name__ == "__main__":
    main()
