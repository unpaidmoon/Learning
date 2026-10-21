"""Resolve configuration once, independently of the caller's working directory."""

import os
import uuid
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_root: Path = PROJECT_ROOT
    host: Literal["127.0.0.1"] = "127.0.0.1"
    backend_port: int = Field(default=8765, ge=1024, le=65535)
    frontend_port: int = Field(default=5173, ge=1024, le=65535)
    data_dir: Path = PROJECT_ROOT / ".runtime/app/data"
    runtime_dir: Path = PROJECT_ROOT / ".runtime/app/run"
    static_dir: Path = PROJECT_ROOT / "frontend/dist"
    instance_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    mode: Literal["dev", "start", "test"] = "dev"
    harness_enabled: bool = False
    model: str = "deepseek-flash"
    thinking: str = "high"
    max_output_tokens: int = Field(default=16384, ge=256, le=131072)
    model_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    secret_file: Path = PROJECT_ROOT / ".local/secrets/deepseek.env"

    @field_validator("data_dir", "runtime_dir", "static_dir", "secret_file", mode="before")
    @classmethod
    def absolute_path(cls, value: str | Path) -> Path:
        path = Path(value)
        return (PROJECT_ROOT / path).resolve() if not path.is_absolute() else path.resolve()

    @property
    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.backend_port}", f"127.0.0.1:{self.frontend_port}"}

    @property
    def allowed_origins(self) -> set[str]:
        return {f"http://{host}" for host in self.allowed_hosts}


def load_config(cli_overrides: dict | None = None, *, environ: dict | None = None) -> AppConfig:
    file_values = dotenv_values(PROJECT_ROOT / ".env")
    environment = dict(os.environ if environ is None else environ)
    values = {**file_values, **environment}
    fields = {
        name: values[f"LEARNING_APP_{name.upper()}"]
        for name in AppConfig.model_fields
        if f"LEARNING_APP_{name.upper()}" in values
    }
    fields.update({key: value for key, value in (cli_overrides or {}).items() if value is not None})
    return AppConfig(**fields)
