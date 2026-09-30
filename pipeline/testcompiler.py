"""**Test Compiler**（规格§二十九–三十一）：把语义层的 ProofObligation 确定性编译成
可安全执行的 TestScenario —— 测试意图（LLM/PM）与可执行命令（机械白名单）之间的编译层。

与 TaskCompiler 同纪律：

* **只从可信源取命令**：调用方显式传入的 planned_commands（API Digest / Skeleton 入口 /
  Contract 检查 / Requirement 验收派生）以及本模块机械生成的 py_compile / import 检查；
  LLM 给的命令即使可信也必须再过一遍 ``verify.reject_reason`` 安全筛（白名单 + 危险片段 +
  禁管道/重定向），不合格**只登记不执行**。
* **禁止猜断言**：行为类场景只有 ``exit_code==0`` 而没有任何输出/行为断言时判
  ``weak_evidence``（rc=0 可能什么都没做），该义务按未覆盖处理（UNPROVEN）。
* **覆盖缺口机械可见**：每条 required PO 必须能指到至少一个 executable scenario；
  planned_commands 为空 ⇒ 除 syntax/import 外全部 coverage_gap，且顶层
  ``automated_commands_empty=True``（语义同规格§三十一：空 automated_commands = UNPROVEN）。

本模块是**纯函数**：不读 state、不调模型、不执行命令（执行仍只发生在 verify 沙箱）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import ontology
from . import taskcompiler
from .config import VERIFY_ALLOWED_BINS, VERIFY_DENY_PATTERNS
from .verify import (  # noqa: PLC2701
    SYNTAX_MAX_FILES,
    _IMPORT_CHECK,
    _python_bin,
    reject_reason,
)

#: 场景状态：可执行且断言充分 / 仅 rc=0 的弱证据 / 没有任何可执行命令
STATUS_EXECUTABLE = "executable"
STATUS_WEAK = "weak_evidence"
STATUS_UNPROVEN = "unproven"

#: 仅凭退出码 0 不能证明行为的 PO 类别（syntax 的职责本来就是"能解析"；import 另有 stdout 标记）
_BEHAVIOR_KINDS = frozenset({
    ontology.PO_KIND_BEHAVIOR,
    ontology.PO_KIND_INTERFACE,
    ontology.PO_KIND_CONTRACT,
    ontology.PO_KIND_MATERIALIZATION,
    ontology.PO_KIND_COMMAND,
    ontology.PO_KIND_REGRESSION,
    ontology.PO_KIND_INVARIANT,
    ontology.PO_KIND_DELIVERY,
})


@dataclass
class TestAction:
    """一条原子执行动作。assertions 为**声明式机械断言**（执行器负责判定）：

    * ``exit_code==0`` / ``exit_code!=0``
    * ``stdout_contains:<文本>`` / ``stderr_contains:<文本>``
    * ``stdout_regex:<正则>``
    """

    command: str
    source: str = ""                       # planned 命令的可信来源（api_digest/skeleton/...）
    expect_exit: int = 0
    assertions: list[str] = field(default_factory=list)
    safe: bool = True
    reject_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "source": self.source,
            "expect_exit": self.expect_exit,
            "assertions": list(self.assertions),
            "safe": self.safe,
            "reject_reason": self.reject_reason,
        }


@dataclass
class TestScenario:
    """一个 TestScenario 恰好回答一条 ProofObligation（多对一允许，一对多禁止）。"""

    id: str
    target_po: str
    kind: str
    title: str
    actions: list[TestAction] = field(default_factory=list)
    setup: list[str] = field(default_factory=list)
    cleanup: list[str] = field(default_factory=list)
    derived_from: list[str] = field(default_factory=list)  # 可信源 id（po/claim/contract/...）
    status: str = STATUS_UNPROVEN
    gap_reason: str = ""

    @property
    def automated_commands(self) -> list[str]:
        """安全且可执行的命令（执行器只应消费这一份）。"""
        return [a.command for a in self.actions if a.safe and a.command]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target_po": self.target_po,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "gap_reason": self.gap_reason,
            "setup": list(self.setup),
            "actions": [a.to_dict() for a in self.actions],
            "cleanup": list(self.cleanup),
            "derived_from": list(self.derived_from),
            "automated_commands": self.automated_commands,
        }


def _as_po(item: Any) -> dict:
    """ProofObligation 对象 / dict 统一成轻量 dict（不丢 required/kind/claim）。"""
    if isinstance(item, ontology.ProofObligation):
        return {"id": item.id, "kind": item.kind, "required": item.required,
                "claim": item.claim, "name": item.name}
    item = item if isinstance(item, dict) else {}
    return {
        "id": str(item.get("id") or ""),
        "kind": str(item.get("kind") or ontology.PO_KIND_BEHAVIOR),
        "required": bool(item.get("required", True)),
        "claim": str(item.get("claim") or ""),
        "name": str(item.get("name") or ""),
    }


def _norm(path: Any) -> str:
    return str(path or "").replace("\\", "/").strip()


def _py_modules(files: list[str]) -> list[str]:
    """规划内 .py 文件 → 可 import 的模块名（复用 TaskCompiler 同口径，去重保序）。"""
    out: list[str] = []
    for path in files:
        if not path.endswith(".py"):
            continue
        mod = taskcompiler._module_of(path)  # noqa: SLF001 —— 同包统一口径
        if mod and mod not in out:
            out.append(mod)
    return out


def _syntax_actions(files: list[str]) -> list[TestAction]:
    """机械生成 py_compile（syntax PO 的合法 verifier；只解析不执行）。"""
    actions: list[TestAction] = []
    for i in range(0, len(files), SYNTAX_MAX_FILES):
        batch = files[i:i + SYNTAX_MAX_FILES]
        actions.append(TestAction(
            command=" ".join([_python_bin(), "-m", "py_compile", *batch]),
            source="mechanical:py_compile",
            assertions=["exit_code==0"],
        ))
    return actions


def _import_actions(modules: list[str]) -> list[TestAction]:
    """机械生成 import 检查（带 stdout 标记，rc=0 + 标记双重判定，不是裸导入）。"""
    if not modules:
        return []
    # 与 verify.import_check_spec 同构：argv 直调不经 shell，-c 保留真实换行。
    return [TestAction(
        command=f'{_python_bin()} -c "{_IMPORT_CHECK}" ' + " ".join(modules[:SYNTAX_MAX_FILES]),
        source="mechanical:import_check",
        assertions=["exit_code==0", "stdout_contains:IMPORT_CHECK OK"],
    )]


def _screen(command: str, source: str, *,
            allowed_bins: frozenset[str], deny_patterns: tuple[str, ...]) -> TestAction:
    """过安全筛：不合格的动作带原因返回（safe=False），调用方不得执行。"""
    reason = reject_reason(command, allowed_bins, deny_patterns)
    return TestAction(command=command, source=source, safe=reason is None,
                      reject_reason=reason or "")


def _scenario_id(po_id: str, actions: list[TestAction]) -> str:
    """场景身份含**命令与断言口径**：断言变化即不同的证明，必须换 id。"""
    return "tscn:" + ontology.stable_hash(
        ontology.canonical_json([
            po_id,
            [[a.command, a.expect_exit, a.assertions] for a in actions],
        ]),
        length=12,
    )


def compile_scenarios(
    *,
    obligations: Any,
    files: Any = (),
    planned_commands: Any = (),
    allowed_bins: frozenset[str] = VERIFY_ALLOWED_BINS,
    deny_patterns: tuple[str, ...] = VERIFY_DENY_PATTERNS,
) -> dict[str, Any]:
    """把 required PO 编译成 TestScenario 集合（纯函数，规格§二十九–三十一）。

    参数
    ----
    obligations:
        ProofObligation 对象或 dict 的可迭代（语义图上的义务，是**唯一覆盖清单**）。
    files:
        方案规划内的文件路径（syntax/import 机械场景的输入）。
    planned_commands:
        可信源给出的命令 dict：``{"command", "source", "assertions?", "target_po?",
        "expect_exit?"}``。只接受显式可信源；每条仍过安全白名单。

    返回
    ----
    ``{"scenarios", "coverage_gap", "weak_evidence", "unsafe_commands",
       "unclaimed_commands", "automated_commands_empty"}``
    """
    pos = [_as_po(p) for p in (obligations or []) if _as_po(p)["id"]]
    required = [p for p in pos if p["required"]]
    py_files = [_norm(f) for f in (files or ()) if _norm(f).endswith(".py")]
    modules = _py_modules(py_files)

    scenarios: list[TestScenario] = []
    unsafe: list[dict[str, str]] = []
    covered: set[str] = set()
    weak: list[str] = []

    # ① 机械场景：syntax / import（命令由本模块从规划文件机械生成，不来自 LLM）
    syntax_actions = _syntax_actions(py_files)
    import_actions = _import_actions(modules)
    for po in required:
        if po["kind"] == ontology.PO_KIND_SYNTAX and syntax_actions:
            sc = TestScenario(
                id=_scenario_id(po["id"], syntax_actions),
                target_po=po["id"], kind=po["kind"],
                title="语法检查（py_compile）",
                actions=[TestAction(**a.to_dict()) for a in syntax_actions],
                derived_from=[po["id"], "mechanical:py_compile"],
                status=STATUS_EXECUTABLE,
            )
            scenarios.append(sc)
            covered.add(po["id"])
        elif po["kind"] == ontology.PO_KIND_IMPORT and import_actions:
            sc = TestScenario(
                id=_scenario_id(po["id"], import_actions),
                target_po=po["id"], kind=po["kind"],
                title="导入检查（import check）",
                actions=[TestAction(**a.to_dict()) for a in import_actions],
                derived_from=[po["id"], "mechanical:import_check"],
                status=STATUS_EXECUTABLE,
            )
            scenarios.append(sc)
            covered.add(po["id"])

    # ② 可信源命令：安全筛 → 按 target_po 显式归档；无显式归属的只归 delivery PO
    #    （"工作区被真实验证"的语义本就由任意真实入口运行承担），其余一律不猜。
    by_po: dict[str, list[TestAction]] = {}
    unclaimed: list[str] = []
    unbound: list[dict] = []
    delivery_po = next(
        (p["id"] for p in required if p["kind"] == ontology.PO_KIND_DELIVERY), ""
    )
    safe_planned_count = 0
    for item in planned_commands or ():
        item = item if isinstance(item, dict) else {}
        command = str(item.get("command") or "").strip()
        if not command:
            continue
        action = _screen(
            command, str(item.get("source") or "planned"),
            allowed_bins=allowed_bins, deny_patterns=deny_patterns,
        )
        action.expect_exit = int(item.get("expect_exit") or 0)
        action.assertions = [str(x) for x in (item.get("assertions") or [])]
        if not action.safe:
            unsafe.append({"command": command, "reason": action.reject_reason})
            continue
        safe_planned_count += 1
        # P0-10 §12.1/12.2：LLM 给的只是**候选**（`target_po_candidate`，兼容旧名 `target_po`），
        # 最终归属由 **compiler** 按一致性判定 —— 定不了就 UNBOUND，**不猜**、
        # 也不静默塞给 delivery PO（那会让一条无关命令"证明"了交付）。
        target = str(item.get("target_po_candidate") or item.get("target_po") or "")
        if target:
            bound = bind_target_po(target, command, action.assertions, required)
            if bound:
                by_po.setdefault(bound, []).append(action)
            else:
                unbound.append({
                    "command": command,
                    "candidate": target,
                    "reason": "候选归属未通过一致性校验 → UNBOUND（不猜）",
                })
        elif delivery_po:
            by_po.setdefault(delivery_po, []).append(action)
        else:
            unclaimed.append(command)

    for po in required:
        if po["id"] in covered:
            continue
        actions = by_po.get(po["id"], [])
        if not actions:
            scenarios.append(TestScenario(
                id=_scenario_id(po["id"], []), target_po=po["id"], kind=po["kind"],
                title=po["name"] or po["claim"][:80],
                derived_from=[po["id"]], status=STATUS_UNPROVEN,
                gap_reason="没有任何来自可信源的可执行命令",
            ))
            continue
        # 行为类：只有 exit_code==0、没有任何输出/行为断言 ⇒ 弱证据，按未覆盖计。
        # （syntax/import 不会走到这里：它们由机械生成器覆盖）
        strong = [a for a in actions if any(
            x and x != "exit_code==0" for x in a.assertions
        )]
        if po["kind"] in _BEHAVIOR_KINDS and not strong:
            sc = TestScenario(
                id=_scenario_id(po["id"], actions),
                target_po=po["id"], kind=po["kind"],
                title=po["name"] or po["claim"][:80], actions=actions,
                derived_from=[po["id"], "planned_commands"],
                status=STATUS_WEAK,
                gap_reason="仅 exit_code==0 而无行为断言（rc=0 不证明任何需求行为）",
            )
            scenarios.append(sc)
            weak.append(sc.id)
        else:
            # 没有任何断言时至少补 exit_code==0（机械底线），避免空断言列表
            for act in actions:
                if not act.assertions:
                    act.assertions = ["exit_code==0"]
            sc = TestScenario(
                id=_scenario_id(po["id"], actions),
                target_po=po["id"], kind=po["kind"],
                title=po["name"] or po["claim"][:80], actions=actions,
                derived_from=[po["id"], "planned_commands"],
                status=STATUS_EXECUTABLE,
            )
            scenarios.append(sc)
            covered.add(po["id"])

    # required PO 没有任何 executable 场景即缺口（weak/unproven 都算）
    coverage_gap = sorted(p["id"] for p in required if p["id"] not in covered)

    return {
        "scenarios": [s.to_dict() for s in scenarios],
        "coverage_gap": coverage_gap,
        "weak_evidence": weak,
        "unsafe_commands": unsafe,
        "unclaimed_commands": unclaimed,
        # P0-10 §12.3：LLM 声称了归属但**证不成**的命令（不静默归给别的 PO）
        "unbound_commands": unbound,
        # 规格§三十一：一条安全的 planned 命令都没有 ⇒ 行为证据为空，只能 UNPROVEN
        "automated_commands_empty": safe_planned_count == 0,
    }


def audit_po_test_coverage(compiled: Any) -> dict[str, list[str]]:
    """方案§八：required PO 的测试覆盖三分类（纯函数，直接消费 compile_scenarios 产物）。

    * ``covered`` —— 存在 **executable** 场景（含有效断言的真实命令）；
    * ``weak``    —— 有场景但只有 rc=0（weak_evidence），按未覆盖处理；
    * ``missing`` —— 没有任何可执行场景（coverage_gap 扣除 weak）。

    weak / missing 都不允许被证明，区别仅留痕给评审与人工。
    """
    compiled = compiled if isinstance(compiled, dict) else {}
    scenarios = [s for s in (compiled.get("scenarios") or []) if isinstance(s, dict)]
    weak = [str(x) for x in (compiled.get("weak_evidence") or [])]
    # weak_evidence 里存的是 scenario id，换算成 target_po 便于与 PO 状态对账。
    weak_pos = [str(s.get("target_po") or "") for s in scenarios
                 if str(s.get("id") or "") in set(weak)]
    covered = sorted({
        str(s.get("target_po") or "")
        for s in scenarios
        if str(s.get("status") or "") == STATUS_EXECUTABLE and str(s.get("target_po") or "")
    })
    gap = [str(x) for x in (compiled.get("coverage_gap") or [])]
    missing = sorted(p for p in gap if p not in set(weak_pos))
    return {"covered": covered, "weak": sorted(p for p in weak_pos if p), "missing": missing}


def bind_target_po(
    candidate: str,
    command: str,
    assertions: list[str] | None,
    required: Any,
) -> str:
    """**Compiler 权威绑定**（P0-10 §12.2/§12.3）：判定一条命令真正证明哪个 PO。

    LLM 输出的 ``target_po`` 只是**候选**（§12.1）。这里按一致性重判：

      * 候选不是 required PO → UNBOUND（不猜）；
      * 候选与命令**形态**不符（如 syntax 类 PO 却给了一条运行命令）→ UNBOUND；
      * 其余按候选绑定；断言强弱（行为类只有 rc=0）由 weak 逻辑单独处理，
        不属于"绑错"，故不在这里判 UNBOUND。

    返回确定的 PO id；定不了返回 ``""``（调用方按 UNBOUND 处理）。
    """
    pos = [_as_po(p) for p in (required or [])]
    by_id = {p["id"]: p for p in pos if p["id"]}
    cand = str(candidate or "").strip()
    if not cand:
        return ""
    po = by_id.get(cand)
    if po is None:
        return ""  # 声称了一个根本不在 required 里的 PO —— 不猜
    cmd = str(command or "").lower()
    kind = str(po.get("kind") or "")
    # 形态一致性：机械类 PO 必须由对应形态的命令来证
    if kind == ontology.PO_KIND_SYNTAX and not (
        "py_compile" in cmd or "compileall" in cmd or "-m compile" in cmd
    ):
        return ""
    if kind == ontology.PO_KIND_IMPORT and "import" not in cmd:
        return ""
    return cand


def proof_coverage_gate(required: Any, compiled: Any) -> dict:
    """业务 Proof 覆盖硬指标（P0-11 §13）：required PO **有没有被真正证明**。

    返回 ``{required, covered, missing, weak, unexecutable}``：

      * ``covered``      —— 有 executable 场景且带有效断言；
      * ``weak``         —— 有场景但只有 rc=0 ⇒ **等于没证明**（§13.2）；
      * ``missing``      —— required PO 根本没有场景（§13.1）；
      * ``unexecutable`` —— 场景存在但不可执行（无命令 / 不安全 / 需外部）⇒ 不能伪装成 PASS。

    真机形态「13 cases / 3 条 FR 覆盖」就是靠这个数字暴露，而不是等 Review 说"测试少"。
    """
    pos = [_as_po(p) for p in (required or [])]
    needed = [p for p in pos if p.get("required")]
    compiled = compiled if isinstance(compiled, dict) else {}
    scenarios = [s for s in (compiled.get("scenarios") or []) if isinstance(s, dict)]

    strong_covered: set[str] = set()
    weak: set[str] = set()
    unexecutable: set[str] = set()
    for sc in scenarios:
        target = str(sc.get("target_po") or "")
        if not target:
            continue
        status = str(sc.get("status") or "")
        if status == STATUS_EXECUTABLE:
            strong_covered.add(target)
        elif status == STATUS_WEAK:
            weak.add(target)
        else:
            unexecutable.add(target)
    needed_ids = [p["id"] for p in needed if p["id"]]
    covered_ids = [p for p in needed_ids if p in strong_covered]
    missing = [p for p in needed_ids
               if p not in strong_covered and p not in weak and p not in unexecutable]
    return {
        "required": len(needed_ids),
        "covered": len(covered_ids),
        "missing": len(missing),
        "weak": len([p for p in needed_ids if p in weak]),
        "unexecutable": len([p for p in needed_ids if p in unexecutable]),
        "missing_ids": sorted(missing),
        "weak_ids": sorted(p for p in needed_ids if p in weak),
        "unexecutable_ids": sorted(p for p in needed_ids if p in unexecutable),
        "covered_ids": sorted(covered_ids),
    }
