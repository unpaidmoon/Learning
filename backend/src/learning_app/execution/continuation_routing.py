"""Native continuation attribution only; never a business capability."""


def resolve_tool_result_continuation(request, bound, session_header, session_runs, marker_resolve):
    if not session_header:
        raise ValueError("ROUTE_SESSION_HEADER_REQUIRED")
    owner = session_runs.get(session_header)
    if owner is None or owner not in bound:
        raise ValueError("ROUTE_SESSION_NOT_BOUND")
    prefix = request.get("messages")
    if not isinstance(prefix, list) or any(not isinstance(m, dict) for m in prefix):
        raise ValueError("INVALID_MESSAGES")
    groups = 0
    while prefix:
        indexes = [i for i, m in enumerate(prefix) if m.get("role") == "user"]
        if not indexes:
            raise ValueError("ROUTE_NO_MARKED_ANCHOR")
        index = indexes[-1]
        if any(m.get("role") != "system" for m in prefix[index + 1 :]):
            raise ValueError("ROUTE_NON_USER_REQUEST_TAIL")
        latest = prefix[index]
        try:
            # 仅查看这一条user，不能借此跳过一个新的无marker用户。
            anchored = marker_resolve({"messages": [latest]})
        except ValueError as error:
            if str(error) != "GENERATION_MARKER_REQUIRED":
                raise
        else:
            if groups == 0:
                raise ValueError("ROUTE_TOOL_RESULT_REQUIRED")
            if anchored != owner:
                raise ValueError("ROUTE_MARKER_OWNER_CONFLICT")
            return owner
        content = latest.get("content")
        if not isinstance(content, list):
            raise ValueError("ROUTE_TOOL_RESULT_REQUIRED")
        results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        if not results:
            raise ValueError("ROUTE_TOOL_RESULT_REQUIRED")
        if index < 1 or prefix[index - 1].get("role") != "assistant":
            raise ValueError("ROUTE_ADJACENT_TOOL_CALL_REQUIRED")
        calls = prefix[index - 1].get("content")
        if not isinstance(calls, list):
            raise ValueError("ROUTE_ADJACENT_TOOL_CALL_REQUIRED")
        call_ids = [
            b.get("id") for b in calls if isinstance(b, dict) and b.get("type") == "tool_use"
        ]
        result_ids = [b.get("tool_use_id") for b in results]
        if (
            not call_ids
            or any(not isinstance(x, str) or not x for x in call_ids + result_ids)
            or len(set(call_ids)) != len(call_ids)
            or len(set(result_ids)) != len(result_ids)
            or set(call_ids) != set(result_ids)
        ):
            raise ValueError("ROUTE_TOOL_CALL_ID_MISMATCH")
        groups += 1
        prefix = prefix[: index - 1]
    raise ValueError("ROUTE_NO_MARKED_ANCHOR")
