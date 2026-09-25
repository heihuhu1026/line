"""问题记录（issues）：把一次运行里散落各处的「问题信号」归一成结构化事件流。

为什么要它：在它出现之前，问题散落在 5 个地方 ——
`llm-calls.jsonl` 的 `attempt/truncated/prompt_over_budget`、`summary.grounding_warnings`、
`review.required_fixes/blockers/residual_risks`、`test.coverage_gaps`、`state.human_feedback`。
人能拼出来，但**无法统计、无法跨轮次追踪复发、无法交给另一个模型做元优化**。

本模块提供：
1. `collect_issues(state)`  —— 纯函数：从 state.json + 阶段产物推导出结构化问题列表（不改状态，可随时重算）。
2. `write_issues(run_dir, ...)` —— 落盘 `issues.jsonl`（机器用）+ `issues.md`（人用）。
3. `build_report(runs_dir)` —— 跨运行汇总，这是「用记录去优化流水线本身」的输入。
4. `pipeline_fingerprint()` / `env_snapshot()` —— 记录当时的提示词与配置指纹。
   **没有指纹就无法比较**：改了 prompts.py 之后，问题的增减到底是因为改动，还是因为别的，必须靠指纹区分。
5. `build_meta_prompt(report)` + `META_PROPOSAL_SCHEMA` —— 把记录整理成可交给模型的分析任务。

设计约束：只做**记录与建议**，永不自动修改提示词或代码。元优化的产物是给人审的建议书。

分类（kind）一览见 `KINDS`；`SEVERITY` 取 info / warn / blocker。
"""
from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import runstore
from .config import (
    BASELINE_PREFILL,
    CODE_BUDGET,
    DEFAULT_BASELINE_PREFILL,
    MAX_REWORK_ROUNDS,
    PROMPT_HARD_RATIO,
    REVIEW_EVERY,
    STAGE_MODELS,
)

SLOW_CALL_S = 240  # 单次调用超过该秒数记一条性能类问题
DETAIL_CHARS = 400
TITLE_CHARS = 160

KINDS: list[str] = [
    "contract_violation",
    "prompt_truncated",
    "prompt_over_budget",
    "slow_call",
    "gpu_partial_offload",
    "prefill_degraded",
    "ungrounded_path",
    "review_rework_architect",
    "review_required_fix",
    "review_blocker",
    "review_residual_risk",
    "review_forced_pass",
    "review_forced_rework",
    "plan_task_uncovered",
    "implementation_empty",
    "task_not_implemented",
    "fabricated_task_ref",
    "anchor_not_found",
    "anchor_ambiguous",
    "patch_incomplete",
    "patch_span_mismatch",
    "symbol_already_exists",
    "patch_symbol_missing",
    "patch_no_effect",
    "patch_already_applied",
    "new_file_duplicate_symbol",
    "new_file_syntax_error",
    "semantic_error",
    "test_gap",
    "pm_unknown",
    "arch_uncertainty",
    "human_gate",
    "human_edit",
    "human_rework",
    "human_directive",
    "needs_human",
]

KIND_CN: dict[str, str] = {
    "contract_violation": "输出不合契约（重试过）",
    "prompt_truncated": "prompt 超预算被裁剪",
    "prompt_over_budget": "prompt 逼近上下文上限",
    "slow_call": "单次调用过慢",
    "gpu_partial_offload": "模型没全量上显存（环境问题）",
    "prefill_degraded": "推理吞吐退化一个数量级（服务需重启）",
    "ungrounded_path": "路径未接地（疑似编造）",
    "review_rework_architect": "评审判定方案返工",
    "review_required_fix": "评审要求必改项",
    "review_blocker": "评审阻断项",
    "review_residual_risk": "评审残留风险（需人工确认）",
    "review_forced_pass": "评审判定被机制放行（返工项全需外部确认）",
    "review_forced_rework": "评审判定被机制推翻（补丁有阻断级问题）",
    "plan_task_uncovered": "方案任务未被任何补丁覆盖",
    "implementation_empty": "实现为空（只有未实现声明）",
    "task_not_implemented": "开发声明未实现的任务",
    "fabricated_task_ref": "引用了方案里不存在的任务 id",
    "anchor_not_found": "补丁 anchor 在原文里找不到",
    "anchor_ambiguous": "补丁 anchor 在原文里不唯一",
    "patch_incomplete": "声称完整替换但补丁是片段",
    "patch_span_mismatch": "anchor 范围与补丁内容不匹配（贴回去会留残码）",
    "symbol_already_exists": "新增的符号原文里已存在（会重复定义）",
    "patch_symbol_missing": "补丁里没有声明的符号",
    "patch_no_effect": "补丁等于没改",
    "patch_already_applied": "补丁看起来已应用过",
    "new_file_duplicate_symbol": "同一个新文件里重复定义同一符号",
    "new_file_syntax_error": "新增文件内容本身有语法错误",
    "semantic_error": "语义错误（类型级）",
    "test_gap": "测试未覆盖",
    "pm_unknown": "需求未决信息",
    "arch_uncertainty": "架构师不确定项",
    "human_gate": "人工闸门暂停",
    "human_edit": "人工修改产物",
    "human_rework": "人工打回重跑",
    "human_directive": "人工意见注入",
    "needs_human": "回流触顶需人工裁决",
}

