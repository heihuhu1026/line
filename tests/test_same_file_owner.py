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


# ---------------------------------------------------------------- DEV 侧租约执法
def _bare_orchestrator(attempt: int = 3):
    """不跑 __init__ 的裸实例：这两个方法只用 state/attempt/client 三个东西。"""
    from pipeline.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch.state = {}
    orch.attempt = attempt
    orch.client = object()  # 不是 MockClient ⇒ 走真实执法分支
    return orch


def _whole_file_add(path: str, symbol: str) -> dict:
    return {"path": path, "change_type": "add", "target_symbol": symbol,
            "patch": "x = 1\n"}


def test_enforce_file_lease_drops_non_owner_add_with_named_code() -> None:
    """非 owner 的整份 add 被丢弃，且留下**具名**判据（不是一句人话）。"""
    orch = _bare_orchestrator(attempt=3)
    data = {"edits": [_whole_file_add("game_logic.py", "game_logic")]}
    violations = orch._enforce_file_lease({"id": "T-02"}, data, {"game_logic.py": "T-01"})
    assert len(violations) == 1 and "创建租约属于 T-01" in violations[0]
    assert data["edits"] == [], "越权的整份 add 没有被机械丢弃"
    rec = orch.state["file_lease_violations"][0]
    assert rec["code"] == "SAME_FILE_MULTI_ADD"
    assert (rec["task"], rec["path"], rec["owner"], rec["round"]) == \
        ("T-02", "game_logic.py", "T-01", 3)


def test_enforce_file_lease_keeps_owner_and_symbol_scoped_add() -> None:
    """owner 自己的整份 add、以及非 owner 的**定点**符号 add 都必须放行。"""
    orch = _bare_orchestrator()
    owner_data = {"edits": [_whole_file_add("game_logic.py", "game_logic")]}
    assert orch._enforce_file_lease({"id": "T-01"}, owner_data,
                                    {"game_logic.py": "T-01"}) == []
    assert len(owner_data["edits"]) == 1
    scoped = {"edits": [_whole_file_add("game_logic.py", "Food")]}
    assert orch._enforce_file_lease({"id": "T-02"}, scoped,
                                    {"game_logic.py": "T-01"}) == []
    assert len(scoped["edits"]) == 1


def test_lease_violation_becomes_named_mechanical_blocker() -> None:
    """被丢弃的正文不能静默：本轮越权必须成为具名机械阻断（评审 pass 也改判）。"""
    orch = _bare_orchestrator(attempt=3)
    orch._enforce_file_lease({"id": "T-02"},
                             {"edits": [_whole_file_add("game_logic.py", "game_logic")]},
                             {"game_logic.py": "T-01"})
    blockers = orch._lease_blockers()
    assert len(blockers) == 1 and blockers[0].startswith("SAME_FILE_MULTI_ADD")


def test_stale_lease_violation_does_not_block_next_round() -> None:
    """上一轮的越权随轮次结束失效 —— 否则每轮 dev 都被陈旧记录判负、永久空转。"""
    orch = _bare_orchestrator(attempt=3)
    orch._enforce_file_lease({"id": "T-02"},
                             {"edits": [_whole_file_add("game_logic.py", "game_logic")]},
                             {"game_logic.py": "T-01"})
    orch.attempt = 4  # 进入下一轮
    assert orch._lease_blockers() == []


def test_lease_blocker_dedupes_repeat_enforcement() -> None:
    """同一任务首版 + 重问两次执法留下等价记录 ⇒ 只报一条阻断。"""
    orch = _bare_orchestrator(attempt=3)
    owner_map = {"game_logic.py": "T-01"}
    for _ in range(2):
        orch._enforce_file_lease(
            {"id": "T-02"},
            {"edits": [_whole_file_add("game_logic.py", "game_logic")]},
            owner_map,
        )
    assert len(orch._lease_blockers()) == 1
