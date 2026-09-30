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
from dataclasses import asdict, dataclass
from typing import Any

from . import ontology as ont

#: 问题严重度（方案§十四）：error 必须硬阻断 Proof Gate；warning 只留痕。
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class Problem:
    """结构化校验问题（方案§十四）：不再只返回字符串。

    ``source`` 为产出该问题的校验器名；``object_id``/``relation_id`` 在可定位时必填，
    无法机械归属时留空（禁止猜）。
    """

    code: str
    severity: str
    message: str
    source: str
    object_id: str = ""
    relation_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _err(source: str, code: str, message: str, *,
         object_id: str = "", relation_id: str = "") -> Problem:
    return Problem(code=code, severity=SEVERITY_ERROR, message=message, source=source,
                   object_id=object_id, relation_id=relation_id)


def _warn(source: str, code: str, message: str, *,
          object_id: str = "", relation_id: str = "") -> Problem:
    return Problem(code=code, severity=SEVERITY_WARNING, message=message, source=source,
                   object_id=object_id, relation_id=relation_id)


def _messages(problems: Iterable[Problem]) -> list[str]:
    """结构化问题 → 旧版字符串 API（兼容 smoke / state 旧消费者）。"""
    return [p.message for p in problems]


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


def validate_objects_structured(g: Any) -> list[Problem]:
    """对象类型/真值/来源合法性（含 ASSERTED 不可被 LLM 覆盖、self_check≠PROVEN）。"""
    g = _as_graph(g)
    problems: list[Problem] = []
    src = "objects"
    for obj in g.objects.values():
        if not obj.id:
            problems.append(_err(src, "object_missing_id", "对象缺少 id"))
            continue
        if obj.type not in ont.OBJECT_TYPES:
            problems.append(_err(src, "object_unknown_type",
                                 f"{obj.id}：未知对象类型 {obj.type!r}", object_id=obj.id))
        if obj.truth not in ont.TRUTH_LEVELS:
            problems.append(_err(src, "object_invalid_truth",
                                 f"{obj.id}：非法 truth {obj.truth!r}", object_id=obj.id))
        sources = {p.source for p in obj.provenance}
        # 红线一：开发/自检自陈永远只能是 DERIVED。
        if obj.truth == ont.TRUTH_PROVEN and ({"dev", "self_check"} & sources) \
                and not (sources - {"dev", "self_check"}):
            problems.append(_err(
                src, "selfcheck_proven",
                f"{obj.id}：self_check/dev 来源不得产生 PROVEN（自陈只能是 DERIVED）",
                object_id=obj.id))
        # ASSERTED 只接受 user/human 来源（规则来源的 PROVEN 也不能伪装成 ASSERTED）。
        if obj.truth == ont.TRUTH_ASSERTED and sources and not (sources & {"user", "human"}):
            problems.append(_err(
                src, "asserted_without_human",
                f"{obj.id}：ASSERTED 对象的 provenance 必须包含 user/human，实际 {sorted(sources)}",
                object_id=obj.id))
        # 高 revision 重写 ASSERTED 对象时，必须有人工来源，否则就是 LLM 静默覆盖。
        if obj.revision > 1 and obj.truth == ont.TRUTH_ASSERTED and not (sources & {"user", "human"}):
            problems.append(_err(
                src, "asserted_overwritten",
                f"{obj.id}：ASSERTED 对象 r{obj.revision} 缺少 user/human provenance（疑似被 LLM 覆盖）",
                object_id=obj.id))
    return problems


def validate_objects(g: Any) -> list[str]:
    """旧版字符串 API（等价 :func:`validate_objects_structured`）。"""
    return _messages(validate_objects_structured(g))


