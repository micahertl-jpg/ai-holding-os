"""
test_db_concurrency.py — a real concurrency test, not a mock. Spins up
actual OS threads hammering one shared Database instance simultaneously
(mirroring how api.py's request thread pool + the scheduler's
background thread will really share one connection), and checks that
every write actually landed with no corruption, no lost updates, and no
crashes. This is exactly the scenario the threading.Lock in db.py was
added for — worth proving it actually works, not just that it compiles.
"""

import os
import threading

from db import Database
from banker import Banker, InsufficientArcError
from approval import ApprovalQueue
from registry import BusinessRegistry, AgentRegistry
from orchestrator import Orchestrator

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_concurrency.db")
CHARGE_TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_concurrency_charge.db")
COMPLETE_TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_concurrency_complete.db")
THREAD_COUNT = 20
REWARDS_PER_THREAD = 10


def test_concurrent_charge_never_overdraws():
    """Regression test for a real race found via this project's own
    testing discipline: banker.charge() used to SELECT the balance,
    check it in Python, then UPDATE -- two concurrent charge() calls for
    the same agent (e.g. two /tasks/{id}/complete requests landing close
    together) could both read the same starting balance, both decide
    they could afford it, and both deduct, together taking the balance
    negative. A forced-timing reproduction (a deliberate sleep widening
    the read-write window) turned this from "rare" to "every single
    time": 20 threads charging 10 against a balance of 100 all
    'succeeded', final balance -100.

    Fixed by making the check-and-deduct one atomic SQL statement
    (`UPDATE ... WHERE arc_balance >= amount`, using the new rowcount
    return from db.py's execute()) instead of two separate calls. This
    test proves the fix holds under REAL concurrency, no artificial
    delay needed -- unlike the original bug, the fix is a structural
    guarantee (atomicity), not a timing-dependent one, so this should
    pass reliably on every run, not just usually."""
    if os.path.exists(CHARGE_TEST_DB_PATH):
        os.remove(CHARGE_TEST_DB_PATH)
    db = Database(CHARGE_TEST_DB_PATH)
    businesses = BusinessRegistry(db)
    agents = AgentRegistry(db)
    banker = Banker(db)

    biz_id = businesses.create("Charge Race Test Co", "test", "prove charge() is race-safe")
    agent_id = agents.create(biz_id, "Target Agent", role="test", permission_level=1)
    starting_balance = 100.0
    banker.allocate(biz_id, agent_id, starting_balance, reason="starting balance")

    charge_amount = 10.0
    max_possible_successes = int(starting_balance // charge_amount)  # 10
    succeeded, failed, errors = [], [], []
    lock = threading.Lock()

    def hammer():
        try:
            banker.charge(agent_id, biz_id, charge_amount, reason="race test")
            with lock:
                succeeded.append(1)
        except InsufficientArcError:
            with lock:
                failed.append(1)
        except Exception as e:
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(THREAD_COUNT)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"unexpected errors: {errors[:3]}"
    assert len(succeeded) == max_possible_successes, (
        f"expected exactly {max_possible_successes} successful charges "
        f"(balance {starting_balance} / charge {charge_amount}), got {len(succeeded)} "
        f"-- more than that succeeding means concurrent charges overdrew the balance"
    )
    final_balance = banker.balance(agent_id)
    assert final_balance == 0.0, (
        f"expected final balance exactly 0.0, got {final_balance} -- "
        f"a negative balance means the race is back"
    )
    ledger_spend_count = db.query_one(
        "SELECT COUNT(*) as c FROM arc_ledger WHERE agent_id=? AND entry_type='spend'",
        (agent_id,),
    )["c"]
    assert ledger_spend_count == max_possible_successes, ledger_spend_count
    print(f"PASS: {THREAD_COUNT} concurrent charge() calls against a balance that can only "
          f"cover {max_possible_successes} of them -- exactly {max_possible_successes} "
          f"succeeded, balance never went negative, no race")
    db.close()
    os.remove(CHARGE_TEST_DB_PATH)


def test_concurrent_complete_task_has_exactly_one_winner():
    """Regression test for the same class of race as
    test_concurrent_charge_never_overdraws, found in the same review
    pass: orchestrator.complete_task() read a task's status, checked it
    in Python, then wrote 'completed' -- a window where two concurrent
    callers (e.g. the owner manually completing via POST
    /tasks/{id}/complete at the same moment the executor's background
    thread auto-completes it) could both pass the check and both
    charge/reward ARC for ONE task's completion. Fixed the same way:
    the status check moved into the UPDATE's WHERE clause, so only one
    caller can ever win the transition."""
    if os.path.exists(COMPLETE_TEST_DB_PATH):
        os.remove(COMPLETE_TEST_DB_PATH)
    db = Database(COMPLETE_TEST_DB_PATH)
    businesses = BusinessRegistry(db)
    agents = AgentRegistry(db)
    banker = Banker(db)
    approvals = ApprovalQueue(db)
    orch = Orchestrator(db, banker, approvals)

    biz_id = businesses.create("Complete Race Test Co", "test", "prove complete_task is race-safe")
    agent_id = agents.create(biz_id, "Worker", role="test", permission_level=1)
    agents.set_status(agent_id, "idle")
    starting_balance = 1000.0
    banker.allocate(biz_id, agent_id, starting_balance, reason="starting balance")

    task_id = orch.create_task(biz_id, "do a thing", permission_level_required=1)
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert task["status"] == "assigned", task["status"]

    reward_amount = 10.0
    succeeded, failed, errors = [], [], []
    lock = threading.Lock()

    def hammer():
        try:
            orch.complete_task(task_id, result="done", cost_arc=0, reward_arc=reward_amount)
            with lock:
                succeeded.append(1)
        except ValueError:
            with lock:
                failed.append(1)
        except Exception as e:
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(THREAD_COUNT)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"unexpected errors: {errors[:3]}"
    assert len(succeeded) == 1, (
        f"expected exactly 1 winner completing this task, got {len(succeeded)} -- "
        f"more than one means the task was double-completed and ARC was double-rewarded"
    )
    assert len(failed) == THREAD_COUNT - 1

    final_balance = banker.balance(agent_id)
    assert final_balance == starting_balance + reward_amount, (
        f"expected exactly one reward of {reward_amount} applied "
        f"({starting_balance} + {reward_amount} = {starting_balance + reward_amount}), "
        f"got {final_balance} -- ARC was rewarded more than once"
    )

    final_task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert final_task["status"] == "completed"
    print(f"PASS: {THREAD_COUNT} concurrent complete_task() calls for the same task -- "
          f"exactly 1 winner, ARC rewarded exactly once, no race")
    db.close()
    os.remove(COMPLETE_TEST_DB_PATH)


def main():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    db = Database(TEST_DB_PATH)
    businesses = BusinessRegistry(db)
    agents = AgentRegistry(db)
    banker = Banker(db)

    biz_id = businesses.create("Concurrency Test Co", "test", "prove the lock works")
    agent_id = agents.create(biz_id, "Target Agent", role="test", permission_level=1)

    errors = []

    def hammer():
        try:
            for _ in range(REWARDS_PER_THREAD):
                banker.reward(agent_id, biz_id, 1.0, reason="concurrency test")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(THREAD_COUNT)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"{len(errors)} threads raised errors: {errors[:3]}"
    print(f"PASS: {THREAD_COUNT} threads x {REWARDS_PER_THREAD} concurrent writes each "
          f"raised no errors")

    expected_total = THREAD_COUNT * REWARDS_PER_THREAD * 1.0
    actual_balance = banker.balance(agent_id)
    assert actual_balance == expected_total, (
        f"expected {expected_total}, got {actual_balance} — lost updates under concurrency"
    )
    print(f"PASS: final ARC balance is exactly {expected_total} "
          f"— no lost updates from concurrent writes")

    ledger_count = db.query_one(
        "SELECT COUNT(*) as c FROM arc_ledger WHERE agent_id=?", (agent_id,)
    )["c"]
    assert ledger_count == THREAD_COUNT * REWARDS_PER_THREAD, ledger_count
    print(f"PASS: exactly {ledger_count} ledger rows written, none lost or duplicated")

    db.close()
    os.remove(TEST_DB_PATH)
    print("\nConcurrency check passed — the threading.Lock in db.py correctly "
          "serializes access across real concurrent threads.")


if __name__ == "__main__":
    main()
    test_concurrent_charge_never_overdraws()
    test_concurrent_complete_task_has_exactly_one_winner()
    print("\nAll db concurrency checks passed.")
