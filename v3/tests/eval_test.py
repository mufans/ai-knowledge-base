"""AI 知识库评估测试。

使用 pytest 框架，覆盖正面 / 负面 / 边界三种场景，
并包含 LLM-as-Judge 质量评估。

运行方式::

    # 全部测试（含 LLM 调用）
    pytest tests/eval_test.py -v

    # 跳过 LLM 慢测试
    pytest tests/eval_test.py -v -m "not slow"
"""

from __future__ import annotations

import json
import re
import warnings

import pytest

# ---------------------------------------------------------------------------
# 加载 .env（让 pytest 能读到 LLM API KEY）
# ---------------------------------------------------------------------------
from workflows.model_client import _load_dotenv

_load_dotenv()

# 屏蔽自定义 slow 标记的 PytestUnknownMarkWarning
warnings.filterwarnings("ignore", category=pytest.PytestUnknownMarkWarning)


# ---------------------------------------------------------------------------
# 共享常量：analyze 阶段的 system prompt（与 nodes.py 保持一致）
# ---------------------------------------------------------------------------

_ANALYZE_SYSTEM = (
    "你是一个 AI 技术分析专家。请对以下 GitHub 仓库信息进行分析，"
    "输出 JSON 格式：\n"
    "{\n"
    '  "summary": "中文摘要（100-200字，概括仓库核心功能和技术亮点）",\n'
    '  "tags": ["标签1", "标签2"],  // 3-5个英文技术标签\n'
    '  "score": 0.85,  // 0-1浮点数，表示AI领域技术价值\n'
    '  "category": "分类名"  // 如 agent/rag/llm/code-generation/multi-agent 等\n'
    "}\n"
    "只输出 JSON，不要其他文字。"
)

_JUDGE_SYSTEM = (
    "你是一个严格的质量评审专家。请对以下 AI 知识库分析结果打分（1-10 整数）。\n"
    "评分维度：摘要准确性、标签相关性、分类合理性。\n"
    "输出 JSON：{\"score\": 8, \"reason\": \"简要理由\"}\n"
    "只输出 JSON。"
)


# ---------------------------------------------------------------------------
# EVAL_CASES：三种场景
# ---------------------------------------------------------------------------

EVAL_CASES = [
    {
        "name": "正面案例-技术文章",
        "input": {
            "title": "langchain-ai/langchain",
            "url": "https://github.com/langchain-ai/langchain",
            "content": (
                "LangChain is a framework for developing applications powered by "
                "large language models (LLMs). It enables context-aware reasoning, "
                "chain-of-thought agents, RAG pipelines, and multi-agent orchestration."
            ),
            "language": "Python",
            "stars": 95000,
            "topics": ["llm", "agents", "rag", "nlp"],
        },
        "expected": {
            "has_summary": True,
            "min_tags": 2,
            "score_range": (0.6, 1.0),
            "category_not_empty": True,
        },
    },
    {
        "name": "负面案例-无关内容",
        "input": {
            "title": "someone/cooking-recipes",
            "url": "https://github.com/someone/cooking-recipes",
            "content": (
                "A collection of home cooking recipes, including pasta, salad, "
                "and dessert. No programming or AI content."
            ),
            "language": "HTML",
            "stars": 5,
            "topics": ["cooking", "recipes", "food"],
        },
        "expected": {
            "has_summary": True,
            "min_tags": 0,
            "score_range": (0.0, 0.5),
            "category_not_empty": True,
        },
    },
    {
        "name": "边界案例-极短输入",
        "input": {
            "title": "test/AI",
            "url": "https://github.com/test/AI",
            "content": "AI",
            "language": "",
            "stars": 0,
            "topics": [],
        },
        "expected": {
            "has_summary": True,
            "min_tags": 0,
            "score_range": (0.0, 1.0),
            "category_not_empty": False,
        },
    },
]


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _build_prompt(source: dict) -> str:
    """将 source dict 拼接为 analyze 的 user prompt。"""
    return (
        f"仓库：{source.get('title', '')}\n"
        f"URL：{source.get('url', '')}\n"
        f"描述：{source.get('content', '')}\n"
        f"语言：{source.get('language', '')}\n"
        f"Stars：{source.get('stars', '')}\n"
        f"Topics：{', '.join(source.get('topics', []))}"
    )


