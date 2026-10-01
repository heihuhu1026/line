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


def _scenario_rows(compiled: dict) -> list[dict]:
    """§27：TestCompiler 场景行 —— 前端据此区分「LLM 候选命令」与「Compiler 最终命令」。

    `commands` 只含**编译器认可**的（安全且非机械场景）；声称了归属却证不成的命令在
    顶层 `unbound_commands` 里单列，UI **不得**把它显示成"已执行"。
    `verification_mode` 是 P0-13 的分类（mechanical / unit / gui_smoke / resident / human_only）。
    """
    rows: list[dict] = []
    for sc in _list(compiled.get("scenarios")):
        if not isinstance(sc, dict):
            continue
        assertions: list[str] = []
        for act in _list(sc.get("actions")):
            if isinstance(act, dict):
                assertions.extend(str(x) for x in _list(act.get("assertions")))
        rows.append({
            "id": str(sc.get("id") or ""),
            "target_po": str(sc.get("target_po") or ""),
            "kind": str(sc.get("kind") or ""),
            "title": str(sc.get("title") or ""),
            "status": str(sc.get("status") or ""),
            "gap_reason": str(sc.get("gap_reason") or ""),
            "verification_mode": str(sc.get("verification_mode") or ""),
            "mechanical_check": _list(sc.get("mechanical_check")),
            "commands": _list(sc.get("automated_commands")),
            "assertions": assertions,
        })
    return rows


def _attach_proof_details(
    rows: list[dict], compiled: dict, evidence: list[dict], verify_report: dict,
) -> None:
    """§22.1：给每条 PO 挂上**后端已绑定**的场景/命令/证据明细（UI 只渲染，不推理）。

    为什么在后端做 join：前端按 id 自行拼装就等于"UI 猜语义"（§32 的硬边界）。
    命令的退出码与 stdout/stderr 取 ``verify_report.commands``（**真实执行痕迹**），
    而不是让前端去猜哪条命令跑过。
    """
    scenarios = {
        str(s.get("id")): s for s in _list(compiled.get("scenarios")) if isinstance(s, dict)
    }
    by_cmd: dict[str, dict] = {}
    for cmd in _list(_dict(verify_report).get("commands")):
        if isinstance(cmd, dict):
            by_cmd.setdefault(str(cmd.get("command") or ""), cmd)
    by_ev = {str(e.get("id")): e for e in evidence}
    for row in rows:
        scenario = scenarios.get(str(row.get("scenario") or ""))
        row["scenario_detail"] = scenario or {}
        row["command_details"] = [
            {
                "command": str(c),
                "status": str((by_cmd.get(str(c)) or {}).get("status") or ""),
                "exit_code": (by_cmd.get(str(c)) or {}).get("exit_code"),
                "stdout_tail": str((by_cmd.get(str(c)) or {}).get("stdout_tail") or "")[-800:],
                "stderr_tail": str((by_cmd.get(str(c)) or {}).get("stderr_tail") or "")[-800:],
            }
            # 原始编译产物里叫 automated_commands（方案§三十一）；兼容已归一化的 commands
            for c in _list((scenario or {}).get("automated_commands")
                           or (scenario or {}).get("commands"))
        ]
        row["evidence_detail"] = [
            by_ev[e] for e in _list(row.get("evidence")) if e in by_ev
        ]


def _plan_diff_view(state: dict) -> dict:
    """§26 方案 → 编译任务 的对照视图（纯聚合，不裁决）。

    人需要回答的是：**为什么架构师的 3 张图最后变成 1 张执行图**。
    这里用**符号集合**做确定性对账（不猜）：

      * ``draft`` / ``compiled``：两侧的图（id / symbols / 文件）；
      * ``files[*].kind``：``merged``（多张 draft 合成 1 张 compiled）、
        ``split``（1 张 draft 被拆成多张）、``one_to_one``；
      * ``create_owner`` / ``modify_tasks``：文件创建租约（谁有整份新建权、谁只能定点增补）；
      * ``reasons``：编译器与架构师原图的口径偏差（审计信号）。
    """
    draft = [t for t in _list(state.get("plan_draft_tasks")) if isinstance(t, dict)]
    compiled = [t for t in _list(state.get("plan_compiled_tasks")) if isinstance(t, dict)]

    def _row(task: dict, side: str) -> dict:
        return {
            "id": str(task.get("id") or ""),
            "side": side,
            "files": _list(task.get("target_files")),
            "symbols": [str(s) for s in _list(task.get("symbols"))],
            "creates_file": bool(task.get("creates_file")),
            "task_revision": task.get("task_revision"),
            "supersedes": _list(task.get("supersedes")),
            "requirements": _list(task.get("implements_requirements")),
            "candidates": _list(task.get("candidate_requirement_ids")),
        }

    draft_rows = [_row(t, "draft") for t in draft]
    compiled_rows = [_row(t, "compiled") for t in compiled]
    files: dict[str, dict] = {}
    for row in draft_rows + compiled_rows:
        for path in row["files"]:
            slot = files.setdefault(str(path), {
                "file": str(path), "draft_ids": [], "compiled_ids": [],
                "symbols": [], "creates_file": False,
            })
            key = "draft_ids" if row["side"] == "draft" else "compiled_ids"
            if row["id"] and row["id"] not in slot[key]:
                slot[key].append(row["id"])
            for sym in row["symbols"]:
                if sym not in slot["symbols"]:
                    slot["symbols"].append(sym)
            if row["side"] == "compiled" and row["creates_file"]:
                slot["creates_file"] = True
    owners = _dict(state.get("plan_file_owners"))
    for path, slot in files.items():
        owner = _dict(owners.get(path))
        slot["create_owner"] = str(owner.get("create_owner") or "")
        slot["modify_tasks"] = _list(owner.get("modify_tasks"))
        nd, nc = len(slot["draft_ids"]), len(slot["compiled_ids"])
        slot["kind"] = ("merged" if nd > 1 and nc == 1
                        else "split" if nd == 1 and nc > 1
                        else "one_to_one" if nd and nc else "unmapped")
    return {
        "draft": draft_rows,
        "compiled": compiled_rows,
        "files": [files[k] for k in sorted(files)],
        "reasons": _list(state.get("plan_compiled_reasons")),
    }


