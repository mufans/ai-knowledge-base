"""LangGraph 工作流节点函数。

每个节点是纯函数：接收 KBState，返回 dict（部分状态更新）。
节点之间通过 KBState 共享状态，按 collect → analyze → organize → review → save 流转。
"""

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workflows.model_client import (
    Usage,
    chat_with_retry,
    create_provider,
)
from workflows.state import KBState

# ---------------------------------------------------------------------------
# 目录常量
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ARTICLES_DIR = _PROJECT_ROOT / "knowledge" / "articles"
_INDEX_FILE = _ARTICLES_DIR / "index.json"

# ---------------------------------------------------------------------------
# LLM 调用辅助函数
# ---------------------------------------------------------------------------


def chat(prompt: str, system: str = "", temperature: float = 0.7) -> tuple[str, Usage]:
    """发送对话请求，返回 (文本, 用量)。"""
    provider = create_provider()
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    response = chat_with_retry(provider, messages, temperature=temperature)
    return response.content, response.usage


def chat_json(prompt: str, system: str = "", temperature: float = 0.7) -> tuple[Any, Usage]:
    """发送对话请求并解析 JSON 响应，返回 (解析结果, 用量)。

    兼容 LLM 将 JSON 包裹在 markdown 代码块中的情况。
    """
    text, usage = chat(prompt, system, temperature=temperature)

    # 尝试从 ```json ... ``` 代码块中提取
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1)), usage
    # 尝试从 ``` ... ``` 代码块中提取
    match = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1)), usage
    # 直接解析
    return json.loads(text.strip()), usage


def accumulate_usage(tracker: dict, usage: Usage, node: str = "") -> None:
    """将 token 用量累加到 KBState 的 cost_tracker 中。"""
    tracker["total_tokens"] = tracker.get("total_tokens", 0) + usage.total_tokens
    tracker.setdefault("calls", []).append(
        {"node": node, "tokens": usage.total_tokens}
    )


# ---------------------------------------------------------------------------
# 节点 1: collect_node
# ---------------------------------------------------------------------------


def collect_node(state: KBState) -> dict:
    """采集节点：调用 GitHub Search API 采集 AI 相关仓库。"""
    print("[CollectNode] 开始采集 GitHub AI 相关仓库...")

    token = os.environ.get("GITHUB_TOKEN", "")
    query = "AI agent LLM"
    url = (
        f"https://api.github.com/search/repositories"
        f"?q={urllib.parse.quote(query)}&sort=stars&order=desc&per_page=30"
    )

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "ai-knowledge-base",
    }
    if token:
        headers["Authorization"] = f"token {token}"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    items = data.get("items", [])
    sources = []
    for item in items:
        sources.append({
            "url": item["html_url"],
            "title": item["full_name"],
            "content": item.get("description") or "",
            "source_type": "api",
            "stars": item.get("stargazers_count", 0),
            "language": item.get("language", ""),
            "topics": item.get("topics", []),
        })

    print(f"[CollectNode] 采集完成，共 {len(sources)} 条来源")
    return {"sources": sources}


# ---------------------------------------------------------------------------
# 节点 2: analyze_node
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


def analyze_node(state: KBState) -> dict:
    """分析节点：用 LLM 对每条数据生成中文摘要、标签、评分。"""
    sources = state.get("sources", [])
    print(f"[AnalyzeNode] 开始分析 {len(sources)} 条数据...")

    cost_tracker = state.get("cost_tracker", {})
    analyses = []

    for i, source in enumerate(sources):
        prompt = (
            f"仓库：{source.get('title', '')}\n"
            f"URL：{source.get('url', '')}\n"
            f"描述：{source.get('content', '')}\n"
            f"语言：{source.get('language', '')}\n"
            f"Stars：{source.get('stars', '')}\n"
            f"Topics：{', '.join(source.get('topics', []))}"
        )

        result, usage = chat_json(prompt, system=_ANALYZE_SYSTEM)
        accumulate_usage(cost_tracker, usage, node="analyze")

        analyses.append({
            "source_url": source["url"],
            "title": source.get("title", ""),
            "summary": result.get("summary", ""),
            "tags": result.get("tags", []),
            "score": float(result.get("score", 0.0)),
            "category": result.get("category", ""),
            "stars": source.get("stars", 0),
            "language": source.get("language", ""),
        })

        if (i + 1) % 5 == 0:
            print(f"[AnalyzeNode] 已分析 {i + 1}/{len(sources)} 条")

    print(f"[AnalyzeNode] 分析完成，共 {len(analyses)} 条结果")
    return {"analyses": analyses, "cost_tracker": cost_tracker}


