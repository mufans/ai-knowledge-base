"""多 Agent 预算守卫：追踪 LLM 调用成本，超预算时熔断。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class BudgetExceededError(Exception):
    """超出预算上限时抛出。"""


# ---------------------------------------------------------------------------
# CostRecord
# ---------------------------------------------------------------------------


@dataclass
class CostRecord:
    """记录单次 LLM 调用的用量和成本。"""

    timestamp: str
    node_name: str
    prompt_tokens: int
    completion_tokens: int
    cost_yuan: float
    model: str = ""


# ---------------------------------------------------------------------------
# CostGuard
# ---------------------------------------------------------------------------


class CostGuard:
    """多 Agent 预算守卫：追踪成本、预警、超限熔断。

    Args:
        budget_yuan: 总预算上限（元）。
        alert_threshold: 预警触发比例（0-1），累计成本占预算的比例达到此值时
            发出 warning。
        input_price_per_million: 输入 token 单价（元 / 百万 token）。
        output_price_per_million: 输出 token 单价（元 / 百万 token）。
    """

    def __init__(
        self,
        budget_yuan: float = 1.0,
        alert_threshold: float = 0.8,
        input_price_per_million: float = 1.0,
        output_price_per_million: float = 2.0,
    ) -> None:
        self.budget_yuan = budget_yuan
        self.alert_threshold = alert_threshold
        self.input_price_per_million = input_price_per_million
        self.output_price_per_million = output_price_per_million
        self._records: list[CostRecord] = []

    # -- 成本计算 -----------------------------------------------------------

    def _estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """根据 token 用量和单价估算单次调用成本。"""
        return (
            prompt_tokens * self.input_price_per_million / 1_000_000
            + completion_tokens * self.output_price_per_million / 1_000_000
        )

    # -- 汇总属性 -----------------------------------------------------------

    @property
    def total_prompt_tokens(self) -> int:
        return sum(r.prompt_tokens for r in self._records)

    @property
    def total_completion_tokens(self) -> int:
        return sum(r.completion_tokens for r in self._records)

    @property
    def total_cost_yuan(self) -> float:
        return sum(r.cost_yuan for r in self._records)

    # -- 核心方法 -----------------------------------------------------------

    def record(
        self,
        node_name: str,
        usage: dict[str, int],
        model: str = "",
    ) -> CostRecord:
        """记录一次 LLM 调用。

        Args:
            node_name: 调用来源节点名。
            usage: token 用量 ``{"prompt_tokens": int, "completion_tokens": int}``。
            model: 模型标识。

        Returns:
            本次调用对应的 ``CostRecord``。
        """
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        cost = self._estimate_cost(prompt_tokens, completion_tokens)

        rec = CostRecord(
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            node_name=node_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_yuan=cost,
            model=model,
        )
        self._records.append(rec)
        return rec

    def check(self) -> dict[str, Any]:
        """检查预算状态。

        Returns:
            包含 status / total_cost / budget / usage_ratio / message 的 dict。

        Raises:
            BudgetExceededError: 累计成本超出预算上限。
        """
        total = self.total_cost_yuan
        ratio = total / self.budget_yuan if self.budget_yuan else 0.0

        if ratio >= 1.0:
            raise BudgetExceededError(
                f"预算已超限: ¥{total:.4f} / ¥{self.budget_yuan:.4f} "
                f"({ratio:.1%})"
            )

        if ratio >= self.alert_threshold:
            return {
                "status": "warning",
                "total_cost": round(total, 6),
                "budget": self.budget_yuan,
                "usage_ratio": round(ratio, 4),
                "message": (
                    f"预算预警: 已用 ¥{total:.4f} / ¥{self.budget_yuan:.4f} "
                    f"({ratio:.1%})，请控制调用频率"
                ),
            }

        return {
            "status": "ok",
            "total_cost": round(total, 6),
            "budget": self.budget_yuan,
            "usage_ratio": round(ratio, 4),
            "message": "预算充足",
        }

    def get_report(self) -> dict[str, Any]:
        """生成成本报告，按节点分组统计。

        Returns:
            包含 total_summary + by_node 的 dict。
        """
        by_node: dict[str, dict[str, Any]] = {}
        for rec in self._records:
            entry = by_node.setdefault(
                rec.node_name,
                {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cost_yuan": 0.0,
                },
            )
            entry["calls"] += 1
            entry["prompt_tokens"] += rec.prompt_tokens
            entry["completion_tokens"] += rec.completion_tokens
            entry["cost_yuan"] = round(entry["cost_yuan"] + rec.cost_yuan, 6)

        return {
            "total_summary": {
                "calls": len(self._records),
                "prompt_tokens": self.total_prompt_tokens,
                "completion_tokens": self.total_completion_tokens,
                "cost_yuan": round(self.total_cost_yuan, 6),
                "budget_yuan": self.budget_yuan,
            },
            "by_node": by_node,
        }

    def save_report(self, path: str | None = None) -> Path:
        """将成本报告保存为 JSON 文件。

        Args:
            path: 目标路径。为 None 时保存到当前目录下的
                ``cost_report_{timestamp}.json``。

        Returns:
            实际写入的文件路径。
        """
        filepath = Path(
            path
            or f"cost_report_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        filepath.write_text(
            json.dumps(self.get_report(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return filepath


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    passed = 0
    failed = 0

    def _assert(condition: bool, label: str) -> None:
        global passed, failed
        if condition:
            passed += 1
            print(f"  PASS  {label}")
        else:
            failed += 1
            print(f"  FAIL  {label}")

    # ---- 测试 1: 成本追踪正确 ----
    print("\n[Test 1] 成本追踪")
    guard = CostGuard(
        budget_yuan=2.0,
        input_price_per_million=1.0,
        output_price_per_million=2.0,
    )
    # 500k prompt + 250k completion = 0.5 + 0.5 = 1.0 元
    guard.record("analyze", {"prompt_tokens": 500_000, "completion_tokens": 250_000}, model="deepseek-chat")

    _assert(guard.total_prompt_tokens == 500_000, "total_prompt_tokens == 500k")
    _assert(guard.total_completion_tokens == 250_000, "total_completion_tokens == 250k")
    _assert(abs(guard.total_cost_yuan - 1.0) < 1e-9, "total_cost_yuan == 1.0")

    result = guard.check()
    _assert(result["status"] == "ok", "单次调用未超预算 → ok")

    # ---- 测试 2: 预警阈值触发 ----
    print("\n[Test 2] 预警阈值")
    guard2 = CostGuard(
        budget_yuan=1.0,
        alert_threshold=0.8,
        input_price_per_million=1.0,
        output_price_per_million=2.0,
    )
    # 400k prompt + 200k completion = 0.4 + 0.4 = 0.8 → 刚好到 80%
    guard2.record("analyze", {"prompt_tokens": 400_000, "completion_tokens": 200_000})

    result = guard2.check()
    _assert(result["status"] == "warning", f"80% 阈值触发 warning（实际 {result['usage_ratio']:.2%}）")
    _assert("预警" in result["message"], "message 包含 '预警'")

    # 再加一小笔仍为 warning
    guard2.record("review", {"prompt_tokens": 10_000, "completion_tokens": 5_000})
    result = guard2.check()
    _assert(
        result["status"] == "warning",
        f"85% 仍为 warning（实际 {result['usage_ratio']:.2%}）",
    )

    # ---- 测试 3: 超预算抛异常 ----
    print("\n[Test 3] 预算超限")
    guard3 = CostGuard(
        budget_yuan=0.01,
        input_price_per_million=1.0,
        output_price_per_million=2.0,
    )
    # 10k prompt + 10k completion = 0.01 + 0.02 = 0.03 > 0.01
    guard3.record("analyze", {"prompt_tokens": 10_000, "completion_tokens": 10_000})

    try:
        guard3.check()
        _assert(False, "应抛出 BudgetExceededError 但没有")
    except BudgetExceededError as e:
        _assert(True, f"正确抛出 BudgetExceededError: {e}")

    # ---- 测试 4: get_report 按节点分组 ----
    print("\n[Test 4] 成本报告分组")
    guard4 = CostGuard(budget_yuan=10.0)
    guard4.record("analyze", {"prompt_tokens": 100, "completion_tokens": 50})
    guard4.record("analyze", {"prompt_tokens": 200, "completion_tokens": 100})
    guard4.record("review", {"prompt_tokens": 50, "completion_tokens": 25})

    report = guard4.get_report()
    _assert(report["total_summary"]["calls"] == 3, "总调用次数 == 3")
    _assert(report["by_node"]["analyze"]["calls"] == 2, "analyze 调用次数 == 2")
    _assert(report["by_node"]["analyze"]["prompt_tokens"] == 300, "analyze prompt_tokens == 300")
    _assert(report["by_node"]["review"]["calls"] == 1, "review 调用次数 == 1")

    # ---- 测试 5: save_report 写文件 ----
    print("\n[Test 5] 保存报告")
    guard5 = CostGuard(budget_yuan=10.0)
    guard5.record("test", {"prompt_tokens": 100, "completion_tokens": 50})

    p = guard5.save_report("test_cost_report.json")
    _assert(p.exists(), "报告文件已创建")
    _assert("total_summary" in json.loads(p.read_text()), "文件内容包含 total_summary")
    p.unlink()  # 清理

    # ---- 汇总 ----
    print(f"\n{'=' * 40}")
    print(f"  结果: {passed} passed, {failed} failed")
    print(f"{'=' * 40}")
