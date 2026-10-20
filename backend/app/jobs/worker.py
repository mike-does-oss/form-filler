"""Background worker: polls the job table and runs jobs in a process (or thread) pool."""

from __future__ import annotations

import logging
import multiprocessing as mp
import threading
from concurrent.futures import Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor

from app.config import get_config
from app.jobs.queue import claim_next, requeue_stale

log = logging.getLogger(__name__)


def _run_in_child(job_id: int) -> dict:
    # fresh process: config comes from FORMFILLER_CONFIG / FF__ env vars, same as the parent
    from app.jobs.pipeline import run_job

    return run_job(job_id)


class Worker:
    def __init__(self, mode: str | None = None, concurrency: int | None = None):
        cfg = get_config()
        self.mode = mode or getattr(cfg.server, "worker_mode", "process")
        self.concurrency = concurrency or cfg.server.workers
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running: dict[int, Future] = {}
        self.executor: Executor
        if self.mode == "process":
            self.executor = ProcessPoolExecutor(max_workers=self.concurrency, mp_context=mp.get_context("spawn"))
        else:
            self.executor = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="job")

    def start(self) -> None:
        n = requeue_stale()
        if n:
            log.info("requeued %s stale job(s)", n)
        self._thread = threading.Thread(target=self._loop, name="job-poller", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._running = {k: f for k, f in self._running.items() if not f.done()}
            if len(self._running) < self.concurrency:
                try:
                    job_id = claim_next()
                except Exception as e:  # noqa: BLE001
                    log.warning("claim failed: %s", e)
                    job_id = None
                if job_id is not None:
                    fn = _run_in_child if self.mode == "process" else _run_in_thread
                    fut = self.executor.submit(fn, job_id)
                    fut.add_done_callback(lambda f, j=job_id: _log_result(j, f))
                    self._running[job_id] = fut
                    continue
            self._stop.wait(0.5)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.executor.shutdown(wait=False, cancel_futures=True)


def _run_in_thread(job_id: int) -> dict:
    from app.jobs.pipeline import run_job

    return run_job(job_id)


def _log_result(job_id: int, fut: Future) -> None:
    exc = fut.exception()
    if exc:
        log.error("job %s ended with error: %s", job_id, exc)
    else:
        log.info("job %s done: %s", job_id, fut.result())
