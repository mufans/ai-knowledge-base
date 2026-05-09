"""LangGraph 工作流节点函数。

每个节点是纯函数：接收 KBState，返回 dict（部分状态更新）。
节点之间通过 KBState 共享状态，按 collect → analyze → organize → review → save 流转。
"""

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from workflows.model_client import (
    Usage,
    chat_with_retry,
    create_provider,
)
from workflows.state import KBState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 目录常量
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ARTICLES_DIR = _PROJECT_ROOT / "knowledge" / "articles"
_INDEX_FILE = _ARTICLES_DIR / "index.json"
_RSS_CONFIG_PATH = _PROJECT_ROOT / "pipeline" / "rss_sources.yaml"

AI_KEYWORDS = [
    "ai", "llm", "agent", "gpt", "claude", "transformer",
    "machine learning", "deep learning", "nlp", "rag", "mcp",
    "openai", "anthropic", "copilot", "codex",
]

_DEFAULT_RSS_FEEDS = [
    "https://hnrss.org/best?q=AI+LLM+agent&count=30",
    "https://lobste.rs/t/ai,ml.rss",
]

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
# RSS 采集辅助函数（复用 pipeline 逻辑）
# ---------------------------------------------------------------------------


def _parse_rss_items(text: str) -> list[dict]:
    """正则解析 RSS XML，提取 title/link/description。"""
    items: list[dict] = []

    item_pattern = re.compile(r"<item>(.*?)</item>", re.DOTALL)
    title_pattern = re.compile(
        r"<title><!\[CDATA\[(.*?)\]\]></title>"
        r"|<title>(.*?)</title>",
        re.DOTALL,
    )
    link_pattern = re.compile(r"<link>(.*?)</link>", re.DOTALL)
    desc_pattern = re.compile(
        r"<description><!\[CDATA\[(.*?)\]\]></description>"
        r"|<description>(.*?)</description>",
        re.DOTALL,
    )

    for match in item_pattern.finditer(text):
        block = match.group(1)

        title_m = title_pattern.search(block)
        title = ""
        if title_m:
            title = (title_m.group(1) or title_m.group(2) or "").strip()

        link_m = link_pattern.search(block)
        link = link_m.group(1).strip() if link_m else ""

        desc_m = desc_pattern.search(block)
        desc = ""
        if desc_m:
            desc = (desc_m.group(1) or desc_m.group(2) or "").strip()

        if title and link:
            items.append({"title": title, "link": link, "description": desc})

    return items


