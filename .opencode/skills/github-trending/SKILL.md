---
name: github-trending
description: >
  采集 GitHub Trending 热门开源项目，聚焦 AI/LLM/Agent/ML 方向并输出结构化 JSON。
  Use when user mentions GitHub trending, GitHub热门, GitHub热榜, 热门项目, 热门仓库,
  开源趋势, 技术趋势, trending repos, trending projects, AI开源, AI项目, 项目发现,
  项目推荐, 采集热门, 抓取GitHub, GitHub排行, 技术动态, 大模型开源, 智能体框架,
  open-source trends, 看看热门项目, 最近有什么新项目, 帮我找AI项目,
  or asks about popular/emerging/new open-source repos on GitHub.
allowed-tools:
  - WebFetch
  - Write
  - Bash
---

# GitHub Trending 采集

## Quick Start

1. WebFetch 抓取 `https://github.com/trending` 页面
2. 解析 HTML 提取仓库信息
3. 按 AI/LLM/Agent/ML 主题过滤
4. 输出 JSON 到 `knowledge/raw/github-trending-YYYY-MM-DD.json`

## Workflow

### Step 1: 抓取 Trending 页面

使用 WebFetch 获取 GitHub Trending 页面。**走 HTML 解析，不调 GitHub API**（避免 rate limit）。

URL: `https://github.com/trending`
可选参数: `?since=daily`（默认）| `weekly` | `monthly`

如需扩大范围，可追加抓取特定语言页面：
`https://github.com/trending/python?since=daily`

### Step 2: 提取仓库信息

每个仓库提取：

| 字段 | 来源 |
|------|------|
| `name` | owner/repo |
| `url` | https://github.com/owner/repo |
| `description` | 项目原始描述 |
| `stars` | Star 数 |
| `topics` | 话题标签 |

### Step 3: 过滤

**纳入**（满足任一）：
- topics 含 `ai` `llm` `agent` `machine-learning` `deep-learning` `nlp` `rag` `mcp` `ml`
- description 含 AI / LLM / Agent / 大模型 / 智能体 / RAG 关键词
- 属于 AI 工具链、模型训练、推理部署、向量数据库等领域

**排除**：
- 名称或描述含 `awesome` 的列表类仓库
- 纯教程/资源汇总类仓库（无实际代码）

### Step 4: 排序 & 截取

按 stars 降序排列，取 Top 50。

### Step 5: 撰写中文摘要

为每个项目撰写 50-120 字中文摘要，公式：**项目名 + 做什么 + 为什么值得关注**。

> **OpenHands**：AI 驱动的软件开发代理平台，能自主完成代码编写、调试和部署，代表了 AI Agent 在软件工程领域的最新实践。

### Step 6: 输出 JSON

写入 `knowledge/raw/github-trending-YYYY-MM-DD.json`（日期用当天实际日期）：

```json
{
  "source": "github-trending",
  "collected_at": "2026-04-26",
  "items": [
    {
      "name": "owner/repo",
      "url": "https://github.com/owner/repo",
      "description": "Original project description",
      "summary": "项目名：做什么，为什么值得关注",
      "stars": 12345,
      "topics": ["ai", "llm"]
    }
  ]
}
```

## 注意事项

- 走 HTML 解析，不调 GitHub API（rate limit 太紧）
- 失败时返回 `{"items": []}`，不抛异常
- 确保 `knowledge/raw/` 目录存在，不存在则先创建
- JSON 使用 UTF-8 编码
- 完成后告知用户采集数量和文件路径