def validate_relations_structured(g: Any) -> list[Problem]:
    """predicate 白名单、端点存在性、谓词类型一致性。"""
    g = _as_graph(g)
    problems: list[Problem] = []
    src = "relations"
    for rel in g.relations:
        if rel.predicate not in ont.PREDICATES:
            problems.append(_err(src, "illegal_predicate",
                                 f"{rel.id}：非法 predicate {rel.predicate!r}（不在白名单，禁止自造）",
                                 relation_id=rel.id))
            continue
        if rel.truth not in ont.TRUTH_LEVELS:
            problems.append(_err(src, "relation_invalid_truth",
                                 f"{rel.id}：非法 truth {rel.truth!r}", relation_id=rel.id))
        subj_type = _endpoint_type(g, rel.subject)
        obj_type = _endpoint_type(g, rel.object)
        if subj_type is None:
            problems.append(_err(src, "dangling_endpoint",
                                 f"{rel.id}：主语不存在（悬空端点）{rel.subject}",
                                 object_id=rel.subject, relation_id=rel.id))
        if obj_type is None:
            problems.append(_err(src, "dangling_endpoint",
                                 f"{rel.id}：宾语不存在（悬空端点）{rel.object}",
                                 object_id=rel.object, relation_id=rel.id))
        rule = ont._PREDICATE_TYPE_RULES.get(rel.predicate)
        if rule and subj_type is not None and subj_type not in rule[ont._SUBJECT_TYPES]:
            problems.append(_err(src, "subject_type_mismatch",
                                 f"{rel.id}：主语类型 {subj_type} 不适配谓词 {rel.predicate}",
                                 object_id=rel.subject, relation_id=rel.id))
        if rule and obj_type is not None and obj_type not in rule[ont._OBJECT_TYPES]:
            problems.append(_err(src, "object_type_mismatch",
                                 f"{rel.id}：宾语类型 {obj_type} 不适配谓词 {rel.predicate}",
                                 object_id=rel.object, relation_id=rel.id))
    return problems


def validate_relations(g: Any) -> list[str]:
    """旧版字符串 API（等价 :func:`validate_relations_structured`）。"""
    return _messages(validate_relations_structured(g))


def validate_evidence_structured(g: Any) -> list[Problem]:
    """证据三态、执行痕迹、revision 绑定（含 stale evidence）。"""
    g = _as_graph(g)
    problems: list[Problem] = []
    src = "evidence"
    head = g.head_revision()
    for ev in g.evidence.values():
        if ev.kind not in ont.EVIDENCE_KINDS:
            problems.append(_err(src, "evidence_unknown_kind",
                                 f"{ev.id}：未知证据 kind {ev.kind!r}", object_id=ev.id))
            continue
        # 人工确认是 ASSERTED，不是 PROVEN。
        if ev.kind in ont.EVIDENCE_ASSERTED_KINDS and ev.truth == ont.TRUTH_PROVEN:
            problems.append(_err(src, "asserted_kind_proven",
                                 f"{ev.id}：{ev.kind} 是人工事实（ASSERTED），不允许标 PROVEN",
                                 object_id=ev.id))
        if ev.status == ont.PO_STATUS_PROVEN and ev.truth != ont.TRUTH_PROVEN:
            problems.append(_err(src, "evidence_status_truth_mismatch",
                                 f"{ev.id}：证据状态 PROVEN 但 truth={ev.truth}（机械证据必须 truth=PROVEN）",
                                 object_id=ev.id))
        if ev.truth == ont.TRUTH_PROVEN:
            if ev.kind in ont.EVIDENCE_EXEC_KINDS:
                if not ev.command or ev.exit_code is None:
                    problems.append(_err(src, "proven_without_execution",
                                         f"{ev.id}：{ev.kind} 标 PROVEN 但缺少执行痕迹（command + exit_code）",
                                         object_id=ev.id))
            elif ev.kind in ont.EVIDENCE_CHECKER_KINDS:
                if not ev.source:
                    problems.append(_err(src, "proven_without_source",
                                         f"{ev.id}：{ev.kind} 标 PROVEN 但缺少机械检查器标识 source",
                                         object_id=ev.id))
            elif ev.kind not in ont.EVIDENCE_ASSERTED_KINDS:
                problems.append(_err(src, "proven_kind_illegal",
                                     f"{ev.id}：{ev.kind} 不允许标 PROVEN", object_id=ev.id))
        # 证据必须绑定真实存在的工作区 revision。
        if ev.workspace_revision and ev.workspace_revision not in g.revisions:
            problems.append(_err(src, "evidence_revision_missing",
                                 f"{ev.id}：绑定的 workspace_revision 不存在 {ev.workspace_revision}",
                                 object_id=ev.id))
        # stale evidence：证明当前 PASS 用的证据绑在旧 revision 上（规格 I）。
        if (head and ev.workspace_revision and ev.workspace_revision != head
                and ev.status == ont.PO_STATUS_PROVEN):
            problems.append(_err(
                src, "stale_evidence",
                f"{ev.id}：stale evidence，绑定旧 revision {ev.workspace_revision}，当前 head={head}",
                object_id=ev.id))
        for eid in ev.proof_obligation_ids:
            if eid not in g.obligations:
                problems.append(_err(src, "evidence_po_missing",
                                     f"{ev.id}：引用不存在的 proof obligation {eid}",
                                     object_id=ev.id))
    return problems


