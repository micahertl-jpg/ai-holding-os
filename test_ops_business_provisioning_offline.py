"""
test_ops_business_provisioning_offline.py — calls api.py's real
ensure_ops_business_provisioned() directly (same technique
test_launch_business_offline.py uses) rather than mirroring its logic
by hand.

The real bug this guards against: ensure_ops_business_provisioned()
used to return immediately once the System Operations business already
existed, which meant a job type added AFTER that business was first
provisioned (owner_digest, added after ops_maintenance_review had
already shipped) would never get scheduled on an already-running
deployment -- only a brand-new install would ever see it. The fix
makes job provisioning idempotent PER JOB TYPE, independent of whether
the business itself is new or already existed.

Requires fastapi/pydantic installed (`.venv/bin/python3`).
"""

import os

import api
from db import Database
from registry import BusinessRegistry, AgentRegistry
from banker import Banker
from scheduler import JobRegistry

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_ops_business_provisioning.db")


def _setup():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    return db, BusinessRegistry(db), AgentRegistry(db), Banker(db), JobRegistry(db)


def test_first_call_creates_the_business_agent_and_both_scheduled_jobs():
    db, businesses, agents, banker, jobs = _setup()
    business_id = api.ensure_ops_business_provisioned(db, businesses, agents, banker, jobs)

    biz = businesses.get(business_id)
    assert biz["type"] == api.OPS_BUSINESS_TYPE
    agent = db.query_one("SELECT * FROM agents WHERE business_id=?", (business_id,))
    assert agent is not None

    job_types = {r["task_type"] for r in jobs.list(business_id)}
    assert job_types == {"ops_maintenance_review", "owner_digest"}
    print("PASS: the first call provisions the business, its agent, and both scheduled jobs")
    db.close()
    os.remove(TEST_DB_PATH)


def test_a_second_call_is_a_complete_no_op():
    db, businesses, agents, banker, jobs = _setup()
    first_id = api.ensure_ops_business_provisioned(db, businesses, agents, banker, jobs)
    second_id = api.ensure_ops_business_provisioned(db, businesses, agents, banker, jobs)

    assert first_id == second_id
    assert len(businesses.list()) == 1
    assert db.query_one("SELECT COUNT(*) as c FROM agents WHERE business_id=?",
                         (first_id,))["c"] == 1
    assert len(jobs.list(first_id)) == 2, "a restart must never create duplicate scheduled jobs"
    print("PASS: a second call (e.g. every real restart) creates nothing new")
    db.close()
    os.remove(TEST_DB_PATH)


def test_backfills_a_new_job_type_onto_an_already_provisioned_business():
    db, businesses, agents, banker, jobs = _setup()
    # Simulate a deployment that was already running BEFORE owner_digest
    # existed: the business/agent and only the ops review job exist.
    business_id = businesses.create(
        api.OPS_BUSINESS_NAME, api.OPS_BUSINESS_TYPE, "Monitor this system's own infrastructure.",
    )
    agent_id = agents.create(business_id, "Ops Monitor", role="Systems Maintenance Analyst",
                              department="ops", permission_level=2)
    banker.allocate(business_id, agent_id, 100.0, reason="starting ARC runway")
    old_job_id = jobs.create(business_id, "System health review", "Review system health",
                              interval_seconds=86400, permission_level_required=2,
                              task_type="ops_maintenance_review")

    returned_id = api.ensure_ops_business_provisioned(db, businesses, agents, banker, jobs)

    assert returned_id == business_id
    assert len(businesses.list()) == 1, "must never create a second System Operations business"
    assert db.query_one("SELECT COUNT(*) as c FROM agents WHERE business_id=?",
                         (business_id,))["c"] == 1, "must never create a second agent"
    job_rows = jobs.list(business_id)
    assert len(job_rows) == 2
    job_types = {r["task_type"] for r in job_rows}
    assert job_types == {"ops_maintenance_review", "owner_digest"}
    assert any(r["id"] == old_job_id for r in job_rows), \
        "the pre-existing ops review job must be left exactly as it was, not recreated"
    print("PASS: a previously-provisioned deployment gets the NEW owner_digest job backfilled, "
          "without touching its existing business/agent/ops-review job")
    db.close()
    os.remove(TEST_DB_PATH)


if __name__ == "__main__":
    test_first_call_creates_the_business_agent_and_both_scheduled_jobs()
    test_a_second_call_is_a_complete_no_op()
    test_backfills_a_new_job_type_onto_an_already_provisioned_business()
    print("\nAll ops-business-provisioning offline tests passed.")
