"""Actual pinned Harness UI visual checks, using generated course files only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from test_browser import Browser, ROOT, check, port_closed, write_json
from test_desktop import composer_fill, press_button
from test_desktop_migration import (
    MigrationInstance, create_course, native_upload, source_digests, synthetic_assets, wait_api,
)
from test_mvp_a import api
from test_workspace_v2 import manager_click, manager_entry


SCENE = """()=>{const ai=document.querySelector('[data-learning-region=ai]');
const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom,right:r.right}};
const header=ai.querySelector('[data-testid=learning-ai-header]');const seat=ai.querySelector('[data-composer-seat]');
const primary=['.learning-course-title','.learning-conversation-title > span:first-child','.file-manager-file-name','[role=tab][aria-selected=true]'].map(s=>document.querySelector(s)).filter(Boolean);
return {width:innerWidth,height:innerHeight,ai:rect(ai),seat:rect(seat),header:header.innerText,
text:ai.innerText,overflow:document.documentElement.scrollWidth>innerWidth,
fonts:primary.map(e=>getComputedStyle(e).fontFamily),bodyFont:getComputedStyle(document.body).fontFamily,
sizes:primary.map(e=>getComputedStyle(e).fontSize),
chrome:[...ai.querySelectorAll('[data-learning-hero-chrome]')].map(e=>getComputedStyle(e).display),
buttonBackground:getComputedStyle(header.querySelector('button')).backgroundColor,
navigation:rect(document.querySelector('.file-manager-navigation')).height,
toolbar:rect(document.querySelector('.file-manager-toolbar')).height,
placeholder:getComputedStyle(ai.querySelector('[data-composer-placeholder]'),'::after').content}}"""


def polish_digests():
    result = source_digests()
    for path in (Path(__file__).resolve(), ROOT / "tests/e2e/test_workspace_v2.py"):
        result[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--backend-port", type=int, default=19040)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id) or not port_closed(args.backend_port):
        parser.error("require a new safe run-id and an unused port")
    output = ROOT / "output/playwright" / args.run_id
    output.mkdir(parents=True, exist_ok=False)
    manifest = synthetic_assets(output / "synthetic", Path(os.environ["LOCALAPPDATA"]) / "Microsoft/WinGet/Links/ffmpeg.exe")
    # The native application lists a generated Word file; it is never opened.
    (Path(manifest["course_root"]) / "合成讲义.docx").write_bytes(b"synthetic unopened file type fixture")
    browser = Browser(output / "browser", Path(os.environ["LOCALAPPDATA"]) / "ms-playwright/chromium-1234/chrome-win64/chrome.exe")
    instance = MigrationInstance(output / "server", args.backend_port)
    result = {"status": "running", "assertions": [], "source_digests_before": polish_digests(),
              "scope": "actual Harness client, real SEA and formal backend, synthetic media, no model submission"}
    try:
        instance.start()
        browser.command("open", instance.url + "/desktop/", f"--config={browser.config}")
        browser.snapshot("initial")
        course_id, mapping = create_course(browser, instance, manifest, result)
        for theme in ("浅色", "深色"):
            press_button(browser, "设置")
            browser.command("run-code", "async page=>{await page.getByRole('menuitem',{name:" + json.dumps(theme) + ",exact:true}).click()}")
            for width, height in ((1440, 1000), (2048, 1104), (1024, 768), (720, 600)):
                browser.command("resize", str(width), str(height))
                scene = browser.evaluate(SCENE)
                check("探索未至之境" not in scene["text"] and "course_" not in scene["header"]
                      and scene["chrome"] == ["none", "none"] and "新对话" in scene["header"]
                      and "从当前资料开始" not in scene["text"],
                      f"{theme}_{width}_blank_learning_conversation_has_no_marketing_or_internal_id", result)
                check(abs(scene["seat"]["bottom"] - height) <= 16 and scene["seat"]["height"] <= 220
                      and not scene["overflow"] and scene["ai"]["width"] >= 280,
                      f"{theme}_{width}_native_composer_is_docked_with_no_page_overflow", result, scene=scene)
                check(scene["buttonBackground"] == "rgba(0, 0, 0, 0)" and scene["navigation"] <= 46
                      and scene["toolbar"] <= (82 if width == 720 else 44),
                      f"{theme}_{width}_ghost_actions_and_compact_file_toolbar", result)
                check(all(value == scene["bodyFont"] for value in scene["fonts"])
                      and all(value == "14px" for value in scene["sizes"]),
                      f"{theme}_{width}_course_files_tabs_and_chat_title_share_native_font", result)
                browser.command("screenshot", f"--filename={output / ('light' if theme == '浅色' else 'dark')}-{width}.png", "--full-page")
        browser.command("resize", "1440", "1000")
        browser.snapshot("before-interaction-checks")
        press_button(browser, "设置")
        normal_menu = browser.evaluate("()=>{const menu=document.querySelector('.learning-menu-surface');return {duration:getComputedStyle(menu).animationDuration,text:menu.innerText}}")
        check(normal_menu["duration"] == "0.1s" and "账号与模型" in normal_menu["text"],
              "settings_and_history_keep_native_menu_with_short_local_motion", result)
        browser.command("press", "Escape")
        browser.command("run-code", "async page=>{await page.keyboard.press('Tab');await page.keyboard.press('Tab');await page.waitForFunction(()=>getComputedStyle(document.querySelector('.learning-ai-actions')).opacity==='1');}")
        check(browser.evaluate("()=>document.querySelector('.learning-ai-header').contains(document.activeElement)"),
              "keyboard_focus_reveals_contextual_conversation_actions", result)
        quiet = browser.evaluate("()=>({count:[...document.querySelectorAll('.file-manager-status')].some(e=>getComputedStyle(e).display!=='none'),labels:document.querySelector('[data-testid=learning-course-directory]').innerText,settings:document.querySelectorAll('[aria-label=设置]').length})")
        check(not quiet["count"] and "课程目录" not in quiet["labels"] and quiet["settings"] == 1,
              "idle_workspace_has_no_redundant_directory_label_or_item_count_and_one_settings_entry", result)
        browser.command("run-code", "async page=>{const handle=page.getByRole('separator',{name:'调整 AI 宽度'});const r=await handle.boundingBox();await page.mouse.move(r.x+3,r.y+200);await page.mouse.down();}")
        dragging = browser.evaluate("()=>({active:document.querySelector('[data-learning-frame]').hasAttribute('data-learning-resizing'),transition:getComputedStyle(document.querySelector('[data-learning-frame]')).transitionDuration})")
        browser.command("run-code", "async page=>{await page.mouse.up();}")
        check(dragging["active"] and dragging["transition"] == "0s", "panel_dragging_has_no_animation_lag", result)
        browser.command("run-code", "async page=>{await page.emulateMedia({reducedMotion:'reduce'});}")
        reduced = browser.evaluate("()=>getComputedStyle(document.querySelector('[data-learning-frame]')).transitionDuration")
        check(reduced == "0s", "reduced_motion_disables_adapted_panel_animation", result)
        press_button(browser, "设置")
        check(browser.evaluate("()=>getComputedStyle(document.querySelector('.learning-menu-surface')).animationName") == "none",
              "reduced_motion_disables_menu_animation", result)
        browser.command("press", "Escape")
        browser.command("run-code", "async page=>{await page.emulateMedia({reducedMotion:'no-preference'});}")
        browser.snapshot("before-file-covers")
        browser.command("run-code", "async page=>{await page.waitForFunction(()=>[...document.querySelectorAll('.learning-file-kind-video img')].some(i=>i.complete&&i.naturalWidth===240),null,{timeout:30000})}")
        with sqlite3.connect(instance.directory / "data/business.sqlite3") as connection:
            opened = connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        check(opened == 0, "file_covers_reuse_cache_without_registering_or_opening_media", result)
        check(browser.evaluate("()=>!!document.querySelector('[data-file-type=word]')"), "Word_file_has_distinct_type_mark", result)
        manager_entry(browser, "migration-read.txt")
        manager_click(browser, "属性")
        check('dialog "属性"' in browser.snapshot("more-properties"), "more_menu_preserves_properties_action", result)
        browser.command("press", "Escape")
        browser.command("run-code", "async page=>{await page.getByRole('dialog',{name:'属性',exact:true}).waitFor({state:'hidden'});}")
        for name in ("合成课程（精简验证）", "合成桌面功能迁移课程"):
            browser.snapshot("before-course-rename")
            browser.command("run-code", "async page=>{await page.locator('.learning-library-course-heading').hover();"
                            "await page.locator('summary[aria-label=课程设置]').click();"
                            "await page.getByRole('button',{name:'重命名课程…',exact:true}).click();"
                            "const dialog=page.getByRole('dialog',{name:'重命名课程',exact:true});"
                            "await dialog.getByRole('textbox',{name:'课程名称'}).fill(" + json.dumps(name) + ");"
                            "await dialog.getByRole('button',{name:'保存',exact:true}).click();await dialog.waitFor({state:'hidden'});}")
            courses = wait_api(browser, "/courses", "value.some(c=>c.course_id===" + json.dumps(course_id) + "&&c.title===" + json.dumps(name) + ")")
            check(any(c["course_id"] == course_id and c["title"] == name for c in courses),
                  "contextual_course_menu_preserves_rename_" + name, result)
        press_button(browser, "设置")
        browser.command("run-code", "async page=>{await page.getByRole('menuitem',{name:'账号与模型',exact:true}).click();"
                        "await page.getByRole('dialog',{name:'账号与模型',exact:true}).waitFor({state:'visible'});}")
        check('dialog "账号与模型"' in browser.snapshot("account-entry"), "single_settings_entry_keeps_account_and_model_panel_accessible", result)
        press_button(browser, "关闭设置")
        text = "合成设计验证草稿：保留文字与图片。"
        composer_fill(browser, text)
        native_upload(browser, [Path(manifest["course_root"]) / "synthetic-frame.png"])
        draft = wait_api(browser, f"/chats/{mapping['chat_id']}/draft", "value.attachment_ids.length===1")
        original_ids = draft["attachment_ids"]
        browser.command("reload")
        browser.command("run-code", "async page=>{await page.waitForFunction(t=>document.querySelector('[data-composer-input]')?.textContent.includes(t)," + json.dumps(text) + ")}")
        restored = wait_api(browser, f"/chats/{mapping['chat_id']}/draft", "value.attachment_ids.length===1")
        check(restored["attachment_ids"] == original_ids and restored["text"] == text,
              "layout_polish_keeps_native_text_and_image_draft_on_reload", result)
        check(len(api(browser, f"/courses/{course_id}/chats")["body"]) == 1 and instance.counts()["local_messages"] == 0,
              "visual_changes_do_not_create_chats_or_dispatch_models", result)
        browser.command("screenshot", f"--filename={output}/draft-preserved.png", "--full-page")
        result.update(status="passed", counts=instance.counts())
    except BaseException as error:
        result.update(status="failed", failure=f"{type(error).__name__}: {error}")
        try:
            browser.snapshot("failure")
            browser.command("screenshot", f"--filename={output}/failure.png", "--full-page")
            (output / "console.txt").write_text(browser.command("console"), "utf-8")
        except Exception:
            pass
    finally:
        result["browser_cleanup"] = browser.close()
        result["cleanup"] = instance.stop()
        after = polish_digests()
        result["source_digests_after"] = after
        result["changed_files_during_run"] = [name for name in sorted(set(after) | set(result["source_digests_before"]))
                                              if after.get(name) != result["source_digests_before"].get(name)]
        if result["changed_files_during_run"] or not result["cleanup"].get("backend_port_closed"):
            result["status"] = "failed"
        write_json(output / "result.json", result)
    print(json.dumps({"status": result["status"], "evidence": str(output / 'result.json')}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