def validate_evidence(g: Any) -> list[str]:
    """旧版字符串 API（等价 :func:`validate_evidence_structured`）。"""
    return _messages(validate_evidence_structured(g))


def validate_obligations_structured(g: Any) -> list[Problem]:
    """PO kind/status/verifier/证据闭环；Task 必须 implements Requirement（场景 B）。"""
    g = _as_graph(g)
    problems: list[Problem] = []
    src = "obligations"
    req_ids = {o.id for o in g.objects.values() if o.type == ont.TYPE_REQUIREMENT}
    for po in g.obligations.values():
        if po.kind not in ont.PROOF_KINDS:
            problems.append(_err(src, "po_unknown_kind",
                                 f"{po.id}：未知 PO kind {po.kind!r}", object_id=po.id))
        if po.status not in ont.PROOF_STATUSES:
            problems.append(_err(src, "po_unknown_status",
                                 f"{po.id}：未知 PO status {po.status!r}", object_id=po.id))
        if po.required and not po.verifier:
            problems.append(_err(src, "required_without_verifier",
                                 f"{po.id}：required ProofObligation 没有可执行 verifier（无法机械兑现）",
                                 object_id=po.id))
        if po.requirement_id and po.requirement_id not in req_ids:
            problems.append(_err(src, "po_requirement_dangling",
                                 f"{po.id}：requirement_id 悬空 {po.requirement_id}", object_id=po.id))
        if po.status == ont.PO_STATUS_PROVEN:
            if not po.evidence_ids:
                problems.append(_err(src, "proven_without_evidence",
                                     f"{po.id}：PO 标 PROVEN 却没有绑定 evidence（PROVEN 只能由机械证据产生）",
                                     object_id=po.id))
            for eid in po.evidence_ids:
                ev = g.evidence.get(eid)
                if ev is None:
                    problems.append(_err(src, "po_evidence_missing",
                                         f"{po.id}：绑定的 evidence 不存在 {eid}", object_id=po.id))
                elif ev.status != ont.PO_STATUS_PROVEN:
                    problems.append(_err(src, "po_evidence_not_proven",
                                         f"{po.id}：由 {eid} 证明，但该证据状态为 {ev.status}（不是 PROVEN）",
                                         object_id=po.id))
    # 场景 B：Task 不能没有 Requirement 锚点。
    implements_by_task: dict[str, set[str]] = {}
    for rel in g.relations:
        if rel.predicate == "implements":
            implements_by_task.setdefault(rel.subject, set()).add(rel.object)
    for obj in g.objects.values():
        if obj.type == ont.TYPE_TASK:
            reqs = implements_by_task.get(obj.id) or set()
            if not (reqs & req_ids):
                problems.append(_err(src, "task_without_requirement",
                                     f"{obj.id}：Task 没有 implements 任何 Requirement（语义断锚，禁止裸任务）",
                                     object_id=obj.id))
    return problems


def validate_obligations(g: Any) -> list[str]:
    """旧版字符串 API（等价 :func:`validate_obligations_structured`）。"""
    return _messages(validate_obligations_structured(g))


