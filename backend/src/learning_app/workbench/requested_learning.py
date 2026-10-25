"""A literal current-user request grants one pinned learning-task admission.

No model classification call, material-text command, path or current-UI lookup
can grant a write. Unsupported or ambiguous requests retain the explicit task
composer. This conservative grammar is not claimed to understand every request.
"""

import re
from dataclasses import asdict

from sqlalchemy import insert, select

from learning_app.drafts import message_selection_ids
from learning_app.errors import AppError
from learning_app.storage import schema as business
from learning_app.storage.repository import _hash

from . import schema as s
from .common import now
from .models import Contract, Identity, MessageRef, TaskKind, TaskState
from .sources import SourceStore
from .submitted_images import SubmittedImages
from .task_coverage import TaskCoverage
from .task_scope_models import TaskAdmission

TOOL = "admit_requested_artifact"
PARSER_VERSION = "literal-learning-request-v1"
KINDS = {
    "note": ("学习笔记", r"笔记|notes?"),
    "handout": ("学习讲义", r"讲义|handout"),
    "book": ("交互学习书", r"交互书|学习书|interactive book"),
    "research": ("资料对照报告", r"调研报告|研究报告|对照报告|比较报告|research report"),
}
EDIT_KIND = {"report": "research", **{kind: kind for kind in KINDS}}


class NativeLearningTaskLink(Contract):
    task_id: Identity
    title: str
    kind: TaskKind
    state: TaskState


def admitted_task_links(repository, course_id, chat_id, session_id, native_turn):
    with repository.engine.connect() as conn:
        runs = {
            row["run_id"]
            for row in conn.execute(
                select(s.native_scopes).where(
                    s.native_scopes.c.course_id == course_id,
                    s.native_scopes.c.session_id == session_id,
                    s.native_scopes.c.owner_kind == "chat",
                    s.native_scopes.c.business_binding["native_turn"].as_integer() == native_turn,
                )
            ).mappings()
        }
        if not runs:
            return []
        rows = list(
            conn.execute(
                select(s.tasks, s.task_targets.c.title)
                .join(s.task_targets)
                .where(
                    s.tasks.c.course_id == course_id,
                    s.tasks.c.chat_id == chat_id,
                    s.tasks.c.scope_spec["origin"]["generation_id"].as_string().in_(runs),
                )
            ).mappings()
        )
        return [
            NativeLearningTaskLink(
                task_id=row["task_id"], title=row["title"], kind=row["kind"], state=row["state"]
            )
            for row in rows
            if row["scope_spec"].get("origin", {}).get("generation_id") in runs
        ]


def literal_request(text):
    """Only direct imperatives in current user text, never quoted instructions."""
    command = re.match(r"^[/\$](notes|research|interactive-book)\b", text.strip())
    if command:
        return {"mode": "create", "kind": {"notes": "note", "research": "research", "interactive-book": "book"}[command[1]]}
    value = text.strip()
    value = re.sub(r"^(?:请你帮我|请帮我|请你|帮我|麻烦你|请|现在)\s*", "", value, count=1)
    if re.search(
        r"不要|不用|无需|(?:^|[，。；\n])\s*别|先不|暂不|仅讨论|如何|怎么|是否|能否|可否|是什么意思|[？?]",
        value,
    ):
        return None
    # A whole document/course/external request needs its dedicated explicit
    # declaration. A submitted crop never grants a surrounding source.
    if re.search(r"整课|整章|全文|整份|全部资料|联网|外部研究|https?://", value, re.I):
        return None
    if re.match(r"^(?:修改|更新|改写|补充|修订)(?:这|此|当前|所选)", value):
        return {"mode": "edit"}
    command = re.match(
        r"^(?:(?:生成|制作|创建|整理(?:成|为)?|保存(?:成|为)?|编写|设计|做成)\s*"
        r"|(?:把|将).{1,100}?(?:整理成|整理为|生成|制作|做成|保存为|保存成|组织成)\s*"
        r"|(?:(?:只|仅)?(?:依据|根据|基于)).{1,100}?(?:生成|制作|创建|编写|整理成|做成)\s*"
        r"|(?:create|make|generate|save|write)\s+)(.{1,200})",
        value,
        re.I | re.S,
    )
    if not command:
        return None
    found = [kind for kind, (_, pattern) in KINDS.items() if re.search(pattern, command[1], re.I)]
    return {"mode": "create", "kind": found[0]} if len(found) == 1 else None


