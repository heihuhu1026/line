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

from . import ontology
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


def compile_plan(
    plan: Any,
    *,
    max_symbols: int = MAX_SYMBOLS_PER_TASK,
    ir: Any = None,
    existing_files: set[str] | None = None,
    previous_tasks: Any = None,
    scope: Any = None,
) -> dict:
    """编译入口（**带错误通道**）：返回 ``{"tasks": [...], "errors": [...]}``。

    ``tasks`` 始终按硬约束生成（单张符号数绝不超 ``max_symbols``）；当一个文件需要的
    图数超过 ``MAX_TASKS_PER_FILE``（即符号总量超过 单图上限×单文件图数上限）时，
    **不靠放宽单图大小消化**（那会产出 dev 单轮写不完的图 —— 旧 ``_split_groups``
    的真实 bug：9/10 个符号被切成 5 个一张），而是照常切出合格的图、同时在 ``errors``
    里报 ``task_capacity_exceeded`` —— 由上游 Design Gate 决定回架构师重拆还是转人工，
    绝不带一张超限的图进开发。

    ``existing_files``：编译时刻仓库里**已存在**的文件相对路径集合。不在集合里的文件
    是本次方案要新建的 —— 其 DAG 第一张图被标 ``creates_file: true``（**文件创建租约**
    的唯一 owner，run 20260929-093329：同文件 4 张图都整份 add，合并互相覆盖、modify
    全部 anchor 落空）。为 None（旧调用 / 单测，信息未知）时按全新项目处理：每个文件
    第一张图都是 owner。
    """
    units = [
        u for u in ((ir or {}).get("units") or []) if isinstance(u, dict) and _norm(u.get("file"))
    ]
    if units:
        tasks, errors = _compile_from_units(units, max_symbols=max_symbols)
    else:
        tasks, errors = _compile_from_changes(plan, max_symbols=max_symbols)
    # Ontology 语义身份/版本链（规格§十四）：跨重编号稳定 + rework 修订可追。
    if previous_tasks is None and isinstance(plan, dict):
        previous_tasks = plan.get("tasks")
    _annotate_semantic_revisions(tasks, previous_tasks)
    _annotate_create_owners(tasks, existing_files)
    # P0-3：把**文件级**需求收窄到每张图自己的 facet（问题 C 的根治）；
    # P0-4：同文件多创建者 = 连续 full-file add，机械阻断。
    # 都只在拿到 scope 时做（老调用/单测不传 scope → 行为不变，向后兼容）。
    if scope is not None:
        _bind_requirements_by_facet(tasks, scope, errors)
        errors.extend(same_file_add_violations(tasks))
    return {"tasks": tasks, "errors": errors, "file_owners": file_owner_map(tasks)}


def compile_tasks(
    plan: Any,
    *,
    max_symbols: int = MAX_SYMBOLS_PER_TASK,
    ir: Any = None,
    existing_files: set[str] | None = None,
) -> list[dict]:
    """兼容包装：只要施工图（容量错误走 :func:`compile_plan` 的 ``errors``）。"""
    return compile_plan(
        plan, max_symbols=max_symbols, ir=ir, existing_files=existing_files
    )["tasks"]


def _semantic_identity(task: dict) -> str:
    """施工图的语义身份（规格§十四）：hash(目标文件 + 符号 + facet 内容)，不含 T-xx 编号。"""
    return ontology.semantic_task_id(
        task.get("target_files") or [],
        task.get("symbols") or [],
        [str(task.get("change") or ""), str(task.get("interface") or "")],
    )


def _annotate_semantic_revisions(tasks: list[dict], previous_tasks: Any) -> None:
    """给每张施工图盖**跨轮稳定身份**与版本链（Ontology Kernel，规格§十四/J）。

      * ``semantic_task_id``：只取决于 文件/符号/facet 内容 —— 架构师 rework 重编号
        T-02→T-03 后身份不变，Defect/责任映射不断链；
      * ``task_revision``：同一语义身份再次编译时 +1，首次为 1；
      * ``supersedes``：记录上一版（及更早）施工图的 T-xx，纯溯源，不参与 DAG。
    """
    prev: dict[str, dict] = {}
    for t in previous_tasks or []:
        if isinstance(t, dict) and t.get("semantic_task_id"):
            prev[str(t["semantic_task_id"])] = t
    for task in tasks:
        sid = str(task.get("semantic_task_id") or _semantic_identity(task))
        task["semantic_task_id"] = sid
        old = prev.get(sid)
        if old is None:
            task.setdefault("task_revision", 1)
            task.setdefault("supersedes", [])
            continue
        task["task_revision"] = int(old.get("task_revision") or 1) + 1
        chain: list[str] = []
        old_id = str(old.get("id") or "")
        if old_id:
            chain.append(old_id)
        for earlier in old.get("supersedes") or []:
            if earlier not in chain:
                chain.append(str(earlier))
        task["supersedes"] = chain


