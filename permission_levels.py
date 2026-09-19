"""
permission_levels.py — the full 0-7 permission-level ladder from the
project's master spec (Section 8), as actually enforced levels rather
than description in prose. Before this file existed, this system only
enforced a single threshold (permission_level_required >= 6 always
routes to the human ApprovalQueue); every other level from 0-5 and 7
existed only as a plain integer with no distinct behavior of its own.

Levels and what they mean here — most are descriptive tiers an agent
can be assigned at (useful for the owner's own judgment about what to
let an agent do), but two have real enforced behavior, noted below:

0  OBSERVE_ONLY          - never auto-assigned any executable task by
                           orchestrator._try_assign, even if a task
                           explicitly requires only level 0. Useful for
                           a newly onboarded/unproven agent that should
                           not yet be doing real work.
1  RESEARCH              - can be assigned tasks that only gather or
                           produce information, no side effects. This
                           MVP's research task types default to
                           requiring this level or higher.
2  RECOMMEND             - can produce a recommendation/assessment for
                           a human or higher-clearance agent to act on.
                           The storefront's paid research tasks require
                           this level.
3  SIMULATE              - can run a simulation / paper / no-real-effect
                           version of an action (e.g. paper trading).
                           Not backed by a real simulation task type in
                           this MVP yet — reserved for the trading
                           vertical the spec describes.
4  EXECUTE_LOW_RISK       - can take a real, low-risk, easily-reversible
                           action with no spending budget attached.
5  EXECUTE_WITH_BUDGET    - can take a real action that spends ARC/real
                           resources, but only within its own budget
                           (banker.py's existing balance checks apply).
6  REQUIRE_APPROVAL       - ENFORCED: always routed to the human
                           ApprovalQueue first, regardless of any
                           agent's own clearance level. Once the owner
                           approves, an eligible agent may execute it —
                           unchanged from this system's original
                           behavior.
7  HUMAN_ONLY             - ENFORCED: like 6, always requires human
                           approval first — but even after approval,
                           the action is NEVER handed to executor.py
                           for automatic execution. Only the owner can
                           mark it complete by hand. Before this fix, a
                           task whose task_type was one of executor.py's
                           auto-executed HANDLERS and which reached
                           'assigned' status (e.g. right after human
                           approval) was picked up and run automatically
                           regardless of its permission_level_required
                           — silently defeating the "human-only"
                           guarantee for any level-7 task using an
                           executable task_type.
"""

LEVEL_NAMES = {
    0: "OBSERVE_ONLY",
    1: "RESEARCH",
    2: "RECOMMEND",
    3: "SIMULATE",
    4: "EXECUTE_LOW_RISK",
    5: "EXECUTE_WITH_BUDGET",
    6: "REQUIRE_APPROVAL",
    7: "HUMAN_ONLY",
}

MIN_LEVEL = 0
MAX_LEVEL = 7

# The two levels with real enforced behavior (see module docstring).
APPROVAL_REQUIRED_LEVEL = 6   # perm_required >= this always goes to human approval
HUMAN_ONLY_LEVEL = 7          # perm_required >= this is NEVER auto-executed, even post-approval

# The floor an agent must clear to be auto-assigned ANY executable
# task, regardless of how low the task's own required level is — an
# OBSERVE_ONLY (0) agent is never handed real work.
MIN_ASSIGNABLE_AGENT_LEVEL = 1


def level_name(level: int) -> str:
    return LEVEL_NAMES.get(level, f"UNKNOWN({level})")
