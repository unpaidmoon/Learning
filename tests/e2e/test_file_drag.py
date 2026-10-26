"""File-manager drag routing in the pinned Harness; local synthetic data only."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import time

from PIL import Image

from test_browser import Browser, ROOT, check, wait_until, write_json
from test_desktop import DesktopInstance
from test_mvp_a import api


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="file-drag-" + time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--scope", choices=["full", "smoke"], default="full")
    parser.add_argument("--browser-path", type=Path, default=Path(os.environ["LOCALAPPDATA"]) / "ms-playwright/chromium-1234/chrome-win64/chrome.exe")
    args = parser.parse_args()
    if not args.run_id.replace("-", "").isalnum():
        parser.error("safe run id required")
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    files = directory / "synthetic"
    files.mkdir()
    (files / "目标目录").mkdir()
    (files / "移动.txt").write_text("内部移动样本", encoding="utf-8")
    (files / "附件.txt").write_text("仅供主动加入草稿的完整文字", encoding="utf-8")
    (files / "复制.txt").write_text("Ctrl 复制样本", encoding="utf-8")
    (files / "笔记.md").write_text("# Synthetic\n\n$x^2+1$", encoding="utf-8")
    Image.new("RGB", (64, 48), "blue").save(files / "题图.png")
    spec = importlib.util.spec_from_file_location("file_drag_assets", ROOT / "tests/fixtures/workspace_v2/generate.py")
    assets = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(assets)
    assets.text_pdf(files / "讲义.pdf")
    original_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files.iterdir() if path.is_file()}
    instance = DesktopInstance(directory / "server", free_port())
    browser = Browser(directory / "browser", args.browser_path)
    result = {"status": "failed", "layer": "pinned Harness + Chromium + synthetic local files", "real_model_calls": 0}
    try:
        instance.start()
        browser.command("open", instance.url + "/desktop/", "--config=" + str(browser.config))
        browser.command("run-code", "async page=>{await page.locator('[data-composer-input]').waitFor();}")
        created = api(browser, "/courses/folder", {"path": str(files.resolve())}, method="POST")
        assert created["status"] == 200, created
        browser.command("reload")
        browser.snapshot("before-workspace")
        browser.command("run-code", "async page=>{await page.getByRole('list',{name:'工作区列表'}).getByRole('button',{name:'synthetic',exact:true}).click();await page.getByRole('option',{name:'移动.txt',exact:true}).waitFor();}")
        browser.evaluate("()=>{window.__fileDragEvents=[];for(const name of ['dragstart','dragenter','dragover','drop'])document.addEventListener(name,e=>{if(window.__fileDragEvents.length<40)window.__fileDragEvents.push({name,types:[...(e.dataTransfer?.types||[])],target:e.target.outerHTML?.slice(0,220)});},true);return true;}")
        browser.snapshot("before-internal-drag")
        browser.command("run-code", "async page=>{const source=await page.getByRole('option',{name:'移动.txt',exact:true}).boundingBox(),target=await page.getByRole('option',{name:'目标目录',exact:true}).boundingBox();await page.mouse.move(source.x+source.width/2,source.y+source.height/2);await page.mouse.down();await page.mouse.move(source.x+source.width/2+12,source.y+source.height/2+12,{steps:4});await page.mouse.move(target.x+target.width/2,target.y+target.height/2,{steps:8});}")
        mask = browser.evaluate("()=>({mask:!!document.querySelector('.file-manager-drop-hint'),folder:!!document.querySelector('.file-manager-entry.is-drop-target')})")
        browser.command("run-code", "async page=>{await page.mouse.up();}")
        observation = browser.evaluate("()=>({events:window.__fileDragEvents,statuses:[...document.querySelectorAll('[role=status]')].map(e=>e.textContent)})")
        write_json(directory / "drag-observation.json", observation)
        check((files / "目标目录/移动.txt").is_file() and not (files / "移动.txt").exists(), "internal_drag_moves_into_folder", result, observed=observation)
        check(not mask["mask"] and mask["folder"], "internal_drag_highlights_folder_without_import_mask", result, observed=mask)
        browser.snapshot("before-copy-drag")
        browser.command("run-code", "async page=>{await page.keyboard.down('Control');try{await page.getByRole('option',{name:'复制.txt',exact:true}).dragTo(page.getByRole('option',{name:'目标目录',exact:true}));}finally{await page.keyboard.up('Control');}}")
        check((files / "复制.txt").read_bytes() == (files / "目标目录/复制.txt").read_bytes(), "ctrl_drag_copies_and_preserves_original", result)
        with sqlite3.connect(instance.directory / "data/business.sqlite3") as connection:
            chat_id = connection.execute("SELECT chat_id FROM chats").fetchone()[0]
        browser.snapshot("before-composer-drop")
        browser.command("run-code", "async page=>{await page.locator('[data-composer-input]').fill('保留的问题');await page.getByRole('option',{name:'附件.txt',exact:true}).dragTo(page.locator('[data-composer-input]'));}")
        wait_until(lambda: len(api(browser, f"/chats/{chat_id}/draft")["body"]["attachment_ids"]) == 1, timeout=20)
        draft = api(browser, f"/chats/{chat_id}/draft")["body"]
        check(len(draft["attachment_ids"]) == 1 and "保留的问题" in draft["text"], "file_drag_attaches_to_draft_and_preserves_text", result, observed=draft)
        if args.scope == "smoke":
            result["status"] = "passed"
            return 0
        browser.snapshot("before-multiple-attachments")
        browser.command("run-code", "async page=>{await page.getByRole('option',{name:'笔记.md',exact:true}).click();await page.getByRole('option',{name:'题图.png',exact:true}).click({modifiers:['Control']});await page.getByRole('option',{name:'讲义.pdf',exact:true}).click({modifiers:['Control']});await page.getByRole('option',{name:'笔记.md',exact:true}).dragTo(page.locator('[data-composer-input]'));}")
        wait_until(lambda: len(api(browser, f"/chats/{chat_id}/draft")["body"]["attachment_ids"]) == 4, timeout=30)
        draft = api(browser, f"/chats/{chat_id}/draft")["body"]
        metadata = [api(browser, f"/attachments/{aid}/metadata")["body"] for aid in draft["attachment_ids"]]
        check({item["filename"] for item in metadata if item["kind"] != "image"} == {"附件.txt", "笔记.md", "讲义.pdf"} and sum(item["kind"] == "image" for item in metadata) == 1, "multiselect_drag_adds_markdown_png_pdf_in_one_gesture", result)
        check(all(hashlib.sha256((files / name).read_bytes()).hexdigest() == digest for name, digest in original_hashes.items() if name != "移动.txt"), "attaching_preserves_all_source_files", result)
        check(all(item["sha256"] == original_hashes["题图.png" if item["kind"] == "image" else item["filename"]] for item in metadata), "attachments_match_original_bytes", result)
        with sqlite3.connect(instance.directory / "data/business.sqlite3") as connection:
            before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("chats", "messages", "generations", "chat_workspaces", "document_attachments")}
        check(before["messages"] == 0 and before["generations"] == 0 and before["chat_workspaces"] == 0, "dropping_from_browsed_workspace_does_not_send_or_expand_read_scope", result, observed=before)
        browser.command("reload")
        browser.command("run-code", "async page=>{await page.locator('[aria-label=待发送附件]').waitFor();}")
        restored = api(browser, f"/chats/{chat_id}/draft")["body"]
        with sqlite3.connect(instance.directory / "data/business.sqlite3") as connection:
            count = connection.execute("SELECT COUNT(*) FROM document_attachments").fetchone()[0]
        check(restored["attachment_ids"] == draft["attachment_ids"] and restored["text"] == draft["text"] and count == before["document_attachments"], "reload_restores_same_draft_without_duplicate_document_preparation", result)
        browser.snapshot("before-navigation-during-drop")
        browser.command("run-code", "async page=>{await page.getByRole('list',{name:'工作区列表'}).getByRole('button',{name:'synthetic',exact:true}).click();await page.route('**/api/v1/courses/*/files/open',async route=>{await page.evaluate(()=>{window.__fileDropWaiting=true;});await page.waitForFunction(()=>window.__releaseFileDrop===true);await route.continue();});await page.getByRole('option',{name:'复制.txt',exact:true}).dragTo(page.locator('[data-composer-input]'));await page.waitForFunction(()=>window.__fileDropWaiting===true);await page.getByRole('button',{name:'新会话',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('[data-composer-input]').textContent);await page.evaluate(()=>{window.__releaseFileDrop=true;});await page.getByText(/FILE_ATTACHMENT_CONTEXT_CHANGED/).waitFor();await page.unroute('**/api/v1/courses/*/files/open');}")
        with sqlite3.connect(instance.directory / "data/business.sqlite3") as connection:
            other_chat = connection.execute("SELECT chat_id FROM chats WHERE chat_id<>?", (chat_id,)).fetchone()[0]
        check(api(browser, f"/chats/{other_chat}/draft")["body"]["attachment_ids"] == [] and api(browser, f"/chats/{chat_id}/draft")["body"]["attachment_ids"] == draft["attachment_ids"], "late_file_read_cannot_attach_after_switching_chat", result)
        browser.snapshot("before-unsupported-drop")
        browser.command("run-code", "async page=>{await page.getByRole('list',{name:'工作区列表'}).getByRole('button',{name:'synthetic',exact:true}).click();await page.getByRole('option',{name:'目标目录',exact:true}).dragTo(page.locator('[data-composer-input]'));await page.getByText(/FILE_ATTACHMENT_UNSUPPORTED/).waitFor();}")
        check(api(browser, f"/chats/{other_chat}/draft")["body"]["attachment_ids"] == [], "folder_drop_reports_error_and_preserves_draft", result)
        result["status"] = "passed"
        return 0
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        result["cleanup"] = {"browser": browser.close(), "server": instance.stop()}
        write_json(directory / "result.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
