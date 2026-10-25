"""Desktop Send freezes original audio time despite later playback movement."""

import argparse
import hashlib
import io
import json
import os
import re
import runpy
import sqlite3
import wave
from pathlib import Path

from test_browser import Browser, ROOT, check, port_closed, write_json
from test_desktop import composer_fill, press_button
from test_desktop_migration import create_course, wait_api
from test_mvp_a import api
from test_workbench_reading import digests
from test_workbench_workspace import WorkspaceInstance
from test_workspace_v2 import manager_entry


def timeline(directory):
    speech = directory / "base.wav"
    runpy.run_path(str(ROOT / "tests/fixtures/workbench/generate.py"))[
        "synthetic_speech"
    ](speech)
    with wave.open(str(speech), "rb") as reader:
        pcm = reader.readframes(reader.getnframes())
    assert len(pcm) < 30 * 32000
    samples = bytearray(180 * 32000)
    for second in (0, 60, 140):
        samples[second * 32000 : second * 32000 + len(pcm)] = pcm
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        writer.writeframes(samples)
    return output.getvalue()


def run_case(directory, executable, port):
    materials = directory / "course"
    materials.mkdir()
    audio = materials / "nearby-timeline.wav"
    audio.write_bytes(timeline(directory))
    original = hashlib.sha256(audio.read_bytes()).hexdigest()
    (materials / "migration-read.txt").write_text(
        "Synthetic folder setup only.", "utf-8"
    )
    result = {
        "status": "running",
        "assertions": [],
        "source_digests_before": digests(),
        "layer": "actual Chromium/native Host/local ASR with synthetic provider and source",
        "actual_model_attempts": 0,
    }
    instance, browser = (
        WorkspaceInstance(directory / "server", port),
        Browser(directory / "browser", executable),
    )
    try:
        instance.start()
        browser.command(
            "open", instance.url + "/desktop/", f"--config={browser.config}"
        )
        browser.snapshot("initial-native")
        course, mapping = create_course(
            browser, instance, {"course_root": str(materials)}, result
        )
        prefix = f"/courses/{course}/learning"
        opened = api(
            browser,
            f"/courses/{course}/files/open",
            {"path": audio.name},
            method="POST",
        )["body"]
        batch = api(
            browser,
            prefix + "/preparations",
            {
                "kind": "asr",
                "asset_id": opened["asset_id"],
                "source_version": opened["source_version"],
                "language": "en",
            },
            method="POST",
        )["body"]
        paused = api(
            browser,
            prefix + f"/preparations/{batch['job_id']}/pause",
            {"operation_id": "manual_pause", "expected_revision": batch["revision"]},
            method="POST",
        )
        check(
            paused["status"] == 200, "explicit_full_batch_pause_before_question", result
        )
        browser.snapshot("before-open-audio")
        manager_entry(browser, audio.name, double=True)
        browser.command(
            "run-code",
            "async(page)=>{await page.waitForFunction(()=>document.querySelector('[data-primary-video]')?.readyState>=2);await page.locator('[data-primary-video]').evaluate(v=>{v.pause();v.currentTime=75;});await page.waitForFunction(()=>Math.abs(document.querySelector('[data-primary-video]').currentTime-75)<0.1);}",
        )
        browser.snapshot("question-at-75")
        composer_fill(browser, "请核对当前时刻附近字幕并解释。")
        press_button(browser, "发送消息")
        browser.command(
            "run-code",
            "async(page)=>{await page.locator('[data-primary-video]').evaluate(v=>{v.currentTime=140;});}",
        )
        chat = wait_api(
            browser,
            f"/chats/{mapping['chat_id']}",
            "value.generations.length===1&&['completed','failed','unknown','cancelled'].includes(value.generations[0].state)",
            timeout_ms=40000,
        )
        check(
            chat["generations"][0]["body"] == "已核对问题附近字幕。",
            "native_send_uses_question_time_to_read_subtitles",
            result,
        )
        with sqlite3.connect(directory / "server/data/business.sqlite3") as conn:
            scope = json.loads(
                conn.execute(
                    "SELECT grants FROM learning_native_scopes WHERE run_id=?",
                    (chat["generations"][0]["generation_id"],),
                ).fetchone()[0]
            )
            receipt = json.loads(
                conn.execute(
                    "SELECT result FROM learning_native_query_receipts WHERE run_id=?",
                    (chat["generations"][0]["generation_id"],),
                ).fetchone()[0]
            )
        check(
            scope["subtitle_anchors"][0]["time_ms"] == 75000
            and receipt["start_ms"] == 30000
            and receipt["end_ms"] == 120000,
            "later_seek_to_140_does_not_change_original_submitted_75s_authority",
            result,
        )
        check(
            receipt["status"] == "matched"
            and not receipt["missing_units"]
            and all(
                30000
                <= item["source"]["reference"]["start_ms"]
                < item["source"]["reference"]["end_ms"]
                <= 120000
                for item in receipt["results"]
            ),
            "only_nearby_ready_subtitle_versions_reach_original_question",
            result,
        )
        batch_after = api(browser, prefix + f"/preparations/{batch['job_id']}")["body"]
        check(
            batch_after["state"] == "paused",
            "question_and_playback_do_not_resume_full_batch",
            result,
        )
        browser.snapshot("before-question-source-panel")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('button',{name:'问答来源',exact:true}).click();await page.getByRole('region',{name:'问答资料来源'}).getByText('问题附近字幕',{exact:true}).first().waitFor();}",
        )
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'nearby-sources.png'}",
            "--full-page",
        )
        evidence = api(
            browser, f"/desktop/sessions/{mapping['session_id']}/learning-evidence"
        )["body"]
        check(
            evidence["items"]
            and all(
                item["source"]["reference"]["kind"] == "subtitle"
                and item["source"]["content_role"] == "original"
                for item in evidence["items"]
            ),
            "program_source_cards_keep_exact_subtitle_identity",
            result,
        )
        browser.snapshot("before-original-time-jump")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('region',{name:'问答资料来源'}).getByRole('button',{name:'打开原文',exact:true}).first().click();await page.waitForFunction(()=>{const v=document.querySelector('[data-primary-video]');return v&&v.currentTime>=59&&v.currentTime<78;});}",
        )
        position = browser.evaluate(
            "()=>document.querySelector('[data-primary-video]').currentTime"
        )
        check(
            59 <= position < 78,
            "source_card_returns_to_frozen_original_audio_timestamp",
            result,
        )
        check(
            len(api(browser, f"/courses/{course}/chats")["body"]) == 1
            and api(browser, prefix + "/artifacts")["body"] == [],
            "nearby_question_retains_chat_without_creating_learning_artifacts",
            result,
        )
        result["status"] = "passed"
    except BaseException as error:
        result.update(status="failed", failure=f"{type(error).__name__}: {error}")
        try:
            browser.snapshot("failure")
            browser.command(
                "screenshot",
                f"--filename={browser.directory / 'failure.png'}",
                "--full-page",
            )
        except Exception:
            pass
    finally:
        result["browser_cleanup"] = browser.close()
        result["cleanup"] = instance.stop()
        result["source_digests_after"] = digests()
        result["source_changed_during_run"] = (
            result["source_digests_before"] != result["source_digests_after"]
        )
        result["original_preserved"] = (
            hashlib.sha256(audio.read_bytes()).hexdigest() == original
        )
        if result["source_changed_during_run"] or not result["original_preserved"]:
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--backend-port", type=int, default=19243)
    parser.add_argument(
        "--browser-path",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id) or not port_closed(
        args.backend_port
    ):
        raise RuntimeError("RUN_ID_INVALID_OR_PORT_IN_USE")
    output = ROOT / "output/playwright" / args.run_id
    output.mkdir(parents=True, exist_ok=False)
    outcome = run_case(output, args.browser_path, args.backend_port)
    print(
        json.dumps(
            {
                "status": outcome["status"],
                "assertions": len(outcome["assertions"]),
                "evidence": str(output / "result.json"),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if outcome["status"] == "passed" else 1)
