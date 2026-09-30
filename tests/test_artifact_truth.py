# -*- coding: utf-8 -*-
"""Phase 4（方案§十六~§十八）：Artifact truth 与 negative_control 合法化单测。

核心规则：
* ``negative_control`` 是**执行类**证据 —— 每条必须带 command/exit_code/source/
  workspace_revision；撤掉改动仍过（无判别力）⇒ FAILED；撤掉后变红 ⇒ PROVEN；
* 旧版报告（只有 checked/no_power 计数、没有 runs 痕迹）缺省安全：不产生证据，
  绝不无痕迹标 PROVEN（旧 run 可读）；
* 合法化后的证据必须通过 evidence 校验器（无 proven_without_execution 等 error）。

直接运行：``python tests\\test_artifact_truth.py``
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import ontology as O  # noqa: E402
from pipeline import ontology_validate as OV  # noqa: E402

ASSERT_CMD = f"{sys.executable} -c \"assert 1+1==2\""


def _po(po_id: str = "po:req1:behav") -> O.ProofObligation:
    return O.ProofObligation(
        id=po_id, name=po_id, requirement_id="req-1", claim=po_id,
        kind=O.PO_KIND_BEHAVIOR, required=True,
        verifier={"kind": "command", "command": ASSERT_CMD},
    )


def _evaluate(control: dict) -> O.OntologyGraph:
    graph = O.OntologyGraph()
    graph.add(O.SemanticObject(
        id="req-1", type=O.TYPE_REQUIREMENT, truth=O.TRUTH_ASSERTED,
        provenance=[O.Provenance(source="user", stage="intake")],
    ))
    graph.add_obligation(_po())
    report = {
        "verdict": "pass",
        "commands": [
            {"command": ASSERT_CMD, "status": "ok", "exit_code": 0,
             "stdout_tail": "", "stderr_tail": ""},
        ],
        "negative_control": control,
    }
    return O.evaluate_against_verify(graph, report)


def _nc_evidence(graph: O.OntologyGraph) -> list[O.EvidenceRecord]:
    return [ev for ev in graph.evidence.values() if ev.kind == "negative_control"]


class TestNegativeControlLegitimacy(unittest.TestCase):
    def test_no_power_rerun_is_failed_with_full_footprint(self):
        g = _evaluate({
            "checked": 1,
            "no_power": [ASSERT_CMD],
            "runs": [{"command": ASSERT_CMD, "exit_code": 0, "status": "ok"}],
        })
        evs = _nc_evidence(g)
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        # 撤掉改动断言仍过 ⇒ 无判别力，证据 FAILED，但事实本身是机械产生的。
        self.assertEqual(ev.status, O.PO_STATUS_FAILED)
        self.assertEqual(ev.truth, O.TRUTH_PROVEN)
        self.assertEqual(ev.command, ASSERT_CMD)
        self.assertEqual(ev.exit_code, 0)
        self.assertTrue(ev.source)
        self.assertTrue(ev.workspace_revision)
        # 带完整执行痕迹 ⇒ 校验器不得报 proven_without_execution / 任何 error。
        errors = OV.blocking_errors(OV.validate_all_structured(g))
        self.assertEqual(errors, [], [p.to_dict() for p in errors])

    def test_red_rerun_is_proven(self):
        g = _evaluate({
            "checked": 1,
            "no_power": [],
            "runs": [{"command": ASSERT_CMD, "exit_code": 1, "status": "fail"}],
        })
        evs = _nc_evidence(g)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].status, O.PO_STATUS_PROVEN)
        self.assertEqual(evs[0].exit_code, 1)
        self.assertEqual(OV.blocking_errors(OV.validate_all_structured(g)), [])

    def test_legacy_report_without_runs_creates_no_evidence(self):
        # 旧产物：只有计数没有执行痕迹 —— 缺省安全，不造无痕迹 PROVEN 证据。
        g = _evaluate({"checked": 1, "no_power": [ASSERT_CMD]})
        self.assertEqual(_nc_evidence(g), [])

    def test_non_executed_run_row_is_ignored(self):
        # 被安全拒绝（无 exit_code）的重跑行不能充当执行证据。
        g = _evaluate({
            "checked": 1, "no_power": [],
            "runs": [{"command": ASSERT_CMD, "exit_code": None, "status": "unavailable"}],
        })
        self.assertEqual(_nc_evidence(g), [])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestNegativeControlLegitimacy)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
