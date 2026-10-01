"""**Plan IR** —— `raw_plan` 与 compiler 之间的**唯一稳定接口**。

真机驱动（为什么必须有这一层）
----------------------------
`compile_tasks()` 是确定性、逐行可预测的代码；让它直接读模型的自由文本
（`changes` / `tasks` / `approach`）等于让它承担"理解"，而"理解"是回归的源头。
更致命的是**无法归因**：架构师提示词、task schema、编译器规则三者纠缠在一起，
指标一动，说不清是"架构师变好了"还是"编译器规则变了"，实验失去可比性。

分层：

    raw_plan.json  --normalize_plan()-->  compiler_ir.json  --compile_tasks()-->  tasks[]

`normalize_plan` 独自负责 **合并 / 清洗 / 冲突解决 / 推导**；
`compile_tasks` 只做**拆分与编号**。Compiler 从此不读 `changes` / `tasks` / `approach`。

Compile Contract 与**冻结时机**
------------------------------
`COMPILER_INPUT_VERSION` 是冻结的输入契约版本：`units[]` 的字段语义一旦发布不再变，
要改就升版本并写进 `env.fingerprint`。

但**冻结的时机**要选对。真机 43 次运行的统计：

  · `changes[].symbols` 填充率 **0/43 = 0%** —— 因为 `prompts.py` 的 changes 契约里
    从没要求过这个字段（schema 早已声明，注释还写着"这是 Task Compiler 的输入"）；
  · 补进契约后的最近 5 次运行，`tasks[].symbols` 填充率 **5/5 = 100%**。

即：**不是 14B 做不到，是契约没要求**。同时说明双源（changes + draft_tasks）目前是
**过渡形态** —— 先让架构师把边界填实，双源才可能退化成单源。在接口内容定型之前
冻结，只会把"架构师不填边界"这个缺陷固化进 v1。
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
from pathlib import PurePosixPath
from typing import Any, Iterable

from . import ontology
from . import symbols as symbol_resolver

#: 编译器**输入契约**版本。见模块 docstring 的「冻结时机」。
#: v2：units 增加 ``self_depends``（draft 自依赖取证）；``depends_on_files``
#: 并入二开项目的**现存 import 边**（``file_imports``）。
#: v3：units 增加 ``work_units``（task 级 facet）——同文件被多张 draft 图覆盖时，
#: change/interface/acceptance/contracts/constraints/test_hint 按**源任务**分别保留，
#: compiler 出图只带本 facet 的字段（真机 run 20260929-093329：v2 first-wins 合并
#: 导致"符号是 A 的、interface 是 B 的"）。file 级仍只合并 intent/data_model。
COMPILER_INPUT_VERSION = "3"

__all__ = [
    "COMPILER_INPUT_VERSION",
    "normalize_plan",
    "existing_import_edges",
    "drop_parent_symbols",
    "stable_id",
    "diff_units",
    "fingerprint",
]


def _norm(path: Any) -> str:
    return str(path or "").replace("\\", "/").strip()


def _as_list(value: Any) -> list:
    """把"可能是字符串、也可能是列表"的字段**安全地**统一成列表。

    **必须做这一步**：字符串是可迭代的，`for x in "必须使用标准库"` 会**逐字**产出十几个
    "条目"。真机 `20260928-110402` 的施工图里正是如此 —— dev 收到的【必须遵守的约束】是
    「必 / 须 / 使 / 用 / 标 / 准 / 库 / ，…」一串单字，约束字段等于**完全失效**，
    而模型还得拿它当约束去猜。

    当时只有 `acceptance` 写了 `isinstance(val, str)` 的单独保护，`constraints` /
    `depends_on` / `contracts.uses` / `symbols` 四处都漏了 —— 所以统一从这一个入口进，
    新增字段时也不会再踩。`dict` 取 values：`contracts` 那种 {a: b} 形态也能读。
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    try:
        # 生成器 / 其它可迭代：**必须收**。第一版只认 list/tuple，于是
        # `_dedup(x for x in ...)` 传进来的生成器被当成"空" —— 符号清单整个变空，
        # 而且不报错（回放真机方案时才看出来：三个文件的 symbols 全成了 `[]`）。
        return list(value)
    except TypeError:
        return []


