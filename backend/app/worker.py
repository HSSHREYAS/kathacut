"""In-process job worker: one thread pulling from the SQLite queue."""
import threading
import time
import traceback

from . import db

HANDLERS: dict = {}
_state = {"alive": False, "current": None}


def handler(name):
    def deco(fn):
        HANDLERS[name] = fn
        return fn
    return deco


class Ctx:
    def __init__(self, job):
        self.job = job
        self._last = 0.0

    def progress(self, p: float, msg: str | None = None):
        now = time.time()
        if msg is None and now - self._last < 0.7:
            return
        self._last = now
        fields = {"progress": max(0.0, min(1.0, float(p)))}
        if msg is not None:
            fields["message"] = msg
        db.update_job(self.job["id"], **fields)

    def sub(self, lo: float, hi: float):
        """A progress callback mapped into the [lo, hi] slice of this job."""
        return lambda p, msg=None: self.progress(lo + (hi - lo) * p, msg)


def _loop():
    from .pipelines import clips, story  # noqa: F401  (registers handlers)
    db.requeue_interrupted()
    _state["alive"] = True
    while True:
        job = None
        try:
            job = db.claim_job()
            if not job:
                time.sleep(1.0)
                continue
            _state["current"] = job["id"]
            fn = HANDLERS.get(job["type"])
            if not fn:
                raise RuntimeError(f"Unknown job type {job['type']}")
            fn(job, Ctx(job))
            db.update_job(job["id"], status="done", progress=1.0, message="Done")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            if job:
                db.update_job(job["id"], status="error", error=f"{type(e).__name__}: {e}"[:2000],
                              message="Failed")
                try:
                    p = db.get_project(job["project_id"])
                    if p and p["status"] in ("planning", "rendering", "analyzing"):
                        db.update_project(p["id"], status="error")
                except Exception:
                    pass
            else:
                time.sleep(2.0)
        finally:
            _state["current"] = None


def start():
    if _state["alive"]:
        return
    threading.Thread(target=_loop, name="kathacut-worker", daemon=True).start()


def status():
    return dict(_state)
