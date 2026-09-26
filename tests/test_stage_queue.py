from __future__ import annotations

import asyncio
import unittest

from paper_endnote.stage_queue import FairStageExecutor


class FairStageExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_worker_count_and_graceful_drain(self) -> None:
        stage = FairStageExecutor(workers=2, max_pending=2)
        two_started = asyncio.Event()
        release = asyncio.Event()
        active = 0
        peak = 0

        async def work() -> None:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
            await release.wait()
            active -= 1

        tasks = [asyncio.create_task(stage.run("a", work)) for _ in range(7)]
        await two_started.wait()
        self.assertEqual(stage.running_count, 2)
        self.assertEqual(stage.pending_count, 2)
        closing = asyncio.create_task(stage.close(drain=True))
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        release.set()
        await asyncio.gather(*tasks)
        await closing
        self.assertEqual(peak, 2)

    async def test_round_robin_preserves_each_batch_fifo(self) -> None:
        stage = FairStageExecutor(workers=1, max_pending=4)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        order: list[str] = []

        async def work(label: str) -> str:
            order.append(label)
            if label == "a1":
                first_started.set()
                await release_first.wait()
            return label

        tasks = [asyncio.create_task(stage.run("a", lambda: work("a1")))]
        await first_started.wait()
        tasks.extend(
            [
                asyncio.create_task(stage.run("a", lambda: work("a2"))),
                asyncio.create_task(stage.run("a", lambda: work("a3"))),
                asyncio.create_task(stage.run("b", lambda: work("b1"))),
            ]
        )
        await asyncio.sleep(0)
        release_first.set()
        self.assertEqual(await asyncio.gather(*tasks), ["a1", "a2", "a3", "b1"])
        self.assertEqual(order, ["a1", "a2", "b1", "a3"])
        await stage.close()

    async def test_bounded_queue_and_queued_cancellation(self) -> None:
        stage = FairStageExecutor(workers=1, max_pending=1)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        started: list[str] = []

        async def first() -> None:
            started.append("first")
            first_started.set()
            await release_first.wait()

        async def other(label: str) -> None:
            started.append(label)

        running = asyncio.create_task(stage.run("a", first))
        await first_started.wait()
        queued = asyncio.create_task(stage.run("a", lambda: other("queued")))
        producer_waiting = asyncio.create_task(stage.run("b", lambda: other("later")))
        await asyncio.sleep(0)
        self.assertEqual(stage.pending_count, 1)
        self.assertEqual(stage.running_count, 1)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        self.assertEqual(stage.pending_count, 1)
        release_first.set()
        await asyncio.gather(running, producer_waiting)
        self.assertEqual(started, ["first", "later"])
        await stage.close()

    async def test_running_cancellation_waits_for_inner_cleanup(self) -> None:
        stage = FairStageExecutor(workers=1, max_pending=1)
        started = asyncio.Event()
        canceled = asyncio.Event()
        release_cleanup = asyncio.Event()
        callbacks: list[str] = []

        async def work() -> None:
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                canceled.set()
                await release_cleanup.wait()
                raise

        task = asyncio.create_task(
            stage.run(
                "a", work,
                on_start=lambda: callbacks.append("start"),
                on_finish=lambda: callbacks.append("finish"),
            )
        )
        await started.wait()
        task.cancel()
        await canceled.wait()
        self.assertFalse(task.done())
        release_cleanup.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(callbacks, ["start", "finish"])
        self.assertEqual(stage.running_count, 0)
        await stage.close()

    async def test_close_cancels_queued_and_drains_running(self) -> None:
        stage = FairStageExecutor(workers=1, max_pending=1)
        started = asyncio.Event()
        finished = asyncio.Event()

        async def work() -> None:
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                finished.set()
                raise

        running = asyncio.create_task(stage.run("a", work))
        await started.wait()
        queued = asyncio.create_task(stage.run("a", work))
        await asyncio.sleep(0)
        await stage.close()
        self.assertTrue(finished.is_set())
        self.assertEqual(stage.running_count, 0)
        self.assertEqual(stage.pending_count, 0)
        for task in (running, queued):
            with self.assertRaises(asyncio.CancelledError):
                await task
        with self.assertRaises(RuntimeError):
            await stage.run("b", work)


if __name__ == "__main__":
    unittest.main()
