"""Cancellable pipe I/O for the SDK's stdio parser and serializer.

The SDK's default AsyncFile wraps blocking reads/writes in worker threads. A
broken output pipe cannot cancel a thread still reading an open stdin. Asyncio
pipe transports let connection shutdown interrupt both directions instead.
"""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager

import anyio
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import INVALID_REQUEST, PARSE_ERROR, ErrorData, JSONRPCError
from pydantic import ValidationError

# Bound a single incoming JSON-RPC line, including arbitrary tool arguments.
MAX_REQUEST_BYTES = 4 * 1024 * 1024


class _Input:
    def __init__(self, reader: asyncio.StreamReader, closed: anyio.Event):
        self.reader = reader
        self.closed = closed

    async def __aiter__(self):
        while line := await self.reader.readline():
            yield line.decode("utf-8", errors="replace")
        self.closed.set()


class _Output:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer

    async def write(self, text: str):
        self.writer.write(text.encode("utf-8"))

    async def flush(self):
        await self.writer.drain()


class _ProtocolInput:
    """Turn SDK validation failures into RPC errors instead of silent timeouts."""
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    async def receive(self):
        while True:
            item = await self.reader.receive()
            if not isinstance(item, Exception):
                return item
            invalid_json = isinstance(item, ValidationError) and any(
                error["type"] == "json_invalid" for error in item.errors()
            )
            error = ErrorData(
                code=PARSE_ERROR if invalid_json else INVALID_REQUEST,
                message="Parse error" if invalid_json else "Invalid request",
            )
            await self.writer.send(SessionMessage(JSONRPCError(jsonrpc="2.0", id=None, error=error)))

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration from None

    async def aclose(self):
        await self.reader.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()


@asynccontextmanager
async def cancellable_stdio():
    """Own one process connection; restore descriptors and close private pipes.

    Like the SDK default, divert incidental stdout to stderr and stdin to the
    null device while serving. Private descriptors alone carry the protocol.
    RPC framing, parsing, serialization and dispatch remain owned by the SDK.
    """
    loop = asyncio.get_running_loop()
    saved_in = saved_out = None
    input_pipe = output_pipe = None
    read_transport = write_transport = None
    diverted = False
    try:
        sys.stdout.flush()
        saved_in = os.dup(sys.stdin.fileno())
        saved_out = os.dup(sys.stdout.fileno())
        blocking_in, blocking_out = os.get_blocking(saved_in), os.get_blocking(saved_out)
        input_pipe = os.fdopen(os.dup(saved_in), "rb", buffering=0)
        output_pipe = os.fdopen(os.dup(saved_out), "wb", buffering=0)
        reader = asyncio.StreamReader(limit=MAX_REQUEST_BYTES)
        read_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), input_pipe,
        )
        write_transport, protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, output_pipe,
        )
        writer = asyncio.StreamWriter(write_transport, protocol, None, loop)
        # No await between diverting descriptors and marking ownership.
        diverted = True
        with open(os.devnull, "rb") as null:
            os.dup2(null.fileno(), sys.stdin.fileno())
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        eof = anyio.Event()
        async with anyio.create_task_group() as lifetime:
            async def stop_on_eof():
                await eof.wait()
                # EOF ends this connection even if the client still holds a
                # full stdout pipe open. Do not wait forever for output drain.
                lifetime.cancel_scope.cancel()

            lifetime.start_soon(stop_on_eof)
            async with stdio_server(
                stdin=_Input(reader, eof), stdout=_Output(writer),  # type: ignore[arg-type]
            ) as (read_stream, write_stream):
                yield _ProtocolInput(read_stream, write_stream), write_stream
    finally:
        # Abort also frees a backpressured writer if the peer stopped reading.
        if write_transport is not None and not write_transport.is_closing():
            write_transport.abort()
        if read_transport is not None and not read_transport.is_closing():
            read_transport.close()
        if diverted:
            sys.stdout.flush()
            os.dup2(saved_in, sys.stdin.fileno())
            os.dup2(saved_out, sys.stdout.fileno())
        if input_pipe is not None:
            input_pipe.close()
        if output_pipe is not None:
            output_pipe.close()
        if saved_in is not None:
            if input_pipe is not None:
                os.set_blocking(saved_in, blocking_in)
            os.close(saved_in)
        if saved_out is not None:
            if output_pipe is not None:
                os.set_blocking(saved_out, blocking_out)
            os.close(saved_out)
