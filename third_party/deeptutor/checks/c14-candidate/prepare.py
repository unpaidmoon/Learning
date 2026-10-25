"""Freeze read-only C14 source/candidate snapshots, never edit package modules."""

from pathlib import Path
import ast
import hashlib
import json

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parents[1]
REPO = PACKAGE.parents[1]
manifest = json.loads((PACKAGE / "reuse-manifest.json").read_text("utf-8"))
known = {r["path"]: r for r in manifest["upstream_files"]}
paths = [
    "deeptutor/knowledge/indexing_run.py",
    "deeptutor/services/parsing/engines/mineru/checkpoints.py",
    "deeptutor/services/rag/index_versioning.py",
    "deeptutor/services/rag/pipelines/llamaindex/pipeline.py",
    "deeptutor/services/rag/pipelines/llamaindex/storage.py",
    "web/features/chat/transport/TurnRuntimeClient.ts",
    "web/hooks/useChatAutoScroll.ts",
]
records = []
for name in paths:
    raw = (PACKAGE / "upstream" / name).read_bytes()
    if name in known:
        assert hashlib.sha256(raw).hexdigest() == known[name]["sha256"]
        assert (
            hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            == known[name]["git_blob_sha1"]
        )
    destination = HERE / "upstream" / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    records.append(
        {
            "source": "DeepTutor",
            "path": name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "commit": "a31d82624c27d75f4e771d4fcb78dce16470b383",
        }
    )

text = (
    HERE / "upstream/deeptutor/services/parsing/engines/mineru/checkpoints.py"
).read_text("utf-8")
tree = ast.parse(text)
node = next(
    n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SliceCheckpoint"
)
lines = text.splitlines(keepends=True)
body = "".join(lines[node.lineno - 1 : node.end_lineno])
(HERE / "slice_checkpoint.py").write_text(
    '''"""Original SliceCheckpoint excerpt; job/root/source authority is external.
Only a candidate, not part of the frozen runtime wheel. Valid hash != valid index.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import tempfile
_MAX_ARCHIVE_BYTES=500*1024*1024

'''
    + body,
    "utf-8",
    newline="\n",
)
original = (HERE / "upstream/deeptutor/knowledge/indexing_run.py").read_text("utf-8")
probe = original.replace(
    "from deeptutor.services.file_io import atomic_write_json, read_text",
    "from deeptutor_reuse.file_io import atomic_write_json, read_text",
)
(HERE / "indexing_run_probe.py").write_text(probe, "utf-8", newline="\n")

project = [
    "frontend/src/reading/readerSession.ts",
    "frontend/src/reading/pdfAdapter.ts",
    "frontend/src/reading/PdfReader.tsx",
    "backend/src/learning_app/workbench/reading.py",
    "backend/src/learning_app/workbench/workspace.py",
]
for name in project:
    raw = (REPO / name).read_bytes()
    target = HERE / "project-snapshots" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    records.append(
        {
            "source": "Learning readonly working snapshot",
            "path": name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        }
    )
protection = {
    str(p.relative_to(PACKAGE)).replace("\\", "/"): hashlib.sha256(
        p.read_bytes()
    ).hexdigest()
    for p in (PACKAGE / "src/deeptutor_reuse").glob("*.py")
}
protection["pyproject.toml"] = hashlib.sha256(
    (PACKAGE / "pyproject.toml").read_bytes()
).hexdigest()
protection["uv.lock"] = hashlib.sha256((PACKAGE / "uv.lock").read_bytes()).hexdigest()
(HERE / "source-manifest.json").write_text(
    json.dumps(
        {
            "sources": records,
            "frozen_package_hashes": protection,
            "candidate_changes": [
                "SliceCheckpoint class excerpt unchanged; fixed cap and standard-library imports.",
                "Full indexing_run probe retargets one file_io import; never calls _start or context entry.",
                "Project files are read-only snapshots, not runtime dependencies.",
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
    + "\n",
    "utf-8",
)
(HERE / "notices").mkdir(exist_ok=True)
(HERE / "notices/DeepTutor-LICENSE").write_bytes((PACKAGE / "LICENSE").read_bytes())
print(
    json.dumps(
        {
            "upstream_sources": len(paths),
            "project_snapshots": len(project),
            "package_modules_frozen": len(protection) - 2,
        }
    )
)