SOURCES_CN = {"model": "模型", "system": "流水线", "human": "人工"}


@dataclass
class Issue:
    kind: str
    stage: str
    severity: str  # info / warn / blocker
    source: str  # model / system / human
    title: str
    detail: str = ""
    attempt: int | None = None
    evidence: dict = field(default_factory=dict)
    occurrence: int = 1  # 同一问题在本次运行中第几次出现
    recurred: bool = False  # 出现过多次 = 上一轮的处理没解决

    @property
    def key(self) -> str:
        return f"{self.kind}|{self.stage}|{_norm(self.title)[:60]}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["key"] = self.key
        data["kind_cn"] = KIND_CN.get(self.kind, self.kind)
        data["source_cn"] = SOURCES_CN.get(self.source, self.source)
        return data


# --------------------------------------------------------------------- 工具
def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text if text is not None else "")).strip()


def _short(text: Any, limit: int = DETAIL_CHARS) -> str:
    text = _norm(text)
    return text[:limit] + ("…" if len(text) > limit else "")


def _severity_counts(issues: list[Issue]) -> dict[str, int]:
    out = {key: 0 for key in ("info", "warn", "blocker")}
    for issue in issues:
        out[issue.severity] = out.get(issue.severity, 0) + 1
    return out


def kind_counts(issues: list[Issue]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue.kind] = counts.get(issue.kind, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def summarize(issues: list[Issue]) -> dict:
    return {
        "total": len(issues),
        "by_kind": kind_counts(issues),
        "by_severity": _severity_counts(issues),
        "recurred": sum(1 for i in issues if i.recurred),
        "blockers": sum(1 for i in issues if i.severity == "blocker"),
    }


# --------------------------------------------------------------------- 采集
def collect_issues(state: dict | None, run_id: str = "") -> list[Issue]:
    """从一次运行的 state（或 summary 兜底）推导问题列表；纯函数，可随时重算。"""
    state = state or {}
    artifacts = state.get("artifacts") or {}
    issues: list[Issue] = []

    def add(kind: str, stage: str, severity: str, source: str, title: Any, detail: Any = "",
            attempt: int | None = None, **evidence: Any) -> None:
        issues.append(
            Issue(
                kind=kind,
                stage=stage,
                severity=severity,
                source=source,
                title=_short(title, TITLE_CHARS),
                detail=_short(detail),
                attempt=attempt,
                evidence={k: v for k, v in evidence.items() if v not in (None, "", [], {})},
            )
        )

    # 1) 每次调用的自身信号（契约重试 / 裁剪 / 预算 / 慢）
    for call in state.get("calls") or []:
        stage = str(call.get("stage") or "?")
        attempt = int(call.get("attempt") or 1)
        if attempt > 1:
            add(
                "contract_violation", stage, "warn" if attempt < 3 else "blocker", "model",
                f"{stage} 输出不合契约，客户端重试第 {attempt} 次",
                "；".join(str(e) for e in (call.get("schema_errors") or [])[:6]),
                attempt=attempt,
                tag=call.get("tag"), num_ctx=call.get("num_ctx"),
            )
        if call.get("truncated"):
            add(
                "prompt_truncated", stage, "warn", "system",
                f"{stage} 的 prompt 超出预算，上游片段被裁剪",
                f"prompt_tokens={call.get('prompt_tokens')} budget={STAGE_MODELS[stage].prompt_token_budget if stage in STAGE_MODELS else '?'}",
                tag=call.get("tag"),
            )
        if call.get("prompt_over_budget"):
            add(
                "prompt_over_budget", stage, "warn", "system",
                f"{stage} 的 prompt 达到上下文 {call.get('prompt_over_ctx_ratio')} 倍（阈值 {PROMPT_HARD_RATIO}）",
                f"prompt_tokens={call.get('prompt_tokens')} num_ctx={call.get('num_ctx')}",
            )
        if (call.get("wall_s") or 0) > SLOW_CALL_S:
            add(
                "slow_call", stage, "info", "system",
                f"{stage} 单次调用 {call.get('wall_s')}s（阈值 {SLOW_CALL_S}s）",
                f"load_s={call.get('load_s')} prompt_tokens={call.get('prompt_tokens')} "
                f"output_tokens={call.get('output_tokens')} prefill={call.get('prefill_tps')} t/s "
                f"gen={call.get('gen_tps')} t/s",
                prefill_tps=call.get("prefill_tps"),
            )
        # 吞吐退化：ollama 服务跑久后会降一个数量级（实测同一 prompt 157 → 6 t/s），
        # 而 /api/ps 仍然报 size_vram == size（假象）。这里按基线抓，并给出"重启服务"的处置建议。
        base = BASELINE_PREFILL.get(str(call.get("tag") or ""), DEFAULT_BASELINE_PREFILL)
        tps = call.get("prefill_tps")
        if tps and tps < base * 0.3:
            add(
                "prefill_degraded", stage, "blocker", "system",
                f"{stage} 的 prefill 只有 {tps} t/s（健康基线约 {base}，不足 30%）",
                f"gen={call.get('gen_tps')} t/s、显存 {call.get('vram_gb')}/{call.get('model_gb')}GB。"
                "实测处置：ollama 服务连续跑数小时后会退化，重启服务即恢复 —— "
                r"powershell -NoProfile -ExecutionPolicy Bypass -File models\start_ollama.ps1，"
                r"然后 python tools\preflight.py 确认",
                prefill_tps=tps,
            )
        ratio = call.get("vram_ratio")
        if ratio is not None and ratio < 0.99:
            add(
                "gpu_partial_offload", stage, "blocker", "system",
                f"{stage} 的 {call.get('tag')} 只有 {ratio:.0%} 在显存里跑",
                f"显存 {call.get('vram_gb')}/{call.get('model_gb')}GB；prefill {call.get('prefill_tps')} t/s。"
                "通常是桌面/远程桌面/IDE/浏览器占走了显存 —— 让模型全量上显存再重跑，否则一次 14B 调用要慢一个数量级",
                vram_ratio=ratio,
            )

    # 2) 事实接地告警。**阻断级**：编造的路径不会自己消失 —— 它会被下游当成
    # 「上游结论里已有的依据」承接下去（真机：assess 编出 pipeline/db/*，方案阶段
    # 接着把「数据库查询结果」写进验收标准）。这类污染必须让人工看到并裁决。
    for warn in state.get("grounding_warnings") or []:
        paths = list(warn.get("paths") or [])
        add(
            "ungrounded_path", str(warn.get("stage") or "?"), "blocker", "model",
            f"{warn.get('stage')} 产出 {len(paths)} 个未接地路径（疑似编造）",
            "、".join(paths[:12]),
            paths=paths[:12],
        )

    # 3) 评审各轮次的结论
    for round_info in state.get("rounds") or []:
        review = round_info.get("review") or {}
        attempt = round_info.get("attempt")
        verdict = round_info.get("verdict") or review.get("verdict")
        if verdict == "rework_architect":
            add(
                "review_rework_architect", "review", "blocker", "model",
                f"第 {attempt} 轮评审判定方案需要返工",
                review.get("summary") or "",
                attempt=attempt,
            )
        for fix in review.get("required_fixes") or []:
            add("review_required_fix", "review", "warn", "model", f"必改项：{fix}", "", attempt=attempt)
        for blocker in review.get("blockers") or []:
            add("review_blocker", "review", "blocker", "model", f"阻断项：{blocker}", "", attempt=attempt)
        # residual_risks 现为 {issue, reason, impact}；这里兼容早期运行的裸字符串。
        # 带上 impact，人工才能判断这条风险要不要拦住交付。
        for risk in review.get("residual_risks") or []:
            if isinstance(risk, dict):
                title = str(risk.get("issue") or "")
                detail = "；".join(
                    part
                    for part in (
                        f"原因：{risk.get('reason')}" if risk.get("reason") else "",
                        f"影响：{risk.get('impact')}" if risk.get("impact") else "",
                    )
                    if part
                )
            else:
                title, detail = str(risk), ""
            add("review_residual_risk", "review", "info", "model", f"残留风险：{title}", detail, attempt=attempt)

    # 3.4) 方案审计（确定性核对：交叉覆盖 / 禁改路径 / task id 规范性）
    plan_audit = artifacts.get("plan_audit") or {}
    if plan_audit.get("uncovered_changes"):
        add(
            "plan_task_uncovered", "architect_plan", "warn", "system",
            f"方案里有 {len(plan_audit['uncovered_changes'])} 个改动文件没被任何任务覆盖",
            "、".join(str(x) for x in plan_audit["uncovered_changes"][:8]),
        )
    if plan_audit.get("dangling_files"):
        add(
            "plan_task_uncovered", "architect_plan", "warn", "system",
            f"方案任务引用了 changes 里没有的文件：{'、'.join(str(x) for x in plan_audit['dangling_files'][:6])}",
        )
    if plan_audit.get("bad_task_ids") or plan_audit.get("duplicate_task_ids"):
        add(
            "plan_task_uncovered", "architect_plan", "warn", "system",
            "方案任务 id 不规范（开发要按它填 covers_tasks，会连带影响覆盖审计）",
            f"不合规 {plan_audit.get('bad_task_ids')}；重复 {plan_audit.get('duplicate_task_ids')}",
        )
    if plan_audit.get("unknown_depends_on"):
        add(
            "plan_task_uncovered", "architect_plan", "warn", "system",
            f"方案 depends_on 引用了不存在的任务 id：{'、'.join(str(x) for x in plan_audit['unknown_depends_on'][:6])}",
        )
    if plan_audit.get("forbidden_touched"):
        add(
            "plan_forbidden_touched", "architect_plan", "blocker", "system",
            f"方案改动了禁改路径：{'、'.join(str(x) for x in plan_audit['forbidden_touched'][:6])}",
            "评估阶段已把这些路径列为 forbidden_paths，方案必须绕开",
        )

    # 3.5) 实现覆盖审计（确定性核对：方案任务有没有被补丁覆盖 / 有没有诚实声明未实现）
    audit = artifacts.get("implementation_audit") or {}
    for task in audit.get("missing") or []:
        add(
            "plan_task_uncovered", "dev", "blocker", "system",
            f"方案任务 `{task}` 没有任何补丁覆盖，也没进 not_implemented",
            "覆盖审计按 plan.tasks[].id 与 edits[].covers_tasks 机械核对",
            tasks=audit.get("missing"),
        )
    if audit.get("empty_implementation"):
        add(
            "implementation_empty", "dev", "blocker", "system",
            "本次实现只有「未实现」声明，没有任何覆盖任务的补丁",
            f"方案任务 {audit.get('task_ids')} 全被声明为未实现（诚实但等于没干活）",
            real_edit_count=audit.get("real_edit_count"),
        )
    for item in audit.get("declared_not_implemented") or []:
        add("task_not_implemented", "dev", "warn", "model", f"未实现：{item}")
    for task in audit.get("unknown_tasks") or []:
        add(
            "fabricated_task_ref", "dev", "warn", "model",
            f"补丁引用了方案里不存在的任务 id `{task}`",
            "covers_tasks 必须是 plan.tasks[].id 里真实存在的值",
        )
    patch_audit = artifacts.get("patch_audit") or {}
    for row in patch_audit.get("edits") or []:
        status = str(row.get("status") or "")
        if status in ("ok", "unchecked", ""):
            continue
        kind = status if status in KINDS else "patch_no_effect"
        # 阻断级别必须与编排器 ``_patch_blockers`` 的口径一致：
        # 那三种 + 新文件的两种（重复定义 / 内容语法错）都是「贴回去必然跑不起来」，
        # 口径不一致会让同一件事在问题记录里被记成 warn，人工按它判断就会误放行。
        add(
            kind, "dev",
            "blocker"
            if status in (
                "patch_incomplete",
                "patch_span_mismatch",
                "anchor_not_found",
                "new_file_duplicate_symbol",
                "new_file_syntax_error",
            )
            else "warn",
            "system",
            f"{row.get('symbol') or row.get('path')}：{KIND_CN.get(kind, kind)}",
            "；".join(row.get("notes") or []) or f"patch_mode={row.get('patch_mode_used') or row.get('patch_mode')}",
            path=row.get("path"),
            anchor_span=row.get("anchor_span"),
            symbol_span=row.get("symbol_span"),
            patch_lines=row.get("patch_lines"),
        )
    for round_info in state.get("rounds") or []:
        if round_info.get("forced_rework"):
            add(
                "review_forced_rework", "review", "warn", "system",
                f"第 {round_info.get('attempt')} 轮评审想给 pass，被补丁校验推翻为 rework_dev",
                "；".join(round_info.get("patch_blockers") or []),
                attempt=round_info.get("attempt"),
            )
        if round_info.get("forced_pass"):
            add(
                "review_forced_pass", "review", "warn", "system",
                f"第 {round_info.get('attempt')} 轮的返工项全属 needs_external，被机制强制放行",
                "；".join(round_info.get("required_fixes_external") or []),
                attempt=round_info.get("attempt"),
            )

    # 4) 阶段产物里的自述缺口。coverage_gaps 现为 {gap, reason, impact} 三元组，
    #    这里兼容早期运行的裸字符串写法。
    for gap in (artifacts.get("test_report") or {}).get("coverage_gaps") or []:
        if isinstance(gap, dict):
            title = str(gap.get("gap") or "")
            detail = "；".join(
                part
                for part in (
                    f"原因：{gap.get('reason')}" if gap.get("reason") else "",
                    f"影响：{gap.get('impact')}" if gap.get("impact") else "",
                )
                if part
            )
        else:
            title, detail = str(gap), ""
        add("test_gap", "test", "warn", "model", f"测试未覆盖：{title}", detail)
    # 语义检查（pyright 类型诊断）：ast 抓不到、执行也常覆盖不到的那类缺陷
    # （属性不存在 / 参数不匹配 / 未被执行到的路径）。只记高置信项，推断性结论噪音大。
    sem = artifacts.get("semantic_audit") or {}
    sem_blocking = [
        d for d in (sem.get("diagnostics") or [])
        if isinstance(d, dict) and d.get("blocking")
    ]
    if sem_blocking:
        add(
            "semantic_error", "dev", "warn", "system",
            f"语义检查发现 {len(sem_blocking)} 个高置信类型问题",
            "；".join(
                f"{d.get('file')}:{d.get('line')} {d.get('message')}"
                for d in sem_blocking[:4]
            ),
        )
    # 测试审计（编排器机械核对）：缺用例类型 = 测试不完整，必须让评审和人工看到，
    # 不能只依赖模型自己在 coverage_gaps 里诚实申报。
    test_audit = artifacts.get("test_audit") or {}
    if test_audit.get("missing_types"):
        add(
            "test_gap", "test", "warn", "system",
            f"测试缺少用例类型：{', '.join(str(t) for t in test_audit['missing_types'])}",
            "编排器机械核对 cases[].type：new / regression / compat 三类缺一不可",
        )
    if test_audit.get("vague_count"):
        add(
            "test_gap", "test", "info", "system",
            f"{test_audit['vague_count']} 条用例的 expected 过于笼统，无法转成断言",
            "；".join(str(x) for x in (test_audit.get("vague_expected") or [])),
        )
    if test_audit.get("missing_symbols"):
        # 「写了很多用例」≠「测到了改动之处」。
        # 已在 coverage_gaps 里交代过原因的算豁免（info）；没交代的属漏测 —— 那条
        # 由 _test_blockers 强制 rework，这里也按 warn 记一笔，便于事后统计。
        unexplained = {str(s) for s in (test_audit.get("missing_unexplained") or [])}
        add(
            "test_gap", "test", "warn" if unexplained else "info", "system",
            f"本次改动的 {len(test_audit['missing_symbols'])} 个符号没出现在任何用例中："
            + "、".join(str(s) for s in test_audit["missing_symbols"]),
            "编排器机械核对：用例 target 要写到符号级，写文件名会与补丁的 target_symbol 对不上；"
            + (
                f"其中 {len(unexplained)} 个未在 coverage_gaps 申诉 → 强制 rework"
                if unexplained
                else "（均已在 coverage_gaps 里交代原因，视为豁免）"
            ),
        )
    scope = artifacts.get("scope") or {}
    for item in scope.get("unknowns") or []:
        add("pm_unknown", "pm", "info", "model", f"需求未决：{item}")
    for item in scope.get("clarifying_questions") or []:
        add("pm_unknown", "pm", "info", "model", f"澄清问题：{item}")
    # 真机教训（2026-09-23）：PM 抛了未决项却不给默认取值，这些裸问题传到下游后
    # 各阶段各自脑补出一版互不一致的答案，方案里因此混进需求根本没提的东西。
    # 有了 assumed_answer 约定后这里做兜底告警，防止该缺陷悄悄回归。
    no_answer = [
        q
        for q in (scope.get("open_questions") or [])
        if isinstance(q, dict) and not str(q.get("assumed_answer") or "").strip()
    ]
    if no_answer:
        titles = [str((q or {}).get("question") or "?")[:60] for q in no_answer[:4]]
        add(
            "pm_unknown",
            "pm",
            "warn",
            "model",
            f"{len(no_answer)} 条 PM 未决项没有默认取值，下游可能各自臆测",
            "；".join(titles),
        )
    # 架构不确定项：现在是结构化的 {issue, assumption, confidence}；
    # 早期运行的产物是裸字符串，这里做兼容，避免老 run 重算时炸掉。
    for item in (artifacts.get("assessment") or {}).get("uncertainties") or []:
        if isinstance(item, dict):
            title = f"架构不确定：{item.get('issue') or ''}"
            confidence = str(item.get("confidence") or "").strip()
            if confidence:
                title += f"（可信度 {confidence}）"
            detail = str(item.get("assumption") or "")
        else:
            title, detail = f"架构不确定：{item}", ""
        add("arch_uncertainty", "architect_assess", "info", "model", title, detail)

    # 5) 人工干预（操作页面/CLI 写入 state.human_actions）
    for action in state.get("human_actions") or []:
        kind = str(action.get("action") or "human_directive")
        kind = kind if kind in KINDS else "human_directive"
        stage = str(action.get("stage") or "?")
        declared = action.get("kind") or ""
        label = KIND_CN.get(declared, declared)
        add(
            kind, stage, "info" if kind == "human_gate" else "warn", "human",
            f"{KIND_CN.get(kind, kind)}：{stage}" + (f"（分类：{label}）" if label else ""),
            action.get("text") or "",
            attempt=action.get("attempt"),
            at=action.get("at"),
            declared_kind=declared,
        )

    # 6) 触顶
    if state.get("needs_human"):
        add("needs_human", "review", "blocker", "system",
            f"回流达到上限（{state.get('max_rework')} 轮）仍需人工裁决",
            f"attempts={state.get('attempt')} cursor={state.get('cursor')}")

    # 复发统计：同 key 再次出现 = 上一轮的处理没解决
    seen: dict[str, int] = {}
    for issue in issues:
        seen[issue.key] = seen.get(issue.key, 0) + 1
        issue.occurrence = seen[issue.key]
        issue.recurred = issue.occurrence > 1

    issues.sort(key=lambda i: (-_SEV_ORDER.get(i.severity, 0), i.stage, i.kind, i.occurrence))
    return issues


_SEV_ORDER = {"blocker": 2, "warn": 1, "info": 0}


# --------------------------------------------------------------------- 落盘
def write_issues(run_dir: Path, run_id: str, issues: list[Issue]) -> None:
    run_dir = Path(run_dir)
    runstore.write_json(run_dir / "issues.json", {
        "run_id": run_id,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summarize(issues),
        "issues": [i.to_dict() for i in issues],
    })
    with (run_dir / "issues.jsonl").open("w", encoding="utf-8") as fh:  # 派生视图：重算即覆盖
        for issue in issues:
            fh.write(json.dumps(issue.to_dict(), ensure_ascii=False) + "\n")
    (run_dir / "issues.md").write_text(issues_markdown(run_id, issues), encoding="utf-8")


def issues_markdown(run_id: str, issues: list[Issue]) -> str:
    stats = summarize(issues)
    lines = [
        f"# 问题记录 — run {run_id}",
        "",
        f"- 合计 {stats['total']} 条：{stats['by_severity']['blocker']} 阻断 / "
        f"{stats['by_severity']['warn']} 警告 / {stats['by_severity']['info']} 提示；其中复发 {stats['recurred']} 条",
        "",
    ]
    if not issues:
        lines.append("（本次运行没有记录到问题）")
        return "\n".join(lines) + "\n"
    for kind, count in stats["by_kind"].items():
        lines += [f"## {KIND_CN.get(kind, kind)} × {count}", ""]
        for issue in [i for i in issues if i.kind == kind]:
            flag = f"（第 {issue.occurrence} 次出现，**复发**）" if issue.recurred else ""
            lines.append(f"- `{issue.stage}` [{issue.severity}] {issue.title}{flag}")
            if issue.detail:
                lines.append(f"  - {issue.detail}")
        lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------- 指纹与环境
def pipeline_fingerprint() -> dict:
    """当前提示词/配置/检索策略的指纹。跨运行对比时必须带上它，否则问题增减无法归因。"""
    from . import prompts, retrieval  # 局部导入，避免环

    models = {
        name: [spec.tag, spec.num_ctx, spec.prompt_token_budget, spec.think, spec.temperature, spec.num_predict]
        for name, spec in STAGE_MODELS.items()
    }
    config_blob = json.dumps(
        {
            "code_budget": CODE_BUDGET,
            "review_every": REVIEW_EVERY,
            "max_rework": MAX_REWORK_ROUNDS,
            "prompt_hard_ratio": PROMPT_HARD_RATIO,
            "models": models,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    # 两套提示词都要进指纹：只算 SYSTEM 的话，改了 SYSTEM_NEW（新建项目那套）指纹不变，
    # 跨运行对比时「问题变多变少」就无法归因到它。
    prompts_blob = json.dumps(
        {"secondary": prompts.SYSTEM, "new": prompts.SYSTEM_NEW},
        ensure_ascii=False,
        sort_keys=True,
    )
    # 检索策略同样决定模型看到什么，必须进指纹（真机教训：改了取片策略后"问题变少"必须能归因到它）
    retrieval_blob = json.dumps(
        {
            "gram_quota": retrieval.GRAM_QUOTA,
            "df_ratio_cutoff": retrieval.DF_RATIO_CUTOFF,
            "max_files_scanned": retrieval.MAX_FILES_SCANNED,
            "ext_weight": retrieval.EXT_WEIGHT,
            "ext_weight_default": retrieval.EXT_WEIGHT_DEFAULT,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "pipeline_hash": hashlib.sha1((config_blob + prompts_blob + retrieval_blob).encode("utf-8")).hexdigest()[:12],
        "prompts_hash": hashlib.sha1(prompts_blob.encode("utf-8")).hexdigest()[:12],
        "config_hash": hashlib.sha1(config_blob.encode("utf-8")).hexdigest()[:12],
        "retrieval_hash": hashlib.sha1(retrieval_blob.encode("utf-8")).hexdigest()[:12],
        "models": {name: spec.tag for name, spec in STAGE_MODELS.items()},
        "review_every": REVIEW_EVERY,
        "max_rework": MAX_REWORK_ROUNDS,
    }


def env_snapshot(repo: str | None = None, host: str | None = None) -> dict:
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "repo": repo,
        "ollama_host": host,
        "fingerprint": pipeline_fingerprint(),
        "prompt_chars": {stage: len(text) for stage, text in system_prompts().items()},
    }


def system_prompts() -> dict[str, str]:
    """当前各阶段的系统提示词（元优化的输入之一）。

    新建项目那套（SYSTEM_NEW）以 `阶段@new` 为键一并返回 —— 否则元优化只看得到
    二开那套，新项目提示词的问题永远沉淀不下来。
    """
    from . import prompts

    out = dict(prompts.SYSTEM)
    out.update({f"{stage}@new": text for stage, text in prompts.SYSTEM_NEW.items()})
    return out


# --------------------------------------------------------------------- 跨运行汇总
def state_of(run_dir: Path) -> tuple[dict, str]:
    """取一次运行的"记录视图"：优先 state.json，本轮改造前的运行退回 summary.json。

    返回 (state, source)，source ∈ {state, summary, none}。
    """
    state = runstore.read_state(run_dir)
    if state:
        return state, "state"
    summary = runstore.read_summary(run_dir)
    if summary:
        return {
            "calls": summary.get("calls") or [],
            "grounding_warnings": summary.get("grounding_warnings") or [],
            "rounds": summary.get("round_details") or [],
            "artifacts": summary.get("artifacts") or {},
            "needs_human": summary.get("needs_human"),
            "attempt": summary.get("attempts"),
            "max_rework": None,
        }, "summary"
    return {}, "none"


def build_report(runs_dir: Path, limit_runs: int = 200) -> dict:
    """扫描 runs/ 汇总问题，作为「元优化」的输入。"""
    runs_dir = Path(runs_dir)
    run_rows: list[dict] = []
    kind_totals: dict[str, int] = {}
    severity_totals = {key: 0 for key in ("info", "warn", "blocker")}
    stage_stats: dict[str, dict] = {}
    fingerprint_rows: dict[str, dict] = {}

    dirs = sorted([d for d in runs_dir.iterdir() if d.is_dir() and not d.name.startswith((".", "_"))])[-limit_runs:]
    for run_dir in dirs:
        state, source = state_of(run_dir)
        if source == "none":
            continue
        issues = collect_issues(state, run_dir.name)
        stats = summarize(issues)
        env = runstore.read_json_if_exists(run_dir / "env.json") or {}
        fp = (env.get("fingerprint") or {}).get("pipeline_hash") or "未知"
        summary = runstore.read_summary(run_dir) or {}

        for kind, count in stats["by_kind"].items():
            kind_totals[kind] = kind_totals.get(kind, 0) + count
        for sev, count in stats["by_severity"].items():
            severity_totals[sev] = severity_totals.get(sev, 0) + count
        for call in state.get("calls") or []:
            row = stage_stats.setdefault(
                str(call.get("stage") or "?"),
                {
                    "calls": 0, "wall_s": 0.0, "prompt_tokens": 0, "output_tokens": 0,
                    "contract_retries": 0, "switches": 0, "prefill_tps": 0.0, "gen_tps": 0.0, "tps_samples": 0,
                },
            )
            row["calls"] += 1
            row["wall_s"] += float(call.get("wall_s") or 0)
            row["prompt_tokens"] += int(call.get("prompt_tokens") or 0)
            row["output_tokens"] += int(call.get("output_tokens") or 0)
            row["contract_retries"] += 1 if int(call.get("attempt") or 1) > 1 else 0
            row["switches"] += 1 if call.get("switched") else 0
            if call.get("prefill_tps"):
                row["prefill_tps"] += float(call["prefill_tps"])
                row["gen_tps"] += float(call.get("gen_tps") or 0)
                row["tps_samples"] += 1
        fp_row = fingerprint_rows.setdefault(fp, {"runs": 0, "issues": 0, "blockers": 0, "pass": 0})
        fp_row["runs"] += 1
        fp_row["issues"] += stats["total"]
        fp_row["blockers"] += stats["blockers"]
        fp_row["pass"] += 1 if summary.get("verdict") == "pass" else 0

        run_rows.append(
            {
                "run_id": run_dir.name,
                "record_source": source,
                "repo": summary.get("repo") or state.get("repo"),
                "status": summary.get("status") or state.get("status"),
                "verdict": summary.get("verdict") or state.get("verdict"),
                "attempts": summary.get("attempts") or state.get("attempt"),
                "wall_s": summary.get("wall_s") or state.get("elapsed_s"),
                "model_switches": summary.get("model_switches") or state.get("model_switches"),
                "fingerprint": fp,
                "issue_summary": stats,
                "top_issues": [i.to_dict() for i in issues[:12]],
            }
        )

    stage_rows = {
        stage: {
            **row,
            "avg_wall_s": round(row["wall_s"] / row["calls"], 1) if row["calls"] else 0,
            "avg_prefill_tps": round(row["prefill_tps"] / row["tps_samples"], 1) if row["tps_samples"] else None,
            "avg_gen_tps": round(row["gen_tps"] / row["tps_samples"], 1) if row["tps_samples"] else None,
        }
        for stage, row in sorted(stage_stats.items())
    }
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "runs_dir": str(runs_dir),
        "runs_analyzed": len(run_rows),
        "totals": {
            "issues": sum(kind_totals.values()),
            "by_kind": dict(sorted(kind_totals.items(), key=lambda kv: (-kv[1], kv[0]))),
            "by_severity": severity_totals,
        },
        "by_stage": stage_rows,
        "by_fingerprint": fingerprint_rows,
        "runs": run_rows,
        "current_fingerprint": pipeline_fingerprint(),
    }


def report_markdown(report: dict) -> str:
    lines = [
        "# 流水线问题总览（跨运行）",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 统计范围：{report['runs_dir']}（{report['runs_analyzed']} 次运行）",
        f"- 当前流水线指纹：`{report['current_fingerprint']['pipeline_hash']}`"
        f"（提示词 `{report['current_fingerprint']['prompts_hash']}`）",
        "",
        "## 问题分布",
        "",
    ]
    if not report["totals"]["by_kind"]:
        lines.append("（没有记录到问题）")
    else:
        lines += ["| 问题类型 | 次数 |", "|---|---|"]
        for kind, count in report["totals"]["by_kind"].items():
            lines.append(f"| {KIND_CN.get(kind, kind)} (`{kind}`) | {count} |")
    lines += [
        "",
        "## 各阶段耗时与契约重试",
        "",
        "| 阶段 | 调用 | 平均耗时 | prompt tok | out tok | prefill t/s | gen t/s | 契约重试 | 切换 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for stage, row in report["by_stage"].items():
        lines.append(
            f"| {stage} | {row['calls']} | {row['avg_wall_s']}s | {row['prompt_tokens']} | "
            f"{row['output_tokens']} | {row.get('avg_prefill_tps') or '-'} | {row.get('avg_gen_tps') or '-'} | "
            f"{row['contract_retries']} | {row['switches']} |"
        )
    lines += [
        "",
        "> 吞吐突然掉一个数量级，先查环境（桌面/远程桌面/IDE/浏览器占显存会让模型被换到共享内存），"
        "再看流水线本身。",
    ]
    lines += ["", "## 按流水线指纹（改了提示词/配置之后问题有没有变少）", "", "| 指纹 | 运行数 | 问题数 | 阻断 | pass |", "|---|---|---|---|---|"]
    for fp, row in report["by_fingerprint"].items():
        lines.append(f"| `{fp}` | {row['runs']} | {row['issues']} | {row['blockers']} | {row['pass']} |")
    lines += ["", "## 最近几次运行", "", "| run | 判定 | 迭代 | 问题 | 阻断 | 指纹 |", "|---|---|---|---|---|---|"]
    for row in report["runs"][-15:]:
        lines.append(
            f"| {row['run_id']} | {row['verdict']} | {row['attempts']} | {row['issue_summary']['total']} | "
            f"{row['issue_summary']['blockers']} | `{row['fingerprint']}` |"
        )
    lines += ["", "> 交给模型做元优化：`python tools\\meta_optimize.py`（只产出建议书，不自动改代码）", ""]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------- 元优化输入
META_PROPOSAL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "diagnosis": {"type": "string"},
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "problem": {"type": "string"},
                    "root_cause": {"type": "string"},
                    "evidence": {"type": "string"},
                    "proposed_change": {"type": "string"},
                    "target": {"type": "string"},
                    "risk": {"type": "string"},
                    "priority": {"type": "integer"},
                },
                "required": ["problem", "root_cause", "evidence", "proposed_change", "target", "risk", "priority"],
            },
        },
        "prompt_edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "stage": {"type": "string"},
                    "issue": {"type": "string"},
                    "proposed_text": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["stage", "issue", "proposed_text", "rationale"],
            },
        },
        "measurement_plan": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["diagnosis", "problems", "prompt_edits", "measurement_plan"],
}

