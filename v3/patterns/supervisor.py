"""Supervisor pattern — Worker executes, Supervisor reviews in a loop.

Workflow:
  1. Worker Agent receives a task and produces a JSON analysis report.
  2. Supervisor Agent reviews the report on three dimensions:
     accuracy (1-10), depth (1-10), format (1-10).
  3. If composite score >= 7 the report is accepted; otherwise the Worker
     retries with the Supervisor's feedback (up to *max_retries* rounds).
  4. After exhausting retries the last report is returned with a warning.

Usage::

    from patterns.supervisor import supervisor

    result = supervisor("分析 RAG 技术的最新发展趋势")
    print(result["output"])
    print(f"attempts={result['attempts']}, score={result['final_score']}")
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Path setup — allow import of pipeline.model_client when run directly
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from pipeline.model_client import quick_chat  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_WORKER_SYSTEM = (
    "你是一名专业的技术分析师。根据用户给出的任务，输出一份 JSON 格式的分析报告。\n\n"
    "JSON schema:\n"
    '{\n'
    '  "title": "报告标题",\n'
    '  "summary": "核心结论摘要（100-200字）",\n'
    '  "analysis": "详细分析内容",\n'
    '  "key_points": ["要点1", "要点2", ...],\n'
    '  "conclusion": "总结与展望"\n'
    '}\n\n'
    "严格要求：只输出合法 JSON，不要 markdown 代码块，不要多余文字。"
)

_SUPERVISOR_SYSTEM = (
    "你是一名严格的质量审核员。你将收到一份技术分析报告和原始任务，"
    "请从以下三个维度评分（1-10）：\n\n"
    "- accuracy（准确性）：分析内容是否准确、是否有事实错误\n"
    "- depth（深度）：分析是否深入、是否有见地\n"
    "- format（格式）：JSON 结构是否完整、字段内容是否充实\n\n"
    "输出严格合法的 JSON（不要 markdown 代码块，不要多余文字）：\n"
    '{\n'
    '  "accuracy": <int 1-10>,\n'
    '  "depth": <int 1-10>,\n'
    '  "format": <int 1-10>,\n'
    '  "passed": <bool>,\n'
    '  "score": <int, 三项平均取整>,\n'
    '  "feedback": "<如果不通过，说明具体问题和改进建议；通过则为空字符串>"\n'
    '}\n\n'
    "评分标准：\n"
    "- 9-10: 卓越，几乎无可挑剔\n"
    "- 7-8: 良好，小问题不影响整体质量（passed=true）\n"
    "- 5-6: 一般，有明显不足需要改进\n"
    "- 1-4: 较差，内容空洞或存在严重问题"
)

# ---------------------------------------------------------------------------
# JSON helper
# ---------------------------------------------------------------------------


def _parse_json(raw: str) -> dict:
    """Parse LLM output as JSON, stripping markdown fences if present."""
    text = raw.strip()
    # Remove ```json ... ``` wrappers
    if text.startswith("```"):
        first_nl = text.index("\n") + 1 if "\n" in text else 3
        last_fence = text.rfind("```")
        text = text[first_nl:last_fence].strip()
    return json.loads(text)


def _chat_json(prompt: str, *, system: str = "") -> dict:
    """Call LLM and parse the response as JSON."""
    raw = quick_chat(prompt, system=system, temperature=0.3, max_tokens=2048)
    return _parse_json(raw)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def worker(task: str, feedback: Optional[str] = None) -> dict:
    """Execute a task and return a JSON analysis report.

    Args:
        task: The analysis task description.
        feedback: Previous Supervisor feedback to incorporate (None on first run).

    Returns:
        Parsed JSON report dict.
    """
    prompt = task
    if feedback:
        prompt = (
            f"原始任务：{task}\n\n"
            f"你上次的报告被审核员退回，反馈如下：\n{feedback}\n\n"
            "请根据反馈改进你的分析报告，重新输出 JSON。"
        )
    return _chat_json(prompt, system=_WORKER_SYSTEM)


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


def supervisor_review(task: str, report: dict) -> Dict[str, Any]:
    """Review a Worker report and return a quality verdict.

    Returns:
        Dict with keys: accuracy, depth, format, passed, score, feedback.
    """
    prompt = (
        f"原始任务：{task}\n\n"
        f"Worker 提交的报告：\n{json.dumps(report, ensure_ascii=False, indent=2)}"
    )
    result = _chat_json(prompt, system=_SUPERVISOR_SYSTEM)

    # Ensure required keys exist with sensible defaults
    result.setdefault("accuracy", 5)
    result.setdefault("depth", 5)
    result.setdefault("format", 5)
    result.setdefault("passed", False)
    result.setdefault("score", 5)
    result.setdefault("feedback", "")

    return result


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def supervisor(task: str, max_retries: int = 3) -> Dict[str, Any]:
    """Run the Worker-Supervisor loop until quality threshold is met.

    Args:
        task: The analysis task description.
        max_retries: Maximum number of Worker retries (total attempts = max_retries + 1).

    Returns:
        A dict containing:
          - output (dict):       The final Worker report.
          - attempts (int):      Total number of Worker attempts made.
          - final_score (int):   The Supervisor's composite score.
          - warning (str|None):  Present when max retries were exhausted.
    """
    warning: Optional[str] = None
    report: dict = {}
    review: Dict[str, Any] = {}

    for attempt in range(1, max_retries + 2):  # +2: 1 initial + max_retries
        logger.info("Worker attempt %d/%d", attempt, max_retries + 1)

        # --- Worker ---
        try:
            report = worker(task, feedback=review.get("feedback") if review else None)
        except Exception as exc:
            logger.error("Worker failed: %s", exc)
            report = {"error": str(exc)}

        # --- Supervisor ---
        try:
            review = supervisor_review(task, report)
        except Exception as exc:
            logger.error("Supervisor review failed: %s", exc)
            review = {
                "passed": False,
                "score": 0,
                "feedback": f"审核员解析失败: {exc}",
            }

        score = review.get("score", 0)
        passed = review.get("passed", False)
        logger.info(
            "Review: score=%d passed=%s (accuracy=%s depth=%s format=%s)",
            score,
            passed,
            review.get("accuracy"),
            review.get("depth"),
            review.get("format"),
        )

        if passed and score >= 7:
            return {
                "output": report,
                "attempts": attempt,
                "final_score": score,
                "warning": None,
            }

    # Exhausted all retries — return last result with a warning
    warning = (
        f"达到最大重试次数 ({max_retries})，最终得分 {score} 未达标。"
        f"反馈：{review.get('feedback', '无')}"
    )
    logger.warning(warning)
    return {
        "output": report,
        "attempts": max_retries + 1,
        "final_score": score,
        "warning": warning,
    }


# ---------------------------------------------------------------------------
# CLI test entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    test_tasks = [
        "分析 RAG（检索增强生成）技术的最新发展趋势",
        "对比 LangGraph 和 CrewAI 两个多 Agent 框架",
    ]

    print("=" * 60)
    print("Supervisor Pattern — Worker-Supervisor 审核循环测试")
    print("=" * 60)

    for task in test_tasks:
        print(f"\n{'─' * 60}")
        print(f"任务: {task}")
        print(f"{'─' * 60}")
        try:
            result = supervisor(task)
            print(f"尝试次数 : {result['attempts']}")
            print(f"最终得分 : {result['final_score']}")
            if result["warning"]:
                print(f"警告     : {result['warning']}")
            print(f"\n报告内容 :")
            print(json.dumps(result["output"], ensure_ascii=False, indent=2))
        except Exception as exc:
            print(f"[错误] {exc}")
