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
    """**裸**顶层名（`process()`）才是"方案该声明却没有" ⇒ block。"""
    plan = _plan([{
        "id": "T-02", "symbols": ["Game"], "interface": "process()",
    }], changes=[{"path": "game_logic.py", "symbols": ["Game"]}])
    findings = planir.validate_architect_plan(plan)
    assert "PLAN_INTERFACE_UNKNOWN" in _codes(findings), findings
    hit = next(f for f in findings if f["code"] == "PLAN_INTERFACE_UNKNOWN")
    assert hit["symbol"] == "process"
    assert hit["severity"] == "block"


def test_unknown_interface_member_is_warn_only() -> None:
    """真机形态：`Game().start()` —— `start` 是**已声明类 `Game` 的成员**。

    方案 symbols 里通常只写类名（不逐条列方法），而"成员是否真实存在"由**冻结的接口
    骨架**当基准（Plan IR 解析不了时会报 ``contract_unresolved``）。成员级判负会让
    方案阶段**永不收敛**（真机 20260930-132625：连续 3 次尝试都没消除）⇒ 降为 warn。
    """
    plan = _plan([{
        "id": "T-02", "symbols": ["Game"], "interface": "Game().start()",
    }], changes=[{"path": "game_logic.py", "symbols": ["Game"]}])
    findings = planir.validate_architect_plan(plan)
    hit = next(f for f in findings if f["code"] == "PLAN_INTERFACE_MEMBER_UNKNOWN")
    assert hit["symbol"] == "start" and hit["severity"] == "warn"
    assert "PLAN_INTERFACE_UNKNOWN" not in _codes(findings)


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


# ------------------------------------------------ §8.2 误报回归（真机 20260930-132625）
# 那一轮 18 条设计阻断里有 13 条是判据 bug：闸门把自己判据的产物当成方案缺陷，
# 架构师重做多少次都消不掉 ⇒ 2 次自纠必然耗尽、停人工闸门。
def test_interface_parameter_names_are_not_symbols() -> None:
    """`Board(width, height), Snake(initial_position, direction)` —— 括号里的是**参数名**，
    方案 symbols 本就不该有它们。以前会被判 4 条 PLAN_INTERFACE_UNKNOWN。"""
    plan = _plan([{
        "id": "T-01", "symbols": ["Board", "Snake"],
        "interface": "Board(width, height), Snake(initial_position, direction)",
    }], changes=[{"path": "game_logic.py", "symbols": ["Board", "Snake"]}])
    assert planir.validate_architect_plan(plan) == []


def test_interface_wildcard_prefix_is_not_a_symbol() -> None:
    """`TestGameLogic.test_*, TestUI.test_*` —— `test_` 是**通配前缀**，不是符号名。"""
    plan = _plan([{
        "id": "T-05", "symbols": ["TestGameLogic"],
        "interface": "TestGameLogic.test_*, TestUI.test_*",
    }], changes=[{"path": "game_logic_test.py", "symbols": ["TestGameLogic", "TestUI"]}])
    assert planir.validate_architect_plan(plan) == []


def test_interface_member_still_surfaced_outside_parens() -> None:
    """修误报不能把问题藏起来：`Food.generate(board)` 里 `generate` 没逐条声明 ⇒ 仍**提示**。"""
    plan = _plan([{
        "id": "T-02", "symbols": ["Food"],
        "interface": "Food.generate(board), Game.check_collision()",
    }], changes=[{"path": "game_logic.py", "symbols": ["Food", "Game.check_collision"]}])
    hits = [f for f in planir.validate_architect_plan(plan)
            if f["code"] == "PLAN_INTERFACE_MEMBER_UNKNOWN"]
    assert [f["symbol"] for f in hits] == ["generate"], hits
    assert hits[0]["severity"] == "warn"


def test_contract_ref_with_file_prefix_resolves() -> None:
    """`game_logic.py:Game` 是**文件:符号**写法；按点号切会得到假的 `py:Game`。"""
    plan = _plan([{
        "id": "T-04", "symbols": ["main"],
        "contracts": {"uses": ["game_logic.py:Game", "ui.py:GameUI"]},
    }], changes=[
        {"path": "main.py", "symbols": ["main"]},
        {"path": "game_logic.py", "symbols": ["Game"]},
        {"path": "ui.py", "symbols": ["GameUI"]},
    ])
    assert planir.validate_architect_plan(plan) == []


