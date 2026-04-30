"""修正节点：根据 review_feedback 修正 analyses。"""

import json

from workflows.nodes import accumulate_usage, chat_json
from workflows.state import KBState

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_REVISE_SYSTEM = (
    "你是知识库质量修正专家。根据审核反馈，逐条修正以下知识条目。\n"
    "保持条目数量不变，仅根据反馈改进内容质量。\n\n"
    "每条输出 JSON 格式：\n"
    "{\n"
    '  "source_url": "原 URL",\n'
    '  "title": "标题",\n'
    '  "summary": "修正后的中文摘要（100-200字）",\n'
    '  "tags": ["标签1", "标签2"],\n'
    '  "score": 0.85,\n'
    '  "category": "分类",\n'
    '  "stars": 1234,\n'
    '  "language": "Python"\n'
    "}\n\n"
    "输出一个 JSON 数组，包含所有修正后的条目。只输出 JSON 数组。"
)


# ---------------------------------------------------------------------------
# revise_node
# ---------------------------------------------------------------------------


def revise_node(state: KBState) -> dict:
    """修正节点：根据 review_feedback 调 LLM 改写 analyses。

    - temperature=0.4（允许创造性改写）
    - analyses 或 feedback 空时跳过，返回 {}
    """
    analyses = state.get("analyses", [])
    feedback = state.get("review_feedback", "")
    cost_tracker = state.get("cost_tracker", {})

    if not analyses or not feedback:
        print("[ReviseNode] analyses 或 feedback 为空，跳过修正")
        return {}

    print(f"[ReviseNode] 开始修正 {len(analyses)} 条 analyses...")

    # 序列化 analyses 为 JSON 供 LLM 阅读
    analyses_json = json.dumps(analyses, ensure_ascii=False, indent=2)

    prompt = (
        f"## 审核反馈\n{feedback}\n\n"
        f"## 待修正条目（共 {len(analyses)} 条）\n{analyses_json}"
    )

    result, usage = chat_json(prompt, system=_REVISE_SYSTEM, temperature=0.4)
    accumulate_usage(cost_tracker, usage, node="revise")

    # LLM 应返回列表；兼容单条 dict 被包装成列表
    if isinstance(result, dict):
        improved = [result]
    elif isinstance(result, list):
        improved = result
    else:
        print(f"[ReviseNode] LLM 返回类型异常（{type(result).__name__}），保留原数据")
        return {"cost_tracker": cost_tracker}

    # 保持条目数量一致：若 LLM 少返回则补原数据，多返回则截断
    if len(improved) < len(analyses):
        improved.extend(analyses[len(improved):])
    elif len(improved) > len(analyses):
        improved = improved[: len(analyses)]

    # 补齐缺失字段（用原 analyses 对应位置兜底）
    for i, item in enumerate(improved):
        original = analyses[i]
        for key in ("source_url", "title", "stars", "language"):
            item.setdefault(key, original.get(key, ""))

    print(f"[ReviseNode] 修正完成，共 {len(improved)} 条")
    return {"analyses": improved, "cost_tracker": cost_tracker}
