"""**Task Compiler**：把架构师的「变更边界」确定性翻译成带字段的施工图。

为什么要有这一层（真机反复验证的结论）
-----------------------------------
让 14B/8K 的架构师**同时**做三件事 —— 架构判断 + 技术经理（拆任务）+ 施工队长（写字段）
—— 必然顾此失彼。真机 `20260927-011207` 出 5 版方案：

  · 文件划分次次不同（每版都重新设计）
  · `symbols`/`test_hint`/`change` **100% 为空**（强制重做 2 次也填不出来）

而它**做得到**边界判断：`impact_areas` 里明确写了「数据库操作 / 命令行解析 / 错误处理」。
所以分工改成：

    Architect  → 只输出「改哪些文件、动哪些符号」（能力内）
    Compiler   → 机械翻译成 tasks（确定性：字段必然齐全、粒度必然可控、每次必然一致）

刻意不做的事
------------
- **不重新理解架构**：编译器不做任何语义判断，只做确定性的分组与编号。
- **不无条件覆盖**：架构师给出的 tasks 若已经合格（字段齐全、粒度合规），就保留它的
  （它可能带了更贴合设计的 acceptance 文本）。只在**不合格**时才接管。
"""
from __future__ import annotations

from typing import Any

from .config import MAX_TASKS_PER_FILE

#: 单张施工图最多承载几个符号（与 dev 单轮输出能力匹配）
MAX_SYMBOLS_PER_TASK = 4


def _norm(path: Any) -> str:
    return str(path or "").replace("\\", "/").strip()


def _module_of(path: str) -> str:
    """`ledger/db.py` → `ledger.db`（用于生成可执行的 import 验收命令）。"""
    p = _norm(path)
    if p.endswith(".py"):
        p = p[: -3]
    return p.replace("/", ".")


def _chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i: i + size] for i in range(0, len(items), size)] or [[]]


def compile_tasks(plan: Any, *, max_symbols: int = MAX_SYMBOLS_PER_TASK) -> list[dict]:
    """从方案的 `changes[]`（变更边界）**确定性**生成 tasks。

    规则（全部可预测、可断言）：
      ① 每个 change 至少一张图；没声明 symbols 时整文件一张；
      ② 符号数超过 `max_symbols` 就切分，保证单轮写得完；
      ③ 单文件天然只由它自己的图覆盖（不会触发"同一文件被多张图覆盖"的告警）；
      ④ 线性 `depends_on`：后一张依赖前一张，保证跨文件接口先建后引用；
      ⑤ `test_hint` 机械生成（Python 项目即 `python -c "import <module>"`），
         不再指望模型填 —— 它**从来没填过**。
    """
    plan = plan if isinstance(plan, dict) else {}
    changes = [c for c in (plan.get("changes") or []) if isinstance(c, dict) and c.get("path")]
    if not changes:
        return []
    tasks: list[dict] = []
    seq = 0
    prev_id = ""
    for change in changes:
        path = _norm(change.get("path"))
        symbols = [str(s).strip() for s in (change.get("symbols") or []) if str(s).strip()]
        if symbols:
            # 切分大小同时受两个上限约束：单张不超过 max_symbols（写得完），
            # 且**同一文件的图数不超过 MAX_TASKS_PER_FILE**（避免同一文件被反复改动）。
            # 后者优先：真机 192001 里 command.py 被 3 张图覆盖，直接引发合并/anchor 冲突。
            size = max(max_symbols, -(-len(symbols) // MAX_TASKS_PER_FILE))
            groups = _chunk(symbols, size)
        else:
            groups = [[]]
        for group in groups:
            seq += 1
            tid = f"T-{seq:02d}"
            if group:
                title = f"实现 {path} 的 {'、'.join(group)}"
                acceptance = "；".join(f"{s} 定义完整且可被导入调用" for s in group)
            else:
                title = f"实现 {path}"
                acceptance = f"{path} 可被导入，且承载 intent 描述的能力"
            task: dict[str, Any] = {
                "id": tid,
                "title": title,
                # 「改什么」：优先用架构师写好的 approach，退回 intent
                "change": str(change.get("approach") or change.get("intent") or "").strip()
                or f"按设计实现 {path}",
                "target_files": [path],
                "acceptance": acceptance,
                "symbols": list(group),
                "depends_on": [prev_id] if prev_id else [],
            }
            if path.endswith(".py"):
                task["test_hint"] = f'python -c "import {_module_of(path)}"'
            tasks.append(task)
            prev_id = tid
    return tasks


def plan_needs_compile(plan: Any, *, max_tasks: int = 5, max_files_per_task: int = 2) -> list[str]:
    """方案自带的 tasks 是否**不合格**（于是该由编译器接管）。

    返回不合格的原因列表；空列表 = 合格，保留架构师自己拆的 tasks。
    """
    plan = plan if isinstance(plan, dict) else {}
    tasks = [t for t in (plan.get("tasks") or []) if isinstance(t, dict)]
    reasons: list[str] = []
    if not tasks:
        return ["方案没有 tasks"]
    if len(tasks) > max_tasks:
        reasons.append(f"tasks 数 {len(tasks)} 超过上限 {max_tasks}")
    for task in tasks:
        tid = str(task.get("id") or "?")
        if not [s for s in (task.get("symbols") or []) if str(s).strip()]:
            reasons.append(f"{tid} 缺 symbols")
            break
    for task in tasks:
        tid = str(task.get("id") or "?")
        if not str(task.get("change") or "").strip():
            reasons.append(f"{tid} 缺 change")
            break
    for task in tasks:
        tid = str(task.get("id") or "?")
        if not str(task.get("test_hint") or "").strip():
            reasons.append(f"{tid} 缺 test_hint")
            break
    for task in tasks:
        tid = str(task.get("id") or "?")
        files = [f for f in (task.get("target_files") or []) if str(f).strip()]
        if len(files) > max_files_per_task:
            reasons.append(f"{tid} 覆盖 {len(files)} 个文件，超过上限 {max_files_per_task}")
            break
    # 同一个文件被多少张图覆盖：**最容易出事**的一条（同一文件被多次改动 ⇒ 合并/anchor 冲突）
    per_file: dict[str, int] = {}
    for task in tasks:
        for f in (task.get("target_files") or []):
            p = _norm(f)
            if p:
                per_file[p] = per_file.get(p, 0) + 1
    for path, cnt in per_file.items():
        if cnt > MAX_TASKS_PER_FILE:
            reasons.append(f"{path} 被 {cnt} 张图覆盖，超过上限 {MAX_TASKS_PER_FILE}")
            break
    return reasons
