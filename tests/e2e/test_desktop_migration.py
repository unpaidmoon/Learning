"""Migrated learning features in the real pinned Harness client.

Only generated files, the formal application, its real SEA Host, and the local
provider fixture are used. Folder/clipboard choices in this runner are explicit
synthetic seams; foreground ownership has a separate real Windows acceptance.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

from test_browser import Browser, PYTHON, ROOT, check, http, port_closed, wait_until, write_json
from test_desktop import (
    DesktopInstance,
    composer_fill,
    press_button,
    serve_fixture as serve_desktop_fixture,
    source_digests as desktop_digests,
)
from test_mvp_a import api
from test_workspace_v2 import manager_click, manager_entry, mutate, wait_file


class MigrationInstance(DesktopInstance):
    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.native_selection()
        self.log = (self.directory / "server.stdout.txt").open("w", encoding="utf-8")
        self.process = self.group.spawn(
            [str(PYTHON), str(Path(__file__).resolve()), "--serve-fixture", str(self.directory),
             "--backend-port", str(self.backend)],
            cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            stdout=self.log, stderr=subprocess.STDOUT,
        )

        def ready():
            if self.process.poll() is not None:
                raise RuntimeError("DESKTOP_MIGRATION_FIXTURE_FAILED: see server.stdout.txt")
            try:
                status, _, value = http(self.url + "/api/v1/health")
                return status == 200 and value["capabilities"]["question_answering"] == "ready"
            except OSError:
                return False

        wait_until(ready, timeout=45)
        return self

    def native_selection(self, *, folder: str | None = None, clipboard: list[str] | None = None):
        write_json(self.directory / "synthetic-native-input.json", {
            "folder": folder, "clipboard": clipboard or [],
        })


async def serve_fixture(directory: Path, port: int) -> int:
    import learning_app.course_files as files

    def chosen(*args, **kwargs):
        value = json.loads((directory / "synthetic-native-input.json").read_text("utf-8"))
        return [value["folder"]] if value.get("folder") else []

    def clipboard():
        from learning_app.errors import AppError

        paths = json.loads((directory / "synthetic-native-input.json").read_text("utf-8"))["clipboard"]
        if not paths:
            raise AppError("CLIPBOARD_FILES_EMPTY", "合成剪贴板没有文件", 422)
        return paths

    files.choose_paths = chosen
    files.read_clipboard_paths = clipboard
    return await serve_desktop_fixture(directory, port)


def source_digests() -> dict[str, str]:
    result = desktop_digests()
    for folder in ("frontend/src/library", "frontend/src/media", "frontend/src/chat"):
        for path in (ROOT / folder).rglob("*"):
            if path.is_file():
                result[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    result[Path(__file__).relative_to(ROOT).as_posix()] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    for path in (ROOT / ".runtime/desktop/client").rglob("*"):
        if path.is_file():
            result[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("pnpm-lock.yaml", "backend/uv.lock", "scripts/desktop/build-learning.mjs",
                 "scripts/desktop/build-video.mjs"):
        path = ROOT / name
        if path.is_file():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def synthetic_assets(directory: Path, ffmpeg: Path) -> dict:
    specification = importlib.util.spec_from_file_location(
        "desktop_migration_assets", ROOT / "tests/fixtures/workspace_v2/generate.py"
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("SYNTHETIC_GENERATOR_MISSING")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    manifest = module.generate(directory, ffmpeg=ffmpeg)
    root = Path(manifest["course_root"])
    (root / "migration-read.txt").write_text(
        "MIGRATED_TEXT_START_1729\n普通消息可以补充条件，也可以纠正前文。\nMIGRATED_TEXT_END_2718\n",
        encoding="utf-8",
    )
    (root / "migration-math.md").write_text(
        "# 合成迁移资料\n\n已知 $x^2-5x+6=0$。\n\nMIGRATED_MARKDOWN_END_3141\n",
        encoding="utf-8",
    )
    manifest["migration_files"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / "migration-read.txt", root / "migration-math.md")
    }
    return manifest


def click_dialog(browser: Browser, name: str) -> None:
    browser.snapshot("before-dialog-action")
    browser.command("run-code", "async(page)=>{await page.getByRole('dialog').last()"
                    ".getByRole('button',{name:" + json.dumps(name) + ",exact:true}).click();}")


def fill(browser: Browser, label: str, text: str) -> None:
    browser.snapshot("before-fill-field")
    browser.command("run-code", "async(page)=>{await page.getByRole('textbox',{name:"
                    + json.dumps(label) + ",exact:true}).fill(" + json.dumps(text) + ");}")


def files_tab(browser: Browser) -> None:
    browser.snapshot("before-files-tab")
    browser.command("run-code", "async(page)=>{await page.getByRole('tab',{name:'文件',exact:true}).click();"
                    "await page.getByRole('region',{name:'文件资源管理器',exact:true}).waitFor({state:'visible'});}")


def native_mapping(browser: Browser, course_id: str) -> dict:
    session_id = browser.evaluate("()=>document.querySelector('[data-testid=learning-materials]')?.dataset.sessionId"
                                  "||document.querySelector('[data-testid=learning-video]')?.dataset.sessionId")
    if session_id:
        return api(browser, "/desktop/sessions/" + session_id)["body"]
    # Opening an already initialized course is local and idempotent. It is a
    # diagnostic fallback, not a replacement for the user-triggered New Chat.
    return api(browser, "/desktop/courses/" + course_id + "/open", {}, method="POST")["body"]


def wait_api(browser: Browser, path: str, predicate: str, *, timeout_ms: int = 25000) -> dict:
    """Poll decoded responses serially; an async waitForFunction can resolve false.

    The installed browser runtime resolves a Promise predicate once rather than
    repolling its false result. Explicitly await every request and predicate so
    no unfinished/older request can satisfy or overwrite an observation.
    """
    route = path if path.startswith("/api/v1/") else "/api/v1" + path
    return browser.evaluate(
        "async()=>{const accept=value=>(" + predicate + ");const until=Date.now()+"
        + str(timeout_ms) + ";while(Date.now()<until){const response=await fetch("
        + json.dumps(route) + ",{cache:'no-store'});if(!response.ok)throw Error('API_WAIT_HTTP_'+response.status);"
        "const value=await response.json();if(accept(value))return value;"
        "await new Promise(resolve=>setTimeout(resolve,100));}throw Error('API_CONDITION_TIMEOUT')}"
    )


def wait_generation(browser: Browser, generation_id: str, state: str) -> dict:
    return wait_api(browser, "/generations/" + generation_id, "value.state===" + json.dumps(state))


def create_course(browser: Browser, instance: MigrationInstance, manifest: dict, result: dict) -> tuple[str, dict]:
    instance.native_selection(folder=manifest["course_root"])
    browser.command("run-code", "async(page)=>{await page.getByRole('dialog').last().waitFor({state:'visible'});}")
    click_dialog(browser, "创建课程")
    scene = browser.snapshot("create-course")
    check("课程文件夹路径" not in scene and 'button "选择课程文件夹"' in scene,
          "course_creation_uses_folder_chooser_and_independent_display_name", result,
          boundary="chooser result is a generated local fixture; real owner/foreground is separately tested")
    click_dialog(browser, "选择课程文件夹")
    fill(browser, "课程名称", "合成桌面功能迁移课程")
    click_dialog(browser, "创建课程")
    browser.command("run-code", "async(page)=>{await page.locator('[data-composer-input]')"
                    ".waitFor({state:'visible',timeout:30000});}")
    wait_file(browser, "migration-read.txt")
    courses = api(browser, "/courses")["body"]
    course = next(item for item in courses if item["title"] == "合成桌面功能迁移课程")
    mapping = native_mapping(browser, course["course_id"])
    check(len(api(browser, f"/courses/{course['course_id']}/chats")["body"]) == 1
          and instance.counts()["local_messages"] == 0,
          "course_initializes_one_business_chat_without_model_dispatch", result)
    return course["course_id"], mapping


def file_management(browser: Browser, instance: MigrationInstance, manifest: dict, course_id: str, result: dict):
    root = Path(manifest["course_root"])
    external = Path(manifest["external_imports"])
    files_tab(browser)
    names = browser.evaluate("()=>Array.from(document.querySelectorAll('.file-manager-entry')).map(e=>e.getAttribute('aria-label'))")
    check({"子目录", "migration-read.txt", "migration-math.md", "synthetic-frame.png"} <= set(names),
          "migrated_manager_lists_real_course_files_in_material_column", result)
    original = (root / "copy-collision.txt").read_bytes()
    copied = mutate(browser, f"/courses/{course_id}/files/import", {"paths": [str(external / "copy-collision.txt")]})
    check(copied["status"] == 200 and (root / "copy-collision.txt").read_bytes() == original
          and (root / "copy-collision (2).txt").read_bytes() == (external / "copy-collision.txt").read_bytes()
          and (external / "copy-collision.txt").is_file(),
          "external_copy_preserves_originals_and_numbers_name_collision", result)
    instance.native_selection(folder=manifest["course_root"], clipboard=[str(external / "clipboard-copy.txt")])
    manager_click(browser, "粘贴")
    wait_file(browser, "clipboard-copy.txt")
    check((external / "clipboard-copy.txt").is_file()
          and (root / "clipboard-copy.txt").read_bytes() == (external / "clipboard-copy.txt").read_bytes(),
          "manager_native_clipboard_paste_copies_without_moving_source", result,
          boundary="clipboard selection is a controlled synthetic seam; filesystem copy is real")
    instance.native_selection(folder=manifest["course_root"])
    manager_entry(browser, "internal-copy.txt")
    manager_click(browser, "复制")
    manager_entry(browser, "子目录", double=True)
    wait_file(browser, "nested-search.txt")
    manager_click(browser, "粘贴")
    wait_file(browser, "internal-copy.txt")
    check((root / "internal-copy.txt").is_file() and (root / "子目录/internal-copy.txt").is_file(),
          "manager_copy_paste_uses_actual_files_and_target_directory", result)
    manager_entry(browser, "子目录/internal-copy.txt")
    manager_click(browser, "剪切")
    manager_click(browser, "上一级")
    wait_file(browser, "internal-copy.txt")
    manager_click(browser, "粘贴")
    wait_file(browser, "internal-copy (2).txt")
    check(not (root / "子目录/internal-copy.txt").exists() and (root / "internal-copy (2).txt").is_file(),
          "manager_cut_paste_moves_selected_file_and_preserves_existing_target", result)
    manager_click(browser, "新建文件夹")
    fill(browser, "名称", "迁移验证目录")
    click_dialog(browser, "确定")
    wait_file(browser, "迁移验证目录")
    check((root / "迁移验证目录").is_dir(), "manager_new_folder_is_real", result)
    manager_entry(browser, "internal-copy (2).txt")
    manager_click(browser, "重命名")
    fill(browser, "名称", "migration-renamed.txt")
    click_dialog(browser, "确定")
    wait_file(browser, "migration-renamed.txt")
    check((root / "migration-renamed.txt").is_file() and not (root / "internal-copy (2).txt").exists(),
          "manager_rename_changes_real_file", result)
    manager_entry(browser, "migration-renamed.txt")
    manager_click(browser, "属性")
    properties = browser.snapshot("actual-file-properties")
    check("migration-renamed.txt" in properties and "修改时间" in properties,
          "manager_properties_display_real_file_metadata", result)
    click_dialog(browser, "关闭")
    fill(browser, "搜索当前文件夹", "migration-renamed")
    filtered = browser.evaluate("()=>Array.from(document.querySelectorAll('.file-manager-entry')).map(e=>e.getAttribute('aria-label'))")
    check(filtered == ["migration-renamed.txt"], "manager_search_filters_current_directory", result)
    fill(browser, "搜索当前文件夹", "")
    manager_entry(browser, "recycle-only.txt")
    manager_click(browser, "移入回收站")
    wait_until(lambda: not (root / "recycle-only.txt").exists(), timeout=15)
    check(not (root / "recycle-only.txt").exists(), "manager_delete_recycles_only_generated_test_file", result,
          boundary="real Windows recycle helper; no recycle bin enumeration, restoration or emptying")
    rejected = api(browser, f"/courses/{course_id}/files?path=..")
    check(rejected["status"] == 422, "migrated_browser_keeps_course_root_path_guard", result)


def reading(browser: Browser, result: dict):
    files_tab(browser)
    manager_entry(browser, "migration-read.txt", double=True)
    browser.command("run-code", "async(page)=>{await page.getByText('MIGRATED_TEXT_END_2718',"
                    "{exact:false}).last().waitFor({state:'visible'});}")
    check(browser.evaluate("()=>document.body.innerText.includes('普通消息可以补充条件，也可以纠正前文。')"),
          "material_text_reader_displays_full_generated_document", result)
    browser.snapshot("before-material-text-selection")
    browser.command("run-code", "async(page)=>{await page.evaluate(()=>{const root=document.querySelector('.learning-material-viewer');"
                    "const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);let node;"
                    "while((node=walker.nextNode())){const at=node.textContent.indexOf('MIGRATED_TEXT_START_1729');"
                    "if(at>=0){const range=document.createRange();range.setStart(node,at);"
                    "range.setEnd(node,at+'MIGRATED_TEXT_START_1729'.length);const s=window.getSelection();"
                    "s.removeAllRanges();s.addRange(range);root.querySelector('pre').dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));break;}}});"
                    "await page.getByRole('button',{name:'添加到对话',exact:true}).last().click();}")
    check(browser.evaluate("()=>document.querySelector('[data-composer-input]').textContent.includes('MIGRATED_TEXT_START_1729')"),
          "selected_material_text_can_be_added_to_native_question_without_model_dispatch", result)
    composer_fill(browser, "")
    files_tab(browser)
    manager_entry(browser, "migration-math.md", double=True)
    browser.command("run-code", "async(page)=>{await page.getByText('MIGRATED_MARKDOWN_END_3141',"
                    "{exact:false}).last().waitFor({state:'visible'});}")
    check(browser.evaluate("()=>!!document.querySelector('[data-testid=learning-materials] .katex')"),
          "material_Markdown_reader_renders_math_with_existing_KaTeX", result)
    files_tab(browser)
    manager_entry(browser, "synthetic-frame.png", double=True)
    browser.command("run-code", "async(page)=>{await page.waitForFunction(()=>{const image=document.querySelector('.document-image');"
                    "return image&&image.complete&&image.naturalWidth===640});}")
    image = browser.evaluate("()=>{const image=document.querySelector('.document-image');return {"
                             "width:image.naturalWidth,height:image.naturalHeight,url:new URL(image.currentSrc).pathname}}")
    check(image["width"] == 640 and image["url"].startswith("/api/v1/course-files/"),
          "material_image_reader_uses_version_bound_business_content", result, image=image)


def open_course_video(browser: Browser, name: str = "seek-preview.webm"):
    files_tab(browser)
    manager_entry(browser, name, double=True)
    browser.command("run-code", "async(page)=>{await page.waitForFunction(()=>{const v=Array.from(document.querySelectorAll('[data-primary-video]'))"
                    ".find(v=>v.getBoundingClientRect().width>0);return v&&v.readyState>=2&&v.videoWidth===640});}")


def preview(browser: Browser, course_id: str, result: dict):
    open_course_video(browser)
    browser.command("run-code", "async(page)=>{await page.locator('[data-primary-video]:visible').evaluate(v=>v.pause());}")
    browser.command("run-code", "async(page)=>{await page.getByRole('slider',{name:'视频播放进度',exact:true})"
                    ".waitFor({state:'visible'});}")
    manifest_path = browser.evaluate("()=>{const v=Array.from(document.querySelectorAll('[data-primary-video]')).find(v=>v.getBoundingClientRect().width>0);"
                                     "const url=new URL(v.currentSrc);"
                                     "return url.pathname.replace(/content$/,'previews')+url.search}")
    manifest = wait_api(browser, manifest_path, "value.status==='ready'&&value.frames?.length>=3", timeout_ms=30000)
    check(manifest["status"] == "ready" and manifest["width"] <= 320 and len(manifest["frames"]) >= 3,
          "registered_course_video_has_persisted_background_low_resolution_preview_index", result,
          frame_count=len(manifest["frames"]), interval_ms=manifest["interval_ms"])
    browser.snapshot("before-cached-preview-hover")
    browser.command("run-code", "async(page)=>{const slider=page.getByRole('slider',{name:'视频播放进度',exact:true});"
                    "const box=await slider.boundingBox();await page.evaluate(()=>window.__migrationHoverStarted=performance.now());"
                    "await page.mouse.move(box.x+box.width*.55,box.y+box.height/2);"
                    "await page.waitForFunction(()=>{const image=document.querySelector('[data-testid=video-preview-image]');"
                    "const panel=image?.closest('.seek-preview');return image&&image.complete&&image.naturalWidth>0"
                    "&&Math.abs(Number(panel.dataset.previewTime)-Number(panel.dataset.previewFrameTime))<=5},{timeout:20000});"
                    "await page.evaluate(()=>window.__migrationHoverElapsed=performance.now()-window.__migrationHoverStarted);}")
    data = browser.evaluate("()=>{const image=document.querySelector('[data-testid=video-preview-image]');return {"
                            "src:image.currentSrc,width:image.naturalWidth,height:image.naturalHeight,"
                            "text:image.closest('.seek-preview').innerText,hover_ms:window.__migrationHoverElapsed,"
                            "target_time:Number(image.closest('.seek-preview').dataset.previewTime),"
                            "frame_time:Number(image.closest('.seek-preview').dataset.previewFrameTime)}}")
    check("/previews/" in data["src"] and data["width"] <= 320 and data["height"] <= 240
          and "正在预览" not in data["text"],
          "seek_hover_displays_real_low_resolution_cached_image_without_loading_or_time_only_placeholder", result,
          observed=data)
    check(data["hover_ms"] < 1000, "warm_cached_seek_preview_is_visible_within_one_second", result,
          elapsed_ms=data["hover_ms"], boundary="this warm synthetic cache observation is not a universal video speed guarantee")
    check(abs(data["frame_time"] - data["target_time"]) <= 5,
          "cached_preview_is_a_nearby_frame_of_the_hover_time", result,
          target_time=data["target_time"], frame_time=data["frame_time"])
    browser.snapshot("before-continuous-preview-drag")
    browser.command("run-code", "async(page)=>{const slider=page.getByRole('slider',{name:'视频播放进度',exact:true});"
                    "await page.locator('[data-primary-video]:visible').evaluate(async v=>{await v.play();"
                    "window.__migrationSeek=0;v.addEventListener('seeking',()=>window.__migrationSeek++);});"
                    "const box=await slider.boundingBox();await page.mouse.move(box.x+box.width*.15,box.y+box.height/2);"
                    "await page.mouse.down();await page.mouse.move(box.x+box.width*.65,box.y+box.height/2,{steps:8});"
                    "await page.evaluate(()=>{const v=Array.from(document.querySelectorAll('[data-primary-video]')).find(v=>v.getBoundingClientRect().width>0);"
                    "window.__migrationDuringDrag={seeking:window.__migrationSeek,paused:v.paused,time:v.currentTime};});"
                    "await page.mouse.up();await page.waitForFunction(()=>window.__migrationSeek>=1);}")
    drag = browser.evaluate("()=>({during:window.__migrationDuringDrag,after:{seeking:window.__migrationSeek,"
                            "paused:Array.from(document.querySelectorAll('[data-primary-video]')).find(v=>v.getBoundingClientRect().width>0).paused}})")
    check(drag["during"]["seeking"] == 0 and not drag["during"]["paused"]
          and drag["after"]["seeking"] == 1 and not drag["after"]["paused"],
          "seek_drag_preserves_primary_playback_and_commits_one_seek_on_release", result, observed=drag)
    browser.command("run-code", "async(page)=>{await page.locator('[data-primary-video]:visible').evaluate(v=>v.pause());}")
    first_url = manifest["frames"][0]["url"]
    before_hash = browser.evaluate("async()=>{const b=await(await fetch(" + json.dumps(first_url)
                                   + ")).arrayBuffer();return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',b)))"
                                   ".map(x=>x.toString(16).padStart(2,'0')).join('')}")
    browser.command("reload")
    open_course_video(browser)
    browser.command("run-code", "async(page)=>{await page.locator('[data-primary-video]:visible').evaluate(v=>v.pause());}")
    after_hash = browser.evaluate("async()=>{const b=await(await fetch(" + json.dumps(first_url)
                                  + ")).arrayBuffer();return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',b)))"
                                  ".map(x=>x.toString(16).padStart(2,'0')).join('')}")
    check(before_hash == after_hash, "cached_preview_bytes_survive_native_UI_reload", result, sha256=before_hash)
    check(api(browser, f"/courses/{course_id}/chats")["status"] == 200,
          "cached_preview_keeps_business_and_chat_requests_responsive", result)


def native_upload(browser: Browser, paths: list[Path], *, new_chat_while_pending: bool = False):
    browser.snapshot("before-native-attachment-selection")
    browser.command("run-code", "async(page)=>{await page.locator('input[type=file]').first().setInputFiles("
                    + json.dumps([str(path) for path in paths]) + ");"
                    + ("await page.getByRole('button',{name:'新对话',exact:true}).click();" if new_chat_while_pending else "")
                    + "}")


def set_capture_rectangle(browser: Browser):
    browser.snapshot("before-capture-rectangle")
    browser.command("run-code", "async(page)=>{for(const [name,value] of Object.entries("
                    + json.dumps({"选区宽度": 320, "选区高度": 180, "选区左侧": 160, "选区顶部": 90})
                    + ")){await page.getByRole('spinbutton',{name,exact:true}).fill(String(value));}}")


def capture_and_reedit(browser: Browser, chat_id: str, text: str, result: dict):
    open_course_video(browser, "synthetic-course.webm")
    press_button(browser, "截图提问")
    set_capture_rectangle(browser)
    press_button(browser, "确认选区并加入草稿")
    wait_api(browser, f"/chats/{chat_id}/draft", "value.attachment_ids?.length===1", timeout_ms=20000)
    before = api(browser, f"/chats/{chat_id}/draft")["body"]["attachment_ids"][0]
    press_button(browser, "重新裁剪图片 1")
    set_capture_rectangle(browser)
    press_button(browser, "确认选区并加入草稿")
    wait_api(browser, f"/chats/{chat_id}/draft", "value.attachment_ids?.length===1&&value.attachment_ids[0]!=="
             + json.dumps(before), timeout_ms=20000)
    after = api(browser, f"/chats/{chat_id}/draft")["body"]
    source = api(browser, "/attachments/" + after["attachment_ids"][0] + "/source")["body"]
    check(len(after["attachment_ids"]) == 1 and after["attachment_ids"][0] != before
          and after["text"] == text and source["parent_attachment_id"] == before
          and source["origin"] == "video" and source["crop"]["width"] == 320
          and source["crop"]["height"] == 180 and api(browser, "/attachments/" + before + "/metadata")["status"] == 200,
          "screenshot_reedit_replaces_one_native_attachment_preserves_text_and_original_source_asset", result,
          crop=source["crop"])


def workspace_capture_cancel(browser: Browser, instance: MigrationInstance, result: dict):
    before = instance.counts()
    browser.snapshot("before-workspace-capture-shortcut")
    browser.command("run-code", "async(page)=>{await page.keyboard.press('Control+Shift+S');"
                    "await page.getByRole('dialog',{name:'框选软件内截图',exact:true}).waitFor({state:'visible'});"
                    "await page.waitForFunction(()=>{const img=document.querySelector('.workspace-capture-frozen');"
                    "return img&&img.complete&&img.naturalWidth>0},null,{timeout:20000});}")
    frozen = browser.evaluate("()=>{const image=document.querySelector('.workspace-capture-frozen');return {"
                              "width:image.naturalWidth,height:image.naturalHeight,local:image.currentSrc.startsWith('data:image/')}}")
    press_button(browser, "取消截图")
    check(frozen["local"] and frozen["width"] > 640 and frozen["height"] > 360
          and instance.counts() == before,
          "workspace_screenshot_shortcut_freezes_visible_synthetic_UI_locally_and_cancel_does_not_dispatch", result,
          frozen=frozen)


def fullscreen_scene(browser: Browser, instance: MigrationInstance, course_id: str, result: dict):
    before = instance.counts()
    sid = native_mapping(browser, course_id)["session_id"]
    press_button(browser, "视频全屏")
    browser.command("run-code", "async(page)=>{await page.waitForFunction(()=>!!document.fullscreenElement"
                    "&&document.querySelector('[data-learning-frame]')?.dataset.learningImmersive==='true');"
                    "await page.keyboard.press('Control+Shift+A');await page.locator('[data-composer-input]')"
                    ".waitFor({state:'visible'});}")
    shown = browser.evaluate("()=>({fullscreen:!!document.fullscreenElement,"
                             "aiOpen:document.querySelector('[data-learning-frame]').dataset.learningAiCollapsed==='false'})")
    press_button(browser, "收起 AI")
    browser.command("run-code", "async(page)=>{await page.waitForFunction(()=>{"
                    "const v=Array.from(document.querySelectorAll('[data-primary-video]')).find(v=>v.getBoundingClientRect().width>0);"
                    "return v&&!v.paused&&document.querySelector('[data-learning-frame]').dataset.learningAiCollapsed==='true'});"
                    "await page.keyboard.press('Escape');await page.waitForFunction(()=>!document.fullscreenElement);}")
    restored = browser.evaluate("()=>({immersive:document.querySelector('[data-learning-frame]').dataset.learningImmersive,"
                                "aiCollapsed:document.querySelector('[data-learning-frame]').dataset.learningAiCollapsed,"
                                "courseWidth:document.querySelector('[data-testid=learning-course-directory]').getBoundingClientRect().width})")
    check(shown["fullscreen"] and shown["aiOpen"] and restored["immersive"] == "false"
          and restored["aiCollapsed"] == "false" and restored["courseWidth"] > 100
          and native_mapping(browser, course_id)["session_id"] == sid and instance.counts() == before,
          "fullscreen_native_AI_open_close_and_exit_restore_learning_layout_without_new_chat", result,
          restored=restored, boundary="Chromium DOM fullscreen; native Electron permission/window behavior is separately tested")


def answer_formula_quote(browser: Browser, chat_id: str, result: dict):
    browser.snapshot("before-native-answer-formula-selection")
    browser.command("run-code", "async(page)=>{await page.evaluate(()=>{"
                    "const formula=Array.from(document.querySelectorAll('[data-learning-region=ai] "
                    "[data-chat-flow-kind=assistant-step] .katex-html')).at(-1);"
                    "if(!formula)throw Error('NATIVE_ANSWER_FORMULA_MISSING');"
                    "const range=document.createRange();range.selectNodeContents(formula);"
                    "const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);"
                    "formula.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));});"
                    "await page.getByRole('button',{name:'添加到对话',exact:true}).last().click();}")
    wait_api(browser, f"/chats/{chat_id}/draft", "value.references?.length===1", timeout_ms=20000)
    draft = api(browser, f"/chats/{chat_id}/draft")["body"]
    reference = draft["references"][0]
    chat = api(browser, f"/chats/{chat_id}")["body"]
    source = next(message for message in chat["messages"] if message["message_id"] == reference["message_id"])
    selected_source = source["text"].encode("utf-16-le")[reference["source_start"] * 2:reference["source_end"] * 2].decode("utf-16-le")
    formula = "x^2-5x+6=(x-2)(x-3)"
    check(selected_source == formula and reference["markdown"] in {formula, "$" + formula + "$", "$$" + formula + "$$"},
          "native_answer_formula_quote_maps_exact_business_message_and_UTF16_source_range", result,
          source_start=reference["source_start"], source_end=reference["source_end"], markdown=reference["markdown"],
          selected_source=selected_source, boundary="math delimiters may be restored for rendering; source interval stays exact")


def history_attachments(browser: Browser, instance: MigrationInstance, course_id: str, chat_id: str,
                        attachment_ids: list[str], manifest: dict, result: dict):
    """Use the actual historical attachment UI, including real download events."""
    before = instance.counts()
    mapping = native_mapping(browser, course_id)
    metadata = {aid: api(browser, f"/attachments/{aid}/metadata")["body"] for aid in attachment_ids}
    image_id = next(aid for aid, value in metadata.items() if value["kind"] == "image")
    source = api(browser, f"/attachments/{image_id}/source")["body"]
    # Move the already-open source away from the capture time, then show files,
    # so a source action must both select the correct material and seek it.
    open_course_video(browser, "synthetic-course.webm")
    browser.command("run-code", "async(page)=>{await page.locator('[data-primary-video]:visible')"
                    ".evaluate(v=>{v.pause();v.currentTime=23;});}")
    files_tab(browser)
    press_button(browser, "本对话附件")
    browser.command("run-code", "async(page)=>{await page.getByTestId('learning-history-attachments')"
                    ".locator('[data-attachment-id]').last().waitFor({state:'visible'});}")
    rows = browser.evaluate("()=>Array.from(document.querySelectorAll('[data-testid=learning-history-attachments] "
                            "[data-attachment-id]')).map(row=>row.dataset.attachmentId)")
    check(set(rows) == set(attachment_ids) and len(rows) == 4,
          "history_attachment_panel_lists_only_four_submitted_originals_after_reedit", result,
          attachment_count=len(rows))
    downloaded = []
    downloads = browser.directory / "downloads"
    downloads.mkdir(exist_ok=True)
    for index, aid in enumerate(attachment_ids):
        destination = downloads / f"attachment-{index}.original"
        browser.snapshot("before-history-download-" + str(index))
        browser.command("run-code", "async(page)=>{const row=page.getByTestId('learning-history-attachments')"
                        ".locator('[data-attachment-id=" + aid + "]');"
                        "const pending=page.waitForEvent('download');await row.getByRole('link').click();"
                        "const download=await pending;await download.saveAs(" + json.dumps(str(destination)) + ");"
                        "if(await download.failure())throw Error('ATTACHMENT_DOWNLOAD_FAILED');}")
        actual_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
        value = metadata[aid]
        check(actual_hash == value["sha256"], "history_attachment_download_matches_original_" + value["kind"],
              result, kind=value["kind"], size_bytes=destination.stat().st_size, sha256=actual_hash)
        if value["kind"] != "image":
            expected = Path(manifest["attachment_directory"]) / value["filename"]
            if hashlib.sha256(expected.read_bytes()).hexdigest() != actual_hash:
                raise AssertionError("HISTORY_DOCUMENT_DOWNLOAD_IS_NOT_SELECTED_ORIGINAL")
        downloaded.append(value["kind"])
    check(set(downloaded) == {"image", "pdf", "text", "markdown"},
          "history_original_download_covers_image_PDF_text_and_Markdown", result)
    browser.snapshot("before-history-source-jump")
    browser.command("run-code", "async(page)=>{await page.getByTestId('learning-history-attachments')"
                    ".locator('[data-attachment-id=" + image_id + "]')"
                    ".getByRole('button',{name:'回到视频来源',exact:true}).click();"
                    "await page.getByTestId('learning-history-attachments').waitFor({state:'hidden'});"
                    "await page.waitForFunction(source=>{const v=Array.from(document.querySelectorAll('[data-primary-video]'))"
                    ".find(v=>v.getBoundingClientRect().width>0);if(!v||v.readyState<2)return false;"
                    "const url=new URL(v.currentSrc);return url.pathname==='/api/v1/videos/'+source.asset_id+'/content'"
                    "&&url.searchParams.get('source_version')===source.source_version"
                    "&&Math.abs(v.currentTime*1000-source.time_ms)<1000}," + json.dumps(source) + ");}")
    check(native_mapping(browser, course_id)["session_id"] == mapping["session_id"]
          and native_mapping(browser, course_id)["chat_id"] == chat_id and instance.counts() == before,
          "history_video_source_jump_restores_original_asset_version_time_without_new_chat_or_dispatch", result,
          time_ms=source["time_ms"], source_version=source["source_version"])


def theme_scene(browser: Browser, instance: MigrationInstance, course_id: str, result: dict):
    before = instance.counts()
    sid = native_mapping(browser, course_id)["session_id"]
    for label, dark in (("浅色", False), ("深色", True)):
        press_button(browser, "设置")
        browser.snapshot("before-theme-" + str(dark))
        browser.command("run-code", "async(page)=>{await page.getByRole('menuitem',{name:"
                        + json.dumps(label) + ",exact:true}).click();await page.waitForFunction(dark=>"
                        "document.body.hasAttribute('data-ds-dark-theme')===dark," + json.dumps(dark) + ");}")
        saved_theme = instance.directory / "data/desktop-ui/theme.json"
        wait_until(lambda: saved_theme.is_file() and json.loads(saved_theme.read_text("utf-8"))
                   .get("preference") == ("dark" if dark else "light"), timeout=15)
        browser.command("reload")
        browser.command("run-code", "async(page)=>{await page.locator('[data-composer-input]').waitFor({state:'visible'});"
                        "await page.waitForFunction(dark=>document.body.hasAttribute('data-ds-dark-theme')===dark,"
                        + json.dumps(dark) + ");}")
        press_button(browser, "设置")
        browser.command("run-code", "async page=>{await page.waitForFunction(label=>[...document.querySelectorAll('[role=menuitem]')]"
                        ".some(e=>e.textContent.includes(label)&&e.querySelector('svg'))," + json.dumps(label) + ");}")
        chosen = label
        browser.command("press", "Escape")
        check(chosen == label and native_mapping(browser, course_id)["session_id"] == sid,
              "native_theme_" + ("dark" if dark else "light") + "_persists_after_reload_without_changing_chat", result)
    check(instance.counts() == before, "native_theme_changes_do_not_dispatch_model_requests", result)


def history_navigation_race(browser: Browser, instance: MigrationInstance, course_id: str, result: dict):
    """An older HTTP open response must not replace the user's newer choice."""
    before = instance.counts()
    original = native_mapping(browser, course_id)
    generated = []
    for name in ("B", "C"):
        created = api(browser, f"/courses/{course_id}/chats", {}, method="POST")
        if created["status"] != 200:
            raise AssertionError("SYNTHETIC_HISTORY_CHAT_CREATION_FAILED")
        chat_id = created["body"]["chat_id"]
        draft = api(browser, f"/chats/{chat_id}/draft")["body"]
        payload = {key: value for key, value in draft.items() if key != "revision"}
        payload.update(text="合成历史草稿 " + name, expected_revision=draft["revision"])
        saved = api(browser, f"/chats/{chat_id}/draft", payload, method="POST")
        if saved["status"] != 200:
            raise AssertionError("SYNTHETIC_HISTORY_DRAFT_SEED_FAILED")
        opened = api(browser, f"/desktop/chats/{chat_id}/open", {}, method="POST")["body"]
        generated.append((chat_id, opened["session_id"]))
    chats = api(browser, f"/courses/{course_id}/chats")["body"]
    labels = {chat["chat_id"]: "对话 " + str(index + 1) for index, chat in enumerate(chats)}
    b_id, _ = generated[0]
    c_id, c_sid = generated[1]
    b_route = "**/api/v1/desktop/chats/" + b_id + "/open"
    browser.command("run-code", "async(page)=>{await page.evaluate(()=>{window.__migrationReleaseB=false;"
                    "window.__migrationBPending=false;window.__migrationBDelivered=false;});"
                    "await page.route(" + json.dumps(b_route) + ",async route=>{const response=await route.fetch();"
                    "await page.evaluate(()=>window.__migrationBPending=true);"
                    "await page.waitForFunction(()=>window.__migrationReleaseB,null,{timeout:25000});"
                    "await route.fulfill({response});await page.evaluate(()=>window.__migrationBDelivered=true);});}")
    press_button(browser, "选择历史对话")
    browser.command("run-code", "async(page)=>{await page.getByRole('menuitem',{name:"
                    + json.dumps(labels[b_id]) + ",exact:true}).click();"
                    "await page.waitForFunction(()=>window.__migrationBPending);}")
    press_button(browser, "选择历史对话")
    browser.command("run-code", "async(page)=>{await page.getByRole('menuitem',{name:"
                    + json.dumps(labels[c_id]) + ",exact:true}).click();"
                    "await page.waitForFunction(sid=>document.querySelector('[data-testid=learning-materials]')"
                    "?.dataset.sessionId===sid," + json.dumps(c_sid) + ");"
                    "await page.waitForFunction(()=>document.querySelector('[data-composer-input]')"
                    "?.textContent.includes('合成历史草稿 C'));"
                    "await page.evaluate(()=>window.__migrationReleaseB=true);"
                    "await page.waitForFunction(()=>window.__migrationBDelivered);"
                    "await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));"
                    "await page.unroute(" + json.dumps(b_route) + ");}")
    composer_fill(browser, "合成历史草稿 C：最后选择保留。")
    saved_c = wait_api(browser, f"/chats/{c_id}/draft", "value.text==='合成历史草稿 C：最后选择保留。'")
    saved_b = api(browser, f"/chats/{b_id}/draft")["body"]
    check(native_mapping(browser, course_id)["session_id"] == c_sid
          and saved_c["text"] == "合成历史草稿 C：最后选择保留。" and saved_b["text"] == "合成历史草稿 B"
          and instance.counts() == before,
          "late_history_B_response_cannot_override_last_selected_C_or_misroute_its_draft", result,
          boundary="real B HTTP response is delayed until the actual native C selection finishes; no model call")
    press_button(browser, "选择历史对话")
    browser.command("run-code", "async(page)=>{await page.getByRole('menuitem',{name:"
                    + json.dumps(labels[original["chat_id"]]) + ",exact:true}).click();"
                    "await page.waitForFunction(sid=>document.querySelector('[data-testid=learning-materials]')"
                    "?.dataset.sessionId===sid," + json.dumps(original["session_id"]) + ");}")


