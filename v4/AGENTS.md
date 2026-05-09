# AGENTS.md — AI 知识库项目

> 本文件是项目的"大脑"——OpenCode 启动时自动加载，指导所有 Agent 的行为。

## 项目定义

**AI Knowledge Base（AI 知识库）** 是一个自动化技术情报收集与分析系统。
它持续追踪 GitHub Trending、Hacker News、arXiv 等来源，将分散的技术资讯
转化为结构化、可检索的知识条目。

### 核心价值
- 每日自动采集 AI/LLM/Agent 领域的高质量技术文章与开源项目
- 通过 Agent 协作完成 **采集 → 分析 → 整理** 三阶段流水线
- 输出格式统一的 JSON 知识条目，便于下游应用消费

## 项目结构

```
ai-knowledge-base/
├── AGENTS.md                          # 项目记忆文件（本文件）
├── .env.example                       # 环境变量模板
├── README.md                          # 使用说明
├── .opencode/
│   ├── agents/
│   │   ├── collector.md               # 采集 Agent 角色定义
│   │   ├── analyzer.md                # 分析 Agent 角色定义
│   │   └── organizer.md               # 整理 Agent 角色定义
│   └── skills/
│       ├── github-trending/SKILL.md   # GitHub Trending 采集技能
│       └── tech-summary/SKILL.md      # 技术摘要生成技能
└── knowledge/
    ├── raw/                           # 原始采集数据（JSON）
    └── articles/                      # 整理后的知识条目（JSON）
```

## 编码规范

### 文件命名
- 原始数据：`knowledge/raw/{source}-{YYYY-MM-DD}.json`
  - 例：`knowledge/raw/github-trending-2026-03-17.json`
  - 例：`knowledge/raw/hackernews-top-2026-03-17.json`
- 知识条目：`knowledge/articles/{YYYY-MM-DD}-{slug}.json`
  - 例：`knowledge/articles/2026-03-17-openai-agents-sdk.json`
- 索引文件：`knowledge/articles/index.json`

### JSON 格式
- 使用 2 空格缩进
- 日期格式：ISO 8601（`YYYY-MM-DDTHH:mm:ssZ`）
- 字符编码：UTF-8

### 知识条目格式

每篇文章对应一个独立 JSON 文件，完整 schema 如下：

```json
{
  "id": "github-20260317-001",
  "title": "文章标题",
  "source": "github",
  "source_url": "https://...",
  "author": "作者",
  "published_at": "2026-03-17T00:00:00Z",
  "collected_at": "2026-03-17T10:30:00Z",
  "summary": "2-3 句技术摘要，至少 50 字",
  "score": 8,
  "tags": ["agent", "mcp"],
  "audience": "intermediate",
  "status": "published",
  "updated_at": "2026-03-17T12:00:00Z"
}
```

#### 字段说明

| 字段 | 必填 | 说明 |
|------|------|------|
| `id` | 是 | 格式：`{source}-{YYYYMMDD}-{NNN}`，如 `github-20260317-001` |
| `title` | 是 | 非空字符串 |
| `source` | 是 | 来源类型：`github` / `hackernews` / `arxiv` 等 |
| `source_url` | 是 | 合法 URL，用于溯源 |
| `author` | 否 | 作者或组织名 |
| `published_at` | 否 | 原始发布时间（ISO 8601） |
| `collected_at` | 是 | 采集时间（ISO 8601） |
| `summary` | 是 | 技术摘要，>= 50 字最佳，>= 20 字及格 |
| `score` | 是 | 技术深度评分，1-10 整数 |
| `tags` | 是 | 1-3 个英文小写标签，连字符分隔 |
| `audience` | 否 | 受众：`beginner` / `intermediate` / `advanced` |
| `status` | 是 | 状态：`draft` / `review` / `published` / `archived` |
| `updated_at` | 否 | 最后更新时间（ISO 8601） |

#### 质量评分

保存前需通过质量校验（`hooks/check_quality.py`），五维度加权总分 100 分：

| 维度 | 满分 | 评分要点 |
|------|------|----------|
| 摘要质量 | 25 | >= 50 字满分，含技术关键词有奖励 |
| 技术深度 | 25 | 基于 score 字段（1-10 映射到 0-25） |
| 格式规范 | 20 | id / title / source_url / status / 时间戳各 4 分 |
| 标签精度 | 15 | 1-3 个合法标签最佳，有标准标签列表校验 |
| 空洞词检测 | 15 | 不得包含"赋能""groundbreaking"等空洞词 |

等级标准：**A >= 80** / **B >= 60** / **C < 60**。只有 B 级及以上的文章才标记为 `published`。

### 语言约定
- 代码、JSON 键名、文件名：英文
- 摘要、分析、注释：中文
- 标签（tags）：英文小写，用连字符分隔（如 `large-language-model`）

## 工作流规则

### 三阶段流水线

```
[Collector] ──采集──→ knowledge/raw/
                          │
[Analyzer]  ──分析──→ knowledge/raw/ (enriched)
                          │
[Organizer] ──整理──→ knowledge/articles/
```

### Agent 协作规则

1. **单向数据流**：Collector → Analyzer → Organizer，不可反向
2. **职责隔离**：每个 Agent 只操作自己权限范围内的文件
3. **幂等性**：重复运行同一天的采集不应产生重复条目
4. **质量门控**：`hooks/check_quality.py` 评分低于 B 级（< 60 分）的条目，Organizer 应丢弃
5. **可追溯**：每个条目保留 `source_url` 和 `collected_at` 用于溯源

### Agent 调用方式

在 OpenCode 中使用 `@` 语法调用特定 Agent：

```
@collector 采集今天的 GitHub Trending 数据
@analyzer 分析 knowledge/raw/github-trending-2026-03-17.json
@organizer 整理今天所有已分析的原始数据
```

也可以在对话中要求主 Agent 依次委派子 Agent，实现流水线作业。

### 错误处理
- 网络请求失败时，记录错误并跳过该条目，不中断整体流程
- API 限流时，等待后重试，最多 3 次
- 数据格式异常时，写入 `knowledge/raw/errors-{date}.json` 供人工排查
- 质量校验：`python3 hooks/check_quality.py knowledge/articles/*.json`，存在 C 级返回退出码 1

## 技术栈
- **运行时**：OpenCode + LLM
- **数据源**：GitHub API v3、Hacker News API (firebase)
- **输出格式**：JSON
- **版本管理**：Git
