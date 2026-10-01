"""P0-13：把行为项交给 PO → compiler **机械分类**（mechanical / unit / gui_smoke /
resident / human_only / unclassified）。

为什么必须有：真机里 GUI / 常驻 / 人工三类义务被当成"应该有单测"，于是要么逼模型编造
断言、要么被记成说不清的缺口。分类把"机械不可验"与"忘了测"分开 —— 前者单列
`external_required`，不该被当成重问素材（重问也修不出来）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import ontology, testcompiler as tc  # noqa: E402


def _po(pid: str, kind: str, claim: str = "") -> dict:
    return {"id": pid, "kind": kind, "required": True, "claim": claim or pid, "name": claim or pid}


def test_mechanical_kinds_are_classified_mechanical() -> None:
    for kind in (ontology.PO_KIND_SYNTAX, ontology.PO_KIND_IMPORT,
                 ontology.PO_KIND_MATERIALIZATION, ontology.PO_KIND_DELIVERY,
                 ontology.PO_KIND_CONTRACT, ontology.PO_KIND_INTERFACE_FREEZE):
        assert tc.classify_obligation(_po("po:x", kind)) == tc.MODE_MECHANICAL, kind


def test_gui_and_resident_are_downgraded_not_unit() -> None:
    """界面/常驻字面证据 → gui_smoke / resident（本沙箱只能冒烟，不能断言画面与手感）。"""
    assert tc.classify_obligation(
        _po("po:ui", ontology.PO_KIND_BEHAVIOR, "窗口标题显示得分与速度")
    ) == tc.MODE_GUI_SMOKE
    assert tc.classify_obligation(
        _po("po:loop", ontology.PO_KIND_BEHAVIOR, "主循环持续运行直到退出")
    ) == tc.MODE_RESIDENT


def test_human_only_requires_explicit_evidence() -> None:
    """只有明确写了"美观/主观/人工确认"才判 human_only —— 不轻易把可单测的义务降档。"""
    assert tc.classify_obligation(
        _po("po:h", ontology.PO_KIND_BEHAVIOR, "配色美观度需人工确认")
    ) == tc.MODE_HUMAN
    # 普通可观测行为仍是 unit（不能因为措辞朴素就判"只能人工"）
    assert tc.classify_obligation(
        _po("po:u", ontology.PO_KIND_BEHAVIOR, "吃到食物后得分 +10")
    ) == tc.MODE_UNIT


def test_short_token_does_not_misfire() -> None:
    """`ui` / `gui` 这类两字母不能命中 `build` / `guide` 之类的无关词。"""
    assert tc.classify_obligation(
        _po("po:b", ontology.PO_KIND_BEHAVIOR, "build guide 生成文档")
    ) == tc.MODE_UNIT


def test_compile_scenarios_exposes_modes_and_external() -> None:
    compiled = tc.compile_scenarios(
        obligations=[
            _po("po:ui", ontology.PO_KIND_BEHAVIOR, "绘制蛇身颜色"),
            _po("po:u", ontology.PO_KIND_BEHAVIOR, "吃到食物得分 +10"),
            _po("po:ctr", ontology.PO_KIND_CONTRACT, "跨文件调用链一致"),
        ],
        files=["main.py"],
    )
    modes = compiled["verification_modes"]
    assert modes[tc.MODE_GUI_SMOKE] == ["po:ui"]
    assert modes[tc.MODE_UNIT] == ["po:u"]
    assert modes[tc.MODE_MECHANICAL] == ["po:ctr"]
    # GUI 类无命令 ⇒ 单列 external_required，而不是与"忘了测"混在一起
    assert compiled["external_required"] == ["po:ui"]
    by = {s["target_po"]: s for s in compiled["scenarios"]}
    assert by["po:ui"]["verification_mode"] == tc.MODE_GUI_SMOKE
    assert "gui_smoke" in by["po:ui"]["gap_reason"]


def test_gate_reports_external_required() -> None:
    required = [_po("po:ui", ontology.PO_KIND_BEHAVIOR, "绘制蛇身颜色")]
    compiled = tc.compile_scenarios(obligations=required, files=["main.py"])
    gate = tc.proof_coverage_gate(required, compiled)
    assert gate["external_required"] == ["po:ui"]
    # 只报事实：GUI 义务仍是未证明（绝不因"分类成冒烟"就当成 PASS）
    assert gate["covered"] == 0 and gate["unexecutable"] == 1
