"""Replay cursors follow complete frames and successful event processing."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from agent_bus.core.sse import MAX_SSE_FRAME_BYTES, iter_sse_frames
from agent_bus.worker import client as clients


async def lines(*values):
    for value in values:
        yield value


async def test_sse_parser_complete_multiline_comments_and_truncation():
    frames = [frame async for frame in iter_sse_frames(lines(
        ': heartbeat', 'event: message', 'id: cursor-one', 'data: {"a":', 'data: 1}', '',
        'event: checkpoint', 'id: cursor-two', 'data: {"cursor":"cursor-two"}', '',
        'id: never-commit', 'data: incomplete',
    ))]
    assert frames == [
        {'event': 'message', 'id': 'cursor-one', 'data': '{"a":\n1}'},
        {'event': 'checkpoint', 'id': 'cursor-two', 'data': '{"cursor":"cursor-two"}'},
    ]


async def test_sse_parser_preserves_data_spaces_and_ignores_null_id():
    frames = [frame async for frame in iter_sse_frames(lines(
        'id: unsafe\x00cursor', 'data:  leading and trailing ', '', ':comment', '',
    ))]
    assert frames == [{'event': 'message', 'id': None, 'data': ' leading and trailing '}]


async def test_sse_parser_rejects_oversized_frame():
    with pytest.raises(ValueError, match='size limit'):
        _ = [frame async for frame in iter_sse_frames(lines('data: ' + 'x' * MAX_SSE_FRAME_BYTES))]


async def test_sse_parser_yields_to_cancellation_during_comment_flood():
    async def comments():
        while True:
            yield ':keepalive'
            yield ''
    async def consume():
        async for _ in iter_sse_frames(comments()):
            pytest.fail('Comments must not become events')
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await consume()


def install(monkeypatch, handler):
    monkeypatch.setattr(clients, 'async_bus_client', lambda *args, **kwargs:
                        httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs))


async def test_failed_callback_replays_previous_cursor(monkeypatch):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, text='id: next\nevent: message\ndata: {"message_id":"m1"}\n\n')
    install(monkeypatch, handle)
    callbacks = []
    async def receive(payload):
        callbacks.append(payload)
        assert client.last_event_id == 'previous'
        if len(callbacks) == 1:
            raise RuntimeError('Consumer failed')
        client.stop()
    client = clients.BusEventClient('alice', cursor='previous', on_event=receive,
                                    reconnect_initial_delay=0.001)
    await asyncio.wait_for(client.start(), timeout=1)
    assert len(callbacks) == 2
    assert [request.headers['Last-Event-ID'] for request in requests] == ['previous', 'previous']
    assert client.last_event_id == 'next'
    assert requests[0].url.path == '/inbox/alice/events'


@pytest.mark.parametrize('handshake', [False, True])
async def test_expired_cursor_notifies_recovery_before_adopting_fresh(monkeypatch, handshake):
    reset = {'error': 'cursor_expired', 'cursor': 'fresh', 'recovery': 'read_inbox'}
    import json
    if handshake:
        response = httpx.Response(410, json=reset)
    else:
        response = httpx.Response(200, text=f'event: reset\ndata: {json.dumps(reset)}\n\n')
    install(monkeypatch, lambda _: response)
    observed = []
    async def recover(payload):
        assert client.last_event_id == 'expired'
        observed.append(payload)
    client = clients.BusEventClient('alice', cursor='expired', on_event=recover)
    client._running = True
    await client._consume_sse()
    assert client.last_event_id == 'fresh'
    assert observed == [{**reset, 'event': 'reset'}]


async def test_checkpoint_updates_cursor_without_message_callback(monkeypatch):
    install(monkeypatch, lambda _: httpx.Response(200, text=(
        'event: checkpoint\nid: tail\ndata: {"cursor":"tail"}\n\n'
        'id: incomplete\ndata: {"message_id":"lost"}'
    )))
    async def receive(_):
        pytest.fail('Checkpoint or incomplete frame must not reach message callback')
    client = clients.BusEventClient('alice', on_event=receive)
    client._running = True
    await client._consume_sse()
    assert client.last_event_id == 'tail'


@pytest.mark.parametrize('status', [401, 403, 422])
async def test_auth_and_invalid_cursor_fail_without_reconnect_or_downgrade(monkeypatch, status):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(status, json={'error': 'rejected'})
    install(monkeypatch, handle)
    client = clients.BusEventClient('alice', cursor='supplied')
    with pytest.raises(httpx.HTTPStatusError):
        await client.start()
    assert len(requests) == 1
    assert client.last_event_id == 'supplied'


async def test_clean_eof_backs_off_instead_of_busy_loop(monkeypatch):
    times = []
    def handle(_):
        times.append(time.monotonic())
        if len(times) == 3:
            client.stop()
        return httpx.Response(200, text='')
    install(monkeypatch, handle)
    client = clients.BusEventClient('alice', reconnect_initial_delay=0.01, reconnect_max_delay=0.02)
    await asyncio.wait_for(client.start(), timeout=1)
    assert times[1] - times[0] >= 0.009
    assert times[2] - times[1] >= 0.019


class SilentStream(httpx.AsyncByteStream):
    def __init__(self):
        self.started = asyncio.Event()
        self.closed = False
    async def __aiter__(self):
        self.started.set()
        await asyncio.Event().wait()
        yield b''
    async def aclose(self):
        self.closed = True


async def test_stop_interrupts_silent_stream_and_closes_response(monkeypatch):
    stream = SilentStream()
    install(monkeypatch, lambda _: httpx.Response(200, stream=stream))
    client = clients.BusEventClient('alice')
    running = asyncio.create_task(client.start())
    await asyncio.wait_for(stream.started.wait(), timeout=1)
    client.stop()
    await asyncio.wait_for(running, timeout=0.2)
    assert stream.closed


async def test_stop_interrupts_reconnect_backoff(monkeypatch):
    reached = asyncio.Event()
    def handle(_):
        reached.set()
        return httpx.Response(200, text='')
    install(monkeypatch, handle)
    client = clients.BusEventClient('alice', reconnect_initial_delay=30, reconnect_max_delay=30)
    running = asyncio.create_task(client.start())
    await reached.wait()
    await asyncio.sleep(0)
    client.stop()
    await asyncio.wait_for(running, timeout=0.2)


async def test_iterator_stop_interrupts_silent_stream(monkeypatch):
    stream = SilentStream()
    install(monkeypatch, lambda _: httpx.Response(200, stream=stream))
    stop = asyncio.Event()
    iterator = clients.iter_bus_events('alice', stop=stop)
    waiting = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(stream.started.wait(), timeout=1)
    stop.set()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiting, timeout=0.2)
    assert stream.closed


async def test_iterator_close_cancels_inflight_callback(monkeypatch):
    stream = SilentStream()
    class OneThenSilent(SilentStream):
        async def __aiter__(self):
            yield b'id: one\ndata: {"message_id":"m1"}\n\n'
            async for value in super().__aiter__():
                yield value
    stream = OneThenSilent()
    install(monkeypatch, lambda _: httpx.Response(200, stream=stream))
    iterator = clients.iter_bus_events('alice')
    result = await asyncio.wait_for(anext(iterator), timeout=1)
    assert result['message_id'] == 'm1'
    await asyncio.wait_for(iterator.aclose(), timeout=0.2)
    assert stream.closed


def test_browser_reconnect_cursor_reset_and_auth_failure():
    """Execute the shipped room script with fetch/DOM stand-ins, including SSE frames."""
    import json
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to execute the browser JavaScript regression')
    source = (Path(__file__).parents[1] / 'src/agent_bus/web/room.html').read_text()
    script = source.split('<script>', 1)[1].split('</script>', 1)[0]
    harness = r'''
const vm = require('node:vm');
const elements = new Map();
const context = vm.createContext({
  console, AbortController, Response, TextDecoder, DOMException,
  setTimeout: fn => setTimeout(fn, 0), clearTimeout,
  clearInterval, Option: function(text, value) { this.text = text; this.value = value; },
  document: {getElementById(id) {
    if (!elements.has(id)) elements.set(id, {replaceChildren(){}, value:'', children:[]});
    return elements.get(id);
  }},
});
vm.runInContext(SOURCE, context);
vm.runInContext(`
(async () => {
  token = 'test-session-token'; principal = {agent_id:'operator'};
  const requests = [], received = [];
  let recoveries = 0;
  refresh = async () => { recoveries++; };
  renderEvent = data => received.push(JSON.parse(data));
  const controller = new AbortController(); streamController = controller;
  fetch = async (path, options) => {
    requests.push({path, headers:options.headers});
    if (requests.length === 1) return new Response(
      'event: checkpoint\\nid: tail\\ndata: {"cursor":"tail"}\\n\\n' +
      'event: message\\nid: delivered\\ndata: {"message_id":\\ndata: "m1"}\\n\\n' +
      'id: truncated\\ndata: {"message_id":"not-delivered"}', {status:200});
    if (requests.length === 2) return new Response(
      JSON.stringify({error:'cursor_expired',cursor:'fresh',recovery:'read_inbox'}), {status:410});
    return new Response('{}', {status:401});
  };
  await streamEvents(controller.signal);
  console.log(JSON.stringify({requests,received,recoveries,token,eventCursor}));
})().catch(error => { console.error(error); process.exitCode = 1; });
`, context);
'''.replace('SOURCE', json.dumps(script))
    result = subprocess.run([node], input=harness, text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['received'] == [{'message_id': 'm1'}]
    assert data['recoveries'] == 1
    assert len(data['requests']) == 3
    assert 'Last-Event-ID' not in data['requests'][0]['headers']
    assert data['requests'][1]['headers']['Last-Event-ID'] == 'delivered'
    assert data['requests'][2]['headers']['Last-Event-ID'] == 'fresh'
    assert all(request['headers']['Authorization'] == 'Bearer test-session-token' for request in data['requests'])
    assert data['token'] == '' and data['eventCursor'] is None