def validate_tasks_structured(
    g: Any, *, changes: Iterable = (), file_symbols: dict[str, set[str]] | None = None
) -> list[Problem]:
    """任务归属一致性：依赖环 / targets 不在改动面 / owns 符号不属于目标文件 / 越权 patch。

    约定（都在对象 payload 中，纯数据）：

    * Task.payload = ``{"target_files": [...], "symbols": [...]}``
    * Patch.payload = ``{"path": "a.py", "task_id": "stask:...", "change_type": "add"}``
    * Symbol 对象 payload = ``{"file": "a.py", "name": "foo"}``
    """
    g = _as_graph(g)
    problems: list[Problem] = []
    src = "tasks"
    tasks = {o.id: o for o in g.objects.values() if o.type == ont.TYPE_TASK}
    change_set = {str(p).replace("\\", "/") for p in (changes or ())}

    # 依赖环（depends_on）。
    deps: dict[str, list[str]] = {tid: [] for tid in tasks}
    for rel in g.relations:
        if rel.predicate == "depends_on" and rel.subject in tasks:
            if rel.object not in tasks:
                problems.append(_err(src, "dependency_missing_task",
                                     f"{rel.id}：depends_on 指向不存在的 Task {rel.object}",
                                     object_id=rel.subject, relation_id=rel.id))
            else:
                deps[rel.subject].append(rel.object)
    visiting: set[str] = set()
    done: set[str] = set()

    def _walk(tid: str, stack: list[str]) -> None:
        if tid in done:
            return
        if tid in visiting:
            cycle = " -> ".join(stack[stack.index(tid):] + [tid]) if tid in stack else tid
            problems.append(_err(src, "dependency_cycle",
                                 f"任务依赖存在环：{cycle}", object_id=tid))
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
            problems.append(_err(src, "task_without_target_files",
                                 f"{tid}：Task 没有 target_files", object_id=tid))
        outside = sorted(p for p in tfiles if change_set and p not in change_set)
        if outside:
            problems.append(_err(src, "task_outside_changes",
                                 f"{tid}：target_files 不在本次改动面（疑似越权）：{outside[:3]}",
                                 object_id=tid))

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
                problems.append(_err(
                    src, "owns_symbol_wrong_file",
                    f"{task.id}：owns 符号 {sym.payload.get('name') or rel.object} 不属于其目标文件 {sfile}",
                    object_id=task.id, relation_id=rel.id))
            known = (file_symbols or {}).get(sfile)
            if known is not None and sym.payload.get("name") and sym.payload["name"] not in known:
                problems.append(_err(
                    src, "owns_symbol_not_materialized",
                    f"{task.id}：owns 符号 {sym.payload['name']} 在 {sfile} 中不存在（未物化/臆造）",
                    object_id=task.id, relation_id=rel.id))

    # 非 owner patch（规格§十七：补丁必须落在 owner 任务的文件上）。
    for obj in g.objects.values():
        if obj.type != ont.TYPE_PATCH:
            continue
        path = str(obj.payload.get("path") or "").replace("\\", "/")
        owner = str(obj.payload.get("task_id") or "")
        if path and owner and owner in task_files and path not in (task_files[owner] or set()):
            problems.append(_err(
                src, "patch_outside_owner",
                f"{obj.id}：补丁写 {path}，但 owner {owner} 的 target_files 不含该文件（越权写入）",
                object_id=obj.id))
    return problems


def validate_tasks(g: Any, *, changes: Iterable = (), file_symbols: dict[str, set[str]] | None = None) -> list[str]:
    """旧版字符串 API（等价 :func:`validate_tasks_structured`）。"""
    return _messages(validate_tasks_structured(g, changes=changes, file_symbols=file_symbols))


