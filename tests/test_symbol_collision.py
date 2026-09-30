"""符号冲突的确定性 AST 检查（P0-9）+ Symbol Manifest（P0-8）。

真机形态：`Game.is_game_over` 同时是属性（self.is_game_over = False）和方法
（def is_game_over(self)）—— 运行期方法被 bool 覆盖，必须**在 DEV 刚产出时**
就被机械抓住，不能等 pyright / review。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import symbols  # noqa: E402


# ---------------------------------------------------------------- P0-9 冲突
def test_attribute_and_method_same_name_is_blocked() -> None:
    """真机 `Game.is_game_over` 形态：属性赋值 + 同名方法。"""
    source = (
        "class Game:\n"
        "    def __init__(self):\n"
        "        self.is_game_over = False\n"
        "\n"
        "    def is_game_over(self):\n"
        "        return self.snake[0] in self.snake[1:]\n"
    )
    hits = symbols.validate_symbol_collisions({"game_logic.py": source})
    codes = [h["code"] for h in hits]
    assert "SYMBOL_MEMBER_COLLISION" in codes, hits
    hit = next(h for h in hits if h["code"] == "SYMBOL_MEMBER_COLLISION")
    assert hit["severity"] == "block"
    assert hit["symbol"] == "Game.is_game_over"
    assert hit["file"] == "game_logic.py"


def test_annotated_attribute_also_counts() -> None:
    """带类型标注的属性（`self.x: bool = False`）同样算属性定义。"""
    source = (
        "class A:\n"
        "    def __init__(self) -> None:\n"
        "        self.ok: bool = False\n"
        "\n"
        "    def ok(self) -> bool:\n"
        "        return True\n"
    )
    codes = [h["code"] for h in symbols.validate_symbol_collisions({"a.py": source})]
    assert "SYMBOL_MEMBER_COLLISION" in codes, codes


def test_duplicate_top_level_symbol_is_blocked() -> None:
    source = "class Game:\n    pass\n\ndef Game():\n    pass\n"
    codes = [h["code"] for h in symbols.validate_symbol_collisions({"g.py": source})]
    assert "DUPLICATE_TOP_LEVEL_SYMBOL" in codes, codes


def test_duplicate_method_is_blocked() -> None:
    source = (
        "class Game:\n"
        "    def start(self):\n"
        "        pass\n"
        "\n"
        "    def start(self):\n"
        "        pass\n"
    )
    codes = [h["code"] for h in symbols.validate_symbol_collisions({"g.py": source})]
    assert "DUPLICATE_METHOD" in codes, codes


def test_clean_code_has_no_findings() -> None:
    """合法代码不得被误判（误报的代价是一整轮返工）。"""
    source = (
        "class Game:\n"
        "    def __init__(self) -> None:\n"
        "        self.score = 0\n"
        "        self.finished = False\n"
        "\n"
        "    def is_game_over(self) -> bool:\n"
        "        return self.finished\n"
    )
    assert symbols.validate_symbol_collisions({"game_logic.py": source}) == []


def test_syntax_error_is_skipped_not_crashed() -> None:
    """语法坏掉的文件不在这里报（交给语法档），但**不能崩**。"""
    assert symbols.validate_symbol_collisions({"bad.py": "def (:\n"}) == []


def test_empty_and_junk_input_is_safe() -> None:
    assert symbols.validate_symbol_collisions({}) == []
    assert symbols.validate_symbol_collisions({"a.py": ""}) == []
    assert symbols.validate_symbol_collisions({"a.py": None}) == []  # type: ignore[dict-item]


# ---------------------------------------------------------------- P0-8 清单
def test_manifest_is_built_from_compiler_tasks() -> None:
    """清单来源是 TaskCompiler + 骨架，**不从 DEV 代码反推**。"""
    tasks = [
        {"target_files": ["game_logic.py"], "symbols": ["Game", "Snake", "Food"]},
        {"target_files": ["ui.py"], "symbols": ["GameUI"]},
        {"target_files": ["main.py"], "symbols": ["main"]},
    ]
    manifest = symbols.build_symbol_manifest(tasks)
    assert manifest["version"] == 1
    assert manifest["files"]["game_logic.py"] == ["Game", "Snake", "Food"]
    assert manifest["files"]["ui.py"] == ["GameUI"]
    assert "taskcompiler" in manifest["sources"]


def test_manifest_merges_skeleton() -> None:
    tasks = [{"target_files": ["game_logic.py"], "symbols": ["Game"]}]
    skeleton = {"files": {"game_logic.py": {"symbols": ["Snake"]}, "ui.py": ["GameUI"]}}
    manifest = symbols.build_symbol_manifest(tasks, skeleton)
    assert manifest["files"]["game_logic.py"] == ["Game", "Snake"]
    assert "skeleton" in manifest["sources"]


def test_planned_vs_actual_reports_only_missing() -> None:
    """只判**声明了却没写出来**；多写辅助符号不判罪。"""
    manifest = symbols.build_symbol_manifest(
        [{"target_files": ["game_logic.py"], "symbols": ["Game", "Snake"]}]
    )
    diff = symbols.planned_vs_actual(manifest, {"game_logic.py": ["Game", "Helper"]})
    assert diff["missing"] == {"game_logic.py": ["Snake"]}
    assert diff["missing_count"] == 1

    full = symbols.planned_vs_actual(manifest, {"game_logic.py": ["Game", "Snake", "Extra"]})
    assert full["missing_count"] == 0
