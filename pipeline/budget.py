"""上下文预算工具：token 估算、文本截断、上游产物蒸馏。

14B 只有 8K 上下文（显存硬约束），因此评审/架构师阶段必须把上游产物蒸馏后再喂。
"""
from __future__ import annotations

import json
from typing import Any

from .config import CHARS_PER_TOKEN


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中英混排），仅用于预算裁剪，真实值以 ollama 返回的 prompt_eval_count 为准。"""
    if not text:
        return 0
    return int(len(text) / CHARS_PER_TOKEN) + 1


def truncate_text(text: str, max_tokens: int, marker: str = "\n…（已截断）") -> str:
    if not text:
        return ""
    max_chars = max(int(max_tokens * CHARS_PER_TOKEN), 200)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + marker


def distill(obj: Any, str_tokens: int = 160, list_items: int = 12, depth: int = 0) -> Any:
    """递归蒸馏：长字符串截断、长列表截断，保留结构（供 schema 化的上游产物复用）。"""
    if isinstance(obj, str):
        return truncate_text(obj, str_tokens, marker="…")
    if isinstance(obj, list):
        kept = [distill(x, str_tokens, list_items, depth + 1) for x in obj[:list_items]]
        if len(obj) > list_items:
            kept.append(f"…另有 {len(obj) - list_items} 项已省略")
        return kept
    if isinstance(obj, dict):
        return {k: distill(v, str_tokens, list_items, depth + 1) for k, v in obj.items()}
    return obj


def distill_json(obj: Any, str_tokens: int = 160, list_items: int = 12) -> str:
    return json.dumps(distill(obj, str_tokens, list_items), ensure_ascii=False, indent=1)


def fit_prompt(parts: list[str], budget_tokens: int, pin: list[str] | None = None) -> tuple[str, bool]:
    """把多个片段拼成一个 prompt，若超预算则依次丢弃最后的片段。

    `pin` 里的片段**永远不会被丢**（先给它们预留预算），用于「人工已确认的事实」「实现覆盖审计」
    这类必须被模型看到的指令。真机教训（2026-09-23）：评审阶段预算最紧，追加在末尾的人工事实块
    被整块截掉，评审于是又把人工已经答过的问题（"数据库表结构未知"）当成残留风险提出。

    返回 (prompt, truncated)；调用方应把 truncated 记入埋点。
    """
    pinned = [p for p in (pin or []) if p]
    pin_cost = sum(estimate_tokens(p) for p in pinned)
    budget = max(budget_tokens - pin_cost, 200)
    kept: list[str] = []
    used = 0
    truncated = pin_cost > budget_tokens
    for part in parts:
        cost = estimate_tokens(part)
        if used + cost > budget:
            truncated = True
            remain = budget - used
            if remain > 200:
                kept.append(truncate_text(part, remain))
                used += estimate_tokens(kept[-1])
            break
        kept.append(part)
        used += cost
    return "\n\n".join([*kept, *pinned]), truncated
