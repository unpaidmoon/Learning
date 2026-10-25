"""Cold whole-package import in the actual native UI, local provider only."""

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from PIL import Image
from test_browser import (
    Browser,
    PYTHON,
    ROOT,
    check,
    http,
    port_closed,
    wait_until,
    write_json,
)
from test_desktop import (
    DesktopInstance,
    composer_fill,
    press_button,
    source_digests as base_digests,
)
from test_desktop_migration import wait_api
from test_mvp_a import api


def source_digests():
    values = base_digests()
    paths = [Path(__file__).resolve()]
    for folder in ("frontend/src/data", "resources/data"):
        paths.extend(p for p in (ROOT / folder).rglob("*") if p.is_file())
    paths.extend((ROOT / ".runtime/desktop/client").glob("library-*"))
    paths.append(ROOT / "resources/workbench/offline-chat-runtime.lock.json")
    for path in paths:
        values[path.relative_to(ROOT).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return values


def verify_offline(directory, executable, result):
    from learning_app.data_package import DataPackage

    target = directory / "exported-offline"
    verified = DataPackage().stage(directory / "downloaded.zip", target)
    database = target / "data/business.sqlite3"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    browser = Browser(directory / "offline-browser", executable)
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
            + json.dumps((target / "offline/index.html").resolve().as_uri())
            + ");await page.getByRole('link',{name:'导入未知历史',exact:true}).click();await page.getByText('UNKNOWN_ANSWER_000 saved',{exact:false}).waitFor();await page.getByText(/原结果待核对/).waitFor();if(!await page.locator('.katex').count())throw Error('MATH_NOT_RENDERED');await page.waitForFunction(()=>Array.from(document.images).some(img=>img.complete&&img.naturalWidth===80));}",
        )
        check(
            True,
            "downloaded_whole_package_opens_with_backend_stopped_and_network_disabled",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('link',{name:'返回课程与聊天',exact:true}).click();await page.getByRole('link',{name:'导入完成历史',exact:true}).click();await page.getByText('COMPLETE_USER_000 explain',{exact:false}).waitFor();await page.getByRole('link',{name:'下一页消息',exact:true}).click();await page.getByText('COMPLETE_ANSWER_052 saved',{exact:false}).waitFor();}",
        )
        check(
            True,
            "downloaded_offline_chat_pages_keep_original_first_and_last_message",
            result,
        )
        observed = browser.command("requests", "--static", "--filter=^https?://")
        check(
            not re.search(r"\bhttps?://", observed),
            "whole_export_reader_makes_no_http_request",
            result,
        )
        browser.command("screenshot", f"--filename={directory / 'offline-export.png'}")
        browser.command("requests", "--clear")
        browser.command(
            "run-code",
            "async(page)=>{await page.goto('https://whole-export-monitor.invalid/calibration').catch(()=>null);}",
        )
        check(
            "calibration"
            in browser.command("requests", "--static", "--filter=^https?://"),
            "whole_export_network_observer_calibrated",
            result,
        )
        check(
            before == hashlib.sha256(database.read_bytes()).hexdigest(),
            "offline_reader_does_not_modify_exported_business_database",
            result,
        )
        result["offline_package_id"] = verified.manifest["package_id"]
    finally:
        result["offline_browser_cleanup"] = browser.close()


