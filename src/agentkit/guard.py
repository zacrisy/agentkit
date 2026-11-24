"""Fail-closed spend and action guard.

The important property is that limits are enforced by code, not by asking a
model to behave. Any violation raises; nothing "continues anyway".
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import ledger

STATE_PATH = Path(os.environ.get("AGENTKIT_GUARD_STATE", Path(__file__).with_name("guard_state.json")))


class GuardError(RuntimeError):
    """Raised when a policy blocks an action. Never swallow and continue."""


@dataclass
class Policy:
    max_per_txn_usd: float = 2.0
    max_per_day_usd: float = 10.0
    max_per_month_usd: float = 100.0
    approval_threshold_usd: float = 0.5
    max_daily_loss_usd: float = 20.0
    max_consecutive_failures: int = 3
    domain_allowlist: tuple[str, ...] = ("api.deepseek.com", "api.openai.com")
    payee_allowlist: tuple[str, ...] = ()


@dataclass
class Guard:
    policy: Policy = field(default_factory=Policy)
    _state: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if STATE_PATH.exists():
            self._state = json.loads(STATE_PATH.read_text())
        self._state.setdefault("approved", [])
        self._state.setdefault("failures", 0)
        self._state.setdefault("halted", False)
        self._state.setdefault("halt_reason", "")

    def _save(self) -> None:
        STATE_PATH.write_text(json.dumps(self._state, ensure_ascii=False, indent=2))

    def halt(self, reason: str) -> None:
        self._state.update(halted=True, halt_reason=reason)
        self._save()
        ledger.record_action("HALT", meta={"reason": reason})
        raise GuardError(f"halted: {reason}")

    def reset(self) -> None:
        self._state.update(halted=False, halt_reason="", failures=0, approved=[])
        self._save()

    def approve(self, payee_or_domain: str, amount_usd: float) -> None:
        self._state["approved"].append(f"{payee_or_domain}:{amount_usd:.2f}")
        self._save()

    def check_spend(self, amount_usd: float, *, payee: str = "", domain: str = "") -> None:
        if self._state["halted"]:
            raise GuardError(f"halted ({self._state['halt_reason']}); refusing to act")
        p = self.policy
        if amount_usd <= 0:
            raise ValueError("amount must be positive")
        if amount_usd > p.max_per_txn_usd:
            raise GuardError(f"per-transaction cap: ${amount_usd:.2f} > ${p.max_per_txn_usd}")
        if domain and domain not in p.domain_allowlist:
            raise GuardError(f"domain not allowlisted: {domain}")
        if payee and payee not in p.payee_allowlist:
            raise GuardError(f"payee not allowlisted: {payee}")

        day, month = time.strftime("%Y-%m-%d"), time.strftime("%Y-%m")
        today = ledger.totals(since=f"{day}T00:00:00")
        if today["expense"] + amount_usd > p.max_per_day_usd:
            self.halt(f"daily budget exceeded: ${today['expense']:.2f} + ${amount_usd:.2f}")
        if ledger.totals(since=f"{month}-01T00:00:00")["expense"] + amount_usd > p.max_per_month_usd:
            self.halt(f"monthly budget exceeded: +${amount_usd:.2f}")
        if today["margin"] < -p.max_daily_loss_usd:
            self.halt(f"daily loss ${-today['margin']:.2f} over ${p.max_daily_loss_usd}")

        if amount_usd > p.approval_threshold_usd:
            token = f"{payee or domain}:{amount_usd:.2f}"
            if token not in self._state["approved"]:
                raise GuardError(f"needs human approval: ${amount_usd:.2f} -> {payee or domain}")

    def record_result(self, ok: bool) -> None:
        self._state["failures"] = 0 if ok else self._state["failures"] + 1
        self._save()
        if self._state["failures"] >= self.policy.max_consecutive_failures:
            self.halt(f"{self._state['failures']} consecutive failures")
