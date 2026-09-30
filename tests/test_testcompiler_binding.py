"""TestCompiler 权威绑定（P0-10）回归。

核心：**LLM 的 target_po 只是候选，compiler 才是权威**（§12.1/§12.2）。
定不了就 UNBOUND —— 不猜，也不静默塞给 delivery PO
（那会让一条无关命令"证明"了交付，正是"一条 Evidence 证明多个无关 PO"的入口）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import ontology  # noqa: E402
from pipeline import testcompiler as tc  # noqa: E402


def _po(pid: str, kind: str, claim: str = "某条待证声明") -> dict:
    return {"id": pid, "kind": kind, "required": True, "claim": claim, "name": pid}


REQUIRED = [
    _po("po:syntax", ontology.PO_KIND_SYNTAX),
    _po("po:import", ontology.PO_KIND_IMPORT),
    _po("po:behavior", ontology.PO_KIND_BEHAVIOR),
    _po("po:delivery", ontology.PO_KIND_DELIVERY),
]


# ---------------------------------------------------------------- §12.2 一致性
def test_candidate_not_in_required_is_unbound() -> None:
    """声称了一个根本不在 required 里的 PO → UNBOUND（不猜）。"""
    assert tc.bind_target_po("po:不存在", "python main.py", [], REQUIRED) == ""


def test_empty_candidate_is_unbound() -> None:
    assert tc.bind_target_po("", "python main.py", [], REQUIRED) == ""


def test_syntax_po_needs_syntax_command() -> None:
    """syntax 类 PO 却给了一条运行命令 → 形态不符，UNBOUND。"""
    assert tc.bind_target_po("po:syntax", "python main.py", ["exit_code==0"], REQUIRED) == ""
    assert tc.bind_target_po(
        "po:syntax", "python -m py_compile game_logic.py", ["exit_code==0"], REQUIRED
    ) == "po:syntax"


def test_import_po_needs_import_command() -> None:
    assert tc.bind_target_po("po:import", "python main.py", [], REQUIRED) == ""
    assert tc.bind_target_po(
        "po:import", 'python -c "import game_logic"', ["exit_code==0"], REQUIRED
    ) == "po:import"


def test_behavior_po_binds_and_weakness_is_handled_elsewhere() -> None:
    """行为类：绑定成立；断言强弱（只有 rc=0）由 weak 逻辑单独处理，不算"绑错"。"""
    assert tc.bind_target_po(
        "po:behavior", "python -m unittest game_logic_test", ["exit_code==0"], REQUIRED
    ) == "po:behavior"


def test_delivery_po_binds() -> None:
    assert tc.bind_target_po("po:delivery", "python main.py", [], REQUIRED) == "po:delivery"


# ---------------------------------------------------------------- §12.1 端到端
def test_misbinding_becomes_unbound_not_silently_delivery() -> None:
    """端到端：LLM 把一条运行命令标成 syntax PO → 不得静默归给 delivery。

    这正是「一条 Evidence 证明了无关的多个 PO」的入口：旧逻辑会把证不成的命令
    塞进 delivery PO，于是同一次运行同时"证明"了语法和交付。
    """
    compiled = tc.compile_scenarios(
        obligations=REQUIRED,
        planned_commands=[{
            "command": "python main.py",
            "source": "test-llm",
            "assertions": ["exit_code==0"],
            "target_po_candidate": "po:syntax",
        }],
        files=["main.py", "game_logic.py"],
    )
    unbound = compiled.get("unbound_commands") or []
    assert unbound, "证不成的候选没有被记为 UNBOUND"
    assert unbound[0]["candidate"] == "po:syntax"
    # 关键：这条命令不得出现在**任何** PO 的场景动作里
    # （delivery PO 本身会有一个 unproven 空场景，那是正常的，不代表命令被归给了它）
    used = []
    for s in compiled.get("scenarios") or []:
        for a in s.get("actions") or ():
            cmd = a.get("command") if isinstance(a, dict) else getattr(a, "command", "")
            if str(cmd or "").strip() == "python main.py":
                used.append(str(s.get("target_po") or ""))
    assert not used, f"证不成的命令被静默归给了：{used}"


def test_legacy_target_po_key_still_honored() -> None:
    """旧字段 `target_po` 仍被读取（向后兼容），但一样要过一致性校验。"""
    compiled = tc.compile_scenarios(
        obligations=REQUIRED,
        planned_commands=[{
            "command": "python -m py_compile game_logic.py",
            "source": "test-llm",
            "assertions": ["exit_code==0"],
            "target_po": "po:syntax",  # 旧名
        }],
        files=["game_logic.py"],
    )
    assert not (compiled.get("unbound_commands") or []), compiled.get("unbound_commands")


def test_behavior_po_with_only_exit_code_is_weak() -> None:
    """§12.4：行为类 PO 只有 exit_code==0 ⇒ WEAK（rc=0 不证明任何需求行为）。"""
    compiled = tc.compile_scenarios(
        obligations=[_po("po:behavior", ontology.PO_KIND_BEHAVIOR)],
        planned_commands=[{
            "command": "python main.py",
            "source": "test-llm",
            "assertions": ["exit_code==0"],
            "target_po_candidate": "po:behavior",
        }],
        files=["main.py"],
    )
    scenarios = [s for s in (compiled.get("scenarios") or [])
                 if str(s.get("target_po") or "") == "po:behavior"]
    assert scenarios and str(scenarios[0].get("status")) == tc.STATUS_WEAK
