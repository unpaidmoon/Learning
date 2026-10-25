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
        account_enabled: bool = False,
        home_directory: Path | None = None,
        desktop_enabled: bool = False,
        business_url: str | None = None,
        business_token: str | None = None,
    ) -> None:
        import secrets

        self.repository, self.run_dir = repository.resolve(), run_dir.resolve()
        self.boundary_url, self.boundary_token = boundary_url, boundary_token
        self.model, self.thinking = model, thinking
        self.max_tokens, self.timeout = max_tokens, timeout
        self.account_enabled = account_enabled
        self.desktop_enabled = desktop_enabled
        self.business_url, self._business_token = business_url, business_token
        self._released_native_runs: set[str] = set()
        self._execution_policy_history: dict[str, dict] = {}
        self.reconcile_only_runs: set[str] = set()
        self.workspace, self.home = self.run_dir / "workspace", self.run_dir / "home"
        if home_directory is not None:
            self.home = home_directory.resolve()
        self.port, self.control_port = free_port(), free_port()
        self._control_token = secrets.token_urlsafe(32)
        self._token: str | None = None
        self.base = f"http://127.0.0.1:{self.port}"
        self._group = OwnedProcessGroup("learning-harness")
        self._lines: list[str] = []
        self._changed = threading.Event()
        self.process = None
        self.cleanup: dict = {}
        self.execution_policies: dict[str, dict] = {}

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
            LEARNING_HARNESS_EXECUTION_POLICIES_PATH=str(
                self.run_dir / "private" / "execution-policies.json"
            ),
            LEARNING_HARNESS_ACCOUNT_ENABLED="1" if self.account_enabled else "0",
            LEARNING_HARNESS_WORKSPACE_RELOCATIONS=str(self.run_dir.parent / "workspace-relocations.json"),
            PKG_NATIVE_CACHE_PATH=str(
                self.repository
                / ".runtime"
                / "hn"
                / hashlib.sha256(str(self.run_dir).encode()).hexdigest()[:10]
            ),
        )
        if self.business_url and self._business_token:
            env.update(
                LEARNING_HARNESS_BUSINESS_URL=self.business_url,
                LEARNING_HARNESS_BUSINESS_TOKEN=self._business_token,
            )
        if self.desktop_enabled:
            env["LEARNING_HARNESS_DESKTOP_PREFERENCES_PATH"] = str(
                self.home / "learning-desktop-settings.json"
            )
        env["LEARNING_HARNESS_RECONCILE_RUNS_PATH"] = str(
            self.run_dir / "private/reconcile-runs.json"
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
        (self.run_dir / "private" / "execution-policies.json").write_text(
            json.dumps(list(self.execution_policies.values())),
            encoding="utf-8",
        )
        (self.run_dir / "private/reconcile-runs.json").write_text(
            json.dumps(sorted(self.reconcile_only_runs)), encoding="utf-8"
        )
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
        preferences = self.home / "learning-desktop-settings.json"
        if self.desktop_enabled and preferences.is_file():
            try:
                saved = json.loads(preferences.read_text("utf-8"))
                if saved.get("version") != 1 or not isinstance(saved.get("namespaces"), dict):
                    raise ValueError("invalid settings document")
                for namespace, values in saved["namespaces"].items():
                    if not isinstance(namespace, str) or not isinstance(values, dict) or not (
                        namespace.startswith("ui-")
                        or namespace in {"locale", "agent-preset-registry", "agent-default-model"}
                    ):
                        raise ValueError("settings namespace is not presentation-owned")
                    effective_profile.append({"id": namespace, "config": values})
            except (OSError, ValueError, TypeError) as exc:
                raise ExecutionError("DESKTOP_SETTINGS_STORAGE_INVALID") from exc
        if not self.business_url:
            for item in effective_profile:
                if item.get("id") == "tool-skill":
                    item["disabled"] = True
        if self.account_enabled:
            for item in effective_profile:
                if item.get("id") in {
                    "account-controller",
                    "deepseek-account",
                    "llm-deepseek-account",
                }:
                    item["disabled"] = False
                if item.get("id") == "deepseek-account":
                    item["config"] = {
                        "platformOrigin": "https://platform.deepseek.com",
                        "desktopPlatform": "win32",
                        "inferenceOrigin": f"http://127.0.0.1:{urlsplit(self.boundary_url).port}",
                    }
                if item.get("id") == "llm-deepseek-account":
                    item["config"] = {
                        "baseURL": self.boundary_url,
                        "reasoningEffort": native_thinking(self.thinking),
                        "maxTokens": self.max_tokens,
                        "streamIdleTimeoutMs": int(self.timeout * 1000),
                        "retryPolicy": {"mode": "normal", "maxRetries": 0},
                    }
        provider = next(item for item in effective_profile if item["id"] == "llm-deepseek")
        template = provider["config"]["models"][0]
        provider["config"]["models"] = [
            {
                **{key: value for key, value in template.items() if key != "systemPromptUpdate"},
                "id": choice,
                "name": choice,
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
                    expected = (
                        [
                            "admit_requested_artifact",
                            "read_document_region",
                            "read_nearby_subtitles",
                            "read_research_page",
                            "read_source_handle",
                            "search_course_concept",
                            "search_research_web",
                            "skill",
                            "stage_result",
                        ]
                        if self.business_url
                        else []
                    )
                    if sorted(value.get("tools", [])) != expected:
                        raise ExecutionError("HARNESS_UNEXPECTED_TOOLS")
                    if value.get("continuationRoutingVersion") != 1:
                        raise ExecutionError("HARNESS_CONTINUATION_PROTOCOL_MISMATCH")
                    if self.business_url and (
                        value.get("businessBridgeVersion") != 1 or "nativeAttempts" not in value
                    ):
                        raise ExecutionError("HARNESS_NATIVE_TOOLS_PROTOCOL_MISMATCH")
                    return value
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
        raise ExecutionError("HARNESS_PROFILE_NOT_READY")

    async def configure_execution(
        self,
        session_id: str,
        run_id: str,
        policy,
        *,
        request_id: str | None = None,
        epoch: int | None = None,
        scope: dict | None = None,
    ) -> None:
        value = self.remember_execution_policy(
            session_id, run_id, policy, request_id=request_id, epoch=epoch, scope=scope
        )
        async with httpx.AsyncClient(trust_env=False, timeout=8) as client:
            response = await client.post(
                f"http://127.0.0.1:{self.control_port}/executions",
                headers={"X-Learning-Harness-Control": self._control_token},
                json=value,
            )
            if response.status_code != 200:
                raise ExecutionError("HARNESS_EXECUTION_POLICY_REJECTED")

    async def prepare_document_images(self, value: dict) -> list[dict]:
        async with httpx.AsyncClient(trust_env=False, timeout=35) as client:
            response = await client.post(
                f"http://127.0.0.1:{self.control_port}/document-images/prepare",
                headers={"X-Learning-Harness-Control": self._control_token},
                json=value,
            )
        if response.status_code != 200:
            raise ExecutionError("DOCUMENT_IMAGE_PREPARATION_REJECTED")
        proofs = response.json().get("proofs")
        if not isinstance(proofs, list) or len(proofs) != len(value["images"]):
            raise ExecutionError("DOCUMENT_IMAGE_PREPARATION_INVALID")
        return proofs

    def remember_execution_policy(
        self,
        session_id: str,
        run_id: str,
        policy,
        *,
        request_id: str | None = None,
        epoch: int | None = None,
        scope: dict | None = None,
    ) -> dict:
        value = {
            "session_id": session_id,
            "run_id": run_id,
            "model": policy.model,
            "thinking": native_thinking(policy.thinking),
            "max_output_tokens": policy.max_output_tokens,
            "timeout_seconds": policy.timeout_seconds,
        }
        if request_id is not None or epoch is not None:
            if (
                not isinstance(request_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", request_id)
                or type(epoch) is not int
                or epoch < 1
            ):
                raise ExecutionError("HARNESS_CONTINUATION_BINDING_INVALID")
            value.update(request_id=request_id, epoch=epoch)
        if scope is not None:
            expected = {"course_id", "purpose", "task_id", "allowed_tools"}
            if set(scope) != expected:
                raise ExecutionError("HARNESS_BUSINESS_SCOPE_INVALID")
            value.update(scope)
        known = self._execution_policy_history.get(run_id)
        if known is not None:
            if known != value:
                raise ExecutionError("HARNESS_EXECUTION_POLICY_IMMUTABLE")
            return known
        prior = self.execution_policies.get(session_id)
        if (
            prior is not None
            and prior != value
            and (
                prior["run_id"] not in self._released_native_runs
                or value.get("epoch", 0) <= prior.get("epoch", 0)
            )
        ):
            raise ExecutionError("HARNESS_EXECUTION_POLICY_IMMUTABLE")
        self.execution_policies[session_id] = value
        self._execution_policy_history[run_id] = value
        return value

    async def release_execution(
        self, session_id: str, run_id: str, request_id: str, epoch: int
    ) -> dict:
        async with httpx.AsyncClient(trust_env=False, timeout=8) as client:
            response = await client.post(
                f"http://127.0.0.1:{self.control_port}/leases/release",
                headers={"X-Learning-Harness-Control": self._control_token},
                json={
                    "session_id": session_id,
                    "run_id": run_id,
                    "request_id": request_id,
                    "epoch": epoch,
                },
            )
            if response.status_code != 200:
                raise ExecutionError("HARNESS_NATIVE_LEASE_NOT_RELEASABLE")
            receipt = response.json()
            if (
                receipt.get("released") is not True
                or receipt.get("run_id") != run_id
                or receipt.get("request_id") != request_id
                or receipt.get("epoch") != epoch
            ):
                raise ExecutionError("HARNESS_NATIVE_RELEASE_RECEIPT_INVALID")
            self._released_native_runs.add(run_id)
            return receipt

    async def recover_readonly_execution(self, session_id, run_id, request_id, epoch):
        async with httpx.AsyncClient(trust_env=False, timeout=8) as client:
            response = await client.post(
                f"http://127.0.0.1:{self.control_port}/leases/recover-readonly",
                headers={"X-Learning-Harness-Control": self._control_token},
                json={
                    "session_id": session_id,
                    "run_id": run_id,
                    "request_id": request_id,
                    "epoch": epoch,
                },
            )
            if response.status_code != 200:
                raise ExecutionError("NATIVE_READONLY_RECOVERY_UNCONFIRMED")
            result = response.json()
            if (
                result.get("recovered") is not True
                or result.get("source") != "physical_read_handle"
                or not isinstance(result.get("native_terminal"), dict)
            ):
                raise ExecutionError("NATIVE_READONLY_RECOVERY_INVALID")
            return result

    async def prove_tool_stop(self, lease):
        async with httpx.AsyncClient(trust_env=False, timeout=8) as client:
            response = await client.post(
                f"http://127.0.0.1:{self.control_port}/leases/prove-tool-stop",
                headers={"X-Learning-Harness-Control": self._control_token},
                json=lease,
            )
            if response.status_code != 200:
                raise ExecutionError("NATIVE_TOOL_STOP_UNCONFIRMED")
            return response.json()

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
