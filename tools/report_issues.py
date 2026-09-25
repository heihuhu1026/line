"""跨运行问题汇总：把 runs/ 下所有运行的问题记录聚合成一份可读报告。

用法:
    python tools/report_issues.py                 # 写到 runs/_reports/issues-<时间戳>.md
    python tools/report_issues.py --json          # 只打印 JSON（给别的程序/模型用）
    python tools/report_issues.py --out report.md # 指定输出文件
    python tools/report_issues.py --limit 50      # 只统计最近 50 次运行
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import issues  # noqa: E402
from pipeline.config import RUNS_DIR  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="流水线问题总览（跨运行）")
    parser.add_argument("--runs-dir", default=str(RUNS_DIR), help=f"runs 目录，默认 {RUNS_DIR}")
    parser.add_argument("--out", help="输出文件（默认 runs/_reports/issues-<时间戳>.md）")
    parser.add_argument("--limit", type=int, default=200, help="只统计最近 N 次运行")
    parser.add_argument("--json", action="store_true", help="只打印 JSON，不写文件")
    args = parser.parse_args()

    runs_dir = Path(args.runs_dir)
    if not runs_dir.exists():
        print(f"找不到 runs 目录: {runs_dir}", file=sys.stderr)
        return 1

    report = issues.build_report(runs_dir, limit_runs=args.limit)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    text = issues.report_markdown(report)
    out = Path(args.out) if args.out else runs_dir / "_reports" / f"issues-{time.strftime('%Y%m%d-%H%M%S')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    totals = report["totals"]
    print(f"统计了 {report['runs_analyzed']} 次运行，共 {totals['issues']} 条问题记录")
    for kind, count in list(totals["by_kind"].items())[:10]:
        print(f"  - {issues.KIND_CN.get(kind, kind)}: {count}")
    print(f"报告已写入: {out}")
    print("交给模型做元优化: python tools\\meta_optimize.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
