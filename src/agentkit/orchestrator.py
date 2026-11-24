"""Multi-project planning loop.

One *project* = one hypothesis about how to produce value. Each day the
orchestrator asks the planner for 3–5 concrete actions per project, validates
them, passes them through the guard, records everything in the ledger and emits
a human-readable report.

Design constraints that shaped this module:
  - Every planned action must carry an observable 24h success metric, otherwise
    it is dropped by the critic. Plans that cannot be checked are not plans.
  - Spend is checked *before* the action, fail-closed, and every real cost is
    journaled — including the cost of planning itself.
  - A run must survive partial failure: one broken project must not stop others.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import ledger
from .brain import DeepSeekBrain, MockBrain, Plan
from .guard import Guard, GuardError, Policy
from .models import Action, Project
from .survival import line as survival_line
from .survival import status as survival_status


@dataclass
class RunConfig:
    projects: list[Project]
    policy: Policy = field(default_factory=Policy)
    report_dir: Path = Path("reports")
    total_budget_usd: float = 5.0
    max_retries: int = 2


def critic(actions: list[Action], project: Project) -> tuple[list[Action], list[str]]:
    """Drop actions without a verifiable metric or that exceed the daily budget."""
    keep, dropped, spent = [], [], 0.0
    for a in actions:
        if not a.success_metric:
            dropped.append(f"{a.kind}: no 24h success metric")
            continue
        if spent + a.cost_usd > project.daily_budget_usd + 1e-9:
            dropped.append(f"{a.kind}: over daily budget")
            continue
        keep.append(a)
        spent += a.cost_usd
    return keep, dropped


def _context(project: Project) -> dict:
    today = time.strftime("%Y-%m-%d")
    return {
        "date": today,
        "budget": {"daily_usd": project.daily_budget_usd,
                   "weekly_usd": project.weekly_budget_usd},
        "ledger": {"today": ledger.totals(since=f"{today}T00:00:00", project=project.name),
                   "all_time": ledger.totals(project=project.name)},
        "recent_actions": ledger.recent(limit=12, project=project.name),
        "already_tried": [a["meta"].get("detail", "")
                          for a in ledger.recent(limit=30, project=project.name)
                          if a["kind"] == "action"
                          and a["meta"].get("detail")
                          and a["meta"].get("mode") == "live"][:10],
        "IMPORTANT": "Entries in recent_actions with executed=false were only PLANNED, "
                     "never carried out. Do not describe them as completed work.",
    }


def plan_project(brain, project: Project, cfg: RunConfig, mock: bool) -> Plan:
    ctx = _context(project)
    plan = brain.plan(project, ctx)
    if mock or "MISSING_CREDENTIAL" in (plan.error or ""):
        return plan
    for attempt in range(1, cfg.max_retries + 1):
        if not plan.error:
            break
        time.sleep(3 * attempt)
        retry_ctx = dict(ctx)
        retry_ctx["previous_error"] = plan.error
        retry_ctx["instruction"] = ("Previous output was invalid or empty. Output exactly "
                                    "one JSON object, no markdown, no prose.")
        plan = brain.plan(project, retry_ctx)
    return plan


def _usd(v: float) -> str:
    return f"${v:.4f}" if 0 < abs(v) < 0.01 else f"${v:.2f}"


def write_report(*, cfg: RunConfig, runs: list[dict], totals: dict,
                 per_project: dict, mode: str, model: str) -> Path:
    cfg.report_dir.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y-%m-%d")
    plan_cost = sum(r["plan"].cost_usd for r in runs)
    tin = sum(r["plan"].tokens_in for r in runs)
    tout = sum(r["plan"].tokens_out for r in runs)

    lines = [
        f"# Portfolio report {day}",
        "",
        f"> {len(runs)} project(s) ｜ mode `{mode}` ｜ model `{model}` ｜ "
        f"planning cost ${plan_cost:.4f} ({tin}+{tout} tokens)",
        "",
        f"**Survival**: {survival_line(survival_status(cfg.total_budget_usd))}",
        "",
        "## Overview",
        "",
        "| project | actions | blocked | cost | margin |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in runs:
        t = per_project.get(r["project"].name, {})
        lines.append(f"| {r['project'].name} | {len(r['kept'])} | {len(r['blocked'])} | "
                     f"${r['plan'].cost_usd:.4f} | {_usd(t.get('margin', 0))} |")
    lines.append("")

    for r in runs:
        p, plan = r["project"], r["plan"]
        lines += [f"## {p.name}", "", f"> **Hypothesis**: {p.hypothesis}", ""]
        if plan.error:
            lines += [f"⚠️ planning failed: `{plan.error}`", ""]
            continue
        for i, (a, res) in enumerate(zip(plan.actions, r["results"]), 1):
            lines.append(f"### {i}. [{a.kind}] {a.detail}")
            lines.append("")
            lines.append(f"- status: **{res.get('status', '?')}**"
                         + (f" — {res['why']}" if res.get("why") else ""))
            if a.payoff:
                lines.append(f"- why: {a.payoff}")
            lines.append(f"- 24h metric: {a.success_metric}")
            if a.cost_usd:
                lines.append(f"- cost: ${a.cost_usd:.2f}")
            lines.append("")
        if plan.reasoning:
            lines += [f"> {plan.reasoning}", ""]
        if plan.questions:
            lines += ["**Needs your call:**", ""] + [f"- [ ] {q}" for q in plan.questions] + [""]
        if r["blocked"] or r["dropped"]:
            lines += ["**Blocked / dropped:**", ""]
            lines += [f"- 🚫 {b}" for b in r["blocked"]]
            lines += [f"- ✂️ {d}" for d in r["dropped"]]
            lines += [""]

    lines += [
        "## Ledger (all projects)",
        "",
        "| revenue | cost | margin | orders |",
        "|---:|---:|---:|---:|",
        f"| {_usd(totals['revenue'])} | {_usd(totals['expense'])} | "
        f"{_usd(totals['margin'])} | {totals['orders']} |",
        "",
        "---",
        f"*generated {time.strftime('%Y-%m-%d %H:%M:%S')} by agentkit*",
    ]
    path = cfg.report_dir / f"{day}.md"
    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")
    (cfg.report_dir / "latest.md").write_text(text, encoding="utf-8")
    return path


def run(cfg: RunConfig, *, projects: list[str] | None = None,
        mock: bool = False, live: bool = False, brain=None) -> Path:
    chosen = [p for p in cfg.projects if not projects or p.name in projects]
    if not chosen:
        raise ValueError("no projects selected")
    brain = brain or (MockBrain() if mock else DeepSeekBrain())
    guard = Guard(policy=cfg.policy)
    mode = "live" if live else "shadow"

    runs, failed = [], 0
    for project in chosen:
        plan = plan_project(brain, project, cfg, mock)
        if plan.cost_usd > 0:
            ledger.record_expense(plan.cost_usd, project=project.name, category="llm-planning",
                                  meta={"tokens_in": plan.tokens_in, "tokens_out": plan.tokens_out})
        kept, dropped = critic(plan.actions, project)
        results, blocked = [], []
        for a in kept:
            if a.cost_usd > 0:
                try:
                    guard.check_spend(a.cost_usd, domain=a.domain or "api.deepseek.com")
                except GuardError as e:
                    blocked.append(f"{a.kind}: {e}")
                    results.append({"status": "BLOCKED", "why": str(e)})
                    continue
                if live:
                    ledger.record_expense(a.cost_usd, project=project.name, category=a.kind)
            ledger.record_action(a.kind, project=project.name, meta={
                "detail": a.detail, "cost_usd": a.cost_usd, "domain": a.domain,
                "payoff": a.payoff, "success_metric": a.success_metric, "mode": mode})
            results.append({"status": "QUEUED" if live else "SHADOW"})
        if plan.reasoning:
            ledger.record_action("decision", project=project.name,
                                 meta={"reasoning": plan.reasoning, "mode": mode})
        failed += 1 if plan.error else 0
        runs.append({"project": project, "plan": plan, "kept": kept, "dropped": dropped,
                     "results": results, "blocked": blocked})

    totals = ledger.totals()
    per_project = {p.name: ledger.totals(project=p.name) for p in chosen}
    return write_report(cfg=cfg, runs=runs, totals=totals, per_project=per_project,
                        mode=mode, model="mock" if mock else "deepseek")