def draft_and_chat(browser: Browser, instance: MigrationInstance, manifest: dict, course_id: str, result: dict, *, quotes: bool):
    before = instance.counts()
    mapping = native_mapping(browser, course_id)
    original_sid, chat_id = mapping["session_id"], mapping["chat_id"]
    text = "未发送草稿：后面通过普通消息补充和更正。"
    composer_fill(browser, text)
    attachments = Path(manifest["attachment_directory"])
    images = []
    if quotes:
        capture_and_reedit(browser, chat_id, text, result)
    else:
        images.append(Path(manifest["course_root"]) / "synthetic-frame.png")
    native_upload(browser, [*images, attachments / "two-page-text.pdf", attachments / "whole-document.txt",
                            attachments / "whole-document.md"], new_chat_while_pending=True)
    browser.command("run-code", "async(page)=>{await page.getByText('two-page-text.pdf',{exact:false})"
                    ".last().waitFor({state:'visible',timeout:25000});}")
    wait_api(browser, f"/chats/{chat_id}/draft", "value.attachment_ids?.length===4")
    draft_before = browser.evaluate("()=>({text:document.querySelector('[data-composer-input]').textContent,"
                                    "attachments:document.querySelector('[aria-label=待发送附件]')?.innerText||''})")
    for _ in range(3):
        press_button(browser, "新对话")
    mapping_after = native_mapping(browser, course_id)
    draft_after = browser.evaluate("()=>({text:document.querySelector('[data-composer-input]').textContent,"
                                   "attachments:document.querySelector('[aria-label=待发送附件]')?.innerText||''})")
    check(mapping_after["session_id"] == original_sid and mapping_after["chat_id"] == chat_id
          and draft_before == draft_after and text in draft_after["text"]
          and len(api(browser, f"/courses/{course_id}/chats")["body"]) == 1 and instance.counts() == before,
          "repeated_New_Chat_reuses_unsent_chat_and_preserves_text_image_and_document_drafts", result)
    persisted = api(browser, f"/chats/{chat_id}/draft")["body"]
    check(text in persisted["text"] and len(persisted["attachment_ids"]) == 4,
          "native_text_and_attachment_drafts_are_saved_to_business_storage", result)
    browser.command("reload")
    browser.command("run-code", "async(page)=>{await page.waitForFunction(text=>document.querySelector('[data-composer-input]')"
                    "?.textContent.includes(text)," + json.dumps(text) + ",{timeout:25000});}")
    browser.command("run-code", "async(page)=>{await page.waitForFunction(()=>"
                    "document.body.innerText.includes('two-page-text.pdf')"
                    "||document.body.innerText.includes('DRAFT_NOT_READY')"
                    "||document.body.innerText.includes('DRAFT_RESTORE_CONFLICT'),null,{timeout:25000});}")
    restored = browser.snapshot("restored-unsent-draft")
    check("two-page-text.pdf" in restored and instance.counts() == before,
          "native_draft_reload_restores_unsent_documents_without_model_dispatch", result)
    restored_draft = api(browser, f"/chats/{chat_id}/draft")["body"]
    with sqlite3.connect(instance.directory / "data/business.sqlite3") as connection:
        prepared_count = connection.execute(
            "SELECT COUNT(*) FROM document_attachments WHERE chat_id=?", (chat_id,),
        ).fetchone()[0]
    check(restored_draft["attachment_ids"] == persisted["attachment_ids"] and prepared_count == 3,
          "draft_rehydration_reuses_original_attachment_receipts_without_repreparing_documents", result,
          prepared_document_count=prepared_count)
    check(not browser.evaluate("()=>document.body.innerText.includes('题目条件与更正')"
                               "||document.body.innerText.includes('当前题目条件（每行一项）')"),
          "independent_conditions_and_corrections_controls_are_absent", result)
    question = "请读取我确认的整份合成文档并回答。"
    composer_fill(browser, question)
    instance.control("scenario", scenario={"text_chunks": ["合成附件接入完成：两页 PDF 与完整文本均已收到。\n\n"
                                                           "公式 $x^2-5x+6=(x-2)(x-3)$。"]})
    press_button(browser, "发送消息")
    wait_until(lambda: instance.counts()["local_messages"] == before["local_messages"] + 1, timeout=25)
    first = instance.generation_for(question)
    wait_generation(browser, first["generation_id"], "completed")
    first_native = instance.control("inspect", generation_id=first["generation_id"])
    outgoing_images = first_native["boundary"][0]["images"]
    with sqlite3.connect(instance.directory / "data/business.sqlite3") as connection:
        placeholders = ",".join("?" for _ in persisted["attachment_ids"])
        selected_hash = connection.execute(
            "SELECT content_hash FROM attachments WHERE media_type LIKE 'image/%' AND attachment_id IN ("
            + placeholders + ")", persisted["attachment_ids"],
        ).fetchone()[0]
    check(len(outgoing_images) == 3 and any(image["sha256"] == selected_hash for image in outgoing_images)
          and len({image["sha256"] for image in outgoing_images}) == 3,
          "native_document_send_contains_only_selected_image_and_two_confirmed_PDF_pages_without_preview_frames", result,
          images=outgoing_images)
    original_request = next((instance.directory / "execution/boundary/private/boundary").glob("*.request.json"))
    request = json.dumps(json.loads(original_request.read_text("utf-8")), ensure_ascii=False)
    check(all(marker in request for marker in (
        "TEXT_START_1729", "TEXT_END_2718", "MARKDOWN_START_3141", "MARKDOWN_END_5926",
    )) and "application/pdf" not in request,
          "native_text_and_Markdown_attachments_reach_actual_request_completely_without_PDF_binary", result)
    if quotes:
        history_attachments(browser, instance, course_id, chat_id, persisted["attachment_ids"], manifest, result)
        answer_formula_quote(browser, chat_id, result)
    correction = "更正刚才的描述：请使用我这条普通消息，并等待我停止。"
    partial = "普通消息的补充与更正已按顺序收到，回答正在生成。"
    instance.control("scenario", scenario={"text_chunks": [partial, "不应在停止后继续的尾部。"],
                                            "pause_after_event": "text_delta", "gate": "migration-stop"})
    composer_fill(browser, correction)
    press_button(browser, "发送消息")
    wait_until(lambda: instance.counts()["local_messages"] == before["local_messages"] + 2, timeout=25)
    second = instance.generation_for(correction)
    instance.control("wait_gate", generation_id=second["generation_id"])
    browser.wait_text(partial)
    press_button(browser, "停止生成")
    stopped = wait_generation(browser, second["generation_id"], "cancelled")
    stopped_native = instance.control("inspect", generation_id=second["generation_id"])
    check(stopped["body"] == partial and stopped_native["host_alive"]
          and stopped_native["boundary"][0]["cancel_observed"],
          "ordinary_correction_message_uses_native_stream_and_active_Stop", result)
    follow = "停止后普通追问：请简短确认。"
    instance.control("scenario", scenario={"text_chunks": ["同一对话追问已完成。"]})
    composer_fill(browser, follow)
    press_button(browser, "发送消息")
    wait_until(lambda: instance.counts()["local_messages"] == before["local_messages"] + 3, timeout=25)
    third = instance.generation_for(follow)
    wait_generation(browser, third["generation_id"], "completed")
    third_native = instance.control("inspect", generation_id=third["generation_id"])
    check(original_sid == first_native["session_id"] == stopped_native["session_id"] == third_native["session_id"],
          "document_send_correction_Stop_and_follow_up_keep_original_native_session", result)
    if quotes:
        workspace_capture_cancel(browser, instance, result)


