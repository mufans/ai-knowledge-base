"""知识库交互 Bot 模块。

提供基于规则意图识别的命令式交互，支持关键词搜索、日期过滤、
标签过滤、用户订阅管理和三级权限控制。

典型用法::

    from bot.knowledge_bot import KnowledgeBot

    bot = KnowledgeBot()
    reply = bot.handle_message("user_001", "/search RAG agent")
    print(reply)
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import date as date_type
from enum import Enum, auto
from pathlib import Path
from typing import Any

# 确保项目根目录在 sys.path 中，支持 python bot/knowledge_bot.py 直接运行
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from workflows.model_client import quick_chat

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 目录常量
# ---------------------------------------------------------------------------

_KNOWLEDGE_DIR = Path(_PROJECT_ROOT) / "knowledge" / "articles"
_INDEX_FILE = _KNOWLEDGE_DIR / "index.json"
_SYNONYMS_FILE = Path(__file__).resolve().parent / "synonyms.json"


# ===========================================================================
# Intent 枚举
# ===========================================================================


class Intent(Enum):
    """用户意图类型。"""

    SEARCH = auto()
    TODAY = auto()
    TOP = auto()
    SUBSCRIBE = auto()
    UNSUBSCRIBE = auto()
    HELP = auto()
    DETAIL = auto()
    UNKNOWN = auto()


# ===========================================================================
# PermissionLevel 枚举
# ===========================================================================


class PermissionLevel(Enum):
    """用户权限等级。"""

    READ = "read"
    WRITE = "write"
    DELETE = "delete"

    def allows(self, required: PermissionLevel) -> bool:
        """判断当前权限是否满足所需权限。

        权限等级: DELETE > WRITE > READ。

        Args:
            required: 所需的最低权限等级。

        Returns:
            当前权限是否 >= 所需权限。
        """
        hierarchy = {PermissionLevel.READ: 0, PermissionLevel.WRITE: 1, PermissionLevel.DELETE: 2}
        return hierarchy.get(self, -1) >= hierarchy.get(required, -1)


# ===========================================================================
# PermissionManager
# ===========================================================================


class PermissionManager:
    """三级权限管理器（READ / WRITE / DELETE）。

    提供用户权限的查询和设置。默认所有用户为 READ 权限。
    """

    def __init__(self) -> None:
        self._permissions: dict[str, PermissionLevel] = {}

    def get_permission(self, user_id: str) -> PermissionLevel:
        """获取用户权限等级。

        Args:
            user_id: 用户标识。

        Returns:
            用户权限等级，未设置则返回 READ。
        """
        return self._permissions.get(user_id, PermissionLevel.READ)

    def set_permission(self, user_id: str, level: PermissionLevel | str) -> None:
        """设置用户权限等级。

        Args:
            user_id: 用户标识。
            level: 目标权限等级，支持枚举或字符串（"read"/"write"/"delete"）。
        """
        if isinstance(level, str):
            level = PermissionLevel(level.lower())
        self._permissions[user_id] = level

    def check(self, user_id: str, required: PermissionLevel) -> bool:
        """检查用户是否拥有指定权限。

        Args:
            user_id: 用户标识。
            required: 所需的最低权限等级。

        Returns:
            是否通过权限检查。
        """
        return self.get_permission(user_id).allows(required)


# ===========================================================================
# SubscriptionManager
# ===========================================================================


class SubscriptionManager:
    """用户订阅管理器。

    管理用户对特定标签的订阅关系，支持增删查。
    """

    def __init__(self) -> None:
        self._subscriptions: dict[str, set[str]] = {}

    def subscribe(self, user_id: str, tags: list[str]) -> list[str]:
        """为用户添加标签订阅。

        Args:
            user_id: 用户标识。
            tags: 要订阅的标签列表。

        Returns:
            实际新增的标签列表（去重）。
        """
        current = self._subscriptions.setdefault(user_id, set())
        added = [t for t in tags if t not in current]
        current.update(tags)
        return added

    def unsubscribe(self, user_id: str, tags: list[str]) -> list[str]:
        """为用户移除标签订阅。

        Args:
            user_id: 用户标识。
            tags: 要取消的标签列表。

        Returns:
            实际移除的标签列表。
        """
        current = self._subscriptions.get(user_id, set())
        removed = [t for t in tags if t in current]
        current -= set(tags)
        return removed

    def get_subscriptions(self, user_id: str) -> list[str]:
        """获取用户的所有订阅标签。

        Args:
            user_id: 用户标识。

        Returns:
            排序后的标签列表。
        """
        return sorted(self._subscriptions.get(user_id, set()))

    def get_subscribers(self, tag: str) -> list[str]:
        """获取订阅了指定标签的所有用户。

        Args:
            tag: 标签名称。

        Returns:
            订阅了该标签的用户 ID 列表。
        """
        return [uid for uid, tags in self._subscriptions.items() if tag in tags]


# ===========================================================================
# KnowledgeSearchEngine
# ===========================================================================


class KnowledgeSearchEngine:
    """知识库搜索引擎。

    基于内存索引，支持关键词、标签、日期范围的多维过滤。
    """

    def __init__(self, knowledge_dir: str | Path | None = None) -> None:
        """初始化搜索引擎并加载索引。

        Args:
            knowledge_dir: 文章 JSON 存放目录，默认为项目内 knowledge/articles/。
        """
        self._dir = Path(knowledge_dir) if knowledge_dir else _KNOWLEDGE_DIR
        self._index: list[dict[str, Any]] = []
        self._synonyms: dict[str, list[str]] = {}
        self._load_index()
        self._load_synonyms()

    def _load_synonyms(self) -> None:
        """加载 synonyms.json 同义词表。"""
        if _SYNONYMS_FILE.exists():
            with open(_SYNONYMS_FILE, encoding="utf-8") as f:
                self._synonyms = json.load(f)

    @staticmethod
    def _expand_keyword(kw: str, synonyms: dict[str, list[str]]) -> list[str]:
        """扩展单个关键词为同义词列表（包含自身）。

        Args:
            kw: 原始关键词（小写）。
            synonyms: 同义词映射表。

        Returns:
            包含自身和所有同义词的列表。
        """
        expanded = {kw}
        for key, vals in synonyms.items():
            group = {key.lower()} | {v.lower() for v in vals}
            if kw in group:
                expanded |= group
        return list(expanded)

    def _load_index(self) -> None:
        """从 index.json 加载文章索引到内存。"""
        if _INDEX_FILE.exists():
            with open(_INDEX_FILE, encoding="utf-8") as f:
                data = json.load(f)
            self._index = data.get("articles", [])
        else:
            self._index = []

    def reload(self) -> None:
        """重新加载索引和同义词表（用于数据更新后刷新）。"""
        self._load_index()
        self._load_synonyms()

    def search(
        self,
        keywords: str | None = None,
        tags: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        source: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """按多条件搜索文章。

        Args:
            keywords: 关键词，空格分隔，匹配标题和摘要（AND 逻辑）。
            tags: 标签过滤列表（OR 逻辑）。
            date_from: 起始日期 (YYYY-MM-DD)，包含。
            date_to: 截止日期 (YYYY-MM-DD)，包含。
            source: 来源过滤 (github / rss)。
            limit: 返回条数上限。

        Returns:
            匹配的文章索引条目列表，按 score 降序排列。
        """
        results = list(self._index)

        # 关键词过滤（同义词扩展）
        if keywords:
            kw_list = keywords.lower().split()
            expanded_groups = [
                self._expand_keyword(kw, self._synonyms) for kw in kw_list
            ]
            filtered = []
            for entry in results:
                text = f"{entry.get('title', '')} {' '.join(entry.get('tags', []))}".lower()
                # AND 逻辑：每个关键词组中至少有一个同义词命中
                if all(any(syn in text for syn in group) for group in expanded_groups):
                    filtered.append(entry)
            results = filtered

        # 标签过滤 (OR)
        if tags:
            tags_lower = {t.lower() for t in tags}
            results = [
                e for e in results
                if tags_lower & {t.lower() for t in e.get("tags", [])}
            ]

        # 日期范围过滤
        if date_from or date_to:
            filtered = []
            for entry in results:
                file_prefix = entry.get("file", "")[:10]
                if not file_prefix:
                    continue
                if date_from and file_prefix < date_from:
                    continue
                if date_to and file_prefix > date_to:
                    continue
                filtered.append(entry)
            results = filtered

        # 来源过滤
        if source:
            results = [e for e in results if e.get("source", "") == source]

        # 按 score 降序
        results.sort(key=lambda e: e.get("score", 0), reverse=True)
        return results[:limit]

    def get_today(self, limit: int = 10) -> list[dict[str, Any]]:
        """获取今日文章。

        Args:
            limit: 返回条数上限。

        Returns:
            今日文章列表，按 score 降序。
        """
        today = date_type.today().isoformat()
        return [
            e for e in self._index
            if e.get("file", "").startswith(today)
        ][:limit]

    def get_top(self, n: int = 10) -> list[dict[str, Any]]:
        """获取评分最高的 N 篇文章。

        Args:
            n: 取前 N 篇。

        Returns:
            按 score 降序排列的文章列表。
        """
        sorted_all = sorted(self._index, key=lambda e: e.get("score", 0), reverse=True)
        return sorted_all[:n]

    def get_article(self, file: str) -> dict[str, Any] | None:
        """读取单篇文章完整内容。

        Args:
            file: 文章文件名。

        Returns:
            文章字典，文件不存在则返回 None。
        """
        path = self._dir / file
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """使用 LLM 对候选结果重排，返回最相关的 top_k 条。

        Args:
            query: 用户原始查询文本。
            candidates: 规则匹配后的候选列表。
            top_k: 返回条数。

        Returns:
            LLM 重排后的文章列表。
        """
        if len(candidates) <= top_k:
            return candidates

        # 构建候选摘要
        items_text = ""
        for i, entry in enumerate(candidates):
            items_text += (
                f"[{i}] {entry.get('title', '')} | "
                f"tags: {', '.join(entry.get('tags', [])[:5])} | "
                f"score: {entry.get('score', 0)}\n"
            )

        prompt = (
            f"用户查询: {query}\n\n"
            f"候选文章列表:\n{items_text}\n"
            f"请根据用户查询，从上述候选中选出最相关的 {top_k} 篇，按相关度从高到低排列。\n"
            f"只输出序号列表，格式为 JSON 数组，例如: [2, 0, 5, 1, 8]\n"
            f"不要输出其他内容。"
        )

        try:
            raw = quick_chat(
                prompt,
                system="你是一个搜索重排助手，只输出 JSON 数组。",
                temperature=0.1,
                max_tokens=128,
            )
            # 解析 LLM 返回的序号列表
            match = re.search(r"\[[\d,\s]+\]", raw)
            if not match:
                logger.warning("LLM rerank 返回格式异常: %s", raw[:100])
                return candidates[:top_k]

            indices: list[int] = json.loads(match.group(0))
            reranked = []
            for idx in indices:
                if 0 <= idx < len(candidates) and candidates[idx] not in reranked:
                    reranked.append(candidates[idx])
                if len(reranked) >= top_k:
                    break

            # LLM 返回不足时用原始顺序补齐
            for entry in candidates:
                if entry not in reranked:
                    reranked.append(entry)
                if len(reranked) >= top_k:
                    break

            return reranked[:top_k]

        except Exception as exc:
            logger.error("LLM rerank 失败: %s", exc)
            return candidates[:top_k]


# ===========================================================================
# 意图识别
# ===========================================================================

# 命令前缀 → Intent 映射
_COMMAND_MAP: dict[str, Intent] = {
    "/search": Intent.SEARCH,
    "/s": Intent.SEARCH,
    "/today": Intent.TODAY,
    "/t": Intent.TODAY,
    "/top": Intent.TOP,
    "/detail": Intent.DETAIL,
    "/d": Intent.DETAIL,
    "/view": Intent.DETAIL,
    "/subscribe": Intent.SUBSCRIBE,
    "/sub": Intent.SUBSCRIBE,
    "/unsubscribe": Intent.UNSUBSCRIBE,
    "/unsub": Intent.UNSUBSCRIBE,
    "/help": Intent.HELP,
    "/h": Intent.HELP,
}

# 自然语言关键词 → Intent 映射
_NL_PATTERNS: list[tuple[re.Pattern[str], Intent]] = [
    (re.compile(r"(详情|详细|查看详情|查看文章|看看|详情页)"), Intent.DETAIL),
    (re.compile(r"(搜索|查询|查找|搜一下|找一下)"), Intent.SEARCH),
    (re.compile(r"(今天|今日|日报|简报|daily)"), Intent.TODAY),
    (re.compile(r"(热门|推荐|排行|top|高分)"), Intent.TOP),
    (re.compile(r"(订阅|关注|subscribe)"), Intent.SUBSCRIBE),
    (re.compile(r"(取消订阅|退订|unsubscribe)"), Intent.UNSUBSCRIBE),
    (re.compile(r"(帮助|帮忙|怎么用|help|使用说明)"), Intent.HELP),
]


def recognize_intent(text: str) -> tuple[Intent, str]:
    """识别用户输入的意图和参数。

    优先匹配命令前缀（如 /search），再匹配自然语言关键词。

    Args:
        text: 用户输入文本。

    Returns:
        (Intent 枚举, 参数字符串) 的二元组。
        参数为命令前缀后的剩余文本，或自然语言中去除关键词后的文本。
    """
    text = text.strip()
    if not text:
        return Intent.UNKNOWN, ""

    # 1. 命令前缀匹配
    parts = text.split(None, 1)
    cmd = parts[0].lower()
    if cmd in _COMMAND_MAP:
        args = parts[1] if len(parts) > 1 else ""
        return _COMMAND_MAP[cmd], args.strip()

    # 2. 自然语言关键词匹配
    for pattern, intent in _NL_PATTERNS:
        match = pattern.search(text)
        if match:
            # 去掉匹配的关键词，剩余部分作为参数
            args = text[match.end():].strip()
            return intent, args

    # 3. 无法识别 → 默认为搜索
    return Intent.SEARCH, text


# ===========================================================================
# KnowledgeBot
# ===========================================================================


class KnowledgeBot:
    """知识库交互 Bot 主入口。

    整合搜索引擎、订阅管理、权限控制，通过 handle_message 统一处理用户消息。

    Usage::

        bot = KnowledgeBot()
        reply = bot.handle_message("user_001", "/search RAG agent")
    """

    def __init__(self, knowledge_dir: str | Path | None = None) -> None:
        """初始化 Bot。

        Args:
            knowledge_dir: 文章 JSON 存放目录，默认为项目内 knowledge/articles/。
        """
        self._engine = KnowledgeSearchEngine(knowledge_dir)
        self._subscription = SubscriptionManager()
        self._permission = PermissionManager()
        self._last_results: list[dict[str, Any]] = []

    @property
    def engine(self) -> KnowledgeSearchEngine:
        """获取搜索引擎实例。"""
        return self._engine

    @property
    def subscription(self) -> SubscriptionManager:
        """获取订阅管理器实例。"""
        return self._subscription

    @property
    def permission(self) -> PermissionManager:
        """获取权限管理器实例。"""
        return self._permission

    # -------------------------------------------------------------------
    # 主入口
    # -------------------------------------------------------------------

    def handle_message(self, user_id: str, text: str) -> str:
        """处理用户消息的统一入口。

        Args:
            user_id: 用户标识。
            text: 用户输入文本。

        Returns:
            Bot 回复文本。
        """
        intent, args = recognize_intent(text)

        handler_map = {
            Intent.SEARCH: self._handle_search,
            Intent.TODAY: self._handle_today,
            Intent.TOP: self._handle_top,
            Intent.DETAIL: self._handle_detail,
            Intent.SUBSCRIBE: self._handle_subscribe,
            Intent.UNSUBSCRIBE: self._handle_unsubscribe,
            Intent.HELP: self._handle_help,
        }

        handler = handler_map.get(intent)
        if handler:
            return handler(user_id, args)
        return "未能识别您的意图，输入 /help 查看可用命令。"

    # -------------------------------------------------------------------
    # 处理器
    # -------------------------------------------------------------------

    def _handle_search(self, user_id: str, args: str) -> str:
        """处理搜索请求。

        Args:
            user_id: 用户标识。
            args: 搜索关键词。

        Returns:
            搜索结果文本。
        """
        if not self._permission.check(user_id, PermissionLevel.READ):
            return "权限不足，您没有搜索权限。"

        if not args:
            return "请提供搜索关键词，例如: /search RAG agent"

        candidates = self._engine.search(keywords=args, limit=10)
        results = self._engine.rerank(query=args, candidates=candidates, top_k=5)
        self._last_results = results

        if not results:
            return f"未找到与「{args}」相关的文章。"

        lines = [f"搜索「{args}」共找到 {len(results)} 条结果 (用 /detail <序号> 查看详情):\n"]
        for i, entry in enumerate(results, 1):
            emoji = _score_emoji(entry.get("score", 0))
            tags = " ".join(f"#{t}" for t in entry.get("tags", [])[:3])
            aid = entry.get("id", "")
            lines.append(
                f"{i}. {emoji} **{entry.get('title', '')}** "
                f"({aid}) {tags}"
            )
        return "\n".join(lines)

    def _handle_today(self, user_id: str, args: str) -> str:
        """处理今日简报请求。

        Args:
            user_id: 用户标识。
            args: 附加参数（未使用）。

        Returns:
            今日文章列表文本。
        """
        if not self._permission.check(user_id, PermissionLevel.READ):
            return "权限不足，您没有查看权限。"

        results = self._engine.get_today(limit=10)
        self._last_results = results

        if not results:
            today = date_type.today().isoformat()
            return f"{today} 暂无新增文章。"

        lines = [f"今日简报 (共 {len(results)} 条，用 /detail <序号> 查看详情):\n"]
        for i, entry in enumerate(results, 1):
            emoji = _score_emoji(entry.get("score", 0))
            source = entry.get("source", "")
            aid = entry.get("id", "")
            lines.append(f"{i}. {emoji} {entry.get('title', '')} [{source}] ({aid})")
        return "\n".join(lines)

    def _handle_top(self, user_id: str, args: str) -> str:
        """处理热门推荐请求。

        Args:
            user_id: 用户标识。
            args: 可选数字参数，指定取前 N 篇。

        Returns:
            Top N 文章列表文本。
        """
        if not self._permission.check(user_id, PermissionLevel.READ):
            return "权限不足，您没有查看权限。"

        n = 10
        if args:
            try:
                n = min(int(args.strip()), 20)
            except ValueError:
                pass

        results = self._engine.get_top(n)
        self._last_results = results

        if not results:
            return "暂无文章数据。"

        lines = [f"热门推荐 Top {len(results)} (用 /detail <序号> 查看详情):\n"]
        for i, entry in enumerate(results, 1):
            emoji = _score_emoji(entry.get("score", 0))
            tags = " ".join(f"#{t}" for t in entry.get("tags", [])[:3])
            aid = entry.get("id", "")
            lines.append(
                f"{i}. {emoji} **{entry.get('title', '')}** "
                f"({aid}) {tags}"
            )
        return "\n".join(lines)

    def _handle_detail(self, user_id: str, args: str) -> str:
        """处理文章详情查看请求。

        支持通过序号（最近一次搜索/列表结果的编号）或文章 ID 查看详情。

        Args:
            user_id: 用户标识。
            args: 序号（如 1）或文章 ID（如 github-20260509-001）。

        Returns:
            文章详情文本。
        """
        if not self._permission.check(user_id, PermissionLevel.READ):
            return "权限不足，您没有查看权限。"

        if not args:
            return "请指定序号或文章 ID，例如: /detail 1 或 /detail github-20260509-001"

        # 1. 尝试按序号查找（从上次搜索结果）
        entry = None
        try:
            idx = int(args.strip())
            if 1 <= idx <= len(self._last_results):
                entry = self._last_results[idx - 1]
        except ValueError:
            pass

        # 2. 按文章 ID 查找
        if entry is None:
            for e in self._engine._index:
                if e.get("id", "") == args.strip():
                    entry = e
                    break

        if entry is None:
            return f"未找到「{args}」。请先搜索，再用序号查看，或使用完整文章 ID。"

        # 读取完整文章
        article = self._engine.get_article(entry.get("file", ""))
        if not article:
            return f"文章文件不存在: {entry.get('file', '')}"

        score = article.get("score", 0)
        emoji = _score_emoji(score)
        tags = " ".join(f"`{t}`" for t in article.get("tags", []))
        source_url = article.get("source_url", "")

        lines = [
            f"{'=' * 45}",
            f"  {article.get('title', '')}",
            f"{'=' * 45}",
            f"  ID:       {article.get('id', '')}",
            f"  来源:     {article.get('source', '')}",
            f"  评分:     {emoji} {score}",
            f"  分类:     {article.get('category', '')}",
            f"  标签:     {tags}",
            f"  作者:     {article.get('author', '')}",
            f"  采集时间: {article.get('collected_at', '')[:10]}",
            f"  状态:     {article.get('status', '')}",
            f"{'-' * 45}",
        ]

        summary = article.get("summary", "")
        if summary:
            lines.append(summary)

        key_insight = article.get("key_insight", "")
        if key_insight:
            lines.append(f"\n💡 {key_insight}")

        if source_url:
            lines.append(f"\n🔗 {source_url}")

        return "\n".join(lines)

    def _handle_subscribe(self, user_id: str, args: str) -> str:
        """处理订阅请求。

        需要 WRITE 权限。

        Args:
            user_id: 用户标识。
            args: 要订阅的标签，空格分隔。

        Returns:
            订阅结果文本。
        """
        if not self._permission.check(user_id, PermissionLevel.WRITE):
            return "权限不足，订阅需要 WRITE 权限。"

        if not args:
            current = self._subscription.get_subscriptions(user_id)
            if current:
                return f"当前订阅标签: {', '.join(current)}"
            return "您暂无订阅。使用 /subscribe tag1 tag2 添加订阅。"

        tags = [t.strip().lower() for t in args.split() if t.strip()]
        if not tags:
            return "请提供要订阅的标签，例如: /subscribe rag agent llm"

        added = self._subscription.subscribe(user_id, tags)
        all_tags = self._subscription.get_subscriptions(user_id)

        if added:
            return f"已订阅: {', '.join(added)}\n当前全部订阅: {', '.join(all_tags)}"
        return f"标签已存在于订阅中。当前订阅: {', '.join(all_tags)}"

    def _handle_unsubscribe(self, user_id: str, args: str) -> str:
        """处理取消订阅请求。

        需要 WRITE 权限。

        Args:
            user_id: 用户标识。
            args: 要取消的标签，空格分隔。

        Returns:
            取消订阅结果文本。
        """
        if not self._permission.check(user_id, PermissionLevel.WRITE):
            return "权限不足，管理订阅需要 WRITE 权限。"

        if not args:
            return "请提供要取消的标签，例如: /unsubscribe rag agent"

        tags = [t.strip().lower() for t in args.split() if t.strip()]
        removed = self._subscription.unsubscribe(user_id, tags)

        if removed:
            return f"已取消订阅: {', '.join(removed)}"
        return "指定标签不在您的订阅列表中。"

    def _handle_help(self, user_id: str, args: str) -> str:
        """处理帮助请求。

        Args:
            user_id: 用户标识。
            args: 附加参数（未使用）。

        Returns:
            帮助文本。
        """
        perm = self._permission.get_permission(user_id)
        return (
            "AI 知识库 Bot 命令列表:\n\n"
            "搜索查询:\n"
            "  /search <关键词>  — 搜索文章（支持空格分隔多词 AND 查询）\n"
            "  /today            — 查看今日简报\n"
            "  /top [N]          — 查看 Top N 热门文章（默认 10）\n"
            "  /detail <序号|ID> — 查看文章详情\n\n"
            "订阅管理 (需 WRITE 权限):\n"
            "  /subscribe <标签> — 订阅标签（空格分隔多个）\n"
            "  /unsubscribe <标签> — 取消标签订阅\n\n"
            "  /help             — 显示本帮助\n\n"
            f"当前权限: {perm.value}"
        )


# ===========================================================================
# 辅助函数
# ===========================================================================


def _score_emoji(score: int) -> str:
    """根据评分返回 emoji。

    Args:
        score: 0-10 评分。

    Returns:
        对应的 emoji 字符。
    """
    if score >= 8:
        return "🟢"
    if score >= 6:
        return "🟡"
    return "🔴"


# ===========================================================================
# CLI 交互主循环
# ===========================================================================


def run_cli(user_id: str = "cli_user", permission: str = "write") -> None:
    """启动 CLI 交互式主循环。

    Args:
        user_id: 默认用户 ID。
        permission: 默认权限等级 (read/write/delete)。
    """
    bot = KnowledgeBot()
    bot.permission.set_permission(user_id, permission)

    print("=" * 50)
    print("  AI 知识库 Bot  (输入 /help 查看命令，q 退出)")
    print("=" * 50)

    while True:
        try:
            text = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见!")
            break

        if not text:
            continue

        if text.lower() in ("quit", "q", "exit"):
            print("再见!")
            break

        reply = bot.handle_message(user_id, text)
        print(reply)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="AI 知识库 Bot CLI")
    parser.add_argument("--user", default="cli_user", help="用户 ID (默认: cli_user)")
    parser.add_argument(
        "--permission",
        default="write",
        choices=["read", "write", "delete"],
        help="权限等级 (默认: write)",
    )
    args = parser.parse_args()
    run_cli(user_id=args.user, permission=args.permission)
