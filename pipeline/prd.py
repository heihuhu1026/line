"""PRD 渲染（PM 结构化产物 → ``prd.md``）的**单一真源**。

为什么单独拎出来
----------------
这份文档有**两个**产出时机：

1. 流水线持久化（``orchestrator._persist`` → ``_write_prd``）—— 跑的时候随产物刷新；
2. 人工在页面上点「按产物重新生成」—— 此时**没有流水线在跑**，得由服务端自己渲染。

原先渲染逻辑整个长在 ``orchestrator._write_prd`` 里，服务端要用只有两条路：起一个
Orchestrator（重、还要客户端）或另写一份（两份必然走偏）。所以抽成纯函数，两边共用。

第 6 节的语义（真机反馈后的改动）
--------------------------------
原先第 6 节**只渲染** ``open_questions``，且写死一句「以下条目人工**尚未确认**」。后果是
人工在页面上逐条裁决完、点了保存，PRD 里**一个字都没变** —— 仍是「尚未确认」的口吻、
仍是「Q1. 问题 / PM 建议 / 本次默认取值」的问答形态。人回头看文档会以为自己没保存成功，
更糟的是文档与下游实际采纳的结论不一致（下游 ``pm_assumptions_block`` 早已把已裁决项当
**确定结论**注入，见 prompts 里那段注释）。

现在按裁决状态把第 6 节拆成两块：
  * 已裁决 → **陈述式结论**（「问题：结论」），明说下游直接采纳；
  * 未裁决 → 保留问答形态与默认取值，明说下游暂按此推进。

下游真正消费的是 ``scope`` 产物 + ``pm_assumptions_block``，**不是**这份 md（md 是给人读的）。
这里改造是为了让「人看到的」与「下游采纳的」恢复一致。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import runstore

__all__ = ["render", "write", "human_edited"]


def human_edited(run_dir: str | Path) -> bool:
    """人工是否直接改写过 ``prd.md``（留下 ``prd.human`` 标记）。"""
    return (Path(run_dir) / runstore.PRD_HUMAN_FLAG).exists()


def render(
    requirement: str,
    scope: Any,
    run_id: str = "",
    repo: str = "",
    generated_at: str | None = None,
) -> str:
    """把 PM 的结构化产物渲染成 PRD markdown（纯函数，不碰磁盘）。"""
    scope = scope if isinstance(scope, dict) else {}

    def block(title: str, items: list[Any]) -> list[str]:
        if not items:
            return []
        return [f"### {title}", *[f"- {item}" for item in items], ""]

    lines: list[str] = [
        f"# 产品需求文档（PRD）— {scope.get('change_request') or requirement}",
        "",
        f"> run `{run_id}` · 仓库 `{repo or '（未指定）'}` "
        f"· 生成于 {generated_at or time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 1. 背景与目标",
        "",
    ]
    if scope.get("background"):
        lines += ["**背景**", "", str(scope["background"]).strip(), ""]
    if scope.get("goal"):
        lines += ["**目标**", "", str(scope["goal"]).strip(), ""]
    lines += block("目标用户", list(scope.get("target_users") or []))
    if not (scope.get("background") or scope.get("goal") or scope.get("target_users")):
        lines += ["（PM 未输出背景/目标字段，请以附录 A 的需求原文为准）", ""]

    lines += ["## 2. 范围", ""]
    lines += block("本期要做（In Scope）", list(scope.get("in_scope") or []))
    lines += block("本期不做（Out of Scope）", list(scope.get("out_of_scope") or []))

    lines += ["## 3. 功能需求", ""]
    frs = [f for f in (scope.get("functional_requirements") or []) if isinstance(f, dict)]
    if frs:
        for f in frs:
            lines += [
                f"#### {f.get('id') or '-'} · {f.get('title') or '未命名需求'}",
                "",
                f"- 优先级：`{f.get('priority') or '-'}`",
                "",
            ]
            if f.get("description"):
                lines += [str(f["description"]).strip(), ""]
            acc = f.get("acceptance") or []
            if acc:
                lines += ["验收要点：", *[f"{i}. {a}" for i, a in enumerate(acc, 1)], ""]
    else:
        lines += ["（PM 未按 functional_requirements 结构化输出，请参考第 2 节范围条目）", ""]

    ac = list(scope.get("acceptance_criteria") or [])
    lines += ["## 4. 验收标准", ""]
    lines += [f"{i}. {a}" for i, a in enumerate(ac, 1)] or ["（无）"]
    lines += [""]

    lines += ["## 5. 影响分析", ""]
    for it in scope.get("impact_areas") or []:
        if isinstance(it, dict):
            lines.append(f"- **{it.get('area')}**（{it.get('severity')}）：{it.get('impact')}")
    lines += [""]

    lines += _section6(scope)

    legacy_u = list(scope.get("unknowns") or [])
    legacy_q = list(scope.get("clarifying_questions") or [])
    if legacy_u or legacy_q:
        lines += ["## 7. 未决问题（纯文本速览）", ""]
        lines += block("unknowns", legacy_u)
        lines += block("clarifying_questions", legacy_q)

    lines += [
        "---",
        "",
        "## 附录 A：需求原文",
        "",
        "```text",
        str(requirement).strip(),
        "```",
        "",
        "## 附录 B：操作方式",
        "",
        "```powershell",
        f"python -m pipeline.cli --resume {run_id}",
        f'python -m pipeline.cli --resume {run_id} --from pm --feedback "补充或修正"',
        "python -m pipeline.server --port 8787",
        "```",
        "",
    ]
    return "\n".join(lines)


def _section6(scope: dict) -> list[str]:
    """第 6 节：把「已裁决的结论」与「未裁决的默认假设」分开陈述。

    ``flow.GATE_SPECS`` 里 PM 闸门的文案是「确认或改写 prd.md 第 6 节后再继续」，
    所以这一节的编号是**对外承诺**，不能因为分块而改号（只加三级小标题）。
    """
    qs = [q for q in (scope.get("open_questions") or []) if isinstance(q, dict)]
    lines: list[str] = ["## 6. 未决问题与默认假设", ""]
    if not qs:
        lines += ["（PM 未提出未决问题）", ""]
        return lines

    decided = [q for q in qs if str(q.get("final_decision") or "").strip()]
    pending = [q for q in qs if not str(q.get("final_decision") or "").strip()]

    lines += [
        f"> 共 {len(qs)} 条，其中**已裁决 {len(decided)} 条**、待裁决 {len(pending)} 条。",
        "> 「已裁决」是**确定结论**，下游各阶段直接采纳，不得擅自推翻；",
        "> 「待裁决」下游暂按「本次默认取值」推进，要调整请改 scope 里的 `assumed_answer`，",
        "> 或带 `--feedback` 打回 PM 阶段。",
        "",
    ]

    if decided:
        lines += ["### 已裁决（确定结论，下游直接采纳）", ""]
        for q in decided:
            lines += [
                f"- **{q.get('question')}**：{q.get('final_decision')}",
                *(
                    [f"  - PM 原本的默认取值：{q['assumed_answer']}"]
                    if q.get("assumed_answer")
                    and str(q["assumed_answer"]).strip() != str(q.get("final_decision")).strip()
                    else []
                ),
            ]
        lines += [""]

    if pending:
        lines += ["### 待裁决（下游暂按默认取值推进）", ""]
        for i, q in enumerate(pending, 1):
            lines += [f"#### Q{i}. {q.get('question')}", ""]
            if q.get("why_it_matters"):
                lines += [f"- **为何重要**：{q['why_it_matters']}"]
            if q.get("recommendation"):
                lines += [f"- **PM 建议**：{q['recommendation']}"]
            lines += [f"- **本次默认取值**：{q.get('assumed_answer') or '（未给出）'}"]
            if q.get("impact_if_wrong"):
                lines += [f"- **猜错的代价**：{q['impact_if_wrong']}"]
            if q.get("severity"):
                lines += [f"- **严重度**：`{q['severity']}`"]
            lines += [""]
    return lines


def write(
    run_dir: str | Path,
    requirement: str,
    scope: Any,
    run_id: str = "",
    repo: str = "",
    force: bool = False,
) -> str | None:
    """渲染并落盘 ``prd.md``，返回写入的文本；没写则返回 ``None``。

    与既有行为一致：人工改写过的（存在 ``prd.human`` 标记）**不覆盖** —— 否则人工一次
    改动就被下一次持久化冲掉。``force=True`` 用于「人工明确点了『按产物重新生成』」——
    那一刻覆盖是**被要求的**，不是意外，由调用方（server）先删标记再传 force。
    """
    run_dir = Path(run_dir)
    if not force and human_edited(run_dir):
        return None
    if not isinstance(scope, dict) or not scope:
        return None
    text = render(requirement, scope, run_id=run_id, repo=repo)
    (run_dir / runstore.PRD_NAME).write_text(text, encoding="utf-8")
    return text


def decided_pending_count(scope: Any) -> tuple[int, int]:
    """返回 ``(已裁决条数, 待裁决条数)``，供接口回给页面显示。"""
    qs = [q for q in ((scope or {}).get("open_questions") or []) if isinstance(q, dict)]
    decided = sum(1 for q in qs if str(q.get("final_decision") or "").strip())
    return decided, len(qs) - decided