def _parse_json_response(text: str) -> dict:
    """从 LLM 响应中提取 JSON（兼容 markdown 代码块）。"""
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    match = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    return json.loads(text.strip())


# ===========================================================================
# 测试 1: 本地验证（不调用 LLM）
# ===========================================================================


def test_eval_cases_structure():
    """验证 EVAL_CASES 结构完整性（本地，不调用 LLM）。"""
    assert len(EVAL_CASES) >= 3, "至少包含 3 个测试场景"

    required_input_keys = {"title", "url", "content"}
    required_expected_keys = {"has_summary", "min_tags", "score_range", "category_not_empty"}

    for case in EVAL_CASES:
        assert "name" in case, f"缺少 name 字段: {case}"
        assert "input" in case, f"缺少 input 字段: {case['name']}"
        assert "expected" in case, f"缺少 expected 字段: {case['name']}"

        missing_input = required_input_keys - set(case["input"].keys())
        assert not missing_input, f"{case['name']} 缺少 input 字段: {missing_input}"

        missing_expected = required_expected_keys - set(case["expected"].keys())
        assert not missing_expected, f"{case['name']} 缺少 expected 字段: {missing_expected}"

        lo, hi = case["expected"]["score_range"]
        assert 0.0 <= lo <= hi <= 1.0, (
            f"{case['name']} score_range 不合法: ({lo}, {hi})"
        )


# ===========================================================================
# 测试 2: 分析质量（调用 LLM）
# ===========================================================================


@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    EVAL_CASES,
    ids=[c["name"] for c in EVAL_CASES],
)
def test_analyze_quality(case):
    """对每种场景调用 LLM 分析，用范围断言验证结果。"""
    from workflows.nodes import chat_json

    prompt = _build_prompt(case["input"])
    result, _usage = chat_json(prompt, system=_ANALYZE_SYSTEM)

    exp = case["expected"]

    # 摘要非空
    if exp["has_summary"]:
        assert len(result.get("summary", "")) > 0, (
            f"{case['name']}: summary 为空"
        )

    # 标签数量
    tags = result.get("tags", [])
    assert len(tags) >= exp["min_tags"], (
        f"{case['name']}: tags 数量 {len(tags)} < {exp['min_tags']}"
    )

    # score 范围
    score = float(result.get("score", 0))
    lo, hi = exp["score_range"]
    assert lo <= score <= hi, (
        f"{case['name']}: score {score} 不在 [{lo}, {hi}] 范围内"
    )

    # 分类
    if exp["category_not_empty"]:
        assert len(result.get("category", "")) > 0, (
            f"{case['name']}: category 为空"
        )


# ===========================================================================
# 测试 3: LLM-as-Judge（调用 LLM）
# ===========================================================================


@pytest.mark.slow
def test_llm_as_judge():
    """LLM 对正面案例分析结果打分，断言 >= 5。"""
    from workflows.nodes import chat_json

    # 取正面案例
    positive = EVAL_CASES[0]
    prompt = _build_prompt(positive["input"])

    # 先分析
    analysis, _usage = chat_json(prompt, system=_ANALYZE_SYSTEM)

    # 再让 LLM 评审
    judge_prompt = (
        f"## 原始输入\n{json.dumps(positive['input'], ensure_ascii=False)}\n\n"
        f"## 分析结果\n{json.dumps(analysis, ensure_ascii=False)}"
    )
    judge_result, _usage = chat_json(judge_prompt, system=_JUDGE_SYSTEM)

    score = int(judge_result.get("score", 0))
    reason = judge_result.get("reason", "")

    assert score >= 5, (
        f"LLM-as-Judge 评分 {score} < 5，理由: {reason}"
    )
