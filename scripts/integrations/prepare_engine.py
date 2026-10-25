"""Install only the reviewed immutable wheel, without modifying dependency locks."""

import hashlib
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]


def main():
    for name in ["bilibili-engine.lock.json", "av.lock.json", "qrcode.lock.json"]:
        prepare(json.loads((ROOT / "resources/integrations" / name).read_text()))


def prepare(lock):
    lock.setdefault("path", ".local/platform-engine/" + lock["filename"])
    target = ROOT / lock["path"]
    if (
        target.exists()
        and hashlib.sha256(target.read_bytes()).hexdigest() == lock["sha256"]
    ):
        print(f"Verified cached {lock['filename']}")
        return
    with httpx.Client(timeout=90, follow_redirects=False) as client:
        url = lock.get("url")
        if url is None:
            response = client.get(lock["source"])
            response.raise_for_status()
            item = next(
                row
                for row in response.json()["urls"]
                if row["filename"] == lock["filename"]
            )
            if item["digests"]["sha256"] != lock["sha256"]:
                raise RuntimeError("ENGINE_SOURCE_HASH_CHANGED")
            url = item["url"]
        if not url.startswith("https://files.pythonhosted.org/packages/"):
            raise RuntimeError("ENGINE_DOWNLOAD_SOURCE_DENIED")
        response = client.get(url)
        response.raise_for_status()
    if hashlib.sha256(response.content).hexdigest() != lock["sha256"]:
        raise RuntimeError("ENGINE_DOWNLOAD_HASH_MISMATCH")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(response.content)
    print(f"Verified {lock['filename']} {lock['sha256']}")


if __name__ == "__main__":
    main()
