"""Single backend entry point, used by the owned application launcher."""

from __future__ import annotations

import argparse
import os
import sys
import threading

import uvicorn

from learning_app.app import create_app
from learning_app.config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="学习应用业务后台")
    parser.add_argument("command", nargs="?", choices=["serve"], default="serve")
    parser.add_argument("--mode", choices=["dev", "start", "test"])
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--instance-id")
    arguments = parser.parse_args(argv)
    overrides = {
        "mode": arguments.mode,
        "host": arguments.host,
        "backend_port": arguments.port,
        "instance_id": arguments.instance_id,
    }
    config = load_config({key: value for key, value in overrides.items() if value is not None})
    app = create_app(config)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.host,
            port=config.backend_port,
            log_level="info",
            access_log=False,
        )
    )
    if os.environ.get("LEARNING_APP_OWNED_STDIN") == "1":

        def observe_parent() -> None:
            # An owned launcher closes this pipe if it exits. Normal shutdown lets
            # the ASGI lifespan persist progress before the job's final cleanup.
            for line in sys.stdin:
                if line.strip() == "shutdown":
                    break
            server.should_exit = True

        threading.Thread(target=observe_parent, name="learning-parent-control", daemon=True).start()
    server.run()
    return 0 if server.started else 1


if __name__ == "__main__":
    raise SystemExit(main())
