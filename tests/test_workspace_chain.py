# -*- coding: utf-8 -*-
"""Phase 5（方案§十九~§二十五）：Task→Patch→Symbol→WorkspaceRevision 链投影单测。

验证：
* 多个已提交事务形成真实父子链（parent = 上一任务 result）；
* Patch materialized_in revision、Patch changes Symbol、Task owns Symbol 三链闭合；
* verify revision 挂在在制链头之下且仍是 head（证据只认 head，不被误判 stale）；
* 投影幂等（对象/关系不重复）；
* semantic_integrity_audit 的事务对齐规则：缺 revision / parent 不一致 / 补丁脱链；
* 旧事务（无 parent/patches 字段）缺省安全，不造悬空端点。

直接运行：``python tests\\test_workspace_chain.py``
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import ontology as O  # noqa: E402
from pipeline import ontology_validate as OV  # noqa: E402


def _task(graph: O.OntologyGraph, tid: str = "stask:1", files=("main.py",)) -> None:
    graph.add(O.SemanticObject(
        id="req:1", type=O.TYPE_REQUIREMENT, truth=O.TRUTH_ASSERTED,
        provenance=[O.Provenance(source="user", stage="intake")],
    ))
    graph.add(O.SemanticObject(
        id=tid, type=O.TYPE_TASK, truth=O.TRUTH_DERIVED,
        payload={"target_files": list(files), "symbols": []},
        provenance=[O.Provenance(source="taskcompiler", stage="compile_plan")],
    ))
    graph.relate(tid, "implements", "req:1")


def _txn(seq: int, *, tid="stask:1", round_no=1, parent="", base="",
         status="committed", patches=None, result=None) -> dict:
    return {
        "at": f"2026-09-29 10:00:0{seq}",
        "round": round_no,
        "task": f"T{seq}",
        "task_semantic_id": tid,
        "patch_id": f"patch:digest{seq}",
        "base_workspace_revision": f"{base}+prior:x" if base else "",
        "base_manifest": base,
        "parent_workspace_revision": parent,
        "result_workspace_revision": result or f"wsr:wip:r{seq:08d}",
        "patches": patches if patches is not None else [
            {"path": "main.py", "symbol": "run", "change_type": "add", "patch_mode": "new_file"},
        ],
        "status": status,
    }


def _chain(txns, *, with_task=True) -> O.OntologyGraph:
    g = O.OntologyGraph()
    if with_task:
        _task(g)
    O.project_workspace_chain(g, txns, at="1")
    return g


class TestWorkspaceChainProjection(unittest.TestCase):
    def test_revisions_form_real_parent_chain(self):
        t1 = _txn(1, base="wsr:wip:base0000")
        t2 = _txn(2, parent=t1["result_workspace_revision"])
        g = _chain([t1, t2])
        r1 = g.revisions[t1["result_workspace_revision"]]
        r2 = g.revisions[t2["result_workspace_revision"]]
        # 根基线 → r1 → r2。
        self.assertEqual(r1.parent_revision, "wsr:wip:base0000")
        self.assertEqual(g.revisions["wsr:wip:base0000"].source, "base")
        self.assertEqual(r2.parent_revision, r1.revision_id)
        # 链头是最后一个任务 revision。
        self.assertEqual(g.head_revision(), r2.revision_id)

    def test_patch_symbol_relations_closed(self):
        t1 = _txn(1)
        g = _chain([t1])
        rev_id = t1["result_workspace_revision"]
        patches = [o for o in g.objects.values() if o.type == O.TYPE_PATCH]
        symbols = [o for o in g.objects.values() if o.type == O.TYPE_SYMBOL]
        self.assertEqual(len(patches), 1)
        self.assertEqual(len(symbols), 1)
        pid, sid = patches[0].id, symbols[0].id
        predicates = {(r.subject, r.predicate, r.object) for r in g.relations}
        self.assertIn((pid, "materialized_in", rev_id), predicates)
        self.assertIn((pid, "changes", sid), predicates)
        self.assertIn(("stask:1", "owns", sid), predicates)
        # Symbol 身份确定性。
        self.assertEqual(sid, O.workspace_symbol_id("main.py", "run"))

    def test_verify_revision_parents_chain_head_and_keeps_head(self):
        t1 = _txn(1)
        g = _chain([t1])
        O.evaluate_against_verify(g, {
            "verdict": "pass",
            "commands": [{"command": "python main.py", "status": "ok", "exit_code": 0,
                          "stdout_tail": "", "stderr_tail": ""}],
        }, parent_revision=t1["result_workspace_revision"])
        verify_revs = [r for r in g.revisions.values() if r.source == "verify"]
        self.assertEqual(len(verify_revs), 1)
        self.assertEqual(verify_revs[0].parent_revision, t1["result_workspace_revision"])
        # verify revision 仍是唯一 head —— PROVEN 证据不会被误判 stale。
        self.assertEqual(g.head_revision(), verify_revs[0].revision_id)
        self.assertEqual(OV.blocking_errors(OV.validate_all_structured(g)), [])

    def test_projection_is_idempotent(self):
        t1 = _txn(1)
        g = _chain([t1])
        n_rel = len(g.relations)
        n_obj = len(g.objects)
        n_rev = len(g.revisions)
        O.project_workspace_chain(g, [t1], at="1")
        self.assertEqual(len(g.relations), n_rel)
        self.assertEqual(len(g.objects), n_obj)
        self.assertEqual(len(g.revisions), n_rev)

    def test_legacy_transactions_create_no_dangling_endpoints(self):
        # 旧 run 事务：复合 base 串、无 parent/patches 字段。
        legacy = {
            "round": 1, "task": "T1", "task_semantic_id": "stask:1",
            "base_workspace_revision": "wsr:wip:base0000+prior:abc",
            "result_workspace_revision": "wsr:wip:r10000001",
        }
        g = _chain([legacy])
        self.assertIn("wsr:wip:r10000001", g.revisions)
        self.assertEqual(g.revisions["wsr:wip:r10000001"].parent_revision, "")
        self.assertEqual(OV.blocking_errors(OV.validate_all_structured(g)), [])

    def test_audit_flags_broken_chain(self):
        state = {"task_transactions": [
            _txn(1, base="wsr:wip:base0000"),
            # 父登记指向图上不存在的 revision。
            _txn(2, parent="wsr:wip:ghost000"),
        ]}
        g = O.OntologyGraph()
        _task(g)
        O.project_workspace_chain(g, state["task_transactions"], at="1")
        codes = {p.code for p in OV.semantic_integrity_audit(g, state)}
        self.assertIn("transaction_parent_dangling", codes)

    def test_audit_flags_unlinked_committed_patch(self):
        # 事务声称应用了补丁，但图上没有对应 materialized_in（模拟漏投影）。
        state = {"task_transactions": [_txn(1)]}
        g = O.OntologyGraph()  # 完全不投影
        codes = {p.code for p in OV.semantic_integrity_audit(g, state)}
        self.assertIn("transaction_revision_missing", codes)

    def test_clean_chain_passes_alignment_audit(self):
        state = {"task_transactions": [
            t1 := _txn(1, base="wsr:wip:base0000"),
            _txn(2, parent=t1["result_workspace_revision"]),
        ]}
        g = O.OntologyGraph()
        _task(g)
        O.project_workspace_chain(g, state["task_transactions"], at="1")
        codes = {p.code for p in OV.semantic_integrity_audit(g, state)}
        self.assertFalse({"transaction_revision_missing", "transaction_parent_dangling",
                          "transaction_parent_mismatch", "transaction_patch_unlinked"} & codes,
                         f"对齐审计不应报错：{codes}")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestWorkspaceChainProjection)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
