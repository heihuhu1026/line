"""方案§三十六：唯一放行闭环 Ontology 端到端冒烟（纯内核，不调 LLM、不依赖仓库）。

正例：Requirement → ProofObligation → TestCompiler Scenario → 真实执行 Evidence
      → required PO 全 PROVEN → can_release pass → Decision based_on 证据 PASS，
      全量语义校验零 error。
反例：PO-A 有证据 PROVEN、PO-B 场景缺失（UNPROVEN）⇒ can_release 不放行、
      Decision=rework。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import ontology as O  # noqa: E402
from pipeline import ontology_validate as OV  # noqa: E402
from pipeline import testcompiler as TC  # noqa: E402

PASS = FAIL = 0


def check(cond: bool, name: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  <- {extra}" if extra else ""))


def _seed_graph(po_ids: list[str]) -> O.OntologyGraph:
    """Requirement → required behavior PO（obligation_for 锚定）。"""
    g = O.OntologyGraph()
    g.add(O.SemanticObject(
        id="req:1", type=O.TYPE_REQUIREMENT, truth=O.TRUTH_ASSERTED,
        payload={"text": "命令行记账：记账与余额行为正确"},
        provenance=[O.Provenance(source="user", stage="intake")],
    ))
    for pid in po_ids:
        g.add_obligation(O.ProofObligation(
            id=pid, name=f"行为义务 {pid}", claim=f"claim-text-{pid}",
            requirement_id="req:1", kind=O.PO_KIND_BEHAVIOR, required=True,
            verifier={"kind": "command", "command": "python test_app.py"},
        ))
        g.relate(pid, "obligation_for", "req:1")
    return g


def _scenario_commands(po_ids: list[str]) -> list[dict]:
    """PO → TestCompiler 场景（带行为断言的可信命令）→ verify 报告命令行。"""
    pos = [{"id": pid, "kind": O.PO_KIND_BEHAVIOR, "required": True,
            "claim": f"行为 {pid}"} for pid in po_ids]
    compiled = TC.compile_scenarios(
        obligations=pos,
        files=["main.py", "test_app.py"],
        planned_commands=[
            {"command": f"python test_app.py --case {pid}", "source": "testcompiler:scenario",
             "target_po": pid, "assertions": ["stdout_contains:OK", "exit_code==0"]}
            for pid in po_ids
        ],
    )
    commands = []
    for scen in compiled["scenarios"]:
        for cmd in scen.get("automated_commands") or []:
            commands.append({
                "command": cmd, "status": "ok", "exit_code": 0,
                "stdout_tail": "OK", "stderr_tail": "",
                "target_po_ids": [scen["target_po"]],
                "assertions": scen.get("assertions") or ["exit_code==0"],
            })
    return commands


def _run_loop(po_ids: list[str], *, evidenced: set[str]) -> tuple[O.OntologyGraph, dict, dict]:
    g = _seed_graph(po_ids)
    commands = [c for c in _scenario_commands(sorted(evidenced))]
    O.evaluate_against_verify(
        g, {"verdict": "pass", "commands": commands},
        parent_revision="",
    )
    proof = O.release_proof_status(
        semantic_pass=True, verify_verdict="pass",
        obligations=list(g.obligations.values()), workspace_verified=True)
    basis = O.release_basis(g)
    gate = O.can_release(proof_status=proof, review_verdict="pass", graph=g)
    if gate["can_pass"]:
        decision_id = O.project_decision(
            g, verdict="pass", reason="闭环正例：required PO 全部机械证明",
            revision=basis["revision"], evidence_ids=basis["evidence_ids"], round_no=1)
    else:
        decision_id = O.project_decision(
            g, verdict=gate["verdict"],
            reason="；".join(gate["blocking_reasons"][:3]), round_no=1)
    return g, gate, {"decision_id": decision_id, "basis": basis, "proof": proof}


def main() -> int:
    # ---- 正例：完整闭环 ----
    print("== 正例：Requirement→PO→Scenario→Evidence→PO PROVEN→Decision PASS ==")
    g_pos, gate_pos, extra_pos = _run_loop({"po:A", "po:B"}, evidenced={"po:A", "po:B"})
    check(all(g_pos.obligations[p].status == O.PO_STATUS_PROVEN for p in ("po:A", "po:B")),
          "两个 required PO 均由绑定证据 PROVEN",
          str({p: g_pos.obligations[p].status for p in ("po:A", "po:B")}))
    check(extra_pos["proof"]["status"] == "PROVEN" and gate_pos["can_pass"],
          "Proof Gate PROVEN 且 can_release 放行", str(gate_pos))
    triples = {(r.subject, r.predicate, r.object) for r in g_pos.relations}
    did = extra_pos["decision_id"]
    check(any(s == did and p == "based_on" for s, p, _ in triples)
          and (did, "applies_to", gate_pos["verified_revision"]) in triples,
          "Decision based_on 证据且 applies_to 经验证链头")
    problems = OV.validate_all_structured(g_pos)
    problems["semantic_integrity"] = OV.semantic_integrity_audit(g_pos)
    check(OV.blocking_errors(problems) == [],
          "全量语义校验（含 semantic_integrity）零 error",
          str([p.code for p in OV.blocking_errors(problems)]))
    decision_obj = g_pos.objects[did]
    check(decision_obj.payload["verdict"] == "pass", "图上 Decision=pass")

    # ---- 反例：PO-B 缺证据 ----
    print("== 反例：PO-A passed / PO-B missing ⇒ Decision NOT PASS ==")
    g_neg, gate_neg, extra_neg = _run_loop({"po:A", "po:B"}, evidenced={"po:A"})
    check(g_neg.obligations["po:A"].status == O.PO_STATUS_PROVEN
          and g_neg.obligations["po:B"].status == O.PO_STATUS_UNPROVEN,
          "PO-A PROVEN、PO-B UNPROVEN",
          f"{g_neg.obligations['po:A'].status}/{g_neg.obligations['po:B'].status}")
    check(extra_neg["proof"]["status"] == "UNPROVEN" and not gate_neg["can_pass"]
          and gate_neg["verdict"] == "rework",
          "Proof Gate UNPROVEN、can_release 不放行", str(gate_neg["blocking_reasons"]))
    check(any("po:B" in r for r in gate_neg["blocking_reasons"]),
          "blocking_reasons 点名缺失的 PO-B", str(gate_neg["blocking_reasons"]))
    check(g_neg.objects[extra_neg["decision_id"]].payload["verdict"] == "rework",
          "图上 Decision=rework（NOT PASS）")

    print()
    print(f"闭环冒烟：通过 {PASS} 项，失败 {FAIL} 项")
    if FAIL:
        print("Ontology closed-loop smoke 未通过")
        return 1
    print("Ontology closed-loop smoke 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
