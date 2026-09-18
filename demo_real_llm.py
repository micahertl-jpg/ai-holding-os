"""
demo_real_llm.py — same core OS as demo.py, but this time the research
task's result comes from an ACTUAL call path to the Anthropic API
(tasks/summarize_urls.py -> llm_client.py), not a hand-written string.

Run: python3 demo_real_llm.py

Behavior depends on your environment:
  - If ANTHROPIC_API_KEY is set AND you have normal internet access:
    this makes REAL API calls and fetches REAL web pages. Costs real
    (tiny) API usage.
  - If not: it automatically falls back to MockClient and prints a loud
    warning before every mock response, so you can never mistake a mock
    run for a verified one.

This was run in a sandbox with NO api key and NO general internet
access, so what you'll see below (if you're reading captured output)
is the mock path — that's expected there. Run it yourself with a real
key to exercise the live path.
"""

import os
import sys
from db import Database
from registry import BusinessRegistry, AgentRegistry
from banker import Banker
from approval import ApprovalQueue
from orchestrator import Orchestrator
from llm_client import get_default_client, AnthropicClient, MockClient
from tasks.summarize_urls import summarize_urls

DB_PATH = os.path.join(os.path.dirname(__file__), "holding_os.db")

# A narrow, fixed, low-risk research task: no news/finance sites, just
# stable reference pages, so repeated test runs are predictable.
DEFAULT_URLS = [
    "https://www.anthropic.com",
    "https://docs.claude.com",
    "https://www.python.org",
]


def main():
    client = get_default_client()
    is_mock = isinstance(client, MockClient)

    print("=" * 60)
    if is_mock:
        print("!! NO ANTHROPIC_API_KEY DETECTED (or MockClient forced) !!")
        print("!! Running in MOCK mode — no real API calls will be made !!")
    else:
        print(">> Real ANTHROPIC_API_KEY detected — this run makes REAL API calls <<")
    print("=" * 60)

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    db = Database(DB_PATH)
    businesses = BusinessRegistry(db)
    agents = AgentRegistry(db)
    banker = Banker(db)
    approvals = ApprovalQueue(db)
    orch = Orchestrator(db, banker, approvals)

    biz_id = businesses.create(
        name="Demo Ventures", type_="opportunity_discovery",
        objective="Prove the real-LLM task wiring end to end",
        budget_usd=0.0,
    )
    research_id = agents.create(biz_id, "Rex (Research agent)", role="Research Analyst",
                                 department="research", permission_level=2)
    agents.set_status(research_id, "idle")
    banker.allocate(biz_id, research_id, 50, reason="initial research budget")

    task_id = orch.create_task(
        biz_id, f"Summarize {len(DEFAULT_URLS)} reference URLs",
        department="research", permission_level_required=2, budget_arc=10,
    )

    print(f"\nCalling summarize_urls() for: {DEFAULT_URLS}\n")
    try:
        results = summarize_urls(DEFAULT_URLS, client)
    except Exception as e:
        # A real failure (bad key, network down, rate limit, etc.) should
        # fail the TASK, not crash silently or fabricate a result.
        orch.fail_task(task_id, reason=f"LLM/task error: {e}")
        print(f"TASK FAILED (recorded in DB, not swallowed): {e}")
        db.close()
        sys.exit(1)

    result_text = "\n".join(f"- {url}: {summary}" for url, summary in results.items())
    print(result_text)

    orch.complete_task(
        task_id, result=result_text, cost_arc=5, reward_arc=8,
        reason="delivered verified summaries" + (" (MOCK run)" if is_mock else ""),
    )

    print("\nStored task result (from DB, not from memory):")
    row = db.query_one("SELECT status, result FROM tasks WHERE id=?", (task_id,))
    print(f"  status={row['status']}")
    print(f"  result={row['result'][:300]}...")

    db.close()
    print(f"\nSQLite file written to: {DB_PATH}")
    if is_mock:
        print("\nReminder: this was a MOCK run. To exercise the real path, set "
              "ANTHROPIC_API_KEY and run this on a machine with normal internet "
              "access (this sandbox has neither).")


if __name__ == "__main__":
    main()
