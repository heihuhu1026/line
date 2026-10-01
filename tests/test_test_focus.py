"""P0-12：测试用例类别按**任务类型**聚焦（不再"每轮 new/regression/compat 三类缺一不可"）。

真机教训：为凑类别，7B 会写一大堆与本次变更无关的用例 —— token 白烧，真正的缺口反被淹。
口径只有一份真源（`tasktype`），提示词与机械审计**共用**它，两类之间不会漂移。

安全边界（只放宽不收紧）：
    必需集合恒为旧三类（new/regression/compat）的**子集** ⇒ 不可能凭空多出模型产不出的类别；
    建议类别（contract/interface）必须在 schema enum 内，且只作提示级、不判负。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import prompts, schemas, tasktype  # noqa: E402

_LEGACY = {"new", "regression", "compat"}


def test_case_types_share_one_source() -> None:
    assert tuple(tasktype.TEST_CASE_TYPES) == tuple(schemas.CASE_TYPE)


def test_feature_first_round_only_needs_new() -> None:
    assert tasktype.expected_test_types(tasktype.FEATURE) == ("new",)
    assert tasktype.expected_test_types(tasktype.FEATURE, has_existing_surface=True) == (
        "new", "regression")


def test_bugfix_focuses_on_regression() -> None:
    assert tasktype.expected_test_types(tasktype.BUGFIX) == ("regression",)
    assert tasktype.suggested_test_types(tasktype.BUGFIX) == ("compat",)


def test_plan_rework_suggests_contract_interface() -> None:
    assert tasktype.expected_test_types(tasktype.PLAN_REWORK) == ("new",)
    suggested = tasktype.suggested_test_types(tasktype.PLAN_REWORK)
    assert suggested == ("contract", "interface")
    assert all(t in tasktype.TEST_CASE_TYPES for t in suggested)


def test_required_sets_only_relax() -> None:
    """只放宽不收紧：任何一轮的必需类别都是旧三类的子集。"""
    for kind in (*tasktype.ROUND_KINDS, "", "nope"):
        assert set(tasktype.expected_test_types(kind)) <= _LEGACY


def test_focus_guidance_names_required_and_suggested() -> None:
    text = tasktype.test_focus_guidance(tasktype.BUGFIX)
    assert "缺陷修复" in text
    assert "必需类别：regression" in text
    text2 = tasktype.test_focus_guidance(tasktype.PLAN_REWORK)
    assert "方案返工后的施工" in text2 and "建议补充（不判负）：contract / interface" in text2


def test_parts_test_carries_focus_and_legacy_falls_back() -> None:
    parts = prompts.parts_test(
        "需求", {}, {"changes": [], "tasks": []},
        test_view={"cases": []},
        test_focus=tasktype.test_focus_guidance(tasktype.FEATURE),
    )
    joined = "\n".join(p for p in parts if p)
    assert "本轮测试聚焦" in joined
    assert "按上方的**本轮测试聚焦**产出用例" in joined
    assert "三类测试用例" not in joined
    # 不传 test_focus（老复跑脚本）⇒ 回退旧措辞，行为不变
    legacy = "\n".join(p for p in prompts.parts_test("需求", {}, {}) if p)
    assert "产出新功能/回归/兼容三类测试用例" in legacy


def test_test_system_prompts_no_longer_hardcode_three_types() -> None:
    for project_type in ("secondary", "new"):
        for kind in tasktype.ROUND_KINDS:
            text = prompts.system_prompt("test", project_type, kind)
            assert "三类**缺一不可**" not in text, (project_type, kind)
    # 但仍要指明口径来源（否则模型没有依据）
    assert "本轮测试聚焦" in prompts.system_prompt("test", "new", tasktype.FEATURE)