def quote_scene(browser: Browser, instance: MigrationInstance, course_id: str, result: dict):
    question = "请显示用于精确引用验证的合成公式。"
    answer = "合成回答🧮的文字与公式。\n\n公式 $x^2-5x+6=(x-2)(x-3)$。"
    mapping = native_mapping(browser, course_id)
    instance.control("scenario", scenario={"text_chunks": [answer]})
    composer_fill(browser, question)
    press_button(browser, "发送消息")
    wait_until(lambda: instance.counts()["local_messages"] == 1, timeout=25)
    generation = instance.generation_for(question)
    wait_generation(browser, generation["generation_id"], "completed")
    browser.command("run-code", "async(page)=>{await page.locator('[data-learning-region=ai] "
                    "[data-chat-flow-kind=assistant-step] .katex-html').last().waitFor({state:'visible'});}")
    answer_formula_quote(browser, mapping["chat_id"], result)


def run_case(directory: Path, executable: Path, port: int, ffmpeg: Path, mode: str) -> dict:
    manifest = synthetic_assets(directory / "synthetic", ffmpeg)
    result = {"id": "desktop-feature-migration", "status": "running", "assertions": [],
              "evidence_level": "real Chromium + original Harness UI + formal application + real SEA + local fixture",
              "mode": mode, "synthetic_manifest": manifest, "real_model_attempts": 0,
              "source_digests_before": source_digests(),
              "limitations": ["Folder and clipboard selection are synthetic seams; real foreground picker has separate evidence.",
                              "This run makes no real model request and does not evaluate human learning value."]}
    instance = MigrationInstance(directory / "server", port)
    browser = Browser(directory / "browser", executable)
    try:
        instance.start()
        browser.command("open", instance.url + "/desktop/", f"--config={browser.config}")
        browser.snapshot("initial-native-client")
        course_id, _ = create_course(browser, instance, manifest, result)
        if mode in {"full", "files"}:
            file_management(browser, instance, manifest, course_id, result)
            reading(browser, result)
        if mode in {"full", "preview"}:
            preview(browser, course_id, result)
        if mode == "full":
            fullscreen_scene(browser, instance, course_id, result)
        if mode in {"full", "chat"}:
            draft_and_chat(browser, instance, manifest, course_id, result, quotes=mode == "full")
        if mode in {"full", "navigation"}:
            theme_scene(browser, instance, course_id, result)
            history_navigation_race(browser, instance, course_id, result)
        if mode == "capture":
            mapping = native_mapping(browser, course_id)
            text = "合成截图重裁验证草稿。"
            composer_fill(browser, text)
            capture_and_reedit(browser, mapping["chat_id"], text, result)
            workspace_capture_cancel(browser, instance, result)
        if mode == "quote":
            quote_scene(browser, instance, course_id, result)
        counts = instance.counts()
        check(counts["real_provider_attempts"] == 0 and counts["business_attempts"] == counts["submissions"]
              == counts["local_messages"], "migration_fixture_has_no_internet_model_forwarding_and_accounted_submissions",
              result, counts=counts)
        browser.snapshot("completed-migrated-features")
        browser.command("screenshot", f"--filename={browser.directory / 'completed-migrated-features.png'}", "--full-page")
        result.update(status="passed", counts=counts)
    except BaseException as error:
        result.update(status="failed", failure=f"{type(error).__name__}: {error}")
        write_json(directory / "result.json", result)
        try:
            (browser.directory / "console.txt").write_text(browser.command("console"), encoding="utf-8")
            browser.snapshot("failure")
            browser.command("screenshot", f"--filename={browser.directory / 'failure.png'}", "--full-page")
        except Exception:
            pass
    finally:
        result["browser_cleanup"] = browser.close()
        if instance.process is not None and instance.process.poll() is None:
            result["fixture_alive_after_browser_close"] = http(instance.url + "/api/v1/health")[0] == 200
        result["cleanup"] = instance.stop()
        after = source_digests()
        result["source_digests_after"] = after
        result["changed_files_during_run"] = [path for path in sorted(set(after) | set(result["source_digests_before"]))
                                              if after.get(path) != result["source_digests_before"].get(path)]
        if not result["cleanup"].get("backend_port_closed"):
            result["status"] = "failed"
        if result["changed_files_during_run"] and result["status"] == "passed":
            result.update(status="failed", failure="SOURCE_CHANGED_DURING_VERIFICATION")
        write_json(directory / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="desktop-migration-" + time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--mode", choices=["full", "files", "preview", "chat", "quote", "capture", "navigation"], default="full")
    parser.add_argument("--backend-port", type=int, default=19030)
    parser.add_argument("--browser-path", type=Path, default=Path(os.environ.get("LOCALAPPDATA", ""))
                        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe")
    parser.add_argument("--ffmpeg-path", type=Path, default=Path(os.environ.get("LOCALAPPDATA", ""))
                        / "Microsoft/WinGet/Links/ffmpeg.EXE")
    parser.add_argument("--serve-fixture", type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.serve_fixture:
        return asyncio.run(serve_fixture(arguments.serve_fixture, arguments.backend_port))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", arguments.run_id):
        parser.error("run-id只能包含字母、数字、横线和下划线")
    if not arguments.browser_path.is_file() or not arguments.ffmpeg_path.is_file():
        raise RuntimeError("LOCAL_TEST_TOOL_MISSING: 指定已有Chromium与ffmpeg，不自动下载")
    if not port_closed(arguments.backend_port):
        raise RuntimeError("PORT_IN_USE: 不接管其他进程")
    directory = ROOT / "output/playwright" / arguments.run_id
    directory.mkdir(parents=True, exist_ok=False)
    result = run_case(directory, arguments.browser_path, arguments.backend_port, arguments.ffmpeg_path, arguments.mode)
    print(json.dumps({"status": result["status"], "evidence": str(directory / "result.json")}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
