"""共享数据模型（Project / Action），供 loop 与 daily 复用。"""
from __future__ import annotations

from dataclasses import dataclass, field

KINDS = ("research", "outreach", "build", "publish", "spend", "decide")


@dataclass
class Project:
    name: str
    hypothesis: str                     # "帮 X 解决 Y，收 Z 元，通过 W 渠道"
    weekly_budget_usd: float = 5.0
    daily_budget_usd: float = 1.5
    kill_criteria: list[str] = field(default_factory=list)


@dataclass
class Action:
    kind: str                           # research | outreach | build | publish | spend | decide
    detail: str
    cost_usd: float = 0.0
    domain: str = ""
    payoff: str = ""                    # 预期收益/验证目标
    success_metric: str = ""            # 24h 内怎么算成功
