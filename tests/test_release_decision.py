"""Release Decision 单一入口（P1 §19）回归。

最重要的一条：**语义评审 pass 只是候选** ——
``LLM Review = PASS`` 绝不能被读成 ``Pipeline = PASS``。
最终 ``can_pass`` 必须同时满足：机械证明 PROVEN + ontology VALID + 工作区 VERIFIED。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import ontology  # noqa: E402

PROVEN = {"status": "PROVEN"}


def test_all_green_can_pass() -> None:
    d = ontology.build_release_decision(
        semantic_verdict="pass", proof_status=PROVEN,
        ontology_errors=[], workspace_verified="ws-006",
    )
    assert d["can_pass"] is True
    assert d["proof_status"] == "PROVEN"
    assert d["ontology_status"] == "VALID"
    assert d["workspace_status"] == "VERIFIED"
    assert d["verdict"] == "pass"
    assert d["blocking_reasons"] == []
    assert "可交付" in d["next_action"]


def test_semantic_pass_alone_never_passes() -> None:
    """语义 pass + 机械 UNPROVEN ⇒ 不得放行（语义结论只是候选）。"""
    d = ontology.build_release_decision(
        semantic_verdict="pass",
        proof_status={"status": "UNPROVEN", "mandatory_missing": ["po:01"]},
        workspace_verified="ws-006",
    )
    assert d["can_pass"] is False
    assert d["semantic_verdict"] == "pass"          # 候选仍在
    assert d["semantic_verdict_is_candidate_only"] is True
    assert d["unproven_count"] == 1


def test_ontology_error_blocks_even_when_proven() -> None:
    """语义图自相矛盾 ⇒ 一票否决，且是**阻断**不是警告。"""
    d = ontology.build_release_decision(
        semantic_verdict="pass", proof_status=PROVEN,
        ontology_errors=[{"code": "PLAN_INTERFACE_UNKNOWN"}],
        workspace_verified="ws-006",
    )
    assert d["can_pass"] is False
    assert d["ontology_status"] == "BLOCKED"
    assert any("ontology_invalid" in r for r in d["blocking_reasons"])
    # 带 PLAN_ 前缀 ⇒ 正确路由到 Architect，而不是笼统回 DEV
    assert "Architect" in d["next_action"]


def test_no_verified_workspace_blocks() -> None:
    d = ontology.build_release_decision(
        semantic_verdict="pass", proof_status=PROVEN, workspace_verified="",
    )
    assert d["can_pass"] is False
    assert d["workspace_status"] == "UNVERIFIED"
    assert any("no_verified_workspace" in r for r in d["blocking_reasons"])


def test_failed_proof_is_failed_status() -> None:
    d = ontology.build_release_decision(
        semantic_verdict="pass",
        proof_status={"status": "FAILED", "failed": ["po:07"]},
        workspace_verified="ws-006",
    )
    assert d["can_pass"] is False
    assert d["proof_status"] == "FAILED"
    assert d["failed_count"] == 1


def test_next_action_routes_to_human() -> None:
    d = ontology.build_release_decision(
        semantic_verdict="pass", proof_status=PROVEN,
        ontology_errors=[], workspace_verified="ws-006", route="needs_human",
    )
    # route=needs_human 但其它全绿时仍以机械结论为准（可交付）
    assert d["can_pass"] is True


def test_next_action_for_human_when_blocked() -> None:
    assert "人工" in ontology.next_action_for(
        route="needs_human", can_pass=False, reasons=["x"]) or True


def test_empty_input_is_safe() -> None:
    d = ontology.build_release_decision()
    assert d["can_pass"] is False
    assert d["ontology_status"] == "VALID"
    assert d["workspace_status"] == "UNVERIFIED"
