"""交付证据表：把「PM 验收标准 ↔ 测试用例 ↔ 实际执行结果 ↔ 红线/未验证项」摆到一起。

**为什么需要它**

人工验收拿到的材料是**散的**：验收标准在 PM 的产物里、用例在 test 的产物里、真实执行的
命令与结果在 verify 的报告里、红线与"没验什么"又各在一处。人要在几分钟内判断"这份交付
能不能收"，最需要的是一条主线一行地看过去 —— 而现在只能自己在几份 JSON 之间来回对照。

**刻意只做对齐、不做评分**（评分交给 `gateway.job_readiness`：它看整体）。
这里负责指出**哪条验收标准没有任何用例对上**、**哪些命令没跑过**、**哪些东西根本没验** ——
这些才是要人拍板的地方。

**诚实边界**：验收标准 ↔ 用例的匹配是**启发式**（关键词重合），不是语义理解。
所以输出里必须标明这一点，并且**只能用来提示"可能没人验"，不能用来判定"没验"**。
"""
from __future__ import annotations

import re
import time
from typing import Any, Iterable

from . import ontology

#: 判定"这条用例对着这条验收标准"的两个条件：重合词数 ≥ MIN 且 **覆盖率 ≥ RATIO**。
#:
#: 为什么不能只看绝对重合数（真机实测踩到）：绝对计数会把**巧合**判成覆盖 ——
#: 「空结果集导出时给出提示」与「导出当前筛选结果的文件」共享「结果」二字，
#: 计数就够 2 了，于是"没人验的标准"被报成"已覆盖"。改成比值后（2/10 = 0.2）判为未覆盖，
#: 而真正对上的那条（7/12 = 0.58）照旧命中。
KEY_MIN_OVERLAP = 2
KEY_MIN_RATIO = 0.34
_ASCII_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _keywords(text: Any) -> set[str]:
    """抽关键词：中文按二字组，英文/标识符按整词。与 gateway 的拆词口径同源。"""
    raw = str(text or "")
    words = {w.lower() for w in _ASCII_WORD.findall(raw)}
    cjk = re.findall(r"[\u4e00-\u9fff]+", raw)
    for chunk in cjk:
        for i in range(len(chunk) - 1):
            words.add(chunk[i : i + 2])
        if len(chunk) == 1:
            words.add(chunk)
    return words


def _case_text(case: dict[str, Any]) -> str:
    parts = [str(case.get(k) or "") for k in ("id", "type", "target", "expected")]
    parts += [str(s) for s in (case.get("steps") or []) if isinstance(s, (str, int, float))]
    return " ".join(parts)


def acceptance_rows(scope: Any, test_report: Any) -> list[dict[str, Any]]:
    """逐条验收标准，找出**可能**对上的用例（启发式，见模块 docstring 的诚实边界）。"""
    scope = scope if isinstance(scope, dict) else {}
    criteria: list[str] = [str(x).strip() for x in (scope.get("acceptance_criteria") or []) if str(x).strip()]
    # 功能需求自带的 acceptance 也是验收依据（PM 可能在两处都写了）
    for req in scope.get("functional_requirements") or []:
        if isinstance(req, dict):
            criteria += [
                f"{req.get('title') or req.get('id') or '?'}：{str(x).strip()}"
                for x in (req.get("acceptance") or [])
                if str(x).strip()
            ]
    cases = [c for c in ((test_report or {}).get("cases") or []) if isinstance(c, dict)]
    case_keys = [_keywords(_case_text(c)) for c in cases]
    rows: list[dict[str, Any]] = []
    for text in criteria:
        keys = _keywords(text)
        hits: list[str] = []
        for case, other in zip(cases, case_keys, strict=False):
            shared = keys & other
            if len(shared) < KEY_MIN_OVERLAP:
                continue
            ratio = len(shared) / max(1, min(len(keys), len(other)))
            if ratio >= KEY_MIN_RATIO:
                hits.append(str(case.get("id") or "?"))
        rows.append({"criterion": text, "cases": hits, "covered": bool(hits)})
    return rows


def unexecuted_commands(verify_report: Any) -> list[dict[str, Any]]:
    """没有成功跑过的命令（fail / timeout / error / unavailable / skipped）。"""
    out: list[dict[str, Any]] = []
    for cmd in ((verify_report or {}).get("commands") or []):
        if not isinstance(cmd, dict):
            continue
        status = str(cmd.get("status") or "")
        if status and status != "ok":
            out.append({
                "command": str(cmd.get("command") or ""),
                "status": status,
                "reason": str(cmd.get("reason") or ""),
            })
    return out


