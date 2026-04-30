#!/usr/bin/env python3
"""Quality scoring for knowledge base JSON article files.

Evaluates articles on 5 weighted dimensions (total 100 points):
  - 摘要质量 (25): summary length and technical keyword richness
  - 技术深度 (25): mapped from article score (1-10 → 0-25)
  - 格式规范 (20): id, title, source_url, status, timestamp checks
  - 标签精度 (15): tag count and standard tag validation
  - 空洞词检测 (15): buzzword blacklist penalty

Usage:
    python hooks/check_quality.py <json_file> [json_file2 ...]
    python hooks/check_quality.py knowledge/articles/*.json

Grades: A >= 80, B >= 60, C < 60
Exit codes: 0 if no C-grade files, 1 otherwise
"""

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUZZWORDS_ZH: list[str] = [
    "赋能", "抓手", "闭环", "打通", "全链路",
    "底层逻辑", "颗粒度", "对齐", "拉通", "沉淀",
    "强大的", "革命性的",
]

BUZZWORDS_EN: list[str] = [
    "groundbreaking", "revolutionary", "game-changing",
    "cutting-edge", "world-class", "next-generation",
    "best-in-class", "industry-leading", "paradigm-shifting",
]

# Standard technical tags for validation
STANDARD_TAGS: set[str] = {
    "ai", "llm", "ml", "nlp", "cv", "rl",
    "agent", "coding-agent", "rag", "mcp",
    "prompt-engineering", "fine-tuning", "embedding",
    "claude", "gpt", "openai", "anthropic",
    "python", "typescript", "go", "rust", "javascript",
    "react", "vue", "nextjs",
    "docker", "kubernetes", "postgres", "redis",
    "open-source", "开源项目", "开源工具",
    "最佳实践", "方法论", "架构设计",
    "context-management", "agent-memory", "knowledge-management",
    "vector-db", "pgvector", "chroma",
    "coding-assistant", "ide", "devtools",
    "security", "privacy", "compliance",
}

TECHNICAL_KEYWORDS: list[str] = [
    "api", "sdk", "mcp", "rag", "llm", "nlp", "cv",
    "agent", "model", "training", "inference",
    "embedding", "vector", "transformer", "attention",
    "prompt", "token", "context", "fine-tun",
    "open-source", "github", "docker", "postgres",
    "python", "typescript", "golang", "rust",
    "architecture", "pipeline", "framework",
    "pgvector", "chroma", "sqlite",
]

ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-\d{8}-\d{3}$")
URL_PATTERN = re.compile(r"^https?://.+")
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class DimensionScore:
    """Score for a single quality dimension."""

    name: str
    max_score: float
    score: float
    detail: str = ""


@dataclass
class QualityReport:
    """Aggregated quality report for a single file."""

    filepath: Path
    dimensions: list[DimensionScore] = field(default_factory=list)
    total_score: float = 0.0
    max_score: float = 100.0
    grade: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def percentage(self) -> float:
        return self.total_score if self.max_score == 100 else (
            self.total_score / self.max_score * 100
        )


# ---------------------------------------------------------------------------
# Scoring functions (each returns a DimensionScore)
# ---------------------------------------------------------------------------

def score_summary(data: dict) -> DimensionScore:
    """Score summary quality: length + technical keyword bonus."""
    max_score = 25
    summary = data.get("summary", "")
    length = len(summary)

    # Base score from length
    if length >= 50:
        base = 20
    elif length >= 20:
        base = 12
    elif length > 0:
        base = 5
    else:
        base = 0

    # Bonus for technical keywords (up to 5 points)
    lower = summary.lower()
    keyword_hits = sum(1 for kw in TECHNICAL_KEYWORDS if kw in lower)
    bonus = min(keyword_hits * 1.5, 5.0)

    score = min(base + bonus, max_score)
    detail = f"长度 {length} 字, 技术关键词 {keyword_hits} 个"

    return DimensionScore("摘要质量", max_score, score, detail)