def _load_rss_sources() -> list[dict]:
    """读取 pipeline/rss_sources.yaml 中 enabled 的源，fallback 到默认列表。"""
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed, using default RSS feeds")
        return [{"url": u} for u in _DEFAULT_RSS_FEEDS]

    if not _RSS_CONFIG_PATH.exists():
        logger.warning("RSS config not found: %s", _RSS_CONFIG_PATH)
        return [{"url": u} for u in _DEFAULT_RSS_FEEDS]

    with open(_RSS_CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    sources = [e for e in config.get("sources", []) if e.get("enabled", False)]
    if not sources:
        logger.warning("No enabled RSS sources in config, using defaults")
        return [{"url": u} for u in _DEFAULT_RSS_FEEDS]

    return sources


def _collect_rss(limit: int = 20) -> list[dict]:
    """采集 RSS feed，按 AI_KEYWORDS 过滤，返回 sources 格式列表。"""
    items: list[dict] = []

    for source in _load_rss_sources():
        feed_url = source["url"]
        source_name = source.get("name", feed_url)
        logger.info("Fetching RSS feed: %s", source_name)

        try:
            with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                resp = client.get(feed_url)
                resp.raise_for_status()
            text = resp.text
        except Exception as exc:
            logger.error("RSS fetch failed (%s): %s", source_name, exc)
            continue

        for entry in _parse_rss_items(text):
            combined = f"{entry['title']} {entry['description']}".lower()
            if not any(kw.lower() in combined for kw in AI_KEYWORDS):
                continue

            items.append({
                "url": entry["link"],
                "title": entry["title"],
                "content": entry["description"],
                "source_type": "rss",
                "stars": 0,
                "language": "",
                "topics": [],
            })

            if len(items) >= limit:
                break

        if len(items) >= limit:
            break

    logger.info("Collected %d items from RSS feeds", len(items))
    return items[:limit]


# ---------------------------------------------------------------------------
# 节点 1: collect_node
# ---------------------------------------------------------------------------


def collect_node(state: KBState) -> dict:
    """采集节点：调用 GitHub Search API 和 RSS feeds 采集 AI 相关内容。"""
    print("[CollectNode] 开始采集 AI 相关内容（GitHub + RSS）...")

    plan = state.get("plan", {})
    per_page = plan.get("per_source_limit", 30)

    # --- GitHub API 采集 ---
    token = os.environ.get("GITHUB_TOKEN", "")
    query = "AI agent LLM"
    url = (
        f"https://api.github.com/search/repositories"
        f"?q={urllib.parse.quote(query)}&sort=stars&order=desc&per_page={per_page}"
    )

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "ai-knowledge-base",
    }
    if token:
        headers["Authorization"] = f"token {token}"

    github_sources = []
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("items", [])
        for item in items:
            github_sources.append({
                "url": item["html_url"],
                "title": item["full_name"],
                "content": item.get("description") or "",
                "source_type": "github",
                "stars": item.get("stargazers_count", 0),
                "language": item.get("language", ""),
                "topics": item.get("topics", []),
            })
        print(f"[CollectNode] GitHub 采集: {len(github_sources)} 条")
    except Exception as exc:
        logger.error("GitHub API request failed: %s", exc)

    # --- RSS 采集 ---
    rss_sources = _collect_rss(limit=per_page)
    print(f"[CollectNode] RSS 采集: {len(rss_sources)} 条")

    # --- 合并 ---
    sources = github_sources + rss_sources
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
    '  "key_insight": "一句话关键洞察" '
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
            "key_insight": result.get("key_insight", ""),
            "tags": result.get("tags", []),
            "score": float(result.get("score", 0.0)),
            "category": result.get("category", ""),
            "stars": source.get("stars", 0),
            "language": source.get("language", ""),
            "source_type": source.get("source_type", "github"),
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

    plan = state.get("plan", {})
    threshold = plan.get("relevance_threshold", 0.6)

    analyses = state.get("analyses", [])

    # 1. 过滤低分条目
    filtered = [a for a in analyses if a.get("score", 0) >= threshold]
    print(f"[OrganizeNode] 过滤后保留 {len(filtered)}/{len(analyses)} 条（阈值 {threshold}）")

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
            "key_insight": a.get("key_insight", ""),
            "category": a.get("category", ""),
            "tags": a.get("tags", []),
            "source_url": a.get("source_url", ""),
            "score": a.get("score", 0),
            "language": a.get("language", ""),
            "stars": a.get("stars", 0),
            "source_type": a.get("source_type", "github"),
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
    date_compact = now.strftime("%Y%m%d")
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # 加载现有索引
    index: dict[str, Any] = {"updated_at": "", "total": 0, "articles": []}
    if _INDEX_FILE.exists():
        with open(_INDEX_FILE, "r", encoding="utf-8") as f:
            index = json.load(f)

    existing_ids = {entry["id"] for entry in index.get("articles", [])}
    existing_files = {entry["file"] for entry in index.get("articles", [])}
    existing_urls = {entry.get("source_url", "") for entry in index.get("articles", [])}

    skipped_count = 0
    saved_count = 0
    seq = 1
    for article in articles:
        source_url = article.get("source_url", "")
        source_type = article.get("source_type", "github")

        # 按 source_url 去重：已存在则跳过
        if source_url and source_url in existing_urls:
            skipped_count += 1
            continue

        # 生成文件名：日期 + 标题 slug
        title_slug = re.sub(r"[^\w\s-]", "", article.get("title", "untitled"))
        title_slug = re.sub(r"[\s_]+", "-", title_slug).strip("-").lower()[:60]
        filename = f"{date_str}-{title_slug}.json"

        # 避免文件名冲突
        if filename in existing_files:
            filename = f"{date_str}-{title_slug}-{saved_count}.json"

        # 生成唯一 ID（根据来源使用不同前缀）
        article_id = f"{source_type}-{date_compact}-{seq:03d}"
        while article_id in existing_ids:
            seq += 1
            article_id = f"{source_type}-{date_compact}-{seq:03d}"
        seq += 1

        # 0-1 评分 → 1-10 整数
        display_score = max(1, min(10, round(article.get("score", 0.5) * 10)))

        # 写入文章文件
        article_data = {
            "id": article_id,
            "title": article.get("title", ""),
            "source": source_type,
            "source_url": source_url,
            "author": article.get("title", "").split("/")[0] if "/" in article.get("title", "") else "",
            "published_at": timestamp,
            "collected_at": timestamp,
            "summary": article.get("content", ""),
            "key_insight": article.get("key_insight", ""),
            "score": display_score,
            "tags": article.get("tags", []),
            "audience": "intermediate",
            "status": "draft",
            "category": article.get("category", ""),
            "updated_at": timestamp,
        }

        filepath = _ARTICLES_DIR / filename
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(article_data, f, ensure_ascii=False, indent=2)

        # 追加索引条目
        index["articles"].append({
            "id": article_id,
            "title": article.get("title", ""),
            "source": source_type,
            "score": display_score,
            "tags": article.get("tags", []),
            "category": article.get("category", ""),
            "file": filename,
        })

        existing_ids.add(article_id)
        existing_files.add(filename)
        existing_urls.add(source_url)
        saved_count += 1

    # 更新索引元数据并写回
    index["updated_at"] = timestamp
    index["total"] = len(index["articles"])

    with open(_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f"[SaveNode] 保存完成，新增 {saved_count} 条，跳过重复 {skipped_count} 条，索引共 {index['total']} 条")
    return {}