def _truth_view(state: dict) -> dict:
    """§29 / §16.3：每条重要事实的**真值来源**（ASSERTED / DERIVED / HUMAN / CONTRADICTED）。

    最关键的一条认知：**默认假设必须显示成"默认假设"**，不能被读成"用户明确要求"。
    所以 `derived` 一栏来自契约的 `derived_facts`（Intake 补充/管线默认值），
    与 `asserted`（逐字来自用户原文、带 source_quote）分开呈现。
    """
    contract = _dict(state.get("requirement_contract"))
    asserted: list[dict] = []
    bucket_labels = {
        "declared_files": "用户声明文件",
        "hard_constraints": "硬约束",
        "entry_files": "运行入口",
        "acceptance_items": "用户验收条件",
        "explicit_exclusions": "明确排除",
        "behavior_claims": "行为断言",
    }
    for bucket, label in bucket_labels.items():
        for item in _list(contract.get(bucket)):
            if not isinstance(item, dict):
                continue
            asserted.append({
                "bucket": bucket,
                "label": label,
                "text": str(item.get("text") or item.get("path") or ""),
                "truth": str(item.get("truth") or "ASSERTED"),
                "source_quote": str(item.get("source_quote") or ""),
                "mechanical_check": _list(item.get("mechanical_check")),
            })
    derived = []
    for item in _list(contract.get("derived_facts")):
        if isinstance(item, dict):
            derived.append({
                "text": str(item.get("text") or item.get("key") or ""),
                "source": str(item.get("source") or "intake"),
                "truth": "DERIVED",
            })
        elif str(item or "").strip():
            derived.append({"text": str(item), "source": "intake", "truth": "DERIVED"})
    # 人工裁决 = HUMAN CONFIRMED（真值高于任何模型推导）
    human = []
    for item in _list(state.get("intake_decisions")):
        if isinstance(item, dict) and item.get("decision"):
            human.append({"scope": "intake", "decision": str(item.get("decision") or ""),
                          "subject": str(item.get("id") or item.get("question") or "")[:120]})
    for item in _list(state.get("pm_decisions")):
        if isinstance(item, dict) and item.get("decision"):
            human.append({"scope": "pm", "decision": str(item.get("decision") or ""),
                          "subject": str(item.get("id") or item.get("question") or "")[:120]})
    contradicted = [
        {"code": str(p.get("code") or ""), "message": str(p.get("message") or ""),
         "object": str(p.get("object") or "")}
        for p in _list(state.get("ontology_problems_structured"))
        if isinstance(p, dict)
    ]
    return {
        "asserted": asserted,
        "derived": derived,
        "human": human,
        "contradicted": contradicted,
        "grounding_errors": _list(contract.get("grounding_errors")),
        "constraint_checks": _list(state.get("constraint_checks")),
        "version": contract.get("version"),
    }


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
        # §30 证明链的首节点（Requirement → PO → …）需要需求条数：从语义图里直接数
        # （只读结构，不做推理）。
        "requirement_count": len([
            o for o in _list(_dict(graph).get("objects"))
            if isinstance(o, dict) and str(o.get("type") or "") == ontology.TYPE_REQUIREMENT
        ]),
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
    compiled = _dict(state.get("test_scenarios"))
    modes = _dict(compiled.get("verification_modes"))
    test = {
        "required": proof_summary["required"],
        "covered": proof_summary["covered"],
        "weak": proof_summary["weak"],
        "missing": proof_summary["unproven"],
        "unexecutable": proof_summary["unexecutable"],
        # 前端必须**区分**「LLM 候选命令」与「Compiler 最终执行命令」：
        # 不允许把候选显示成"已执行"。优先读 TestCompiler 的编译产物（含区分信息），
        # 老 run 没有它时退回 Proof Gate 的同名字段（向后兼容）。
        "unbound_commands": _list(compiled.get("unbound_commands"))
        or _list(proof_gate.get("unbound_commands")),
        "unsafe_commands": _list(compiled.get("unsafe_commands"))
        or _list(proof_gate.get("unsafe_commands")),
        # P0-13：验证方式分类 —— 哪条义务只能冒烟（gui_smoke / resident）、只能人工（human_only）
        "verification_modes": {str(k): _list(v) for k, v in modes.items()},
        "external_required": _list(compiled.get("external_required")),
        # §27：场景明细（Compiler 认可的命令 + 断言 + 缺口原因）
        "scenarios": _scenario_rows(compiled),
        "coverage_gap": _list(compiled.get("coverage_gap")),
    }
    # §22.1：把「场景 → 命令 → 退出码/stdout」与证据明细**在后端 join** 好，
    # 前端只渲染（UI 不按 id 自行拼装 —— 那就是"UI 猜语义"）。
    _attach_proof_details(proofs, compiled, evidence, _dict(state.get("verify_report")))

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
        # §26：方案 → 编译任务 的对照（merged / split / create_owner / modify_tasks）
        "plan_diff": _plan_diff_view(state),
        # §29 / §16.3：真值来源（ASSERTED / DERIVED / HUMAN CONFIRMED / CONTRADICTED）
        "truth": _truth_view(state),
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
