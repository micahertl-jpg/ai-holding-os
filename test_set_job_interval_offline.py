"""
test_set_job_interval_offline.py — calls api.py's real
set_job_interval() function directly (same technique
test_delete_abandoned_order_offline.py and
test_api_request_models_offline.py use) rather than mirroring its
logic by hand.

This endpoint exists so the owner can fix a scheduled job's cadence
(most importantly a trading_cycle job whose watchlist size x daily
fire count can exceed a market-data provider's free-tier request
quota) without disabling and recreating the job.

Requires fastapi/pydantic installed (`.venv/bin/python3`).
"""

import os

from fastapi import HTTPException

import api
from db import Database
from registry import BusinessRegistry
from scheduler import JobRegistry

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_set_job_interval.db")


def _setup():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    api.state["db"] = db
    api.state["businesses"] = BusinessRegistry(db)
    api.state["jobs"] = JobRegistry(db)
    biz_id = api.state["businesses"].create("Trading Co", "trading", "test", 0.0)
    job_id = api.state["jobs"].create(biz_id, "Paper trading cycle", "Run a paper-trading cycle",
                                       interval_seconds=14400, task_type="trading_cycle")
    return db, job_id


def test_set_job_interval_updates_the_real_row():
    db, job_id = _setup()
    result = api.set_job_interval(job_id, api.SetJobIntervalRequest(interval_seconds=21600))
    assert result == {"status": "updated"}
    row = api.state["jobs"].get(job_id)
    assert row["interval_seconds"] == 21600
    print("PASS: set_job_interval updates the real scheduled_jobs row")
    db.close()
    os.remove(TEST_DB_PATH)


def test_set_job_interval_rejects_below_the_30_second_floor():
    db, job_id = _setup()
    try:
        api.set_job_interval(job_id, api.SetJobIntervalRequest(interval_seconds=5))
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 400
    row = api.state["jobs"].get(job_id)
    assert row["interval_seconds"] == 14400, "a rejected update must not partially apply"
    print("PASS: set_job_interval rejects an interval below the 30-second floor, unchanged")
    db.close()
    os.remove(TEST_DB_PATH)


def test_set_job_interval_404s_on_an_unknown_job():
    db, _job_id = _setup()
    try:
        api.set_job_interval("job_does_not_exist", api.SetJobIntervalRequest(interval_seconds=3600))
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 404
    print("PASS: set_job_interval 404s on an unknown job")
    db.close()
    os.remove(TEST_DB_PATH)


if __name__ == "__main__":
    test_set_job_interval_updates_the_real_row()
    test_set_job_interval_rejects_below_the_30_second_floor()
    test_set_job_interval_404s_on_an_unknown_job()
    print("\nAll set_job_interval offline tests passed.")