META_SYSTEM = (
    "你是流水线的元优化分析员。给你的是这条「多角色 LLM 串行流水线」的运行记录汇总"
    "（问题分类统计、各阶段耗时、几次运行的问题明细），以及受影响阶段的当前系统提示词。\n"
    "你的任务：从记录中找出**流水线自身**的设计缺陷（提示词、上下文预算、阶段顺序、评审频率、契约约束），"
    "给出可落地的最小改动建议。\n"
    "硬性要求：\n"
    "1. 每条结论必须绑定记录中的证据（引用问题类型、阶段、次数或 run_id），没有证据的猜测写进 unknowns。\n"
    "2. proposed_change / target 必须指向**流水线自身**的文件（如 pipeline/prompts.py 的 SYSTEM['review']、"
    "pipeline/config.py 的 CODE_BUDGET['review']），说明「改什么 + 为什么能减少哪一类问题」；"
    "**不要**把被改造仓库里的文件当成改动对象。\n"
    "3. priority 为 1~3（1 最优先）。不允许给「整体提高质量」这类空话。\n"
    "4. 你只产出**建议书**：不要输出代码补丁、不要假设自己可以修改文件。\n"
    "5. prompt_edits 里 proposed_text 必须是可直接替换的完整英文/中文提示片段（保持原语言风格）。\n"
    "【输出要求】只输出一个符合给定 JSON Schema 的对象，不要 markdown 代码块。"
)


