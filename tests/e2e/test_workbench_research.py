"""Native research composition, incremental evidence, mixed editing and external scope."""

import argparse
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


def source_digests():
    result = digests()
    for name in ("test_workbench_research.py", "test_workbench_workspace.py"):
        path = Path(__file__).with_name(name)
        result[path.relative_to(ROOT).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return result


def run_case(directory, executable, port):
    materials = directory / "synthetic-course"
    materials.mkdir()
    (materials / "migration-read.txt").write_text(
        "Synthetic evidence A: verify conditions and matching units before comparing results.\n"
        * 450,
        "utf-8",
    )
    (materials / "second.txt").write_text(
        "资料B补充约束：单位一致，比较时保留原来源。", "utf-8"
    )
    originals = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in materials.iterdir()
    }
    result = {
        "status": "running",
        "assertions": [],
        "source_digests_before": source_digests(),
        "layer": "actual Chromium + pinned native Harness + formal research DB + local SSE and HTTP fixtures",
        "actual_model_attempts": 0,
        "external_http_transport": "fixture",
        "user_learning_experience": "not_run",
    }
    instance, browser = (
        WorkspaceInstance(directory / "server", port),
        Browser(directory / "browser", executable),
    )
    instance.directory.mkdir()
    for marker in ("research-fixture", "hold-research-summary"):
        (instance.directory / marker).write_text("owned fixture gate", "utf-8")
    try:
        instance.start()
        browser.command(
            "open", instance.url + "/desktop/", f"--config={browser.config}"
        )
        course, _ = create_course(
            browser, instance, {"course_root": str(materials)}, result
        )
        prefix = f"/courses/{course}/learning"
        note = api(
            browser,
            prefix + "/artifacts",
            {
                "operation_id": "explicit_source_note",
                "kind": "note",
                "title": "合成对照笔记",
                "body": {
                    "blocks": [
                        {
                            "block_id": "user_note",
                            "kind": "markdown",
                            "markdown": "用户笔记：核对条件与单位，再做比较。",
                            "attribution": "user_authored",
                            "source_snapshot_ids": [],
                        }
                    ]
                },
            },
            method="POST",
        )["body"]["artifact"]
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('button',{name:'整理资料',exact:true}).click();const form=page.getByRole('region',{name:'发起学习任务'});await form.getByRole('combobox',{name:'成果类型',exact:true}).selectOption('research');await form.getByRole('combobox',{name:'资料范围',exact:true}).selectOption('custom');await form.getByRole('checkbox',{name:'migration-read.txt',exact:true}).check();await form.getByRole('list',{name:'已选来源'}).getByRole('listitem').filter({hasText:'migration-read.txt'}).waitFor();await form.getByRole('checkbox',{name:'second.txt',exact:true}).check();await form.getByRole('list',{name:'已选来源'}).getByRole('listitem').filter({hasText:'second.txt'}).waitFor();await form.getByRole('button',{name:'笔记与学习成果',exact:true}).click();await form.getByRole('checkbox',{name:'合成对照笔记 · 用户笔记',exact:true}).check();await form.getByRole('textbox',{name:'标题',exact:true}).fill('多资料研究报告');await form.getByRole('textbox',{name:'具体要求',exact:true}).fill('完整比较指定三份材料，保留用户笔记身份；长材料先留证据再做综合。');await form.getByRole('checkbox',{name:'只依据指定资料',exact:true}).check();await form.getByRole('button',{name:'开始生成并保存',exact:true}).click();}",
        )
        tasks = wait_api(
            browser,
            prefix + "/tasks",
            "value.length===1&&value[0].units.some(u=>u.result_artifact_id)&&value[0].state==='running'",
            timeout_ms=50000,
        )
        task_id = tasks[0]["task_id"]
        progress = wait_api(
            browser,
            prefix + f"/tasks/{task_id}/research",
            "value.checkpoints.some(c=>c.phase==='synthesis'&&c.state==='dispatching')",
            timeout_ms=40000,
        )
        coverage = api(browser, prefix + f"/tasks/{task_id}/coverage")["body"]
        check(
            len(coverage["sources"]) == 3
            and any(
                s["declaration"].get("revision_id") == note["head_revision_id"]
                for s in coverage["sources"]
            ),
            "picker_freezes_exact_two_documents_and_the_selected_note_revision",
            result,
        )
        check(
            not progress["external_reads"] and not progress["allow_external"],
            "local_comparison_grants_no_external_http_or_search",
            result,
        )
        check(
            any(c["phase"] == "synthesis" for c in progress["checkpoints"])
            and tasks[0]["state"] != "completed",
            "long_comparison_waits_for_synthesis_in_the_same_queue",
            result,
        )
        artifact_id = coverage["target"]["artifact_id"]
        first = api(browser, prefix + f"/artifacts/{artifact_id}")["body"]
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'阅读成果',exact:true}).first().click();await page.getByRole('button',{name:'编辑',exact:true}).click();await page.getByRole('textbox',{name:'编辑内容块',exact:true}).first().fill('用户在综合归纳进行时手改了首段。');await page.getByRole('button',{name:'保存修改',exact:true}).click();await page.getByText('已保存新版本',{exact:true}).waitFor();}",
        )
        (instance.directory / "release-research-summary").write_text("release", "utf-8")
        wait_api(
            browser,
            prefix + f"/tasks/{task_id}",
            "value.state==='completed'",
            timeout_ms=40000,
        )
        final = api(browser, prefix + f"/artifacts/{artifact_id}")["body"]
        check(
            final["content_role"] == "mixed"
            and any(
                "用户在综合归纳进行时手改" in b["markdown"]
                for b in final["revision"]["body"]["blocks"]
            ),
            "background_synthesis_appends_without_overwriting_manual_edits",
            result,
        )
        check(
            len(api(browser, prefix + "/artifacts")["body"]) == 2,
            "all_research_batches_publish_one_report_beside_the_existing_note",
            result,
        )
        check(
            final["head_revision_id"] != first["head_revision_id"] and final["indexed"],
            "report_revisions_and_default_index_are_persistent",
            result,
        )
        completed = api(browser, prefix + f"/tasks/{task_id}/research")["body"]
        synth = next(c for c in completed["checkpoints"] if c["phase"] == "synthesis")
        evidence = api(
            browser, prefix + f"/tasks/{task_id}/research/evidence/{synth['unit_id']}"
        )["body"]["items"]
        check(
            all(not e["from_current_run"] for e in evidence)
            and {e["source"]["content_role"] for e in evidence}
            == {"original", "user_note"},
            "synthesis_keeps_prior_read_versions_and_user_note_identity",
            result,
        )
        browser.command("reload")
        browser.snapshot("reopen-local-sources")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('navigation',{name:'学习成果列表',exact:true}).getByRole('button').filter({hasText:'多资料研究报告'}).first().click();await page.getByRole('button',{name:'来源 1',exact:true}).first().click();await page.getByText('文档选区 · 原始资料',{exact:true}).waitFor();await page.getByRole('button',{name:'关闭来源',exact:true}).click();}",
        )
        check(
            True,
            "original_selection_formatter_is_used_in_the_native_saved_source_view",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'整理资料',exact:true}).click();const form=page.getByRole('region',{name:'发起学习任务'});await form.getByRole('combobox',{name:'成果类型',exact:true}).selectOption('research');await form.getByRole('combobox',{name:'资料范围',exact:true}).selectOption('custom');await form.getByRole('checkbox',{name:'second.txt',exact:true}).check();await form.getByRole('list',{name:'已选来源'}).getByRole('listitem').filter({hasText:'second.txt'}).waitFor();await form.getByRole('textbox',{name:'标题',exact:true}).fill('公开文字研究');await form.getByRole('checkbox',{name:'允许为本次研究检索和读取外部公开文字资料',exact:true}).check();await form.getByRole('textbox',{name:'指定外部网页（可选，每行一个网址）',exact:true}).fill('https://example.com/evidence');await form.getByRole('button',{name:'开始生成并保存',exact:true}).click();}",
        )
        all_tasks = wait_api(
            browser,
            prefix + "/tasks",
            "value.length===2&&value.every(t=>t.state==='completed')",
            timeout_ms=40000,
        )
        web_task = next(t for t in all_tasks if t["task_id"] != task_id)
        web_progress = api(browser, prefix + f"/tasks/{web_task['task_id']}/research")[
            "body"
        ]
        check(
            len(web_progress["external_reads"]) == 1
            and web_progress["external_reads"][0]["transport_source"] == "fixture",
            "explicit_url_read_has_a_separate_http_receipt_not_a_model_attempt",
            result,
        )
        web_target = api(browser, prefix + f"/tasks/{web_task['task_id']}/coverage")[
            "body"
        ]["target"]["artifact_id"]
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'阅读成果',exact:true}).first().click();await page.getByRole('button',{name:'来源 2',exact:true}).click();await page.getByText('外部网页文字 · 抓取时版本',{exact:true}).waitFor();await page.getByText('公开资料也须核对条件和单位。',{exact:true}).waitFor();}",
        )
        snapshot = browser.snapshot("external-snapshot")
        check(
            "HIDDEN_VIDEO_SENTINEL" not in snapshot
            and "HIDDEN_SCRIPT_SENTINEL" not in snapshot,
            "external_reader_keeps_only_text_and_never_supplements_media",
            result,
        )
        link = browser.evaluate(
            "()=>[...document.querySelectorAll('a')].find(a=>a.textContent==='打开公开网页当前版本')?.getAttribute('href')"
        )
        check(
            link == "https://example.com/evidence",
            "snapshot_and_current_webpage_navigation_are_explicitly_separate",
            result,
        )
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'research-sources.png'}",
            "--full-page",
        )
        before = [
            u["run_id"]
            for u in api(browser, prefix + f"/tasks/{web_task['task_id']}")["body"][
                "units"
            ]
        ]
        browser.command("reload")
        browser.snapshot("after-reload")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('textbox',{name:'搜索学习成果或任务',exact:true}).fill('公开文字研究');await page.getByRole('navigation',{name:'学习成果列表',exact:true}).getByRole('button').filter({hasText:'公开文字研究'}).first().click();await page.getByRole('heading',{name:'公开文字研究',exact:true}).waitFor();}",
        )
        reopened = api(browser, prefix + f"/artifacts/{web_target}")["body"]
        check(
            reopened["kind"] == "report" and reopened["indexed"],
            "saved_report_can_be_found_and_reopened_from_the_native_workspace",
            result,
        )
        check(
            before
            == [
                u["run_id"]
                for u in api(browser, prefix + f"/tasks/{web_task['task_id']}")["body"][
                    "units"
                ]
            ],
            "reload_never_starts_another_research_request",
            result,
        )
        check(
            all(
                hashlib.sha256((materials / name).read_bytes()).hexdigest() == digest
                for name, digest in originals.items()
            ),
            "original_documents_remain_unchanged",
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
        (instance.directory / "release-research-summary").write_text(
            "cleanup release", "utf-8"
        )
        result["browser_cleanup"] = browser.close()
        result["cleanup"] = instance.stop()
        result["source_digests_after"] = source_digests()
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
    parser.add_argument("--backend-port", type=int, default=19249)
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
