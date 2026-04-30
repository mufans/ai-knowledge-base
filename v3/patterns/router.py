"""Router pattern — two-layer intent classification with handler dispatch.

Layer 1: Keyword fast-match (zero LLM cost)
Layer 2: LLM classification fallback for ambiguous intents

Three intents:
  - github_search   → GitHub Search API (urllib)
  - knowledge_query → local knowledge/articles/index.json
  - general_chat    → LLM direct answer

Usage::

    from patterns.router import route

    answer = route("最近有什么热门的 AI 项目？")
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Literal, Optional

# ---------------------------------------------------------------------------
# Path setup — allow import of pipeline.model_client when run directly
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from pipeline.model_client import quick_chat  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"

KNOWLEDGE_INDEX_PATH = _PROJECT_ROOT / "knowledge" / "articles" / "index.json"

Intent = Literal["github_search", "knowledge_query", "general_chat"]

# ---------------------------------------------------------------------------
# Layer 1: Keyword rules
# ---------------------------------------------------------------------------

_KEYWORD_RULES: Dict[str, List[str]] = {
    "github_search": [
        "github", "repo", "repository", "star", "fork",
        "开源项目", "代码仓库", "仓库",
    ],
    "knowledge_query": [
        "知识库", "文章", "已收录", "历史文章", "index",
        "knowledge", "article", "收藏",
    ],
}

# Words that strongly indicate a general chat intent
_GENERAL_KEYWORDS = [
    "你好", "hello", "hi", "谢谢", "感谢", "再见",
    "你是谁", "你叫什么", "帮我写", "帮我翻译",
]


def _keyword_match(query: str) -> Optional[Intent]:
    """Return the matched intent or *None* if no keyword rule fires."""
    q_lower = query.lower()

    for intent, keywords in _KEYWORD_RULES.items():
        for kw in keywords:
            if kw in q_lower:
                return intent  # type: ignore[return-value]

    for kw in _GENERAL_KEYWORDS:
        if kw in q_lower:
            return "general_chat"

    return None


# ---------------------------------------------------------------------------
# Layer 2: LLM classification fallback
# ---------------------------------------------------------------------------

_CLASSIFY_SYSTEM = (
    "你是一个意图分类器。根据用户输入判断意图类型，只返回以下三个值之一：\n"
    "  - github_search：用户明确要求搜索 GitHub、查找开源项目或代码仓库\n"
    "  - knowledge_query：用户明确要求查看「知识库」「已收录文章」「历史文章」等本地收藏内容\n"
    "  - general_chat：其他所有情况，包括技术问答、概念解释、对比分析、闲聊等\n\n"
    "注意：一般的「A 和 B 有什么区别」「什么是 X」类问题属于 general_chat，"
    "除非用户明确提到要查知识库或已收录的文章。\n\n"
    "只返回意图标签本身，不要有任何额外文字。"
)


def _llm_classify(query: str) -> Intent:
    """Ask the LLM to classify the intent. Returns one of the three intents."""
    raw = quick_chat(query, system=_CLASSIFY_SYSTEM, temperature=0.0, max_tokens=16)
    label = raw.strip().lower()

    for candidate in ("github_search", "knowledge_query", "general_chat"):
        if candidate in label:
            return candidate  # type: ignore[return-value]

    # Default to general_chat when the response is unparseable
    logger.warning("LLM classification returned unexpected label: %s", raw.strip())
    return "general_chat"


# ---------------------------------------------------------------------------
# Helper: JSON mode chat (model_client has no chat_json, so we build one)
# ---------------------------------------------------------------------------

def _chat_json(prompt: str, *, system: str = "") -> dict:
    """Call the LLM and parse the response as JSON."""
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    raw = quick_chat(prompt, system=system, temperature=0.3, max_tokens=1024)
    # Strip markdown code fences if present
    text = raw.strip()
    if text.startswith("```"):
        first_nl = text.index("\n") + 1 if "\n" in text else 3
        last_fence = text.rfind("```")
        text = text[first_nl:last_fence].strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def handle_github_search(query: str) -> str:
    """Search GitHub repositories via the Search API.

    The query string is URL-encoded with ``urllib.parse.quote`` to handle
    Chinese characters and spaces.
    """
    encoded_q = urllib.parse.quote(query, safe="")
    url = f"{GITHUB_SEARCH_URL}?q={encoded_q}&sort=stars&order=desc&per_page=5"

    headers: Dict[str, str] = {
        "Accept": "application/vnd.github.v3+json",
    }
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.error("GitHub API request failed: %s", exc)
        return f"[GitHub 搜索失败] {exc}"

    items = data.get("items", [])
    if not items:
        return f"未在 GitHub 上找到与「{query}」相关的仓库。"

    lines: list[str] = [f"GitHub 搜索结果（关键词：{query}）：\n"]
    for i, repo in enumerate(items[:5], 1):
        name = repo.get("full_name", "")
        desc = repo.get("description") or "（无描述）"
        stars = repo.get("stargazers_count", 0)
        lang = repo.get("language") or ""
        url_str = repo.get("html_url", "")
        lines.append(f"  {i}. {name}  ⭐{stars}  [{lang}]")
        lines.append(f"     {desc}")
        lines.append(f"     {url_str}")
        lines.append("")

    return "\n".join(lines)


def handle_knowledge_query(query: str) -> str:
    """Search the local knowledge base index for relevant articles."""
    if not KNOWLEDGE_INDEX_PATH.is_file():
        return "[知识库] index.json 文件不存在，无法检索。"

    with open(KNOWLEDGE_INDEX_PATH, encoding="utf-8") as f:
        index_data = json.load(f)

    articles = index_data.get("articles", [])
    if not articles:
        return "[知识库] 当前没有收录任何文章。"

    # Simple keyword scoring across title + tags
    query_lower = query.lower()
    scored: list[tuple[int, dict]] = []
    for art in articles:
        title = art.get("title", "").lower()
        tags = " ".join(art.get("tags", [])).lower()
        text = f"{title} {tags}"
        hits = sum(1 for word in query_lower.split() if word and word in text)
        if hits > 0:
            scored.append((hits, art))

    scored.sort(key=lambda pair: pair[0], reverse=True)

    if not scored:
        return f"[知识库] 未找到与「{query}」相关的文章（共 {len(articles)} 篇）。"

    top = scored[:5]
    lines: list[str] = [f"知识库检索结果（共命中 {len(scored)} 篇）：\n"]
    for hits, art in top:
        title = art.get("title", "（无标题）")
        score = art.get("score", "-")
        tags = ", ".join(art.get("tags", []))
        lines.append(f"  • {title}  (评分: {score})")
        lines.append(f"    标签: {tags}")
    return "\n".join(lines)


def handle_general_chat(query: str) -> str:
    """Let the LLM answer the query directly."""
    return quick_chat(query, system="你是一个友善的 AI 助手。")


# ---------------------------------------------------------------------------
# Handler dispatch table
# ---------------------------------------------------------------------------

_HANDLERS: Dict[Intent, callable] = {
    "github_search": handle_github_search,
    "knowledge_query": handle_knowledge_query,
    "general_chat": handle_general_chat,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def route(query: str) -> str:
    """Classify *query* and dispatch to the appropriate handler.

    Strategy:
      1. Keyword fast-match (zero cost)
      2. LLM classification fallback (for ambiguous queries)

    Returns the handler's result string.
    """
    # Layer 1 — keyword match
    intent = _keyword_match(query)

    if intent is None:
        # Layer 2 — LLM classification
        logger.info("No keyword match, falling back to LLM classification")
        intent = _llm_classify(query)

    logger.info("Routing query to intent: %s", intent)
    handler = _HANDLERS[intent]
    return handler(query)


# ---------------------------------------------------------------------------
# CLI test entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    test_queries = [
        "帮我找一些 GitHub 上关于 RAG 的开源项目",
        "知识库里有哪些关于 agent 的文章？",
        "你好，你是谁？",
        "RAG 和 fine-tuning 有什么区别？",  # ambiguous — LLM fallback
    ]

    print("=" * 60)
    print("Router Pattern — 交互式测试")
    print("输入问题进行路由，输入 q 退出")
    print("=" * 60)

    # If arguments are passed, run them as batch test
    if len(sys.argv) > 1:
        for q in sys.argv[1:]:
            print(f"\n>>> {q}")
            print(route(q))
    else:
        # Interactive mode
        for q in test_queries:
            print(f"\n>>> {q}")
            try:
                print(route(q))
            except Exception as exc:
                print(f"[错误] {exc}")

        print("\n--- 进入交互模式 (输入 q 退出) ---")
        while True:
            try:
                user_input = input("\n>>> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user_input or user_input.lower() == "q":
                break
            try:
                print(route(user_input))
            except Exception as exc:
                print(f"[错误] {exc}")

    print("\n再见！")
