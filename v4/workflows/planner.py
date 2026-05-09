"""Planner Agent：根据目标采集量选择执行策略，只规划不执行。"""

import os

from workflows.state import KBState

# ---------------------------------------------------------------------------
# 三档策略定义
# ---------------------------------------------------------------------------

_STRATEGIES = {
    "lite": {
        "per_source_limit": 5,
        "relevance_threshold": 0.7,
        "max_iterations": 1,
        "rationale": (
            "目标采集量较小（<10），采用激进策略：高阈值快速筛选，"
            "单轮审核即可，节省 token 开销。"
        ),
    },
    "standard": {
        "per_source_limit": 10,
        "relevance_threshold": 0.5,
        "max_iterations": 2,
        "rationale": (
            "目标采集量适中（10-19），采用均衡策略：适度阈值保证覆盖面，"
            "允许两轮审核循环兼顾质量与成本。"
        ),
    },
    "full": {
        "per_source_limit": 20,
        "relevance_threshold": 0.4,
        "max_iterations": 3,
        "rationale": (
            "目标采集量较大（>=20），采用保守策略：低阈值扩大捕获面，"
            "三轮审核循环确保大规模数据的质量底线。"
        ),
    },
}

_ENV_KEY = "PLANNER_TARGET_COUNT"
_DEFAULT_TARGET = 30


# ---------------------------------------------------------------------------
# plan_strategy
# ---------------------------------------------------------------------------


def plan_strategy(target_count: int | None = None) -> dict:
    """根据目标采集量返回策略 dict。

    Args:
        target_count: 目标采集条数。为 None 时从环境变量
            ``PLANNER_TARGET_COUNT`` 读取，默认 10。

    Returns:
        策略 dict，包含 per_source_limit / relevance_threshold /
        max_iterations / rationale / target_count / strategy_name。
    """
    if target_count is None:
        raw = os.environ.get(_ENV_KEY, "")
        target_count = int(raw) if raw else _DEFAULT_TARGET

    if target_count < 10:
        name = "lite"
    elif target_count < 20:
        name = "standard"
    else:
        name = "full"

    strategy = _STRATEGIES[name]
    return {
        "strategy_name": name,
        "target_count": target_count,
        "per_source_limit": strategy["per_source_limit"],
        "relevance_threshold": strategy["relevance_threshold"],
        "max_iterations": strategy["max_iterations"],
        "rationale": strategy["rationale"],
    }


# ---------------------------------------------------------------------------
# planner_node（LangGraph 节点包装）
# ---------------------------------------------------------------------------


def planner_node(state: KBState) -> dict:
    """规划节点：生成执行策略写入 state["plan"]，下游节点按策略调整行为。"""
    plan = plan_strategy()
    print(
        f"[Planner] 策略={plan['strategy_name']}, "
        f"target={plan['target_count']}, "
        f"threshold={plan['relevance_threshold']}, "
        f"max_iter={plan['max_iterations']}"
    )
    return {"plan": plan}
