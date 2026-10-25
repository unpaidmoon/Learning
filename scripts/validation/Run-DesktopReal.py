"""Real-provider acceptance through the pinned native Harness browser UI.

Only a generated synthetic video and its explicitly confirmed crop are used.
The API key stays in the existing private configuration/backend process. All
three prompts are sent by the native composer; Stop uses its native button.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/e2e"))
sys.path.insert(0, str(ROOT / "backend/src"))

from test_browser import (  # noqa: E402
    Browser, FixtureInstance, PYTHON, check, http, port_closed, wait_until, write_json,
)
from test_desktop import (  # noqa: E402
    composer_fill, open_video, press_button, source_digests, synthetic_assets,
)
from test_mvp_a import api  # noqa: E402

TERMINAL = {"completed", "cancelled", "failed", "truncated"}


class RealInstance(FixtureInstance):
    def __init__(self, directory: Path, port: int, arguments):
        super().__init__(directory, port)
        self.arguments = arguments

    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
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
                raise RuntimeError("DESKTOP_REAL_START_FAILED: see private server.stdout.txt")
            try:
                status, _, health = http(self.url + "/api/v1/health")
                return status == 200 and health["capabilities"]["question_answering"] == "ready"
            except OSError:
                return False

        wait_until(ready, timeout=60)
        return self


async def serve_real(directory: Path, arguments) -> int:
    import uvicorn
    from dotenv import dotenv_values

    from learning_app.app import create_app
    from learning_app.config import AppConfig
    from learning_app.execution.adapter import ExecutionAdapter

    key = dotenv_values(ROOT / ".local/secrets/deepseek.env").get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("MODEL_KEY_NOT_CONFIGURED")
    adapter = ExecutionAdapter(
        ROOT, directory / "execution", mode="official", credential_route="api_key",
        api_key=key, model=arguments.model, thinking=arguments.thinking,
        max_tokens=arguments.max_output_tokens, timeout=arguments.timeout, desktop_enabled=True,
    )
    key = None
    await adapter.open()
    await adapter.validate_choice(arguments.model, arguments.thinking)
    application = create_app(
        AppConfig(
            mode="start", backend_port=arguments.backend_port, frontend_port=arguments.backend_port,
            data_dir=directory / "data", runtime_dir=directory / "runtime",
            harness_enabled=True, desktop_enabled=True, credential_route="api_key",
            model=arguments.model, thinking=arguments.thinking,
            max_output_tokens=arguments.max_output_tokens, model_timeout_seconds=arguments.timeout,
            instance_id="desktop-real-" + directory.name,
        ), execution=adapter,
    )
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=arguments.backend_port,
                                         access_log=False))
    loop = asyncio.get_running_loop()

    async def control(command: dict) -> dict:
        generation_id = command["generation_id"]
        service = application.state.service
        if command["operation"] == "reconcile":
            await service.reconcile(generation_id, resume_observer=True)
        elif command["operation"] != "inspect":
            raise ValueError("UNKNOWN_TEST_CONTROL")
        binding = service._binding_for(generation_id)
        generation = service.generation_view(generation_id)
        rows = [row for row in adapter.boundary.records if row.get("generation_id") == generation_id]
        active = any(row.get("dispatch_ns") and row.get("upstream_status") == 200
                     and not row.get("finished_ns") for row in rows)
        # Raw observations stay in the ignored run directory; no credentials
        # or provider request headers enter the report/control response.
        write_json(directory / "private" / f"{generation_id}.json", {
            "generation": application.state.repository.get_generation(generation_id),
            "view": generation, "boundary_records": rows,
        })
        return {
            "generation": generation, "session_id": binding.session_id, "epoch": binding.epoch,
            "host_alive": adapter.host.process.poll() is None, "provider_active": bool(active),
            "actual_attempts": sum(bool(row.get("actual_outbound_attempt")) for row in rows),
            "boundary": [{key: row.get(key) for key in (
                "request_id", "model", "max_tokens", "thinking", "output_config",
                "images", "cancel_observed", "status", "usage",
                "dispatch_ns", "upstream_status", "finished_ns", "actual_outbound_attempt",
            )} for row in rows],
        }

    async def prepare_shutdown():
        # This is explicit suite cleanup, separate from the native Stop test.
        # Persist per-generation cancellation before disposing the owned Host.
        service = application.state.service
        active = [value for value in service.repository.recoverable_generations()
                  if value["state"] not in TERMINAL]
        for generation in active:
            gid = generation["generation_id"]
            if generation["state"] == "unknown":
                await service.reconcile(gid, resume_observer=False)
            if service.generation_view(gid)["state"] not in TERMINAL:
                await service.cancel(gid)
        write_json(directory / "cleanup-cancel-intents.json", {
            "scope": "explicit isolated suite shutdown, not native Stop acceptance evidence",
            "generations": [service.generation_view(value["generation_id"]) for value in active],
        })

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
        try:
            asyncio.run_coroutine_threadsafe(prepare_shutdown(), loop).result(25)
        except Exception as error:
            write_json(directory / "cleanup-cancel-error.json", {"error": f"{type(error).__name__}: {error}"})
        server.should_exit = True

    threading.Thread(target=parent, daemon=True).start()
    try:
        await server.serve()
    finally:
        write_json(directory / "worker-cleanup.json", await adapter.close())
    return 0 if server.started else 1


def generation_for(instance, text: str, timeout: float = 15) -> dict:
    def find():
        try:
            return instance.generation_for(text)
        except AssertionError:
            return None
    return wait_until(find, timeout=timeout)


def observe(instance, browser, generation_id: str, arguments, *, stop: bool) -> dict:
    started = time.monotonic()
    seen = []
    stop_evidence = None
    terminal_at = None
    previous = None
    while time.monotonic() - started < arguments.timeout + 35:
        state = instance.control("inspect", generation_id=generation_id)
        generation = state["generation"]
        marker = (generation["state"], generation["event_watermark"], generation["usage"]["status"])
        if marker != previous:
            seen.append({"at_ms": round((time.monotonic() - started) * 1000, 3),
                         "state": generation["state"], "watermark": generation["event_watermark"],
                         "body_characters": len(generation["body"]),
                         "reasoning_characters": len(generation["reasoning"]),
                         "provider_active": state["provider_active"],
                         "usage_status": generation["usage"]["status"]})
            previous = marker
        if stop and stop_evidence is None and len(generation["body"]) >= 24 and state["provider_active"]:
            stop_evidence = {"body_characters_before_stop": len(generation["body"]),
                             "provider_active_before_stop": True,
                             "host_alive_before_stop": state["host_alive"]}
            press_button(browser, "停止生成")
        if generation["state"] == "unknown":
            instance.control("reconcile", generation_id=generation_id)
        if generation["state"] in TERMINAL:
            terminal_at = terminal_at or time.monotonic()
            if generation["usage"]["raw"] is not None or time.monotonic() - terminal_at >= 3:
                break
        time.sleep(0.08)
    else:
        raise TimeoutError("DESKTOP_REAL_GENERATION_RESULT_UNKNOWN: reconcile original identity, do not resubmit")
    final = instance.control("inspect", generation_id=generation_id)
    generation = final["generation"]
    expected = "cancelled" if stop else "completed"
    passed = generation["state"] == expected and bool(generation["body"]) and final["host_alive"]
    if stop:
        passed = (passed and bool(stop_evidence)
                  and any(row.get("cancel_observed") for row in final["boundary"]))
    final.update(passed=bool(passed), stop_evidence=stop_evidence, observed=seen,
                 elapsed_ms=round((time.monotonic() - started) * 1000, 3))
    return final


def run(arguments) -> dict:
    from PIL import Image
    import io

    directory = ROOT / ".runtime/validation/desktop-real" / arguments.run_id
    directory.mkdir(parents=True, exist_ok=False)
    artifacts = ROOT / "output/playwright" / arguments.run_id
    artifacts.mkdir(parents=True, exist_ok=False)
    manifest = synthetic_assets(directory / "synthetic", arguments.ffmpeg_path)
    digests = source_digests()
    digests[str(Path(__file__).relative_to(ROOT).as_posix())] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result = {"run_id": arguments.run_id, "client_date": "2026-10-22", "status": "running",
              "evidence_level": "real Chromium + official Harness UI + project business backend + pinned SEA + real provider",
              "route": "api_key", "model": arguments.model, "thinking": arguments.thinking,
              "max_output_tokens": arguments.max_output_tokens, "timeout_seconds": arguments.timeout,
              "synthetic_manifest": manifest, "source_digests_before": digests,
              "assertions": [], "cases": [], "limitations": [
                  "Generated synthetic media only; no user screen, course, or video was read.",
                  "Browser native UI acceptance does not establish Electron installer or human learning/visual acceptance.",
                  "Provider completion before an active Stop condition is uncovered/failure; no prompt is silently repeated.",
                  "Missing usage stays unknown and no reasoning usage is invented.",
              ]}
    instance = RealInstance(directory / "server", arguments.backend_port, arguments)
    browser = Browser(artifacts / "browser", arguments.browser_path)
    try:
        instance.start()
        browser.command("open", instance.url + "/desktop/", f"--config={browser.config}")
        browser.command("run-code", "async(page)=>{await page.waitForFunction(()=>document.readyState==='complete');}")
        course = api(browser, "/courses", {"title": "真实传输验证用合成课程"}, method="POST")["body"]
        video = api(browser, f"/courses/{course['course_id']}/videos", {"path": manifest["video"]}, method="POST")
        check(video["status"] == 200, "synthetic_video_registered", result)
        browser.command("goto", instance.url + "/desktop/?course_id=" + course["course_id"])
        browser.command("run-code", "async(page)=>{await page.locator('[data-composer-input]')"
                        ".waitFor({state:'visible',timeout:30000});}")
        open_video(browser)
        browser.snapshot("native-ready")
        press_button(browser, "截图提问")
        browser.command("run-code", "async(page)=>{const values=" + json.dumps(
            {"选区宽度": 320, "选区高度": 180, "选区左侧": 160, "选区顶部": 90})
            + ";for(const [name,value] of Object.entries(values)){await page.getByRole('spinbutton',"
            "{name,exact:true}).fill(String(value));}}")
        press_button(browser, "确认选区并加入草稿")
        check(instance.counts()["real_provider_attempts"] == 0,
              "screenshot_confirmation_has_zero_real_model_dispatches", result)
        chats = api(browser, f"/courses/{course['course_id']}/chats")["body"]
        check(len(chats) == 1, "one_business_chat_initialized_without_dispatch", result)
        session_id = browser.evaluate("()=>document.querySelector('[data-testid=learning-video]').dataset.sessionId")
        mapping = api(browser, "/desktop/sessions/" + session_id)["body"]
        chat_id = mapping["chat_id"]
        prompts = [
            ("image-answer", "请完整解答确认截图中的一元二次方程，给出因式分解和验根。", False),
            ("active-stop", "请从基本定义开始详细推导二次方程求根公式，然后逐题给出至少60个不同整系数二次方程的完整解答，每题列出配方与验根过程。持续详细输出，暂时不要总结。", True),
            ("same-session-follow-up", "接着刚才截图的问题，请只给出两个根，并各用一句话验根。", False),
        ]
        for label, prompt, stop in prompts:
            composer_fill(browser, prompt)
            press_button(browser, "发送消息")
            generation = generation_for(instance, prompt)
            case = observe(instance, browser, generation["generation_id"], arguments, stop=stop)
            case["id"] = label
            write_json(directory / "private" / f"case-{label}.json", case)
            summary = {key: case[key] for key in ("id", "passed", "session_id", "epoch", "host_alive",
                                                 "actual_attempts", "stop_evidence", "elapsed_ms")}
            summary.update(generation_id=generation["generation_id"],
                           state=case["generation"]["state"],
                           body_characters=len(case["generation"]["body"]),
                           usage=case["generation"]["usage"], boundary=case["boundary"])
            result["cases"].append(summary)
            check(case["passed"], label + "_completed_expected_real_UI_path", result)
            check(case["actual_attempts"] == 1, label + "_has_one_actual_outbound_attempt", result)
            if label == "image-answer":
                images = case["boundary"][0]["images"]
                check(len(images) == 1 and images[0].get("dimensions") == [320, 180],
                      "real_outbound_contains_only_confirmed_crop", result, images=images)
                attachment_id = api(browser, f"/chats/{chat_id}")["body"]["messages"][0]["attachment_ids"][0]
                data = bytes(browser.evaluate("async()=>{const r=await fetch('/api/v1/attachments/"
                                              + attachment_id + "');return Array.from(new Uint8Array(await r.arrayBuffer()))}"))
                with Image.open(io.BytesIO(data)) as image:
                    dimensions = image.size
                check(dimensions == (320, 180) and hashlib.sha256(data).hexdigest() == images[0]["sha256"],
                      "confirmed_publication_and_real_provider_crop_bytes_match", result)
            browser.snapshot("after-" + label)
        check(len({case["session_id"] for case in result["cases"]}) == 1,
              "active_stop_and_follow_up_keep_same_native_session", result)
        check(not browser.evaluate("()=>document.body.innerText.includes('[[learning-generation:')"),
              "native_transcript_hides_execution_marker", result)
        before = instance.counts()
        browser.command("reload")
        browser.command("run-code", "async(page)=>{await page.locator('[data-composer-input]')"
                        ".waitFor({state:'visible',timeout:30000});}")
        check(instance.counts()["real_provider_attempts"] == before["real_provider_attempts"],
              "history_reload_does_not_repeat_model_request", result)
        open_video(browser)
        browser.command("run-code", "async(page)=>{await page.locator('video').evaluate(v=>v.pause());}")
        browser.snapshot("completed-real-native-flow")
        browser.command("screenshot", f"--filename={artifacts / 'completed-real-native-flow.png'}", "--full-page")
        result["counts"] = instance.counts()
        check(result["counts"]["real_provider_attempts"] == 3
              and result["counts"]["business_attempts"] == 3,
              "three_browser_submissions_reconcile_to_three_actual_attempts", result)
        result["status"] = "passed"
    except BaseException as error:
        result.update(status="failed", failure=f"{type(error).__name__}: {error}")
        try:
            browser.snapshot("failure")
            browser.command("screenshot", f"--filename={artifacts / 'failure.png'}", "--full-page")
            result["counts"] = instance.counts()
        except Exception:
            pass
    finally:
        result["cleanup"] = instance.stop()
        result["browser_cleanup"] = browser.close()
        after = source_digests()
        after[str(Path(__file__).relative_to(ROOT).as_posix())] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        result["source_digests_after"] = after
        changed = [name for name in sorted(set(digests) | set(after)) if digests.get(name) != after.get(name)]
        result["changed_files_during_run"] = changed
        if changed and result["status"] == "passed":
            result.update(status="failed", failure="SOURCE_CHANGED_DURING_VERIFICATION")
        write_json(directory / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="desktop-real-" + time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--backend-port", type=int, default=18990)
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
    if not arguments.browser_path.is_file() or not arguments.ffmpeg_path.is_file():
        raise RuntimeError("LOCAL_TEST_TOOL_MISSING: 指定已有Chromium和ffmpeg，不自动下载")
    if not port_closed(arguments.backend_port):
        raise RuntimeError("PORT_IN_USE: 不接管其他进程")
    result = run(arguments)
    print(json.dumps({"status": result["status"], "real_model_attempts": result.get("counts", {}).get("real_provider_attempts"),
                      "evidence": str(ROOT / ".runtime/validation/desktop-real" / arguments.run_id / "result.json")}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
