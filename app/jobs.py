"""A very small background job runner.

Generation takes minutes, so the HTTP request cannot wait for it. Jobs run on a
bounded thread pool; the UI polls the job endpoint with HTMX until it settles.
"""
from __future__ import annotations

import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .config import MAX_CONCURRENT_JOBS

_pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS,
                           thread_name_prefix="cf-job")
_lock = threading.Lock()
_jobs: dict[str, "Job"] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    kind: str                      # article | voice | ideas
    label: str
    status: str = "queued"         # queued | running | done | error
    result: Any = None
    error: str = ""
    meta: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    finished_at: str = ""

    @property
    def settled(self) -> bool:
        return self.status in ("done", "error")


def submit(kind: str, label: str, fn: Callable[[], Any], **meta) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label, meta=meta)
    with _lock:
        _jobs[job.id] = job

    def _run() -> None:
        with _lock:
            job.status = "running"
        try:
            job.result = fn()
            with _lock:
                job.status = "done"
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            with _lock:
                job.status = "error"
                job.error = str(exc) or exc.__class__.__name__
        finally:
            job.finished_at = _now()

    _pool.submit(_run)
    return job


def get(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def active() -> list[Job]:
    with _lock:
        return [j for j in _jobs.values() if not j.settled]


def prune(keep: int = 200) -> None:
    with _lock:
        if len(_jobs) <= keep:
            return
        settled = sorted((j for j in _jobs.values() if j.settled),
                         key=lambda j: j.finished_at)
        for j in settled[: len(_jobs) - keep]:
            _jobs.pop(j.id, None)
