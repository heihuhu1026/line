"""§7 Plan Completeness Gate + 硬约束绑定机械检查（`validate_plan_completeness` /
`forbidden_modules` / `constraint_checks` / `PLAN_FORBIDDEN_DEPENDENCY` /
`verify.forbidden_import_problems`）。

为什么需要：约束只写在提示词里＝等于没有。`tkinter` 是**标准库**，导入探针、
pyright、`planir` 的内置依赖名单都不会判它有问题 —— 只有把契约里"禁止依赖"的模块名
喂进机械检查，才可能在 DEV 之前（方案 lint）与 DEV 之后（verify）都抓住它。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import planir, prompts, semantics, verify  # noqa: E402

SNAKE_REQ = ROOT / "tools" / "_repro" / "requirement_snake_20260930.txt"


def _contract(constraints: list[str]) -> dict:
    text = "\n".join(constraints)
    return semantics.build_requirement_contract(text, {})


# --------------------------------------------------------------- 模块名提取
def test_forbidden_modules_three_phrasings() -> None:
    """真机三种写法都要认（裸名单 / 标记词+import / 模块在导入词之前）。"""
    got = semantics.forbidden_modules(_contract([
        "禁止 pygame、numpy 以及任何需要 pip install 的第三方库",
        "game_logic.py 中不得出现 import tkinter",
        "出现 sqlalchemy 导入即视为不合格",
    ]))
    assert set(got) == {"pygame", "numpy", "tkinter", "sqlalchemy"}, got


def test_forbidden_modules_no_false_positive() -> None:
    """路径/非 ascii/纯数字/下一行的文件名**不得**被当成被禁止的模块名。"""
    got = semantics.forbidden_modules(_contract([
        "不要修改 aaa.py 里的接口",
        "不允许 180 度反向",
        "绝不能生成在蛇身上",
        "只允许使用 Python 标准库",
        # 下一行的文件名曾经被 `\s` 跨行吃掉（真机语料就是这么拼进来的）
        "game_logic.py 中不得出现 import tkinter",
    ]))
    assert got == ["tkinter"], got


def test_forbidden_modules_on_real_snake_requirement() -> None:
    contract = semantics.build_requirement_contract(
        SNAKE_REQ.read_text(encoding="utf-8"), {}
    )
    assert set(semantics.forbidden_modules(contract)) == {"pygame", "numpy", "tkinter"}
    # 约束文本也在契约里逐字可查（ASSERTED）
    rows = {c["mechanical_check"][0].split(":")[1]: c
            for c in semantics.constraint_checks(contract) if c["mechanical_check"]}
    assert "tkinter" in rows and rows["tkinter"]["human_only"] is False


def test_constraint_without_check_is_marked_human_only() -> None:
    """无法机械化的约束必须显式标成"人工确认"，而不是笼统说"已传达"。"""
    checks = semantics.constraint_checks(_contract(["不允许 180 度反向"]))
    assert checks and checks[0]["human_only"] is True
    assert checks[0]["mechanical_check"] == []


# --------------------------------------------------------------- 完整性闸门
def test_plan_completeness_reports_declared_test_and_entry() -> None:
    """真机语料：5 个声明文件里方案只留 3 个 ⇒ 必须同时报"文件缺失 + 测试文件缺失"。"""
    contract = semantics.build_requirement_contract(
        SNAKE_REQ.read_text(encoding="utf-8"), {}
    )
    assert len(contract["declared_files"]) == 5
    plan = {"changes": [{"path": "game_logic.py"}, {"path": "ui.py"},
                        {"path": "main.py"}]}
    findings = semantics.validate_plan_completeness(contract, plan)
    codes = {f["code"] for f in findings}
    assert "plan_missing_declared_file" in codes
    assert "plan_missing_test_file" in codes
    missing = [f for f in findings if f["code"] == "plan_missing_declared_file"][0]["files"]
    assert {"game_logic_test.py", "ui_test.py"} <= set(missing), missing
    blocks = [f for f in findings if f["severity"] == "block"]
    assert {f["code"] for f in blocks} == {
        "plan_missing_declared_file", "plan_missing_test_file",
    }
    # 约束未被承认只提示、不阻断（机械检查已兜底，不能变成新的死循环）
    assert all(f["severity"] != "block" for f in findings if f["code"] == "plan_constraint_unbound")


def test_plan_completeness_blocks_only_real_gaps() -> None:
    """方案把用户点名文件全规划上、并写明约束 ⇒ 零发现（不误伤）。"""
    contract = semantics.build_requirement_contract(
        SNAKE_REQ.read_text(encoding="utf-8"), {}
    )
    plan = {"changes": [
        {"path": p, "constraints": ["只允许标准库，不得依赖 tkinter / pygame / numpy"]}
        for p in ("game_logic.py", "ui.py", "main.py",
                  "game_logic_test.py", "ui_test.py")
    ]}
    assert semantics.validate_plan_completeness(contract, plan) == []


def test_plan_completeness_warns_but_not_blocks_unacknowledged_constraint() -> None:
    """约束没被方案承认 ⇒ **warn**（机械检查已兜底，不能变成新的死循环）。"""
    contract = _contract(["game_logic.py 中不得出现 import tkinter"])
    plan = {"changes": [{"path": "game_logic.py"}]}
    findings = semantics.validate_plan_completeness(contract, plan)
    assert [f["code"] for f in findings] == ["plan_constraint_unbound"]
    assert findings[0]["severity"] == "warn"
    # 方案 constraints 提了 tkinter ⇒ 不再提示
    plan2 = {"changes": [{"path": "game_logic.py", "constraints": ["不得依赖 tkinter"]}]}
    assert semantics.validate_plan_completeness(contract, plan2) == []


def test_plan_completeness_backward_compatible_on_old_runs() -> None:
    """老 run 没有 requirement_contract ⇒ 一律空（不误伤旧运行）。"""
    assert semantics.validate_plan_completeness(None, {"changes": [{"path": "a.py"}]}) == []
    assert semantics.validate_plan_completeness({}, {"changes": [{"path": "a.py"}]}) == []
    # 方案还没产出（无 changes）时不判缺失，避免把中间态误报成全被删
    contract = semantics.build_requirement_contract("交付 game_logic.py\n", {})
    assert semantics.plan_missing_declared_files(contract, {"changes": []}) == []


# --------------------------------------------------------------- 方案 lint
def test_forbidden_module_scope_follows_the_file_it_names() -> None:
    """⚠ 真机贪吃蛇需求：`game_logic.py` 禁 tkinter，而 `ui.py` **必须**用 tkinter 画 Canvas。

    后半句「出现 tkinter 导入即视为不合格」是前半句的补述，必须**继承作用域** ——
    否则会去禁 ui.py 的 tkinter，把正确方案判负（架构师改不对，因为改对的方式就是用 tkinter）。
    """
    contract = semantics.build_requirement_contract(
        SNAKE_REQ.read_text(encoding="utf-8"), {}
    )
    scopes = {s["module"]: s["files"] for s in semantics.forbidden_module_scopes(contract)}
    assert scopes["tkinter"] == ["game_logic.py"], scopes
    # pygame / numpy 是**全项目**禁止（原文没说只限某文件）⇒ 空作用域
    assert scopes["pygame"] == [] and scopes["numpy"] == [], scopes
    # 提示词要把作用域写出来，否则架构师会把 ui.py 也避开 tkinter
    block = prompts.grounded_contract_block(contract)
    assert "forbidden_import:tkinter，仅限 `game_logic.py`" in block, block[-400:]


def test_plan_lint_respects_scope_and_does_not_convict_ui() -> None:
    """作用域内的文件判负；作用域外的文件（ui.py）**必须放行**。"""
    scopes = semantics.forbidden_module_scopes(
        semantics.build_requirement_contract(SNAKE_REQ.read_text(encoding="utf-8"), {})
    )
    plan = {"tasks": [
        {"id": "T-02", "target_files": ["game_logic.py"], "symbols": ["Game.score"],
         "contracts": {"uses": ["tkinter.event"]}},          # ← 违规：在作用域内
        {"id": "T-04", "target_files": ["ui.py"], "symbols": ["GameUI"],
         "contracts": {"uses": ["tkinter.canvas"]}},          # ← 正确：ui.py 就该用 tkinter
    ]}
    findings = planir.validate_architect_plan(plan, None, forbidden_modules=scopes)
    hit = [f for f in findings if f["code"] == "PLAN_FORBIDDEN_DEPENDENCY"]
    assert [f["task"] for f in hit] == ["T-02"], hit
    assert "作用域：game_logic.py" in hit[0]["detail"]
    # pygame 全项目禁止 ⇒ 任何文件碰它都判负
    plan2 = {"tasks": [{"id": "T-09", "target_files": ["ui.py"], "symbols": ["X"],
                        "contracts": {"uses": ["pygame.display"]}}]}
    hit2 = [f for f in planir.validate_architect_plan(plan2, None, forbidden_modules=scopes)
            if f["code"] == "PLAN_FORBIDDEN_DEPENDENCY"]
    assert hit2 and "全项目" in hit2[0]["detail"]


def test_forbidden_accepts_mapping_without_losing_scope() -> None:
    """传**映射**（{模块: 文件}）不得静默丢掉作用域 —— 那会退化成全局误伤。"""
    mapping = {"tkinter": ["game_logic.py"]}
    plan = {"tasks": [
        {"id": "T-02", "target_files": ["game_logic.py"], "symbols": ["Game.score"],
         "contracts": {"uses": ["tkinter.event"]}},
        {"id": "T-04", "target_files": ["ui.py"], "symbols": ["GameUI"],
         "contracts": {"uses": ["tkinter.canvas"]}},
    ]}
    hit = [f for f in planir.validate_architect_plan(plan, None, forbidden_modules=mapping)
           if f["code"] == "PLAN_FORBIDDEN_DEPENDENCY"]
    assert [f["task"] for f in hit] == ["T-02"], hit


def test_verify_forbidden_import_respects_scope(tmp_path: Path) -> None:
    """真产物那道检查同样按作用域：game_logic.py 里 import tkinter 判负，ui.py 里不判。"""
    scopes = semantics.forbidden_module_scopes(
        semantics.build_requirement_contract(SNAKE_REQ.read_text(encoding="utf-8"), {})
    )
    (tmp_path / "game_logic.py").write_text("import tkinter\n", encoding="utf-8")
    (tmp_path / "ui.py").write_text("import tkinter\nimport numpy\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("import tkinter\n", encoding="utf-8")
    out = verify.forbidden_import_problems(
        tmp_path, ["game_logic.py", "ui.py", "main.py"], scopes
    )
    joined = " | ".join(out)
    assert "`game_logic.py` 里 import 了 `tkinter`" in joined        # 作用域内 ⇒ 判负
    assert "tkinter" not in "".join(x for x in out if "ui.py" in x)   # ui.py 的 tkinter 放行
    assert "`ui.py` 里 import 了 `numpy`" in joined                   # 全项目禁止 ⇒ 判负
    assert not [x for x in out if "main.py" in x and "tkinter" in x]  # main.py 的 tkinter 放行


def test_plan_lint_blocks_user_forbidden_module() -> None:
    plan = {"tasks": [{"id": "T-01", "target_files": ["game_logic.py"],
                       "symbols": ["Game"], "contracts": {"uses": ["tkinter.event"]}}]}
    findings = planir.validate_architect_plan(
        plan, None, forbidden_modules=["tkinter"]
    )
    got = [f for f in findings if f["code"] == "PLAN_FORBIDDEN_DEPENDENCY"]
    assert got and got[0]["severity"] == "block"
    assert got[0]["files"] == ["game_logic.py"]
    # 不传 forbidden ⇒ 标准库模块不会被内置名单误判（tkinter 是 stdlib）
    assert not [f for f in planir.validate_architect_plan(plan, None)
                if f["code"] == "PLAN_FORBIDDEN_DEPENDENCY"]


# --------------------------------------------------------------- verify 机械检查
def test_forbidden_import_problems_catches_real_import(tmp_path: Path) -> None:
    (tmp_path / "game_logic.py").write_text(
        "import tkinter\n\nclass Game:\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "main.py").write_text(
        "from tkinter import messagebox\n\nprint('x')\n", encoding="utf-8"
    )
    (tmp_path / "ui.py").write_text("from . import game_logic\n", encoding="utf-8")
    out = verify.forbidden_import_problems(
        tmp_path, ["game_logic.py", "main.py", "ui.py"], ["tkinter"]
    )
    assert len(out) == 2, out
    assert any("game_logic.py" in x for x in out)
    assert any("main.py" in x for x in out)
    # 相对导入不是外部依赖；没传名单时零开销、零误报
    assert verify.forbidden_import_problems(tmp_path, ["ui.py"], []) == []


def test_forbidden_import_problems_survives_broken_syntax(tmp_path: Path) -> None:
    """语法坏掉的文件交给 py_compile 档去报，本检查不得崩。"""
    (tmp_path / "bad.py").write_text("def f(:\n", encoding="utf-8")
    assert verify.forbidden_import_problems(tmp_path, ["bad.py"], ["tkinter"]) == []
