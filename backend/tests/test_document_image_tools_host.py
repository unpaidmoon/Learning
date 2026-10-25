"""Pinned Harness text query -> local figure -> true tool-result image -> history."""

import asyncio
import base64
import io
import threading
from pathlib import Path

import pytest
from pdf_graphics_support import GraphicsPDF, clipped_form_pdf
from PIL import Image
from sqlalchemy import select
from test_course_corpus import index_all
from test_document_regions import document_parent
from workbench_support import learning_course

from learning_app.config import AppConfig
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.boundary import MessagesBoundary, _image_summary
from learning_app.execution.native_tools import NativeCallRegistry, NativeToolTransport
from learning_app.service import GenerationService
from learning_app.workbench import corpus_schema as c
from learning_app.workbench import document_image_schema as d
from learning_app.workbench import schema as s
from learning_app.workbench.corpus import CourseCorpus
from learning_app.workbench.native_business import NativeBusiness

ROOT = Path(__file__).resolve().parents[2]


class FigureFixture(MessagesBoundary):
    def __init__(self, path, registry, repo):
        super().__init__(path, native_registry=registry)
        self.repo, self.requests, self.steps, self.first = repo, [], {}, None
        self.repeat_image = False

    def _fixture(self, handler, request, scenario, row, observer):
        run_id = row["generation_id"]
        self.requests.append((run_id, request))
        self.first = self.first or run_id
        step = self.steps.get(run_id, 0) + 1
        self.steps[run_id] = step
        calls = []
        if run_id == self.first:
            if step == 1:
                calls = [{"id": "load_skill", "name": "skill", "input": {"name": "course-read"}}]
            elif step == 2:
                calls = [
                    {
                        "id": "read_text",
                        "name": "search_course_concept",
                        "input": {"concept": "green diagram"},
                    }
                ]
            elif step in {3, 4} or (step == 5 and self.repeat_image):
                with self.repo.engine.connect() as conn:
                    receipts = list(
                        conn.execute(
                            select(c.query_receipts.c.result).where(
                                c.query_receipts.c.run_id == run_id
                            )
                        ).scalars()
                    )
                query = next(value for value in receipts if value.get("results"))
                parent = query["results"][0]
                arguments = {key: parent[key] for key in ("handle_id", "source_version")}
                arguments["region_id"] = None
                if step >= 4:
                    location = next(value for value in receipts if value.get("regions"))
                    # The upper green image is selected; no neighboring pixels.
                    arguments["region_id"] = min(
                        location["regions"],
                        key=lambda value: (value["kind"] != "form_image", value["region"]["y0"]),
                    )["region_id"]
                calls = [
                    {
                        "id": "locate_figure"
                        if step == 3
                        else "read_figure"
                        if step == 4
                        else "read_figure_again",
                        "name": "read_document_region",
                        "input": arguments,
                    }
                ]
        return super()._fixture(
            handler,
            request,
            {
                "text_chunks": [] if calls else ["本轮来源读取处理已结束。"],
                "tool_calls": calls,
            },
            row,
            observer,
        )


def tool_images(request):
    return [
        part
        for message in request["messages"]
        if isinstance(message.get("content"), list)
        for result in message["content"]
        if result.get("type") == "tool_result" and isinstance(result.get("content"), list)
        for part in result["content"]
        if part.get("type") == "image"
    ]


