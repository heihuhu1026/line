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