def seed(directory):
    from learning_app.course_files import CourseFileStore
    from learning_app.data_import import prepare_import
    from learning_app.data_location import ProfileLocation
    from learning_app.data_package import DataPackage
    from learning_app.storage import Repository

    repo = Repository(directory / "source-data")
    materials = directory / "synthetic-materials"
    materials.mkdir()
    (materials / "lesson.txt").write_text("Self-created course $x^2$.", "utf-8")
    (materials / "unopened.txt").write_text("Unopened course original.", "utf-8")
    files = CourseFileStore(repo)
    course = files.bind(None, str(materials), "数据迁移验证课程")["course_id"]
    files.open(course, "lesson.txt")
    picture = io.BytesIO()
    Image.new("RGB", (80, 40), "#1234af").save(picture, "PNG")
    image = repo.stage_attachment(picture.getvalue(), "image/png")["attachment_id"]
    generations = {}
    for kind in ("complete", "unknown"):
        chat = "saved_" + kind
        repo.create_chat(course, chat)
        binding = repo.bind_execution(
            chat,
            "old_" + kind,
            adapter_metadata={
                "desktop_title_override": "导入完成历史"
                if kind == "complete"
                else "导入未知历史"
            },
        )
        for index in range(53 if kind == "complete" else 1):
            result = repo.publish_submission(
                chat,
                f"{kind.upper()}_USER_{index:03d} explain $x^2$",
                f"input_{kind}_{index}",
                attachment_ids=[image] if index == 0 else [],
            )
            gid = result["generation_id"]
            repo.claim_dispatch(gid, binding["binding_id"], binding["epoch"])
            repo.ingest_event(
                gid,
                binding_id=binding["binding_id"],
                epoch=binding["epoch"],
                event_id="saved_" + gid,
                seq=1,
                kind="body",
                text=f"{kind.upper()}_ANSWER_{index:03d} saved $x^2$ prefix",
            )
            if kind == "complete":
                repo.ingest_event(
                    gid,
                    binding_id=binding["binding_id"],
                    epoch=binding["epoch"],
                    event_id="end_" + gid,
                    seq=2,
                    kind="terminal",
                    status="completed",
                )
            else:
                repo.mark_unknown(gid, "saved_fixture_disconnect")
            generations[kind] = gid
    archive = directory / "saved-package.zip"
    stream, manifest = DataPackage(repo).build(include_originals=True)
    with stream, archive.open("wb") as output:
        import shutil

        shutil.copyfileobj(stream, output)
    before_unknown = repo.get_generation(generations["unknown"])
    source_hash = hashlib.sha256(repo.db_path.read_bytes()).hexdigest()
    initial = directory / "data"
    initial.mkdir()
    location = ProfileLocation(initial)
    plan = location.plan("cold_import", "import", directory)
    target = Path(plan["target"])
    prepare_import(
        archive,
        Path(plan["stage"]),
        target / "data",
        final_materials=target / "materials",
    )
    location.ready(plan)
    location.schedule("cold_import")
    repo.close()
    with location.lease():
        location.apply_closed()
    write_json(
        directory / "seed.json",
        {
            "course_id": course,
            "package_id": manifest["package_id"],
            "source_database_sha256": source_hash,
            "unknown_generation_id": generations["unknown"],
            "unknown_before": {
                k: before_unknown[k]
                for k in ("state", "body", "cancel_requested", "outbound_attempt_id")
                if k in before_unknown
            },
            "current_data": str(location.current()),
            "image_id": image,
        },
    )
    return location.current()


async def serve(directory, port):
    import uvicorn
    from learning_app.app import create_app
    from learning_app.config import AppConfig
    from learning_app.execution.adapter import ExecutionAdapter

    active_data = seed(directory)
    adapter = ExecutionAdapter(
        ROOT,
        active_data / "execution",
        mode="fixture",
        thinking="none",
        timeout=60,
        desktop_enabled=True,
    )
    await adapter.open()
    application = create_app(
        AppConfig(
            mode="test",
            backend_port=port,
            frontend_port=port,
            data_dir=directory / "data",
            runtime_dir=directory / "runtime",
            desktop_enabled=True,
            thinking="none",
            secret_file=directory / "NO_SECRETS",
        ),
        execution=adapter,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host="127.0.0.1",
            port=port,
            access_log=False,
            timeout_graceful_shutdown=5,
        )
    )

    def parent():
        for line in sys.stdin:
            if line.strip() == "shutdown":
                break
        server.should_exit = True

    threading.Thread(target=parent, daemon=True).start()
    try:
        await server.serve()
    finally:
        write_json(
            directory / "provider-observations.json",
            {
                "count": len(adapter.boundary.records),
                "requests": [
                    {
                        k: row[k]
                        for k in ("generation_id", "request_id", "status")
                        if k in row
                    }
                    for row in adapter.boundary.records
                ],
            },
        )
        write_json(directory / "worker-cleanup.json", await adapter.close())
    return 0


class Instance(DesktopInstance):
    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log = (self.directory / "server.stdout.txt").open("w", encoding="utf-8")
        self.process = self.group.spawn(
            [
                str(PYTHON),
                str(Path(__file__).resolve()),
                "--serve-fixture",
                str(self.directory),
                "--backend-port",
                str(self.backend),
            ],
            cwd=ROOT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )

        def ready():
            if self.process.poll() is not None:
                raise RuntimeError("DATA_FIXTURE_START_FAILED")
            try:
                return http(self.url + "/api/v1/health")[2]["ready"]
            except OSError:
                return False

        wait_until(ready, timeout=90)
        return self


