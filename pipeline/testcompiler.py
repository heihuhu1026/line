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

#: 场景状态：可执行且断言充分 / 仅 rc=0 的弱证据 / 没有任何可执行命令 / 由就地机械检查器证明
STATUS_EXECUTABLE = "executable"
STATUS_WEAK = "weak_evidence"
STATUS_UNPROVEN = "unproven"
#: 编译器机械生成、由编排器**就地**跑的机械检查器证明（无 shell 命令，见 §12.5）：
#: contract → ``verify.contract_check``；interface_freeze → ``contract_check`` +
#: ``skeleton_conformance``；materialization → 补丁物化。它们**不是**可执行命令，
#: 因此既不能进 ``automated_commands``，也不能被 verify 当命令执行。
STATUS_MECHANICAL = "mechanical"

#: P0-10 §12.5：权威 verifier 是「编排器就地机械检查器」的 PO 类别 —— 由 TestCompiler
#: 自己生成证明场景，**不交给 Test LLM**（LLM 无从产出一条能证明它们的命令）。
#: 值为给人看的 check_id 名单；真正的证据仍由 verify/编排器机械产出。
_INPROCESS_MECHANICAL_VERIFIERS: dict[str, tuple[str, ...]] = {
    ontology.PO_KIND_CONTRACT: ("contract_check",),
    ontology.PO_KIND_INTERFACE_FREEZE: ("contract_check", "skeleton_conformance"),
    ontology.PO_KIND_MATERIALIZATION: ("patch_apply",),
}

# ------------------------------------------------------------------ 验证方式分类（P0-13）
#: 每条 required PO 必须被机械归入**一档验证方式**；归不了档的显式暴露（`unclassified`），
#: 不许"看起来已被覆盖"。分类纯字面 + PO kind，**不引 LLM**：真机里 GUI / 常驻 / 人工三类
#: 义务被当成"应该有单测"，于是要么逼模型编造断言、要么被记成说不清的缺口。
MODE_MECHANICAL = "mechanical"        # 就地机械检查器（py_compile / import / 契约 / 冻结 / 物化）
MODE_UNIT = "unit"                    # 进程内断言（python -c / unittest）即可机械判定
MODE_GUI_SMOKE = "gui_smoke"          # 需要图形界面 / 显示器：本沙箱只能冒烟（跑起来）
MODE_RESIDENT = "resident"            # 常驻进程 / 主循环：只能短超时跑起来
MODE_HUMAN = "human_only"             # 机械不可验（主观 / 无可观测口径）：必须人工确认
MODE_UNCLASSIFIED = "unclassified"    # 归不了档：显式暴露，绝不猜成"已覆盖"

#: 正则→分类用的小写字面证据。刻意用**较长**的 token：`ui` / `gui` 这类两字母会命中
#: `build` / `guide` 之类的无关词，从而把可单测的义务误降为"只能冒烟"。
_GUI_HINTS = (
    "界面", "窗口", "渲染", "绘制", "画布", "颜色", "字体", "按钮", "鼠标", "显示",
    "标题栏", "tkinter", "pygame", "canvas", "window", "render", "color", "font",
    "display", "sprite",
)
_RESIDENT_HINTS = (
    "主循环", "事件循环", "常驻", "持续运行", "每帧", "帧率", "循环刷新", "游戏循环",
    "mainloop", "event loop", "game loop", "while true",
)
_HUMAN_HINTS = (
    "美观", "好看", "视觉", "手感", "体验", "主观", "人工确认", "无可观测", "无客观",
)

#: 归入"进程内断言 / 入口命令"两类的 PO kind（其余 kind 由机械检查器承担）。
_ASSERTABLE_KINDS = frozenset({
    ontology.PO_KIND_BEHAVIOR,
    ontology.PO_KIND_COMMAND,
    ontology.PO_KIND_REGRESSION,
    ontology.PO_KIND_INVARIANT,
    ontology.PO_KIND_INTERFACE,
})

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
    #: STATUS_MECHANICAL 专用：证明该 PO 的就地机械检查器（check_id 列表，供前端/评审显示）。
    mechanical_check: list[str] = field(default_factory=list)
    #: P0-13：该义务的**验证方式**（mechanical / unit / command / gui_smoke / resident /
    #: human_only / unclassified）。用于把"机械不可验"与"忘了测"分开，不逼模型编造断言。
    verification_mode: str = MODE_UNCLASSIFIED

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
            "mechanical_check": list(self.mechanical_check),
            "verification_mode": self.verification_mode,
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


