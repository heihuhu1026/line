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
    "REPEAT_ESCALATE_AFTER",
    "classify",
    "render",
    "defect_key",
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

#: 同型归因**连续**出现这么多次 ⇒ 现有手段已被证明无效，**建议**升级去向。
#:
#: 本轮只**记录**（`escalate_suggested`），不改路由：路由是行为，改行为要有基线数据。
#: 先让它跑几轮真机，看清"同类重复"的真实分布，再决定阈值与升级目标
#: （回方案？还是直接交人工？）。"先把信号变成可查询数据、再用它改行为"是本轮纪律。
REPEAT_ESCALATE_AFTER = 3

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
}


def _short(text: Any, limit: int = 90) -> str:
    """压平空白并截断 —— 台账 key 与日志都用它，保证同一缺陷得到同一个字符串。"""
    flat = " ".join(str(text or "").split())
    return flat[:limit]


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _texts(value: Any) -> list[str]:
    return [str(x).strip() for x in _as_list(value) if str(x).strip()]


def classify(
    *,
    review: Any = None,
    attempt: int = 0,
    max_rework: int = 0,
    guard_stop: str = "",
    patch_blockers: Any = None,
    mechanical_blockers: Any = None,
    unresolved: Any = None,
    missing_entry: bool = False,
    uncovered_files: Any = None,
    external_fixes: Any = None,
    prior_types: Any = None,
) -> dict:
    """一次确定性归因 + 去向判定。

    入参全部是**已有产物的只读投影**（编排层负责取，本函数不读 state、不调模型）。

    返回同形 dict：

        {"type", "owner", "recover_stage", "needs_human", "stop",
         "evidence", "detail", "round", "repeat", "escalate_suggested"}

      · `type`    —— "为什么没修好"（与去向**分开**：去向会被上限/停滞截断，
                     但归因不该被截断，否则触顶那一轮的账就丢了）；
      · `recover_stage` ∈ {"dev", "architect_plan", "human_review", "done"}，与
        `flow.CONDITIONAL_EDGES["review"]` 的取值同域；
      · `stop`    —— 被哪条护栏截断（空 = 正常回流）。
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

    # ---------------------------------------------------------------- 归因（为什么）
    # 顺序 = "根因优先于症状"：补丁定位不上是本因，沙箱跑不通只是它（或别的原因）的表现；
    # 方案层问题优先于实现层问题，因为方案不改、实现层改多少次都无效。
    if patch_blockers:
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

    # `owner`（谁能改）与 `recover_stage`（本轮实际去哪）**允许不一致**，且必须显式标出来：
    # 前者是归因结论，后者是**历史行为**。契约虚依赖这类归因指向方案层，但路由暂时不动它
    # —— 改路由是改行为，要有基线数据（先看真机分布，再决定是否把它接进路由）。
    # 不标出来的话，这份数据读起来会像"机制已经这么做了"，那是最危险的误读。
    mismatch = bool(owner and owner != recover)

    return {
        "type": ftype,
        "owner": owner,
        "recover_stage": recover,
        "owner_mismatch": mismatch,
        "needs_human": needs_human,
        "stop": stop,
        "evidence": evidence[:4],
        "detail": detail,
        "round": int(attempt or 0),
        "repeat": repeat,
        "escalate_suggested": escalate,
    }


def render(record: Any) -> list[str]:
    """归因的**日志形态**（编排层负责打印，文案与留痕统一在这里）。"""
    rec = record if isinstance(record, dict) else {}
    ftype = str(rec.get("type") or NONE)
    label = _TYPE_LABELS.get(ftype, ftype)
    owner = str(rec.get("owner") or "")
    line = f"  [归因] {label}（owner={owner or '—'}）"
    if int(rec.get("repeat") or 0) > 1:
        line += f"；**同类连续第 {rec['repeat']} 轮**"
    if rec.get("owner_mismatch"):
        line += f"；归因指向 {owner}，但本轮路由按既有规则走（路由改动需基线，先记录）"
    if rec.get("escalate_suggested"):
        line += " → 现有手段已证明无效，建议升级去向（本轮仅记录）"
    out = [line]
    evidence = [str(x) for x in _as_list(rec.get("evidence")) if str(x).strip()]
    if evidence:
        out.append("        证据：" + "；".join(evidence[:3]))
    if rec.get("detail"):
        out.append(f"        {rec['detail']}")
    return out


# --------------------------------------------------------------------- 缺陷台账
def defect_key(row: Any) -> str:
    """一条缺陷的**跨轮稳定键**。

    用 `tasktype.defect_items` 已经给出的四个字段拼（位置 + 符号 + 行 + 摘要），
    而不是用"第几条"这种序号 —— 序号每轮都会重排，用它做键等于没有台账。
    键是**指纹**（摘要按 90 字截断）：措辞微调会计成新条目，这是刻意的保守选择 ——
    宁可多报"新出现"，也不要把两条不同的缺陷合成一条。
    """
    item = row if isinstance(row, dict) else {}
    path = _short(item.get("path"), 200)
    symbol = _short(item.get("symbol"), 80)
    line = _short(item.get("line"), 12)
    return f"{path}::{symbol}::{line}::{_short(item.get('what'))}"


def ledger(previous: Any, *, rows: Any, round_no: int = 0) -> dict:
    """跨轮缺陷台账：**仍开 / 本轮转绿 / 不再出现**。

    为什么必须有：`defect_verdicts` 每轮从 verify 重算，没有跨轮身份，
    于是"这条修好了"与"这条被漏报了"分不清 —— 而返工反复不收敛时最缺的可观测性
    就是这一句"到底还剩几条、上次那几条去哪了"。

    `dropped`（上轮还开着、这轮不再出现）刻意**不自动判为已修复**：它也可能是被漏报。
    把它标出来交给人/评审，比替它们下结论可靠。
    """
    prev_open = {
        str(k): v
        for k, v in ((previous or {}).get("open") or {}).items()
        if isinstance(v, dict)
    }
    prev_fixed = {str(k) for k in ((previous or {}).get("fixed_verified") or [])}
    open_now: dict[str, dict] = {}
    fixed: list[str] = []
    fresh = 0
    for row in _as_list(rows):
        if not isinstance(row, dict):
            continue
        key = defect_key(row)
        rec = dict(prev_open.get(key) or {})
        if not rec:
            fresh += 1
        rec.update(
            {
                "first_round": rec.get("first_round", round_no),
                "last_round": round_no,
                "rounds": int(rec.get("rounds") or 0) + 1,
                "status": str(row.get("status") or ""),
                "where": _short(row.get("where"), 120),
                "what": _short(row.get("what")),
            }
        )
        if rec["status"] == "green":
            fixed.append(key)
        else:
            # **回归**：上一轮已核对转绿、这一轮又红了。这是台账能给出的最有价值的一条信号
            # —— 比"还剩几条"更重要，因为它说明上一轮的修复**是假的或脆的**
            # （真机形态：某条命令这轮绿、下轮红，来回摆）。
            if key in prev_fixed:
                rec["regressed"] = True
            open_now[key] = rec
    fixed_set = set(fixed)
    dropped = sorted(k for k in prev_open if k not in open_now and k not in fixed_set)
    return {
        "round": int(round_no or 0),
        "open": open_now,
        "fixed_verified": sorted(fixed_set),
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
            d.split("::")[0] or d for d in dropped[:3]
        )
    regressed = [str(x) for x in _as_list(led.get("regressions")) if str(x).strip()]
    if regressed:
        line += "；**回归**（上轮已转绿又红了）：" + "、".join(
            r.split("::")[0] or r for r in regressed[:3]
        )
    return line
