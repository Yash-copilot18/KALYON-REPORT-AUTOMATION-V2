"""
One worker pool, created once and kept warm, shared by every forecast run.

Both forecast buttons spend nearly all their time inside `fit`, and those fits
are independent, so they are spread across processes. The catch is where the
time actually went before this module existed: on Windows a new process is
*spawned*, not forked, so every worker starts a fresh interpreter and re-imports
the whole application graph - scikit-learn included. Measured on this machine:

    pool startup,  4 workers   3.48s
    pool startup, 11 workers   7.47s

A plant forecast only has about 14 seconds of fitting in it, so building a pool
per request spent half the run creating the thing meant to make it faster: the
first parallel attempt came out at 8.4s against 9.7s serial, a 1.16x return for
a great deal of machinery.

The pool is therefore built once and reused. The spawn cost is paid a single
time, in a background thread at startup, and by the time anyone presses a
button the workers are already sitting there with scikit-learn loaded.

The cost of that is memory: roughly 119 MB per worker, held for the life of the
process. On a twelve-core host the default of eleven workers is about 1.3 GB.
Set FORECAST_WORKERS to cap it, or to 1 to disable parallelism altogether and
run everything in this process.

Nothing here changes a single number. Each task is a self-contained fit over
its own slice of history with a fixed seed and `n_jobs=1`, and results are
reassembled in order by the caller.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import threading
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Callable, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Long enough that the pool must spawn every worker to satisfy the warm-up
# in parallel, short enough that startup is not noticeably delayed.
_WARM_SLEEP_S = 0.75

# Beyond this, more workers measurably stopped helping on this workload.
WORKER_CAP = int(os.getenv("FORECAST_WORKER_CAP", "8"))

_lock = threading.Lock()
_pool: Optional[ProcessPoolExecutor] = None
_pool_size = 0


def _worker_init() -> None:
    """
    One thread per worker.

    The pool already occupies every core, so letting each worker start its own
    BLAS thread pool oversubscribes the machine and makes the run slower, not
    faster. It also keeps the forest deterministic, which matters more.
    """
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[var] = "1"


def size() -> int:
    """
    How many workers to run. 0 or unset means automatic; 1 disables the pool.

    One core is left for the server itself, so the API keeps answering while a
    forecast is in progress.
    """
    raw = os.getenv("FORECAST_WORKERS", "").strip()
    if raw:
        try:
            want = int(raw)
        except ValueError:
            logger.warning("FORECAST_WORKERS=%r is not a number; "
                           "using automatic instead.", raw)
            want = 0
        if want == 1:
            return 1
        if want > 0:
            return want
    # One core left for the server, and capped. os.cpu_count() reports logical
    # processors, and this work is dense numeric fitting that gains nothing
    # from hyperthreads - measured on this 8-core/12-thread host, the device
    # run took 72.6s on 8 workers and 75.8s on 11, so the extra three cost
    # time rather than saving it. Four was clearly too few at 102.9s.
    return max(1, min((os.cpu_count() or 2) - 1, WORKER_CAP))


def get() -> Optional[ProcessPoolExecutor]:
    """
    The shared pool, creating it on first use.

    Returns None when parallelism is unavailable or switched off, which is the
    caller's signal to do the work in this process. A worker never gets a pool
    of its own: nesting them would fork bomb a machine quietly.
    """
    if multiprocessing.parent_process() is not None:
        return None

    n = size()
    if n <= 1:
        return None

    global _pool, _pool_size
    with _lock:
        if _pool is not None and _pool_size == n:
            return _pool
        if _pool is not None:
            # Size changed under us; replace it rather than run on the old one.
            _pool.shutdown(wait=False)
            _pool = None
        try:
            _pool = ProcessPoolExecutor(max_workers=n, initializer=_worker_init)
            _pool_size = n
            logger.info("forecast worker pool created with %d workers", n)
        except Exception as exc:                        # noqa: BLE001
            # A locked-down host or a frozen build may refuse to spawn. The
            # forecast must still run, just in this process and slower.
            logger.warning("Worker pool unavailable (%s); running in-process.", exc)
            _pool, _pool_size = None, 0
        return _pool


def run_map(fn: Callable[..., Any], *iterables: Iterable[Any]) -> Optional[List[Any]]:
    """
    Map `fn` across the pool, or return None if the caller should do it itself.

    A pool whose worker died is discarded and rebuilt once; if the retry also
    fails the caller falls back to running in-process. Either way the work gets
    done - the only difference is how long it takes.
    """
    args = [list(it) for it in iterables]
    if not args or not args[0]:
        return []

    for attempt in (1, 2):
        pool = get()
        if pool is None:
            return None
        try:
            return list(pool.map(fn, *args))
        except Exception as exc:                        # noqa: BLE001
            logger.warning("Pool map failed on attempt %d (%s).", attempt, exc)
            reset()
    return None


def reset() -> None:
    """Drop the pool so the next call builds a fresh one."""
    global _pool, _pool_size
    with _lock:
        if _pool is not None:
            _pool.shutdown(wait=False)
        _pool, _pool_size = None, 0


def warm() -> None:
    """
    Pay the spawn cost at startup instead of on someone's first click.

    Safe to call from a background thread, and safe to call when the pool is
    disabled - it simply does nothing. A failure here is not fatal: the pool is
    an optimisation, and every caller can run without it.
    """
    if os.getenv("FORECAST_POOL_WARM", "1").strip() in ("0", "false", "False"):
        logger.info("forecast worker pool warm-up disabled")
        return
    try:
        pool = get()
        if pool is None:
            return
        # Each worker must actually be made to start. A pool creates workers
        # lazily, so mapping instant tasks over it is satisfied by the first
        # one or two processes and the rest are never spawned - the warm-up
        # then returns in half a second and the first real run still pays the
        # spawn cost. Giving every task a short sleep means the pool has no
        # choice but to bring all of them up at once.
        deadline = max(1, _pool_size) * [_WARM_SLEEP_S]
        list(pool.map(_touch, deadline))
        logger.info("forecast worker pool warm (%d workers)", _pool_size)
    except Exception as exc:                            # noqa: BLE001
        logger.warning("Worker pool warm-up failed (%s); "
                       "runs will fall back as needed.", exc)


def _touch(seconds: float) -> bool:
    """
    Make one worker do everything a real task will need, except the work.

    Two things have to happen here, and missing either one makes the warm-up
    look successful while achieving nothing:

    * **Import what the tasks import.** This module is deliberately cheap - os,
      logging, threading - so a worker that only starts it is not warm at all.
      The expensive import is scikit-learn, reached through the backtest
      module, and until a child has done it the first real task still pays for
      it. Importing it here moved the first plant forecast from 6.9s to 2.6s;
      without it the warm-up finished in under a second and bought nothing.

    * **Stay busy while the others start.** A pool creates workers lazily, so
      instant warm-up tasks are swallowed by the first process and the rest
      are never spawned. The sleep forces the pool to bring all of them up.
    """
    import time
    from app.forecast import backtest  # noqa: F401 - imported for its side effect
    time.sleep(seconds)
    return True


def shutdown() -> None:
    """Release the workers when the application stops."""
    global _pool, _pool_size
    with _lock:
        if _pool is not None:
            _pool.shutdown(wait=False)
            logger.info("forecast worker pool shut down")
        _pool, _pool_size = None, 0