# ---------------------------------------------------------------------------
# 节点 6: publish_node
# ---------------------------------------------------------------------------


def publish_node(state: KBState) -> dict:
    """推送节点：生成每日摘要并推送到飞书等渠道，导出 markdown 文件。"""
    print("[PublishNode] 开始生成摘要并推送...")

    import asyncio

    from distribution.formatter import generate_daily_digest
    from distribution.publisher import publish_daily_digest, publish_file

    knowledge_dir = str(_PROJECT_ROOT / "knowledge" / "articles")

    # 1. 生成三种格式的每日摘要
    try:
        digest = generate_daily_digest(knowledge_dir=knowledge_dir)
    except Exception as exc:
        logger.error("生成摘要失败: %s", exc)
        return {}

    # 2. 导出 markdown 到 output/
    md_path = ""
    try:
        md_path = publish_file(
            digest["markdown"],
            output_dir=str(_PROJECT_ROOT / "output"),
        )
        print(f"[PublishNode] Markdown 已导出: {md_path}")
    except Exception as exc:
        logger.error("导出 markdown 失败: %s", exc)

    # 3. 推送到飞书等渠道
    results = []
    try:
        results = asyncio.run(
            publish_daily_digest(knowledge_dir=knowledge_dir)
        )
        for r in results:
            status = "成功" if r.success else f"失败({r.error})"
            print(f"[PublishNode] {r.channel}: {status}")
    except Exception as exc:
        logger.error("推送失败: %s", exc)

    print(f"[PublishNode] 推送完成（{len(results)} 个渠道）")
    return {}
