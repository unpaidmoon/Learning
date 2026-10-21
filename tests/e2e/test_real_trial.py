"""Real Chromium + formal Harness trial using only generated synthetic video.

No provider fixture, replay, user video or unconfirmed frame is used. Provider
request bodies remain private; the public result contains image hashes, actual
attempts, raw usage, rendered increment observations and owned cleanup only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from test_browser import (
    Instance,
    ROOT,
    check,
    port_closed,
    wait_until,
    write_json,
)
from test_mvp_a import (
    Browser,
    api,
    click,
    code_digests,
    fill,
    open_chat,
    set_crop,
    video_state,
    wait_video,
)


def boundary_evidence(directory: Path, crop_hash: str) -> dict:
    rows, latest = [], {}
    for path in (directory / "data").rglob("boundary-ledger.jsonl"):
        rows.extend(json.loads(line) for line in path.read_text("utf-8").splitlines())
    for row in rows:
        if row.get("request_id"):
            latest.setdefault(row["request_id"], {}).update(row)
    audits = []
    for row in latest.values():
        request_path = next(
            (directory / "data").rglob(row["request_id"] + ".request.json")
        )
        request = json.loads(request_path.read_text("utf-8"))
        audits.append(
            {
                key: row.get(key)
                for key in (
                    "request_id",
                    "generation_id",
                    "actual_outbound_attempt",
                    "images",
                    "tools",
                    "body_sha256",
                    "usage",
                    "status",
                )
            }
            | {
                "model": request.get("model"),
                "thinking": request.get("thinking"),
                "output_config": request.get("output_config"),
                "max_tokens": request.get("max_tokens"),
                "all_images_are_confirmed_crop": all(
                    image["sha256"] == crop_hash for image in row.get("images", [])
                ),
            }
        )
    with sqlite3.connect(directory / "data/business.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        attempts = [
            dict(row) for row in connection.execute("SELECT * FROM outbound_attempts")
        ]
        usage = [
            dict(row)
            for row in connection.execute(
                "SELECT generation_id,settlement_id,source,raw_usage,field_presence,normalized FROM usage_records"
            )
        ]
    effective = {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (directory / "data").rglob("effective.patch.json")
    }
    return {
        "actual_outbound_attempts": sum(
            row.get("event") == "dispatch_started" and bool(row.get("attempt"))
            for row in rows
        ),
        "ledger_audits": audits,
        "business_attempts": attempts,
        "usage_records": usage,
        "effective_profile_sha256": effective,
    }


def install_stream_observer(browser: Browser) -> None:
    browser.evaluate("""()=>{
      window.__realTrial={clickedAt:null,observations:[],beforeAssistantCount:document.querySelectorAll('.message.assistant .markdown-answer').length};
      document.addEventListener('click',event=>{if(event.target.closest('button')?.textContent.trim()==='发送')window.__realTrial.clickedAt=performance.now()},{once:true,capture:true});
      window.__realTrialObserver?.disconnect();
      window.__realTrialObserver=new MutationObserver(()=>{
        const blocks=[...document.querySelectorAll('.message.assistant .markdown-answer')];
        const text=blocks.at(-1)?.textContent||'';
        const result=window.__realTrial;
        if(blocks.length<=result.beforeAssistantCount)return;
        if(result.clickedAt!==null && text && text!=='正在等待内容…' && result.observations.at(-1)?.text!==text)
          result.observations.push({at_ms:Math.round(performance.now()-result.clickedAt),text,chars:text.length});
      });
      window.__realTrialObserver.observe(document.querySelector('.messages'),{subtree:true,childList:true,characterData:true});
      return true;
    }""")


def wait_completed(browser: Browser, chat_id: str, expected: int) -> list[dict]:
    deadline = time.monotonic() + 340
    while time.monotonic() < deadline:
        result = api(browser, f"/chats/{chat_id}")["body"]
        generations = result["generations"]
        if len(generations) >= expected:
            state = generations[-1]["state"]
            if state in {"completed", "failed", "cancelled", "unknown", "truncated"}:
                if state != "completed":
                    raise AssertionError(
                        f"REAL_MODEL_TERMINAL_{state}: {generations[-1].get('error_code')}"
                    )
                browser.command(
                    "run-code",
                    "async(page)=>{await page.waitForFunction(()=>!document.querySelector('button[aria-label=\"停止回答\"]'));}",
                )
                return generations
        # An explicit DOM condition waits for a projection change; no fabricated stream.
        browser.command("run-code", "async(page)=>{await page.waitForTimeout(500);}")
    raise TimeoutError("REAL_MODEL_DID_NOT_COMPLETE_WITHIN_OBSERVATION_WINDOW")


def run(args: argparse.Namespace) -> dict:
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    spec = importlib.util.spec_from_file_location(
        "real_trial_media", ROOT / "tests/fixtures/mvp_a/generate.py"
    )
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    manifest = generator.generate(directory / "synthetic", ffmpeg=args.ffmpeg_path)
    before = code_digests()
    before["tests/e2e/test_real_trial.py"] = hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest()
    instance = Instance(
        directory / "server", "start", args.backend_port, args.backend_port
    )
    instance.env.update(
        {
            "LEARNING_APP_HARNESS_ENABLED": "true",
            "LEARNING_APP_MODEL": "deepseek-flash",
            "LEARNING_APP_THINKING": "high",
            "LEARNING_APP_MAX_OUTPUT_TOKENS": "16384",
            "LEARNING_APP_MODEL_TIMEOUT_SECONDS": "300",
        }
    )
    browser = Browser(directory / "browser", args.browser_path)
    result = {
        "id": args.run_id,
        "status": "running",
        "evidence_level": "real Chromium + formal launcher + real Harness + official provider",
        "input_scope": "synthetic generated coordinate video only; no registered user video opened",
        "configuration": {
            "model": "deepseek-flash",
            "thinking": "high",
            "max_output_tokens": 16384,
            "timeout_seconds": 300,
        },
        "synthetic_manifest": manifest,
        "code_sha256_before": before,
        "assertions": [],
        "not_covered": [
            "real body cancellation",
            "human learning value",
            "human useful-content timing",
            "unapproved real video frames",
        ],
    }
    crop_hash = ""
    try:
        instance.start()
        browser.command("open", instance.url, f"--config={browser.config}")
        browser.wait_text("后台已连接")
        check(
            api(browser, "/health")["body"]["capabilities"]["question_answering"]
            == "ready",
            "real_Harness_ready",
            result,
        )
        fill(browser, "新课程名称", "真实模型与合成截图试用")
        click(browser, "新建")
        courses = api(browser, "/courses")["body"]
        course = courses[-1]["course_id"]
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('combobox',{name:'选择课程'}).selectOption("
            + json.dumps(course)
            + ");}",
        )
        browser.snapshot("before-local-association")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByText('用文件路径关联视频',{exact:true}).click();}",
        )
        fill(browser, "视频路径", manifest["video"])
        click(browser, "关联视频")
        browser.wait_text("synthetic-course.webm")
        click(browser, "synthetic-course.webm")
        wait_video(browser)
        click(browser, "＋ 新对话")
        browser.wait_text("课程对话")
        chat_id = api(browser, f"/courses/{course}/chats")["body"][-1]["chat_id"]
        result["chat_id"] = chat_id
        fill(
            browser,
            "问题或追问",
            "请解答我确认图片中的二次方程，用中文给出因式分解、求根公式两种方法，并逐步解释与代入验证。公式使用LaTeX排版；只依据这张图与文字，不要补其他画面，不联网。约400字完整回答。",
        )
        click(browser, "截图提问")
        check(video_state(browser)["paused"], "user_screenshot_pauses_video", result)
        set_crop(browser, 160, 90, 320, 180)
        click(browser, "确认选区并加入草稿")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByAltText('待发送的确认图片 1').waitFor({state:'visible'});}",
        )
        attachment = browser.evaluate(
            "async()=>{const i=document.querySelector('img[alt=\"待发送的确认图片 1\"]');const r=await fetch(i.src);const b=await r.arrayBuffer();return {id:i.src.split('/').at(-1),width:i.naturalWidth,height:i.naturalHeight,hash:[...new Uint8Array(await crypto.subtle.digest('SHA-256',b))].map(x=>x.toString(16).padStart(2,'0')).join('')}}"
        )
        crop_hash = attachment["hash"]
        result["confirmed_crop"] = attachment
        check(
            attachment["width"] == 320 and attachment["height"] == 180,
            "only_confirmed_crop_in_draft",
            result,
        )
        install_stream_observer(browser)
        click(browser, "发送")
        generations = wait_completed(browser, chat_id, 1)
        browser.command(
            "run-code",
            "async(page)=>{await page.locator('.markdown-answer .katex').first().waitFor({state:'visible'});}",
        )
        result["first_stream"] = browser.evaluate("()=>window.__realTrial")
        check(
            len(result["first_stream"]["observations"]) >= 2,
            "real_incremental_rendering_observed",
            result,
        )
        check(
            browser.evaluate(
                "()=>document.querySelectorAll('.markdown-answer .katex').length"
            )
            > 0,
            "real_response_KaTeX_rendered",
            result,
        )
        browser.snapshot("real-answer")
        browser.command("screenshot", "--filename=real-answer.png")
        result["first_generation"] = generations[0]
        fill(
            browser,
            "问题或追问",
            "继续沿用刚才图片中的同一道题，不重新取图。请用韦达定理核对两个根，并说明为什么没有遗漏其他实数根。约200字，公式用LaTeX。",
        )
        install_stream_observer(browser)
        click(browser, "发送")
        generations = wait_completed(browser, chat_id, 2)
        result["second_stream"] = browser.evaluate("()=>window.__realTrial")
        result["second_generation"] = generations[-1]
        check(len(generations) == 2, "same_business_chat_followup_completed", result)
        fill(browser, "问题或追问", "未发送的续问草稿：下一次解释判别式。")
        click(browser, "保存草稿")
        browser.wait_text("草稿已保存")
        saved = api(browser, f"/chats/{chat_id}")["body"]
        browser.command("reload")
        browser.wait_text("后台已连接")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('combobox',{name:'选择课程'}).selectOption("
            + json.dumps(course)
            + ");}",
        )
        open_chat(browser, chat_id)
        browser.command(
            "run-code",
            "async(page)=>{await page.waitForFunction(()=>document.querySelector('#question-input')?.value==='未发送的续问草稿：下一次解释判别式。');}",
        )
        reopened = api(browser, f"/chats/{chat_id}")["body"]
        check(
            saved == reopened,
            "messages_generations_and_confirmed_attachment_reopen_identically",
            result,
        )
        check(
            len(reopened["messages"]) == 4,
            "two_user_and_two_assistant_messages_persisted",
            result,
        )
        result["final_chat"] = reopened
        result["boundary"] = boundary_evidence(instance.directory, crop_hash)
        check(
            result["boundary"]["actual_outbound_attempts"] == 2,
            "two_actual_real_attempts_no_hidden_retry",
            result,
        )
        check(
            all(
                row["all_images_are_confirmed_crop"] and row["images"]
                for row in result["boundary"]["ledger_audits"]
            ),
            "every_real_request_image_is_confirmed_crop_hash",
            result,
        )
        click(browser, "synthetic-course.webm")
        wait_video(browser)
        check(
            not video_state(browser)["paused"],
            "explicit_video_reopen_continues_playback_after_chat",
            result,
        )
        click(browser, "安全退出")
        wait_until(lambda: port_closed(args.backend_port), timeout=30)
        result["status"] = "passed"
    except BaseException as error:
        result.update(status="failed", failure=f"{type(error).__name__}: {error}")
        try:
            browser.snapshot("failure")
        except Exception:
            pass
    finally:
        result["instance_cleanup"] = instance.stop()
        result["browser_cleanup"] = browser.close()
        if crop_hash:
            result["boundary"] = boundary_evidence(instance.directory, crop_hash)
        after = code_digests()
        result["source_changed_during_run"] = [
            path for path in before if path in after and before[path] != after[path]
        ]
        result["code_sha256_after"] = after
        if not result["instance_cleanup"].get("backend_port_closed") or result[
            "browser_cleanup"
        ].get("remaining_pids"):
            result["status"] = "failed"
        write_json(directory / "result.json", result)
        write_json(ROOT / "research/reports/v3" / (args.run_id + ".json"), result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="real-ui-20261021-r1")
    parser.add_argument("--backend-port", type=int, default=18797)
    parser.add_argument(
        "--browser-path",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe",
    )
    parser.add_argument(
        "--ffmpeg-path",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", ""))
        / "Microsoft/WinGet/Links/ffmpeg.EXE",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("run-id仅允许英文数字下划线横线")
    if not port_closed(args.backend_port):
        raise RuntimeError("PORT_IN_USE: 不接管其他进程")
    result = run(args)
    print(
        json.dumps(
            {
                "status": result["status"],
                "failure": result.get("failure"),
                "evidence": "research/reports/v3/" + args.run_id + ".json",
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
