"""Pinned internal Web Remote wire. Kept private behind ExecutionAdapter."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import websockets

from .host import HarnessHost
from .types import ExecutionError


class RemotePeer:
    def __init__(self, host: HarnessHost) -> None:
        self.host = host
        self.client = httpx.AsyncClient(base_url=host.base, timeout=15, trust_env=False)
        self.ws: Any = None
        self.reader: asyncio.Task | None = None
        self.streams: dict[str, asyncio.Queue] = {}
        self._closing = False

    async def connect(self) -> None:
        self.client.cookies.clear()
        response = await self.client.get(
            "/connect", params={"token": self.host._token, "interactions": "none"}
        )
        if response.status_code != 302:
            raise ExecutionError("HARNESS_PEER_CONNECT_FAILED")
        cookie = "; ".join(f"{key}={value}" for key, value in self.client.cookies.items())
        self.ws = await websockets.connect(
            f"ws://127.0.0.1:{self.host.port}/api/remote.mux",
            additional_headers={"Cookie": cookie},
            proxy=None,
            open_timeout=10,
            close_timeout=2,
        )
        self._closing = False
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                frame = json.loads(raw)
                queue = self.streams.get(frame.get("streamId"))
                if queue is not None:
                    await queue.put(frame)
        except (websockets.ConnectionClosed, OSError):
            pass
        finally:
            for queue in self.streams.values():
                await queue.put({"type": "disconnected"})

    async def rpc(self, method: str, request: dict) -> Any:
        return await self.rpc_args(method, {"request": request})

    async def rpc_args(self, method: str, arguments: dict) -> Any:
        """Named official controller arguments remain inside the adapter."""
        response = await self.client.post(
            "/api/" + method,
            json={
                "type": "client-request",
                "rpcId": str(uuid.uuid4()),
                "method": method,
                "payload": {"args": arguments},
            },
        )
        try:
            value = response.json()
        except ValueError:
            raise ExecutionError(f"HARNESS_REMOTE_HTTP_{response.status_code}") from None
        if response.status_code != 200 or not value.get("result", {}).get("ok"):
            raise ExecutionError("HARNESS_REMOTE_REJECTED")
        return value["result"]["value"]

    async def follow(self, session_id: str) -> tuple[str, dict]:
        stream = "app-" + uuid.uuid4().hex
        self.streams[stream] = queue = asyncio.Queue()
        await self.ws.send(
            json.dumps(
                {
                    "type": "open",
                    "streamId": stream,
                    "endpoint": "session/follow",
                    "payload": {
                        "args": {
                            "request": {
                                "address": {"kind": "session", "sessionId": session_id},
                                "assistantStream": True,
                                "maxMessages": 500,
                            }
                        }
                    },
                }
            )
        )
        opening = await asyncio.wait_for(queue.get(), timeout=10)
        value = opening.get("value", {})
        if opening.get("type") != "item" or value.get("type") != "snapshot":
            await self.unfollow(stream)
            raise ExecutionError("HARNESS_FOLLOW_NOT_FOUND")
        return stream, value

    async def unfollow(self, stream: str) -> None:
        if self.ws is not None:
            try:
                await self.ws.send(json.dumps({"type": "cancel", "streamId": stream}))
            except websockets.ConnectionClosed:
                pass
        self.streams.pop(stream, None)

    async def page(self, session_id: str, snapshot: dict) -> dict:
        return await self.rpc(
            "session/page",
            {
                "address": {"kind": "session", "sessionId": session_id},
                "throughSeq": snapshot["cursor"],
                "maxMessages": 500,
            },
        )

    async def disconnect(self) -> None:
        if self.ws is not None:
            await self.ws.close()
        if self.reader is not None:
            await self.reader
        self.ws, self.reader = None, None

    async def close(self) -> None:
        await self.disconnect()
        await self.client.aclose()
