"""Background job runner for assessments (thread pool; GPU work is serialised by DRA_WORKERS=1)."""

from concurrent.futures import ThreadPoolExecutor
import logging
import threading

from . import db
from .config import get_config

log = logging.getLogger("dra.jobs")


class JobRunner:
    def __init__(self):
        self._pool = None
        self._cancel = set()
        self._lock = threading.Lock()
        self.futures = {}

    def _executor(self):
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=get_config().workers, thread_name_prefix="dra-job")
            return self._pool

    def submit(self, run_id):
        future = self._executor().submit(self._run, run_id)
        self.futures[run_id] = future
        return future

    def cancel(self, run_id):
        with self._lock:
            self._cancel.add(run_id)

    def _run(self, run_id):
        from .services.assess import Job

        try:
            return Job(run_id, cancelled=lambda: run_id in self._cancel).run_job()
        except Exception:
            log.exception("Job %s crashed", run_id)
            with db.connect() as conn:
                db.update(conn, "runs", run_id, {"status": "failed", "message": "Internal error", "finished_at": db.now()})
            return "failed"
        finally:
            with self._lock:
                self._cancel.discard(run_id)
            self.futures.pop(run_id, None)

    def recover(self):
        """At startup: runs interrupted mid-flight fail; queued runs with uploads are resumed."""
        with db.connect() as conn:
            conn.execute("UPDATE runs SET status = 'failed', message = 'Interrupted by a server restart', "
                         "finished_at = ? WHERE status = 'running'", (db.now(),))
            queued = [r["id"] for r in db.all_rows(conn, "SELECT id FROM runs WHERE status = 'queued' ORDER BY id")]
        for run_id in queued:
            self.submit(run_id)

    def shutdown(self):
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)


runner = JobRunner()
