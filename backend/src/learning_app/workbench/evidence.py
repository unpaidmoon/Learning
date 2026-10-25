"""User-visible source cards come from actual immutable input/read receipts."""

from pydantic import Field
from sqlalchemy import select

from learning_app.errors import AppError
from learning_app.storage import schema as business
from learning_app.storage.repository import _hash

from . import corpus_schema as c
from . import schema as s
from .models import Contract, Identity, SourceSnapshot
from .sources import SourceStore


class EvidenceCard(Contract):
    generation_id: Identity
    question: str
    label: str
    source: SourceSnapshot


class ChatEvidence(Contract):
    course_id: Identity
    chat_id: Identity
    generation_limit: int = 20
    items: list[EvidenceCard] = Field(max_length=50)
    next_cursor: Identity | None = None


def chat_evidence(repository, course_id, chat_id, *, after=""):
    with repository.engine.connect() as conn:
        chat = conn.execute(
            select(business.chats).where(
                business.chats.c.chat_id == chat_id, business.chats.c.course_id == course_id
            )
        ).first()
        if chat is None:
            raise AppError("SOURCE_CHAT_DENIED", "对话不属于指定课程", 403)
        generations = list(
            conn.execute(
                select(business.generations.c.generation_id)
                .join(business.submissions)
                .where(business.submissions.c.chat_id == chat_id)
                .order_by(
                    business.generations.c.created_at.desc(),
                    business.generations.c.generation_id.desc(),
                )
                .limit(20)
            ).scalars()
        )
        scopes = (
            conn.execute(
                select(s.native_scopes)
                .where(
                    s.native_scopes.c.run_id.in_(generations),
                    s.native_scopes.c.owner_kind == "chat",
                    s.native_scopes.c.course_id == course_id,
                )
                .order_by(s.native_scopes.c.created_at.desc(), s.native_scopes.c.run_id.desc())
            )
            .mappings()
            .all()
        )
        cards = {}
        for scope in scopes:
            run = scope["run_id"]
            # Metadata is the user's original question; no generated citation
            # strings or model-supplied paths can replace these references.
            question = scope["raw_user_text"][:200]

            def add(identity, label):
                if identity not in cards or cards[identity]["generation_id"] == run:
                    cards[identity] = {"generation_id": run, "question": question, "label": label}

            for identity in scope["grants"]["source_snapshot_ids"]:
                add(identity, "本次提交及合法历史中的选区")
            for row in conn.execute(
                select(s.native_read_receipts).where(s.native_read_receipts.c.run_id == run)
            ).mappings():
                if _hash(row["result"]) != row["result_hash"]:
                    raise AppError("SOURCE_EVIDENCE_INTEGRITY", "原始工具回执校验失败", 409)
                source = row["result"]["source"]
                add(source["snapshot_id"], source["label"])
            for row in conn.execute(
                select(c.query_receipts).where(c.query_receipts.c.run_id == run)
            ).mappings():
                if _hash(row["result"]) != row["result_hash"]:
                    raise AppError("SOURCE_EVIDENCE_INTEGRITY", "原始检索回执校验失败", 409)
                for entry in row["result"]["results"]:
                    source = entry["source"]
                    add(source["snapshot_id"], source["label"])
        ordered = sorted(
            cards,
            key=lambda identity: (generations.index(cards[identity]["generation_id"]), identity),
        )
        if after:
            if after not in ordered:
                raise AppError(
                    "SOURCE_EVIDENCE_CURSOR_EXPIRED", "最近问答来源已更新，请返回首批资料", 409
                )
            ordered = ordered[ordered.index(after) + 1 :]
        selected = ordered[:50]
        items = [
            EvidenceCard(
                **cards[identity], source=SourceStore.in_transaction(conn, course_id, identity)
            )
            for identity in selected
        ]
        return ChatEvidence(
            course_id=course_id,
            chat_id=chat_id,
            items=items,
            next_cursor=selected[-1] if len(ordered) > 50 else None,
        )
