#!/usr/bin/env python3
"""MCP Server for AI Knowledge Base — stdio JSON-RPC 2.0.

Provides three tools for AI agents to search the local knowledge base:

  - search_articles(keyword, limit=5)
  - get_article(article_id)
  - knowledge_stats()

Usage::

    python pipeline/mcp_knowledge_server.py

Protocol: MCP over stdio (JSON-RPC 2.0).
No third-party dependencies — stdlib only.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
ARTICLES_DIR = PROJECT_ROOT / "knowledge" / "articles"

SERVER_NAME = "ai-knowledge-base"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------

_cache: list[dict] | None = None


def _load_articles() -> list[dict]:
    global _cache
    if _cache is not None:
        return _cache

    articles: list[dict] = []
    if not ARTICLES_DIR.exists():
        _cache = articles
        return articles

    for path in sorted(ARTICLES_DIR.glob("*.json")):
        if path.name == "index.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                articles.append(data)
        except (json.JSONDecodeError, OSError):
            continue

    _cache = articles
    return articles


def _invalidate_cache() -> None:
    global _cache
    _cache = None


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "search_articles",
        "description": (
            "按关键词搜索知识库文章，匹配标题和摘要。"
            "返回匹配度最高的文章列表（含 id、title、source、score、tags、summary）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词，支持中英文",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回结果数量上限，默认 5",
                    "default": 5,
                },
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "get_article",
        "description": (
            "按文章 ID 获取完整内容，包括标题、摘要、来源、评分、标签等所有字段。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "article_id": {
                    "type": "string",
                    "description": "文章 ID，格式如 github-20260429-001",
                },
            },
            "required": ["article_id"],
        },
    },
    {
        "name": "knowledge_stats",
        "description": (
            "返回知识库统计信息：文章总数、来源分布、评分分布、热门标签 Top 10。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


def _tool_search_articles(keyword: str, limit: int = 5) -> list[dict]:
    articles = _load_articles()
    kw = keyword.lower()

    scored: list[tuple[int, dict]] = []
    for art in articles:
        title = art.get("title", "").lower()
        summary = art.get("summary", "").lower()
        tags = " ".join(art.get("tags", [])).lower()

        rank = 0
        if kw in title:
            rank += 10
        if kw in summary:
            rank += 5
        if kw in tags:
            rank += 3
        if rank == 0:
            continue

        scored.append((rank, art))

    scored.sort(key=lambda x: (x[0], x[1].get("score", 0)), reverse=True)

    results: list[dict] = []
    for _, art in scored[:limit]:
        results.append({
            "id": art.get("id", ""),
            "title": art.get("title", ""),
            "source": art.get("source", ""),
            "source_url": art.get("source_url", ""),
            "score": art.get("score", 0),
            "tags": art.get("tags", []),
            "summary": art.get("summary", ""),
        })
    return results


def _tool_get_article(article_id: str) -> dict | None:
    for art in _load_articles():
        if art.get("id") == article_id:
            return art
    return None


def _tool_knowledge_stats() -> dict[str, Any]:
    articles = _load_articles()

    source_counts: Counter[str] = Counter()
    tag_counts: Counter[str] = Counter()
    score_buckets: dict[str, int] = {
        "9-10": 0,
        "7-8": 0,
        "5-6": 0,
        "1-4": 0,
    }

    for art in articles:
        source_counts[art.get("source", "unknown")] += 1

        for tag in art.get("tags", []):
            tag_counts[tag] += 1

        score = art.get("score", 0)
        if score >= 9:
            score_buckets["9-10"] += 1
        elif score >= 7:
            score_buckets["7-8"] += 1
        elif score >= 5:
            score_buckets["5-6"] += 1
        else:
            score_buckets["1-4"] += 1

    return {
        "total": len(articles),
        "sources": dict(source_counts.most_common()),
        "score_distribution": score_buckets,
        "top_tags": [
            {"tag": tag, "count": count}
            for tag, count in tag_counts.most_common(10)
        ],
    }


TOOL_HANDLERS: dict[str, Any] = {
    "search_articles": _tool_search_articles,
    "get_article": _tool_get_article,
    "knowledge_stats": _tool_knowledge_stats,
}


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 dispatch
# ---------------------------------------------------------------------------

def _make_result(id_: Any, result: Any) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": id_,
        "result": result,
    }


def _make_error(id_: Any, code: int, message: str, data: Any = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {
        "jsonrpc": "2.0",
        "id": id_,
        "error": error,
    }


def _handle_initialize(params: dict) -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {
            "tools": {},
        },
        "serverInfo": {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
        },
    }


def _handle_tools_list(params: dict) -> dict:
    return {
        "tools": TOOL_DEFINITIONS,
    }


def _handle_tools_call(params: dict) -> dict:
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})

    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": f"Unknown tool: {tool_name}",
                },
            ],
        }

    try:
        result = handler(**arguments)
    except TypeError as exc:
        return {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": f"Invalid arguments for {tool_name}: {exc}",
                },
            ],
        }
    except Exception as exc:
        return {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": f"Tool execution failed: {exc}",
                },
            ],
        }

    text = json.dumps(result, ensure_ascii=False, indent=2) if result is not None else "Not found"
    return {
        "content": [
            {
                "type": "text",
                "text": text,
            },
        ],
    }


METHOD_HANDLERS: dict[str, Any] = {
    "initialize": _handle_initialize,
    "notifications/initialized": None,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
}


def _dispatch(request: dict) -> dict | None:
    id_ = request.get("id")
    method = request.get("method", "")
    params = request.get("params", {})

    if method == "notifications/initialized":
        return None

    handler = METHOD_HANDLERS.get(method)
    if handler is None:
        return _make_error(
            id_, -32601,
            f"Method not found: {method}",
        )

    try:
        result = handler(params)
    except Exception as exc:
        return _make_error(id_, -32603, f"Internal error: {exc}")

    return _make_result(id_, result)


# ---------------------------------------------------------------------------
# stdio transport
# ---------------------------------------------------------------------------

def _read_message() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _write_message(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    while True:
        request = _read_message()
        if request is None:
            break

        response = _dispatch(request)
        if response is not None:
            _write_message(response)


if __name__ == "__main__":
    main()
