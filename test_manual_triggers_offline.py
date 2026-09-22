"""
test_manual_triggers_offline.py — calls api.py's real
trigger_ops_review()/trigger_owner_digest() functions directly (same
technique test_ops_business_provisioning_offline.py uses), rather than
mirroring their logic by hand. Both are the same "run this
system-wide scheduled job right now, outside its normal cadence"
pattern, so a bug in one is likely to also be in the other -- worth
covering together.

Requires fastapi/pydantic installed (`.venv/bin/python3`).
"""

import os

import api
from db import Database
from registry import BusinessRegistry, AgentRegistry
from banker import Banker
from approval import ApprovalQueue
from orchestrator import Orchestrator
from scheduler import JobRegistry

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_manual_triggers.db")


def _setup():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    api.state["db"] = db
    api.state["businesses"] = BusinessRegistry(db)
    api.state["agents"] = AgentRegistry(db)
    api.state["banker"] = Banker(db)
    api.state["approvals"] = ApprovalQueue(db)
    api.state["orchestrator"] = Orchestrator(db, api.state["banker"], api.state["approvals"])
    api.state["jobs"] = JobRegistry(db)
    return db


def test_trigger_ops_review_creates_a_real_task_under_the_ops_business():
    db = _setup()
    result = api.trigger_ops_review()
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (result["task_id"],))
    assert task is not None
    assert task["task_type"] == "ops_maintenance_review"
    biz = api.state["businesses"].get(task["business_id"])
    assert biz["type"] == api.OPS_BUSINESS_TYPE
    print("PASS: trigger_ops_review creates a real ops_maintenance_review task under the "
          "System Operations business")
    db.close()
    os.remove(TEST_DB_PATH)


def test_trigger_owner_digest_creates_a_real_task_under_the_ops_business():
    db = _setup()
    result = api.trigger_owner_digest()
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (result["task_id"],))
    assert task is not None
    assert task["task_type"] == "owner_digest"
    biz = api.state["businesses"].get(task["business_id"])
    assert biz["type"] == api.OPS_BUSINESS_TYPE
    print("PASS: trigger_owner_digest creates a real owner_digest task under the "
          "System Operations business, on demand -- no need to wait for the next "
          "OWNER_DIGEST_INTERVAL_SECONDS to confirm a fixed email configuration works")
    db.close()
    os.remove(TEST_DB_PATH)


def test_both_triggers_reuse_the_same_system_operations_business_not_duplicate_it():
    db = _setup()
    ops_result = api.trigger_ops_review()
    digest_result = api.trigger_owner_digest()
    ops_task = db.query_one("SELECT business_id FROM tasks WHERE id=?", (ops_result["task_id"],))
    digest_task = db.query_one("SELECT business_id FROM tasks WHERE id=?", (digest_result["task_id"],))
    assert ops_task["business_id"] == digest_task["business_id"]
    assert len(api.state["businesses"].list()) == 1
    print("PASS: both manual triggers provision/reuse the SAME System Operations business, "
          "never a duplicate")
    db.close()
    os.remove(TEST_DB_PATH)


if __name__ == "__main__":
    test_trigger_ops_review_creates_a_real_task_under_the_ops_business()
    test_trigger_owner_digest_creates_a_real_task_under_the_ops_business()
    test_both_triggers_reuse_the_same_system_operations_business_not_duplicate_it()
    print("\nAll manual-trigger offline tests passed.")
