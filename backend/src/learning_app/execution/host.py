"""Own a pinned loopback Host. Its capability tokens stay in this module."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from learning_app.processes import OwnedProcessGroup

from .selection import model_choices, native_thinking, supports_images
from .types import ExecutionError


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class HarnessHost:
    def __init__(
        self,
        repository: Path,
        run_dir: Path,
        boundary_url: str,
        boundary_token: str,
        *,
        model: str = "deepseek-flash",
        thinking: str = "high",
        max_tokens: int = 16384,
        timeout: float = 240,
    ) -> None:
        import secrets

        self.repository, self.run_dir = repository.resolve(), run_dir.resolve()
        self.boundary_url, self.boundary_token = boundary_url, boundary_token
        self.model, self.thinking = model, thinking
        self.max_tokens, self.timeout = max_tokens, timeout
        self.workspace, self.home = self.run_dir / "workspace", self.run_dir / "home"
        self.port, self.control_port = free_port(), free_port()
        self._control_token = secrets.token_urlsafe(32)
        self._token: str | None = None
        self.base = f"http://127.0.0.1:{self.port}"
        self._group = OwnedProcessGroup("learning-harness")
        self._lines: list[str] = []
        self._changed = threading.Event()
        self.process = None
        self.cleanup: dict = {}

    def _environment(self) -> dict[str, str]:
        # Deliberately do not inherit provider keys, user hooks or global Node options.
        keep = (
            "SystemRoot",
            "WINDIR",
            "COMSPEC",
            "TEMP",
            "TMP",
            "APPDATA",
            "LOCALAPPDATA",
            "USERPROFILE",
            "HOMEDRIVE",
            "HOMEPATH",
            "PROGRAMFILES",
            "PROGRAMDATA",
            "PATHEXT",
            "PATH",
        )
        env = {key: os.environ[key] for key in keep if key in os.environ}
        env.update(
            DSH_HOME=str(self.home),
            DSH_RESOURCE_CACHE=str(self.run_dir / "resources"),
            DSH_PRIMARY_RUNTIME="",
            DSH_TELEMETRY_MODE="DISABLED",
            DSH_TELEMETRY_DISABLED="1",
            NO_COLOR="1",
            DEEPSEEK_BASE_URL=self.boundary_url,
            DEEPSEEK_API_KEY=self.boundary_token,
            LEARNING_HARNESS_BOUNDARY_URL=self.boundary_url,
            LEARNING_HARNESS_AUDIT_PATH=str(self.run_dir / "private" / "plugin-audit.jsonl"),
            LEARNING_HARNESS_CONTROL_PORT=str(self.control_port),
            LEARNING_HARNESS_CONTROL_TOKEN=self._control_token,
            PKG_NATIVE_CACHE_PATH=str(
                self.repository
                / ".runtime"
                / "hn"
                / hashlib.sha256(str(self.run_dir).encode()).hexdigest()[:10]
            ),
        )
        return env

    async def start(self) -> HarnessHost:
        executable = (
            self.repository
            / ".runtime/harness/install"
            / "deepseek-harness-sdk-runtime-win-x64.exe"
        )
        if not executable.is_file():
            raise ExecutionError("HARNESS_NOT_INSTALLED")
        actual_hash = await asyncio.to_thread(
            lambda: hashlib.sha256(executable.read_bytes()).hexdigest()
        )
        if actual_hash != "f9ed01f241df37c50c702d81b6c226d576210b8f3947699735ca90285ea13365":
            raise ExecutionError("HARNESS_ARTIFACT_HASH_MISMATCH")
        for directory in (self.home, self.workspace, self.run_dir / "private"):
            directory.mkdir(parents=True, exist_ok=True)
        profile = (self.repository / "harness/profiles/learning.patch.json").read_text("utf-8")
        replacements = {
            "__BOUNDARY_URL__": self.boundary_url,
            "__PLUGIN_PATH__": (self.repository / "harness/plugins/learning-guard.mjs").as_posix(),
            "__MODEL__": self.model,
            "__THINKING__": native_thinking(self.thinking),
        }
        for source, value in replacements.items():
            profile = profile.replace(source, value)
        profile = profile.replace('"__MAX_TOKENS__"', str(self.max_tokens))
        profile = profile.replace('"__TIMEOUT_MS__"', str(int(self.timeout * 1000)))
        effective_profile = json.loads(profile)
        provider = next(item for item in effective_profile if item["id"] == "llm-deepseek")
        template = provider["config"]["models"][0]
        provider["config"]["models"] = [
            {
                **{key: value for key, value in template.items() if key != "systemPromptUpdate"},
                "id": choice, "name": choice,
                "inputModalities": ["text", "image"] if supports_images(choice) else ["text"],
                # The pinned Host cannot represent the newer leading-only
                # metadata. Do not declare its sole in-history value for pro;
                # the application system prompt remains fixed for all sessions.
                **({"systemPromptUpdate": "in-history"} if choice == "deepseek-flash" else {}),
            }
            for choice in model_choices(self.model)
        ]
        profile = json.dumps(effective_profile, ensure_ascii=False, indent=2)
        profile_path = self.run_dir / "effective.patch.json"
        profile_path.write_text(profile, encoding="utf-8")
        try:
            self.process = self._group.spawn(
                [
                    str(executable),
                    "web",
                    "--patch",
                    str(profile_path),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                    "--no-open",
                ],
                cwd=self.workspace,
                env=self._environment(),
            )

            def drain(pipe):
                for line in pipe:
                    self._lines.append(line.rstrip())
                    self._changed.set()

            for pipe in (self.process.stdout, self.process.stderr):
                threading.Thread(target=drain, args=(pipe,), daemon=True).start()
            async with asyncio.timeout(45):
                while self.process.poll() is None:
                    for line in list(self._lines):
                        for url in re.findall(r"https?://[^\s\x1b]+", line):
                            parts = urlsplit(url)
                            token = parse_qs(parts.query).get("token")
                            if parts.hostname == "127.0.0.1" and parts.port == self.port and token:
                                self._token = token[0]
                                await self.audit_state()
                                return self
                    await asyncio.to_thread(self._changed.wait, 0.05)
                    self._changed.clear()
            raise ExecutionError("HARNESS_START_FAILED")
        except BaseException:
            await self.stop()
            raise

    async def audit_state(self) -> dict:
        async with httpx.AsyncClient(trust_env=False, timeout=8) as client:
            # Separate authenticated, loopback-only control capability; never public API.
            for _ in range(80):
                try:
                    response = await client.get(
                        f"http://127.0.0.1:{self.control_port}/state",
                        headers={"X-Learning-Harness-Control": self._control_token},
                    )
                    response.raise_for_status()
                    value = response.json()
                    if value.get("tools") != []:
                        raise ExecutionError("HARNESS_UNEXPECTED_TOOLS")
                    return value
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
        raise ExecutionError("HARNESS_PROFILE_NOT_READY")

    async def stop(self) -> dict:
        self.cleanup = await asyncio.to_thread(self._group.close, grace_seconds=2)
        with socket.socket() as sock:
            self.cleanup["host_port_closed"] = sock.connect_ex(("127.0.0.1", self.port)) != 0
        if self.run_dir.exists():
            safe_lines = [
                re.sub(r"(?i)(token=)[^&\s\"']+", r"\1<redacted>", s) for s in self._lines
            ]
            (self.run_dir / "private").mkdir(exist_ok=True)
            (self.run_dir / "private/host-diagnostics.json").write_text(
                json.dumps(safe_lines, ensure_ascii=False), encoding="utf-8"
            )
            (self.run_dir / "cleanup.json").write_text(json.dumps(self.cleanup), encoding="utf-8")
        self._token = None
        return self.cleanup