def validate_revisions_structured(g: Any) -> list[Problem]:
    """工作区修订链：revision 存在、parent 成链、materialized_in 不悬空。"""
    g = _as_graph(g)
    problems: list[Problem] = []
    src = "revisions"
    for rev in g.revisions.values():
        if not rev.workspace_id:
            problems.append(_err(src, "revision_without_workspace",
                                 f"{rev.revision_id}：WorkspaceRevision 缺少 workspace_id",
                                 object_id=rev.revision_id))
        if rev.parent_revision and rev.parent_revision not in g.revisions:
            problems.append(_err(src, "revision_parent_dangling",
                                 f"{rev.revision_id}：parent_revision 悬空 {rev.parent_revision}",
                                 object_id=rev.revision_id))
    # parent 环。
    for rid, rev in g.revisions.items():
        seen: set[str] = set()
        cur = rid
        while cur:
            if cur in seen:
                problems.append(_err(src, "revision_cycle",
                                     f"workspace revision 链存在环（从 {rid} 起）", object_id=rid))
                break
            seen.add(cur)
            parent = g.revisions[cur].parent_revision
            cur = parent if parent in g.revisions else ""
    for rel in g.relations:
        if rel.predicate == "materialized_in" and rel.object not in g.revisions:
            problems.append(_err(src, "materialized_in_dangling",
                                 f"{rel.id}：materialized_in 指向不存在的 WorkspaceRevision {rel.object}",
                                 object_id=rel.subject, relation_id=rel.id))
        if rel.predicate == "has_revision" and rel.object not in g.revisions:
            problems.append(_err(src, "has_revision_dangling",
                                 f"{rel.id}：has_revision 指向不存在的 WorkspaceRevision {rel.object}",
                                 relation_id=rel.id))
    return problems


def validate_revisions(g: Any) -> list[str]:
    """旧版字符串 API（等价 :func:`validate_revisions_structured`）。"""
    return _messages(validate_revisions_structured(g))


def validate_invariants_structured(g: Any) -> list[Problem]:
    """Invariant 可执行绑定（规格§九）：声称机械兑现的不变量必须绑到登记过的检查。

    纯人话不变量（``check.type != mechanical``）不拦 —— 它没有承诺可机械兑现；
    但一旦声明机械检查，check_id 就必须在内置可执行检查表里（``known=True``），
    且 entry 非空。拦截「写个假 check_id 假装能自动验证」。
    """
    g = _as_graph(g)
    problems: list[Problem] = []
    src = "invariants"
    for obj in g.objects.values():
        if obj.type != ont.TYPE_INVARIANT:
            continue
        check = obj.payload.get("check") if isinstance(obj.payload, dict) else None
        if not isinstance(check, dict) or str(check.get("type") or "") != "mechanical":
            continue
        check_id = str(check.get("check_id") or "").strip()
        binding = ont.bound_invariant_check(obj)
        if not check_id:
            problems.append(_err(src, "mechanical_check_without_id",
                                 f"{obj.id}：Invariant 声明了 mechanical check 却没有 check_id",
                                 object_id=obj.id))
        elif not binding.get("known"):
            problems.append(_err(
                src, "mechanical_check_unknown",
                f"{obj.id}：Invariant 绑定的机械检查 {check_id!r} 不在可执行检查登记表"
                "（must_not_break 不能只写 check_id，必须能真跑）",
                object_id=obj.id))
        elif not str(check.get("entry") or "").strip():
            problems.append(_err(src, "mechanical_check_without_entry",
                                 f"{obj.id}：Invariant 的机械检查 {check_id} 缺少 entry（无法执行）",
                                 object_id=obj.id))
    return problems


def validate_invariants(g: Any) -> list[str]:
    """旧版字符串 API（等价 :func:`validate_invariants_structured`）。"""
    return _messages(validate_invariants_structured(g))


