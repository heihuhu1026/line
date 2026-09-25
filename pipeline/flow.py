"""流水线「流定义」的单一真源（节点 / 边 / 回流 / 闸门）。

**为什么要单独拎出来**：同一份流程知识以前散落在 4 处 ——
``runstore.FLOW_ORDER``、``config.FULL_STAGE_ORDER``、``orchestrator.ONLY_STAGES``、
``orchestrator._step`` 的 if 链，外加 ``cli.ALL_STAGES`` / ``server.PAUSE_STAGES`` 两份白名单。
真机教训（run 20260924-134458）：新增 ``intake`` 阶段后 CLI 白名单漏改，
``--pause-after intake`` 被判「未知阶段」、子进程 exit 2，运行刚启动就死。

这里把拓扑收敛成一份声明，其余模块**派生**出各自的表，并由 :func:`validate`
做跨表一致性校验 —— 新增阶段时漏改任何一张表都会在启动期报出来，而不是等真机跑挂。

术语对齐（借用 LangGraph 的语义，便于和框架对照）：
    node      游标取值（含非模型步骤 ``retrieve`` 与终止态 ``done``）
    edge      node -> node 的转移（线性边 / 条件边 / 回流边）
    interrupt 人工闸门（暂停等人工；由 ``orchestrator._gate_after`` 判定、``_interrupt`` 执行）

本模块**不依赖**同包其他模块（只依赖标准库），避免循环导入；
:func:`validate` 需要跨表校验时才在函数内部延迟导入。
"""
from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------- 节点
START = "intake"
TERMINAL = "done"

#: 会调用模型的节点（决定 ``config.STAGE_MODELS`` 与 ``schemas.STAGE_SCHEMAS`` 应有的键）
MODEL_NODES: list[str] = [
    "intake",
    "pm",
    "architect_assess",
    "architect_plan",
    "dev",
    "test",
    "review",
]

#: 不调用模型、也不产出工件的中间节点
PLAIN_NODES: list[str] = ["retrieve"]

#: 不调用模型、但**会产出工件**的机械步骤（跑真实命令做验证）
CHECK_NODES: list[str] = ["verify"]

#: 本身就是人工闸门的节点（不调模型；到达即 interrupt）
GATE_NODES: list[str] = ["human_review"]

#: 前置节点：跑在 ``intake`` **之前**、由 ``pipeline/gateway.py`` 调用的节点。
#:
#: 刻意**不进** ``EXEC_ORDER``：它不是流水线的阶段，而是**入口总闸**——
#: 判定项目规模（小/大）后要么原样透传（small），要么把项目拆成模块、逐个送进
#: 同一套流水线（large）。因此：
#:   * ``FLOW_ORDER`` / ``EXEC_ORDER`` / ``PAUSABLE_NODES`` 全部不受影响（旁路时行为零差异）；
#:   * 它的产物落在**作业目录** ``runs/_jobs/<job_id>/``，不进 ``runs/<run_id>/`` 的
#:     ``state.json``（沿用「隐藏目录不污染运行列表」的既有约定）；
#:   * 但它的**模型登记**照样受 :func:`validate` 守护 —— 漏登会在启动期报错。
PRE_NODES: list[str] = ["global_architecture_analysis"]

#: 前置节点的分支边：{节点: {分支: 目标}}。目标是真实节点名（直通），或以 ``@`` 开头
#: 的**作业动作**（``@modules`` = 按模块逐个送子流水线，由 gateway 执行）。
PRE_EDGES: dict[str, dict[str, str]] = {
    "global_architecture_analysis": {"small": "intake", "large": "@modules"},
}

#: 会产生工件的节点（= 需要 STAGE_SCHEMAS 与 STAGE_STATE_KEY 登记的阶段）
ARTIFACT_NODES: list[str] = MODEL_NODES + CHECK_NODES

#: 完整执行顺序（游标可能取到的全部值，含非模型步骤与终止态）
EXEC_ORDER: list[str] = [
    "intake",
    "pm",
    "retrieve",
    "architect_assess",
    "architect_plan",
    "dev",
    "test",
    "verify",
    "review",
    "human_review",
    "done",
]

