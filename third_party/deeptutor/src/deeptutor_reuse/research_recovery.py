"""Project recovery review over retained DT topic records; never dispatches work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from collections.abc import Mapping, Sequence

from .research_records import TopicBlock, TopicStatus


@dataclass(frozen=True)
class RecoveryReview:
    completed: tuple[str, ...]
    pending: tuple[str, ...]
    reconciliation_required: tuple[str, ...]
    failed_decision_required: tuple[str, ...]
    blocked: tuple[tuple[str, tuple[str, ...]], ...]


def _utc(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
    except (ValueError, TypeError, AttributeError):
        return False


def review_research_recovery(
    blocks: Sequence[TopicBlock],
    *,
    available_sources: frozenset[tuple[str, str, str]],
    available_evidence: Mapping[tuple[str, str], str],
) -> RecoveryReview:
    """Caller supplies authorized frozen sources/evidence, not filesystem paths.

    RESEARCHING always requires the application's actual attempt-ledger review.
    Missing sources/receipts never trigger external search or implicit retry.
    """
    ids = [b.block_id for b in blocks]
    if len(ids) != len(set(ids)) or any(not key for key in ids):
        raise ValueError("RESEARCH_TOPIC_ID_CONFLICT")
    completed = []
    pending = []
    reconcile = []
    failed = []
    blocked = []
    for block in blocks:
        reasons = []
        if not _utc(block.created_at) or not _utc(block.updated_at):
            reasons.append("RESEARCH_TIMESTAMP_REQUIRES_MIGRATION")
        refs = block.metadata.get("source_refs")
        if not isinstance(refs, list) or not refs:
            reasons.append("RESEARCH_SOURCE_REQUIRES_DECISION")
        else:
            for ref in refs:
                if (
                    not isinstance(ref, dict)
                    or (
                        ref.get("asset_id"),
                        ref.get("source_version"),
                        ref.get("content_role"),
                    )
                    not in available_sources
                ):
                    reasons.append("RESEARCH_SOURCE_VERSION_UNAVAILABLE")
                    break
        if block.status == TopicStatus.COMPLETED and not block.tool_traces:
            reasons.append("RESEARCH_EVIDENCE_MISSING")
        for trace in block.tool_traces:
            expected = available_evidence.get(
                (trace.evidence_asset_id, trace.evidence_version)
            )
            if expected != trace.evidence_sha256:
                reasons.append("RESEARCH_EVIDENCE_VERSION_UNAVAILABLE")
                break
        if reasons:
            blocked.append((block.block_id, tuple(reasons)))
        elif block.status == TopicStatus.COMPLETED:
            completed.append(block.block_id)
        elif block.status == TopicStatus.RESEARCHING:
            reconcile.append(block.block_id)
        elif block.status == TopicStatus.FAILED:
            failed.append(block.block_id)
        elif block.status == TopicStatus.PENDING:
            pending.append(block.block_id)
        else:
            raise ValueError("RESEARCH_TOPIC_STATUS_INVALID")
    return RecoveryReview(
        tuple(completed),
        tuple(pending),
        tuple(reconcile),
        tuple(failed),
        tuple(blocked),
    )
