"""
test_scheduler_offline.py — real tests for scheduler.tick(), using the
actual SQLite Database/Orchestrator (not mocks). Controls time
explicitly via tick()'s `now` parameter rather than sleeping, so this
runs instantly and deterministically. Does NOT test run_forever() or
the background thread itself — that genuinely needs a running process,
same caveat as api.py/dashboard.js elsewhere in this project.
"""

import os
from datetime import datetime, timezone, timedelta

from db import Database
from registry import BusinessRegistry, AgentRegistry
from banker import Banker
from approval import ApprovalQueue
from orchestrator import Orchestrator
from scheduler import JobRegistry, tick

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_scheduler.db")


def main():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    businesses = BusinessRegistry(db)
    agents = AgentRegistry(db)
    banker = Banker(db)
    approvals = ApprovalQueue(db)
    orch = Orchestrator(db, banker, approvals)
    jobs = JobRegistry(db)

    biz_id = businesses.create("Scheduler Test Co", "test", "prove scheduler.tick() works")
    agent_id = agents.create(biz_id, "Worker", role="Worker", department="ops",
                              permission_level=2)
    agents.set_status(agent_id, "idle")

    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    # --- job created with interval_seconds < 30 must be rejected ---
    try:
        jobs.create(biz_id, "too fast", "do a thing", interval_seconds=5)
        raise AssertionError("expected ValueError for interval_seconds < 30")
    except ValueError as e:
        print(f"PASS: rejects interval_seconds < 30 ({e})")

    # --- a freshly-created job is due immediately (next_run_at = creation time) ---
    job_id = jobs.create(biz_id, "Daily scan", "Scan for new opportunities",
                          interval_seconds=3600, department="ops",
                          permission_level_required=2, now=t0)
    created = tick(db, orch, jobs, now=t0)
    assert len(created) == 1, created
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (created[0],))
    assert task["objective"] == "Scan for new opportunities"
    assert task["business_id"] == biz_id
    print("PASS: a due job creates a real task via the orchestrator on tick()")

    # --- next_run_at correctly pushed forward by interval_seconds ---
    job_row = jobs.get(job_id)
    assert job_row["last_run_at"] == "2026-01-01 12:00:00", job_row["last_run_at"]
    assert job_row["next_run_at"] == "2026-01-01 13:00:00", job_row["next_run_at"]
    print("PASS: next_run_at advances by exactly interval_seconds")

    # --- ticking again immediately (same `now`) must NOT double-fire ---
    created_again = tick(db, orch, jobs, now=t0)
    assert created_again == [], created_again
    task_count = db.query_one("SELECT COUNT(*) as c FROM tasks")["c"]
    assert task_count == 1, task_count
    print("PASS: ticking before next_run_at creates no additional task (no double-fire)")

    # --- ticking again once next_run_at has actually arrived DOES fire ---
    t1 = t0 + timedelta(hours=1)
    created_later = tick(db, orch, jobs, now=t1)
    assert len(created_later) == 1, created_later
    task_count = db.query_one("SELECT COUNT(*) as c FROM tasks")["c"]
    assert task_count == 2, task_count
    print("PASS: ticking after next_run_at fires the job again, exactly once")

    # --- disabled jobs never fire, regardless of next_run_at ---
    jobs.set_enabled(job_id, False)
    t2 = t1 + timedelta(hours=1)
    created_disabled = tick(db, orch, jobs, now=t2)
    assert created_disabled == [], created_disabled
    task_count = db.query_one("SELECT COUNT(*) as c FROM tasks")["c"]
    assert task_count == 2, task_count
    print("PASS: a disabled job does not fire even when due")

    # --- re-enabling makes it fire again on the next due tick ---
    jobs.set_enabled(job_id, True)
    created_reenabled = tick(db, orch, jobs, now=t2)
    assert len(created_reenabled) == 1, created_reenabled
    print("PASS: re-enabling a job allows it to fire again")

    # --- set_interval rejects the same floor as create() ---
    try:
        jobs.set_interval(job_id, 5)
        raise AssertionError("expected ValueError for interval_seconds < 30")
    except ValueError as e:
        print(f"PASS: set_interval rejects interval_seconds < 30 ({e})")

    # --- set_interval does not retroactively move an already-due
    # next_run_at (it was computed under the old interval and stays
    # valid); the NEW interval only governs the next_run_at computed
    # the next time the job actually fires ---
    before = jobs.get(job_id)
    jobs.set_interval(job_id, 7200)
    after = jobs.get(job_id)
    assert after["interval_seconds"] == 7200
    assert after["next_run_at"] == before["next_run_at"], \
        "set_interval must not retroactively change an already-computed next_run_at"
    t3 = t2 + timedelta(hours=1)   # exactly when the OLD 1h interval already made it due
    created_at_old_schedule = tick(db, orch, jobs, now=t3)
    assert len(created_at_old_schedule) == 1, created_at_old_schedule
    print("PASS: set_interval doesn't retroactively delay a fire already due under the old interval")

    # --- but the fire that just happened now schedules its NEXT
    # next_run_at using the NEW interval ---
    t3_plus_1h = t3 + timedelta(hours=1)   # only 1h of the new 2h interval elapsed
    created_too_soon = tick(db, orch, jobs, now=t3_plus_1h)
    assert created_too_soon == [], created_too_soon
    t3_plus_2h = t3 + timedelta(hours=2)   # now the new 2h interval has fully elapsed
    created_at_new_interval = tick(db, orch, jobs, now=t3_plus_2h)
    assert len(created_at_new_interval) == 1, created_at_new_interval
    print("PASS: set_interval's new interval takes effect starting from the job's next fire")

    db.close()
    os.remove(TEST_DB_PATH)
    print("\nAll scheduler.tick() offline checks passed.")


if __name__ == "__main__":
    main()
