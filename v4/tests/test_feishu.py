"""飞书消息发送测试脚本。

用法:
    # 1. 设置环境变量
    export FEISHU_APP_ID="cli_xxxxxxxx"
    export FEISHU_APP_SECRET="xxxxxxxx"
    export FEISHU_CHAT_ID="oc_xxxxxxxx"

    # 2. 运行测试
    python tests/test_feishu.py

    # 3. 或指定日期测试简报
    python tests/test_feishu.py --date 2026-05-09 --top-n 3

FEISHU_CHAT_ID 获取方式:
    方法1: 通过 API 获取群列表
        先设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET，然后运行:
        python tests/test_feishu.py --list-chats
    方法2: 在飞书群聊设置中查看
        打开群聊 → 设置 → 群名片 → 更多信息 → 群 ID
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

# 自动切换到项目 venv
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_PYTHON = os.path.join(_ROOT, ".venv", "bin", "python3")

if sys.executable != _VENV_PYTHON and os.path.isfile(_VENV_PYTHON):
    os.execv(_VENV_PYTHON, [_VENV_PYTHON] + sys.argv)

# 确保项目根目录在 sys.path 中
sys.path.insert(0, _ROOT)

from distribution.publisher import FeishuPublisher, PublishResult


async def _get_token() -> str | None:
    """获取 tenant_access_token。"""
    import aiohttp

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    payload = {
        "app_id": os.environ.get("FEISHU_APP_ID", ""),
        "app_secret": os.environ.get("FEISHU_APP_SECRET", ""),
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            data = await resp.json()
            if data.get("code") == 0:
                return data["tenant_access_token"]
            print(f"获取 token 失败: {data}")
            return None


async def list_chats() -> None:
    """列出机器人所在的所有群聊。"""
    import aiohttp

    token = await _get_token()
    if not token:
        return

    headers = {"Authorization": f"Bearer {token}"}
    url = "https://open.feishu.cn/open-apis/im/v1/chats"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if data.get("code") == 0:
                chats = data.get("data", {}).get("items", [])
                if not chats:
                    print("机器人未加入任何群聊。请先在群聊中添加机器人。")
                    return
                print(f"找到 {len(chats)} 个群聊:\n")
                for chat in chats:
                    chat_id = chat.get("chat_id", "")
                    name = chat.get("name", "未命名")
                    print(f"  {name}")
                    print(f"    chat_id: {chat_id}")
                    print()
            else:
                print(f"获取群列表失败: {data.get('msg', '未知错误')}")


async def test_text_message(publisher: FeishuPublisher) -> PublishResult:
    """发送纯文本测试消息。"""
    print("[测试] 发送纯文本消息...")
    result = await publisher.send_message("飞书消息发送测试 - 纯文本 ✅")
    _print_result(result)
    return result


async def test_card_message(publisher: FeishuPublisher) -> PublishResult:
    """发送卡片测试消息。"""
    print("[测试] 发送卡片消息...")
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "飞书卡片测试"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "**这是一条测试卡片消息**\n用于验证飞书 IM API 是否正常工作。",
                    },
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "状态: ✅ 正常 | 时间: " + _now(),
                    },
                },
            ],
        },
    }
    result = await publisher.send_message(card)
    _print_result(result)
    return result


async def test_digest(
    publisher: FeishuPublisher, date: str, top_n: int
) -> PublishResult:
    """发送每日简报。"""
    print(f"[测试] 发送 {date} 简报 (top_n={top_n})...")
    result = await publisher.send_digest(
        knowledge_dir="knowledge/articles", date=date, top_n=top_n
    )
    _print_result(result)
    return result


def _now() -> str:
    """返回当前时间字符串。"""
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _print_result(result: PublishResult) -> None:
    """打印发布结果。"""
    if result.success:
        print(f"  -> 成功 ✅  message_id={result.message_id}")
    else:
        print(f"  -> 失败 ❌  error={result.error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="飞书消息发送测试")
    parser.add_argument(
        "--list-chats", action="store_true", help="列出机器人所在的所有群聊"
    )
    parser.add_argument(
        "--date", default=None, help="简报日期 (YYYY-MM-DD)，默认今天"
    )
    parser.add_argument(
        "--top-n", type=int, default=3, help="每个 category 取前 N 篇 (默认 3)"
    )
    parser.add_argument(
        "--chat-id", default=None, help="覆盖环境变量 FEISHU_CHAT_ID"
    )
    args = parser.parse_args()

    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    chat_id = args.chat_id or os.environ.get("FEISHU_CHAT_ID", "")

    if not app_id or not app_secret:
        print("错误: 请设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET 环境变量")
        sys.exit(1)

    if args.list_chats:
        asyncio.run(list_chats())
        return

    if not chat_id:
        print("错误: 请设置 FEISHU_CHAT_ID 环境变量或传入 --chat-id")
        print("提示: 运行 --list-chats 查看可用群聊 ID")
        sys.exit(1)

    publisher = FeishuPublisher(app_id=app_id, app_secret=app_secret, chat_id=chat_id)

    async def run() -> None:
        await test_text_message(publisher)
        print()
        await test_card_message(publisher)
        print()
        await test_digest(publisher, args.date, args.top_n)

    asyncio.run(run())


if __name__ == "__main__":
    main()
