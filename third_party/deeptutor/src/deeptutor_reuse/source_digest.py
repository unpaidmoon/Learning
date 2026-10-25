"""DeepTutor source_digest excerpt; provenance in distribution UPSTREAM.md."""

import hashlib
from pathlib import Path


def source_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
