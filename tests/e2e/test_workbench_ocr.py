"""Installed local OCR -> pinned Viewer -> exact source card; synthetic originals."""

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
    specification = importlib.util.spec_from_file_location(
        "ocr_fixture", ROOT / "tests/fixtures/workbench/generate.py"
    )
    generator = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(generator)
    material = directory / "synthetic-course"
    material.mkdir()
    generator.scanned_pdf(material / "scan-12.pdf", 12)
    (material / "migration-read.txt").write_text(
        "Synthetic course setup only.", "utf-8"
    )
    original = hashlib.sha256((material / "scan-12.pdf").read_bytes()).hexdigest()
    result = {
        "id": "workbench-ocr",
        "status": "running",
        "assertions": [],
        "actual_model_attempts": 0,
        "layer": "real Chromium + pinned Harness + installed OCR + PDF.js",
        "source_digests_before": digests(),
        "limits": [
            "Synthetic, clearly drawn bitmap; no user scan accuracy or human experience claim."
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
        browser.snapshot("before-open-scan")
        manager_entry(browser, "scan-12.pdf", double=True)
        browser.command(
            "run-code",
            """async(page)=>{
          await page.frameLocator('iframe[data-learning-pdf-reader]').locator('.page[data-page-number="1"] .textLayer [data-ocr-item]').first().waitFor({timeout:30000});
          await page.waitForFunction(()=>{const w=document.querySelector('iframe[data-learning-pdf-reader]').contentWindow;return w.document.querySelector('.page[data-page-number="1"] .textLayer').textContent.includes('LEARNING WORKBENCH')});
        }""",
        )
        browser.snapshot("first-ocr-page")
        canvas = browser.evaluate(
            """()=>{const w=document.querySelector('iframe[data-learning-pdf-reader]').contentWindow;const p=w.PDFViewerApplication.pdfViewer.getPageView(0);w.__learningFixtureCanvas=p.canvas;return {rendered:!!p.canvas,raw:w.document.querySelector('.page[data-page-number="1"] .textLayer').textContent};}"""
        )
        check(
            canvas["rendered"] and "LEARNING WORKBENCH" in canvas["raw"],
            "automatic_current_page_gets_real_selectable_ocr_layer",
            result,
            observed=canvas,
        )
        browser.snapshot("before-real-glyph-drag")
        browser.command(
            "run-code",
            """async(page)=>{
          const box=await page.evaluate(()=>{const f=document.querySelector('iframe[data-learning-pdf-reader]'),w=f.contentWindow;
            const spans=[...w.document.querySelectorAll('.page[data-page-number="1"] .textLayer [data-ocr-item]')];
            const first=spans.findIndex(s=>s.textContent==='L'),a=spans[first].getBoundingClientRect(),b=spans[first+7].getBoundingClientRect(),o=f.getBoundingClientRect();
            return {x:o.left+a.left+.1,y:o.top+(a.top+a.bottom)/2,x2:o.left+b.right-.1,y2:o.top+(b.top+b.bottom)/2};});
          await page.mouse.move(box.x,box.y);await page.mouse.down();await page.mouse.move(box.x2,box.y2,{steps:20});await page.mouse.up();
          await page.getByRole('button',{name:'加入 AI',exact:true}).waitFor();
        }""",
        )
        selected = browser.evaluate(
            "()=>document.querySelector('iframe[data-learning-pdf-reader]').contentWindow.getSelection().toString()"
        )
        check(
            selected == "LEARNING",
            "real_mouse_selects_exact_ocr_characters",
            result,
            observed=selected,
        )
        # Observe another background unit committing while the current DOM
        # selection stays alive. Read-only polling is not an OCR/model trigger.
        browser.command(
            "run-code",
            f"""async(page)=>{{await page.waitForFunction(async()=>{{const r=await fetch('/api/v1/courses/{cid}/learning/preparations');const j=await r.json();return j[0]?.units.filter(u=>u.state==='completed').length>=3;}},null,{{timeout:30000}});}}""",
        )
        held = browser.evaluate(
            "()=>{const w=document.querySelector('iframe[data-learning-pdf-reader]').contentWindow;return {text:w.getSelection().toString(),canvas:w.__learningFixtureCanvas===w.PDFViewerApplication.pdfViewer.getPageView(0).canvas};}"
        )
        check(
            held["text"] == "LEARNING" and held["canvas"],
            "background_results_preserve_active_selection_and_original_canvas",
            result,
            observed=held,
        )
        browser.command(
            "run-code",
            """async(page)=>{await page.context().grantPermissions(['clipboard-read','clipboard-write']);await page.keyboard.press('Control+C');}""",
        )
        copied = browser.evaluate("()=>navigator.clipboard.readText()")
        check(
            copied == "LEARNING",
            "real_copy_preserves_raw_ocr_selection",
            result,
            observed=copied,
        )
        browser.snapshot("before-pause-batch")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'暂停整份',exact:true}).dispatchEvent('click');await page.getByText(/整份已暂停/).waitFor();}",
        )
        jobs = api(browser, f"/courses/{cid}/learning/preparations")["body"]
        batch = next(job for job in jobs if job["scope"] == "full")
        frozen = sum(unit["state"] == "completed" for unit in batch["units"])
        check(
            batch["state"] == "paused" and frozen < 12,
            "visible_pause_persists_incomplete_full_document_checkpoint",
            result,
            completed=frozen,
        )
        geometry = browser.evaluate(
            """()=>{const w=document.querySelector('iframe[data-learning-pdf-reader]').contentWindow,a=w.PDFViewerApplication,p=a.pdfViewer.getPageView(0),r=w.getSelection().getRangeAt(0);const rect=b=>({left:b.left,top:b.top,width:b.width,height:b.height,right:b.right,bottom:b.bottom});return {start:r.startContainer.textContent,startOffset:r.startOffset,end:r.endContainer.textContent,endOffset:r.endOffset,rects:[...r.getClientRects()].map(rect),canvas:rect(p.canvas.getBoundingClientRect()),wrapper:rect(p.div.querySelector('.canvasWrapper').getBoundingClientRect()),viewBox:p.viewport.viewBox,width:p.viewport.width,height:p.viewport.height,spans:[...p.div.querySelectorAll('[data-ocr-item]')].slice(0,20).map(e=>({text:e.textContent,box:rect(e.getBoundingClientRect()),html:e.outerHTML}))};}"""
        )
        write_json(directory / "selection-geometry.json", geometry)
        browser.snapshot("before-source-card")
        browser.command(
            "run-code",
            """async(page)=>{const pending=page.waitForResponse(r=>r.url().endsWith('/learning/selections/pdf'));await page.getByRole('button',{name:'加入 AI',exact:true}).click();const r=await pending;const value={request:r.request().postDataJSON(),status:r.status(),body:await r.json()};await page.evaluate(v=>window.__fixtureSelection=v,value);}""",
        )
        receipt = browser.evaluate("()=>window.__fixtureSelection")
        write_json(directory / "selection-request.json", receipt)
        check(
            receipt["status"] == 200,
            "real_glyph_geometry_is_accepted_by_server_selection_guard",
            result,
            receipt=receipt,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.locator('[data-source-snapshot]').waitFor();}",
        )
        draft = api(browser, f"/chats/{mapping['chat_id']}/draft")["body"]
        quote = api(
            browser,
            f"/courses/{cid}/learning/selections/{draft['source_snapshot_ids'][0]}",
        )["body"]
        check(
            quote["text"] == "LEARNING"
            and quote["reference"]["parts"][0]["page_index"] == 0
            and quote["reference"]["parts"][0]["parser_version"].startswith("ocr:"),
            "source_card_uses_saved_ocr_version_and_exact_word_only",
            result,
            snapshot=quote,
        )
        browser.snapshot("before-prioritize-last-page")
        browser.command(
            "run-code",
            """async(page)=>{const f=page.frameLocator('iframe[data-learning-pdf-reader]');await f.locator('#pageNumber').fill('12');await f.locator('#pageNumber').press('Enter');await f.locator('.page[data-page-number="12"] .textLayer [data-ocr-item]').first().waitFor({timeout:30000});}""",
        )
        jobs = api(browser, f"/courses/{cid}/learning/preparations")["body"]
        batch = next(job for job in jobs if job["scope"] == "full")
        single = next(job for job in jobs if job["scope"] == "unit")
        check(
            batch["state"] == "paused"
            and single["state"] == "completed"
            and single["units"][0]["unit_index"] == 11,
            "new_current_page_finishes_without_reviving_manual_batch_pause",
            result,
        )
        browser.snapshot("before-resume-batch")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'恢复整份',exact:true}).click();await page.getByRole('button',{name:'暂停整份',exact:true}).waitFor();}",
        )
        browser.snapshot("before-live-find")
        browser.command(
            "run-code",
            """async(page)=>{const f=page.frameLocator('iframe[data-learning-pdf-reader]');await f.locator('#viewFindButton').click();await f.locator('#findInput').fill('WORKBENCH');await page.waitForFunction(()=>{const a=document.querySelector('iframe[data-learning-pdf-reader]').contentWindow.PDFViewerApplication;return a.findController.pageMatches.filter(x=>x?.length).length===12;},null,{timeout:30000});}""",
        )
        matches = browser.evaluate(
            "()=>document.querySelector('iframe[data-learning-pdf-reader]').contentWindow.PDFViewerApplication.findController.pageMatches.map(x=>x?.length||0)"
        )
        check(
            len(matches) == 12 and all(value == 1 for value in matches),
            "ongoing_native_find_refreshes_as_all_saved_ocr_pages_arrive",
            result,
            matches=matches,
        )
        browser.snapshot("before-rotate-and-zoom")
        browser.command(
            "run-code",
            """async(page)=>{const f=page.frameLocator('iframe[data-learning-pdf-reader]');await f.locator('#scaleSelect').selectOption('page-fit');await f.locator('#secondaryToolbarToggleButton').click();await f.locator('#pageRotateCw').click();await f.locator('.page[data-page-number="12"] .textLayer [data-ocr-item]').first().waitFor();}""",
        )
        rotated = browser.evaluate(
            """()=>{const w=document.querySelector('iframe[data-learning-pdf-reader]').contentWindow,a=w.PDFViewerApplication;return {rotation:a.pdfViewer.pagesRotation,scale:a.pdfViewer.currentScaleValue,layer:w.document.querySelector('.page[data-page-number="12"] .textLayer').textContent};}"""
        )
        check(
            rotated["rotation"] == 90
            and rotated["scale"] == "page-fit"
            and "LEARNING WORKBENCH" in rotated["layer"],
            "original_viewer_redraw_preserves_ocr_layer_after_rotation_and_zoom",
            result,
        )
        check(
            instance.counts()["local_messages"]
            == instance.counts()["real_provider_attempts"]
            == 0,
            "all_ocr_reading_search_copy_and_queue_actions_have_zero_provider_attempts",
            result,
        )
        check(
            hashlib.sha256((material / "scan-12.pdf").read_bytes()).hexdigest()
            == original,
            "original_scanned_pdf_bytes_are_unchanged",
            result,
        )
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'ocr-reading.png'}",
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
    parser.add_argument("--backend-port", type=int, default=19231)
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
