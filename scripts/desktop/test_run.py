"""Launcher failure windows: cleanup only the confirmed owned backend."""

from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import run as desktop_run
from learning_app.processes import ProcessError


class BackendDouble:
    pid = 101

    def __init__(self) -> None:
        self.running = True
        self.waited = False

    def poll(self):
        return None if self.running else 0

    def wait(self, timeout=None):
        self.waited = True
        self.running = False
        return 0


class DesktopRunTests(unittest.TestCase):
    def test_repeat_launch_restores_only_identity_checked_owned_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = "a" * 32
            runtime = root / ".runtime/desktop/app/run"
            logs = runtime / "desktop-launches/launch"
            logs.mkdir(parents=True)
            (logs / "backend.stdout.log").write_text('LEARNING_APP_READY ' + json.dumps({'instance_id': identity}) + '\n', 'utf-8')
            record = runtime / "apps" / identity / "run.json"
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({'status': 'ready', 'backend_port': 18986}), 'utf-8')
            with (patch.object(desktop_run, 'ROOT', root),
                  patch('learning_app.dev.wait_for_http') as health,
                  patch('learning_app.native_files.desktop_picker_owner', return_value=123) as owner,
                  redirect_stdout(io.StringIO())):
                self.assertTrue(desktop_run.activate_existing(18986))
                health.assert_called_once_with('http://127.0.0.1:18986/api/v1/health', processes=[], timeout=1, instance_id=identity)
                owner.assert_called_once_with(logs / 'native-owner.json')
            with (patch.object(desktop_run, 'ROOT', root),
                  patch('learning_app.dev.wait_for_http', side_effect=ProcessError('INSTANCE_MISMATCH', 'fixture')),
                  patch('learning_app.native_files.desktop_picker_owner') as owner):
                self.assertFalse(desktop_run.activate_existing(18986))
                owner.assert_not_called()

    def test_existing_window_activation_does_not_prepare_or_start_another_backend(self):
        with (patch.object(sys, 'argv', ['run.py']),
              patch('learning_app.processes.require_free_port', side_effect=ProcessError('PORT_IN_USE', 'fixture')),
              patch.object(desktop_run, 'activate_existing', return_value=True),
              patch.object(desktop_run, 'prepare') as prepare,
              patch.object(desktop_run.subprocess, 'Popen') as spawn):
            self.assertEqual(desktop_run.main(), 0)
            prepare.assert_not_called()
            spawn.assert_not_called()

    def test_invalid_debug_port_rejects_before_preparation_or_spawn(self) -> None:
        with (
            patch.object(sys, "argv", ["run.py", "--no-prepare", "--debug-port", "0"]),
            patch.object(desktop_run, "verified_executable") as verify,
            patch.object(desktop_run.subprocess, "Popen") as spawn,
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as result:
                desktop_run.main()
            self.assertEqual(result.exception.code, 2)
            verify.assert_not_called()
            spawn.assert_not_called()

    def test_occupied_debug_port_rejects_before_preparation_or_spawn(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with (
                patch.object(sys, "argv", ["run.py", "--no-prepare", "--debug-port", str(listener.getsockname()[1])]),
                patch.object(desktop_run, "verified_executable") as verify,
                patch.object(desktop_run.subprocess, "Popen") as spawn,
            ):
                with self.assertRaises(ProcessError) as result:
                    desktop_run.main()
                self.assertEqual(result.exception.code, "PORT_IN_USE")
                verify.assert_not_called()
                spawn.assert_not_called()

    def exercise_spawn_failure(self, *, exit_error: ProcessError | None = None) -> tuple:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            backend = BackendDouble()
            group = MagicMock()
            group.spawn.side_effect = OSError("synthetic Electron spawn failure")
            record = {"instance_id": "a" * 32, "backend_port": 18986}

            def launch_backend(*args, **kwargs):
                record_path = root / ".runtime/desktop/app/run/apps" / record["instance_id"] / "run.json"
                record_path.parent.mkdir(parents=True)
                record_path.write_text(json.dumps(record), "utf-8")
                kwargs["stdout"].write("LEARNING_APP_READY " + json.dumps(record) + "\n")
                kwargs["stdout"].flush()
                return backend

            for context in (
                patch.object(sys, "argv", ["run.py", "--no-prepare", "--backend-port", "18986"]),
                patch.object(desktop_run, "ROOT", root),
                patch.object(desktop_run, "verified_executable", return_value=root / "electron.exe"),
                patch.object(desktop_run.subprocess, "Popen", side_effect=launch_backend),
                patch("learning_app.processes.OwnedProcessGroup", return_value=group),
                patch("learning_app.processes.require_free_port"),
                patch("learning_app.dev.wait_for_http"),
            ):
                stack.enter_context(context)
            request_exit = stack.enter_context(patch("learning_app.dev._request_owned_exit", side_effect=exit_error))
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            with self.assertRaisesRegex(OSError, "synthetic Electron spawn failure"):
                desktop_run.main()
            request_exit.assert_called_once_with(record, confirm_active=False, timeout=5)
            group.close.assert_called_once_with(grace_seconds=3)
            return backend.running, backend.waited, output.getvalue()

    def test_post_readiness_spawn_failure_safely_stops_idle_backend_once(self) -> None:
        running, waited, output = self.exercise_spawn_failure()
        self.assertFalse(running)
        self.assertTrue(waited)
        self.assertNotIn("DESKTOP_BACKEND_CONTINUES", output)

    def test_post_readiness_spawn_failure_preserves_active_backend(self) -> None:
        running, waited, output = self.exercise_spawn_failure(exit_error=ProcessError("ACTIVE_GENERATIONS_CONFIRMATION_REQUIRED", "fixture active"))
        self.assertTrue(running)
        self.assertFalse(waited)
        self.assertIn("ACTIVE_GENERATIONS_CONFIRMATION_REQUIRED", output)

    def test_post_readiness_spawn_failure_does_not_repeat_unknown_exit(self) -> None:
        running, waited, output = self.exercise_spawn_failure(exit_error=ProcessError("EXIT_RESULT_UNKNOWN", "fixture unknown"))
        self.assertTrue(running)
        self.assertFalse(waited)
        self.assertIn("EXIT_RESULT_UNKNOWN", output)


if __name__ == "__main__":
    unittest.main()
