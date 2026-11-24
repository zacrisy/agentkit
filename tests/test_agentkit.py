"""Behavioural tests. These encode the properties the library promises."""
import json
import sqlite3

import pytest

from agentkit import guard as guard_mod
from agentkit import ledger
from agentkit.brain import MockBrain, parse_plan
from agentkit.guard import Guard, GuardError, Policy
from agentkit.models import Action, Project
from agentkit.orchestrator import RunConfig, critic, run
from agentkit.survival import status


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(guard_mod, "STATE_PATH", tmp_path / "guard_state.json")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    yield tmp_path


PROJ = Project(name="demo", hypothesis="deliver X to Y for Z", daily_budget_usd=1.5)


# ---------------------------------------------------------------- ledger

def test_margin_tracks_revenue_and_cost():
    ledger.record_expense(0.5, project="demo", category="llm")
    ledger.record_revenue(200.0, project="demo")
    t = ledger.totals(project="demo")
    assert t["revenue"] == 200.0 and t["expense"] == 0.5
    assert t["margin"] == 199.5 and t["orders"] == 1


def test_hash_chain_detects_tampering():
    ledger.record_expense(0.1, project="demo")
    ledger.record_expense(0.2, project="demo")
    assert ledger.verify_chain()[0] is True
    with sqlite3.connect(ledger.DB_PATH) as conn:
        conn.execute("UPDATE journal SET amount_usd = 999 WHERE seq = 1")
        conn.commit()
    ok, msg = ledger.verify_chain()
    assert not ok


def test_recent_returns_parsed_meta():
    ledger.record_action("build", project="demo", meta={"detail": "ship it"})
    row = ledger.recent(limit=1, project="demo")[0]
    assert row["meta"]["detail"] == "ship it"


# ---------------------------------------------------------------- guard

def test_per_txn_cap_blocks():
    g = Guard(policy=Policy(max_per_txn_usd=1.0))
    with pytest.raises(GuardError):
        g.check_spend(2.0, domain="api.deepseek.com")


def test_domain_allowlist_blocks():
    g = Guard()
    with pytest.raises(GuardError):
        g.check_spend(0.01, domain="evil.example.com")


def test_approval_threshold_requires_human():
    g = Guard(policy=Policy(approval_threshold_usd=0.5))
    with pytest.raises(GuardError, match="human approval"):
        g.check_spend(1.0, domain="api.deepseek.com")
    g.approve("api.deepseek.com", 1.0)
    g.check_spend(1.0, domain="api.deepseek.com")


def test_daily_budget_halts_and_stays_halted():
    g = Guard(policy=Policy(max_per_txn_usd=10, max_per_day_usd=1.0, approval_threshold_usd=0))
    ledger.record_expense(0.9, project="demo")
    with pytest.raises(GuardError, match="daily budget"):
        g.check_spend(0.2, domain="api.deepseek.com")
    with pytest.raises(GuardError, match="halted"):
        g.check_spend(0.1, domain="api.deepseek.com")


def test_consecutive_failures_halt():
    g = Guard(policy=Policy(max_consecutive_failures=2))
    g.record_result(False)
    with pytest.raises(GuardError):
        g.record_result(False)


# ---------------------------------------------------------------- planner

def _payload(**over):
    a = {"kind": "research", "detail": "collect 10 leads", "cost_usd": 0.2,
         "domain": "api.deepseek.com", "payoff": "validate demand",
         "success_metric": "10 leads recorded"}
    a.update(over)
    return json.dumps({"reasoning": "why", "actions": [a], "questions_for_human": ["scope?"]})


def test_parse_plan_accepts_valid_and_fenced_json():
    assert parse_plan(_payload(), PROJ)[1] == ""
    assert parse_plan("```json\n" + _payload() + "\n```", PROJ)[1] == ""


@pytest.mark.parametrize("kwargs,fragment", [
    ({"kind": "hack"}, "Invalid action kind"),
    ({"success_metric": ""}, "missing"),
    ({"cost_usd": 99}, "exceeds"),
])
def test_parse_plan_rejects_bad_actions(kwargs, fragment):
    assert fragment in parse_plan(_payload(**kwargs), PROJ)[1]


def test_parse_plan_rejects_garbage():
    _, err = parse_plan("not json", PROJ)
    assert "JSON" in err or "json" in err


def test_personal_credentials_are_never_read_from_disk():
    """The planner must only ever use the environment variable."""
    from agentkit.brain import load_api_key
    assert load_api_key() is None


# ---------------------------------------------------------------- orchestrator

def test_critic_drops_unverifiable_and_over_budget():
    acts = [Action("research", "a", 1.0, success_metric="ok"),
            Action("build", "b", 1.0, success_metric="ok"),
            Action("outreach", "c", 0.0)]
    kept, dropped = critic(acts, PROJ)
    assert [a.detail for a in kept] == ["a"]
    assert len(dropped) == 2


def test_run_end_to_end_shadow_does_not_spend(tmp_path):
    cfg = RunConfig(projects=[PROJ], report_dir=tmp_path / "reports")
    path = run(cfg, mock=True, brain=MockBrain())
    assert path.exists()
    text = path.read_text()
    assert "Portfolio report" in text and "Survival" in text
    assert ledger.totals(project="demo")["expense"] == 0.0
    assert ledger.verify_chain()[0] is True


def test_one_broken_project_does_not_stop_others(tmp_path):
    class Exploding:
        def plan(self, project, ctx):
            from agentkit.brain import Plan
            if project.name == "broken":
                return Plan("", [], error="boom")
            return MockBrain().plan(project, ctx)

    bad = Project(name="broken", hypothesis="will fail", daily_budget_usd=1.0)
    cfg = RunConfig(projects=[PROJ, bad], report_dir=tmp_path / "reports")
    text = run(cfg, mock=True, brain=Exploding()).read_text()
    assert "planning failed" in text
    assert "## demo" in text


# ---------------------------------------------------------------- survival

def test_survival_none_without_spend():
    st = status(5.0)
    assert st["level"] == "ok"


def test_survival_counts_down_with_burn():
    ledger.record_expense(1.0, project="demo")
    st = status(0.5)
    assert st["remaining"] <= 0 or st["days_left"] is not None
