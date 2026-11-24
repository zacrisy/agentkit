"""agentkit — auditable automation primitives for long-running agents.

Modules:
  ledger        append-only SQLite journal with hash-chain tamper detection
  guard         fail-closed budget / allowlist / approval / circuit-breaker policy
  brain         LLM planner with strict JSON validation, retries and cost accounting
  survival      budget countdown ("how many days can this still run")
  orchestrator  multi-project daily planning loop
"""
__version__ = "0.1.0"
