"""Multi-channel publisher module for article digest distribution.

Provides async publishers for Telegram and Feishu, with a unified entry
point ``publish_daily_digest()`` that formats and sends to all channels
concurrently.

Environment variables:
    TELEGRAM_BOT_TOKEN: Telegram Bot API token.
    TELEGRAM_CHAT_ID: Target chat ID for Telegram messages.
    FEISHU_APP_ID: 飞书应用 ID。
    FEISHU_APP_SECRET: 飞书应用密钥。
    FEISHU_CHAT_ID: 飞书目标群聊 ID（oc_xxx）。

Feishu 配置:
    1. 飞书开放平台 → 创建「企业自建应用」
    2. 应用需开通「机器人」能力并申请 im:message:send_as_bot 权限
    3. 将机器人添加到目标群聊
    4. 配置环境变量:
       FEISHU_APP_ID=cli_xxxxxxxx
       FEISHU_APP_SECRET=xxxxxxxx
       FEISHU_CHAT_ID=oc_xxxxxxxx
    5. tenant_access_token 会在发送时自动获取，通过 Authorization header 鉴权

    参考: https://open.feishu.cn/document/server-docs/im-v1/message/create
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

from distribution.formatter import (
    generate_daily_digest,
    json_to_telegram,
    json_to_feishu,
)

logger = logging.getLogger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


# ── PublishResult ───────────────────────────────────────────────────


@dataclass
class PublishResult:
    """记录单次发布操作的结果。

    Attributes:
        channel: 发布渠道名称（如 "telegram", "feishu"）。
        success: 是否发布成功。
        message_id: 成功时返回的消息 ID（Telegram 为 message_id，
            飞书无则留空）。
        error: 失败时的错误信息。
    """

    channel: str
    success: bool
    message_id: str = ""
    error: str = ""


# ── BasePublisher ───────────────────────────────────────────────────


class BasePublisher(ABC):
    """发布器抽象基类。

    子类需实现 send_message 和 send_digest 两个异步方法。
    """

    @abstractmethod
    async def send_message(self, content: str) -> PublishResult:
        """发送单条消息。

        Args:
            content: 已格式化的消息内容。

        Returns:
            PublishResult 实例。
        """

    @abstractmethod
    async def send_digest(
        self,
        knowledge_dir: str = "knowledge/articles",
        date: str | None = None,
        top_n: int = 5,
    ) -> PublishResult:
        """生成并发送每日简报。

        Args:
            knowledge_dir: 文章 JSON 存放目录。
            date: 日期字符串，默认今天。
            top_n: 每个 category 取前 N 篇。

        Returns:
            PublishResult 实例。
        """


# ── TelegramPublisher ───────────────────────────────────────────────


class TelegramPublisher(BasePublisher):
    """通过 Telegram Bot API 发送 MarkdownV2 消息。

    Args:
        bot_token: Telegram Bot Token，默认从环境变量
            TELEGRAM_BOT_TOKEN 读取。
        chat_id: 目标 Chat ID，默认从环境变量 TELEGRAM_CHAT_ID 读取。
        timeout: 请求超时秒数，默认 30。
    """

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.bot_token = bot_token or os.environ["TELEGRAM_BOT_TOKEN"]
        self.chat_id = chat_id or os.environ["TELEGRAM_CHAT_ID"]
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._api_url = f"{_TELEGRAM_API_BASE}/bot{self.bot_token}/sendMessage"

    async def send_message(self, content: str) -> PublishResult:
        """发送 Telegram MarkdownV2 消息。

        Args:
            content: MarkdownV2 格式的消息文本。

        Returns:
            PublishResult，成功时 message_id 为 Telegram message_id。
        """
        payload = {
            "chat_id": self.chat_id,
            "text": content,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(self._api_url, json=payload) as resp:
                    body = await resp.json()
                    if resp.status == 200 and body.get("ok"):
                        msg_id = str(body["result"]["message_id"])
                        logger.info("Telegram message sent: %s", msg_id)
                        return PublishResult(
                            channel="telegram", success=True, message_id=msg_id
                        )
                    error_desc = body.get("description", resp.reason)
                    logger.error("Telegram API error: %s", error_desc)
                    return PublishResult(
                        channel="telegram", success=False, error=str(error_desc)
                    )
        except Exception as exc:
            logger.exception("Telegram send failed")
            return PublishResult(channel="telegram", success=False, error=str(exc))

    async def send_digest(
        self,
        knowledge_dir: str = "knowledge/articles",
        date: str | None = None,
        top_n: int = 5,
    ) -> PublishResult:
        """生成 Telegram 简报并发送。

        Args:
            knowledge_dir: 文章 JSON 存放目录。
            date: 日期字符串，默认今天。
            top_n: 每个 category 取前 N 篇。

        Returns:
            PublishResult 实例。
        """
        digest = generate_daily_digest(knowledge_dir, date, top_n)
        return await self.send_message(digest["telegram"])


# ── FeishuPublisher ─────────────────────────────────────────────────


class FeishuPublisher(BasePublisher):
    """通过飞书 IM API 发送消息到群聊。

    使用 app_id + app_secret 动态获取 tenant_access_token，
    通过 Authorization header 鉴权，调用 /im/v1/messages 接口发送。

    环境变量:
        FEISHU_APP_ID: 飞书应用 ID。
        FEISHU_APP_SECRET: 飞书应用密钥。
        FEISHU_CHAT_ID: 目标群聊 ID（oc_xxx）。

    Args:
        app_id: 飞书应用 ID，默认从环境变量读取。
        app_secret: 飞书应用密钥，默认从环境变量读取。
        chat_id: 目标群聊 ID，默认从环境变量读取。
        timeout: 请求超时秒数，默认 30。
    """

    _TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    _SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

    def __init__(
        self,
        app_id: str | None = None,
        app_secret: str | None = None,
        chat_id: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.app_id = app_id or os.environ.get("FEISHU_APP_ID", "")
        self.app_secret = app_secret or os.environ.get("FEISHU_APP_SECRET", "")
        self.chat_id = chat_id or os.environ.get("FEISHU_CHAT_ID", "")
        self.timeout = aiohttp.ClientTimeout(total=timeout)

        if not self.app_id or not self.app_secret:
            logger.warning("FEISHU_APP_ID / FEISHU_APP_SECRET 未设置")
        if not self.chat_id:
            logger.warning("FEISHU_CHAT_ID 未设置")

    async def _get_tenant_token(self) -> str | None:
        """用 app_id + app_secret 获取 tenant_access_token。

        Returns:
            tenant_access_token 字符串，失败返回 None。
        """
        payload = {"app_id": self.app_id, "app_secret": self.app_secret}
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(self._TOKEN_URL, json=payload) as resp:
                    data = await resp.json()
                    if data.get("code") == 0:
                        return data["tenant_access_token"]
                    logger.error("[飞书] 获取 token 失败: %s", data)
                    return None
        except Exception as exc:
            logger.error("[飞书] 获取 token 异常: %s", exc)
            return None

    async def send_message(self, content: str | dict) -> PublishResult:
        """通过 IM API 发送消息到飞书群聊。

        支持:
        - dict: 直接作为卡片消息体发送（需含 msg_type 和 card 字段）
        - str: 尝试 JSON 解析，失败则包装为纯文本

        Args:
            content: 消息内容。

        Returns:
            PublishResult 实例。
        """
        if not self.chat_id:
            return PublishResult(
                channel="feishu", success=False, error="未配置 FEISHU_CHAT_ID"
            )

        token = await self._get_tenant_token()
        if not token:
            return PublishResult(
                channel="feishu", success=False, error="获取 tenant_access_token 失败"
            )

        # 解析 content 得到 msg_type 和 card/content
        if isinstance(content, dict):
            msg_type = content.get("msg_type", "interactive")
            card_data = content.get("card", content)
        elif isinstance(content, str):
            try:
                parsed = json.loads(content)
                msg_type = parsed.get("msg_type", "interactive")
                card_data = parsed.get("card", parsed)
            except json.JSONDecodeError:
                msg_type = "text"
                card_data = {"text": content}
        else:
            msg_type = "text"
            card_data = {"text": str(content)}

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        payload = {
            "receive_id": self.chat_id,
            "msg_type": msg_type,
            "content": json.dumps(card_data, ensure_ascii=False),
        }
        params = {"receive_id_type": "chat_id"}

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(
                    self._SEND_URL, json=payload, headers=headers, params=params
                ) as resp:
                    data = await resp.json()
                    if data.get("code") == 0:
                        msg_id = (data.get("data") or {}).get("message_id", "")
                        logger.info("[飞书] 消息发送成功: %s", msg_id)
                        return PublishResult(
                            channel="feishu", success=True, message_id=msg_id
                        )
                    error = data.get("msg", "未知错误")
                    logger.error("[飞书] 发送失败: %s", error)
                    return PublishResult(
                        channel="feishu", success=False, error=str(error)
                    )
        except asyncio.TimeoutError:
            return PublishResult(
                channel="feishu", success=False, error="请求超时（30s）"
            )
        except aiohttp.ClientError as exc:
            return PublishResult(
                channel="feishu", success=False, error=f"网络错误: {exc}"
            )

    async def send_digest(
        self,
        knowledge_dir: str = "knowledge/articles",
        date: str | None = None,
        top_n: int = 5,
    ) -> PublishResult:
        """生成飞书卡片简报并发送。

        Args:
            knowledge_dir: 文章 JSON 存放目录。
            date: 日期字符串，默认今天。
            top_n: 每个 category 取前 N 篇。

        Returns:
            PublishResult 实例。
        """
        digest = generate_daily_digest(knowledge_dir, date, top_n)
        return await self.send_message(digest["feishu"])


# ── Unified entry ───────────────────────────────────────────────────


async def publish_daily_digest(
    knowledge_dir: str = "knowledge/articles",
    date: str | None = None,
    top_n: int = 5,
) -> list[PublishResult]:
    """统一异步入口：生成简报并并发发布到所有已配置渠道。

    自动检测环境变量来决定启用哪些渠道:
    - TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID → TelegramPublisher
    - FEISHU_WEBHOOK_URL → FeishuPublisher

    Args:
        knowledge_dir: 文章 JSON 存放目录。
        date: 日期字符串，默认今天。
        top_n: 每个 category 取前 N 篇。

    Returns:
        各渠道 PublishResult 的列表。
    """
    publishers: list[BasePublisher] = []

    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        publishers.append(TelegramPublisher())

    if os.environ.get("FEISHU_APP_ID") and os.environ.get("FEISHU_CHAT_ID"):
        publishers.append(FeishuPublisher())

    if not publishers:
        logger.warning("No publisher configured, skipping")
        return [
            PublishResult(
                channel="none", success=False, error="No publisher configured"
            )
        ]

    tasks = [p.send_digest(knowledge_dir, date, top_n) for p in publishers]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    return list(results)


# ── File export ─────────────────────────────────────────────────────


def publish_file(
    content: str,
    filename: str | None = None,
    output_dir: str = "output",
) -> str:
    """将简报内容导出为 Markdown 文件。

    Args:
        content: Markdown 格式的简报内容。
        filename: 输出文件名，默认 ``digest-{today}.md``。
        output_dir: 输出目录，默认 "output"。

    Returns:
        写入文件的绝对路径。
    """
    if filename is None:
        from datetime import date as date_type

        filename = f"digest-{date_type.today().isoformat()}.md"

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    file_path = out_path / filename
    file_path.write_text(content, encoding="utf-8")
    logger.info("简报已导出: %s", file_path.resolve())
    return str(file_path.resolve())
