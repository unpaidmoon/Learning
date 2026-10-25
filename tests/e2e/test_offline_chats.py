"""Actual file:// chat reader, hashed raw objects, math and no background/network."""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
from pathlib import Path

from PIL import Image
from test_browser import Browser, ROOT, check, write_json

from learning_app.data_snapshot import BusinessSnapshot
from learning_app.offline_chats import OfflineChats
from learning_app.storage import Repository


def seed(directory):
    repo = Repository(directory / "source-data")
    course = repo.create_course("离线课程fixture")["course_id"]
    repo.create_chat(course, "saved_chat")
    binding = repo.bind_execution(
        "saved_chat",
        "saved_sid",
        adapter_metadata={"desktop_title_override": "公式与确认图"},
    )
    attachments = []
    for kind, color in [("PNG", "#1245ab"), ("JPEG", "#168040")]:
        stream = io.BytesIO()
        Image.new("RGB", (80, 50), color).save(stream, kind)
        attachments.append(
            repo.stage_attachment(
                stream.getvalue(), "image/png" if kind == "PNG" else "image/jpeg"
            )
        )
    submitted = repo.publish_submission(
        "saved_chat",
        "请看这两张已提交图片，解释 $x^2$。 </script><script>globalThis.INJECTED=true</script>",
        "saved_input",
        attachment_ids=[a["attachment_id"] for a in attachments],
    )
    repo.mark_dispatch_started(
        submitted["generation_id"], binding["binding_id"], binding["epoch"]
    )
    repo.ingest_event(
        submitted["generation_id"],
        binding_id=binding["binding_id"],
        epoch=binding["epoch"],
        event_id="partial",
        seq=1,
        kind="text_delta",
        text="保留部分：$x^2$ 是平方，原结果仍待核对。",
    )
    repo.mark_unknown(submitted["generation_id"], "owned_disconnect_fixture")
    repo.create_chat(course, "empty_chat")
    package = directory / "package"
    package.mkdir()
    with BusinessSnapshot(repo).capture() as cut:
        shutil.copytree(cut.directory, package / "data")
        OfflineChats().write(cut, package / "offline")
    repo.close()
    return package


def run(directory, executable):
    package = seed(directory)
    db = package / "data/business.sqlite3"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    browser = Browser(directory / "browser", executable)
    result = {
        "status": "running",
        "layer": "actual Chromium file reader + consistent local saved-data fixture",
        "assertions": [],
        "actual_provider_calls": 0,
        "host_calls": 0,
        "user_learning_experience": "not_run",
    }
    try:
        browser.command("open", "about:blank", f"--config={browser.config}")
        browser.command(
            "run-code",
            "async(page)=>{await page.context().setOffline(true);await page.context().route('**/*',async(route)=>{if(/^https?:/.test(route.request().url()))await route.abort();else await route.continue();});}",
        )
        browser.command("requests", "--clear")
        browser.command(
            "run-code",
            "async(page)=>{await page.goto("
            + json.dumps((package / "offline/index.html").resolve().as_uri())
            + ");await page.getByRole('heading',{name:'聊天记录',exact:true}).waitFor();await page.getByRole('link',{name:'公式与确认图',exact:true}).click();await page.getByRole('heading',{name:'公式与确认图',exact:true}).waitFor();}",
        )
        check(True, "course_index_and_saved_manual_title_open_without_backend", result)
        browser.command(
            "run-code",
            "async(page)=>{await page.getByText(/原结果待核对/).waitFor();if(await page.locator('.katex').count()<2)throw Error('OFFLINE_CHAT_MATH');const images=page.locator('img');if(await images.count()!==2)throw Error('CHAT_IMAGES');await images.first().scrollIntoViewIfNeeded();await page.waitForFunction(()=>Array.from(document.images).every(img=>img.complete&&img.naturalWidth===80));if(await page.evaluate(()=>globalThis.INJECTED===true))throw Error('HTML_INJECTION');}",
        )
        check(
            True, "partial_unknown_answer_math_and_png_jpeg_raw_objects_render", result
        )
        browser.command("screenshot", f"--filename={directory / 'offline-chat.png'}")
        requests = browser.command("requests", "--static", "--filter=^https?://")
        check(
            not re.search(r"\bhttps?://", requests),
            "chat_reader_makes_no_http_requests",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('link',{name:'返回课程与聊天',exact:true}).click();await page.getByRole('link',{name:'新对话',exact:true}).click();await page.getByRole('heading',{name:'新对话',exact:true}).waitFor();}",
        )
        check(True, "empty_saved_chat_still_opens", result)
        browser.command("requests", "--clear")
        browser.command(
            "run-code",
            "async(page)=>{await page.goto('https://offline-chat-monitor.invalid/calibration').catch(()=>null);}",
        )
        check(
            "calibration"
            in browser.command("requests", "--static", "--filter=^https?://"),
            "network_observer_calibrated_after_measuring_reader",
            result,
        )
        check(
            hashlib.sha256(db.read_bytes()).hexdigest() == before,
            "offline_reader_does_not_change_business_data",
            result,
        )
        result["status"] = "passed"
    except Exception as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
        try:
            browser.snapshot("failure")
        except Exception:
            pass
    finally:
        result["cleanup"] = browser.close()
        write_json(directory / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--browser-path",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        raise RuntimeError("OWNED_RUN_ID_INVALID")
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    result = run(directory, args.browser_path)
    print(
        json.dumps(
            {
                "status": result["status"],
                "assertions": len(result["assertions"]),
                "error": result.get("error"),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if result["status"] == "passed" else 1)
