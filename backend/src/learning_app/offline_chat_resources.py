"""Separate fixed chat reader; existing artifact-reader releases stay intact."""

import json

from learning_app.config import PROJECT_ROOT
from learning_app.errors import AppError
from learning_app.workbench.offline_resources import OfflineRuntime


class OfflineChatRuntime(OfflineRuntime):
    def __init__(self, project_root=PROJECT_ROOT):
        self.project_root = project_root.resolve()
        self.lock = json.loads(
            (self.project_root / "resources/workbench/offline-chat-runtime.lock.json").read_text(
                "utf-8"
            )
        )
        if self.lock.get("format") != 1 or self.lock.get("release_id") != "offline-chat-v1":
            raise AppError("OFFLINE_CHAT_RUNTIME_VERSION_INVALID", "离线聊天资源版本不匹配")
        self.directory = (
            self.project_root / ".runtime/workbench/offline-chat-reader" / self.lock["release_id"]
        )