NODES: list[str] = list(EXEC_ORDER)

#: 人工可指定 / 可暂停的阶段 —— ``--pause-after``、``--only``、``--from`` 的白名单。
#: ``human_review`` 由流程在评审通过后自动触发，不接受人工指定。
#: ``verify``（运行验证）虽不调模型，但它是「看一眼真实运行结果」的最佳停点，故可暂停。
#: 顺序按 ``EXEC_ORDER`` 走（而不是 MODEL+CHECK 拼接），这样白名单与执行顺序一致，
#: 页面/CLI 展示出来的顺序才符合直觉。
PAUSABLE_NODES: list[str] = [
    n for n in EXEC_ORDER if n in (set(MODEL_NODES) | set(CHECK_NODES))
]

#: 兼容旧名：``runstore.FLOW_ORDER`` 的内容（不含 retrieve/done）
FLOW_ORDER: list[str] = [n for n in EXEC_ORDER if n in ARTIFACT_NODES or n in GATE_NODES]

#: 兼容旧名：``orchestrator.ONLY_STAGES`` 的内容（模型阶段 + 机械验证 + 人工闸门）
ONLY_STAGES: list[str] = [n for n in EXEC_ORDER if n in ARTIFACT_NODES or n in GATE_NODES]

# --------------------------------------------------------------------- 边
#: 线性边：node -> 无条件的下一个 node（``None`` 表示该节点的去向由条件决定）
LINEAR_EDGES: dict[str, str | None] = {
    "intake": "pm",
    "pm": "retrieve",
    "retrieve": None,  # 由 project_type 决定
    "architect_assess": "architect_plan",
    "architect_plan": "dev",  # 同时是「一轮迭代」的起点（_begin_round）
    "dev": "test",
    "test": "verify",
    "verify": "review",
    "review": None,  # 由评审判定决定
    "human_review": None,  # 由人工 verdict 决定
}

#: 条件边：node -> {条件: 目标}
CONDITIONAL_EDGES: dict[str, dict[str, str]] = {
    # 新建项目没有存量代码可评估，assess 只会编造不存在的目录/模块，直接进方案阶段
    "retrieve": {"new": "architect_plan", "secondary": "architect_assess"},
    "review": {
        "pass": "human_review",
        "rework_dev": "dev",
        "rework_architect": "architect_plan",
        "escalated_ambiguous": "done",
        "needs_human": "done",
    },
    "human_review": {"approve": "done", "reject": "dev"},
}

#: 回流边（循环）：(起点, 终点)
LOOP_EDGES: list[tuple[str, str]] = [
    ("review", "dev"),
    ("review", "architect_plan"),
    ("human_review", "dev"),
]

# --------------------------------------------------------------------- 闸门
@dataclass(frozen=True)
class GateSpec:
    """一个人工闸门的静态声明。动态触发条件在 ``orchestrator._gate_after`` 里判定。"""

    stage: str
    kind: str  # explicit（人工显式勾选）/ conditional（阶段自带条件）/ mandatory（到达即停）
    title: str
    detail: str = ""


GATE_SPECS: list[GateSpec] = [
    GateSpec("pm", "conditional", "PM 未决项闸门",
             "PM 提出了未决项（已带默认取值），确认或改写 prd.md 第 6 节后再继续。"),
    GateSpec("intake", "conditional", "需求补强闸门",
             "以下缺失要素的默认假设需要人工确认。"),
    GateSpec("human_review", "mandatory", "人工审核闸门"),
]

#: 阶段名 -> state.json 里 artifacts 的键（``runstore.STAGE_STATE_KEY`` 的真源）
STAGE_STATE_KEY: dict[str, str] = {
    "intake": "intake",
    "pm": "scope",
    "architect_assess": "assessment",
    "architect_plan": "plan",
    "dev": "implementation",
    "test": "test_report",
    "verify": "verify_report",
    "review": "review",
    "human_review": "human_review",
}