def score_technical_depth(data: dict) -> DimensionScore:
    """Score technical depth based on article score (1-10 → 0-25)."""
    max_score = 25

    # score may be at top level or inside analysis
    raw_score = data.get("score")
    if raw_score is None and isinstance(data.get("analysis"), dict):
        raw_score = data["analysis"].get("score")

    if raw_score is None:
        return DimensionScore("技术深度", max_score, 0, "无 score 字段")

    try:
        raw_score = float(raw_score)
    except (TypeError, ValueError):
        return DimensionScore("技术深度", max_score, 0, "score 非数字")

    # Map 1-10 to 0-25
    clamped = max(1.0, min(10.0, raw_score))
    mapped = (clamped / 10.0) * max_score

    return DimensionScore(
        "技术深度", max_score, round(mapped, 1),
        f"原始 score={raw_score}",
    )


def score_format(data: dict) -> DimensionScore:
    """Score format compliance: id, title, source_url, status, timestamp."""
    max_score = 20
    items: list[tuple[str, bool]] = []

    # id format (4 points)
    id_val = data.get("id", "")
    items.append(("id 格式", bool(ID_PATTERN.match(str(id_val)))))

    # title presence (4 points)
    title = data.get("title", "")
    items.append(("title", bool(title and isinstance(title, str))))

    # source_url format (4 points)
    url = data.get("source_url", "")
    if not url and isinstance(data.get("metadata"), dict):
        url = data["metadata"].get("github_url", "")
    items.append(("source_url", bool(URL_PATTERN.match(str(url)))))

    # status (4 points)
    status = data.get("status", "")
    valid_statuses = {"draft", "review", "published", "archived"}
    items.append(("status", str(status) in valid_statuses))

    # timestamp/date (4 points)
    ts_val = (
        data.get("published_at", "")
        or data.get("collected_at", "")
        or data.get("updated_at", "")
    )
    items.append(("时间戳", bool(TIMESTAMP_PATTERN.match(str(ts_val)))))

    passed = sum(1 for _, ok in items if ok)
    score = (passed / len(items)) * max_score

    failed_items = [name for name, ok in items if not ok]
    if failed_items:
        detail = f"未通过: {', '.join(failed_items)}"
    else:
        detail = "全部通过"

    return DimensionScore("格式规范", max_score, score, detail)


def score_tags(data: dict) -> DimensionScore:
    """Score tag quality: count optimization + standard tag ratio."""
    max_score = 15
    tags = data.get("tags", [])

    if not isinstance(tags, list):
        return DimensionScore("标签精度", max_score, 0, "tags 非列表")

    count = len(tags)

    # Count score: 1-3 tags is optimal (8 points), 4-6 good (6), 7+ excessive (3), 0 zero
    if count == 0:
        count_score = 0
    elif 1 <= count <= 3:
        count_score = 8
    elif 4 <= count <= 6:
        count_score = 6
    else:
        count_score = 3

    # Standard tag ratio (7 points)
    if count > 0:
        standard_count = sum(1 for t in tags if isinstance(t, str) and t.lower() in STANDARD_TAGS)
        ratio = standard_count / count
        standard_score = ratio * 7
    else:
        standard_score = 0

    score = min(count_score + standard_score, max_score)
    non_standard = [t for t in tags if isinstance(t, str) and t.lower() not in STANDARD_TAGS]

    parts = [f"数量 {count}"]
    if non_standard:
        parts.append(f"非标准标签: {', '.join(non_standard[:3])}")
    detail = ", ".join(parts)

    return DimensionScore("标签精度", max_score, round(score, 1), detail)


