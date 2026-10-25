"""One projection for chat and task raw usage. Missing is never zero."""

from collections.abc import Mapping


def usage_projection(
    raw: dict | None, fields: Mapping[str, bool] | None = None
) -> tuple[dict, dict]:
    presence = dict(fields) if fields is not None else dict((raw or {}).get("field_presence", {}))
    if not presence:
        presence = {
            key: key in (raw or {})
            for key in (
                "input_tokens",
                "output_tokens",
                "prompt_tokens",
                "completion_tokens",
                "cached_input_tokens",
                "cache_read_input_tokens",
                "reasoning_tokens",
            )
        }

    def known(*keys: str) -> int | None:
        if raw and "normalized" in raw:
            if not raw.get("settled", False):
                return None
            for key in keys:
                value = raw["normalized"].get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
            return None
        for key in keys:
            value = raw.get(key) if raw else None
            if presence.get(key, False) and isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    return presence, {
        "input_tokens": known("input_tokens", "prompt_tokens"),
        "output_tokens": known("output_tokens", "completion_tokens"),
        "cached_input_tokens": known("cached_input_tokens", "cache_read_input_tokens"),
        "reasoning_tokens": known("reasoning_tokens"),
    }


def usage_is_settled(raw: dict | None) -> bool:
    # Flat legacy protocol fixtures represent final observations.
    return (bool(raw["settled"]) if "settled" in raw else True) if raw is not None else False
