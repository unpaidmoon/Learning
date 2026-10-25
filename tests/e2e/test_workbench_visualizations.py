"""Actual browser workers, controls, animation and durable graph revisions."""

import argparse
from copy import deepcopy
import hashlib
import json
import os
import re
from pathlib import Path

from test_browser import Browser, ROOT, check, port_closed, write_json
from test_desktop_migration import create_course, wait_api
from test_mvp_a import api
from test_workbench_reading import digests
from test_workbench_workspace import WorkspaceInstance
from test_workspace_v2 import manager_entry


def run_case(directory, executable, port):
    materials = directory / "synthetic-course"
    materials.mkdir()
    (materials / "migration-read.txt").write_text("Synthetic setup only.", "utf-8")
    original = materials / "lesson.txt"
    original.write_text(
        "函数y=a*x²；a=1、x=1时y=1，a=2时y=2。先平方，再乘系数。", "utf-8"
    )
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    result = {
        "status": "running",
        "assertions": [],
        "source_digests_before": digests(),
        "layer": "actual Chromium and actual Web Workers + pinned native Harness + synthetic SSE",
        "actual_model_attempts": 0,
        "user_learning_experience": "not_run",
    }
    instance, browser = (
        WorkspaceInstance(directory / "server", port),
        Browser(directory / "browser", executable),
    )
    try:
        instance.start()
        browser.command(
            "open", instance.url + "/desktop/", f"--config={browser.config}"
        )
        browser.snapshot("initial-native")
        course, _ = create_course(
            browser, instance, {"course_root": str(materials)}, result
        )
        manager_entry(browser, "lesson.txt", double=True)
        browser.command(
            "run-code",
            "async(page)=>{await page.getByText('函数y=a*x²；a=1、x=1时y=1，a=2时y=2。先平方，再乘系数。',{exact:true}).waitFor();await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('button',{name:'整理资料',exact:true}).click();const form=page.getByRole('region',{name:'发起学习任务'});await form.getByRole('combobox',{name:'成果类型',exact:true}).selectOption('visualization');await form.getByRole('textbox',{name:'标题',exact:true}).fill('合成函数与动画');await form.getByRole('textbox',{name:'具体要求',exact:true}).fill('保存静态图、系数互动和动画，各自保留来源。');await form.getByRole('button',{name:'开始生成并保存',exact:true}).click();}",
        )
        prefix = f"/courses/{course}/learning"
        tasks = wait_api(
            browser,
            prefix + "/tasks",
            "value.length===1&&value[0].state==='completed'",
            timeout_ms=40000,
        )
        target = api(browser, prefix + f"/tasks/{tasks[0]['task_id']}/coverage")[
            "body"
        ]["target"]
        artifact_id = target["artifact_id"]
        artifact = api(browser, prefix + f"/artifacts/{artifact_id}")["body"]
        check(
            artifact["kind"] == "visualization"
            and len(artifact["revision"]["body"]["blocks"]) == 3
            and artifact["indexed"],
            "explicit_generation_saves_three_typed_visuals_and_source_chain",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'阅读成果',exact:true}).first().click();const plot=page.getByRole('figure',{name:'参数与函数',exact:true});await plot.scrollIntoViewIfNeeded();await plot.getByText('f(1) = 1；此处斜率 = 2',{exact:true}).waitFor({timeout:15000});if(page.workers().length<1||page.workers().length>4)throw Error('REAL_WORKER_BUDGET_INVALID');await page.getByRole('img',{name:'从输入经过变换得到结果',exact:true}).waitFor();}",
        )
        check(
            browser.evaluate(
                "()=>Number(document.querySelector('figure[aria-label=\"参数与函数\"] svg[data-worker-frame]').dataset.workerFrame)>0"
            ),
            "visible_function_calculated_by_actual_bounded_browser_worker",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{const plot=page.getByRole('figure',{name:'参数与函数',exact:true});await plot.getByRole('slider',{name:'系数a',exact:true}).focus();await plot.getByRole('slider',{name:'系数a',exact:true}).press('End');await plot.getByText('f(1) = 2；此处斜率 = 4',{exact:true}).waitFor({timeout:10000});}",
        )
        check(True, "changing_a_updates_actual_curve_and_analytic_probe_value", result)
        browser.command(
            "run-code",
            "async(page)=>{const animation=page.getByRole('figure',{name:'系数动画',exact:true});const svg=animation.locator('svg[data-worker-frame]');const initial=Number(await svg.getAttribute('data-worker-frame'));await animation.getByRole('button',{name:'播放动画',exact:true}).click();await page.waitForFunction(n=>Number(Array.from(document.querySelectorAll('svg[data-worker-frame]')).at(-1).dataset.workerFrame)>n,initial+3,{timeout:10000});await animation.getByRole('button',{name:'暂停动画',exact:true}).click();await animation.getByRole('button',{name:'播放动画',exact:true}).waitFor();}",
        )
        check(
            True,
            "user_started_animation_produces_new_worker_frames_and_can_pause",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('figure',{name:'参数与函数',exact:true}).screenshot({path:"
            + json.dumps(str(browser.directory / "parameter-plot.png"))
            + "});await page.getByRole('figure',{name:'输入与结果',exact:true}).screenshot({path:"
            + json.dumps(str(browser.directory / "static-diagram.png"))
            + "});}",
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'文件',exact:true}).click();const until=Date.now()+5000;while(page.workers().length&&Date.now()<until)await page.waitForTimeout(50);if(page.workers().length)throw Error('HIDDEN_MATH_WORKERS_REMAIN');await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('figure',{name:'参数与函数',exact:true}).scrollIntoViewIfNeeded();}",
        )
        check(True, "hiding_the_workbench_releases_actual_math_workers", result)
        forged = {
            "operation_id": "bad_renderer",
            "kind": "visualization",
            "title": "拒绝越权渲染",
            "body": deepcopy(artifact["revision"]["body"]),
        }
        forged["body"]["blocks"][0]["visualization"]["renderer"].update(
            target="iframe", entry_url="/api/v1/session"
        )
        response = api(browser, prefix + "/artifacts", forged, method="POST")
        check(
            response["status"] == 422,
            "authenticated_write_still_rejects_iframe_and_business_url_payload",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'编辑',exact:true}).click();await page.getByRole('textbox',{name:'函数表达式',exact:true}).first().fill('a*x^3');await page.getByRole('button',{name:'保存修改',exact:true}).click();await page.getByText('已保存新版本',{exact:true}).waitFor();await page.getByText('y = a*x^3',{exact:true}).waitFor();}",
        )
        updated = api(browser, prefix + f"/artifacts/{artifact_id}")["body"]
        plot = updated["revision"]["body"]["blocks"][1]["visualization"]["payload"][
            "data"
        ]
        check(
            updated["head_revision_id"] != artifact["head_revision_id"]
            and updated["content_role"] == "mixed"
            and plot["expression_tree"]["right"]["right"]["value"] == 3,
            "formula_edit_recompiles_the_tree_in_a_new_mixed_revision",
            result,
        )
        browser.command("reload")
        browser.snapshot("after-reload")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('button',{name:/合成函数与动画/}).click();await page.getByText('y = a*x^3',{exact:true}).waitFor();await page.getByRole('figure',{name:'系数动画',exact:true}).getByRole('button',{name:'播放动画',exact:true}).waitFor();}",
        )
        check(
            api(browser, prefix + f"/artifacts/{artifact_id}")["body"][
                "head_revision_id"
            ]
            == updated["head_revision_id"],
            "reopen_restores_saved_graph_and_keeps_animation_paused",
            result,
        )
        check(
            len(api(browser, prefix + "/tasks")["body"]) == 1
            and len(api(browser, prefix + "/artifacts")["body"]) == 1,
            "graph_controls_edit_and_reopen_do_not_dispatch_model_or_create_extra_products",
            result,
        )
        check(
            hashlib.sha256(original.read_bytes()).hexdigest() == original_hash,
            "source_material_preserved",
            result,
        )
        result["status"] = "passed"
    except BaseException as error:
        result.update(status="failed", failure=f"{type(error).__name__}: {error}")
        try:
            browser.snapshot("failure")
            browser.command(
                "screenshot",
                f"--filename={browser.directory / 'failure.png'}",
                "--full-page",
            )
        except Exception:
            pass
    finally:
        result["browser_cleanup"] = browser.close()
        result["cleanup"] = instance.stop()
        result["source_digests_after"] = digests()
        result["source_changed_during_run"] = (
            result["source_digests_before"] != result["source_digests_after"]
        )
        if result["source_changed_during_run"]:
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--backend-port", type=int, default=19246)
    parser.add_argument(
        "--browser-path",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id) or not port_closed(
        args.backend_port
    ):
        raise RuntimeError("RUN_ID_INVALID_OR_PORT_IN_USE")
    output = ROOT / "output/playwright" / args.run_id
    output.mkdir(parents=True, exist_ok=False)
    result = run_case(output, args.browser_path, args.backend_port)
    print(
        json.dumps(
            {
                "status": result["status"],
                "assertions": len(result["assertions"]),
                "evidence": str(output / "result.json"),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if result["status"] == "passed" else 1)
