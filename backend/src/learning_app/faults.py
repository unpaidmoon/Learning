"""Explicit deterministic fault seams, injected by isolated validation instances only."""

from collections.abc import Callable


class Faults:
    def __init__(self, hook: Callable[[str, dict], None] | None = None):
        self.hook = hook

    def hit(self, name: str, context: dict) -> None:
        if self.hook is not None:
            self.hook(name, context)
