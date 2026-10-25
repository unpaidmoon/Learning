"""Pinned generic PDF Viewer inside the real Harness UI, synthetic PDF only."""

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
from test_workspace_v2 import manager_entry


def digests():
    files = []
    for folder in (
        "backend/src/learning_app",
        "frontend/src",
        "desktop",
        "harness/plugins",
        "resources/workbench",
    ):
        files.extend(
            path
            for path in (ROOT / folder).rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and (
                folder != "resources/workbench"
                or path.name.endswith(".lock.json")
                or any(part.startswith("pdfjs-") for part in path.parts)
            )
        )
    files.extend(
        path for path in (ROOT / ".runtime/desktop/client").glob("*") if path.is_file()
    )
    for folder in (".runtime/workbench/schedulers", ".runtime/workbench/essay"):
        files.extend(path for path in (ROOT / folder).rglob("*") if path.is_file())
    return {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(files)
    }


def make_pdf(path):
    specification = importlib.util.spec_from_file_location(
        "workbench_fixture_generator", ROOT / "tests/fixtures/workbench/generate.py"
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    module.reading_pdf(path, 34)


def run_case(directory: Path, executable: Path, port: int):
    material = directory / "synthetic-course"
    material.mkdir()
    make_pdf(material / "reader-34.pdf")
    (material / "migration-read.txt").write_text(
        "Synthetic file for shared course setup only.", "utf-8"
    )
    result = {
        "id": "workbench-reading",
        "status": "running",
        "assertions": [],
        "layer": "real Chromium + pinned Harness + local PDF.js",
        "actual_model_attempts": 0,
        "source_digests_before": digests(),
        "limits": [
            "Synthetic PDF reading, exact draft and translation via real SEA/local provider; OCR, real model and human experience are not claimed."
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
        course_id, mapping = create_course(
            browser, instance, {"course_root": str(material)}, result
        )
        before = mapping["chat_id"]
        browser.snapshot("before-open-pdf")
        manager_entry(browser, "reader-34.pdf", double=True)
        browser.command(
            "run-code",
            "async(page)=>{await page.locator('iframe[data-learning-pdf-reader]').waitFor({state:'visible',timeout:15000});const frame=page.frameLocator('iframe[data-learning-pdf-reader]');await frame.locator('.page[data-page-number=\"1\"] canvas').waitFor({state:'visible',timeout:25000});}",
        )
        browser.snapshot("pdf-loaded")
        state = browser.evaluate(
            "() => {const w=document.querySelector('iframe[data-learning-pdf-reader]').contentWindow;const a=w.PDFViewerApplication;return {pages:a.pdfDocument.numPages,source:a.url,scripting:w.PDFViewerApplicationOptions.get('enableScripting'),eval:w.PDFViewerApplicationOptions.get('isEvalSupported'),editor:w.PDFViewerApplicationOptions.get('annotationEditorMode'),canvas:!!w.document.querySelector('.page canvas')};}"
        )
        check(
            state["pages"] == 34 and state["canvas"],
            "reader_does_not_inherit_32_page_attachment_limit",
            result,
            observed=state,
        )
        check(
            not state["scripting"] and not state["eval"] and state["editor"] == -1,
            "document_execution_and_editing_disabled_before_open",
            result,
        )
        browser.snapshot("before-page-jump")
        browser.command(
            "run-code",
            "async(page)=>{const f=page.frameLocator('iframe[data-learning-pdf-reader]');await f.locator('#pageNumber').fill('34');await f.locator('#pageNumber').press('Enter');await f.locator('.page[data-page-number=\"34\"] canvas').waitFor({state:'visible'});}",
        )
        current = browser.evaluate(
            "() => document.querySelector('iframe[data-learning-pdf-reader]').contentWindow.PDFViewerApplication.pdfViewer.currentPageNumber"
        )
        check(current == 34, "native_page_control_opens_last_page", result)
        browser.snapshot("before-scale")
        browser.command(
            "run-code",
            "async(page)=>{await page.frameLocator('iframe[data-learning-pdf-reader]').locator('#scaleSelect').selectOption('page-fit');}",
        )
        scale = browser.evaluate(
            "() => document.querySelector('iframe[data-learning-pdf-reader]').contentWindow.PDFViewerApplication.pdfViewer.currentScaleValue"
        )
        check(scale == "page-fit", "native_fit_page_scale_operates", result)
        browser.snapshot("before-rotate")
        browser.command(
            "run-code",
            "async(page)=>{const f=page.frameLocator('iframe[data-learning-pdf-reader]');await f.locator('#secondaryToolbarToggleButton').click();await f.locator('#pageRotateCw').click();}",
        )
        rotation = browser.evaluate(
            "() => document.querySelector('iframe[data-learning-pdf-reader]').contentWindow.PDFViewerApplication.pdfViewer.pagesRotation"
        )
        check(rotation == 90, "native_rotation_operates", result)
        browser.snapshot("before-search")
        browser.command(
            "run-code",
            "async(page)=>{const f=page.frameLocator('iframe[data-learning-pdf-reader]');await f.locator('#viewFindButton').click();await f.locator('#findInput').fill('Learning');await page.waitForFunction(()=>{const a=document.querySelector('iframe[data-learning-pdf-reader]').contentWindow.PDFViewerApplication;return a.findController.pageMatches.filter(x=>x?.length).length===34;});}",
        )
        matches = browser.evaluate(
            "() => document.querySelector('iframe[data-learning-pdf-reader]').contentWindow.PDFViewerApplication.findController.pageMatches.map(x=>x?.length||0)"
        )
        check(
            len(matches) == 34 and all(value == 1 for value in matches),
            "native_search_indexes_all_text_pages",
            result,
        )
        chats = api(browser, f"/courses/{course_id}/chats")["body"]
        check(
            len(chats) == 1 and chats[0]["chat_id"] == before,
            "opening_and_reading_keeps_same_chat",
            result,
        )
        counts = instance.counts()
        check(
            counts["local_messages"] == counts["real_provider_attempts"] == 0,
            "reading_never_submits_pdf_or_calls_model",
            result,
            counts=counts,
        )
        browser.snapshot("before-text-selection")
        browser.command(
            "run-code",
            """async(page)=>{
          await page.evaluate(()=>{const w=document.querySelector('iframe[data-learning-pdf-reader]').contentWindow;
            const a=w.PDFViewerApplication; a.pdfViewer.pagesRotation=0; a.pdfViewer.currentPageNumber=1;
            a.findBar.close();});
          await page.frameLocator('iframe[data-learning-pdf-reader]').locator('.page[data-page-number="1"] .textLayer span').first().waitFor();
          await page.evaluate(()=>{const w=document.querySelector('iframe[data-learning-pdf-reader]').contentWindow;
            const node=w.document.querySelector('.page[data-page-number="1"] .textLayer span').firstChild;
            const r=w.document.createRange();r.setStart(node,0);r.setEnd(node,8);
            w.getSelection().removeAllRanges();w.getSelection().addRange(r);});
          await page.getByRole('button',{name:'加入 AI',exact:true}).waitFor();
        }""",
        )
        browser.snapshot("selected-first-word")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'加入 AI',exact:true}).click();await page.locator('[data-source-snapshot]').waitFor();}",
        )
        saved = api(browser, f"/chats/{before}/draft")["body"]
        ids = saved.get("source_snapshot_ids", [])
        check(
            len(ids) == 1, "pdf_selection_creates_one_source_card_in_same_draft", result
        )
        source = api(browser, f"/courses/{course_id}/learning/selections/{ids[0]}")[
            "body"
        ]
        check(
            source["text"] == "Learning"
            and source["reference"]["parts"][0]["page_index"] == 0
            and source["reference"]["source_version"] in state["source"],
            "selection_preserves_only_exact_word_and_versioned_page_geometry",
            result,
            snapshot=source,
        )
        check(
            instance.counts()["local_messages"] == 0,
            "adding_source_to_ai_draft_never_sends_surrounding_page_or_model_request",
            result,
        )
        instance.control("scenario", scenario={"text_chunks": ["页"]})
        browser.snapshot("before-translation-selection")
        browser.command(
            "run-code",
            """async(page)=>{
          await page.evaluate(()=>{const w=document.querySelector('iframe[data-learning-pdf-reader]').contentWindow;
            const node=w.document.querySelector('.page[data-page-number="1"] .textLayer span').firstChild;
            const start=node.textContent.indexOf('page');const r=w.document.createRange();r.setStart(node,start);r.setEnd(node,start+4);
            w.getSelection().removeAllRanges();w.getSelection().addRange(r);});
          await page.getByRole('button',{name:'翻译',exact:true}).waitFor();
        }""",
        )
        browser.snapshot("before-translate-action")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'翻译',exact:true}).click();await page.getByText('译文已保存到当前课程',{exact:true}).waitFor({timeout:25000});}",
        )
        tasks = api(browser, f"/courses/{course_id}/learning/tasks")["body"]
        check(
            len(tasks) == 1
            and tasks[0]["state"] == "completed"
            and tasks[0]["chat_id"] == before,
            "explicit_translation_completes_as_task_belonging_to_original_chat",
            result,
        )
        artifact_id = tasks[0]["units"][0]["result_artifact_id"]
        artifact = api(
            browser, f"/courses/{course_id}/learning/artifacts/{artifact_id}"
        )["body"]
        check(
            artifact["kind"] == "translation"
            and artifact["content_role"] == "ai_generated"
            and artifact["indexed"]
            and artifact["revision"]["body"]["blocks"][0]["markdown"] == "页",
            "translation_is_saved_and_indexed_as_independent_course_material",
            result,
        )
        raw_files = list(
            (directory / "server/execution/boundary/private").rglob("*.request.json")
        )
        check(
            len(raw_files) == 1,
            "one_translation_action_has_one_fixture_attempt",
            result,
        )
        request = json.loads(raw_files[0].read_text("utf-8"))
        prompt = "\n".join(
            block.get("text", "")
            for message in request["messages"]
            for block in message["content"]
            if isinstance(block, dict)
        )
        check(
            '"selected_text": "page"' in prompt
            and "immutable synthetic source" not in prompt
            and ids[0] not in prompt
            and not request.get("tools"),
            "translation_excludes_neighboring_text_draft_quote_and_tool_reads",
            result,
        )
        check(
            len(api(browser, f"/courses/{course_id}/chats")["body"]) == 1
            and not api(browser, f"/chats/{before}")["body"]["messages"]
            and api(browser, f"/chats/{before}/draft")["body"]["source_snapshot_ids"]
            == ids,
            "translation_preserves_native_chat_and_unsent_quote",
            result,
        )
        original_run = api(
            browser,
            f"/courses/{course_id}/learning/tasks/{tasks[0]['task_id']}/runs/{tasks[0]['units'][0]['run_id']}",
        )["body"]
        browser.command(
            "run-code",
            """async(page)=>{
          await page.evaluate(()=>{const w=document.querySelector('iframe[data-learning-pdf-reader]').contentWindow;
            const node=w.document.querySelector('.page[data-page-number="1"] .textLayer span').firstChild;
            const start=node.textContent.indexOf('page');const range=w.document.createRange();range.setStart(node,start);range.setEnd(node,start+4);
            w.getSelection().removeAllRanges();w.getSelection().addRange(range);});
          await page.getByRole('button',{name:'翻译',exact:true}).click();
          await page.getByText('沿用已保存的翻译，本次没有新增模型请求。原执行用量保留在原任务中。',{exact:true}).waitFor({timeout:15000});
        }""",
        )
        reused = api(browser, f"/courses/{course_id}/learning/tasks")["body"]
        check(
            len(reused) == 1
            and reused[0]["task_id"] == tasks[0]["task_id"]
            and len(
                list(
                    (directory / "server/execution/boundary/private").rglob(
                        "*.request.json"
                    )
                )
            )
            == 1,
            "same_selection_uses_original_lru_and_saved_translation_without_another_model_request",
            result,
        )
        check(
            api(
                browser,
                f"/courses/{course_id}/learning/tasks/{tasks[0]['task_id']}/runs/{tasks[0]['units'][0]['run_id']}",
            )["body"]
            == original_run,
            "cache_reuse_preserves_original_raw_usage_without_cloning_or_zeroing_it",
            result,
        )
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'cached-translation.png'}",
            "--full-page",
        )
        instance.control("scenario", scenario={"text_chunks": ["页（重新生成）"]})
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'重新生成译文',exact:true}).click();await page.getByText('页（重新生成）',{exact:true}).waitFor({timeout:25000});}",
        )
        check(
            len(api(browser, f"/courses/{course_id}/learning/tasks")["body"]) == 2
            and len(
                list(
                    (directory / "server/execution/boundary/private").rglob(
                        "*.request.json"
                    )
                )
            )
            == 2,
            "explicit_regeneration_bypasses_cache_without_overwriting_the_original_translation",
            result,
        )
        browser.snapshot("before-source-jump")
        browser.command(
            "run-code",
            """async(page)=>{
          await page.frameLocator('iframe[data-learning-pdf-reader]').locator('#pageNumber').fill('5');
          await page.frameLocator('iframe[data-learning-pdf-reader]').locator('#pageNumber').press('Enter');
          await page.getByRole('button',{name:'回看原文',exact:true}).click();
          await page.frameLocator('iframe[data-learning-pdf-reader]').locator('[aria-label="原文选区"]').waitFor();
        }""",
        )
        jump_page = browser.evaluate(
            "()=>document.querySelector('iframe[data-learning-pdf-reader]').contentWindow.PDFViewerApplication.pdfViewer.currentPageNumber"
        )
        check(
            jump_page == 1,
            "saved_reference_jumps_to_original_version_page_and_highlights_region",
            result,
        )
        browser.snapshot("before-reader-capture-shortcut")
        messages_before_capture = instance.counts()["local_messages"]
        browser.command(
            "run-code",
            """async(page)=>{
          await page.frameLocator('iframe[data-learning-pdf-reader]').locator('#viewerContainer').press('Control+Shift+S');
          await page.getByRole('dialog',{name:'框选软件内截图',exact:true}).waitFor();
          await page.waitForFunction(()=>{const image=document.querySelector('.workspace-capture-frozen');return image?.complete&&image.naturalWidth>0;},null,{timeout:20000});
        }""",
        )
        frozen = browser.evaluate("""()=>{
          const image=document.querySelector('.workspace-capture-frozen');
          const frame=document.querySelector('iframe[data-learning-pdf-reader]');
          const rendered=frame.contentDocument.querySelector('.page[data-page-number="1"] canvas');
          const canvas=document.createElement('canvas');canvas.width=image.naturalWidth;canvas.height=image.naturalHeight;
          const context=canvas.getContext('2d');context.drawImage(image,0,0);
          const surface=document.querySelector('.workspace-capture-surface').getBoundingClientRect();
          const f=frame.getBoundingClientRect(), p=rendered.getBoundingClientRect();
          const ratio=image.naturalWidth/surface.width;
          const x=Math.max(0,Math.ceil((f.left+p.left-surface.left)*ratio));
          const y=Math.max(0,Math.ceil((f.top+p.top-surface.top)*ratio));
          const width=Math.min(canvas.width-x,Math.floor(p.width*ratio));
          const height=Math.min(canvas.height-y,Math.floor(p.height*ratio),240);
          const pixels=context.getImageData(x,y,width,height).data;
          let dark=0;for(let i=0;i<pixels.length;i+=4)if(pixels[i]<100&&pixels[i+1]<100&&pixels[i+2]<100)dark++;
          return {local:image.currentSrc.startsWith('data:image/'),dark,width,height};
        }""")
        check(
            frozen["local"] and frozen["dark"] > 30,
            "reader_capture_contains_only_visible_pdf_canvas_pixels_locally",
            result,
            sample=frozen,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'取消截图',exact:true}).click();}",
        )
        check(
            instance.counts()["local_messages"] == messages_before_capture
            and not api(browser, f"/chats/{before}/draft")["body"]["attachment_ids"],
            "cancelled_pdf_capture_does_not_submit_original_page_or_add_attachment",
            result,
        )
        browser.snapshot("reading-complete")
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'reading.png'}",
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
    parser.add_argument("--backend-port", type=int, default=19230)
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
