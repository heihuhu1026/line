"""交付控制塔的**后端派生视图**（P1 §32）。

为什么要有它：前端（``console.html``）不应该自己解析 ``state.ontology`` 这类内部结构
—— 那是"UI 自行猜测语义"，一旦 state 结构演进，页面会静默显示错的东西。
这里把机械结论整理成**前端可以直接渲染**的形状，前端只消费 ``control_plane``。

**硬性边界**：本模块**只做展示聚合，不做裁决**。
能不能放行一律以 :func:`ontology.build_release_decision` 的 ``can_pass`` 为准，
这里只是把它搬到页面上（并把"语义评审 PASS"与"机器放行"分开显示 —— 这是本 UI
最重要的认知改进：LLM Review = PASS 不等于 Pipeline = PASS）。

纯函数、零模型、对老 run 缺字段一律返回中性值（不崩、不猜）。
"""
from __future__ import annotations

from typing import Any

from . import ontology


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return [x for x in value or [] if x is not None] if isinstance(value, (list, tuple)) else []


def _evidence_rows(graph: Any) -> list[dict]:
    """从 ontology 图里取出 Evidence 行（**按结构探测，探不到就返回空**）。

    前端要回答"这条证据证明了什么" —— 但前提是后端已经绑定好；
    这里只读绑定结果，**不在 UI/视图层做任何推理**。
    """
    objects = _dict(graph).get("objects")
    rows: list[dict] = []
    for obj in _list(objects):
        if not isinstance(obj, dict):
            continue
        if str(obj.get("type") or "") != ontology.TYPE_EVIDENCE:
            continue
        payload = _dict(obj.get("payload"))
        rows.append({
            "id": str(obj.get("id") or ""),
            "truth": str(obj.get("truth") or ""),
            "status": str(payload.get("status") or ""),
            "kind": str(payload.get("kind") or ""),
            "source": str(payload.get("source") or ""),
            "command": str(payload.get("command") or ""),
            "exit_code": payload.get("exit_code"),
            "target_po": _list(payload.get("target_po")),
            "workspace_revision": str(payload.get("workspace_revision") or ""),
            "artifact": str(payload.get("artifact") or ""),
            "created_at": str(payload.get("created_at") or ""),
        })
    return rows


def _proof_rows(proof_gate: dict, graph: Any) -> list[dict]:
    """证明义务矩阵（§22）：每行一个 PO —— 状态 / 需求 / 声明 / 场景 / 证据 / 工作区。"""
    rows: list[dict] = []
    for item in _list(proof_gate.get("obligations")):
        if not isinstance(item, dict):
            continue
        rows.append({
            "id": str(item.get("id") or ""),
            "status": str(item.get("status") or ontology.PO_STATUS_UNPROVEN),
            "requirement": str(item.get("requirement") or ""),
            "claim": str(item.get("claim") or item.get("name") or ""),
            "kind": str(item.get("kind") or ""),
            "task": str(item.get("task") or ""),
            "scenario": str(item.get("scenario") or ""),
            "evidence": _list(item.get("evidence")),
            "workspace_revision": str(item.get("workspace_revision") or ""),
            "required": bool(item.get("required", True)),
        })
    if rows:
        return rows
    # 退回：从 ontology 图里直接读 ProofObligation 对象（结构探测）
    for obj in _list(_dict(graph).get("objects")):
        if not isinstance(obj, dict):
            continue
        if str(obj.get("type") or "") != ontology.TYPE_PROOF_OBLIGATION:
            continue
        payload = _dict(obj.get("payload"))
        rows.append({
            "id": str(obj.get("id") or ""),
            "status": str(payload.get("status") or ontology.PO_STATUS_UNPROVEN),
            "requirement": str(payload.get("requirement") or ""),
            "claim": str(payload.get("claim") or ""),
            "kind": str(payload.get("kind") or ""),
            "task": "", "scenario": "",
            "evidence": _list(payload.get("evidence")),
            "workspace_revision": str(payload.get("workspace_revision") or ""),
            "required": bool(payload.get("required", True)),
        })
    return rows


