"""Fair, bounded stage execution shared by concurrent acquisition batches."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(eq=False)
class _Job:
    batch_id: str
    work: Callable[[], Awaitable[Any]]
    result: asyncio.Future[Any]
    done: asyncio.Future[None]
    on_start: Callable[[], None] | None
    on_finish: Callable[[], None] | None
    state: str = "waiting"
    inner: asyncio.Task[Any] | None = None
    cancel_requested: bool = False


class FairStageExecutor:
    """Run a stage with fixed workers and round-robin batch scheduling.

    ``max_pending`` bounds jobs accepted into the execution queue. Producers
    wait for space without occupying a worker. Callbacks are synchronous and
    run only for jobs that a worker actually starts.
    """

    def __init__(self, workers: int, max_pending: int) -> None:
        if workers < 1 or max_pending < 1:
            raise ValueError("workers and max_pending must be positive")
        self._worker_count = workers
        self._max_pending = max_pending
        self._condition = asyncio.Condition()
        self._waiting: dict[str, deque[_Job]] = {}
        self._waiting_order: deque[str] = deque()
        self._pending: dict[str, deque[_Job]] = {}
        self._ready_order: deque[str] = deque()
        self._pending_count = 0
        self._running: set[_Job] = set()
        self._workers: list[asyncio.Task[None]] = []
        self._closed = False

    @property
    def pending_count(self) -> int:
        return self._pending_count

    @property
    def running_count(self) -> int:
        return len(self._running)

    def _admit_locked(self) -> None:
        while self._pending_count < self._max_pending and self._waiting_order:
            batch_id = self._waiting_order.popleft()
            waiting = self._waiting[batch_id]
            job = waiting.popleft()
            if waiting:
                self._waiting_order.append(batch_id)
            else:
                del self._waiting[batch_id]
            pending = self._pending.setdefault(batch_id, deque())
            if not pending:
                self._ready_order.append(batch_id)
            pending.append(job)
            job.state = "pending"
            self._pending_count += 1
        self._condition.notify_all()

    def _remove_queued_locked(self, job: _Job) -> bool:
        if job.state == "waiting":
            queue = self._waiting[job.batch_id]
            queue.remove(job)
            if not queue:
                del self._waiting[job.batch_id]
                self._waiting_order.remove(job.batch_id)
        elif job.state == "pending":
            queue = self._pending[job.batch_id]
            queue.remove(job)
            self._pending_count -= 1
            if not queue:
                del self._pending[job.batch_id]
                self._ready_order.remove(job.batch_id)
            self._admit_locked()
        else:
            return False
        job.state = "done"
        job.result.cancel()
        job.done.set_result(None)
        self._condition.notify_all()
        return True

    async def _cancel_job(self, job: _Job) -> None:
        async with self._condition:
            if self._remove_queued_locked(job):
                return
            if job.state == "done":
                return
            job.result.cancel()
            if not job.cancel_requested:
                job.cancel_requested = True
                if job.inner is not None:
                    job.inner.cancel()
        await asyncio.shield(job.done)

    async def run(
        self,
        batch_id: str,
        async_callable: Callable[[], Awaitable[Any]],
        on_start: Callable[[], None] | None = None,
        on_finish: Callable[[], None] | None = None,
    ) -> Any:
        """Submit one operation and wait for its result or complete cancellation."""
        loop = asyncio.get_running_loop()
        job = _Job(
            batch_id=batch_id,
            work=async_callable,
            result=loop.create_future(),
            done=loop.create_future(),
            on_start=on_start,
            on_finish=on_finish,
        )
        async with self._condition:
            if self._closed:
                raise RuntimeError("stage executor is closed")
            waiting = self._waiting.setdefault(batch_id, deque())
            if not waiting:
                self._waiting_order.append(batch_id)
            waiting.append(job)
            self._admit_locked()
            if not self._workers:
                self._workers = [
                    asyncio.create_task(self._worker()) for _ in range(self._worker_count)
                ]
        try:
            return await asyncio.shield(job.result)
        except asyncio.CancelledError:
            await self._cancel_job(job)
            raise

    async def _next_job(self) -> _Job | None:
        async with self._condition:
            while True:
                if self._ready_order:
                    batch_id = self._ready_order.popleft()
                    queue = self._pending[batch_id]
                    job = queue.popleft()
                    self._pending_count -= 1
                    if queue:
                        self._ready_order.append(batch_id)
                    else:
                        del self._pending[batch_id]
                    job.state = "running"
                    self._running.add(job)
                    self._admit_locked()
                    return job
                if self._closed and not self._waiting:
                    return None
                await self._condition.wait()

    async def _worker(self) -> None:
        while True:
            job = await self._next_job()
            if job is None:
                return
            value: Any = None
            error: BaseException | None = None
            started = False
            try:
                if job.cancel_requested:
                    raise asyncio.CancelledError
                started = True
                if job.on_start is not None:
                    job.on_start()
                job.inner = asyncio.create_task(job.work())
                if job.cancel_requested:
                    job.inner.cancel()
                value = await job.inner
            except BaseException as exc:
                error = exc
            finally:
                if started and job.on_finish is not None:
                    try:
                        job.on_finish()
                    except BaseException as exc:
                        if error is None:
                            error = exc
                async with self._condition:
                    job.state = "done"
                    self._running.discard(job)
                    if not job.result.done():
                        if error is None:
                            job.result.set_result(value)
                        elif isinstance(error, asyncio.CancelledError):
                            job.result.cancel()
                        else:
                            job.result.set_exception(error)
                    job.done.set_result(None)
                    self._condition.notify_all()

    async def close(self, *, drain: bool = False) -> None:
        """Stop submissions; cancel and drain work, or finish it if requested."""
        async with self._condition:
            self._closed = True
            if not drain:
                for queue in (*self._waiting.values(), *self._pending.values()):
                    for job in queue:
                        job.state = "done"
                        job.result.cancel()
                        job.done.set_result(None)
                self._waiting.clear()
                self._waiting_order.clear()
                self._pending.clear()
                self._ready_order.clear()
                self._pending_count = 0
                for job in self._running:
                    job.result.cancel()
                    if not job.cancel_requested:
                        job.cancel_requested = True
                        if job.inner is not None:
                            job.inner.cancel()
            self._condition.notify_all()
            workers = list(self._workers)
        if workers:
            await asyncio.gather(*workers)
