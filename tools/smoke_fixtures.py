"""规格§四十九/§五十：_repro/fixture_002–008 离线回放冒烟。

每个 fixture 是一份脱敏的「真机形态 → 机械期望」JSON：input 只喂内核纯函数
（patches / ontology / planir / taskcompiler / diagnose，不调 LLM、不依赖仓库），
expect 锁定 ontology 投影、Proof Gate 裁决与 recovery 责任。fixture_001 的裁决
部分由 smoke_ontology.py F 段回放（无 scenario 字段，这里跳过）。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import diagnose as DGN  # noqa: E402
from pipeline import ontology as O  # noqa: E402
from pipeline import patches  # noqa: E402
from pipeline import planir as P  # noqa: E402
from pipeline import taskcompiler as TC  # noqa: E402

PASS = FAIL = 0


def check(cond: bool, name: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  <- {extra}" if extra else ""))


def _proof_decision(proof: dict) -> dict:
    return DGN.review_decision(
        semantic_verdict="pass", blocked=False, has_in_material=True,
        has_architect_fixes=False, proof=proof)


def _replay_patches_apply(fx: dict, inp: dict, exp: dict) -> None:
    with tempfile.TemporaryDirectory(prefix="fixture_patches_") as tmp:
        repo = Path(tmp) / ("empty-repo" if inp.get("repo_state") == "empty" else "repo")
        repo.mkdir()
        impl = inp["impl"]
        audit = patches.analyze_all(str(repo), impl)
        statuses = [e.get("status") for e in audit["edits"]]
        check(statuses == exp["statuses"], f"{fx['id']}：edit 状态序列 {exp['statuses']}", str(statuses))
        out = Path(tmp) / "out"
        report = patches.apply_all(str(repo), impl, audit, in_place=False, out_dir=str(out))
        files = sorted(Path(str(f.get("path"))).name
                       for f in (report.get("files") or []) if isinstance(f, dict))
        check(files == exp["materialized_files"], f"{fx['id']}：物化文件 {exp['materialized_files']}", str(files))
        for rel in exp["materialized_files"]:
            text = (out / rel).read_text(encoding="utf-8")
            for needle in exp.get("content_contains") or []:
                check(needle in text, f"{fx['id']}：产物包含 {needle!r}")
            for needle in exp.get("content_not_contains") or []:
                check(needle not in text, f"{fx['id']}：产物不含旧内容 {needle!r}")
        bad_skips = [s for s in report.get("skipped") or []
                     if s.get("status") and not patches.is_benign_skip(s)]
        check(len(bad_skips) == exp["skipped_non_benign"],
              f"{fx['id']}：非良性跳过 {exp['skipped_non_benign']} 条", str(bad_skips))


def _replay_interface_freeze(fx: dict, inp: dict, exp: dict) -> None:
    frozen = O.freeze_symbols(inp["skeleton"], inp["plan_symbols"])
    fset = sorted((f["file"], f["symbol"]) for f in frozen)
    check(fset == sorted(tuple(x) for x in exp["frozen_symbols"]),
          f"{fx['id']}：冻结集合 = 骨架∩方案", str(fset))
    g = O.OntologyGraph()
    O.project_interface_freeze(g, frozen)
    O.evaluate_against_verify(
        g, inp["verify_report"],
        contract_problems=inp.get("contract_problems") or [],
        contract_checked=bool(inp.get("contract_checked")),
        skeleton_conformance=inp.get("skeleton_conformance"))
    # 漂移点名的是 Store.save：该符号 PO 必须 FAILED，且只挂骨架失败证据。
    target = next(p for p in g.obligations.values()
                  if p.kind == O.PO_KIND_INTERFACE_FREEZE
                  and p.verifier.get("symbol") == "Store.save")
    check(target.status == exp["po_status"], f"{fx['id']}：PO={exp['po_status']}", target.status)
    kinds = sorted({g.evidence[e].kind for e in target.evidence_ids})
    check(kinds == exp["po_evidence_kinds"], f"{fx['id']}：失败证据路 {exp['po_evidence_kinds']}", str(kinds))
    proof = O.release_proof_status(
        semantic_pass=True, verify_verdict=inp["verify_report"]["verdict"],
        obligations=list(g.obligations.values()), workspace_verified=True)
    check(proof["status"] == exp["proof_status"] and proof["code"] == exp["proof_code"]
          and proof["can_pass"] == exp["can_pass"],
          f"{fx['id']}：Proof Gate {exp['proof_status']}/{exp['proof_code']}",
          f"{proof['status']}/{proof['code']}")
    dec = _proof_decision(proof)
    check(dec["verdict"] == exp["decision"] and dec["action"] == exp["decision_action"],
          f"{fx['id']}：评审裁决 {exp['decision']}/{exp['decision_action']}",
          f"{dec['verdict']}/{dec['action']}")
    tax = DGN.failure_taxonomy(DGN.VERIFY_FAILED)
    check(tax["recover_stage"] == exp["recovery_stage"],
          f"{fx['id']}：recovery 阶段 {exp['recovery_stage']}", tax["recover_stage"])


def _replay_behavior_coverage(fx: dict, inp: dict, exp: dict) -> None:
    g = O.OntologyGraph()
    O.build_requirement_projection(g, inp["scope"], original_requirement="记账工具")
    O.evaluate_against_verify(g, inp["verify_report"])
    behaviors = [p for p in g.obligations.values() if p.kind in exp["unproven_po_kinds"]]
    check(bool(behaviors) and all(p.status == O.PO_STATUS_UNPROVEN for p in behaviors),
          f"{fx['id']}：只有 rc=0 时 behavior PO 全 UNPROVEN",
          str([(p.kind, p.status) for p in behaviors]))
    if exp.get("no_proven_behavior"):
        check(not any(p.kind == "behavior" and p.status == O.PO_STATUS_PROVEN
                     for p in g.obligations.values()),
              f"{fx['id']}：不存在被冒烟命令冒充 PROVEN 的 behavior PO")
    proof = O.release_proof_status(
        semantic_pass=True, verify_verdict=inp["verify_report"]["verdict"],
        obligations=list(g.obligations.values()), workspace_verified=True)
    check(proof["status"] == exp["proof_status"] and proof["code"] == exp["proof_code"]
          and proof["can_pass"] == exp["can_pass"],
          f"{fx['id']}：Proof Gate {exp['proof_status']}/{exp['proof_code']}",
          f"{proof['status']}/{proof['code']}")
    dec = _proof_decision(proof)
    check(dec["verdict"] == exp["decision"] and dec["action"] == exp["decision_action"],
          f"{fx['id']}：评审裁决 {exp['decision']}/{exp['decision_action']}",
          f"{dec['verdict']}/{dec['action']}")
    check(DGN.failure_taxonomy("proof_gate_unproven")["recover_stage"] == exp["recovery_stage"],
          f"{fx['id']}：recovery 阶段 {exp['recovery_stage']}")


def _replay_stale_defect(fx: dict, inp: dict, exp: dict) -> None:
    k1 = DGN.defect_key(inp["row_round_1"])
    k2 = DGN.defect_key(inp["row_round_2"])
    check((k1 == k2) == exp["defect_key_equal"],
          f"{fx['id']}：行号/措辞漂移后 defect_key 仍是同一条", f"{k1} vs {k2}")
    tax = DGN.failure_taxonomy(inp["reason_code"])
    check(tax["type"] == exp["failure_type"] and tax["owner_role"] == exp["owner_role"]
          and tax["recover_stage"] == exp["recovery_stage"],
          f"{fx['id']}：责任三元组 {exp['failure_type']}/{exp['owner_role']}/{exp['recovery_stage']}",
          str(tax))
    proof = O.release_proof_status(semantic_pass=True, verify_verdict="fail",
                                   obligations=[], workspace_verified=False)
    dec = _proof_decision(proof)
    check(dec["verdict"] == exp["decision"] and dec["action"] == exp["decision_action"],
          f"{fx['id']}：残留缺陷阻断 pass ⇒ {exp['decision']}/{exp['decision_action']}",
          f"{dec['verdict']}/{dec['action']}")


def _replay_plan_gap(fx: dict, inp: dict, exp: dict) -> None:
    r = inp["release"]
    proof = O.release_proof_status(
        semantic_pass=r["semantic_pass"], verify_verdict=r["verify_verdict"],
        workspace_verified=r["workspace_verified"], plan_gap=r["plan_gap"])
    check(proof["status"] == exp["proof_status"] and proof["code"] == exp["proof_code"]
          and proof["can_pass"] == exp["can_pass"],
          f"{fx['id']}：Proof Gate {exp['proof_status']}/{exp['proof_code']}",
          f"{proof['status']}/{proof['code']}")
    check(any(exp["reason_contains"] in m for m in proof["mandatory_missing"]),
          f"{fx['id']}：mandatory_missing 点名 {exp['reason_contains']}")
    rv = inp["review"]
    dec = DGN.review_decision(
        semantic_verdict=rv["semantic_verdict"], blocked=rv["blocked"],
        has_in_material=rv["has_in_material"], has_architect_fixes=rv["has_architect_fixes"],
        plan_gap=rv["plan_gap"])
    check(dec["verdict"] == exp["decision"] and dec["action"] == exp["decision_action"],
          f"{fx['id']}：评审回架构补规划 {exp['decision']}/{exp['decision_action']}",
          f"{dec['verdict']}/{dec['action']}")
    tax = DGN.failure_taxonomy(inp["reason_code"])
    check(tax["type"] == exp["failure_type"] and tax["owner_role"] == exp["owner_role"]
          and tax["recover_stage"] == exp["recovery_stage"],
          f"{fx['id']}：责任三元组 {exp['failure_type']}/{exp['owner_role']}/{exp['recovery_stage']}",
          str(tax))


def _replay_recompile_id(fx: dict, inp: dict, exp: dict) -> None:
    scope = inp.get("scope") or {}
    ir1 = P.normalize_plan(inp["plan_round_1"], scope=scope, original_requirement="记账工具")
    res1 = TC.compile_plan(inp["plan_round_1"], ir=ir1, existing_files=set())
    ir2 = P.normalize_plan(inp["plan_round_2"], scope=scope, original_requirement="记账工具")
    res2 = TC.compile_plan(inp["plan_round_2"], ir=ir2, existing_files=set(),
                           previous_tasks=res1["tasks"])
    sym = exp["symbol"]
    t1 = next(t for t in res1["tasks"] if sym in (t.get("symbols") or []))
    t2 = next(t for t in res2["tasks"] if sym in (t.get("symbols") or []))
    check((t1["id"] != t2["id"]) == exp["task_id_changes"],
          f"{fx['id']}：编号确实漂移（{t1['id']} → {t2['id']}）")
    check((t1["semantic_task_id"] == t2["semantic_task_id"]) == exp["semantic_id_stable"],
          f"{fx['id']}：semantic_task_id 跨编号稳定",
          f"{t1['semantic_task_id']} vs {t2['semantic_task_id']}")
    check(t2.get("task_revision") == exp["revision_round_2"],
          f"{fx['id']}：再编译 revision={exp['revision_round_2']}", str(t2.get("task_revision")))
    check((t1["id"] in (t2.get("supersedes") or [])) == exp["supersedes_contains_old_id"],
          f"{fx['id']}：supersedes 指向上版 {t1['id']}", str(t2.get("supersedes")))


DISPATCH = {
    "patches_apply": _replay_patches_apply,
    "interface_freeze": _replay_interface_freeze,
    "behavior_coverage": _replay_behavior_coverage,
    "stale_defect": _replay_stale_defect,
    "plan_gap": _replay_plan_gap,
    "recompile_id": _replay_recompile_id,
}


def main() -> int:
    fixture_dir = ROOT / "tools" / "_repro"
    paths = sorted(fixture_dir.glob("fixture_*.json"))
    replayed = 0
    for path in paths:
        fx = json.loads(path.read_text(encoding="utf-8"))
        scenario = fx.get("scenario")
        if scenario not in DISPATCH:
            # 001 无 scenario：其裁决回放归在 smoke_ontology F 段。
            continue
        replayed += 1
        print(f"== {fx['id']} ==")
        DISPATCH[scenario](fx, fx["input"], fx["expect"])
    print()
    print(f"回放 {replayed} 个 fixture；通过 {PASS} 项，失败 {FAIL} 项")
    if FAIL:
        print("Fixture replay 未通过")
        return 1
    print("Fixture replay 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
