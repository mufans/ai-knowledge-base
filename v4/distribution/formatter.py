"""Article formatting module for multi-channel distribution.

Provides pure functions that convert article dicts into Markdown,
Telegram MarkdownV2, and Feishu interactive card payloads.
"""

import json
import re
from datetime import date as date_type
from pathlib import Path
from typing import Any

# Telegram MarkdownV2 需要转义的特殊字符
_TG_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!])")


def _score_emoji(score: int) -> str:
    """根据评分返回对应 emoji。

    Args:
        score: 0-10 的原始评分。

    Returns:
        🟢 (>=8) / 🟡 (>=6) / 🔴 (<6)
    """
    if score >= 8:
        return "🟢"
    if score >= 6:
        return "🟡"
    return "🔴"


def _score_color(score: int) -> str:
    """根据评分返回飞书卡片 header 颜色。

    Args:
        score: 0-10 的原始评分。

    Returns:
        "green" / "yellow" / "red"
    """
    if score >= 8:
        return "green"
    if score >= 6:
        return "yellow"
    return "red"


def _get_insight(article: dict[str, Any]) -> str:
    """取 key_insight，无则用 summary 兜底。

    Args:
        article: 文章字典。

    Returns:
        短摘要文本。
    """
    return article.get("key_insight") or article.get("summary", "")


def _tg_escape(text: str) -> str:
    """转义 Telegram MarkdownV2 特殊字符。

    Args:
        text: 原始文本。

    Returns:
        转义后的安全文本。
    """
    return _TG_ESCAPE_RE.sub(r"\\\1", text)


def json_to_markdown(article: dict[str, Any]) -> str:
    """将单篇文章格式化为 Markdown。

    Args:
        article: 文章字典，需包含 title, source, source_url, collected_at,
                 score, tags, summary 等字段。

    Returns:
        Markdown 格式字符串。
    """
    score = article.get("score", 0)
    collected_date = article.get("collected_at", "")[:10]
    tags = " ".join(f"`{t}`" for t in article.get("tags", []))
    emoji = _score_emoji(score)

    lines = [
        f"## {article.get('title', '')}",
        f"",
        f"- **来源**: {article.get('source', '')}",
        f"- **日期**: {collected_date}",
        f"- **评分**: {emoji} {score}",
        f"- **标签**: {tags}",
        f"",
        f"{_get_insight(article)}",
        f"",
        f"[原文链接]({article.get('source_url', '')})",
    ]
    return "\n".join(lines)


def json_to_telegram(article: dict[str, Any]) -> str:
    """将单篇文章格式化为 Telegram MarkdownV2。

    转义规则: _*[]()~`>#+-=|{}.! 全部加反斜杠前缀。

    Args:
        article: 文章字典。

    Returns:
        Telegram MarkdownV2 格式字符串。
    """
    score = article.get("score", 0)
    emoji = _score_emoji(score)
    title = _tg_escape(article.get("title", ""))
    source_url = _tg_escape(article.get("source_url", ""))
    summary = _tg_escape(_get_insight(article))
    source = _tg_escape(article.get("source", ""))
    tags = " ".join(
        "#" + _tg_escape(t.replace(" ", "_"))
        for t in article.get("tags", [])
    )

    lines = [
        f"[{title}]({source_url})",
        summary,
        f"评分: {emoji} {score}",
        f"来源: {source}",
        tags,
    ]
    return "\n".join(lines)


def json_to_feishu(article: dict[str, Any]) -> dict[str, Any]:
    """将单篇文章格式化为飞书 interactive 卡片消息体。

    Args:
        article: 文章字典。

    Returns:
        飞书消息体字典，msg_type=interactive。
    """
    score = article.get("score", 0)
    color = _score_color(score)
    collected_date = article.get("collected_at", "")[:10]
    tags = "、".join(article.get("tags", []))

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**{article.get('title', '')}**\n"
                    f"{_get_insight(article)}"
                ),
            },
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"来源: {article.get('source', '')} | "
                    f"日期: {collected_date} | "
                    f"评分: {_score_emoji(score)} {score}"
                ),
            },
        },
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看原文"},
                    "url": article.get("source_url", ""),
                    "type": "primary",
                }
            ],
        },
    ]

    if tags:
        elements.insert(
            2,
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"标签: {tags}",
                },
            },
        )

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": article.get("title", ""),
                },
                "template": color,
            },
            "elements": elements,
        },
    }


