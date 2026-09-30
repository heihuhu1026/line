"""回放基线（§39/§40）：本轮新增机制必须能在**真实故障形态**上被验证。

两部分：
1. **夹具驱动**：tools/_repro/fixture_017..024 每条对应一个本轮修掉的问题，
   用对应的纯函数跑一遍并断言 expect。
2. **真机基线**：run 20260930-000332（贪吃蛇）的验收清单 —— 用户声明的 5 个文件、
   同文件 add、未知接口、未声明契约、Proof 覆盖、控制塔显示。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import ontology  # noqa: E402
from pipeline import controlplane, planir, symbols  # noqa: E402
from pipeline import taskcompiler as tc  # noqa: E402
from pipeline import testcompiler as tsc  # noqa: E402
from pipeline.semantics import build_requirement_contract, plan_missing_declared_files  # noqa: E402

REPRO = ROOT / "tools" / "_repro"
SNAKE_REQ = REPRO / "requirement_snake_20260930.txt"


def _load(num: str) -> dict:
    hits = sorted(REPRO.glob(f"fixture_{num}_*.json"))
    if not hits:
        pytest.skip(f"缺少夹具 fixture_{num}")
    return json.loads(hits[0].read_text(encoding="utf-8"))


# ---------------------------------------------------------------- §39 夹具驱动
def test_fixture_017_missing_declared_test_files() -> None:
    fx = _load("017")
    contract = build_requirement_contract(fx["input"]["requirement"])
    assert len(contract["declared_files"]) == fx["expect"]["declared_files_count"]
    missing = plan_missing_declared_files(contract, {"changes": fx["input"]["plan_changes"]})
    assert missing == fx["expect"]["missing"], missing


def test_fixture_018_file_level_requirement_overbinding() -> None:
    fx = _load("018")
    inp = fx["input"]
    tasks = [dict(t, implements_requirements=list(inp["file_level"])) for t in inp["tasks"]]
    tc._bind_requirements_by_facet(tasks, inp["scope"], [])
    sets = [tuple(sorted(t["implements_requirements"])) for t in tasks]
    assert len(set(sets)) > 1, f"三张图的需求仍然完全相同（问题 C 未修复）：{sets}"
    assert all(t.get("candidate_requirement_ids") for t in tasks), tasks


def test_fixture_019_same_file_multi_add() -> None:
    fx = _load("019")
    bad = tc.same_file_add_violations(fx["input"]["tasks"])
    assert bad and bad[0]["code"] == fx["expect"]["code"]
    assert bad[0]["extra_creators"] == fx["expect"]["extra_creators"]
    # 正常形态（1 owner + N modify）绝不能误判
    assert tc.same_file_add_violations(fx["input"]["normal_tasks"]) == []


def test_fixture_020_architect_unknown_interface() -> None:
    fx = _load("020")
    findings = planir.validate_architect_plan(fx["input"]["plan"])
    hit = next(f for f in findings if f["code"] == fx["expect"]["code"])
    assert hit["symbol"] == fx["expect"]["symbol"]
    assert hit["severity"] == fx["expect"]["severity"]


def test_fixture_021_symbol_member_collision() -> None:
    fx = _load("021")
    hits = symbols.validate_symbol_collisions(fx["input"]["files"])
    hit = next(h for h in hits if h["code"] == fx["expect"]["code"])
    assert hit["symbol"] == fx["expect"]["symbol"]


def test_fixture_022_test_po_candidate_misbinding() -> None:
    fx = _load("022")
    compiled = tsc.compile_scenarios(
        obligations=fx["input"]["required"],
        planned_commands=fx["input"]["planned_commands"],
    )
    unbound = compiled.get("unbound_commands") or []
    assert len(unbound) == fx["expect"]["unbound_count"], unbound
    assert unbound[0]["candidate"] == fx["expect"]["unbound_candidate"]
    # 不得静默归给 delivery
    used = [
        (a.get("command") if isinstance(a, dict) else getattr(a, "command", ""))
        for s in compiled.get("scenarios") or []
        for a in s.get("actions") or ()
    ]
    assert "python main.py" not in used, "证不成的命令被归给了别的 PO"


def test_fixture_023_unproven_required_po() -> None:
    fx = _load("023")
    gate = tsc.proof_coverage_gate(
        fx["input"]["required"], {"scenarios": fx["input"]["scenarios"]}
    )
    assert gate["required"] == fx["expect"]["required"]
    assert gate["covered"] == fx["expect"]["covered"]
    assert gate["missing_ids"] == fx["expect"]["missing_ids"]


def test_fixture_024_control_plane_release_block() -> None:
    fx = _load("024")
    inp = fx["input"]
    decision = ontology.build_release_decision(
        semantic_verdict=inp["semantic_verdict"],
        proof_status=inp["proof_status"],
        ontology_errors=inp["ontology_errors"],
        workspace_verified=inp["workspace_verified"],
    )
    assert decision["can_pass"] is fx["expect"]["can_pass"]
    assert decision["ontology_status"] == fx["expect"]["ontology_status"]
    assert fx["expect"]["next_action_contains"] in decision["next_action"]
    assert decision["semantic_verdict"] == fx["expect"]["semantic_verdict"]


# ---------------------------------------------------------------- §40 真机基线
@pytest.mark.skipif(not SNAKE_REQ.is_file(), reason="缺少真机需求夹具")
def test_snake_user_files_are_not_lost() -> None:
    """① 用户要求的 5 个文件不能在 Architecture 阶段丢失。"""
    contract = build_requirement_contract(SNAKE_REQ.read_text(encoding="utf-8"))
    paths = {f["path"] for f in contract["declared_files"]}
    assert {"main.py", "game_logic.py", "ui.py",
            "game_logic_test.py", "ui_test.py"} <= paths, sorted(paths)
    # 方案只剩 3 个 ⇒ 闸门必须判出缺的两个
    missing = plan_missing_declared_files(contract, {"changes": [
        {"path": "game_logic.py"}, {"path": "ui.py"}, {"path": "main.py"},
    ]})
    assert missing == ["game_logic_test.py", "ui_test.py"], missing


def test_snake_same_file_has_single_owner() -> None:
    """② game_logic.py 不应默认产生 3 个独立 add task —— 只能有一个创建者。"""
    tasks = [
        {"id": "T-01", "target_files": ["game_logic.py"], "symbols": ["Snake", "Food"]},
        {"id": "T-02", "target_files": ["game_logic.py"], "symbols": ["Game.score"]},
        {"id": "T-03", "target_files": ["game_logic.py"], "symbols": ["Game.is_game_over"]},
    ]
    tc._annotate_create_owners(tasks, existing_files=set())
    owners = tc.file_owner_map(tasks)["game_logic.py"]
    assert owners["create_owner"] == "T-01"
    assert owners["modify_tasks"] == ["T-02", "T-03"]
    assert tc.same_file_add_violations(tasks) == []


def test_snake_unknown_interface_caught_before_dev() -> None:
    """③ Game().start() 若未定义，必须在 DEV 前被识别。"""
    plan = {
        "changes": [{"path": "game_logic.py", "symbols": ["Game", "Snake"]}],
        "tasks": [{"id": "T-02", "symbols": ["Game"], "interface": "Game().start()"}],
    }
    codes = [f["code"] for f in planir.validate_architect_plan(plan)]
    assert "PLAN_INTERFACE_UNKNOWN" in codes, codes


def test_snake_undeclared_contract_caught_in_plan_gate() -> None:
    """④ 未声明 SNAKE_BODY_COLOR 的契约必须在 Plan Gate 被识别。"""
    plan = {
        "changes": [{"path": "ui.py", "symbols": ["GameUI"]}],
        "tasks": [{"id": "T-03", "symbols": ["GameUI"],
                   "contracts": {"uses": ["game_logic.SNAKE_BODY_COLOR"]}}],
    }
    codes = [f["code"] for f in planir.validate_architect_plan(plan)]
    assert "PLAN_CONTRACT_UNKNOWN" in codes, codes


def test_snake_proof_coverage_gap_is_exposed() -> None:
    """⑤ 测试覆盖不足时必须出现 Proof coverage gap（而不是等 Review 说测试少）。"""
    required = [{"id": f"po:FR-{i:02d}", "kind": "behavior", "required": True,
                 "claim": c, "name": f"po:FR-{i:02d}"}
                for i, c in enumerate(["吃到食物得分+10", "撞墙游戏结束",
                                       "蛇初始长度 3", "食物不生成在蛇身上"], start=1)]
    # 只证了 1 条 ⇒ 3 条 missing
    gate = tsc.proof_coverage_gate(required, {"scenarios": [
        {"id": "sc-1", "target_po": "po:FR-01", "status": "executable"},
    ]})
    assert gate["required"] == 4 and gate["covered"] == 1 and gate["missing"] == 3


def test_snake_symbol_manifest_diff_reports_missing() -> None:
    """⑥ main.py 这类未写出的符号：planned vs actual 的机械 diff 必须报缺失。"""
    manifest = symbols.build_symbol_manifest([
        {"target_files": ["main.py"], "symbols": ["main"]},
        {"target_files": ["game_logic.py"], "symbols": ["Game"]},
    ])
    diff = symbols.planned_vs_actual(manifest, {"game_logic.py": ["Game"]})
    assert diff["missing"] == {"main.py": ["main"]}


def test_snake_verify_fail_cannot_pass() -> None:
    """⑦ Verify fail 后，即便语义评审 pass 也不能放行。"""
    decision = ontology.build_release_decision(
        semantic_verdict="pass",
        proof_status={"status": "FAILED", "failed": ["po:FR-02"]},
        workspace_verified="ws-006",
    )
    assert decision["can_pass"] is False
    assert decision["proof_status"] == "FAILED"


def test_snake_control_tower_shows_block_state() -> None:
    """⑧ 控制塔必须显示 FAILED/UNPROVEN、缺失证明、下一步、工作区版本。"""
    view = controlplane.build_control_plane({"state": {
        "release_gate": {
            "semantic_verdict": "pass", "can_pass": False,
            "proof_status": "UNPROVEN", "verified_revision": "ws-006",
            "blocking_reasons": ["proof_unproven：po:FR-02"],
            "next_action": "去 Test 阶段：补齐缺失的 Proof 证据",
        },
        "proof_gate": {"obligations": [
            {"id": "po:FR-01", "status": "PROVEN", "required": True},
            {"id": "po:FR-02", "status": "FAILED", "required": True},
            {"id": "po:FR-03", "status": "UNPROVEN", "required": True},
        ]},
    }})
    gate = view["release_gate"]
    assert gate["can_pass"] is False
    assert gate["proof_status"] == "UNPROVEN"
    assert gate["semantic_verdict"] == "pass"       # 候选仍在，但不等于放行
    assert gate["verified_revision"] == "ws-006"
    assert view["proof_summary"] == dict(required=3, covered=1, failed=1,
                                         unproven=1, weak=0, unexecutable=0)
    assert "Test" in view["next_action"]["text"] or "Proof" in view["next_action"]["text"]
    assert view["decision"]["semantic_review_is_candidate_only"] is True