def classify_obligation(obligation: Any) -> str:
    """把一条 ProofObligation 归入一种**验证方式**（P0-13，纯函数，确定性，不引 LLM）。

    为什么需要：真机里 GUI / 常驻 / 人工三类义务被当成"应该有单测"，于是要么逼模型
    编造断言、要么被记成说不清的缺口。分类把"机械不可验"与"忘了测"分开。

    判定顺序（先强后弱；判不准时倾向"可机械验"，只有明确凭证才降档）：

      ① PO kind 自带机械 verifier（syntax / import / materialization / delivery /
         contract / interface_freeze）→ ``mechanical``；
      ② 字面命中"只能人工"证据（美观 / 手感 / 主观 …）→ ``human_only``；
      ③ 字面命中图形界面证据（窗口 / 绘制 / tkinter / pygame …）→ ``gui_smoke``；
      ④ 字面命中常驻进程证据（主循环 / 每帧 / mainloop …）→ ``resident``；
      ⑤ 可断言 kind（behavior / command / regression / invariant / interface）→ ``unit``；
      ⑥ 其余 → ``unclassified``（**显式暴露**，绝不猜成"已覆盖"）。
    """
    item = _as_po(obligation)
    kind = str(item.get("kind") or "")
    if kind in _INPROCESS_MECHANICAL_VERIFIERS or kind in (
        ontology.PO_KIND_SYNTAX,
        ontology.PO_KIND_IMPORT,
        ontology.PO_KIND_MATERIALIZATION,
        ontology.PO_KIND_DELIVERY,
    ):
        return MODE_MECHANICAL
    text = f"{item.get('name') or ''} {item.get('claim') or ''}".lower()
    if any(h in text for h in _HUMAN_HINTS):
        return MODE_HUMAN
    if any(h in text for h in _GUI_HINTS):
        return MODE_GUI_SMOKE
    if any(h in text for h in _RESIDENT_HINTS):
        return MODE_RESIDENT
    if kind in _ASSERTABLE_KINDS:
        return MODE_UNIT
    return MODE_UNCLASSIFIED


def mode_summary(obligations: Any) -> dict[str, list[str]]:
    """``{验证方式: [PO id, …]}`` 汇总（P0-13）：供审计 / 控制塔显示"哪些只能冒烟/人工"。"""
    out: dict[str, list[str]] = {}
    for item in obligations or ():
        po = _as_po(item)
        if not po["id"]:
            continue
        out.setdefault(classify_obligation(po), []).append(po["id"])
    for ids in out.values():
        ids.sort()
    return out


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


