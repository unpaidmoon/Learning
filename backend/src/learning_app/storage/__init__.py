"""Business persistence. Network DTOs must not expose private storage paths."""

from .repository import Repository, StorageError

__all__ = ["Repository", "StorageError"]
