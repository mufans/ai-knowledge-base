---
name: github-trending
description: 当需要采集 GitHub 热门开源项目时使用此技能
allowed-tools:
  - Read
  - Grep
  - Glob
  - WebFetch
---

# GitHub Trending 采集技能

## 使用场景

- 需要了解当前 GitHub 上热门的开源项目动态
- 需要为知识库采集 AI/LLM/Agent 相关的高质量开源项目
- 定期跟踪技术趋势，发现有价值的开源工具和框架

## 执行步骤

### 步骤 1：搜索热门仓库

通过 GitHub API 搜索近期热门仓库：

```
GET https://api.github.com/search/repositories?q=created:>2025-01-01&sort=stars&order=desc&per_page=100
```

可针对 AI/LLM/Agent 方向进行多轮搜索，例如：

```
GET https://api.github.com/search/repositories?q=topic:ai+topic:llm+topic:agent&sort=stars&order=desc&per_page=100
GET https://api.github.com/search/repositories?q=AI+agent+framework&sort=stars&order=desc&per_page=100
GET https://api.github.com/search/repositories?q=LLM+RAG+tool&sort=stars&order=desc&per_page=100
```

### 步骤 2：提取信息

从每个仓库的 API 返回结果中提取以下字段：

- `full_name`：项目全名（owner/repo）
- `html_url`：项目地址
- `description`：项目描述
- `stargazers_count`：Star 数
- `language`：主要编程语言
- `topics`：话题标签

### 步骤 3：过滤

**纳入条件**（满足其一即可）：

- 项目 topics 中包含 `ai`、`llm`、`agent`、`machine-learning`、`deep-learning`、`nlp`、`rag`、`mcp` 等相关标签
- 项目描述中包含 AI、LLM、Agent、大模型、智能体、RAG 等关键词
- 项目属于 AI 工具链、模型训练、推理部署、向量数据库等相关领域

**排除条件**（命中即排除）：

- 项目名或描述中包含 `awesome` 的列表类仓库
- 纯教程、纯资源汇总类仓库（无实际代码）

### 步骤 4：去重

以 `full_name`（owner/repo）作为唯一标识，合并多轮搜索结果，去除重复项目。

### 步骤 5：撰写中文摘要

为每个项目撰写简明中文摘要，遵循以下公式：

**项目名 + 做什么 + 为什么值得关注**

示例：

> **OpenHands**：一个 AI 驱动的软件开发代理平台，能自主完成代码编写、调试和部署，值得关注是因为它代表了 AI Agent 在软件工程领域的最新实践。

摘要要求：

- 控制在 50-120 字
- 突出项目核心功能和差异化价值
- 避免翻译式描述，用自然流畅的中文表达

### 步骤 6：排序取 Top 15

按 `stargazers_count` 降序排列，取前 15 个项目。

### 步骤 7：输出 JSON

将结果写入 `knowledge/raw/github-trending-YYYY-MM-DD.json`，文件中的日期使用采集当天的实际日期。

## 注意事项

- GitHub API 有速率限制（未认证 60 次/小时，认证后 5000 次/小时），注意控制请求频率
- 如果遇到 API 限流，等待后再试或缩小查询范围
- 确保输出目录 `knowledge/raw/` 存在，不存在则先创建
- JSON 文件使用 UTF-8 编码，确保中文内容正确输出
- 采集完成后告知用户采集的项目数量和文件路径

## 输出格式

输出 JSON 文件结构如下：

```json
{
  "source": "github-trending",
  "skill": "github-trending",
  "collected_at": "2025-04-26",
  "items": [
    {
      "name": "owner/repo",
      "url": "https://github.com/owner/repo",
      "summary": "项目名：做什么，为什么值得关注",
      "stars": 12345,
      "language": "Python",
      "topics": ["ai", "llm", "agent"]
    }
  ]
}
```
