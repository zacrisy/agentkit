# agentkit

**Auditable automation primitives for long-running agents.**

Most agent frameworks optimise for *capability* — more tools, more autonomy.
This library optimises for the boring properties that decide whether an
autonomous system can be left running unattended: **every action is journaled,
every dollar is accounted for, and every limit fails closed.**

It is deliberately small (pure standard library, ~1k lines), because the parts
that matter here are not clever, they are *reliable*.

---

## Why

An agent that runs once can be reviewed by a human. An agent that runs every day
for months has three failure modes that benchmarks never surface:

1. **Unaccountable spend.** You cannot tell whether it earned or burned money.
2. **Unverifiable progress.** Plans get recorded as if they happened, and the
   next plan is built on work that was never done.
3. **Silent degradation.** A limit is enforced by a prompt, the model ignores
   it, and nothing stops it.

`agentkit` addresses each with a concrete mechanism: a hash-chained journal,
an explicit executed-vs-planned distinction, and a policy guard enforced in
code rather than in prose.

---

## Architecture

```
                    ┌──────────────────────────────────────────┐
                    │  orchestrator.run(cfg)                   │
                    │  for each project:                       │
                    │    plan → critic → guard → journal       │
                    └───────┬──────────────┬───────────────────┘
                            │              │
        ┌───────────────────▼──┐      ┌────▼─────────────────┐
        │ brain                │      │ guard                │
        │ LLM planner, strict  │      │ fail-closed policy:  │
        │ JSON schema, retries,│      │ per-txn / daily /    │
        │ cost accounting      │      │ monthly caps,        │
        └───────────────────┬──┘      │ allowlist, approval, │
                            │         │ circuit breaker      │
                            │         └────┬─────────────────┘
                            ▼              ▼
                    ┌──────────────────────────────────────────┐
                    │ ledger — append-only SQLite journal      │
                    │ each row hashes the previous one, so any │
                    │ edit to history is detectable            │
                    └──────────────────────────────────────────┘
                                        │
                                        ▼
                               survival: budget countdown
```

---

## Modules

| Module | Responsibility | Property it guarantees |
|---|---|---|
| `ledger` | append-only journal of spend, revenue, actions, decisions | tampering with history is detectable via `verify_chain()` |
| `guard` | per-transaction / daily / monthly caps, domain & payee allowlists, approval threshold, circuit breaker | violations raise; once halted it refuses everything until a human resets it |
| `brain` | LLM planner with strict schema validation, retry with backoff, token/cost accounting | malformed plans are rejected, never partially executed |
| `orchestrator` | multi-project daily loop, critic, report generation | one failing project does not stop the others; shadow mode spends nothing |
| `survival` | budget countdown ("how many days can this still run") | the operator sees depletion before it happens |

---

## Quickstart

```bash
python -m pip install -e .
```

```python
from pathlib import Path
from agentkit.guard import Policy
from agentkit.models import Project
from agentkit.orchestrator import RunConfig, run
from agentkit.brain import MockBrain

cfg = RunConfig(
    projects=[Project(
        name="demo",
        hypothesis="Deliver a 48h code review to indie developers for $200.",
        daily_budget_usd=1.5,
        kill_criteria=["no paying customer in 8 weeks"],
    )],
    policy=Policy(max_per_txn_usd=2.0, approval_threshold_usd=0.5),
    report_dir=Path("reports"),
    total_budget_usd=5.0,
)

path = run(cfg, mock=True, brain=MockBrain())   # dry run, spends nothing
print(path.read_text())
```

With a real planner:

```bash
export DEEPSEEK_API_KEY=sk-...
```

```python
from agentkit.brain import DeepSeekBrain
run(cfg, brain=DeepSeekBrain(model="deepseek-flash", reasoning_effort="low"))
```

---

## The three design decisions worth explaining

**1. Write-ahead accounting, including the cost of thinking.**
The planner call itself is journaled as an expense before its actions are
considered. A system that only counts "external" actions will silently drift
into unprofitable loops driven by planning cost.

**2. `executed` is a first-class distinction.**
Shadow-mode plans are journalled with `mode=shadow`; only `mode=live` rows are
treated as "already tried". This exists because of a real bug: a planner that
reads its own unimplemented plans starts reporting work that never happened and
compounds the error daily.

**3. Limits are code, not instructions.**
`Policy` is enforced in `Guard.check_spend` and raises `GuardError`. Nothing in
`agentkit` catches that error and continues — that decision belongs to the
caller, and it should be visible in the diff.

---

## Testing

```bash
python -m pytest -q     # 19 tests
```

The suite encodes the guarantees, not the implementation: tamper detection,
fail-closed halting, approval gating, malformed-plan rejection, partial-failure
isolation, and "shadow mode must not spend".

---

## What this is *not*

- Not an agent framework — it has no tools, no memory, no planner prompts beyond
  one. It is the accounting and safety layer you put **under** one.
- Not a money-making bot. The economics live entirely in the projects you
  describe; this library only makes them measurable and safe.
- Not multi-tenant or networked. Single host, single user, SQLite on disk.

---

## 中文摘要

`agentkit` 是一套给"长期无人值守运行的智能体"用的**可审计基础设施**：

- **账本**：追加写入的 SQLite 流水，每行哈希前一行，历史被改动可立刻检出
- **围栏**：单笔/单日/单月预算上限、域名白名单、超阈值人工审批、连续失败熔断，全部 fail-closed
- **规划器**：LLM 输出严格校验 JSON，失败重试并记账（含"思考"本身的成本）
- **编排**：多项目并行，单条失败不影响其他；影子模式不产生任何支出
- **生存倒计时**：按当前燃烧速度算出"还能跑几天"

设计上的关键一点：**区分"计划过"和"执行过"**（`mode=shadow` vs `mode=live`）。
这是踩过坑之后加的——规划器会把没做的事当成做完的事，然后每天在错误前提上叠加。

MIT License.
