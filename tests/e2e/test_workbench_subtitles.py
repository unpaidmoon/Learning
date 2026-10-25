"""Actual local synthetic speech -> durable ASR -> native desk caption operations."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path

from test_browser import Browser, ROOT, check, port_closed, write_json
from test_desktop_migration import MigrationInstance, create_course
from test_mvp_a import api
from test_workbench_reading import digests
from test_workspace_v2 import manager_entry


def run_case(directory: Path, executable: Path, port: int):
    spec = importlib.util.spec_from_file_location(
        "subtitle_fixture", ROOT / "tests/fixtures/workbench/generate.py"
    )
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    material = directory / "synthetic-course"
    material.mkdir()
    speech = material / "synthetic-lesson.wav"
    generator.synthetic_speech(speech)
    original = hashlib.sha256(speech.read_bytes()).hexdigest()
    (material / "migration-read.txt").write_text(
        "Synthetic course setup only.", "utf-8"
    )
    result = {
        "id": "workbench-subtitles",
        "status": "running",
        "assertions": [],
        "layer": "real Chromium + pinned Harness + installed ASR/CUDA + synthetic local speech",
        "actual_model_attempts": 0,
        "source_digests_before": digests(),
        "limits": [
            "Synthetic clear English TTS; no user recording, WER or human learning acceptance."
        ],
    }
    instance = MigrationInstance(directory / "server", port)
    browser = Browser(directory / "browser", executable)
    try:
        instance.start()
        browser.command(
            "open", instance.url + "/desktop/", f"--config={browser.config}"
        )
        browser.snapshot("native-initial")
        cid, mapping = create_course(
            browser, instance, {"course_root": str(material)}, result
        )
        browser.snapshot("before-open-audio")
        manager_entry(browser, speech.name, double=True)
        browser.command(
            "run-code",
            """async(page)=>{
          await page.getByRole('region',{name:'课程字幕',exact:true}).locator('[data-subtitle-segment]').first().waitFor({timeout:30000});
          await page.waitForFunction(()=>document.querySelector('.subtitle-lines')?.textContent.toLowerCase().includes('synthetic'));
        }""",
        )
        browser.snapshot("captions-ready")
        media = api(browser, f"/courses/{cid}/videos")["body"]
        check(
            len(media) == 1 and media[0]["media_type"] == "audio/wav",
            "course_recording_uses_versioned_native_playback",
            result,
        )
        asset = media[0]
        prefix = f"/courses/{cid}/learning"
        ids = api(
            browser,
            prefix
            + f"/audio/{asset['asset_id']}/{asset['source_version']}/transcripts",
        )["body"]
        track = api(browser, prefix + f"/transcripts/{ids[0]}")["body"]
        check(
            track["units_total"] == 1
            and not track["missing_units"]
            and track["segments"],
            "automatic_local_transcription_is_saved_with_complete_audio_window_coverage",
            result,
            track_id=track["track_id"],
            audio_end_ms=track["audio_end_ms"],
        )
        original_segment = track["segments"][0]
        check(
            all(segment["origin"] == "original" for segment in track["segments"]),
            "asr_originals_have_source_version_and_unmodified_role",
            result,
        )
        browser.snapshot("before-quote-original")
        browser.command(
            "run-code",
            """async(page)=>{const line=page.locator('[data-subtitle-segment]').first();await line.hover();await line.getByRole('button',{name:'加入 AI',exact:true}).click();await page.locator('[data-source-snapshot]').first().waitFor();}""",
        )
        draft = api(browser, f"/chats/{mapping['chat_id']}/draft")["body"]
        snapshot = api(
            browser, prefix + f"/selections/{draft['source_snapshot_ids'][0]}"
        )["body"]
        check(
            snapshot["text"] == original_segment["text"]
            and snapshot["reference"]["kind"] == "subtitle"
            and snapshot["reference"]["start_ms"] == original_segment["start_ms"]
            and snapshot["content_role"] == "original",
            "caption_action_freezes_only_one_exact_segment_in_original_chat_draft",
            result,
        )
        browser.snapshot("before-user-correction")
        browser.command(
            "run-code",
            """async(page)=>{const line=page.locator('[data-subtitle-segment]').first();await line.hover();await line.getByRole('button',{name:'修改',exact:true}).click();await page.getByRole('textbox',{name:'字幕修订正文',exact:true}).fill('This subtitle was corrected by the user.');await page.getByRole('button',{name:'保存字幕',exact:true}).click();await page.getByRole('textbox',{name:'字幕修订正文',exact:true}).waitFor({state:'hidden'});await page.locator('.subtitle-line').first().getByText('This subtitle was corrected by the user.',{exact:true}).waitFor();}""",
        )
        edited = api(browser, prefix + f"/transcripts/{ids[0]}")["body"]
        check(
            edited["segments"][0]["text"] == "This subtitle was corrected by the user."
            and edited["segments"][0]["raw_text"] == original_segment["text"]
            and edited["segments"][0]["origin"] == "user_note"
            and edited["edit_head_id"],
            "visible_edit_saves_a_user_revision_without_overwriting_raw_asr",
            result,
        )
        browser.snapshot("before-quote-correction")
        browser.command(
            "run-code",
            """async(page)=>{const line=page.locator('[data-subtitle-segment]').first();await line.hover();await line.getByRole('button',{name:'加入 AI',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('[data-source-snapshot]').length===2);}""",
        )
        draft = api(browser, f"/chats/{mapping['chat_id']}/draft")["body"]
        selected = [
            api(browser, prefix + f"/selections/{sid}")["body"]
            for sid in draft["source_snapshot_ids"]
        ]
        check(
            [item["content_role"] for item in selected] == ["original", "user_note"]
            and selected[0]["text"] == original_segment["text"]
            and selected[1]["text"] == "This subtitle was corrected by the user.",
            "old_original_quote_and_new_user_caption_remain_distinct_immutable_sources",
            result,
        )
        browser.snapshot("before-timestamp-jump")
        browser.command(
            "run-code",
            """async(page)=>{await page.locator('video').evaluate(v=>{v.pause();v.currentTime=12;});await page.locator('[data-source-snapshot]').first().getByRole('button',{name:'回看原文',exact:true}).click();await page.waitForFunction(()=>document.querySelector('video').currentTime<3);}""",
        )
        check(
            browser.evaluate("()=>document.querySelector('video').currentTime") < 3,
            "source_card_returns_to_original_audio_version_timestamp",
            result,
        )
        check(
            len(api(browser, f"/courses/{cid}/chats")["body"]) == 1
            and not api(browser, f"/chats/{mapping['chat_id']}")["body"]["messages"],
            "caption_reading_editing_and_jumps_keep_native_chat_and_unsent_sources",
            result,
        )
        check(
            instance.counts()["local_messages"]
            == instance.counts()["real_provider_attempts"]
            == 0,
            "caption_workflow_never_calls_remote_provider_or_auto_corrects_with_ai",
            result,
        )
        check(
            hashlib.sha256(speech.read_bytes()).hexdigest() == original,
            "original_recording_bytes_remain_unchanged",
            result,
        )
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'subtitles.png'}",
            "--full-page",
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
            (browser.directory / "console.txt").write_text(
                browser.command("console"), "utf-8"
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
        if result["source_changed_during_run"]:
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--backend-port", type=int, default=19233)
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
    result = run_case(output, args.browser_path, args.backend_port)
    print(
        json.dumps(
            {
                "status": result["status"],
                "assertions": len(result["assertions"]),
                "evidence": str(output / "result.json"),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if result["status"] == "passed" else 1)