@pytest.mark.host
@pytest.mark.parametrize("repeat_image", [False, True], ids=["single_read", "duplicate_read"])
@pytest.mark.parametrize(
    "rebuild_history", [False, True], ids=["retained_session", "revised_input"]
)
async def test_real_native_tool_image_and_frozen_history_without_reopening_original(
    tmp_path, repeat_image, rebuild_history, graphics_layout="raster"
):
    repo, course, root, _ = learning_course(tmp_path)
    if graphics_layout == "form":
        pdf = clipped_form_pdf(root / "form.pdf", nested=True, fractional=True)
    elif graphics_layout == "vector":
        pdf = GraphicsPDF().write(
            root / "vector.pdf", "0 .7 0 rg 60 240 80 50 re f\n1 0 0 rg 290 70 50 40 re f"
        )
    else:
        pdf, _ = document_parent(repo, course, root)
    index_all(CourseCorpus(repo), course)
    repo.create_chat(course, "figure_chat")
    port = NativeBusiness(repo)
    registry = NativeCallRegistry(tmp_path / "native.jsonl")
    boundary = FigureFixture(tmp_path / "boundary", registry, repo)
    boundary.repeat_image = repeat_image
    adapter = ExecutionAdapter(
        ROOT, tmp_path / "host", boundary=boundary, native_tools=NativeToolTransport(registry, port)
    )
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        try:
            first = await service.submit(
                {
                    "chat_id": "figure_chat",
                    "submission_id": "figure_first",
                    "text": "请依据本课green diagram的文字和对应局部图解释。",
                    "model": "deepseek-flash",
                    "thinking": "high",
                }
            )
            await asyncio.wait_for(service.tasks[first["generation_id"]], 65)
            run = repo.get_generation(first["generation_id"])
            assert run["state"] == "completed", run
            requests = [
                request
                for identity, request in boundary.requests
                if identity == first["generation_id"]
            ]
            assert len(requests) == (6 if repeat_image else 5) and all(
                not tool_images(value) for value in requests[:4]
            )
            pictures = tool_images(requests[-1])
            assert (
                len(pictures) == (2 if repeat_image else 1)
                and pictures[0]["source"]["type"] == "base64"
            )
            raw = base64.b64decode(pictures[0]["source"]["data"])
            with Image.open(io.BytesIO(raw)) as image:
                assert image.width < 1000 and image.height < 400
                assert not any(r > 150 and g < 30 for r, g, b in image.convert("RGB").getdata())
            with repo.engine.connect() as conn:
                effects = list(
                    conn.execute(
                        select(d.image_reads).where(
                            d.image_reads.c.run_id == first["generation_id"]
                        )
                    )
                    .mappings()
                    .all()
                )
                assert len(effects) == (2 if repeat_image else 1)
                effect = effects[0]
                assert effect["state"] == "completed"
                proof = effect["result"]["encoding_proof"]
            assert boundary.records[-1]["images"][0]["sha256"] == proof["encoded_sha256"]
            assert first["generation_id"] in registry.released
            # Only the frozen crop is used for legitimate history, even if the
            # original PDF disappears. No new document/neighbor read occurs.
            pdf.unlink()
            if rebuild_history:
                user = next(
                    message
                    for message in repo.get_chat("figure_chat")["messages"]
                    if message["role"] == "user"
                )
                repo.revise_input(
                    user["message_id"], user["text"] + "（已明确修订的文字条件；不重发旧问题。）"
                )
            second = await service.submit(
                {
                    "chat_id": "figure_chat",
                    "submission_id": "figure_followup",
                    "text": "继续解释刚才的局部图。",
                    "model": "deepseek-flash",
                    "thinking": "none",
                }
            )
            await asyncio.wait_for(service.tasks[second["generation_id"]], 45)
            followup = repo.get_generation(second["generation_id"])
            assert followup["state"] == "completed", followup
            with repo.engine.connect() as conn:
                scope = (
                    conn.execute(
                        select(s.native_scopes).where(
                            s.native_scopes.c.run_id == second["generation_id"]
                        )
                    )
                    .mappings()
                    .one()
                )
                assert {
                    item["effect_id"] for item in scope["grants"]["document_image_history"]
                } == {row["effect_id"] for row in effects}
                allowances = list(
                    conn.execute(
                        select(d.image_allowances).where(
                            d.image_allowances.c.run_id == second["generation_id"]
                        )
                    )
                    .mappings()
                    .all()
                )
                assert len(allowances) == len(effects)
                for allowance in allowances:
                    assert allowance["proof"]["source_sha256"] == proof["source_sha256"]
                    assert allowance["proof"]["run_id"] == second["generation_id"]
                    assert allowance["proof"]["model"] == "deepseek-flash"
            later = [
                request
                for identity, request in boundary.requests
                if identity == second["generation_id"]
            ]
            assert len(later) == 1
            if rebuild_history:
                assert len(tool_images(later[0])) == 0
                assert len(_image_summary(later[0])) == 1
                assert "READ_ONLY_DOCUMENT_IMAGE_HISTORY:" in str(later[0])
            else:
                assert len(tool_images(later[0])) == (2 if repeat_image else 1)
            assert second["generation_id"] in registry.released
            assert boundary.actual_outbound_attempts == 0
        finally:
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()


@pytest.mark.host
@pytest.mark.parametrize("graphics_layout", ["form", "vector"])
async def test_form_and_vector_true_tool_image_and_rebuilt_history(tmp_path, graphics_layout):
    await test_real_native_tool_image_and_frozen_history_without_reopening_original(
        tmp_path, False, True, graphics_layout
    )


@pytest.mark.host
@pytest.mark.parametrize("mode", ["disconnect", "truncate"])
async def test_true_lost_http_image_reply_reads_exact_receipt_without_reinvocation(tmp_path, mode):
    repo, port, registry, boundary, adapter = fault_runtime(tmp_path)
    lost = []

    def fault(path, envelope, result):
        if (
            path == "/bridge/invoke"
            and envelope.get("tool_name") == "read_document_region"
            and envelope["arguments"]["region_id"] is not None
            and not lost
        ):
            lost.append(mode)
            return mode

    adapter.native_tools.fault_hook = fault
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        encode = port.document_images.prepare
        encodings = []

        def counted(scope, values):
            encodings.append(len(values))
            return encode(scope, values)

        port.document_images.prepare = counted
        try:
            result = await service.submit(
                {
                    "chat_id": "figure_chat",
                    "submission_id": "figure_transport_fault",
                    "text": "按需读取本课local green diagram及局部图。",
                    "model": "deepseek-flash",
                    "thinking": "high",
                }
            )
            await asyncio.wait_for(service.tasks[result["generation_id"]], 65)
            assert repo.get_generation(result["generation_id"])["state"] == "completed"
            assert lost == [mode] and encodings == [1]
            assert len(boundary.requests) == 5 and len(tool_images(boundary.requests[-1][1])) == 1
            assert (
                sum(
                    event.get("path") == "/bridge/reconcile" and event.get("allowed") is True
                    for event in adapter.native_tools.events
                )
                == 1
            )
            assert (
                sum(
                    event.get("path") == "/bridge/invoke" and event.get("allowed") is True
                    for event in adapter.native_tools.events
                )
                == 3
            )
            assert (
                result["generation_id"] in registry.released
                and boundary.actual_outbound_attempts == 0
            )
        finally:
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()