# --------------------------------------------------------------------- 查询
def next_linear(node: str) -> str:
    """取该节点的线性后继；去向由条件决定的节点会抛 ``KeyError``（调用方必须显式分支）。"""
    target = LINEAR_EDGES.get(node)
    if target is None:
        raise KeyError(f"{node} 没有线性后继（去向由条件决定）")
    return target


def gate_spec(stage: str) -> GateSpec | None:
    for spec in GATE_SPECS:
        if spec.stage == stage:
            return spec
    return None


def is_linear_hop(src: str, dst: str) -> bool:
    return LINEAR_EDGES.get(src) == dst


# --------------------------------------------------------------------- 导出 / 校验
def mermaid() -> str:
    """把拓扑渲染成 Mermaid flowchart（便于贴进文档 / 可视化）。"""
    lines = ["flowchart TD"]
    # 前置节点（入口总闸）画在本体之前，用虚线边区分「不是流水线阶段」这一点
    for node, table in PRE_EDGES.items():
        lines.append(f'    START((需求)) -.-> {node}["{node}<br/>入口总闸"]')
        for branch, target in table.items():
            dst = f'MODS[["各模块 <br/>子流水线"]]' if target.startswith("@") else target
            lines.append(f"    {node} -.->|{branch}| {dst}")
    for node, target in LINEAR_EDGES.items():
        if target:
            lines.append(f"    {node} --> {target}")
    for node, table in CONDITIONAL_EDGES.items():
        for cond, target in table.items():
            lines.append(f"    {node} -->|{cond}| {target}")
    gates = [g.stage for g in GATE_SPECS]
    for node in GATE_NODES:
        if node in gates:
            lines.append(f"    style {node} fill:#ffe9b0,stroke:#c98a00")
    for node in PLAIN_NODES:
        lines.append(f"    style {node} fill:#e8e8e8,stroke:#999")
    for node in CHECK_NODES:
        lines.append(f"    style {node} fill:#e3e9ff,stroke:#5b74c8")
    return "\n".join(lines)


