"""Background benchmark job: a worker thread + threading.Event kept in st.session_state.

The worker never touches Streamlit APIs; it only writes into a plain dict that the page's
polling fragment reads.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable

JOB_KEY = "bench_job"
LAST_RUN_KEY = "bench_last_run"


def start_job(session_state: Any, *, db: Any, endpoints: list, config: Any,
              client_factory: Callable[[Any], Any], base_url: str, logger: Any = None) -> dict:
    from src.benchmark_engine import BenchmarkRunner
    from src.run_lock import RunBusyError

    stop_event = threading.Event()
    state: dict[str, Any] = {
        "run_id": None, "completed": 0, "total": 0, "current": None, "elapsed_s": 0.0,
        "message": "Starting…", "log": deque(maxlen=200), "done": False, "error": None,
        "started": time.monotonic(), "ok": 0, "failed": 0,
    }

    def progress_cb(p: Any) -> None:
        state["run_id"] = p.run_id
        state["completed"], state["total"] = p.completed, p.total
        state["current"], state["elapsed_s"] = p.current_endpoint, p.elapsed_s
        state["message"] = p.message or ""
        r = p.last_result
        if r is not None:
            if r.success:
                state["ok"] += 1
            else:
                state["failed"] += 1
            lat = "—" if r.latency_ms is None else f"{r.latency_ms:,.1f} ms"
            tag = " (warm-up)" if r.is_warmup else ""
            state["log"].appendleft(
                f"#{r.seq:<4} {r.endpoint_id:<20} {lat:>12}  {r.outcome}"
                f"{' · HTTP ' + str(r.http_status) if r.http_status else ''}{tag}")

    def worker() -> None:
        try:
            runner = BenchmarkRunner(db, endpoints, client_factory, base_url=base_url, logger=logger)
            state["run_id"] = runner.run(config, progress_cb=progress_cb, stop_event=stop_event)
            state["message"] = "Stopped by you." if stop_event.is_set() else "Finished."
        except RunBusyError as exc:
            state["error"] = str(exc)
        except Exception as exc:  # never surface secrets: message only, redacted
            try:
                from src.redaction import redact_text

                state["error"] = redact_text(f"{type(exc).__name__}: {exc}")
            except Exception:
                state["error"] = type(exc).__name__
            if logger is not None:
                logger.exception("Benchmark worker failed")
        finally:
            state["done"] = True

    thread = threading.Thread(target=worker, name="tsbench-runner", daemon=True)
    job = {"thread": thread, "stop": stop_event, "state": state}
    session_state[JOB_KEY] = job
    thread.start()
    return job


def active_job(session_state: Any) -> dict | None:
    return session_state.get(JOB_KEY)
