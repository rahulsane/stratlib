"""Worker processes for the slow, independent parts of screens and backtests.

Each worker opens the database read-only and receives the shared context once, then runs tasks. Results come
back in task order, so callers merge them exactly as a single process would have produced them.

More than one worker needs an import-safe main module: on Windows each worker imports the script that started
Python, so a script without an ``if __name__ == "__main__":`` guard starts its work again in every worker and
hangs. The stratlib command and the web app are safe; library calls default to one process.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Callable

log = logging.getLogger(__name__)
_state: dict = {}


def all_cores() -> int:
    return os.cpu_count() or 1


def run_tasks(store, context: dict, function: Callable[[dict, Any], Any], tasks: list, *, workers: int = 1,
              progress: Callable[[int, int], None] | None = None, local: dict | None = None) -> list:
    """``function(state, task)`` for each task, in task order. ``state`` holds ``store`` and the context; a task
    may keep its own caches in it, which last for the worker's tasks. ``function`` must be a module-level
    function so workers can import it. ``progress(done, total)`` follows the tasks finished. ``local`` joins the
    state only when the tasks run in this process, for what cannot reach a worker, such as a progress callback."""
    if workers < 2 or len(tasks) < 2:
        state = {"store": store, **context, **(local or {})}
        results = []
        for task in tasks:
            results.append(function(state, task))
            if progress:
                progress(len(results), len(tasks))
        return results
    results: list = [None] * len(tasks)
    try:
        with ProcessPoolExecutor(min(workers, len(tasks)), initializer=_start,
                                 initargs=(str(store.db_path), context)) as pool:
            futures = {pool.submit(_call, function, task): i for i, task in enumerate(tasks)}
            for done, future in enumerate(as_completed(futures), 1):
                results[futures[future]] = future.result()
                if progress:
                    progress(done, len(tasks))
    except BrokenProcessPool as exc:
        log.warning("Worker processes stopped (%s); continuing in this process.", exc)
        return run_tasks(store, context, function, tasks, workers=1, progress=progress)
    return results


def _start(db_path: str, context: dict) -> None:
    from .store import Store
    _state.update(store=Store(db_path, read_only=True), **context)


def _call(function, task):
    return function(_state, task)
