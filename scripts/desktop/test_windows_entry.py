"""Exercise the Windows interpreter and CMD entry used by a double click."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.name == "nt", "Windows desktop entry")
class WindowsEntryTests(unittest.TestCase):
    def test_cmd_reports_hidden_runner_failure_and_preserves_its_exit_code(self):
        with tempfile.TemporaryDirectory(prefix="learning desktop failure ") as directory:
            root = Path(directory)
            command = root / "desktop.cmd"
            command.write_bytes((ROOT / "启动Harness桌面.cmd").read_bytes())
            launcher = root / "scripts/desktop/Launch-Desktop.ps1"
            launcher.parent.mkdir(parents=True)
            text = (ROOT / "scripts/desktop/Launch-Desktop.ps1").read_text("utf-8-sig")
            python = str(ROOT / "backend/.venv/Scripts/python.exe").replace("'", "''")
            text = text.replace("$pythonPath = Join-Path $projectRoot 'backend/.venv/Scripts/python.exe'",
                                f"$pythonPath = '{python}'")
            launcher.write_text(text, encoding="utf-8-sig")
            (launcher.parent / "run.py").write_text(
                "import sys\nprint('SYNTHETIC_START_FAILED', file=sys.stderr)\nsys.exit(23)\n", "utf-8")
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", str(command)], cwd=root,
                stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.assertEqual(result.returncode, 23, (result.stdout + result.stderr).decode(errors="replace"))
            self.assertIn(b"SYNTHETIC_START_FAILED", result.stdout + result.stderr)

    def test_windows_powershell_parses_the_real_launcher(self):
        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        # ParseFile uses the interpreter's actual file-encoding rules, unlike
        # ParseInput(Get-Content -Encoding UTF8), which concealed this failure.
        script = (
            "$tokens=$null;$errors=$null;"
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "(Join-Path (Get-Location) 'scripts/desktop/Launch-Desktop.ps1'),"
            "[ref]$tokens,[ref]$errors) | Out-Null;"
            "if($errors.Count){$errors | ForEach-Object {$_.Message};exit 1}"
        )
        result = subprocess.run(
            [powershell, "-NoProfile", "-Command", script], cwd=ROOT,
            capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stdout.decode(errors="replace"))

    def test_cmd_preserves_a_failed_launcher_exit_code(self):
        with tempfile.TemporaryDirectory(prefix="learning desktop entry ") as directory:
            root = Path(directory)
            command = root / "desktop.cmd"
            command.write_bytes((ROOT / "启动Harness桌面.cmd").read_bytes())
            launcher = root / "scripts/desktop/Launch-Desktop.ps1"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("exit 23\n", encoding="ascii")
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", str(command)], cwd=root,
                stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.assertEqual(result.returncode, 23, result.stdout.decode(errors="replace"))
