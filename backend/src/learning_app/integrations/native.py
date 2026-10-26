"""One frozen, read-only native query interface. URLs and provider protocols stay here."""

import re
import threading
from contextlib import nullcontext
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError
from sqlalchemy import insert, select, update

from learning_app.errors import AppError
from learning_app.workbench.preparations import PreparationStore

from . import schema as s
from .access import Grant
from .bilibili.catalogue import browse_catalogue, catalogue_link
from .bilibili.provider import video_url
from .common import content_hash as digest
from .models import NativeBilibiliQuery, VideoPart

PAGE_SIZE = 20


def input_links(text):
    """Capture only URLs actually submitted now; never infer a video from prose/history."""
    result = []
    for match in re.finditer(r'https://[^\s<>"()\[\]（）【】]+', text):
        value = match.group().rstrip(").,;!?]}，。；！？")
        try:
            parsed = urlsplit(value)
            if parsed.scheme != "https" or parsed.netloc not in {
                "www.bilibili.com",
                "bilibili.com",
                "space.bilibili.com",
            }:
                continue
            if parsed.netloc == "space.bilibili.com":
                value = catalogue_link(value).url
            elif re.fullmatch(r"/cheese/play/(ss|ep)\d+/?", parsed.path):
                value = "https://www.bilibili.com" + parsed.path.rstrip("/")
            elif parsed.path.startswith("/bangumi/"):
                from .bilibili.programme import programme_link

                value = programme_link(value).url
            else:
                if len(parse_qs(parsed.query).get("p", [])) > 1:
                    continue
                identity, page = video_url(value)
                value = (
                    "https://www.bilibili.com/video/" + identity + (f"?p={page}" if page else "")
                )
            if value not in result:
                result.append(value)
        except (AppError, ValueError):
            continue
    return result


def tasks_requested(text):
    return bool(re.search(r"B站|哔哩哔哩|bilibili", text, re.I)) and bool(
        re.search(r"任务|下载|字幕", text)
        and re.search(r"状态|进度|完成|失败|排队|暂停|查看", text)
    )


