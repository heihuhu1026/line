"""Phase 1 P0 Proof Binding 单测 —— 方案§三 3.3 证据显式绑定六规则。

覆盖：
* Case A：断言证据只绑定 PO-A ⇒ A=PROVEN / 未绑定的 PO-B=UNPROVEN（禁止串证）；
* Case B：绑定到 PO 的失败证据 ⇒ FAILED（未绑定者不得判 FAILED）；
* Case C：证据无 proof_obligation_ids ⇒ 不能证明任何 required PO（纯函数 + 集成两路径）；
* Case D：证据绑定旧 revision、head_revision 不一致 ⇒ 不能证明当前 PO；
* 另含 kind 匹配（rc==0 的 command_result 绑 behavior PO 不成立）与
  无任何绑定来源时的旧行为兼容路径（pass+断言 ⇒ 行为族全 PROVEN）。

直接运行：``python tests/test_ontology_proof_binding.py``（退出码 0/1）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import ontology as O  # noqa: E402

TEST_CMD = f"{sys.executable} -m unittest test_main"
SMOKE_CMD = f"{sys.executable} main.py"


def _po(po_id: str, name: str = "", kind: str = O.PO_KIND_BEHAVIOR) -> O.ProofObligation:
    return O.ProofObligation(
        id=po_id, name=name or po_id, requirement_id="req-1", claim=name or po_id,
        kind=kind, required=True,
    )


def _verify_report(*commands: dict) -> dict:
    return {"verdict": "pass", "commands": list(commands)}


def _cmd(command: str, status: str = "ok", exit_code: int = 0, **extra) -> dict:
    item = {"command": command, "status": status, "exit_code": exit_code,
             "stdout_tail": "ok", "stderr_tail": ""}
    item.update(extra)
    return item


def _eval_with(pos: list[O.ProofObligation], report: dict, **kwargs) -> O.OntologyGraph:
    graph = O.OntologyGraph()
    for po in pos:
        graph.add_obligation(po)
    return O.evaluate_against_verify(graph, report, **kwargs)


class TestEvidencePoHelpers(unittest.TestCase):
    """纯函数 evidence_proves_po / evidence_fails_po / evidence_po_bound。"""

    def _ev(self, *, po_ids, kind="test_result", status=O.PO_STATUS_PROVEN,
              rev="wsr:verify:cur", exit_code=0):
        return O.EvidenceRecord(
            id=f"ev:{kind}:x", kind=kind, source=TEST_CMD, status=status,
            truth=O.TRUTH_PROVEN, proof_obligation_ids=list(po_ids),
            workspace_revision=rev, command=TEST_CMD, exit_code=exit_code,
        )

    def test_case_c_unbound_evidence_proves_nothing(self):
        po = _po("po:req1:behav")
        ev = self._ev(po_ids=[])
        self.assertFalse(O.evidence_po_bound(ev, po))
        self.assertFalse(O.evidence_proves_po(ev, po, head_revision="wsr:verify:cur"))

    def test_case_d_stale_revision_not_proven(self):
        po = _po("po:req1:behav")
        ev = self._ev(po_ids=["po:req1:behav"], rev="wsr:verify:old")
        self.assertTrue(O.evidence_po_bound(ev, po))  # 绑定与 kind 仍成立
        self.assertFalse(O.evidence_proves_po(ev, po, head_revision="wsr:verify:cur"))
        # 不要求 revision 时（无 head）才成立 —— 证明规则差异确实来自 revision
        self.assertTrue(O.evidence_proves_po(ev, po))

    def test_kind_mismatch_smoke_cannot_prove_behavior(self):
        po = _po("po:req1:behav", kind=O.PO_KIND_BEHAVIOR)
        ev = self._ev(po_ids=["po:req1:behav"], kind="command_result")
        self.assertFalse(O.evidence_po_bound(ev, po))
        self.assertFalse(O.evidence_proves_po(ev, po))

    def test_derived_truth_cannot_prove(self):
        po = _po("po:req1:behav")
        ev = self._ev(po_ids=["po:req1:behav"])
        ev.truth = O.TRUTH_DERIVED
        self.assertFalse(O.evidence_proves_po(ev, po))

    def test_bound_failed_evidence_fails_po(self):
        po = _po("po:req1:behav")
        ev = self._ev(po_ids=["po:req1:behav"], status=O.PO_STATUS_FAILED, exit_code=1)
        self.assertTrue(O.evidence_fails_po(ev, po))
        self.assertFalse(O.evidence_proves_po(ev, po))
        # 未绑定到该 PO 的失败证据不能判它 FAILED
        other = _po("po:req1:other")
        self.assertFalse(O.evidence_fails_po(ev, other))


class TestEvaluateStrictBinding(unittest.TestCase):
    """evaluate_against_verify 集成：显式绑定存在时走严格路径。"""

    def test_case_a_explicit_binding_no_cross_proof(self):
        report = _verify_report(_cmd(TEST_CMD, target_po="po:req1:a"))
        g = _eval_with([_po("po:req1:a"), _po("po:req1:b")], report)
        self.assertEqual(g.obligations["po:req1:a"].status, O.PO_STATUS_PROVEN)
        self.assertEqual(g.obligations["po:req1:b"].status, O.PO_STATUS_UNPROVEN)
        # 证据对象上只留显式绑定
        ev = g.evidence[g.obligations["po:req1:a"].evidence_ids[0]]
        self.assertEqual(ev.proof_obligation_ids, ["po:req1:a"])

    def test_case_a2_bindings_param_equivalent(self):
        report = _verify_report(_cmd(TEST_CMD))
        g = _eval_with(
            [_po("po:req1:a"), _po("po:req1:b")], report,
            command_po_bindings={TEST_CMD: ["po:req1:a", "po:req1:b"]},
        )
        self.assertEqual(g.obligations["po:req1:a"].status, O.PO_STATUS_PROVEN)
        self.assertEqual(g.obligations["po:req1:b"].status, O.PO_STATUS_PROVEN)

    def test_case_b_bound_failure_fails_only_bound_po(self):
        report = _verify_report(
            _cmd(TEST_CMD, status="fail", exit_code=1, target_po_ids=["po:req1:a"]))
        report["verdict"] = "fail"
        g = _eval_with([_po("po:req1:a"), _po("po:req1:b")], report)
        self.assertEqual(g.obligations["po:req1:a"].status, O.PO_STATUS_FAILED)
        self.assertEqual(g.obligations["po:req1:b"].status, O.PO_STATUS_UNPROVEN)

    def test_case_c_passing_test_without_binding_unproven(self):
        # 同一轮里存在显式绑定来源（冒烟命令绑了别的 PO），未绑定 PO 不得被串证。
        report = _verify_report(
            _cmd(TEST_CMD, target_po="po:req1:a"),
            _cmd(SMOKE_CMD, target_po="po:req1:a"),
        )
        g = _eval_with([_po("po:req1:a"), _po("po:req1:behav")], report)
        self.assertEqual(g.obligations["po:req1:a"].status, O.PO_STATUS_PROVEN)
        self.assertEqual(g.obligations["po:req1:behav"].status, O.PO_STATUS_UNPROVEN)

    def test_smoke_command_bound_to_behavior_po_not_proven(self):
        # rc==0 的入口冒烟（command_result）即使显式绑给 behavior PO 也不能证明业务行为。
        report = _verify_report(_cmd(SMOKE_CMD, target_po="po:req1:a"))
        g = _eval_with([_po("po:req1:a")], report)
        self.assertEqual(g.obligations["po:req1:a"].status, O.PO_STATUS_UNPROVEN)


class TestEvaluateLegacyPath(unittest.TestCase):
    """无任何绑定来源时必须保留旧行为（smoke_ontology L 段与旧 run 依赖）。"""

    def test_legacy_pass_assertion_proves_all_behavior_pos(self):
        report = _verify_report(_cmd(TEST_CMD))  # 无 target_po 字段、无 bindings 入参
        g = _eval_with([_po("po:req1:a"), _po("po:req1:b")], report)
        self.assertEqual(g.obligations["po:req1:a"].status, O.PO_STATUS_PROVEN)
        self.assertEqual(g.obligations["po:req1:b"].status, O.PO_STATUS_PROVEN)

    def test_legacy_smoke_only_stays_unproven(self):
        report = _verify_report(_cmd(SMOKE_CMD))
        g = _eval_with([_po("po:req1:a")], report)
        self.assertEqual(g.obligations["po:req1:a"].status, O.PO_STATUS_UNPROVEN)


if __name__ == "__main__":
    _program = unittest.main(verbosity=2, exit=False)
    sys.exit(0 if _program.result.wasSuccessful() else 1)