def resolve_bug_task(defect: Any, tasks: Any) -> dict:
    """规格§二十八：把缺陷机械归到一张施工图（**禁止"看起来像 T-03"**）。

    优先级逐级下降，命中即返回；同级多候选判 ``unresolved`` 不猜：

      1. ``semantic_task_id`` 相等 **且** 符号属于该图（跨 rework 重编号不断链）；
      2. legacy ``stable_id`` 相等且符号属于该图；
      3. 文件命中且符号属于该图（file + symbol）；
      4. 仅文件命中（file only）——同文件多图且无法再区分时也报候选，不强压第一张。

    返回 ``{"task_id", "semantic_task_id", "matched_by", "candidates"}``；
    无法定位时 ``task_id`` 为空、``matched_by="unresolved"``。
    """
    defect = defect if isinstance(defect, dict) else {}
    task_list = [t for t in (tasks or []) if isinstance(t, dict)]

    path = _norm(defect.get("path"))
    raw_sym = str(defect.get("symbol") or "").strip()
    sym = raw_sym.rsplit(".", 1)[-1]
    defect_sid = str(defect.get("semantic_task_id") or "")
    stable_hint = str(defect.get("stable_id") or "")

    def _symbols_of(task: dict) -> set[str]:
        return {str(s).strip().rsplit(".", 1)[-1] for s in (task.get("symbols") or []) if str(s).strip()}

    def _files_of(task: dict) -> set[str]:
        return {_norm(p) for p in (task.get("target_files") or []) if _norm(p)}

    def _pick(matched: list[dict], require_symbol: bool) -> dict | None:
        if not matched:
            return None
        if require_symbol and sym:
            with_sym = [t for t in matched if sym in _symbols_of(t)]
            if len(with_sym) == 1:
                return with_sym[0]
            if len(with_sym) > 1:
                return {"ambiguous": [str(t.get("id") or "") for t in with_sym]}
            return None
        if len(matched) == 1:
            return matched[0]
        return {"ambiguous": [str(t.get("id") or "") for t in matched]}

    def _answer(hit: dict | None, by: str) -> dict | None:
        if isinstance(hit, dict) and "ambiguous" not in hit:
            return {"task_id": str(hit.get("id") or ""),
                    "semantic_task_id": str(hit.get("semantic_task_id") or ""),
                    "matched_by": by, "candidates": []}
        return None

    def _ambiguous(hit: dict | None, by: str) -> dict | None:
        if isinstance(hit, dict) and "ambiguous" in hit:
            return {"task_id": "", "semantic_task_id": "",
                    "matched_by": "ambiguous_" + by, "candidates": hit["ambiguous"]}
        return None

    # ① 语义身份 + 符号（最强证据；同 id 命中多张只可能是重复编译，歧义必须显报）
    if defect_sid:
        hit = _pick([t for t in task_list if t.get("semantic_task_id") == defect_sid], True)
        ans = _answer(hit, "semantic_id+symbol") or _ambiguous(hit, "semantic_id")
        if ans:
            return ans
    # ② legacy stable_id + 符号
    if stable_hint:
        hit = _pick([t for t in task_list if t.get("stable_id") == stable_hint], True)
        ans = _answer(hit, "stable_id+symbol") or _ambiguous(hit, "stable_id")
        if ans:
            return ans
    # ③ 文件 + 符号
    if path and sym:
        in_file = [t for t in task_list if path in _files_of(t)]
        hit = _pick(in_file, True)
        ans = _answer(hit, "file+symbol") or _ambiguous(hit, "symbol")
        if ans:
            return ans
    # ④ 仅文件（同文件多图时不强压，交候选）
    if path:
        in_file = [t for t in task_list if path in _files_of(t)]
        hit = _pick(in_file, False)
        ans = _answer(hit, "file-only") or _ambiguous(hit, "file")
        if ans:
            return ans
    return {"task_id": "", "semantic_task_id": "", "matched_by": "unresolved", "candidates": []}


