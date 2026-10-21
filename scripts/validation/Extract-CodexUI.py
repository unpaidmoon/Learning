"""Read selected packaged UI resources as inert bytes; never execute the target.

REA establishes the artifact/entry inventory separately. CSS observations here
retain their own exact member digests and source offsets, not runtime defaults.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
from pathlib import Path


def extract(artifact: Path, evidence_ids: list[str]) -> dict:
    digest = hashlib.sha256()
    with artifact.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        stream.seek(0)
        words = struct.unpack("<4I", stream.read(16))
        if words[0] != 4 or not 0 < words[3] <= 64 * 1024 * 1024:
            raise ValueError("ASAR_HEADER_INVALID")
        header = json.loads(stream.read(words[3]))
        data_offset = 8 + words[1]
        entries = {}

        def walk(node: dict, prefix: str = "") -> None:
            for name, item in node.get("files", {}).items():
                path = prefix + name
                if "files" in item:
                    walk(item, path + "/")
                else:
                    entries[path] = item

        walk(header)

        def read(path: str) -> tuple[bytes, int]:
            item = entries[path]
            if item.get("unpacked") or "link" in item:
                raise ValueError("ONLY_PACKED_RESOURCES_SUPPORTED")
            offset, size = data_offset + int(item["offset"]), int(item["size"])
            if size > 32 * 1024 * 1024:
                raise ValueError("SELECTED_RESOURCE_TOO_LARGE")
            stream.seek(offset)
            content = stream.read(size)
            if len(content) != size:
                raise ValueError("ASAR_RESOURCE_TRUNCATED")
            return content, offset

        package_bytes, _ = read("package.json")
        package = json.loads(package_bytes)
        css_paths = sorted(
            path for path in entries
            if re.fullmatch(r"webview/assets/app-(initial|primary|shared)-[^/]+\.css", path)
        )
        styles = []
        names = re.compile(
            r"^--(?:font-(?:sans|sans-default|content|mono)|spacing|radius-(?:sm|md|lg|xl|2xl|3xl|4xl)"
            r"|text-(?:xs|sm|base|lg)|composer-(?:radius|gutter|compact-gutter|background-color)"
            r"|border-width-hairline|app-color-(?:text-[\w-]+|background-[\w-]+|border[\w-]*))$"
        )
        for path in css_paths:
            raw, offset = read(path)
            text = raw.decode("utf-8")
            observations = []
            seen = set()
            for match in re.finditer(r"(--[\w-]+)\s*:\s*([^;{}]+)", text):
                name, value = match.group(1), match.group(2).strip()
                if not names.match(name) or (name, value) in seen:
                    continue
                seen.add((name, value))
                observations.append({
                    "name": name, "literal": value,
                    "source_character_offset": match.start(),
                })
            styles.append({
                "path": path, "sha256": hashlib.sha256(raw).hexdigest(),
                "archive_byte_offset": offset, "size_bytes": len(raw),
                "declarations": observations,
            })
        seeds = []
        for path in sorted(entries):
            if not re.fullmatch(r"webview/assets/app-initial-[^/]+\.js", path):
                continue
            raw, offset = read(path)
            text = raw.decode("utf-8")
            for match in re.finditer(r"[\w$]+=\{dark:\{accent:`#[\da-fA-F]+`,contrast:", text):
                region = text[match.start():match.start() + 1400]
                if "},light:{" not in region:
                    continue
                dark, light = region.split("},light:{", 1)
                variants = {}
                for variant, source in (("dark", dark), ("light", light)):
                    values = {}
                    for key in ("accent", "ink", "surface"):
                        found = re.search(key + r":`(#[\da-fA-F]+)`", source)
                        if found:
                            values[key] = found.group(1)
                    found = re.search(r"contrast:(\d+)", source)
                    if found:
                        values["contrast"] = int(found.group(1))
                    variants[variant] = values
                seeds.append({
                    "path": path, "sha256": hashlib.sha256(raw).hexdigest(),
                    "archive_byte_offset": offset,
                    "source_character_offset": match.start(),
                    "variants": variants,
                    "classification": "static desktop seed candidate; not observed active theme",
                })
        return {
            "schema_version": 1,
            "artifact": str(artifact), "canonical_artifact": str(artifact.resolve()),
            "sha256": digest.hexdigest(),
            "package": {"name": package.get("name"), "version": package.get("version")},
            "rea_evidence_ids": evidence_ids,
            "scope": "packaged static CSS declarations and desktop theme seed candidates",
            "styles": styles, "desktop_seed_candidates": seeds,
            "limitations": [
                "CSS extraction is a separate read-only operation, not REA semantic JavaScript analysis.",
                "Variable declarations are not resolved computed styles; selector/variant precedence is not inferred.",
                "Static seeds do not establish the user's active theme, OS fonts or runtime behavior.",
                "REA recorded eight native companion integrity contradictions; those resources were not executed.",
                "No user chat, video, screen, cookie or credential was read by this extractor.",
            ],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rea-evidence", action="append", default=[])
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; choose a new evidence path")
    result = extract(args.artifact, args.rea_evidence)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output), "sha256": result["sha256"],
        "styles": len(result["styles"]), "desktop_seed_candidates": len(result["desktop_seed_candidates"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