def _mechanical_scenario_id(po_id: str, checks: tuple[str, ...]) -> str:
    """就地机械证明的场景身份：只取决于 PO 与其机械检查器名单（确定性、可复现）。"""
    return "tscn:" + ontology.stable_hash(
        ontology.canonical_json([po_id, "mechanical", list(checks)]), length=12
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
       "unclaimed_commands", "unbound_commands", "automated_commands_empty",
       "verification_modes", "external_required"}``

    后两项是 P0-13 的**验证方式分类**：``verification_modes`` 为 ``{方式: [PO id]}``，
    ``external_required`` 是只有本沙箱之外（人工确认 / 显示器 / 常驻交互）才能定的义务 ——
    单列出来，免得被当成"忘测"反复重问。
    """
    pos = [_as_po(p) for p in (obligations or []) if _as_po(p)["id"]]
    required = [p for p in pos if p["required"]]
    py_files = [_norm(f) for f in (files or ()) if _norm(f).endswith(".py")]
    modules = _py_modules(py_files)

    scenarios: list[TestScenario] = []
    unsafe: list[dict[str, str]] = []
    covered: set[str] = set()
    weak: list[str] = []
    #: P0-13：只有本沙箱之外的验证手段才能定的义务（人工确认 / 显示器 / 常驻交互）
    external_required: list[str] = []

    # ① 机械场景（P0-10 §12.5，命令由本模块机械生成，不来自 LLM）：
    #    · syntax / import → 真实 shell 命令（py_compile / import 检查），STATUS_EXECUTABLE；
    #    · contract / interface_freeze / materialization → 编排器**就地**机械检查器，
    #      无 shell 命令，STATUS_MECHANICAL（不进 automated_commands / verify 绑定）。
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
        elif po["kind"] in _INPROCESS_MECHANICAL_VERIFIERS:
            # 权威 verifier 是编排器就地机械检查器 ⇒ 编译器自己生成证明，不交给 Test LLM。
            # 刻意 actions=[]：不是 shell 命令，执行器不得消费（证据来自 verify/编排器的机械结论）。
            checks = _INPROCESS_MECHANICAL_VERIFIERS[po["kind"]]
            sc = TestScenario(
                id=_mechanical_scenario_id(po["id"], checks),
                target_po=po["id"], kind=po["kind"],
                title=po["name"] or po["claim"][:80] or "机械检查",
                derived_from=[po["id"], *[f"mechanical:{c}" for c in checks]],
                status=STATUS_MECHANICAL,
                mechanical_check=list(checks),
            )
            scenarios.append(sc)
            covered.add(po["id"])

    # ① 产出的都是**机械证明**（syntax/import 是真命令；contract/freeze/物化走就地检查器）
    for sc in scenarios:
        sc.verification_mode = MODE_MECHANICAL

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
            declared = next((p for p in required if p["id"] == target), None)
            if declared and declared["kind"] in _INPROCESS_MECHANICAL_VERIFIERS:
                # §12.5：这类 PO 的权威 verifier 是就地机械检查器，**命令不是它的证据**。
                # 登记成 UNBOUND（含原因），既不静默丢弃也不把它塞给别的 PO。
                unbound.append({
                    "command": command,
                    "candidate": target,
                    "reason": "该 PO 由就地机械检查器证明（"
                              + "、".join(_INPROCESS_MECHANICAL_VERIFIERS[declared["kind"]])
                              + "），命令不能作为它的证据 → UNBOUND（不猜）",
                })
                continue
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
        mode = classify_obligation(po)
        if not actions:
            # P0-13：把"机械不可验"（人工 / GUI / 常驻）与"忘了测"分开措辞，并单列
            # `external_required` —— 后者不该被当成失败重问的素材（重问也修不出来）。
            if mode == MODE_HUMAN:
                reason = "只能人工确认（P0-13 归类 human_only：机械不可验）"
            elif mode in (MODE_GUI_SMOKE, MODE_RESIDENT):
                reason = ("没有任何可执行命令（P0-13 归类 " + mode
                          + "：本沙箱只能冒烟 / 短超时跑起来，画面与手感不可断言）")
            else:
                reason = "没有任何来自可信源的可执行命令"
            scenarios.append(TestScenario(
                id=_scenario_id(po["id"], []), target_po=po["id"], kind=po["kind"],
                title=po["name"] or po["claim"][:80],
                derived_from=[po["id"]], status=STATUS_UNPROVEN,
                verification_mode=mode, gap_reason=reason,
            ))
            if mode in (MODE_HUMAN, MODE_GUI_SMOKE, MODE_RESIDENT):
                external_required.append(po["id"])
            continue
        # 行为类：只有 exit_code==0、没有任何输出/行为断言 ⇒ 弱证据，按未覆盖计。
        # （syntax/import 不会走到这里：它们由机械生成器覆盖）
        strong = [a for a in actions if any(
            x and x != "exit_code==0" for x in a.assertions
        )]
        if po["kind"] in _BEHAVIOR_KINDS and not strong:
            gap = "仅 exit_code==0 而无行为断言（rc=0 不证明任何需求行为）"
            if mode in (MODE_GUI_SMOKE, MODE_RESIDENT):
                gap += f"；该义务归类 {mode}，启动命令已是本沙箱的最佳可得证据"
            sc = TestScenario(
                id=_scenario_id(po["id"], actions),
                target_po=po["id"], kind=po["kind"],
                title=po["name"] or po["claim"][:80], actions=actions,
                derived_from=[po["id"], "planned_commands"],
                status=STATUS_WEAK, verification_mode=mode,
                gap_reason=gap,
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
                status=STATUS_EXECUTABLE, verification_mode=mode,
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
        # P0-13：验证方式分类 —— "机械不可验"（人工 / GUI / 常驻）单列，别当"忘了测"
        "verification_modes": mode_summary(required),
        "external_required": sorted(external_required),
    }


def audit_po_test_coverage(compiled: Any) -> dict[str, list[str]]:
    """方案§八：required PO 的测试覆盖三分类（纯函数，直接消费 compile_scenarios 产物）。

    * ``covered`` —— 存在 **executable** 场景（含有效断言的真实命令）或 **mechanical**
      场景（§12.5：由编排器就地机械检查器证明，如 contract/interface_freeze/materialization）；
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
        if str(s.get("status") or "") in (STATUS_EXECUTABLE, STATUS_MECHANICAL)
        and str(s.get("target_po") or "")
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

      * ``covered``      —— 有 executable 场景且带有效断言，或 mechanical 场景
        （§12.5：就地机械检查器证明；证据仍由 verify/编排器产出，见 ``can_release``）；
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
        if status in (STATUS_EXECUTABLE, STATUS_MECHANICAL):
            strong_covered.add(target)
        elif status == STATUS_WEAK:
            weak.add(target)
        else:
            unexecutable.add(target)
    needed_ids = [p["id"] for p in needed if p["id"]]
    covered_ids = [p for p in needed_ids if p in strong_covered]
    missing = [p for p in needed_ids
               if p not in strong_covered and p not in weak and p not in unexecutable]
    # P0-13：机械不可验（人工确认 / 图形界面 / 常驻进程）的义务单列 —— 它们不该被
    # 当成"忘测"反复重问（重问也修不出来），人工与评审据此判"该走外部验证"。
    external = sorted(
        str(x) for x in (compiled.get("external_required") or []) if str(x) in set(needed_ids)
    )
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
        "external_required": external,
    }
