"""Phase 2 P0 TestCompiler 主链单测 —— 方案§四~§十。

覆盖：
1. audit_po_test_coverage：covered / weak / missing 三分类；
2. verify.plan_commands：TestCompiler 绑定盖到执行 spec，机械命令不带绑定；
3. verify.run_command：expect_exit 与声明式断言的通过/失败判定 + 绑定字段继承；
4. 闭环：compile_scenarios → 绑定映射 → verify 执行结果 →
   evaluate_against_verify 精确证明 PO（有断言才证明、裸 rc=0 不证明、未绑定不串证）。

直接运行：``python tests/test_testcompiler_mainline.py``（退出码 0/1）。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import ontology as O  # noqa: E402
from pipeline import testcompiler as TC  # noqa: E402
from pipeline import verify as V  # noqa: E402
from pipeline.config import VERIFY_ALLOWED_BINS, VERIFY_DENY_PATTERNS  # noqa: E402

#: 与 verify 内部同口径：路径含空格时必须带引号，否则过不了白名单分词。
PY = V._python_bin()


def _po(po_id: str, kind: str = O.PO_KIND_BEHAVIOR, claim: str = "") -> O.ProofObligation:
    return O.ProofObligation(
        id=po_id, name=claim or po_id, requirement_id="req-1", claim=claim or po_id,
        kind=kind, required=True,
    )


class TestCoverageAudit(unittest.TestCase):
    def test_covered_weak_missing(self):
        compiled = TC.compile_scenarios(
            obligations=[
                {"id": "po:a", "kind": O.PO_KIND_BEHAVIOR, "required": True, "claim": "a", "name": "a"},
                {"id": "po:w", "kind": O.PO_KIND_BEHAVIOR, "required": True, "claim": "w", "name": "w"},
                {"id": "po:m", "kind": O.PO_KIND_BEHAVIOR, "required": True, "claim": "m", "name": "m"},
            ],
            planned_commands=[
                {"command": f"{PY} -m unittest t", "target_po": "po:a",
                 "assertions": ["stdout_contains:OK"]},
                {"command": f"{PY} main.py", "target_po": "po:w"},  # 仅 rc=0 ⇒ weak
            ],
        )
        audit = TC.audit_po_test_coverage(compiled)
        self.assertEqual(audit["covered"], ["po:a"])
        self.assertEqual(audit["weak"], ["po:w"])
        self.assertEqual(audit["missing"], ["po:m"])


class TestPlanCommandsBinding(unittest.TestCase):
    def test_bindings_stamp_only_declared_commands(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            (work / "main.py").write_text("print('x')\n", encoding="utf-8")
            cmd = f"{PY} main.py"
            report = {"automated_commands": [{"command": cmd, "description": "run"}]}
            meta = {cmd: {"target_po_ids": ["po:a"], "assertions": ["exit_code==0"], "expect_exit": 0}}
            specs = V.plan_commands(work, ["main.py"], report, max_commands=10, impl=None,
                                    command_meta=meta)
        by_cmd = {s["command"]: s for s in specs}
        planned = by_cmd[cmd]
        self.assertEqual(planned["target_po_ids"], ["po:a"])
        self.assertEqual(planned["assertions"], ["exit_code==0"])
        # 机械生成的语法检查不允许携带测试绑定
        mechanical = [s for s in specs if s.get("source") == "syntax"]
        self.assertTrue(mechanical)
        self.assertNotIn("target_po_ids", mechanical[0])


class TestRunCommandAssertions(unittest.TestCase):
    def _run(self, command, **spec_extra):
        with tempfile.TemporaryDirectory() as td:
            return V.run_command(
                {"command": command, **spec_extra},
                cwd=Path(td), timeout=30,
                allowed_bins=VERIFY_ALLOWED_BINS,
                deny_patterns=VERIFY_DENY_PATTERNS,
            )

    def test_assertion_pass_and_inheritance(self):
        cmd = f'{PY} -c "print(\'OK\')"'
        out = self._run(cmd, target_po_ids=["po:a"], assertions=["stdout_contains:OK"])
        self.assertEqual(out["status"], "ok", out.get("reason"))
        self.assertEqual(out["target_po_ids"], ["po:a"])
        self.assertEqual(out["assertion_failures"], [])

    def test_assertion_failure_marks_fail(self):
        cmd = f'{PY} -c "print(\'OK\')"'
        out = self._run(cmd, assertions=["stdout_contains:NO"])
        self.assertEqual(out["status"], "fail")
        self.assertIn("stdout_contains:NO", out["assertion_failures"])

    def test_expect_nonzero_exit(self):
        cmd = f'{PY} -c "import sys; sys.exit(2)"'
        out = self._run(cmd, expect_exit=2, assertions=["exit_code!=0"])
        self.assertEqual(out["status"], "ok", out.get("reason"))

    def test_unknown_assertion_fail_closed(self):
        cmd = f'{PY} -c "print(1)"'
        out = self._run(cmd, assertions=["some_magic_check"])
        self.assertEqual(out["status"], "fail")


class TestClosedChain(unittest.TestCase):
    """compile → binding → verify 报告 → ontology.evaluate 全链确定性演练。"""

    def test_bound_assertion_proves_only_target_po(self):
        po_a, po_b = _po("po:req:a", claim="插入后查询得到记录"), _po("po:req:b", claim="列出全部记录")
        cmd = f'{PY} -m unittest test_main'
        compiled = TC.compile_scenarios(
            obligations=[po_a, po_b],
            planned_commands=[{"command": cmd, "target_po": "po:req:a",
                              "assertions": ["stdout_contains:OK"]}],
        )
        # 组装 verify 执行后报告（等价 run_command 输出 + evaluate 需要的字段）
        bindings = {}
        for sc in compiled["scenarios"]:
            if sc["status"] != TC.STATUS_EXECUTABLE:
                continue
            for act in sc["actions"]:
                if act["safe"]:
                    bindings.setdefault(act["command"], {
                        "target_po_ids": [], "assertions": act["assertions"],
                        "expect_exit": act["expect_exit"]})
                    bindings[act["command"]]["target_po_ids"].append(sc["target_po"])
        report = {"verdict": "pass", "commands": [{
            "command": cmd, "status": "ok", "exit_code": 0,
            "stdout_tail": "OK", "stderr_tail": "",
            **bindings[cmd],
        }]}
        graph = O.OntologyGraph()
        graph.add_obligation(po_a)
        graph.add_obligation(po_b)
        O.evaluate_against_verify(graph, report)
        self.assertEqual(graph.obligations["po:req:a"].status, O.PO_STATUS_PROVEN)
        self.assertEqual(graph.obligations["po:req:b"].status, O.PO_STATUS_UNPROVEN)

    def test_weak_rc_only_command_does_not_bind_nor_prove(self):
        po_a = _po("po:req:w", claim="裸跑入口")
        cmd = f"{PY} main.py"
        compiled = TC.compile_scenarios(
            obligations=[po_a],
            planned_commands=[{"command": cmd, "target_po": "po:req:w"}],  # 无断言 ⇒ weak
        )
        bindings = {}
        for sc in compiled["scenarios"]:
            if sc["status"] == TC.STATUS_EXECUTABLE:  # weak 不产生绑定
                for act in sc["actions"]:
                    bindings.setdefault(act["command"], [])
        self.assertEqual(bindings, {})
        # verify 报告里该命令无绑定 ⇒ 兼容路径下也只是 command_result，行为 PO 不得 PROVEN
        report = {"verdict": "pass", "commands": [
            {"command": cmd, "status": "ok", "exit_code": 0, "stdout_tail": "", "stderr_tail": ""}]}
        graph = O.OntologyGraph()
        graph.add_obligation(po_a)
        O.evaluate_against_verify(graph, report)
        self.assertEqual(graph.obligations["po:req:w"].status, O.PO_STATUS_UNPROVEN)


if __name__ == "__main__":
    _program = unittest.main(verbosity=2, exit=False)
    sys.exit(0 if _program.result.wasSuccessful() else 1)