def validate_contradictions_structured(g: Any) -> list[Problem]:
    """方案§十三.9/.10：Interface Freeze 矛盾 与 同级 Claim 矛盾（都必须硬阻断）。

    * **freeze 矛盾**：同一 ``interface_freeze`` PO 绑定的双路证据（contract /
      skeleton_conformance）一路 PROVEN 一路 FAILED —— 两个机械检查器对同一冻结符号
      给出对立结论，不能挑一个信。
    * **Claim 矛盾**：两个同事实等级（同 truth 且同来源优先级）的 Claim 对同一
      ``payload.subject`` 极性对立，且没有明确人工裁决（含 user/human 来源的
      Decision，其 ``payload.adjudicated_subjects`` 列出已裁决主体）。
      没有 ``subject/polarity`` 载荷的 Claim 不参与比较（无共同事实主体不可比较）。
    """
    g = _as_graph(g)
    problems: list[Problem] = []
    src = "contradictions"
    # 9. interface freeze 双路证据对立。
    channel_kind = {"contract": "contract", "skeleton_conformance": "skeleton_conformance"}
    for po in g.obligations.values():
        if po.kind != ont.PO_KIND_INTERFACE_FREEZE:
            continue
        channel_status: dict[str, str] = {}
        for eid in po.evidence_ids:
            ev = g.evidence.get(eid)
            if ev is not None and ev.kind in channel_kind:
                channel_status[channel_kind[ev.kind]] = ev.status
        statuses = set(channel_status.values())
        if ont.PO_STATUS_PROVEN in statuses and ont.PO_STATUS_FAILED in statuses:
            problems.append(_err(
                src, "interface_freeze_contradiction",
                f"{po.id}：Interface Freeze 双路机械证据对立"
                f"（contract={channel_status.get('contract', '缺席')} / "
                f"skeleton={channel_status.get('skeleton_conformance', '缺席')}），禁止放行",
                object_id=po.id))
    # 10. 同级 Claim 矛盾（复用 ontology.reconcile_claims 的确定性仲裁）。
    claim_rows: list[dict[str, Any]] = []
    for obj in g.objects.values():
        if obj.type != ont.TYPE_CLAIM:
            continue
        payload = obj.payload if isinstance(obj.payload, dict) else {}
        subject = str(payload.get("subject") or "")
        if not subject or "polarity" not in payload:
            continue
        source = next((p.source for p in obj.provenance if p.source), "")
        claim_rows.append({
            "id": obj.id, "subject": subject,
            "polarity": payload.get("polarity"),
            "source": source, "truth": obj.truth,
        })
    if claim_rows:
        adjudicated: set[str] = set()
        for obj in g.objects.values():
            if obj.type != ont.TYPE_DECISION:
                continue
            human = {p.source for p in obj.provenance} & {"user", "human"}
            payload = obj.payload if isinstance(obj.payload, dict) else {}
            if human:
                adjudicated.update(str(s) for s in (payload.get("adjudicated_subjects") or []))
        for item in ont.reconcile_claims(claim_rows).get("contradictions") or []:
            subject = str(item.get("subject") or "")
            if subject in adjudicated:
                continue  # 已有明确人工裁决（方案§十三.10 例外条款）
            ids = sorted(str(x) for x in (item.get("ids") or []))
            problems.append(_err(
                src, "claim_contradiction",
                f"同级 Claim 在主体 {subject!r} 上对立且无人工裁决：{ids}",
                object_id=ids[0] if ids else ""))
    return problems


def validate_contradictions(g: Any) -> list[str]:
    """旧版字符串 API（等价 :func:`validate_contradictions_structured`）。"""
    return _messages(validate_contradictions_structured(g))


def validate_release_structured(g: Any) -> list[Problem]:
    """Decision=pass 前的最终闸门：所有 required PO 必须 PROVEN（场景 F）。"""
    g = _as_graph(g)
    problems: list[Problem] = []
    src = "release"
    required = [po for po in g.obligations.values() if po.required]
    for obj in g.objects.values():
        if obj.type != ont.TYPE_DECISION:
            continue
        if str(obj.payload.get("verdict") or obj.status or "") != "pass":
            continue
        unproven = [po.id for po in required if po.status != ont.PO_STATUS_PROVEN]
        if unproven:
            problems.append(_err(
                src, "decision_pass_unproven",
                f"{obj.id}：Decision=pass 但仍有 {len(unproven)} 个 required PO 未 PROVEN：{unproven[:5]}",
                object_id=obj.id))
    return problems


def validate_release(g: Any) -> list[str]:
    """旧版字符串 API（等价 :func:`validate_release_structured`）。"""
    return _messages(validate_release_structured(g))


