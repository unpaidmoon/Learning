"""Official Harness UI acceptance using synthetic video and a local provider.

The browser loads the actual pinned Harness client through the application's
desktop proxy. The model boundary is fixture-only: no key, real course video,
screen acquisition, or external model connection is used by this runner.
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
import subprocess
import sys
import threading
import time

from test_browser import Browser, FixtureInstance, PYTHON, ROOT, check, http, port_closed, wait_until, write_json
from test_mvp_a import api


class DesktopInstance(FixtureInstance):
    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log = (self.directory / "server.stdout.txt").open("w", encoding="utf-8")
        self.process = self.group.spawn(
            [str(PYTHON), str(Path(__file__).resolve()), "--serve-fixture", str(self.directory),
             "--backend-port", str(self.backend)],
            cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            stdout=self.log, stderr=subprocess.STDOUT,
        )

        def ready():
            if self.process.poll() is not None:
                raise RuntimeError("DESKTOP_FIXTURE_START_FAILED: see server.stdout.txt")
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

    adapter = ExecutionAdapter(ROOT, directory / "execution", mode="fixture", timeout=120,
                               desktop_enabled=True)
    await adapter.open()
    application = create_app(
        AppConfig(mode="start", backend_port=port, frontend_port=port,
                  data_dir=directory / "data", runtime_dir=directory / "runtime",
                  harness_enabled=True, desktop_enabled=True,
                  instance_id="desktop-fixture-" + directory.name),
        execution=adapter,
    )
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port, access_log=False,
                                          timeout_graceful_shutdown=5))
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
            event = await asyncio.to_thread(adapter.boundary.wait_for_event, "gate_waiting", 15,
                                            generation_id=generation_id)
            return {"fault_point_event": event}
        if operation == "inspect":
            binding = application.state.service._binding_for(generation_id)
            native = await adapter.reconcile(binding)
            rows = [row for row in adapter.boundary.records if row["generation_id"] == generation_id]
            return {"generation_id": generation_id, "native_status": native["status"],
                    "session_id": binding.session_id,
                    "host_alive": adapter.host.process.poll() is None,
                    "boundary": [{"images": row.get("images", []),
                                  "cancel_observed": row.get("cancel_observed", False),
                                  "status": row["status"]} for row in rows]}
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


def source_digests() -> dict[str, str]:
    paths = [Path(__file__).resolve(), ROOT / "harness/runtime.lock.json"]
    if (ROOT / "harness/desktop.lock.json").is_file():
        paths.append(ROOT / "harness/desktop.lock.json")
    for folder in ("desktop", "harness/plugins", "backend/src/learning_app"):
        paths.extend(path for path in (ROOT / folder).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts
                     and "node_modules" not in path.parts)
    return {str(path.relative_to(ROOT).as_posix()): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(set(paths))}


def synthetic_assets(directory: Path, ffmpeg: Path) -> dict:
    spec = importlib.util.spec_from_file_location("desktop_fixture_assets", ROOT / "tests/fixtures/mvp_a/generate.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("SYNTHETIC_GENERATOR_MISSING")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.generate(directory, ffmpeg=ffmpeg)


def press_button(browser: Browser, name: str) -> None:
    browser.snapshot("before-" + re.sub(r"[^A-Za-z0-9_-]", "_", name))
    browser.command("run-code", "async(page)=>{await page.getByRole('button',{name:"
                    + json.dumps(name) + ",exact:true}).click();}")


def composer_fill(browser: Browser, text: str) -> None:
    browser.snapshot("before-composer-fill")
    browser.command("run-code", "async(page)=>{await page.locator('[data-composer-input]').fill("
                    + json.dumps(text) + ");}")


def open_video(browser: Browser) -> None:
    browser.snapshot("before-open-video-from-course-directory")
    browser.command("run-code", "async(page)=>{await page.getByTestId('learning-course-directory')"
                    ".getByRole('button',{name:'synthetic-course.webm',exact:true}).click();"
                    "await page.waitForFunction(()=>{const v=document.querySelector('video');"
                    "return v&&v.readyState>=2&&v.videoWidth===640});}")


def wait_generation(browser: Browser, generation_id: str, terminal: str) -> dict:
    browser.command("run-code", "async(page)=>{await page.waitForFunction(async value=>{"
                    "const r=await fetch('/api/v1/generations/'+value.id);const g=await r.json();"
                    "return g.state===value.state}," + json.dumps({"id": generation_id, "state": terminal})
                    + ",{timeout:25000});}")
    return api(browser, "/generations/" + generation_id)["body"]


def run_case(directory: Path, executable: Path, port: int, ffmpeg: Path) -> dict:
    from PIL import Image

    manifest = synthetic_assets(directory / "synthetic", ffmpeg)
    result = {"id": "harness-desktop-browser", "status": "running", "assertions": [],
              "evidence_level": "real Chromium + official pinned Harness UI + formal ASGI + SEA Host + local provider fixture",
              "real_model_attempts": 0, "human_visual_acceptance": "not evaluated",
              "synthetic_manifest": manifest, "source_digests_before": source_digests(),
              "playwright_cli_version": json.loads((ROOT / "node_modules/@playwright/cli/package.json")
                                                   .read_text("utf-8"))["version"],
              "browser_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}
    instance = DesktopInstance(directory / "server", port)
    browser = Browser(directory / "browser", executable)
    try:
        instance.start()
        browser.command("open", instance.url + "/desktop/", f"--config={browser.config}")
        browser.command("run-code", "async(page)=>{await page.waitForFunction(()=>"
                        "document.readyState==='complete'&&document.body.innerText.length>10);}")
        browser.snapshot("native-initial")
        course = api(browser, "/courses", {"title": "合成桌面数学课程"}, method="POST")
        check(course["status"] == 200, "business_course_created_without_model_dispatch", result)
        course_id = course["body"]["course_id"]
        video = api(browser, f"/courses/{course_id}/videos", {"path": manifest["video"]}, method="POST")
        check(video["status"] == 200, "synthetic_video_registered_by_business_identity", result)
        asset = video["body"]
        browser.command("run-code", "async(page)=>{page.__desktopMux=[];page.on('websocket',ws=>{"
                        "ws.on('framesent',event=>{try{const f=JSON.parse(event.payload);"
                        "const a=f.payload?.args;const request=a?.request||a;"
                        "page.__desktopMux.push({type:f.type,endpoint:f.endpoint,"
                        "payloadKeys:Object.keys(f.payload||{}),argumentKeys:Object.keys(a||{}),"
                        "addressKind:request?.address?.kind});}catch{}});"
                        "ws.on('close',()=>page.__desktopMux.push({closed:true}));});}")
        browser.command("goto", instance.url + "/desktop/?course_id=" + course_id)
        browser.command("run-code", "async(page)=>{await page.locator('[data-composer-input]')"
                        ".waitFor({state:'visible',timeout:30000});}")
        browser.snapshot("native-course")
        resources = browser.evaluate("()=>{const urls=performance.getEntriesByType('resource')"
                                     ".map(x=>new URL(x.name));return {paths:urls.map(x=>x.pathname)"
                                     ".filter(x=>/desktop\\/(assets|plugins)\\//.test(x)),"
                                     "modules:Array.from(new Set(urls.flatMap(x=>Array.from(x.href.matchAll("
                                     "/@deepseek-ai\\/[a-z0-9-]+/g),m=>m[0])))),"
                                     "credentialQuery:urls.some(x=>x.searchParams.has('token')"
                                     "||x.searchParams.has('peerToken'))}}")
        native_slots = browser.evaluate("()=>({root:!!document.querySelector('[data-slot=\"root\"]'),"
                                         "conversation:!!document.querySelector('[data-slot^=\"conversation.\"]'),"
                                         "composer:!!document.querySelector('[data-composer-input]')})")
        check("@deepseek-ai/dsh-client-ui-chat" in resources["modules"]
              and "@deepseek-ai/dsh-client-ui-conversation" in resources["modules"]
              and all(native_slots.values()), "official_Harness_client_resources_and_slots_loaded",
              result, resources=resources, slots=native_slots)
        check(not resources["credentialQuery"], "Host_capability_not_exposed_in_renderer_resource_URLs", result)
        open_video(browser)
        browser.command("run-code", "async(page)=>{await page.locator('video').evaluate(v=>v.play());await page.waitForFunction(()=>{"
                        "const v=document.querySelector('video');return !v.paused&&v.currentTime>0.2});}")
        video_state = browser.evaluate("()=>{const v=document.querySelector('video');"
                                       "return {source:v.currentSrc,width:v.videoWidth,height:v.videoHeight,"
                                       "paused:v.paused,time:v.currentTime}}")
        check(video_state["source"].startswith(instance.url + "/api/v1/videos/")
              and video_state["width"] == 640 and not video_state["paused"] and video_state["time"] > 0.2,
              "registered_video_decodes_and_plays_through_Range_route", result,
              observed=video_state)
        content = f"/api/v1/videos/{asset['asset_id']}/content?source_version={asset['source_version']}"
        ranges = browser.evaluate("async()=>{const r=await fetch(" + json.dumps(content)
                                  + ",{headers:{Range:'bytes=0-31'}});return {status:r.status,bytes:(await r.arrayBuffer()).byteLength}}")
        check(ranges == {"status": 206, "bytes": 32}, "desktop_video_uses_partial_content", result)
        press_button(browser, "截图提问")
        browser.snapshot("capture-selection")
        browser.command("run-code", "async(page)=>{const values=" + json.dumps(
            {"选区宽度": 320, "选区高度": 180, "选区左侧": 160, "选区顶部": 90})
            + ";for(const [name,value] of Object.entries(values)){await page.getByRole('spinbutton',"
            "{name,exact:true}).fill(String(value));}}")
        press_button(browser, "确认选区并加入草稿")
        check(instance.counts()["local_messages"] == 0,
              "capture_confirmation_does_not_submit_a_model_request", result)
        # The prepared attachment is read through its explicit business identity.
        drafts = api(browser, f"/courses/{course_id}/chats")["body"]
        initial_chat_count = len(drafts)
        native_session_id = browser.evaluate("()=>document.querySelector('[data-testid=learning-video]').dataset.sessionId")
        selected = api(browser, "/desktop/sessions/" + native_session_id)["body"]
        chat_id = selected["chat_id"]
        check(any(chat["chat_id"] == chat_id for chat in drafts),
              "native_selected_Session_maps_to_current_course_business_chat", result)
        question = "请解答确认选区中的合成数学题。"
        instance.control("scenario", scenario={"text_chunks": ["合成解答：$x=2$ 或 $x=3$。"]})
        composer_fill(browser, question)
        press_button(browser, "发送消息")
        wait_until(lambda: instance.counts()["local_messages"] == 1, timeout=20)
        first = instance.generation_for(question)
        wait_generation(browser, first["generation_id"], "completed")
        browser.command("run-code", "async(page)=>{await page.getByText('合成解答：',"
                        "{exact:false}).last().waitFor({state:'visible',timeout:20000});}")
        marker_visible_after_first = browser.evaluate("()=>document.body.innerText.includes('[[learning-generation:')")
        native = instance.control("inspect", generation_id=first["generation_id"])
        images = native["boundary"][0]["images"]
        check(len(images) == 1 and images[0].get("dimensions") == [320, 180],
              "actual_Host_outbound_contains_only_confirmed_crop", result, images=images)
        attachment_ids = api(browser, f"/chats/{chat_id}")["body"]["messages"][0]["attachment_ids"]
        attachment_id = attachment_ids[0]
        encoded = browser.evaluate("async()=>{const r=await fetch('/api/v1/attachments/"
                                   + attachment_id + "');return Array.from(new Uint8Array(await r.arrayBuffer()))}")
        confirmed = bytes(encoded)
        with Image.open(io.BytesIO(confirmed)) as image:
            dimensions = image.size
            border = image.convert("RGB").getpixel((1, 1))
        confirmed_hash = hashlib.sha256(confirmed).hexdigest()
        check(dimensions == (320, 180) and border[1] > border[0] and border[1] > border[2]
              and confirmed_hash == images[0]["sha256"],
              "published_and_outbound_crop_bytes_match_without_outer_frame", result,
              dimensions=dimensions, corner_rgb=border, sha256=confirmed_hash)
        partial = "合成流式回答正在输出，供活动中停止验证。"
        second_question = "请继续推导，并等待我停止。"
        instance.control("scenario", scenario={"text_chunks": [partial, "停止后不应输出的尾段。"],
                                               "pause_after_event": "text_delta", "gate": "desktop-stop"})
        composer_fill(browser, second_question)
        press_button(browser, "发送消息")
        wait_until(lambda: instance.counts()["local_messages"] == 2, timeout=20)
        second = instance.generation_for(second_question)
        instance.control("wait_gate", generation_id=second["generation_id"])
        browser.wait_text(partial)
        press_button(browser, "停止生成")
        stopped = wait_generation(browser, second["generation_id"], "cancelled")
        stopped_native = instance.control("inspect", generation_id=second["generation_id"])
        check(stopped["body"] == partial and stopped_native["host_alive"]
              and stopped_native["boundary"][0]["cancel_observed"],
              "native_Stop_cancels_active_provider_stream_and_preserves_partial_answer", result,
              native_status=stopped_native["native_status"])
        follow_question = "停止后的追问：请只说最终答案。"
        instance.control("scenario", scenario={"text_chunks": ["停止后同会话追问完成：2 和 3。"]})
        composer_fill(browser, follow_question)
        press_button(browser, "发送消息")
        wait_until(lambda: instance.counts()["local_messages"] == 3, timeout=20)
        follow = instance.generation_for(follow_question)
        wait_generation(browser, follow["generation_id"], "completed")
        follow_native = instance.control("inspect", generation_id=follow["generation_id"])
        check(native["session_id"] == stopped_native["session_id"] == follow_native["session_id"],
              "follow_up_after_active_Stop_uses_original_Harness_session", result)
        browser.command("reload")
        browser.wait_text("停止后同会话追问完成：2 和 3。")
        check(instance.counts()["local_messages"] == 3,
              "native_history_reloads_without_resubmitting_questions", result)
        open_video(browser)
        browser.command("run-code", "async(page)=>{await page.waitForFunction(()=>{const v=document.querySelector('video');"
                        "return v&&v.readyState>=2&&v.videoWidth===640});await page.locator('video').evaluate(v=>v.pause());}")
        before_navigation = instance.counts()
        press_button(browser, "关闭 synthetic-course.webm")
        browser.command("run-code", "async(page)=>{await page.waitForFunction(()=>!document.querySelector('video'));"
                        "await page.getByRole('tab',{name:'文件',exact:true}).waitFor({state:'visible'});}")
        files_active = browser.evaluate("()=>document.querySelector('[role=tab][aria-selected=true]')?.textContent")
        open_video(browser)
        reopened_session = browser.evaluate("()=>document.querySelector('[data-testid=learning-video]').dataset.sessionId")
        check(files_active == "文件" and reopened_session == native_session_id
              and instance.counts() == before_navigation,
              "closing_last_video_returns_to_Files_and_reopen_keeps_chat_without_dispatch", result)
        press_button(browser, "收起 AI")
        browser.command("run-code", "async(page)=>{await page.getByRole('button',{name:'展开 AI',exact:true})"
                        ".waitFor({state:'visible'});}")
        press_button(browser, "展开 AI")
        check(browser.evaluate("()=>document.querySelector('[data-testid=learning-video]').dataset.sessionId")
              == native_session_id and instance.counts() == before_navigation,
              "collapsing_and_expanding_native_AI_keeps_chat_without_dispatch", result)
        chats_before_new = len(api(browser, f"/courses/{course_id}/chats")["body"])
        press_button(browser, "新对话")
        browser.command("run-code", "async(page)=>{await page.waitForFunction(async value=>{"
                        "const c=await(await fetch('/api/v1/courses/'+value.id+'/chats')).json();"
                        "return c.length===value.count},"
                        + json.dumps({"id": course_id, "count": chats_before_new + 1}) + ");}")
        check(instance.counts() == before_navigation,
              "native_New_Chat_creates_business_chat_without_model_dispatch", result)
        press_button(browser, "选择历史对话")
        browser.snapshot("native-history-menu")
        browser.command("run-code", "async(page)=>{await page.getByRole('menuitem',{name:'对话 1',exact:true}).click();}")
        browser.wait_text("停止后同会话追问完成：2 和 3。")
        check(browser.evaluate("()=>document.querySelector('[data-testid=learning-video]').dataset.sessionId")
              == native_session_id and instance.counts() == before_navigation,
              "native_title_history_menu_returns_to_original_chat_without_dispatch", result)
        counts = instance.counts()
        check(counts["real_provider_attempts"] == 0 and counts["business_attempts"] == 3
              and counts["submissions"] == 3,
              "all_native_UI_submissions_recorded_and_fixture_never_forwarded_to_real_provider", result,
              counts=counts)
        check(initial_chat_count == 1, "desktop_initializes_one_business_chat", result,
              count=initial_chat_count)
        check(not marker_visible_after_first
              and not browser.evaluate("()=>document.body.innerText.includes('[[learning-generation:')"),
              "native_transcript_and_titles_hide_internal_execution_routing_markers", result)
        geometry = browser.evaluate("()=>{const box=selector=>{const e=document.querySelector(selector);"
                                     "if(!e)return null;const r=e.getBoundingClientRect();return {left:r.left,"
                                     "right:r.right,width:r.width,height:r.height}};return {"
                                     "courses:box('[data-testid=learning-course-directory]'),"
                                     "material:box('[data-testid=learning-video]'),"
                                     "composer:box('[data-composer-input]'),"
                                     "horizontalOverflow:document.documentElement.scrollWidth>innerWidth+1}}")
        left, center, right = (geometry[key] for key in ("courses", "material", "composer"))
        check(left and center and right and min(left["width"], center["width"], right["width"]) > 100
              and left["right"] <= center["left"] + 8 and center["right"] <= right["left"] + 8
              and not geometry["horizontalOverflow"],
              "course_directory_left_material_center_native_AI_right_without_page_overflow", result,
              geometry=geometry)
        browser.snapshot("completed-native-flow")
        browser.command("screenshot", f"--filename={browser.directory / 'completed-native-flow.png'}", "--full-page")
        result.update(status="passed", counts=counts)
    except BaseException as error:
        result.update(status="failed", failure=f"{type(error).__name__}: {error}")
        write_json(directory / "result.json", result)
        try:
            result["mux_diagnostics"] = json.loads(browser.command(
                "run-code", "async(page)=>page.__desktopMux?.slice(-64)||[]", raw=True))
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
    parser.add_argument("--run-id", default="desktop-" + time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--backend-port", type=int, default=18980)
    parser.add_argument("--browser-path", type=Path, default=Path(os.environ.get("LOCALAPPDATA", ""))
                        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe")
    parser.add_argument("--ffmpeg-path", type=Path, default=Path(os.environ.get("LOCALAPPDATA", ""))
                        / "Microsoft/WinGet/Links/ffmpeg.EXE")
    parser.add_argument("--serve-fixture", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve_fixture:
        return asyncio.run(serve_fixture(args.serve_fixture, args.backend_port))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("run-id只能包含字母、数字、横线和下划线")
    if not args.browser_path.is_file() or not args.ffmpeg_path.is_file():
        raise RuntimeError("LOCAL_TEST_TOOL_MISSING: 请指定已有Chromium与ffmpeg，不自动下载")
    if not port_closed(args.backend_port):
        raise RuntimeError("PORT_IN_USE: 不接管其他进程")
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    result = run_case(directory, args.browser_path, args.backend_port, args.ffmpeg_path)
    print(json.dumps({"status": result["status"], "evidence": str(directory / "result.json")}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
