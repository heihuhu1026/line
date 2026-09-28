"""给 PM 未决项（强控闸门）提交人工裁决 —— 命令行版，逻辑与页面 `_save_pm_decisions` 一致。

为什么单独做工具：**裁决内容本身也是喂给下游的输入**（它会被写成 `confirmed_facts`，
进 PRD、进架构师方案、进验收标准）。所以这个工具刻意**不自动猜**：必须显式给答案，
否则就是把"机器编的假设"伪装成"人工已确认的事实" —— 那比不裁决更糟。

用法：
    python tools/pm_decide.py --run 20260928-110402 --list
    python tools/pm_decide.py --run 20260928-110402 --set 1=允许负数 --set 2=按显示序号
    python tools/pm_decide.py --run 20260928-110402 --answers-file runs/_pm_answers.json
答案文件格式（与 `--set` 等价）：{"1": "允许负数", "2": "按显示序号"}
最后会**复算闸门**并打印是否放行（pending / vague 计数）。
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import orchestrator as O  # noqa: E402
from pipeline import prompts as prompts_mod  # noqa: E402
from pipeline import runstore  # noqa: E402


def unresolved(run_dir: Path) -> dict:
    artifact = (runstore.latest_artifacts(run_dir) or {}).get("pm") or {}
    state = runstore.read_state(run_dir) or {}
    return O.pm_unresolved_items(artifact, state.get("pm_decisions") or []), artifact, state


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--set", action="append", default=[], metavar="N=答案")
    ap.add_argument("--answers-file")
    args = ap.parse_args()

    run_dir = ROOT / "runs" / args.run
    if not run_dir.is_dir():
        raise SystemExit(f"找不到运行目录 {run_dir}")

    left, artifact, state = unresolved(run_dir)
    items = [*left["pending"], *left["vague"]]
    if args.list or (not args.set and not args.answers_file):
        print(f"运行 {args.run}：未裁决 {len(left['pending'])} 条 / 未明确 {len(left['vague'])} 条")
        for i, text in enumerate(items, 1):
            print(f"  [{i}] {text}")
        if not items:
            print("  （闸门已放行，无需裁决）")
        print("\n（用 --set N=答案 提交；答案会变成下游可见的**陈述**）")
        return 0

    answers: dict[str, str] = {}
    if args.answers_file:
        raw = json.loads((ROOT / args.answers_file).read_text(encoding="utf-8"))
        answers.update({str(k): str(v) for k, v in raw.items()})
    for pair in args.set:
        num, _, text = pair.partition("=")
        if not text.strip():
            raise SystemExit(f"--set 需要 N=答案 形式，收到 {pair!r}")
        answers[num.strip()] = text.strip()

    decisions: list[dict] = []
    for num, text in answers.items():
        try:
            ref = items[int(num) - 1]
        except (ValueError, IndexError):
            raise SystemExit(f"--set 的编号 {num} 不在 1..{len(items)} 范围内") from None
        decisions.append({"ref": ref[:300], "decision": text[:1000]})

    if not decisions:
        print("没有可提交的裁决")
        return 0

    merged = 0
    if isinstance(artifact, dict):
        merged_art = prompts_mod.apply_pm_decisions(artifact, decisions, overwrite=True)
        before = unresolved(run_dir)[0]
        runstore.save_artifact(run_dir, "pm", merged_art, note="pm-decisions")
        after = unresolved(run_dir)[0]
        merged = (len(before["pending"]) + len(before["vague"])) - (
            len(after["pending"]) + len(after["vague"])
        )
    state["pm_decisions"] = decisions
    runstore.write_state(run_dir, state)

    # PRD 不在这里刷新：续跑时 `_persist` → `_write_prd` 会按产物重渲染，
    # 而这里多写一遍会与"人工改过 prd.md 就不覆盖"的约定打架（见 orchestrator._write_prd）。
    now, _, _ = unresolved(run_dir)
    for d in decisions:
        print(f"  已裁决：{d['ref'][:60]} → {d['decision'][:60]}")
    print(f"\n闸门复算：未裁决 {len(now['pending'])} 条 / 未明确 {len(now['vague'])} 条（消掉 {merged} 条）")
    if now["pending"] or now["vague"]:
        print("  ⚠ 仍有剩余项 —— 续跑时会被**再次拦下**（这是刻意的强控）")
        for text in [*now["pending"], *now["vague"]]:
            print(f"    - {text}")
        return 1
    print("  ✅ 放行：可以 --resume 续跑")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