def validate_all_structured(
    g: Any, *, changes: Iterable = (), file_symbols: dict[str, set[str]] | None = None
) -> dict[str, list[Problem]]:
    """跑全部校验，返回 ``{check: [Problem]}``（方案§十四 结构化结果）。

    比旧版多一组 ``contradictions``（freeze / 同级 Claim 矛盾）。
    """
    g = _as_graph(g)
    return {
        "objects": validate_objects_structured(g),
        "relations": validate_relations_structured(g),
        "evidence": validate_evidence_structured(g),
        "obligations": validate_obligations_structured(g),
        "invariants": validate_invariants_structured(g),
        "tasks": validate_tasks_structured(g, changes=changes, file_symbols=file_symbols),
        "revisions": validate_revisions_structured(g),
        "contradictions": validate_contradictions_structured(g),
        "release": validate_release_structured(g),
    }


def validate_all(g: Any, *, changes: Iterable = (), file_symbols: dict[str, set[str]] | None = None) -> dict[str, list[str]]:
    """跑全部校验，返回 ``{check: problems}``；任意列表非空即拦截（旧版字符串 API）。"""
    structured = validate_all_structured(g, changes=changes, file_symbols=file_symbols)
    return {check: _messages(items) for check, items in structured.items()}


def blocking_errors(problems_by_check: dict[str, list[Problem]]) -> list[Problem]:
    """拍平所有 severity=error 的问题（Proof Gate 只认 error；warning 留痕不拦）。"""
    out: list[Problem] = []
    for items in problems_by_check.values():
        out.extend(p for p in items if p.severity == SEVERITY_ERROR)
    return out


def has_blocking(problems_by_check: dict[str, list[Any]]) -> bool:
    """聚合结果中是否存在任何拦截项（同时兼容字符串与 Problem 列表）。"""
    return any(bool(items) for items in problems_by_check.values())