def _annotate_create_owners(tasks: list[dict], existing_files: set[str] | None) -> None:
    """**文件创建租约**（G3）：每个新建文件的第一张施工图标记 ``creates_file: true``。

    只认 DAG 排序后的**第一张**（tasks 已是拓扑序；同文件顺序边保证 owner 先施工）：
    owner 有权整份新建（含文件头 import），其余同文件图只能在**在制文件**上定点增补；
    非 owner 的 ``add`` 整份重写由 orchestrator 机械丢弃。仓库已存在的文件不发租约
    （所有图天然都是 modify）。确定性：同输入同标注（不依赖 dict 序）。
    """
    existing = {_norm(p) for p in (existing_files or set())}
    known = existing_files is not None
    owned: set[str] = set()
    for task in tasks:
        for p in (task.get("target_files") or []):
            path = _norm(p)
            if not path or path in owned:
                continue
            if known and path in existing:
                continue  # 存量文件：没有"创建"动作，不设 owner
            task["creates_file"] = True
            owned.add(path)


def file_owner_map(tasks: list[dict]) -> dict[str, dict]:
    """每个文件的**创建租约**台账（P0-4 §6.1）：``{file: {create_owner, modify_tasks}}``。

    `create_owner` 是唯一有权整份新建的图；其余同文件的图只能在**在制文件**上定点增补。
    回答「同一个新文件为什么不应该被连续 add 三次」—— 有了 owner，第二、三张图的
    `change_type=add` 整份重写就是越权（``SAME_FILE_MULTI_ADD``），而不是"正常施工"。
    """
    owners: dict[str, dict] = {}
    for task in tasks:
        tid = str(task.get("id") or "")
        for raw in task.get("target_files") or []:
            path = _norm(raw)
            if not path:
                continue
            slot = owners.setdefault(path, {"create_owner": "", "modify_tasks": []})
            if task.get("creates_file"):
                if not slot["create_owner"]:
                    slot["create_owner"] = tid
                elif tid and tid != slot["create_owner"]:
                    slot["modify_tasks"].append(tid)
            elif tid:
                slot["modify_tasks"].append(tid)
    return owners


def same_file_add_violations(tasks: list[dict]) -> list[dict]:
    """**机械阻断**（P0-4 §6.3）：同一新文件出现**多个**创建者 = 连续 full-file add。

    ``_annotate_create_owners`` 正常情况下只发一份租约，所以这里命中即说明租约被绕过
    （或调用方没走编译链）。真机形态：T-01/T-02/T-03 都对 ``game_logic.py`` 整份 add
    → 后写的覆盖先写的、符号丢失、modify 的 anchor 全落空。
    合法例外只有 ``legacy migration``（显式标注且带 base revision）。
    """
    violations: list[dict] = []
    for path in file_owner_map(tasks):
        # **只看声明了创建租约的图有几张**：1 owner + N 张 modify 是**正常**形态
        # （那是"先整份新建、再定点增补"），绝不能当成违规；
        # 出现第 2 个创建者才是"同一新文件被连续 add"。
        creators = [
            t for t in tasks
            if t.get("creates_file")
            and any(_norm(p) == path for p in (t.get("target_files") or []))
        ]
        if len(creators) <= 1:
            continue
        ids = [str(t.get("id") or "") for t in creators]
        violations.append({
            "code": "SAME_FILE_MULTI_ADD",
            "file": path,
            "create_owner": ids[0],
            "extra_creators": ids[1:],
            "detail": (
                f"{path} 出现 {len(creators)} 个创建者（{'、'.join(ids)}）"
                " —— 同一新文件只允许第一张图 add 整份新建，其余必须 modify 定点增补；"
                "否则后写的整份覆盖先写的，符号与文件头都会丢"
            ),
        })
    return violations


def bind_task_requirements(scope: Any, task: dict) -> dict:
    """按**任务 facet**（不是文件）重新绑定 Requirement / Claim / PO。

    为什么必须重绑：`ontology.requirement_unit_links` 是**文件级**匹配，一个文件的
    所有施工图会拿到同一批 FR（问题 C：T-01/02/03 全是 FR-01 FR-04 FR-05），
    任务与需求的关系因此失去精确性。这里把每张图**自己的**
    symbols / change / interface / acceptance 喂回同一个确定性匹配器，
    得到这张图真正对得上号的需求。

    刻意复用既有匹配器（ascii 词 + CJK bigram，共享比阈值）—— 不新写
    ``if "score" in text`` 这种模糊包含（规格 §5.4：那只能当低置信提示）。
    """
    if not isinstance(scope, dict) or not isinstance(task, dict):
        return {}
    files = [str(p) for p in (task.get("target_files") or []) if str(p)]
    path = _norm(files[0]) if files else ""
    pseudo = {
        "file": path,
        "symbols": [str(s) for s in (task.get("symbols") or [])],
        "change": str(task.get("change") or ""),
        "interface": str(task.get("interface") or ""),
        "acceptance": [task.get("acceptance")] if task.get("acceptance") else [],
        "intent": [],
    }
    links = ontology.requirement_unit_links(scope, [pseudo])
    hit = (links.get("by_file") or {}).get(path) or {}
    return {
        "requirements": [str(x) for x in (hit.get("requirements") or [])],
        "claims": [str(x) for x in (hit.get("claims") or [])],
        "proof_obligations": [str(x) for x in (hit.get("proof_obligations") or [])],
    }


