"""Real native-client migration acceptance with generated media/documents only.

Document selection uses the real native file input, not a direct upload API.
Folder choice is an explicit synthetic seam; Windows owner/foreground behavior
has a separate acceptance. No real course, user screen, or user video is read.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/e2e"))
sys.path.insert(0, str(ROOT / "backend/src"))

from test_browser import Browser, PYTHON, check, http, port_closed, wait_until, write_json  # noqa: E402
from test_desktop import composer_fill, press_button  # noqa: E402
from test_desktop_migration import (  # noqa: E402
    create_course, native_mapping, native_upload, open_course_video, source_digests, synthetic_assets, wait_api,
)
from test_mvp_a import api  # noqa: E402


def import_file(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError("VALIDATION_HELPER_MISSING")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


base = import_file("desktop_real_base", ROOT / "scripts/validation/Run-DesktopReal.py")


class MigrationRealInstance(base.RealInstance):
    def native_selection(self, *, folder: str | None = None, clipboard: list[str] | None = None):
        write_json(self.directory / "synthetic-native-input.json", {"folder": folder, "clipboard": clipboard or []})

    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.native_selection()
        self.log = (self.directory / "server.stdout.txt").open("w", encoding="utf-8")
        self.process = self.group.spawn(
            [str(PYTHON), str(Path(__file__).resolve()), "--serve-real", str(self.directory),
             "--backend-port", str(self.backend), "--model", self.arguments.model,
             "--thinking", self.arguments.thinking, "--max-output-tokens", str(self.arguments.max_output_tokens),
             "--timeout", str(self.arguments.timeout)],
            cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            stdout=self.log, stderr=subprocess.STDOUT,
        )

        def ready():
            if self.process.poll() is not None:
                raise RuntimeError("DESKTOP_MIGRATION_REAL_START_FAILED: see private server.stdout.txt")
            try:
                status, _, value = http(self.url + "/api/v1/health")
                return status == 200 and value["capabilities"]["question_answering"] == "ready"
            except OSError:
                return False
        wait_until(ready, timeout=60)
        return self


async def serve_real(directory: Path, arguments) -> int:
    import learning_app.course_files as files

    def chosen(*args, **kwargs):
        selection = json.loads((directory / "synthetic-native-input.json").read_text("utf-8"))
        return [selection["folder"]] if selection.get("folder") else []
    files.choose_paths = chosen
    return await base.serve_real(directory, arguments)


def digests() -> dict[str, str]:
    result = source_digests()
    for name in ("scripts/validation/Run-DesktopMigrationReal.py", "scripts/validation/Run-DesktopReal.py"):
        result[name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    return result


def thumbnail_scene(browser: Browser, result: dict) -> dict:
    open_course_video(browser, "synthetic-course.webm")
    browser.command("run-code", "async(page)=>{await page.locator('[data-primary-video]').evaluate(v=>v.pause());}")
    path = browser.evaluate("()=>{const content=new URL(document.querySelector('[data-primary-video]').currentSrc);"
                            "return content.pathname.replace(/content$/,'previews')+content.search}")
    manifest = wait_api(browser, path, "value.status==='ready'&&value.frames?.length>=3", timeout_ms=45000)
    browser.evaluate("()=>{window.__migrationRealPreviews=" + json.dumps(manifest) + ";return true;}")
    browser.snapshot("before-real-thumbnail-hover")
    browser.command("run-code", "async(page)=>{const slider=page.getByRole('slider',{name:'视频播放进度',exact:true});"
                    "const box=await slider.boundingBox();await page.mouse.move(box.x+box.width*.45,box.y+box.height/2);"
                    "await page.waitForFunction(()=>{const image=document.querySelector('[data-testid=video-preview-image]');"
                    "return image&&image.complete&&image.naturalWidth>0});}")
    shown = browser.evaluate("()=>{const image=document.querySelector('[data-testid=video-preview-image]');"
                             "return {src:image.currentSrc,width:image.naturalWidth,height:image.naturalHeight}}")
    hashes = browser.evaluate("async()=>{const result=[];for(const frame of window.__migrationRealPreviews.frames){"
                              "const response=await fetch(frame.url);if(!response.ok)throw Error('PREVIEW_BYTES_MISSING');"
                              "const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await response.arrayBuffer())))"
                              ".map(value=>value.toString(16).padStart(2,'0')).join('');result.push(hash);}return result;}")
    check(manifest["status"] == "ready" and manifest["width"] <= 320 and "/previews/" in shown["src"]
          and shown["width"] <= 320 and shown["height"] <= 240,
          "real_UI_uses_persisted_low_resolution_thumbnail_cache_before_question", result,
          frame_count=len(manifest["frames"]), shown=shown)
    return {"manifest": manifest, "shown": shown, "sha256": sorted(set(hashes))}


def capture_scene(browser: Browser):
    press_button(browser, "截图提问")
    browser.command("run-code", "async(page)=>{await page.getByRole('spinbutton',{name:'选区宽度',exact:true})"
                    ".waitFor({state:'visible'});const values=" + json.dumps(
                        {"选区宽度": 320, "选区高度": 180, "选区左侧": 160, "选区顶部": 90})
                    + ";for(const [name,value] of Object.entries(values)){await page.getByRole('spinbutton',"
                    "{name,exact:true}).fill(String(value));}}")
    press_button(browser, "确认选区并加入草稿")


def draft_round_trip(browser: Browser, instance, manifest: dict, course_id: str, result: dict) -> tuple[dict, list[dict]]:
    from PIL import Image

    mapping = native_mapping(browser, course_id)
    text = "未发送迁移草稿：通过普通消息补充或更正。"
    composer_fill(browser, text)
    capture_scene(browser)
    documents = Path(manifest["attachment_directory"])
    paths = [documents / "two-page-text.pdf", documents / "whole-document.txt", documents / "whole-document.md"]
    native_upload(browser, paths, new_chat_while_pending=True)
    wait_api(browser, "/api/v1/chats/" + mapping["chat_id"] + "/draft",
             "value.attachment_ids?.length===4", timeout_ms=30000)
    before = api(browser, "/chats/" + mapping["chat_id"] + "/draft")["body"]
    for _ in range(2):
        press_button(browser, "新对话")
    after = native_mapping(browser, course_id)
    check(after["chat_id"] == mapping["chat_id"] and after["session_id"] == mapping["session_id"]
          and len(api(browser, f"/courses/{course_id}/chats")["body"]) == 1,
          "native_New_Chat_reuses_unsent_chat_during_image_and_document_preparation", result)
    browser.command("reload")
    browser.command("run-code", "async(page)=>{await page.waitForFunction(text=>document.querySelector('[data-composer-input]')"
                    "?.textContent.includes(text)," + json.dumps(text) + ",{timeout:30000});"
                    "await page.getByText('two-page-text.pdf',{exact:false}).last().waitFor({state:'visible',timeout:30000});}")
    restored = api(browser, "/chats/" + mapping["chat_id"] + "/draft")["body"]
    check(restored["text"] == before["text"] and restored["attachment_ids"] == before["attachment_ids"]
          and len(restored["attachment_ids"]) == 4 and instance.counts()["real_provider_attempts"] == 0,
          "native_file_receipts_and_confirmed_image_draft_survive_reload_without_model_dispatch", result)
    check(not browser.evaluate("()=>document.body.innerText.includes('题目条件与更正')"
                               "||document.body.innerText.includes('当前题目条件（每行一项）')"),
          "ordinary_message_flow_has_no_independent_conditions_or_corrections_controls", result)
    metadata = [api(browser, f"/attachments/{aid}/metadata")["body"] for aid in restored["attachment_ids"]]
    originals = {path.name: path for path in paths}
    for item in metadata:
        data = bytes(browser.evaluate("async()=>Array.from(new Uint8Array(await(await fetch("
                                      + json.dumps(item["download_url"]) + ")).arrayBuffer()))"))
        if item["kind"] == "image":
            with Image.open(io.BytesIO(data)) as selected:
                dimensions = selected.size
            source = api(browser, f"/attachments/{item['attachment_id']}/source")["body"]
            check(dimensions == (320, 180) and source.get("crop") == {"x": 160, "y": 90, "width": 320, "height": 180}
                  and hashlib.sha256(data).hexdigest() == item["sha256"],
                  "native_confirmed_crop_stays_exactly_320x180_across_draft_reload", result,
                  dimensions=dimensions, sha256=item["sha256"], crop=source.get("crop"))
            continue
        expected = originals[item["filename"]].read_bytes()
        check(data == expected and item["sha256"] == hashlib.sha256(expected).hexdigest(),
              "native_" + item["kind"] + "_upload_receipt_preserves_complete_original", result,
              filename=item["filename"], bytes=len(data), page_count=item.get("page_count"))
    browser.snapshot("restored-native-migration-draft")
    return mapping, metadata


def document_evidence(instance, chat_id: str, metadata: list[dict]) -> dict:
    with sqlite3.connect(instance.directory / "data/business.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT attachment_id,filename,payload FROM document_attachments WHERE chat_id=?", (chat_id,)).fetchall()
    documents = [{"attachment_id": row["attachment_id"], "filename": row["filename"],
                  "payload": json.loads(row["payload"])} for row in rows]
    image = next(value for value in metadata if value["kind"] == "image")
    pdf = next(value for value in documents if value["filename"] == "two-page-text.pdf")
    expected = [image["sha256"], *(page["sha256"] for page in pdf["payload"]["pages"])]
    return {"documents": documents, "confirmed_image": image, "expected_image_hashes": expected,
            "pdf": pdf, "whole_documents": all(value["payload"].get("whole_document") for value in documents)}


def actual_request(instance, request_id: str) -> dict:
    if not re.fullmatch(r"boundary-[a-f0-9]+", request_id):
        raise ValueError("UNEXPECTED_BOUNDARY_ID")
    path = instance.directory / "execution/boundary/private/boundary" / (request_id + ".request.json")
    return json.loads(path.read_text("utf-8"))


def native_rows(browser: Browser) -> list[dict]:
    return browser.evaluate("()=>Array.from(document.querySelectorAll('[data-learning-region=ai] [data-chat-flow-kind=user]'))"
                            ".map(row=>({text:row.innerText,images:row.querySelectorAll('img').length,"
                            "attachments:row.querySelector('[data-message-attachments]')?.innerText||''}))")


def run(arguments) -> dict:
    directory = ROOT / ".runtime/validation/desktop-migration-real" / arguments.run_id
    directory.mkdir(parents=True, exist_ok=False)
    artifacts = ROOT / "output/playwright" / arguments.run_id
    artifacts.mkdir(parents=True, exist_ok=False)
    manifest = synthetic_assets(directory / "synthetic", arguments.ffmpeg_path)
    before = digests()
    result = {"run_id": arguments.run_id, "client_date": "2026-10-22", "status": "running",
              "evidence_level": "real Chromium native Harness UI, real file input and draft restore, project backend, SEA Host, real provider",
              "route": "api_key", "model": arguments.model, "thinking": arguments.thinking,
              "max_output_tokens": arguments.max_output_tokens, "timeout_seconds": arguments.timeout,
              "synthetic_manifest": manifest, "source_digests_before": before, "assertions": [], "cases": [],
              "limitations": ["Only generated files/video are used; folder selection is an explicit synthetic seam.",
                              "No Electron foreground/window, installer or human learning acceptance is claimed.",
                              "Missing usage stays unknown; no request is automatically repeated."]}
    instance = MigrationRealInstance(directory / "server", arguments.backend_port, arguments)
    browser = Browser(artifacts / "browser", arguments.browser_path)
    try:
        instance.start()
        browser.command("open", instance.url + "/desktop/", f"--config={browser.config}")
        course_id, _ = create_course(browser, instance, manifest, result)
        previews = thumbnail_scene(browser, result)
        result["previews"] = previews
        mapping, metadata = draft_round_trip(browser, instance, manifest, course_id, result)
        docs = document_evidence(instance, mapping["chat_id"], metadata)
        result["confirmed_documents"] = docs
        check(docs["whole_documents"] and docs["pdf"]["payload"]["page_count"] == 2
              and [page["page"] for page in docs["pdf"]["payload"]["pages"]] == [1, 2],
              "native_PDF_receipt_prepares_both_pages_in_original_order", result)
        prompts = [
            ("whole-documents-and-confirmed-crop", "请完整解答我确认截图中的方程并验根，同时逐项列出PDF第一页和第二页的标记、TXT与Markdown各自末尾标记，说明整份资料都已收到。不要读取截图之外的视频画面。", False),
            ("ordinary-message-active-stop", "请从基本定义详细推导二次方程求根公式，再逐题给出至少60个不同整系数二次方程的完整解答，每题列出配方和验根过程。持续详细输出，暂时不要总结。", True),
            ("ordinary-correction-follow-up", "更正我刚才的要求：停止长篇示例，仍使用最初截图中的方程。请只给出两个根并验根，然后列出两页PDF标记、TXT和Markdown的末尾标记。", False),
        ]
        expected_hashes = docs["expected_image_hashes"]
        for label, prompt, stop in prompts:
            composer_fill(browser, prompt)
            press_button(browser, "发送消息")
            generation = base.generation_for(instance, prompt)
            observed = base.observe(instance, browser, generation["generation_id"], arguments, stop=stop)
            observed["id"] = label
            write_json(directory / "private" / f"case-{label}.json", observed)
            summary = {key: observed[key] for key in ("id", "passed", "session_id", "epoch", "host_alive", "actual_attempts", "stop_evidence", "elapsed_ms")}
            summary.update(generation_id=generation["generation_id"], state=observed["generation"]["state"],
                           body_characters=len(observed["generation"]["body"]), usage=observed["generation"]["usage"],
                           boundary=observed["boundary"])
            result["cases"].append(summary)
            request = actual_request(instance, observed["boundary"][0]["request_id"])
            texts = "\n".join(part.get("text", "") for message in request.get("messages", [])
                              for part in message.get("content", []) if isinstance(part, dict) and part.get("type") == "text")
            images = [image for row in observed["boundary"] for image in row.get("images", [])]
            actual_hashes = [image["sha256"] for image in images]
            check(set(actual_hashes) <= set(expected_hashes) and not (set(actual_hashes) & set(previews["sha256"]))
                  and "/previews/" not in texts,
                  label + "_has_no_thumbnail_or_unconfirmed_video_frame", result,
                  images=images, allowed_hashes=expected_hashes)
            if len(result["cases"]) == 1:
                check(actual_hashes == expected_hashes,
                      "first_real_request_has_exact_confirmed_crop_and_two_PDF_pages_in_order", result)
                paths = Path(manifest["attachment_directory"])
                check((paths / "whole-document.txt").read_bytes().decode("utf-8-sig") in texts
                      and (paths / "whole-document.md").read_bytes().decode("utf-8-sig") in texts,
                      "actual_provider_request_contains_complete_TXT_and_Markdown_without_truncation", result)
            check(observed["passed"] and observed["actual_attempts"] == 1,
                  label + "_uses_native_UI_and_one_actual_attempt", result)
            if len(result["cases"]) == 1:
                answer = observed["generation"]["body"].replace("\\_", "_")
                check(all(value in answer for value in (
                    "ALPHA", "1729", "OMEGA", "2718", "TEXT_END_2718", "MARKDOWN_END_5926")),
                    "real_answer_reports_both_PDF_page_markers_and_text_document_end_markers", result)
            browser.snapshot("after-" + label)
        check(len({case["session_id"] for case in result["cases"]}) == 1,
              "whole_document_question_Stop_and_ordinary_correction_keep_native_session", result)
        rows = native_rows(browser)
        for _, prompt, _ in prompts[1:]:
            row = next((value for value in rows if prompt in value["text"]), None)
            check(row is not None and row["images"] == 0 and not row["attachments"],
                  "native_current_user_row_does_not_repeat_historical_image_or_document_attachments", result,
                  prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(), row=row)
        check(not browser.evaluate("()=>document.body.innerText.includes('[[learning-generation:')"),
              "native_migrated_transcript_hides_internal_execution_markers", result)
        browser.command("reload")
        open_course_video(browser, "synthetic-course.webm")
        browser.command("run-code", "async(page)=>{await page.locator('[data-primary-video]').evaluate(v=>v.pause());}")
        browser.snapshot("completed-real-migration")
        browser.command("screenshot", f"--filename={artifacts / 'completed-real-migration.png'}", "--full-page")
        result["counts"] = instance.counts()
        check(result["counts"]["real_provider_attempts"] == 3 and result["counts"]["business_attempts"] == 3
              and result["counts"]["submissions"] == 3,
              "native_file_upload_restore_reload_and_three_submissions_reconcile_to_three_actual_attempts", result)
        result["status"] = "passed"
    except BaseException as error:
        result.update(status="failed", failure=f"{type(error).__name__}: {error}")
        try:
            result["counts"] = instance.counts()
            browser.snapshot("failure")
            browser.command("screenshot", f"--filename={artifacts / 'failure.png'}", "--full-page")
        except Exception:
            pass
    finally:
        result["cleanup"] = instance.stop()
        result["browser_cleanup"] = browser.close()
        after = digests()
        result["source_digests_after"] = after
        result["changed_files_during_run"] = [name for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)]
        if result["changed_files_during_run"] and result["status"] == "passed":
            result.update(status="failed", failure="SOURCE_CHANGED_DURING_VERIFICATION")
        write_json(directory / "result.json", result)
    return result


def resume_run(arguments) -> dict:
    """Continue two unsent cases after a known first answer and optional truncation.

    This narrow recovery exists for the r1 report-reader path defect. It never
    resends the first prompt or changes product state/source. The original run
    remains intact; only its stopped, generated test state is copied.
    """
    parent = ROOT / ".runtime/validation/desktop-migration-real"
    previous_dir = parent / arguments.resume_from
    previous = json.loads((previous_dir / "result.json").read_text("utf-8"))
    inherited_attempts = previous["counts"]["real_provider_attempts"]
    prior_cases = previous.get("cases", [])
    if (inherited_attempts not in {1, 2} or len(prior_cases) != inherited_attempts
            or prior_cases[0]["state"] != "completed"):
        raise RuntimeError("RESUME_REQUIRES_KNOWN_COMPLETED_FIRST_REQUEST")
    if inherited_attempts == 2 and (prior_cases[1]["state"] != "truncated"
                                    or prior_cases[1]["body_characters"] != 0
                                    or prior_cases[1].get("stop_evidence") is not None):
        raise RuntimeError("RESUME_SECOND_CASE_MUST_BE_KNOWN_TRUNCATED_WITHOUT_STOP")
    if any(previous[name] != getattr(arguments, name) for name in ("model", "thinking", "max_output_tokens")):
        raise RuntimeError("RESUME_MODEL_CONFIGURATION_CHANGED")
    directory = parent / arguments.run_id
    directory.mkdir(parents=True, exist_ok=False)
    artifacts = ROOT / "output/playwright" / arguments.run_id
    artifacts.mkdir(parents=True, exist_ok=False)
    before = digests()
    changed = [name for name in set(before) | set(previous["source_digests_before"])
               if before.get(name) != previous["source_digests_before"].get(name)]
    runner_name = Path(__file__).relative_to(ROOT).as_posix()
    if any(name != runner_name for name in changed):
        raise RuntimeError("RESUME_PRODUCT_OR_INPUT_SOURCE_CHANGED")
    original_database = previous_dir / "server/data/business.sqlite3"
    database_sha256 = hashlib.sha256(original_database.read_bytes()).hexdigest()
    shutil.copytree(previous_dir / "server", directory / "server")
    if hashlib.sha256((directory / "server/data/business.sqlite3").read_bytes()).hexdigest() != database_sha256:
        raise RuntimeError("RESUME_STATE_COPY_CHANGED")
    manifest = previous["synthetic_manifest"]
    previews = previous["previews"]
    docs = previous["confirmed_documents"]
    result = {**previous, "run_id": arguments.run_id, "status": "running", "failure": None,
              "evidence_level": "explicit two-request continuation of one known completed native-UI request; generated state copied, product unchanged",
              "source_digests_before": before, "assertions": [],
              "cases": [{**case, "inherited_from_run": arguments.resume_from} for case in prior_cases],
              "resume": {"previous_run_id": arguments.resume_from, "previous_report_status": previous["status"],
                         "previous_failure": previous.get("failure"), "inherited_actual_attempts": inherited_attempts,
                         "copied_business_sqlite_sha256": database_sha256,
                         "product_and_bundle_hashes_unchanged": True,
                         "runner_hash_before": previous["source_digests_before"].get(runner_name),
                         "runner_hash_after": before[runner_name],
                         "first_prompt_is_not_resubmitted": True}}
    instance = MigrationRealInstance(directory / "server", arguments.backend_port, arguments)
    browser = Browser(artifacts / "browser", arguments.browser_path)
    first = previous["cases"][0]
    try:
        instance.start()
        browser.command("open", instance.url + "/desktop/", f"--config={browser.config}")
        # GET is the first generation operation: confirm the original result
        # before considering either remaining, previously unsent prompt.
        stored = api(browser, "/generations/" + first["generation_id"])["body"]
        check(stored["state"] == "completed" and instance.counts()["real_provider_attempts"] == inherited_attempts,
              "resume_GET_confirms_original_completed_request_and_exact_inherited_attempts", result)
        if inherited_attempts == 2:
            truncated = api(browser, "/generations/" + prior_cases[1]["generation_id"])["body"]
            check(truncated["state"] == "truncated" and not truncated["body"] and not truncated["cancel_requested"],
                  "resume_GET_confirms_known_thinking_only_truncation_without_claiming_Stop", result)
        chat = api(browser, "/courses")["body"][0]
        browser.command("goto", instance.url + "/desktop/?course_id=" + chat["course_id"])
        browser.command("run-code", "async(page)=>{await page.locator('[data-composer-input]')"
                        ".waitFor({state:'visible',timeout:30000});}")
        mapping = native_mapping(browser, chat["course_id"])
        check(mapping["session_id"] == first["session_id"],
              "resume_reopens_original_business_and_native_session_without_first_prompt_replay", result)
        expected_hashes = docs["expected_image_hashes"]
        original_request = actual_request(instance, first["boundary"][0]["request_id"])
        original_texts = "\n".join(part.get("text", "") for message in original_request.get("messages", [])
                                   for part in message.get("content", []) if isinstance(part, dict) and part.get("type") == "text")
        check([image["sha256"] for image in first["boundary"][0]["images"]] == expected_hashes,
              "preserved_first_request_has_exact_confirmed_crop_and_two_PDF_pages_in_order", result)
        document_dir = Path(manifest["attachment_directory"])
        check((document_dir / "whole-document.txt").read_bytes().decode("utf-8-sig") in original_texts
              and (document_dir / "whole-document.md").read_bytes().decode("utf-8-sig") in original_texts,
              "preserved_first_request_contains_complete_original_TXT_and_Markdown", result)
        first_body = stored["body"].replace("\\_", "_")
        check(all(value in first_body for value in ("ALPHA", "1729", "OMEGA", "2718", "TEXT_END_2718", "MARKDOWN_END_5926")),
              "preserved_first_answer_reports_both_PDF_pages_and_document_end_markers", result)
        stop_label = "ordinary-message-active-stop-retest" if inherited_attempts == 2 else "ordinary-message-active-stop"
        stop_prompt = ("接下来不做计算或推导，请直接写一篇面向初学者的中文学习方法导读，分100段，每段约100字，围绕预习、听课、笔记、练习、复习、错题整理依次展开。现在开始写正文，暂不总结。"
                       if inherited_attempts == 2 else "请从基本定义详细推导二次方程求根公式，再逐题给出至少60个不同整系数二次方程的完整解答，每题列出配方和验根过程。持续详细输出，暂时不要总结。")
        prompts = [
            (stop_label, stop_prompt, True),
            ("ordinary-correction-follow-up", "更正我刚才的要求：停止长篇示例，仍使用最初截图中的方程。请只给出两个根并验根，然后列出两页PDF标记、TXT和Markdown的末尾标记。", False),
        ]
        for label, prompt, stop in prompts:
            composer_fill(browser, prompt)
            press_button(browser, "发送消息")
            generation = base.generation_for(instance, prompt)
            observed = base.observe(instance, browser, generation["generation_id"], arguments, stop=stop)
            observed["id"] = label
            write_json(directory / "private" / f"case-{label}.json", observed)
            summary = {key: observed[key] for key in ("id", "passed", "session_id", "epoch", "host_alive", "actual_attempts", "stop_evidence", "elapsed_ms")}
            summary.update(generation_id=generation["generation_id"], state=observed["generation"]["state"],
                           body_characters=len(observed["generation"]["body"]), usage=observed["generation"]["usage"],
                           boundary=observed["boundary"])
            result["cases"].append(summary)
            request = actual_request(instance, observed["boundary"][0]["request_id"])
            request_text = "\n".join(part.get("text", "") for message in request.get("messages", [])
                                     for part in message.get("content", []) if isinstance(part, dict) and part.get("type") == "text")
            images = [image for row in observed["boundary"] for image in row.get("images", [])]
            check(set(image["sha256"] for image in images) <= set(expected_hashes)
                  and not (set(image["sha256"] for image in images) & set(previews["sha256"]))
                  and "/previews/" not in request_text,
                  label + "_contains_only_confirmed_scope_and_no_thumbnail", result, images=images)
            check(observed["passed"] and observed["actual_attempts"] == 1 and observed["session_id"] == first["session_id"],
                  label + "_uses_original_native_session_and_one_new_actual_attempt", result)
            browser.snapshot("after-" + label)
        rows = native_rows(browser)
        for _, prompt, _ in prompts:
            row = next((value for value in rows if prompt in value["text"]), None)
            check(row is not None and row["images"] == 0 and not row["attachments"],
                  "native_current_user_row_does_not_repeat_historical_image_or_document_attachments", result, row=row)
        check(not browser.evaluate("()=>document.body.innerText.includes('[[learning-generation:')"),
              "continued_native_transcript_hides_internal_execution_markers", result)
        browser.command("reload")
        open_course_video(browser, "synthetic-course.webm")
        browser.command("run-code", "async(page)=>{await page.locator('[data-primary-video]').evaluate(v=>v.pause());}")
        browser.snapshot("completed-real-migration")
        browser.command("screenshot", f"--filename={artifacts / 'completed-real-migration.png'}", "--full-page")
        result["counts"] = instance.counts()
        result["new_real_model_attempts"] = result["counts"]["real_provider_attempts"] - inherited_attempts
        expected_total = inherited_attempts + 2
        check(result["counts"]["real_provider_attempts"] == expected_total
              and result["counts"]["business_attempts"] == expected_total
              and result["counts"]["submissions"] == expected_total and result["new_real_model_attempts"] == 2,
              "cross_phase_prompts_reconcile_to_exact_actual_attempts_without_historical_prompt_replay", result)
        result["historical_truncation_retained"] = inherited_attempts == 2
        result["status"] = "passed"
    except BaseException as error:
        result.update(status="failed", failure=f"{type(error).__name__}: {error}")
        try:
            result["counts"] = instance.counts()
            browser.snapshot("failure")
            browser.command("screenshot", f"--filename={artifacts / 'failure.png'}", "--full-page")
        except Exception:
            pass
    finally:
        result["cleanup"] = instance.stop()
        result["browser_cleanup"] = browser.close()
        after = digests()
        result["source_digests_after"] = after
        result["changed_files_during_run"] = [name for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)]
        if result["changed_files_during_run"] and result["status"] == "passed":
            result.update(status="failed", failure="SOURCE_CHANGED_DURING_VERIFICATION")
        write_json(directory / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="desktop-migration-real-" + time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--backend-port", type=int, default=19060)
    parser.add_argument("--resume-from", help="接续已完成首问及可选已知截断样例的本轮私有run-id；不重发历史")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--thinking", default="high")
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--browser-path", type=Path, default=Path(os.environ.get("LOCALAPPDATA", ""))
                        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe")
    parser.add_argument("--ffmpeg-path", type=Path, default=Path(os.environ.get("LOCALAPPDATA", ""))
                        / "Microsoft/WinGet/Links/ffmpeg.EXE")
    parser.add_argument("--serve-real", type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.serve_real:
        return asyncio.run(serve_real(arguments.serve_real, arguments))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", arguments.run_id):
        parser.error("run-id只能包含字母、数字、横线和下划线")
    if arguments.resume_from and not re.fullmatch(r"[A-Za-z0-9_-]+", arguments.resume_from):
        parser.error("resume-from只能是本轮私有run-id")
    if not arguments.browser_path.is_file() or not arguments.ffmpeg_path.is_file():
        raise RuntimeError("LOCAL_TEST_TOOL_MISSING: 指定已有Chromium和ffmpeg，不自动下载")
    if not port_closed(arguments.backend_port):
        raise RuntimeError("PORT_IN_USE: 不接管其他进程")
    result = resume_run(arguments) if arguments.resume_from else run(arguments)
    print(json.dumps({"status": result["status"], "real_model_attempts": result.get("counts", {}).get("real_provider_attempts"),
                      "evidence": str(ROOT / ".runtime/validation/desktop-migration-real" / arguments.run_id / "result.json")}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
