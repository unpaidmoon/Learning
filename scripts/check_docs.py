"""Check repository Markdown navigation without opening private linked assets."""

from pathlib import Path
import re
import subprocess
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
# Upstream license notices are preserved byte-for-byte. Their relative links
# describe the upstream repository, not this deliberately limited runtime copy.
UPSTREAM_NOTICES = "harness/upstream/THIRD_PARTY_NOTICES.md"


def main() -> int:
    names = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
    ).decode("utf-8").split("\0")
    errors = []
    checked = 0
    for name in sorted(set(names)):
        path = ROOT / name
        if not name.endswith(".md") or name == UPSTREAM_NOTICES or not path.is_file():
            continue
        checked += 1
        content = path.read_text(encoding="utf-8-sig")
        content = re.sub(r"(?ms)^```.*?^```[^\n]*", "", content)
        for match in re.finditer(r"\]\(([^\n)]+)\)", content):
            target = re.sub(r'\s+"[^"]*"$', "", match[1].strip()).strip("<>")
            if target.startswith(("https:", "http:", "mailto:", "codex:", "#")):
                continue
            target = unquote(target.split("#", 1)[0])
            if not target:
                continue
            if re.match(r"^/?[A-Za-z]:[/\\]", target) or target.startswith("/"):
                errors.append(f"{name}: use a relative repository link: {target}")
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.is_relative_to(ROOT) or not resolved.exists():
                errors.append(f"{name}: unresolved repository link: {target}")
    for error in errors:
        print(error)
    print(f"Markdown navigation: {checked} files, {len(errors)} errors; upstream notices excluded")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