def semantic_integrity_audit(g: Any, state: Any = None) -> list[Problem]:
    """方案§三十五：跨对象关键关系的统一语义审计（不止 schema 校验）。

    在 :func:`validate_all_structured` 的结构性校验之上，补一组**关系闭合**检查，
    覆盖 Requirement/Claim/PO/Task/Symbol/Patch/Revision/Evidence/Decision/Defect/Recovery
    之间的关键链。纯函数、只读 ``graph`` 与 ``state``（state 可空，缺省安全）。

    当前实现的跨对象规则（后续 Phase 5/6/7 投影补齐后再增量加规则，不重复造校验器）：

    * ``claim_orphan``（error）：Claim 挂在 ``proves``/``claim_satisfies`` 关系上，
      但既不 claim_satisfies 任何 PO、也无 Claim 派生自 Requirement —— 语义孤岛；
    * ``po_orphan``（warning）：required PO 没有 obligation_for / requirement_id
      锚到 Requirement（投影缺口，留给 Phase 5 链闭合后升级 error）；
    * ``patch_not_materialized``（warning）：Patch 对象没有 materialized_in 关系
      （投影缺口，保留 warning 以兼容旧 run）；
    * ``decision_without_evidence``（error）：pass Decision 没有 based_on Evidence
      （Phase 6 起硬阻断：无证据的 pass 裁决不得存在）。
    """
    g = _as_graph(g) if not isinstance(g, ont.OntologyGraph) else g
    problems: list[Problem] = []
    src = "semantic_integrity"
    _ = state if isinstance(state, dict) else {}  # 预留：后续跨 run 状态规则的读入口

    req_ids = {o.id for o in g.objects.values() if o.type == ont.TYPE_REQUIREMENT}
    claim_ids = {o.id for o in g.objects.values() if o.type == ont.TYPE_CLAIM}
    decision_ids = {o.id for o in g.objects.values() if o.type == ont.TYPE_DECISION}
    patch_ids = {o.id for o in g.objects.values() if o.type == ont.TYPE_PATCH}

    satisfied: set[str] = set()
    derives_req: set[str] = set()
    based_on_ev: set[str] = set()
    materialized: set[str] = set()
    for rel in g.relations:
        if rel.predicate == "claim_satisfies":
            satisfied.add(rel.subject)
        elif rel.predicate == "claim_derives":
            derives_req.add(rel.subject)
        elif rel.predicate == "based_on":
            based_on_ev.add(rel.subject)
        elif rel.predicate == "materialized_in":
            materialized.add(rel.subject)

    for cid in sorted(claim_ids):
        # 只审计真正接入证明链的 Claim；intake 背景 Claim 可独立存在，不拦。
        linked = cid in satisfied or cid in derives_req
        in_chain = any(
            rel.predicate == "proves" and rel.object == cid for rel in g.relations
        )
        if in_chain and not linked:
            problems.append(_err(src, "claim_orphan",
                                 f"{cid}：Claim 被证据 proves 但不满足任何 PO、也不派生自 Requirement（语义孤岛）",
                                 object_id=cid))

    for po in g.obligations.values():
        if not po.required:
            continue
        anchored = bool(po.requirement_id and po.requirement_id in req_ids) or any(
            rel.predicate == "obligation_for" and rel.subject == po.id and rel.object in req_ids
            for rel in g.relations
        )
        if not anchored:
            problems.append(_warn(src, "po_orphan",
                                  f"{po.id}：required PO 没有锚到任何 Requirement", object_id=po.id))

    for pid in sorted(patch_ids):
        if pid not in materialized:
            problems.append(_warn(src, "patch_not_materialized",
                                  f"{pid}：Patch 未 materialized_in 任何 WorkspaceRevision",
                                  object_id=pid))

    for did in sorted(decision_ids):
        verdict = ""
        obj = g.objects.get(did)
        if obj is not None:
            verdict = str(obj.payload.get("verdict") or obj.status or "")
        if verdict == "pass" and did not in based_on_ev:
            # Phase 6 起升级 error：方案§二十六/§二十八，pass Decision 必须以真实证据为基
            # （can_release 投影保证挂边；无证据的 pass 裁决即语义完整性破坏）。
            problems.append(_err(src, "decision_without_evidence",
                                 f"{did}：pass Decision 没有 based_on 任何 Evidence", object_id=did))

    # ---- Task transaction ↔ WorkspaceRevision 对齐（方案§二十四 机械校验）----
    # state 里的事务行是开发期的机械台账；投影后必须与图上链严格一致。
    transactions = []
    if isinstance(state, dict):
        transactions = [t for t in (state.get("task_transactions") or []) if isinstance(t, dict)]
    # result revision → 挂在它上面的 Patch 集合（materialized_in）。
    patches_in: dict[str, set[str]] = {}
    for rel in g.relations:
        if rel.predicate == "materialized_in" and rel.object in g.revisions:
            patches_in.setdefault(rel.object, set()).add(rel.subject)
    for txn in transactions:
        result = str(txn.get("result_workspace_revision") or "")
        if not result:
            continue
        rev = g.revisions.get(result)
        committed = str(txn.get("status") or "") == "committed"
        if rev is None:
            if committed:
                problems.append(_err(src, "transaction_revision_missing",
                                     f"已提交任务 {txn.get('task_semantic_id') or txn.get('task') or ''}"
                                     f" 的 result_workspace_revision 未投影到图：{result}",
                                     object_id=result))
            continue
        recorded_parent = str(txn.get("parent_workspace_revision") or "")
        if recorded_parent:
            if recorded_parent not in g.revisions:
                problems.append(_err(src, "transaction_parent_dangling",
                                     f"{result}：事务登记的父 revision 不在图上：{recorded_parent}",
                                     object_id=result))
            elif rev.parent_revision != recorded_parent:
                problems.append(_err(src, "transaction_parent_mismatch",
                                     f"{result}：图上 parent={rev.parent_revision or '∅'} 与事务登记 "
                                     f"{recorded_parent} 不一致（Task.base ≠ Revision.parent）",
                                     object_id=result))
        # 已提交事务若带补丁清单，图上必须有 Patch materialized_in 该 revision。
        manifest = [p for p in (txn.get("patches") or []) if isinstance(p, dict) and p.get("path")]
        if committed and manifest and not patches_in.get(result):
            problems.append(_err(src, "transaction_patch_unlinked",
                                 f"{result}：{len(manifest)} 个已应用补丁没有 Patch materialized_in 此 revision",
                                 object_id=result))
    return problems