class NativeBilibili:
    def __init__(self, host):
        self.host = host
        self.lock = threading.RLock()

    def freeze(self, course_id, run_id, text, *, connection=None):
        links, tasks = input_links(text), tasks_requested(text)
        if not links and not tasks:
            return None
        module = next(
            v for v in self.host.access.modules(connection=connection) if v.platform == "bilibili"
        )
        if (
            not module.enabled
            or not module.connection_id
            or module.connection_state not in {"guest", "connected"}
        ):
            return None
        grant = self.host.access.grant(module.connection_id, connection=connection)
        frozen = {
            "course_id": course_id,
            "run_id": run_id,
            "grant": {
                key: getattr(grant, key)
                for key in ("connection_id", "module_epoch", "connection_epoch")
            },
            "links": [
                {"handle_id": "bili_link_" + digest({"run": run_id, "url": url}), "url": url}
                for url in links
            ],
            "tasks_allowed": tasks,
            "tasks": [],
        }
        if tasks:
            for job in PreparationStore(self.host.repository).list(
                course_id, connection=connection
            ):
                source = self.host.source(
                    job.asset_id, job.source_version, course_id, connection=connection
                )
                if source and source["connection_id"] == grant.connection_id:
                    frozen["tasks"].append(
                        {
                            "handle_id": "bili_task_" + digest({"run": run_id, "job": job.job_id}),
                            "job_id": job.job_id,
                        }
                    )
        return frozen

    def validate(self, frozen, arguments, *, connection=None):
        if not frozen:
            raise AppError("BILI_NATIVE_QUERY_DENIED", "本次输入没有获准的B站查询范围", 403)
        try:
            value = NativeBilibiliQuery.model_validate(arguments, strict=True)
        except ValidationError as error:
            raise AppError(
                "BILI_NATIVE_QUERY_ARGUMENTS", "B站查询只接受原句柄、操作及页码", 422
            ) from error
        self.host.access.check(Grant(**frozen["grant"]), connection=connection)
        if value.operation == "tasks":
            allowed = frozen["tasks_allowed"]
            handles = frozen["tasks"]
        else:
            allowed = bool(frozen["links"])
            handles = frozen["links"]
        if not allowed or (value.operation == "inspect" and value.handle_id is None):
            raise AppError("BILI_NATIVE_QUERY_DENIED", "此操作不属于本次明确提交的范围", 403)
        if value.handle_id is not None and not any(
            v["handle_id"] == value.handle_id for v in handles
        ):
            raise AppError("BILI_NATIVE_HANDLE_DENIED", "句柄不属于本次课程、账号和输入", 403)
        return value

    def _key(self, frozen, args_hash):
        return "bili_query_" + digest({"run_id": frozen["run_id"], "args_sha256": args_hash})

    def receipt(self, frozen, args_hash, *, connection=None):
        if not frozen:
            return None
        with (
            self.host.repository.engine.connect() if connection is None else nullcontext(connection)
        ) as conn:
            row = (
                conn.execute(
                    select(s.operations).where(
                        s.operations.c.operation_id == self._key(frozen, args_hash)
                    )
                )
                .mappings()
                .first()
            )
        if not row or row["state"] != "completed" or row["kind"] != "native_query":
            return None
        receipt = row["receipt"]
        if (
            row["course_id"] != frozen["course_id"]
            or row["connection_id"] != frozen["grant"]["connection_id"]
            or row["request_hash"] != digest({"scope": digest(frozen), "args": args_hash})
            or receipt["scope_hash"] != digest(frozen)
            or receipt["result_hash"] != digest(receipt["result"])
            or receipt["first_native_call"]["run_id"] != frozen["run_id"]
            or receipt["first_native_call"]["args_sha256"] != args_hash
        ):
            raise AppError("BILI_NATIVE_RECEIPT_CONFLICT", "原查询回执或归属不符", 409)
        return receipt["result"]

    def query(self, frozen, arguments, call, check):
        value = self.validate(frozen, arguments)
        if call["run_id"] != frozen["run_id"] or call["args_sha256"] != digest(arguments):
            raise AppError("BILI_NATIVE_QUERY_OWNER", "查询与本次原生调用身份不符", 403)
        key = self._key(frozen, call["args_sha256"])
        request_hash = digest({"scope": digest(frozen), "args": call["args_sha256"]})
        with self.lock:
            check()
            known = self.receipt(frozen, call["args_sha256"])
            if known is not None:
                return known
            with self.host.repository.engine.begin() as conn:
                if conn.execute(
                    select(s.operations.c.operation_id).where(s.operations.c.operation_id == key)
                ).first():
                    raise AppError("BILI_NATIVE_QUERY_UNKNOWN", "原查询结果待核对，未自动重发", 409)
                conn.execute(
                    insert(s.operations).values(
                        operation_id=key,
                        course_id=frozen["course_id"],
                        connection_id=frozen["grant"]["connection_id"],
                        kind="native_query",
                        request_hash=request_hash,
                        state="pending",
                    )
                )
            grant = replace(Grant(**frozen["grant"]), scope_check=check, operation_id=key)
            try:
                result = self._read(frozen, value, grant)
                check()
            except AppError as error:
                result = {
                    "status": "unknown" if error.status >= 500 else "unavailable",
                    "error_code": error.code,
                    "retried": False,
                    "media_opened": False,
                }
            receipt = {
                "result": result,
                "result_hash": digest(result),
                "scope_hash": digest(frozen),
                "first_native_call": call,
            }
            with self.host.repository.engine.begin() as conn:
                conn.execute(
                    update(s.operations)
                    .where(
                        s.operations.c.operation_id == key,
                        s.operations.c.request_hash == request_hash,
                    )
                    .values(state="completed", receipt=receipt)
                )
            return result

    def _read(self, frozen, value, grant):
        if value.operation == "inspect":
            link = next(v for v in frozen["links"] if v["handle_id"] == value.handle_id)
            if urlsplit(link["url"]).hostname == "space.bilibili.com":
                raw = browse_catalogue(
                    self.host.provider, grant, catalogue_link(link["url"]), value.page
                )
                return {k: v for k, v in raw.items() if k not in {"group_id", "videos"}} | {
                    "videos": [
                        {k: video[k] for k in ("bvid", "title", "ordinal")}
                        for video in raw["videos"]
                    ],
                    "status": "ready",
                    "coverage": "directory_only",
                    "media_opened": False,
                }
            if value.page != 1:
                raise AppError("BILI_NATIVE_PAGE_INVALID", "普通视频或课程目录没有此分页", 422)
            raw = self.host.provider.resolve(grant, link["url"])
            return {
                "status": "ready",
                "title": raw["title"],
                "complete": raw["complete"],
                "parts": [
                    VideoPart.model_validate(
                        {k: p[k] for k in VideoPart.model_fields if k in p}
                    ).model_dump()
                    for p in raw["parts"]
                ],
                "coverage": "directory_only",
                "media_opened": False,
                "imported": False,
                "ui_command": "/bilibili",
            }
        items = frozen["links"] if value.operation == "links" else frozen["tasks"]
        items = [v for v in items if value.handle_id is None or v["handle_id"] == value.handle_id]
        offset = (value.page - 1) * PAGE_SIZE
        if offset >= len(items) and value.page != 1:
            raise AppError("BILI_NATIVE_PAGE_INVALID", "冻结查询范围没有此页", 422)
        chosen = items[offset : offset + PAGE_SIZE]
        if value.operation == "tasks":
            store = PreparationStore(self.host.repository)
            chosen = [
                {
                    "handle_id": item["handle_id"],
                    **self._task(store.get(frozen["course_id"], item["job_id"]), frozen),
                }
                for item in chosen
            ]
        return {
            "status": "ready",
            "page": value.page,
            "total": len(items),
            "has_more": offset + len(chosen) < len(items),
            "items": chosen,
            "media_opened": False,
        }

    def _task(self, job, frozen):
        source = self.host.source(job.asset_id, job.source_version, frozen["course_id"])
        if not source or source["connection_id"] != frozen["grant"]["connection_id"]:
            raise AppError("BILI_NATIVE_TASK_DENIED", "任务不属于冻结的课程和连接", 403)
        return {
            "title": source["payload"]["title"],
            "kind": job.kind,
            "state": job.state,
            "revision": job.revision,
            "units_total": len(job.units),
            "units_completed": sum(u.state == "completed" for u in job.units),
            "error_codes": sorted({u.error_code for u in job.units if u.error_code}),
        }
