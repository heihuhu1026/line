"""§12.5 机械测试自动生成：contract / interface_freeze / materialization 三类 PO 的
权威 verifier 是**编排器就地跑的机械检查器**（``contract_check`` / ``skeleton_conformance`` /
补丁物化），不是 shell 命令 —— 因此必须由 **TestCompiler 自己生成**，不交给 Test LLM，
也不能让 LLM 的命令冒充它们的证据。

安全规则（规格§四十二）：
    机械场景无 shell 命令      → 不得进 automated_commands / verify 绑定
    命令声称证明机械 PO        → UNBOUND（具名原因，不静默丢也不塞给别人）
    机械场景                  → 计入 covered，但仍须真实机械证据才能 PROVEN（can_release 守）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import ontology  # noqa: E402
from pipeline import testcompiler as tc  # noqa: E402


def _po(pid: str, kind: str, required: bool = True) -> dict:
    return {"id": pid, "kind": kind, "required": required, "claim": pid, "name": pid}


def test_contract_freeze_materialization_are_compiler_generated() -> None:
    compiled = tc.compile_scenarios(
        obligations=[
            _po("po:ctr", ontology.PO_KIND_CONTRACT),
            _po("po:ifz", ontology.PO_KIND_INTERFACE_FREEZE),
            _po("po:mat", ontology.PO_KIND_MATERIALIZATION),
        ],
        files=["main.py"],
    )
    by = {s["target_po"]: s for s in compiled["scenarios"]}
    assert by["po:ctr"]["status"] == tc.STATUS_MECHANICAL
    assert by["po:ctr"]["mechanical_check"] == ["contract_check"]
    assert by["po:ifz"]["mechanical_check"] == ["contract_check", "skeleton_conformance"]
    assert by["po:mat"]["mechanical_check"] == ["patch_apply"]
    # 机械场景不带 shell 命令（执行器只消费 automated_commands）
    assert all(s["automated_commands"] == [] for s in by.values())
    assert compiled["coverage_gap"] == []


def test_mechanical_scenario_id_is_stable() -> None:
    a = tc.compile_scenarios(obligations=[_po("po:ctr", ontology.PO_KIND_CONTRACT)])
    b = tc.compile_scenarios(obligations=[_po("po:ctr", ontology.PO_KIND_CONTRACT)])
    assert a["scenarios"][0]["id"] == b["scenarios"][0]["id"]
    assert a["scenarios"][0]["id"].startswith("tscn:")


def test_command_claiming_mechanical_po_is_unbound_not_silently_dropped() -> None:
    """LLM 声称一条运行命令能证明 contract PO —— 命令不是它的证据，必须 UNBOUND 留痕。"""
    compiled = tc.compile_scenarios(
        obligations=[_po("po:ctr", ontology.PO_KIND_CONTRACT)],
        planned_commands=[
            {"command": "python main.py", "target_po": "po:ctr",
             "assertions": ["stdout_contains:OK"]},
        ],
    )
    unbound = compiled["unbound_commands"]
    assert len(unbound) == 1 and unbound[0]["candidate"] == "po:ctr", unbound
    assert "就地机械检查器" in unbound[0]["reason"]
    used = [a["command"] for s in compiled["scenarios"] for a in s["actions"]]
    assert used == [], "证不成的命令被归给了别的 PO"


def test_mechanical_counts_as_covered_in_coverage_gate() -> None:
    required = [_po("po:ctr", ontology.PO_KIND_CONTRACT), _po("po:beh", ontology.PO_KIND_BEHAVIOR)]
    compiled = tc.compile_scenarios(obligations=required, files=["main.py"])
    gate = tc.proof_coverage_gate(required, compiled)
    # contract 由机械场景覆盖；行为 PO 无命令 ⇒ 场景 UNPROVEN ⇒ unexecutable（不得伪装成 PASS）
    assert gate["covered"] == 1 and gate["missing"] == 0 and gate["unexecutable"] == 1
    assert gate["covered_ids"] == ["po:ctr"]
    assert gate["unexecutable_ids"] == ["po:beh"]
    audit = tc.audit_po_test_coverage(compiled)
    assert audit["covered"] == ["po:ctr"] and audit["missing"] == ["po:beh"]


def test_non_required_mechanical_po_produces_nothing() -> None:
    compiled = tc.compile_scenarios(
        obligations=[_po("po:opt", ontology.PO_KIND_CONTRACT, required=False)]
    )
    assert compiled["scenarios"] == [] and compiled["coverage_gap"] == []
