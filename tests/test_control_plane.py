"""交付控制塔后端派生视图（P1 §32 / §33）回归。

两条硬边界：
  · 本视图**只聚合不裁决** —— can_pass 一律来自 build_release_decision 的结论；
  · 前端只消费 control_plane，不得自己解析 state.ontology（那是让 UI 猜语义）。
另：老 run 缺字段时必须返回中性值，**不崩、不猜**。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import controlplane as cp  # noqa: E402


def _detail(state: dict | None = None, **extra: object) -> dict:
    return {"state": state or {}, "issue_summary": {}, **extra}


# ---------------------------------------------------------------- 健壮性
def test_empty_detail_is_safe() -> None:
    view = cp.build_control_plane({})
    assert view["version"] == 1
    assert view["release_gate"]["can_pass"] is False
    assert view["proof_summary"]["required"] == 0
    assert view["evidence"] == []
    assert view["ontology"]["status"] == "VALID"
    assert view["workspace"]["status"] == "UNVERIFIED"


def test_non_dict_input_is_safe() -> None:
    assert cp.build_control_plane("not a detail")["version"] == 1
    assert cp.build_control_plane(None)["version"] == 1


# ---------------------------------------------------------------- 放行闸门
def test_release_gate_is_carried_not_recomputed() -> None:
    """裁决权不在本模块：state 里给什么就搬什么（不重新发明 PASS 条件）。"""
    view = cp.build_control_plane(_detail({
        "release_gate": {
            "can_pass": True, "status": "PROVEN", "verdict": "pass",
            "proof_status": "PROVEN", "ontology_status": "VALID",
            "workspace_status": "VERIFIED", "verified_revision": "ws-006",
            "next_action": "可交付", "decision_id": "dec-1",
        },
    }))
    gate = view["release_gate"]
    assert gate["can_pass"] is True
    assert gate["verified_revision"] == "ws-006"
    assert view["decision"]["decision_id"] == "dec-1"
    # 语义评审的 pass **永远**只是候选
    assert view["decision"]["semantic_review_is_candidate_only"] is True


def test_blocking_reasons_are_exposed() -> None:
    view = cp.build_control_plane(_detail({
        "release_gate": {"can_pass": False, "blocking_reasons": ["proof_unproven：po:01"]},
    }))
    assert view["release_gate"]["blocking_reasons"] == ["proof_unproven：po:01"]


# ---------------------------------------------------------------- 证明矩阵
def test_proof_matrix_counts() -> None:
    view = cp.build_control_plane(_detail({
        "proof_gate": {"obligations": [
            {"id": "po:01", "status": "PROVEN", "required": True},
            {"id": "po:02", "status": "FAILED", "required": True},
            {"id": "po:03", "status": "UNPROVEN", "required": True},
            {"id": "po:04", "status": "PROVEN", "required": False},  # 非 required 不计入
        ]},
    }))
    summary = view["proof_summary"]
    assert summary["required"] == 3
    assert summary["covered"] == 1
    assert summary["failed"] == 1
    assert summary["unproven"] == 1
    assert len(view["proofs"]) == 4  # 矩阵本身仍列出全部


def test_proofs_fall_back_to_ontology_graph() -> None:
    """没有 proof_gate 时，从语义图里按结构探测 PO（探不到就空，不猜）。"""
    view = cp.build_control_plane(_detail({
        "ontology": {"objects": [
            {"type": "ProofObligation", "id": "po:09",
             "payload": {"status": "PROVEN", "claim": "吃到食物得分+10", "required": True}},
            {"type": "Something else", "id": "x"},
        ]},
    }))
    assert [p["id"] for p in view["proofs"]] == ["po:09"]
    assert view["proof_summary"]["covered"] == 1


# ---------------------------------------------------------------- 证据链
def test_evidence_rows_come_from_graph() -> None:
    view = cp.build_control_plane(_detail({
        "ontology": {"objects": [
            {"type": "Evidence", "id": "ev-22", "truth": "PROVEN",
             "payload": {"status": "fresh", "kind": "command", "command": "python -m unittest",
                         "exit_code": 0, "target_po": ["po:01"], "workspace_revision": "ws-006"}},
        ]},
    }))
    row = view["evidence"][0]
    assert row["id"] == "ev-22"
    assert row["target_po"] == ["po:01"]      # 绑定由后端给出，UI 不推理
    assert row["workspace_revision"] == "ws-006"
    assert view["evidence_summary"]["total"] == 1


def test_stale_evidence_is_counted() -> None:
    view = cp.build_control_plane(_detail({
        "ontology": {"objects": [
            {"type": "Evidence", "id": "ev-1", "payload": {"status": "stale"}},
            {"type": "Evidence", "id": "ev-2", "payload": {"status": "fresh"}},
        ]},
    }))
    assert view["evidence_summary"]["stale"] == 1


# ---------------------------------------------------------------- 语义完整性
def test_ontology_errors_block() -> None:
    view = cp.build_control_plane(_detail({
        "ontology_problems_structured": [{"code": "PLAN_INTERFACE_UNKNOWN", "object": "T-02"}],
    }))
    assert view["ontology"]["status"] == "BLOCKED"
    assert view["ontology"]["error_count"] == 1


# ---------------------------------------------------------------- 任务 / 测试
def test_tasks_are_exposed_with_binding() -> None:
    view = cp.build_control_plane(_detail({
        "plan_compiled_tasks": [{
            "id": "T-01", "target_files": ["game_logic.py"], "symbols": ["Snake"],
            "creates_file": True, "implements_requirements": ["req:FR-01"],
            "candidate_requirement_ids": ["req:FR-04"],
        }],
    }))
    task = view["tasks"][0]
    assert task["creates_file"] is True
    assert task["implements_requirements"] == ["req:FR-01"]
    assert task["candidate_requirement_ids"] == ["req:FR-04"]


def test_test_view_separates_candidate_from_compiled() -> None:
    """前端必须能区分「LLM 候选命令」与「Compiler 最终命令」——这里把 unbound 单列。"""
    view = cp.build_control_plane(_detail({
        "proof_gate": {
            "unbound_commands": [{"command": "python main.py", "candidate": "po:syntax"}],
            "unsafe_commands": [{"command": "rm -rf /"}],
        },
    }))
    assert len(view["test"]["unbound_commands"]) == 1
    assert len(view["test"]["unsafe_commands"]) == 1


def test_test_view_exposes_modes_and_scenarios() -> None:
    """§27 / P0-13：场景明细 + 验证方式分类 + 机械不可验义务（只聚合，不裁决）。"""
    view = cp.build_control_plane(_detail({
        "test_scenarios": {
            "verification_modes": {"mechanical": ["po:ctr"], "gui_smoke": ["po:ui"]},
            "external_required": ["po:ui"],
            "coverage_gap": ["po:ui"],
            "unbound_commands": [{"command": "python main.py", "candidate": "po:ctr",
                                  "reason": "就地机械检查器"}],
            "scenarios": [{
                "id": "tscn:1", "target_po": "po:ctr", "kind": "contract",
                "status": "mechanical", "verification_mode": "mechanical",
                "mechanical_check": ["contract_check"], "automated_commands": [],
                "actions": [{"assertions": ["exit_code==0"]}],
            }],
        },
    }))
    test = view["test"]
    assert test["verification_modes"]["gui_smoke"] == ["po:ui"]
    assert test["external_required"] == ["po:ui"]
    assert test["coverage_gap"] == ["po:ui"]
    # 编译产物优先于 Proof Gate 的同名字段（老 run 才走回退）
    assert len(test["unbound_commands"]) == 1
    row = test["scenarios"][0]
    assert row["mechanical_check"] == ["contract_check"]
    assert row["commands"] == [] and row["assertions"] == ["exit_code==0"]
    assert row["verification_mode"] == "mechanical"


def test_proof_rows_carry_bound_details() -> None:
    """§22.1：场景/命令/退出码/证据的 join 在**后端**做 —— 前端只渲染，不按 id 猜。"""
    view = cp.build_control_plane(_detail({
        "proof_gate": {"obligations": [{
            "id": "po:1", "status": "FAILED", "claim": "吃到食物得分 +10",
            "kind": "behavior", "scenario": "tscn:1", "evidence": ["ev:1"],
            "workspace_revision": "ws-006", "required": True,
        }]},
        "test_scenarios": {"scenarios": [{
            "id": "tscn:1", "target_po": "po:1", "status": "executable",
            "automated_commands": ["python -c \"assert 1\""],
            "actions": [{"assertions": ["stdout_contains:ok"]}],
        }]},
        "verify_report": {"commands": [{
            "command": "python -c \"assert 1\"", "status": "fail", "exit_code": 1,
            "stdout_tail": "boom", "stderr_tail": "AssertionError",
        }]},
        "ontology": {"objects": [{"id": "ev:1", "type": "Evidence",
                                  "payload": {"status": "fail", "target_po": ["po:1"]}}]},
    }))
    row = view["proofs"][0]
    assert row["scenario_detail"]["id"] == "tscn:1"
    cmd = row["command_details"][0]
    assert (cmd["status"], cmd["exit_code"], cmd["stdout_tail"]) == ("fail", 1, "boom")
    assert cmd["stderr_tail"] == "AssertionError"
    assert row["evidence_detail"][0]["id"] == "ev:1"


def test_truth_view_separates_asserted_from_derived() -> None:
    """§29/§16.3：默认假设**不得**与用户原文混在一起（否则会被读成用户要求）。"""
    view = cp.build_control_plane(_detail({
        "requirement_contract": {
            "version": 1,
            "declared_files": [{"path": "game_logic.py", "truth": "ASSERTED",
                                "source_quote": "game_logic.py"}],
            "hard_constraints": [{"id": "constraint:01", "text": "不得依赖 tkinter",
                                  "truth": "ASSERTED", "source_quote": "不得依赖 tkinter",
                                  "mechanical_check": ["forbidden_import:tkinter"]}],
            "derived_facts": [{"text": "目标用户为终端玩家", "source": "intake"}],
            "grounding_errors": [{"code": "ASSERTED_WITHOUT_QUOTE", "detail": "x"}],
        },
        "intake_decisions": [{"decision": "采用建议答案", "id": "q1"}],
        "ontology_problems_structured": [{"code": "PLAN_FORBIDDEN_DEPENDENCY",
                                          "message": "T-01 依赖 tkinter", "object": "T-01"}],
    }))
    truth = view["truth"]
    assert [a["text"] for a in truth["asserted"] if a["bucket"] == "declared_files"] == ["game_logic.py"]
    assert truth["asserted"][1]["mechanical_check"] == ["forbidden_import:tkinter"]
    assert truth["derived"][0]["truth"] == "DERIVED"
    assert truth["human"][0]["scope"] == "intake"
    assert truth["contradicted"][0]["code"] == "PLAN_FORBIDDEN_DEPENDENCY"
    assert truth["grounding_errors"]


def test_plan_diff_view_reports_merge_and_owner() -> None:
    """§26：3 张架构师图合成 1 张执行图 ⇒ 必须显示 merged 与文件租约。"""
    view = cp.build_control_plane(_detail({
        "plan_draft_tasks": [
            {"id": "T-01", "target_files": ["game_logic.py"], "symbols": ["Snake"]},
            {"id": "T-02", "target_files": ["game_logic.py"], "symbols": ["Game.score"]},
        ],
        "plan_compiled_tasks": [
            {"id": "T-01", "target_files": ["game_logic.py"],
             "symbols": ["Snake", "Game.score"], "creates_file": True},
        ],
        "plan_file_owners": {"game_logic.py": {"create_owner": "T-01", "modify_tasks": []}},
        "plan_compiled_reasons": ["架构师把同文件拆成多张图"],
    }))
    diff = view["plan_diff"]
    assert len(diff["draft"]) == 2 and len(diff["compiled"]) == 1
    slot = diff["files"][0]
    assert slot["kind"] == "merged" and slot["create_owner"] == "T-01"
    assert slot["draft_ids"] == ["T-01", "T-02"] and slot["creates_file"] is True
    assert diff["reasons"]


def test_requirement_count_for_proof_chain() -> None:
    """§30 证明链首节点：需求条数从语义图直接数（只读结构，不做推理）。"""
    view = cp.build_control_plane(_detail({
        "ontology": {"objects": [
            {"id": "req:1", "type": "Requirement", "payload": {}},
            {"id": "req:2", "type": "Requirement", "payload": {}},
            {"id": "po:1", "type": "ProofObligation", "payload": {}},
        ]},
    }))
    assert view["ontology"]["requirement_count"] == 2


# ---------------------------------------------------------------- live 轮询
def test_live_view_is_compact() -> None:
    live = cp.build_live(_detail({
        "status": "running", "cursor": "dev", "attempt": 2, "needs_human": False,
        "release_gate": {"can_pass": False, "proof_status": "UNPROVEN",
                         "verified_revision": "ws-003"},
    }, running=True))
    for key in ("running", "status", "cursor", "attempt", "needs_human",
                "proof_status", "proof_counts", "ontology_error_count",
                "workspace_revision", "can_pass"):
        assert key in live, key
    assert live["cursor"] == "dev"
    assert live["workspace_revision"] == "ws-003"
    # 轻量视图不得把整份明细带出去
    assert "proofs" not in live and "evidence" not in live