def validate() -> list[str]:
    """跨表一致性校验：返回问题清单（空 = 一致）。

    覆盖四类历史易漏点：
      1. 边指向了不存在的节点；
      2. 模型阶段没在 ``config.STAGE_MODELS`` / ``schemas.STAGE_SCHEMAS`` 里登记；
      3. ``runstore.STAGE_STATE_KEY`` 漏了某个阶段（续跑/人工编辑按阶段回写产物会失败）；
      4. 闸门声明的阶段不是可暂停阶段（``human_review`` 不能被人工指定）。
    """
    from . import config, runstore  # 延迟导入：避免配置/存储反向依赖本模块造成循环

    problems: list[str] = []

    # 1) 边目标合法性
    for src, dst in LINEAR_EDGES.items():
        if src not in NODES:
            problems.append(f"线性边起点 {src} 不是节点")
        if dst is not None and dst not in NODES:
            problems.append(f"线性边 {src} -> {dst} 指向不存在的节点")
    for src, table in CONDITIONAL_EDGES.items():
        if src not in NODES:
            problems.append(f"条件边起点 {src} 不是节点")
        for cond, dst in table.items():
            if dst not in NODES:
                problems.append(f"条件边 {src} -[{cond}]-> {dst} 指向不存在的节点")
    for src, dst in LOOP_EDGES:
        if src not in NODES or dst not in NODES:
            problems.append(f"回流边 {src} -> {dst} 指向不存在的节点")

    # 2) 线性边必须与 EXEC_ORDER 的自洽（防止「改了顺序忘了改边」）
    for node, target in LINEAR_EDGES.items():
        if target is None or node not in EXEC_ORDER:
            continue
        idx = EXEC_ORDER.index(node)
        expect = EXEC_ORDER[idx + 1] if idx + 1 < len(EXEC_ORDER) else None
        if target != expect:
            problems.append(
                f"线性边 {node} -> {target} 与执行顺序不符（按 EXEC_ORDER 应为 {expect}）"
            )

    # 3) 模型阶段的登记完整性。前置节点也调模型，所以一并纳入 —— 漏登记一样在启动期报出来。
    model_specs = set(config.STAGE_MODELS)
    expect_models = set(MODEL_NODES) | set(PRE_NODES)
    if model_specs != expect_models:
        problems.append(
            f"config.STAGE_MODELS 与 flow.MODEL_NODES + PRE_NODES 不一致："
            f"缺 {sorted(expect_models - model_specs)} / 多 {sorted(model_specs - expect_models)}"
        )
    for node in CHECK_NODES:
        if node in config.STAGE_MODELS:
            problems.append(f"{node} 是机械验证阶段（不调模型），不应出现在 config.STAGE_MODELS 里")
    from .schemas import PRE_SCHEMAS, STAGE_SCHEMAS

    for node in ARTIFACT_NODES:
        if node not in STAGE_SCHEMAS:
            problems.append(f"schemas.STAGE_SCHEMAS 缺阶段 {node}（凡产出工件的阶段都要登记）")
    # 前置节点走独立登记表（它不是流水线阶段，混进 STAGE_SCHEMAS 会污染阶段语义）
    if set(PRE_SCHEMAS) != set(PRE_NODES):
        problems.append(
            f"schemas.PRE_SCHEMAS 与 flow.PRE_NODES 不一致："
            f"缺 {sorted(set(PRE_NODES) - set(PRE_SCHEMAS))} / 多 {sorted(set(PRE_SCHEMAS) - set(PRE_NODES))}"
        )

    # 3.5) 前置节点绝不能混进执行顺序 —— 它是入口闸门，不是流水线阶段。
    #      一旦混进去，resume/游标/暂停语义全会跟着歪，必须在这里拦住。
    for node in PRE_NODES:
        if node in EXEC_ORDER:
            problems.append(f"前置节点 {node} 不应出现在 EXEC_ORDER 里（它是入口闸门，不是阶段）")
        if node in PAUSABLE_NODES:
            problems.append(f"前置节点 {node} 不应是可暂停阶段（由 --gateway 单独控制）")
        if node not in PRE_EDGES:
            problems.append(f"前置节点 {node} 没有声明分支边（PRE_EDGES）")
    for node, table in PRE_EDGES.items():
        if node not in PRE_NODES:
            problems.append(f"PRE_EDGES 里的 {node} 不是前置节点")
        if not table:
            problems.append(f"前置节点 {node} 的分支边为空")
        for branch, target in table.items():
            if target.startswith("@"):
                continue  # 作业动作（如 @modules），由 gateway 解释
            if target not in NODES:
                problems.append(f"前置边 {node} -[{branch}]-> {target} 指向不存在的节点")

    # 4) 状态键覆盖
    for node in FLOW_ORDER:
        if node not in STAGE_STATE_KEY:
            problems.append(f"STAGE_STATE_KEY 缺阶段 {node}")
    for node in FLOW_ORDER:
        if node not in runstore.STAGE_STATE_KEY:
            problems.append(f"runstore.STAGE_STATE_KEY 缺阶段 {node}")

    # 5) 闸门声明合法性
    for spec in GATE_SPECS:
        if spec.stage not in NODES:
            problems.append(f"闸门 {spec.stage} 不是节点")
        if spec.kind != "mandatory" and spec.stage not in PAUSABLE_NODES:
            problems.append(f"闸门 {spec.stage}（{spec.kind}）不是可暂停阶段")
    if "human_review" in PAUSABLE_NODES:
        problems.append("human_review 不应出现在可暂停阶段里（由流程自动触发）")

    return problems


def assert_valid() -> None:
    """启动期调用：流定义自相矛盾时直接抛错，避免「跑起来才发现阶段漏登记」。"""
    problems = validate()
    if problems:
        raise RuntimeError("流定义不一致：\n  - " + "\n  - ".join(problems))
