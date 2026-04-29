# ai-knowledge-base

基于多 Agent 协作的 AI 技术动态知识库。通过三个专职 Agent 自动完成技术资讯的采集、分析与入库，持续追踪 GitHub Trending、Hacker News 等社区的热门 AI 项目与技术趋势。

## 架构设计

采用 **采集 → 分析 → 整理** 的三阶段流水线架构，每个阶段由独立的 Agent 负责，通过目录隔离和权限控制实现职责分离。

```
Collector Agent          Analyzer Agent          Organizer Agent
   (采集)                   (分析)                   (整理)
      |                       |                       |
      v                       |                       |
 knowledge/raw/  ---------->  |                       |
                              v                       |
                  knowledge/analysis/                  |
                                              |       |
                                              v       v
                                       knowledge/articles/
                                           + index.json
```

## Agent 职责

| Agent | 职责 | 写入范围 | 核心工具 |
|-------|------|---------|---------|
| **Collector** | 从技术社区采集热门项目与文章 | `knowledge/raw/` | WebFetch |
| **Analyzer** | 生成摘要、提取亮点、质量评分、标签建议 | 无（只分析不写入） | Read, WebFetch |
| **Organizer** | 去重、格式化、分类归档、维护索引 | `knowledge/articles/` | Read, Write, Edit |

### 权限控制原则

- **Collector**：可抓取外部页面，但只能写入 `knowledge/raw/`，禁止修改已有文件
- **Analyzer**：可读取原始数据并访问外部链接做深度分析，但**禁止写入任何文件**
- **Organizer**：唯一可以写入 `knowledge/articles/` 的 Agent，**禁止访问外部页面**，确保入库内容全部来自上游

## 目录结构

```
.
├── .opencode/
│   └── agents/
│       ├── collector.md      # 采集 Agent 角色定义
│       ├── analyzer.md       # 分析 Agent 角色定义
│       └── organizer.md      # 整理 Agent 角色定义
├── knowledge/
│   ├── raw/                  # 原始采集数据（Collector 产出）
│   ├── analysis/             # 分析结果（Analyzer 产出）
│   └── articles/             # 最终入库条目（Organizer 产出）
│       ├── index.json        # 全局索引
│       └── *.json            # 标准化知识条目
├── sub-agent-test-log.md     # Agent 协作测试日志
└── README.md
```

## 数据格式

### 最终入库条目示例

```json
{
  "id": "2026-04-25-hermes-agent",
  "title": "Hermes Agent - 自我改进型 AI Agent，越用越懂你",
  "date": "2026-04-25",
  "source": "github-trending",
  "tags": ["ai", "agent", "llm", "open-source"],
  "summary": "结构化中文摘要，涵盖项目定位、核心能力与技术路线",
  "highlights": ["亮点1", "亮点2", "亮点3"],
  "analysis": {
    "score": 10,
    "score_reason": "评分理由",
    "recommendation": "一句话推荐"
  }
}
```

### 质量评分标准

| 分数 | 等级 | 含义 |
|------|------|------|
| 9-10 | 改变格局 | 行业颠覆性的技术突破或范式转变 |
| 7-8 | 直接有帮助 | 可直接应用于开发工作的工具、框架、最佳实践 |
| 5-6 | 值得了解 | 拓宽技术视野的趋势与概念 |
| 1-4 | 可略过 | 信息价值有限或关联度低 |

## 数据源

当前支持的数据源：

- **GitHub Trending** — `https://github.com/trending`
- **Hacker News** — `https://news.ycombinator.com/`

可通过扩展 Collector Agent 角色定义接入更多数据源（Product Hunt、Reddit r/programming、V2EX 等）。

## 使用方式

本项目通过 [OpenCode](https://github.com/opencode-ai/opencode) 框架运行，Agent 定义位于 `.opencode/agents/` 目录下。按以下顺序依次调用各 Agent：

1. **启动采集**：调用 Collector Agent，采集结果写入 `knowledge/raw/`
2. **执行分析**：调用 Analyzer Agent，读取 raw 数据并产出分析结果
3. **整理入库**：调用 Organizer Agent，去重格式化后写入 `knowledge/articles/`

## 测试记录

详细的 Agent 协作测试日志见 [sub-agent-test-log.md](sub-agent-test-log.md)，包含首次全流程测试中各 Agent 的角色执行合规性、输出格式偏差、权限遵守情况及改进建议。
