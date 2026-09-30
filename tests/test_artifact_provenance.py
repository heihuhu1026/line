# -*- coding: utf-8 -*-
"""Phase 7（方案§三十~§三十三）：Artifact provenance 链 + 需求矛盾 Claim 投影单测。

直接运行：``python tests\\test_artifact_provenance.py``
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import ontology as O  # noqa: E402
from pipeline import ontology_validate as OV  # noqa: E402


def _env(aid: str, stage: str, *, rev: int = 1, caused_by=None, supersedes=None,
         truth: str = O.TRUTH_DERIVED) -> dict:
    return {
        "artifact_id": aid, "stage": stage, "produced_by": stage,
        "revision": rev, "caused_by": caused_by or [], "supersedes": supersedes or [],
        "input_hash": "h:in", "output_hash": "h:out", "truth": truth,
        "created_at": "2026-09-29 12:00:00",
    }


def _fact(text: str, polarity: int, *, source: str = "pm", truth: str = "DERIVED",
          where: str = "") -> dict:
    return {"id": text[:6], "text": text, "where": where,
            "source": source, "truth": truth, "polarity": polarity}


class TestArtifactChain(unittest.TestCase):
    def _envelopes(self) -> list[dict]:
        return [
            _env("01-intake", "intake"),
            _env("02-pm", "pm", caused_by=["01-intake"]),
            _env("03-plan", "architect_plan", caused_by=["02-pm"]),
            _env("04-plan", "architect_plan", rev=2,
                 caused_by=["02-pm"], supersedes=["03-plan"]),
            _env("05-dev", "dev", caused_by=["04-plan"]),
            # ghost-dev 尚不存在 → derived_from 边不得悬空
            _env("06-verify", "verify", caused_by=["05-dev", "ghost-dev"]),
        ]

    def test_derived_from_and_supersedes_chain(self):
        g = O.OntologyGraph()
        count = O.project_artifact_chain(g, self._envelopes())
        self.assertEqual(count, {"artifacts": 6, "derived_from": 5, "supersedes": 1})
        triples = {(r.subject, r.predicate, r.object) for r in g.relations}
        self.assertIn(("06-verify", "derived_from", "05-dev"), triples)
        self.assertIn(("04-plan", "supersedes", "03-plan"), triples)
        self.assertFalse(any(o == "ghost-dev" for _, _, o in triples))
        # 不重复造 hash：envelope 里的内容指纹原样落到 payload。
        self.assertEqual(g.objects["06-verify"].payload["output_hash"], "h:out")

    def test_idempotent(self):
        g = O.OntologyGraph()
        O.project_artifact_chain(g, self._envelopes())
        n_obj, n_rel = len(g.objects), len(g.relations)
        count = O.project_artifact_chain(g, self._envelopes())
        self.assertEqual(count, {"artifacts": 0, "derived_from": 0, "supersedes": 0})
        self.assertEqual((len(g.objects), len(g.relations)), (n_obj, n_rel))

    def test_human_review_asserted_artifact_is_valid(self):
        g = O.OntologyGraph()
        O.project_artifact_chain(g, [
            _env("07-review", "review"),
            _env("08-human", "human_review", rev=1, caused_by=["07-review"],
                 truth=O.TRUTH_ASSERTED),
        ])
        self.assertEqual(g.objects["08-human"].truth, O.TRUTH_ASSERTED)
        self.assertEqual(OV.blocking_errors(OV.validate_all_structured(g)), [])

    def test_empty_and_malformed_are_safe(self):
        g = O.OntologyGraph()
        self.assertEqual(
            O.project_artifact_chain(g, [None, {}, {"artifact_id": ""}]),
            {"artifacts": 0, "derived_from": 0, "supersedes": 0},
        )


class TestConflictClaims(unittest.TestCase):
    def test_same_level_conflict_blocks(self):
        g = O.OntologyGraph()
        group = [
            _fact("删除后序号会自动重置", 1, where="acceptance_criteria[2]"),
            _fact("删除后序号不会自动重置", -1, where="open_questions.assumed_answer"),
        ]
        n = O.project_conflict_claims(g, [group])
        self.assertEqual(n, 2)
        codes = {p.code for p in OV.validate_contradictions_structured(g)}
        self.assertIn("claim_contradiction", codes)
        # 硬阻断：blocking_errors 收得到。
        self.assertTrue(OV.blocking_errors(OV.validate_all_structured(g)))

    def test_human_overrides_pm_no_contradiction(self):
        g = O.OntologyGraph()
        group = [
            _fact("删除记录后序号保持不变不重置", 1, source="human", truth="ASSERTED",
                  where="open_questions.final_decision"),
            _fact("删除后序号会自动重新排序重置", -1, where="acceptance_criteria[2]"),
        ]
        O.project_conflict_claims(g, [group])
        codes = {p.code for p in OV.validate_contradictions_structured(g)}
        self.assertNotIn("claim_contradiction", codes)

    def test_projection_idempotent(self):
        g = O.OntologyGraph()
        group = [
            _fact("窗口可以调整大小", 1),
            _fact("窗口不可以调整大小", -1),
        ]
        self.assertEqual(O.project_conflict_claims(g, [group]), 2)
        self.assertEqual(O.project_conflict_claims(g, [group]), 0)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([
        loader.loadTestsFromTestCase(TestArtifactChain),
        loader.loadTestsFromTestCase(TestConflictClaims),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
