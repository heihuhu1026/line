"""Ontology 纯函数校验器（改造规格§十）。

每个函数接受 :class:`ontology.OntologyGraph` 或其 ``to_dict()`` 结果，
返回 ``list[str]`` 形式的问题（空列表 = 通过）。**不做任何裁决 / IO / 写库**。
拦截项覆盖规格§十的十五类硬错误，包括三条机械红线：

* ``self_check`` 来源不允许产生 ``PROVEN``（规格§三十）；
* ``WAIVED / SKIPPED != PROVEN``，必需 PO 未 PROVEN 不得 PASS（规格§七/§四十五）；
* 证据必须绑定**当前** workspace revision，stale evidence 不能证明新工作区（规格§二十二/I）。
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from . import ontology as ont


def _as_graph(data: Any) -> ont.OntologyGraph:
    return data if isinstance(data, ont.OntologyGraph) else ont.OntologyGraph.from_dict(data)


def _endpoint_type(g: ont.OntologyGraph, oid: str) -> str | None:
    """关系端点类型：envelope 对象之外，PO/Evidence/WorkspaceRevision 也是一等端点。"""
    obj = g.objects.get(oid)
    if obj is not None:
        return obj.type
    if oid in g.obligations:
        return ont.TYPE_PROOF_OBLIGATION
    if oid in g.evidence:
        return ont.TYPE_EVIDENCE
    if oid in g.revisions:
        return ont.TYPE_WORKSPACE_REVISION
    return None


def validate_objects(g: Any) -> list[str]:
    """对象类型/真值/来源合法性（含 ASSERTED 不可被 LLM 覆盖、self_check≠PROVEN）。"""
    g = _as_graph(g)
    problems: list[str] = []
    for obj in g.objects.values():
        if not obj.id:
            problems.append("对象缺少 id")
            continue
        if obj.type not in ont.OBJECT_TYPES:
            problems.append(f"{obj.id}：未知对象类型 {obj.type!r}")
        if obj.truth not in ont.TRUTH_LEVELS:
            problems.append(f"{obj.id}：非法 truth {obj.truth!r}")
        sources = {p.source for p in obj.provenance}
        # 红线一：开发/自检自陈永远只能是 DERIVED。
        if obj.truth == ont.TRUTH_PROVEN and ({"dev", "self_check"} & sources) \
                and not (sources - {"dev", "self_check"}):
            problems.append(f"{obj.id}：self_check/dev 来源不得产生 PROVEN（自陈只能是 DERIVED）")
        # ASSERTED 只接受 user/human 来源（规则来源的 PROVEN 也不能伪装成 ASSERTED）。
        if obj.truth == ont.TRUTH_ASSERTED and sources and not (sources & {"user", "human"}):
            problems.append(f"{obj.id}：ASSERTED 对象的 provenance 必须包含 user/human，实际 {sorted(sources)}")
        # 高 revision 重写 ASSERTED 对象时，必须有人工来源，否则就是 LLM 静默覆盖。
        if obj.revision > 1 and obj.truth == ont.TRUTH_ASSERTED and not (sources & {"user", "human"}):
            problems.append(f"{obj.id}：ASSERTED 对象 r{obj.revision} 缺少 user/human provenance（疑似被 LLM 覆盖）")
    return problems


def validate_relations(g: Any) -> list[str]:
    """predicate 白名单、端点存在性、谓词类型一致性。"""
    g = _as_graph(g)
    problems: list[str] = []
    for rel in g.relations:
        if rel.predicate not in ont.PREDICATES:
            problems.append(f"{rel.id}：非法 predicate {rel.predicate!r}（不在白名单，禁止自造）")
            continue
        if rel.truth not in ont.TRUTH_LEVELS:
            problems.append(f"{rel.id}：非法 truth {rel.truth!r}")
        subj_type = _endpoint_type(g, rel.subject)
        obj_type = _endpoint_type(g, rel.object)
        if subj_type is None:
            problems.append(f"{rel.id}：主语不存在（悬空端点）{rel.subject}")
        if obj_type is None:
            problems.append(f"{rel.id}：宾语不存在（悬空端点）{rel.object}")
        rule = ont._PREDICATE_TYPE_RULES.get(rel.predicate)
        if rule and subj_type is not None and subj_type not in rule[ont._SUBJECT_TYPES]:
            problems.append(f"{rel.id}：主语类型 {subj_type} 不适配谓词 {rel.predicate}")
        if rule and obj_type is not None and obj_type not in rule[ont._OBJECT_TYPES]:
            problems.append(f"{rel.id}：宾语类型 {obj_type} 不适配谓词 {rel.predicate}")
    return problems


def validate_evidence(g: Any) -> list[str]:
    """证据三态、执行痕迹、revision 绑定（含 stale evidence）。"""
    g = _as_graph(g)
    problems: list[str] = []
    head = g.head_revision()
    for ev in g.evidence.values():
        if ev.kind not in ont.EVIDENCE_KINDS:
            problems.append(f"{ev.id}：未知证据 kind {ev.kind!r}")
            continue
        # 人工确认是 ASSERTED，不是 PROVEN。
        if ev.kind in ont.EVIDENCE_ASSERTED_KINDS and ev.truth == ont.TRUTH_PROVEN:
            problems.append(f"{ev.id}：{ev.kind} 是人工事实（ASSERTED），不允许标 PROVEN")
        if ev.status == ont.PO_STATUS_PROVEN and ev.truth != ont.TRUTH_PROVEN:
            problems.append(f"{ev.id}：证据状态 PROVEN 但 truth={ev.truth}（机械证据必须 truth=PROVEN）")
        if ev.truth == ont.TRUTH_PROVEN:
            if ev.kind in ont.EVIDENCE_EXEC_KINDS:
                if not ev.command or ev.exit_code is None:
                    problems.append(f"{ev.id}：{ev.kind} 标 PROVEN 但缺少执行痕迹（command + exit_code）")
            elif ev.kind in ont.EVIDENCE_CHECKER_KINDS:
                if not ev.source:
                    problems.append(f"{ev.id}：{ev.kind} 标 PROVEN 但缺少机械检查器标识 source")
            elif ev.kind not in ont.EVIDENCE_ASSERTED_KINDS:
                problems.append(f"{ev.id}：{ev.kind} 不允许标 PROVEN")
        # 证据必须绑定真实存在的工作区 revision。
        if ev.workspace_revision and ev.workspace_revision not in g.revisions:
            problems.append(f"{ev.id}：绑定的 workspace_revision 不存在 {ev.workspace_revision}")
        # stale evidence：证明当前 PASS 用的证据绑在旧 revision 上（规格 I）。
        if (head and ev.workspace_revision and ev.workspace_revision != head
                and ev.status == ont.PO_STATUS_PROVEN):
            problems.append(
                f"{ev.id}：stale evidence，绑定旧 revision {ev.workspace_revision}，当前 head={head}"
            )
        for eid in ev.proof_obligation_ids:
            if eid not in g.obligations:
                problems.append(f"{ev.id}：引用不存在的 proof obligation {eid}")
    return problems


def validate_obligations(g: Any) -> list[str]:
    """PO kind/status/verifier/证据闭环；Task 必须 implements Requirement（场景 B）。"""
    g = _as_graph(g)
    problems: list[str] = []
    req_ids = {o.id for o in g.objects.values() if o.type == ont.TYPE_REQUIREMENT}
    for po in g.obligations.values():
        if po.kind not in ont.PROOF_KINDS:
            problems.append(f"{po.id}：未知 PO kind {po.kind!r}")
        if po.status not in ont.PROOF_STATUSES:
            problems.append(f"{po.id}：未知 PO status {po.status!r}")
        if po.required and not po.verifier:
            problems.append(f"{po.id}：required ProofObligation 没有可执行 verifier（无法机械兑现）")
        if po.requirement_id and po.requirement_id not in req_ids:
            problems.append(f"{po.id}：requirement_id 悬空 {po.requirement_id}")
        if po.status == ont.PO_STATUS_PROVEN:
            if not po.evidence_ids:
                problems.append(f"{po.id}：PO 标 PROVEN 却没有绑定 evidence（PROVEN 只能由机械证据产生）")
            for eid in po.evidence_ids:
                ev = g.evidence.get(eid)
                if ev is None:
                    problems.append(f"{po.id}：绑定的 evidence 不存在 {eid}")
                elif ev.status != ont.PO_STATUS_PROVEN:
                    problems.append(f"{po.id}：由 {eid} 证明，但该证据状态为 {ev.status}（不是 PROVEN）")
    # 场景 B：Task 不能没有 Requirement 锚点。
    implements_by_task: dict[str, set[str]] = {}
    for rel in g.relations:
        if rel.predicate == "implements":
            implements_by_task.setdefault(rel.subject, set()).add(rel.object)
    for obj in g.objects.values():
        if obj.type == ont.TYPE_TASK:
            reqs = implements_by_task.get(obj.id) or set()
            if not (reqs & req_ids):
                problems.append(f"{obj.id}：Task 没有 implements 任何 Requirement（语义断锚，禁止裸任务）")
    return problems


def validate_tasks(g: Any, *, changes: Iterable = (), file_symbols: dict[str, set[str]] | None = None) -> list[str]:
    """任务归属一致性：依赖环 / targets 不在改动面 / owns 符号不属于目标文件 / 越权 patch。

    约定（都在对象 payload 中，纯数据）：

    * Task.payload = ``{"target_files": [...], "symbols": [...]}``
    * Patch.payload = ``{"path": "a.py", "task_id": "stask:...", "change_type": "add"}``
    * Symbol 对象 payload = ``{"file": "a.py", "name": "foo"}``
    """
    g = _as_graph(g)
    problems: list[str] = []
    tasks = {o.id: o for o in g.objects.values() if o.type == ont.TYPE_TASK}
    change_set = {str(p).replace("\\", "/") for p in (changes or ())}

    # 依赖环（depends_on）。
    deps: dict[str, list[str]] = {tid: [] for tid in tasks}
    for rel in g.relations:
        if rel.predicate == "depends_on" and rel.subject in tasks:
            if rel.object not in tasks:
                problems.append(f"{rel.id}：depends_on 指向不存在的 Task {rel.object}")
            else:
                deps[rel.subject].append(rel.object)
    visiting: set[str] = set()
    done: set[str] = set()

    def _walk(tid: str, stack: list[str]) -> None:
        if tid in done:
            return
        if tid in visiting:
            cycle = " -> ".join(stack[stack.index(tid):] + [tid]) if tid in stack else tid
            problems.append(f"任务依赖存在环：{cycle}")
            return
        visiting.add(tid)
        stack.append(tid)
        for nxt in deps.get(tid, []):
            _walk(nxt, stack)
        stack.pop()
        visiting.discard(tid)
        done.add(tid)

    for tid in sorted(tasks):
        _walk(tid, [])

    # Task -> owns Symbol -> belongs to File。
    task_files: dict[str, set[str]] = {}
    for tid, obj in tasks.items():
        tfiles = {str(p).replace("\\", "/") for p in (obj.payload.get("target_files") or [])}
        task_files[tid] = tfiles
        if change_set and not tfiles:
            problems.append(f"{tid}：Task 没有 target_files")
        outside = sorted(p for p in tfiles if change_set and p not in change_set)
        if outside:
            problems.append(f"{tid}：target_files 不在本次改动面（疑似越权）：{outside[:3]}")

    symbols = {o.id: o for o in g.objects.values() if o.type == ont.TYPE_SYMBOL}
    for rel in g.relations:
        if rel.predicate == "owns":
            task = tasks.get(rel.subject)
            sym = symbols.get(rel.object)
            if task is None or sym is None:
                continue
            sfile = str(sym.payload.get("file") or "").replace("\\", "/")
            tfiles = task_files.get(task.id) or set()
            if sfile and tfiles and sfile not in tfiles:
                problems.append(f"{task.id}：owns 符号 {sym.payload.get('name') or rel.object} 不属于其目标文件 {sfile}")
            known = (file_symbols or {}).get(sfile)
            if known is not None and sym.payload.get("name") and sym.payload["name"] not in known:
                problems.append(f"{task.id}：owns 符号 {sym.payload['name']} 在 {sfile} 中不存在（未物化/臆造）")

    # 非 owner patch（规格§十七：补丁必须落在 owner 任务的文件上）。
    for obj in g.objects.values():
        if obj.type != ont.TYPE_PATCH:
            continue
        path = str(obj.payload.get("path") or "").replace("\\", "/")
        owner = str(obj.payload.get("task_id") or "")
        if path and owner and owner in task_files and path not in (task_files[owner] or set()):
            problems.append(f"{obj.id}：补丁写 {path}，但 owner {owner} 的 target_files 不含该文件（越权写入）")
    return problems


def validate_revisions(g: Any) -> list[str]:
    """工作区修订链：revision 存在、parent 成链、materialized_in 不悬空。"""
    g = _as_graph(g)
    problems: list[str] = []
    for rev in g.revisions.values():
        if not rev.workspace_id:
            problems.append(f"{rev.revision_id}：WorkspaceRevision 缺少 workspace_id")
        if rev.parent_revision and rev.parent_revision not in g.revisions:
            problems.append(f"{rev.revision_id}：parent_revision 悬空 {rev.parent_revision}")
    # parent 环。
    for rid, rev in g.revisions.items():
        seen: set[str] = set()
        cur = rid
        while cur:
            if cur in seen:
                problems.append(f"workspace revision 链存在环（从 {rid} 起）")
                break
            seen.add(cur)
            parent = g.revisions[cur].parent_revision
            cur = parent if parent in g.revisions else ""
    for rel in g.relations:
        if rel.predicate == "materialized_in" and rel.object not in g.revisions:
            problems.append(f"{rel.id}：materialized_in 指向不存在的 WorkspaceRevision {rel.object}")
        if rel.predicate == "has_revision" and rel.object not in g.revisions:
            problems.append(f"{rel.id}：has_revision 指向不存在的 WorkspaceRevision {rel.object}")
    return problems


def validate_invariants(g: Any) -> list[str]:
    """Invariant 可执行绑定（规格§九）：声称机械兑现的不变量必须绑到登记过的检查。

    纯人话不变量（``check.type != mechanical``）不拦 —— 它没有承诺可机械兑现；
    但一旦声明机械检查，check_id 就必须在内置可执行检查表里（``known=True``），
    且 entry 非空。拦截「写个假 check_id 假装能自动验证」。
    """
    g = _as_graph(g)
    problems: list[str] = []
    for obj in g.objects.values():
        if obj.type != ont.TYPE_INVARIANT:
            continue
        check = obj.payload.get("check") if isinstance(obj.payload, dict) else None
        if not isinstance(check, dict) or str(check.get("type") or "") != "mechanical":
            continue
        check_id = str(check.get("check_id") or "").strip()
        binding = ont.bound_invariant_check(obj)
        if not check_id:
            problems.append(f"{obj.id}：Invariant 声明了 mechanical check 却没有 check_id")
        elif not binding.get("known"):
            problems.append(
                f"{obj.id}：Invariant 绑定的机械检查 {check_id!r} 不在可执行检查登记表"
                "（must_not_break 不能只写 check_id，必须能真跑）"
            )
        elif not str(check.get("entry") or "").strip():
            problems.append(f"{obj.id}：Invariant 的机械检查 {check_id} 缺少 entry（无法执行）")
    return problems


def validate_release(g: Any) -> list[str]:
    """Decision=pass 前的最终闸门：所有 required PO 必须 PROVEN（场景 F）。"""
    g = _as_graph(g)
    problems: list[str] = []
    required = [po for po in g.obligations.values() if po.required]
    for obj in g.objects.values():
        if obj.type != ont.TYPE_DECISION:
            continue
        if str(obj.payload.get("verdict") or obj.status or "") != "pass":
            continue
        unproven = [po.id for po in required if po.status != ont.PO_STATUS_PROVEN]
        if unproven:
            problems.append(
                f"{obj.id}：Decision=pass 但仍有 {len(unproven)} 个 required PO 未 PROVEN：{unproven[:5]}"
            )
    return problems


def validate_all(g: Any, *, changes: Iterable = (), file_symbols: dict[str, set[str]] | None = None) -> dict[str, list[str]]:
    """跑全部校验，返回 ``{check: problems}``；任意列表非空即拦截。"""
    g = _as_graph(g)
    return {
        "objects": validate_objects(g),
        "relations": validate_relations(g),
        "evidence": validate_evidence(g),
        "obligations": validate_obligations(g),
        "invariants": validate_invariants(g),
        "tasks": validate_tasks(g, changes=changes, file_symbols=file_symbols),
        "revisions": validate_revisions(g),
        "release": validate_release(g),
    }


def has_blocking(problems_by_check: dict[str, list[str]]) -> bool:
    """聚合结果中是否存在任何拦截项。"""
    return any(bool(items) for items in problems_by_check.values())
