"""Four-step knowledge base automation pipeline.

Steps:
  1. Collect  - Fetch AI content from GitHub Search API and RSS feeds
  2. Analyze  - Use LLM to generate summaries, scores, and tags
  3. Organize - Deduplicate, standardize, and validate
  4. Save     - Write individual JSON articles to knowledge/articles/

Usage::

    python pipeline/pipeline.py --sources github,rss --limit 20
    python pipeline/pipeline.py --sources github --limit 5
    python pipeline/pipeline.py --dry-run --verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from model_client import Usage, create_provider, chat_with_retry, estimate_cost

_PROVIDER_MODEL_ENV = {
    "deepseek": "DEEPSEEK_MODEL",
    "qwen": "DASHSCOPE_MODEL",
    "openai": "OPENAI_MODEL",
}

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "knowledge" / "raw"
ARTICLES_DIR = PROJECT_ROOT / "knowledge" / "articles"

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"

DEFAULT_RSS_FEEDS = [
    "https://hnrss.org/newest?q=AI+LLM+agent&count=30",
    "https://hnrss.org/frontpage",
]

AI_KEYWORDS = [
    "ai", "llm", "agent", "gpt", "claude", "transformer",
    "machine learning", "deep learning", "nlp", "rag", "mcp",
    "openai", "anthropic", "copilot", "codex",
]

GITHUB_QUERY = "AI LLM agent"
GITHUB_SORT = "stars"
GITHUB_ORDER = "desc"

ANALYSIS_SYSTEM_PROMPT = (
    "你是一名 AI 技术分析师。请分析以下技术内容，返回严格合法的 JSON"
    "（不要 markdown 代码块，不要多余文字）。\n\n"
    "JSON schema:\n"
    '{\n'
    '  "summary": "中文技术摘要，50-150字，包含技术关键词",\n'
    '  "score": 8,\n'
    '  "tags": ["tag1", "tag2"],\n'
    '  "audience": "intermediate"\n'
    "}\n\n"
    "评分规则 (score 1-10):\n"
    "- 9-10: 开创性技术突破或业界标杆级项目\n"
    "- 7-8: 有实质技术深度或重要行业影响\n"
    "- 5-6: 有一定价值但深度或创新性一般\n"
    "- 3-4: 浅层内容或入门级工具\n"
    "- 1-2: 无技术价值\n\n"
    "tags: 1-3 个英文小写标签，优先从以下选择: "
    "ai, llm, agent, rag, mcp, prompt-engineering, fine-tuning, "
    "embedding, code-generation, multi-agent, tool-use, reasoning, "
    "deployment, security, audio, cv, nlp, open-source\n\n"
    "audience: beginner / intermediate / advanced"
)

ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-\d{8}-\d{3}$")
URL_PATTERN = re.compile(r"^https?://.+")
VALID_STATUSES = {"draft", "review", "published", "archived"}
VALID_AUDIENCES = {"beginner", "intermediate", "advanced"}
VALID_SOURCES = {"github", "hackernews", "rss", "arxiv"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _date_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _date_dash() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "-", text).strip("-")
    return text[:60].lower()


def _clamp_score(value: Any) -> int:
    try:
        return max(1, min(10, int(value)))
    except (TypeError, ValueError):
        return 5


def _normalize_tags(tags: Any) -> list[str]:
    if not isinstance(tags, list):
        return []
    normalized: list[str] = []
    for t in tags[:3]:
        tag = str(t).lower().replace(" ", "-").replace("_", "-")
        if re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", tag):
            normalized.append(tag)
    return normalized


# ---------------------------------------------------------------------------
# Step 1: Collect
# ---------------------------------------------------------------------------


def collect_github(limit: int = 20) -> list[dict]:
    headers: dict[str, str] = {
        "Accept": "application/vnd.github.v3+json",
    }
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    params = {
        "q": GITHUB_QUERY,
        "sort": GITHUB_SORT,
        "order": GITHUB_ORDER,
        "per_page": min(limit, 100),
    }

    logger.info("Fetching GitHub Search API (limit=%d)...", limit)

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(GITHUB_SEARCH_URL, params=params, headers=headers)
            resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("GitHub API request failed: %s", exc)
        return []

    now = _now_iso()
    date_str = _date_compact()
    items: list[dict] = []

    for i, repo in enumerate(data.get("items", [])[:limit], start=1):
        desc = repo.get("description") or ""
        lang = repo.get("language") or ""
        stars = repo.get("stargazers_count", 0)
        forks = repo.get("forks_count", 0)
        items.append({
            "id": f"github-{date_str}-{i:03d}",
            "title": repo.get("full_name", ""),
            "source": "github",
            "source_url": repo.get("html_url", ""),
            "author": repo.get("owner", {}).get("login", ""),
            "published_at": repo.get("created_at") or now,
            "raw_description": f"{desc} {lang} {stars} stars, {forks} forks.".strip(),
            "collected_at": now,
        })

    logger.info("Collected %d items from GitHub", len(items))
    return items


def _parse_rss_items(text: str) -> list[dict]:
    items: list[dict] = []

    item_pattern = re.compile(r"<item>(.*?)</item>", re.DOTALL)
    title_pattern = re.compile(
        r"<title><!\[CDATA\[(.*?)\]\]></title>"
        r"|<title>(.*?)</title>",
        re.DOTALL,
    )
    link_pattern = re.compile(r"<link>(.*?)</link>", re.DOTALL)
    desc_pattern = re.compile(
        r"<description><!\[CDATA\[(.*?)\]\]></description>"
        r"|<description>(.*?)</description>",
        re.DOTALL,
    )

    for match in item_pattern.finditer(text):
        block = match.group(1)

        title_m = title_pattern.search(block)
        title = ""
        if title_m:
            title = (title_m.group(1) or title_m.group(2) or "").strip()

        link_m = link_pattern.search(block)
        link = link_m.group(1).strip() if link_m else ""

        desc_m = desc_pattern.search(block)
        desc = ""
        if desc_m:
            desc = (desc_m.group(1) or desc_m.group(2) or "").strip()

        if title and link:
            items.append({"title": title, "link": link, "description": desc})

    return items


def collect_rss(limit: int = 20) -> list[dict]:
    now = _now_iso()
    date_str = _date_compact()
    items: list[dict] = []

    for feed_url in DEFAULT_RSS_FEEDS:
        logger.info("Fetching RSS feed: %s", feed_url)
        try:
            with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                resp = client.get(feed_url)
                resp.raise_for_status()
            text = resp.text
        except Exception as exc:
            logger.error("RSS fetch failed (%s): %s", feed_url, exc)
            continue

        for entry in _parse_rss_items(text):
            combined = f"{entry['title']} {entry['description']}".lower()
            if not any(kw.lower() in combined for kw in AI_KEYWORDS):
                continue

            items.append({
                "id": f"rss-{date_str}-{len(items) + 1:03d}",
                "title": entry["title"],
                "source": "rss",
                "source_url": entry["link"],
                "author": "",
                "published_at": now,
                "raw_description": entry["description"],
                "collected_at": now,
            })

            if len(items) >= limit:
                break

        if len(items) >= limit:
            break

    logger.info("Collected %d items from RSS feeds", len(items))
    return items[:limit]


def save_raw(items: list[dict], source: str) -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"{source}-{_date_dash()}.json"

    existing: list[dict] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    existing_ids = {item.get("id") for item in existing}
    for item in items:
        if item.get("id") not in existing_ids:
            existing.append(item)

    path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Saved %d raw items to %s", len(existing), path)
    return path


# ---------------------------------------------------------------------------
# Step 2: Analyze
# ---------------------------------------------------------------------------


def analyze_item(item: dict, provider: Any) -> tuple[dict | None, Usage]:
    title = item.get("title", "")
    raw_desc = item.get("raw_description", "")

    prompt = f"标题: {title}\n\n描述: {raw_desc}\n\n请分析以上内容，返回 JSON。"
    messages = [
        {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    try:
        resp = chat_with_retry(
            provider, messages, temperature=0.3, max_tokens=512,
        )
        content = resp.content.strip()
        usage = resp.usage
    except Exception as exc:
        logger.error("LLM analysis failed for '%s': %s", title[:40], exc)
        return None, Usage()

    content = re.sub(r"^```(?:json)?\s*\n?", "", content)
    content = re.sub(r"\n?```\s*$", "", content)

    try:
        analysis = json.loads(content)
    except json.JSONDecodeError:
        logger.error(
            "LLM returned invalid JSON for '%s': %s",
            title[:40], content[:100],
        )
        return None, usage

    return {
        **item,
        "summary": analysis.get("summary", ""),
        "score": _clamp_score(analysis.get("score", 5)),
        "tags": _normalize_tags(analysis.get("tags", [])),
        "audience": analysis.get("audience", "intermediate"),
    }, usage


# ---------------------------------------------------------------------------
# Step 3: Organize
# ---------------------------------------------------------------------------


def _load_existing_urls() -> set[str]:
    urls: set[str] = set()
    if not ARTICLES_DIR.exists():
        return urls
    for path in ARTICLES_DIR.glob("*.json"):
        if path.name == "index.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            url = data.get("source_url", "")
            if url:
                urls.add(url)
        except (json.JSONDecodeError, OSError):
            continue
    return urls


def _validate_article(article: dict) -> list[str]:
    errors: list[str] = []
    if not ID_PATTERN.match(str(article.get("id", ""))):
        errors.append("invalid id")
    if not article.get("title"):
        errors.append("missing title")
    if not URL_PATTERN.match(str(article.get("source_url", ""))):
        errors.append("invalid source_url")
    if str(article.get("status", "")) not in VALID_STATUSES:
        errors.append("invalid status")
    if article.get("source") not in VALID_SOURCES:
        errors.append("invalid source")
    summary = article.get("summary", "")
    if len(summary) < 10:
        errors.append(f"summary too short ({len(summary)} chars)")
    return errors


def organize(articles: list[dict]) -> list[dict]:
    seen_urls: set[str] = _load_existing_urls()
    result: list[dict] = []
    now = _now_iso()

    for article in articles:
        url = article.get("source_url", "")
        if url in seen_urls:
            logger.debug("Skipping duplicate: %s", article.get("title", "")[:40])
            continue
        seen_urls.add(url)

        article.setdefault("status", "draft")
        article.setdefault("collected_at", now)
        article.setdefault("summary", "")
        article.setdefault("score", 5)
        article.setdefault("tags", [])
        article.setdefault("audience", "intermediate")
        article.setdefault("author", "")
        article.setdefault("published_at", now)
        article["updated_at"] = now

        article.pop("raw_description", None)
        article.pop("analysis_note", None)

        errors = _validate_article(article)
        if errors:
            logger.warning(
                "Validation failed for '%s': %s",
                article.get("title", "")[:40],
                "; ".join(errors),
            )
            continue

        result.append(article)

    dropped = len(articles) - len(result)
    logger.info(
        "Organized: %d articles (%d duplicates/invalid dropped)",
        len(result), dropped,
    )
    return result


# ---------------------------------------------------------------------------
# Step 4: Save
# ---------------------------------------------------------------------------


def save_articles(articles: list[dict], *, dry_run: bool = False) -> list[Path]:
    if dry_run:
        logger.info("[DRY RUN] Would save %d articles", len(articles))
        return []

    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []

    for article in articles:
        slug = _slugify(article.get("title", "untitled"))
        date_str = _date_dash()
        filename = f"{date_str}-{slug}.json"
        path = ARTICLES_DIR / filename

        counter = 1
        while path.exists():
            path = ARTICLES_DIR / f"{date_str}-{slug}-{counter}.json"
            counter += 1

        path.write_text(
            json.dumps(article, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        saved.append(path)
        logger.debug("Saved: %s", path.name)

    logger.info("Saved %d article files", len(saved))
    return saved


def update_index(articles: list[dict], *, dry_run: bool = False) -> None:
    if dry_run:
        return

    index_path = ARTICLES_DIR / "index.json"

    existing: dict[str, Any] = {
        "updated_at": "", "total": 0, "articles": [],
    }
    if index_path.exists():
        try:
            existing = json.loads(index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    existing_ids = {a["id"] for a in existing.get("articles", [])}
    date_str = _date_dash()

    for article in articles:
        if article.get("id") in existing_ids:
            continue
        slug = _slugify(article.get("title", "untitled"))
        existing["articles"].append({
            "id": article["id"],
            "title": article["title"],
            "source": article["source"],
            "score": article["score"],
            "tags": article["tags"],
            "file": f"{date_str}-{slug}.json",
        })

    existing["total"] = len(existing["articles"])
    existing["updated_at"] = _now_iso()

    index_path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Updated index.json (%d total articles)", existing["total"])


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------


def run_pipeline(
    sources: list[str] | None = None,
    limit: int = 20,
    dry_run: bool = False,
) -> None:
    sources = sources or ["github", "rss"]
    start = time.time()

    print(f"\n{'=' * 60}")
    print(f"  Knowledge Base Pipeline")
    print(
        f"  Sources: {', '.join(sources)}  |  "
        f"Limit: {limit}  |  Dry-run: {dry_run}"
    )
    print(f"{'=' * 60}\n")

    # Step 1: Collect
    print("[Step 1/4] Collecting...")
    raw_items: list[dict] = []

    if "github" in sources:
        github_items = collect_github(limit=limit)
        raw_items.extend(github_items)
        if github_items and not dry_run:
            save_raw(github_items, "github-search")

    if "rss" in sources:
        rss_items = collect_rss(limit=limit)
        raw_items.extend(rss_items)
        if rss_items and not dry_run:
            save_raw(rss_items, "rss")

    print(f"  Collected {len(raw_items)} items total\n")

    if not raw_items:
        print("No items collected. Exiting.")
        return

    # Step 2: Analyze
    print("[Step 2/4] Analyzing with LLM...")
    provider = create_provider()
    analyzed: list[dict] = []
    total_usage = Usage()

    for i, item in enumerate(raw_items, 1):
        logger.info(
            "Analyzing %d/%d: %s",
            i, len(raw_items), item.get("title", "")[:50],
        )
        result, usage = analyze_item(item, provider)
        total_usage.prompt_tokens += usage.prompt_tokens
        total_usage.completion_tokens += usage.completion_tokens
        total_usage.total_tokens += usage.total_tokens
        if result:
            analyzed.append(result)

    provider_name = os.environ.get("LLM_PROVIDER", "deepseek")
    model_env_key = _PROVIDER_MODEL_ENV.get(provider_name, "")
    model_name = (
        os.environ.get(model_env_key, "")
        or provider._default_model
    )
    total_cost = estimate_cost(total_usage, model_name)

    print(f"  Analyzed {len(analyzed)}/{len(raw_items)} items")
    print(
        f"  Tokens: {total_usage.total_tokens:,} "
        f"(prompt {total_usage.prompt_tokens:,} + "
        f"completion {total_usage.completion_tokens:,})"
    )
    print(f"  Cost: ${total_cost:.4f}\n")

    if not analyzed:
        print("No items passed analysis. Exiting.")
        return

    # Step 3: Organize
    print("[Step 3/4] Organizing...")
    organized = organize(analyzed)
    print(f"  {len(organized)} articles after dedup & validation\n")

    if not organized:
        print("No new articles after dedup. Exiting.")
        return

    # Step 4: Save
    print("[Step 4/4] Saving...")
    saved = save_articles(organized, dry_run=dry_run)
    update_index(organized, dry_run=dry_run)
    print(f"  Saved {len(saved)} article files\n")

    elapsed = time.time() - start
    print(f"{'=' * 60}")
    print(
        f"  Pipeline completed in {elapsed:.1f}s\n"
        f"  Collected: {len(raw_items)} | "
        f"Analyzed: {len(analyzed)} | "
        f"Saved: {len(saved)}"
    )
    print(f"{'=' * 60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AI Knowledge Base - four-step automation pipeline",
    )
    parser.add_argument(
        "--sources",
        default="github,rss",
        help="Comma-separated sources: github, rss (default: github,rss)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max items per source (default: 20)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run pipeline without writing files",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    sources = [s.strip().lower() for s in args.sources.split(",")]
    run_pipeline(sources=sources, limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