def build_meta_prompt(report: dict, prompts_excerpt: dict[str, str] | None = None, max_runs: int = 8) -> str:
    """把问题汇总压成一份给模型看的材料（14B 只有 8K，必须省着用）。"""
    totals = report.get("totals", {})
    repos = sorted({str(row.get("repo")) for row in (report.get("runs") or []) if row.get("repo")})
    lines = [
        "【你分析的对象】这条流水线被套用在下列仓库上（**它们不是流水线自己的代码**）：",
        *[f"  - {repo}" for repo in repos[:5]],
        "  流水线自身的代码在 pipeline/（prompts.py / config.py / retrieval.py / orchestrator.py …），"
        "因此「改流水线」的建议要指向 pipeline/ 下的文件。",
        "",
        "【问题分布（全部历史运行）】",
        json.dumps(totals.get("by_kind") or {}, ensure_ascii=False),
        f"严重度：{json.dumps(totals.get('by_severity') or {}, ensure_ascii=False)}",
        "",
        "【各阶段耗时/契约表现】",
        json.dumps(report.get("by_stage") or {}, ensure_ascii=False),
        "",
        "【按提示词指纹（不同指纹不可直接比较）】",
        json.dumps(report.get("by_fingerprint") or {}, ensure_ascii=False),
        "",
        f"【最近 {max_runs} 次运行的问题明细】",
    ]
    for row in (report.get("runs") or [])[-max_runs:]:
        lines.append(
            f"- run {row['run_id']} verdict={row['verdict']} attempts={row['attempts']} "
            f"issue总={row['issue_summary']['total']} 阻断={row['issue_summary']['blockers']} "
            f"分类={json.dumps(row['issue_summary']['by_kind'], ensure_ascii=False)} 指纹={row['fingerprint']}"
        )
        for issue in row["top_issues"][:6]:
            flag = f"（复发 x{issue['occurrence']}）" if issue.get("recurred") else ""
            detail = f" :: {issue['detail']}" if issue.get("detail") else ""
            lines.append(f"    * [{issue['kind']}][{issue['stage']}][{issue['severity']}] {issue['title']}{flag}{detail}")
    if prompts_excerpt:
        lines += ["", "【受影响阶段的当前系统提示词（可能被截断）】"]
        for stage, text in prompts_excerpt.items():
            lines.append(f"--- prompts.py SYSTEM['{stage}'] ---")
            lines.append(text)
    lines += [
        "",
        "【任务】按 schema 输出：诊断 → 问题清单（带证据与最小改动建议）→ 提示词改法 → 验证计划。",
    ]
    return "\n".join(lines)
