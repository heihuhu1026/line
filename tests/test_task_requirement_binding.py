"""Task 需求绑定（P0-3）回归 —— 问题 C 的根治。

问题 C：TaskCompiler 用**文件级** requirement links，同文件的每张图都继承同一批 FR
（T-01/T-02/T-03 全是 FR-01 FR-04 FR-05），任务与需求的关系失去精确性。

本轮改为按**每张图自己的 facet**（symbols/change/interface/acceptance）重新绑定：
对得上号的进 `implements_requirements`，对不上号的只留在 `candidate_requirement_ids`
（**不静默复制**）；显式声称却完全无法证实的 → `invalid_requirement_binding`。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import taskcompiler as tc  # noqa: E402

SCOPE = {
    "functional_requirements": [
        {
            "id": "FR-01",
            "description": "棋盘 20x20，蛇初始长度 3 段，初始位置居中，初始朝向向右",
            "acceptance": ["snake 初始长度为 3"],
        },
        {
            "id": "FR-04",
            "description": "吃到食物得分 +10 长度 +1，每 3 个食物速度提升一档，下限 60ms",
            "acceptance": ["score 加 10"],
        },
        {
            "id": "FR-05",
            "description": "撞墙或撞到自身游戏结束，显示最终得分，空格键重开一局",
            "acceptance": ["game_over 状态正确"],
        },
    ]
}

FILE_LEVEL = ["req:FR-01", "req:FR-04", "req:FR-05"]


def _tasks() -> list[dict]:
    """同一个 `game_logic.py` 的三张图（真机问题 B/C 的形态）。"""
    return [
        {
            "id": "T-01", "target_files": ["game_logic.py"],
            "symbols": ["Snake", "Food"], "change": "实现 Snake 与 Food 类及棋盘",
            "acceptance": "snake 初始长度 3", "implements_requirements": list(FILE_LEVEL),
        },
        {
            "id": "T-02", "target_files": ["game_logic.py"],
            "symbols": ["Game.score", "Game.speed"], "change": "实现计分与速度档位",
            "acceptance": "score 加 10", "implements_requirements": list(FILE_LEVEL),
        },
        {
            "id": "T-03", "target_files": ["game_logic.py"],
            "symbols": ["Game.is_game_over"], "change": "实现碰撞检测与游戏结束判定",
            "acceptance": "game_over 状态", "implements_requirements": list(FILE_LEVEL),
        },
    ]


def test_file_level_links_are_not_copied_to_every_task() -> None:
    """核心断言：三张图**不再**拿到完全相同的一批 FR。"""
    tasks = _tasks()
    errors: list[dict] = []
    tc._bind_requirements_by_facet(tasks, SCOPE, errors)
    sets = [tuple(sorted(t["implements_requirements"])) for t in tasks]
    assert len(set(sets)) > 1, f"三张图的需求仍然完全相同，问题 C 未修复：{sets}"
    assert all(set(s) <= set(FILE_LEVEL) for s in sets), sets


def test_unproven_requirements_stay_candidates() -> None:
    """对不上号的**不静默复制**进权威列表，只留在候选里（可被人/闸门看到）。"""
    tasks = _tasks()
    tc._bind_requirements_by_facet(tasks, SCOPE, [])
    for task in tasks:
        resolved = set(task["implements_requirements"])
        candidates = set(task.get("candidate_requirement_ids") or [])
        assert not (resolved & candidates), f"{task['id']} 同一条既权威又候选"
        assert resolved | candidates == set(FILE_LEVEL), (
            f"{task['id']} 丢了需求：权威 {sorted(resolved)} + 候选 {sorted(candidates)}"
        )


def test_explicit_but_unprovable_claim_is_rejected() -> None:
    """显式声称 FR-99，但 facet 完全无法证实 → invalid_requirement_binding（不静默接受）。"""
    tasks = [{
        "id": "T-09", "target_files": ["game_logic.py"], "symbols": ["X"],
        "change": "与需求无关的实现", "acceptance": "无关",
        "implements_requirements": ["req:FR-01"],
        "requirement_ids": ["req:FR-99"],
    }]
    errors: list[dict] = []
    tc._bind_requirements_by_facet(tasks, SCOPE, errors)
    codes = [e["code"] for e in errors]
    assert "invalid_requirement_binding" in codes, errors
    assert "req:FR-99" in errors[0]["requirements"]


def test_explicit_claim_is_kept_when_provable() -> None:
    """显式声称且 facet 能证实 → 保留进权威列表（模型候选经 compiler 认可后生效）。"""
    tasks = [{
        "id": "T-10", "target_files": ["game_logic.py"], "symbols": ["Snake"],
        "change": "实现蛇的初始状态与棋盘", "acceptance": "snake 初始长度 3",
        "implements_requirements": [], "requirement_ids": ["req:FR-01"],
    }]
    errors: list[dict] = []
    tc._bind_requirements_by_facet(tasks, SCOPE, errors)
    assert "req:FR-01" in tasks[0]["implements_requirements"]
    assert not errors


def test_no_scope_keeps_old_behavior() -> None:
    """不传 scope（老调用/单测）→ 行为不变，向后兼容。"""
    tasks = _tasks()
    tc._bind_requirements_by_facet(tasks, None, [])
    for task in tasks:
        assert task["implements_requirements"] == FILE_LEVEL


def test_po_id_maps_back_to_requirement() -> None:
    """PO id 形如 `po:FR-04:...` → 确定性反解出 req:FR-04（不靠猜）。"""
    assert tc._req_of("po:FR-04:abc") == "req:FR-04"
    assert tc._req_of("") == ""
