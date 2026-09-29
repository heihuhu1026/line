"""Ontology Kernel 离线冒烟（改造规格§四十八场景 A–J + §五十 fixture replay）。

随 P0-2/P0-3 分阶段补齐：本轮（P0-1/P0-2）覆盖 A/B/C(身份稳定)/D/E/F/G/H/I/J 的
**内核与机械闸门**语义；resolve_symbol_to_file 的歧义解析在 P0-3 接入后补 C 的后半。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import ontology as O  # noqa: E402
from pipeline import ontology_validate as OV  # noqa: E402
from pipeline import verify as V  # noqa: E402
from pipeline.config import VERIFY_ALLOWED_BINS, VERIFY_DENY_PATTERNS  # noqa: E402

PASS = FAIL = 0


def check(cond: bool, name: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  <- {extra}" if extra else ""))


def _pm_scope() -> dict:
    return {
        "functional_requirements": [
            {"id": "FR-01", "description": "运行记账命令后在 SQLite 建表并写入一条流水",
             "acceptance": ["参数缺失时退出码为 2 并打印用法"]},
            {"id": "FR-02", "description": "支持按月汇总支出金额"},
        ],
        "acceptance_criteria": ["命令执行成功退出码为 0"],
    }


def main() -> int:
    # ---- A：Requirement → Claim → ProofObligation 投影 ----
    print("== A. Requirement → Claim → ProofObligation（可追溯验证义务）==")
    g = O.OntologyGraph()
    O.build_requirement_projection(g, _pm_scope(), original_requirement="做一个命令行记账工具")
    reqs = [o for o in g.objects.values() if o.type == O.TYPE_REQUIREMENT]
    claims = [o for o in g.objects.values() if o.type == O.TYPE_CLAIM]
    pos = list(g.obligations.values())
    check(len(reqs) >= 3, f"原始需求 + PM 的 FR 都成为 Requirement（{len(reqs)} 个）", str(len(reqs)))
    check(g.get("req:root") and g.get("req:root").truth == O.TRUTH_ASSERTED,
          "原始用户需求是 ASSERTED（不可被 LLM 覆盖）")
    check(all(o.truth == O.TRUTH_DERIVED for o in reqs if o.id != "req:root"),
          "PM 推导的 FR 是 DERIVED")
    check(len(claims) >= 4 and len(pos) >= 5,
          f"每条 FR/验收口径都有 Claim + PO（claims={len(claims)}, pos={len(pos)}）")
    check(all(p.required and p.verifier for p in pos),
          "所有 PO required 且绑定可执行 verifier（不只写人话）")
    kinds = {p.kind for p in pos}
    check("materialization" in kinds, f"“建表”文本确定性映射到 materialization PO（{kinds}）")
    pred = {r.predicate for r in g.relations}
    check({"claim_derives", "claim_satisfies", "obligation_for"} <= pred,
          "Requirement→Claim→PO 关系链完整")
    check(not OV.has_blocking(OV.validate_all(g)),
          "投影出的干净图通过全部校验器", json.dumps(OV.validate_all(g), ensure_ascii=False))

    # 投影幂等：同一 scope 再投一次不产生重复对象。
    before = len(g.objects)
    O.build_requirement_projection(g, _pm_scope(), original_requirement="做一个命令行记账工具")
    check(len(g.objects) == before, "投影幂等（同输入不重复造对象）", f"{before} -> {len(g.objects)}")

    # ---- B：Task 没有 Requirement ⇒ 失败 ----
    print("== B. Task 无 Requirement 锚点必须被拦截 ==")
    g2 = O.OntologyGraph()
    t = O.SemanticObject(id="stask:x", type=O.TYPE_TASK, payload={"target_files": ["a.py"]})
    g2.add(t)
    problems = OV.validate_obligations(g2)
    check(any("没有 implements 任何 Requirement" in p for p in problems),
          "裸 Task（不 implements Requirement）被拦截")
    g2.add(O.SemanticObject(id="req:1", type=O.TYPE_REQUIREMENT,
                           truth=O.TRUTH_ASSERTED, provenance=[O.Provenance(source="user")]))
    g2.relate("stask:x", "implements", "req:1")
    check(not OV.validate_obligations(g2), "挂上 implements Requirement 后通过")

    # ---- C：semantic_task_id 稳定性（resolve_symbol_to_file 歧义解析在 P0-3 补）----
    print("== C. 语义任务身份：与 T-xx 编号无关、跨重编译稳定 ==")
    sid1 = O.semantic_task_id(["db.py"], ["init_db"], ["facet:create_table"])
    sid2 = O.semantic_task_id(["db.py"], ["init_db"], ["facet:create_table"])
    sid3 = O.semantic_task_id(["db.py"], ["insert_record"], ["facet:create_table"])
    check(sid1 == sid2 and sid1.startswith("stask:"), "同语义输入 ⇒ 同 ID（确定性）")
    check(sid1 != sid3, "符号/facet 变了 ⇒ ID 变（语义身份区分）")
    check(O.semantic_task_id(["db\\py"], [], []) == O.semantic_task_id(["db/py"], [], []),
          "路径分隔符归一（Windows \\ 与 / 同身份）")

    # ---- D：Patch → WorkspaceRevision 物化链 ----
    print("== D. Patch → WorkspaceRevision 物化链 ==")
    gd = O.OntologyGraph()
    gd.add_revision(O.WorkspaceRevision(
        revision_id="wsr:1", workspace_id="ws", source="base"))
    gd.add_revision(O.WorkspaceRevision(
        revision_id="wsr:2", workspace_id="ws", parent_revision="wsr:1",
        source="task", task_id="stask:x", patch_digest="h1", manifest_digest="m1"))
    gd.add(O.SemanticObject(id="ws", type=O.TYPE_WORKSPACE))
    gd.relate("ws", "has_revision", "wsr:1")
    gd.relate("ws", "has_revision", "wsr:2")
    gd.add(O.SemanticObject(id="patch:1", type=O.TYPE_PATCH,
                           payload={"path": "db.py", "task_id": "stask:x", "change_type": "add"}))
    gd.relate("patch:1", "materialized_in", "wsr:2")
    check(gd.head_revision() == "wsr:2", "parent 链 head = wsr:2")
    check(not [p for k, v in OV.validate_all(gd, changes=["db.py"]).items() for p in v],
          "物化链通过全部校验", json.dumps(OV.validate_all(gd, changes=["db.py"]), ensure_ascii=False))
    gd.relate("patch:1", "materialized_in", "wsr:missing")
    check(any("materialized_in" in p for p in OV.validate_revisions(gd)),
          "materialized_in 悬空 revision 被拦截")

    # ---- E：verify 真跑成功 → 证据 PROVEN → PO PROVEN → release PROVEN ----
    print("== E. 真实 verify 成功 = PROVEN（机械证据闭环）==")
    ge = O.OntologyGraph()
    ge.add_revision(O.WorkspaceRevision(revision_id="wsr:1", workspace_id="ws", source="verify",
                                        status="VERIFIED", verification_digest="v1"))
    ev = O.EvidenceRecord(
        id="ev:1", kind="command_result", source="python main.py", status=O.PO_STATUS_PROVEN,
        truth=O.TRUTH_PROVEN, workspace_revision="wsr:1", command="python main.py",
        exit_code=0, proof_obligation_ids=["po:1"])
    ge.add_evidence(ev)
    ge.add_obligation(O.ProofObligation(
        id="po:1", name="n", requirement_id="", claim="c", kind=O.PO_KIND_BEHAVIOR,
        verifier={"type": "command_assert"},
        status=O.PO_STATUS_PROVEN, evidence_ids=["ev:1"]))
    check(not OV.validate_evidence(ge) and not OV.validate_obligations(ge),
          "带 command+exit_code 的执行证据支撑 PO=PROVEN，校验通过")
    rel = O.release_proof_status(
        semantic_pass=True, verify_verdict="pass", obligations=list(ge.obligations.values()),
        workspace_verified=True)
    check(rel["status"] == "PROVEN" and rel["can_pass"],
          "verify pass + PO PROVEN + 工作区 VERIFIED ⇒ PROVEN 可放行", rel["status"])

    # ---- F：verify skipped + 语义 pass ⇒ 绝对不能 PASS（121404 fixture）----
    print("== F. verify skipped 即使语义评审 pass，也不是 PASS（核心铁律）==")
    fx = json.loads((ROOT / "tools" / "_repro" / "fixture_001_pass_without_verify.json")
                    .read_text(encoding="utf-8"))
    inp = fx["input"]
    got = O.release_proof_status(
        semantic_pass=inp["semantic_pass"],
        mechanical_blockers=inp["mechanical_blockers"],
        verify_verdict=inp["verify_verdict"],
        obligations=inp["obligations"],
        workspace_verified=inp["workspace_verified"],
        negative_control_no_power=inp["negative_control_no_power"],
        unresolved_contracts=inp["unresolved_contracts"],
        plan_gap=inp["plan_gap"],
    )
    exp = fx["expect"]
    check(got["status"] == exp["status"], f"fixture 状态 {exp['status']}", got["status"])
    check(got["can_pass"] is False, "can_pass=False（语义 pass 也救不了 skipped）")
    check(got["code"] == exp["code"], f"失败码 {exp['code']}", got["code"])
    check(any(exp["reason_contains"] in m for m in got["mandatory_missing"]),
          "mandatory_missing 点名 verify_skipped")
    # 同事实写进图：Decision=pass 必须被 release 校验器拒绝。
    gf = O.OntologyGraph.from_dict(ge.to_dict())  # 复用 E 的图，再把状态拨回 skipped
    gf.obligations["po:1"].status = O.PO_STATUS_UNPROVEN
    gf.obligations["po:1"].evidence_ids = []
    gf.add(O.SemanticObject(id="dec:1", type=O.TYPE_DECISION, status="pass",
                           payload={"verdict": "pass"}))
    check(any("required PO 未 PROVEN" in p for p in OV.validate_release(gf)),
          "图上 Decision=pass + required PO 未 PROVEN 被拦截")
    # verify fail 是 FAILED 而不是 UNPROVEN（失败 ≠ 缺证）。
    fail = O.release_proof_status(semantic_pass=True, verify_verdict="fail",
                                  obligations=[], workspace_verified=True)
    check(fail["status"] == "FAILED" and fail["code"] == "required_proof_failed",
          "verify fail ⇒ FAILED（不是 UNPROVEN）", fail["status"])

    # diagnose.review_decision 终局闸门：语义 pass、PASS_EXTERNAL、PASS_RESIDUAL
    # 三条历史漏成 pass 的路径，在机械证据不成立时都必须翻回 rework_dev。
    from pipeline import diagnose as D  # noqa: E402
    unproven = O.release_proof_status(semantic_pass=True, verify_verdict="skipped",
                                      obligations=inp["obligations"],
                                      workspace_verified=False)
    d1 = D.review_decision(semantic_verdict="pass", blocked=False,
                           has_in_material=False, has_architect_fixes=False, proof=unproven)
    check(d1["verdict"] == "rework_dev" and d1["action"] == D.DECISION_PROOF_UNPROVEN,
          "语义 pass + verify skipped ⇒ 翻回 rework_dev（proof_gate_unproven）",
          f"{d1['verdict']}/{d1['action']}")
    # PASS_EXTERNAL 路径（语义 rework_dev + 无任何材料）以前会强制 pass。
    d2 = D.review_decision(semantic_verdict="rework_dev", blocked=False,
                           has_in_material=False, has_architect_fixes=False, proof=unproven)
    check(d2["verdict"] != "pass",
          "PASS_EXTERNAL 漏点被堵：问题全在范围外但 verify skipped ⇒ 不放行", d2["verdict"])
    # PASS_RESIDUAL 路径（verify_pass=True 不可能与 skipped 同时出现；构造 fail 证据）
    failed = O.release_proof_status(semantic_pass=True, verify_verdict="fail",
                                    obligations=[], workspace_verified=False)
    d3 = D.review_decision(semantic_verdict="pass", blocked=False,
                           has_in_material=True, has_architect_fixes=False, proof=failed)
    check(d3["verdict"] == "rework_dev" and d3["action"] == D.DECISION_PROOF_FAILED,
          "必需证据 FAILED ⇒ proof_gate_failed，不允许 pass", f"{d3['verdict']}/{d3['action']}")
    # 不传 proof（mock 路径）⇒ 旧行为逐分支不变。
    d4 = D.review_decision(semantic_verdict="rework_dev", blocked=False,
                           has_in_material=False, has_architect_fixes=False)
    check(d4["action"] == D.DECISION_PASS_EXTERNAL and d4["verdict"] == "pass",
          "无 proof 入参（mock 通道）保持历史行为不变", d4["action"])
    proven = O.release_proof_status(semantic_pass=True, verify_verdict="pass",
                                    obligations=[], workspace_verified=True)
    d5 = D.review_decision(semantic_verdict="pass", blocked=False,
                           has_in_material=False, has_architect_fixes=False,
                           evidence_clean=True, verify_pass=True, proof=proven)
    check(d5["verdict"] == "pass", "证据全 PROVEN ⇒ pass 不被拦截", d5["verdict"])

    # ---- G：self_check / 人工确认 的真值纪律 ----
    print("== G. self_check 只能 DERIVED；human_confirmation 不是 PROVEN ==")
    gg = O.OntologyGraph()
    gg.add(O.SemanticObject(id="claim:sc", type=O.TYPE_CLAIM, truth=O.TRUTH_PROVEN,
                           provenance=[O.Provenance(source="self_check", stage="dev")]))
    check(any("self_check" in p for p in OV.validate_objects(gg)),
          "dev self_check 标 PROVEN 被拦截")
    evh = O.EvidenceRecord(id="ev:h", kind="human_confirmation", source="human@review",
                           status=O.PO_STATUS_PROVEN, truth=O.TRUTH_PROVEN)
    gg.add_evidence(evh)
    check(any("human_confirmation" in p for p in OV.validate_evidence(gg)),
          "human_confirmation 冒充 PROVEN 被拦截（人工事实是 ASSERTED）")
    evx = O.EvidenceRecord(id="ev:x", kind="command_result", source="python t.py",
                           status=O.PO_STATUS_PROVEN, truth=O.TRUTH_PROVEN,
                           workspace_revision="wsr:1", command="python t.py")
    gg.add_evidence(evx)
    check(any("exit_code" in p for p in OV.validate_evidence(gg)),
          "PROVEN 执行证据缺 exit_code 被拦截（防止口头说跑过）")

    # ---- H：Defect → PO/Invariant → Failure → Recovery owner ----
    print("== H. Defect/Failure/Recovery 责任链 ==")
    gh = O.OntologyGraph()
    gh.add(O.SemanticObject(id="req:1", type=O.TYPE_REQUIREMENT, truth=O.TRUTH_ASSERTED,
                           provenance=[O.Provenance(source="user")]))
    gh.add_obligation(O.ProofObligation(id="po:1", name="n", requirement_id="req:1",
                                        claim="c", status=O.PO_STATUS_FAILED))
    gh.add(O.SemanticObject(id="defect:1", type=O.TYPE_DEFECT, payload={"check_id": "verify"}))
    gh.add(O.SemanticObject(id="task:1", type=O.TYPE_TASK))
    gh.relate("task:1", "implements", "req:1")
    gh.relate("defect:1", "violates", "po:1")
    gh.relate("defect:1", "affects_task", "task:1")
    check(not [p for p in OV.validate_relations(gh)],
          "Defect violates PO / affects Task 关系合法")

    # ---- I：stale evidence 不能证明新工作区 ----
    print("== I. stale evidence 不能证明新 workspace revision ==")
    gi = O.OntologyGraph()
    gi.add_revision(O.WorkspaceRevision(revision_id="wsr:1", workspace_id="ws", source="verify"))
    gi.add_revision(O.WorkspaceRevision(revision_id="wsr:2", workspace_id="ws",
                                        parent_revision="wsr:1", source="task"))
    old = O.EvidenceRecord(id="ev:old", kind="command_result", source="python t.py",
                           status=O.PO_STATUS_PROVEN, truth=O.TRUTH_PROVEN,
                           workspace_revision="wsr:1", command="python t.py", exit_code=0)
    gi.add_evidence(old)
    check(any("stale evidence" in p for p in OV.validate_evidence(gi)),
          "新 head=wsr:2 时，绑在 wsr:1 的 PROVEN 证据被判 stale")

    # ---- J：recompile 重编号后语义 ID 不变，defect 不断链 ----
    print("== J. 架构师 rework 重编号 ⇒ 语义身份稳定、Defect 链不断 ==")
    id_round1 = O.semantic_task_id(["db.py"], ["init_db"], ["facet:create"])
    id_round2 = O.semantic_task_id(["db.py"], ["init_db"], ["facet:create"])
    check(id_round1 == id_round2, "两轮 compile 的同语义任务 ID 一致（不随 T-02/T-03 漂移）")
    gj = O.OntologyGraph()
    gj.add(O.SemanticObject(id=id_round1, type=O.TYPE_TASK))
    gj.add(O.SemanticObject(id="req:1", type=O.TYPE_REQUIREMENT, truth=O.TRUTH_ASSERTED,
                           provenance=[O.Provenance(source="user")]))
    gj.relate(id_round1, "implements", "req:1")
    gj.add(O.SemanticObject(id="defect:1", type=O.TYPE_DEFECT))
    gj.relate("defect:1", "affects_task", id_round1)
    check(not OV.validate_relations(gj), "Defect 用语义 ID 挂任务，重编号后链仍然有效")

    # ---- L：new 项目无 repo ⇒ verify/base 空基线物化 + 真跑（端到端，临时目录）----
    print("== L. new 项目空基线 verify 真跑（规格§二十 P0-2）==")
    import tempfile
    td = Path(tempfile.mkdtemp(prefix="ont_verify_"))
    main_py = (
        "def main():\n"
        "    print('ledger ok')\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )
    test_py = (
        "import unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertEqual(1, 1)\n"
    )
    py = f'"{sys.executable}"'
    impl_new = {
        "run": f"{py} main.py",
        "edits": [
            {"change_type": "add", "path": "main.py", "patch": main_py},
            {"change_type": "add", "path": "test_main.py", "patch": test_py},
        ],
    }
    test_report = {"automated_commands": [
        {"command": f"{py} -m unittest test_main"}]}
    rep_new = V.verify(
        td, None, impl_new, {"source_available": False}, test_report,
        allowed_bins=VERIFY_ALLOWED_BINS, deny_patterns=VERIFY_DENY_PATTERNS,
        project_type="new",
    )
    check(rep_new["verdict"] == "pass", f"new 项目空基线真跑 verdict=pass（实际 {rep_new['verdict']}）",
          rep_new.get("summary", "") + " | " + "；".join(rep_new.get("problems", [])[:2]))
    check(rep_new["executed"] is True and rep_new["reason_code"] == "verify_passed",
          "executed=True / reason_code=verify_passed", f"{rep_new['executed']}/{rep_new['reason_code']}")
    check((td / "verify" / "base").is_dir(), "runs/<id>/verify/base/ 空基线目录已建立")
    check((Path(rep_new["sandbox"]) / "main.py").is_file(), "main.py 已物化为完整新文件")
    cmds = rep_new.get("commands") or []
    check(any("unittest" in str(c.get("command")) and c.get("status") == "ok" for c in cmds),
          "unittest 命令真实执行成功（断言型证据存在）")
    # 语义图评估：真跑 pass ⇒ delivery/materialization/behavior 全部 PROVEN
    gl = O.OntologyGraph()
    O.build_requirement_projection(gl, {
        "functional_requirements": [
            {"id": "FR-01", "description": "命令行记账主流程跑通",
             "acceptance": ["unittest 测试全部通过"]},
        ]}, original_requirement="记账工具")
    O.evaluate_against_verify(gl, rep_new)
    kinds = {po.kind: po.status for po in gl.obligations.values()}
    check(kinds.get(O.PO_KIND_DELIVERY) == O.PO_STATUS_PROVEN,
          f"delivery PO=PROVEN（{kinds}）")
    check(all(s == O.PO_STATUS_PROVEN for s in kinds.values()),
          f"所有 PO 在真跑断言通过后 PROVEN（{kinds}）")
    # materialization 类义务（“建表”文本映射而来）由物化证据单独证明。
    gm = O.OntologyGraph()
    O.build_requirement_projection(gm, {
        "functional_requirements": [
            {"id": "FR-09", "description": "运行后在 SQLite 建表并写入初始数据"}]})
    O.evaluate_against_verify(gm, rep_new)
    mk = {po.kind: po.status for po in gm.obligations.values()}
    check(mk.get(O.PO_KIND_MATERIALIZATION) == O.PO_STATUS_PROVEN,
          f"materialization PO 由 patch_apply 物化证据 PROVEN（{mk}）")
    proof_new = O.release_proof_status(
        semantic_pass=True, verify_verdict="pass",
        obligations=list(gl.obligations.values()), workspace_verified=True)
    check(proof_new["can_pass"] and proof_new["status"] == "PROVEN",
          "证据齐备 ⇒ Proof Gate 放行", proof_new["status"])
    # 对照：secondary 无 repo ⇒ skipped（reason_code=no_repo），Proof Gate 阻断 pass
    td2 = Path(tempfile.mkdtemp(prefix="ont_verify_sec_"))
    rep_sec = V.verify(
        td2, None, impl_new, {"source_available": False}, test_report,
        allowed_bins=VERIFY_ALLOWED_BINS, deny_patterns=VERIFY_DENY_PATTERNS,
        project_type="secondary",
    )
    check(rep_sec["verdict"] == "skipped" and rep_sec["reason_code"] == "no_repo",
          "secondary 无 repo 允许 skipped（但不允许 pass）", rep_sec["verdict"])
    gs = O.OntologyGraph()
    O.build_requirement_projection(gs, {
        "functional_requirements": [
            {"id": "FR-01", "description": "命令行记账主流程跑通",
             "acceptance": ["unittest 测试全部通过"]},
        ]}, original_requirement="记账工具")
    O.evaluate_against_verify(gs, rep_sec)
    proof_sec = O.release_proof_status(
        semantic_pass=True, verify_verdict="skipped",
        obligations=list(gs.obligations.values()), workspace_verified=False)
    check(proof_sec["status"] == "UNPROVEN" and not proof_sec["can_pass"],
          "secondary skipped ⇒ UNPROVEN 机械阻断 pass", proof_sec["status"])

    # ---- K. 版本常量与序列化 ----
    print("== K. 版本 / 序列化纪律 ==")
    check(O.ONTOLOGY_SCHEMA_VERSION == "1" and O.ONTOLOGY_RULES_VERSION == "1",
          "schema/rules 版本常量 = 1")
    raw = json.loads(json.dumps(g.to_dict(), ensure_ascii=False))
    g_back = O.OntologyGraph.from_dict(raw)
    check(len(g_back.objects) == len(g.objects) and len(g_back.obligations) == len(g.obligations),
          "图 JSON 往返无损")
    check(O.canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}',
          "canonical JSON 键序稳定、分隔符紧凑")
    check(O.stable_hash({"a": 1}) == O.stable_hash({"a": 1}), "stable_hash 确定性")

    # ---- M. P0-3：Plan/Task 语义投影、稳定身份、歧义不猜、指纹版本、骨架越权 ----
    print("== M. Plan IR / TaskCompiler 语义集成 ==")
    from pipeline import planir as P  # noqa: E402
    from pipeline import taskcompiler as TC  # noqa: E402
    from pipeline import symbols as S  # noqa: E402

    raw_plan = {
        "changes": [
            {
                "path": "ledger_db.py",
                "symbols": ["init_database"],
                "intent": "运行记账命令后在 SQLite 建表并写入一条流水",
            }
        ],
        "tasks": [
            {"id": "T-01", "target_files": ["ledger_db.py"], "symbols": ["init_database"],
             "constraints": ["不得删除已有流水"]},
        ],
    }
    ir_m = P.normalize_plan(raw_plan, scope=_pm_scope(), original_requirement="做一个命令行记账工具")
    ont_m = ir_m.get("ontology") or {}
    check(isinstance(ont_m.get("objects"), list) and ont_m["objects"],
          "normalize_plan IR 带 ontology 投影（对象非空）")
    unit_m = ir_m["units"][0]
    check("req:FR-01" in (unit_m.get("implements_requirements") or []),
          f"施工单元确定性挂上 FR-01（{unit_m.get('implements_requirements')}）",
          str(unit_m.get("implements_requirements")))
    check(unit_m.get("proof_obligation_ids"), "单元携带 FR-01 的 ProofObligation id")
    cst_objs = [o for o in ont_m["objects"] if o.get("type") == O.TYPE_CONSTRAINT]
    check(cst_objs and cst_objs[0]["truth"] == O.TRUTH_DERIVED,
          "单元 constraints 投影为 DERIVED Constraint 对象")

    res1 = TC.compile_plan(raw_plan, ir=ir_m, existing_files=set())
    t1 = res1["tasks"][0]
    sid1 = t1.get("semantic_task_id") or ""
    check(sid1.startswith("stask:") and t1.get("task_revision") == 1
          and t1.get("supersedes") == [],
          "新编译施工图有 semantic_task_id、revision=1、空 supersedes")
    check("req:FR-01" in (t1.get("implements_requirements") or []),
          "施工图继承 implements_requirements")
    check(t1.get("proof_obligations") == unit_m.get("proof_obligation_ids"),
          "施工图继承 proof_obligations")

    # 重新编译：前面插一张无关文件的图，目标图 T-01 被重编号为 T-02，语义身份必须不变。
    raw_plan2 = {
        "changes": [
            {"path": "util_helpers.py", "symbols": ["format_money"],
             "intent": "按月汇总支出金额时格式化金额"},
            raw_plan["changes"][0],
        ],
        "tasks": [
            {"id": "T-09", "target_files": ["util_helpers.py"], "symbols": ["format_money"]},
            {"id": "T-10", "target_files": ["ledger_db.py"], "symbols": ["init_database"],
             "constraints": ["不得删除已有流水"]},
        ],
    }
    ir2 = P.normalize_plan(raw_plan2, scope=_pm_scope(), original_requirement="做一个命令行记账工具")
    res2 = TC.compile_plan(raw_plan2, ir=ir2, existing_files=set(),
                           previous_tasks=res1["tasks"])
    t2 = [t for t in res2["tasks"] if t.get("symbols") == ["init_database"]][0]
    check(t2["id"] != t1["id"] and t2["semantic_task_id"] == sid1,
          f"重编号后语义身份稳定（{t1['id']}→{t2['id']} 同一 {sid1[:16]}…）")
    check(t2.get("task_revision") == 2 and t1["id"] in (t2.get("supersedes") or []),
          f"同身份再编译 revision=2 且 supersedes 指向上版（{t2.get('supersedes')}）")

    # resolve_symbol_to_file：唯一命中 / 歧义不猜 / 标准库 external
    skel_unique = {"ledger_db.py": ["class LedgerStore", "def fetch_all()"]}
    r_unique = S.resolve_symbol_to_file(
        "fetch_all", changes_files=["ledger_db.py"], skeleton=skel_unique)
    check(r_unique["status"] == "resolved" and r_unique["file"] == "ledger_db.py",
          f"全局唯一符号解析到文件（{r_unique}）")
    skel_amb = {
        "shop/store.py": ["class Store"],
        "warehouse/store.py": ["class Store"],
    }
    r_amb = S.resolve_symbol_to_file(
        "Store", changes_files=["shop/store.py", "warehouse/store.py"], skeleton=skel_amb)
    check(r_amb["status"] == "unresolved" and r_amb["reason"] == "ambiguous_symbol"
          and len(r_amb["candidates"]) == 2,
          f"多候选必须 ambiguous_symbol，禁止猜第一个（{r_amb}）")
    r_mod = S.resolve_symbol_to_file(
        "ledger_db.init_database", changes_files=["ledger_db.py"],
        skeleton={"ledger_db.py": ["def init_database():"]})
    check(r_mod["status"] == "resolved" and r_mod["file"] == "ledger_db.py",
          f"模块.符号 精确命中（{r_mod}）")
    r_ext = S.resolve_symbol_to_file("sqlite3", changes_files=["ledger_db.py"])
    check(r_ext["status"] == "external", f"标准库识别为 external（{r_ext}）")

    # fingerprint：ontology 版本与单套 compiler_hash
    fp = P.fingerprint()
    check(fp.get("ontology_schema_version") == O.ONTOLOGY_SCHEMA_VERSION
          and fp.get("ontology_rules_version") == O.ONTOLOGY_RULES_VERSION
          and len(str(fp.get("compiler_hash") or "")) == 12,
          f"fingerprint 含 ontology 版本且仍为单一 compiler_hash（{fp}）")

    # 骨架越权：新增文件 + 新增符号都必须暴露；方案 symbols 为空时不判符号越权
    over = P.skeleton_overreach(
        {"changes": [{"path": "db.py", "symbols": ["Database"]}]},
        {"db.py": ["class Database", "class Secret"], "rogue.py": ["class Rogue"]})
    check(over["extra_files"] == ["rogue.py"] and over["extra_symbols"].get("db.py") == ["Secret"],
          f"骨架越权文件/符号被机械识别（{over}）")
    over2 = P.skeleton_overreach(
        {"changes": [{"path": "db.py"}]},
        {"db.py": ["class Database", "class Secret"]})
    check(not over2["extra_files"] and not over2["extra_symbols"],
          "方案未声明 symbols 时骨架是回填源，不判越权")

    # ---- N. P1-1：runstore 只追加语义版本 + evidence 工厂真值纪律 ----
    print("== N. runstore ontology 版本存储 / Evidence 工厂 ==")
    import tempfile  # noqa: E402
    from pipeline import runstore as RS  # noqa: E402
    from pipeline import evidence as E  # noqa: E402

    run_d = Path(tempfile.mkdtemp(prefix="ont_runstore_"))
    d1 = RS.write_ontology(run_d, g.to_dict(), stage="architect_plan")
    d2 = RS.write_ontology(run_d, g.to_dict(), stage="review")
    check(d1["revision"] == 1 and d2["revision"] == 2 and d2["revision_id"] == "ont:0002:review",
          f"语义图只追加版本 1/2（{d1['revision_id']} / {d2['revision_id']}）")
    check(RS.latest_ontology_revision(run_d).get("revision") == 2,
          "latest_ontology_revision 指向第 2 版")
    env_latest = RS.read_ontology(run_d)
    env_one = RS.read_ontology(run_d, 1)
    check(env_latest and env_latest["revision"] == 2
          and env_one and env_one["revision_id"] == "ont:0001:architect_plan"
          and isinstance(env_latest["graph"].get("objects"), list),
          "read_ontology latest/序号/ont id 三种取法都能读回图")
    check(RS.read_ontology(run_d, 99) is None and RS.latest_ontology_revision(Path(tempfile.mkdtemp())) == {},
          "缺版本 / 旧 run（无 ontology 目录）安全降级为空")

    ev_human = E.make_evidence("human_confirmation", "user@gate", claim_ids=["claim:x"])
    check(ev_human["truth"] == O.TRUTH_ASSERTED and ev_human["id"].startswith("ev:"),
          "human_confirmation 机械锁为 ASSERTED（人工事实不等于 PROVEN）")
    ev_derived = E.make_evidence("syntax", "py_compile", status=O.PO_STATUS_PROVEN)
    check(ev_derived["truth"] == O.TRUTH_DERIVED,
          "非人工证据默认 DERIVED（状态 PROVEN 也不能私自抬真值）")
    ev_back = O.EvidenceRecord.from_dict(ev_human)
    check(ev_back.kind == "human_confirmation" and ev_back.claim_ids == ["claim:x"],
          "证据 dict 与 ontology.EvidenceRecord JSON 往返同形")

    print("== O. Defect 投影 / Failure taxonomy / 责任回溯（P1-2）==")
    from pipeline import diagnose as DGN  # noqa: E402
    from pipeline import taskcompiler as TC  # noqa: E402

    # ① 9 类闭集的机械映射（不推断）
    tx = DGN.failure_taxonomy
    check(tx(DGN.VERIFY_FAILED)["type"] == DGN.FAIL_VERIFICATION
          and tx(DGN.VERIFY_FAILED)["owner_role"] == DGN.ROLE_DEV,
          "verify_failed → verification_failure / dev")
    check(tx(DGN.PATCH_UNAPPLIABLE, DGN.OWNER_PATCH_RUNTIME)["type"] == DGN.FAIL_PIPELINE
          and tx(DGN.PATCH_UNAPPLIABLE, DGN.OWNER_PATCH_RUNTIME)["recover_stage"] == "human_review",
          "补丁物化故障 → pipeline_failure / human_review（重试 Agent 无意义）")
    check(tx(DGN.PATCH_UNAPPLIABLE, DGN.OWNER_COMPILER_TARGET)["type"] == DGN.FAIL_COMPILATION
          and tx(DGN.PATCH_UNAPPLIABLE, DGN.OWNER_COMPILER_TARGET)["owner_role"] == DGN.ROLE_COMPILER,
          "虚靶补丁 → compilation_failure / compiler")
    check(tx(DGN.PATCH_UNAPPLIABLE)["type"] == DGN.FAIL_IMPLEMENTATION,
          "普通补丁判负 → implementation_failure / dev")
    check(tx(DGN.CONTRACT_UNRESOLVED)["type"] == DGN.FAIL_PLANNING
          and tx(DGN.PLAN_GAP)["type"] == DGN.FAIL_PLANNING
          and tx(DGN.MISSING_ENTRY)["type"] == DGN.FAIL_PLANNING,
          "契约虚依赖 / 方案漏项 / 缺入口 → planning_failure / architect")
    check(tx(DGN.AMBIGUOUS)["type"] == DGN.FAIL_AMBIGUOUS
          and tx(DGN.NONE)["type"] == ""
          and tx("proof_gate_unproven")["type"] == DGN.FAIL_VERIFICATION
          and tx("proof_gate_failed")["type"] == DGN.FAIL_VERIFICATION,
          "ambiguous→ambiguous_failure；none→空（不许建 Defect）；Proof Gate 码→verification")
    # 未知归因不许偷偷进确定责任桶
    check(tx("some_new_reason")["type"] == DGN.FAIL_AMBIGUOUS,
          "未知归因保守落 ambiguous_failure")
    # classify 附加键与既有键并存（历史调用方不受影响）
    rec = DGN.classify(review={"verdict": "fail"},
                       mechanical_blockers=["验证命令 test_x 退出码 1"])
    check(rec["type"] == DGN.VERIFY_FAILED and rec["failure"]["type"] == DGN.FAIL_VERIFICATION
          and set(("type", "owner", "recover_stage", "recovery")) <= set(rec),
          "classify() 既有键不变且新增 failure 三元组")

    # ② resolve_bug_task 四级优先 + 歧义不猜
    tasks = [
        {"id": "T-02", "stable_id": "stable_a", "semantic_task_id": "stask:aaa",
         "target_files": ["src/store.py"], "symbols": ["Store"]},
        {"id": "T-05", "stable_id": "stable_b", "semantic_task_id": "stask:bbb",
         "target_files": ["src/store.py"], "symbols": ["Database"]},
        {"id": "T-04", "stable_id": "stable_c", "semantic_task_id": "stask:ccc",
         "target_files": ["src/cli.py"], "symbols": ["main"]},
        {"id": "T-07", "stable_id": "stable_d", "semantic_task_id": "stask:ddd",
         "target_files": ["src/multi.py"], "symbols": ["f"]},
        {"id": "T-08", "stable_id": "stable_e", "semantic_task_id": "stask:eee",
         "target_files": ["src/multi.py"], "symbols": ["f"]},
    ]
    r1 = TC.resolve_bug_task({"semantic_task_id": "stask:bbb",
                              "path": "src/legacy.py", "symbol": "Database"}, tasks)
    check(r1["task_id"] == "T-05" and r1["matched_by"] == "semantic_id+symbol",
          "① 语义身份命中即断链：rework 重编号（原 T-03 现 T-05）仍回到同一张图")
    r2 = TC.resolve_bug_task({"stable_id": "stable_a", "symbol": "Store"}, tasks)
    check(r2["task_id"] == "T-02" and r2["matched_by"] == "stable_id+symbol",
          "② legacy stable_id + 符号回退")
    r3 = TC.resolve_bug_task({"path": "src/cli.py", "symbol": "pkg.mod.main"}, tasks)
    check(r3["task_id"] == "T-04" and r3["matched_by"] == "file+symbol",
          "③ 文件+符号命中（限定符号按叶子名比较）")
    r4 = TC.resolve_bug_task({"path": "src/cli.py"}, tasks)
    check(r4["task_id"] == "T-04" and r4["matched_by"] == "file-only",
          "④ 仅文件命中（同文件唯一图）")
    r5 = TC.resolve_bug_task({"path": "src/multi.py", "symbol": "f"}, tasks)
    check(r5["task_id"] == "" and r5["matched_by"] == "ambiguous_symbol"
          and set(r5["candidates"]) == {"T-07", "T-08"},
          "同文件同符号多候选 → ambiguous_symbol，禁止猜第一张")
    r6 = TC.resolve_bug_task({"path": "src/multi.py"}, tasks)
    check(r6["matched_by"] == "ambiguous_file" and len(r6["candidates"]) == 2,
          "仅文件但多图 → ambiguous_file")
    r7 = TC.resolve_bug_task({"path": "nope.py", "symbol": "x"}, tasks)
    check(r7["task_id"] == "" and r7["matched_by"] == "unresolved",
          "完全挂不上 → unresolved（不猜 T-id）")

    # ③ Defect 跨轮身份：行号/措辞漂移仍是同一条
    k1 = DGN.defect_key({"kind": "traceback:ImportError", "path": "a.py", "symbol": "f",
                         "command": "python -m unittest", "line": 20, "what": "导入失败"})
    k2 = DGN.defect_key({"kind": "traceback:ImportError", "path": "a.py", "symbol": "f",
                         "command": "python -m unittest", "line": 27, "what": "模块无法导入"})
    check(k1 == k2 and k1.startswith(DGN.DEFECT_KEY_VERSION + "::"),
          "defect_key 只取 kind+check+file+symbol（line/what 是证据，不进身份）")

    # ④ 语义图投影：Defect→Task/PO，Failure.raises→Defect.triggers→Recovery，责任链回溯
    go = O.OntologyGraph()
    O.build_requirement_projection(go, _pm_scope(), original_requirement="做一个命令行记账工具")
    for t in tasks[:3]:
        sid = t["semantic_task_id"]
        go.add(O.SemanticObject(id=sid, type=O.TYPE_TASK, truth=O.TRUTH_DERIVED,
                                payload={"task_id": t["id"]}))
        go.relate(sid, "implements", "req:root", truth=O.TRUTH_DERIVED)
    po_fail = next(iter(go.obligations.values()))
    po_fail.status = O.PO_STATUS_FAILED
    row = {"kind": "traceback:ImportError", "path": "src/store.py", "symbol": "Database",
           "command": "python -m pytest", "what": "导入失败", "status": "fail",
           "source": "verify"}
    mapping = TC.resolve_bug_task(row, tasks)
    did = O.project_defect(go, key=k1, row=row,
                           task_id=mapping["semantic_task_id"], po_ids=[po_fail.id])
    rels = {(r.subject, r.predicate, r.object) for r in go.relations}
    check((did, "affects_task", "stask:bbb") in rels and (did, "violates", po_fail.id) in rels,
          "Defect 机械挂到责任 Task 与 FAILED PO（不造悬空端点）")
    check(go.get(did).type == O.TYPE_DEFECT and go.get(did).truth == O.TRUTH_DERIVED,
          "Defect 是 DERIVED 语义投影，台账仍是唯一事实源")
    # 产物责任链：Defect caused_by Artifact derived_from 上游 Artifact/Requirement
    go.add(O.SemanticObject(id="art:patch1", type=O.TYPE_ARTIFACT, truth=O.TRUTH_DERIVED))
    go.add(O.SemanticObject(id="art:plan", type=O.TYPE_ARTIFACT, truth=O.TRUTH_DERIVED))
    go.relate(did, "caused_by", "art:patch1", truth=O.TRUTH_DERIVED)
    go.relate("art:patch1", "derived_from", "art:plan", truth=O.TRUTH_DERIVED)
    go.relate("art:plan", "derived_from", "req:root", truth=O.TRUTH_DERIVED)

    fail_info = tx(DGN.VERIFY_FAILED)
    fid = O.failure_id(3, fail_info["type"])
    check(fid == O.failure_id(3, fail_info["type"]) and fid != O.failure_id(4, fail_info["type"]),
          "failure_id 按轮次+类别稳定（同轮同因同 id，跨轮不同）")
    rid = "rec:" + O.stable_hash(O.canonical_json([fid, 3]), length=12)
    O.project_recovery(go, rid=rid,
                       recovery={"action": "escalate_plan", "reason": "同证据连失两轮", "streak": 2},
                       round_no=3)
    got_fid = O.project_failure(go, fid=fid, failure=fail_info, round_no=3,
                                defect_ids=[did], recovery_id=rid)
    check(got_fid == fid, "Failure 投影成功（空 type 才允许不建）")
    check(O.project_failure(go, fid="fail:skip", failure=tx(DGN.NONE)) == "",
          "NONE 归因拒绝创建 Failure 对象")
    rels = {(r.subject, r.predicate, r.object) for r in go.relations}
    check((fid, "raises", did) in rels and (fid, "triggers", rid) in rels,
          "Failure.raises→Defect、Failure.triggers→Recovery")
    trace = O.trace_recovery_owner(go, fid)
    check(trace["owner_role"] == DGN.ROLE_DEV and trace["tasks"] == ["stask:bbb"]
          and "art:patch1" in trace["artifacts"] and "art:plan" in trace["artifacts"]
          and did in trace["defects"] and len(trace["chain"]) >= 4,
          "责任回溯：角色=dev、Task=T-05 语义图、产物链上溯到 plan",
          json.dumps(trace, ensure_ascii=False))
    rec_obj = go.get(rid)
    check(rec_obj.type == O.TYPE_RECOVERY and rec_obj.payload["action"] == "escalate_plan"
          and rec_obj.payload["streak"] == 2,
          "Recovery 记录动作/理由/证据连续次数（returns_to 是 Stage，落 payload 不造对象）")
    # 幂等：同 Defect/Failure 再投不产生重复边
    n_before = len(go.relations)
    O.project_defect(go, key=k1, row=row, task_id=mapping["semantic_task_id"], po_ids=[po_fail.id])
    O.project_failure(go, fid=fid, failure=fail_info, round_no=3, defect_ids=[did], recovery_id=rid)
    check(len(go.relations) == n_before, "Defect/Failure 重复投影幂等")
    check(not OV.has_blocking(OV.validate_all(go)),
          "P1-2 投影图通过全部校验器",
          json.dumps(OV.validate_all(go), ensure_ascii=False))

    print("== P. 事实优先级仲裁 / Intake DERIVED 投影（P2）==")
    from pipeline import prompts as P  # noqa: E402

    # ① reconcile_claims：优先级链 USER>HUMAN>PM>ARCHITECT>TEST>DEV_SELF_CHECK
    claims = [
        {"id": "c_pm", "subject": "auto-renumber", "polarity": 1,
         "source": O.FACT_SOURCE_PM, "truth": O.TRUTH_DERIVED},
        {"id": "c_human", "subject": "auto-renumber", "polarity": -1,
         "source": O.FACT_SOURCE_HUMAN, "truth": O.TRUTH_ASSERTED},
    ]
    rc1 = O.reconcile_claims(list(reversed(claims)))  # 故意倒序输入
    check(rc1["accepted"] == ["c_human"]
          and rc1["overridden"][0]["id"] == "c_pm"
          and rc1["overridden"][0]["by"] == "c_human"
          and not rc1["contradictions"],
          "人工 ASSERTED 压过 PM DERIVED；结论与字段出现顺序无关（后字段不覆盖前字段）")
    # DERIVED 即使来自更高角色也不能压 ASSERTED（三真值铁律在仲裁里再次落地）
    rc1b = O.reconcile_claims([
        {"id": "u", "subject": "s", "polarity": 1,
         "source": O.FACT_SOURCE_USER, "truth": O.TRUTH_DERIVED},
        {"id": "h", "subject": "s", "polarity": -1,
         "source": O.FACT_SOURCE_HUMAN, "truth": O.TRUTH_ASSERTED},
    ])
    check(rc1b["accepted"] == ["h"],
          "真值优先于角色：ASSERTED human 不被 DERIVED user 覆盖")
    # 同级对立 → contradiction，不猜
    rc2 = O.reconcile_claims([
        {"id": "a", "subject": "t", "polarity": 1,
         "source": O.FACT_SOURCE_PM, "truth": O.TRUTH_DERIVED},
        {"id": "b", "subject": "t", "polarity": -1,
         "source": O.FACT_SOURCE_PM, "truth": O.TRUTH_DERIVED},
    ])
    check(bool(rc2["contradictions"]) and rc2["contradictions"][0]["reason"] == "same_level_conflict"
          and set(rc2["accepted"]) == {"a", "b"},
          "同优先级一肯一否 ⇒ CONTRADICTION：双方保留、阻断、不猜赢家")
    # 同极性互证 / 不同主体互不干扰 / 无主语不并组
    rc3 = O.reconcile_claims([
        {"id": "a", "subject": "x", "polarity": 1, "source": O.FACT_SOURCE_PM},
        {"id": "b", "subject": "x", "polarity": 1, "source": O.FACT_SOURCE_ARCHITECT},
        {"id": "c", "subject": "y", "polarity": -1, "source": O.FACT_SOURCE_PM},
        {"id": "d", "polarity": 1, "source": O.FACT_SOURCE_TEST},
    ])
    check(set(rc3["accepted"]) == {"a", "b", "c", "d"} and not rc3["contradictions"],
          "同极性互证 / 不同主体不冲突 / 无 subject 不强行并组")

    # ② prompts.claim_conflict_groups：机械发现 + 跨级/同级分组
    scope_pm = {
        "functional_requirements": [
            {"id": "FR-01",
             "description": "删除记账条目后，序号字段自动重新排序",
             "acceptance": ["删除后列表序号连续"]},
        ],
        "open_questions": [
            {"question": "删除条目后序号是否自动重排？", "final_decision": "序号保持不变，不自动重新排序"},
        ],
        "confirmed_facts": [],
    }
    groups = P.claim_conflict_groups(scope_pm, [])
    check(len(groups) == 1 and {f["source"] for f in groups[0]} == {"human", "pm"},
          "跨级极性翻转被机械发现（human 裁决 vs PM FR）")
    sub = [{"id": f["id"], "subject": "g0", "polarity": f["polarity"],
            "source": f["source"], "truth": f["truth"]} for f in groups[0]]
    check(not O.reconcile_claims(sub)["contradictions"],
          "跨级组经仲裁不阻断（高优先级 human 覆盖 PM，PM 返工改文本即可）")
    # 同级：PM 自己两条验收一肯一否
    scope_same = {
        "functional_requirements": [
            {"id": "FR-01",
             "description": "删除记账条目后，序号字段自动重新排序",
             "acceptance": ["删除后序号保持不变、不自动重新排序"]},
        ],
    }
    groups2 = P.claim_conflict_groups(scope_same, [])
    check(len(groups2) == 1 and all(f["source"] == "pm" for f in groups2[0]),
          "PM 内部同主体一肯一否被发现")
    sub2 = [{"id": f["id"], "subject": "g0", "polarity": f["polarity"],
             "source": f["source"], "truth": f["truth"]} for f in groups2[0]]
    check(bool(O.reconcile_claims(sub2)["contradictions"]),
          "PM 同级对立 → CONTRADICTION 阻断（不让后写的字段静默覆盖）")

    # ③ project_intake：背景/目标用户/默认假设 DERIVED；人工裁决 ASSERTED
    gp = O.OntologyGraph()
    O.build_requirement_projection(gp, {"functional_requirements": []},
                                   original_requirement="做一个记账工具")
    ids = O.project_intake(
        gp,
        scope={"background": "用户在命令行管理日常收支",
               "target_users": ["个人用户", "个体户"]},
        intake_rows=[
            {"element": "数据存储位置", "default_assumption": "默认使用本地 SQLite 文件"},
            {"element": "是否需要多用户", "final_decision": "本期只做单机单用户"},
        ],
    )
    check(len(ids) == 5, f"背景+2 目标用户+2 intake 行共 5 条 Claim（{len(ids)}）", str(ids))
    derived = [gp.get(i) for i in ids if gp.get(i).truth == O.TRUTH_DERIVED]
    asserted = [gp.get(i) for i in ids if gp.get(i).truth == O.TRUTH_ASSERTED]
    check(len(derived) == 4 and len(asserted) == 1
          and asserted[0].payload["text"] == "本期只做单机单用户",
          "背景/目标用户/默认假设 = DERIVED；人工 final_decision = ASSERTED")
    pred = {(r.subject, r.predicate, r.object) for r in gp.relations}
    check(all((i, "claim_derives", "req:root") in pred for i in ids),
          "Intake Claim 全部 derived_from 原始需求（可追溯，不凭空出现）")
    # 幂等 + 无 req:root 安全跳过
    n_before = len(gp.objects)
    O.project_intake(gp, scope={"background": "用户在命令行管理日常收支", "target_users": ["个人用户", "个体户"]},
                     intake_rows=[{"element": "数据存储位置", "default_assumption": "默认使用本地 SQLite 文件"}])
    check(len(gp.objects) == n_before, "Intake 投影幂等（默认假设与前次同文不重复造）")
    g_empty = O.OntologyGraph()
    check(O.project_intake(g_empty, scope={"background": "无主"}) == [],
          "底图没有 req:root 时不挂悬空 DerivedClaim")
    check(not OV.has_blocking(OV.validate_all(gp)),
          "Intake 投影图通过全部校验器",
          json.dumps(OV.validate_all(gp), ensure_ascii=False))

    # ---- Q：Interface Freeze 双证据 / Invariant 绑定 / Schema Guardian（本轮收尾）----
    print("== Q. Interface Freeze 双证据 + Invariant 可执行绑定 + Schema Guardian ==")

    # ① freeze_symbols：冻结集合 = 骨架声明 ∩ 方案承诺（类/方法/函数三级判定）
    skel_q = {
        "store.py": ["class Store", "    def save(self, x)", "    def drop(self)"],
        "util.py": ["def helper()"],
        "ghost.py": ["class Ghost"],
    }
    plan_syms = {"store.py": {"Store.save"}, "util.py": set()}
    frozen_q = O.freeze_symbols(skel_q, plan_syms)
    fset_q = {(f["file"], f["symbol"]) for f in frozen_q}
    check(fset_q == {("store.py", "Store"), ("store.py", "Store.save")},
          f"类（方案承诺其方法即冻结）与命中方法冻结；未承诺方法/函数/文件不冻结（{frozen_q}）")
    # 文件态骨架形态同样可解析；路径分隔符归一
    frozen_files = O.freeze_symbols(
        {"files": [{"path": "a\\b.py", "symbols": ["class Box", "    def open(self)"]}]},
        {"a/b.py": {"Box.open"}})
    check((frozen_files[0]["file"] == "a/b.py" and len(frozen_files) == 2),
          f"文件态骨架与 \\ 路径归一（{frozen_files}）")

    # ② project_interface_freeze：双证据 PO 投影 + 幂等
    gq = O.OntologyGraph()
    po_ids_q = O.project_interface_freeze(gq, frozen_q)
    n_obj, n_rel, n_po = len(gq.objects), len(gq.relations), len(gq.obligations)
    check(len(po_ids_q) == 2 and all(p.kind == O.PO_KIND_INTERFACE_FREEZE for p in gq.obligations.values()),
          f"每个冻结符号一条 interface_freeze PO（{po_ids_q}）")
    po0 = gq.obligations[po_ids_q[0]]
    check(po0.required and po0.verifier.get("type") == "dual_evidence"
          and set(po0.verifier.get("requires") or []) == {"contract_check", "skeleton_conformance"}
          and po0.verifier.get("file") and po0.verifier.get("symbol"),
          f"freeze PO required + 双证据 verifier + file/symbol 随 PO 持久化（{po0.verifier}）")
    O.project_interface_freeze(gq, frozen_q)
    check(len(gq.objects) == n_obj and len(gq.relations) == n_rel and len(gq.obligations) == n_po,
          "freeze 重复投影幂等（不造重复 Claim/PO/边）")
    check(not OV.has_blocking(OV.validate_all(gq)),
          "freeze 投影图通过全部校验器",
          json.dumps(OV.validate_all(gq), ensure_ascii=False))

    # ③ evaluate 四路：双过 PROVEN / 缺一路 UNPROVEN / 契约失败 FAILED / 骨架失败 FAILED
    def _freeze_graph() -> tuple[O.OntologyGraph, str]:
        gg = O.OntologyGraph()
        ids = O.project_interface_freeze(
            gg, [{"file": "store.py", "symbol": "Store.save", "granularity": "method"}])
        return gg, ids[0]

    rep_pass = {"verdict": "pass", "materialized": ["store.py"],
                "commands": [{"status": "ok", "command": "python main.py", "exit_code": 0,
                              "stdout_tail": "", "stderr_tail": "", "source": "entry"}]}
    g_both, pid_both = _freeze_graph()
    O.evaluate_against_verify(g_both, dict(rep_pass), contract_checked=True,
                              skeleton_conformance={"missing": [], "mismatch": [],
                                                    "by_file": {}, "checked": 1})
    check(g_both.obligations[pid_both].status == O.PO_STATUS_PROVEN
          and len(g_both.obligations[pid_both].evidence_ids) == 2,
          f"双证据都真跑且都过 ⇒ PROVEN（{g_both.obligations[pid_both].status}）")
    g_no_skel, pid_no_skel = _freeze_graph()
    O.evaluate_against_verify(g_no_skel, dict(rep_pass), contract_checked=True,
                              skeleton_conformance=None)
    check(g_no_skel.obligations[pid_no_skel].status == O.PO_STATUS_UNPROVEN,
          "缺骨架路证据（None）⇒ UNPROVEN，契约路不得顶替")
    g_no_ct, pid_no_ct = _freeze_graph()
    O.evaluate_against_verify(g_no_ct, dict(rep_pass), contract_checked=False,
                              skeleton_conformance={"missing": [], "mismatch": [],
                                                    "by_file": {}, "checked": 1})
    check(g_no_ct.obligations[pid_no_ct].status == O.PO_STATUS_UNPROVEN,
          "缺契约路证据（本轮没跑 contract_check）⇒ UNPROVEN")
    g_cbad, pid_cbad = _freeze_graph()
    O.evaluate_against_verify(
        g_cbad, dict(rep_pass),
        contract_problems=["store.py 契约要求的 `Store.save` 未在产物中定义"],
        contract_checked=True,
        skeleton_conformance={"missing": [], "mismatch": [], "by_file": {}, "checked": 1})
    check(g_cbad.obligations[pid_cbad].status == O.PO_STATUS_FAILED
          and g_cbad.obligations[pid_cbad].evidence_ids
          and all(g_cbad.evidence[e].kind == "contract"
                  for e in g_cbad.obligations[pid_cbad].evidence_ids),
          "契约路点名该符号失败 ⇒ FAILED（只挂失败的契约证据）",
          g_cbad.obligations[pid_cbad].status)
    g_sbad, pid_sbad = _freeze_graph()
    O.evaluate_against_verify(
        g_sbad, dict(rep_pass), contract_checked=True,
        skeleton_conformance={
            "missing": ["产物 store.py 中未找到冻结声明的 `Store.save`"],
            "mismatch": [], "by_file": {"store.py": ["Store.save"]}, "checked": 1})
    check(g_sbad.obligations[pid_sbad].status == O.PO_STATUS_FAILED
          and all(g_sbad.evidence[e].kind == "skeleton_conformance"
                  for e in g_sbad.obligations[pid_sbad].evidence_ids),
          "骨架路 missing/by_file 点名该符号 ⇒ FAILED（只挂失败的骨架证据）",
          g_sbad.obligations[pid_sbad].status)
    g_skip, pid_skip = _freeze_graph()
    O.evaluate_against_verify(
        g_skip, {"verdict": "skipped", "materialized": [], "commands": []},
        contract_checked=True,
        skeleton_conformance={"missing": [], "mismatch": [], "by_file": {}, "checked": 1})
    check(g_skip.obligations[pid_skip].status == O.PO_STATUS_UNPROVEN,
          "verify skipped ⇒ 静态路视为没真跑，freeze PO 保持 UNPROVEN")

    # ④ bound_invariant_check 四态 + check_id 反查
    b1 = O.bound_invariant_check({"id": "", "check": {"type": "mechanical", "check_id": "smoke_merge"}})
    check(b1["bound"] and b1["known"] and b1["check_id"] == "smoke_merge"
          and b1["entry"].endswith("smoke_merge.py"),
          f"check_id 直命中内置表（{b1}）")
    b2 = O.bound_invariant_check({"id": "documentation_refs"})
    check(b2["bound"] and b2["known"] and b2["check_id"] == "check_refs",
          f"invariant id 命中（{b2}）")
    b3 = O.bound_invariant_check({"name": "必须保证 cross_round_merge_order（补丁按轮序合并）"})
    check(b3["bound"] and b3["known"] and b3["invariant_id"] == "cross_round_merge_order",
          f"名字字面包含内置 id 的确定性兜底（{b3}）")
    b4 = O.bound_invariant_check({"id": "x", "check": {"type": "mechanical", "check_id": "fake_check"}})
    check(b4["bound"] and not b4["known"] and b4["check_id"] == "fake_check",
          f"声明机械但查无此检查 ⇒ known=False（{b4}）")
    b5 = O.bound_invariant_check({"name": "代码要保持优雅可读"})
    check((not b5["bound"]) and (not b5["known"]), f"纯人话不变量 ⇒ bound=False（{b5}）")

    # ⑤ validate_invariants：假 check_id 拦截，真绑定+entry 放行
    gv = O.OntologyGraph()
    gv.add(O.SemanticObject(id="inv:fake", type=O.TYPE_INVARIANT, truth=O.TRUTH_DERIVED,
                            payload={"text": "自定义机械检查",
                                     "check": {"type": "mechanical", "check_id": "made_up"}}))
    probs_v = OV.validate_invariants(gv)
    check(any("made_up" in p for p in probs_v),
          f"假机械 check_id 被 validate_invariants 拦截（{probs_v}）")
    gv.add(O.SemanticObject(id="inv:real", type=O.TYPE_INVARIANT, truth=O.TRUTH_DERIVED,
                            payload={"text": "冻结接口",
                                     "check": {"type": "mechanical", "check_id": "skeleton_conformance",
                                               "entry": "pipeline/verify.py:skeleton_conformance"}}))
    check(all("inv:real" not in p for p in OV.validate_invariants(gv)),
          "登记表内 check_id + entry ⇒ 放行")
    gv.add(O.SemanticObject(id="inv:prose", type=O.TYPE_INVARIANT, truth=O.TRUTH_DERIVED,
                            payload={"text": "保持优雅", "check": {"type": "prose"}}))
    check(all("inv:prose" not in p for p in OV.validate_invariants(gv)),
          "纯人话不变量不拦（没承诺机械兑现）")

    # ⑥ Schema Guardian：启动期登记表自洽为空
    check(O.schema_self_check() == [],
          "schema_self_check() 空（版本/predicate/verifier/证据集合/Invariant 绑定全自洽）",
          json.dumps(O.schema_self_check(), ensure_ascii=False))

    # ⑦ planir 端到端：骨架∩方案投影 freeze PO；invariant payload executable 标记如实
    from pipeline import planir as PQ  # noqa: E402
    plan_q = {
        "changes": [{"path": "store.py", "symbols": ["Store.save"], "intent": "保存收支流水"}],
        "tasks": [{"id": "T-01", "target_files": ["store.py"], "symbols": ["Store.save"]}],
        "invariants": [
            {"id": "interface_freeze", "text": "冻结接口不得在实现期漂移"},
            {"id": "", "text": "命名应保持清晰可读"},
            {"id": "x", "text": "自定义检查", "check": {"type": "mechanical", "check_id": "made_up_check"}},
        ],
    }
    ir_q = PQ.normalize_plan(plan_q, skeleton=skel_q, scope=_pm_scope(),
                             original_requirement="做一个命令行记账工具")
    g_ir = O.OntologyGraph.from_dict(ir_q["ontology"])
    iface_pos = [p for p in g_ir.obligations.values() if p.kind == O.PO_KIND_INTERFACE_FREEZE]
    check(len(iface_pos) == 2, f"normalize_plan 自动投影 2 条 freeze PO（{len(iface_pos)}）")
    inv_objs = {o.payload.get("text"): o.payload for o in g_ir.objects.values()
                if o.type == O.TYPE_INVARIANT}
    check(bool(inv_objs["冻结接口不得在实现期漂移"]["executable"])
          and inv_objs["冻结接口不得在实现期漂移"]["check"]["check_id"] == "skeleton_conformance",
          f"内置 invariant 标 executable 并绑真检查（{inv_objs['冻结接口不得在实现期漂移']}）")
    check(not inv_objs["命名应保持清晰可读"]["executable"]
          and inv_objs["命名应保持清晰可读"]["check"]["type"] == "prose",
          "纯人话 invariant 如实标 prose/executable=False")
    inv_problems = OV.validate_all(g_ir)["invariants"]
    check(any("made_up_check" in p for p in inv_problems),
          f"plan 里伪造的机械 check_id 经投影后被 validator 拦截（{inv_problems}）")

    print()
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAIL:
        print("Ontology smoke 未通过")
        return 1
    print("Ontology smoke 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
