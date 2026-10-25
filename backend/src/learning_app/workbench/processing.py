"""One durable local queue; current pages preempt batch work at owned boundaries."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import tempfile
import threading
from contextlib import suppress
from pathlib import Path

from learning_app.errors import AppError
from learning_app.storage.repository import _hash

from .local_workers import run_json_worker
from .ocr_resources import OcrResources
from .preparations import PreparationStore
from .processing_models import PreparationCreate
from .reading import DocumentReader
from .subtitle_waits import SubtitleWaits
from .transcribing import LocalTranscriber

logger = logging.getLogger(__name__)


class PreparationService:
    def __init__(
        self,
        repository,
        project_root: Path,
        *,
        is_busy=None,
        is_stopping=None,
        is_playing=None,
        is_interacting=None,
        admission_gate=None,
        get_resource_policy=None,
    ):
        self.repository, self.project_root = repository, project_root
        self.store = PreparationStore(repository)
        self.is_busy = is_busy or (lambda: False)
        self.is_stopping = is_stopping or (lambda: False)
        self.is_playing = is_playing or (lambda: False)
        self.is_interacting = is_interacting or (lambda: False)
        self.subtitle_waits = SubtitleWaits(repository)
        self.admission_gate = admission_gate or asyncio.Lock()
        self._wake = asyncio.Event()
        self._worker = None
        self._closing = False
        self._active: dict | None = None
        self._cancelled: threading.Event | None = None
        self.get_resource_policy = get_resource_policy or (lambda: None)

    async def open(self):
        from learning_app.data_activation import require_active_data

        require_active_data(self.repository.root)
        await asyncio.to_thread(self.store.recover)
        self._worker = asyncio.create_task(self._work(), name="source-preparation-worker")
        self.wake()

    def wake(self):
        self._wake.set()

    def interrupt(self, job_id: str):
        if self._active and self._active["job_id"] == job_id and self._cancelled:
            self._cancelled.set()
        self.wake()

    def create_pdf(self, course_id: str, value: PreparationCreate):
        return self.store.create_pdf(course_id, value, self.project_root)

    def create_asr(self, course_id: str, value: PreparationCreate):
        return self.store.publish_pdf(self.store.define_asr(course_id, value, self.project_root))

    def _ocr(self, lease: dict, cancelled: threading.Event) -> dict:
        reader = DocumentReader(self.repository)
        page = reader.page(
            lease["course_id"],
            lease["asset_id"],
            lease["source_version"],
            lease["unit_index"],
            cancelled=cancelled,
        )
        if page.source_digest != lease["source_digest"]:
            raise AppError("SOURCE_VERSION_UNAVAILABLE", "资料版本与任务冻结版本不符")
        if page.text_status != "needs_ocr":
            return {
                "kind": "cached_page",
                "parser_version": page.parser_version,
                "source_digest": page.source_digest,
                "page_index": page.page_index,
                "engine_invocations": 0,
            }
        resources = OcrResources(self.project_root)
        spec = resources.launch_spec()
        if spec["manifest_sha256"] != lease["parameters"]["manifest_sha256"]:
            raise AppError("OCR_RESOURCE_VERSION_CONFLICT", "任务冻结的OCR引擎与当前版本不同")
        evidence = (
            self.repository.root
            / "preparation-evidence"
            / lease["job_id"]
            / str(lease["unit_index"])
        )
        if not evidence.resolve().is_relative_to(self.repository.root.resolve()):
            raise AppError("OCR_EVIDENCE_PATH_INVALID", "OCR结果目录归属不符")
        input_hash = _hash(
            {
                "source_digest": lease["source_digest"],
                "asset_id": lease["asset_id"],
                "unit_index": lease["unit_index"],
                "parameters": lease["parameters"],
            }
        )
        for saved in evidence.glob("*.result.json"):
            value = json.loads(saved.read_text("utf-8"))
            expected = saved.name.split(".")[-3]
            if _hash(value) != expected:
                raise AppError("OCR_CHECKPOINT_INTEGRITY", "本地识别留稿校验失败，未自动重算或覆盖")
            if value.get("input_hash") == input_hash:
                return value
        with tempfile.TemporaryDirectory(
            prefix="page-ocr-", dir=self.repository.staging
        ) as temporary:
            directory = Path(temporary).resolve()
            if not directory.is_relative_to(self.repository.staging.resolve()):
                raise AppError("OCR_TEMPORARY_PATH_INVALID", "OCR临时目录归属不符")
            image, rendered = reader.render(
                lease["course_id"],
                lease["asset_id"],
                lease["source_version"],
                lease["unit_index"],
                directory,
                cancelled=cancelled,
            )
            request = {
                "protocol_version": 1,
                "request_id": lease["lease_id"],
                "epoch": lease["lease_epoch"],
                "spec": spec,
                "image_base64": base64.b64encode(image).decode("ascii"),
                "image_sha256": rendered["image_sha256"],
                "psm": lease["parameters"]["psm"],
            }
            raw = run_json_worker(
                [
                    str(self.project_root / ".tools/node/node.exe"),
                    "--max-old-space-size=512",
                    str(Path(__file__).with_name("ocr_worker.cjs")),
                ],
                request,
                directory=directory,
                cancelled=cancelled,
                on_progress=lambda value: self.store.progress(lease, value),
                log_path=evidence / f"{lease['lease_id']}.jsonl",
                timeout_seconds=120,
                resource_policy=lease.get("_resource_policy"),
            )
            if raw["image_sha256"] != rendered["image_sha256"] or (raw["width"], raw["height"]) != (
                rendered["width"],
                rendered["height"],
            ):
                raise AppError("OCR_RESULT_INTEGRITY", "OCR结果与本次原页位图不符")
            result = {
                "kind": "ocr",
                "source_digest": lease["source_digest"],
                "page_index": lease["unit_index"],
                "render": rendered,
                "raw": {
                    key: value for key, value in raw.items() if key not in {"request_id", "epoch"}
                },
                "engine_manifest_sha256": spec["manifest_sha256"],
                "engine_invocations": 1,
                "input_hash": input_hash,
            }
            (evidence / f"{lease['lease_id']}.{_hash(result)}.result.json").write_text(
                json.dumps(result, ensure_ascii=False), "utf-8"
            )
            return result

    async def _work(self):
        while not self._closing:
            if (
                self.is_stopping()
                or self.is_interacting()
                or (self.is_busy() and not self.subtitle_waits.has_active())
            ):
                await asyncio.sleep(0.1)
                continue
            async with self.admission_gate:
                if (
                    self.is_stopping()
                    or self.is_interacting()
                    or (self.is_busy() and not self.subtitle_waits.has_active())
                ):
                    continue
                lease = self.store.claim(native_wait_only=self.is_busy())
            if lease is None:
                # Idle means idle: no course discovery, indexing or OCR.
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1)
                except TimeoutError:
                    pass
                continue
            self._active, self._cancelled = lease, threading.Event()
            lease["_resource_policy"] = self.get_resource_policy()
            handler = (
                self._ocr
                if lease["kind"] == "pdf_ocr"
                else LocalTranscriber(
                    self.repository,
                    self.project_root,
                    self.store.progress,
                    resource_policy=lease.get("_resource_policy"),
                ).run
            )
            work = asyncio.create_task(asyncio.to_thread(handler, lease, self._cancelled))
            try:
                while not work.done():
                    await asyncio.wait([work], timeout=0.1)
                    if (
                        self._closing
                        or self.is_stopping()
                        or self.is_interacting()
                        or (
                            self.is_busy()
                            and not self.subtitle_waits.job_is_active(lease["job_id"])
                        )
                        or (
                            lease["parameters"].get("native_read_run_id") is not None
                            and not self.subtitle_waits.job_is_active(lease["job_id"])
                        )
                        or await asyncio.to_thread(
                            self.store.should_yield, lease, native_wait_only=self.is_busy()
                        )
                    ):
                        self._cancelled.set()
                result = work.result()
                await asyncio.to_thread(self.store.finish, lease, result)
            except AppError as error:
                if error.code == "PREPARATION_INTERRUPTED":
                    await asyncio.to_thread(self.store.release, lease)
                else:
                    await asyncio.to_thread(self.store.finish, lease, None, error.code)
                    logger.error("Source preparation failed: %s", error.code)
            except asyncio.CancelledError:
                self._cancelled.set()
                with suppress(AppError, OSError, ValueError):
                    await asyncio.shield(work)
                await asyncio.to_thread(self.store.release, lease)
                raise
            except (OSError, ValueError, KeyError) as error:
                await asyncio.to_thread(self.store.finish, lease, None, "LOCAL_PREPARATION_FAILED")
                logger.error("Source preparation failed: %s", type(error).__name__)
            finally:
                self._active = self._cancelled = None
            if self.is_playing():
                await asyncio.sleep(0.3)

    async def close(self):
        self._closing = True
        if self._cancelled:
            self._cancelled.set()
        self.wake()
        if self._worker:
            await self._worker
            self._worker = None
