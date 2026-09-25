"""套用某次运行产出的锚定补丁。

默认 **dry-run**：只报告每条补丁会怎么套用，并把结果写到 `<run>/applied/` 副本里（不动原仓库）。
要真正改仓库必须显式 `--in-place`，且会先留 `*.orig` 备份。

用法:
    python tools/apply_patches.py --run 20260923-011410                       # 预览（写副本）
    python tools/apply_patches.py --run 20260923-011410 --out D:/tmp/applied  # 指定副本目录
    python tools/apply_patches.py --run 20260923-011410 --in-place            # 真正改仓库（留备份）
    python tools/apply_patches.py --run 20260923-011410 --repo <另一个仓库路径>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import patches, runstore  # noqa: E402
from pipeline.config import RUNS_DIR  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="套用运行产物里的锚定补丁（默认 dry-run）")
    parser.add_argument("--run", required=True, help="run_id 或 runs/<run_id> 路径")
    parser.add_argument("--runs-dir", default=str(RUNS_DIR), help=f"runs 目录，默认 {RUNS_DIR}")
    parser.add_argument("--repo", help="目标仓库（默认取该 run 记录里的仓库）")
    parser.add_argument("--out", help="把结果写到这个副本目录（默认 <run>/applied/）")
    parser.add_argument("--in-place", action="store_true", help="直接改仓库（会留 *.orig 备份）")
    args = parser.parse_args()

    run_dir = Path(args.run)
    if not run_dir.exists():
        run_dir = Path(args.runs_dir) / args.run
    state = runstore.read_state(run_dir)
    if not state:
        print(f"找不到 state.json：{run_dir}", file=sys.stderr)
        return 1
    artifacts = state.get("artifacts") or {}
    impl = artifacts.get("implementation")
    if not impl:
        print("该运行没有实现产物（dev 阶段还没跑）", file=sys.stderr)
        return 1

    repo = args.repo or state.get("repo")
    if not repo:
        print("不知道目标仓库：state.json 里没有 repo，请用 --repo 指定", file=sys.stderr)
        return 1

    audit = artifacts.get("patch_audit") or patches.analyze_all(repo, impl)
    print(f"仓库：{repo}")
    print(f"补丁：{audit.get('ok', 0)} 条可套用 / {audit.get('problems', 0)} 条有问题")
    for row in audit.get("edits") or []:
        mark = "OK  " if row.get("status") == "ok" else "SKIP"
        detail = patches.STATUS_CN.get(row.get("status"), row.get("status"))
        span = row.get("anchor_span") or row.get("symbol_span")
        print(
            f"  [{mark}] {row.get('symbol') or row.get('path'):<28} {detail}"
            + (f"  第 {span[0]}-{span[1]} 行" if span else "")
            + (f"  语义={row.get('patch_mode_used') or row.get('patch_mode')}" if row.get("patch_mode_used") else "")
        )
        for note in row.get("notes") or []:
            print(f"         └ {note}")

    out_dir = Path(args.out) if args.out else run_dir / "applied"
    try:
        report = patches.apply_all(repo, impl, audit, in_place=args.in_place, out_dir=out_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"套用失败：{exc}", file=sys.stderr)
        return 3

    print()
    for item in report["files"]:
        print(f"已写出：{item['written']}（{item['patches']} 条补丁）" + (f"  备份：{item['backup']}" if item.get("backup") else ""))
    if report["skipped"]:
        print("跳过：")
        for item in report["skipped"]:
            print(f"  - {item.get('symbol') or item.get('path') or '?'}：{item['reason']}")
    if not args.in_place:
        print()
        print("这是 dry-run（结果写在副本目录里，原仓库未改动）。要真正改仓库加 --in-place。")
        print("也可以直接使用生成的 unified diff：")
        for path in sorted((run_dir / "patches").glob("*.patch")):
            print(f"  git apply -p1 --directory=<仓库> {path}")
    (run_dir / "apply-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
