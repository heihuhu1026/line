"""Plan Completeness Gate（P0-2）回归。

要证明：用户原文声明的交付文件**不可能**在方案层被静默删除 ——
契约把事实钉住（喂给 PM/Architect），闸门再机械判一次缺失（兜住"喂了但还是删"）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import prompts  # noqa: E402
from pipeline.semantics import build_requirement_contract, plan_missing_declared_files  # noqa: E402

FIXTURE = ROOT / "tools" / "_repro" / "requirement_snake_20260930.txt"

USER_FILES = ["main.py", "game_logic.py", "ui.py", "game_logic_test.py", "ui_test.py"]


@pytest.fixture(scope="module")
def requirement() -> str:
    if not FIXTURE.is_file():
        pytest.skip(f"缺少回放夹具：{FIXTURE}")
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def contract(requirement: str) -> dict:
    return build_requirement_contract(requirement)


def _plan(*paths: str) -> dict:
    return {"changes": [{"path": p} for p in paths]}


# ---------------------------------------------------------------- 缺失判定
def test_missing_test_files_are_detected(contract: dict) -> None:
    """问题 A 的形态：方案只剩 3 个文件，两个测试文件被静默删掉。"""
    plan = _plan("game_logic.py", "ui.py", "main.py")
    missing = plan_missing_declared_files(contract, plan)
    assert sorted(missing) == ["game_logic_test.py", "ui_test.py"], missing


def test_all_covered_is_clean(contract: dict) -> None:
    assert plan_missing_declared_files(contract, _plan(*USER_FILES)) == []


def test_path_forms_are_normalized(contract: dict) -> None:
    """方案写 `./ui.py` 或 `src/game_logic.py` 都应算命中，不因写法差异误报。"""
    plan = _plan("./main.py", "src/game_logic.py", "ui.py",
                 "game_logic_test.py", "ui_test.py")
    assert plan_missing_declared_files(contract, plan) == []


def test_no_contract_is_backward_compatible() -> None:
    """老 run 没有 requirement_contract 字段 → 判据必须返回空，不得误伤。"""
    assert plan_missing_declared_files(None, _plan("main.py")) == []
    assert plan_missing_declared_files({}, _plan("main.py")) == []


def test_no_plan_is_backward_compatible(contract: dict) -> None:
    assert plan_missing_declared_files(contract, None) == []
    assert plan_missing_declared_files(contract, {}) == []


# ---------------------------------------------------------------- 提示词注入
def test_contract_block_lists_user_files(contract: dict) -> None:
    block = prompts.grounded_contract_block(contract)
    for path in USER_FILES:
        assert f"`{path}`" in block, f"契约区块里缺少用户声明文件 {path}"
    assert "不得删除" in block
    assert "PLAN_MISSING_DECLARED_FILE" in block


def test_contract_block_states_no_silent_deletion(contract: dict) -> None:
    block = prompts.grounded_contract_block(contract)
    assert "plan_exception" in block, "必须告诉模型：不实现要显式写例外，不得静默删"


def test_contract_block_empty_when_no_contract() -> None:
    assert prompts.grounded_contract_block(None) == ""
    assert prompts.grounded_contract_block({}) == ""