def test_contract_ref_with_file_prefix_still_caught_when_missing() -> None:
    """同一写法下真缺符号仍要判负（别把整类引用一起放过）。"""
    plan = _plan([{
        "id": "T-03", "symbols": ["GameUI"],
        "contracts": {"uses": ["ui_test.py:TestUI.test_render_board"]},
    }], changes=[{"path": "ui.py", "symbols": ["GameUI"]}])
    hits = [f for f in planir.validate_architect_plan(plan)
            if f["code"] == "PLAN_CONTRACT_UNKNOWN"]
    assert hits and "test_render_board" in hits[0]["detail"], hits


def test_production_must_not_depend_on_tests_is_mechanically_stripped() -> None:
    """真机形态：模型把测试方法写进**生产任务**的 `contracts.uses` ⇒ 机械剔除。

    方向不可能成立（测试依赖实现，实现永不依赖测试）。留作阻断会让方案阶段永不收敛
    （真机 20260930-132625 连续 3 次尝试都没改对）⇒ 就地归一 + 留痕。
    测试引用生产、测试引用测试**一律不动**。
    """
    plan = _plan([
        {"id": "T-02", "target_files": ["game_logic.py"], "symbols": ["Snake"],
         "contracts": {"uses": ["game_logic_test.py:TestSnake.test_move", "game_logic.Board"]}},
        {"id": "T-05", "target_files": ["game_logic_test.py"], "symbols": ["TestSnake"],
         "contracts": {"uses": ["game_logic.py:Snake"]}},
        {"id": "T-06", "target_files": ["ui_test.py"], "symbols": ["TestUI"],
         "contracts": {"uses": ["game_logic_test.py:TestSnake"]}},
    ])
    dropped = planir.strip_test_dependencies(plan)
    assert [d["ref"] for d in dropped] == ["game_logic_test.py:TestSnake.test_move"]
    uses = {t["id"]: (t["contracts"].get("uses") or []) for t in plan["tasks"]}
    assert uses["T-02"] == ["game_logic.Board"]
    assert uses["T-05"] == ["game_logic.py:Snake"]
    assert uses["T-06"] == ["game_logic_test.py:TestSnake"]
    # 剔除之后同一条引用不再被 lint 判负（同一份 plan）
    assert not [f for f in planir.validate_architect_plan(plan)
                if str(f.get("symbol") or "").endswith("test_move")]


def test_is_test_path_detection() -> None:
    for path in ("game_logic_test.py", "tests/helper.py", "pkg/test_x.py"):
        assert planir._is_test_path(path) is True, path
    for path in ("game_logic.py", "testament.py", "contest.py", "mytest.py"):
        assert planir._is_test_path(path) is False, path


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


# ------------------------------------------------ 骨架越权：只判顶层（真机 20260930-132625）
def test_skeleton_members_are_not_overreach() -> None:
    """骨架的**成员方法**不是"扩大方案边界"。

    真机形态：方案 changes 的 symbols 是类名（`Snake`/`Game`），骨架冻结的是方法级
    （`    def move(self)`）。若把成员也算越权，任何按类名规划的方案都**必然**被判负。
    """
    plan = {"changes": [{"path": "game_logic.py", "symbols": ["Board", "Snake", "Food", "Game"]}]}
    skeleton = {"game_logic.py": [
        "class Board(:[)",
        "    实例属性: size",
        "    def __init__(size)",
        "class Snake(:[)",
        "    def __init__(position,direction)",
        "    def move(direction)",
        "    def grow()",
    ]}
    over = planir.skeleton_overreach(plan, skeleton)
    assert over["extra_symbols"] == {}, over


def test_skeleton_top_level_extra_is_still_caught() -> None:
    """顶层多出来的类仍要暴露（这才是真越权），缩进的成员不跟着报。"""
    plan = {"changes": [{"path": "ui.py", "symbols": ["GameUI"]}]}
    skeleton = {"ui.py": [
        "class GameUI(:[)",
        "    def render_board(self)",
        "class KeyBinder(:[)",          # ← 方案没声明：真越权
        "    def bind_keys(self)",
    ]}
    over = planir.skeleton_overreach(plan, skeleton)
    assert over["extra_symbols"] == {"ui.py": ["KeyBinder"]}, over


# ---------------------------------------------------------------- 健壮性
def test_non_dict_plan_is_safe() -> None:
    assert planir.validate_architect_plan(None) == []
    assert planir.validate_architect_plan("not a plan") == []  # type: ignore[arg-type]


def test_task_without_contracts_is_safe() -> None:
    plan = _plan([{"id": "T-01", "symbols": ["Game"]}])
    assert planir.validate_architect_plan(plan) == []