def _req_of(po_id: str) -> str:
    """PO id 形如 ``po:FR-01:...`` → 反解出需求 id（**确定性**，不靠猜）。"""
    parts = str(po_id or "").split(":")
    return f"req:{parts[1]}" if len(parts) >= 2 and parts[1] else ""


def _bind_requirements_by_facet(tasks: list[dict], scope: Any, errors: list[dict]) -> None:
    """把文件级需求收窄到**每张图自己的 facet**（P0-3 §5.1 / §5.3）。

    优先级（规格 §5.1）：
        1. draft 图**显式**声明的 requirement_ids（模型候选，compiler 才是权威）
        2. 显式 claim_ids
        3. 显式 proof_obligation_ids（PO id 反解需求）
        4. 文件级 ``requirement_unit_links`` —— **只能当候选**，必须经 facet 验证

    只有第 4 档来源时：不无条件复制进 ``implements_requirements``，
    而是留在 ``candidate_requirement_ids`` 供人工/闸门看到"这张图可能还涉及这些"。
    显式声称却**完全无法**与 facet 对上号的 → ``invalid_requirement_binding``（不静默接受）。
    """
    if not isinstance(scope, dict):
        return
    for task in tasks:
        file_level = [str(x) for x in (task.get("implements_requirements") or [])]
        facet = bind_task_requirements(scope, task)
        facet_reqs = list(facet.get("requirements") or [])
        # 1) 显式声明（模型候选）
        explicit = [str(x) for x in (task.get("requirement_ids") or [])]
        # 3) PO 反解
        from_po = [r for r in (_req_of(p) for p in (task.get("proof_obligations") or [])) if r]
        claimed = _dedup([*(explicit or []), *(from_po or [])])
        # facet 验证：facet 命中 ∩（显式声称 ∪ 文件级候选）
        allowed = set(facet_reqs) | set(file_level)
        resolved = list(facet_reqs)
        for r in claimed:
            if r not in resolved:
                resolved.append(r)
        invalid = [r for r in claimed if r not in allowed]
        if invalid:
            errors.append({
                "code": "invalid_requirement_binding",
                "task": str(task.get("id") or ""),
                "requirements": invalid,
                "detail": (
                    f"{task.get('id')} 声称实现 {'、'.join(invalid)}，"
                    "但其 acceptance / change / symbols / interface 均无法与该需求对应"
                    " —— 不能静默接受（要么补上可证的关系，要么改声称）"
                ),
            })
        task["implements_requirements"] = resolved
        task["candidate_requirement_ids"] = [r for r in file_level if r not in resolved]
        if facet.get("proof_obligations"):
            task["proof_obligations"] = list(facet["proof_obligations"])
        if facet.get("claims"):
            task["claim_ids"] = list(facet["claims"])


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
    """按**单张符号上限**硬切：单张绝不超过 `max_symbols`（dev 单轮写得完的前提）。

    刻意不再为了迁就 `MAX_TASKS_PER_FILE` 而放宽单张大小 —— 旧实现
    ``size=max(max_symbols, ceil(n/MAX_TASKS_PER_FILE))`` 在 9/10 个符号时会切出
    5 个符号一张的图，直接违反单图 ≤4 的约束，且没有任何错误通道。
    单文件图数超限（需要的图 > `MAX_TASKS_PER_FILE`）改由
    :func:`_compile_from_units` 产出 ``task_capacity_exceeded`` 错误，交给 Design Gate。
    """
    if not symbols:
        return [[]]
    return _chunk(symbols, max_symbols)


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


