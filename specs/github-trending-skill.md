# skill: github-trending · 需求

## 要做什么
- 抓 github.com/trending Top 50
- 过滤 repo topics 含 ai/llm/agent/ml 的
- 输出 JSON 数组 · 字段 [name, url, stars, topics, description]

## 不做什么
- 不调 GitHub API（rate limit 太紧）· 走 HTML 解析
- 不存数据库 · 只 stdout
- 不做去重（由 caller 处理）

## 边界 & 验收
- 单次执行 < 10s
- 失败时返回空数组 · 不抛异常
- 输出必须通过 jsonschema 验证

## 怎么验证
- 跑 `skill-invoke github-trending` 后 · 检查输出是 JSON 且字段完整