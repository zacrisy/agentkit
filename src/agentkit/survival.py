"""生存状态：给机器人一个"还能活几天"的倒计时。

来自那个 AI 求生实验的启发：明确的死亡线（余额耗尽/服务器关闭）是最强的驱动力。
对应到本项目：总预算 = 老板愿意为"试错"烧掉的钱；日耗 = 近 7 天的平均推理成本。

输出被 daily.py 的「生存状态」区块使用，也供 pulse 判断是否该降频。
"""
from __future__ import annotations

import time
from pathlib import Path




def status(total_budget_usd: float = 5.0) -> dict:
    from . import ledger

    rows = ledger.recent(limit=1000)
    if not rows:
        return {"total_budget": total_budget_usd, "spent": 0.0, "remaining": total_budget_usd,
                "daily_burn": 0.0, "days_left": None, "level": "ok"}

    ts = [r["ts"] for r in rows]
    try:
        oldest = time.mktime(time.strptime(min(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
        span_days = max((time.time() - oldest) / 86400, 1.0)
    except Exception:
        span_days = 1.0

    spent = sum(r["amount_usd"] for r in rows if r["kind"] == "expense")
    revenue = sum(r["amount_usd"] for r in rows
                  if r["kind"] == "revenue" and r.get("project") != "personal")
    burn = spent / span_days
    remaining = total_budget_usd + revenue - spent
    days_left = (remaining / burn) if burn > 0 else None

    if remaining <= 0:
        level = "dead"
    elif days_left is not None and days_left < 7:
        level = "critical"
    elif days_left is not None and days_left < 30:
        level = "warning"
    else:
        level = "ok"

    return {"total_budget": total_budget_usd, "spent": round(spent, 4),
            "revenue": round(revenue, 2), "personal_revenue": round(
                sum(r["amount_usd"] for r in rows
                    if r["kind"] == "revenue" and r.get("project") == "personal"), 2),
            "remaining": round(remaining, 4), "daily_burn": round(burn, 5),
            "days_left": None if days_left is None else round(days_left, 1), "level": level}


def line(st: dict) -> str:
    icon = {"ok": "🟢", "warning": "🟡", "critical": "🟠", "dead": "🔴"}.get(st["level"], "⚪")
    days = "∞" if st["days_left"] is None else f"{st['days_left']:.0f} 天"
    return (f"{icon} 预算余额 ${st['remaining']:.2f}（初始 ${st['total_budget']:.2f}"
            f"+项目收入 ${st['revenue']:.2f}−已花 ${st['spent']:.2f}）｜"
            f"日耗 ${st['daily_burn']:.4f}｜**还能跑 {days}**")


if __name__ == "__main__":
    print(line(status()))
