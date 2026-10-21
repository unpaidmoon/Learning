"""MVP-A acceptance: real Chromium/formal Host with synthetic local provider only.

No user video, screenshot, key or provider connection is opened by this runner.
The fixture is distinct from real model mathematics and human learning acceptance.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

from test_browser import (
    Browser as ExistingBrowser,
    FixtureInstance,
    Instance,
    PYTHON,
    ROOT,
    check,
    http,
    port_closed,
    wait_until,
    write_json,
)


class Browser(ExistingBrowser):
    def wait_text(self, text: str) -> None:
        self.command(
            "run-code", "async(page)=>{await page.getByText(" + json.dumps(text)
            + ",{exact:true}).last().waitFor({state:'visible',timeout:20000});}",
        )


class MvpInstance(FixtureInstance):
    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log = (self.directory / "server.stdout.txt").open("w", encoding="utf-8")
        self.process = self.group.spawn(
            [str(PYTHON), str(Path(__file__).resolve()), "--serve-fixture",
             str(self.directory), "--backend-port", str(self.backend)],
            cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            stdout=self.log, stderr=subprocess.STDOUT,
        )

        def ready():
            if self.process.poll() is not None:
                raise RuntimeError("MVP合成Host服务启动失败，见server.stdout.txt")
            try:
                status, _, health = http(self.url + "/api/v1/health")
                return status == 200 and health["capabilities"]["question_answering"] == "ready"
            except OSError:
                return False

        wait_until(ready, timeout=45)
        return self


async def serve_fixture(directory: Path, port: int) -> int:
    import uvicorn

    from learning_app.app import create_app
    from learning_app.config import AppConfig
    from learning_app.execution.adapter import ExecutionAdapter

    adapter = ExecutionAdapter(ROOT, directory / "execution", mode="fixture", timeout=120)
    await adapter.open()
    application = create_app(
        AppConfig(mode="start", backend_port=port, frontend_port=port,
                  data_dir=directory / "data", runtime_dir=directory / "runtime",
                  harness_enabled=True, instance_id="mvp-fixture-" + directory.name),
        execution=adapter,
    )
    server = uvicorn.Server(uvicorn.Config(
        application, host="127.0.0.1", port=port, access_log=False,
    ))
    loop = asyncio.get_running_loop()

    async def control(command: dict) -> dict:
        operation = command["operation"]
        if operation == "scenario":
            adapter.boundary.set_scenario(command["scenario"])
            return {"scenario_installed": True}
        if operation == "release_gate":
            adapter.boundary.release_event(command["gate"])
            return {"gate_released": True}
        generation_id = command["generation_id"]
        if operation == "wait_gate":
            event = await asyncio.to_thread(
                adapter.boundary.wait_for_event, "gate_waiting", 15,
                generation_id=generation_id,
            )
            return {"fault_point_event": event}
        if operation == "inspect":
            binding = application.state.service._binding_for(generation_id)
            native = await adapter.reconcile(binding)
            rows = [row for row in adapter.boundary.records
                    if row["generation_id"] == generation_id]
            return {
                "generation_id": generation_id, "native_status": native["status"],
                "native_body": "".join(event.text for event in native["events"]
                                       if event.kind == "snapshot"),
                "host_alive": adapter.host.process.poll() is None,
                "boundary": [{"images": row.get("images", []),
                              "cancel_observed": row.get("cancel_observed", False),
                              "status": row["status"]} for row in rows],
            }
        raise ValueError("UNKNOWN_TEST_CONTROL")

    def parent():
        for line in sys.stdin:
            if line.strip() == "shutdown":
                break
            command = json.loads(line)
            try:
                response = asyncio.run_coroutine_threadsafe(control(command), loop).result(25)
            except Exception as error:
                response = {"error": f"{type(error).__name__}: {error}"}
            write_json(directory / "control" / f"{command['id']}.json", response)
        server.should_exit = True

    threading.Thread(target=parent, daemon=True).start()
    try:
        await server.serve()
    finally:
        write_json(directory / "worker-cleanup.json", await adapter.close())
    return 0 if server.started else 1


def api(browser: Browser, path: str, body=None, *, method: str = "GET") -> dict:
    """Cookie/CSRF stay in page memory and are never returned to a transcript."""
    return browser.evaluate(
        "async()=>{const session=await(await fetch('/api/v1/session')).json();"
        "const response=await fetch('/api/v1'+" + json.dumps(path) + ","
        "{method:" + json.dumps(method) + ",headers:{'Content-Type':'application/json',"
        "'X-Learning-CSRF':session.csrf_token},body:"
        + ("undefined" if body is None else "JSON.stringify(" + json.dumps(body) + ")")
        + "});return {status:response.status,body:await response.json()}}"
    )


def click(browser: Browser, name: str) -> None:
    snapshot = browser.snapshot("before-" + re.sub(r"[^A-Za-z0-9_-]", "_", name))
    if name.endswith(".webm"):
        if name not in snapshot:
            raise AssertionError("资源未出现在最新快照")
        browser.command("run-code", "async(page)=>{await page.locator('.video-resource-list button')"
                        ".filter({hasText:" + json.dumps(name) + "}).first().click();}")
        return
    browser.ref(snapshot, f'button "{name}"')
    browser.command("run-code", "async(page)=>{await page.getByRole('button',"
                    "{name:" + json.dumps(name) + ",exact:true}).click();}")


def fill(browser: Browser, name: str, value: str) -> None:
    snapshot = browser.snapshot("before-fill")
    browser.ref(snapshot, f'textbox "{name}"')
    browser.command("run-code", "async(page)=>{await page.getByRole('textbox',"
                    "{name:" + json.dumps(name) + ",exact:true}).fill("
                    + json.dumps(value) + ");}")


def video_state(browser: Browser) -> dict:
    return browser.evaluate("()=>{const v=document.querySelector('video');return v?"
                            "{src:v.currentSrc,paused:v.paused,time:v.currentTime,"
                            "duration:v.duration,width:v.videoWidth,height:v.videoHeight,"
                            "ready:v.readyState,error:v.error?.code||null}:null}")


def wait_video(browser: Browser) -> None:
    browser.command("run-code", "async(page)=>{await page.waitForFunction(()=>{"
                    "const v=document.querySelector('video');return v&&v.readyState>=2"
                    "&&v.videoWidth===640},{},{timeout:20000});}")


def code_digests() -> dict[str, str]:
    paths = [ROOT / "pnpm-lock.yaml", ROOT / "backend/uv.lock",
             ROOT / "harness/runtime.lock.json", Path(__file__).resolve(),
             ROOT / "tests/fixtures/mvp_a/generate.py"]
    for folder in ("backend/src/learning_app", "frontend/src", "frontend/dist",
                   "harness/profiles", "harness/plugins"):
        paths.extend(path for path in (ROOT / folder).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts)
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


def main_case(directory: Path, executable: Path, port: int, ffmpeg: Path | None) -> dict:
    from PIL import Image

    spec = importlib.util.spec_from_file_location(
        "mvp_fixture_generator", ROOT / "tests/fixtures/mvp_a/generate.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    manifest = fixture.generate(directory / "synthetic", ffmpeg=ffmpeg)
    before = code_digests()
    result = {
        "id": "MVP-A-V3-browser", "status": "running",
        "evidence_level": "real Chromium + formal ASGI + pinned real Host + local provider fixture",
        "real_model_attempts": 0, "synthetic_manifest": manifest,
        "human_learning_acceptance": "requires user participation; not evaluated by automation",
        "assertions": [],
        "code_digests_before": before,
        "browser_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "playwright_cli_version": json.loads((ROOT / "node_modules/@playwright/cli/package.json")
                                              .read_text("utf-8"))["version"],
    }
    instance = MvpInstance(directory / "server", port)
    browser = Browser(directory / "browser", executable)
    try:
        instance.start()
        browser.command("open", instance.url, f"--config={browser.config}")
        browser.wait_text("后台已连接")
        snapshot = browser.snapshot("initial")
        check("P0 工程与持久化验证界面" not in snapshot,
              "learning_UI_replaces_P0_label", result)
        fill(browser, "新课程名称", "合成数学课程 A")
        click(browser, "新建")
        browser.command("run-code", "async(page)=>{await page.getByRole('combobox',"
                        "{name:'选择课程'}).locator('option').filter({hasText:'合成数学课程 A'})"
                        ".waitFor({state:'attached'});}")
        courses = api(browser, "/courses")["body"]
        course_a = next(course["course_id"] for course in courses
                        if course["title"] == "合成数学课程 A")
        browser.command("run-code", "async(page)=>{await page.getByRole('combobox',"
                        "{name:'选择课程'}).selectOption(" + json.dumps(course_a) + ");}")
        browser.snapshot("path-disclosure")
        browser.command("run-code", "async(page)=>{await page.getByText('用文件路径关联视频',"
                        "{exact:true}).click();}")
        fill(browser, "视频路径", manifest["video"])
        click(browser, "关联视频")
        browser.wait_text("synthetic-course.webm")
        assets = api(browser, f"/courses/{course_a}/videos")["body"]
        asset = assets[0]
        check(len(assets) == 1 and asset["availability"] == "available",
              "original_video_registered_without_copy", result, asset=asset)
        click(browser, "synthetic-course.webm")
        wait_video(browser)
        state = video_state(browser)
        check(state["src"].startswith(instance.url + "/api/v1/videos/")
              and not state["src"].startswith("blob:") and state["width"] == 640,
              "registered_Range_route_is_video_source_no_whole_Blob", result, video=state)
        content_path = f"/videos/{asset['asset_id']}/content?source_version={asset['source_version']}"
        ranges = browser.evaluate(
            "async()=>{const path='/api/v1'+" + json.dumps(content_path) + ";"
            "const r=await fetch(path,{headers:{Range:'bytes=0-31'}});"
            "const n=await fetch(path,{headers:{Range:'bytes=999999999-'}});"
            "return {status:r.status,bytes:(await r.arrayBuffer()).byteLength,"
            "range:r.headers.get('content-range'),invalid:n.status}}"
        )
        check(ranges["status"] == 206 and ranges["bytes"] == 32 and ranges["invalid"] == 416,
              "partial_content_and_unsatisfiable_range", result, observed=ranges)
        click(browser, "暂停视频") if not state["paused"] else None
        browser.command("run-code", "async(page)=>{await page.locator('video').evaluate(v=>{"
                        "v.currentTime=7.25;v.pause()});await page.waitForFunction(()=>"
                        "Math.abs(document.querySelector('video').currentTime-7.25)<0.2);}")
        browser.command("run-code", "async(page)=>{await page.waitForFunction(async course=>{"
                        "const rows=await(await fetch('/api/v1/courses/'+course+'/videos')).json();"
                        "return rows[0].position_ms>=7000}," + json.dumps(course_a)
                        + ",{timeout:20000});}")
        stored = api(browser, f"/courses/{course_a}/videos")["body"][0]
        check(7000 <= stored["position_ms"] <= 7500,
              "seek_and_pause_persist_integer_milliseconds", result, observed=stored)
        # Reload is an application boundary: no source auto-opens on course entry.
        browser.command("reload")
        browser.wait_text("后台已连接")
        browser.command("run-code", "async(page)=>{await page.getByRole('combobox',"
                        "{name:'选择课程'}).selectOption(" + json.dumps(course_a) + ");}")
        browser.wait_text("synthetic-course.webm")
        check(video_state(browser) is None or not video_state(browser).get("src"),
              "entering_course_does_not_automatically_open_last_material", result)
        click(browser, "synthetic-course.webm")
        wait_video(browser)
        restored = video_state(browser)
        check(restored["time"] >= 7 and not restored["paused"],
              "explicit_open_restores_position_and_plays", result, observed=restored)
        click(browser, "＋ 新对话")
        browser.wait_text("课程对话")
        chats = api(browser, f"/courses/{course_a}/chats")["body"]
        chat_a = chats[-1]["chat_id"]
        layout_and_fullscreen(browser, course_a, chat_a, result)
        fill(browser, "问题或追问", "草稿保留：请解答中心合成题。")
        click(browser, "截图提问")
        check(video_state(browser)["paused"], "user_capture_pauses_video", result)
        snapshot = browser.snapshot("capture-editor")
        check("确认选区并加入草稿" in snapshot, "capture_editor_is_direct_draft_flow", result)
        click(browser, "放大截图")
        click(browser, "移动画面")
        browser.snapshot("before-pan-capture")
        browser.command("run-code", "async(page)=>{const r=await page.locator('.capture-viewport')"
                        ".boundingBox();await page.mouse.move(r.x+r.width/2,r.y+r.height/2);"
                        "await page.mouse.down();await page.mouse.move(r.x+r.width/2+20,"
                        "r.y+r.height/2+10,{steps:4});await page.mouse.up();}")
        click(browser, "框选")
        # Coordinates are source pixels; changing zoom must not change crop bytes.
        set_crop(browser, 160, 90, 320, 180)
        click(browser, "确认选区并加入草稿")
        browser.command("run-code", "async(page)=>{await page.getByAltText('待发送的确认图片 1')"
                        ".waitFor({state:'visible'});}")
        check(instance.counts()["local_messages"] == 0,
              "capture_confirmation_saves_attachment_without_model_dispatch", result)
        attachment = browser.evaluate("async()=>{const i=document.querySelector('img[alt=\"待发送的确认图片 1\"]');"
                                      "const r=await fetch(i.src);return {src:i.src,"
                                      "width:i.naturalWidth,height:i.naturalHeight,"
                                      "bytes:Array.from(new Uint8Array(await r.arrayBuffer()))}}")
        confirmed = bytes(attachment.pop("bytes"))
        with Image.open(io.BytesIO(confirmed)) as image:
            dimensions = image.size
            border = image.convert("RGB").getpixel((1, 1))
        check(dimensions == (320, 180) and border[1] > border[0] and border[1] > border[2],
              "confirmed_image_contains_only_center_crop_no_outer_frame", result,
              dimensions=dimensions, corner_rgb=border)
        result["confirmed_image_sha256"] = hashlib.sha256(confirmed).hexdigest()
        check(attachment["src"].find("/attachments/") >= 0,
              "only_confirmed_crop_is_published_attachment", result)
        click(browser, "重新裁剪图片 1")
        set_crop(browser, 0, 0, 100, 100)
        click(browser, "取消")
        preserved = browser.evaluate("()=>({text:document.querySelector('textarea').value,"
                                     "images:document.querySelectorAll('img[alt^=\"待发送的确认图片\"]').length})")
        check(preserved["text"] == "草稿保留：请解答中心合成题。" and preserved["images"] == 1,
              "cancel_recrop_preserves_original_text_and_other_attachments", result)
        paste_and_composition(browser, instance, directory / "synthetic", result)
        click(browser, "保存草稿")
        browser.wait_text("草稿已保存")
        # Reopening the same saved chat must restore text and confirmed attachment.
        browser.command("reload")
        browser.wait_text("后台已连接")
        browser.command("run-code", "async(page)=>{await page.getByRole('combobox',"
                        "{name:'选择课程'}).selectOption(" + json.dumps(course_a) + ");}")
        open_chat(browser, chat_a)
        browser.command("run-code", "async(page)=>{await page.getByAltText('待发送的确认图片 1')"
                        ".waitFor({state:'visible'});}")
        check(browser.evaluate("()=>document.querySelector('textarea').value")
              == "草稿保留：请解答中心合成题。", "draft_text_and_crop_restore_after_reload", result)
        # A separate generated original exercises changing materials in the same course.
        # This fixture copy is test setup, never an application video-import action.
        second_path = Path(manifest["video"]).with_name("synthetic-course-2.webm")
        shutil.copyfile(manifest["video"], second_path)
        second = api(browser, f"/courses/{course_a}/videos", {"path": str(second_path)}, method="POST")
        check(second["status"] == 200, "second_synthetic_material_registered", result)
        click(browser, "刷新目录")
        click(browser, "synthetic-course-2.webm")
        wait_video(browser)
        same_course_chats = api(browser, f"/courses/{course_a}/chats")["body"]
        check(len(same_course_chats) == 1 and same_course_chats[0]["chat_id"] == chat_a,
              "same_course_material_switch_preserves_existing_chat", result)
        click(browser, "synthetic-course.webm")
        wait_video(browser)
        browser.command("run-code", "async(page)=>{await page.locator('video').evaluate(async v=>{"
                        "v.currentTime=1;await v.play()});}")
        # The local fixture emits actual transport deltas; it does not prove math quality.
        markdown = "合成解答：由 $x^2-5x+6=(x-2)(x-3)$，可得 $x=2$ 或 $x=3$。\n\n"
        long_body = markdown + "\n\n".join("合成阅读段落 " + str(index) + "：保持原题条件，继续展示推导。"
                                          for index in range(45))
        instance.control("scenario", scenario={"text_chunks": [long_body, "这是合成验证回答。"],
                         "pause_after_event": "text_delta", "gate": "mvp-scroll"})
        click(browser, "发送")
        browser.command("run-code", "async(page)=>{await page.locator('.katex').first()"
                        ".waitFor({state:'visible'});}")
        first = instance.generation_for("草稿保留：请解答中心合成题。")
        instance.control("wait_gate", generation_id=first["generation_id"])
        check(not video_state(browser)["paused"],
              "submitting_question_does_not_pause_parallel_video_playback", result)
        browser.evaluate("()=>{const e=document.querySelector('.messages');e.scrollTop=0;"
                         "e.dispatchEvent(new Event('scroll',{bubbles:true}));return e.scrollHeight>e.clientHeight}")
        instance.control("release_gate", gate="mvp-scroll")
        browser.wait_text("已完成")
        scroll = browser.evaluate("()=>{const e=document.querySelector('.messages');return "
                                   "{top:e.scrollTop,height:e.scrollHeight,viewport:e.clientHeight}}")
        check(scroll["height"] > scroll["viewport"] and scroll["top"] < 72,
              "new_content_does_not_pull_user_away_from_prior_reading", result, observed=scroll)
        first = instance.generation_for("草稿保留：请解答中心合成题。")
        native = instance.control("inspect", generation_id=first["generation_id"])
        images = native["boundary"][0]["images"]
        check(len(images) == 1 and images[0]["dimensions"] == [320, 180]
              and images[0]["sha256"] == result["confirmed_image_sha256"],
              "actual_Host_outbound_contains_exact_confirmed_crop_only", result, images=images)
        formula = browser.evaluate("()=>({count:document.querySelectorAll('.katex').length,"
                                   "overflow:document.documentElement.scrollWidth>innerWidth})")
        check(formula["count"] >= 1 and not formula["overflow"],
              "Chinese_markdown_and_mathematics_render_without_page_overflow", result,
              observed=formula)
        click(browser, "引用整条公式 1")
        quoted = browser.evaluate("()=>document.querySelector('#question-input').value")
        check("x^2-5x+6=(x-2)(x-3)" in quoted and "$$" in quoted,
              "whole_formula_reference_preserves_LaTeX_in_draft", result)
        fill(browser, "问题或追问", "")
        browser.command("screenshot", f"--filename={browser.directory / 'math-light.png'}", "--full-page")
        click(browser, "深色")
        browser.command("screenshot", f"--filename={browser.directory / 'math-dark.png'}", "--full-page")
        check(browser.evaluate("()=>document.querySelector('.app-shell').classList.contains('dark')"),
              "manual_dark_mode_preserves_readable_math_layout", result)
        active_stop(browser, instance, result)
        duplicate_probe(browser, instance, chat_a, result)
        lost_receipt_probe(browser, instance, result)
        background_and_course(browser, instance, course_a, chat_a, result)
        source_invalidations(browser, manifest, course_a, asset, result)
        counts = instance.counts()
        check(counts["real_provider_attempts"] == 0,
              "fixture_never_forwarded_to_real_provider", result, counts=counts)
        result["counts"] = counts
        result["status"] = "passed"
    except BaseException as error:
        result.update(status="failed", failure=f"{type(error).__name__}: {error}")
        try:
            browser.snapshot("failure")
            browser.command("screenshot", f"--filename={browser.directory / 'failure.png'}", "--full-page")
        except Exception:
            pass
    finally:
        result["cleanup"] = instance.stop()
        result["browser_cleanup"] = browser.close()
        after = code_digests()
        result["code_digests_after"] = after
        result["changed_files_during_run"] = [path for path in sorted(set(before) | set(after))
                                              if before.get(path) != after.get(path)]
        if not result["cleanup"].get("backend_port_closed"):
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    return result


def set_crop(browser: Browser, x: int, y: int, width: int, height: int) -> None:
    browser.snapshot("before-crop-coordinates")
    # Stable explicit numeric inputs are also mouse-accessible precision controls.
    browser.command("run-code", "async(page)=>{const values=" + json.dumps(
        {"选区宽度": width, "选区高度": height, "选区左侧": x, "选区顶部": y}) + ";"
        "for(const [name,value] of Object.entries(values)){await page.getByRole('spinbutton',"
        "{name,exact:true}).fill(String(value));}}")


def layout_and_fullscreen(browser: Browser, course: str, chat: str, result: dict) -> None:
    baseline = len(api(browser, f"/courses/{course}/chats")["body"])
    browser.snapshot("before-panel-drag")
    drag = json.loads(browser.command("run-code", "async(page)=>{"
        "const s=page.getByRole('separator',{name:'调整视频与聊天宽度'});"
        "const before=await s.getAttribute('aria-valuenow');const b=await s.boundingBox();"
        "await page.mouse.move(b.x+b.width/2,b.y+b.height/2);await page.mouse.down();"
        "await page.mouse.move(b.x-70,b.y+b.height/2,{steps:5});await page.mouse.up();"
        "return {before,after:await s.getAttribute('aria-valuenow')};}", raw=True))
    check(drag["before"] != drag["after"], "mouse_adjusts_video_chat_width", result, observed=drag)
    click(browser, "收起课程目录")
    click(browser, "展开课程目录")
    click(browser, "视频全屏")
    check(browser.evaluate("()=>Boolean(document.fullscreenElement)"),
          "video_enters_native_fullscreen", result)
    click(browser, "打开问答")
    check(not browser.evaluate("()=>Boolean(document.fullscreenElement)"),
          "immersive_question_exits_fullscreen_to_existing_chat", result)
    click(browser, "收起聊天")
    browser.command("run-code", "async(page)=>{await page.waitForFunction(()=>"
                    "Boolean(document.fullscreenElement)&&!document.querySelector('video').paused);}")
    check(True, "closing_immersive_chat_restores_fullscreen_and_playback", result)
    browser.command("run-code", "async(page)=>{await page.evaluate(()=>document.exitFullscreen());}")
    click(browser, "展开聊天")
    chats = api(browser, f"/courses/{course}/chats")["body"]
    check(len(chats) == baseline and any(row["chat_id"] == chat for row in chats),
          "panel_toggle_resize_and_fullscreen_preserve_chat_identity", result)


def paste_and_composition(browser: Browser, instance: MvpInstance, source: Path, result: dict) -> None:
    baseline = instance.counts()
    browser.snapshot("before-composition-and-paste")
    browser.command("run-code", "async(page)=>{const box=page.getByRole('textbox',"
                    "{name:'问题或追问'});await box.dispatchEvent('compositionstart',{data:'中'});"
                    "await box.press('Control+Enter');await box.dispatchEvent('compositionend',{data:'中'});}")
    check(instance.counts() == baseline, "IME_composition_Ctrl_Enter_does_not_submit", result)
    encoded = [base64.b64encode((source / f"paste-{index}.png").read_bytes()).decode()
               for index in (1, 2)]
    browser.evaluate("()=>{const values=" + json.dumps(encoded) + ";const data=new DataTransfer();"
        "values.forEach((value,index)=>{const bytes=Uint8Array.from(atob(value),c=>c.charCodeAt(0));"
        "data.items.add(new File([bytes],'synthetic-'+index+'.png',{type:'image/png'}))});"
        "document.querySelector('#question-input').dispatchEvent(new ClipboardEvent('paste',"
        "{bubbles:true,cancelable:true,clipboardData:data}));return {images:data.items.length}}")
    browser.wait_text("编辑截图")
    click(browser, "确认选区并加入草稿")
    browser.wait_text("编辑截图")
    click(browser, "确认选区并加入草稿")
    browser.command("run-code", "async(page)=>{await page.getByAltText('待发送的确认图片 3')"
                    ".waitFor({state:'visible'});}")
    check(instance.counts() == baseline, "multiple_pasted_images_require_confirmation_without_dispatch", result)
    click(browser, "移除图片 3")
    click(browser, "移除图片 2")
    check(browser.evaluate("()=>document.querySelector('#question-input').value")
          == "草稿保留：请解答中心合成题。", "multi_image_paste_does_not_replace_text_draft", result)


def open_chat(browser: Browser, chat_id: str) -> None:
    browser.snapshot("before-open-chat")
    browser.command("run-code", "async(page)=>{await page.getByRole('button').filter("
                    "{hasText:" + json.dumps(chat_id[:8]) + "}).click();}")
    browser.wait_text("课程对话")


def active_stop(browser: Browser, instance: MvpInstance, result: dict) -> None:
    partial = "合成正文第一段，用于证明正文活动中停止并保留部分内容。"
    forbidden = "停止之后不可出现的合成尾段。"
    instance.control("scenario", scenario={"text_chunks": [partial, forbidden],
                     "pause_after_event": "text_delta", "gate": "mvp-body-stop"})
    question = "请展示可停止的合成正文。"
    fill(browser, "问题或追问", question)
    click(browser, "发送")
    browser.wait_text(partial)
    generation = instance.generation_for(question)
    gate = instance.control("wait_gate", generation_id=generation["generation_id"])
    check(generation["state"] not in {"completed", "cancelled"},
          "stop_test_has_real_active_body_and_transport_gate", result, gate=gate)
    click(browser, "停止回答")
    browser.wait_text("已停止，保留部分内容")
    stopped = instance.generation_for(question)
    native = instance.control("inspect", generation_id=stopped["generation_id"])
    check(stopped["state"] == "cancelled" and stopped["body"] == partial
          and native["native_body"] == partial and native["host_alive"]
          and native["boundary"][0]["cancel_observed"],
          "active_cancel_retains_partial_answer_without_killing_Host", result)
    instance.control("scenario", scenario={"text_chunks": ["停止后仍可在同一聊天继续追问。"]})
    fill(browser, "问题或追问", "接着刚才继续。")
    click(browser, "发送")
    browser.wait_text("停止后仍可在同一聊天继续追问。")
    check(True, "same_chat_followup_after_active_stop", result)


def duplicate_probe(browser: Browser, instance: MvpInstance, chat_id: str, result: dict) -> None:
    baseline = instance.counts()
    payload = {"chat_id": chat_id, "submission_id": "mvp-duplicate-" + uuid.uuid4().hex,
               "text": "重复身份验证。", "model": "deepseek-flash", "thinking": "high"}
    first = api(browser, "/submissions", payload, method="POST")
    second = api(browser, "/submissions", payload, method="POST")
    conflict = api(browser, "/submissions", {**payload, "text": "同ID不同输入"}, method="POST")
    check(first["status"] == 200 and second["status"] == 200
          and first["body"]["generation_id"] == second["body"]["generation_id"]
          and conflict["status"] == 409,
          "same_submission_identity_reuses_generation_and_conflicting_body_rejected", result)
    wait_until(lambda: instance.counts()["local_messages"] >= baseline["local_messages"] + 1)
    after = instance.counts()
    check(after["submissions"] == baseline["submissions"] + 1
          and after["local_messages"] == baseline["local_messages"] + 1,
          "duplicate_browser_HTTP_submission_dispatches_once", result, before=baseline, after=after)
    browser.command("run-code", "async(page)=>{await page.waitForFunction(async gid=>{"
                    "const g=await(await fetch('/api/v1/generations/'+gid)).json();"
                    "return g.state==='completed'}," + json.dumps(first["body"]["generation_id"])
                    + ",{timeout:20000});}")


def lost_receipt_probe(browser: Browser, instance: MvpInstance, result: dict) -> None:
    baseline = instance.counts()
    instance.control("scenario", scenario={"text_chunks": ["这次已被后台接受，丢失回执时必须先核对。"]})
    question = "合成丢失提交回执验证。"
    fill(browser, "问题或追问", question)
    browser.snapshot("before-receipt-drop")
    browser.command("run-code", "async(page)=>{await page.route('**/api/v1/submissions',"
                    "async route=>{await route.fetch();await route.abort('connectionfailed');},{times:1});}")
    click(browser, "发送")
    browser.wait_text("这次提交的回执尚未确认。先核对，不会自动重发。")
    uncertain = instance.counts()
    check(uncertain["submissions"] == baseline["submissions"] + 1,
          "lost_browser_receipt_preserves_original_submission_identity", result)
    browser.command("reload")
    browser.wait_text("后台已连接")
    courses = api(browser, "/courses")["body"]
    course = next(row["course_id"] for row in courses if row["title"] == "合成数学课程 A")
    browser.command("run-code", "async(page)=>{await page.getByRole('combobox',"
                    "{name:'选择课程'}).selectOption(" + json.dumps(course) + ");}")
    chats = api(browser, f"/courses/{course}/chats")["body"]
    open_chat(browser, chats[-1]["chat_id"])
    browser.wait_text("这次提交的回执尚未确认。先核对，不会自动重发。")
    check(instance.counts() == uncertain,
          "refresh_with_unknown_receipt_does_not_resubmit", result)
    click(browser, "核对提交")
    browser.wait_text("这次已被后台接受，丢失回执时必须先核对。")
    check(instance.counts() == uncertain,
          "explicit_receipt_reconciliation_finds_original_without_second_dispatch", result)


def background_and_course(browser: Browser, instance: MvpInstance, course_a: str,
                          chat_a: str, result: dict) -> None:
    browser.command("run-code", "async(page)=>{await page.getByText('已完成',{exact:true})"
                    ".first().waitFor({state:'visible'});}")
    partial = "旧课程合成回答仍在后台生成。"
    tail = "关闭页面后，后台仍能完成这段回答。"
    instance.control("scenario", scenario={"text_chunks": [partial, tail],
                     "pause_after_event": "text_delta", "gate": "mvp-background"})
    question = "跨课程和页面关闭的后台归属验证。"
    fill(browser, "问题或追问", question)
    click(browser, "发送")
    browser.wait_text(partial)
    generation = instance.generation_for(question)
    instance.control("wait_gate", generation_id=generation["generation_id"])
    fill(browser, "新课程名称", "合成课程 B")
    click(browser, "新建")
    browser.snapshot("different-course-active-generation")
    still = instance.generation_for(question)
    check(still["generation_id"] == generation["generation_id"]
          and still["state"] not in {"cancelled", "completed"},
          "course_switch_does_not_cancel_or_move_old_generation", result)
    browser.command("run-code", "async(page)=>{await page.getByRole('combobox',"
                    "{name:'选择课程'}).selectOption(" + json.dumps(course_a) + ");}")
    browser.wait_text(partial)
    check(browser.evaluate("()=>document.querySelector('.chat-heading small')?.textContent")
          == chat_a[:8], "returning_to_course_in_same_use_resumes_original_chat", result)
    browser.command("goto", "about:blank")
    instance.control("release_gate", gate="mvp-background")
    wait_until(lambda: instance.generation_for(question)["state"] == "completed", timeout=30)
    browser.command("goto", instance.url)
    browser.wait_text("后台已连接")
    browser.command("run-code", "async(page)=>{await page.getByRole('combobox',"
                    "{name:'选择课程'}).selectOption(" + json.dumps(course_a) + ");}")
    open_chat(browser, chat_a)
    browser.wait_text(partial + tail)
    check(instance.generation_for(question)["body"] == partial + tail,
          "closing_page_keeps_background_then_saved_chat_reopens_complete_answer", result)


def source_invalidations(browser: Browser, manifest: dict, course: str,
                         asset: dict, result: dict) -> None:
    original = Path(manifest["video"])
    relocated = original.with_name("synthetic-moved.webm")
    original.rename(relocated)
    missing = next(row for row in api(browser, f"/courses/{course}/videos")["body"]
                   if row["asset_id"] == asset["asset_id"])
    opened = api(browser, f"/videos/{asset['asset_id']}/open",
                 {"source_version": asset["source_version"]}, method="POST")
    check(missing["availability"] == "missing" and opened["status"] >= 400,
          "missing_original_is_explicit_and_never_silently_replaced", result)
    response = api(browser, f"/videos/{asset['asset_id']}/relocate",
                   {"source_version": asset["source_version"], "path": str(relocated)}, method="POST")
    check(response["status"] == 200 and response["body"]["asset_id"] == asset["asset_id"],
          "explicit_relocation_retains_stable_asset_and_verified_version", result)
    with relocated.open("ab") as stream:
        stream.write(b"synthetic-version-change")
    changed = next(row for row in api(browser, f"/courses/{course}/videos")["body"]
                   if row["asset_id"] == asset["asset_id"])
    check(changed["availability"] == "version_mismatch",
          "source_overwrite_preserves_old_version_and_reports_mismatch", result)


def owned_ui_exit(directory: Path, executable: Path, port: int) -> dict:
    """Exercise the real launcher mailbox through explicit user-facing exit."""
    result = {"id": "MVP-A-owned-UI-exit", "status": "running", "real_model_attempts": 0,
              "assertions": []}
    instance = Instance(directory / "owned", "start", port, port)
    browser = Browser(directory / "browser", executable)
    try:
        instance.start()
        browser.command("open", instance.url, f"--config={browser.config}")
        browser.wait_text("后台已连接")
        fill(browser, "新课程名称", "合成安全退出课程")
        click(browser, "新建")
        click(browser, "＋ 新对话")
        browser.wait_text("课程对话")
        fill(browser, "问题或追问", "这是安全退出前需要保存的中文草稿。")
        runtime = api(browser, "/runtime")["body"]
        check(runtime["can_exit"], "real_launcher_exposes_owned_UI_exit_capability", result)
        click(browser, "安全退出")
        wait_until(lambda: port_closed(port), timeout=30)
        result["first_cleanup"] = instance.stop()
        check(result["first_cleanup"]["backend_port_closed"]
              and not result["first_cleanup"]["record"].get("cleanup", {}).get("remaining_pids"),
              "UI_safe_exit_stops_only_owned_backend_and_launcher", result)
        instance = Instance(directory / "owned", "start", port, port).start()
        browser.command("goto", instance.url)
        browser.wait_text("后台已连接")
        courses = api(browser, "/courses")["body"]
        identity = next(row["course_id"] for row in courses if row["title"] == "合成安全退出课程")
        browser.command("run-code", "async(page)=>{await page.getByRole('combobox',"
                        "{name:'选择课程'}).selectOption(" + json.dumps(identity) + ");}")
        chat = api(browser, f"/courses/{identity}/chats")["body"][0]
        open_chat(browser, chat["chat_id"])
        browser.command("run-code", "async(page)=>{await page.waitForFunction(()=>"
                        "document.querySelector('#question-input')?.value==='这是安全退出前需要保存的中文草稿。');}")
        check(True, "UI_safe_exit_saves_draft_before_reopen", result)
        result["status"] = "passed"
    except BaseException as error:
        result.update(status="failed", failure=f"{type(error).__name__}: {error}")
        try:
            browser.snapshot("exit-failure")
        except Exception:
            pass
    finally:
        result["cleanup"] = instance.stop()
        result["browser_cleanup"] = browser.close()
        if not result["cleanup"].get("backend_port_closed"):
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="mvp-a-" + time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--browser-path", type=Path)
    parser.add_argument("--ffmpeg-path", type=Path)
    parser.add_argument("--backend-port", type=int, default=18840)
    parser.add_argument("--serve-fixture", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve_fixture:
        return asyncio.run(serve_fixture(args.serve_fixture, args.backend_port))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("run-id只能包含字母、数字、横线和下划线")
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    executable = args.browser_path or (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe"
    )
    if not executable.is_file():
        raise RuntimeError("BROWSER_MISSING: 请指定项目现有Chromium；不自动下载")
    if not port_closed(args.backend_port):
        raise RuntimeError("PORT_IN_USE: 不接管其他进程")
    record = main_case(directory, executable, args.backend_port, args.ffmpeg_path)
    if record["status"] == "passed":
        record["owned_ui_exit"] = owned_ui_exit(directory / "exit", executable, args.backend_port + 1)
        if record["owned_ui_exit"]["status"] != "passed":
            record["status"] = "failed"
        write_json(directory / "result.json", record)
    print(json.dumps({"status": record["status"], "evidence": str(directory / "result.json")},
                     ensure_ascii=False))
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
