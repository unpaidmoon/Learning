"""Small actionable schema feedback without echoing source data or input JSON."""

import re


def validation_details(error):
    issues = []
    for issue in error.errors(include_input=False, include_url=False)[:6]:
        path = [
            part
            if type(part) is int and 0 <= part <= 2000
            else part
            if isinstance(part, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", part)
            else "field"
            for part in issue.get("loc", ())[:12]
        ]
        value = {"path": path, "type": issue["type"]}
        context = issue.get("ctx", {})
        pattern = context.get("pattern")
        if isinstance(pattern, str) and len(pattern) <= 128 and pattern.isascii():
            value["pattern"] = pattern
        for key in ("min_length", "max_length", "ge", "gt", "le", "lt"):
            if type(context.get(key)) is int:
                value[key] = context[key]
        if issue["type"] == "json_invalid":
            position = re.search(r"at line (\d+) column (\d+)", issue.get("msg", ""))
            if position:
                value.update(line=int(position[1]), column=int(position[2]))
        issues.append(value)
    return {
        "issues": issues,
        "hints": [
            "body is one JSON string containing blocks; escape JSON backslashes or use literal Unicode",
            "plain text blocks use kind=markdown; do not replace requested content with a test payload",
            "omit duration_seconds for a static visualization; an active animation needs 1..120 seconds",
        ],
    }


def source_details(index, block, allowed_sources):
    """Only current run IDs, never source text or ungranted ancestor IDs."""
    if not block.markdown.strip():
        return {
            "issues": [
                {"path": ["blocks", index, "markdown"], "type": "string_too_short", "min_length": 1}
            ],
            "hints": ["every block needs its actual nonblank markdown content"],
        }
    allowed = sorted(allowed_sources)
    return {
        "issues": [
            {
                "path": ["blocks", index, "source_snapshot_ids"],
                "type": "current_run_source_required",
            }
        ],
        "allowed_source_snapshot_ids": allowed[:16],
        "list_complete": len(allowed) <= 16,
        "source_count": len(allowed),
        "hints": [
            "cite only snapshot_id from current run inputs or the top-level source returned by read_source_handle",
            "source.reference.source_snapshot_ids is an ancestry record, not permission to cite those original sources as this run's evidence",
            "preserve ai_generated and mixed identity; when this list is partial, use all required IDs from the original current run input contract",
        ],
    }