def _facet_segments(unit: dict, symbols: list[str], max_symbols: int) -> list[tuple[list[str], dict | None]]:
    """把一个 file unit 切成 ``[(符号组, 源 facet)]``。

    v3：优先按 ``work_units``（task 级 facet）切 —— 每张图只继承**产生这些符号的那张
    draft 图**的 change/interface/contracts/acceptance/constraints/test_hint；同文件
    多图之间不再 first-wins 串味（run 20260929-093329）。没有符号的 facet 不出图
    （它的符号都归到了同任务的其它文件）。v2 IR / changes 兼容路径退化为整文件切分。
    """
    wus = [w for w in (unit.get("work_units") or []) if isinstance(w, dict)]
    segments: list[tuple[list[str], dict | None]] = []
    seen_symbols: list[str] = []
    for wu in wus:
        wu_symbols = [str(s).strip() for s in (wu.get("symbols") or []) if str(s).strip()]
        for chunk in _chunk(wu_symbols, max_symbols):
            if chunk:
                segments.append((chunk, wu))
                seen_symbols.extend(chunk)
    # 兜底：facet 的符号并集与 unit.symbols 不一致（旧版 IR、或手写 IR）时，
    # 漏掉的符号仍按 v2 口径补成无 facet 的组，绝不静默丢符号。
    missing = [s for s in symbols if s not in seen_symbols]
    if missing:
        for chunk in _chunk(missing, max_symbols):
            segments.append((chunk, None))
    if not segments:
        segments = [(chunk, None) for chunk in _split_groups(symbols, max_symbols)]
    return segments


