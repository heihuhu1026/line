# -*- coding: utf-8 -*-
"""Phase 3（方案§十二~§十五）：Ontology Hard Gate 单测。

覆盖方案§十五的必测反例：
invalid relation / dangling evidence / invalid PROVEN evidence /
missing verifier / task without requirement → error 硬阻断；
并验证 ``ontology_problems != [] → can_pass == False`` 的 Proof Gate 降级规则，
以及 warning 不阻断、Problem 六字段结构。

直接运行：``python tests\\test_ontology_gate.py``
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import ontology as ont  # noqa: E402
from pipeline import ontology_validate as ov  # noqa: E402


def _requirement(g: ont.OntologyGraph, rid: str = "req:1") -> None:
    g.add(ont.SemanticObject(
        id=rid, type=ont.TYPE_REQUIREMENT, truth=ont.TRUTH_ASSERTED,
        payload={"text": "需求"},
        provenance=[ont.Provenance(source="user", stage="intake")],
    ))


def _clean_graph() -> ont.OntologyGraph:
    """一张通过全部结构化校验的最小图。"""
    g = ont.OntologyGraph()
    _requirement(g)
    g.add_obligation(ont.ProofObligation(
        id="po:1", name="行为可验证", requirement_id="req:1", claim="claim:1",
        kind=ont.PO_KIND_BEHAVIOR, required=True,
        verifier={"kind": "command", "command": "python -m unittest"},
    ))
    g.add(ont.SemanticObject(
        id="stask:1", type=ont.TYPE_TASK, truth=ont.TRUTH_DERIVED,
        payload={"target_files": ["main.py"]},
        provenance=[ont.Provenance(source="taskcompiler", stage="compile_plan")],
    ))
    g.relate("stask:1", "implements", "req:1", truth=ont.TRUTH_DERIVED)
    return g


def _proven_graph() -> ont.OntologyGraph:
    """一张所有 required PO 都 PROVEN、证据/revision 齐备的可放行图。"""
    g = _clean_graph()
    g.add_revision(ont.WorkspaceRevision(
        revision_id="wsr:1", workspace_id="ws:1", source="verify", status="VERIFIED",
    ))
    g.add_evidence(ont.EvidenceRecord(
        id="ev:1", kind="test_result", source="python -m unittest",
        status=ont.PO_STATUS_PROVEN, truth=ont.TRUTH_PROVEN,
        command="python -m unittest", exit_code=0,
        workspace_revision="wsr:1", proof_obligation_ids=["po:1"],
    ))
    po = g.obligations["po:1"]
    po.status = ont.PO_STATUS_PROVEN
    po.evidence_ids = ["ev:1"]
    return g


class TestOntologyHardGate(unittest.TestCase):
    def test_clean_graph_has_no_blocking_errors(self):
        problems = ov.validate_all_structured(_clean_graph())
        errors = ov.blocking_errors(problems)
        self.assertEqual(errors, [], f"干净图不应有 error：{[p.to_dict() for p in errors]}")

    def test_invalid_relation_blocks(self):
        g = _clean_graph()
        # 非法谓词无法经 relate() 写入（写入面本身拒绝）；它只可能来自旧产物反序列化。
        g.relations.append(ont.OntologyRelation(
            id="rel:bad", subject="stask:1", predicate="not_a_registered_predicate",
            object="req:1",
        ))
        codes = {p.code for p in ov.validate_relations_structured(g)}
        self.assertIn("illegal_predicate", codes)
        self.assertTrue(ov.blocking_errors(ov.validate_all_structured(g)))

    def test_dangling_evidence_revision_blocks(self):
        g = _clean_graph()
        g.add_evidence(ont.EvidenceRecord(
            id="ev:dangle", kind="test_result", source="python -m unittest",
            status=ont.PO_STATUS_PROVEN, truth=ont.TRUTH_PROVEN,
            command="python -m unittest", exit_code=0,
            workspace_revision="wsr:does_not_exist",
        ))
        codes = {p.code for p in ov.validate_evidence_structured(g)}
        self.assertIn("evidence_revision_missing", codes)

    def test_invalid_proven_evidence_without_execution_blocks(self):
        g = _clean_graph()
        g.add_evidence(ont.EvidenceRecord(
            id="ev:nofootprint", kind="test_result", source="",
            status=ont.PO_STATUS_PROVEN, truth=ont.TRUTH_PROVEN,
        ))
        codes = {p.code for p in ov.validate_evidence_structured(g)}
        self.assertIn("proven_without_execution", codes)

    def test_required_po_without_verifier_blocks(self):
        g = _clean_graph()
        g.add_obligation(ont.ProofObligation(
            id="po:noverify", name="无验证器义务", requirement_id="req:1", claim="claim:x",
            required=True, verifier={},
        ))
        codes = {p.code for p in ov.validate_obligations_structured(g)}
        self.assertIn("required_without_verifier", codes)

    def test_task_without_requirement_blocks(self):
        g = _clean_graph()
        g.add(ont.SemanticObject(
            id="stask:orphan", type=ont.TYPE_TASK, truth=ont.TRUTH_DERIVED,
            payload={"target_files": ["other.py"]},
        ))
        codes = {p.code for p in ov.validate_obligations_structured(g)}
        self.assertIn("task_without_requirement", codes)

    def test_problem_shape_and_warning_does_not_block(self):
        g = _clean_graph()
        # 造一个只有 warning 的情形：required PO 不锚任何 Requirement。
        g.add_obligation(ont.ProofObligation(
            id="po:wandering", name="无锚义务", requirement_id="", claim="",
            required=True, verifier={"kind": "command"},
        ))
        audit = ov.semantic_integrity_audit(g, {})
        self.assertTrue(any(p.code == "po_orphan" and p.severity == ov.SEVERITY_WARNING
                            for p in audit))
        self.assertEqual(ov.blocking_errors({"semantic_integrity": audit}), [])
        # 六字段结构（方案§十四）。
        self.assertEqual(
            set(audit[0].to_dict().keys()),
            {"code", "severity", "message", "source", "object_id", "relation_id"},
        )

    def test_claim_contradiction_blocks_until_human_adjudication(self):
        g = _clean_graph()
        for cid, pol, source in (("claim:a", 1, "pm"), ("claim:b", -1, "pm")):
            g.add(ont.SemanticObject(
                id=cid, type=ont.TYPE_CLAIM, truth=ont.TRUTH_DERIVED,
                payload={"subject": "feat:X", "polarity": pol},
                provenance=[ont.Provenance(source=source, stage="pm")],
            ))
        self.assertIn("claim_contradiction",
                      {p.code for p in ov.validate_contradictions_structured(g)})
        # 有人工裁决后放行该主体。
        g.add(ont.SemanticObject(
            id="dec:human:1", type=ont.TYPE_DECISION, truth=ont.TRUTH_ASSERTED,
            payload={"verdict": "adjudicated", "adjudicated_subjects": ["feat:X"]},
            provenance=[ont.Provenance(source="human", stage="review")],
        ))
        self.assertNotIn("claim_contradiction",
                         {p.code for p in ov.validate_contradictions_structured(g)})

    def test_gate_problem_forces_can_pass_false(self):
        # 无问题时：机械证据齐备 ⇒ PROVEN / can_pass=True。
        g_ok = _proven_graph()
        self.assertEqual(ov.blocking_errors(ov.validate_all_structured(g_ok)), [])
        proof_ok = ont.release_proof_status(
            semantic_pass=True, verify_verdict="pass",
            obligations=list(g_ok.obligations.values()), workspace_verified=True,
        )
        self.assertEqual(proof_ok["status"], "PROVEN")
        self.assertTrue(proof_ok["can_pass"])

        # 同一图注入一条非法关系 ⇒ 按 _proof_gate 的降级规则必须 NOT PASS。
        g_bad = _proven_graph()
        g_bad.relations.append(ont.OntologyRelation(
            id="rel:bad", subject="stask:1", predicate="not_a_registered_predicate",
            object="req:1",
        ))
        errors = ov.blocking_errors(ov.validate_all_structured(g_bad))
        self.assertTrue(errors)
        proof_bad = ont.release_proof_status(
            semantic_pass=True, verify_verdict="pass",
            obligations=list(g_bad.obligations.values()), workspace_verified=True,
        )
        proof_bad["status"] = "FAILED"
        proof_bad["can_pass"] = False
        proof_bad["code"] = "ontology_integrity_error"
        proof_bad["failed"] = list(proof_bad.get("failed") or []) + [
            f"ontology_integrity[{p.source}/{p.code}]：{p.message}" for p in errors
        ]
        self.assertFalse(proof_bad["can_pass"])
        self.assertEqual(proof_bad["status"], "FAILED")
        self.assertTrue(
            any("ontology_integrity" in line for line in proof_bad["failed"])
        )


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestOntologyHardGate)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
