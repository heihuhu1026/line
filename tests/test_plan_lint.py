"""Architect Plan Static Lint（P0-6）回归。

目的：**不要再等 DEV 发现接口明显错误**。
真机点名的两类：
  · `Game().start()` 出现在 interface，但方案没定义 `start`
  · `uses = game_logic.SNAKE_BODY_COLOR`，但方案没定义 `SNAKE_BODY_COLOR`
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import planir  # noqa: E402


def _plan(tasks: list[dict], changes: list[dict] | None = None) -> dict:
    return {"tasks": tasks, "changes": changes or []}


def _codes(findings: list[dict]) -> list[str]:
    return [f["code"] for f in findings]


# ---------------------------------------------------------------- §8.2 interface
def test_unknown_interface_symbol_is_blocked() -> None:
    """真机形态：`Game().start()` —— `start` 方案里根本没有。"""
    plan = _plan([{
        "id": "T-02", "symbols": ["Game"], "interface": "Game().start()",
    }], changes=[{"path": "game_logic.py", "symbols": ["Game"]}])
    findings = planir.validate_architect_plan(plan)
    assert "PLAN_INTERFACE_UNKNOWN" in _codes(findings), findings
    hit = next(f for f in findings if f["code"] == "PLAN_INTERFACE_UNKNOWN")
    assert hit["symbol"] == "start"
    assert hit["severity"] == "block"


def test_declared_interface_symbol_is_clean() -> None:
    plan = _plan([{
        "id": "T-02", "symbols": ["Game", "start"], "interface": "Game().start()",
    }], changes=[{"path": "game_logic.py", "symbols": ["Game", "start"]}])
    assert planir.validate_architect_plan(plan) == []


def test_stopwords_are_not_reported() -> None:
    """`print` / `len` 这类内建不得被当成未声明符号（否则全是噪音）。"""
    plan = _plan([{"id": "T-01", "symbols": ["Game"], "interface": "print(len(x))"}])
    assert planir.validate_architect_plan(plan) == []


# ---------------------------------------------------------------- §8.3 contracts
def test_unknown_contract_symbol_is_blocked() -> None:
    """真机形态：`uses = game_logic.SNAKE_BODY_COLOR`，方案没定义该常量。"""
    plan = _plan([{
        "id": "T-03", "symbols": ["GameUI"],
        "contracts": {"uses": ["game_logic.SNAKE_BODY_COLOR"]},
    }], changes=[{"path": "ui.py", "symbols": ["GameUI"]}])
    findings = planir.validate_architect_plan(plan)
    assert "PLAN_CONTRACT_UNKNOWN" in _codes(findings), findings


def test_declared_contract_symbol_is_clean() -> None:
    plan = _plan([{
        "id": "T-03", "symbols": ["GameUI"],
        "contracts": {"uses": ["game_logic.SNAKE_BODY_COLOR"]},
    }], changes=[
        {"path": "ui.py", "symbols": ["GameUI"]},
        {"path": "game_logic.py", "symbols": ["SNAKE_BODY_COLOR"]},
    ])
    assert planir.validate_architect_plan(plan) == []


# ---------------------------------------------------------------- §8.4 / §8.5 依赖
def test_third_party_dependency_is_blocked() -> None:
    """需求只允许标准库时出现 pygame → 立即阻断（不是警告）。"""
    plan = _plan([{
        "id": "T-01", "symbols": ["Game"], "contracts": {"uses": ["pygame"]},
    }], changes=[{"path": "game_logic.py", "symbols": ["Game"]}])
    findings = planir.validate_architect_plan(plan)
    assert "PLAN_UNDECLARED_DEPENDENCY" in _codes(findings), findings
    hit = next(f for f in findings if f["code"] == "PLAN_UNDECLARED_DEPENDENCY")
    assert hit["severity"] == "block"


def test_unrelated_stdlib_dependency_is_warning_only() -> None:
    """`math` / `time` 能 import 但未必被要求 —— 只 warn，不随意阻断（§8.5）。"""
    plan = _plan([{
        "id": "T-01", "symbols": ["Game"], "contracts": {"uses": ["math"]},
    }], changes=[{"path": "game_logic.py", "symbols": ["Game"]}])
    findings = planir.validate_architect_plan(plan)
    assert "PLAN_UNRELATED_DEPENDENCY" in _codes(findings), findings
    hit = next(f for f in findings if f["code"] == "PLAN_UNRELATED_DEPENDENCY")
    assert hit["severity"] == "warn"


# ---------------------------------------------------------------- §8.1 成员写法
def test_member_symbol_without_owner_is_blocked() -> None:
    plan = _plan([{"id": "T-01", "symbols": ["Game.score"]}], changes=[])
    findings = planir.validate_architect_plan(plan)
    assert "PLAN_SYMBOL_UNKNOWN_OWNER" in _codes(findings), findings


def test_member_symbol_with_declared_owner_is_clean() -> None:
    plan = _plan([{"id": "T-01", "symbols": ["Game.score"]}],
                 changes=[{"path": "game_logic.py", "symbols": ["Game"]}])
    assert planir.validate_architect_plan(plan) == []


# ---------------------------------------------------------------- 健壮性
def test_non_dict_plan_is_safe() -> None:
    assert planir.validate_architect_plan(None) == []
    assert planir.validate_architect_plan("not a plan") == []  # type: ignore[arg-type]


def test_task_without_contracts_is_safe() -> None:
    plan = _plan([{"id": "T-01", "symbols": ["Game"]}])
    assert planir.validate_architect_plan(plan) == []