def _dedup(items: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in _as_list(items):
        key = str(it or "").strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _is_segment_prefix(a: str, b: str) -> bool:
    """`a` 是否为 `b` 的**按点分段**前缀。

    分段比较是关键：`add` 不是 `add_all` 的前缀（字面 `startswith` 会误判）。
    """
    if a == b:
        return False
    return b.startswith(a + ".")


def drop_parent_symbols(symbols: Any) -> tuple[list[str], list[dict]]:
    """**最长匹配原则**：父节点是"范围"，不是"实现目标"。

    真机里架构师与骨架常同时给出 `CLI` 与 `CLI.add`：让开发"定义 CLI"这句话
    既无法施工（`CLI` 本身是容器），又会与 `CLI.add` 的锚点打架。
    按分段前缀删掉父节点，并**记录删了什么、被谁覆盖** —— 不能静默丢弃模型输入。
    """
    syms = _dedup(symbols)
    dropped: list[dict] = []
    keep: list[str] = []
    for s in syms:
        covered_by = [t for t in syms if _is_segment_prefix(s, t)]
        if covered_by:
            dropped.append({"symbol": s, "reason": "被更长的符号覆盖（最长匹配原则）", "covered_by": covered_by})
        else:
            keep.append(s)
    return keep, dropped


def stable_id(file: str, symbols: Any) -> str:
    """跨轮**稳定身份**：不依赖 draft task 编号。

    真机证据：方案一重做，task id 就被重新编号，于是跨轮累积的 `covers_tasks`
    全部变成"编造"（`unknown_tasks`）。归因链必须建在不随编号漂移的东西上。
    """
    syms = sorted(_dedup(symbols))
    anchor = syms[0] if syms else "<whole-file>"
    return f"{_norm(file)}::{anchor}"


def _dotted_module(path: str) -> str:
    """`a/b.py` → `a.b`；`a/b/__init__.py` → `a.b`（包本身）。"""
    p = _norm(path)
    if p.endswith("/__init__.py"):
        p = p[: -len("/__init__.py")]
    elif p.endswith(".py"):
        p = p[:-3]
    return p.replace("/", ".")


def existing_import_edges(files: Any, sources: Any) -> dict[str, list[str]]:
    """二开项目：从**现存源码**的 import 语句机械推出方案内文件间的依赖边。

    ``files``：本次方案涉及的文件（changes + draft target_files）；
    ``sources``：``{相对路径: 现存源码正文}``（编排层从 repo 读，读不到的文件不给）。

    返回 ``{path: [被它 import、且属于方案文件集的路径…]}``（有序去重）。只解析
    ``import x`` / ``from x import y`` / 相对导入三种形态（AST，不靠正则猜）；
    标准库 / 第三方 / 方案外文件天然不建边（模块表里没有对应节点）—— 未改动的
    现存文件不产生任务，给它们建边没有任何排序意义。
    """
    planned = [_norm(p) for p in _as_list(files) if _norm(p)]
    # dotted 模块 → 方案内文件（a/b.py 与 a/b/__init__.py 不可能同时存在；确定性取序）
    module_of: dict[str, str] = {}
    for p in sorted(planned):
        module_of.setdefault(_dotted_module(p), p)

    def _resolve_module(name: str) -> str:
        """模块点号名 → 方案内文件；精确命中优先，再按最长前缀退（`a.b.c` → a/b.py）。"""
        name = (name or "").strip().lstrip(".")
        if not name:
            return ""
        if name in module_of:
            return module_of[name]
        parts = name.split(".")
        while parts:
            parts.pop()
            hit = module_of.get(".".join(parts))
            if hit:
                return hit
        return ""

    edges: dict[str, list[str]] = {}
    src = sources if isinstance(sources, dict) else {}
    for path in planned:
        text = src.get(path)
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue  # 现存代码可能本身就编译不过：import 边是增强信号，不该因此崩编译链
        mod = _dotted_module(path)
        pkg_parts = mod.split(".")[:-1] if not path.endswith("/__init__.py") else mod.split(".")
        found: set[str] = set()

        def _add_module(name: str) -> None:
            hit = _resolve_module(name)
            if hit and hit != path:
                found.add(hit)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    _add_module(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    # 相对导入：level=1 锚定当前包，每多一级再上溯一个包段
                    up = node.level - 1
                    base_parts = pkg_parts[: len(pkg_parts) - up] if up <= len(pkg_parts) else []
                else:
                    base_parts = []
                mod_parts = (node.module or "").split(".") if node.module else []
                base = ".".join([*base_parts, *mod_parts])
                _add_module(base)
                # `from pkg import sub` 里 sub 也可能是子模块（pkg/sub.py）
                for alias in node.names:
                    if alias.name != "*":
                        _add_module(f"{base}.{alias.name}" if base else alias.name)
        if found:
            edges[path] = sorted(found)
    return edges


def _relative_name(sym: str, hit: dict) -> str:
    """去掉「文件限定」前缀：`a.run` → `run`、`cli.py.CLI.add` → `CLI.add`。

    unit 是**按文件**成组的，文件前缀在这里是冗余的；但**类的点号必须保留** ——
    它区分同名方法（`CLI.add` / `Food.add`），也是锚点定位的依据。
    两者字面同形（`a.run` 与 `CLI.add` 长得一样），只能靠"前缀是否等于该文件的
    路径 / 模块名"来分辨，**不靠猜**。
    """
    if not hit.get("resolved"):
        return sym
    for path in (hit.get("candidates") or [])[:1]:
        p = _norm(path)
        stem = p[:-3] if p.endswith(".py") else p
        for cand in (p, stem, stem.rsplit("/", 1)[-1]):
            if cand and sym.startswith(cand + "."):
                return sym[len(cand) + 1:]
    return sym


def _skeleton_order(skeleton: Any) -> dict[str, tuple[int, int]]:
    """接口基准里符号的声明顺序 → 排序键。

    IR 必须**逐字节可复现**（同一输入必得同一 IR），所以排序不能依赖 dict 插入顺序；
    而纯字母序切分又会把 `CLI.add` 和别的类的符号混在一张图里。
    基准声明顺序同时满足"确定"与"语义成组"两个要求；基准缺失时退化为字母序。
    """
    order: dict[str, tuple[int, int]] = {}
    seq = 0
    for _path, rows in symbol_resolver.digest_symbols(skeleton).items():
        for kind, name in rows:
            if kind in ("class", "function"):
                seq += 1
                order.setdefault(name, (0, seq))
            elif kind == "method":
                seq += 1
                order[name] = (0, seq)
                order.setdefault(name.rsplit(".", 1)[-1], (1, seq))
    return order


def _symbol_sort_key(order: dict[str, tuple[int, int]]):
    def key(sym: str) -> tuple:
        got = order.get(sym) or order.get(sym.rsplit(".", 1)[-1])
        return (*got,) if got else (2, 0)

    return key


def normalize_plan(
    plan: Any, *, skeleton: Any = None, previous: Any = None, file_imports: Any = None,
    scope: Any = None, original_requirement: str = "", intake_rows: Any = None,
) -> dict:
    """`raw_plan` → `compiler_ir`。**纯函数**：同输入必得同输出（含 key 顺序）。

    冲突规则（全部显式规定，不留"看情况"）：

      ① **symbols 最长匹配**：`CLI` 与 `CLI.add` 并存 ⇒ 删 `CLI`，记入 `conflicts`；
      ② **多张 draft task 改同一文件** ⇒ 合并成一个 unit，但 `intent` 保留
         `{text, source}` 来源，出问题时能追溯到是哪张图提的要求；
      ③ **稳定排序**：draft task id → change.order → 符号字母序
         （**不依赖 dict 插入顺序** —— 那会让 IR 在等价输入下漂移）。

    ``file_imports``：二开项目里由 :func:`existing_import_edges` 从**现存源码 AST**
    机械推出的 ``{文件: [它 import 的方案内文件]}``。与契约解析出的依赖取并集，
    使执行 DAG 的依赖来源为「contract ∪ symbol ∪ 现存 import 图」（新建项目没有
    现存源码，不传即可 —— 不靠模型写路径）。
    """
    plan = plan if isinstance(plan, dict) else {}
    changes = [c for c in (plan.get("changes") or []) if isinstance(c, dict) and _norm(c.get("path"))]
    draft = [t for t in (plan.get("tasks") or []) if isinstance(t, dict)]

    order_index = {_norm(c.get("path")): i for i, c in enumerate(changes)}
    conflicts: list[dict] = []

    # ---------- 1) 文件全集：changes ∪ draft target_files ∪ 骨架文件
    files: list[str] = [_norm(c.get("path")) for c in changes]
    for t in draft:
        files.extend(_norm(p) for p in (t.get("target_files") or []))
    if isinstance(skeleton, dict) and not skeleton.get("files"):
        # 摘要形态：`{path: [行]}`，文件清单就是它的键（接口基准里出现的文件也要进索引）
        files.extend(_norm(p) for p in skeleton.keys())
    else:
        for entry in ((skeleton or {}).get("files") or []):
            if isinstance(entry, dict) and _norm(entry.get("path")):
                files.append(_norm(entry.get("path")))
    files = _dedup(files)
    index = symbol_resolver.build_index(files, skeleton)
    skel_order = _skeleton_order(skeleton)

    # ---------- 2) 以 changes 为骨架建 unit（changes 的顺序即声明顺序）
    units: dict[str, dict] = {}

    def _unit(path: str) -> dict:
        if path not in units:
            units[path] = {
                "file": path,
                "symbols": [],
                "intent": [],
                "acceptance": [],
                "change": "",
                "interface": "",
                "contracts": {"uses": [], "exposes": []},
                "data_model": "",
                "constraints": [],
                "test_hint": "",
                "depends_on": [],
                "self_depends": [],
                "source_task_ids": [],
                #: task 级 facet（v3）：同一文件被多张 draft 图覆盖时，各自的
                #: change/interface/acceptance/contracts/constraints/test_hint **不合并**，
                #: compiler 按 facet 出图。file 级同名首胜字段保留，仅作兼容回退。
                "work_units": [],
                "uses_resolved": [],
                "unresolved": [],
                "_declared": [],
                "_order": order_index.get(path, len(changes)),
            }
        return units[path]

    def _changes_facet(unit: dict) -> dict:
        """changes[] 贡献的 file 级 facet（每文件至多一个，固定排在最前）。"""
        wus = unit["work_units"]
        if not wus or wus[0].get("source") != "@changes":
            wus.insert(
                0,
                {
                    "source": "@changes",
                    "symbols": [],
                    "change": "",
                    "acceptance": [],
                    "interface": "",
                    "test_hint": "",
                    "constraints": [],
                    "deps_raw": [],
                    "uses_raw": [],
                    "exposes_raw": [],
                    "uses": [],
                    "exposes": [],
                },
            )
        return wus[0]

    for change in changes:
        path = _norm(change.get("path"))
        unit = _unit(path)
        # **入口也要归一**：`changes[].symbols` 真机里写成 `main()`（带调用括号），而 task
        # 那一路已经过 resolver 的 `clean_symbol` ⇒ 裸名。两条入口书写不一致时，同一符号会在
        # 定稿后的清单里**出现两次**（真机 `20260928-110402`：`main` 与 `main()` 并存），
        # 提示词里就成了"要定义两个东西"，而机械自检的判据也跟着分裂。
        change_syms = [
            symbol_resolver.clean_symbol(s)
            for s in _as_list(change.get("symbols"))
            if symbol_resolver.clean_symbol(s)
        ]
        unit["_declared"].extend(change_syms)
        facet = _changes_facet(unit)
        facet["symbols"].extend(change_syms)
        if str(change.get("intent") or "").strip():
            unit["intent"].append({"text": str(change["intent"]).strip(), "source": "changes"})
        if not unit["change"]:
            # 「改什么」优先取 approach（更具体），退回 intent
            unit["change"] = str(change.get("approach") or change.get("intent") or "").strip()
        if not facet["change"]:
            facet["change"] = str(change.get("approach") or change.get("intent") or "").strip()

    # ---------- 3) 合并 draft tasks（稳定排序）
    def _task_sort_key(t: dict) -> tuple:
        tid = str(t.get("id") or "").strip()
        tid_files = [_norm(p) for p in (t.get("target_files") or []) if _norm(p)]
        first_order = min((order_index.get(f, len(changes)) for f in tid_files), default=len(changes))
        sym_key = tuple(sorted(str(s).strip() for s in (t.get("symbols") or []) if str(s).strip()))
        return (tid, first_order, sym_key)

    per_file_tasks: dict[str, list[str]] = {}
    for task in sorted(draft, key=_task_sort_key):
        tid = str(task.get("id") or "").strip()
        tid_files = [_norm(p) for p in (task.get("target_files") or []) if _norm(p)]
        if not tid_files:
            conflicts.append({"kind": "task_without_files", "detail": f"{tid} 没有 target_files，已忽略"})
            continue
        for f in tid_files:
            per_file_tasks.setdefault(f, []).append(tid)
            if f not in order_index:
                conflicts.append(
                    {"kind": "task_file_not_in_changes", "detail": f"{tid} 指向 {f}，但 changes 里没有它"}
                )
        syms = [str(s).strip() for s in _as_list(task.get("symbols")) if str(s).strip()]
        # 符号归位：一张图覆盖多文件时，用 resolver 决定每个符号属于哪个文件
        buckets: dict[str, list[str]] = {f: [] for f in tid_files}
        for sym in syms:
            hit = symbol_resolver.resolve(sym, files=tid_files, index=index)
            target = ""
            if hit.get("resolved") and hit.get("candidates"):
                cand = hit["candidates"][0]
                target = cand if cand in buckets else tid_files[0]
            else:
                target = tid_files[0]
                if len(tid_files) > 1 and not hit.get("resolved"):
                    conflicts.append(
                        {
                            "kind": "symbol_target_ambiguous",
                            "detail": f"{tid} 的 {sym} 无法在 {tid_files} 中定位（{hit.get('reason')}）→ 归入 {target}",
                        }
                    )
            # 用 resolver **归一后**的名字：`DBManager.insert()` → `DBManager.insert`,
            # `sqlite3.connect()` → `sqlite3.connect`（真机 20260927-214253 全是这种写法）
            buckets[target].append(_relative_name(str(hit.get("symbol") or sym), hit))

        for f in tid_files:
            unit = _unit(f)
            if tid and tid not in unit["source_task_ids"]:
                unit["source_task_ids"].append(tid)
            bucket_syms = buckets.get(f) or []
            unit["symbols"].extend(bucket_syms)
            # task 级 facet（v3）：该 draft 图对**这一个文件**的全部字段原样留存，
            # 不再被同文件其它图的 first-wins 合并污染（run 20260929-093329 的漂移点）。
            contracts = task.get("contracts")
            facet_contracts = contracts if isinstance(contracts, dict) else {}
            dep_ids = [
                str(d).strip() for d in _as_list(task.get("depends_on")) if str(d).strip()
            ]
            facet_acceptance = str(task.get("acceptance") or "").strip()
            unit["work_units"].append(
                {
                    "source": tid or "draft",
                    "symbols": list(bucket_syms),
                    "change": str(task.get("change") or "").strip(),
                    "acceptance": (
                        [{"text": facet_acceptance, "source": tid or "draft"}]
                        if facet_acceptance
                        else []
                    ),
                    "interface": str(task.get("interface") or "").strip(),
                    "test_hint": str(task.get("test_hint") or "").strip(),
                    "constraints": _dedup(_as_list(task.get("constraints"))),
                    "deps_raw": dep_ids,
                    "uses_raw": _dedup(_as_list(facet_contracts.get("uses"))),
                    "exposes_raw": _dedup(_as_list(facet_contracts.get("exposes"))),
                    # uses/exposes 在定稿阶段按 unit 级解析结果回填
                    "uses": [],
                    "exposes": [],
                }
            )
            # 来源追溯：多张图改同一文件时，逐条记录是"谁提的"（file 级聚合保留，供旧消费方）
            if str(task.get("change") or "").strip() and not unit["change"]:
                unit["change"] = str(task["change"]).strip()
            if facet_acceptance:
                unit["acceptance"].append({"text": facet_acceptance, "source": tid or "draft"})
            if str(task.get("test_hint") or "").strip() and not unit["test_hint"]:
                unit["test_hint"] = str(task["test_hint"]).strip()
            if str(task.get("interface") or "").strip() and not unit["interface"]:
                unit["interface"] = str(task["interface"]).strip()
            if str(task.get("data_model") or "").strip() and not unit["data_model"]:
                # data_model 是**文件级**事实（表结构只可能有一版），允许 file 级 first-wins 合并
                unit["data_model"] = str(task["data_model"]).strip()
            unit["constraints"].extend(
                str(s).strip() for s in _as_list(task.get("constraints")) if str(s).strip()
            )
            unit["_declared"].extend(bucket_syms)
            unit["depends_on"].extend(dep_ids)
            # **自依赖取证**：draft 图 T 声明 depends_on 含 T 自己，是方案层的硬错误，
            # 编译器据此报 self_dependency。必须在合并前留证 —— 合并后只剩 unit 聚合，
            # 无法区分「T-03→T-03（真错误）」与「T-02/T-03 同文件合并后 T-03→T-02
            # 变成自引用（合并的机械副产物，应静默滤掉）」。
            if tid and tid in dep_ids and tid not in unit["self_depends"]:
                unit["self_depends"].append(tid)
            if isinstance(contracts, dict):
                for key in ("uses", "exposes"):
                    unit["contracts"][key].extend(
                        str(s).strip() for s in _as_list(contracts.get(key)) if str(s).strip()
                    )

    # 多张图覆盖同一文件 ⇒ 记一条合并记录（可解释性；不是错误）
    for path, tids in per_file_tasks.items():
        if len(set(tids)) > 1:
            conflicts.append(
                {
                    "kind": "multi_task_same_file",
                    "detail": f"{path} 被 {sorted(set(tids))} 覆盖 → 已合并为一个 unit（来源保留）",
                }
            )

    # ---------- 4) 逐 unit 定稿：最长匹配、去重、依赖解析、稳定排序
    planned_files = set(files)
    out_units: list[dict] = []
    for path in sorted(units, key=lambda p: (units[p]["_order"], p)):
        unit = units[path]
        # 定稿再归一一次（幂等）：将来新增第三处入口时也不会再出现"两种书写并存"。
        declared = _dedup(symbol_resolver.clean_symbol(s) for s in _as_list(unit["_declared"]))
        kept, dropped = drop_parent_symbols(declared)
        if dropped:
            conflicts.append({"kind": "symbol_longest_match", "file": path, "dropped": dropped})
        unit["symbols"] = sorted(kept, key=_symbol_sort_key(skel_order))
        unit.pop("_declared", None)

        # facet 定稿：符号按同一套最长匹配结果归位（被父符号覆盖的同样从 facet 剔除），
        # 顺序与 unit.symbols 的排序口径一致（单 facet 场景出图必须与 v2 完全相同）。
        kept_set = set(kept)
        # 双源去重：changes[].symbols 是**边界声明**，draft tasks 才是可施工的拆解。
        # 同一符号两边都写时（真机常态），归属 draft facet —— 否则 @changes 会多出
        # 一组重复图，同一符号被两张图各实现一遍。@changes 只保留 draft 没覆盖的符号。
        draft_owned: set[str] = set()
        for wu in unit["work_units"]:
            if wu.get("source") == "@changes":
                continue
            draft_owned.update(
                s for s in _dedup(symbol_resolver.clean_symbol(x) for x in _as_list(wu.get("symbols")))
                if s in kept_set
            )
        for wu_order, wu in enumerate(unit["work_units"]):
            wu_syms = _dedup(symbol_resolver.clean_symbol(s) for s in _as_list(wu.get("symbols")))
            wu_syms = [s for s in wu_syms if s in kept_set]
            if wu.get("source") == "@changes" and draft_owned:
                wu_syms = [s for s in wu_syms if s not in draft_owned]
            wu["symbols"] = sorted(wu_syms, key=_symbol_sort_key(skel_order))
            wu["constraints"] = _dedup(wu.get("constraints"))
            wu["deps_raw"] = _dedup(wu.get("deps_raw"))
            wu["uses_raw"] = _dedup(wu.get("uses_raw"))
            wu["exposes_raw"] = _dedup(wu.get("exposes_raw"))
            wu["order"] = wu_order

        uses = _dedup(unit["contracts"]["uses"])
        unit["contracts"]["uses"] = uses
        unit["contracts"]["exposes"] = _dedup(unit["contracts"]["exposes"])
        # 同一 raw use 在 unit 级只解析一次，facet 按原文回填解析结果 —— 解析口径
        # 必须全文件唯一，否则同一条 uses 在两张图上解析成两个目标，契约又漂了。
        resolution = {
            s: dict(symbol_resolver.resolve(s, files=[path], index=index), role="uses")
            for s in uses
        }
        resolved = list(resolution.values())
        resolved_name = {
            raw: str(row.get("symbol")) for raw, row in resolution.items() if row.get("resolved")
        }
        for wu in unit["work_units"]:
            wu["uses"] = _dedup(resolved_name[raw] for raw in wu["uses_raw"] if raw in resolved_name)
            wu["exposes"] = list(wu["exposes_raw"])
        unit["uses_resolved"] = resolved
        unit["unresolved"] = symbol_resolver.unresolved_warnings(resolved)
        # 外部依赖（标准库/第三方）单独记：它们是**合法**依赖，只是不参与
        # "产物内契约对不上"的核对。混进 unresolved 会稀释真正要修的那几条。
        unit["externals"] = sorted(
            {str(r.get("symbol")) for r in resolved if r.get("kind") == "external"}
        )
        # **施工图里只保留"能落地"的契约。**
        # 解析不了的引用**不是契约**，是方案层的错误（架构师凭空写了一个接口基准里
        # 不存在的符号）。把它留在施工图里后果是实测出来的（真机 20260927-214253）：
        # dev 把 `contracts.uses` 当成了"要实现/要依赖的东西"，重出补丁时把 anchor
        # 直接写成 `DBManager.insert()` 这种**调用表达式** —— 4 条 anchor_not_found，
        # 首轮即停人工。而 dev 本来就无权改方案（它被约束在 changes 范围内），
        # 喂给它只会让它白烧。所以：从契约里摘掉，转成 `unresolved_uses` 交给
        # 施工图的**禁止项**与 state 的 plan_unresolved（评审/人工据此判返工方案）。
        unit["unresolved_uses"] = [str(r.get("symbol")) for r in resolved if not r.get("resolved")]
        unit["contracts"]["uses"] = [str(r.get("symbol")) for r in resolved if r.get("resolved")]
        # 依赖的**文件集合**：契约/符号解析推出的边 ∪ 二开现存源码的 import 边
        # （都不是让模型写路径）。只连**本次方案内**文件 —— 未规划的现存文件没有
        # 对应任务节点，边连过去既无排序意义也会污染执行 DAG。
        dep_files = {
            c
            for row in resolved
            if row.get("resolved")
            for c in (row.get("candidates") or [])
            if c != path
        }
        for c in (_as_list((file_imports or {}).get(path)) if isinstance(file_imports, dict) else []):
            cand = _norm(c)
            if cand and cand != path and cand in planned_files:
                dep_files.add(cand)
        unit["depends_on_files"] = sorted(dep_files)
        unit["stable_id"] = stable_id(path, unit["symbols"])
        unit["intent"] = _dedup_intents(unit["intent"])
        unit["acceptance"] = _dedup_intents(unit["acceptance"])
        unit["constraints"] = _dedup(unit["constraints"])
        unit["depends_on"] = _dedup(unit["depends_on"])
        unit["self_depends"] = _dedup(unit["self_depends"])
        unit["source_task_ids"] = _dedup(unit["source_task_ids"])
        out_units.append(unit)

    # ---------------------------------------------------------- Ontology 投影（规格§十二）
    # 在 IR 定稿前叠加语义覆盖层（**不改任何既有 IR 字段**，旧消费者忽略新键即可）：
    #   · Requirement(ASSERTED 用户 + DERIVED PM) → Claim → ProofObligation；
    #   · 单元的 constraints → Constraint 对象；plan.invariants（若有）→ Invariant；
    #   · requirement_unit_links 把 FR 确定性挂到文件（不猜，挂不上显式暴露）。
    ont_graph = ontology.OntologyGraph()
    links: dict[str, Any] = {"by_file": {}, "unlinked_requirements": [], "unlinked_files": []}
    if scope is not None:
        ontology.build_requirement_projection(
            ont_graph, scope, original_requirement=str(original_requirement or "")
        )
        links = ontology.requirement_unit_links(scope, out_units)
        # 规格§三十四：intake/PM 的背景与默认推断全部 DERIVED 上链；人工裁决才是 ASSERTED。
        # intake_rows 由编排层经 prompts.intake_items 归一后传入（planir 不反向依赖 prompts）。
        ontology.project_intake(
            ont_graph, scope=scope, intake_rows=(intake_rows or ()),
        )
    for unit in out_units:
        path = unit["file"]
        link = (links.get("by_file") or {}).get(path) or {}
        unit["implements_requirements"] = list(link.get("requirements") or [])
        unit["proof_obligation_ids"] = list(link.get("proof_obligations") or [])
        unit["claim_ids"] = list(link.get("claims") or [])
        for idx_c, text in enumerate(unit.get("constraints") or []):
            cid = f"cst:{ontology.stable_hash([path, idx_c, str(text)], length=10)}"
            if ont_graph.get(cid) is None:
                ont_graph.add(ontology.SemanticObject(
                    id=cid, type=ontology.TYPE_CONSTRAINT, truth=ontology.TRUTH_DERIVED,
                    payload={"file": path, "text": str(text)},
                    provenance=[ontology.Provenance(source="architect", stage="architect_plan")],
                ))
    # 规格§十八：冻结接口 = 冻结骨架声明 ∩ 方案承诺符号。每个冻结符号一条双证据 PO；
    # 骨架里有、方案没承诺的符号不冻结（两次 LLM 输出不一致不判实现缺陷，见 verify 注释）。
    plan_file_symbols = {str(u.get("file") or ""): list(u.get("symbols") or []) for u in out_units}
    frozen = ontology.freeze_symbols(skeleton, plan_file_symbols)
    ontology.project_interface_freeze(ont_graph, frozen)
    for idx_i, inv in enumerate((plan.get("invariants") or []) if isinstance(plan, dict) else []):
        if isinstance(inv, dict):
            inv_id = str(inv.get("id") or "")
            inv_text = str(inv.get("text") or inv.get("rule") or inv.get("name") or "")
            check_obj = inv.get("check") if isinstance(inv.get("check"), dict) else {}
            check_id = str(inv.get("check_id") or (check_obj.get("check_id") if check_obj else "") or "")
        else:
            inv_id, inv_text, check_id, check_obj = "", str(inv), "", {}
        if not inv_text:
            continue
        iid = f"inv:{ontology.stable_hash([idx_i, inv_text], length=10)}"
        # 规格§九：must_not_break 必须能绑 executable check。显式 check/内置 id/名字含
        # 内置 id 都解析为机械绑定；查无此检查的机械声明与纯人话不变量如实标注（不冒充）。
        binding = ontology.bound_invariant_check({
            "id": inv_id, "name": inv_text,
            "check": (check_obj or {"type": "mechanical", "check_id": check_id} if check_id else {}),
        })
        ont_graph.add(ontology.SemanticObject(
            id=iid, type=ontology.TYPE_INVARIANT, truth=ontology.TRUTH_DERIVED,
            payload={
                "text": inv_text, "check_id": str(binding.get("check_id") or check_id),
                "executable": bool(binding.get("bound") and binding.get("known")),
                "check": {
                    "type": "mechanical" if binding.get("bound") else "prose",
                    "check_id": str(binding.get("check_id") or check_id),
                    "entry": str(binding.get("entry") or ""),
                    "known": bool(binding.get("known")),
                },
            },
            provenance=[ontology.Provenance(source="architect", stage="architect_plan")],
        ))

    ir: dict[str, Any] = {
        "compiler_input_version": COMPILER_INPUT_VERSION,
        "plan_sources": {"changes": bool(changes), "draft_tasks": bool(draft)},
        "units": out_units,
        "conflicts": conflicts,
        "warnings": [w for u in out_units for w in (u.get("unresolved") or [])],
        # Ontology Kernel 语义覆盖层（规格§三）：可 JSON 化；旧 run / 旧消费者无此键安全降级。
        "ontology": ont_graph.to_dict(),
        "ontology_links": links,
    }
    if isinstance(previous, dict) and previous.get("units"):
        ir["diff_vs_previous"] = diff_units(previous, ir)
    return ir


def _dedup_intents(items: Any) -> list[dict]:
    """`[{text, source}]` 去重（保留首个来源，稳定）。"""
    seen: set[str] = set()
    out: list[dict] = []
    for it in (items or []):
        if isinstance(it, dict):
            text = str(it.get("text") or "").strip()
            source = str(it.get("source") or "")
        else:
            text, source = str(it or "").strip(), ""
        if not text or text in seen:
            continue
        seen.add(text)
        out.append({"text": text, "source": source})
    return out


def diff_units(prev_ir: Any, ir: Any) -> dict:
    """两版 IR 的任务级差异（**历史可解释**：谁被删了、谁是新加的）。

    为什么不用 hash 做"要不要重新编译"的判据：draft 文本改一个空格 hash 就变，
    而"hash 未变但语义已变"更危险。是否继承由 `round_kind`
    （`rework_dev` 继承 / `rework_architect` 重编译）决定；hash 只做**审计记录**。
    """
    def _ids(obj: Any) -> set[str]:
        return {str(u.get("stable_id")) for u in ((obj or {}).get("units") or []) if u.get("stable_id")}

    before, after = _ids(prev_ir), _ids(ir)
    return {
        "added_units": sorted(after - before),
        "removed_units": sorted(before - after),
        "kept_units": sorted(before & after),
    }


def fingerprint(*, prompt_hash: str = "") -> dict:
    """三方指纹：`pipeline_hash` / `prompt_hash` / `compiler_hash`，写进 `env.json`。

    没有它，实验无法归因 —— 指标动了，说不清是流水线变了、提示词变了、还是编译器规则变了。
    `compiler_hash` 取编译链三个模块的源码哈希：规则改了必然变。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    compiler = hashlib.sha1()
    # ontology.py / ontology_validate.py 也是编译/裁决链的一部分（语义投影与纯函数闸门），
    # 规则改动必须反映在 compiler_hash 里（规格§五十九：不造第二套 fingerprint）。
    for name in ("planir.py", "taskcompiler.py", "symbols.py", "ontology.py", "ontology_validate.py"):
        try:
            with io.open(os.path.join(here, name), encoding="utf-8") as fh:
                compiler.update(fh.read().encode("utf-8"))
        except OSError:
            continue
    compiler.update(COMPILER_INPUT_VERSION.encode("utf-8"))
    compiler.update(ontology.ONTOLOGY_SCHEMA_VERSION.encode("utf-8"))
    compiler.update(ontology.ONTOLOGY_RULES_VERSION.encode("utf-8"))
    return {
        "compiler_input_version": COMPILER_INPUT_VERSION,
        "ontology_schema_version": ontology.ONTOLOGY_SCHEMA_VERSION,
        "ontology_rules_version": ontology.ONTOLOGY_RULES_VERSION,
        "compiler_hash": compiler.hexdigest()[:12],
        "prompt_hash": str(prompt_hash or ""),
    }


# ---------------------------------------------------------- 骨架越权检测（规格§十三：方案层矛盾）
def _skeleton_symbol(line: str) -> tuple[str, int]:
    """骨架一行 → ``(对外符号名, 缩进)``；非定义行返回 ``("", 缩进)``。

    **缩进必须留着**：``class Snake``（缩进 0）是**顶层声明**，``    def move(self)``
    （缩进 4）是它的**成员**。以前统一 ``strip()`` 把两者混为一谈，于是"类的方法"被当成
    "骨架私自扩大的方案边界" —— 而方案里写的是类名，方法名**必然**不在其中 ⇒ 只要架构师
    按类名规划，这条**必然触发**、且重做多少次都消不掉。真机 20260930-132625 的 18 条
    设计阻断里 4 条正是它，直接把闸门卡死（2 次自纠耗尽 → 停人工）。
    """
    raw = str(line)
    indent = len(raw) - len(raw.lstrip())
    text = raw.strip()
    for prefix in ("class ", "def ", "async def "):
        if text.startswith(prefix):
            return text[len(prefix):].split("(")[0].strip(), indent
    return "", indent


def _skeleton_member_names(line: str) -> str:
    """兼容包装：只要符号名（不再用于越权判定，判据见 :func:`skeleton_overreach`）。"""
    return _skeleton_symbol(line)[0]


def skeleton_overreach(plan: Any, skeleton: Any) -> dict[str, Any]:
    """冻结骨架相对方案声明的**越权**（开发无权修复的方案层矛盾），纯函数。

    与 orchestrator._declared_vs_skeleton 的方向相反：那边查「方案声明了、骨架没有」，
    这边查「骨架多出来」——第二次 LLM 调用不能私自扩大方案边界：

      * ``extra_files``：骨架里出现、方案 changes 未规划的文件；
      * ``extra_symbols``：文件方案已声明非空 symbols 时，骨架多出的**顶层**类/模块函数
        （方案 symbols 为空时骨架是**回填基准**，不算越权，见 _backfill_task_symbols）。

    **只判顶层（缩进 0）**：`class Game` 下面挂的 `def move(...)` 是**类的成员**，属于
    骨架冻结接口的正当职责，不是"扩大方案边界"。判成员名 ∈ 方案 symbols 必然为假
    （方案写的是类名），那会让闸门永远不放行 —— 真机踩过，见 `_skeleton_symbol` 的注释。
    """
    plan = plan if isinstance(plan, dict) else {}
    skeleton = skeleton if isinstance(skeleton, dict) else {}
    planned_paths: set[str] = set()
    declared: dict[str, set[str]] = {}
    for change in plan.get("changes") or []:
        if not isinstance(change, dict) or not change.get("path"):
            continue
        path = str(change["path"]).replace("\\", "/")
        planned_paths.add(path)
        names = {
            symbol_resolver.clean_symbol(s).rsplit(".", 1)[-1]
            for s in (change.get("symbols") or [])
            if symbol_resolver.clean_symbol(s)
        }
        if names:
            declared[path] = names

    extra_files: list[str] = []
    extra_symbols: dict[str, list[str]] = {}
    for raw_path, lines in skeleton.items():
        path = str(raw_path).replace("\\", "/")
        if path not in planned_paths:
            extra_files.append(path)
            continue
        planned_names = declared.get(path)
        if planned_names is None:
            continue  # 方案没声明 symbols：骨架是回填源，不判越权
        extras: list[str] = []
        for line in lines or []:
            name, indent = _skeleton_symbol(line)
            # 只判顶层声明：缩进 > 0 的是成员（类的方法），不算越权
            if not name or indent > 0:
                continue
            if name not in planned_names:
                extras.append(name)
        if extras:
            extra_symbols[path] = extras
    return {
        "extra_files": sorted(extra_files),
        "extra_symbols": {k: sorted(v) for k, v in sorted(extra_symbols.items())},
    }


# ---------------------------------------------------------- Transition Record 版本摘要（建议⑭）
def _stable_json(obj: Any) -> str:
    """排序键、保证中文不转义的规范 JSON —— 给哈希用，避免键序/空白造成假版本漂移。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def plan_digest(plan: Any) -> str:
    """方案（normalized plan）的 12 位短哈希：Transition Record 的 ``plan_version``。

    只取决定「方案边界与口径」的骨架 —— changes（path/intent/approach/symbols）、
    tasks（id/target_files/symbols/acceptance/interface/contracts/depends_on）、rollback；
    rationale/summary 之类措辞不进哈希（文字润色不应让版本乱跳），但任何文件 / 符号 /
    契约 / 验收 / 依赖变化必然改版本。
    """
    plan = plan if isinstance(plan, dict) else {}
    changes = [
        {
            "path": str(c.get("path") or ""),
            "intent": str(c.get("intent") or ""),
            "approach": str(c.get("approach") or ""),
            "symbols": sorted(str(s) for s in (c.get("symbols") or [])),
        }
        for c in (plan.get("changes") or [])
        if isinstance(c, dict)
    ]
    tasks = [
        {
            "id": str(t.get("id") or ""),
            "target_files": sorted(str(p) for p in (t.get("target_files") or [])),
            "symbols": sorted(str(s) for s in (t.get("symbols") or [])),
            "acceptance": str(t.get("acceptance") or ""),
            "interface": str(t.get("interface") or ""),
            "contracts": t.get("contracts") if isinstance(t.get("contracts"), dict) else {},
            "depends_on": sorted(str(d) for d in (t.get("depends_on") or [])),
        }
        for t in (plan.get("tasks") or [])
        if isinstance(t, dict)
    ]
    payload = {"changes": changes, "tasks": tasks, "rollback": str(plan.get("rollback") or "")}
    return hashlib.sha1(_stable_json(payload).encode("utf-8")).hexdigest()[:12]


def tasks_digest(tasks: Any) -> str:
    """**编译后**施工图序列的 12 位短哈希：Transition Record 的 ``task_version``。

    入参可以是编译产物 IR（``{"units": [...]}``）或 task 列表；字段口径与
    :func:`plan_digest` 的 tasks 段一致（编译后若改写了 id/target_files 等，
    这里与 plan_version 会不同 —— 这正是要留痕的差异）。
    """
    if isinstance(tasks, dict):
        tasks = tasks.get("units") or tasks.get("tasks") or []
    out = []
    for t in (tasks or []):
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or t.get("stable_id") or "")
        contracts = t.get("contracts") if isinstance(t.get("contracts"), dict) else {}
        out.append(
            {
                "id": tid,
                "target_files": sorted(str(p) for p in (t.get("target_files") or [])),
                "symbols": sorted(str(s) for s in (t.get("symbols") or [])),
                "acceptance": str(t.get("acceptance") or ""),
                "interface": str(t.get("interface") or ""),
                "contracts": contracts,
                "depends_on": sorted(str(d) for d in (t.get("depends_on") or [])),
            }
        )
    return hashlib.sha1(_stable_json(out).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Architect Plan Static Lint（P0-6）
# ---------------------------------------------------------------------------
# 这一层的目的：**不要再等 DEV 发现接口明显错误**。
# 方案里的 symbols / interface / contracts 三者必须互相可解释 ——
# 接口里写了 `Game().start()` 却没声明 `start`、契约里 `uses` 了一个不存在的符号、
# 需求只允许标准库却冒出 pygame，这些都是**纯机械**可判的，进 DEV 之前就该拦下。

#: 明确禁止的第三方依赖（需求只允许标准库时出现在 uses 里 = 硬阻断）
_NON_STDLIB_BLOCK: tuple[str, ...] = (
    "pygame", "numpy", "requests", "pytest", "pandas", "scipy",
    "flask", "django", "torch", "matplotlib", "click", "rich",
)

#: 「能 import 但未必被要求」的弱依赖 —— 只记录 warning，不随意阻断（规格 §8.5）
_WEAK_DEPS: tuple[str, ...] = (
    "math", "time", "random", "collections", "itertools", "functools", "copy", "re",
)

#: 接口/契约文本里的噪声词（Python 关键字与常用内建），不参与"未声明"判定
_STOPWORDS: frozenset[str] = frozenset({
    "self", "cls", "print", "return", "import", "from", "def", "class", "if", "else",
    "for", "while", "in", "not", "and", "or", "True", "False", "None", "len", "str",
    "int", "float", "bool", "list", "dict", "set", "tuple", "range", "enumerate",
    "super", "type", "isinstance", "raise", "try", "except", "with", "as", "pass",
    "lambda", "yield", "assert", "del", "global", "break", "continue", "main",
})


def _idents(text: Any) -> set[str]:
    """从一段文本里取出标识符（不用正则：方案文本里混着中英文与标点）。"""
    out: set[str] = set()
    cur = ""
    for ch in str(text or ""):
        if ch.isalnum() or ch == "_":
            cur += ch
            continue
        if cur and not cur[0].isdigit():
            out.add(cur)
        cur = ""
    if cur and not cur[0].isdigit():
        out.add(cur)
    return out


def _interface_refs(text: Any) -> dict[str, bool]:
    """interface 文本 → ``{标识符: 是不是"点号成员"}``（§8.2 的判据真源）。

    刻意排除两类（真机 20260930-132625 的 10 条 ``PLAN_INTERFACE_UNKNOWN`` 里 8 条是它们）：
      * **参数位**（括号内）：`Board(width, height)` 的 width/height 是**参数名**，
        方案 symbols 里本就不该有它们 —— 不排掉就一直判负，且无从修好；
      * **通配/前缀写法**：`TestUI.test_*` 里的 `test_` 是模式前缀，不是符号名
        （判据：以 `_` 结尾的 token 一律跳过）。

    **成员与顶层的区别要留住**：调用方据此决定"判负还是只提示" —— 见
    :func:`validate_architect_plan` 里 §8.2 的一段（成员由**冻结骨架**当基准，
    方案 symbols 里通常只写类名）。
    """
    out: dict[str, bool] = {}
    depth = 0
    cur = ""
    prev_dot = False
    for ch in str(text or "") + " ":
        if ch.isalnum() or ch == "_":
            cur += ch
            continue
        if cur:
            if depth == 0 and not cur.endswith("_") and not cur[0].isdigit() \
                    and cur not in _STOPWORDS:
                out[cur] = out.get(cur, False) or prev_dot
            cur = ""
        prev_dot = ch == "."
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
    return out


def _is_test_path(path: Any) -> bool:
    """是不是**测试文件**（``test_*.py`` / ``*_test.py`` / ``tests/`` 下）。

    只认最通行的三种命名 —— 宁可漏认也不能把生产文件误判成测试文件
    （误判会让"生产依赖测试"的清洗反过来吃掉正常契约）。
    """
    text = str(path or "").replace("\\", "/")
    if not text:
        return False
    name = text.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py") or "/tests/" in f"/{text}"


def strip_test_dependencies(plan: Any) -> list[dict]:
    """**机械剔除**「生产文件 → 测试文件」的契约引用（§8.3 的不可满足形态）。

    为什么必须由机制做，而不是靠架构师改：真机 20260930-132625 连续 3 次产出
    ``T-02 的 contracts.uses = ['game_logic_test.py:TestSnake.test_move']`` ——
    生产模块引用测试方法，方向**不可能**成立（测试依赖实现，实现永不依赖测试）。
    闸门每次都判 ``PLAN_CONTRACT_UNKNOWN`` / ``contract_unresolved``，模型只会把
    测试方法改个名字，2 次自纠必然耗尽 ⇒ 整条流水线卡在方案阶段。

    这里把这类引用**删掉**（原地改 ``plan``），并返回被删清单供调用方留痕 ——
    与既有纪律一致：机械能判定的不可能项，就地归一 + 暴露，而不是无限返工。

    ⚠ 只删「非测试文件 → 测试文件」这一种方向。测试文件引用生产符号、
    生产文件引用生产符号，一律不动。
    """
    dropped: list[dict] = []
    for task in (plan or {}).get("tasks") or []:
        if not isinstance(task, dict):
            continue
        owner_files = [str(p) for p in (task.get("target_files") or []) if str(p)]
        if not owner_files or all(_is_test_path(p) for p in owner_files):
            continue                      # 测试文件自己的 uses 合法，不动
        contracts = task.get("contracts")
        if not isinstance(contracts, dict):
            continue
        uses = [str(x) for x in (contracts.get("uses") or []) if str(x or "").strip()]
        keep: list[str] = []
        for ref in uses:
            target = ref.partition(":")[0] or ref.split(".")[0]
            if _is_test_path(target):
                dropped.append({
                    "task": str(task.get("id") or ""),
                    "files": owner_files,
                    "ref": ref,
                    "detail": (f"{task.get('id')}（{'、'.join(owner_files)}）的 contracts.uses "
                               f"引用了测试文件 {ref} —— 生产代码不得依赖测试，已机械剔除"),
                })
                continue
            keep.append(ref)
        if len(keep) != len(uses):
            contracts["uses"] = keep
    return dropped


def _split_ref(ref: Any) -> tuple[str, str]:
    """把 ``contracts.uses`` 的引用拆成 ``(来源, 符号)``。

    支持架构师实际在用的两种写法：
      * ``game_logic.py:Game`` —— **文件:符号**（真机最常见）
      * ``game_logic.SNAKE_BODY_COLOR`` / ``Game.score`` —— 点号路径

    以前只按点号切 ⇒ ``game_logic.py:Game`` 被切成 ``py:Game``，于是报出
    "方案里没有定义 `py:Game`" 这种**假**阻断（真机 20260930-132625 命中 2 条，
    而 `Game` 明明就在方案 changes 里）。
    """
    text = str(ref or "").strip()
    if ":" in text:
        source, _, symbol = text.partition(":")
        return source.strip(), symbol.strip()
    parts = [p for p in text.split(".") if p]
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return "", (parts[0] if parts else "")


def _declared_symbol_names(plan: Any, skeleton: Any = None) -> tuple[set[str], set[str]]:
    """方案 + 骨架声明过的符号，返回 ``(broad, top_level)`` 两个集合。

    ``broad``
        含完整写法与其各段（`Game.score` → `Game.score` / `Game` / `score`），
        供 interface / contracts 的**引用**比对用。
    ``top_level``
        **只含无点号的顶层符号** —— 成员写法的归属核对必须用这一份。
        若也把 `Game.score` 拆出的 `Game` 算进"已声明"，那
        「`Game.score` 有没有归属」这个问题会**恒为真**（自己证明自己），检查形同虚设。
    """
    broad: set[str] = set()
    top_level: set[str] = set()

    def _add(raw: Any) -> None:
        sym = str(raw or "").strip()
        if not sym:
            return
        broad.add(sym)
        for part in sym.split("."):
            part = part.strip()
            if part:
                broad.add(part)
        if "." not in sym:
            top_level.add(sym)

    for bucket in (plan.get("changes") if isinstance(plan, dict) else None,
                   plan.get("tasks") if isinstance(plan, dict) else None):
        for item in bucket or []:
            if not isinstance(item, dict):
                continue
            for sym in item.get("symbols") or []:
                _add(sym)
            for sym in item.get("target_symbols") or []:
                _add(sym)
    if isinstance(skeleton, dict):
        for path, val in (skeleton.get("files") or {}).items():
            if isinstance(path, str) and path.endswith(".py"):
                stem = path[:-3].rsplit("/", 1)[-1]
                broad.add(stem)
                top_level.add(stem)
            for sym in (val.get("symbols") if isinstance(val, dict) else val) or []:
                _add(sym)
        for item in skeleton.get("changes") or []:
            if isinstance(item, dict):
                for sym in item.get("symbols") or []:
                    _add(sym)
    return broad, top_level


def _task_dep_refs(task: dict) -> list[str]:
    """一张图声明的所有"我用谁"的引用（contracts.uses / deps / 任务级 deps 与 uses）。"""
    refs: list[str] = []
    contracts = task.get("contracts") if isinstance(task.get("contracts"), dict) else {}
    for key in ("uses", "deps"):
        for raw in contracts.get(key) or []:
            if str(raw or "").strip():
                refs.append(str(raw).strip())
    for key in ("deps", "uses"):
        value = task.get(key)
        if isinstance(value, (list, tuple)):
            refs.extend(str(x).strip() for x in value if str(x or "").strip())
        elif isinstance(value, str) and value.strip():
            refs.append(value.strip())
    return refs


def _normalize_forbidden(entries: Any) -> dict[str, list[str]]:
    """``forbidden_modules`` 归一成 ``{模块: [作用域文件]}``（空列表 = 全项目）。

    接三种形态，向后兼容：
      * ``"tkinter"`` —— 全项目禁用；
      * ``{"module": "tkinter", "files": ["game_logic.py"]}`` —— **只在这些文件里**禁用；
      * ``{"tkinter": ["game_logic.py"]}`` —— 整份映射（``semantics.forbidden_module_scopes``
        的字典化形态）。**必须显式支持**：传映射时若按"可迭代 = 逐个模块名"处理，
        拿到的是键（字符串）⇒ 作用域被静默丢掉、退化成全局禁用，正好制造误伤。

    作用域是必需的：真机贪吃蛇需求写的是「`game_logic.py` 中不得出现 import tkinter」，
    而 `ui.py` **必须**用 tkinter 画 Canvas。全局禁用会把正确方案判成违规 → 架构师
    无限返工（永远改不对，因为改对的方式就是"ui.py 用 tkinter"）。
    """
    if isinstance(entries, dict) and not isinstance(entries, type):
        entries = [{"module": k, "files": v} for k, v in entries.items()]
    out: dict[str, list[str]] = {}
    for entry in entries or ():
        if isinstance(entry, dict):
            module = str(entry.get("module") or "").strip()
            files = [str(f).replace("\\", "/") for f in (entry.get("files") or []) if str(f)]
        else:
            module, files = str(entry or "").strip(), []
        if not module:
            continue
        if module not in out:
            out[module] = files
        elif not files:
            out[module] = []           # 任一约束是全项目 ⇒ 取最严
        elif out[module]:
            out[module] = sorted(set(out[module]) | set(files))
    return out


def _scope_hit(files: Any, scope: list[str]) -> bool:
    """任务文件是否落在约束作用域内（空作用域 = 全项目，恒真）。"""
    if not scope:
        return True
    names = {str(f).replace("\\", "/") for f in (files or [])}
    for item in scope:
        if item in names or PurePosixPath(item).name in {PurePosixPath(n).name for n in names}:
            return True
    return False


def validate_architect_plan(
    plan: Any, skeleton: Any = None, *, forbidden_modules: Iterable[Any] = (),
) -> list[dict]:
    """方案静态 lint（**纯函数**，零模型）：symbols / interface / contracts 三者互证。

    返回 finding 列表，每项 ``{code, task, symbol, severity, detail}``；
    ``severity`` 为 ``block``（阻断，回架构师）或 ``warn``（只提示，不阻断）。

      * ``PLAN_SYMBOL_UNKNOWN_OWNER``  —— `Game.score` 这种成员写法，但 `Game` 没声明
      * ``PLAN_INTERFACE_UNKNOWN``     —— interface 里调用了没声明的符号（§8.2）
      * ``PLAN_CONTRACT_UNKNOWN``      —— contracts.uses 引用了方案里不存在的符号（§8.3）
      * ``PLAN_UNDECLARED_DEPENDENCY`` —— 出现明确禁止的第三方依赖（§8.4）
      * ``PLAN_FORBIDDEN_DEPENDENCY``  —— 命中**用户硬约束**禁止的模块（§7 禁止约束完整性）
      * ``PLAN_UNRELATED_DEPENDENCY``  —— math/time 这类未必被要求的依赖（§8.5，仅 warn）

    ``forbidden_modules`` 来自 Grounded Requirement Contract 的硬约束
    （``semantics.forbidden_modules``）。为什么必须由调用方传进来：`tkinter` 是标准库，
    既不在 `_NON_STDLIB_BLOCK` 也不在 `_WEAK_DEPS` —— 单靠本模块的内置名单**永远抓不到**
    "game_logic.py 不得 import tkinter" 这类用户约束。
    """
    findings: list[dict] = []
    if not isinstance(plan, dict):
        return findings
    forbidden = _normalize_forbidden(forbidden_modules)
    declared, top_level = _declared_symbol_names(plan, skeleton)
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        tid = str(task.get("id") or "")
        files = [str(p) for p in (task.get("target_files") or []) if str(p)]
        # ---- §7 禁止约束完整性：命中**用户硬约束**点名的模块 ⇒ 直接阻断。
        # 与 §8.4 的区别：§8.4 是本模块的内置"非标准库"名单，抓不到 tkinter 这类标准库
        # 模块；用户说"game_logic.py 不得依赖 tkinter"时，只有这条能抓住。
        if forbidden:
            for ref in _task_dep_refs(task):
                module = ref.split(".")[0].strip()
                scope = forbidden.get(module)
                if scope is None:
                    continue
                # 作用域：只查约束点名的文件（`game_logic.py` 禁 tkinter 不该连坐 ui.py）。
                # 任务没有文件信息时不判（宁漏不误伤 —— 真产物还有 verify 那道机械检查兜底）。
                if not files or not _scope_hit(files, scope):
                    continue
                findings.append({
                    "code": "PLAN_FORBIDDEN_DEPENDENCY",
                    "task": tid,
                    "symbol": ref,
                    "files": files,
                    "severity": "block",
                    "detail": (
                        f"{tid} 依赖 {module}，而用户硬约束明确禁止依赖它"
                        + (f"（作用域：{'、'.join(scope)}）" if scope else "（全项目禁止）")
                        + (f"；该任务文件：{'、'.join(files)}" if files else "")
                        + " —— 请改用允许的实现方式；若确有必要，必须先由人工修改约束"
                    ),
                })
        # ---- §8.1 成员写法必须有归属
        for raw in task.get("symbols") or []:
            sym = str(raw or "").strip()
            if "." not in sym:
                continue
            owner = sym.split(".")[0].strip()
            if owner and owner not in top_level:
                findings.append({
                    "code": "PLAN_SYMBOL_UNKNOWN_OWNER", "task": tid, "symbol": sym,
                    "severity": "block",
                    "detail": f"{tid} 声明了成员符号 {sym}，但 {owner} 未在方案/骨架里声明",
                })
        # ---- §8.2 interface 调用了没声明的符号
        interface = str(task.get("interface") or "").strip()
        if interface:
            for name, is_member in sorted(_interface_refs(interface).items()):
                # 单字母（`x`/`i`）一律是局部名/参数，不可能是"方案该声明的符号"
                if len(name) <= 1 or name in declared:
                    continue
                if is_member:
                    # **成员**（`Food.generate(board)` 里的 generate）：方案 symbols 里
                    # 通常只写类名，逐条列方法不是惯例；而"成员是否真实存在"由**冻结的
                    # 接口骨架**当基准（Plan IR 就按骨架解析，解析不了另有 contract_unresolved
                    # 兜底）。这里只提示，不阻断 —— 判负会让方案阶段**永不收敛**
                    # （真机 20260930-132625：这条 3 次尝试都没消除，2 次自纠必然耗尽）。
                    findings.append({
                        "code": "PLAN_INTERFACE_MEMBER_UNKNOWN", "task": tid, "symbol": name,
                        "severity": "warn",
                        "detail": (f"{tid} 的 interface 用了成员 {name}，但它没在 symbols 里"
                                   "逐条声明；接口基准以骨架为准（仅提示）"),
                    })
                    continue
                # **裸标识符**（`Game()` / `process()`）才是"方案该声明却没有"的真问题
                findings.append({
                    "code": "PLAN_INTERFACE_UNKNOWN", "task": tid, "symbol": name,
                    "severity": "block",
                    "detail": f"{tid} 的 interface 用到 {name}，但方案 symbols/骨架里没有它（§8.2）",
                })
        # ---- §8.3 / §8.4 / §8.5 contracts
        contracts = task.get("contracts") if isinstance(task.get("contracts"), dict) else {}
        for raw in contracts.get("uses") or []:
            ref = str(raw or "").strip()
            if not ref:
                continue
            module = ref.split(".")[0].split(":")[0].strip()
            symbol = _split_ref(ref)[1]
            if module in _NON_STDLIB_BLOCK:
                findings.append({
                    "code": "PLAN_UNDECLARED_DEPENDENCY", "task": tid, "symbol": ref,
                    "severity": "block",
                    "detail": f"{tid} 依赖 {module}（非标准库且未被需求允许）",
                })
                continue
            if module in _WEAK_DEPS:
                findings.append({
                    "code": "PLAN_UNRELATED_DEPENDENCY", "task": tid, "symbol": ref,
                    "severity": "warn",
                    "detail": f"{tid} 用到 {module}，但接口/行为未要求它（§8.5，仅提示）",
                })
                continue
            if symbol and symbol not in declared and module not in declared:
                findings.append({
                    "code": "PLAN_CONTRACT_UNKNOWN", "task": tid, "symbol": ref,
                    "severity": "block",
                    "detail": f"{tid} 的 contracts.uses 引用 {ref}，但方案里没有定义 {symbol}（§8.3）",
                })
    return findings
