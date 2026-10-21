"""Open the pinned official browser sign-in flow without reading an API key.

The Host owns PKCE, its loopback callback and persistent credentials. This
entrypoint prints only the official authorize URL and safe account status.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend/src"))

from learning_app.execution.boundary import MessagesBoundary  # noqa: E402
from learning_app.execution.host import HarnessHost  # noqa: E402
from learning_app.execution.remote import RemotePeer  # noqa: E402


async def sign_in(data_dir: Path, timeout: float) -> int:
    boundary = MessagesBoundary(data_dir / "account-login-boundary", mode="fixture")
    boundary.start()
    host = HarnessHost(
        ROOT,
        data_dir / "execution/host",
        boundary.url,
        boundary.local_token,
        account_enabled=True,
    )
    peer: RemotePeer | None = None
    try:
        await host.start()
        peer = RemotePeer(host)
        await peer.connect()
        client = {"version": "0.1.0", "locale": "zh-CN", "timezoneOffsetSeconds": 28800}
        view = await peer.rpc_args("account/getState", {})
        if view.get("status") == "credential-stored":
            print(json.dumps({"status": "credential-stored"}), flush=True)
            return 0
        await peer.rpc_args(
            "account/startSignIn",
            {"client": client, "callbackOrigin": host.base, "loginSource": "web"},
        )
        printed = False
        async with asyncio.timeout(timeout):
            while True:
                view = await peer.rpc_args("account/getState", {})
                attempt = view.get("attempt") or {}
                url = attempt.get("authorizeUrl")
                if url and not printed:
                    print(
                        json.dumps({"status": "waiting-browser", "authorize_url": url}),
                        flush=True,
                    )
                    printed = True
                if view.get("status") == "credential-stored":
                    print(json.dumps({"status": "credential-stored"}), flush=True)
                    return 0
                if attempt.get("phase") in {"failed", "cancelled", "expired"}:
                    print(
                        json.dumps(
                            {
                                "status": attempt["phase"],
                                "code": attempt.get("errorCode"),
                            }
                        ),
                        flush=True,
                    )
                    return 1
                await asyncio.sleep(1)
    except TimeoutError:
        print(
            json.dumps({"status": "expired", "code": "ACCOUNT_LOGIN_TIMEOUT"}),
            flush=True,
        )
        return 1
    finally:
        if peer is not None:
            await peer.close()
        await host.stop()
        await asyncio.to_thread(boundary.stop)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="使用固定官方 Harness 在浏览器登录 DeepSeek"
    )
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".runtime/app/data")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 3600:
        parser.error("timeout must be between 1 and 3600 seconds")
    return asyncio.run(sign_in(args.data_dir.resolve(), args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
