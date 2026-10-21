"""Explicit session choices supported by the pinned official-provider route."""

import re

from .types import ExecutionError

# The pinned Host catalog supports off/low/high/max. The application uses
# "none" for the same explicit disabled mode; unsupported effort is rejected.
THINKING_LEVELS = frozenset({"none", "low", "high", "max"})


def native_thinking(thinking: str) -> str:
    return "off" if thinking == "none" else thinking


def supports_images(model: str) -> bool:
    # Official model metadata: flash accepts text/image; v4-pro accepts text.
    # Unknown explicitly configured models do not gain an inferred image ability.
    return model == "deepseek-flash"


def model_choices(configured_model: str) -> tuple[str, ...]:
    if not isinstance(configured_model, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", configured_model
    ):
        raise ExecutionError("MODEL_SELECTION_INVALID")
    return tuple(dict.fromkeys((configured_model, "deepseek-flash", "deepseek-v4-pro")))


def validate_selection(configured_model: str, model: str, thinking: str) -> None:
    if model not in model_choices(configured_model):
        raise ExecutionError("MODEL_SELECTION_UNAVAILABLE")
    if thinking not in THINKING_LEVELS:
        raise ExecutionError("THINKING_SELECTION_UNAVAILABLE")
