"""Independent synthetic exploration of redesigned MVP-A recovery and continuity.

This is a supplementary real-Chromium/pinned-Host/local-provider run. It does
not substitute for the complete redesign suite or a real model acceptance run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from test_browser import ROOT, check, port_closed, wait_until, write_json
from test_mvp_a import Browser, MvpInstance, active_stop, api, duplicate_probe
from test_redesign import (
    course_and_page_background,
    lost_receipt,
    select_course,
    select_history,
    source_digests,
    wait_ready,
)


def run(directory: Path, executable: Path, port: int) -> dict:
    result = {
        "id": "MVP-A-redesign-late-exploration",
        "status": "running",
        "evidence_level": "supplementary real Chromium + formal ASGI + pinned Host + local provider fixture",
        "scope": [
            "active body stop and same-chat follow-up",
            "duplicate submission identity",
            "lost submission receipt across reload",
            "cross-course and closed-page background continuity",
        ],
        "real_model_attempts": None,
        "assertions": [],
        "source_digests_before": source_digests(),
        "browser_executable_sha256": hashlib.sha256(
            executable.read_bytes()
        ).hexdigest(),
    }
    instance = MvpInstance(directory / "server", port)
    browser = Browser(directory / "browser", executable)
    try:
        instance.start()
        browser.command("open", instance.url, f"--config={browser.config}")
        wait_ready(browser)
        browser.snapshot("initial")
        course_a = api(browser, "/courses", {"title": "合成后半探测 A"}, method="POST")
        course_b = api(browser, "/courses", {"title": "合成后半探测 B"}, method="POST")
        check(
            course_a["status"] == course_b["status"] == 200,
            "synthetic_legacy_courses_created_without_reading_user_files",
            result,
        )
        course_id = course_a["body"]["course_id"]
        second_id = course_b["body"]["course_id"]
        chat = api(browser, f"/courses/{course_id}/chats", method="POST")
        check(chat["status"] == 200, "synthetic_chat_created", result)
        chat_id = chat["body"]["chat_id"]
        seed_question = "后半探索预置合成问题。"
        seed_answer = "这是独立后半探索的合成回答，用于显示已有正文。"
        instance.control("scenario", scenario={"text_chunks": [seed_answer]})
        seeded = api(
            browser,
            "/submissions",
            {
                "chat_id": chat_id,
                "submission_id": "late-seed-" + uuid.uuid4().hex,
                "text": seed_question,
                "model": "deepseek-flash",
                "thinking": "high",
            },
            method="POST",
        )
        check(seeded["status"] == 200, "synthetic_seed_submission_accepted", result)
        wait_until(
            lambda: instance.generation_for(seed_question)["state"] == "completed",
            timeout=30,
        )
        browser.command("reload")
        wait_ready(browser)
        select_course(browser, course_id)
        select_history(browser, chat_id)
        browser.wait_text(seed_answer)
        active_stop(browser, instance, result)
        duplicate_probe(browser, instance, chat_id, result)
        lost_receipt(browser, instance, course_id, chat_id, result)
        course_and_page_background(
            browser, instance, course_id, second_id, chat_id, result
        )
        counts = instance.counts()
        result["real_model_attempts"] = counts["real_provider_attempts"]
        check(
            counts["real_provider_attempts"] == 0,
            "supplementary_fixture_did_not_call_real_provider",
            result,
            counts=counts,
        )
        result["status"] = "passed"
    except Exception as error:
        result["status"] = "failed"
        result["error"] = f"{type(error).__name__}: {error}"
        try:
            browser.snapshot("failure")
            browser.command(
                "screenshot", f"--filename={browser.directory / 'failure.png'}"
            )
        except Exception as capture_error:
            result["capture_error"] = type(capture_error).__name__
    finally:
        result["source_digests_after"] = source_digests()
        result["source_changed_during_exploration"] = (
            result["source_digests_before"] != result["source_digests_after"]
        )
        result["browser_cleanup"] = browser.close()
        result["server_cleanup"] = instance.stop()
        try:
            result["transport_counts"] = instance.counts()
            result["real_model_attempts"] = result["transport_counts"][
                "real_provider_attempts"
            ]
        except Exception as count_error:
            result["transport_count_error"] = type(count_error).__name__
        if not result["server_cleanup"].get("backend_port_closed"):
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id", default="redesign-late-" + time.strftime("%Y%m%d-%H%M%S")
    )
    parser.add_argument("--browser-path", type=Path)
    parser.add_argument("--backend-port", type=int, default=18873)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("run-id只能包含字母、数字、横线和下划线")
    executable = args.browser_path or (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe"
    )
    if not executable.is_file():
        raise RuntimeError("BROWSER_MISSING: 指定已有固定Chromium，不自动下载")
    if not port_closed(args.backend_port):
        raise RuntimeError("PORT_IN_USE: 不接管其他进程")
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    result = run(directory, executable, args.backend_port)
    print(
        json.dumps(
            {"status": result["status"], "evidence": str(directory / "result.json")},
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