class RequestedLearning:
    @staticmethod
    def freeze(conn, repository, run, chat, current, context):
        parsed = literal_request(current["text"])
        if parsed is None:
            return None
        course_id = chat["course_id"]
        selected = list(dict.fromkeys(message_selection_ids(current.get("conditions", []))))
        target = None
        if parsed["mode"] == "edit":
            refs = [
                SourceStore.in_transaction(conn, course_id, identity).reference
                for identity in selected
            ]
            refs = [ref for ref in refs if ref.kind == "artifact"]
            if not refs or len({(ref.artifact_id, ref.revision_id) for ref in refs}) != 1:
                return None
            artifact = (
                conn.execute(
                    select(s.artifacts).where(
                        s.artifacts.c.artifact_id == refs[0].artifact_id,
                        s.artifacts.c.course_id == course_id,
                    )
                )
                .mappings()
                .one()
            )
            kind = EDIT_KIND.get(artifact["kind"])
            if kind is None:
                return None
            target = {
                "mode": "edit",
                "artifact_id": refs[0].artifact_id,
                "expected_revision_id": refs[0].revision_id,
                "block_ids": list(dict.fromkeys(ref.block_id for ref in refs)),
            }
        else:
            kind = parsed["kind"]
            target = {"mode": "create", "title": KINDS[kind][0]}
        # Actual submitted conversation only. Current explicit material wins;
        # historical text/images are retained when the user refers back to it.
        history = bool(
            re.search(r"上面|上文|刚才|之前|这道题|本题|上述|对话|回答", current["text"])
        )
        messages = context["messages"] if history else [current]
        identities = list(selected)
        if history:
            identities.extend(
                identity
                for message in messages
                for identity in message_selection_ids(message.get("conditions", []))
            )
        for message in messages:
            if not message["text"]:
                continue
            row = (
                conn.execute(
                    select(business.messages).where(
                        business.messages.c.message_id == message["message_id"],
                        business.messages.c.revision_id == message["revision_id"],
                        business.messages.c.chat_id == chat["chat_id"],
                    )
                )
                .mappings()
                .first()
            )
            if row is None or row["text"] != message["text"]:
                raise AppError("LEARNING_REQUEST_CONTEXT_CONFLICT", "请求文字与冻结消息不符")
            snapshot = SourceStore.save_in_transaction(
                conn,
                course_id,
                MessageRef(
                    message_id=row["message_id"],
                    revision_id=row["revision_id"],
                    start=0,
                    end=len(row["text"]),
                ),
                row["text"],
                "ai_generated" if row["role"] == "assistant" else "user_note",
            )
            identities.append(snapshot.snapshot_id)
        image_context = {**context, "messages": messages}
        identities.extend(
            item.snapshot_id
            for item in SubmittedImages.freeze(
                conn,
                repository,
                course_id,
                run["snapshot_id"],
                context["context_hash"],
                image_context,
            )
        )
        identities = list(dict.fromkeys(identities))
        if len(identities) > 512:
            return None
        save_course_id = course_id
        match = re.search(r"(?:保存到|存到|放到)[“\"「]?([^，。；\n”\"」]+)", current["text"])
        if match:
            title = match[1].strip().removesuffix("课程").strip()
            destinations = [item for item in conn.execute(select(business.courses)).mappings()
                            if item["title"] == title or item["course_id"] == title]
            if len(destinations) != 1:
                raise AppError("SAVE_TARGET_UNRESOLVED", "请明确指定一个可用的保存课程", 422)
            save_course_id = destinations[0]["course_id"]
        return {
            "version": PARSER_VERSION,
            "save_course_id": save_course_id,
            "open_result": bool(re.search(r"(?:生成|保存|完成)(?:后|并)(?:立即)?打开", current["text"])),
            "kind": kind,
            "target": target,
            "source_snapshot_ids": identities,
            "source_hashes": {
                identity: SourceStore.in_transaction(conn, course_id, identity).content_hash
                for identity in identities
            },
            "sources_only": bool(
                re.search(r"只依据|仅依据|只根据|仅根据|仅用|只用", current["text"])
            ),
            "context_snapshot_id": run["snapshot_id"],
            "context_hash": context["context_hash"],
            "user_message_id": current["message_id"],
            "user_revision_id": current["revision_id"],
            "raw_user_text_hash": _hash(current["text"]),
        }

    @staticmethod
    def authorize(conn, scope, arguments):
        request = scope["grants"].get("learning_request")
        if scope["owner_kind"] != "chat" or scope["purpose"] != "qa" or not request:
            raise AppError("LEARNING_REQUEST_NOT_GRANTED", "本次问答没有成果创建或修改授权", 403)
        if not isinstance(arguments, dict) or arguments:
            raise AppError(
                "LEARNING_REQUEST_ARGUMENTS", "成果范围由原用户请求固定，不接收模型自填参数", 422
            )
        if (
            request["context_snapshot_id"] != scope["grants"]["context_snapshot_id"]
            or request["context_hash"] != scope["grants"]["context_hash"]
            or request["raw_user_text_hash"] != _hash(scope["raw_user_text"])
        ):
            raise AppError("LEARNING_REQUEST_SCOPE_CHANGED", "原用户请求或上下文授权发生冲突", 409)
        for identity, digest in request["source_hashes"].items():
            snapshot = SourceStore.in_transaction(conn, scope["course_id"], identity)
            if snapshot.content_hash != digest:
                raise AppError("LEARNING_REQUEST_SOURCE_CHANGED", "原授权来源快照不符", 409)
        return request

    @staticmethod
    def receipt(conn, call, scope):
        identity = "native_admit_" + _hash(asdict(call))
        row = (
            conn.execute(
                select(s.operations).where(
                    s.operations.c.operation_id == identity,
                    s.operations.c.course_id == scope["course_id"],
                    s.operations.c.kind == "native_task_admission",
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        value = row["result"]
        if (
            value.get("native_call") != asdict(call)
            or row["payload_hash"] != _hash(asdict(call))
            or value.get("receipt_hash") != _hash(value["receipt"])
        ):
            raise AppError("LEARNING_REQUEST_RECEIPT_CONFLICT", "原任务准入回执校验失败", 409)
        return value["receipt"]

    @classmethod
    def admit(cls, conn, repository, call, scope, arguments):
        request = cls.authorize(conn, scope, arguments)
        prior = cls.receipt(conn, call, scope)
        if prior is not None:
            return prior
        intent_id = "requested_" + _hash({"run_id": call.run_id, "request": request})
        chat_id = conn.execute(
            select(business.context_snapshots.c.chat_id).where(
                business.context_snapshots.c.snapshot_id == request["context_snapshot_id"]
            )
        ).scalar_one()
        value = TaskAdmission(
            operation_id=intent_id,
            chat_id=chat_id,
            kind=request["kind"],
            instruction=scope["raw_user_text"],
            settings=scope["settings"],
            sources=[
                {"kind": "selection", "snapshot_id": identity}
                for identity in request["source_snapshot_ids"]
            ],
            target=request["target"],
            sources_only=request["sources_only"],
        )
        payload = value.model_dump(
            mode="json", exclude={"answer_review", "essay_review", "research"}
        )
        declarations = list(payload["sources"])
        if value.target.mode == "edit":
            declarations.append(
                {
                    "kind": "artifact",
                    "artifact_id": value.target.artifact_id,
                    "revision_id": value.target.expected_revision_id,
                    "block_ids": value.target.block_ids,
                }
            )
        origin = {"generation_id": call.run_id, "intent_id": intent_id, **request}
        task = TaskCoverage(repository)._admit_in_transaction(
            conn,
            scope["course_id"],
            value,
            payload,
            declarations,
            origin=origin,
        )
        result = {
            "intent_id": intent_id,
            "task_id": task.task_id,
            "artifact_id": conn.execute(
                select(s.task_targets.c.reserved_artifact_id).where(
                    s.task_targets.c.task_id == task.task_id
                )
            ).scalar_one(),
            "kind": task.kind,
            "mode": value.target.mode,
            "state_at_admission": task.state,
            "disposition": "admitted",
            "message": "已开始处理，进度和完成文件将显示在本对话中。",
        }
        conn.execute(
            insert(s.operations).values(
                operation_id="native_admit_" + _hash(asdict(call)),
                course_id=scope["course_id"],
                kind="native_task_admission",
                payload_hash=_hash(asdict(call)),
                result={
                    "native_call": asdict(call),
                    "receipt": result,
                    "receipt_hash": _hash(result),
                },
                created_at=now(),
            )
        )
        repository._hook("native_admission_before_commit", run_id=call.run_id)
        return result
