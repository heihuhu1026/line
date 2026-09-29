"""失败归因（机械 Failure Analyzer）：把散落的机械判据收敛成**一次确定性分类**。

为什么是纯函数、而不是"再开一个角色"：
  · 判据全部机械可枚举 —— 补丁 anchor 对不上（`_patch_blockers`）、沙箱跑不通
    （`_mechanical_blockers`）、跨文件契约虚依赖（`state.plan_unresolved`）、
    方案漏规划文件（`_plan_uncovered_defects`）、触顶 / 停滞（`_guard_stop_reason`）。
    它们本来就是**已有函数的输出**，再让模型读一遍不会多出信息；
  · 归因直接参与**路由**（回开发 / 回方案 / 交人工）。路由错了比判错更贵，
    而每多一次模型调用就多一次不确定性抖动；
  · 单驻留下每多一个模型角色就是一次完整 prefill（24K），而这里要回答的问题
    「这一轮为什么没修好」90% 是确定性的。

**先纯函数、后阶段**：本模块不碰 `self`、不调模型、不写 `state`（落账与日志由编排层做）。
将来若确实需要独立产物 / 独立指标，把本模块整体搬进 `_run_diagnose` 即可 ——
代码不用改，只多一层壳；反过来（先开阶段再降级）要拆状态机、改白名单、改指纹，代价大得多。

它替换的原先是 `_step_review` 末尾一条七层 if 链：同一件事（"这轮为什么没修好、下一跳去哪"）
散在四个函数里判，改动时容易只改一半 —— 那正是本项目"机制空转"的常见成因。
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "PATCH_UNAPPLIABLE",
    "VERIFY_FAILED",
    "CONTRACT_UNRESOLVED",
    "PLAN_GAP",
    "MISSING_ENTRY",
    "AMBIGUOUS",
    "REVIEW_QUALITY",
    "NONE",
    "OWNER_DEV",
    "OWNER_ARCHITECT",
    "OWNER_HUMAN",
    "OWNER_DEV_PATCH",
    "OWNER_COMPILER_TARGET",
    "OWNER_PATCH_RUNTIME",
    "REPEAT_ESCALATE_AFTER",
    "RECOVERY_STREAK_AFTER",
    "DEFECT_KEY_VERSION",
    "classify",
    "render",
    "defect_key",
    "patch_owner_class",
    "ledger",
    "ledger_line",
]

# --------------------------------------------------------------- 归因类型与责任方
#: 补丁定位不上（anchor / 符号与原文对不上）—— 永远套用不上，属**实现层**，回 dev 重派。
PATCH_UNAPPLIABLE = "patch_unappliable"
#: 沙箱里跑不起来 / 命令失败（含语法、导入、运行异常）—— 实现层，回 dev。
VERIFY_FAILED = "verify_failed"
#: 跨文件契约虚依赖（引用了基准里不存在的符号）—— 实现与方案都可能，归**方案层**核对。
CONTRACT_UNRESOLVED = "contract_unresolved"
#: 方案漏规划了缺陷指向的文件 —— **方案层**（dev 受白名单约束，无权创建方案外的文件）。
PLAN_GAP = "plan_gap"
#: 新建项目方案没规划可执行入口 —— 方案层（与 plan_gap 同因不同名，单独可观测）。
MISSING_ENTRY = "missing_entry"
#: 评审结论自相矛盾（判方案有错却全归"需外部确认"）—— 交人工。
AMBIGUOUS = "ambiguous"
#: 评审提出的实现层质量问题（机械证据无法证伪）—— 实现层，回 dev。
REVIEW_QUALITY = "review_quality"
#: 无缺陷（pass）。
NONE = "none"

OWNER_DEV = "dev"
OWNER_ARCHITECT = "architect_plan"
OWNER_HUMAN = "human"

# ------------------------------------------------- 补丁判负的责任主体三分类（建议⑨）
#: 补丁**内容**写错（anchor 对不上 / 补丁不完整 / 跨距不匹配 / 新文件语法错 / 重复定义）：
#: 开发重写补丁可解，归开发。
OWNER_DEV_PATCH = "dev_patch"
#: 补丁要打的**靶子本身是虚的**（施工图/方案点名的符号在基准里不存在、modify 了一个方案内
#: 但仓库里不存在的文件）：重写补丁解不了，归编译/方案层核对目标。
OWNER_COMPILER_TARGET = "compiler_target"
#: **物化链 / 仓库快照**等基础设施故障（没拿到仓库、add→modify 链断）：不是模型的锅，
#: 继续烧 Agent 无意义，达 Recovery 阈值后作为流水线/运行时错误交人工。
OWNER_PATCH_RUNTIME = "patch_runtime"

#: 同型归因**连续**出现这么多次 ⇒ 现有手段已被证明无效，**建议**升级去向。
#:
#: 本轮只**记录**（`escalate_suggested`），不改路由：路由是行为，改行为要有基线数据。
#: 先让它跑几轮真机，看清"同类重复"的真实分布，再决定阈值与升级目标
#: （回方案？还是直接交人工？）。"先把信号变成可查询数据、再用它改行为"是本轮纪律。
REPEAT_ESCALATE_AFTER = 3

#: Recovery Policy（建议⑩）触发阈值：同一缺陷 + 相同证据连续 N 轮（且实现层还要满足
#: "补丁无实质变化"）⇒ **停止原责任方继续重试**，按证据升级（Escalation by evidence），
#: 而不是按轮数空转（Escalation by round count）。
RECOVERY_STREAK_AFTER = 2

#: 缺陷跨轮身份键的版本。v1 用 `path::symbol::line::what` —— 行号漂移/措辞微调就被当成
#: 新缺陷；v2 用「缺陷类型 + 验收口径(check) + 文件 + 符号」，line/what/stderr 只是证据。
DEFECT_KEY_VERSION = "v2"

_TYPE_LABELS = {
    PATCH_UNAPPLIABLE: "补丁定位不上",
    VERIFY_FAILED: "沙箱跑不通",
    CONTRACT_UNRESOLVED: "跨文件契约虚依赖",
    PLAN_GAP: "方案漏规划文件",
    MISSING_ENTRY: "方案缺可执行入口",
    AMBIGUOUS: "评审分类自相矛盾",
    REVIEW_QUALITY: "评审提出的实现层质量问题",
    NONE: "无缺陷",
}

_STOP_LABELS = {
    "rework_limit": "已达回流上限",
    "guard_stop": "护栏停止（预算 / 停滞）",
    "ambiguous": "分类矛盾转人工",
    "recovery_escalation": "Recovery Policy：按证据升级（原责任方重试已被证明无效）",
}

_OWNER_CLASS_LABELS = {
    OWNER_DEV_PATCH: "补丁内容（DEV）",
    OWNER_COMPILER_TARGET: "目标不存在（编译/方案）",
    OWNER_PATCH_RUNTIME: "物化/仓库快照（运行时基础设施）",
}


def _short(text: Any, limit: int = 90) -> str:
    """压平空白并截断 —— 台账 key 与日志都用它，保证同一缺陷得到同一个字符串。"""
    flat = " ".join(str(text or "").split())
    return flat[:limit]


def patch_owner_class(
    status: str,
    *,
    note: str = "",
    symbol_planned: bool = False,
    file_planned: bool = False,
) -> str:
    """一条判负补丁的**责任主体**（建议⑨ 的三分类），纯机械映射。

      · ``unchecked`` + 没拿到仓库 ⇒ 运行时基础设施（快照读取/物化环境问题）；
      · ``unchecked`` + 目标文件缺失：方案/施工图覆盖了这个文件却在仓库里找不到
        ⇒ 编译/方案靶子虚（``file_planned``）；否则是开发把 add 写成了 modify ⇒ DEV；
      · ``symbol_not_found``：符号在方案/施工图里被点名、基准里却没有
        ⇒ 编译/方案靶子虚（``symbol_planned``）；否则是开发自己写错符号名 ⇒ DEV；
      · 其余（anchor 对不上、补丁残缺、新文件语法错/重复定义…）一律 DEV：
        这些是补丁**内容**问题，重写就能解。
    """
    s = str(status or "")
    note = str(note or "")
    if s == "unchecked":
        if "没有提供仓库路径" in note:
            return OWNER_PATCH_RUNTIME
        return OWNER_COMPILER_TARGET if file_planned else OWNER_DEV_PATCH
    if s == "symbol_not_found":
        return OWNER_COMPILER_TARGET if symbol_planned else OWNER_DEV_PATCH
    return OWNER_DEV_PATCH


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _texts(value: Any) -> list[str]:
    return [str(x).strip() for x in _as_list(value) if str(x).strip()]


# ---- 建议⑫ P1-2：评审三层的「第三层 · 确定性裁决」纯函数 ----
# 语义层（唯一一次 LLM）只回答机械证明不了的问题（这轮做得对不对 / 风险是不是真风险），
# verdict 在这里只是**建议**；路由由机械事实 + 语义建议按固定规则算出，规则单一真源在此。
DECISION_SEMANTIC = "semantic"  # 尊重语义 verdict
DECISION_MECH_DEV = "mechanical_rework_dev"  # 机械阻断不允许 pass
DECISION_PLAN_ARCH = "plan_rework_architect"  # 方案层矛盾/漏项
DECISION_AMBIGUOUS = "ambiguous_human"  # 只有风险描述、没有材料支撑
DECISION_PASS_EXTERNAL = "pass_external"  # 问题全在范围外
DECISION_PASS_RESIDUAL = "pass_residual_downgrade"  # 验证全绿，残留风险降级


def review_decision(
    *,
    semantic_verdict: str,
    blocked: bool,
    has_in_material: bool,
    has_architect_fixes: bool,
    plan_gap: bool = False,
    verify_pass: bool = False,
    evidence_clean: bool = False,
) -> dict:
    """把「语义建议 + 机械事实」收敛成确定 verdict（纯函数，无 IO、无状态）。

    顺序与旧版 :meth:`Orchestrator._normalize_review` 的内联裁决**逐分支等价**（仅提取）：

      1. 存在阻断级机械证据却判 pass ⇒ 强制 rework_dev；
      2. 有方案/施工图层修改项却判 pass ⇒ 强制 rework_architect；
      3. 缺陷指向方案未规划的文件（plan_gap），且当前裁决在
         (""/pass/rework_dev) ⇒ 强制 rework_architect（注意此步在 1 之后，
         因此「阻断 + pass + 漏项」历史上最终落 rework_architect，保持不变）；
      4. 终局：
         · 只剩方案层风险描述、没有任何材料 ⇒ rework_architect + escalated_ambiguous；
         · 问题全在范围外（无任何实现/方案材料）⇒ 强制 pass；
         · verify 全绿 + 机械证据干净 + 无方案修改项 ⇒ rework_dev 残留风险降级 pass；
         · 其余情况尊重语义 verdict。

    返回 ``{"verdict", "forced", "action", "reason"}``；``forced`` 仅在 verdict
    被机制改写时为真（ambiguous 不翻转 verdict，故 forced=False）。
    """
    semantic = str(semantic_verdict or "").strip()
    if semantic not in ("pass", "rework_dev", "rework_architect"):
        semantic = "rework_dev"
    cur = semantic
    forced = False
    action = DECISION_SEMANTIC
    reason = ""

    if blocked and cur == "pass":
        cur = "rework_dev"
        forced = True
        action = DECISION_MECH_DEV
        reason = "机制判定：存在阻断级机械证据（见上方机械审计），不允许判定 pass"
    elif has_architect_fixes and cur == "pass":
        cur = "rework_architect"
        forced = True
        action = DECISION_PLAN_ARCH
        reason = "机制判定：评审同时指出了方案/施工图层修改项，实现层无法消化"

    # 漏项纠正在阻断纠正**之后**（保持历史行为，见 docstring 第 3 条）。
    if plan_gap and cur in ("", "pass", "rework_dev"):
        cur = "rework_architect"
        forced = True
        action = DECISION_PLAN_ARCH
        reason = "机制判定：缺陷指向方案未规划的文件，必须回到架构方案补规划"

    if (
        not has_in_material
        and not has_architect_fixes
        and cur == "rework_architect"
        and not blocked
    ):
        # 评审只说"方案有风险"却给不出任何方案/实现材料：分类自相矛盾。
        action = DECISION_AMBIGUOUS
        reason = "机制判定：评审选择了 rework_architect 但未提供任何方案/实现层材料，标记为分类矛盾"
    elif (
        not has_in_material
        and not has_architect_fixes
        and cur == "rework_dev"
        and not blocked
    ):
        cur = "pass"
        forced = True
        action = DECISION_PASS_EXTERNAL
        reason = "机制判定：所有问题都在范围外，强制 pass（范围外问题记录为 external_fixes）"
    elif (
        cur == "rework_dev"
        and not blocked
        and not has_architect_fixes
        and evidence_clean
        and verify_pass
    ):
        cur = "pass"
        forced = True
        action = DECISION_PASS_RESIDUAL
        reason = "机制判定：verify 全绿且机械证据干净，残留风险降级为 residual_risks"
    return {"verdict": cur, "forced": forced, "action": action, "reason": reason}


def classify(
    *,
    review: Any = None,
    attempt: int = 0,
    max_rework: int = 0,
    guard_stop: str = "",
    patch_blockers: Any = None,
    patch_failures: Any = None,
    mechanical_blockers: Any = None,
    unresolved: Any = None,
    missing_entry: bool = False,
    uncovered_files: Any = None,
    external_fixes: Any = None,
    prior_types: Any = None,
    open_defects: Any = None,
    impl_unchanged: bool = False,
) -> dict:
    """一次确定性归因 + 去向判定。

    入参全部是**已有产物的只读投影**（编排层负责取，本函数不读 state、不调模型）。

    返回同形 dict：

        {"type", "owner", "owner_class", "recover_stage", "needs_human", "stop",
         "evidence", "detail", "round", "repeat", "escalate_suggested", "recovery"}

      · `type`    —— "为什么没修好"（与去向**分开**：去向会被上限/停滞截断，
                     但归因不该被截断，否则触顶那一轮的账就丢了）；
      · `owner_class` —— 补丁判负的责任主体三分类（dev_patch / compiler_target /
                     patch_runtime），非补丁类归因为空；
      · `recover_stage` ∈ {"dev", "architect_plan", "human_review", "done"}，与
        `flow.CONDITIONAL_EDGES["review"]` 的取值同域；
      · `stop`    —— 被哪条护栏截断（空 = 正常回流）；
      · `recovery` —— Recovery Policy 的裁决（retry_owner / escalate_plan /
                     escalate_human + 证据 streak + 理由）。
    """
    rev = review if isinstance(review, dict) else {}
    verdict = str(rev.get("verdict") or "")
    patch_blockers = _texts(patch_blockers)
    mechanical = _texts(mechanical_blockers)
    unresolved = [u for u in _as_list(unresolved) if isinstance(u, dict)]
    gaps = _texts(uncovered_files)
    arch_fixes = _texts(rev.get("architect_fixes"))
    in_material = _texts(rev.get("required_fixes"))
    external = _texts(external_fixes)

    evidence: list[str] = []
    owner_class = ""

    # 补丁判负的责任主体：结构化补丁行（status + 是否方案内目标）→ 三分类。
    # 多条判负时取**最不该甩给开发**的那一类（基础设施 > 虚靶 > 内容），
    # 避免一条 unchecked 藏在一堆 anchor_not_found 里又把整轮打回 dev。
    if patch_blockers:
        classes = [
            patch_owner_class(
                str(f.get("status") or ""),
                note="；".join(_texts(f.get("notes"))[:1]),
                symbol_planned=bool(f.get("symbol_planned")),
                file_planned=bool(f.get("file_planned")),
            )
            for f in _as_list(patch_failures)
            if isinstance(f, dict)
        ]
        if OWNER_PATCH_RUNTIME in classes:
            owner_class = OWNER_PATCH_RUNTIME
        elif OWNER_COMPILER_TARGET in classes:
            owner_class = OWNER_COMPILER_TARGET
        elif classes:
            owner_class = OWNER_DEV_PATCH
        else:
            owner_class = OWNER_DEV_PATCH

    # ---------------------------------------------------------------- 归因（为什么）
    # 顺序 = "根因优先于症状"：补丁定位不上是本因，沙箱跑不通只是它（或别的原因）的表现；
    # 方案层问题优先于实现层问题，因为方案不改、实现层改多少次都无效。
    if patch_blockers:
        if owner_class == OWNER_PATCH_RUNTIME:
            ftype, owner = PATCH_UNAPPLIABLE, OWNER_PATCH_RUNTIME
        elif owner_class == OWNER_COMPILER_TARGET:
            ftype, owner = PATCH_UNAPPLIABLE, OWNER_ARCHITECT
        else:
            ftype, owner = PATCH_UNAPPLIABLE, OWNER_DEV
        evidence = patch_blockers
    elif mechanical:
        ftype, owner = VERIFY_FAILED, OWNER_DEV
        evidence = mechanical
    elif unresolved:
        ftype, owner = CONTRACT_UNRESOLVED, OWNER_ARCHITECT
        evidence = [f"{u.get('symbol')}（{_short(u.get('reason'), 60)}）" for u in unresolved]
    elif gaps:
        ftype, owner = PLAN_GAP, OWNER_ARCHITECT
        evidence = [f"方案未覆盖：{'、'.join(gaps[:4])}"]
    elif missing_entry and arch_fixes:
        ftype, owner = MISSING_ENTRY, OWNER_ARCHITECT
        evidence = ["方案没有可执行入口，而开发无权创建方案外的文件"]
    elif arch_fixes:
        ftype, owner = PLAN_GAP, OWNER_ARCHITECT
        evidence = arch_fixes
    elif rev.get("escalated_ambiguous"):
        ftype, owner = AMBIGUOUS, OWNER_HUMAN
        evidence = ["判 rework_architect，但返工项全被归为 needs_external"]
    elif verdict == "pass":
        ftype, owner = NONE, ""
    else:
        ftype, owner = REVIEW_QUALITY, OWNER_DEV
        evidence = in_material or external

    # ---------------------------------------------------------------- 去向（下一跳）
    # 顺序必须与历史 `_step_review` 完全一致（这一层是行为，不是重构）：
    # pass → escalated_ambiguous → 触顶 → 护栏 → 方案层 → 实现层。
    needs_human = False
    stop = ""
    detail = ""
    if verdict == "pass":
        recover = "human_review"
    elif rev.get("escalated_ambiguous"):
        needs_human, stop, recover = True, "ambiguous", "done"
        detail = "评审结论自相矛盾（判方案有错却又全需外部确认），交人工裁决"
    elif attempt > max_rework:
        needs_human, stop, recover = True, "rework_limit", "done"
        detail = f"已达回流上限 {max_rework}，标记 needs_human"
    elif guard_stop:
        needs_human, stop, recover = True, "guard_stop", "done"
        detail = f"[护栏] {guard_stop} → 停止返工，转人工裁决（不再继续烧）"
    elif verdict == "rework_architect" or arch_fixes:
        recover = "architect_plan"
    else:
        recover = "dev"

    # ---------------------------------------------------------------- 重复信号（只记录）
    prior = [str(t or "") for t in _as_list(prior_types)]
    repeat = 0
    if ftype != NONE:
        tail = 0
        for item in reversed(prior):
            if item != ftype:
                break
            tail += 1
        repeat = tail + 1
    escalate = bool(
        repeat >= REPEAT_ESCALATE_AFTER and owner == OWNER_DEV and recover == "dev"
    )

    # ------------------------------------------- Recovery Policy（建议⑩：按证据升级）
    # 四个条件全满足才动路由：同一缺陷身份 + 相同证据 + 连续 N 轮 + （实现层）补丁无实质变化。
    # 与上面的 repeat 建议不同：那条只**记录**轮数；这条有证据支撑，**真的停止原责任方重试**。
    recovery = {"action": "retry_owner", "reason": "", "streak": 0}
    open_map = open_defects if isinstance(open_defects, dict) else {}
    defect_streak = max(
        (int(rec.get("evidence_streak") or 0) for rec in open_map.values()
         if isinstance(rec, dict)),
        default=0,
    )
    if ftype != NONE and not stop:
        if owner == OWNER_PATCH_RUNTIME and repeat >= RECOVERY_STREAK_AFTER:
            # 物化/仓库快照错误反复出现 ⇒ 停止 Agent，按流水线/运行时错误交人工。
            recovery = {
                "action": "escalate_human",
                "reason": "物化/仓库快照类故障连续出现，重试 Agent 不可能修复基础设施",
                "streak": repeat,
            }
        elif owner == OWNER_ARCHITECT and repeat >= RECOVERY_STREAK_AFTER:
            # 同一方案层归因连续 N 轮 ⇒ 架构师自查也未解决，升级人工。
            recovery = {
                "action": "escalate_human",
                "reason": "方案层归因连续出现且方案返工未消除，交人工裁决",
                "streak": repeat,
            }
        elif (
            recover == "dev"
            and defect_streak >= RECOVERY_STREAK_AFTER
            and impl_unchanged
        ):
            # 同一缺陷、相同证据、开发连修 N 轮、交付指纹与上轮**完全相同**
            # ⇒ 这不是"开发再试一次"能解的，回方案复查（Plan Recheck）。
            recovery = {
                "action": "escalate_plan",
                "reason": (
                    f"同一缺陷以相同证据连续 {defect_streak} 轮失败，"
                    "且本轮交付相对上轮无实质变化 → 停止打回 DEV，转方案复查"
                ),
                "streak": defect_streak,
            }
        if recovery["action"] == "escalate_human":
            needs_human, stop, recover = True, "recovery_escalation", "done"
            detail = f"[Recovery] {recovery['reason']}（连续第 {recovery['streak']} 轮）"
        elif recovery["action"] == "escalate_plan":
            recover = "architect_plan"
            detail = f"[Recovery] {recovery['reason']}"

    # `owner`（谁能改）与 `recover_stage`（本轮实际去哪）**允许不一致**，且必须显式标出来：
    # 前者是归因结论，后者是**历史行为**。契约虚依赖这类归因指向方案层，但路由暂时不动它
    # —— 改路由是改行为，要有基线数据（先看真机分布，再决定是否把它接进路由）。
    # 不标出来的话，这份数据读起来会像"机制已经这么做了"，那是最危险的误读。
    mismatch = bool(owner and owner != recover)

    return {
        "type": ftype,
        "owner": owner,
        "owner_class": owner_class,
        "recover_stage": recover,
        "owner_mismatch": mismatch,
        "needs_human": needs_human,
        "stop": stop,
        "evidence": evidence[:4],
        "detail": detail,
        "round": int(attempt or 0),
        "repeat": repeat,
        "escalate_suggested": escalate,
        "recovery": recovery,
    }


def render(record: Any) -> list[str]:
    """归因的**日志形态**（编排层负责打印，文案与留痕统一在这里）。"""
    rec = record if isinstance(record, dict) else {}
    ftype = str(rec.get("type") or NONE)
    label = _TYPE_LABELS.get(ftype, ftype)
    owner = str(rec.get("owner") or "")
    owner_class = str(rec.get("owner_class") or "")
    line = f"  [归因] {label}（owner={owner or '—'}"
    if owner_class and owner_class != OWNER_DEV_PATCH:
        line += f"，责任主体={_OWNER_CLASS_LABELS.get(owner_class, owner_class)}"
    line += "）"
    if int(rec.get("repeat") or 0) > 1:
        line += f"；**同类连续第 {rec['repeat']} 轮**"
    recovery = rec.get("recovery") if isinstance(rec.get("recovery"), dict) else {}
    recovery_action = str(recovery.get("action") or "retry_owner")
    if rec.get("owner_mismatch") and recovery_action == "retry_owner":
        line += f"；归因指向 {owner}，但本轮路由按既有规则走（路由改动需基线，先记录）"
    if rec.get("escalate_suggested"):
        line += " → 现有手段已证明无效，建议升级去向（本轮仅记录）"
    out = [line]
    evidence = [str(x) for x in _as_list(rec.get("evidence")) if str(x).strip()]
    if evidence:
        out.append("        证据：" + "；".join(evidence[:3]))
    if recovery_action != "retry_owner":
        target = {"escalate_plan": "方案复查", "escalate_human": "人工裁决"}.get(
            recovery_action, recovery_action
        )
        out.append(f"        [Recovery Policy] → {target}：{recovery.get('reason') or ''}")
    if rec.get("detail"):
        out.append(f"        {rec['detail']}")
    return out


# --------------------------------------------------------------------- 缺陷台账
#: defect_items 的来源 → 缺陷类别（行里没有 kind 时的兜底推导用）。
_SOURCE_KIND = {
    "patch_audit": "patch",
    "traceback": "traceback",
    "verify": "verify",
    "review": "review",
    "coverage": "coverage",
}


def _normalize_command(command: Any) -> str:
    """命令的归一化 check_id（与 tasktype._cmd_key 同口径：压空白 + 小写）。"""
    return " ".join(str(command or "").split()).lower()[:160]


def _defect_kind(item: dict) -> str:
    """缺陷类别：优先用行里的 `kind`（如 ``patch:anchor_not_found`` /
    ``traceback:ImportError``）；没有就从来源退导；再没有用 ``legacy``。"""
    kind = _short(item.get("kind"), 80)
    if kind:
        return kind
    return _SOURCE_KIND.get(str(item.get("source") or ""), "legacy")


def _check_id(item: dict, kind: str) -> str:
    """验收口径身份（check_id）：命令类缺陷 = 归一化命令；漏图 = missing；
    补丁类 = 空（类别里已含状态码）；评审/兜底行 = 问题摘要（文本本身就是口径）。"""
    if kind.startswith("traceback") or kind == "verify":
        return _normalize_command(item.get("command"))
    if kind == "coverage":
        return "missing"
    if kind.startswith("patch"):
        # 老行（只有 source=patch_audit、没有 kind 状态码）退回摘要保身份
        return "" if kind != "patch" else _short(item.get("what"), 60)
    return _short(item.get("what"), 90)


def defect_key(row: Any) -> str:
    """一条缺陷的**跨轮稳定键（v2，建议⑦）**。

    身份 = ``缺陷类型 + 验收口径(check_id) + 文件 + 符号``，例如
    ``v2::traceback:ImportError::main.py::main::python -m unittest``。

    **刻意不进身份**的：行号、问题措辞、stderr —— 它们只是**证据**。同一个导入错误
    从第 20 行漂到第 27 行、"模块无法导入"改成"import 失败"，都还是同一条缺陷；
    v1 把这些拼进键，于是每次返工都报"新缺陷"，回归永远识别不出来。

    仍保守的地方：评审/兜底行没有机械 check 口径，只能退用摘要文本，措辞变化仍会
    新开一条 —— 宁可多报新出现，也不把两条不同缺陷合成一条。
    """
    item = row if isinstance(row, dict) else {}
    kind = _defect_kind(item)
    path = _short(item.get("path"), 200)
    symbol = _short(item.get("symbol"), 80)
    return "::".join([DEFECT_KEY_VERSION, kind, path, symbol, _check_id(item, kind)])


def _key_path(key: str) -> str:
    """从台账键里取文件段（v2 键多段，不能再用 ``split("::")[0]``）。"""
    parts = str(key).split("::")
    if len(parts) >= 5 and parts[0] == DEFECT_KEY_VERSION:
        return parts[2]
    return parts[0] if parts else str(key)


def _evidence_digest(item: dict) -> str:
    """缺陷的**证据指纹**：只取问题摘要（不含行号 —— 行漂移不算新证据）。
    Recovery Policy 用它判断"是不是还在拿同一个错误原地打转"。"""
    return _short(item.get("what"), 120)


def ledger(previous: Any, *, rows: Any, round_no: int = 0) -> dict:
    """跨轮缺陷台账：**仍开 / 已核对转绿 / 不再出现 / 回归**。

    为什么必须有：`defect_verdicts` 每轮从 verify 重算，没有跨轮身份，
    于是"这条修好了"与"这条被漏报了"分不清 —— 而返工反复不收敛时最缺的可观测性
    就是这一句"到底还剩几条、上次那几条去哪了"。

    跨轮**守恒**（这是台账的立身之本）：
      · 本轮没再出现的**已转绿**条目继续计入 `fixed_verified` —— 绿是机械核对过的事实，
        不能因为"这轮没跑那条命令"就把它从账上抹掉（旧实现因此每次 pass 后账目清零）；
      · 已转绿条目这轮又红 ⇒ 回归：从 fixed 摘掉、重新进 open 并标 `regressed`；
      · `dropped`（上轮还开着、这轮不再出现也没转绿）刻意**不自动判为已修复**：
        它也可能是被漏报，标出来交给人/评审，比替它们下结论可靠。
    """
    prev_open = {
        str(k): v
        for k, v in ((previous or {}).get("open") or {}).items()
        if isinstance(v, dict)
    }
    prev_fixed = {str(k) for k in ((previous or {}).get("fixed_verified") or [])}
    prev_fixed_detail = {
        str(k): v
        for k, v in ((previous or {}).get("fixed_detail") or {}).items()
        if isinstance(v, dict)
    }
    open_now: dict[str, dict] = {}
    fixed_detail: dict[str, dict] = dict(prev_fixed_detail)
    green_now: set[str] = set()
    reopened: set[str] = set()
    fresh = 0
    for row in _as_list(rows):
        if not isinstance(row, dict):
            continue
        key = defect_key(row)
        was_open = key in prev_open
        was_fixed = key in prev_fixed
        rec = dict(prev_open.get(key) or prev_fixed_detail.get(key) or {})
        if not was_open and not was_fixed:
            fresh += 1
        # **证据连续计数**（Recovery Policy 用）：上一轮就开着、且证据指纹与本轮相同
        # ⇒ +1；转绿后重开算新一轮失败发作，从 1 重新计（不能把绿之前的旧账并进来）。
        ev = _evidence_digest(row)
        if was_open and rec.get("evidence") and rec.get("evidence") == ev:
            evidence_streak = int(rec.get("evidence_streak") or 1) + 1
        else:
            evidence_streak = 1
        rec.update(
            {
                "first_round": rec.get("first_round", round_no),
                "last_round": round_no,
                "rounds": int(rec.get("rounds") or 0) + 1,
                "status": str(row.get("status") or ""),
                "where": _short(row.get("where"), 120),
                "what": _short(row.get("what")),
                "evidence": ev,
                "evidence_streak": evidence_streak,
            }
        )
        if rec["status"] == "green":
            green_now.add(key)
            # 以前哪一轮转绿的要保留（本轮没记录的已绿条目由 fixed_detail 原样继承）
            rec.setdefault("fixed_round", round_no)
            fixed_detail[key] = rec
        else:
            # **回归**：上一轮已核对转绿、这一轮又红了。这是台账能给出的最有价值的一条信号
            # —— 比"还剩几条"更重要，因为它说明上一轮的修复**是假的或脆的**
            # （真机形态：某条命令这轮绿、下轮红，来回摆）。
            if was_fixed and not was_open:
                rec["regressed"] = True
                rec["reopened_round"] = round_no
                reopened.add(key)
            open_now[key] = rec
    # 已转绿又变红 ⇒ 从 fixed 侧摘掉；其余历史转绿条目**守恒保留**
    fixed_set = (prev_fixed - reopened) | green_now
    for key in reopened:
        fixed_detail.pop(key, None)
    dropped = sorted(k for k in prev_open if k not in open_now and k not in green_now)
    return {
        "round": int(round_no or 0),
        "open": open_now,
        "fixed_verified": sorted(fixed_set),
        # 转绿条目的明细（首现轮次/转绿轮次/最近证据），供回归时恢复身份与人工追溯
        "fixed_detail": {k: fixed_detail[k] for k in sorted(fixed_set) if k in fixed_detail},
        "dropped": dropped,
        "regressions": sorted(k for k, v in open_now.items() if v.get("regressed")),
        "fresh": fresh,
    }


def ledger_line(state: Any) -> str:
    """台账的**日志形态**：一句话说清"还剩几条、上次那几条去哪了"。"""
    led = state if isinstance(state, dict) else {}
    open_n = len(led.get("open") or {})
    line = (
        f"  [缺陷台账] 第 {led.get('round')} 轮：仍开 {open_n} 条"
        f"（本轮新增 {int(led.get('fresh') or 0)}）"
        f" / 转绿 {len(led.get('fixed_verified') or [])} 条"
        f" / 不再出现 {len(led.get('dropped') or [])} 条"
    )
    dropped = [str(x) for x in _as_list(led.get("dropped")) if str(x).strip()]
    if dropped:
        # "不再出现"不等于"已修复"（也可能是漏报）—— 点名出来，别让人以为它修好了。
        line += "；不再出现的（**不等于已修复，需人工/评审核对**）：" + "、".join(
            _key_path(d) or d for d in dropped[:3]
        )
    regressed = [str(x) for x in _as_list(led.get("regressions")) if str(x).strip()]
    if regressed:
        line += "；**回归**（上轮已转绿又红了）：" + "、".join(
            _key_path(r) or r for r in regressed[:3]
        )
    return line
