"""Pinned Harness UI + real owned Windows picker; synthetic workspace data only."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sqlite3
import time

from test_browser import Browser, ROOT, check, wait_until, write_json
from test_desktop import DesktopInstance
from test_mvp_a import api
from test_native_course_picker import CoursePicker


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def click(browser, pattern):
    snapshot = browser.snapshot("before-action")
    browser.command("click", browser.ref(snapshot, pattern))


def rows(browser):
    return browser.evaluate("()=>Array.from(document.querySelectorAll('.learning-workspace-name'),e=>e.getAttribute('aria-label'))")


def chat_scope(directory):
    with sqlite3.connect(directory / "data/business.sqlite3") as connection:
        return {
            "chats": connection.execute("SELECT chat_id,course_id FROM chats ORDER BY chat_id").fetchall(),
            "workspaces": connection.execute("SELECT * FROM chat_workspaces ORDER BY chat_id").fetchall(),
        }


def run_interactions(browser, instance, folders, directory, result, *, chat_only=False, hover_only=False):
    def action(code):
        browser.snapshot("before-interaction")
        browser.command("run-code", "async page=>{" + code + "}")

    courses = api(browser, "/courses")["body"]
    math = next(row for row in courses if row["title"] == "数学")
    english = next(row for row in courses if row["title"] == "英语")
    selector = '[data-course-id="' + math["course_id"] + '"]'
    row = "page.locator(" + json.dumps(selector) + ")"
    if hover_only:
        run_hover_feedback(browser, math, row, selector, action, directory, result)
        return
    (folders[0] / "子目录" / "深层目录").mkdir()
    (folders[0] / "子目录" / "子目录原文.txt").write_text("目录浏览样本", encoding="utf-8")
    baseline = chat_scope(instance.directory)
    with sqlite3.connect(instance.directory / "data/business.sqlite3") as connection:
        old_chat = connection.execute("SELECT chat_id FROM chats").fetchone()[0]
    action("await page.locator('[data-composer-input]').fill('切换前保留的草稿');")
    if chat_only:
        run_course_chat(browser, instance, math, english, row, baseline, old_chat, action, result)
        return
    action("await " + row + ".getByRole('button',{name:'展开文件夹 数学',exact:true}).click();"
           "await " + row + ".getByRole('button',{name:'子目录',exact:true}).waitFor();")
    check(chat_scope(instance.directory) == baseline, "expand_does_not_create_chat_or_change_read_scope", result)
    check(browser.evaluate("()=>!document.querySelector('[data-testid=learning-course-directory]').textContent.includes('原文.txt')"), "left_navigation_contains_directories_only", result)
    action("await " + row + ".getByRole('button',{name:'展开文件夹 子目录',exact:true}).click();"
           "await " + row + ".getByRole('button',{name:'深层目录',exact:true}).waitFor();")
    check(browser.evaluate("()=>document.querySelectorAll('.learning-subfolder-row .learning-workspace-new-chat').length===0"), "subfolders_have_no_new_chat_controls", result)
    action("await " + row + ".getByRole('button',{name:'子目录',exact:true}).click();"
           "await page.getByTestId('learning-library-files').getByRole('option',{name:'子目录原文.txt',exact:true}).waitFor();")
    check(chat_scope(instance.directory) == baseline, "subfolder_browsing_preserves_chat_and_permission_scope", result)
    check(browser.evaluate("()=>document.querySelector('[data-composer-input]').innerText") == "切换前保留的草稿", "subfolder_browsing_keeps_draft", result)
    action("await " + row + ".getByRole('button',{name:'收起文件夹 子目录',exact:true}).click();"
           "await page.getByTestId('learning-library-files').getByRole('option',{name:'子目录原文.txt',exact:true}).waitFor();")
    check(browser.evaluate("()=>document.querySelector('.learning-subfolder-row[data-selected=true]').dataset.folderPath") == "子目录", "collapse_only_changes_tree_not_browsed_folder", result)
    action("await page.getByRole('button',{name:'英语',exact:true}).click();"
           "await page.getByTestId('learning-library-files').getByRole('option',{name:'原文.txt',exact:true}).waitFor();")
    check(chat_scope(instance.directory) == baseline, "cross_course_browsing_does_not_grant_read_access", result)
    action("await " + row + ".getByRole('button',{name:'数学',exact:true}).hover();"
           "await page.locator('.learning-workspace-hover').waitFor();")
    hover = browser.evaluate("()=>document.querySelector('.learning-workspace-hover').innerText")
    check(math["root_path"] in hover and "创建于 " in hover and "1970年" not in hover, "native_hover_card_has_title_registered_path_and_creation_time", result, hover=hover)
    check(browser.evaluate("()=>{const r=document.querySelector(" + json.dumps(selector) + ");return getComputedStyle(r.querySelector('.learning-workspace-new-chat')).opacity==='1'&&getComputedStyle(r.querySelector('.learning-workspace-chevron')).display!=='none'}"), "hover_reveals_native_new_chat_icon_and_expand_arrow", result)
    browser.snapshot("workspace-hover")
    browser.command("screenshot", "--filename=" + str(directory / "workspace-hover.png"))
    action("await " + row + ".getByRole('button',{name:'工作区“数学”的操作',exact:true}).click();"
           "await page.getByRole('menuitem',{name:'置顶',exact:true}).waitFor();")
    check(browser.evaluate("()=>!document.querySelector('.learning-workspace-hover')"), "workspace_menu_suppresses_hover_card", result)
    browser.command("press", "Escape")
    run_course_chat(browser, instance, math, english, row, baseline, old_chat, action, result)


def run_hover_feedback(browser, math, row, selector, action, directory, result):
    # Reproduce the currently running older backend's two-field course payload.
    action("await page.route('**/api/v1/courses',async route=>{const response=await route.fetch();"
           "const data=await response.json();await route.fulfill({response,json:data.map(({course_id,title})=>({course_id,title}))});});"
           "await page.reload();await page.getByRole('button',{name:'数学',exact:true}).waitFor();")
    action("await " + row + ".getByRole('button',{name:'数学',exact:true}).click();"
           "await page.mouse.move(600,50);"
           "await " + row + ".getByRole('button',{name:'数学',exact:true}).hover();"
           "await page.locator('.learning-workspace-hover').waitFor();")
    before = browser.evaluate("()=>{const row=document.querySelector(" + json.dumps(selector) + ").querySelector('.learning-workspace-row');"
                              "return {row:getComputedStyle(row).backgroundColor,button:getComputedStyle(row.querySelector('.learning-workspace-name')).backgroundColor,hover:document.querySelector('.learning-workspace-hover').innerText}}")
    browser.snapshot("hover-feedback")
    browser.command("screenshot", "--filename=" + str(directory / "hover-feedback.png"))
    action("await page.mouse.move(-10,-10);await page.evaluate(()=>window.dispatchEvent(new Event('blur')));")
    after = browser.evaluate("()=>{const row=document.querySelector(" + json.dumps(selector) + ").querySelector('.learning-workspace-row');"
                             "return {row:getComputedStyle(row).backgroundColor,buttons:Array.from(row.querySelectorAll('.learning-workspace-actions'),e=>getComputedStyle(e).opacity),hover:!!document.querySelector('.learning-workspace-hover')}}")
    result["observations"] = {"before": before, "after": after}
    checks = [
        (before["button"] == "rgba(0, 0, 0, 0)", "workspace_hover_uses_one_full_row_background"),
        (after["row"] == "rgba(0, 0, 0, 0)" and all(value == "0" for value in after["buttons"]) and not after["hover"], "leaving_window_clears_highlight_actions_and_hover_card"),
        (math["root_path"] in before["hover"] and "创建于 " in before["hover"] and "未知" not in before["hover"], "legacy_course_payload_still_resolves_actual_workspace_metadata"),
    ]
    result["assertions"] = [{"name": name, "passed": ok} for ok, name in checks]
    assert all(ok for ok, _ in checks), result["observations"]


def run_course_chat(browser, instance, math, english, row, baseline, old_chat, action, result):
    # Start from an unsent draft with multiple readable courses. The explicit
    # root-row action must create a separate chat with only the clicked course.
    old_sid = api(browser, "/desktop/default/open", {}, method="POST")["body"]["session_id"]
    envelope = {"type": "client-request", "rpcId": "workspace-draft-title", "method": "session/rename",
                "payload": {"args": {"request": {"sessionId": old_sid, "title": "旧草稿验证对话"}}}}
    renamed = browser.evaluate(
        "async()=>{const session=await(await fetch('/api/v1/session')).json();"
        "const r=await fetch('/desktop/api/session/rename',{method:'POST',headers:{'Content-Type':'application/json','X-Learning-CSRF':session.csrf_token},body:JSON.stringify("
        + json.dumps(envelope) + ")});return {status:r.status,body:await r.json()}}"
    )
    assert renamed["status"] == 200 and renamed["body"]["result"]["ok"], renamed
    added = api(browser, "/desktop/sessions/" + old_sid + "/workspaces", {"course_ids": [math["course_id"], english["course_id"]]}, method="POST")
    assert added["status"] == 200, added
    action("await " + row + ".getByRole('button',{name:'数学',exact:true}).hover();await " + row + ".getByRole('button',{name:'在“数学”中新建对话',exact:true}).dblclick();"
           "await page.waitForFunction(()=>document.querySelector('[data-composer-input]')?.innerText==='');")
    current = chat_scope(instance.directory)
    new_chats = [value for value in current["chats"] if value not in baseline["chats"]]
    check(len(new_chats) == 1 and new_chats[0][1] == math["course_id"], "course_new_chat_uses_clicked_course_and_double_click_creates_once", result)
    with sqlite3.connect(instance.directory / "data/business.sqlite3") as connection:
        payload = json.loads(connection.execute("SELECT payload FROM chat_drafts WHERE chat_id=?", (old_chat,)).fetchone()[0])
    check(payload["text"] == "切换前保留的草稿", "new_chat_persists_old_unsent_draft", result)
    check(browser.evaluate("()=>document.querySelector('[aria-label=对话工作区]').title") == "数学", "new_chat_does_not_inherit_other_read_workspaces", result)
    action("await page.getByRole('button',{name:'选择历史对话'}).click();"
           "await page.getByRole('menuitem',{name:'旧草稿验证对话',exact:true}).click();"
           "await page.waitForFunction(()=>document.querySelector('[data-composer-input]')?.innerText==='切换前保留的草稿');")
    check(browser.evaluate("()=>document.querySelector('[data-composer-input]').innerText") == "切换前保留的草稿", "reopening_old_chat_restores_draft_in_native_composer", result)
    check(api(browser, "/desktop/sessions/" + old_sid)["body"]["workspace_ids"] == [math["course_id"], english["course_id"]], "old_conversation_keeps_its_original_multi_workspace_scope", result)
    counts = instance.counts()
    check(counts["business_attempts"] == 0 and counts["real_provider_attempts"] == 0, "workspace_interactions_dispatch_no_model_request", result, counts=counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="workspace-navigation-" + time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--scope", choices=["full", "remaining", "interactions", "chat-restore", "hover-feedback"], default="full", help="Select affected interactions or reuse earlier evidence for remaining checks.")
    parser.add_argument("--browser-path", type=Path, default=Path(os.environ["LOCALAPPDATA"]) / "ms-playwright/chromium-1234/chrome-win64/chrome.exe")
    args = parser.parse_args()
    if not args.run_id.replace("-", "").isalnum():
        parser.error("safe run id required")
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    synthetic = directory / "synthetic"
    synthetic.mkdir()
    folders = [synthetic / name for name in ("数学", "英语", "新增工作区")]
    for folder in folders:
        folder.mkdir()
        (folder / "原文.txt").write_text("仅用于工作区导航验证", encoding="utf-8")
        (folder / "子目录").mkdir()
    instance = DesktopInstance(directory / "server", free_port())
    browser = Browser(directory / "browser", args.browser_path)
    picker = None
    layer = ("fixed Harness + Chromium + synthetic local directories; no native picker exercised"
             if args.scope in {"interactions", "chat-restore", "hover-feedback"}
             else "fixed Harness + Chromium + real owned Windows picker + synthetic data")
    result = {"status": "failed", "layer": layer, "real_model_calls": 0}
    try:
        instance.start()
        instance.record = {"backend_pid": instance.process.pid, "owned_pids": instance.group.pids()}
        if args.scope in {"full", "remaining"}:
            picker = CoursePicker(instance, synthetic)
        browser.command("open", instance.url + "/desktop/", "--config=" + str(browser.config))
        browser.command("run-code", "async page=>{await page.getByRole('list',{name:'工作区列表'}).waitFor();await page.locator('[data-composer-input]').waitFor();}")
        browser.snapshot("initial")
        for folder in folders[:2]:
            created = api(browser, "/courses/folder", {"path": str(folder.resolve())}, method="POST")
            assert created["status"] == 200, created
        browser.command("reload")
        browser.command("run-code", "async page=>{await page.getByRole('button',{name:'英语',exact:true}).waitFor();}")
        initial_order = rows(browser)
        if args.scope in {"interactions", "chat-restore", "hover-feedback"}:
            run_interactions(browser, instance, folders, directory, result, chat_only=args.scope == "chat-restore", hover_only=args.scope == "hover-feedback")
            result["layer"] = "fixed Harness + Chromium + synthetic local directories; no native picker exercised"
            result["status"] = "passed"
            return 0
        check(initial_order == ["未分类", "数学", "英语"], "workspace_roots_start_collapsed", result, rows=initial_order)
        browser.snapshot("before-draft")
        browser.command("run-code", "async page=>{await page.locator('[data-composer-input]').fill('导航时保留的草稿');}")
        baseline = chat_scope(instance.directory)
        scope_title = browser.evaluate("()=>document.querySelector('[aria-label=对话工作区]').title")
        if args.scope == "full":
            click(browser, 'button "数学"')
            browser.command("run-code", "async page=>{await page.getByRole('option',{name:'原文.txt',exact:true}).waitFor();}")
            check(chat_scope(instance.directory) == baseline, "browsing_preserves_chat_identity_and_read_workspaces", result)
            click(browser, 'button "工作区“英语”的操作"')
            click(browser, 'menuitem "置顶"')
            check(rows(browser) == ["英语", "未分类", "数学"], "pin_moves_workspace_to_top", result)
            click(browser, 'button "工作区“数学”的操作"')
            click(browser, 'menuitem "置顶"')
            check(rows(browser) == ["数学", "英语", "未分类"], "multiple_pins_keep_original_relative_order", result)
            click(browser, 'button "工作区“数学”的操作"')
            click(browser, 'menuitem "取消置顶"')
            check(rows(browser) == ["英语", "未分类", "数学"], "unpin_restores_original_relative_order", result)
            click(browser, 'button "工作区“未分类”的操作"')
            menu = browser.snapshot("system-menu")
            check('menuitem "置顶"' in menu and 'menuitem "移除"' not in menu and 'menuitem "重命名"' not in menu, "unclassified_menu_only_pins", result)
            browser.command("press", "Escape")
        else:
            click(browser, 'button "工作区“英语”的操作"')
            click(browser, 'menuitem "置顶"')
        click(browser, 'button "添加工作区"')
        wait_until(picker.find, timeout=20)
        picker.finish(None)
        check(rows(browser) == ["英语", "未分类", "数学"], "native_picker_cancel_creates_nothing", result)
        click(browser, 'button "添加工作区"')
        wait_until(picker.find, timeout=20)
        picker.finish(folders[2])
        browser.command("run-code", "async page=>{await page.locator('.learning-workspace-row[data-selected=true]').getByRole('button',{name:'新增工作区',exact:true}).waitFor();}")
        check(chat_scope(instance.directory) == baseline, "native_picker_add_browses_without_creating_or_switching_chat", result)
        check(browser.evaluate("()=>document.querySelector('[aria-label=对话工作区]').title") == scope_title, "adding_keeps_conversation_workspace_selection", result)
        click(browser, 'button "工作区“新增工作区”的操作"')
        click(browser, 'menuitem "重命名"')
        snapshot = browser.snapshot("rename-dialog")
        browser.command("fill", browser.ref(snapshot, 'textbox "工作区名称"'), "自定义名称")
        click(browser, 'button "保存"')
        browser.command("run-code", "async page=>{await page.getByRole('list',{name:'工作区列表'}).getByRole('button',{name:'自定义名称',exact:true}).waitFor();}")
        check(folders[2].is_dir() and (folders[2] / "原文.txt").is_file(), "rename_changes_display_only", result)
        click(browser, 'button "工作区“自定义名称”的操作"')
        click(browser, 'menuitem "移除"')
        click(browser, 'button "移除工作区"')
        browser.command("run-code", "async page=>{await page.getByRole('list',{name:'工作区列表'}).getByRole('button',{name:'自定义名称',exact:true}).waitFor({state:'detached'});}")
        check((folders[2] / "原文.txt").read_text("utf-8") == "仅用于工作区导航验证" and chat_scope(instance.directory) == baseline, "remove_preserves_files_and_conversation", result)
        check(browser.evaluate("()=>document.querySelector('[data-composer-input]').innerText") == "导航时保留的草稿", "navigation_and_management_keep_current_draft", result)
        browser.command("reload")
        browser.command("run-code", "async page=>{await page.getByRole('button',{name:'英语',exact:true}).waitFor();await page.locator('[data-composer-input]').filter({hasText:'导航时保留的草稿'}).waitFor();}")
        check(rows(browser) == ["英语", "未分类", "数学"], "pins_and_removal_persist_after_reload", result)
        counts = instance.counts()
        check(counts["business_attempts"] == 0 and counts["real_provider_attempts"] == 0, "no_model_dispatch_during_workspace_operations", result, counts=counts)
        browser.snapshot("completed")
        browser.command("screenshot", "--filename=" + str(directory / "workspaces.png"), "--full-page")
        result["status"] = "passed"
    except BaseException as error:
        result["failure"] = f"{type(error).__name__}: {error}"
        try:
            browser.snapshot("failure")
        except Exception:
            pass
    finally:
        if picker:
            result["picker"] = picker.evidence
            if picker.dialog:
                picker.finish(None)
            picker.kernel.CloseHandle(picker.handle)
        result["browser_cleanup"] = browser.close()
        result["server_cleanup"] = instance.stop()
        write_json(directory / "result.json", result)
    print(json.dumps({"status": result["status"], "failure": result.get("failure"), "checks": len(result.get("assertions", [])), "evidence": str(directory / "result.json")}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