def build_control_plane(detail: Any) -> dict:
    """把一次运行的机械结论聚合成**交付控制塔**视图（纯函数，不裁决）。

    参数
    ----
    detail
        ``server._detail()`` 产出的运行详情（含 ``state`` / ``issues`` 等）。

    返回
    ----
    dict，含 ``release_gate`` / ``proof_summary`` / ``proofs`` / ``evidence_summary`` /
    ``evidence`` / ``ontology`` / ``workspace`` / ``tasks`` / ``test`` / ``decision`` /
    ``issues`` / ``next_action``。缺数据的段一律给中性值（不猜、不崩）。
    """
    detail = _dict(detail)
    state = _dict(detail.get("state"))
    graph = state.get("ontology")
    release = _dict(state.get("release_gate"))
    proof_gate = _dict(state.get("proof_gate"))

    # ---------------------------------------------------------------- 放行闸门
    # 裁决权不在本模块：state 里已有就直接用；没有就按可拿到的输入确定性重算一次。
    gate = {
        "can_pass": bool(release.get("can_pass")),
        "status": str(release.get("status") or proof_gate.get("status") or "UNPROVEN"),
        "semantic_verdict": str(release.get("semantic_verdict") or state.get("verdict") or ""),
        "proof_status": str(release.get("proof_status") or proof_gate.get("status") or "UNPROVEN"),
        "ontology_status": str(release.get("ontology_status") or "VALID"),
        "workspace_status": str(release.get("workspace_status") or "UNVERIFIED"),
        "verified_revision": str(release.get("verified_revision") or ""),
        "blocking_reasons": _list(release.get("blocking_reasons")),
    }

    # ---------------------------------------------------------------- 证明矩阵
    proofs = _proof_rows(proof_gate, graph)
    required = [p for p in proofs if p.get("required")]
    by_status = {
        "PROVEN": [p for p in required if p["status"] == ontology.PO_STATUS_PROVEN],
        "FAILED": [p for p in required if p["status"] == ontology.PO_STATUS_FAILED],
        "UNPROVEN": [p for p in required if p["status"] == ontology.PO_STATUS_UNPROVEN],
    }
    proof_summary = {
        "required": len(required),
        "covered": len(by_status["PROVEN"]),
        "failed": len(by_status["FAILED"]),
        "unproven": len(by_status["UNPROVEN"]),
        # 「弱证据」= 有场景但只有 rc=0 —— 由 TestCompiler 判定后带出，这里只搬运
        "weak": int(proof_gate.get("weak_count") or 0),
        "unexecutable": int(proof_gate.get("unexecutable_count") or 0),
    }

    # ---------------------------------------------------------------- 证据链
    evidence = _evidence_rows(graph)
    evidence_summary = {
        "total": len(evidence),
        "stale": len([e for e in evidence if str(e.get("status") or "") == "stale"]),
        "by_kind": {
            kind: len([e for e in evidence if e.get("kind") == kind])
            for kind in sorted({str(e.get("kind") or "") for e in evidence if e.get("kind")})
        },
    }

    # ---------------------------------------------------------------- 语义完整性
    onto_problems = _list(state.get("ontology_problems_structured"))
    ontology_view = {
        "status": "BLOCKED" if onto_problems else str(release.get("ontology_status") or "VALID"),
        "errors": onto_problems[:50],
        "error_count": len(onto_problems),
        "revision": str(state.get("ontology_revision") or ""),
    }

    # ---------------------------------------------------------------- 工作区
    workspace = {
        "verified_revision": gate["verified_revision"],
        "status": gate["workspace_status"],
        "chain": _list(_dict(state.get("workspace_chain")).get("revisions")),
    }

    # ---------------------------------------------------------------- 任务 / 方案
    tasks: list[dict] = []
    for task in _list(state.get("plan_compiled_tasks")):
        if not isinstance(task, dict):
            continue
        tasks.append({
            "id": str(task.get("id") or ""),
            "semantic_task_id": str(task.get("semantic_task_id") or ""),
            "task_revision": task.get("task_revision"),
            "target_files": _list(task.get("target_files")),
            "symbols": _list(task.get("symbols")),
            "creates_file": bool(task.get("creates_file")),
            "implements_requirements": _list(task.get("implements_requirements")),
            "candidate_requirement_ids": _list(task.get("candidate_requirement_ids")),
        })

    # ---------------------------------------------------------------- 测试编译
    test = {
        "required": proof_summary["required"],
        "covered": proof_summary["covered"],
        "weak": proof_summary["weak"],
        "missing": proof_summary["unproven"],
        "unexecutable": proof_summary["unexecutable"],
        # 前端必须**区分**「LLM 候选命令」与「Compiler 最终执行命令」：
        # 不允许把候选显示成"已执行"。
        "unbound_commands": _list(proof_gate.get("unbound_commands")),
        "unsafe_commands": _list(proof_gate.get("unsafe_commands")),
    }

    # ---------------------------------------------------------------- 放行依据
    decision = {
        "verdict": str(release.get("verdict") or state.get("verdict") or ""),
        "can_pass": gate["can_pass"],
        "decision_id": str(release.get("decision_id") or ""),
        "evidence_ids": _list(release.get("evidence_ids")),
        "defect_ids": _list(release.get("defect_ids")),
        "semantic_review_is_candidate_only": True,
    }

    return {
        "version": 1,
        "release_gate": gate,
        "proof_summary": proof_summary,
        "proofs": proofs,
        "evidence_summary": evidence_summary,
        "evidence": evidence,
        "ontology": ontology_view,
        "workspace": workspace,
        "tasks": tasks,
        "test": test,
        "decision": decision,
        "issues": _dict(detail.get("issue_summary")),
        "next_action": {
            "text": str(release.get("next_action") or ""),
            "route": str(release.get("route") or ""),
        },
        # 本轮（A–F）新增的机械信号，一并透出便于页面与排查
        "requirement_contract": _dict(state.get("requirement_contract")),
        "file_owners": _dict(state.get("plan_file_owners")),
        "symbol_collisions": _list(state.get("symbol_collisions")),
        "plan_lint_warnings": _list(state.get("plan_lint_warnings")),
    }


def build_live(detail: Any) -> dict:
    """运行中轮询的**轻量**视图（P1 §33）：只回状态数字，不回整个 detail。

    前端每 2.5s 拉这个；只有阶段/状态变化、结束、人工操作才重新拉完整详情。
    """
    detail = _dict(detail)
    state = _dict(detail.get("state"))
    control = build_control_plane(detail)
    return {
        "running": bool(detail.get("running")),
        "status": str(state.get("status") or ""),
        "cursor": str(state.get("cursor") or ""),
        "attempt": state.get("attempt"),
        "needs_human": bool(state.get("needs_human")),
        "verdict": str(state.get("verdict") or ""),
        "proof_status": control["release_gate"]["proof_status"],
        "proof_counts": _dict(control["proof_summary"]),
        "ontology_error_count": int(control["ontology"]["error_count"]),
        "workspace_revision": control["release_gate"]["verified_revision"],
        "can_pass": bool(control["release_gate"]["can_pass"]),
    }
