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


def compile_tasks(
    plan: Any, *, max_symbols: int = MAX_SYMBOLS_PER_TASK, ir: Any = None
) -> list[dict]:
    """从 **compiler_ir** 确定性生成 tasks。

    优先读 IR（`planir.normalize_plan` 的产物）—— 合并 / 清洗 / 冲突解决**已经做完**，
    这里只做**拆分与编号**。没有 IR 时退回直接读 `plan["changes"]`（兼容旧调用点与单测）。

    规则（全部可预测、可断言）：
      ① 每个 unit 至少一张图；没声明 symbols 时整文件一张；
      ② 符号数超过 `max_symbols` 就切分，保证单轮写得完；
      ③ 单文件天然只由它自己的图覆盖（不会触发"同一文件被多张图覆盖"的告警）；
      ④ 依赖优先用方案声明的 `depends_on`（按 unit 翻译成新编号）；没有声明的挂到
         前一张图 ⇒ 保住"先建文件、后引用接口"的线性次序；
      ⑤ `test_hint` 优先用 IR 里那条（方案阶段补的施工图字段），否则机械生成
         `python -c "import <module>"` —— 不再指望模型填，它**从来没填过**。
    """
    units = [
        u for u in ((ir or {}).get("units") or []) if isinstance(u, dict) and _norm(u.get("file"))
    ]
    if units:
        return _compile_from_units(units, max_symbols=max_symbols)
    return _compile_from_changes(plan, max_symbols=max_symbols)


def _dedup(items: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in (items or []):
        key = str(it or "").strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _split_groups(symbols: list[str], max_symbols: int) -> list[list[str]]:
    """切分大小同时受两个上限约束：单张不超过 `max_symbols`（写得完），
    且**同一文件的图数不超过 `MAX_TASKS_PER_FILE`**（避免同一文件被反复改动）。
    后者优先：真机 192001 里 `command.py` 被 3 张图覆盖，直接引发合并 / anchor 冲突。
    """
    if not symbols:
        return [[]]
    size = max(max_symbols, -(-len(symbols) // MAX_TASKS_PER_FILE))
    return _chunk(symbols, size)


def _acceptance_text(raw: Any, path: str, group: list[str]) -> str:
    """IR 的 `acceptance` 是 `[{text, source}]`（**带来源追溯**）—— 取文本并保留来源。

    来源要留下：多张 draft 图合并到同一文件后，出问题时必须能回答"这是谁提的要求"。
    """
    parts: list[str] = []
    for item in (raw if isinstance(raw, list) else []):
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            source = str(item.get("source") or "").strip()
            if text:
                parts.append(f"{text}（来源 {source}）" if source else text)
        elif str(item or "").strip():
            parts.append(str(item).strip())
    if parts:
        return "；".join(parts)
    if group:
        return "；".join(f"{s} 定义完整且可被导入调用" for s in group)
    return f"{path} 可被导入，且承载 intent 描述的能力"


def _compile_from_units(units: list[dict], *, max_symbols: int) -> list[dict]:
    tasks: list[dict] = []
    seq = 0
    id_map: dict[str, list[str]] = {}
    pending: list[tuple[dict, list[str]]] = []
    for unit in units:
        path = _norm(unit.get("file"))
        symbols = [str(s).strip() for s in (unit.get("symbols") or []) if str(s).strip()]
        change_text = str(unit.get("change") or "").strip() or f"按设计实现 {path}"
        # 施工图字段**原样传给 dev**：这些是方案阶段好不容易补上的信息，
        # 编译器不得丢失（丢了 dev 就只能猜，跨文件接口必然对不上）
        hints = {
            key: unit.get(key)
            for key in ("interface", "contracts", "data_model", "constraints", "unresolved_uses")
            if unit.get(key) not in (None, "", [], {})
        }
        new_ids: list[str] = []
        for group in _split_groups(symbols, max_symbols):
            seq += 1
            tid = f"T-{seq:02d}"
            new_ids.append(tid)
            task: dict[str, Any] = {
                "id": tid,
                "title": f"实现 {path} 的 {'、'.join(group)}" if group else f"实现 {path}",
                "change": change_text,
                "target_files": [path],
                "acceptance": _acceptance_text(unit.get("acceptance"), path, group),
                "symbols": list(group),
                "depends_on": [],
                # 跨轮**稳定身份**：task id 会被重编号（方案一重做就变），
                # 「哪些图没变」与归因必须建在不随编号漂移的东西上（见 planir.stable_id）
                "stable_id": f"{path}::{sorted(group)[0]}" if group else f"{path}::<whole-file>",
            }
            if unit.get("test_hint"):
                task["test_hint"] = str(unit["test_hint"])
            elif path.endswith(".py"):
                task["test_hint"] = f'python -c "import {_module_of(path)}"'
            for key, val in hints.items():
                task.setdefault(key, val)
            tasks.append(task)
            pending.append((task, _dedup(unit.get("depends_on"))))
        for key in _dedup([*(unit.get("source_task_ids") or []), str(unit.get("stable_id") or "")]):
            id_map.setdefault(key, []).extend(new_ids)
    # 依赖翻译：原 draft id → 新编号（挂到该 unit 的**全部**图上，即"整个 unit 完成"）
    for task, deps in pending:
        mapped: list[str] = []
        for dep in deps:
            mapped.extend(id_map.get(dep, []))
        # **必须滤掉自引用**：多张原图合并进同一 unit 时，合并后的图天然会"依赖自己"
        # （真机形态：T-02 与 T-03 都改 cli.py，T-03 声明依赖 T-02，两者合成一张图后
        # 就变成 T-03 → T-03）。自引用会让拓扑排序退化、也让"前置任务"这句话变成噪音。
        task["depends_on"] = [d for d in _dedup(mapped) if d != task["id"]]
    prev = ""
    for task in tasks:
        if not task["depends_on"] and prev:
            task["depends_on"] = [prev]
        prev = task["id"]
    return tasks


def _compile_from_changes(plan: Any, *, max_symbols: int) -> list[dict]:
    """**兼容路径**：没有 IR 时直接读 `changes[]`（旧调用点与单测走这里）。"""
    plan = plan if isinstance(plan, dict) else {}
    changes = [c for c in (plan.get("changes") or []) if isinstance(c, dict) and c.get("path")]
    if not changes:
        return []
    units: list[dict] = []
    for change in changes:
        path = _norm(change.get("path"))
        units.append(
            {
                "file": path,
                "symbols": [str(s).strip() for s in (change.get("symbols") or []) if str(s).strip()],
                "change": str(change.get("approach") or change.get("intent") or "").strip(),
                "acceptance": [],
                "depends_on": [],
                "source_task_ids": [],
                "stable_id": f"{path}::<whole-file>",
            }
        )
    return _compile_from_units(units, max_symbols=max_symbols)


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