def _compile_from_units(
    units: list[dict], *, max_symbols: int
) -> tuple[list[dict], list[dict]]:
    """返回 ``(tasks, errors)``：tasks 永远满足单图符号上限；
    单文件图数超 ``MAX_TASKS_PER_FILE`` 时在 errors 里登记容量错误（图仍照常切出）。"""
    tasks: list[dict] = []
    errors: list[dict] = []
    seq = 0
    id_map: dict[str, list[str]] = {}
    file_tasks: dict[str, list[str]] = {}
    pending: list[tuple[dict, list[str], dict, dict | None]] = []
    for unit in units:
        path = _norm(unit.get("file"))
        symbols = [str(s).strip() for s in (unit.get("symbols") or []) if str(s).strip()]
        # 施工图字段**原样传给 dev**：这些是方案阶段好不容易补上的信息，
        # 编译器不得丢失（丢了 dev 就只能猜，跨文件接口必然对不上）。
        # 注意 data_model / unresolved_uses 是**文件级**事实（表结构只有一版、
        # 禁用项对全文件生效），挂给该文件每张图；interface/contracts/constraints
        # 已改为 facet 级，不再在这里整文件下发。
        file_hints = {
            key: unit.get(key)
            for key in ("data_model", "unresolved_uses")
            if unit.get(key) not in (None, "", [], {})
        }
        segments = _facet_segments(unit, symbols, max_symbols)
        # 容量按**符号总量**判，不再按 facet 切出的图数判：v3 保留架构师对同文件的
        # 多任务拆解（同文件顺序施工是受支持的形态，见文件创建租约），8 个符号分 4 张
        # facet 图与分 2 张图，dev 工作量相同、且每张图仍 ≤max_symbols，不该阻断。
        # 真正超的是单文件符号总量（>上限张数×单图上限），那才是文件边界要重拆。
        symbol_capacity = MAX_TASKS_PER_FILE * max_symbols
        if len(symbols) > symbol_capacity:
            errors.append(
                {
                    "code": "task_capacity_exceeded",
                    "file": path,
                    "symbols": len(symbols),
                    "tasks_needed": len(segments),
                    "max_tasks_per_file": MAX_TASKS_PER_FILE,
                    "max_symbols_per_task": max_symbols,
                    "detail": (
                        f"{path} 声明 {len(symbols)} 个符号，超过单文件容量 {symbol_capacity}"
                        f"（{MAX_TASKS_PER_FILE} 张图 × 每张 {max_symbols} 个符号；当前需 "
                        f"{len(segments)} 张图）—— 需架构师重拆文件边界或合并符号"
                    ),
                }
            )
        # 自依赖在 planir 合并前已取证（unit.self_depends）：这是方案层硬错误，
        # 不把该 id 再映成边（否则会同文件多图时凭空造出反向边），只登记错误交 Design Gate。
        self_ids = set(_dedup(unit.get("self_depends")))
        for sid in sorted(self_ids):
            errors.append(
                {
                    "code": "self_dependency",
                    "file": path,
                    "task": sid,
                    "detail": f"{path} 的施工图 {sid} 在 depends_on 里声明了它自己（任务不能自依赖）",
                }
            )
        new_ids: list[str] = []
        unit_has_facets = any(facet is not None for _group, facet in segments)
        # 只保留**跨 unit** 的 draft 依赖参与建边：
        #  · 自依赖（self_ids）：方案硬错误，上面已登记，且不能映成边（多图时会造幻影反向边）；
        #  · 同一 unit 内部的 draft 互依（T-02/T-03 合并后符号被重新分组，对应关系已不存在）：
        #    静默丢弃 —— 组间顺序由「同文件顺序边」承担，映成"所有组互依"反而会造假环。
        source_ids = set(_dedup(unit.get("source_task_ids")))
        unit_raw_deps = [
            d for d in _dedup(unit.get("depends_on"))
            if d not in self_ids and d not in source_ids
        ]
        for group, facet in segments:
            seq += 1
            tid = f"T-{seq:02d}"
            new_ids.append(tid)
            # ---- facet 级字段：只带产生本组符号的那张 draft 图的内容（v3）
            if facet is not None:
                change_text = str(facet.get("change") or "").strip()
                if not change_text and str(facet.get("source") or "") == "@changes":
                    # changes facet 的 change 为空时退回 file 级 approach/intent
                    change_text = str(unit.get("change") or "").strip()
                acceptance = _acceptance_text(facet.get("acceptance"), path, group)
                interface = str(facet.get("interface") or "").strip()
                constraints = _dedup(facet.get("constraints"))
                test_hint = str(facet.get("test_hint") or "").strip()
                uses = _dedup((facet.get("contracts") or {}).get("uses")) if isinstance(facet.get("contracts"), dict) else []
                exposes = _dedup((facet.get("contracts") or {}).get("exposes")) if isinstance(facet.get("contracts"), dict) else []
                if not uses and not exposes:
                    uses = _dedup(facet.get("uses"))
                    exposes = _dedup(facet.get("exposes"))
                facet_source = str(facet.get("source") or "")
                facet_deps = [
                    d for d in _dedup(facet.get("deps_raw"))
                    if d not in self_ids and d not in source_ids
                ]
            else:
                change_text = str(unit.get("change") or "").strip()
                acceptance = _acceptance_text(unit.get("acceptance"), path, group)
                interface = str(unit.get("interface") or "").strip()
                constraints = _dedup(unit.get("constraints"))
                test_hint = str(unit.get("test_hint") or "").strip()
                uses = _dedup((unit.get("contracts") or {}).get("uses"))
                exposes = _dedup((unit.get("contracts") or {}).get("exposes"))
                facet_source = ""
                facet_deps = unit_raw_deps
            task: dict[str, Any] = {
                "id": tid,
                "title": f"实现 {path} 的 {'、'.join(group)}" if group else f"实现 {path}",
                "change": change_text or f"按设计实现 {path}",
                "target_files": [path],
                "acceptance": acceptance,
                "symbols": list(group),
                "depends_on": [],
                # 跨轮**稳定身份**：task id 会被重编号（方案一重做就变），
                # 「哪些图没变」与归因必须建在不随编号漂移的东西上（见 planir.stable_id）
                "stable_id": f"{path}::{sorted(group)[0]}" if group else f"{path}::<whole-file>",
                # Ontology：本图实现哪些 Requirement / 需对哪些 PO 交证据（planir 投影）。
                "implements_requirements": list(unit.get("implements_requirements") or []),
                "proof_obligations": list(unit.get("proof_obligation_ids") or []),
            }
            if test_hint:
                task["test_hint"] = test_hint
            elif path.endswith(".py"):
                task["test_hint"] = f'python -c "import {_module_of(path)}"'
            if interface:
                task["interface"] = interface
            if constraints:
                task["constraints"] = constraints
            if uses or exposes:
                task["contracts"] = {"uses": uses, "exposes": exposes}
            for key, val in file_hints.items():
                task.setdefault(key, val)
            tasks.append(task)
            file_tasks.setdefault(path, []).append(tid)
            pending.append((task, facet_deps, unit, facet if facet is not None and facet_source else None))
            # draft 原图 id → **它自己 facet 切出的图**（v3：不再把依赖挂给同文件
            # 其它图；同文件先后由顺序边保证）。无 facet（v2 兼容）维持全量映射。
            if facet_source and facet_source != "@changes":
                id_map.setdefault(facet_source, []).extend([tid])
        # stable_id 始终映射整 unit；draft 原图 id 只在**无 facet（v2 兼容）**时
        # 映射整 unit（v3 下它们在上面各自只映射自己 facet 的图）。
        mapped_keys = [str(unit.get("stable_id") or "")]
        if not unit_has_facets:
            mapped_keys.extend(source_ids)
        for key in _dedup(mapped_keys):
            id_map.setdefault(key, []).extend(new_ids)
    tasks, dep_errors = _assemble_dependency_dag(tasks, pending, id_map, file_tasks)
    errors.extend(dep_errors)
    return tasks, errors


