"""LLM planner: turns project hypothesis + ledger state into concrete, verifiable actions.

后端：
  DeepSeekBrain —— 直连 DeepSeek OpenAI 兼容接口，JSON 模式，成本记账（默认）
  MockBrain     —— 离线确定性输出，测试 / 无 key 时用

安全约定：API key 只在内存里出现，**绝不写日志、绝不落盘**。
API key is read from the DEEPSEEK_API_KEY environment variable only.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .models import KINDS, Action, Project


SYSTEM_PROMPT = """You are the operations planner for a one-person venture. You have one principal
and no other staff. Your only objective is to make this project produce its first real revenue,
within budget.

Hard constraints (violating any of them invalidates your output):
1. Only propose actions that need no new paid account, do not intrude on anyone's systems,
   and are legal.
2. Every action must be verifiable within 24 hours. success_metric must be observable.
3. The sum of all action costs must not exceed the daily budget.
4. Do not repeat an action that was already tried in the last 7 days without progress,
   unless you can explain why this attempt differs.
5. Never invent facts or figures. Anything uncertain or requiring the principal's decision
   belongs in questions_for_human.
6. Return 3-5 actions. Fewer and concrete beats many and vague.

Output a single JSON object. No markdown fences, no prose.
{
  "reasoning": "<=200 chars: why these actions today",
  "actions": [
    {"kind": "research|outreach|build|publish|spend|decide",
     "detail": "one sentence: what to do and what the deliverable is",
     "cost_usd": 0.0,
     "domain": "API domain if it costs money, otherwise empty string",
     "payoff": "what this is expected to validate or produce",
     "success_metric": "observable pass/fail criterion within 24h"}
  ],
  "questions_for_human": ["at most 3 decisions the principal must make"]
}"""


# ------------------------------------------------------------------ key 解析

def load_api_key() -> str | None:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    return key.strip() or None


# ------------------------------------------------------------------ 结果对象

@dataclass
class Plan:
    reasoning: str
    actions: list[Action]
    questions: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "reasoning": self.reasoning,
            "actions": [a.__dict__ for a in self.actions],
            "questions": self.questions,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 6),
            "error": self.error,
        }


# ------------------------------------------------------------------ 后端

class MockBrain:
    """离线确定性规划器，用于测试与无 key 演示。"""

    def plan(self, project: Project, context: dict) -> Plan:
        return Plan(
            reasoning="[mock] 影子模式下验证流水线：先确认需求，再做可交付样机。",
            actions=[
                Action("research", "找 10 个潜在客户，确认他们是否真在乎这个问题",
                       cost_usd=0.0, payoff="验证需求真伪", success_metric="10 条真实线索，≥3 条有明确痛点"),
                Action("build", "做 1 个 30 分钟内可交付的样机，用于报价",
                       cost_usd=0.0, payoff="可报价", success_metric="样机可演示，且老板认可质量"),
                Action("outreach", "起草 3 封外联邮件（进待审队列，不发送）",
                       cost_usd=0.0, payoff="获取 1 次回复", success_metric="3 封草稿完成并待审"),
            ],
            questions=["[mock] 目标客户是哪个细分？预算上限是否维持？"],
        )


class DeepSeekBrain:
    def __init__(self, *, model: str = "deepseek-flash", base_url: str = "https://api.deepseek.com/v1",
                 temperature: float = 0.4, max_tokens: int = 3000, timeout: int = 120,
                 price_in_per_1m: float = 0.15, price_out_per_1m: float = 0.60,
                 reasoning_effort: str = "low"):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.price_in = price_in_per_1m
        self.price_out = price_out_per_1m
        self.reasoning_effort = reasoning_effort

    # -- http ---------------------------------------------------------
    def _chat(self, messages: list[dict], key: str) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode())

    # -- 主入口 -------------------------------------------------------
    def plan(self, project: Project, context: dict) -> Plan:
        key = load_api_key()
        if not key:
            return Plan("", [], error="MISSING_CREDENTIAL: DEEPSEEK_API_KEY environment variable is not set")

        user_msg = (
            f"Today is {time.strftime('%Y-%m-%d')}.\n"
            f"Project: {project.name}\n"
            f"Hypothesis: {project.hypothesis}\n"
            f"Kill criteria: {json.dumps(project.kill_criteria, ensure_ascii=False)}\n"
            f"Daily budget: ${project.daily_budget_usd}\n\n"
            f"Context (ledger / recent actions / lessons):\n"
            f"{json.dumps(context, ensure_ascii=False, indent=1)}"
        )
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg}]

        try:
            data = self._chat(messages, key)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            return Plan("", [], error=f"HTTP {e.code}: {detail}")
        except Exception as e:  # noqa: BLE001 - 网络层任何异常都当作可重试错误
            return Plan("", [], error=f"{type(e).__name__}: {e}")

        usage = data.get("usage") or {}
        tin = int(usage.get("prompt_tokens", 0))
        tout = int(usage.get("completion_tokens", 0))
        cost = tin / 1e6 * self.price_in + tout / 1e6 * self.price_out

        choice = (data.get("choices") or [{}])[0] or {}
        msg = choice.get("message") or {}
        finish = choice.get("finish_reason")
        content = (msg.get("content") or "").strip()

        # 推理模型可能把内容全放进 reasoning_content，正文为空 → 兜底提取 JSON
        if not content:
            rc = msg.get("reasoning_content") or ""
            m = re.search(r"\{.*\}", rc, re.S)
            if m:
                content = m.group(0)
        if not content:
            plan = Plan("", [], tokens_in=tin, tokens_out=tout, cost_usd=cost,
                        error=f"empty response (finish={finish}, {tout} output tokens consumed "
                              "by reasoning). Retry, or raise max_tokens / lower reasoning_effort")
            return plan
        plan, err = parse_plan(content, project)
        plan.tokens_in, plan.tokens_out, plan.cost_usd = tin, tout, cost
        if err:
            plan.error = err
        return plan


# ------------------------------------------------------------------ 解析与校验

def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    return text


def parse_plan(raw: str, project: Project) -> tuple[Plan, str]:
    """严格校验模型输出；不合法就返回空计划和错误原因（由调用方决定重试/降级）。"""
    try:
        obj = json.loads(_strip_fence(raw))
    except json.JSONDecodeError as e:
        return Plan("", [], error=f"JSON parse failed: {e}"), f"JSON 解析失败: {e}"

    actions: list[Action] = []
    total = 0.0
    for item in obj.get("actions", [])[:5]:
        kind = str(item.get("kind", "")).strip()
        if kind not in KINDS:
            msg = f"Invalid action kind: {kind}"
            return Plan("", [], error=msg), msg
        detail = str(item.get("detail", "")).strip()
        metric = str(item.get("success_metric", "")).strip()
        if not detail or not metric:
            msg = f"action missing detail/success_metric: {item}"
            return Plan("", [], error=msg), msg
        cost = float(item.get("cost_usd", 0) or 0)
        total += cost
        actions.append(Action(
            kind=kind, detail=detail, cost_usd=cost,
            domain=str(item.get("domain", "")).strip(),
            payoff=str(item.get("payoff", "")).strip(),
            success_metric=metric,
        ))

    if not actions:
        msg = "model returned no actions"
        return Plan("", [], error=msg), msg
    if total > project.daily_budget_usd + 1e-9:
        msg = f"total cost ${total:.2f} exceeds daily budget ${project.daily_budget_usd}"
        return Plan("", [], error=msg), msg

    plan = Plan(
        reasoning=str(obj.get("reasoning", "")).strip()[:400],
        actions=actions,
        questions=[str(q) for q in obj.get("questions_for_human", [])[:3]],
    )
    return plan, ""
