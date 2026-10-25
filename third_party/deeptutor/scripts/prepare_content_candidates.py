"""Fixed-source pure excerpts; no business queue, model or dependency change."""

from pathlib import Path
import ast
import base64
import hashlib
import json
import shutil

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
HERE = ROOT / "checks/content-candidate"
HERE.mkdir(exist_ok=True)
SOURCE = ROOT / "src/deeptutor_reuse"
intake = json.loads(
    (
        REPO / ".local/cache/workbench/deeptutor-c06-c08-c13/matching-intake.json"
    ).read_text("utf-8")
)
raw = base64.b64decode(intake["content"])
assert (
    hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    == intake["sha"]
)
matching = ROOT / "upstream/deeptutor/services/rag/eval/matching.py"
matching.parent.mkdir(parents=True, exist_ok=True)
matching.write_bytes(raw)
(SOURCE / "eval_matching.py").write_bytes(raw)

# C06 belongs to DS, not DT. Keep its archival/license boundary in checks.
ds_manifest = json.loads(
    (REPO / "third_party/deepstudent/manifest.json").read_text("utf-8")
)
ds_items = {x["source_path"]: x for x in ds_manifest["files"]}
archived = []
for name in ("ir.ts", "markdown.ts", "windowing.ts"):
    path = "src/features/media-handout/" + name
    source = REPO / "third_party/deepstudent/upstream" / path
    content = source.read_bytes()
    record = ds_items[path]
    assert hashlib.sha256(content).hexdigest() == record["sha256"]
    assert (
        hashlib.sha1(
            b"blob " + str(len(content)).encode() + b"\0" + content
        ).hexdigest()
        == record["git_blob_sha1"]
    )
    dest = HERE / "upstream/deepstudent" / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    archived.append(
        {
            "path": str(dest.relative_to(HERE)).replace("\\", "/"),
            "source_path": path,
            "sha256": record["sha256"],
            "git_blob_sha1": record["git_blob_sha1"],
        }
    )
notices = HERE / "notices"
notices.mkdir(exist_ok=True)
shutil.copyfile(
    REPO / "third_party/deepstudent/notices/LICENSE.AGPL-3.0",
    notices / "DeepStudent-LICENSE.AGPL-3.0",
)
shutil.copyfile(
    REPO / "third_party/deepstudent/notices/THIRD_PARTY_LICENSES.upstream.txt",
    notices / "DeepStudent-THIRD-PARTY.upstream.txt",
)

# Original DT topic DTO methods and read-only queue queries. No append/mark/
# save/load/auto-save methods or default executor enter the runtime package.
original = ROOT / "upstream/deeptutor/agents/research/data_structures.py"
text = original.read_text("utf-8")
tree = ast.parse(text)
status = next(
    n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TopicStatus"
)
topic = next(
    n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TopicBlock"
)
queue = next(
    n
    for n in tree.body
    if isinstance(n, ast.ClassDef) and n.name == "DynamicTopicQueue"
)
methods = {n.name: n for n in queue.body if isinstance(n, ast.FunctionDef)}


def segment(node):
    lines = text.splitlines(keepends=True)
    start = (
        min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]) - 1
    )
    return "".join(lines[start : node.end_lineno])


head = '''"""Retained DT topic records/query methods with explicit project adaptations.

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

'''
adapted = (
    segment(topic)
    .replace("ToolTrace", "TraceReference")
    .replace("datetime.now()", "datetime.now(timezone.utc)")
)
read_only = [
    "has_topic",
    "find_similar",
    "list_topics",
    "get_pending_block",
    "get_block_by_id",
    "get_all_completed_blocks",
    "get_all_pending_blocks",
    "is_all_completed",
    "get_statistics",
]
view = '\n\n@dataclass(frozen=True)\nclass ResearchTopicView(TopicSimilarity):\n    """Read-only projection over application-supplied records, not a task queue."""\n    blocks: tuple[TopicBlock, ...]\n\n'
for name in read_only:
    view += segment(methods[name]) + "\n\n"
(SOURCE / "research_records.py").write_text(
    head + segment(status) + "\n\n" + adapted + view, "utf-8"
)
record = {
    "deeptutor_commit": "a31d82624c27d75f4e771d4fcb78dce16470b383",
    "deepstudent_commit": ds_manifest["source_commit"],
    "C06_sources": archived,
    "C08_matching": {
        "path": "deeptutor/services/rag/eval/matching.py",
        "git_blob_sha1": intake["sha"],
        "sha256": hashlib.sha256(raw).hexdigest(),
    },
    "C13": {
        "source": "deeptutor/agents/research/data_structures.py",
        "source_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
        "original_query_methods": read_only,
        "adaptations": [
            "UTC timestamps",
            "TraceReference replaces truncating raw answers",
            "No mutating queue or file IO methods",
        ],
    },
}
(HERE / "extraction-manifest.json").write_text(
    json.dumps(record, ensure_ascii=False, indent=2) + "\n", "utf-8"
)
print(
    json.dumps(
        {
            "matching_sha256": record["C08_matching"]["sha256"],
            "C06_files": len(archived),
            "C13_readonly_methods": len(read_only),
        }
    )
)
