"""facet / 文件级需求匹配阈值的**校准测量**（CONTEXT §35 未完成清单第 7 项）。

为什么要单独一个工具：阈值（``ontology._LINK_MIN_SHARED`` / ``ontology._LINK_RATIO``）
原先是按**文件级**文本调的；facet 粒度文本更短，``shared >= 2`` 更难达标
（真机实测 T-02 的 FR-04 落入 ``candidate_requirement_ids`` —— 未漏未误判，只是未命中）。
要动阈值**必须先有数字**，否则是拿"过绑定"（P0-3 刚修掉的问题）去换命中率。

本工具在真实 run 上同时量**两种粒度**，并扫一遍阈值网格：
    · 文件级（旧形态）：把同一文件所有图合并成一个单元 —— 复现"同文件每张图拿到同一批 FR"
    · facet 级（P0-3）：每张图用自己的 change/acceptance/symbols/interface 匹配

指标（都只是**计数**，不含主观判断）：
    hit     —— 至少命中 1 条需求的图/文件数
    over    —— 单图命中 ≥3 条需求（过绑定的机械代理指标）
    links   —— 需求×图 的连接总数
    orphan  —— 没有任何图命中的需求数（漏挂的代理指标）
    cand    —— facet 未命中、但文件级命中的需求（"落入候选"的规模，即本项要提升的部分）

用法::

    python tools/check_facet_links.py                       # 扫 runs/ 下所有含语料的 run
    python tools/check_facet_links.py --run 20260930-000332 # 只看某次
    python tools/check_facet_links.py --runs runs --grid    # 打印阈值网格
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import ontology, taskcompiler  # noqa: E402

#: 扫描的阈值网格：min_shared ∈ 1..3，ratio ∈ 0.2..0.5
MIN_SHARED_GRID = (1, 2, 3)
RATIO_GRID = (0.20, 0.35, 0.50)


def _load_state(run_dir: Path) -> dict:
    """读 state.json；产物层在 ``artifacts`` 里（顶层是快照键）。"""
    path = run_dir / "state.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    arts = data.get("artifacts")
    return dict(arts) if isinstance(arts, dict) else data


def _corpus(state: dict) -> tuple[dict, list[dict]]:
    """从一次 run 里取出 (scope, draft tasks)；缺任一项即视为无语料。"""
    scope = state.get("scope")
    tasks = state.get("plan_draft_tasks") or (state.get("plan") or {}).get("tasks") or []
    tasks = [t for t in tasks if isinstance(t, dict)]
    return (scope if isinstance(scope, dict) else {}), tasks


def _file_units(tasks: list[dict]) -> list[dict]:
    """把同一文件的所有图**合并**成一个单元 —— 复现文件级旧形态的匹配输入。"""
    merged: dict[str, dict] = {}
    for task in tasks:
        for path in (task.get("target_files") or []):
            path = str(path or "").replace("\\", "/").strip()
            if not path:
                continue
            slot = merged.setdefault(path, {
                "file": path, "symbols": [], "change": [], "interface": [],
                "acceptance": [], "intent": [],
            })
            slot["symbols"].extend(str(s) for s in (task.get("symbols") or []))
            slot["change"].append(str(task.get("change") or ""))
            slot["interface"].append(str(task.get("interface") or ""))
            if task.get("acceptance"):
                slot["acceptance"].append(task.get("acceptance"))
    for slot in merged.values():
        slot["symbols"] = sorted(set(slot["symbols"]))
    return list(merged.values())


def _facet_links(scope: dict, task: dict) -> list[str]:
    """facet 级命中（与 P0-3 主线同一函数，不另写一套匹配）。"""
    return list(taskcompiler.bind_task_requirements(scope, task).get("requirements") or [])


def _measure(scope: dict, tasks: list[dict]) -> dict:
    """当前阈值下的两种粒度读数 + 落入候选的规模。"""
    facet: dict[str, list[str]] = {}
    for task in tasks:
        tid = str(task.get("id") or "")
        facet[tid] = _facet_links(scope, task)
    file_links = ontology.requirement_unit_links(scope, _file_units(tasks))["by_file"]
    file_hit = {f: list(v.get("requirements") or []) for f, v in file_links.items()}

    reqs = {f"req:{c['req_id']}" for c in ontology.requirement_claims(scope) if c.get("req_id")}
    facet_reqs = {r for hit in facet.values() for r in hit}
    file_reqs = {r for hit in file_hit.values() for r in hit}

    # "落入候选"：文件级命中、facet 未命中 —— 阈值若能再收一点，这些就能挂到具体图上
    cand: dict[str, list[str]] = {}
    for task in tasks:
        tid = str(task.get("id") or "")
        file_level: set[str] = set()
        for path in (task.get("target_files") or []):
            file_level |= set(file_hit.get(str(path).replace("\\", "/"), []))
        missing = sorted(file_level - set(facet[tid]))
        if missing:
            cand[tid] = missing
    return {
        "tasks": len(tasks),
        "facet": facet,
        "facet_hit": sum(1 for v in facet.values() if v),
        "facet_over": sum(1 for v in facet.values() if len(v) >= 3),
        "facet_links": sum(len(v) for v in facet.values()),
        "file_level": {f: sorted(v) for f, v in file_hit.items()},
        "file_over": sum(1 for v in file_hit.values() if len(v) >= 3),
        "file_links": sum(len(v) for v in file_hit.values()),
        "orphan": sorted(reqs - (facet_reqs | file_reqs)),
        "candidate_only": {k: v for k, v in cand.items() if v},
    }


def _row(label: str, m: dict) -> str:
    return (f"  {label:<28} 命中图 {m['facet_hit']:>2}/{m['tasks']:<2} "
            f"过绑定(≥3) {m['facet_over']:<2} 连接 {m['facet_links']:<3} "
            f"| 文件级 命中 {len(m['file_level']):<2} 过绑定 {m['file_over']:<2} "
            f"连接 {m['file_links']:<3} | 漏挂需求 {len(m['orphan']):<2} "
            f"落入候选 {sum(len(v) for v in m['candidate_only'].values()):<2}")


def _report_run(run_dir: Path, *, grid: bool) -> bool:
    state = _load_state(run_dir)
    scope, tasks = _corpus(state)
    if not scope or not tasks or not ontology.requirement_claims(scope):
        return False
    print(f"\n== {run_dir.name}（需求 {len(ontology.requirement_claims(scope))} 条 / "
          f"图 {len(tasks)} 张）")
    base = _measure(scope, tasks)
    print(_row(f"当前阈值 (≥{ontology._LINK_MIN_SHARED}, ≥{ontology._LINK_RATIO})", base))  # noqa: SLF001
    for tid, hit in base["facet"].items():
        cand = base["candidate_only"].get(tid) or []
        print(f"      {tid:<6} facet={hit or '（无）'}"
              + (f"  落入候选={cand}" if cand else ""))
    if base["orphan"]:
        print(f"      无人认领的需求：{base['orphan']}")

    if grid:
        old = (ontology._LINK_MIN_SHARED, ontology._LINK_RATIO)  # noqa: SLF001
        try:
            print("  -- 阈值网格（facet 粒度）--")
            for ms in MIN_SHARED_GRID:
                for ratio in RATIO_GRID:
                    ontology._LINK_MIN_SHARED = ms  # noqa: SLF001
                    ontology._LINK_RATIO = ratio    # noqa: SLF001
                    m = _measure(scope, tasks)
                    mark = "  ← 当前" if (ms, ratio) == old else ""
                    print(f"     min_shared={ms} ratio={ratio:.2f}"
                          f"  命中 {m['facet_hit']}  过绑定 {m['facet_over']}"
                          f"  连接 {m['facet_links']}  落入候选 "
                          f"{sum(len(v) for v in m['candidate_only'].values())}"
                          f"  漏挂 {len(m['orphan'])}{mark}")
        finally:
            ontology._LINK_MIN_SHARED, ontology._LINK_RATIO = old  # noqa: SLF001
    # 结论口径提醒（避免把"连接更多"当成"更好"）
    print("  注：连接变多**不等于**更好 —— 过绑定正是 P0-3 修掉的问题。"
          "优先挑「命中↑ 且 过绑定不升 且 漏挂不升」的格点。")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="facet / 文件级需求匹配阈值校准")
    ap.add_argument("--runs", default="runs", help="runs 目录（默认 runs）")
    ap.add_argument("--run", default="", help="只看某次 run id")
    ap.add_argument("--grid", action="store_true", help="打印阈值网格")
    args = ap.parse_args()

    root = Path(args.runs)
    if args.run:
        dirs = [root / args.run]
    else:
        dirs = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    hit = [d for d in dirs if _report_run(d, grid=args.grid)]
    print(f"\n有语料（scope + draft tasks）的 run：{len(hit)} / {len(dirs)}")
    if not hit:
        print("没有可用于校准的 run：需要同时有 scope.functional_requirements 与 plan_draft_tasks。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
