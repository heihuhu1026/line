"""业务 Proof 覆盖硬指标（P0-11 §13）回归。

真机形态「13 个 case、只覆盖 3/13 条 FR」不该等 Review 说"测试少" ——
`proof_coverage_gate` 必须把这个缺口变成**可查询的数字**。

安全规则（规格§四十二）：
    required PO 无场景      → missing（UNPROVEN）
    有场景但只有 rc=0       → weak（等于没证明）
    场景不可执行            → unexecutable（不能伪装成 PASS）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import ontology  # noqa: E402
from pipeline import testcompiler as tc  # noqa: E402


def _po(pid: str, kind: str = ontology.PO_KIND_BEHAVIOR, required: bool = True) -> dict:
    return {"id": pid, "kind": kind, "required": required, "claim": "c", "name": pid}


def _sc(target: str, status: str) -> dict:
    return {"id": f"sc-{target}", "target_po": target, "status": status}


def test_counts_are_reported() -> None:
    required = [_po(f"po:{i:02d}") for i in range(1, 6)]
    compiled = {"scenarios": [
        _sc("po:01", tc.STATUS_EXECUTABLE),
        _sc("po:02", tc.STATUS_EXECUTABLE),
        _sc("po:03", tc.STATUS_WEAK),
        _sc("po:04", tc.STATUS_UNPROVEN),
        # po:05 完全没有场景 → missing
    ]}
    gate = tc.proof_coverage_gate(required, compiled)
    assert gate["required"] == 5
    assert gate["covered"] == 2
    assert gate["weak"] == 1
    assert gate["unexecutable"] == 1
    assert gate["missing"] == 1
    assert gate["missing_ids"] == ["po:05"]


def test_weak_is_not_covered() -> None:
    """§13.2：只有 rc=0 的场景**不算**已证明 —— weak 绝不能计进 covered。"""
    required = [_po("po:01")]
    compiled = {"scenarios": [_sc("po:01", tc.STATUS_WEAK)]}
    gate = tc.proof_coverage_gate(required, compiled)
    assert gate["covered"] == 0
    assert gate["weak"] == 1
    assert gate["weak_ids"] == ["po:01"]


def test_unproven_scenario_counts_as_unexecutable_not_pass() -> None:
    """§13.3：场景不可执行 → unexecutable，**不得**伪装成 PASS。"""
    required = [_po("po:01")]
    compiled = {"scenarios": [_sc("po:01", tc.STATUS_UNPROVEN)]}
    gate = tc.proof_coverage_gate(required, compiled)
    assert gate["covered"] == 0
    assert gate["unexecutable"] == 1


def test_non_required_po_is_excluded() -> None:
    """非 required 的 PO 不计入分母（否则覆盖数字会被无关义务稀释）。"""
    required = [_po("po:req"), _po("po:opt", required=False)]
    compiled = {"scenarios": [_sc("po:req", tc.STATUS_EXECUTABLE)]}
    gate = tc.proof_coverage_gate(required, compiled)
    assert gate["required"] == 1
    assert gate["covered"] == 1


def test_all_covered_is_clean() -> None:
    required = [_po("po:01"), _po("po:02")]
    compiled = {"scenarios": [
        _sc("po:01", tc.STATUS_EXECUTABLE),
        _sc("po:02", tc.STATUS_EXECUTABLE),
    ]}
    gate = tc.proof_coverage_gate(required, compiled)
    assert gate["missing"] == 0 and gate["weak"] == 0 and gate["unexecutable"] == 0
    assert gate["covered"] == gate["required"] == 2


def test_empty_input_is_safe() -> None:
    gate = tc.proof_coverage_gate(None, None)
    assert gate["required"] == 0 and gate["covered"] == 0 and gate["missing"] == 0
