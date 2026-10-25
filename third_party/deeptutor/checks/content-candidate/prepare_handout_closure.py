"""Prepare a directly importable TS closure; never include frame/prompt pipeline."""

from pathlib import Path
import hashlib
import json

HERE = Path(__file__).resolve().parent
RAW = HERE / "upstream/deepstudent/src/features/media-handout"
OUT = HERE / "retained"
OUT.mkdir(exist_ok=True)
ir = (RAW / "ir.ts").read_text("utf-8")
prefix = ir.split("\n/**\n * 两次解析失败后的兜底", 1)[0]
assert (
    len(prefix) < len(ir)
    and "collectFigureTimestamps" not in prefix
    and "salvageBlocks" not in prefix
)
(OUT / "ir.ts").write_text(prefix, "utf-8", newline="\n")
markdown = (
    (RAW / "markdown.ts")
    .read_text("utf-8")
    .replace("from './prompts'", "from './handout_types'")
)
(OUT / "markdown.ts").write_text(markdown, "utf-8", newline="\n")
(OUT / "handout_types.ts").write_text(
    "// Project type-only closure; no prompts or frame pipeline.\nexport type HandoutLang = 'zh' | 'en';\n",
    "utf-8",
    newline="\n",
)
manifest = json.loads((HERE / "extraction-manifest.json").read_text("utf-8"))
manifest["C06_runtime"] = [
    {
        "path": str(p.relative_to(HERE)).replace("\\", "/"),
        "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
    }
    for p in sorted(OUT.glob("*.ts"))
]
manifest["C06_runtime_changes"] = [
    "Original IR prefix retained, excluding salvage and frame-timestamp collector.",
    "Original Markdown method bodies retained; type-only prompts import redirected to local type alias.",
    "Public wrapper imports retained closure; no windowing/frame/prompt runtime modules.",
]
(HERE / "extraction-manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8"
)
print(
    json.dumps(
        {
            "runtime_ts_files": len(manifest["C06_runtime"]),
            "has_frame_or_prompt_runtime": False,
        }
    )
)
