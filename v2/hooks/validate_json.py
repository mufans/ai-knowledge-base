#!/usr/bin/env python3
"""Validate knowledge base JSON article files.

Usage:
    python hooks/validate_json.py <json_file> [json_file2 ...]
    python hooks/validate_json.py knowledge/articles/*.json

Exit codes:
    0 - all files passed validation
    1 - one or more files failed validation
"""

import json
import re
import sys
from pathlib import Path

# Required fields: {field_name: expected_type}
REQUIRED_FIELDS: dict[str, type] = {
    "id": str,
    "title": str,
    "source_url": str,
    "summary": str,
    "tags": list,
    "status": str,
}

VALID_STATUSES = {"draft", "review", "published", "archived"}
VALID_AUDIENCES = {"beginner", "intermediate", "advanced"}

ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-(\d{8})-(\d{3})$")
URL_PATTERN = re.compile(r"^https?://.+")
MIN_SUMMARY_LENGTH = 20
MIN_TAGS_COUNT = 1
SCORE_MIN = 1
SCORE_MAX = 10


def validate_file(filepath: Path) -> list[str]:
    """Validate a single JSON file and return a list of error messages."""
    errors: list[str] = []

    # 1. Parse JSON
    try:
        text = filepath.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"无法读取文件: {exc}"]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"JSON 解析失败: {exc}"]

    if not isinstance(data, dict):
        return ["JSON 顶层必须是对象 (dict)"]

    # 2. Required fields and types
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in data:
            errors.append(f"缺少必填字段: {field}")
        elif not isinstance(data[field], expected_type):
            actual = type(data[field]).__name__
            errors.append(
                f"字段 '{field}' 类型错误: 期望 {expected_type.__name__}, "
                f"实际 {actual}"
            )

    # Stop further checks if required fields are missing or wrong type
    missing_or_wrong = {
        f for f in REQUIRED_FIELDS
        if f not in data or not isinstance(data[f], REQUIRED_FIELDS[f])
    }
    if missing_or_wrong:
        return errors

    # 3. ID format: {source}-{YYYYMMDD}-{NNN}
    id_value = data["id"]
    match = ID_PATTERN.match(id_value)
    if not match:
        errors.append(
            f"ID 格式错误: '{id_value}', "
            f"期望格式: {{source}}-{{YYYYMMDD}}-{{NNN}} (如 github-20260317-001)"
        )
    else:
        date_str = match.group(1)
        month = int(date_str[4:6])
        day = int(date_str[6:8])
        if month < 1 or month > 12 or day < 1 or day > 31:
            errors.append(f"ID 中的日期无效: {date_str}")

    # 4. Status enum
    if data["status"] not in VALID_STATUSES:
        errors.append(
            f"status 值无效: '{data['status']}', "
            f"有效值: {', '.join(sorted(VALID_STATUSES))}"
        )

    # 5. URL format
    if not URL_PATTERN.match(data["source_url"]):
        errors.append(
            f"source_url 格式无效: '{data['source_url']}', 需要匹配 https?://..."
        )

    # 6. Summary length
    if len(data["summary"]) < MIN_SUMMARY_LENGTH:
        errors.append(
            f"summary 长度不足: {len(data['summary'])} 字, "
            f"最少 {MIN_SUMMARY_LENGTH} 字"
        )

    # 7. Tags count
    if len(data["tags"]) < MIN_TAGS_COUNT:
        errors.append(f"tags 数量不足: {len(data['tags'])} 个, 最少 {MIN_TAGS_COUNT} 个")

    # 8. Optional: score
    if "score" in data:
        score = data["score"]
        if not isinstance(score, (int, float)):
            errors.append(
                f"score 类型错误: 期望 int/float, 实际 {type(score).__name__}"
            )
        elif not (SCORE_MIN <= score <= SCORE_MAX):
            errors.append(
                f"score 超出范围: {score}, 有效范围 {SCORE_MIN}-{SCORE_MAX}"
            )

    # 9. Optional: audience
    if "audience" in data:
        audience = data["audience"]
        if not isinstance(audience, str):
            errors.append(
                f"audience 类型错误: 期望 str, 实际 {type(audience).__name__}"
            )
        elif audience not in VALID_AUDIENCES:
            errors.append(
                f"audience 值无效: '{audience}', "
                f"有效值: {', '.join(sorted(VALID_AUDIENCES))}"
            )

    return errors


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("用法: python hooks/validate_json.py <json_file> [json_file2 ...]")
        sys.exit(1)

    # Expand glob patterns (shell usually handles this, but support manual *.json)
    files: list[Path] = []
    for arg in args:
        p = Path(arg)
        if p.is_file():
            files.append(p)
        elif "*" in arg:
            parent = p.parent
            pattern = p.name
            matched = sorted(parent.glob(pattern))
            if not matched:
                print(f"警告: 通配符 '{arg}' 未匹配到任何文件")
            files.extend(matched)
        else:
            print(f"警告: 跳过不存在的文件: {arg}")

    if not files:
        print("错误: 没有找到可校验的文件")
        sys.exit(1)

    total = len(files)
    passed = 0
    failed = 0
    all_errors: dict[Path, list[str]] = {}

    for filepath in files:
        errors = validate_file(filepath)
        if errors:
            failed += 1
            all_errors[filepath] = errors
        else:
            passed += 1
            print(f"  ✓ {filepath}")

    # Print errors
    if all_errors:
        print()
        for filepath, errors in all_errors.items():
            print(f"✗ {filepath}")
            for err in errors:
                print(f"    - {err}")
            print()

    # Summary
    print(f"校验完成: {total} 个文件, {passed} 通过, {failed} 失败")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
