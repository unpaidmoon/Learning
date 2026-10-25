"""Edits of long text fields use disjoint intervals in one immutable base."""

from pydantic import BaseModel
from sqlalchemy import select

from learning_app.errors import AppError
from learning_app.storage.repository import _hash

from . import schema as s
from .common import save_operation
from .models import ArtifactBody, MarkdownBlock

RANGE_EDIT_VERSION = "original-cp-ranges-v1"


class PatchReceipt(BaseModel):
    task_id: str
    unit_id: str
    run_id: str
    edit_range: dict
    replacement: MarkdownBlock
    published_revision_id: str


class RangeEdits:
    @staticmethod
    def scope(base, block_ids):
        selected = [block for block in base.body.blocks if block.block_id in block_ids]
        if (
            sum(len(block.markdown) for block in selected) <= 24_000
            or any(block.kind not in {"markdown", "translation"} for block in selected)
            or any(not block.markdown.strip() for block in selected)
        ):
            return None
        return {
            "version": RANGE_EDIT_VERSION,
            "base_revision_id": base.revision_id,
            "blocks": {
                block.block_id: {
                    "field": "markdown",
                    "block_hash": _hash(block.model_dump(mode="json")),
                    "length": len(block.markdown),
                    "field_offset": len(block.translation.source_text) + 2
                    if block.kind == "translation"
                    else 0,
                }
                for block in selected
            },
        }

    @staticmethod
    def budget(settings):
        return min(6_000, max(128, settings["max_output_tokens"] // 2))

    @staticmethod
    def validate_work(conn, task, spec, target):
        scope = target["write_scope"].get("range_edit")
        edit = spec.get("edit_range")
        if not scope and not edit:
            return None
        if not scope or not edit or scope.get("version") != RANGE_EDIT_VERSION:
            raise AppError("TASK_EDIT_RANGE_INVALID", "分段修改的固定范围不完整，未执行")
        block = scope["blocks"].get(edit.get("block_id"))
        if (
            not block
            or edit.get("version") != RANGE_EDIT_VERSION
            or edit.get("base_revision_id") != scope["base_revision_id"]
            or edit.get("block_hash") != block["block_hash"]
            or edit.get("field") != "markdown"
            or type(edit.get("start")) is not int
            or type(edit.get("end")) is not int
            or not 0 <= edit["start"] < edit["end"] <= block["length"]
        ):
            raise AppError("TASK_EDIT_RANGE_INVALID", "分段修改越出固定原版本的正文范围", 403)
        matches = [item for item in spec["inputs"] if item["snapshot_id"] == edit["snapshot_id"]]
        if len(matches) != 1 or (
            matches[0]["char_start"],
            matches[0]["char_end"],
            matches[0]["snapshot_hash"],
        ) != (edit["start"], edit["end"], edit["snapshot_hash"]):
            raise AppError("TASK_EDIT_RANGE_INPUT_DENIED", "修改区间与实际读取的冻结片段不符", 403)
        from .artifacts import ArtifactStore
        from .sources import SourceStore

        base = ArtifactStore._revision(
            conn, target["reserved_artifact_id"], scope["base_revision_id"]
        )
        original = next(b for b in base.body.blocks if b.block_id == edit["block_id"])
        snapshot = SourceStore.in_transaction(conn, task["course_id"], edit["snapshot_id"])
        if (
            _hash(original.model_dump(mode="json")) != edit["block_hash"]
            or snapshot.text != original.markdown[edit["start"] : edit["end"]]
            or snapshot.content_hash != edit["snapshot_hash"]
        ):
            raise AppError("TASK_EDIT_RANGE_INPUT_DENIED", "分段正文与固定原版本不符", 403)
        return edit

    @staticmethod
    def validate_body(conn, task, run, body):
        edit = (run["input_contract"] or {}).get("edit_range")
        if not edit:
            return
        if (
            len(body.blocks) != 1
            or body.blocks[0].kind != "markdown"
            or body.blocks[0].block_id != edit["block_id"]
            or edit["snapshot_id"] not in body.blocks[0].source_snapshot_ids
        ):
            raise AppError(
                "TASK_EDIT_RANGE_OUTPUT_DENIED",
                "分段修改须只返回指定区间的正文和实际片段引用，不能替换整块或原文来源",
                403,
            )

    @staticmethod
    def receipts(conn, task_id, block_id):
        units = list(
            conn.execute(
                select(s.task_units).where(
                    s.task_units.c.task_id == task_id,
                    s.task_units.c.result_artifact_id.is_not(None),
                )
            ).mappings()
        )
        result = []
        for unit in units:
            edit = (unit["work_spec"] or {}).get("edit_range")
            if not edit or edit["block_id"] != block_id:
                continue
            row = (
                conn.execute(
                    select(s.operations).where(
                        s.operations.c.operation_id == "range_patch_" + unit["run_id"],
                        s.operations.c.kind == "task_range_patch",
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AppError("TASK_EDIT_RECEIPT_MISSING", "已发布区间缺少回执，先对账而不覆盖")
            receipt = PatchReceipt.model_validate(row["result"])
            if (
                receipt.task_id != task_id
                or receipt.unit_id != unit["unit_id"]
                or receipt.run_id != unit["run_id"]
                or receipt.edit_range != edit
                or row["payload_hash"] != _hash({"edit_range": edit, "run_id": unit["run_id"]})
            ):
                raise AppError("TASK_EDIT_RECEIPT_CONFLICT", "分段修改回执冲突，未重放或覆盖")
            result.append(receipt)
        return result

    @staticmethod
    def rebuild(original, receipts):
        cursor, pieces = 0, []
        citations = list(original.source_snapshot_ids)
        for receipt in sorted(receipts, key=lambda item: item.edit_range["start"]):
            edit = receipt.edit_range
            if edit["start"] < cursor:
                raise AppError("TASK_EDIT_RANGE_OVERLAP", "正文修改区间重叠，已有版本保留")
            pieces.extend((original.markdown[cursor : edit["start"]], receipt.replacement.markdown))
            cursor = edit["end"]
            if original.kind != "translation":
                citations.extend(receipt.replacement.source_snapshot_ids)
        pieces.append(original.markdown[cursor:])
        # The original-language field and its exact source parts are immutable.
        # Actual edit evidence is also retained in the per-range operation receipt.
        attribution = "source_paraphrase" if receipts else original.attribution
        return type(original).model_validate(
            {
                **original.model_dump(mode="json"),
                "markdown": "".join(pieces),
                "attribution": attribution,
                "source_snapshot_ids": list(dict.fromkeys(citations)),
            }
        )

    @classmethod
    def merge(cls, store, conn, task, unit, run, target, body):
        edit = cls.validate_work(conn, task, unit["work_spec"], target)
        cls.validate_body(conn, task, run, body)
        scope = target["write_scope"]["range_edit"]
        base = store._revision(conn, target["reserved_artifact_id"], scope["base_revision_id"])
        original = next(block for block in base.body.blocks if block.block_id == edit["block_id"])
        current = store._view(conn, task["course_id"], target["reserved_artifact_id"])
        receipts = cls.receipts(conn, task["task_id"], edit["block_id"])
        expected = cls.rebuild(original, receipts)
        actual = next(
            (block for block in current.revision.body.blocks if block.block_id == edit["block_id"]),
            None,
        )
        receipt = PatchReceipt(
            task_id=task["task_id"],
            unit_id=unit["unit_id"],
            run_id=run["run_id"],
            edit_range=edit,
            replacement=body.blocks[0],
            published_revision_id="pending",
        )
        replacement = cls.rebuild(original, [*receipts, receipt])
        # The last own publication is a safe conflict parent; unrelated current
        # blocks commute, but a manual edit in this block never silently merges.
        parent = (
            current.revision
            if actual == expected
            else store._revision(conn, current.artifact_id, target["expected_revision_id"])
        )
        proposed = {block.block_id: block for block in parent.body.blocks}
        proposed[edit["block_id"]] = replacement
        try:
            merged = ArtifactBody(blocks=list(proposed.values()))
        except ValueError as exc:
            raise AppError(
                "TASK_ARTIFACT_SIZE_LIMIT", "修改后正文超出大小上限，草稿仍保留"
            ) from exc
        result = store._revision_result(
            conn,
            current,
            parent,
            merged,
            parent.title,
            "mixed" if current.content_role in {"mixed", "user_note"} else "ai_generated",
            author="model",
            task_id=task["task_id"],
        )
        if result.outcome == "applied":
            save_operation(
                conn,
                task["course_id"],
                "task_range_patch",
                "range_patch_" + run["run_id"],
                {"edit_range": edit, "run_id": run["run_id"]},
                receipt.model_copy(
                    update={"published_revision_id": result.artifact.head_revision_id}
                ),
            )
        return result