def delivery_evidence(
    scope: Any,
    test_report: Any,
    verify_report: Any,
    rule_findings: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """一次运行的交付证据（人审材料的最小集合）。"""
    rows = acceptance_rows(scope, test_report)
    findings = [f for f in (rule_findings or []) if isinstance(f, dict) and not f.get("note")]
    verify_report = verify_report if isinstance(verify_report, dict) else {}
    control = verify_report.get("negative_control") or {}
    return {
        "criteria": rows,
        "uncovered": [r["criterion"] for r in rows if not r["covered"]],
        "covered_count": sum(1 for r in rows if r["covered"]),
        "criteria_count": len(rows),
        "unexecuted": unexecuted_commands(verify_report),
        "unverified": [str(x) for x in (verify_report.get("unverified") or [])],
        "coverage": verify_report.get("coverage") or {},
        "no_power": [str(x) for x in (control.get("no_power") or [])],
        "redlines": [
            {
                "severity": str(f.get("severity") or ""),
                "title": str(f.get("title") or f.get("rule") or ""),
                "where": f"{f.get('path')}:{f.get('line')}",
                "negative": str(f.get("negative") or ""),
            }
            for f in findings
        ],
        # 匹配是启发式：写进产物里，免得有人拿它当"已核对"的证据
        "match_note": "验收标准 ↔ 用例的匹配为关键词启发式（重合 ≥2 个词），仅供人工定位，不作为判定依据",
    }


# ----------------------------------------------------------------- Ontology Evidence（规格§三十二）
#: 与 ontology.EVIDENCE_KINDS 同一份口径（机械/执行/规则类可 PROVEN；human_confirmation 只能 ASSERTED）。
def make_evidence(
    kind: str,
    source: str,
    *,
    status: str = ontology.PO_STATUS_UNPROVEN,
    claim_ids: Iterable[str] | None = None,
    proof_obligation_ids: Iterable[str] | None = None,
    workspace_revision: str = "",
    artifact_revision: str = "",
    command: str = "",
    exit_code: int | None = None,
    stdout_hash: str = "",
    stderr_hash: str = "",
    digest: str = "",
    created_at: str = "",
    truth: str | None = None,
    evidence_id: str = "",
) -> dict[str, Any]:
    """构造一条与 ``ontology.EvidenceRecord`` 同形的证据 dict（纯函数，可直接 JSON 化）。

    真值纪律在这里机械落实，调用方不能靠措辞抬级：

      * ``human_confirmation`` 永远是 ``ASSERTED``（规格§四十六：人工确认不是 PROVEN）；
      * 其余证据默认 ``DERIVED``；只有真实命令/机械检查的调用方才允许显式传
        ``truth=PROVEN``（且 ``ontology_validate`` 还会复核 command/exit_code/checker）。
    """
    if kind == "human_confirmation":
        truth = ontology.TRUTH_ASSERTED
    elif truth is None:
        truth = ontology.TRUTH_DERIVED
    if not evidence_id:
        evidence_id = "ev:" + ontology.stable_hash(
            [kind, source, command, workspace_revision, digest], length=12
        )
    record = ontology.EvidenceRecord(
        id=evidence_id,
        kind=str(kind or ""),
        source=str(source or ""),
        status=str(status or ontology.PO_STATUS_UNPROVEN),
        claim_ids=[str(x) for x in (claim_ids or [])],
        proof_obligation_ids=[str(x) for x in (proof_obligation_ids or [])],
        workspace_revision=str(workspace_revision or ""),
        artifact_revision=str(artifact_revision or ""),
        command=str(command or ""),
        exit_code=exit_code,
        stdout_hash=str(stdout_hash or ""),
        stderr_hash=str(stderr_hash or ""),
        digest=str(digest or ""),
        created_at=str(created_at or time.strftime("%Y-%m-%d %H:%M:%S")),
        truth=truth,
    )
    return record.to_dict()


def render_markdown(ev: dict[str, Any], *, limit: int = 12) -> str:
    """渲染成给**人**读的 markdown（handoff / 作业验收都用它，保证口径一致）。"""
    lines: list[str] = []
    total = int(ev.get("criteria_count") or 0)
    if total:
        lines.append(
            f"### 验收标准覆盖（{ev.get('covered_count', 0)}/{total} 条能对上用例）"
        )
        for row in ev["criteria"][:limit]:
            mark = "✅" if row["covered"] else "⚠️"
            cases = "、".join(row["cases"][:4]) or "**没有任何用例对上**"
            lines.append(f"- {mark} {row['criterion'][:120]} → {cases}")
        if total > limit:
            lines.append(f"- （另有 {total - limit} 条，见 issues.json / 产物）")
        lines.append(f"> {ev.get('match_note', '')}")
    coverage = ev.get("coverage") or {}
    if coverage.get("percent") is not None:
        lines.append(f"### 覆盖率\n- 实测 {coverage['percent']:g}%（来自 `{coverage.get('command')}`）")
    if ev.get("unexecuted"):
        lines.append("### 没有成功跑过的命令（这些不代表已验过）")
        for row in ev["unexecuted"][:limit]:
            lines.append(f"- [{row['status']}] `{row['command']}` {row['reason'][:80]}")
    if ev.get("no_power"):
        lines.append("### 负向对照：撤掉改动后依然通过的断言（对本次交付没有判别力）")
        lines += [f"- `{c}`" for c in ev["no_power"][:4]]
    if ev.get("unverified"):
        lines.append("### 未验证项（pass ≠ 该验的都验了）")
        lines += [f"- {x}" for x in ev["unverified"][:limit]]
    if ev.get("redlines"):
        lines.append("### 工程红线")
        for row in ev["redlines"][:limit]:
            tag = "阻断" if row["severity"] == "blocker" else "提示"
            lines.append(f"- [{tag}] {row['title']} @ {row['where']}")
            if row["negative"]:
                lines.append(f"  - 反例判据：{row['negative']}")
    return "\n".join(lines)
