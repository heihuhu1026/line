"""用真机运行数据校准 token 估算系数。

**为什么需要它**：预算裁剪要靠 token 估算，而估算是按字符类别加权的启发式 ——
换个模型（分词器不同）系数就该重拟合。旧实现是固定「1.6 字符/token」，
在真机 41 条样本上对照 ollama 实回的 ``prompt_eval_count`` 实测**平均高估 40.9%**，
且**全是高估**：近一半预算浪费在「以为占了、其实没占」的额度上，真正该喂的代码被提前截掉。

**它怎么算**：读 ``runs/*/traces.jsonl``（完整 system/user 文本）与配对的
``runs/*/llm-calls.jsonl``（``prompt_tokens`` = ollama 实回的真实值），
按 (ASCII, 非 ASCII) 两类字符数做最小二乘拟合，并报告拟合前后的误差。

用法::

    python tools/calibrate_tokens.py                 # 用 runs/ 下的全部运行
    python tools/calibrate_tokens.py --runs-dir DIR  # 指定 runs 目录
    python tools/calibrate_tokens.py --json          # 只输出 JSON（供脚本消费）

拿到系数后，两种生效方式（任选）：
  1. 设环境变量 ``PIPELINE_TOK_ASCII`` / ``PIPELINE_TOK_NONASCII``；
  2. 改 ``pipeline/config.py`` 里 ``TOK_PER_ASCII_CHAR`` / ``TOK_PER_NONASCII_CHAR`` 的默认值。

样本建议：**20 条以上**、覆盖多个阶段。样本太少时系数会过拟合
（曾见「其它字符」类系数被拟合成负数）。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import budget  # noqa: E402
from pipeline.config import RUNS_DIR, TOK_PER_ASCII_CHAR, TOK_PER_NONASCII_CHAR  # noqa: E402


def _load_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            with contextlib.suppress(ValueError):
                out.append(json.loads(line))
    return out


def collect_samples(runs_dir: Path) -> list[tuple[str, str, str, int]]:
    """返回 ``[(run_id, stage, prompt 全文, ollama 实回 token 数)]``。

    traces.jsonl 与 llm-calls.jsonl 按 ``(stage, attempt)`` 顺序配对：
    ``_record`` 会先写 llm-calls 再写 trace，两者顺序一致。
    mock 运行的 ``prompt_tokens`` 本身就是估算值，必须排除（否则会「自己校准自己」）。
    """
    samples: list[tuple[str, str, str, int]] = []
    for run in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        traces = _load_jsonl(run / "traces.jsonl")
        calls = _load_jsonl(run / "llm-calls.jsonl")
        by_key: dict[tuple, list[dict]] = {}
        for call in calls:
            by_key.setdefault((call.get("stage"), call.get("attempt")), []).append(call)
        used: dict[tuple, int] = {}
        for trace in traces:
            key = (trace.get("stage"), trace.get("attempt"))
            bucket = by_key.get(key) or []
            idx = used.get(key, 0)
            if idx >= len(bucket):
                continue
            used[key] = idx + 1
            call = bucket[idx]
            actual = call.get("prompt_tokens") or 0
            if call.get("mock") or not actual:
                continue
            text = (trace.get("system") or "") + "\n\n" + (trace.get("user") or "")
            if text.strip():
                samples.append((run.name, str(trace.get("stage")), text, int(actual)))
    return samples


def _counts(text: str) -> tuple[int, int]:
    """(ASCII 字符数, 非 ASCII 字符数) —— 与 budget.estimate_tokens 的口径一致。"""
    total = len(text)
    ascii_n = total if text.isascii() else len(text.encode("ascii", "ignore"))
    return ascii_n, total - ascii_n


def _est(text: str, coef: tuple[float, float]) -> int:
    """按给定系数估算 token 数 —— 与 ``budget.estimate_tokens`` 同一算式。"""
    ascii_n, nonascii_n = _counts(text)
    return int(ascii_n * coef[0] + nonascii_n * coef[1]) + 1


def _least_squares(rows: list[tuple[int, int]], targets: list[int]) -> tuple[float, float]:
    """两参数最小二乘：解 A^T A x = A^T b（纯标准库，不引 numpy）。"""
    s_aa = sum(a * a for a, _ in rows)
    s_an = sum(a * b for a, b in rows)
    s_nn = sum(b * b for _, b in rows)
    s_at = sum(a * t for (a, _), t in zip(rows, targets, strict=True))
    s_nt = sum(b * t for (_, b), t in zip(rows, targets, strict=True))
    det = s_aa * s_nn - s_an * s_an
    if abs(det) < 1e-9:
        return TOK_PER_ASCII_CHAR, TOK_PER_NONASCII_CHAR
    return (s_nn * s_at - s_an * s_nt) / det, (s_aa * s_nt - s_an * s_at) / det


def _errors(samples, coef) -> tuple[float, float, int]:
    """返回 (平均绝对误差, 平均带符号偏差, 高估合计)。"""
    abs_err: list[float] = []
    signed: list[float] = []
    over = 0
    for _rid, _stage, text, actual in samples:
        est = _est(text, coef)
        abs_err.append(abs(est - actual) / actual)
        signed.append((est - actual) / actual)
        over += max(est - actual, 0)
    return statistics.mean(abs_err), statistics.mean(signed), over


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用真机运行数据校准 token 估算系数")
    parser.add_argument("--runs-dir", default=str(RUNS_DIR), help=f"runs 目录，默认 {RUNS_DIR}")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument("--min-samples", type=int, default=10, help="少于这个数量就拒绝给系数")
    args = parser.parse_args(argv)

    # 自检：本工具的算式必须与 budget.estimate_tokens 完全一致。
    # 两边漂移的话，这里算出来的「当前误差」就不是线上真实的误差，校准结论会误导人。
    probe = "abc中文函数 def foo(): return 1\n" * 20
    if budget.estimate_tokens(probe) != _est(probe, (TOK_PER_ASCII_CHAR, TOK_PER_NONASCII_CHAR)):
        print("⚠️ 本工具与 budget.estimate_tokens 的算式不一致 —— 先修这个再校准。")
        return 1

    samples = collect_samples(Path(args.runs_dir))
    if not samples:
        print(f"没找到可用样本（{args.runs_dir} 下需要 traces.jsonl + 非 mock 的 llm-calls.jsonl）")
        return 1

    rows = [_counts(text) for _rid, _s, text, _a in samples]
    targets = [actual for _rid, _s, _t, actual in samples]
    fitted = _least_squares(rows, targets)
    # 取两位小数：既好读，实测上也比未取整略准（浮点噪声），且便于写进环境变量
    rounded = (round(fitted[0], 2), round(fitted[1], 2))
    current = (TOK_PER_ASCII_CHAR, TOK_PER_NONASCII_CHAR)

    cur_err, cur_bias, cur_over = _errors(samples, current)
    fit_err, fit_bias, fit_over = _errors(samples, rounded)

    if args.json:
        print(json.dumps({
            "samples": len(samples),
            "current_coef": current, "current_mae": round(cur_err, 4),
            "fitted_coef": rounded, "fitted_mae": round(fit_err, 4),
            "total_actual_tokens": sum(targets),
        }, ensure_ascii=False, indent=2))
        return 0

    by_stage: dict[str, list[float]] = {}
    for _rid, stage, text, actual in samples:
        a, n = _counts(text)
        est = int(a * rounded[0] + n * rounded[1]) + 1
        by_stage.setdefault(stage, []).append(abs(est - actual) / actual)

    print(f"样本：{len(samples)} 条（来自 {len({s[0] for s in samples})} 个 run）")
    print(f"实际 token 总量：{sum(targets)}")
    print()
    print(f"当前系数  ASCII={current[0]}  非ASCII={current[1]}")
    print(f"  平均绝对误差 {cur_err:.1%} | 带符号偏差 {cur_bias:+.1%} | 高估合计 {cur_over} token")
    print(f"拟合系数  ASCII={rounded[0]}  非ASCII={rounded[1]}")
    print(f"  平均绝对误差 {fit_err:.1%} | 带符号偏差 {fit_bias:+.1%} | 高估合计 {fit_over} token")
    print()
    print("按阶段看拟合后的误差：")
    for stage, errs in sorted(by_stage.items(), key=lambda kv: -statistics.mean(kv[1])):
        print(f"  {stage:<24} n={len(errs):<4} 平均绝对误差 {statistics.mean(errs):.1%}")
    print()

    if len(samples) < args.min_samples:
        print(f"⚠️ 样本只有 {len(samples)} 条（< {args.min_samples}）：系数可能过拟合，**不建议**直接采用。")
        print("   多跑几个 run（覆盖 pm/architect/dev/test/review）再算。")
        return 0

    if abs(rounded[0] - current[0]) < 0.02 and abs(rounded[1] - current[1]) < 0.02:
        print("✅ 与当前系数基本一致，无需改动。")
        return 0

    print("要采用这组系数，任选一种：")
    print(f'  1) 环境变量：$env:PIPELINE_TOK_ASCII="{rounded[0]}"; $env:PIPELINE_TOK_NONASCII="{rounded[1]}"')
    print(f"  2) 改 pipeline/config.py：TOK_PER_ASCII_CHAR = {rounded[0]}"
          f" / TOK_PER_NONASCII_CHAR = {rounded[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
