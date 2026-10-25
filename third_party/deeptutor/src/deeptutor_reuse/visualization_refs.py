"""Original recursive remote-schema-reference detection excerpt."""

from typing import Any


def _find_remote_schema_ref(value: Any) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef"} and isinstance(item, str):
                if not item.startswith("#"):
                    return item
            found = _find_remote_schema_ref(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_remote_schema_ref(item)
            if found:
                return found
    return ""



find_remote_schema_ref = _find_remote_schema_ref
