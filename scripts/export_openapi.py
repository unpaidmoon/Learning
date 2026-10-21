"""Export without startup, database mutation, port, Host or credential access."""

import argparse
import json
from pathlib import Path

from learning_app.app import create_app
from learning_app.config import AppConfig

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, default=Path("contracts/openapi.json"))
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
schema = create_app(AppConfig(mode="test")).openapi()
content = json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
if args.check:
    if not args.output.is_file() or args.output.read_text("utf-8") != content:
        raise SystemExit("OPENAPI_DRIFT: run pnpm contracts:generate")
else:
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
