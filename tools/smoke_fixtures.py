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
from pipeline import ontology_validate as OV  # noqa: E402
from pipeline import patches  # noqa: E402
from pipeline import planir as P  # noqa: E402
from pipeline import taskcompiler as TC  # noqa: E402
from pipeline import testcompiler as TSC  # noqa: E402

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


# ----------------------------------------------------------------- 方案§三十七 新增 8 fixture

def _graph_with_required(inp: dict) -> O.OntologyGraph:
    """建 Requirement + required PO（行为类）底图。"""
    g = O.OntologyGraph()
    g.add(O.SemanticObject(
        id="req:1", type=O.TYPE_REQUIREMENT, truth=O.TRUTH_ASSERTED,
        payload={"text": str(inp.get("requirement") or "需求")},
        provenance=[O.Provenance(source="user", stage="intake")],
    ))
    for raw in inp.get("obligations") or []:
        g.add_obligation(O.ProofObligation(
            id=raw["id"], name=str(raw.get("claim") or raw["id"]),
            claim=str(raw.get("claim") or raw["id"]),
            requirement_id="req:1", kind=str(raw.get("kind") or O.PO_KIND_BEHAVIOR),
            required=bool(raw.get("required", True)),
            verifier={"kind": "command", "command": "python -m pytest"},
        ))
    return g


def _replay_proof_binding(fx: dict, inp: dict, exp: dict) -> None:
    g = _graph_with_required(inp)
    O.evaluate_against_verify(g, inp["verify_report"])
    proven = g.obligations[exp["proven_po"]]
    unproven = g.obligations[exp["unproven_po"]]
    check(proven.status == O.PO_STATUS_PROVEN, f"{fx['id']}：显式绑定的 PO PROVEN", proven.status)
    check(unproven.status == O.PO_STATUS_UNPROVEN,
          f"{fx['id']}：未绑定证据的 required PO 保持 UNPROVEN（禁止串证）", unproven.status)
    proof = O.release_proof_status(
        semantic_pass=True, verify_verdict=inp["verify_report"]["verdict"],
        obligations=list(g.obligations.values()), workspace_verified=True)
    check(proof["status"] == exp["proof_status"] and proof["code"] == exp["proof_code"]
          and proof["can_pass"] == exp["can_pass"],
          f"{fx['id']}：Proof Gate {exp['proof_status']}/{exp['proof_code']}",
          f"{proof['status']}/{proof['code']}")
    check(any(exp["unproven_po"] in m for m in proof["mandatory_missing"]),
          f"{fx['id']}：mandatory_missing 点名 {exp['unproven_po']}")


def _replay_testcompiler_runtime(fx: dict, inp: dict, exp: dict) -> None:
    res = TSC.compile_scenarios(obligations=inp["obligations"], files=inp["files"])
    by_target = {s["target_po"]: s for s in res["scenarios"]}
    scen = by_target.get(exp["executable_target"])
    check(scen is not None and scen["status"] == TSC.STATUS_EXECUTABLE
          and any(exp["executable_command_contains"] in str(c)
                  for c in scen.get("automated_commands") or []),
          f"{fx['id']}：{exp['executable_target']} 机械编译出可执行场景",
          str(scen))
    check(exp["coverage_gap_contains"] in (res.get("coverage_gap") or []),
          f"{fx['id']}：无机械命令的 PO 落 coverage_gap {exp['coverage_gap_contains']}",
          str(res.get("coverage_gap")))


def _replay_invalid_ontology(fx: dict, inp: dict, exp: dict) -> None:
    g = O.OntologyGraph()
    g.add(O.SemanticObject(
        id="req:1", type=O.TYPE_REQUIREMENT, truth=O.TRUTH_ASSERTED,
        provenance=[O.Provenance(source="user", stage="intake")]))
    g.add(O.SemanticObject(
        id=inp["subject"], type=O.TYPE_TASK, truth=O.TRUTH_DERIVED,
        provenance=[O.Provenance(source="taskcompiler", stage="compile_plan")]))
    # 脏数据（旧产物反序列化）：relate 正常路径会拒绝，直接 append 模拟。
    g.relations.append(O.OntologyRelation(
        id="rel:bad", subject=inp["subject"], predicate=inp["predicate"],
        object=inp["object"]))
    errors = OV.blocking_errors(OV.validate_all_structured(g))
    codes = {p.code for p in errors}
    check(exp["error_code"] in codes, f"{fx['id']}：硬阻断 {exp['error_code']}", str(codes))


def _replay_workspace_chain(fx: dict, inp: dict, exp: dict) -> None:
    g = O.OntologyGraph()
    g.add(O.SemanticObject(
        id="req:1", type=O.TYPE_REQUIREMENT, truth=O.TRUTH_ASSERTED,
        provenance=[O.Provenance(source="user", stage="intake")]))
    tid = inp["task_id"]
    g.add(O.SemanticObject(
        id=tid, type=O.TYPE_TASK, truth=O.TRUTH_DERIVED,
        payload={"target_files": [inp["file"]], "symbols": []},
        provenance=[O.Provenance(source="taskcompiler", stage="compile_plan")]))
    g.relate(tid, "implements", "req:1")

    def _txn(seq: int, parent: str) -> dict:
        return {
            "round": 1, "task": f"T{seq}", "task_semantic_id": tid,
            "base_manifest": inp["base"] if seq == 1 else "",
            "parent_workspace_revision": parent,
            "result_workspace_revision": f"wsr:wip:r{seq:08d}",
            "patches": [{"path": inp["file"], "symbol": inp["symbol"],
                         "change_type": "add", "patch_mode": "new_file"}],
            "status": "committed",
        }

    t1 = _txn(1, "")
    t2 = _txn(2, t1["result_workspace_revision"])
    count = O.project_workspace_chain(g, [t1, t2], at="1")
    check((count["revisions"], count["patches"], count["symbols"])
          == (exp["result_revisions"], exp["patches"], exp["symbols"]),
          f"{fx['id']}：链投影计数 {exp['result_revisions']}/{exp['patches']}/{exp['symbols']}",
          str(count))
    check(exp["base_revision"] in g.revisions
          and g.revisions[t1["result_workspace_revision"]].parent_revision == exp["base_revision"],
          f"{fx['id']}：r1 挂在 source=base 的根 revision 之下")
    check(g.revisions[t2["result_workspace_revision"]].parent_revision
          == t1["result_workspace_revision"], f"{fx['id']}：r2.parent = r1")
    check(g.head_revision() == t2["result_workspace_revision"], f"{fx['id']}：链头 = r2")
    state = {"task_transactions": [t1, t2]}
    audit_errors = [p for p in OV.semantic_integrity_audit(g, state) if p.severity == "error"]
    check(len(audit_errors) == exp["alignment_errors"],
          f"{fx['id']}：事务对齐零 error", str([p.code for p in audit_errors]))


