"""人工审核标记节点：超出最大轮次时暂存 analyses，等待人工介入。"""

import json
from datetime import datetime, timezone
from pathlib import Path

from workflows.state import KBState

# ---------------------------------------------------------------------------
# 目录常量
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PENDING_DIR = _PROJECT_ROOT / "knowledge" / "pending_review"


# ---------------------------------------------------------------------------
# human_flag_node
# ---------------------------------------------------------------------------


def human_flag_node(state: KBState) -> dict:
    """将超出审核轮次的 analyses 整理为 JSON 暂存到 knowledge/pending_review/。

    文件名格式：pending-{timestamp}.json
    文件内容：timestamp, iterations_used, last_feedback, analyses
    """
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")

    payload = {
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "iterations_used": state.get("iteration", 0),
        "last_feedback": state.get("review_feedback", ""),
        "analyses": state.get("analyses", []),
    }

    _PENDING_DIR.mkdir(parents=True, exist_ok=True)
    filepath = _PENDING_DIR / f"pending-{timestamp}.json"

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[HumanFlag] 已暂存至 {filepath.relative_to(_PROJECT_ROOT)}")
    print(
        f"[HumanFlag] 轮次={payload['iterations_used']}, "
        f"条目={len(payload['analyses'])}"
    )

    return {"needs_human_review": True}
