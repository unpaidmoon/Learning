"""Exact image provenance shared by planning, native encoding and receipts."""

from learning_app.errors import AppError
from learning_app.storage.repository import _hash

NATIVE_RUNTIME_SHA256 = "f9ed01f241df37c50c702d81b6c226d576210b8f3947699735ca90285ea13365"


def validate_encoding(snapshot, proof, owner, model, *, prefix="DOCUMENT_IMAGE"):
    expected = {
        **image_grant(snapshot),
        **owner,
        "model": model,
        "runtime_sha256": NATIVE_RUNTIME_SHA256,
    }
    if (
        not isinstance(proof, dict)
        or any(proof.get(key) != value for key, value in expected.items())
        or proof.get("provider") not in {"deepseek-account", "deepseek-official"}
    ):
        raise AppError(prefix + "_PREPARATION_SCOPE", "图像派生回执与原图片、执行或模型不符")
    for name in ("normalized_sha256", "encoded_sha256"):
        value = proof.get(name)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise AppError(prefix + "_PREPARATION_INVALID", "图像派生摘要无效")
    if (
        proof.get("normalized_ref", {}).get("attachmentId")
        != "sha256:" + proof["normalized_sha256"]
        or proof.get("depth") != "uchar"
        or proof.get("space") != "srgb"
        or proof.get("encoded_media_type") not in {"image/png", "image/jpeg", "image/webp"}
        or any(
            type(proof.get(key)) is not int or proof[key] <= 0
            for key in ("encoded_bytes", "encoded_width", "encoded_height")
        )
        or proof["encoded_bytes"] > 14 * 1024 * 1024
        or max(proof["encoded_width"], proof["encoded_height"]) > 16384
    ):
        raise AppError(prefix + "_PREPARATION_INVALID", "图像编码身份、尺寸或预算无效")


def image_grant(snapshot):
    ref = snapshot.reference
    common = {
        "source_id": snapshot.snapshot_id,
        "course_id": snapshot.course_id,
        "source_sha256": ref.image_sha256,
    }
    if ref.kind == "document_region_image":
        return {
            **common,
            "kind": "document_region",
            "source_version": ref.source_version,
            "source_digest": ref.source_digest,
            "page_index": ref.page_index,
            "parent_text_snapshot_id": ref.parent_text_snapshot_id,
            "parent_text_hash": ref.parent_text_hash,
            "region_id": ref.region_id,
            "region_sha256": _hash(
                {
                    "region": ref.region.model_dump(mode="json"),
                    "pixel_rect": ref.pixel_rect,
                    "image_to_pdf": ref.image_to_pdf,
                    "parser": ref.region_parser_version,
                }
            ),
            "media_type": "image/png",
        }
    if ref.kind == "document_image":
        return {
            **common,
            "kind": "document_page",
            "source_version": ref.source_version,
            "source_digest": ref.source_digest,
            "page_index": ref.page_index,
            "media_type": "image/png",
        }
    if ref.kind == "submitted_image":
        return {
            **common,
            "kind": "submitted_image",
            "context_snapshot_id": ref.context_snapshot_id,
            "context_hash": ref.context_hash,
            "message_id": ref.message_id,
            "revision_id": ref.revision_id,
            "attachment_id": ref.attachment_id,
            "media_type": ref.media_type,
        }
    raise AppError("TASK_IMAGE_KIND_DENIED", "该来源不具备图像读取权限", 403)
