"""打印某次运行的阶段成本表（读 llm-calls.jsonl，输出纯 ASCII 便于在任意终端查看）。

用法:
    python tools/show_run.py                 # 最近一次
    python tools/show_run.py 20260922-234723
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else None
    if target:
        run_dir = RUNS / target
    else:
        dirs = sorted(p for p in RUNS.glob("20*") if p.is_dir())
        if not dirs:
            print("runs/ 下没有运行记录")
            return 1
        run_dir = dirs[-1]

    calls_path = run_dir / "llm-calls.jsonl"
    if not calls_path.exists():
        print(f"{run_dir} 下没有 llm-calls.jsonl")
        return 1

    rows = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def rate(tokens: int, seconds: float) -> str:
        return f"{tokens / seconds:>6.1f}" if seconds else "     -"

    header = (
        f"{'#':>2} {'stage':<17}{'tag':<26}{'think':<6}{'switch':<7}{'load_s':>7}{'wall_s':>7}"
        f"{'prompt':>7}{'pref_t/s':>9}{'out':>6}{'gen_t/s':>8}  note"
    )
    print(f"run: {run_dir.name}   calls: {len(rows)}")
    print(header)
    print("-" * len(header))
    for i, c in enumerate(rows, 1):
        print(
            f"{i:>2} {c.get('stage',''):<17}{c.get('tag',''):<26}"
            f"{str(c.get('think')):<6}{('Y' if c.get('switched') else '-'):<7}"
            f"{c.get('load_s', 0):>7.1f}{c.get('wall_s', 0):>7.1f}"
            f"{c.get('prompt_tokens', 0):>7}{rate(c.get('prompt_tokens', 0), c.get('prompt_s', 0))}"
            f"{c.get('output_tokens', 0):>6}{rate(c.get('output_tokens', 0), c.get('eval_s', 0))}  {c.get('note') or ''}"
            + ("  [RETRY]" if (c.get("attempt") or 1) > 1 else "")
            + ("  [over-budget]" if c.get("prompt_over_budget") else "")
            + ("  [trimmed]" if c.get("truncated") else "")
        )
    total_wall = sum(c.get("wall_s", 0) for c in rows)
    total_load = sum(c.get("load_s", 0) for c in rows)
    switches = sum(1 for c in rows if c.get("switched"))
    print("-" * len(header))
    print(
        f"合计: wall {total_wall:.1f}s | load {total_load:.1f}s | 切换 {switches} 次 | "
        f"prompt {sum(c.get('prompt_tokens',0) for c in rows)} tok | "
        f"output {sum(c.get('output_tokens',0) for c in rows)} tok"
    )

    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        s = json.loads(summary_path.read_text(encoding="utf-8"))
        print(f"判定: {s.get('verdict')}  rounds={s.get('rounds')}  needs_human={s.get('needs_human')}")
        if s.get("grounding_warnings"):
            print("未接地路径告警:")
            for warn in s["grounding_warnings"]:
                print(f"  - {warn['stage']}: {warn['paths'][:6]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