# ---------------------------------------------------------------------------
# 节点 3: organize_node
# ---------------------------------------------------------------------------


def organize_node(state: KBState) -> dict:
    """整理节点：过滤低分、去重，将 analyses 映射为 articles。"""
    print("[OrganizeNode] 开始整理数据...")

    analyses = state.get("analyses", [])

    # 1. 过滤低分条目（score < 0.6）
    filtered = [a for a in analyses if a.get("score", 0) >= 0.6]
    print(f"[OrganizeNode] 过滤后保留 {len(filtered)}/{len(analyses)} 条（阈值 0.6）")

    # 2. 按 URL 去重
    seen_urls: set[str] = set()
    deduped = []
    for item in filtered:
        url = item.get("source_url", "")
        if url not in seen_urls:
            seen_urls.add(url)
            deduped.append(item)
    print(f"[OrganizeNode] 去重后保留 {len(deduped)} 条")

    # 3. 映射为 article 格式
    articles = [
        {
            "title": a.get("title", ""),
            "content": a.get("summary", ""),
            "category": a.get("category", ""),
            "tags": a.get("tags", []),
            "source_url": a.get("source_url", ""),
            "score": a.get("score", 0),
            "language": a.get("language", ""),
            "stars": a.get("stars", 0),
        }
        for a in deduped
    ]

    print(f"[OrganizeNode] 整理完成，最终 {len(articles)} 条")
    return {"articles": articles}


# ---------------------------------------------------------------------------
# 节点 4: review_node
# ---------------------------------------------------------------------------

_REVIEW_SYSTEM = (
    "你是知识库质量审核专家。请从四个维度对以下知识条目整体评分：\n"
    "1. summary_quality（摘要质量）：摘要是否准确、完整、有信息量\n"
    "2. tag_accuracy（标签准确）：标签是否准确反映内容\n"
    "3. category_relevance（分类合理）：分类是否恰当\n"
    "4. consistency（一致性）：各字段是否互相一致\n\n"
    "输出 JSON 格式：\n"
    "{\n"
    '  "passed": true,\n'
    '  "overall_score": 0.85,\n'
    '  "feedback": "具体修改建议（不通过时必填，通过时可为空字符串）",\n'
    '  "scores": {\n'
    '    "summary_quality": 0.9,\n'
    '    "tag_accuracy": 0.8,\n'
    '    "category_relevance": 0.85,\n'
    '    "consistency": 0.85\n'
    '  }\n'
    "}\n"
    "overall_score >= 0.7 为通过。只输出 JSON。"
)