def fault_runtime(tmp_path):
    repo, course, root, _ = learning_course(tmp_path)
    document_parent(repo, course, root)
    index_all(CourseCorpus(repo), course)
    repo.create_chat(course, "figure_chat")
    port = NativeBusiness(repo)
    registry = NativeCallRegistry(tmp_path / "native.jsonl")
    boundary = FigureFixture(tmp_path / "boundary", registry, repo)
    adapter = ExecutionAdapter(
        ROOT, tmp_path / "host", boundary=boundary, native_tools=NativeToolTransport(registry, port)
    )
    return repo, port, registry, boundary, adapter


@pytest.mark.host
@pytest.mark.parametrize(
    "point,delivered",
    [("document_image_after_commit", True), ("document_image_after_encoding", False)],
)
async def test_original_image_read_receipt_reconciles_without_invoking_or_encoding_again(
    tmp_path, point, delivered
):
    repo, port, registry, boundary, adapter = fault_runtime(tmp_path)
    seen = []

    def fault(name, detail):
        if name == point and not seen:
            seen.append(name)
            raise RuntimeError("owned document image receipt interruption")

    repo.fault_hook = fault
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        encode = port.document_images.prepare
        encodings = []

        def counted(scope, values):
            encodings.append(len(values))
            return encode(scope, values)

        port.document_images.prepare = counted
        try:
            result = await service.submit(
                {
                    "chat_id": "figure_chat",
                    "submission_id": "figure_fault",
                    "text": "按需查阅本课local green diagram及其局部图。",
                    "model": "deepseek-flash",
                    "thinking": "high",
                }
            )
            await asyncio.wait_for(service.tasks[result["generation_id"]], 65)
            run = repo.get_generation(result["generation_id"])
            assert run["state"] == "completed", run
            assert seen == [point] and encodings == [1]
            requests = [
                request
                for identity, request in boundary.requests
                if identity == result["generation_id"]
            ]
            assert len(requests) == 5 and bool(tool_images(requests[-1])) is delivered
            with repo.engine.connect() as conn:
                row = conn.execute(select(d.image_reads)).mappings().one()
                assert row["state"] == ("completed" if delivered else "failed")
            assert any(
                event["path"] == "/bridge/reconcile" for event in adapter.native_tools.events
            )
            assert (
                result["generation_id"] in registry.released
                and boundary.actual_outbound_attempts == 0
            )
        finally:
            repo.fault_hook = None
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()


@pytest.mark.host
async def test_active_image_read_cancel_denies_late_grant_and_same_chat_can_continue(tmp_path):
    repo, _, registry, boundary, adapter = fault_runtime(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def pause(name, detail):
        if name == "document_image_before_encoding":
            entered.set()
            if not release.wait(15):
                raise RuntimeError("owned image gate was not released")

    repo.fault_hook = pause
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        try:
            first = await service.submit(
                {
                    "chat_id": "figure_chat",
                    "submission_id": "figure_cancel",
                    "text": "按需读取本课local green diagram及其局部图。",
                    "model": "deepseek-flash",
                    "thinking": "high",
                }
            )
            for _ in range(300):
                if entered.is_set():
                    break
                await asyncio.sleep(0.03)
            assert entered.is_set()
            cancel = asyncio.create_task(service.cancel(first["generation_id"]))
            await asyncio.sleep(0.05)
            assert repo.get_generation(first["generation_id"])["cancel_requested"]
            release.set()
            await asyncio.wait_for(cancel, 12)
            await asyncio.wait_for(service.tasks[first["generation_id"]], 15)
            assert repo.get_generation(first["generation_id"])["state"] == "cancelled"
            with repo.engine.connect() as conn:
                assert list(conn.execute(select(d.image_allowances))) == []
            assert not any(tool_images(request) for _, request in boundary.requests)
            second = await service.submit(
                {
                    "chat_id": "figure_chat",
                    "submission_id": "after_figure_cancel",
                    "text": "继续普通说明，不读取新图。",
                    "model": "deepseek-flash",
                    "thinking": "high",
                }
            )
            await asyncio.wait_for(service.tasks[second["generation_id"]], 35)
            assert repo.get_generation(second["generation_id"])["state"] == "completed"
            assert first["generation_id"] in registry.released
            assert boundary.actual_outbound_attempts == 0
        finally:
            release.set()
            repo.fault_hook = None
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()
