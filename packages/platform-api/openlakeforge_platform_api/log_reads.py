"""Run one log archive read off the event loop, held to a deadline, and stopped when its client goes away.

Both log routes read the archive here, on one small pool, so at most `_READS`'s workers decode archive objects at once,
whichever route asked: that bound is what keeps the platform API inside its memory limit. A read that waits for a
worker longer than QUEUE_WAIT_SECONDS is answered 503 with Retry-After instead of waiting on, and the read's own
READ_DEADLINE_SECONDS start when a worker takes it, so time spent queued never shortens it. A request therefore takes
at most QUEUE_WAIT_SECONDS + READ_DEADLINE_SECONDS.

The routes are async so the waiting side can see a disconnect: a sync route runs in Starlette's thread pool and nothing
would notice that the browser gave up. When it does, the read is cancelled: a queued one never starts, and a running
one stops at its next check (between listing pages, objects and records), and starts none of its queued fetches.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request

# The whole read, as in run_failure: for a run's logs the run read and the archive read together. Each call has its own
# timeout, but a slow store reading three days would otherwise hold the request for their sum. The archive read also
# checks it itself between pages, objects and records, so a read abandoned here stops soon after instead of holding a
# worker.
READ_DEADLINE_SECONDS = 20.0
# Long enough to ride out one read finishing, short enough that a client retrying after Retry-After is not slower
# than waiting; with the deadline it stays under the e2e's 30 s client timeout (`API_READ_TIMEOUT`).
QUEUE_WAIT_SECONDS = 5.0
RETRY_AFTER_SECONDS = 5
# How often the waiting side asks whether the client is still there. A read finishing wakes it at once.
DISCONNECT_POLL_SECONDS = 0.25
# Each worker holds one read's fetched and decoding objects, so the pool's size bounds what the memory limit must hold.
# A read abandoned at the deadline keeps its worker until it next checks the deadline or a call's own timeout ends it.
_READS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="log-archive-read")
# Not a status any client sees: it is gone, and uvicorn's `send` drops whatever is sent to a closed connection. The
# code is the one nginx and Traefik log for a request the client closed.
CLIENT_CLOSED_REQUEST = 499
OVERLOADED: dict[int | str, dict[str, Any]] = {
    503: {
        "description": (
            f"Every log read worker stayed busy for {QUEUE_WAIT_SECONDS:g} s, so nothing was read: retry after the "
            "`Retry-After` seconds. Also, without `Retry-After`, when the API holds no object-store key for the "
            "archive."
        ),
        "headers": {"Retry-After": {"schema": {"type": "integer"}, "description": "Seconds to wait before retrying."}},
    }
}


class ReadCancelled(Exception):
    """The read was cancelled before a worker started it."""


@dataclass
class ReadSlot:
    """One read's deadline, set when a worker starts it, and its cancellation, which the read checks as it goes."""

    # What the read is waiting on, named in the 502 when the deadline passes; the read may change it as it moves on.
    waiting_on: str
    cancelled: threading.Event = field(default_factory=threading.Event)
    expires: float | None = None


async def bounded_read[T](request: Request, read: Callable[[ReadSlot], T], *, waiting_on: str) -> T:
    """`read(slot)` on a log read worker: 503 when none frees up in time, 502 past the deadline, stopped on disconnect.

    `read` passes `slot.expires` and `slot.cancelled.is_set` to the archive reader, which stops on either between
    pages, objects and records. Its own exceptions, HTTPException included, are raised here unchanged.
    """
    slot = ReadSlot(waiting_on)

    def start() -> T:
        if slot.cancelled.is_set():
            raise ReadCancelled
        slot.expires = time.monotonic() + READ_DEADLINE_SECONDS
        return read(slot)

    queued_until = time.monotonic() + QUEUE_WAIT_SECONDS
    answer = asyncio.wrap_future(_READS.submit(start))
    try:
        while not answer.done():
            if await request.is_disconnected():
                raise HTTPException(status_code=CLIENT_CLOSED_REQUEST, detail="the client closed the request")
            expires = slot.expires
            left = (queued_until if expires is None else expires) - time.monotonic()
            if left <= 0:
                if expires is None:
                    raise HTTPException(
                        status_code=503,
                        detail=f"every log read worker is busy; retry in {RETRY_AFTER_SECONDS} s",
                        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
                    )
                raise HTTPException(
                    status_code=502, detail=f"{slot.waiting_on} did not answer in {READ_DEADLINE_SECONDS:g} s"
                )
            await asyncio.wait({answer}, timeout=min(left, DISCONNECT_POLL_SECONDS))
        return answer.result()
    finally:
        if not answer.done():
            # The event before the cancel: a worker that takes the read between the two sees it and does not start.
            slot.cancelled.set()
            # Also cancels the queued job; once running, the future can no longer be cancelled and the event stops it.
            answer.cancel()
