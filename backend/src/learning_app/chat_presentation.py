"""Business-owned conversation titles and archive state across native identities."""
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    ForeignKey,
    MetaData,
    String,
    Table,
    insert,
    select,
    update,
)

from learning_app.storage import schema as s

chat_presentation = Table(
    "chat_presentation", MetaData(),
    Column("chat_id", ForeignKey(s.chats.c.chat_id), primary_key=True),
    Column("title", String), Column("manual", Boolean, nullable=False),
    Column("archived", Boolean, nullable=False), Column("title_request_id", String),
    Column("title_receipt", JSON, nullable=False),
)


class ChatPresentation:
    def __init__(self, repository):
        self.repository = repository

    def all(self):
        with self.repository.engine.connect() as conn:
            return {row["chat_id"]: dict(row) for row in conn.execute(select(chat_presentation)).mappings()}

    def set(self, chat_id, **values):
        with self.repository._lock, self.repository.engine.begin() as conn:
            row = conn.execute(select(chat_presentation).where(chat_presentation.c.chat_id == chat_id)).mappings().first()
            if row:
                conn.execute(update(chat_presentation).where(chat_presentation.c.chat_id == chat_id).values(**values))
            else:
                conn.execute(insert(chat_presentation).values(
                    **{"chat_id": chat_id, "title": None, "manual": False, "archived": False,
                       "title_request_id": None, "title_receipt": {}, **values}))

    def reserve_title(self, chat_id):
        with self.repository._lock:
            previous = self.all().get(chat_id, {})
            if previous.get("manual") or previous.get("title_request_id"):
                return None
            identity = "title_" + uuid.uuid4().hex
            self.set(chat_id, title_request_id=identity, title_receipt={"state": "reserved"})
            return identity

    def title(self, chat, native_title=None):
        saved = self.all().get(chat["chat_id"], {})
        if saved.get("title"):
            return saved["title"]
        if isinstance(native_title, str) and native_title.strip() and not native_title.startswith(("[[learning-generation:", "learning-")) and native_title not in {"workspace", chat["course_id"]}:
            return native_title
        first = next((m["text"] for m in chat["messages"] if m["role"] == "user"), "")
        return " ".join(first.split())[:80] or ("图片提问" if chat["messages"] else "新对话")
