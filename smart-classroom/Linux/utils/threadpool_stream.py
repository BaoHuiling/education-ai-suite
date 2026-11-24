# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Drive a blocking generator from an async streaming route.

Pipeline generators block between items -- waiting for the text_gen slot,
prefilling a long prompt, a whole non-streaming map call -- so stepping one on
the event loop stalls every other request, /health included, and the desktop
app then reports the backend degraded mid-summary.
"""

from typing import AsyncIterator, Iterable, TypeVar

import anyio

T = TypeVar("T")

# next() cannot raise StopIteration across a thread boundary.
_DONE = object()


async def iterate_in_thread(iterable: Iterable[T]) -> AsyncIterator[T]:
    """Yield ``iterable``'s items, advancing it in a worker thread.

    Unlike starlette's ``iterate_in_threadpool`` the generator is closed here,
    off the loop, when the stream ends early (client gone), so its cleanup --
    stopping generation, freeing the model slot -- runs promptly and does not
    block the loop either. The worker inherits the caller's context, so model
    calls are still attributed to the open stage.
    """
    iterator = iter(iterable)
    try:
        while True:
            item = await anyio.to_thread.run_sync(next, iterator, _DONE)
            if item is _DONE:
                return
            yield item
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            # A disconnect cancels the response scope; shield or this await is
            # cancelled too and the generator is left for the GC to close.
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(close)
