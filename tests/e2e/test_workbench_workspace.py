"""Real native desktop workspace; generated text and deterministic local SSE."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

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
from test_desktop_migration import MigrationInstance, create_course, wait_api
from test_mvp_a import api
from test_workbench_reading import digests
from test_workspace_v2 import manager_entry


class WorkspaceInstance(MigrationInstance):
    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.native_selection()
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
                raise RuntimeError("WORKSPACE_FIXTURE_START_FAILED")
            try:
                status, _, body = http(self.url + "/api/v1/health")
                return (
                    status == 200
                    and body["capabilities"]["question_answering"] == "ready"
                )
            except OSError:
                return False

        wait_until(ready, timeout=45)
        return self


async def serve(directory, port):
    import uvicorn
    import learning_app.course_files as files
    from learning_app.app import create_app
    from learning_app.config import AppConfig
    from learning_app.execution.adapter import ExecutionAdapter
    from learning_app.execution.boundary import MessagesBoundary
    from learning_app.execution.native_tools import (
        NativeCallRegistry,
        NativeToolTransport,
    )
    from learning_app.storage import Repository
    from learning_app.workbench.native_business import NativeBusiness
    from learning_app.workbench.runs import TaskRunStore
    from learning_app.workbench import practice_schema as practice
    from learning_app.workbench import essay_schema as essays
    from learning_app.workbench.essays import EssayStore
    from learning_app.workbench import schema as learning
    from learning_app.workbench import corpus_schema as corpus
    from sqlalchemy import select

    def chosen(*_args, **_kwargs):
        data = json.loads(
            (directory / "synthetic-native-input.json").read_text("utf-8")
        )
        return [data["folder"]] if data.get("folder") else []

    files.choose_paths = chosen
    repo = Repository(directory / "data")
    registry = NativeCallRegistry(directory / "execution/native-calls.jsonl")
    business = NativeBusiness(repo)
    if (directory / "research-fixture").exists():
        import httpx
        from learning_app.workbench.external_http import PublicTextReader

        def public_page(_request):
            if (directory / "fail-research-page").exists():
                return httpx.Response(503)
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                stream=httpx.ByteStream(
                    "<title>公开合成资料</title><p>公开资料也须核对条件和单位。</p><video>HIDDEN_VIDEO_SENTINEL</video><script>HIDDEN_SCRIPT_SENTINEL</script>".encode(
                        "utf-8"
                    )
                ),
            )

        business.research_external.reader = PublicTextReader(
            resolver=lambda *_: ["93.184.216.34"],
            transport=httpx.MockTransport(public_page),
        )
    bridge = NativeToolTransport(registry, business)

    class Boundary(MessagesBoundary):
        qa_steps = {}
        research_steps = {}

        def _fixture(self, handler, request, scenario, row, observer):
            with repo.engine.connect() as conn:
                is_task = conn.execute(
                    select(learning.task_runs.c.run_id).where(
                        learning.task_runs.c.run_id == row["generation_id"]
                    )
                ).first()
            if is_task is None:
                run_id = row["generation_id"]
                with repo.engine.connect() as conn:
                    nearby = bool(
                        NativeBusiness._scope(conn, run_id)["grants"].get(
                            "subtitle_anchors"
                        )
                    )
                step = self.qa_steps.get(run_id, 0) + 1
                self.qa_steps[run_id] = step
                calls = []
                if (directory / "requested-learning-fixture").exists():
                    allowed = {tool["name"] for tool in request.get("tools", [])}
                    if step == 1 and "admit_requested_artifact" in allowed:
                        return super()._fixture(
                            handler,
                            request,
                            {
                                "text_chunks": [],
                                "tool_calls": [
                                    {
                                        "id": "explicit_learning_request",
                                        "name": "admit_requested_artifact",
                                        "input": {},
                                    }
                                ],
                            },
                            row,
                            observer,
                        )
                    with repo.engine.connect() as conn:
                        admitted = [
                            task["task_id"]
                            for task in conn.execute(select(learning.tasks)).mappings()
                            if task["scope_spec"].get("origin", {}).get("generation_id")
                            == run_id
                        ]
                    text = (
                        f"已保存任务。[查看任务](#learning-task={admitted[0]})"
                        if admitted
                        else "这是普通问答，没有生成额外成果。"
                    )
                    return super()._fixture(
                        handler, request, {"text_chunks": [text]}, row, observer
                    )
                if step == 1:
                    calls = [
                        {
                            "id": "native_course_skill",
                            "name": "skill",
                            "input": {"name": "course-read"},
                        }
                    ]
                elif step == 2:
                    calls = [
                        {
                            "id": "native_concept_query",
                            "name": "read_nearby_subtitles"
                            if nearby
                            else "search_course_concept",
                            "input": {"anchor_id": None}
                            if nearby
                            else {"concept": "Learning page 3"},
                        }
                    ]
                elif step == 3:
                    with repo.engine.connect() as conn:
                        receipt = conn.execute(
                            select(corpus.query_receipts.c.result).where(
                                corpus.query_receipts.c.run_id == run_id
                            )
                        ).scalar_one()
                    if receipt["results"]:
                        source = receipt["results"][0]
                        calls = [
                            {
                                "id": "native_concept_read",
                                "name": "read_source_handle",
                                "input": {
                                    key: source[key]
                                    for key in ("handle_id", "source_version")
                                },
                            }
                        ]
                return super()._fixture(
                    handler,
                    request,
                    {
                        "text_chunks": []
                        if calls
                        else [
                            "已核对问题附近字幕。"
                            if nearby
                            else "已核对当前课程第3页原文。"
                        ],
                        "tool_calls": calls,
                    },
                    row,
                    observer,
                )
            run = TaskRunStore(repo).internal(row["generation_id"])
            if run["input_contract"].get("phase") == "outline":
                context = run["input_contract"]["book_context"]
                chapters = context["supplied_outline"] or [
                    {
                        "chapter_id": "chapter_1",
                        "title": "来源与核对",
                        "objective": "从实际材料出发",
                    },
                    {
                        "chapter_id": "chapter_2",
                        "title": "串联与回顾",
                        "objective": "把来源联系起来",
                    },
                ]
                source_ids = [source["source_id"] for source in context["sources"]]
                proposal = {
                    "chapters": [
                        {
                            **chapter,
                            "source_ids": source_ids[index :: len(chapters)]
                            or source_ids[:1],
                        }
                        for index, chapter in enumerate(chapters)
                    ],
                    "rationale": "按完整指定资料清单拟目录，正文尚待实际处理。",
                }
                return super()._fixture(
                    handler,
                    request,
                    {"text_chunks": [json.dumps(proposal, ensure_ascii=False)]},
                    row,
                    observer,
                )
            ids = [item["snapshot_id"] for item in run["input_contract"]["inputs"]]
            if run["input_contract"].get("edit_range"):
                from learning_app.workbench.sources import SourceStore

                edit = run["input_contract"]["edit_range"]
                with repo.engine.connect() as conn:
                    course = conn.execute(
                        select(learning.tasks.c.course_id).where(
                            learning.tasks.c.task_id == run["task_id"]
                        )
                    ).scalar_one()
                    actual = SourceStore.in_transaction(
                        conn, course, edit["snapshot_id"]
                    )
                value = {
                    "blocks": [
                        {
                            "block_id": edit["block_id"],
                            "kind": "markdown",
                            "markdown": actual.text.replace("old", "modern"),
                            "attribution": "source_paraphrase",
                            "source_snapshot_ids": ids,
                        }
                    ]
                }
                return super()._fixture(
                    handler,
                    request,
                    {"text_chunks": [json.dumps(value, ensure_ascii=False)]},
                    row,
                    observer,
                )
            value = {
                "blocks": [
                    {
                        "block_id": "lesson",
                        "markdown": "合成资料讲义：先核实来源，再解释内容。",
                        "attribution": "source_paraphrase",
                        "source_snapshot_ids": ids,
                    }
                ]
            }
            if (directory / "requested-learning-fixture").exists() and run[
                "input_contract"
            ]["target"]["mode"] == "edit":
                value["blocks"][0]["block_id"] = run["input_contract"]["target"][
                    "write_scope"
                ]["block_ids"][0]
                value["blocks"][0]["markdown"] = (
                    "用户笔记的模型修订：先核对来源。例子：比较数值前先检查单位。"
                )
            with repo.engine.connect() as conn:
                kind = conn.execute(
                    select(learning.tasks.c.kind, learning.tasks.c.instruction).where(
                        learning.tasks.c.task_id == run["task_id"]
                    )
                ).one()
                kind, instruction = kind
            if kind == "aligned_translation":
                with repo.engine.connect() as conn:
                    course_id = conn.execute(
                        select(learning.tasks.c.course_id).where(
                            learning.tasks.c.task_id == run["task_id"]
                        )
                    ).scalar_one()
                    from learning_app.workbench.sources import SourceStore

                    selected_text = "".join(
                        SourceStore.in_transaction(
                            conn, course_id, item["snapshot_id"]
                        ).text
                        for item in run["input_contract"]["inputs"]
                    )
                    ordinal = conn.execute(
                        select(learning.task_units.c.ordinal).where(
                            learning.task_units.c.unit_id == run["unit_id"]
                        )
                    ).scalar_one()
                raw = json.dumps(
                    {"src": selected_text, "tgt": "对应中文译文：先核对指定文字。"},
                    ensure_ascii=False,
                )
                raw += (
                    '\n{"src":"broken'
                    if (directory / "aligned-broken-final").exists() and ordinal > 0
                    else '\n{"done":true}'
                )
                return super()._fixture(
                    handler, request, {"text_chunks": [raw]}, row, observer
                )
            if kind == "research":
                from learning_app.workbench.research_external import ResearchExternal

                context = run["input_contract"]["research"]
                if context["phase"] == "synthesis":
                    if (directory / "hold-research-summary").exists() and not (
                        directory / "release-research-summary"
                    ).exists():
                        write_json(
                            directory / "research-summary-waiting.json",
                            {"run_id": run["run_id"]},
                        )
                        until = time.monotonic() + 90
                        while (
                            not (directory / "release-research-summary").exists()
                            and time.monotonic() < until
                        ):
                            time.sleep(0.03)
                    ids = context["citation_sources"]
                elif context.get("external"):
                    step = self.research_steps[run["run_id"]] = (
                        self.research_steps.get(run["run_id"], 0) + 1
                    )
                    if step == 1:
                        return super()._fixture(
                            handler,
                            request,
                            {
                                "text_chunks": [],
                                "tool_calls": [
                                    {
                                        "id": "read_public_text",
                                        "name": "read_research_page",
                                        "input": {
                                            "url": context["external"]["urls"][0]
                                        },
                                    }
                                ],
                            },
                            row,
                            observer,
                        )
                    with repo.engine.connect() as conn:
                        ids += sorted(
                            ResearchExternal.cited_sources(conn, run["run_id"])
                        )
                value = {
                    "blocks": [
                        {
                            "block_id": "comparison",
                            "kind": "markdown",
                            "markdown": "综合比较：各资料均要求核对条件和单位，用户笔记与原件分开保留。"
                            if context["phase"] == "synthesis"
                            else "证据整理：本批资料的共同点是先核对条件，再比较单位与适用范围。",
                            "attribution": "source_inference"
                            if context["phase"] == "synthesis"
                            else "source_paraphrase",
                            "source_snapshot_ids": ids,
                        }
                    ]
                }
            elif kind == "question_set":
                value = {
                    "blocks": [
                        {
                            "block_id": "choice",
                            "kind": "question",
                            "markdown": "Which action comes first?",
                            "attribution": "source_paraphrase",
                            "source_snapshot_ids": ids,
                            "question": {
                                "question_type": "single_choice",
                                "options": [
                                    {"option_id": "A", "text": "Verify the source."},
                                    {
                                        "option_id": "B",
                                        "text": "Explain without checking.",
                                    },
                                ],
                                "reference_answer": "A",
                                "explanation": "The material explicitly puts source verification first.",
                                "answer_authority": "ai_reference",
                            },
                        }
                    ]
                }
            elif kind == "book":
                chapter = run["input_contract"]["book"]["chapter_id"]
                if chapter.endswith("2") and (directory / "hold-book-page").exists():
                    write_json(
                        directory / "book-page-waiting.json",
                        {"run_id": run["run_id"], "chapter_id": chapter},
                    )
                    until = time.monotonic() + 75
                    while (
                        not (directory / "release-book-page").exists()
                        and time.monotonic() < until
                    ):
                        time.sleep(0.03)
                value = {
                    "blocks": [
                        {
                            "block_id": "book_text",
                            "kind": "markdown",
                            "markdown": "先核对实际资料，再串联概念。函数使用所给系数。",
                            "attribution": "source_paraphrase",
                            "source_snapshot_ids": ids,
                        },
                        {
                            "block_id": "book_question",
                            "kind": "question",
                            "markdown": "请写下你如何核对本页条件。",
                            "attribution": "source_paraphrase",
                            "source_snapshot_ids": ids,
                            "question": {
                                "question_type": "short_answer",
                                "options": [],
                                "reference_answer": "先检查指定资料中的条件和来源。",
                                "explanation": "不能用其他版本替代原条件。",
                                "answer_authority": "ai_reference",
                            },
                        },
                        {
                            "block_id": "book_plot",
                            "kind": "visualization",
                            "markdown": "按公式观察系数的作用。",
                            "attribution": "source_paraphrase",
                            "source_snapshot_ids": ids,
                            "visualization": {
                                "render_type": "cartesian",
                                "renderer": {"native_renderer": "cartesian"},
                                "presentation": {
                                    "title": "书中参数图",
                                    "alt_text": "二次函数参数图",
                                },
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
                                    }
                                },
                            },
                        },
                    ]
                }
            elif kind == "learning_path":
                value = {
                    "blocks": [
                        {
                            "block_id": f"concept_{index}",
                            "kind": "path_step",
                            "markdown": "以实际指定资料学习，顺序可以调整。",
                            "attribution": "source_paraphrase",
                            "source_snapshot_ids": ids,
                            "step": {
                                "title": title,
                                "objective": "能核对资料后说明概念。",
                                "suggested_order": index,
                                "recall_prompt": f"先回忆：{title}是什么？",
                                "explanation": f"{title}的完整讲解，不能以跳过替代掌握。",
                                "questions": [],
                            },
                        }
                        for index, title in enumerate(("来源身份", "核对条件"))
                    ]
                }
            elif kind == "visualization":
                diagram = {
                    "render_type": "diagram",
                    "renderer": {"native_renderer": "diagram"},
                    "presentation": {
                        "title": "输入与结果",
                        "alt_text": "从输入经过变换得到结果",
                    },
                    "payload": {
                        "data": {
                            "kind": "diagram",
                            "nodes": [
                                {
                                    "node_id": "input",
                                    "x": 20,
                                    "y": 50,
                                    "label": "输入x",
                                    "shape": "circle",
                                },
                                {
                                    "node_id": "output",
                                    "x": 80,
                                    "y": 50,
                                    "label": "结果y",
                                    "shape": "box",
                                    "color": "green",
                                },
                            ],
                            "edges": [
                                {
                                    "source": "input",
                                    "target": "output",
                                    "label": "a×x²",
                                    "arrow": True,
                                }
                            ],
                        }
                    },
                }
                plots = [
                    {
                        "render_type": "cartesian",
                        "renderer": {"native_renderer": "cartesian"},
                        "presentation": {"title": title, "alt_text": title},
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
                                "animate_parameter": animate,
                                "duration_seconds": 4,
                            }
                        },
                    }
                    for title, animate in (("参数与函数", None), ("系数动画", "a"))
                ]
                value = {
                    "blocks": [
                        {
                            "block_id": f"visual_{index}",
                            "kind": "visualization",
                            "markdown": "图形使用当前指定资料中的函数关系。",
                            "attribution": "source_paraphrase",
                            "source_snapshot_ids": ids,
                            "visualization": envelope,
                        }
                        for index, envelope in enumerate([diagram, *plots])
                    ]
                }
            elif kind == "card_set":
                value = {
                    "blocks": [
                        {
                            "block_id": "recall",
                            "kind": "card",
                            "markdown": "What should be checked first?",
                            "attribution": "source_paraphrase",
                            "source_snapshot_ids": ids,
                            "card": {"back": "Verify the source."},
                        }
                    ]
                }
                if "针对本轮原稿" in instruction:
                    value["blocks"][0].update(
                        markdown="I ___ a book.", card={"back": "have：主语I使用have。"}
                    )
            elif kind == "answer_review":
                with repo.engine.connect() as conn:
                    bound = (
                        conn.execute(
                            select(practice.answer_reviews).where(
                                practice.answer_reviews.c.task_id == run["task_id"]
                            )
                        )
                        .mappings()
                        .one()
                    )
                value = {
                    "blocks": [
                        {
                            "block_id": "feedback",
                            "kind": "assessment",
                            "markdown": "The actual B answer skips the required source check; AI reference judgment only.",
                            "attribution": "source_paraphrase",
                            "source_snapshot_ids": ids,
                            "assessment": {
                                "answer_id": bound["answer_id"],
                                "expected_revision": bound["expected_revision"],
                                "verdict": "wrong",
                                "score": 0,
                            },
                        }
                    ]
                }
            elif kind == "essay_review":
                with repo.engine.connect() as conn:
                    bound = (
                        conn.execute(
                            select(essays.essay_rounds).where(
                                essays.essay_rounds.c.task_id == run["task_id"]
                            )
                        )
                        .mappings()
                        .one()
                    )
                    actual, _ = EssayStore.draft(
                        conn,
                        bound["course_id"],
                        bound["artifact_id"],
                        bound["draft_revision_id"],
                        bound["block_id"],
                    )
                annotated = actual.markdown.replace(
                    "has", '<replace old="has" new="have" reason="主语I使用have"/>', 1
                )
                score = 70 if bound["round_number"] == 1 else 80
                value = {
                    "blocks": [
                        {
                            "block_id": "essay_feedback",
                            "kind": "essay_feedback",
                            "markdown": f'<score total="{score}" max="100"></score>\nAI参考批改：检查主谓一致，保留实际原稿与用户评价。',
                            "attribution": "source_paraphrase",
                            "source_snapshot_ids": ids,
                            "feedback": {
                                "round_id": bound["round_id"],
                                "draft_revision_id": bound["draft_revision_id"],
                                "draft_content_hash": bound["draft_content_hash"],
                                "annotated_text": annotated,
                            },
                        }
                    ]
                }
            return super()._fixture(
                handler,
                request,
                {"text_chunks": [json.dumps(value, ensure_ascii=False)]},
                row,
                observer,
            )

    boundary = Boundary(directory / "execution/boundary", native_registry=registry)
    adapter = ExecutionAdapter(
        ROOT,
        directory / "execution",
        mode="fixture",
        timeout=120,
        desktop_enabled=True,
        boundary=boundary,
        native_tools=bridge,
    )
    await adapter.open()
    app = create_app(
        AppConfig(
            mode="start",
            backend_port=port,
            frontend_port=port,
            data_dir=directory / "data",
            runtime_dir=directory / "runtime",
            harness_enabled=True,
            desktop_enabled=True,
            instance_id="workspace-fixture",
        ),
        repository=repo,
        execution=adapter,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
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
        write_json(directory / "worker-cleanup.json", await adapter.close())
    return 0 if server.started else 1


def exercise_practice(browser, prefix, result):
    def generate(kind, title):
        browser.snapshot("before-create-" + kind)
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'整理资料',exact:true}).click();const form=page.getByRole('region',{name:'发起学习任务'});await form.getByRole('combobox',{name:'成果类型',exact:true}).selectOption("
            + json.dumps(kind)
            + ");await form.getByRole('combobox',{name:'资料范围',exact:true}).selectOption('document');await form.getByRole('textbox',{name:'标题',exact:true}).fill("
            + json.dumps(title)
            + ");await form.getByRole('button',{name:'开始生成并保存',exact:true}).click();}",
        )
        task = wait_api(
            browser,
            prefix + "/tasks",
            "value.find(t=>t.kind===" + json.dumps(kind) + ")?.state==='completed'",
            timeout_ms=30000,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('heading',{name:'已完成',exact:true}).waitFor({timeout:15000});await page.getByRole('button',{name:'阅读成果',exact:true}).first().click();await page.getByRole('heading',{name:"
            + json.dumps(title)
            + ",exact:true}).waitFor();}",
        )
        return next(item for item in task if item["kind"] == kind)

    generate("question_set", "合成练习")
    questions = api(browser, prefix + "/artifacts")["body"]
    paper = next(item for item in questions if item["kind"] == "question_set")
    block_id = paper["revision"]["body"]["blocks"][0]["block_id"]
    browser.snapshot("before-actual-answer")
    browser.command(
        "run-code",
        "async(page)=>{await page.getByRole('textbox',{name:'实际作答',exact:true}).fill('B');await page.getByRole('button',{name:'提交作答',exact:true}).click();await page.getByText('待判断 · 确定性比较',{exact:true}).waitFor();}",
    )
    history = api(
        browser,
        prefix + f"/artifacts/{paper['artifact_id']}/answers?block_id={block_id}",
    )["body"]["answers"]
    actual = history[0]
    check(
        len(history) == 1
        and actual["text"] == "B"
        and actual["question_revision_id"] == paper["head_revision_id"]
        and actual["effective_assessment"]["verdict"] == "pending",
        "actual_answer_frozen_to_question_revision_and_unconfirmed_key_is_pending",
        result,
    )
    check(
        not api(browser, prefix + "/reviews")["body"]["items"],
        "pending_answer_does_not_create_wrong_review",
        result,
    )
    browser.snapshot("before-ai-reference")
    browser.command(
        "run-code",
        "async(page)=>{await page.getByRole('button',{name:'AI参考判分',exact:true}).click();}",
    )
    assessed = wait_api(
        browser,
        prefix + f"/answers/{actual['answer_id']}",
        "value.effective_assessment.author==='ai_reference'",
        timeout_ms=30000,
    )
    check(
        assessed["text"] == "B"
        and assessed["effective_assessment"]["verdict"] == "wrong"
        and assessed["effective_assessment"]["run_id"],
        "actual_native_reference_grade_retains_answer_and_run_attribution",
        result,
    )
    wrong = api(browser, prefix + "/reviews")["body"]["items"][0]
    browser.command(
        "run-code",
        "async(page)=>{await page.getByRole('button',{name:'今日复习',exact:true}).click();await page.getByRole('combobox',{name:'查看',exact:true}).selectOption('active');await page.getByRole('button',{name:/Which action comes first/}).click();await page.getByRole('button',{name:'暂停复习',exact:true}).click();await page.getByRole('button',{name:'恢复入队',exact:true}).waitFor();}",
    )
    check(
        api(browser, prefix + f"/reviews/{wrong['review_id']}")["body"]["queue_state"]
        == "paused",
        "wrong_review_has_explicit_persisted_pause",
        result,
    )
    browser.snapshot("before-user-override")
    browser.command(
        "run-code",
        "async(page)=>{await page.getByRole('button',{name:'学习成果',exact:true}).click();await page.getByRole('button',{name:/合成练习/}).click();await page.getByText('更正此判定',{exact:true}).click();await page.getByRole('combobox',{name:'用户判定',exact:true}).selectOption('correct');await page.getByRole('textbox',{name:'更正理由',exact:true}).fill('用户核对后更正，保留原参考判定。');await page.getByRole('button',{name:'保存改判并调整后续复习',exact:true}).click();await page.getByText('正确 · 用户更正',{exact:true}).waitFor();}",
    )
    final = api(browser, prefix + f"/answers/{actual['answer_id']}/assessments")[
        "body"
    ]["assessments"]
    current_wrong = api(browser, prefix + f"/reviews/{wrong['review_id']}")["body"]
    check(
        [item["author"] for item in final]
        == ["deterministic", "ai_reference", "user_override"]
        and not current_wrong["eligible"]
        and current_wrong["queue_state"] == "paused",
        "user_override_preserves_original_judgments_and_adjusts_schedule_without_unpause",
        result,
    )
    generate("card_set", "合成复习卡")
    reviews = api(browser, prefix + "/reviews")["body"]["items"]
    card = next(item for item in reviews if item["kind"] == "card")
    check(
        card["queue_state"] == "active"
        and card["eligible"]
        and card["scheduler_state"]["reps"] == 0,
        "explicit_card_save_and_default_enqueue_have_one_durable_receipt",
        result,
    )
    browser.snapshot("before-card-review")
    browser.command(
        "run-code",
        "async(page)=>{await page.getByRole('button',{name:'今日复习',exact:true}).click();await page.getByRole('button',{name:/What should be checked first/}).click();await page.getByRole('button',{name:'开始本次复习',exact:true}).click();await page.getByRole('button',{name:'回忆后翻面',exact:true}).waitFor();const review=page.getByRole('article',{name:'复习内容'});if(await review.getByText('Verify the source.',{exact:true}).count())throw Error('BACK_REVEALED_BEFORE_USER_ACTION');await page.getByRole('button',{name:'回忆后翻面',exact:true}).click();await review.getByText('Verify the source.',{exact:true}).waitFor();await page.getByRole('button',{name:'记得',exact:true}).click();await page.getByRole('button',{name:'撤销本次评分',exact:true}).waitFor();}",
    )
    rated = api(browser, prefix + f"/reviews/{card['review_id']}")["body"]
    today = api(browser, "/learning/reviews/today")["body"]
    check(
        rated["scheduler_state"]["reps"] == 1
        and rated["due_ms"] > card["due_ms"]
        and today["admitted_count"] == today["completed_count"] == 1,
        "user_recall_reveal_and_actual_fixed_fsrs_rating_share_global_today_budget",
        result,
    )
    browser.command(
        "screenshot",
        f"--filename={browser.directory / 'practice-review.png'}",
        "--full-page",
    )
    browser.snapshot("before-rating-undo")
    browser.command(
        "run-code",
        "async(page)=>{await page.getByRole('button',{name:'撤销本次评分',exact:true}).click();await page.getByRole('button',{name:'记得',exact:true}).waitFor();}",
    )
    undone = api(browser, prefix + f"/reviews/{card['review_id']}")["body"]
    check(
        undone["scheduler_state"]["reps"] == 0
        and undone["schedule_revision"] > rated["schedule_revision"]
        and api(browser, "/learning/reviews/today")["body"]["admitted_count"] == 1,
        "rating_undo_appends_history_without_refunding_daily_admission",
        result,
    )


def exercise_essays(browser, prefix, result, instance):
    text = "🙂 I has a book.\nI like learning."
    browser.snapshot("before-essay-draft")
    browser.command(
        "run-code",
        "async(page)=>{await page.getByRole('button',{name:'写作文',exact:true}).click();const form=page.getByRole('region',{name:'保存作文原稿'});await form.getByRole('textbox',{name:'标题',exact:true}).fill('合成作文');await form.getByRole('textbox',{name:'实际作文',exact:true}).fill("
        + json.dumps(text)
        + ");await form.getByRole('button',{name:'保存原稿',exact:true}).click();await page.getByRole('heading',{name:'合成作文',exact:true}).waitFor();await page.getByRole('button',{name:'批改当前稿，保存独立轮次',exact:true}).waitFor();}",
    )
    original = next(
        item
        for item in api(browser, prefix + "/artifacts")["body"]
        if item["kind"] == "essay"
    )
    check(
        original["revision"]["body"]["blocks"][0]["markdown"] == text
        and original["content_role"] == "user_note",
        "essay_actual_unicode_draft_saved_without_trimming",
        result,
    )
    path = prefix + f"/artifacts/{original['artifact_id']}/essay-rounds"
    browser.snapshot("before-first-essay-review")
    browser.command(
        "run-code",
        "async(page)=>{await page.getByRole('button',{name:'批改当前稿，保存独立轮次',exact:true}).click();}",
    )
    first = wait_api(
        browser,
        path,
        "value.items.length===1&&value.items[0].state==='completed'",
        timeout_ms=30000,
    )["items"][0]
    browser.command(
        "run-code",
        "async(page)=>{await page.getByRole('heading',{name:'已完成',exact:true}).waitFor({timeout:15000});await page.getByRole('button',{name:'阅读成果',exact:true}).first().click();await page.getByText('AI参考评分：70 / 100',{exact:true}).waitFor();}",
    )
    row1 = api(browser, prefix + f"/essay-rounds/{first['round_id']}")["body"]
    check(
        row1["draft"]["markdown"] == text
        and row1["result"]["annotations"][0]["quote"] == "has"
        and row1["result"]["annotations"][0]["start"] == 4,
        "actual_native_essay_feedback_anchors_real_codepoints_after_emoji",
        result,
    )
    browser.snapshot("before-essay-user-correction")
    browser.command(
        "run-code",
        "async(page)=>{await page.getByText('自评或更正参考分',{exact:true}).click();await page.getByRole('spinbutton',{name:'用户参考分',exact:true}).fill('85');await page.getByRole('textbox',{name:'评价理由',exact:true}).fill('用户更正，保留AI原分。');await page.getByRole('button',{name:'保存用户评价',exact:true}).click();await page.getByText('用户更正：85 / 100',{exact:true}).waitFor();await page.getByRole('checkbox',{name:'采纳为：have',exact:true}).check();await page.getByRole('button',{name:'采纳所选，保存新稿',exact:true}).click();await page.getByRole('heading',{name:'合成作文',exact:true}).waitFor();}",
    )
    revised = wait_api(
        browser,
        prefix + f"/artifacts/{original['artifact_id']}",
        "value.head_revision_id!==" + json.dumps(original["head_revision_id"]),
        timeout_ms=10000,
    )
    first_now = api(browser, prefix + f"/essay-rounds/{first['round_id']}")["body"]
    check(
        revised["content_role"] == "mixed"
        and revised["revision"]["body"]["blocks"][0]["markdown"]
        == text.replace("has", "have", 1)
        and revised["revision"]["body"]["blocks"][0]["source_snapshot_ids"],
        "essay_suggestion_acceptance_creates_mixed_new_version_with_feedback_provenance",
        result,
    )
    check(
        [item["total"] for item in first_now["assessments"]] == [70, 85],
        "essay_user_correction_preserves_ai_original_score",
        result,
    )
    browser.snapshot("before-second-essay-review")
    browser.command(
        "run-code",
        "async(page)=>{await page.getByRole('button',{name:/第1轮/}).waitFor();await page.getByRole('button',{name:'批改当前稿，保存独立轮次',exact:true}).click();}",
    )
    second = wait_api(
        browser,
        path,
        "value.items.length===2&&value.items[0].state==='completed'",
        timeout_ms=30000,
    )["items"][0]
    browser.command(
        "run-code",
        "async(page)=>{await page.getByRole('heading',{name:'已完成',exact:true}).waitFor({timeout:15000});await page.getByRole('button',{name:'阅读成果',exact:true}).first().click();await page.getByText('AI参考评分：80 / 100',{exact:true}).waitFor();}",
    )
    row2 = api(browser, prefix + f"/essay-rounds/{second['round_id']}")["body"]
    check(
        row2["draft_revision_id"] == revised["head_revision_id"]
        and row2["draft"]["markdown"] != row1["draft"]["markdown"]
        and api(browser, prefix + f"/artifacts/{original['artifact_id']}")["body"][
            "head_revision_id"
        ]
        == revised["head_revision_id"],
        "two_essay_rounds_bind_distinct_drafts_without_model_rewriting_current_draft",
        result,
    )
    comparison_counts = instance.counts()
    browser.command(
        "run-code",
        "async(page)=>{await page.getByRole('button',{name:'回到作文原稿',exact:true}).click();await page.getByRole('button',{name:'对照两轮',exact:true}).click();await page.getByRole('article',{name:'第1轮对照内容',exact:true}).waitFor();await page.getByRole('article',{name:'第2轮对照内容',exact:true}).waitFor();}",
    )
    comparison = browser.evaluate(
        "()=>[...document.querySelectorAll('.essay-comparison-columns article')].map(article=>({text:article.querySelector('pre').textContent,body:article.innerText,mark:article.querySelector('pre mark')?.textContent||'',x:article.getBoundingClientRect().x,y:article.getBoundingClientRect().y}))"
    )
    check(
        len(comparison) == 2
        and comparison[0]["text"] == row1["draft"]["markdown"]
        and comparison[1]["text"] == row2["draft"]["markdown"]
        and "AI参考评分：70 / 100" in comparison[0]["body"]
        and "用户更正：85 / 100" in comparison[0]["body"]
        and "AI参考评分：80 / 100" in comparison[1]["body"]
        and comparison[0]["mark"] == "s"
        and comparison[1]["mark"] == "ve"
        and comparison[1]["x"] > comparison[0]["x"] + 10
        and abs(comparison[1]["y"] - comparison[0]["y"]) < 5
        and instance.counts() == comparison_counts,
        "two_round_comparison_preserves_exact_drafts_reference_and_override_without_new_writes",
        result,
    )
    browser.command(
        "run-code",
        "async(page)=>{await page.getByRole('region',{name:'两轮作文对照',exact:true}).scrollIntoViewIfNeeded();}",
    )
    browser.command(
        "screenshot",
        f"--filename={browser.directory / 'essay-two-rounds.png'}",
        "--full-page",
    )
    browser.command(
        "run-code",
        "async(page)=>{await page.getByRole('button',{name:'收起对照',exact:true}).click();await page.getByRole('button',{name:/^第2轮 ·/}).click();}",
    )
    browser.command(
        "screenshot",
        f"--filename={browser.directory / 'essay-rounds.png'}",
        "--full-page",
    )
    browser.snapshot("before-explicit-essay-cards")
    browser.command(
        "run-code",
        "async(page)=>{await page.getByRole('button',{name:'从本轮原稿与批改生成卡片',exact:true}).click();}",
    )
    derivative = wait_api(
        browser,
        prefix + "/tasks",
        "value.find(t=>t.kind==='card_set'&&t.instruction.includes('针对本轮原稿'))?.state==='completed'",
        timeout_ms=30000,
    )
    derived_task = next(
        task
        for task in derivative
        if task["kind"] == "card_set" and "针对本轮原稿" in task["instruction"]
    )
    target = api(browser, prefix + f"/tasks/{derived_task['task_id']}/coverage")[
        "body"
    ]["target"]
    derived = api(browser, prefix + f"/artifacts/{target['artifact_id']}")["body"]
    check(
        derived["kind"] == "card_set"
        and len(derived["revision"]["body"]["blocks"][0]["source_snapshot_ids"]) == 2,
        "essay_cards_require_explicit_action_and_keep_both_actual_draft_and_reference_sources",
        result,
    )


def run_case(directory, executable, port):
    material = directory / "synthetic-course"
    material.mkdir()
    (material / "migration-read.txt").write_text(
        "OUTSIDE_SCOPE_SENTINEL. Synthetic course setup only.", "utf-8"
    )
    (material / "lesson.txt").write_text(
        "Only the current source. Verify the source before explaining.", "utf-8"
    )
    (material / "broken.pdf").write_bytes(b"%PDF-1.7\ncorrupt synthetic fixture")
    result = {
        "status": "running",
        "assertions": [],
        "layer": "actual Chromium + fixed native Harness + real business DB + local SSE",
        "actual_model_attempts": 0,
        "source_digests_before": digests(),
        "limits": [
            "No user materials, real provider, Windows visual acceptance or learning-quality acceptance."
        ],
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
        browser.snapshot("native-initial")
        cid, mapping = create_course(
            browser, instance, {"course_root": str(material)}, result
        )
        browser.snapshot("before-open-current-text")
        manager_entry(browser, "lesson.txt", double=True)
        browser.command(
            "run-code",
            "async(page)=>{await page.getByText('Only the current source. Verify the source before explaining.',{exact:true}).waitFor();}",
        )
        browser.snapshot("before-workbench")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('button',{name:'整理资料',exact:true}).click();}",
        )
        browser.snapshot("composer")
        selected_scope = browser.evaluate(
            "()=>document.querySelector('.workbench-composer select').parentElement.parentElement.textContent"
        )
        check(
            "lesson.txt" in selected_scope,
            "current_document_is_explicit_default_scope",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{const form=page.getByRole('region',{name:'发起学习任务'});await form.getByRole('textbox',{name:'标题',exact:true}).fill('合成资料讲义');await form.getByRole('button',{name:'开始生成并保存',exact:true}).click();}",
        )
        prefix = f"/courses/{cid}/learning"
        tasks = wait_api(
            browser,
            prefix + "/tasks",
            "value.length===1&&value[0].state==='completed'",
            timeout_ms=30000,
        )
        task = tasks[0]
        check(
            task["chat_id"] == mapping["chat_id"]
            and task["settings"]["model"] == "deepseek-flash",
            "native_model_and_original_chat_own_task",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('heading',{name:'已完成',exact:true}).waitFor({timeout:15000});}",
        )
        browser.snapshot("task-completed")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'阅读成果',exact:true}).first().click();await page.getByRole('heading',{name:'合成资料讲义',exact:true}).waitFor();}",
        )
        assets = api(browser, prefix + "/artifacts")["body"]
        check(
            len(assets) == 1
            and assets[0]["content_role"] == "ai_generated"
            and assets[0]["indexed"],
            "generated_asset_is_independent_indexed_and_marked_ai",
            result,
        )
        asset = assets[0]
        browser.snapshot("before-manual-edit")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'编辑',exact:true}).click();await page.getByRole('textbox',{name:'编辑内容块',exact:true}).fill('用户手动修订：先查看原文，再回答。');await page.getByRole('button',{name:'保存修改',exact:true}).click();await page.getByText('已保存新版本',{exact:true}).waitFor();}",
        )
        saved = api(browser, prefix + "/artifacts/" + asset["artifact_id"])["body"]
        check(
            saved["content_role"] == "mixed"
            and saved["revision"]["body"]["blocks"][0]["attribution"]
            == "user_authored",
            "manual_edit_preserves_mixed_identity_and_new_revision",
            result,
        )
        browser.snapshot("before-history")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'版本记录',exact:true}).click();const versions=page.getByRole('complementary',{name:'版本记录'});await versions.getByRole('button',{name:/AI/}).click();await page.getByRole('button',{name:'采用此版本，保留历史',exact:true}).click();await page.getByText('合成资料讲义：先核实来源，再解释内容。',{exact:true}).waitFor();}",
        )
        history = api(
            browser, prefix + "/artifacts/" + asset["artifact_id"] + "/revisions"
        )["body"]
        check(
            len(history) == 3
            and history[0]["revision_id"] != history[1]["revision_id"],
            "restore_creates_new_revision_without_deleting_history",
            result,
        )
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'workspace-panel.png'}",
            "--full-page",
        )
        browser.snapshot("before-source-open")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'来源 1',exact:true}).click();await page.getByRole('button',{name:'回看原资料',exact:true}).click();await page.getByRole('tab',{name:'lesson.txt',exact:true}).waitFor();}",
        )
        chats = api(browser, f"/courses/{cid}/chats")["body"]
        check(
            len(chats) == 1 and chats[0]["chat_id"] == mapping["chat_id"],
            "asset_history_and_source_navigation_do_not_create_chat",
            result,
        )
        requests = list(
            (instance.directory / "execution/boundary/private/boundary").glob(
                "*.request.json"
            )
        )
        encoded = "\n".join(path.read_text("utf-8") for path in requests)
        check(
            len(requests) == 1 and "OUTSIDE_SCOPE_SENTINEL" not in encoded,
            "actual_native_request_contains_only_current_declared_document",
            result,
        )
        browser.snapshot("before-whole-course-gap-task")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('tab',{name:'学习成果与任务',exact:true}).click();await page.getByRole('button',{name:'整理资料',exact:true}).click();const form=page.getByRole('region',{name:'发起学习任务'});await form.getByRole('combobox',{name:'资料范围',exact:true}).selectOption('course');await form.getByRole('textbox',{name:'标题',exact:true}).fill('带缺口的整课讲义');await form.getByRole('button',{name:'开始生成并保存',exact:true}).click();}",
        )
        second = wait_api(
            browser,
            prefix + "/tasks",
            "value.length===2&&value.find(t=>t.task_id!=="
            + json.dumps(task["task_id"])
            + ")?.state==='needs_sources'",
            timeout_ms=30000,
        )
        partial_task = next(
            item for item in second if item["task_id"] != task["task_id"]
        )
        coverage = api(browser, prefix + f"/tasks/{partial_task['task_id']}/coverage")[
            "body"
        ]
        broken = next(
            item for item in coverage["sources"] if item["label"] == "broken.pdf"
        )
        check(
            broken["expected_leaf_count"] is None
            and broken["error_code"]
            and coverage["target"]["materialized"],
            "unknown_pdf_extent_pauses_complete_task_and_retains_good_asset",
            result,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('heading',{name:'等待处理来源缺口',exact:true}).waitFor({timeout:15000});}",
        )
        browser.snapshot("before-explicit-partial")
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'source-gaps.png'}",
            "--full-page",
        )
        before_requests = len(
            list(
                (instance.directory / "execution/boundary/private/boundary").glob(
                    "*.request.json"
                )
            )
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('button',{name:'接受当前部分成果并结束',exact:true}).click();await page.getByRole('heading',{name:'已接受部分成果',exact:true}).waitFor();}",
        )
        accepted = api(browser, prefix + f"/tasks/{partial_task['task_id']}")["body"]
        check(
            accepted["state"] == "partial"
            and accepted["scope_revision"] > partial_task["scope_revision"],
            "only_explicit_user_action_accepts_partial_with_scope_revision",
            result,
        )
        check(
            len(
                list(
                    (instance.directory / "execution/boundary/private/boundary").glob(
                        "*.request.json"
                    )
                )
            )
            == before_requests,
            "gap_decision_does_not_resend_model_work",
            result,
        )
        exercise_practice(browser, prefix, result)
        exercise_essays(browser, prefix, result, instance)
        browser.snapshot("workspace-complete")
        browser.command(
            "screenshot",
            f"--filename={browser.directory / 'workspace.png'}",
            "--full-page",
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
    parser.add_argument("--serve-fixture", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--backend-port", type=int, default=19235)
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
        raise RuntimeError("RUN_ID_INVALID_OR_PORT_IN_USE")
    output = ROOT / "output/playwright" / args.run_id
    output.mkdir(parents=True, exist_ok=False)
    outcome = run_case(output, args.browser_path, args.backend_port)
    print(
        json.dumps(
            {
                "status": outcome["status"],
                "assertions": len(outcome["assertions"]),
                "evidence": str(output / "result.json"),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if outcome["status"] == "passed" else 1)
