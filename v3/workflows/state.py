"""LangGraph 工作流共享状态定义。

遵循「报告式通信」原则：每个字段存储的是结构化摘要，
而非未经处理的原始数据，便于节点间高效传递信息。
"""

from typing import TypedDict


class KBState(TypedDict, total=False):
    """知识库构建工作流的全局状态。

    各节点通过读写此状态进行协作，字段均为结构化摘要，
    避免传递大量原始数据。
    """

    sources: list[dict]
    """采集到的原始数据列表。

    每个元素为一条来源记录，格式示例::

        {
            "url": "https://...",
            "title": "文章标题",
            "content": "正文内容",
            "source_type": "web" | "file" | "api",
        }
    """

    analyses: list[dict]
    """LLM 分析后的结构化结果。

    每个元素对应一条 source 的分析摘要，格式示例::

        {
            "source_url": "https://...",
            "summary": "核心要点摘要",
            "key_points": ["要点1", "要点2"],
            "category": "分类标签",
            "confidence": 0.95,
        }
    """

    articles: list[dict]
    """格式化、去重后的知识条目。

    已经过质量审核和去重处理，可直接写入知识库，格式示例::

        {
            "title": "条目标题",
            "content": "格式化后的正文",
            "category": "分类标签",
            "tags": ["标签1", "标签2"],
            "source_url": "https://...",
        }
    """

    review_feedback: str
    """审核反馈意见。

    当 review_passed 为 False 时，包含具体的修改建议；
    通过时可为空字符串。
    """

    review_passed: bool
    """审核是否通过。

    True 表示 articles 质量达标，可进入下一阶段；
    False 表示需要根据 review_feedback 返回修改。
    """

    iteration: int
    """当前审核循环次数，从 0 开始。

    上限为 3 次，超过后强制通过以避免无限循环。
    """

    cost_tracker: dict
    """Token 用量追踪。

    汇总各节点的 LLM 调用开销，格式示例::

        {
            "total_tokens": 0,
            "total_cost": 0.0,
            "calls": [
                {"node": "analyze", "tokens": 1500, "cost": 0.03},
            ],
        }
    """