def review_node(state: KBState) -> dict:
    """审核节点：LLM 四维度评分，iteration >= 2 强制通过。"""
    print("[ReviewNode] 开始审核...")

    iteration = state.get("iteration", 0)
    articles = state.get("articles", [])
    cost_tracker = state.get("cost_tracker", {})

    # iteration >= 2 强制通过，避免无限循环
    if iteration >= 2:
        print(f"[ReviewNode] 已达第 {iteration} 轮，强制通过")
        return {
            "review_passed": True,
            "review_feedback": "",
            "iteration": iteration + 1,
            "cost_tracker": cost_tracker,
        }

    # 将所有条目拼接为审核文本
    articles_text = "\n---\n".join(
        f"标题：{a.get('title', '')}\n"
        f"摘要：{a.get('content', '')}\n"
        f"标签：{a.get('tags', [])}\n"
        f"分类：{a.get('category', '')}\n"
        f"评分：{a.get('score', '')}"
        for a in articles
    )

    result, usage = chat_json(articles_text, system=_REVIEW_SYSTEM)
    accumulate_usage(cost_tracker, usage, node="review")

    passed = bool(result.get("passed", False))
    overall_score = float(result.get("overall_score", 0))
    feedback = result.get("feedback", "")

    status = "通过" if passed else "未通过"
    print(f"[ReviewNode] 审核结果：{status}（综合评分 {overall_score:.2f}）")
    if not passed:
        print(f"[ReviewNode] 反馈：{feedback}")

    return {
        "review_passed": passed,
        "review_feedback": feedback,
        "iteration": iteration + 1,
        "cost_tracker": cost_tracker,
    }


# ---------------------------------------------------------------------------
# 节点 5: save_node
# ---------------------------------------------------------------------------


def save_node(state: KBState) -> dict:
    """保存节点：将 articles 写入 knowledge/articles/ 并更新 index.json。"""
    print("[SaveNode] 开始保存知识条目...")

    articles = state.get("articles", [])
    _ARTICLES_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # 加载现有索引
    index: dict[str, Any] = {"updated_at": "", "total": 0, "articles": []}
    if _INDEX_FILE.exists():
        with open(_INDEX_FILE, "r", encoding="utf-8") as f:
            index = json.load(f)

    existing_ids = {entry["id"] for entry in index.get("articles", [])}
    existing_files = {entry["file"] for entry in index.get("articles", [])}

    saved_count = 0
    seq = 1
    for article in articles:
        # 跳过已存在的条目（按 source_url 去重）
        source_url = article.get("source_url", "")
        already_indexed = any(
            entry.get("id", "").startswith(f"github-{now.strftime('%Y%m%d')}")
            and entry.get("file", "").startswith(date_str)
            for entry in index.get("articles", [])
        )

        # 生成文件名：日期 + 标题 slug
        title_slug = re.sub(r"[^\w\s-]", "", article.get("title", "untitled"))
        title_slug = re.sub(r"[\s_]+", "-", title_slug).strip("-").lower()[:60]
        filename = f"{date_str}-{title_slug}.json"

        # 避免文件名冲突
        if filename in existing_files:
            filename = f"{date_str}-{title_slug}-{saved_count}.json"

        # 生成唯一 ID
        article_id = f"github-{now.strftime('%Y%m%d')}-{seq:03d}"
        while article_id in existing_ids:
            seq += 1
            article_id = f"github-{now.strftime('%Y%m%d')}-{seq:03d}"
        seq += 1

        # 0-1 评分 → 1-10 整数
        display_score = max(1, min(10, round(article.get("score", 0.5) * 10)))

        # 写入文章文件
        article_data = {
            "id": article_id,
            "title": article.get("title", ""),
            "source": "github",
            "source_url": source_url,
            "author": article.get("title", "").split("/")[0] if "/" in article.get("title", "") else "",
            "published_at": timestamp,
            "collected_at": timestamp,
            "summary": article.get("content", ""),
            "score": display_score,
            "tags": article.get("tags", []),
            "audience": "intermediate",
            "status": "draft",
            "updated_at": timestamp,
        }

        filepath = _ARTICLES_DIR / filename
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(article_data, f, ensure_ascii=False, indent=2)

        # 追加索引条目
        index["articles"].append({
            "id": article_id,
            "title": article.get("title", ""),
            "source": "github",
            "score": display_score,
            "tags": article.get("tags", []),
            "file": filename,
        })

        existing_ids.add(article_id)
        existing_files.add(filename)
        saved_count += 1

    # 更新索引元数据并写回
    index["updated_at"] = timestamp
    index["total"] = len(index["articles"])

    with open(_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f"[SaveNode] 保存完成，新增 {saved_count} 条，索引共 {index['total']} 条")
    return {}
