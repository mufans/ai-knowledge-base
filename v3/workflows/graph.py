"""LangGraph 工作流组装。

图结构::

    collect → analyze → review ──┬── (通过) ─────→ organize → save → END
                        ↑        │
                        │   (未通过, < 3轮)
                        │        ↓
                        └─── revise
                                 │
                        (未通过, >= 3轮)
                                 ↓
                            human_flag → END
"""

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中，支持 python workflows/graph.py 直接运行
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from langgraph.graph import END, StateGraph

from workflows.human_flag import human_flag_node
from workflows.nodes import (
    analyze_node,
    collect_node,
    organize_node,
    save_node,
)
from workflows.reviewer import review_node
from workflows.reviser import revise_node
from workflows.state import KBState


# ---------------------------------------------------------------------------
# 路由函数：review 之后的条件分支
# ---------------------------------------------------------------------------


def _route_after_review(state: KBState) -> str:
    """根据审核结果决定下一步。

    - 通过 → organize
    - 未通过 + 需要人工 → human_flag
    - 未通过 + 可继续 → revise
    """
    if state.get("review_passed", False):
        return "organize"
    if state.get("needs_human_review", False):
        return "human_flag"
    return "revise"


# ---------------------------------------------------------------------------
# 图构建
# ---------------------------------------------------------------------------


def build_graph() -> StateGraph:
    """组装并编译工作流图，返回可执行的 CompiledGraph。"""
    graph = StateGraph(KBState)

    # 添加节点
    graph.add_node("collect", collect_node)
    graph.add_node("analyze", analyze_node)
    graph.add_node("review", review_node)
    graph.add_node("revise", revise_node)
    graph.add_node("human_flag", human_flag_node)
    graph.add_node("organize", organize_node)
    graph.add_node("save", save_node)

    # 入口点
    graph.set_entry_point("collect")

    # 线性边
    graph.add_edge("collect", "analyze")
    graph.add_edge("analyze", "review")

    # 条件边：review → organize | revise | human_flag
    graph.add_conditional_edges(
        "review",
        _route_after_review,
        {"organize": "organize", "revise": "revise", "human_flag": "human_flag"},
    )

    # 修正后重新审核
    graph.add_edge("revise", "review")

    # 人工标记后结束
    graph.add_edge("human_flag", END)

    # 审核通过后整理并保存
    graph.add_edge("organize", "save")
    graph.add_edge("save", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = build_graph()

    # 初始状态
    initial_state: KBState = {
        "sources": [],
        "analyses": [],
        "articles": [],
        "review_feedback": "",
        "review_passed": False,
        "needs_human_review": False,
        "iteration": 0,
        "cost_tracker": {"total_tokens": 0, "calls": []},
    }

    print("=" * 56)
    print("  AI Knowledge Base 工作流启动")
    print("=" * 56)

    # 流式执行，逐节点打印输出
    for output in app.stream(initial_state, {"recursion_limit": 10}):
        for node_name, result in output.items():
            print(f"\n── {node_name} 输出 ──")
            if not result:
                print("  (完成)")
                continue
            if "sources" in result:
                print(f"  采集条数: {len(result['sources'])}")
            if "analyses" in result:
                print(f"  分析条数: {len(result['analyses'])}")
            if "articles" in result:
                print(f"  文章条数: {len(result['articles'])}")
            if "review_passed" in result:
                status = "通过" if result["review_passed"] else "未通过"
                print(f"  审核: {status}")
            if "iteration" in result:
                print(f"  迭代轮次: {result['iteration']}")
            if "cost_tracker" in result:
                tracker = result["cost_tracker"]
                print(f"  累计 tokens: {tracker.get('total_tokens', 0)}")

    print("\n" + "=" * 56)
    print("  工作流执行完毕")
    print("=" * 56)
