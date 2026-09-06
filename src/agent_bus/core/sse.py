"""Bounded SSE frame parsing shared by bus clients and the MCP adapter."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

MAX_SSE_FRAME_BYTES = 256 * 1024


async def iter_sse_frames(lines: AsyncIterator[str]) -> AsyncIterator[dict]:
    """Dispatch complete data frames only; EOF never commits a truncated frame."""
    event = "message"
    frame_id = None
    data: list[str] = []
    size = 0
    count = 0
    async for line in lines:
        count += 1
        if count % 64 == 0:
            await asyncio.sleep(0)
        # HTTPX aiter_lines removes terminators. Also accept raw CRLF lines.
        line = line.rstrip("\r\n")
        size += len(line.encode("utf-8")) + 1
        if size > MAX_SSE_FRAME_BYTES:
            raise ValueError("SSE frame exceeds size limit")
        if not line:
            if data:
                yield {"event": event, "data": "\n".join(data), "id": frame_id}
            event, frame_id, data, size = "message", None, [], 0
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data.append(value)
        elif field == "event":
            event = value or "message"
        elif field == "id" and "\x00" not in value:
            frame_id = value
