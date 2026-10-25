"""Failed initial draft GET -> backend death -> read-only reconnect -> edit/send."""

import argparse
import json
import os
from pathlib import Path

from test_backend_reconnect import Instance, ledger
from test_browser import Browser, ROOT, check, http, port_closed, wait_until, write_json
from test_data_transfer import source_digests
from test_desktop import composer_fill
from test_desktop_migration import wait_api
from test_execution_degradation import api


def run(directory, executable, port):
    from learning_app.course_files import CourseFileStore
    from learning_app.storage import Repository

    materials = directory / "synthetic-course"
    materials.mkdir()
    (materials / "lesson.txt").write_text("Owned draft reconnection fixture.", "utf-8")
    result = {
        "status": "running",
        "assertions": [],
        "source_before": source_digests(),
        "layer": "actual pinned native Host + outer backend death + Chromium; aborted initial draft GET",
        "actual_cloud_requests": 0,
        "user_learning_experience": "not_run",
    }
    instance, browser = (
        Instance(directory / "server", port),
        Browser(directory / "browser", executable),
    )
    try:
        instance.start()
        repo = Repository(instance.directory / "data")
        try:
            course = CourseFileStore(repo).bind(None, str(materials), "草稿接回验证")[
                "course_id"
            ]
        finally:
            repo.close()
        browser.command("open", "about:blank", f"--config={browser.config}")
        browser.command(
            "run-code",
            "async(page)=>{let first=true;await page.route('**/api/v1/chats/*/draft',async route=>{if(first&&route.request().method()==='GET'){first=false;await route.abort('connectionrefused');}else await route.continue();});await page.goto("
            + json.dumps(instance.url + "/desktop/")
            + ");await page.getByRole('textbox',{name:'输入问题',exact:true}).waitFor();await page.getByRole('alert').filter({hasText:/DRAFT_NOT_READY|草稿尚未接回/}).waitFor();}",
        )
        check(
            True,
            "actual_native_draft_read_failure_is_observed_before_backend_crash",
            result,
        )
        mapping = api(browser, "/desktop/courses/" + course + "/open", {})
        original = "UNSAVED_NATIVE_DRAFT_AFTER_READ_FAILURE"
        composer_fill(browser, original)
        baseline = api(browser, "/chats/" + mapping["chat_id"] + "/draft")
        before = api(browser, "/health")
        browser.command(
            "run-code",
            r"async(page)=>{await page.evaluate(()=>{window.__draftSameDocument=true;window.__draftPostCount=0;});page.on('request',async request=>{if(request.method()==='POST'&&/\/api\/v1\/chats\/[^/]+\/draft$/.test(request.url()))await page.evaluate(()=>{window.__draftPostCount++;}).catch(()=>{});});}",
        )
        instance.process.stdin.write("crash-backend\n")
        instance.process.stdin.flush()

        def recovered():
            try:
                health = http(instance.url + "/api/v1/health")[2]
                return (
                    health
                    and health["instance_id"] != before["instance_id"]
                    and health["capabilities"]["question_answering"] == "ready"
                )
            except OSError:
                return False

        wait_until(recovered, timeout=65)
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('alert').filter({hasText:/DRAFT_NOT_READY|草稿尚未接回/}).waitFor({state:'hidden',timeout:30000});if(!await page.evaluate(()=>window.__draftSameDocument))throw Error('DRAFT_PAGE_RELOADED');if((await page.locator('[data-composer-input]').innerText()).trim()!=="
            + json.dumps(original)
            + ")throw Error('NATIVE_DRAFT_LOST');if(await page.evaluate(()=>window.__draftPostCount)!==0)throw Error('DRAFT_WRITE_REPLAYED_DURING_RECOVERY');}",
        )
        check(
            True,
            "same_native_draft_reconnects_and_clears_transport_error_without_replaying_save",
            result,
        )
        check(
            api(browser, "/chats/" + mapping["chat_id"] + "/draft") == baseline,
            "server_draft_and_revision_are_unchanged_by_read_only_recovery",
            result,
        )
        edited = "EXPLICIT_EDIT_AFTER_DRAFT_RECOVERY"
        composer_fill(browser, edited)
        wait_api(
            browser,
            "/chats/" + mapping["chat_id"] + "/draft",
            "value.text===" + json.dumps(edited),
        )
        check(
            True,
            "new_user_edit_persists_after_failed_loading_and_work_chains_are_reconnected",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.locator('[data-composer-input]').press('Enter');}",
        )
        wait_api(
            browser,
            "/chats/" + mapping["chat_id"],
            "value.generations.length===1&&value.generations[0].state==='completed'",
        )
        counts = ledger(instance.directory)
        check(
            len(counts["request_ids"]) == 1 and counts["actual_cloud_attempts"] == 0,
            "only_the_explicit_send_creates_one_local_model_request",
            result,
        )
        browser.command(
            "screenshot", f"--filename={directory / 'draft-recovered-and-sent.png'}"
        )
        result["status"] = "passed"
    except Exception as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
        try:
            browser.snapshot("failure")
        except Exception:
            pass
    finally:
        result["browser_cleanup"] = browser.close()
        result["cleanup"] = instance.stop()
        result["source_after"] = source_digests()
        if (
            result["source_before"] != result["source_after"]
            or not result["cleanup"]["backend_port_closed"]
        ):
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--backend-port", type=int, default=19427)
    parser.add_argument(
        "--browser-path",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe",
    )
    args = parser.parse_args()
    if not args.run_id.replace("-", "").replace("_", "").isalnum() or not port_closed(
        args.backend_port
    ):
        raise RuntimeError("OWNED_DRAFT_TARGET_INVALID")
    target = ROOT / "output/playwright" / args.run_id
    target.mkdir(parents=True, exist_ok=False)
    record = run(target, args.browser_path, args.backend_port)
    print(
        json.dumps(
            {
                "status": record["status"],
                "assertions": len(record["assertions"]),
                "error": record.get("error"),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if record["status"] == "passed" else 1)
