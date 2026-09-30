"""Grounded Requirement Contract（P0-1）回归。

用**真机 run 20260930-000332 的原始需求**做回放基准（已固化为
`tools/_repro/requirement_snake_20260930.txt`）—— 不依赖 `runs/`（那是 gitignore 的）。

核心要证明的事：
  1. 用户明确写的 5 个文件（含两个测试文件）**必须**进 declared_files；
  2. 硬约束（game_logic.py 不得 import tkinter）必须被抓到，且 source_quote 可逐字校验；
  3. 就算 Intake 漏掉文件，契约仍能从**原文**恢复（契约不靠 Intake 施舍）；
  4. Intake 自己发明的内容只能是 DERIVED，**绝不污染** ASSERTED。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.semantics import build_requirement_contract  # noqa: E402

FIXTURE = ROOT / "tools" / "_repro" / "requirement_snake_20260930.txt"

ASSERTED_BUCKETS = (
    "declared_files",
    "hard_constraints",
    "explicit_exclusions",
    "acceptance_items",
    "behavior_claims",
    "source_facts",
)


@pytest.fixture(scope="module")
def requirement() -> str:
    if not FIXTURE.is_file():
        pytest.skip(f"缺少回放夹具：{FIXTURE}")
    return FIXTURE.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 1. declared files
def test_declared_files_keep_all_five_user_files(requirement: str) -> None:
    """问题 A 的根因：测试文件在 Architect 阶段凭空消失。

    契约必须在**最上游**就把它们钉成 ASSERTED，后面谁删谁违规。
    """
    contract = build_requirement_contract(requirement)
    paths = {f["path"] for f in contract["declared_files"]}
    expected = {"main.py", "game_logic.py", "ui.py", "game_logic_test.py", "ui_test.py"}
    missing = expected - paths
    assert not missing, f"用户明确声明的文件在契约里丢了：{sorted(missing)}（实得 {sorted(paths)}）"


def test_declared_files_are_asserted_and_quoted(requirement: str) -> None:
    contract = build_requirement_contract(requirement)
    for item in contract["declared_files"]:
        assert item["truth"] == "ASSERTED", item
        assert item["source"] == "user", item
        # source_quote 必须能在原文逐字找到 —— 这是 ASSERTED 的唯一凭据
        assert item["source_quote"] and item["source_quote"] in requirement, item


def test_table_rows_are_high_confidence(requirement: str) -> None:
    """真机原文是 Markdown 表格（且被拍平成单行，靠 || 分隔），应识别为最高置信。"""
    contract = build_requirement_contract(requirement)
    by_path = {f["path"]: f for f in contract["declared_files"]}
    assert by_path["game_logic_test.py"]["evidence"] == "markdown_table", by_path["game_logic_test.py"]
    assert by_path["game_logic_test.py"]["confidence"] == "high"


# ---------------------------------------------------------------- 2. hard constraints
def test_tkinter_hard_constraint_is_captured(requirement: str) -> None:
    contract = build_requirement_contract(requirement)
    hits = [
        c for c in contract["hard_constraints"]
        if "tkinter" in c["source_quote"] and "不得" in c["source_quote"]
    ]
    assert hits, (
        "「game_logic.py 不得 import tkinter」这条硬约束没被抓到；"
        f"实得约束：{[c['source_quote'][:40] for c in contract['hard_constraints']]}"
    )
    assert hits[0]["truth"] == "ASSERTED"
    assert hits[0]["severity"] == "hard"
    # 必须逐字可查，不能是模型/规则编出来的转述
    assert hits[0]["source_quote"] in requirement


def test_stdlib_only_constraint_is_captured(requirement: str) -> None:
    contract = build_requirement_contract(requirement)
    blob = json.dumps(contract["hard_constraints"], ensure_ascii=False)
    assert "标准库" in blob, "「只允许使用 Python 标准库」缺失"
    assert "禁止" in blob or "pygame" in blob, "禁止第三方库（pygame/numpy）缺失"


# ---------------------------------------------------------------- 3. intake 漏文件
def test_contract_recovers_files_even_if_intake_drops_them(requirement: str) -> None:
    """Intake 漏掉文件 → 契约照样从 original_requirement 恢复。

    这正是问题 A 的免疫：契约的源头是**用户原文**，不是 Intake 的转述。
    """
    intake = {
        "background": "一个贪吃蛇小游戏",
        "key_behaviors": ["移动", "吃食物"],
        # 故意完全不提任何文件
    }
    contract = build_requirement_contract(requirement, intake=intake)
    paths = {f["path"] for f in contract["declared_files"]}
    assert "game_logic_test.py" in paths
    assert "ui_test.py" in paths


# ---------------------------------------------------------------- 4. intake 不得污染
def test_intake_invented_content_stays_derived(requirement: str) -> None:
    """Intake 自己发明的「目标用户」只能进 derived_facts，绝不污染 ASSERTED。"""
    invented = "面向 8-12 岁儿童的编程教育初学者"
    contract = build_requirement_contract(
        requirement, intake={"target_users": invented, "background": "随便编的背景"}
    )
    # 必须出现在 derived_facts
    assert invented in json.dumps(contract["derived_facts"], ensure_ascii=False), (
        "Intake 产物没进 derived_facts"
    )
    # derived_facts 一律 DERIVED
    for item in contract["derived_facts"]:
        assert item["truth"] == "DERIVED", item
        assert item["source"] == "intake", item
    # 绝不能出现在任何 ASSERTED 集合
    for bucket in ASSERTED_BUCKETS:
        blob = json.dumps(contract[bucket], ensure_ascii=False)
        assert invented not in blob, f"Intake 发明的内容污染了 ASSERTED 集合：{bucket}"


# ---------------------------------------------------------------- 不变量与「不猜」
def test_every_asserted_item_has_verifiable_quote(requirement: str) -> None:
    contract = build_requirement_contract(requirement)
    for bucket in ASSERTED_BUCKETS:
        for item in contract[bucket]:
            if item.get("truth") == "ASSERTED":
                assert item.get("source_quote") in requirement, f"{bucket}: {item}"


def test_no_context_filename_is_not_guessed() -> None:
    """只出现文件名、没有任何表格/文件上下文 → 必须进 grounding_errors，不得猜。"""
    text = "参考 config.json 的写法来组织配置。"
    contract = build_requirement_contract(text)
    paths = {f["path"] for f in contract["declared_files"]}
    assert "config.json" not in paths, "无上下文的文件名被猜成了 declared file"
    codes = {e["code"] for e in contract["grounding_errors"]}
    assert "FILE_NO_CONTEXT" in codes, contract["grounding_errors"]


def test_empty_requirement_is_reported_not_invented() -> None:
    contract = build_requirement_contract("", intake={"background": "x"})
    codes = {e["code"] for e in contract["grounding_errors"]}
    assert "EMPTY_REQUIREMENT" in codes
    assert contract["declared_files"] == []


def test_contract_shape_is_stable(requirement: str) -> None:
    """字段集稳定（新字段可加，既有字段不得消失 —— 向后兼容旧 run）。"""
    contract = build_requirement_contract(requirement)
    for key in ("version", "declared_files", "hard_constraints", "explicit_exclusions",
                "acceptance_items", "behavior_claims", "source_facts",
                "derived_facts", "grounding_errors"):
        assert key in contract, key
    assert contract["version"] == 1