def score_buzzwords(data: dict) -> DimensionScore:
    """Score buzzword absence: penalize each buzzword hit."""
    max_score = 15

    # Combine all text fields for checking
    texts: list[str] = []
    for key in ("summary", "title"):
        val = data.get(key, "")
        if isinstance(val, str):
            texts.append(val)
    if isinstance(data.get("highlights"), list):
        texts.extend(str(h) for h in data["highlights"] if h)
    if isinstance(data.get("analysis"), dict):
        for key in ("score_reason", "recommendation", "innovation"):
            val = data["analysis"].get(key, "")
            if isinstance(val, str):
                texts.append(val)

    combined = " ".join(texts).lower()

    # Count buzzword hits
    hits_zh = [w for w in BUZZWORDS_ZH if w in combined]
    hits_en = [w for w in BUZZWORDS_EN if w in combined]
    total_hits = len(hits_zh) + len(hits_en)

    # Penalty: 3 points per hit, minimum 0
    penalty = min(total_hits * 3, max_score)
    score = max_score - penalty

    found = hits_zh + hits_en
    if found:
        detail = f"发现 {total_hits} 个空洞词: {', '.join(found)}"
    else:
        detail = "未发现空洞词"

    return DimensionScore("空洞词检测", max_score, score, detail)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def evaluate_file(filepath: Path) -> QualityReport:
    """Evaluate a single JSON file and return a QualityReport."""
    report = QualityReport(filepath=filepath)

    # Parse JSON
    try:
        text = filepath.read_text(encoding="utf-8")
    except OSError as exc:
        report.errors.append(f"无法读取文件: {exc}")
        return report

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        report.errors.append(f"JSON 解析失败: {exc}")
        return report

    if not isinstance(data, dict):
        report.errors.append("JSON 顶层必须是对象 (dict)")
        return report

    # Run all dimensions
    scorers = [
        score_summary,
        score_technical_depth,
        score_format,
        score_tags,
        score_buzzwords,
    ]

    for scorer in scorers:
        dim = scorer(data)
        report.dimensions.append(dim)
        report.total_score += dim.score

    # Round total
    report.total_score = round(report.total_score, 1)

    # Assign grade
    if report.total_score >= 80:
        report.grade = "A"
    elif report.total_score >= 60:
        report.grade = "B"
    else:
        report.grade = "C"

    return report


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def bar(score: float, max_score: float, width: int = 20) -> str:
    """Render a text progress bar."""
    filled = int(score / max_score * width) if max_score > 0 else 0
    filled = max(0, min(width, filled))
    empty = width - filled
    return "[" + "#" * filled + "-" * empty + "]"


def print_report(report: QualityReport) -> None:
    """Print a formatted quality report."""
    name = report.filepath
    if report.errors:
        print(f"\n  {name}")
        for err in report.errors:
            print(f"    !! {err}")
        return

    # Grade color marker
    grade_marker = {"A": "+", "B": "~", "C": "!"}[report.grade]

    print(f"\n  {name}  [{grade_marker} {report.grade}]  {report.total_score}/100")
    print("  " + "-" * 50)

    for dim in report.dimensions:
        b = bar(dim.score, dim.max_score)
        print(f"  {dim.name:　<6s} {b} {dim.score:>5.1f}/{dim.max_score:<5.0f}  {dim.detail}")

    print()


SKIP_FILES = {"index.json"}


def collect_files(args: list[str]) -> list[Path]:
    """Resolve file arguments, supporting glob patterns."""
    files: list[Path] = []
    for arg in args:
        p = Path(arg)
        if p.is_file():
            if p.name not in SKIP_FILES:
                files.append(p)
        elif "*" in arg:
            matched = sorted(
                f for f in p.parent.glob(p.name) if f.name not in SKIP_FILES
            )
            if not matched:
                print(f"警告: 通配符 '{arg}' 未匹配到任何文件")
            files.extend(matched)
        else:
            print(f"警告: 跳过不存在的文件: {arg}")
    return files


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("用法: python hooks/check_quality.py <json_file> [json_file2 ...]")
        sys.exit(1)

    files = collect_files(args)
    if not files:
        print("错误: 没有找到可评分的文件")
        sys.exit(1)

    reports: list[QualityReport] = []
    for filepath in files:
        report = evaluate_file(filepath)
        reports.append(report)
        print_report(report)

    # Summary
    grade_counts = {"A": 0, "B": 0, "C": 0}
    error_count = 0
    for r in reports:
        if r.errors:
            error_count += 1
        else:
            grade_counts[r.grade] += 1

    total = len(reports)
    print("=" * 54)
    print(
        f"  评分完成: {total} 个文件"
        f"  |  A: {grade_counts['A']}  B: {grade_counts['B']}  C: {grade_counts['C']}"
        + (f"  解析错误: {error_count}" if error_count else "")
    )
    print("=" * 54)

    if grade_counts["C"] > 0 or error_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
