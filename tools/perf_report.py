"""阶段级 LLM 性能报告：把埋点聚合成人能读的表，并按基线标出退化。

**聚合本身不在这里重写** —— `issues.build_report()` 的 `by_stage` 已经算了
`calls / wall_s / prompt_tokens / output_tokens / contract_retries / switches / avg_prefill_tps`。
这里只做它没做的三件事：

  1. **渲染**成人能读的表（`report_issues.py` 只渲染问题计数）；
  2. **按 prompt 长度分桶** —— 实测同一阶段内 prefill 速率随 n 单调下降
     （同一台机器：n=1536→507 t/s、n=3072→255、n=5120→163、n=7680→114），
     不分桶看 min/max 会把"长 prompt 的健康"误读成"退化"；
  3. 用 `pipeline.perfguard.baseline_for()` 的**同一套基线**判 DEGRADED ——
     避免"性能报告一个阈值、运行期护栏另一个阈值"这种两处漂移。

退化长什么样（2026-09-27 实测，本机 AMD RX 6700 + Vulkan）：同一条曲线整体 ÷12~30，
约 1/6 请求命中，且**逐请求翻转、会自行恢复**（见 pipeline/perfguard.py 的完整说明）。

用法:
    python tools/perf_report.py                 # 最近 30 次运行
    python tools/perf_report.py --limit 100
    python tools/perf_report.py --stage review  # 只看某阶段
    python tools/perf_report.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import perfguard, runstore  # noqa: E402
from pipeline.config import RUNS_DIR  # noqa: E402

#: prompt 长度分桶（token）。桶内速率才有可比性（注意力开销随 n 增长）。
BUCKETS: tuple[tuple[int, int, str], ...] = (
    (0, 500, "<500"),
    (500, 2000, "500-2k"),
    (2000, 5000, "2k-5k"),
    (5000, 10**9, ">5k"),
)


def _bucket(n: int) -> str:
    for lo, hi, name in BUCKETS:
        if lo <= n < hi:
            return name
    return "?"


def collect(runs_dir: Path, limit: int, only_stage: str | None = None) -> dict:
    """扫最近 N 次运行的 `state.calls`，按 (stage, tag, prompt 桶) 聚合。"""
    rows: dict[tuple[str, str, str], dict] = {}
    dirs = sorted(
        [d for d in runs_dir.iterdir() if d.is_dir() and not d.name.startswith((".", "_"))]
    )[-limit:]
    used = 0
    for run_dir in dirs:
        state = runstore.read_state(run_dir) or {}
        calls = state.get("calls") or []
        if not calls:
            continue
        used += 1
        for call in calls:
            stage = str(call.get("stage") or "?")
            if only_stage and stage != only_stage:
                continue
            n = int(call.get("prompt_tokens") or 0)
            tps = call.get("prefill_tps")
            gen = call.get("gen_tps")
            key = (stage, str(call.get("tag") or "?"), _bucket(n))
            row = rows.setdefault(
                key,
                {"calls": 0, "wall_s": 0.0, "tokens_in": 0, "tokens_out": 0,
                 "prefill": [], "gen": [], "switches": 0, "over_budget": 0},
            )
            row["calls"] += 1
            row["wall_s"] += float(call.get("wall_s") or 0)
            row["tokens_in"] += n
            row["tokens_out"] += int(call.get("output_tokens") or 0)
            row["switches"] += 1 if call.get("switched") else 0
            row["over_budget"] += 1 if call.get("prompt_over_budget") else 0
            if tps:
                row["prefill"].append(float(tps))
            if gen:
                row["gen"].append(float(gen))
    out = []
    for (stage, tag, bucket), row in sorted(rows.items()):
        pf = sorted(row["prefill"])
        base = perfguard.baseline_for(tag)
        avg = sum(pf) / len(pf) if pf else 0.0
        out.append(
            {
                "stage": stage,
                "tag": tag,
                "bucket": bucket,
                "calls": row["calls"],
                "wall_s": round(row["wall_s"], 1),
                "avg_wall_s": round(row["wall_s"] / max(1, row["calls"]), 1),
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "prefill_avg": round(avg, 1),
                "prefill_min": round(pf[0], 1) if pf else None,
                "prefill_max": round(pf[-1], 1) if pf else None,
                "gen_avg": round(sum(row["gen"]) / len(row["gen"]), 1) if row["gen"] else None,
                "baseline": base,
                "degraded": bool(pf) and avg < base * perfguard.DEGRADED_RATIO,
                # 慢调用占比：比 avg 更能反映"被几次退化拖垮"的体感
                "slow_share": round(
                    sum(1 for t in pf if t < base * perfguard.DEGRADED_RATIO) / len(pf), 2
                ) if pf else None,
                "switches": row["switches"],
                "over_budget": row["over_budget"],
            }
        )
    return {"runs_analyzed": used, "rows": out}


def render(report: dict) -> str:
    lines = [
        f"# 阶段性能报告（{report['runs_analyzed']} 次运行）",
        "",
        "prefill 速率**必须按 prompt 长度分桶看**：同一台机器上 n 越大速率越低（注意力开销），"
        "不分桶会把长 prompt 的健康误判成退化。",
        "",
        "| stage | tag | prompt | calls | 均 wall | prefill 均/最低 | gen 均 | 基线 | 慢调用占比 | 判定 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in report["rows"]:
        flag = "**DEGRADED**" if r["degraded"] else "ok"
        prefill = f"{r['prefill_avg']}/{r['prefill_min']}" if r["prefill_min"] else "-"
        lines.append(
            f"| {r['stage']} | {r['tag']} | {r['bucket']} | {r['calls']} | {r['avg_wall_s']}s | "
            f"{prefill} | {r['gen_avg'] or '-'} | {r['baseline']:.0f} | "
            f"{('%.0f%%' % (100 * r['slow_share'])) if r['slow_share'] is not None else '-'} | {flag} |"
        )
    bad = [r for r in report["rows"] if r["degraded"]]
    lines += ["", f"退化桶：{len(bad)} / {len(report['rows'])}"]
    for r in bad:
        lines.append(
            f"  - {r['stage']}@{r['bucket']}：均 {r['prefill_avg']} t/s（基线 {r['baseline']:.0f}，"
            f"慢调用 {r['slow_share']:.0%}）—— 逐请求退化/会自行恢复，见 pipeline/perfguard.py"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段级 LLM 性能报告")
    parser.add_argument("--runs-dir", default=str(RUNS_DIR))
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--stage", help="只看某阶段")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    runs_dir = Path(args.runs_dir)
    if not runs_dir.exists():
        print(f"找不到 runs 目录: {runs_dir}", file=sys.stderr)
        return 1
    report = collect(runs_dir, args.limit, args.stage)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    print(render(report))
    return 1 if any(r["degraded"] for r in report["rows"]) else 0


if __name__ == "__main__":
    sys.exit(main())