def run(directory, executable, port):
    result = {
        "status": "running",
        "assertions": [],
        "source_digests_before": source_digests(),
        "layer": "actual Chromium + fixed native Harness + whole ZIP and cold imported business data",
        "cloud_provider_requests": 0,
        "human_learning_experience": "not_run",
    }
    instance = Instance(directory / "server", port)
    browser = Browser(directory / "browser", executable)
    try:
        instance.start()
        seeded = json.loads((instance.directory / "seed.json").read_text("utf-8"))
        browser.command(
            "open", instance.url + "/desktop/", f"--config={browser.config}"
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByText('UNKNOWN_ANSWER_000 saved',{exact:false}).waitFor();await page.getByText(/旧请求结果仍待核对/).waitFor();if(await page.locator('[data-composer-input]:visible').count())throw Error('HISTORY_COMPOSER_WRITABLE');await page.waitForFunction(()=>Array.from(document.images).some(img=>img.complete&&img.naturalWidth===80));}",
        )
        check(
            True,
            "cold_imported_unknown_text_math_submitted_image_and_readonly_native_view",
            result,
        )
        before = api(browser, "/generations/" + seeded["unknown_generation_id"])
        browser.command(
            "run-code",
            "async(page)=>{await page.reload();await page.getByText('UNKNOWN_ANSWER_000 saved',{exact:false}).waitFor();}",
        )
        check(True, "readonly_unknown_history_survives_real_page_refresh", result)
        press_button(browser, "设置")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('menuitem',{name:'数据管理',exact:true}).click();await page.getByRole('button',{name:'导出全部软件数据',exact:true}).waitFor();}",
        )
        press_button(browser, "导出全部软件数据")
        info = wait_api(
            browser,
            "/data",
            "value.operations.some(row=>row.kind==='export'&&row.state==='completed')",
        )
        exported = next(row for row in info["operations"] if row["kind"] == "export")
        check(
            exported["result"]["include_originals"] is False,
            "actual_settings_export_defaults_to_software_data_and_has_completed_receipt",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{const [download]=await Promise.all([page.waitForEvent('download'),page.getByRole('link',{name:'下载数据包',exact:true}).click()]);await download.saveAs("
            + json.dumps(str(directory / "downloaded.zip"))
            + ");}",
        )
        check(
            (directory / "downloaded.zip").is_file(),
            "native_settings_downloads_the_exact_completed_whole_package",
            result,
        )
        press_button(browser, "关闭数据管理")
        press_button(browser, "选择历史对话")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('menuitem',{name:'对话 1',exact:true}).click();await page.getByText('COMPLETE_ANSWER_052 saved',{exact:false}).waitFor();}",
        )
        check(True, "business_chat_navigation_opens_saved_completed_history", result)
        browser.command(
            "run-code",
            "async(page)=>{for(let i=0;i<12;i++){const found=await page.getByText('COMPLETE_USER_000 explain',{exact:false}).count();if(found)break;await page.locator('[data-session-scroll]').evaluateAll(items=>items.forEach(el=>el.scrollTop=0));await page.mouse.wheel(0,-12000);await page.waitForTimeout(250);}await page.getByText('COMPLETE_USER_000 explain',{exact:false}).waitFor();}",
        )
        check(
            True,
            "native_imported_history_pagination_reaches_first_of_106_messages",
            result,
        )
        browser.command(
            "screenshot", f"--filename={directory / 'imported-history.png'}"
        )
        press_button(browser, "继续提问")
        browser.command(
            "run-code",
            "async(page)=>{await page.locator('[data-composer-input]:visible').waitFor();}",
        )
        composer_fill(
            browser, "fixture-only new question after imported completed history"
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.locator('[data-composer-input]').press('Enter');}",
        )
        saved = wait_api(
            browser,
            "/chats/saved_complete",
            "value.generations.length===54&&value.generations.at(-1).state==='completed'",
        )
        check(
            len(saved["generations"]) == 54,
            "explicit_continue_uses_one_new_native_execution_with_business_history",
            result,
        )
        after = api(browser, "/generations/" + seeded["unknown_generation_id"])
        check(
            before == after,
            "old_unknown_state_and_saved_prefix_never_replayed_or_rewritten",
            result,
        )
        result["status"] = "passed"
    except Exception as error:
        result.update(status="failed", failure=f"{type(error).__name__}: {error}")
        try:
            browser.snapshot("failure")
            browser.command("screenshot", f"--filename={directory / 'failure.png'}")
        except Exception:
            pass
    finally:
        result["browser_cleanup"] = browser.close()
        result["cleanup"] = instance.stop()
        if result["status"] == "passed" and result["cleanup"].get(
            "backend_port_closed"
        ):
            try:
                observations = json.loads(
                    (instance.directory / "provider-observations.json").read_text(
                        "utf-8"
                    )
                )
                check(
                    observations["count"] == 1,
                    "only_one_explicit_local_fixture_request_no_imported_replay",
                    result,
                )
                result["local_fixture_requests"] = observations["count"]
                verify_offline(directory, executable, result)
            except Exception as error:
                result.update(
                    status="failed", failure=f"{type(error).__name__}: {error}"
                )
        result["source_digests_after"] = source_digests()
        result["changed_files"] = [
            key
            for key in result["source_digests_before"]
            if result["source_digests_before"][key]
            != result["source_digests_after"].get(key)
        ]
        if result["changed_files"] or not result["cleanup"].get("backend_port_closed"):
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve-fixture", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--backend-port", type=int, default=19405)
    parser.add_argument(
        "--browser-path",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe",
    )
    args = parser.parse_args()
    if args.serve_fixture:
        raise SystemExit(asyncio.run(serve(args.serve_fixture, args.backend_port)))
    if (
        not args.run_id
        or not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id)
        or not port_closed(args.backend_port)
    ):
        raise RuntimeError("OWNED_TEST_TARGET_INVALID")
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    value = run(directory, args.browser_path, args.backend_port)
    print(
        json.dumps(
            {
                "status": value["status"],
                "assertions": len(value["assertions"]),
                "failure": value.get("failure"),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if value["status"] == "passed" else 1)