def _assemble_dependency_dag(
    tasks: list[dict],
    pending: list[tuple[dict, list[str], dict, dict | None]],
    id_map: dict[str, list[str]],
    file_tasks: dict[str, list[str]],
) -> tuple[list[dict], list[dict]]:
    """装配执行 DAG 的边并做三项机械校验，最后 Kahn 拓扑排序输出施工图顺序。

    边的**唯一来源**（不再有人为的"每张图默认依赖前一张"线性链）：
      ① draft 显式 depends_on（原 id 经 id_map 映到编译后编号，挂在该 unit 的全部图上）；
      ② planir 机械推出的 ``depends_on_files``（contract ∪ symbol；二开再 ∪ 现存 import 图）
         —— 整个前置文件 unit 的图全部完成，本 unit 才能施工；
      ③ 同文件多图：后者在前者的成果上继续（同一产物文件，顺序即真实依赖）。

    三项校验（出错只登记、不偷偷改图）：依赖目标必须存在 / 不能自依赖（取证在 planir）/
    不能有环。环与未知依赖都交 Design Gate 回架构师，绝不把 DAG 悄悄重新串成线。
    """
    errors: list[dict] = []
    order_index = {str(t.get("id")): i for i, t in enumerate(tasks)}
    unknown: dict[str, list[str]] = {}
    # 边 → 人话来源（真机 20260928-160609：只报「T-02 → T-03 → T-02」模型根本不知道
    # 哪条边是自己 depends_on 声明的、哪条是编译器加的同文件顺序边，无从下手打断环）。
    edge_src: dict[tuple[str, str], list[str]] = {}

    def _note(src: str, dst: str, why: str) -> None:
        bucket = edge_src.setdefault((src, dst), [])
        if why not in bucket:
            bucket.append(why)

    # 编译后编号 → 模型原图任务 id（编号会重排，给模型看的指引必须能对回它自己的图）。
    # v3：有 facet 的图精确对到**它自己的源图**，不再整 unit 混报。
    source_of: dict[str, list[str]] = {}
    for task, deps, unit, facet in pending:
        path = _norm(unit.get("file"))
        tid = str(task.get("id"))
        if facet is not None and str(facet.get("source") or "") not in ("", "@changes"):
            src_ids = [str(facet.get("source"))]
        else:
            src_ids = [str(x) for x in _dedup(unit.get("source_task_ids"))]
        if src_ids:
            source_of[tid] = src_ids
        edges: set[str] = set()
        for dep in deps:
            mapped = id_map.get(dep)
            if mapped:
                for dst in mapped:
                    _note(
                        tid,
                        dst,
                        f"方案声明（原图任务 {'、'.join(src_ids) or tid} 的 depends_on 含 {dep}）",
                    )
                edges.update(mapped)
            else:
                # 依赖目标不存在：旧实现静默丢弃，dev 于是看不到前置文件、
                # 拓扑排序也把它当"无依赖" —— 方案里的拼写错误（真机：引号污染的 T-06）
                # 直到运行时才炸。显式登记 unknown_dependency。
                unknown.setdefault(dep, [])
                if path not in unknown[dep]:
                    unknown[dep].append(path)
        for dep_file in (unit.get("depends_on_files") or []):
            # 方案外文件（未改动的现存文件等）没有编译节点：无排序意义，忽略。
            mapped_files = file_tasks.get(_norm(dep_file), [])
            for dst in mapped_files:
                _note(tid, dst, f"跨文件引用推出（{_norm(dep_file)}：contracts.uses / 现存 import）")
            edges.update(mapped_files)
        same = file_tasks.get(path, [])
        pos = same.index(tid)
        if pos > 0:
            _note(tid, same[pos - 1], f"同文件顺序边（{path} 内编译器按施工图顺序自动添加）")
            edges.add(same[pos - 1])
        # 合并导致的机械自引用（T-02/T-03 同文件合并后 T-03→T-02 变成自己）在此滤掉：
        # 那不是模型错误，与 planir 取证的 draft 显式自依赖（self_dependency）区分开。
        edges.discard(tid)
        task["depends_on"] = sorted(edges, key=lambda d: order_index.get(d, 1 << 30))
    for dep, declared_by in sorted(unknown.items()):
        errors.append(
            {
                "code": "unknown_dependency",
                "dep": dep,
                "declared_by": declared_by,
                "detail": (
                    f"施工图声明依赖不存在的任务 `{dep}`（声明方：{'、'.join(declared_by)}）"
                    " —— 依赖目标必须存在（id 拼写 / 引号污染 / 引用了被删掉的图？）"
                ),
            }
        )
    ordered, in_cycle = _topo_order(tasks)
    if in_cycle:
        cyc = _find_one_cycle(in_cycle, tasks)
        cyc = cyc or sorted(in_cycle)
        cycle_edges: list[dict[str, Any]] = []
        has_draft_edge = False
        for a, b in zip(cyc, cyc[1:]):
            whys = edge_src.get((a, b), [])
            if not whys:
                whys = ["来源未知（编译器内部）"]
            if any("方案声明" in w for w in whys):
                has_draft_edge = True
            cycle_edges.append({"from": a, "to": b, "sources": whys})
        id_hint = "；".join(
            f"{tid} 对应原图 {'、'.join(sids)}" for tid, sids in sorted(source_of.items())
            if tid in set(in_cycle)
        )
        if has_draft_edge:
            howto = (
                "打断方法：删除或改向环上标为「方案声明」的那条 depends_on —— "
                "被依赖的底层模块（如数据库）不得反向依赖调用方；"
                "「同文件顺序边」由编译器自动添加、不能直接删，需靠重新拆分文件或调整任务排列消除。"
            )
        else:
            howto = (
                "环上**没有**你显式声明的依赖，全部是编译器自动添加的同文件顺序/跨文件引用边："
                "请重新拆分文件边界，或调整任务在方案中的排列顺序，打断同文件图序。"
            )
        detail = (
            "依赖图存在环："
            + " → ".join(cyc)
            + " —— 环上的施工图互相等待，无法决定施工顺序。各条边的来源：\n"
            + "\n".join(f"  · {e['from']} → {e['to']}：{'；'.join(e['sources'])}" for e in cycle_edges)
            + (f"\n编号对照：{id_hint}。" if id_hint else "")
            + "\n"
            + howto
        )
        errors.append(
            {
                "code": "dependency_cycle",
                "tasks": sorted(in_cycle),
                "cycle": cyc,
                "cycle_edges": cycle_edges,
                "source_tasks": {
                    tid: source_of[tid] for tid in sorted(source_of) if tid in set(in_cycle)
                },
                "detail": detail,
            }
        )
    return ordered, errors


