# -*- coding: utf-8 -*-
"""方案§三十/§三十一：确定性 Task Plan 的 Artifact envelope 接线单测。

直接运行：``python tests\\test_task_plan_artifact.py``
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import ontology as O  # noqa: E402
from pipeline import ontology_validate as OV  # noqa: E402
from pipeline.ollama_client import MockClient  # noqa: E402
from pipeline.orchestrator import Orchestrator  # noqa: E402


class TestTaskPlanArtifact(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name) / "20260101-000000"
        self.run_dir.mkdir()
        self.orch = Orchestrator(
            client=MockClient(), runs_dir=self._tmp.name,
            unload_at_end=False, log=lambda _msg: None,
        )
        self.orch.run_dir = self.run_dir
        # 模拟上游已有一版 Architect Plan 快照（_record 已登记 chain）。
        self.orch.state["artifact_chain"] = {"architect_plan": "07-architect_plan"}
        self.plan_env = {
            "artifact_id": "07-architect_plan", "stage": "architect_plan",
            "produced_by": "architect_plan", "revision": 1,
            "supersedes": [], "input_hash": "h:in", "output_hash": "h:plan",
            "caused_by": ["02-pm"], "truth": O.TRUTH_DERIVED,
            "created_at": "2026-09-29 12:00:00",
        }
        self.orch.state["artifact_log"] = [dict(self.plan_env)]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _task_plan_envs(self) -> list[dict]:
        return [e for e in self.orch.state["artifact_log"] if e.get("stage") == "task_plan"]

    def test_envelope_derived_from_architect_plan(self) -> None:
        """首版：revision=1、caused_by 指向 architect_plan、DERIVED，并落阶段快照。"""
        self.orch._record_task_plan({"v": 1}, [{"id": "T-01"}], [])
        envs = self._task_plan_envs()
        self.assertEqual(len(envs), 1)
        env = envs[0]
        self.assertEqual(env["revision"], 1)
        self.assertEqual(env["caused_by"], ["07-architect_plan"])
        self.assertEqual(env["supersedes"], [])
        self.assertEqual(env["truth"], O.TRUTH_DERIVED)
        self.assertTrue(env["artifact_id"].endswith("-task_plan"))
        # 阶段快照落盘（确定性编译也可逐版回放），且不写模型调用台账。
        snap = self.run_dir / f"{env['artifact_id']}.json"
        self.assertTrue(snap.is_file())
        self.assertFalse((self.run_dir / "llm-calls.jsonl").exists())
        saved = __import__("json").loads(snap.read_text(encoding="utf-8"))
        self.assertEqual(saved["meta"]["kind"], "deterministic")
        self.assertEqual(saved["artifact"]["tasks"], [{"id": "T-01"}])

    def test_identical_recompile_is_same_version(self) -> None:
        """同输入确定性重算输出逐字相同 ⇒ 不新增 envelope/快照（幂等，不制造版本）。"""
        self.orch._record_task_plan({"v": 1}, [{"id": "T-01"}], [])
        seq_after_first = self.orch._seq
        self.orch._record_task_plan({"v": 1}, [{"id": "T-01"}], [])
        self.assertEqual(len(self._task_plan_envs()), 1)
        self.assertEqual(self.orch._seq, seq_after_first)

    def test_changed_output_supersedes_previous(self) -> None:
        """输出变化（施工图改版）⇒ revision=2 且 supersedes 指向上一版。"""
        self.orch._record_task_plan({"v": 1}, [{"id": "T-01"}], [])
        first_id = self._task_plan_envs()[0]["artifact_id"]
        self.orch._record_task_plan({"v": 2}, [{"id": "T-01"}, {"id": "T-02"}], [])
        envs = self._task_plan_envs()
        self.assertEqual(len(envs), 2)
        self.assertEqual(envs[1]["revision"], 2)
        self.assertEqual(envs[1]["supersedes"], [first_id])
        self.assertEqual(envs[1]["caused_by"], ["07-architect_plan"])

    def test_projection_chain_and_validate_clean(self) -> None:
        """投影：Task Plan derived_from Architect Plan、再版 supersedes，校验零 error。"""
        self.orch._record_task_plan({"v": 1}, [{"id": "T-01"}], [])
        self.orch._record_task_plan({"v": 2}, [{"id": "T-01"}, {"id": "T-02"}], [])
        g = O.OntologyGraph()
        count = O.project_artifact_chain(g, self.orch.state["artifact_log"])
        envs = self._task_plan_envs()
        self.assertEqual(count["artifacts"], 3)  # architect_plan + 两版 task_plan
        # plan 的上游 02-pm 不在图上不计边；两版 Task Plan 各 derived_from plan = 2
        self.assertEqual(count["derived_from"], 2)
        self.assertEqual(count["supersedes"], 1)
        triples = {(r.subject, r.predicate, r.object) for r in g.relations}
        self.assertIn((envs[0]["artifact_id"], "derived_from", "07-architect_plan"), triples)
        self.assertIn((envs[1]["artifact_id"], "supersedes", envs[0]["artifact_id"]), triples)
        self.assertEqual(OV.blocking_errors(OV.validate_all_structured(g)), [])

    def test_run_dir_none_is_safe(self) -> None:
        """未落运行目录（异常路径）调用安全无操作。"""
        self.orch.run_dir = None
        self.orch._record_task_plan({"v": 1}, [{"id": "T-01"}], [])
        self.assertEqual(self._task_plan_envs(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
