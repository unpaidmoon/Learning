"""Model-facing material, separate from complete program-owned source locators."""

import json

from learning_app.errors import AppError


class TaskSourceInputError(AppError):
    """The exact declared leaf failed before any dispatch; other inputs stay usable."""

    def __init__(self, error: AppError, leaf_id: str):
        super().__init__(error.code, error.message, error.status)
        self.source_leaf_ids = (leaf_id,)


def material(item, snapshot):
    reference = snapshot.reference.model_dump(mode="json")
    # Pixel geometry remains in the immutable source snapshot for exact jumps.
    # A text generation needs the selected characters, identity and page/range,
    # not thousands of display boxes or any surrounding document content.
    reference.pop("regions", None)
    if reference["kind"] == "document_selection":
        reference["parts"] = [
            {key: value for key, value in part.items() if key != "regions"}
            for part in reference["parts"]
        ]
    return {
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_hash": snapshot.content_hash,
        "content_role": snapshot.content_role,
        "source_course_id": snapshot.course_id,
        "reference": reference,
        "text": snapshot.text,
        "representation": item["representation"],
        "leaf_id": item["leaf_id"],
        "label": item["label"],
    }


def material_size(item, snapshot):
    return len(json.dumps(material(item, snapshot), ensure_ascii=False))