def digest_from_index(
    knowledge_dir: str = "knowledge/articles",
    date: str | None = None,
    top_n: int = 5,
) -> dict[str, Any]:
    """基于 index.json 的快速预览，不读全文文件。

    仅读取 index.json 中当日的条目（按 file 字段日期前缀过滤），
    按 category 分组，每组取 score 最高的 top_n 条，返回标题、评分等
    摘要信息。需要详情时可凭 ``file`` 字段回读对应 JSON。

    Args:
        knowledge_dir: 文章 JSON 存放目录，默认 "knowledge/articles"。
        date: 日期字符串，格式 YYYY-MM-DD，默认今天。
        top_n: 每个 category 取前 N 篇，默认 5。

    Returns:
        dict，包含:
        - ``date``: 查询日期
        - ``total``: 当日总条目数
        - ``categories``: 有序字典，key 为 category，value 为该组
          top_n 条目列表（每条含 id, title, score, tags, file, source）
        - ``markdown``: 快速预览 Markdown 字符串
    """
    if date is None:
        date = date_type.today().isoformat()

    index_path = Path(knowledge_dir) / "index.json"
    if not index_path.exists():
        return {
            "date": date,
            "total": 0,
            "categories": {},
            "markdown": f"📭 index.json 不存在",
        }

    with open(index_path, encoding="utf-8") as f:
        index = json.load(f)

    # 按 file 字段日期前缀过滤当日条目
    daily = [
        a for a in index.get("articles", [])
        if a.get("file", "").startswith(date)
    ]

    if not daily:
        return {
            "date": date,
            "total": 0,
            "categories": {},
            "markdown": f"📭 {date} 暂无新增知识条目",
        }

    # 按 category 分组
    from collections import OrderedDict
    groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for a in daily:
        cat = a.get("category") or "uncategorized"
        groups.setdefault(cat, []).append(a)

    # 每组按 score 降序取 top_n
    for cat in groups:
        groups[cat].sort(key=lambda a: a.get("score", 0), reverse=True)
        groups[cat] = groups[cat][:top_n]

    # 生成 Markdown 预览
    md_lines = [f"# AI 知识日报 {date} (预览)\n"]
    for cat, articles in groups.items():
        md_lines.append(f"\n### {cat.upper()} ({len(articles)})\n")
        for a in articles:
            emoji = _score_emoji(a.get("score", 0))
            tags = " ".join(f"`{t}`" for t in a.get("tags", []))
            md_lines.append(
                f"- {emoji} **{a.get('title', '')}** — "
                f"{a.get('score', 0)} | {tags}"
            )

    return {
        "date": date,
        "total": len(daily),
        "categories": groups,
        "markdown": "\n".join(md_lines),
    }


def generate_daily_digest(
    knowledge_dir: str = "knowledge/articles",
    date: str | None = None,
    top_n: int = 5,
) -> dict[str, str]:
    """生成当日知识简报。

    扫描 knowledge_dir 下匹配 {date}-*.json 的文件，按 score 降序取
    Top N，分别输出 Markdown、Telegram、飞书三种格式。

    Args:
        knowledge_dir: 文章 JSON 存放目录，默认 "knowledge/articles"。
        date: 日期字符串，格式 YYYY-MM-DD，默认今天。
        top_n: 取前 N 篇，默认 5。

    Returns:
        {"markdown": str, "telegram": str, "feishu": str} 字典。
        当日无文章时三者均为 "📭 {date} 暂无新增知识条目"。
    """
    if date is None:
        date = date_type.today().isoformat()

    articles_dir = Path(knowledge_dir)
    files = sorted(articles_dir.glob(f"{date}-*.json"))

    if not files:
        empty_msg = f"📭 {date} 暂无新增知识条目"
        return {"markdown": empty_msg, "telegram": empty_msg, "feishu": empty_msg}

    all_articles: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            article = json.load(f)
        url = article.get("source_url", "")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        all_articles.append(article)

    # 按 category 分组，无 category 归入 uncategorized
    from collections import OrderedDict
    groups: dict[str, list[dict[str, Any]]] = OrderedDict()
    for a in all_articles:
        cat = a.get("category") or "uncategorized"
        groups.setdefault(cat, []).append(a)

    # 每组按 score 降序排列并取 top_n
    for cat in groups:
        groups[cat].sort(key=lambda a: a.get("score", 0), reverse=True)
        groups[cat] = groups[cat][:top_n]

    md_parts = [f"# AI 知识日报 {date}\n"]
    tg_parts = [f"*AI 知识日报 {date}*\n"]
    feishu_combined: dict[str, Any] = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"AI 知识日报 {date}",
                },
                "template": "blue",
            },
            "elements": [],
        },
    }

    for cat, articles in groups.items():
        md_parts.append(f"\n### {cat.upper()}\n")
        for a in articles:
            md_parts.append(json_to_markdown(a))
            md_parts.append("---")

        tg_parts.append(f"\n*{cat.upper()}*")
        for a in articles:
            tg_parts.append(json_to_telegram(a))

        for a in articles:
            feishu_combined["card"]["elements"].append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{cat.upper()}**",
                    },
                }
            )
            card = json_to_feishu(a)
            feishu_combined["card"]["elements"].extend(card["card"]["elements"])

    return {
        "markdown": "\n".join(md_parts),
        "telegram": "\n\n".join(tg_parts),
        "feishu": json.dumps(feishu_combined, ensure_ascii=False),
    }
