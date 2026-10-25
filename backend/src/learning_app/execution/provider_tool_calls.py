"""Passive Messages SSE tool-call attribution; no dispatch or business authority."""

import codecs
import hashlib
import json


class ProviderToolCalls:
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.pending, self.data = "", []
        self.open, self.calls = {}, []

    def feed(self, chunk):
        self.pending += self.decoder.decode(chunk)
        added = []
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            line = line.removesuffix("\r")
            if line:
                if line.startswith("data:"):
                    self.data.append(line[5:].lstrip(" "))
                continue
            if not self.data:
                continue
            event = json.loads("\n".join(self.data))
            self.data = []
            kind, index = event.get("type"), event.get("index")
            if (
                kind == "content_block_start"
                and event.get("content_block", {}).get("type") == "tool_use"
            ):
                block = event["content_block"]
                if index in self.open:
                    raise ValueError("PROVIDER_TOOL_INDEX_REUSED")
                self.open[index] = {
                    "call_id": block["id"],
                    "name": block["name"],
                    "initial": block.get("input", {}),
                    "parts": [],
                }
            elif (
                kind == "content_block_delta"
                and index in self.open
                and event.get("delta", {}).get("type") == "input_json_delta"
            ):
                self.open[index]["parts"].append(event["delta"]["partial_json"])
            elif kind == "content_block_stop" and index in self.open:
                block = self.open.pop(index)
                args = json.loads("".join(block["parts"])) if block["parts"] else block["initial"]
                digest = hashlib.sha256(
                    json.dumps(
                        args, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                    ).encode()
                ).hexdigest()
                call = {
                    "ordinal": len(self.calls),
                    "call_id": block["call_id"],
                    "name": block["name"],
                    "args_sha256": digest,
                    "input": args,
                }
                if any(c["call_id"] == call["call_id"] for c in self.calls):
                    raise ValueError("PROVIDER_DUPLICATE_CALL_ID_IN_ATTEMPT")
                self.calls.append(call)
                added.append(call)
        return added
