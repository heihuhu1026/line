"""上下文预算工具：token 估算、文本截断、上游产物蒸馏。

14B 只有 8K 上下文（显存硬约束），因此评审/架构师阶段必须把上游产物蒸馏后再喂。

token 估算的来历（2026-09-26 重做）
----------------------------------
旧实现是 ``len(text) / CHARS_PER_TOKEN``（固定 1.6 字符/token）。用真机 3 个 run、
41 条样本对照 ollama 实回的 ``prompt_eval_count`` 实测：**平均高估 40.9%，且 41 条
全是高估**——等于近一半预算浪费在「以为占了、其实没占」的额度上，真正该喂的代码被
提前截掉。

现按字符类别加权估算，系数由 ``tools/calibrate_tokens.py`` 在同一批真机样本上拟合：

    ASCII      0.28 token/字
    非 ASCII   0.76 token/字

同一批样本上平均绝对误差 **1.7%**（各阶段 1.2%~3.2%），带符号偏差 -0.4%。
注意：这些系数是**量出来的，不是猜的**——外部文档曾建议 CJK 取 1.5 token/字，
按那组系数实测误差 **50.6%**，比不改还差，所以没有采用。

换模型/换硬件后想重新校准：``python -m tools.calibrate_tokens``，它会打印可直接
粘贴的系数值。
"""
from __future__ import annotations

import json
from typing import Any

from .config import CHARS_PER_TOKEN, TOK_PER_ASCII_CHAR, TOK_PER_NONASCII_CHAR


def estimate_tokens(text: str) -> int:
    """按字符类别加权估算 token 数（中英混排）。

    只用于预算裁剪；真实值以 ollama 返回的 ``prompt_eval_count`` 为准。
    """
    if not text:
        return 0
    total = len(text)
    # ``encode('ascii', 'ignore')`` 会丢掉所有非 ASCII 字符，长度即 ASCII 字数；
    # 这是 C 层实现，比逐字符 ord() 快得多，且结果精确（不是估算）。
    ascii_n = len(text.encode("ascii", "ignore")) if not text.isascii() else total
    return int(ascii_n * TOK_PER_ASCII_CHAR + (total - ascii_n) * TOK_PER_NONASCII_CHAR) + 1


def chars_for_tokens(max_tokens: int, sample: str | None = None) -> int:
    """把 token 上限换算成字符上限。

    给了 ``sample`` 就按该文本自身的「字符/token」比换算 —— 同一台机器上代码与中文
    的比例差近 3 倍（代码约 3.6 字/token、中文约 1.3 字/token），用固定常数必然
    一侧浪费一侧超限。没有样本时退回 ``CHARS_PER_TOKEN``（保守值）。
    """
    if max_tokens <= 0:
        return 0
    if sample:
        est = estimate_tokens(sample)
        if est > 0:
            return max(int(max_tokens * len(sample) / est), 1)
    return max(int(max_tokens * CHARS_PER_TOKEN), 1)


def truncate_text(text: str, max_tokens: int, marker: str = "\n…（已截断）") -> str:
    """按 token 预算截断文本。

    旧实现用 ``max_tokens * CHARS_PER_TOKEN`` 反推字符数：那个常数在代码与中文之间
    差近 3 倍，截出来的长度必然忽长忽短。现在直接对「估算 token 数」二分——估算对
    前缀长度单调不减，所以二分正确，且长度自适应文本自身构成。
    """
    if not text:
        return ""
    if max_tokens <= 0:
        return marker
    if estimate_tokens(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + marker


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
