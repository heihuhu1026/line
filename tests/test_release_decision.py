# -*- coding: utf-8 -*-
"""Phase 6（方案§二十六~§二十八）：can_release 唯一放行裁决 + Decision 对象投影单测。

直接运行：``python tests\\test_release_decision.py``
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import ontology as O  # noqa: E402
from pipeline import ontology_validate as OV  # noqa: E402


def _proven_graph() -> O.OntologyGraph:
    """Requirement + required behavior PO + verify pass（断言命令显式绑定该 PO）。"""
    g = O.OntologyGraph()
    g.add(O.SemanticObject(
        id="req:1", type=O.TYPE_REQUIREMENT, truth=O.TRUTH_ASSERTED,
        provenance=[O.Provenance(source="user", stage="intake")],
    ))
    g.add_obligation(O.ProofObligation(
        id="po:1", name="程序行为符合需求", claim="claim:po1", required=True,
        kind=O.PO_KIND_BEHAVIOR, requirement_id="req:1",
        verifier={"kind": "command", "command": "python test_main.py"},
    ))
    O.evaluate_against_verify(g, {
        "verdict": "pass",
        "commands": [{
            "command": "python test_main.py", "status": "ok", "exit_code": 0,
            "stdout_tail": "ok", "stderr_tail": "", "target_po_ids": ["po:1"],
        }],
    })
    return g


def _proven_proof(g: O.OntologyGraph) -> dict:
    return O.release_proof_status(
        semantic_pass=True, verify_verdict="pass",
        obligations=list(g.obligations.values()), workspace_verified=True,
    )


class TestCanRelease(unittest.TestCase):
    def test_all_green_releases(self):
        g = _proven_graph()
        gate = O.can_release(proof_status=_proven_proof(g), review_verdict="pass", graph=g)
        self.assertTrue(gate["can_pass"], gate["blocking_reasons"])
        self.assertEqual(gate["verdict"], "pass")
        self.assertEqual(gate["status"], "PROVEN")
        self.assertTrue(gate["verified_revision"].startswith("wsr:verify:"))

    def test_blocks_on_semantic_verdict(self):
        g = _proven_graph()
        gate = O.can_release(proof_status=_proven_proof(g), review_verdict="rework_dev", graph=g)
        self.assertFalse(gate["can_pass"])
        self.assertTrue(any("semantic_review_not_pass" in r for r in gate["blocking_reasons"]))

    def test_blocks_on_unproven_proof(self):
        gate = O.can_release(
            proof_status={"status": "UNPROVEN", "failed": [],
                          "mandatory_missing": ["verify_skipped：运行验证被跳过"]},
            review_verdict="pass", verified_workspace_revision="wsr:verify:x",
        )
        self.assertFalse(gate["can_pass"])
        self.assertEqual(gate["status"], "UNPROVEN")
        self.assertTrue(any("proof_unproven" in r for r in gate["blocking_reasons"]))

    def test_blocks_without_verified_workspace(self):
        gate = O.can_release(
            proof_status={"status": "PROVEN", "failed": [], "mandatory_missing": []},
            review_verdict="pass",
        )
        self.assertFalse(gate["can_pass"])
        self.assertTrue(any("no_verified_workspace" in r for r in gate["blocking_reasons"]))

    def test_none_proof_is_blocked(self):
        gate = O.can_release(proof_status=None, review_verdict="pass",
                             verified_workspace_revision="wsr:verify:x")
        self.assertFalse(gate["can_pass"])


class TestDecisionProjection(unittest.TestCase):
    def test_pass_decision_closes_loop_and_passes_validators(self):
        g = _proven_graph()
        basis = O.release_basis(g)
        self.assertTrue(basis["evidence_ids"], "PROVEN 证据必须被 release_basis 收齐")
        did = O.project_decision(
            g, verdict="pass", reason="机械证据全绿 + 语义 pass",
            revision=basis["revision"], evidence_ids=basis["evidence_ids"],
            round_no=1,
        )
        triples = {(r.subject, r.predicate, r.object) for r in g.relations}
        ev_id = basis["evidence_ids"][0]
        self.assertIn((did, "based_on", ev_id), triples)
        self.assertIn((did, "applies_to", basis["revision"]), triples)
        # 全量语义校验无 error（闭环：Decision 有证据、证据绑链头、required PO PROVEN）。
        self.assertEqual(OV.blocking_errors(OV.validate_all_structured(g)), [])
        audit = {p.code for p in OV.semantic_integrity_audit(g)}
        self.assertNotIn("decision_without_evidence", audit)

    def test_resolves_only_existing_defects_and_idempotent(self):
        g = _proven_graph()
        g.add(O.SemanticObject(
            id="def:open1", type=O.TYPE_DEFECT, truth=O.TRUTH_DERIVED, status="OPEN",
            provenance=[O.Provenance(source="defect_ledger", stage="review")],
        ))
        basis = O.release_basis(g)
        kw = dict(verdict="pass", revision=basis["revision"],
                  evidence_ids=basis["evidence_ids"], round_no=1)
        did = O.project_decision(g, reason="", defect_ids=["def:open1", "def:ghost"], **kw)
        triples = {(r.subject, r.predicate, r.object) for r in g.relations}
        self.assertIn((did, "resolves", "def:open1"), triples)
        self.assertFalse(any(o == "def:ghost" for _, _, o in triples), "不造悬空 Defect 端点")
        n_rel, n_obj = len(g.relations), len(g.objects)
        O.project_decision(g, reason="改了理由", defect_ids=["def:open1"], **kw)
        self.assertEqual(len(g.relations), n_rel)
        self.assertEqual(len(g.objects), n_obj)
        self.assertEqual(g.objects[did].payload["reason"], "改了理由")

    def test_pass_decision_without_evidence_is_error(self):
        g = _proven_graph()
        basis = O.release_basis(g)
        O.project_decision(g, verdict="pass", revision=basis["revision"], round_no=2)
        codes = {p.code for p in OV.semantic_integrity_audit(g)}
        self.assertIn("decision_without_evidence", codes)

    def test_rework_decision_needs_no_evidence(self):
        g = _proven_graph()
        # rework 裁决无 based_on 不报错；挂了不存在端点也不造边。
        O.project_decision(g, verdict="rework", reason="verify skipped", round_no=1)
        self.assertEqual(OV.blocking_errors(OV.validate_all_structured(g)), [])


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([
        loader.loadTestsFromTestCase(TestCanRelease),
        loader.loadTestsFromTestCase(TestDecisionProjection),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
