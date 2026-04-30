"""审核节点：对 analyses 进行五维度评分。"""

from workflows.nodes import accumulate_usage, chat_json
from workflows.state import KBState

# ---------------------------------------------------------------------------
# 五维度权重配置
# ---------------------------------------------------------------------------

_DIMENSIONS = {
    "summary_quality": 0.25,
    "technical_depth": 0.25,
    "relevance": 0.20,
    "originality": 0.15,
    "formatting": 0.15,
}

_PASS_THRESHOLD = 7.0
_MAX_REVIEWS = 5  # 最多审核条数，控制 token 消耗

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_REVIEW_SYSTEM = (
    "你是知识库质量审核专家。请从以下五个维度对知识条目逐项评分（1-10 整数）：\n"
    "1. summary_quality（摘要质量）：摘要是否准确、完整、有信息量\n"
    "2. technical_depth（技术深度）：是否包含具体技术细节，而非泛泛而谈\n"
    "3. relevance（相关性）：内容与 AI/LLM 领域的关联程度\n"
    "4. originality（原创性）：是否有独特见解或新颖视角\n"
    "5. formatting（格式规范）：字段完整性、标签规范性\n\n"
    "输出 JSON 格式：\n"
    "{\n"
    '  "scores": {\n'
    '    "summary_quality": 8,\n'
    '    "technical_depth": 7,\n'
    '    "relevance": 9,\n'
    '    "originality": 6,\n'
    '    "formatting": 8\n'
    '  },\n'
    '  "feedback": "具体修改建议"\n'
    "}\n"
    "只输出 JSON。scores 中每个值必须是 1-10 的整数。"
)


# ---------------------------------------------------------------------------
# 加权总分计算（不信任模型算术）
# ---------------------------------------------------------------------------


def _weighted_score(scores: dict) -> float:
    """根据五维度分数和权重计算加权总分。"""
    total = 0.0
    for dim, weight in _DIMENSIONS.items():
        raw = scores.get(dim, 5)
        # 钳位到 [1, 10]
        clamped = max(1, min(10, int(raw)))
        total += clamped * weight
    return round(total, 2)


# ---------------------------------------------------------------------------
# review_node
# ---------------------------------------------------------------------------


def review_node(state: KBState) -> dict:
    """审核节点：对 state['analyses'] 五维度评分，加权总分 >= 7.0 通过。

    - 只审核前 5 条 analyses（控 token）
    - temperature=0.1（评分一致性）
    - LLM 调用失败时自动通过（不阻塞流程）
    - iteration >= 3 标记 needs_human_review，路由到 human_flag 节点
    """
    print("[ReviewNode] 开始审核...")

    iteration = state.get("iteration", 0)
    analyses = state.get("analyses", [])
    cost_tracker = state.get("cost_tracker", {})

    # iteration >= 3：不再强制通过，而是标记需要人工审核
    if iteration >= 3:
        print(f"[ReviewNode] 已达第 {iteration} 轮，转人工审核")
        return {
            "review_passed": False,
            "needs_human_review": True,
            "review_feedback": state.get("review_feedback", ""),
            "iteration": iteration + 1,
            "cost_tracker": cost_tracker,
        }

    # 无数据直接通过
    if not analyses:
        print("[ReviewNode] 无 analyses 数据，自动通过")
        return {
            "review_passed": True,
            "review_feedback": "",
            "iteration": iteration + 1,
            "cost_tracker": cost_tracker,
        }

    # 只取前 5 条
    sample = analyses[:_MAX_REVIEWS]

    # 拼接审核文本
    analyses_text = "\n---\n".join(
        f"标题：{a.get('title', '')}\n"
        f"摘要：{a.get('summary', '')}\n"
        f"标签：{a.get('tags', [])}\n"
        f"分类：{a.get('category', '')}\n"
        f"评分：{a.get('score', '')}"
        for a in sample
    )

    # 调用 LLM，失败时自动通过
    try:
        result, usage = chat_json(
            analyses_text, system=_REVIEW_SYSTEM, temperature=0.1
        )
        accumulate_usage(cost_tracker, usage, node="review")
    except Exception as exc:
        print(f"[ReviewNode] LLM 调用失败（{exc}），自动通过")
        return {
            "review_passed": True,
            "review_feedback": "",
            "iteration": iteration + 1,
            "cost_tracker": cost_tracker,
        }

    # 提取分数并用代码重算加权总分
    scores = result.get("scores", {})
    overall = _weighted_score(scores)
    feedback = result.get("feedback", "")
    passed = overall >= _PASS_THRESHOLD

    status = "通过" if passed else "未通过"
    print(f"[ReviewNode] 审核结果：{status}（加权总分 {overall}）")
    print(
        f"[ReviewNode] 明细："
        + ", ".join(f"{k}={scores.get(k, 'N/A')}" for k in _DIMENSIONS)
    )
    if not passed and feedback:
        print(f"[ReviewNode] 反馈：{feedback}")

    return {
        "review_passed": passed,
        "review_feedback": feedback,
        "iteration": iteration + 1,
        "cost_tracker": cost_tracker,
    }
