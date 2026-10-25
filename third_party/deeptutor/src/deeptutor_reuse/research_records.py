"""Retained DT topic records/query methods with explicit project adaptations.

No authoritative queue, filesystem persistence, provider or automatic dispatch.
Full source evidence stays in application-owned versioned assets; traces hold refs.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from .research_similarity import TopicSimilarity, DEFAULT_TOPIC_SIMILARITY_THRESHOLD

@dataclass
class TraceReference:
    tool_id: str
    citation_id: str
    tool_type: str
    query: str
    summary: str
    evidence_asset_id: str
    evidence_version: str
    evidence_sha256: str
    operation_id: str

    def __post_init__(self):
        if not all((self.tool_id,self.citation_id,self.evidence_asset_id,self.evidence_version,self.operation_id)):
            raise ValueError("RESEARCH_TRACE_REFERENCE_INVALID")
        if len(self.evidence_sha256)!=64 or any(c not in "0123456789abcdef" for c in self.evidence_sha256):
            raise ValueError("RESEARCH_TRACE_REFERENCE_INVALID")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls,data):
        if "raw_answer" in data:
            raise ValueError("RESEARCH_LEGACY_RAW_ANSWER_REQUIRES_MIGRATION")
        return cls(**data)

class TopicStatus(Enum):
    """Topic block status enumeration"""

    PENDING = "pending"  # Pending research
    RESEARCHING = "researching"  # Researching
    COMPLETED = "completed"  # Completed
    FAILED = "failed"  # Failed


@dataclass
class TopicBlock:
    """
    Topic block - Minimum scheduling unit in queue
    """

    block_id: str  # Unique identifier (e.g., "block_1", "block_2")
    sub_topic: str  # Sub-topic name
    overview: str  # Topic overview/background
    status: TopicStatus = TopicStatus.PENDING  # Topic status
    tool_traces: list[TraceReference] = field(default_factory=list)  # Tool call trace list
    iteration_count: int = 0  # Current iteration count
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)  # Additional metadata

    def add_tool_trace(self, trace: TraceReference) -> None:
        """Add tool trace"""
        self.tool_traces.append(trace)
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def get_latest_trace(self) -> TraceReference | None:
        """Get latest tool trace"""
        return self.tool_traces[-1] if self.tool_traces else None

    def get_all_summaries(self) -> str:
        """Get concatenated summaries of all tool traces"""
        if not self.tool_traces:
            return ""
        return "\n".join([f"[{trace.tool_type}] {trace.summary}" for trace in self.tool_traces])

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary"""
        data = asdict(self)
        data["status"] = self.status.value
        data["tool_traces"] = [trace.to_dict() for trace in self.tool_traces]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TopicBlock":
        """Create from dictionary"""
        data_copy = data.copy()
        if isinstance(data_copy.get("status"), str):
            data_copy["status"] = TopicStatus(data_copy["status"])
        if "tool_traces" in data_copy:
            data_copy["tool_traces"] = [
                TraceReference.from_dict(t) if isinstance(t, dict) else t
                for t in data_copy["tool_traces"]
            ]
        return cls(**data_copy)


@dataclass(frozen=True)
class ResearchTopicView(TopicSimilarity):
    """Read-only projection over application-supplied records, not a task queue."""
    blocks: tuple[TopicBlock, ...]

    def has_topic(self, sub_topic: str) -> bool:
        """Check if topic already exists (case-insensitive, ignoring leading/trailing spaces)"""
        target = self._normalize_topic(sub_topic)
        if not target:
            return False
        return any(self._normalize_topic(b.sub_topic) == target for b in self.blocks)


    def find_similar(
        self,
        sub_topic: str,
        *,
        threshold: float = DEFAULT_TOPIC_SIMILARITY_THRESHOLD,
    ) -> TopicBlock | None:
        """Return an existing block whose title is fuzzily similar to
        ``sub_topic``, or ``None`` when no match exceeds ``threshold``.

        Used to dedup ``APPEND`` requests so the LLM can't reliably keep
        proposing the same topic in slightly different words. Exact
        normalised matches always win; otherwise the highest-scoring
        block above ``threshold`` is returned.
        """
        target = self._normalize_topic(sub_topic)
        if not target:
            return None

        best: tuple[float, TopicBlock] | None = None
        for block in self.blocks:
            candidate = self._normalize_topic(block.sub_topic)
            if not candidate:
                continue
            if candidate == target:
                return block
            score = self._topic_similarity(target, candidate)
            if score >= threshold and (best is None or score > best[0]):
                best = (score, block)
        return best[1] if best else None


    def list_topics(self) -> list[str]:
        """List all current topic titles"""
        return [b.sub_topic for b in self.blocks]


    def get_pending_block(self) -> TopicBlock | None:
        """
        Get first pending topic block

        Returns:
            First TopicBlock with PENDING status, or None if not found
        """
        for block in self.blocks:
            if block.status == TopicStatus.PENDING:
                return block
        return None


    def get_block_by_id(self, block_id: str) -> TopicBlock | None:
        """
        Get topic block by ID

        Args:
            block_id: Topic block ID

        Returns:
            Corresponding TopicBlock, or None if not found
        """
        for block in self.blocks:
            if block.block_id == block_id:
                return block
        return None


    def get_all_completed_blocks(self) -> list[TopicBlock]:
        """Get all completed topic blocks"""
        return [b for b in self.blocks if b.status == TopicStatus.COMPLETED]


    def get_all_pending_blocks(self) -> list[TopicBlock]:
        """Get all pending topic blocks"""
        return [b for b in self.blocks if b.status == TopicStatus.PENDING]


    def is_all_completed(self) -> bool:
        """Check if all topic blocks are completed"""
        if not self.blocks:
            return False
        return all(b.status == TopicStatus.COMPLETED for b in self.blocks)


    def get_statistics(self) -> dict[str, Any]:
        """Get queue statistics"""
        return {
            "total_blocks": len(self.blocks),
            "pending": len(self.get_all_pending_blocks()),
            "researching": len([b for b in self.blocks if b.status == TopicStatus.RESEARCHING]),
            "completed": len(self.get_all_completed_blocks()),
            "failed": len([b for b in self.blocks if b.status == TopicStatus.FAILED]),
            "total_tool_calls": sum(len(b.tool_traces) for b in self.blocks),
        }


