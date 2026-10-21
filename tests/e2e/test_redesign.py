"""Redesigned MVP-A acceptance using synthetic files and a local provider.

This runner uses the project's fixed Playwright CLI, formal ASGI application and
pinned Harness. It never reads a model key, signs in to an account, or captures
real learning material. Fixture transport is not real model or human acceptance.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from test_browser import NODE, ROOT, check, http, port_closed, wait_until, write_json
from test_mvp_a import (
    Browser,
    MvpInstance,
    active_stop,
    api,
    duplicate_probe,
    video_state,
    wait_video,
)


def source_digests() -> dict[str, str]:
    paths = [ROOT / "pnpm-lock.yaml", ROOT / "backend/uv.lock", Path(__file__).resolve()]
    for folder in (
        "backend/src/learning_app",
        "frontend/src",
        "frontend/dist",
        "harness/profiles",
        "harness/plugins",
        "tests/fixtures/redesign",
    ):
        paths.extend(path for path in (ROOT / folder).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts)
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


def synthetic_assets(directory: Path, ffmpeg: Path | None) -> dict:
    spec = importlib.util.spec_from_file_location(
        "redesign_assets", ROOT / "tests/fixtures/redesign/generate.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("SYNTHETIC_GENERATOR_MISSING")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.generate(directory, ffmpeg=ffmpeg)


def click(browser: Browser, name: str) -> None:
    snapshot = browser.snapshot("before-click-" + re.sub(r"[^A-Za-z0-9_-]", "_", name))
    browser.ref(snapshot, f'button "{name}"')
    browser.command("run-code", "async(page)=>{await page.getByRole('button',"
                    "{name:" + json.dumps(name) + ",exact:true}).click();}")


def fill(browser: Browser, name: str, value: str) -> None:
    snapshot = browser.snapshot("before-fill")
    browser.ref(snapshot, f'textbox "{name}"')
    browser.command("run-code", "async(page)=>{await page.getByRole('textbox',"
                    "{name:" + json.dumps(name) + ",exact:true}).fill("
                    + json.dumps(value) + ");}")


def mutation(browser: Browser, path: str, body: dict) -> dict:
    return api(browser, path, {**body, "operation_id": "redesign-" + uuid.uuid4().hex},
               method="POST")


def wait_ready(browser: Browser) -> None:
    browser.command("run-code", "async(page)=>{await page.getByRole('combobox',"
                    "{name:'选择课程',exact:true}).waitFor({state:'visible',timeout:20000});}")


def select_course(browser: Browser, course_id: str) -> None:
    browser.snapshot("before-select-course")
    browser.command("run-code", "async(page)=>{const select=page.getByRole('combobox',"
                    "{name:'选择课程',exact:true});await select.selectOption("
                    + json.dumps(course_id) + ");}")


def secret_field_paths(value: object, path: str = "") -> list[str]:
    forbidden = {"token", "access_token", "refresh_token", "authorization", "cookie",
                 "password", "api_key", "peer_token", "credentials", "secret"}
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else key
            if key.lower() in forbidden:
                found.append(child)
            found.extend(secret_field_paths(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(secret_field_paths(item, f"{path}[{index}]"))
    return found


def account_boundary(browser: Browser, result: dict) -> None:
    response = api(browser, "/account")
    check(response["status"] == 200, "official_account_status_route_is_available", result)
    fields = secret_field_paths(response["body"])
    check(not fields, "public_account_DTO_contains_no_credential_fields", result,
          forbidden_field_paths=fields, public_fields=sorted(response["body"]))
    result["official_account_login"] = {
        "status": "not_run",
        "reason": "Real account authorization requires user action; no login was simulated.",
    }


def file_operations(browser: Browser, manifest: dict, result: dict) -> str:
    root = Path(manifest["course_root"])
    response = api(browser, "/courses/folder", {"path": str(root)}, method="POST")
    check(response["status"] == 200, "existing_synthetic_folder_becomes_course", result)
    course = response["body"]
    course_id = course["course_id"]
    check(course["title"] == root.name and course["root_status"] == "ready",
          "course_name_matches_real_root_directory", result)
    prefix = f"/courses/{course_id}/files"
    listing = api(browser, prefix)["body"]
    names = {entry["name"] for entry in listing["entries"]}
    check({"子目录", "说明.txt", "公式.md", "synthetic-course.webm",
           "synthetic-frame.png", "不支持的格式.fixture"} <= names,
          "course_tree_lists_supported_and_unsupported_files", result)

    imports = Path(manifest["external_imports"])
    move_request = {
        "paths": [str(imports / "move-me.txt")],
        "operation_id": "redesign-" + uuid.uuid4().hex,
    }
    moved = api(browser, prefix + "/import", move_request, method="POST")
    check(moved["status"] == 200 and (root / "move-me.txt").is_file()
          and not (imports / "move-me.txt").exists(), "default_import_moves_selected_file", result)
    reconciled = api(browser, prefix + "/operations/" + move_request["operation_id"])
    replayed = api(browser, prefix + "/import", move_request, method="POST")
    check(reconciled["status"] == replayed["status"] == 200
          and replayed["body"] == moved["body"]
          and len(list(root.glob("move-me*.txt"))) == 1,
          "file_operation_reconciliation_and_explicit_replay_do_not_repeat_move", result)
    conflicted_id = api(browser, prefix + "/import", {
        **move_request, "paths": [str(imports / "copy-me.txt")],
    }, method="POST")
    check(conflicted_id["status"] == 409 and (imports / "copy-me.txt").is_file(),
          "same_file_operation_identity_rejects_different_input", result)
    copied = mutation(browser, prefix + "/import", {
        "paths": [str(imports / "copy-me.txt")], "mode": "copy", "destination": "子目录",
    })
    check(copied["status"] == 200 and (root / "子目录/copy-me.txt").is_file()
          and (imports / "copy-me.txt").is_file(), "explicit_copy_keeps_original_file", result)

    collision = Path(manifest["external_collisions"]) / "冲突.txt"
    original_hash = hashlib.sha256((root / "冲突.txt").read_bytes()).hexdigest()
    conflict = mutation(browser, prefix + "/import", {"paths": [str(collision)]})
    check(conflict["status"] == 200 and conflict["body"]["state"] == "conflict"
          and collision.exists()
          and hashlib.sha256((root / "冲突.txt").read_bytes()).hexdigest() == original_hash,
          "same_name_conflict_changes_neither_original_nor_source", result)
    skip = mutation(browser, prefix + "/import", {"paths": [str(collision)], "conflict": "skip"})
    check(skip["status"] == 200 and collision.exists()
          and hashlib.sha256((root / "冲突.txt").read_bytes()).hexdigest() == original_hash,
          "explicit_skip_keeps_both_existing_files", result)
    keep = mutation(browser, prefix + "/import", {
        "paths": [str(collision)], "mode": "copy", "conflict": "keep_both",
    })
    same_content = [path for path in root.glob("*.txt")
                    if path.read_bytes() == collision.read_bytes()]
    check(keep["status"] == 200 and len(same_content) == 1
          and same_content[0].name != "冲突.txt" and collision.exists(),
          "explicit_keep_both_creates_distinct_filename", result)
    replace = mutation(browser, prefix + "/import", {
        "paths": [str(collision)], "mode": "copy", "conflict": "replace",
    })
    check(replace["status"] == 200 and collision.exists()
          and (root / "冲突.txt").read_bytes() == collision.read_bytes(),
          "explicit_replace_changes_only_selected_destination", result)

    created = mutation(browser, prefix + "/directory", {"path": "", "name": "整理目录"})
    check(created["status"] == 200 and (root / "整理目录").is_dir(),
          "new_directory_is_real_on_disk", result)
    opened = api(browser, prefix + "/open", {"path": "rename-source.webm"}, method="POST")
    check(opened["status"] == 200 and opened["body"]["asset_id"],
          "video_open_allocates_stable_resource_identity", result)
    identity = opened["body"]
    renamed = mutation(browser, prefix + "/rename", {
        "path": "rename-source.webm", "name": "renamed-video.webm",
    })
    relocated = mutation(browser, prefix + "/move", {
        "path": "renamed-video.webm", "destination": "整理目录",
    })
    verified = api(browser, prefix + "/open", {
        "path": "整理目录/renamed-video.webm",
    }, method="POST")
    check(renamed["status"] == relocated["status"] == verified["status"] == 200
          and verified["body"]["asset_id"] == identity["asset_id"]
          and verified["body"]["source_version"] == identity["source_version"]
          and (root / "整理目录/renamed-video.webm").is_file(),
          "rename_and_move_preserve_resource_and_source_version", result)

    sentinel = Path(manifest["outside_sentinel"])
    before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    escape = api(browser, prefix + "/open", {"path": "../outside-sentinel.txt"}, method="POST")
    absolute = api(browser, prefix + "/open", {"path": str(sentinel)}, method="POST")
    check(escape["status"] >= 400 and absolute["status"] >= 400
          and hashlib.sha256(sentinel.read_bytes()).hexdigest() == before,
          "relative_and_absolute_path_escape_are_rejected", result)

    link = root / "outside-link.txt"
    try:
        link.symlink_to(sentinel)
    except (OSError, NotImplementedError) as error:
        result.setdefault("not_run", []).append({
            "name": "symlink_escape_is_rejected", "reason": type(error).__name__,
        })
    else:
        linked = api(browser, prefix + "/open", {"path": link.name}, method="POST")
        check(linked["status"] >= 400 and sentinel.read_text("utf-8").startswith("SYNTHETIC-OUTSIDE"),
              "symlink_escape_is_rejected", result)
    return course_id


def open_material(browser: Browser, path: str) -> None:
    snapshot = browser.snapshot("before-open-material")
    if Path(path).name not in snapshot:
        raise AssertionError("Expected synthetic file is absent from the current tree snapshot")
    selector = "[data-file-path=" + json.dumps(path, ensure_ascii=False) + "]"
    browser.command("run-code", "async(page)=>{await page.locator("
                    + json.dumps(selector) + ").click();}")


def activate_tab(browser: Browser, name: str) -> None:
    browser.snapshot("before-select-tab")
    browser.command("run-code", "async(page)=>{await page.getByRole('tab',"
                    "{name:" + json.dumps(name) + ",exact:true}).click();}")


def select_history(browser: Browser, chat_id: str) -> None:
    browser.snapshot("before-select-history")
    browser.command("run-code", "async(page)=>{await page.getByRole('combobox',"
                    "{name:'历史对话',exact:true}).selectOption(" + json.dumps(chat_id) + ");"
                    "await page.getByRole('textbox',{name:'问题或追问',exact:true})"
                    ".waitFor({state:'visible',timeout:20000});}")


def wait_video_condition(browser: Browser, condition: str) -> None:
    browser.command("run-code", "async(page)=>{await page.waitForFunction(()=>{"
                    "const v=document.querySelector('video');return v&&(" + condition
                    + ")},{},{timeout:20000});}")


def keyboard_and_tabs(browser: Browser, course_id: str, result: dict) -> None:
    open_material(browser, "synthetic-course.webm")
    wait_video(browser)
    check(video_state(browser)["src"].find("/api/v1/videos/") >= 0,
          "video_uses_authenticated_Range_route_without_whole_file_Blob", result)
    browser.snapshot("before-player-keyboard")
    browser.command("run-code", "async(page)=>{await page.getByLabel('视频画面',"
                    "{exact:true}).focus();await page.locator('video').evaluate(v=>{"
                    "v.pause();v.currentTime=5});await page.keyboard.press('ArrowRight');}")
    wait_video_condition(browser, "v.paused&&Math.abs(v.currentTime-7)<0.25")
    check(True, "short_right_arrow_seeks_two_seconds", result)
    browser.command("run-code", "async(page)=>{await page.keyboard.press('ArrowLeft');}")
    wait_video_condition(browser, "Math.abs(v.currentTime-5)<0.25")
    check(True, "short_left_arrow_seeks_back_two_seconds", result)
    browser.command("run-code", "async(page)=>{await page.locator('video').evaluate(v=>"
                    "v.playbackRate=1.25);await page.keyboard.down('ArrowRight');"
                    "await page.waitForFunction(()=>document.querySelector('video').playbackRate===2);"
                    "await page.keyboard.up('ArrowRight');}")
    wait_video_condition(browser, "v.playbackRate===1.25")
    check(abs(video_state(browser)["time"] - 5) < 0.25,
          "long_right_temporarily_doubles_rate_without_extra_short_seek", result)

    browser.command("run-code", "async(page)=>{await page.keyboard.press('m');}")
    muted = browser.evaluate("()=>document.querySelector('video').muted")
    browser.command("run-code", "async(page)=>{await page.keyboard.press('m');}")
    check(muted != browser.evaluate("()=>document.querySelector('video').muted"),
          "M_key_toggles_mute", result)

    fill(browser, "问题或追问", "输入区保留 M 和空格")
    before = video_state(browser)
    browser.command("run-code", "async(page)=>{const input=page.getByRole('textbox',"
                    "{name:'问题或追问',exact:true});await input.focus();"
                    "await page.keyboard.press('Space');await page.keyboard.press('m');"
                    "await page.keyboard.press('ArrowLeft');await page.keyboard.press('ArrowRight');}")
    after = video_state(browser)
    check(after["paused"] == before["paused"] and abs(after["time"] - before["time"]) < 0.25
          and browser.evaluate("()=>document.querySelector('#question-input').value").endswith(" m"),
          "text_input_keeps_space_M_and_cursor_keys_without_player_commands", result)
    fill(browser, "问题或追问", "")
    volume_before = browser.evaluate("()=>document.querySelector('video').volume")
    browser.command("run-code", "async(page)=>{await page.getByLabel('视频画面',{exact:true}).hover();"
                    "await page.getByLabel('音量控制区域',{exact:true}).hover();"
                    "await page.mouse.wheel(0,100);}")
    wait_video_condition(browser, "v.volume<" + str(volume_before))
    check(True, "nonfullscreen_volume_control_wheel_adjusts_volume", result)

    browser.command("run-code", "async(page)=>{await page.locator('video').evaluate(async v=>{"
                    "v.playbackRate=1;v.currentTime=2;await v.play()});}")
    open_material(browser, "说明.txt")
    wait_video_condition(browser, "v.paused")
    stored_time = video_state(browser)["time"]
    activate_tab(browser, "synthetic-course.webm")
    wait_video_condition(browser, "!v.paused")
    check(video_state(browser)["time"] >= stored_time - 0.25,
          "switching_away_pauses_and_returning_restores_previous_playing_state", result)
    browser.command("run-code", "async(page)=>{await page.locator('video').evaluate(v=>v.pause());}")
    activate_tab(browser, "说明.txt")
    activate_tab(browser, "synthetic-course.webm")
    wait_video_condition(browser, "v.paused")
    check(True, "previously_paused_video_stays_paused_when_tab_returns", result)
    chats = api(browser, f"/courses/{course_id}/chats")["body"]
    check(len(chats) == 1, "material_tabs_do_not_create_extra_conversations", result)


def fullscreen_ai(browser: Browser, result: dict) -> None:
    browser.snapshot("before-fullscreen")
    browser.command("run-code", "async(page)=>{await page.locator('video').evaluate(async v=>{"
                    "v.currentTime=2;await v.play()});await page.getByLabel('视频画面',"
                    "{exact:true}).dblclick();await page.waitForFunction(()=>!!document.fullscreenElement);}")
    browser.command("run-code", "async(page)=>{await page.keyboard.press('Control+Shift+A');"
                    "await page.getByRole('textbox',{name:'问题或追问',exact:true})"
                    ".waitFor({state:'visible'});}")
    check(browser.evaluate("()=>!!document.fullscreenElement") and not video_state(browser)["paused"],
          "fullscreen_AI_shortcut_keeps_workspace_fullscreen_and_video_playing", result)
    before_volume = browser.evaluate("()=>document.querySelector('video').volume")
    browser.command("run-code", "async(page)=>{const box=await page.getByLabel('视频画面',"
                    "{exact:true}).boundingBox();await page.mouse.move(box.x+box.width/2,"
                    "box.y+box.height/2);await page.mouse.wheel(0,100);}")
    wait_video_condition(browser, "v.volume<" + str(before_volume))
    check(True, "fullscreen_video_area_wheel_adjusts_volume", result)
    browser.command("run-code", "async(page)=>{const box=await page.getByRole('textbox',"
                    "{name:'问题或追问',exact:true}).boundingBox();await page.mouse.move("
                    "box.x+box.width/2,box.y+box.height/2);await page.mouse.wheel(0,-100);}")
    volume = browser.evaluate("()=>document.querySelector('video').volume")
    browser.command("run-code", "async(page)=>{const box=await page.getByRole('textbox',"
                    "{name:'问题或追问',exact:true}).boundingBox();await page.mouse.move("
                    "box.x+box.width/2,box.y+box.height/2);await page.mouse.wheel(0,100);}")
    check(browser.evaluate("()=>document.querySelector('video').volume") == volume,
          "fullscreen_AI_wheel_does_not_change_video_volume", result)
    browser.command("run-code", "async(page)=>{await page.keyboard.press('Control+Shift+A');}")
    check(browser.evaluate("()=>!!document.fullscreenElement") and not video_state(browser)["paused"],
          "closing_fullscreen_AI_returns_to_video_without_cancel_or_pause", result)
    browser.command("run-code", "async(page)=>{await page.keyboard.press('Escape');"
                    "await page.waitForFunction(()=>!document.fullscreenElement);"
                    "await page.keyboard.press('Control+Shift+A');}")


def confirmed_image(browser: Browser, index: int) -> tuple[bytes, dict]:
    return_data = browser.evaluate("async()=>{const i=document.querySelectorAll("
        "'img[alt^=\"待发送的确认图片\"]')[" + str(index - 1) + "];if(!i)throw new Error('IMAGE_MISSING');"
        "const r=await fetch(i.src);return {src:i.src,width:i.naturalWidth,height:i.naturalHeight,"
        "bytes:Array.from(new Uint8Array(await r.arrayBuffer()))}}")
    return bytes(return_data.pop("bytes")), return_data


def drag_capture(browser: Browser, box: dict) -> None:
    browser.snapshot("before-workspace-capture")
    browser.command("run-code", "async(page)=>{await page.keyboard.press('Control+Shift+S');"
                    "await page.getByRole('dialog',{name:'框选软件内截图',exact:true})"
                    ".waitFor({state:'visible'});await page.getByText('按住鼠标拖动，松开加入草稿 · Esc 取消',"
                    "{exact:true}).waitFor({state:'visible',timeout:20000});"
                    "await page.mouse.move(" + str(box["x"]) + "," + str(box["y"]) + ");"
                    "await page.mouse.down();await page.mouse.move("
                    + str(box["x"] + box["width"]) + "," + str(box["y"] + box["height"])
                    + ",{steps:8});await page.mouse.up();}")


def screenshot_and_transport(browser: Browser, instance: MvpInstance, result: dict) -> str:
    from PIL import Image

    baseline = instance.counts()
    question = "草稿保留：请解答我确认的合成题图。"
    fill(browser, "问题或追问", question)
    browser.command("run-code", "async(page)=>{const box=page.getByRole('textbox',"
                    "{name:'问题或追问',exact:true});await box.dispatchEvent('compositionstart',{data:'中'});"
                    "await box.press('Control+Enter');await box.dispatchEvent('compositionend',{data:'中'});}")
    check(instance.counts() == baseline,
          "IME_composition_Ctrl_Enter_does_not_submit_current_draft", result)
    box = browser.evaluate("()=>{const v=document.querySelector('video'),r=v.getBoundingClientRect();"
        "const scale=Math.min(r.width/v.videoWidth,r.height/v.videoHeight);"
        "return {x:r.x+(r.width-v.videoWidth*scale)/2+205*scale,"
        "y:r.y+(r.height-v.videoHeight*scale)/2+125*scale,width:140*scale,height:70*scale}}")
    drag_capture(browser, box)
    browser.command("run-code", "async(page)=>{await page.getByAltText('待发送的确认图片 1',"
                    "{exact:true}).waitFor({state:'visible'});}")
    data, metadata = confirmed_image(browser, 1)
    with Image.open(io.BytesIO(data)) as image:
        dimensions = image.size
        corner = image.convert("RGB").getpixel((2, 2))
        colors = image.convert("RGB").get_flattened_data()
        forbidden = sum(1 for red, green, blue in colors
                        if (red > 235 and blue > 235 and green < 30)
                        or (green > 235 and blue > 235 and red < 30))
    check(abs(dimensions[0] - box["width"]) <= 2 and abs(dimensions[1] - box["height"]) <= 2
          and corner[1] > corner[0] and corner[1] > corner[2] and forbidden == 0,
          "workspace_drag_publishes_selected_video_pixels_without_outer_frame", result,
          dimensions=dimensions, corner_rgb=corner, forbidden_outer_pixels=forbidden)
    check(video_state(browser)["paused"] and instance.counts() == baseline
          and browser.evaluate("()=>document.querySelector('#question-input').value") == question,
          "drag_release_pauses_video_preserves_text_and_does_not_dispatch_model", result)
    check(browser.evaluate("()=>document.activeElement?.id") == "question-input",
          "drag_release_expands_AI_and_focuses_question_input", result)
    image_hash = hashlib.sha256(data).hexdigest()
    result["confirmed_video_selection"] = {
        "sha256": image_hash, "dimensions": dimensions,
        "published_as_attachment": "/attachments/" in metadata["src"],
    }
    browser.snapshot("before-cancel-next-capture")
    browser.command("run-code", "async(page)=>{await page.keyboard.press('Control+Shift+S');"
                    "await page.getByRole('dialog',{name:'框选软件内截图',exact:true})"
                    ".waitFor({state:'visible'});await page.keyboard.press('Escape');}")
    check(browser.evaluate("()=>({count:document.querySelectorAll("
          "'img[alt^=\"待发送的确认图片\"]').length,text:document.querySelector('#question-input').value})")
          == {"count": 1, "text": question} and instance.counts() == baseline,
          "cancelling_capture_preserves_existing_attachment_and_text", result)
    instance.control("scenario", scenario={"text_chunks": [
        "合成选区文字用于引用，后半句不应被引用。\n\n"
        "由 $x^2-5x+6=(x-2)(x-3)$，可得 $x=2$ 或 $x=3$。\n\n"
        "分式示例：$\\frac{x+1}{x-1}=2$。\n\n这是合成验证回答。",
    ]})
    click(browser, "发送")
    browser.wait_text("这是合成验证回答。")
    generation = instance.generation_for(question)
    wait_until(lambda: instance.generation_for(question)["state"] == "completed", timeout=25)
    native = instance.control("inspect", generation_id=generation["generation_id"])
    images = native["boundary"][0]["images"]
    check(len(images) == 1 and images[0]["sha256"] == image_hash
          and images[0]["dimensions"] == list(dimensions),
          "actual_Host_outbound_contains_exact_workspace_selection_only", result, images=images)
    browser.command("run-code", "async(page)=>{await page.locator('.katex-html').first()"
                    ".waitFor({state:'visible'});}")
    check(not browser.evaluate("()=>document.documentElement.scrollWidth>innerWidth"),
          "Chinese_answer_and_math_do_not_overflow_application_width", result)
    return generation["generation_id"]


def answer_capture(browser: Browser, instance: MvpInstance, result: dict) -> None:
    from PIL import Image

    before = instance.counts()
    paragraph = "合成选区文字用于引用，后半句不应被引用。"
    browser.snapshot("before-answer-capture")
    browser.command("run-code", "async(page)=>{await page.getByText("
                    + json.dumps(paragraph) + ",{exact:true}).scrollIntoViewIfNeeded();}")
    box = browser.evaluate("()=>{const element=[...document.querySelectorAll('.markdown-answer p')]"
        ".find(e=>e.textContent===" + json.dumps(paragraph) + ");const r=element.getBoundingClientRect();"
        "return {x:r.x+2,y:r.y+1,width:Math.min(r.width-4,260),height:Math.min(r.height-2,58)}}")
    drag_capture(browser, box)
    browser.command("run-code", "async(page)=>{await page.getByAltText('待发送的确认图片 1',"
                    "{exact:true}).waitFor({state:'visible'});}")
    data, _ = confirmed_image(browser, 1)
    with Image.open(io.BytesIO(data)) as image:
        dimensions = image.size
        colors = image.convert("RGB").getcolors(maxcolors=1_000_000) or []
        variety = len(colors)
    check(abs(dimensions[0] - box["width"]) <= 2 and abs(dimensions[1] - box["height"]) <= 2
          and variety > 8 and instance.counts() == before,
          "workspace_selection_captures_visible_AI_text_without_model_dispatch", result,
          dimensions=dimensions, distinct_colors=variety)
    # Keep this second confirmed selection in the persistent draft for reload coverage.
    result["confirmed_answer_selection"] = {
        "sha256": hashlib.sha256(data).hexdigest(), "dimensions": dimensions,
    }


def mouse_select(browser: Browser, coordinates: dict) -> None:
    browser.snapshot("before-real-mouse-selection")
    browser.command("run-code", "async(page)=>{await page.mouse.move("
                    + str(coordinates["x1"]) + "," + str(coordinates["y1"]) + ");"
                    "await page.mouse.down();await page.mouse.move("
                    + str(coordinates["x2"]) + "," + str(coordinates["y2"])
                    + ",{steps:12});await page.mouse.up();}")


def file_ui_unknown_receipt(browser: Browser, manifest: dict, course_id: str,
                            chat_id: str, result: dict) -> None:
    source = Path(manifest["external_imports"]) / "ui-import.txt"
    root = Path(manifest["course_root"])
    click(browser, "导入资料")
    defaults = browser.evaluate("()=>({mode:document.querySelector('[aria-label=\"导入方式\"]').value,"
                               "conflict:document.querySelector('[aria-label=\"同名冲突\"]').value})")
    check(defaults == {"mode": "move", "conflict": "ask"},
          "file_import_UI_defaults_to_move_and_explicit_conflict_question", result)
    fill(browser, "资料导入路径", str(source))
    browser.command("run-code", "async(page)=>{await page.getByRole('combobox',"
                    "{name:'导入方式',exact:true}).selectOption('copy');"
                    "let dropped=false;await page.route('**/api/v1/courses/"
                    + course_id + "/files/import',async route=>{"
                    "await page.evaluate(()=>sessionStorage.setItem('redesign-import-count',"
                    "String(Number(sessionStorage.getItem('redesign-import-count')||'0')+1)));"
                    "if(!dropped){dropped=true;await route.fetch();await route.abort('connectionfailed')}"
                    "else await route.continue();});}")
    click(browser, "确认")
    browser.wait_text("结果尚未确认，先核对本次操作。")
    check(source.is_file() and (root / source.name).is_file()
          and browser.evaluate("()=>sessionStorage.getItem('redesign-import-count')") == "1",
          "file_import_lost_receipt_preserves_source_and_pending_operation", result)
    browser.command("reload")
    wait_ready(browser)
    select_course(browser, course_id)
    select_history(browser, chat_id)
    browser.wait_text("结果尚未确认，先核对本次操作。")
    check(browser.evaluate("()=>sessionStorage.getItem('redesign-import-count')") == "1",
          "file_import_unknown_after_reload_does_not_resubmit", result)
    click(browser, "核对文件操作")
    browser.command("run-code", "async(page)=>{await page.getByRole('button',"
                    "{name:'核对文件操作',exact:true}).waitFor({state:'hidden'});}")
    check(browser.evaluate("()=>sessionStorage.getItem('redesign-import-count')") == "1"
          and len(list(root.glob("ui-import*.txt"))) == 1,
          "file_operation_reconciliation_uses_saved_identity_without_second_import", result)


def reference_and_restore(browser: Browser, instance: MvpInstance, course_id: str,
                          chat_id: str, result: dict) -> None:
    question = "引用后的补充问题保持独立。"
    fill(browser, "问题或追问", question)
    target = "合成选区文字"
    coordinates = browser.evaluate("()=>{const root=document.querySelector('.messages');"
        "const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);let n;while(n=walker.nextNode()){"
        "const i=n.data.indexOf(" + json.dumps(target) + ");if(i<0)continue;"
        "n.parentElement.scrollIntoView({block:'center'});const a=document.createRange(),b=document.createRange();"
        "a.setStart(n,i);a.collapse(true);b.setStart(n,i+" + str(len(target)) + ");b.collapse(true);"
        "const l=a.getBoundingClientRect(),r=b.getBoundingClientRect();return {"
        "x1:l.x+.25,y1:l.y+l.height/2,x2:r.x+.25,y2:r.y+r.height/2}}throw new Error('TEXT_NOT_FOUND')}")
    mouse_select(browser, coordinates)
    selected = browser.evaluate("()=>getSelection()?.toString()")
    check(selected == target and api(browser, f"/chats/{chat_id}/draft")["body"]["references"] == [],
          "ordinary_mouse_text_selection_does_not_add_reference", result, selected=selected)
    click(browser, "添加到对话")
    browser.command("run-code", "async(page)=>{await page.waitForFunction(async chat=>{const d="
                    "await(await fetch('/api/v1/chats/'+chat+'/draft')).json();return d.references.length===1},"
                    + json.dumps(chat_id) + ");}")
    draft = api(browser, f"/chats/{chat_id}/draft")["body"]
    check(draft["references"][0]["text"] == target
          and draft["references"][0]["markdown"] == target and draft["text"] == question,
          "selected_text_becomes_source_bound_card_without_replacing_question", result)

    browser.command("run-code", "async(page)=>{await page.locator('.messages .selection-formula')"
                    ".last().scrollIntoViewIfNeeded();}")
    coordinates = browser.evaluate("()=>{const formula=[...document.querySelectorAll("
        "'.messages .selection-formula')].find(e=>e.querySelector('annotation')?.textContent==="
        + json.dumps("\\frac{x+1}{x-1}=2") + ");"
        "if(!formula)throw new Error('FRACTION_NOT_FOUND');const offset=Number(formula.dataset.formulaStart);"
        "const spans=[...formula.querySelectorAll('.katex-html [data-selection-start]')]"
        ".filter(e=>Number(e.dataset.selectionStart)>=offset+6&&Number(e.dataset.selectionEnd)<=offset+9);"
        "const rects=spans.map(e=>e.getBoundingClientRect()).filter(r=>r.width>0);"
        "if(!rects.length)throw new Error('NUMERATOR_NOT_FOUND');const left=Math.min(...rects.map(r=>r.left)),"
        "right=Math.max(...rects.map(r=>r.right)),top=Math.min(...rects.map(r=>r.top)),"
        "bottom=Math.max(...rects.map(r=>r.bottom));return {x1:left+.2,y1:(top+bottom)/2,"
        "x2:right-.2,y2:(top+bottom)/2}}")
    mouse_select(browser, coordinates)
    numerator = browser.evaluate("()=>getSelection()?.toString()")
    click(browser, "添加到对话")
    browser.command("run-code", "async(page)=>{await page.waitForFunction(async chat=>{const d="
                    "await(await fetch('/api/v1/chats/'+chat+'/draft')).json();return d.references.length===2},"
                    + json.dumps(chat_id) + ");}")
    draft = api(browser, f"/chats/{chat_id}/draft")["body"]
    quote = draft["references"][1]
    check(quote["markdown"] == "$x+1$" and quote["source_end"] - quote["source_start"] == 3
          and draft["text"] == question,
          "real_mouse_fraction_numerator_maps_only_selected_LaTeX_to_card", result,
          selected=numerator, markdown=quote["markdown"])
    click(browser, "移除引用 1")
    click(browser, "保存草稿")
    browser.wait_text("草稿已保存")
    browser.command("screenshot", f"--filename={browser.directory / 'learning-light.png'}", "--full-page")
    click(browser, "切换深色")
    browser.command("screenshot", f"--filename={browser.directory / 'learning-dark.png'}", "--full-page")
    check(browser.evaluate("()=>document.querySelector('.app-shell').classList.contains('dark')")
          and not browser.evaluate("()=>document.documentElement.scrollWidth>innerWidth"),
          "manual_dark_theme_preserves_populated_learning_layout", result)
    click(browser, "切换浅色")
    browser.command("reload")
    wait_ready(browser)
    select_course(browser, course_id)
    select_history(browser, chat_id)
    browser.command("run-code", "async(page)=>{await page.getByAltText('待发送的确认图片 1',"
                    "{exact:true}).waitFor({state:'visible'});}")
    restored = api(browser, f"/chats/{chat_id}/draft")["body"]
    check(restored["text"] == question and len(restored["references"]) == 1
          and restored["references"][0]["markdown"] == "$x+1$"
          and len(restored["attachment_ids"]) == 1,
          "confirmed_selection_text_and_formula_card_restore_after_reload", result)
    instance.control("scenario", scenario={"text_chunks": ["已收到当前草稿中保留的局部引用与确认选区。"]})
    click(browser, "发送")
    browser.wait_text("已收到当前草稿中保留的局部引用与确认选区。")
    chat = api(browser, f"/chats/{chat_id}")["body"]
    user = next(message for message in chat["messages"]
                if message["role"] == "user" and message["text"] == question)
    check(len(user["references"]) == 1 and user["references"][0]["markdown"] == "$x+1$",
          "submitted_history_retains_exact_reference_card_and_source", result)


def lost_receipt(browser: Browser, instance: MvpInstance, course_id: str,
                 chat_id: str, result: dict) -> None:
    before = instance.counts()
    answer = "这是后台已接受的合成提交，丢失回执只应核对。"
    instance.control("scenario", scenario={"text_chunks": [answer]})
    fill(browser, "问题或追问", "重设计合成丢失回执验证。")
    browser.snapshot("before-lost-receipt")
    browser.command("run-code", "async(page)=>{await page.route('**/api/v1/submissions',"
                    "async route=>{await route.fetch();await route.abort('connectionfailed');},{times:1});}")
    click(browser, "发送")
    browser.wait_text("这次提交的回执尚未确认。先核对，不会自动重发。")
    uncertain = instance.counts()
    check(uncertain["submissions"] == before["submissions"] + 1,
          "lost_submission_receipt_retains_original_submission_identity", result)
    browser.command("reload")
    wait_ready(browser)
    select_course(browser, course_id)
    select_history(browser, chat_id)
    browser.wait_text("这次提交的回执尚未确认。先核对，不会自动重发。")
    check(instance.counts() == uncertain, "reload_with_unknown_receipt_does_not_resubmit", result)
    click(browser, "核对提交")
    browser.wait_text(answer)
    check(instance.counts() == uncertain,
          "explicit_submission_reconciliation_finds_original_without_second_dispatch", result)


def course_and_page_background(browser: Browser, instance: MvpInstance, course_a: str,
                               course_b: str, chat_id: str, result: dict) -> None:
    partial = "旧课程的合成正文正在后台生成。"
    tail = "关闭页面后仍继续完成，并保存到原课程对话。"
    question = "重设计跨课程与关闭页面归属验证。"
    instance.control("scenario", scenario={"text_chunks": [partial, tail],
                     "pause_after_event": "text_delta", "gate": "redesign-background"})
    fill(browser, "问题或追问", question)
    click(browser, "发送")
    browser.wait_text(partial)
    generation = instance.generation_for(question)
    instance.control("wait_gate", generation_id=generation["generation_id"])
    select_course(browser, course_b)
    continued = instance.generation_for(question)
    check(continued["generation_id"] == generation["generation_id"]
          and continued["state"] not in {"cancelled", "completed"},
          "switching_courses_keeps_old_active_generation_on_original_chat", result)
    select_course(browser, course_a)
    browser.wait_text(partial)
    selected = browser.evaluate("()=>document.querySelector('[aria-label=\"历史对话\"]').value")
    check(selected == chat_id, "returning_to_course_reuses_current_conversation", result)
    browser.command("goto", "about:blank")
    instance.control("release_gate", gate="redesign-background")
    wait_until(lambda: instance.generation_for(question)["state"] == "completed", timeout=30)
    browser.command("goto", instance.url)
    wait_ready(browser)
    select_course(browser, course_a)
    select_history(browser, chat_id)
    browser.wait_text(partial + tail)
    check(instance.generation_for(question)["body"] == partial + tail,
          "closing_webpage_does_not_stop_background_generation_or_lose_saved_answer", result)


def snapshot_smoke(directory: Path, executable: Path, port: int, ffmpeg: Path | None) -> dict:
    """Real Chromium rasterization on an isolated same-origin synthetic page."""
    from PIL import Image
    from learning_app.processes import OwnedProcessGroup

    manifest = synthetic_assets(directory / "synthetic", ffmpeg)
    def fs_url(path: Path | str) -> str:
        return "/@fs/" + Path(path).resolve().as_posix()
    video = fs_url(manifest["media"]["video"])
    picture = fs_url(Path(manifest["media"]["frame"]).parent / "paste-1.png")
    katex_js = fs_url(ROOT / "frontend/node_modules/katex/dist/katex.mjs")
    katex_css = fs_url(ROOT / "frontend/node_modules/katex/dist/katex.min.css")
    page = directory / "synthetic-snapshot.html"
    page.write_text(
        "<!doctype html><html><head><meta charset='utf-8'><link rel='stylesheet' href='"
        + katex_css + "'><style>body{margin:0;background:white;font-family:'Microsoft YaHei UI',sans-serif}"
        "#fixture-root{position:relative;width:1000px;height:700px;background:white;color:#111}"
        "h1{position:absolute;left:20px;top:10px}video{position:absolute;left:20px;top:100px;"
        "width:640px;height:360px}#answer{position:absolute;left:690px;top:100px;width:280px;"
        "font-size:22px;line-height:1.6}#picture{position:absolute;left:20px;top:500px;width:240px;"
        "height:120px}#formula{font-size:24px}</style></head><body><main id='fixture-root'>"
        "<h1>全部为合成内容 · 截图像素验证</h1><video id='video' muted preload='auto' src='"
        + video + "'></video><article id='answer'><p>合成回答文字与公式，检查实际截图包含文字。</p>"
        "<div id='formula'></div></article><img id='picture' src='" + picture + "'></main>"
        "<script type='module'>import {captureWorkspace} from '/src/media/workspaceSnapshot.ts';"
        "import katex from '" + katex_js + "';"
        "katex.render('\\\\frac{x+1}{x-1}=2',document.querySelector('#formula'));"
        "window.captureSynthetic=()=>captureWorkspace(document.querySelector('#fixture-root'));"
        "window.syntheticReady=true;</script></body></html>",
        encoding="utf-8",
    )
    group = OwnedProcessGroup("redesign-snapshot-smoke")
    browser = Browser(directory / "browser", executable)
    result = {"id": "MVP-A-real-browser-snapshot-smoke", "status": "running",
              "evidence_level": "real Chromium + Vite transformed source + synthetic DOM/video/KaTeX",
              "real_model_attempts": 0, "assertions": []}
    log = (directory / "vite.stdout.txt").open("w", encoding="utf-8")
    process = None
    try:
        process = group.spawn(
            [str(NODE), str(ROOT / "frontend/node_modules/vite/bin/vite.js"),
             "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
            cwd=ROOT / "frontend", env=os.environ, stdout=log, stderr=subprocess.STDOUT,
        )
        url = f"http://127.0.0.1:{port}" + fs_url(page)

        def ready():
            if process.poll() is not None:
                raise RuntimeError("SNAPSHOT_SMOKE_VITE_FAILED")
            try:
                return http(url)[0] == 200
            except OSError:
                return False

        wait_until(ready, timeout=30)
        browser.command("open", url, f"--config={browser.config}")
        browser.command("run-code", "async(page)=>{await page.waitForFunction(()=>"
                        "window.syntheticReady&&document.querySelector('video').readyState>=2);"
                        "await page.evaluate(()=>document.fonts.ready);}")
        browser.snapshot("synthetic-only")
        capture = browser.evaluate("async()=>{const result=await window.captureSynthetic();"
            "return {width:result.source.width,height:result.source.height,dataUrl:result.source.dataUrl}}")
        png = base64.b64decode(capture.pop("dataUrl").split(",", 1)[1])
        target = directory / "captured-synthetic.png"
        target.write_bytes(png)
        with Image.open(io.BytesIO(png)) as image:
            pixels = image.convert("RGB")
            green = pixels.getpixel((225, 225))
            photo = pixels.getpixel((30, 510))
            answer = pixels.crop((690, 100, 970, 300))
            ink = sum(1 for red, green_value, blue in answer.get_flattened_data()
                      if max(red, green_value, blue) < 150)
            math = pixels.crop((690, 220, 970, 310))
            math_ink = sum(1 for red, green_value, blue in math.get_flattened_data()
                           if max(red, green_value, blue) < 150)
            numerator = pixels.crop((690, 210, 738, 237))
            denominator = pixels.crop((690, 237, 738, 263))
            numerator_ink = sum(1 for red, green_value, blue in numerator.get_flattened_data()
                                if max(red, green_value, blue) < 150)
            denominator_ink = sum(1 for red, green_value, blue in denominator.get_flattened_data()
                                  if max(red, green_value, blue) < 150)
        check((capture["width"], capture["height"]) == (1000, 700),
              "visible_root_dimensions_are_preserved", result, observed=capture)
        check(green[1] > green[0] and green[1] > green[2],
              "actual_video_frame_has_expected_center_color", result, center_rgb=green)
        check(photo[2] > photo[0] and photo[1] > photo[0],
              "same_origin_image_is_present_in_raster", result, sample_rgb=photo)
        check(ink > 100 and math_ink > 10,
              "Chinese_text_and_KaTeX_are_nonblank_in_real_raster", result,
              answer_ink_pixels=ink, formula_ink_pixels=math_ink)
        check(numerator_ink > 15 and denominator_ink > 15,
              "fraction_numerator_and_denominator_are_both_present_in_raster", result,
              numerator_ink_pixels=numerator_ink, denominator_ink_pixels=denominator_ink)
        result["captured_png_sha256"] = hashlib.sha256(png).hexdigest()
        browser.command("screenshot", f"--filename={directory / 'browser-synthetic.png'}", "--full-page")
        result["status"] = "passed"
    except Exception as error:
        result["status"] = "failed"
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        result["browser_cleanup"] = browser.close()
        result["server_cleanup"] = group.close()
        log.close()
        result["port_closed"] = port_closed(port)
        if not result["port_closed"]:
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    return result


def main_case(directory: Path, executable: Path, port: int, ffmpeg: Path | None) -> dict:
    manifest = synthetic_assets(directory / "synthetic", ffmpeg)
    result = {
        "id": "MVP-A-redesign-browser", "status": "running",
        "evidence_level": "real Chromium + formal ASGI + pinned Host + local provider fixture",
        "real_model_attempts": None,
        "human_visual_and_learning_acceptance": "not_run; requires user participation",
        "assertions": [], "source_digests_before": source_digests(),
        "synthetic_manifest": manifest,
        "browser_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
    }
    instance = MvpInstance(directory / "server", port)
    browser = Browser(directory / "browser", executable)
    try:
        instance.start()
        browser.command("open", instance.url, f"--config={browser.config}")
        wait_ready(browser)
        browser.snapshot("initial")
        account_boundary(browser, result)
        course_id = file_operations(browser, manifest, result)
        chat_response = api(browser, f"/courses/{course_id}/chats", method="POST")
        check(chat_response["status"] == 200, "course_chat_uses_existing_business_API", result)
        chat_id = chat_response["body"]["chat_id"]
        second_course = api(browser, "/courses/folder", {
            "path": manifest["course_b_root"],
        }, method="POST")
        check(second_course["status"] == 200, "second_synthetic_course_has_independent_root", result)
        course_b = second_course["body"]["course_id"]
        # Refresh only loads the newly created course list, never an original video.
        browser.command("reload")
        wait_ready(browser)
        select_course(browser, course_id)
        select_history(browser, chat_id)
        browser.snapshot("course-file-tree")
        result["course_id"] = course_id
        file_ui_unknown_receipt(browser, manifest, course_id, chat_id, result)
        keyboard_and_tabs(browser, course_id, result)
        fullscreen_ai(browser, result)
        screenshot_and_transport(browser, instance, result)
        answer_capture(browser, instance, result)
        reference_and_restore(browser, instance, course_id, chat_id, result)
        active_stop(browser, instance, result)
        duplicate_probe(browser, instance, chat_id, result)
        lost_receipt(browser, instance, course_id, chat_id, result)
        course_and_page_background(browser, instance, course_id, course_b, chat_id, result)
        counts = instance.counts()
        result["real_model_attempts"] = counts["real_provider_attempts"]
        check(counts["real_provider_attempts"] == 0,
              "fixture_never_forwarded_to_real_provider", result, counts=counts)
        result["source_digests_after"] = source_digests()
        check(result["source_digests_before"] == result["source_digests_after"],
              "code_and_build_did_not_change_during_acceptance_run", result)
        result["status"] = "passed"
    except Exception as error:
        result["status"] = "failed"
        result["error"] = f"{type(error).__name__}: {error}"
        try:
            browser.snapshot("failure")
            browser.command("screenshot", f"--filename={browser.directory / 'failure.png'}", "--full-page")
        except Exception as capture_error:
            result["capture_error"] = type(capture_error).__name__
    finally:
        result["browser_cleanup"] = browser.close()
        result["server_cleanup"] = instance.stop()
        try:
            result["transport_counts"] = instance.counts()
            result["real_model_attempts"] = result["transport_counts"]["real_provider_attempts"]
        except Exception as count_error:
            result["transport_count_error"] = type(count_error).__name__
        if not result["server_cleanup"].get("backend_port_closed"):
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="redesign-" + time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--browser-path", type=Path)
    parser.add_argument("--ffmpeg-path", type=Path)
    parser.add_argument("--backend-port", type=int, default=18870)
    parser.add_argument("--snapshot-smoke", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("run-id只能包含字母、数字、横线和下划线")
    executable = args.browser_path or (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe"
    )
    if not executable.is_file():
        raise RuntimeError("BROWSER_MISSING: 指定项目现有Chromium，不自动下载")
    if not port_closed(args.backend_port):
        raise RuntimeError("PORT_IN_USE: 不接管其他进程")
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    operation = snapshot_smoke if args.snapshot_smoke else main_case
    record = operation(directory, executable, args.backend_port, args.ffmpeg_path)
    print(json.dumps({"status": record["status"], "evidence": str(directory / "result.json")},
                     ensure_ascii=False))
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
