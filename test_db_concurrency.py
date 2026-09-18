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
from banker import Banker
from registry import BusinessRegistry, AgentRegistry

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_concurrency.db")
THREAD_COUNT = 20
REWARDS_PER_THREAD = 10


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