def _topo_order(tasks: list[dict]) -> tuple[list[dict], set[str]]:
    """Kahn 拓扑排序（**稳定**：同等就绪度按编译编号序）。

    返回 ``(排序后的施工图, 环上节点集合)``。环存在时不抛异常：环上节点按原序附在尾部
    （best-effort，Design Gate 会据 dependency_cycle 阻断，绝不让流水线死循环）。
    """
    ids = [str(t.get("id")) for t in tasks]
    by_id = dict(zip(ids, tasks))
    deps_of = {
        tid: {str(d) for d in (by_id[tid].get("depends_on") or []) if str(d) in ids}
        for tid in ids
    }
    done: set[str] = set()
    emitted: list[str] = []
    while True:
        ready = [tid for tid in ids if tid not in done and deps_of[tid] <= done]
        if not ready:
            break
        pick = ready[0]  # ids 即编译编号序，取第一个 ⇒ 同输入必得同顺序
        done.add(pick)
        emitted.append(pick)
    leftover = {tid for tid in ids if tid not in done}
    ordered_ids = [*emitted, *(tid for tid in ids if tid in leftover)]
    ordered = [by_id[tid] for tid in ordered_ids]
    return ordered, leftover


def _find_one_cycle(cycle_ids: set[str], tasks: list[dict]) -> list[str]:
    """从环上节点里 DFS 摘出**一条**具体环（首尾同点，便于人工/架构师指认）；摘不到返回 []。"""
    deps = {
        str(t.get("id")): [
            str(d) for d in (t.get("depends_on") or []) if str(d) in cycle_ids
        ]
        for t in tasks
        if str(t.get("id")) in cycle_ids
    }
    color: dict[str, int] = {}
    stack: list[str] = []

    def _dfs(node: str) -> list[str] | None:
        color[node] = 1
        stack.append(node)
        for nxt in deps.get(node, []):
            if color.get(nxt) == 1:
                return stack[stack.index(nxt):] + [nxt]
            if color.get(nxt) != 2:
                hit = _dfs(nxt)
                if hit:
                    return hit
        stack.pop()
        color[node] = 2
        return None

    for start in sorted(cycle_ids):
        if color.get(start) != 2:
            hit = _dfs(start)
            if hit:
                return hit
    return []


def _compile_from_changes(
    plan: Any, *, max_symbols: int
) -> tuple[list[dict], list[dict]]:
    """**兼容路径**：没有 IR 时直接读 `changes[]`（旧调用点与单测走这里）。"""
    plan = plan if isinstance(plan, dict) else {}
    changes = [c for c in (plan.get("changes") or []) if isinstance(c, dict) and c.get("path")]
    if not changes:
        return [], []
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
