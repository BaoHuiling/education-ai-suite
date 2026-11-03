# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import asyncio
import contextvars
import threading
import time

import anyio
import pytest

from utils.threadpool_stream import iterate_in_thread

_VAR = contextvars.ContextVar("test_var", default=None)


def test_blocking_generator_does_not_stall_the_loop():
    def slow():
        time.sleep(0.5)
        yield "token"

    async def main():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        items = [item async for item in iterate_in_thread(slow())]
        beat.cancel()
        return items, ticks

    items, ticks = asyncio.run(main())
    assert items == ["token"]
    # Stepped on the loop, the sleep would have allowed no ticks at all.
    assert ticks >= 10


def test_early_exit_closes_generator_off_the_loop():
    loop_thread = threading.get_ident()
    closed_on = []

    def gen():
        try:
            yield 1
            yield 2
        finally:
            closed_on.append(threading.get_ident())

    async def main():
        stream = iterate_in_thread(gen())
        async for item in stream:
            break
        await stream.aclose()

    asyncio.run(main())
    assert len(closed_on) == 1 and closed_on[0] != loop_thread


def test_cancelled_stream_still_closes_generator():
    closed = threading.Event()

    def gen():
        try:
            yield 1
            time.sleep(0.2)
            yield 2
        finally:
            closed.set()

    async def main():
        async with anyio.create_task_group() as tg:
            async def consume():
                async for _ in iterate_in_thread(gen()):
                    pass

            tg.start_soon(consume)
            await anyio.sleep(0.05)
            tg.cancel_scope.cancel()

    anyio.run(main)
    assert closed.is_set()


def test_worker_sees_callers_context():
    def gen():
        yield _VAR.get()

    async def main():
        _VAR.set("summary")
        return [item async for item in iterate_in_thread(gen())]

    assert asyncio.run(main()) == ["summary"]


def test_generator_errors_propagate():
    def gen():
        yield 1
        raise ValueError("boom")

    async def main():
        return [item async for item in iterate_in_thread(gen())]

    with pytest.raises(ValueError, match="boom"):
        asyncio.run(main())
