"""Read-only business history projected through the fixed native message protocol.

The view is not an ExecutionBinding or a writable Host journal. It has its own
dense cursor, no execution events, elapsed-time claims, usage or retry authority.
"""

import asyncio
import copy
import hashlib
from collections import defaultdict
from datetime import datetime

from learning_app.errors import AppError


def milliseconds(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def history_page(records, through_seq, before_seq=None, max_messages=50):
    if type(through_seq) is not int or not -1 <= through_seq < len(records):
        raise AppError("IMPORT_HISTORY_CUT_INVALID", "历史页位置已改变，请重新打开")
    if type(max_messages) is not int or not 1 <= max_messages <= 500:
        raise AppError("IMPORT_HISTORY_LIMIT_INVALID", "历史页数量无效")
    if before_seq is not None and (type(before_seq) is not int or before_seq < 0):
        raise AppError("IMPORT_HISTORY_BEFORE_INVALID", "历史页边界无效")
    end = min(through_seq + 1, before_seq if before_seq is not None else through_seq + 1)
    start = max(0, end - max_messages)
    return {"records": copy.deepcopy(records[start:end]), "hasMore": start > 0}


class ImportedHistory:
    def __init__(self, service, content):
        self.service, self.content = service, content
        self.views = {}
        self.chats = {}
        self.carriers = {}
        self.locks = defaultdict(asyncio.Lock)
        for course in service.repository.list_courses():
            for index in service.repository.list_chats(course["course_id"]):
                chat = service.repository.get_chat(index["chat_id"])
                generations = {
                    row["generation_id"]: row
                    for row in chat["generations"]
                    if service.imported.contains("generation", row["generation_id"])
                }
                if not generations:
                    continue
                user_ids = {row["user_message_id"] for row in generations.values()}
                messages = [
                    row
                    for row in chat["messages"]
                    if row["message_id"] in user_ids or row.get("generation_id") in generations
                ]
                sid = (
                    "learning-history-" + hashlib.sha256(chat["chat_id"].encode()).hexdigest()[:32]
                )
                binding = service.repository.current_binding(chat["chat_id"])
                title = (binding or {}).get("adapter_metadata", {}).get("desktop_title_override")
                title = title or next(
                    (
                        " ".join(m["text"].split())[:80]
                        for m in messages
                        if m["role"] == "user" and m["text"].strip()
                    ),
                    "导入历史",
                )
                records, turn = [], 0
                for message in messages:
                    content_value = content(message)
                    event = {
                        "type": "user/message"
                        if message["role"] == "user"
                        else "assistant/message",
                        "seq": len(records),
                        "time": milliseconds(message["created_at"]),
                        "surfaceOp": "append",
                    }
                    if message["role"] == "user":
                        turn += 1
                        event["data"] = {
                            "id": message["message_id"],
                            "role": "user",
                            "source": {
                                "kind": "user",
                                "importedBusinessMessage": message["message_id"],
                            },
                            "content": content_value,
                        }
                    elif message["role"] == "assistant":
                        generation = generations[message["generation_id"]]
                        event["data"] = {
                            "turn": max(1, turn),
                            "step": 1,
                            "stream": [],
                            "message": {
                                "id": message["message_id"],
                                "role": "assistant",
                                "source": {
                                    "kind": "model",
                                    "provider": "project-business-history",
                                    "model": generation["model"],
                                },
                                "content": content_value,
                            },
                        }
                    else:
                        raise AppError("IMPORT_HISTORY_ROLE_UNSUPPORTED", "历史消息类型无法显示")
                    records.append({"type": "event", "event": event})
                unknown = any(
                    g["state"] not in {"completed", "failed", "cancelled", "truncated"}
                    for g in generations.values()
                )
                mapping = {
                    "chat_id": chat["chat_id"],
                    "course_id": course["course_id"],
                    "session_id": sid,
                    "epoch": (binding or {}).get("epoch", 0),
                    "read_only": True,
                    "history_available": True,
                    "has_unresolved_history": unknown,
                }
                self.views[sid] = {
                    "mapping": mapping,
                    "records": records,
                    "title": title,
                    "created_at": chat["created_at"],
                    "attachment_ids": frozenset(
                        aid for m in messages for aid in m["attachment_ids"]
                    ),
                }
                self.chats[chat["chat_id"]] = sid

    def mapping(self, sid):
        return self.views[sid]["mapping"] if sid in self.views else None

    def projections(self, sid):
        view = self.views[sid]
        return {
            "asOfSeq": len(view["records"]) - 1,
            "values": {
                "title": view["title"],
                "workingDirectory": str(
                    self.service.execution.host.workspace / view["mapping"]["course_id"]
                ),
                "sessionListMetadata": {
                    "blank": not bool(view["records"]),
                    "lastPromptAt": next(
                        (
                            r["event"]["time"]
                            for r in reversed(view["records"])
                            if r["event"]["type"] == "user/message"
                        ),
                        None,
                    ),
                },
            },
        }

    def summaries(self):
        return [
            {
                "sessionId": sid,
                "updatedAt": milliseconds(view["created_at"]),
                "running": False,
                "blank": not bool(view["records"]),
                "agentAvailable": False,
                "formatStatus": "current",
                "cwd": str(self.service.execution.host.workspace / view["mapping"]["course_id"]),
                "projections": {"kind": "sequenced", **self.projections(sid)},
            }
            for sid, view in self.views.items()
        ]

    async def opening(self, sid, max_messages=50):
        async with self.locks[sid]:
            return await self._opening(sid, max_messages)

    async def _opening(self, sid, max_messages):
        if sid not in self.carriers:
            execution = self.service.execution
            await execution.peer.rpc(
                "session/create", {"cwd": str(execution.host.workspace), "sessionId": sid}
            )
            stream, native = await execution.peer.follow(sid)
            try:
                if native.get("cursor") != -1 or native.get("records"):
                    raise AppError(
                        "IMPORT_HISTORY_CARRIER_NOT_EMPTY", "历史视图身份包含其他记录，未覆盖"
                    )
                self.carriers[sid] = copy.deepcopy(native)
            finally:
                await execution.peer.unfollow(stream)
        native = copy.deepcopy(self.carriers[sid])
        records = self.views[sid]["records"]
        native.update(history_page(records, len(records) - 1, max_messages=max_messages))
        native.update(
            cursor=len(records) - 1,
            projections=self.projections(sid),
            assistantStream={"revision": 0},
        )
        return native

    def page(self, sid, request):
        return history_page(
            self.views[sid]["records"],
            request.get("throughSeq"),
            request.get("beforeSeq"),
            request.get("maxMessages", 50),
        )
