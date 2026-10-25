"""One saved business cut -> offline messages, partial state and submitted images."""

import io
import json
import re

from PIL import Image
from workbench_support import learning_course

from learning_app.data_snapshot import BusinessSnapshot
from learning_app.drafts import submission_selection_conditions
from learning_app.offline_chats import OfflineChats


def image(color):
    output = io.BytesIO()
    Image.new("RGB", (20, 12), color).save(output, "PNG")
    return output.getvalue()


def document(path):
    raw = path.read_text("utf-8")
    return json.loads(
        re.search(
            r'<script type="application/json" id="learning-offline-chat-data">(.*?)</script>',
            raw,
            re.S,
        ).group(1)
    )


def test_snapshot_projection_keeps_order_unknown_math_and_only_submitted_child_image(tmp_path):
    repo, course, _, selection = learning_course(tmp_path)
    repo.create_chat(course, "offline_chat")
    binding = repo.bind_execution(
        "offline_chat", "offline_sid", adapter_metadata={"desktop_title_override": "已保存标题"}
    )
    parent = repo.stage_attachment(image("red"), attachment_id="unsubmitted_parent")
    child = repo.stage_attachment(image("green"), attachment_id="submitted_child")
    result = repo.publish_submission(
        "offline_chat",
        "只看确认选区，解释 $x^2$。 </script><script>BAD()</script>",
        "offline_input",
        attachment_ids=[child["attachment_id"]],
        conditions=submission_selection_conditions(repo, "offline_chat", [selection.snapshot_id]),
    )
    repo.mark_dispatch_started(result["generation_id"], binding["binding_id"], binding["epoch"])
    repo.ingest_event(
        result["generation_id"],
        binding_id=binding["binding_id"],
        epoch=binding["epoch"],
        event_id="partial",
        seq=1,
        kind="text_delta",
        text="已保存部分：$x^2$ 的条件仍待补齐。",
    )
    repo.mark_unknown(result["generation_id"], "fixture_disconnect")
    before = repo.get_chat("offline_chat")
    with BusinessSnapshot(repo).capture() as cut:
        index = OfflineChats().write(cut, tmp_path / "offline")
        assert index["courses"][0]["chats"][0]["title"] == "已保存标题"
        page = document(tmp_path / "offline/chats/offline_chat.html")
        assert [m["role"] for m in page["messages"]] == ["user", "assistant"]
        user, assistant = page["messages"]
        assert (
            assistant["text"] == "已保存部分：$x^2$ 的条件仍待补齐。"
            and assistant["status"] == "unknown"
        )
        assert (
            len(user["attachments"]) == 1
            and child["content_hash"] in user["attachments"][0]["path"]
        )
        assert parent["content_hash"] not in json.dumps(page)
        assert any("lesson.txt" in label for label in user["sources"])
        html = (tmp_path / "offline/chats/offline_chat.html").read_text("utf-8")
        assert "</script><script>BAD()" not in html
        assert "connect-src &#x27;none&#x27;" in html and "../reader/reader.js" in html
    assert repo.get_chat("offline_chat") == before
    repo.close()


def test_long_chat_is_paged_without_truncating_messages_or_copying_runtime_per_chat(tmp_path):
    repo, course, _, _ = learning_course(tmp_path)
    repo.create_chat(course, "long_chat")
    binding = repo.bind_execution("long_chat", "long_sid")
    for index in range(51):
        submitted = repo.publish_submission("long_chat", f"问题{index}", f"input_{index}")
        repo.mark_dispatch_started(
            submitted["generation_id"], binding["binding_id"], binding["epoch"]
        )
        repo.ingest_event(
            submitted["generation_id"],
            binding_id=binding["binding_id"],
            epoch=binding["epoch"],
            event_id=f"text_{index}",
            seq=index * 2 + 1,
            kind="text_delta",
            text=f"回答{index}",
        )
        repo.ingest_event(
            submitted["generation_id"],
            binding_id=binding["binding_id"],
            epoch=binding["epoch"],
            event_id=f"done_{index}",
            seq=index * 2 + 2,
            kind="terminal",
            status="completed",
        )
    with BusinessSnapshot(repo).capture() as cut:
        OfflineChats().write(cut, tmp_path / "offline")
    first = document(tmp_path / "offline/chats/long_chat.html")
    last = document(tmp_path / "offline/chats/long_chat-1.html")
    assert len(first["messages"]) == 100 and [m["text"] for m in last["messages"]] == [
        "问题50",
        "回答50",
    ]
    assert (
        first["navigation"]["next"] == "long_chat-1.html"
        and last["navigation"]["previous"] == "long_chat.html"
    )
    assert last["navigation"]["total"] == 102
    assert len(list((tmp_path / "offline/reader").glob("reader.js"))) == 1
    repo.close()