def _replay_stale_evidence(fx: dict, inp: dict, exp: dict) -> None:
    g = O.OntologyGraph()
    g.add_revision(O.WorkspaceRevision(
        revision_id=inp["old_revision"], workspace_id="ws", source="verify",
        status="VERIFIED"))
    g.add_revision(O.WorkspaceRevision(
        revision_id=inp["new_revision"], workspace_id="ws", source="verify",
        status="VERIFIED"))
    g.add_evidence(O.EvidenceRecord(
        id="ev:old", kind="test_result", source="python test_x.py",
        status=O.PO_STATUS_PROVEN, truth=O.TRUTH_PROVEN,
        workspace_revision=inp["old_revision"],
        command="python test_x.py", exit_code=0))
    codes = {p.code for p in OV.blocking_errors(OV.validate_all_structured(g))}
    check(exp["error_code"] in codes, f"{fx['id']}：{exp['error_code']} 硬阻断", str(codes))


def _replay_verify_skipped(fx: dict, inp: dict, exp: dict) -> None:
    g = _graph_with_required(inp)
    proof = O.release_proof_status(
        semantic_pass=bool(inp["semantic_pass"]),
        verify_verdict=inp["verify_verdict"],
        obligations=list(g.obligations.values()),
        workspace_verified=bool(inp["workspace_verified"]))
    check(proof["status"] == exp["proof_status"] and proof["code"] == exp["proof_code"],
          f"{fx['id']}：Proof Gate {exp['proof_status']}/{exp['proof_code']}",
          f"{proof['status']}/{proof['code']}")
    check(any(exp["blocking_reason_contains"] in m for m in proof["mandatory_missing"]),
          f"{fx['id']}：mandatory_missing 点名 {exp['blocking_reason_contains']}",
          str(proof["mandatory_missing"]))
    dec = DGN.review_decision(
        semantic_verdict="pass", blocked=False, has_in_material=False,
        has_architect_fixes=False, verify_pass=False, evidence_clean=True, proof=proof)
    check(dec["verdict"] == exp["decision"], f"{fx['id']}：裁决 != pass（{exp['decision']}）",
          f"{dec['verdict']}/{dec['action']}")
    gate = O.can_release(proof_status=proof, review_verdict="pass", graph=g)
    check(gate["can_pass"] == exp["can_release"] and gate["verdict"] == "rework",
          f"{fx['id']}：can_release 不可放行", str(gate["blocking_reasons"]))


def _replay_selfcheck_not_proven(fx: dict, inp: dict, exp: dict) -> None:
    g = O.OntologyGraph()
    g.add(O.SemanticObject(
        id=inp["object_id"], type=O.TYPE_CLAIM, truth=O.TRUTH_PROVEN,
        payload={"text": "开发自陈：功能已完成"},
        provenance=[O.Provenance(source=inp["source"], stage="dev")]))
    codes = {p.code for p in OV.blocking_errors(OV.validate_all_structured(g))}
    check(exp["error_code"] in codes, f"{fx['id']}：{exp['error_code']} 硬阻断", str(codes))


def _replay_decision_without_evidence(fx: dict, inp: dict, exp: dict) -> None:
    g = O.OntologyGraph()
    g.add(O.SemanticObject(
        id=inp["decision_id"], type=O.TYPE_DECISION, truth=O.TRUTH_DERIVED,
        payload={"verdict": "pass", "reason": "无证据裁决（反例）", "revision": ""},
        provenance=[O.Provenance(source="release_gate", stage="review")]))
    codes = {p.code for p in OV.semantic_integrity_audit(g)}
    check(exp["error_code"] in codes, f"{fx['id']}：{exp['error_code']} 硬阻断", str(codes))
    gate = O.can_release(proof_status=None, review_verdict="pass")
    check(not gate["can_pass"], f"{fx['id']}：空证明 can_release 不可放行")


DISPATCH = {
    "patches_apply": _replay_patches_apply,
    "interface_freeze": _replay_interface_freeze,
    "behavior_coverage": _replay_behavior_coverage,
    "stale_defect": _replay_stale_defect,
    "plan_gap": _replay_plan_gap,
    "recompile_id": _replay_recompile_id,
    "proof_binding": _replay_proof_binding,
    "testcompiler_runtime": _replay_testcompiler_runtime,
    "invalid_ontology": _replay_invalid_ontology,
    "workspace_chain": _replay_workspace_chain,
    "stale_evidence": _replay_stale_evidence,
    "verify_skipped": _replay_verify_skipped,
    "selfcheck_not_proven": _replay_selfcheck_not_proven,
    "decision_without_evidence": _replay_decision_without_evidence,
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
