"""Explicit research-only external reads, immutable evidence and durable receipts."""

from dataclasses import asdict

import httpx
from sqlalchemy import insert, select, update

from learning_app.errors import AppError
from learning_app.storage.repository import _hash

from . import research_schema as r
from . import schema as s
from .common import now
from .external_http import DeepSeekWebSearch, PublicTextReader, public_url
from .models import WebEvidenceRef
from .sources import SourceStore
from .task_materials import material

EXTERNAL_TOOLS = frozenset({"search_research_web", "read_research_page"})
EXTERNAL_CHUNK = 24_000


class ResearchExternal:
    def __init__(self, repository, *, search=None, reader=None):
        self.repository = repository
        self.search = search or DeepSeekWebSearch()
        self.reader = reader or PublicTextReader()

    @staticmethod
    def scope(conn, task):
        if task["kind"] != "research" or not task["allow_external"] or task["sources_only"]:
            return None
        sources = conn.execute(
            select(s.task_sources).where(
                s.task_sources.c.task_id == task["task_id"],
                s.task_sources.c.excluded_at_scope_revision.is_(None),
            )
        ).mappings()
        source = next(
            (row for row in sources if row["declaration"]["kind"] == "external_research"), None
        )
        if source is None:
            return None
        return {
            "source_id": source["source_id"],
            "epoch": (source["frozen"] or {}).get("external_epoch", 0),
            "urls": source["declaration"].get("urls", []),
        }

    @classmethod
    def authorize(cls, conn, task, scope, arguments, name):
        expected = scope["grants"].get("research", {}).get("external")
        current = cls.scope(conn, task)
        if not current or current != expected:
            raise AppError(
                "RESEARCH_EXTERNAL_SCOPE_DENIED", "本次未获外部研究许可或原范围已变化", 403
            )
        if name == "search_research_web":
            if (
                not isinstance(arguments, dict)
                or set(arguments) != {"query"}
                or not isinstance(arguments["query"], str)
                or len(arguments["query"]) > 320
                or not 2 <= len(arguments["query"].strip()) <= 320
                or any(ord(c) < 32 for c in arguments["query"])
                or current["urls"]
            ):
                raise AppError(
                    "RESEARCH_QUERY_SCOPE_DENIED",
                    "检索只接收本研究的短查询；指定网页任务不扩为全网检索",
                    403,
                )
        elif name == "read_research_page":
            if not isinstance(arguments, dict) or set(arguments) != {"url"}:
                raise AppError("RESEARCH_URL_ARGUMENTS", "网页读取参数无效", 422)
            address = public_url(arguments["url"])
            if current["urls"] and address not in current["urls"]:
                raise AppError("RESEARCH_URL_SCOPE_DENIED", "网页不在用户指定的外部资料范围内", 403)
        else:
            raise AppError("RESEARCH_EXTERNAL_TOOL_DENIED", "此入口不授予其他外部操作", 403)
        return current

    @staticmethod
    def source_state(conn, source):
        epoch = (source["frozen"] or {}).get("external_epoch", 0)
        known_gap = any(
            (unit["work_spec"] or {}).get("research", {}).get("external")
            == {
                "source_id": source["source_id"],
                "epoch": epoch,
                "urls": source["declaration"].get("urls", []),
            }
            for unit in conn.execute(
                select(s.task_units).where(
                    s.task_units.c.task_id == source["task_id"],
                    s.task_units.c.state == "failed",
                    s.task_units.c.error_code == "RESEARCH_EXTERNAL_GAPS",
                    s.task_units.c.excluded_at_scope_revision.is_(None),
                )
            ).mappings()
        )
        reads = list(
            conn.execute(
                select(r.external_reads)
                .where(
                    r.external_reads.c.source_id == source["source_id"],
                    r.external_reads.c.source_epoch == epoch,
                )
                .order_by(r.external_reads.c.created_at)
            ).mappings()
        )
        if not reads:
            return (
                ("blocked", "RESEARCH_EXTERNAL_EVIDENCE_MISSING")
                if known_gap
                else ("preparing", None)
            )
        if any(read["state"] == "started" for read in reads):
            return "blocked", "RESEARCH_EXTERNAL_READ_UNKNOWN"
        urls = source["declaration"].get("urls", [])
        if urls:
            latest = {
                row["request"]["url"]: row
                for row in reads
                if row["native_call"]["tool_name"] == "read_research_page"
            }
            missing = [
                url
                for url in urls
                if url not in latest
                or latest[url]["state"] != "completed"
                or not latest[url]["result"].get("sources")
            ]
            if missing:
                failed = next((latest[url] for url in missing if url in latest), None)
                return (
                    ("blocked", failed["error_code"] or "RESEARCH_EXTERNAL_EVIDENCE_MISSING")
                    if failed
                    else ("blocked", "RESEARCH_EXTERNAL_EVIDENCE_MISSING")
                    if known_gap
                    else ("preparing", None)
                )
            if source["declaration"].get("complete_text"):
                for row in latest.values():
                    unread = {h["handle_id"] for h in row["result"].get("unread_continuations", [])}
                    if unread:
                        actually_read = set(
                            conn.execute(
                                select(s.native_read_receipts.c.snapshot_id).where(
                                    s.native_read_receipts.c.run_id.in_(
                                        select(s.task_runs.c.run_id).where(
                                            s.task_runs.c.task_id == source["task_id"]
                                        )
                                    ),
                                    s.native_read_receipts.c.snapshot_id.in_(unread),
                                )
                            ).scalars()
                        )
                        if unread - actually_read:
                            return (
                                ("blocked", "RESEARCH_EXTERNAL_TEXT_NOT_READ")
                                if known_gap
                                else ("preparing", None)
                            )
        elif not any(row["state"] == "completed" and row["result"].get("sources") for row in reads):
            return "blocked", next(
                (row["error_code"] for row in reversed(reads) if row["error_code"]),
                "RESEARCH_EXTERNAL_EVIDENCE_MISSING",
            )
        return "ready", None

    @staticmethod
    def _row(conn, effect):
        return (
            conn.execute(select(r.external_reads).where(r.external_reads.c.effect_id == effect))
            .mappings()
            .first()
        )

    @classmethod
    def receipt(cls, conn, call):
        row = cls._row(conn, _hash(asdict(call)))
        if (
            not row
            or row["native_call"] != asdict(call)
            or row["state"] not in {"completed", "failed"}
            or not row["result"]
            or _hash(row["result"]) != row["result_hash"]
        ):
            return None
        return row["result"]

    @staticmethod
    def is_external_snapshot(conn, course_id, identity):
        seen = set()
        for _ in range(17):
            if identity in seen:
                raise AppError("SOURCE_REFERENCE_DEPTH", "来源分段链无法核对")
            seen.add(identity)
            snapshot = SourceStore.in_transaction(conn, course_id, identity)
            if snapshot.reference.kind != "snapshot_slice":
                return snapshot.reference.kind == "web_evidence"
            identity = snapshot.reference.parent_snapshot_id
        raise AppError("SOURCE_REFERENCE_DEPTH", "来源分段层次超出范围")

    @staticmethod
    def cited_sources(conn, run_id):
        result = set()
        course = conn.execute(
            select(s.tasks.c.course_id)
            .join(s.task_runs, s.task_runs.c.task_id == s.tasks.c.task_id)
            .where(s.task_runs.c.run_id == run_id)
        ).scalar_one()
        for row in conn.execute(
            select(r.external_reads).where(
                r.external_reads.c.run_id == run_id, r.external_reads.c.state == "completed"
            )
        ).mappings():
            if row["result_hash"] != _hash(row["result"]):
                raise AppError("RESEARCH_EXTERNAL_RECEIPT_CORRUPT", "外部来源回执摘要不符")
            result.update(
                item["source"]["snapshot_id"] for item in row["result"].get("sources", [])
            )
        # An unread page continuation is not evidence. Only the exact committed
        # read_source_handle receipt admits it to the citation set.
        for row in conn.execute(
            select(s.native_read_receipts).where(s.native_read_receipts.c.run_id == run_id)
        ).mappings():
            if row["result_hash"] != _hash(row["result"]):
                raise AppError("RESEARCH_EXTERNAL_RECEIPT_CORRUPT", "来源读取回执摘要不符")
            if ResearchExternal.is_external_snapshot(conn, course, row["snapshot_id"]):
                result.add(row["snapshot_id"])
        return result

    @classmethod
    def validate_output(cls, conn, task, run, body):
        context = (run["input_contract"] or {}).get("research", {})
        external = context.get("external")
        if not external or context.get("phase") == "synthesis":
            return set()
        if cls.scope(conn, task) != external:
            raise AppError("RESEARCH_EXTERNAL_SCOPE_CHANGED", "外部研究范围已改变，原结果保留")
        source = (
            conn.execute(
                select(s.task_sources).where(s.task_sources.c.source_id == external["source_id"])
            )
            .mappings()
            .one()
        )
        state, error = cls.source_state(conn, source)
        if state != "ready":
            raise AppError(
                "RESEARCH_EXTERNAL_GAPS",
                "指定外部证据尚有缺口，原模型草稿保留；请明确重试、排除或接受部分",
                409,
            )
        allowed = cls.cited_sources(conn, run["run_id"])
        cited = {identity for block in body.blocks for identity in block.source_snapshot_ids}
        if not allowed & cited:
            raise AppError(
                "RESEARCH_EXTERNAL_CITATION_MISSING", "外部研究报告须引用本轮实际取得的外部证据"
            )
        for url in external["urls"]:
            rows = list(
                conn.execute(
                    select(r.external_reads).where(
                        r.external_reads.c.run_id == run["run_id"],
                        r.external_reads.c.state == "completed",
                    )
                ).mappings()
            )
            candidates = {
                item["handle_id"]
                for row in rows
                if row["request"].get("url") == url
                for item in row["result"].get("sources", [])
                + row["result"].get("unread_continuations", [])
            }
            if not candidates & cited & allowed:
                raise AppError("RESEARCH_EXTERNAL_CITATION_MISSING", "报告遗漏指定外部资料的依据")
        return allowed

    def invoke(self, business, call, arguments):
        effect = _hash(asdict(call))
        cached = None
        with self.repository._lock, self.repository.engine.begin() as conn:
            scope = business._authorize(conn, call, arguments)
            run, task, _ = business._task(conn, call.run_id)
            external = self.authorize(conn, task, scope, arguments, call.tool_name)
            request = (
                {"url": public_url(arguments["url"])}
                if call.tool_name == "read_research_page"
                else {"query": arguments["query"].strip()}
            )
            prior = self._row(conn, effect)
            if prior:
                known = self.receipt(conn, call)
                if known is not None:
                    return known
                raise AppError(
                    "RESEARCH_EXTERNAL_READ_UNKNOWN", "原外部读取结果未知，先对账，不重新请求"
                )
            matches = [
                row
                for row in conn.execute(
                    select(r.external_reads)
                    .where(
                        r.external_reads.c.source_id == external["source_id"],
                        r.external_reads.c.source_epoch == external["epoch"],
                    )
                    .order_by(r.external_reads.c.created_at)
                ).mappings()
                if row["native_call"]["tool_name"] == call.tool_name
                and all(row["request"].get(k) == v for k, v in request.items())
            ]
            if any(row["state"] == "started" for row in matches):
                raise AppError(
                    "RESEARCH_EXTERNAL_READ_UNKNOWN", "原读取未完成对账，不再发送相同请求"
                )
            cached = next((row for row in matches if row["state"] == "completed"), None)
            if cached is None:
                cached = next(
                    (
                        row
                        for row in reversed(matches)
                        if row["state"] == "failed" and row["result"]
                    ),
                    None,
                )
            if cached and _hash(cached["result"]) != cached["result_hash"]:
                raise AppError("RESEARCH_EXTERNAL_RECEIPT_CORRUPT", "原外部读取回执摘要不符")
            conn.execute(
                insert(r.external_reads).values(
                    effect_id=effect,
                    task_id=task["task_id"],
                    run_id=call.run_id,
                    source_id=external["source_id"],
                    source_epoch=external["epoch"],
                    native_call=asdict(call),
                    request={
                        **request,
                        "transport_source": cached["request"].get("transport_source", "unknown")
                        if cached
                        else "fixture"
                        if (
                            getattr(
                                self.search
                                if call.tool_name == "search_research_web"
                                else self.reader,
                                "transport",
                                None,
                            )
                            is not None
                        )
                        else "public_http",
                    },
                    state="started",
                    network_events=[],
                    created_at=now(),
                )
            )
        if cached is not None:
            result = {
                **cached["result"],
                "cached_from_effect_id": cached["effect_id"],
                "reused_evidence": True,
            }
            for item in result.get("sources", []) + result.get("unread_continuations", []):
                snapshot = SourceStore(self.repository).snapshot(
                    task["course_id"], item["handle_id"]
                )
                SourceStore(self.repository).require_current(snapshot)
                if snapshot.content_hash != item["source_version"]:
                    raise AppError("RESEARCH_EXTERNAL_RECEIPT_CORRUPT", "已保存网页版本与句柄不符")
                if "source" in item and item["source"] != material(
                    {"representation": "text", "leaf_id": None, "label": item["label"]}, snapshot
                ):
                    raise AppError("RESEARCH_EXTERNAL_RECEIPT_CORRUPT", "已保存网页文字与回执不符")
            return self._complete(business, call, arguments, external, effect, result)
        self.repository._hook("research_external_before_http", effect_id=effect)

        def check():
            with self.repository.engine.connect() as conn:
                business._authorize(conn, call, arguments)

        def observe(kind, facts):
            check()
            with self.repository._lock, self.repository.engine.begin() as conn:
                row = self._row(conn, effect)
                conn.execute(
                    update(r.external_reads)
                    .where(r.external_reads.c.effect_id == effect)
                    .values(
                        network_events=[
                            *row["network_events"],
                            {"kind": kind, "facts": facts, "at": now()},
                        ],
                    )
                )

        error = None
        try:
            found = (
                self.search.search(request["query"], check, observe)
                if call.tool_name == "search_research_web"
                else self.reader.read(request["url"], check, observe)
            )
        except (AppError, httpx.HTTPError, OSError) as failure:
            error = getattr(failure, "code", "EXTERNAL_NETWORK_UNAVAILABLE")
            if error == "NATIVE_RUN_CANCEL_REQUESTED":
                with self.repository._lock, self.repository.engine.begin() as conn:
                    conn.execute(
                        update(r.external_reads)
                        .where(r.external_reads.c.effect_id == effect)
                        .values(state="cancelled", error_code=error, finished_at=now())
                    )
                raise
            found = None
        self.repository._hook("research_external_after_http", effect_id=effect)
        check()
        sources, continuations = [], []
        captured = now()
        if found is not None:
            items = (
                found["items"]
                if call.tool_name == "search_research_web"
                else [
                    {
                        "url": found["requested_url"],
                        "title": found["title"],
                        "text": found["text"],
                        "published_at": None,
                    }
                ]
            )
            for item in items:
                if not item["text"].strip():
                    continue
                parent = SourceStore(self.repository)._save_verified(
                    task["course_id"],
                    WebEvidenceRef(
                        evidence_kind="search_excerpt"
                        if call.tool_name == "search_research_web"
                        else "page_text",
                        url=item["url"],
                        final_url=found.get("final_url"),
                        title=item["title"],
                        retrieved_at=captured,
                        published_at=item["published_at"],
                        response_sha256=found["response_sha256"],
                        target_http_status=found.get("http_status")
                        if call.tool_name == "read_research_page"
                        else None,
                        parser_version=found.get("parser_version", "deepseek-search-excerpt-v1"),
                    ),
                    item["text"],
                    "original",
                )
                chunks = (
                    [
                        SourceStore(self.repository).slice(
                            parent, start, min(start + EXTERNAL_CHUNK, len(parent.text))
                        )
                        for start in range(0, len(parent.text), EXTERNAL_CHUNK)
                    ]
                    if len(parent.text) > EXTERNAL_CHUNK
                    else [parent]
                )
                for index, chunk in enumerate(chunks):
                    handle = {
                        "handle_id": chunk.snapshot_id,
                        "source_version": chunk.content_hash,
                        "label": item["title"] or item["url"],
                        "range": [
                            index * EXTERNAL_CHUNK,
                            min((index + 1) * EXTERNAL_CHUNK, len(parent.text)),
                        ],
                    }
                    if index == 0:
                        sources.append(
                            {
                                **handle,
                                "source": material(
                                    {
                                        "representation": "text",
                                        "leaf_id": None,
                                        "label": handle["label"],
                                    },
                                    chunk,
                                ),
                            }
                        )
                    else:
                        continuations.append(handle)
        result = {
            "status": "matched" if sources else "not_ready",
            "sources": sources,
            "unread_continuations": continuations,
            "error_code": error,
            "external_fallback": False,
            "retrieval_only": call.tool_name == "search_research_web",
            "truncated": found.get("truncated", False) if found else False,
            "external_usage": None,
            "external_usage_presence": False,
        }
        return self._complete(business, call, arguments, external, effect, result)

    def _complete(self, business, call, arguments, external, effect, result):
        sources, continuations = result["sources"], result["unread_continuations"]
        with self.repository._lock, self.repository.engine.begin() as conn:
            business._authorize(conn, call, arguments)
            for item in sources + continuations:
                if (
                    conn.execute(
                        select(s.native_source_handles).where(
                            s.native_source_handles.c.run_id == call.run_id,
                            s.native_source_handles.c.handle_id == item["handle_id"],
                        )
                    ).first()
                    is None
                ):
                    conn.execute(
                        insert(s.native_source_handles).values(
                            handle_id=item["handle_id"],
                            run_id=call.run_id,
                            kind="snapshot",
                            source_version=item["source_version"],
                            locator={"snapshot_id": item["handle_id"], "external_research": True},
                            created_at=now(),
                        )
                    )
            conn.execute(
                update(r.external_reads)
                .where(r.external_reads.c.effect_id == effect)
                .values(
                    state="completed" if sources else "failed",
                    result=result,
                    result_hash=_hash(result),
                    error_code=result["error_code"],
                    finished_at=now(),
                )
            )
            source = (
                conn.execute(
                    select(s.task_sources).where(
                        s.task_sources.c.source_id == external["source_id"]
                    )
                )
                .mappings()
                .one()
            )
            state, code = self.source_state(conn, source)
            conn.execute(
                update(s.task_sources)
                .where(s.task_sources.c.source_id == external["source_id"])
                .values(state=state, error_code=code, updated_at=now())
            )
            self.repository._hook("research_external_before_commit", effect_id=effect)
        self.repository._hook("research_external_after_commit", effect_id=effect)
        return result
