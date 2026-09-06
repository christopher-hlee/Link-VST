"""A tiny background job runner.

Downloads and analysis shouldn't block the UI, and you want to keep digging
while ten records come down in the background. One asyncio worker pool, an
in-memory job list, no broker.
"""
from __future__ import annotations

import asyncio
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class Job:
    id: str
    kind: str
    label: str
    status: str = "queued"  # queued|running|done|error
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: Any = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
        }


class JobRunner:
    def __init__(self, concurrency: int = 3, history: int = 200):
        self.concurrency = concurrency
        self.history = history
        self.jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._queue: asyncio.Queue[tuple[Job, Callable[[], Awaitable[Any]]]] | None = None
        self._workers: list[asyncio.Task] = []

    async def start(self) -> None:
        if self._workers:
            return
        self._queue = asyncio.Queue()
        self._workers = [
            asyncio.create_task(self._worker(), name=f"crate-worker-{i}")
            for i in range(self.concurrency)
        ]

    async def stop(self) -> None:
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._workers.clear()
        self._queue = None

    def submit(
        self, kind: str, label: str, fn: Callable[[], Awaitable[Any]]
    ) -> Job:
        if self._queue is None:
            raise RuntimeError("JobRunner is not running")
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label)
        self.jobs[job.id] = job
        self._order.append(job.id)
        self._trim()
        self._queue.put_nowait((job, fn))
        return job

    def _trim(self) -> None:
        while len(self._order) > self.history:
            stale = self._order.pop(0)
            job = self.jobs.get(stale)
            if job and job.status in ("queued", "running"):
                self._order.append(stale)  # never drop live work
                return
            self.jobs.pop(stale, None)

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            job, fn = await self._queue.get()
            job.status, job.started_at = "running", time.time()
            try:
                job.result = await fn()
                job.status = "done"
            except asyncio.CancelledError:
                job.status, job.error = "error", "cancelled"
                raise
            except Exception as exc:
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            finally:
                job.finished_at = time.time()
                self._queue.task_done()

    def listing(self, limit: int = 50) -> list[dict[str, Any]]:
        ids = self._order[-limit:][::-1]
        return [self.jobs[i].as_dict() for i in ids if i in self.jobs]

    @property
    def active(self) -> int:
        return sum(1 for j in self.jobs.values() if j.status in ("queued", "running"))
