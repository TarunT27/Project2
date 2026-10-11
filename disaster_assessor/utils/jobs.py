"""Bounded background jobs. No Streamlit calls or shared model mutation in workers."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
import uuid

from .processing import process, check_inputs
from .storage import empty_batch


class JobManager:
    def __init__(self, store, workers=2):
        self.store = store
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="assessment")
        self.capacity = workers
        self.lock = threading.RLock()
        self.jobs = {}
        store.recover_interrupted()

    def start(self, incident, user, settings, inputs, demo=False, previous_run=None):
        settings.validate()
        check_inputs(inputs, demo)
        with self.lock:
            active = [j for j in self.jobs.values() if j["status"] == "running"]
            if len(active) >= self.capacity:
                raise ValueError("The server is processing its maximum number of jobs. Try again after a run finishes.")
            if any(j["user"] == user for j in active):
                raise ValueError("You already have a running assessment. Finish or cancel it first.")
            previous = self.store.get_run(previous_run, user) if previous_run else None
            if previous and (previous["incident_id"] != incident or previous["status"] == "running"):
                raise ValueError("Choose a finished assessment in this incident to retry.")
            # Prune completed job metadata; raw inputs are freed when the worker returns.
            for key in list(self.jobs):
                if self.jobs[key]["status"] != "running":
                    del self.jobs[key]
            run_id = self.store.begin_run(incident, user, settings.to_dict(), previous_run)
            identity = uuid.uuid4().hex
            job = {"id": identity, "run_id": run_id, "incident": incident, "user": user,
                   "status": "running", "progress": 0.0, "message": "Preparing assessment", "cancel": threading.Event()}
            self.jobs[identity] = job
            self.pool.submit(self._execute, job, inputs, settings, demo, previous)
            return identity

    def _execute(self, job, inputs, settings, demo, previous):
        def progress(value, message):
            with self.lock:
                job.update(progress=value, message=message)
        try:
            batch, status = process(inputs, settings, job["cancel"], progress, demo,
                                    previous["batch"] if previous else None)
        except Exception as exc:
            batch = deepcopy(previous["batch"]) if previous else empty_batch()
            status = "partial" if batch["images"] else "failed"
            batch.update(demo=settings.demo, model="synthetic-demo" if settings.demo else settings.model_path,
                         threshold=settings.threshold, errors=[str(exc)])
            batch["failed_files"] = [{"name": i.name, "fingerprint": i.identity, "reason": str(exc)} for i in inputs]
        try:
            self.store.finish_run(job["run_id"], batch, status)
            with self.lock:
                job.update(status=status, progress=1.0, message=f"Assessment {status}")
        except Exception as exc:
            with self.lock:
                job.update(status="failed", message=f"Could not save assessment: {exc}")

    def snapshot(self, identity, user):
        with self.lock:
            job = self.jobs.get(identity)
            if not job or job["user"] != user:
                return None
            # Revalidate ACL even if a job started before access was revoked.
            self.store.role(job["incident"], user)
            return deepcopy({k: v for k, v in job.items() if k != "cancel"})

    def active_for(self, user, incident):
        with self.lock:
            return next((j["id"] for j in self.jobs.values() if j["user"] == user and j["incident"] == incident and j["status"] == "running"), None)

    def cancel(self, identity, user):
        with self.lock:
            job = self.jobs.get(identity)
            if not job or job["user"] != user:
                raise PermissionError("Job unavailable.")
            self.store.role(job["incident"], user)
            job["cancel"].set()
            job["message"] = "Cancellation requested. Finishing the current model call…"

    def shutdown(self):
        self.pool.shutdown(wait=True)
