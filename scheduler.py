"""
scheduler.py — a lightweight in-process recurring-task scheduler.

No external dependencies (no Celery, no Redis, no APScheduler) — this
runs as a background thread inside the same process as the API,
polling the `scheduled_jobs` table on a fixed interval and creating a
real task via the Orchestrator whenever a job comes due. This is
deliberately the simplest thing that could work for a single-process
MVP.

When this stops being enough: once you run more than one API process
(for horizontal scaling), this polling-thread design will double-fire
jobs, since nothing here coordinates across processes. At that point
this needs a real job queue (or at minimum a `SELECT ... FOR UPDATE`
row lock) — noted here rather than silently left as a surprise.

Split on purpose:
  - `tick()` is pure enough to unit-test directly (given a db,
    orchestrator, and an explicit `now`) — see test_scheduler_offline.py.
  - `run_forever()` is a thin sleep-loop that can only really be
    verified by actually running the server; kept as small as possible
    for exactly that reason.
"""

import threading
import json
from datetime import datetime, timezone, timedelta

from db import new_id

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def _fmt(dt):
    return dt.strftime(TIMESTAMP_FORMAT)


class JobRegistry:
    def __init__(self, db):
        self.db = db

    def create(self, business_id, name, objective, interval_seconds, department=None,
               permission_level_required=1, budget_arc=0.0, enabled=True, now: datetime = None,
               task_type="manual", task_input=None):
        if interval_seconds < 30:
            # A polling scheduler isn't the right tool below this cadence;
            # rather than silently busy-loop, refuse and say so.
            raise ValueError("interval_seconds must be at least 30")
        job_id = new_id("job")
        now_str = _fmt(now or datetime.now(timezone.utc))
        task_input_json = json.dumps(task_input) if task_input is not None else None
        self.db.execute(
            "INSERT INTO scheduled_jobs (id, business_id, name, objective, department, "
            "permission_level_required, budget_arc, interval_seconds, enabled, next_run_at, "
            "task_type, task_input) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, business_id, name, objective, department, permission_level_required,
             budget_arc, interval_seconds, 1 if enabled else 0, now_str, task_type,
             task_input_json),
        )
        self.db.audit("owner", "create_scheduled_job", "scheduled_job", job_id,
                       {"name": name, "interval_seconds": interval_seconds,
                        "task_type": task_type})
        return job_id

    def list(self, business_id=None):
        if business_id:
            return self.db.query("SELECT * FROM scheduled_jobs WHERE business_id=? "
                                  "ORDER BY created_at DESC", (business_id,))
        return self.db.query("SELECT * FROM scheduled_jobs ORDER BY created_at DESC")

    def get(self, job_id):
        return self.db.query_one("SELECT * FROM scheduled_jobs WHERE id=?", (job_id,))

    def set_enabled(self, job_id, enabled):
        self.db.execute("UPDATE scheduled_jobs SET enabled=? WHERE id=?",
                         (1 if enabled else 0, job_id))
        self.db.audit("owner", "set_scheduled_job_enabled", "scheduled_job", job_id,
                       {"enabled": enabled})

    def set_interval(self, job_id, interval_seconds):
        if interval_seconds < 30:
            raise ValueError("interval_seconds must be at least 30")
        self.db.execute("UPDATE scheduled_jobs SET interval_seconds=? WHERE id=?",
                         (interval_seconds, job_id))
        self.db.audit("owner", "set_scheduled_job_interval", "scheduled_job", job_id,
                       {"interval_seconds": interval_seconds})


def tick(db, orchestrator, jobs: JobRegistry, now: datetime = None):
    """Runs one scheduler pass. Finds every enabled job whose
    next_run_at has arrived, creates a real task for it via the
    orchestrator (so it goes through the exact same auto-assignment and
    permission-gating logic as any manually-created task — a scheduled
    job is not a shortcut around the approval queue), and pushes its
    next_run_at forward by its interval. Returns the list of task ids
    created on this tick, so callers/tests can see exactly what fired."""
    now = now or datetime.now(timezone.utc)
    now_str = _fmt(now)
    due_jobs = db.query(
        "SELECT * FROM scheduled_jobs WHERE enabled=1 AND next_run_at <= ?", (now_str,)
    )
    created_task_ids = []
    for job in due_jobs:
        job_task_input = json.loads(job["task_input"]) if job["task_input"] else None
        task_id = orchestrator.create_task(
            job["business_id"], job["objective"], department=job["department"],
            permission_level_required=job["permission_level_required"],
            budget_arc=job["budget_arc"],
            task_type=job["task_type"] if job["task_type"] else "manual",
            task_input=job_task_input,
        )
        created_task_ids.append(task_id)
        next_run_str = _fmt(now + timedelta(seconds=job["interval_seconds"]))
        db.execute(
            "UPDATE scheduled_jobs SET last_run_at=?, next_run_at=? WHERE id=?",
            (now_str, next_run_str, job["id"]),
        )
        db.audit("scheduler", "scheduled_job_fired", "scheduled_job", job["id"],
                  {"task_id": task_id})
    return created_task_ids


def run_forever(db, orchestrator, jobs: JobRegistry, poll_interval_seconds: float,
                 stop_event: threading.Event):
    """Thin wrapper around tick(): sleep, tick, repeat, until stop_event
    is set. Never lets one bad tick kill the loop — a failure is
    recorded to the audit log and the loop continues, since a scheduler
    that silently dies is worse than one that logs an error and keeps
    trying."""
    while not stop_event.is_set():
        try:
            tick(db, orchestrator, jobs)
        except Exception as e:
            try:
                db.audit("scheduler", "scheduler_tick_error", details={"error": str(e)})
            except Exception:
                pass  # don't let audit-logging itself take down the loop
        stop_event.wait(poll_interval_seconds)
