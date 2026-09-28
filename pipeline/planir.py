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

import hashlib
import io
import os
from typing import Any

from . import symbols as symbol_resolver

#: 编译器**输入契约**版本。见模块 docstring 的「冻结时机」。
COMPILER_INPUT_VERSION = "1"

__all__ = [
    "COMPILER_INPUT_VERSION",
    "normalize_plan",
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


def normalize_plan(plan: Any, *, skeleton: Any = None, previous: Any = None) -> dict:
    """`raw_plan` → `compiler_ir`。**纯函数**：同输入必得同输出（含 key 顺序）。

    冲突规则（全部显式规定，不留"看情况"）：

      ① **symbols 最长匹配**：`CLI` 与 `CLI.add` 并存 ⇒ 删 `CLI`，记入 `conflicts`；
      ② **多张 draft task 改同一文件** ⇒ 合并成一个 unit，但 `intent` 保留
         `{text, source}` 来源，出问题时能追溯到是哪张图提的要求；
      ③ **稳定排序**：draft task id → change.order → 符号字母序
         （**不依赖 dict 插入顺序** —— 那会让 IR 在等价输入下漂移）。
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
                "source_task_ids": [],
                "uses_resolved": [],
                "unresolved": [],
                "_declared": [],
                "_order": order_index.get(path, len(changes)),
            }
        return units[path]

    for change in changes:
        path = _norm(change.get("path"))
        unit = _unit(path)
        # **入口也要归一**：`changes[].symbols` 真机里写成 `main()`（带调用括号），而 task
        # 那一路已经过 resolver 的 `clean_symbol` ⇒ 裸名。两条入口书写不一致时，同一符号会在
        # 定稿后的清单里**出现两次**（真机 `20260928-110402`：`main` 与 `main()` 并存），
        # 提示词里就成了"要定义两个东西"，而机械自检的判据也跟着分裂。
        unit["_declared"].extend(
            symbol_resolver.clean_symbol(s)
            for s in _as_list(change.get("symbols"))
            if symbol_resolver.clean_symbol(s)
        )
        if str(change.get("intent") or "").strip():
            unit["intent"].append({"text": str(change["intent"]).strip(), "source": "changes"})
        if not unit["change"]:
            # 「改什么」优先取 approach（更具体），退回 intent
            unit["change"] = str(change.get("approach") or change.get("intent") or "").strip()

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
            unit["symbols"].extend(buckets.get(f) or [])
            # 来源追溯：多张图改同一文件时，逐条记录是"谁提的"
            for key in ("change", "acceptance"):
                val = task.get(key)
                if key == "acceptance" and not isinstance(val, str):
                    continue
                if isinstance(val, str) and val.strip():
                    if key == "change" and not unit["change"]:
                        unit["change"] = val.strip()
                    if key == "acceptance":
                        unit["acceptance"].append({"text": val.strip(), "source": tid or "draft"})
            if str(task.get("test_hint") or "").strip() and not unit["test_hint"]:
                unit["test_hint"] = str(task["test_hint"]).strip()
            if str(task.get("interface") or "").strip() and not unit["interface"]:
                unit["interface"] = str(task["interface"]).strip()
            if str(task.get("data_model") or "").strip() and not unit["data_model"]:
                unit["data_model"] = str(task["data_model"]).strip()
            unit["constraints"].extend(
                str(s).strip() for s in _as_list(task.get("constraints")) if str(s).strip()
            )
            unit["_declared"].extend(buckets.get(f) or [])
            unit["depends_on"].extend(
                str(d).strip() for d in _as_list(task.get("depends_on")) if str(d).strip()
            )
            contracts = task.get("contracts")
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

        uses = _dedup(unit["contracts"]["uses"])
        unit["contracts"]["uses"] = uses
        unit["contracts"]["exposes"] = _dedup(unit["contracts"]["exposes"])
        resolved = [dict(symbol_resolver.resolve(s, files=[path], index=index), role="uses") for s in uses]
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
        # 依赖的**文件集合**（由符号解析推出，不是让模型写路径）
        dep_files = sorted(
            {
                c
                for row in resolved
                if row.get("resolved")
                for c in (row.get("candidates") or [])
                if c != path
            }
        )
        unit["depends_on_files"] = dep_files
        unit["stable_id"] = stable_id(path, unit["symbols"])
        unit["intent"] = _dedup_intents(unit["intent"])
        unit["acceptance"] = _dedup_intents(unit["acceptance"])
        unit["constraints"] = _dedup(unit["constraints"])
        unit["depends_on"] = _dedup(unit["depends_on"])
        unit["source_task_ids"] = _dedup(unit["source_task_ids"])
        out_units.append(unit)

    ir: dict[str, Any] = {
        "compiler_input_version": COMPILER_INPUT_VERSION,
        "plan_sources": {"changes": bool(changes), "draft_tasks": bool(draft)},
        "units": out_units,
        "conflicts": conflicts,
        "warnings": [w for u in out_units for w in (u.get("unresolved") or [])],
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
    for name in ("planir.py", "taskcompiler.py", "symbols.py"):
        try:
            with io.open(os.path.join(here, name), encoding="utf-8") as fh:
                compiler.update(fh.read().encode("utf-8"))
        except OSError:
            continue
    compiler.update(COMPILER_INPUT_VERSION.encode("utf-8"))
    return {
        "compiler_input_version": COMPILER_INPUT_VERSION,
        "compiler_hash": compiler.hexdigest()[:12],
        "prompt_hash": str(prompt_hash or ""),
    }
