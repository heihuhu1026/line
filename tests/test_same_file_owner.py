"""同文件施工边界（P0-4）回归 —— 问题 B 的根治。

问题 B：同一个新文件 `game_logic.py` 被拆成 T-01/T-02/T-03 连续 `add` 整份施工，
导致 add → add → add/replace → interface overlap → symbol collision。

机制：文件创建租约（owner）只有一个；其余同文件的图只能定点增补（modify）。
· `file_owner_map` 给出台账（谁有权整份新建、谁只能增补）
· `same_file_add_violations` 机械阻断"同一新文件多个创建者"
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import taskcompiler as tc  # noqa: E402


def _three_on_same_file() -> list[dict]:
    return [
        {"id": "T-01", "target_files": ["game_logic.py"], "symbols": ["Snake", "Food"]},
        {"id": "T-02", "target_files": ["game_logic.py"], "symbols": ["Game.score"]},
        {"id": "T-03", "target_files": ["game_logic.py"], "symbols": ["Game.is_game_over"]},
    ]


def test_owner_is_leased_to_first_task_only() -> None:
    """`_annotate_create_owners` 只给 DAG 第一张图发租约（全新项目）。"""
    tasks = _three_on_same_file()
    tc._annotate_create_owners(tasks, existing_files=set())
    owners = [t["id"] for t in tasks if t.get("creates_file")]
    assert owners == ["T-01"], owners


def test_existing_file_gets_no_creation_lease() -> None:
    """仓库已存在的文件没有"创建"动作 —— 所有图天然都是 modify。"""
    tasks = _three_on_same_file()
    tc._annotate_create_owners(tasks, existing_files={"game_logic.py"})
    assert not any(t.get("creates_file") for t in tasks)


def test_owner_map_reports_single_owner_and_modifiers() -> None:
    tasks = _three_on_same_file()
    tc._annotate_create_owners(tasks, existing_files=set())
    owners = tc.file_owner_map(tasks)
    slot = owners["game_logic.py"]
    assert slot["create_owner"] == "T-01"
    assert slot["modify_tasks"] == ["T-02", "T-03"], slot


def test_normal_add_then_modify_is_not_a_violation() -> None:
    """1 个 owner + N 张 modify 是**正常**形态，绝不能误判成 SAME_FILE_MULTI_ADD。"""
    tasks = _three_on_same_file()
    tc._annotate_create_owners(tasks, existing_files=set())
    assert tc.same_file_add_violations(tasks) == []


def test_second_creator_is_blocked() -> None:
    """绕过租约出现第二个创建者 = 同一新文件被连续 add → 机械阻断。"""
    tasks = _three_on_same_file()
    for task in tasks:
        task["creates_file"] = True  # 模拟租约被绕过
    violations = tc.same_file_add_violations(tasks)
    assert len(violations) == 1, violations
    assert violations[0]["code"] == "SAME_FILE_MULTI_ADD"
    assert violations[0]["file"] == "game_logic.py"
    assert violations[0]["create_owner"] == "T-01"
    assert violations[0]["extra_creators"] == ["T-02", "T-03"]


def test_different_files_each_get_own_owner() -> None:
    tasks = [
        {"id": "T-01", "target_files": ["game_logic.py"], "symbols": ["Snake"]},
        {"id": "T-02", "target_files": ["ui.py"], "symbols": ["GameUI"]},
    ]
    tc._annotate_create_owners(tasks, existing_files=set())
    assert tc.same_file_add_violations(tasks) == []
    owners = tc.file_owner_map(tasks)
    assert owners["game_logic.py"]["create_owner"] == "T-01"
    assert owners["ui.py"]["create_owner"] == "T-02"
