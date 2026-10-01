"""Phase H（§36/§37）：评审上下文的机械摘要**在最前** + llm-calls 的语义上下文 telemetry。

§36 的动因：`budget.fit_prompt` 从**末尾**截断，而评审是上下文最紧的阶段（8K）。
机械事实若排在后面会被整段吃掉 —— 语义层于是只能凭措辞猜"该不该放行"。
§37 的动因：`llm-calls.jsonl` 要能直接回答"这轮 review 为什么 context 这么大"。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import budget, prompts, runstore  # noqa: E402


def _summary() -> dict:
    return {
        "verify": {"verdict": "fail", "commands": 3, "failed": 1},
        "patch": {"applied": 5, "problems": 1},
        "test": {"covered_symbols": 4, "missing_symbols": 2,
                 "expected_types": ["new", "regression"], "missing_types": ["regression"],
                 "external_required": ["po:ui"]},
        "proof": {"obligations": 23, "evidence": 26, "status": "UNPROVEN"},
        "ontology_errors": 0,
        "workspace": {"revision": "ws-006", "status": "VERIFIED"},
        "defects": {"green": 2, "red": 1, "unverifiable": 0},
        "blockers": ["补丁未能套用到 main.py"],
    }


def test_block_renders_each_mechanical_fact() -> None:
    text = prompts.mechanical_summary_block(_summary())
    for needle in ("运行验证：fail", "执行 3 条命令，失败 1 条",
                   "证明义务：23 条 / 证据 26 条", "门 UNPROVEN",
                   "补丁物化：套用 5 条 / 机械问题 1 条",
                   "本轮必需类别 new/regression（缺 regression）",
                   "需外部/人工确认 1 条",
                   "语义完整性：0 个 error",
                   "工作区：ws-006 VERIFIED",
                   "逐项验收：转绿 2 / 仍失败 1 / 无从核对 0",
                   "机械阻断项：1 条"):
        assert needle in text, needle


def test_missing_fields_are_omitted_not_invented() -> None:
    """老 run / 尚未建图 ⇒ 省略该行，**绝不臆造**（0 与"没有数据"是两回事）。"""
    text = prompts.mechanical_summary_block({"blockers": []})
    assert "运行验证" not in text and "工作区" not in text
    # 机器零阻断是**值得说**的事实（与"没有数据"不同）
    assert "机械阻断项：0 条" in text
    # 完全没有机械事实 ⇒ 不渲染空壳标题（不占评审那点紧张预算）
    assert prompts.mechanical_summary_block(None) == ""
    assert prompts.mechanical_summary_block({}) == ""


def test_summary_is_first_part_and_survives_tail_truncation() -> None:
    """摘要在 parts 的**最前**：fit_prompt 从末尾截断 ⇒ 它必然存活。"""
    block = prompts.mechanical_summary_block(_summary())
    parts = prompts.parts_review(
        "需求原文", {"acceptance_criteria": []}, {"changes": []},
        {"edits": []}, {"cases": []}, summary_block=block,
    )
    assert parts[0] == block
    fitted, _ = budget.fit_prompt(parts, budget_tokens=600)
    assert "机械摘要" in fitted
    # 不传 summary_block 时行为不变（老调用点）
    legacy = prompts.parts_review("需求原文", {}, {}, {}, {})
    assert "机械摘要" not in "\n".join(legacy)


def test_call_fields_carry_semantic_context() -> None:
    """§37：固定列必须含语义上下文规模，否则聚合脚本取不到。"""
    for key in ("proof_obligation_count", "evidence_count"):
        assert key in runstore.CALL_FIELDS
    rec = runstore.normalize_call_record(
        {"stage": "review", "proof_obligation_count": 23, "evidence_count": 26}
    )
    assert rec["proof_obligation_count"] == 23 and rec["evidence_count"] == 26
    # 缺值时补 None（旧记录读得安全，不臆造 0）
    assert runstore.normalize_call_record({"stage": "dev"})["proof_obligation_count"] is None
