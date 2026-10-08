"""Server-Sent Events plumbing for `POST /api/sessions/{sid}/messages` (docs/ARCHITECTURE.md section 6).

The service yields `(event_name, payload)` tuples; this module turns them into `event:`/`data:` frames and owns the
transport concerns the service must not care about:

* a pump task feeds an `asyncio.Queue`, so a `: ping` comment can be sent every 15 s while the service is busy awaiting
  (proxies and browsers drop idle connections; RAGAS alone can be silent for a minute);
* when the client goes away the pump is cancelled and the service generator is closed, which is how a run is cancelled
  (the service treats `GeneratorExit` / `CancelledError` as "stop the agent");
* an unexpected exception inside the service becomes one `error` event followed by `done`, never a half-open stream.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, AsyncIterator, Optional

import anyio
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from reportlens.models import ServiceError

log = logging.getLogger("reportlens.sse")

MEDIA_TYPE = "text/event-stream"
PING_INTERVAL_S = 15.0           # module-level on purpose: tests shrink it
CLEANUP_TIMEOUT_S = 5.0          # how long we wait for a cancelled service generator to unwind
QUEUE_SIZE = 1024                # back-pressure: a stalled client eventually pauses the producer instead of eating memory
SSE_HEADERS = {
    "Cache-Control": "no-cache",       # never cache or transform the stream
    "X-Accel-Buffering": "no",         # nginx: do not buffer
    "Connection": "keep-alive",
}
PING_FRAME = ": ping\n\n"
INTERNAL_ERROR_MESSAGE = "Something went wrong while answering. Please try again."

_EVENT_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_END = object()                  # sentinel: the producer is finished

Event = tuple[str, Any]


def _json_default(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


def format_event(name: str, payload: Any) -> str:
    """One SSE frame. `ensure_ascii` keeps every frame pure ASCII: no U+2028 surprises, no lone surrogates that would
    break UTF-8 encoding mid-stream, and the payload is a single `data:` line because JSON escapes newlines."""
    if not _EVENT_NAME.fullmatch(name):
        raise ValueError(f"invalid SSE event name: {name!r}")
    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    return f"event: {name}\ndata: {json.dumps(data, default=_json_default, separators=(',', ':'), allow_nan=False)}\n\n"


def _error_event(code: str, message: str) -> Event:
    return "error", {"code": code, "message": message, "message_id": None}


async def _pump(source: AsyncIterator[Event], first: Optional[Event], queue: "asyncio.Queue[Any]") -> None:
    """Moves the service's events into the queue. Never raises (except cancellation): failures become events."""
    try:
        if first is not None:
            await queue.put(first)
        async for item in source:
            await queue.put(item)
    except ServiceError as exc:
        await queue.put(_error_event(exc.code, exc.message))
    except Exception:
        log.exception("Unexpected error while streaming an answer")
        await queue.put(_error_event("internal_error", INTERNAL_ERROR_MESSAGE))
    await queue.put(_END)


async def _shutdown(pump: "asyncio.Task[None]", source: AsyncIterator[Event]) -> None:
    """Cancel the producer, let the service generator unwind, then close it. Shielded: this runs while the request is
    being cancelled (client disconnect), and anyio would otherwise re-cancel every await in here."""
    with anyio.CancelScope(shield=True):
        if not pump.done():
            pump.cancel()
        await asyncio.wait({pump}, timeout=CLEANUP_TIMEOUT_S)
        if pump.done() and not pump.cancelled():
            pump.exception()                      # mark retrieved; _pump logs its own failures
        aclose = getattr(source, "aclose", None)
        if aclose is not None:
            try:
                await asyncio.wait_for(aclose(), timeout=CLEANUP_TIMEOUT_S)
            except Exception:
                log.warning("Closing the answer generator failed", exc_info=True)


async def stream_events(
    source: AsyncIterator[Event], *, first: Optional[Event] = None, ping_interval: Optional[float] = None
) -> AsyncIterator[str]:
    """Async generator of SSE frames. `first` is an event the caller already pulled from `source` (peek-first)."""
    interval = PING_INTERVAL_S if ping_interval is None else ping_interval
    queue: "asyncio.Queue[Any]" = asyncio.Queue(QUEUE_SIZE)
    pump = asyncio.create_task(_pump(source, first, queue), name="sse-pump")
    saw_done = False
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                yield PING_FRAME
                continue
            if item is _END:
                break
            name, payload = item
            try:
                frame = format_event(name, payload)
            except (TypeError, ValueError):
                log.exception("Dropping an unserialisable %r event", name)
                frame = format_event(*_error_event("internal_error", INTERNAL_ERROR_MESSAGE))
                yield frame
                break
            saw_done = saw_done or name == "done"
            yield frame
        if not saw_done:                          # the client's "stream finished" marker, always last
            yield format_event("done", {})
    finally:
        await _shutdown(pump, source)


class EventStreamResponse(StreamingResponse):
    """StreamingResponse tuned for long, mostly silent streams.

    * Starlette (ASGI spec >= 2.4) notices a vanished client only when a `send` fails, i.e. at the next token or ping,
      which can be 15 s away. We also watch `http.disconnect`, so a closed tab cancels the agent run immediately.
    * The service generator is always closed on exit, even if the client disappears before the body starts or while a
      chunk is being sent (the body generator is then parked at a `yield`, where cancellation cannot reach it)."""

    def __init__(self, source: AsyncIterator[Event], *, first: Optional[Event] = None, status_code: int = 200):
        self._source = source
        self._body = stream_events(source, first=first)
        super().__init__(self._body, status_code=status_code, media_type=MEDIA_TYPE, headers=SSE_HEADERS)

    @staticmethod
    async def _watch_disconnect(receive: Any, scope: anyio.CancelScope) -> None:
        while True:
            if (await receive())["type"] == "http.disconnect":
                scope.cancel()
                return

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(self._watch_disconnect, receive, group.cancel_scope)
                try:
                    await self.stream_response(send)
                except OSError:
                    log.info("Client closed the event stream")   # ASGI 2.4: a failed send means the client is gone
                finally:
                    group.cancel_scope.cancel()                  # stops the watcher; unwinds a client-initiated cancel
        finally:
            with anyio.CancelScope(shield=True):
                await self._body.aclose()                # runs stream_events' cleanup if it was parked at a yield
                aclose = getattr(self._source, "aclose", None)
                if aclose is not None:                   # covers "response never started": the body never ran
                    try:
                        await asyncio.wait_for(aclose(), timeout=CLEANUP_TIMEOUT_S)
                    except Exception:
                        log.warning("Closing the answer generator failed", exc_info=True)
