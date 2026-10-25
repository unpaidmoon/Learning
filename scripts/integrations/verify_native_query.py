"""One explicit imported sample → real native model query; no import or media access."""

import argparse
import json
import time
import unicodedata
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import func, select

from learning_app.course_files import CourseFileStore
from learning_app.integrations import schema as s
from learning_app.integrations.host import IntegrationHost
from learning_app.storage import Repository
from learning_app.storage import schema as business
from learning_app.workbench import schema as workbench
from learning_app.workbench.preparations import PreparationStore

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / ".runtime/platforms/desktop/data"


def images(value):
    if isinstance(value, list):
        return sum(images(v) for v in value)
    if isinstance(value, dict):
        return int(
            isinstance(value.get("type"), str)
            and value["type"] in {"image", "image_url", "input_image"}
        ) + sum(images(v) for v in value.values())
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--item", type=Path, required=True)
    parser.add_argument(
        "--query-kind", choices=["directory", "tasks"], default="directory"
    )
    args = parser.parse_args()
    path = args.item.resolve(strict=True)
    if not path.is_relative_to(ROOT / ".runtime/platforms"):
        raise RuntimeError("VERIFY_ITEM_SCOPE_DENIED")
    item = json.loads(path.read_text("utf-8"))
    evidence = (
        ROOT / ".runtime/platforms/evidence" / ("native-model-" + uuid4().hex + ".json")
    )
    evidence.parent.mkdir(parents=True, exist_ok=True)
    summary = {"submission_id": "verify_bili_query_" + uuid4().hex, "passed": False}
    repo = Repository(DATA)
    host = IntegrationHost(repo, ROOT)
    repo.integrations = host
    try:
        source = host.source(
            item["asset_id"], item["source_version"], item["course_id"]
        )
        root = Path(CourseFileStore(repo).folder(item["course_id"])["root_path"])
        if not root.resolve().is_relative_to(ROOT / ".runtime/platforms") or not source:
            raise RuntimeError("VERIFY_COURSE_SCOPE_DENIED")
        part = source["payload"]
        url = part.get("url") or ("https://www.bilibili.com/video/" + part["bvid"])
        chats = [
            repo.get_chat(c["chat_id"]) for c in repo.list_chats(item["course_id"])
        ]
        usable = [chat for chat in chats if chat["generations"]]
        if not usable:
            raise RuntimeError("VERIFY_REQUIRES_EXISTING_CHAT")
        chat = max(usable, key=lambda c: max(g["created_at"] for g in c["generations"]))
        latest = max(chat["generations"], key=lambda g: g["created_at"])
        binding = repo.current_binding(chat["chat_id"])
        selected = (
            binding.get("adapter_metadata", {}).get("desktop_selection") or latest
        )
        prompt = (
            "请本次通过B站只读查询重新读取下面链接当前的分P目录，不沿用历史查询结果。告诉我共有几P，并列出各P的原始标题。不要下载、不要导入，也不要读取字幕或画面。链接："
            + url
            if args.query_kind == "directory"
            else "请只读查看当前课程B站下载和字幕任务的状态，逐项列出原始标题和状态。不要启动、恢复、重试或取消任何任务，不要读取字幕、音频或画面。"
        )
        request = {
            "chat_id": chat["chat_id"],
            "submission_id": summary["submission_id"],
            "text": prompt,
            "model": selected["model"],
            "thinking": selected["thinking"],
        }
        with repo.engine.connect() as conn:
            sources_before = conn.execute(
                select(func.count()).select_from(s.sources)
            ).scalar_one()
            requests_before = conn.execute(
                select(func.count()).select_from(s.requests)
            ).scalar_one()
        jobs_before = len(PreparationStore(repo).list(item["course_id"]))
        with httpx.Client(
            base_url="http://127.0.0.1:8876", trust_env=False, timeout=15
        ) as client:
            health = client.get("/api/v1/health").json()
            record = json.loads(
                (
                    ROOT
                    / ".runtime/desktop/app/run/apps"
                    / health["instance_id"]
                    / "run.json"
                ).read_text("utf-8")
            )
            if record["status"] != "ready" or record["backend_port"] != 8876:
                raise RuntimeError("VERIFY_INSTANCE_MISMATCH")
            client.headers["X-Learning-CSRF"] = client.get("/api/v1/session").json()[
                "csrf_token"
            ]
            evidence.write_text(json.dumps(summary, indent=2), "utf-8")
            try:
                response = client.post("/api/v1/submissions", json=request)
                response.raise_for_status()
                receipt = response.json()
            except httpx.HTTPError:
                lookup = client.get("/api/v1/submissions/" + summary["submission_id"])
                if lookup.status_code != 200:
                    raise RuntimeError("VERIFY_SUBMISSION_UNKNOWN_NOT_RESENT") from None
                receipt = lookup.json()
            generation_id = summary["generation_id"] = receipt["generation_id"]
            evidence.write_text(json.dumps(summary, indent=2), "utf-8")
            deadline = time.monotonic() + 360
            while True:
                response = client.get("/api/v1/generations/" + generation_id)
                response.raise_for_status()
                view = response.json()
                if view["state"] in {"completed", "failed", "cancelled", "truncated"}:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("VERIFY_GENERATION_PENDING_NOT_REDISPATCHED")
                time.sleep(0.5)
            summary["state"] = view["state"]
            if view["state"] != "completed":
                raise RuntimeError(
                    "VERIFY_MODEL_QUERY_FAILED:" + str(view.get("error_code"))
                )
        generation = repo.get_generation(generation_id)
        with repo.engine.connect() as conn:
            operations = list(conn.execute(select(s.operations)).mappings())
            operations = [
                op
                for op in operations
                if op["kind"] == "native_query"
                and op["receipt"]
                and op["receipt"]["first_native_call"]["run_id"] == generation_id
            ]
            attempts = list(
                conn.execute(
                    select(business.outbound_attempts).where(
                        business.outbound_attempts.c.generation_id == generation_id
                    )
                ).mappings()
            )
            usage = list(
                conn.execute(
                    select(business.usage_records).where(
                        business.usage_records.c.generation_id == generation_id
                    )
                ).mappings()
            )
            sources_after = conn.execute(
                select(func.count()).select_from(s.sources)
            ).scalar_one()
        inspected = [
            op["receipt"]["result"]
            for op in operations
            if (
                op["receipt"]["result"].get("coverage") == "directory_only"
                if args.query_kind == "directory"
                else "items" in op["receipt"]["result"]
            )
        ]
        summary.update(
            query_kind=args.query_kind,
            actual_model_attempts=sum(bool(a["actual_outbound"]) for a in attempts),
            usage_record_count=len(usage),
            fresh_query_receipts=len(inspected),
            source_count_unchanged=sources_after == sources_before,
        )
        if len(inspected) != 1 or not attempts or sources_after != sources_before:
            raise RuntimeError("VERIFY_NATIVE_READ_OR_SOURCE_INVARIANT_FAILED")
        directory = inspected[0]
        normalized = unicodedata.normalize(
            "NFKC", view["body"].replace("\\", "")
        ).casefold()
        title_rows = (
            directory["parts"] if args.query_kind == "directory" else directory["items"]
        )
        titles_present = all(
            unicodedata.normalize("NFKC", p["title"]).casefold() in normalized
            for p in title_rows
        )
        image_blocks = 0
        for attempt in attempts:
            raw = (
                DATA
                / "execution/boundary/private/boundary"
                / (attempt["request_id"] + ".request.json")
            )
            image_blocks += images(json.loads(raw.read_text("utf-8")))
        until = time.monotonic() + 20
        while True:
            with repo.engine.connect() as conn:
                released = (
                    conn.execute(
                        select(workbench.native_scopes.c.state).where(
                            workbench.native_scopes.c.run_id == generation_id
                        )
                    ).scalar_one()
                    == "released"
                )
            if released or time.monotonic() > until:
                break
            time.sleep(0.2)
        summary.update(
            model=generation["model"],
            thinking=generation["thinking"],
            max_output_tokens=generation["max_output_tokens"],
            timeout_seconds=generation["timeout_seconds"],
            actual_model_attempts=sum(bool(a["actual_outbound"]) for a in attempts),
            usage_record_count=len(usage),
            usage=[
                {
                    k: row[k]
                    for k in ("source", "raw_usage", "field_presence", "normalized")
                }
                for row in usage
            ],
            query_kind=args.query_kind,
            result_items=len(title_rows),
            answer_contains_all_original_titles=titles_present,
            image_blocks=image_blocks,
            source_count_unchanged=True,
            native_scope_released=released,
            answer_chars=len(view["body"]),
            same_existing_chat=True,
            preparation_count_unchanged=len(
                PreparationStore(repo).list(item["course_id"])
            )
            == jobs_before,
        )
        with repo.engine.connect() as conn:
            summary["platform_requests"] = (
                conn.execute(select(func.count()).select_from(s.requests)).scalar_one()
                - requests_before
            )
        if args.query_kind == "tasks" and summary["platform_requests"] != 0:
            raise RuntimeError("VERIFY_LOCAL_TASK_QUERY_MADE_PLATFORM_REQUESTS")
        summary["passed"] = (
            image_blocks == 0
            and titles_present
            and released
            and summary["actual_model_attempts"] > 0
            and bool(usage)
            and summary["preparation_count_unchanged"]
            and all(row["raw_usage"] is not None for row in usage)
        )
        if not summary["passed"]:
            raise RuntimeError("VERIFY_NATIVE_MODEL_EVIDENCE_INCOMPLETE")
    finally:
        evidence.write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
        print(
            json.dumps(
                {
                    k: v
                    for k, v in summary.items()
                    if k not in {"usage", "submission_id", "generation_id"}
                },
                ensure_ascii=False,
            )
        )
        print(json.dumps({"private_evidence": str(evidence)}, ensure_ascii=False))
        repo.close()


if __name__ == "__main__":
    main()
