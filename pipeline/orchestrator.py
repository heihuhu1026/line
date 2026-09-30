"""串行编排器（可暂停 / 可续跑）：
    PM -> 检索 -> 架构师(评估) -> 架构师(方案) -> [开发 -> 测试 -> 评审] 循环。

设计要点：
- 严格单驻留：每次模型调用前 ensure_exclusive，确保显存里只有目标模型（10GB 卡硬约束）。
- 前置评估 + 方案共用 14B 的同一段驻留，全流程只需 3 次模型切换。
- 回流：评审 verdict=rework_dev 回到开发；rework_architect 回到架构师方案；超过上限标记 needs_human。
- 人工闸门：pause_after 里的阶段结束后暂停并落盘 state.json；resume() 从游标处继续。
  人工直接编辑 runs/<id>/NN-<stage>.json 里的 artifact，续跑时以文件为准（编辑生效）。
- 评审频率：review_every（默认 2）—— 首轮与末轮必评审，中间轮可跳过，省模型切换开销。
- 埋点：每次调用记录 tag/ctx/prompt tokens/耗时/是否发生模型切换，落盘到 runs/<run_id>/。

流程游标（state.json 的 cursor）取值：intake / pm / retrieve / architect_assess / architect_plan /
dev / test / review / done。暂停后 cursor 已指向「下一步」，所以 resume 只需接着跑。
（intake＝需求入口补强，跑在 pm 之前；retrieve 是检索非模型步骤。）
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import flow
from . import issues as issues_mod
from . import (
    diagnose,
    evidence,
    patches,
    perfguard,
    planir,
    prd,
    presence,
    prompts,
    retrieval,
    rules,
    runstore,
    symbols as symbol_resolver,
)
from . import taskcompiler, tasktype
from . import testcompiler
from . import semantics
from . import ontology
from . import ontology_validate
from . import verify as verify_mod
from .budget import chars_for_tokens, estimate_tokens, fit_prompt
from .config import (
    CODE_BUDGET,
    DELIVER_ENABLED,
    DEV_CONTENT_REPAIR_TRIES,
    DEV_PER_TASK,
    DEV_TWO_PASS,
    HUMAN_REVIEW_GATE,
    HUMAN_REWORK_BUDGET,
    HUMAN_REWORK_TOPUP_MAX,
    INTAKE_PAUSE_ON_GAPS,
    LSP_ENABLED,
    MAX_PLAN_TASKS,
    MAX_REWORK_ROUNDS,
    MAX_TASKS_PER_FILE,
    MAX_TOTAL_TOKENS,
    MAX_WALL_S,
    REWORK_STAGNATION_LIMIT,
    PER_FILE_TOKENS,
    POOL_PER_FILE_TOKENS,
    PROMPT_HARD_RATIO,
    REVIEW_EVERY,
    ROOT,
    RUNS_DIR,
    STAGE_MODELS,
    STAGE_PER_FILE_TOKENS,
    VERIFY_ALLOWED_BINS,
    VERIFY_COPY_LIMIT_MB,
    VERIFY_DENY_PATTERNS,
    VERIFY_ENABLED,
    VERIFY_MAX_COMMANDS,
    VERIFY_SKIP_DIRS,
    VERIFY_TIMEOUT,
)
from .ollama_client import MockClient, OllamaClient
from .retrieval import Excerpt
from .schemas import SKELETON, STAGE_SCHEMAS

#: 「当前实现正文」纳入的文件后缀（只挑代码/文档，避免把二进制资源塞进 prompt）
_CURRENT_CODE_SUFFIXES = frozenset(
    {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".cs", ".rb", ".md", ".txt", ".json"}
)
#: 单文件纳入上限（字符）：超长的截断并标注
_CURRENT_CODE_PER_FILE_CHARS = 6000
#: 最多纳入多少个文件（防止大仓库把 prompt 撑爆；真正的总量由 dev 的 token 预算兜底）
_CURRENT_CODE_MAX_FILES = 40

#: 主语言后缀 → 语言名。只认「主程序文件」；配置文件类后缀（.json/.md/.yaml…）不参与技术栈判定。
_LANG_SUFFIXES: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".jsx": "javascript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".kt": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".sh": "shell",
}
#: 语言族：TypeScript 与 JavaScript 属同一栈（ts 编译成 js），不该被判成「混用」。
_LANG_FAMILY: dict[str, str] = {"typescript": "javascript"}

# 人工可指定的阶段白名单：真源在 pipeline/flow.py（新增阶段只改那一处即可，cli/server 都从它派生）
ONLY_STAGES = list(flow.ONLY_STAGES)

# 游标指向"下一步"时可能停在非模型步骤（retrieve）或已结束，人工意见需要一个真实阶段作落点
CURSOR_FEEDBACK_TARGET = {"retrieve": "architect_assess", "done": "dev"}

#: 游标 -> 处理函数名。**这份表就是原先 ``_step`` 里那条 if 链的真源**。
#:
#: 以前阶段分发是 if/elif 链，只有跑到那个游标才知道有没有分支 —— 新增阶段时
#: 「注册表都改对了、忘了加 if 分支」要到真机跑到才炸。改成表驱动后，游标集合由
#: ``flow.EXEC_ORDER`` 决定、处理函数由本表决定，两者不一致会被 ``flow.validate()``
#: 在启动期报出来（见文件末尾的 register_step_handlers）。
#:
#: ``done`` 不在表里：它是终止态，``_execute`` 的循环条件 ``while cursor != "done"``
#: 会在调用 ``_step`` 之前就退出。
STEP_METHODS: dict[str, str] = {
    "intake": "_run_intake",
    "pm": "_run_pm",
    "retrieve": "_run_retrieve",
    "architect_assess": "_run_architect_assess",
    "architect_plan": "_run_architect_plan",
    "dev": "_run_dev",
    "test": "_run_test",
    "verify": "_run_verify",
    "review": "_run_review",
    "human_review": "_run_human_review",
}

# 登记给流定义做一致性校验（flow 不能反向 import 本模块，否则循环）。
flow.register_step_handlers(STEP_METHODS)


@dataclass(frozen=True)
class Interrupt:
    """一次人工闸门的判定结果（对齐 LangGraph 的 interrupt 语义）。

    以前「该不该停下等人工」散在三处各判一遍：``_should_pause``（显式/条件闸门）、
    ``_step`` 的 intake 内联判断、``_step_human_review`` 的自暂停。现在统一由
    ``_gate_after`` 判定、``_interrupt`` 执行，消息文案与留痕也就只有一份。
    """

    stage: str
    kind: str  # explicit / conditional / mandatory，见 flow.GateSpec
    title: str
    detail: str = ""


def feedback_target(cursor: str | None, from_stage: str | None = None) -> str:
    """人工意见该注入哪个阶段：优先显式打回的阶段，否则取游标，再否则取最近的可执行阶段。"""
    if from_stage:
        return from_stage
    if cursor in ONLY_STAGES:
        return cursor  # type: ignore[return-value]
    return CURSOR_FEEDBACK_TARGET.get(cursor or "", "dev")


#: 单个 task 的**容量上限**（按 7B 实测单轮输出 ~1385 tok 留余量定）。
#: 超过就应该再拆：按 task 分派之后，一个 dev 调用只做一张施工图，写不完就只能写浅、漏符号，
#: 于是又退回「整批投喂 + 反复返工」的老路。
MAX_TASK_FILES = 2      # 一个 task 最多改动几个文件
MAX_TASK_SYMBOLS = 4    # 一个 task 最多定义几个顶层符号

#: 「同一个文件最多被几张施工图覆盖」这个上限在 `config.MAX_TASKS_PER_FILE`
#: （提示词、机械校验、Task Compiler 共用同一份值，不要在这里再写一个数字）。
#: 它比"总 task 数"更能反映"拆太碎"：真机 20260927-192001 里 `command.py` / `database.py`
#: 各被 3 张图覆盖 ⇒ 同一文件被三次 dev 调用分别改动 ⇒ 合并与 anchor 冲突，
#: 表现为「新增文件写残」「anchor 找不到」；而整体判据根本抓不到它。



#: 施工图**必填字段**缺了最多让方案重做几次。
#: 强制的理由：真机 run 20260927-002903 的 5 张施工图 `symbols`/`interface`/`contracts`/
#: `test_hint` **全是空的** —— 字段写进契约了，但小模型照样不填。不强制就形同虚设：
#: 跨文件契约比对无从下手（返回 0 条会被读成"接口都对得上"），开发也拿不到验收命令。
#: 代价对比：方案阶段重做一次 ≈ 一次调用；下游返工一轮 ≈ dev+test+verify+review ≈ 5 分钟 + 一次 14B 评审。
#: 触顶后**降级为提示**，不无限重做 —— 小模型可能真的填不出来，那时继续循环只是烧时间。
PLAN_CONTRACT_RETRIES = 2


#: Design Gate（设计闸门）阻断后最多自纠几次。
#:
#: 阻断来源全是**机械判据**（见 ``_design_gate_blockers``）：编译器容量错误
#: （单文件符号装不进图上限）、Plan IR 的产物内未解析符号、方案声明 vs 冻结接口基准
#: 冲突。自纠顺序刻意与判据成本匹配：
#:   1. 第一次若**只有**骨架冲突 ⇒ 先重新冻结一次骨架（骨架自身也是 14B 产物，
#:      可能是它错，不该第一时间打扰架构师）；
#:   2. 其余情况 ⇒ 带阻断清单回架构师重做一次方案（允许新增文件/重拆任务）。
#: 之后仍阻断就**停人工闸门**，绝不带已知坏方案进 DEV（旧实现只打 warning，
#: 真机 20260928-095848 因此 dev 怎么写都必被判负，白烧整轮）。
#: 0 = 关闭自纠（阻断即停人工）。
DESIGN_GATE_RETRIES = 2


def pm_unresolved_items(scope: Any, decisions: Any = None) -> dict[str, list[str]]:
    """PM 产物里**尚未成为陈述**的条目：未裁决的 ``open_questions`` + 两列未明确项。

    这是 PM 强控的判据（见 ``Orchestrator._pm_unresolved``）。做成模块级函数是为了让
    **作业层**能在不构造 Orchestrator 的情况下核对每个模块的闸门是否已解决
    （见 ``gateway.job_pm_blockers``）—— 两处口径必须是同一份实现。

    读取时再并一次裁决（与消费点同一口径），而不是只看产物里的 ``final_decision``：
    写入接口万一没并回产物，"裁决存在却说没裁决"会把人工卡死
    （intake 那边已经踩过，见 ``prompts.apply_intake_decisions``）。
    """
    if not isinstance(scope, dict):
        return {"pending": [], "vague": []}
    # 先做确定性准入（折叠三列重复/剔除技术类）再并裁决：旧 run 的产物是在准入规则之前
    # 落库的，这里兜住，保证页面、作业层（gateway）与闸门判据口径一致。
    scope = prompts.normalize_pm_questions(scope)
    merged = prompts.apply_pm_decisions(scope, decisions or [])
    pending = [
        str(q.get("question") or "").strip()
        for q in (merged.get("open_questions") or [])
        if isinstance(q, dict)
        and str(q.get("question") or "").strip()
        and not str(q.get("final_decision") or "").strip()
    ]
    # 未明确项：`apply_pm_decisions` 已把「有人工裁决」的那些从两列里移除并转成
    # confirmed_facts，所以这里剩下的就是**还没有结论**的
    vague: list[str] = []
    # 循环变量**不要叫 `field`**：本模块从 `dataclasses` 导入了 `field`（见 `RunResult`），
    # 同名遮蔽会被 ruff 的 F402 抓到。今天不炸（`RunResult` 在模块导入时就用完了），
    # 但只要有人在**这个循环之后**再定义 dataclass，就会撞上一个非常难查的 TypeError。
    for vague_field in prompts.PM_VAGUE_FIELDS:
        for item in merged.get(vague_field) or []:
            text = prompts.pm_vague_text(item)
            if text:
                vague.append(text)
    return {"pending": pending, "vague": vague}


class OrchestratorError(RuntimeError):
    pass


@dataclass
class RunResult:
    run_id: str
    run_dir: Path
    summary: dict
    artifacts: dict = field(default_factory=dict)
    paused: bool = False
    paused_after: str | None = None


class Orchestrator:
    def __init__(
        self,
        client: OllamaClient | MockClient,
        repo: str | Path | None = None,
        runs_dir: str | Path = RUNS_DIR,
        max_rework: int = MAX_REWORK_ROUNDS,
        unload_at_end: bool = True,
        log: Callable[[str], None] = print,
        review_every: int = REVIEW_EVERY,
        pause_after: list[str] | tuple[str, ...] | None = None,
        project_type: str = "secondary",
        # PM 未决项**强控**：默认开，且不再由配置/环境变量/页面暴露（见 config.py 的说明）。
        # 保留这个构造参数只是给测试与嵌入方的**代码级**逃生门 —— 不是可以随手关掉的开关。
        pause_on_open_questions: bool = True,
        #: 作业模式：**模块级验收延后到作业统一验收**（不在这里停）。
        #: 分模块各停一次人工审核是没有意义的 —— 真正要人看的是**所有模块合起来**的交付物。
        defer_human_review: bool = False,
    ) -> None:
        self.client = client
        self.repo = Path(repo) if repo else None
        self.runs_dir = Path(runs_dir)
        self.max_rework = max_rework
        self.unload_at_end = unload_at_end
        self.log = log
        self.review_every = max(1, review_every)
        # secondary＝基于存量仓库的二次开发；new＝从零生成的全新项目。
        # 决定用哪套系统提示词，并决定是否跳过 architect_assess（见 _step）。
        self.project_type = project_type if project_type in ("new", "secondary") else "secondary"
        self.pause_on_open_questions = pause_on_open_questions
        # 作业模式：模块级人工审核延后到作业统一验收（见 _step_human_review）
        self.defer_human_review = defer_human_review
        self.calls: list[dict] = []
        self.state: dict[str, Any] = {}
        self.pool: list[Excerpt] = []
        self.grounding_warnings: list[dict] = []
        self.requirement = ""
        self.run_id = ""
        self.run_dir: Path | None = None
        self._seq = 0

        # ---- 可续跑状态
        self.status = "idle"  # idle / running / paused / done
        self.mode = "full"  # full / only
        self.cursor = "intake"
        self.attempt = 0
        self.last_review_attempt = 0
        self.fixes: list[str] | None = None
        self.needs_human = False

        # 在场标记（见 pipeline/presence.py）：运行期间由本进程维持，让服务端在
        # 自己重启过之后仍能判出「这个运行真的还在跑」。非运行期为 None。
        self._presence: presence.Guard | None = None
        self.rounds: list[dict] = []
        self.human_feedback: dict[str, list[str]] = {}
        self.human_actions: list[dict] = []
        # 人工对补强阶段「待确认问题」的裁决（页面录入，见 server._save_intake_decisions）。
        # 补强给的是默认假设/建议答案，人工裁决后才是已知前提 —— 必须传下去，
        # 否则补强白做了一半：模型看到的仍是猜的那份。
        self.intake_decisions: list[dict] = []
        # 人工对 **PM 未决项** 的裁决。它必须一并进 _snapshot/_restore：否则编排器
        # 下一次 _persist() 重建 state.json 时会把它整个丢掉（此前就漏了这一个键，
        # 页面存下的 PM 裁决会在下一轮持久化时无声消失）。
        self.pm_decisions: list[dict] = []
        self.pause_after: set[str] = set(pause_after or ())
        self._initial_pause_after: list[str] = sorted(self.pause_after)
        self.paused_after: str | None = None
        #: 上次停在哪个闸门（resume 时从 state 读入）。条件闸门据此在续跑时**重新判定**。
        self._resume_gate_stage: str | None = None
        self.elapsed_s = 0.0
        self.trace_enabled = os.getenv("PIPELINE_TRACE", "1") != "0"
        self._t0 = time.time()

    @property
    def round_kind(self) -> str:
        """本轮的**任务类型**：首次开发 / 方案返工后施工 / 返工修缺陷。

        **不再用「`fixes` 有没有」派生** —— 那是两值时代的判据，三值下会判错：
        方案层返工后 `fixes` 里有 architect_fixes，旧判据于是把这一轮当成 bugfix，
        而 bugfix 口径写着"只改缺陷单指到的部分、其余文件划分一律不动"。方案刚重做过，
        开发拿到的却是最小改动纪律 ⇒ 与"按新方案施工"正面对抗（真机 L2）。

        所以类型由**路由决策点**写入（`_mark_next_round`）、由 `_begin_round` 落到 dev 轮上，
        并持久化进 state —— 续跑与 `--from dev` 都要能恢复。

        兜底链：`state.round_kind` → 无则退回旧判据（老 running 的 state 里没有这个键）。
        getattr 兜底同样必要：`_snapshot` / 续跑会构造出未经 `__init__` 的半成品实例。
        """
        state = getattr(self, "state", None) or {}
        kind = str(state.get("round_kind") or "").strip()
        if kind in tasktype.ROUND_KINDS:
            return kind
        return tasktype.BUGFIX if getattr(self, "fixes", None) else tasktype.FEATURE

    # 注：**模块级人工闸门在作业模式下已由 `defer_human_review` 延后**（编排层入参，
    # gateway 创建模块运行时传 True），人工只在作业跑完后做一次统一验收。
    # 不要在这里再按 run_id 猜「是不是作业模式」—— 那等于把同一件事做成两个真源，
    # 分头维护必然漂移（这一整轮修的就是这类问题）。

    # ------------------------------------------------------------------ 埋点落盘
    @staticmethod
    def _safe_stage_stem(stage: str) -> str:
        """把阶段名净化成合法文件名（逐图施工时 stage=dev-<task id>）。

        正常 task id 形如 ``T-01``（编译期 _clean_task_id 已规范化），不受影响；
        但脏 id（如 mock 占位符 ``<id>``）含 Windows 禁用字符 ``<>``，直接拼文件名会让
        write_json 抛错并**静默丢掉整次调用的快照 / trace / llm-calls 记录**
        （逐图调用外层有容错），人工反馈是否真的注入都无从核对 —— 这里统一兜底。
        """
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(stage or "")).strip().rstrip(". ")
        return stem or "stage"

    # 建议⑰：产物因果链（轻量版，**不做** Event Sourcing）。
    # 每个快照带 artifact_id / produced_by / caused_by，就能串起
    # Requirement→Plan→Task→Patch→Workspace→Verify→Defect→Fix→Verify 做 replay。
    _DEV_TASK_STAGE_RE = re.compile(r"^dev-(T-[0-9A-Za-z]+)(-repair)?$")

    def _parse_produced(self, stage: str) -> tuple[str, str | None, bool]:
        """把快照 stage 拆成 (逻辑阶段, task id, 是否补漏)。

        形态：``dev`` / ``dev-T-01`` / ``dev-T-01-repair``；其余阶段（含第二次调用的
        ``architect_skeleton``）逻辑阶段就是它自己、不绑 task。
        """
        m = self._DEV_TASK_STAGE_RE.match(str(stage or ""))
        if m:
            return "dev", m.group(1), bool(m.group(2))
        return str(stage or ""), None, False

    def _caused_by_keys(self, base_stage: str, task: str | None, is_repair: bool) -> list[str]:
        """本产物的**直接上游**在 artifact_chain 里的键（取最近一次同键产物）。"""
        if base_stage == "dev":
            keys = ["architect_plan"]
            # 返工轮的补丁是被评审缺陷打回的：Defect → Fix，评审快照就是缺陷出处。
            if getattr(self, "fixes", None):
                keys.append("review")
            if task and is_repair:
                keys.append(f"dev_task:{task}")  # 补漏的上一版同 task 补丁
            return keys
        return {
            "pm": ["intake"],
            "architect_assess": ["pm"],
            "architect_plan": ["architect_assess", "review"],
            # 方案§三十：确定性 Task Plan 是 Architect Plan 的下游编译产物。
            "task_plan": ["architect_plan"],
            "test": ["dev"],
            "verify": ["dev"],
            "review": ["verify"],
            "human_review": ["review"],
        }.get(base_stage, [])

    def _provenance(self, stage: str) -> tuple[str, dict, list[str]]:
        """生成 (artifact_id, produced_by, caused_by) 并登记进 state.artifact_chain。

        ``artifact_id`` 与快照文件同名（``序号-stem``），天然唯一且可反查文件。
        注意：必须在序号自增**之后**调用。
        """
        base_stage, task, is_repair = self._parse_produced(stage)
        artifact_id = f"{self._seq:02d}-{self._safe_stage_stem(stage)}"
        produced_by = {"stage": base_stage, "attempt": self.attempt}
        if task:
            produced_by["task"] = task
        chain = self.state.setdefault("artifact_chain", {})
        caused_by = [
            str(chain[k]) for k in self._caused_by_keys(base_stage, task, is_repair)
            if chain.get(k) and str(chain[k]) != artifact_id
        ]
        # 登记为「该逻辑阶段的最近产物」；逐图产物额外按 task 登记（补漏要找上一版）。
        chain[base_stage] = artifact_id
        if task:
            chain[f"dev_task:{task}"] = artifact_id
        return artifact_id, produced_by, caused_by

    def _record(
        self,
        stage: str,
        artifact: Any,
        meta: dict,
        request_preview: str,
        *,
        system: str = "",
        raw_text: str = "",
        raw_thinking: str = "",
        caused_by_extra: list[str] | None = None,
    ) -> None:
        """落阶段快照。**输入与输出都完整留存，不做截断**。

        为什么必须完整：优化提示词的唯一依据就是「这次到底喂了什么、模型到底吐了什么」。
        原先 `request_preview` 截到 4000 字、且只存 user 不存 system —— 照那种预览调提示词
        等于盲人摸象（真机上对 `prompt_version` 归因时，只能回头翻 traces.jsonl 才对得上号）。
        """
        self._seq += 1
        assert self.run_dir is not None
        base_stage, _task, _repair = self._parse_produced(stage)
        chain = self.state.setdefault("artifact_chain", {})
        prev_artifact = str(chain.get(base_stage) or "")
        revisions = self.state.setdefault("artifact_revisions", {})
        artifact_revision = int(revisions.get(base_stage) or 0) + 1
        revisions[base_stage] = artifact_revision
        artifact_id, produced_by, caused_by = self._provenance(stage)
        if caused_by_extra:
            for aid in caused_by_extra:
                if aid and aid not in caused_by:
                    caused_by.append(str(aid))
        # Ontology 真值（方案§十六/§十七）：**Artifact 不等于证明** —— verify 报告本身
        # 只是机械流程的派生产物（DERIVED），PROVEN/FAILED 只能由其 Evidence（绑定
        # workspace revision 的执行痕迹）携带；human_review 携带人工事实 ⇒ ASSERTED；
        # 其余阶段（含 intake/pm/plan/skeleton/dev/test/review LLM）一律 DERIVED。
        artifact_truth = {
            "human_review": ontology.TRUTH_ASSERTED,
        }.get(base_stage, ontology.TRUTH_DERIVED)
        created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            input_hash = "h:" + ontology.stable_hash(
                ontology.canonical_json([request_preview, system]), length=12)
            output_hash = "h:" + ontology.stable_hash(
                ontology.canonical_json(artifact), length=12)
        except (TypeError, ValueError):
            # 兜底：理论上产物都可 JSON 化；不可算 hash 时留空，不阻断落盘。
            input_hash = output_hash = ""
        payload = {
            "stage": stage,
            # 建议⑰：artifact_id / produced_by / caused_by 放快照顶层（与 meta 平级），
            # 读取方不必钻进 meta 就能串因果链。
            "artifact_id": artifact_id,
            "produced_by": produced_by,
            "caused_by": caused_by,
            # Ontology provenance（规格§三十三/三十四）：同一逻辑阶段再产出 ⇒ revision+1、
            # supersedes 指向上版；输入/输出内容 hash 让两代产物可逐字比对。
            "artifact_revision": artifact_revision,
            "supersedes": [prev_artifact] if prev_artifact and prev_artifact != artifact_id else [],
            "input_hash": input_hash,
            "output_hash": output_hash,
            "created_at": created_at,
            "truth": artifact_truth,
            "meta": meta,
            "artifact": artifact,
            # 完整用户输入（不再截断）
            "request_preview": request_preview,
            # 完整系统提示词：换了提示词之后产出质量的变化才能归因
            "system_prompt": system,
            # 模型**原始**输出（含 thinking）：解析后的 artifact 是被 schema 约束过的，
            # 看不出模型原本想说什么、有没有跑偏
            "response_text": raw_text,
            "response_thinking": raw_thinking,
        }
        # 方案§三十/§三十一：只追加的 artifact envelope 台账（不重复算 hash），
        # 供 Ontology 投影 Artifact derived_from / supersedes 链；旧 run 缺该字段缺省安全。
        self.state.setdefault("artifact_log", []).append({
            "artifact_id": artifact_id,
            "stage": base_stage,
            "produced_by": produced_by.get("stage") or base_stage,
            "revision": artifact_revision,
            "supersedes": payload["supersedes"],
            "input_hash": input_hash,
            "output_hash": output_hash,
            "caused_by": caused_by,
            "truth": artifact_truth,
            "created_at": created_at,
        })
        runstore.write_json(
            self.run_dir / f"{self._seq:02d}-{self._safe_stage_stem(stage)}.json", payload
        )
        with (self.run_dir / "llm-calls.jsonl").open("a", encoding="utf-8") as fh:
            # 规整成固定列（见 runstore.CALL_FIELDS）：列一定在，聚合脚本可以无条件取；
            # 不加这一层的话列集由 meta 的构造点隐式决定，缺列只有到聚合时才发现。
            # 规格§五十七：补 artifact/ontology/task 三列 provenance（缺省 null，不臆造）。
            task_semantic_id = ""
            if _task:
                for t in ((self.state.get("plan") or {}).get("tasks") or []):
                    if isinstance(t, dict) and str(t.get("id") or "") == str(_task):
                        task_semantic_id = str(t.get("semantic_task_id") or "")
                        break
            ont_desc = self.state.get("ontology_revision")
            record = runstore.normalize_call_record(
                {
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"), "stage": stage,
                    "artifact_id": artifact_id,
                    "ontology_revision": (
                        ont_desc.get("revision_id") if isinstance(ont_desc, dict) else ""),
                    "task_semantic_id": task_semantic_id,
                    **meta,
                }
            )
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _save_impl_snapshot(self, artifact: Any) -> None:
        """把**累积实现**落成 dev 阶段快照（它才是 dev 阶段的产物）。

        为什么必须单独落一次：dev 阶段会有**很多次调用**（逐张施工图、重问修正、两遍模式的
        中间版），`_call` 会把每一次的产物都按 stage 写成快照。而续跑时 `_restore` 取的是
        「dev 阶段**最后一次**快照」当 `state["implementation"]` —— 拿到的却是某一次**调用**
        的产物，于是在跑的实现被缩成那一份：上一轮 add 出来的文件整份消失，本轮对它们发
        modify 一律判「目标文件不存在」，沙箱里只剩一个 main.py（真机实测：

          · 20260927-134222：累积 17 条 → 续跑后 4 条，返工轮白烧
          · 20260927-150931：内存里 16 条 → `state.json` 里只剩 1 条）

        在 `_stage_dev` 末尾落这一次，不变量才成立：**最新 dev 快照 = 累积实现**。
        逐张施工图的产物另有独立 artifact_stage（`dev-T-01`），不会来抢这个位置。
        """
        if self.run_dir is None:
            return
        self._seq += 1
        artifact_id, produced_by, caused_by = self._provenance("dev")
        payload = {
            "stage": "dev",
            # 建议⑰：累积实现快照同样挂因果链；chain["dev"] 指向它 ——
            # test / verify 的 caused_by 因此精确指到「被验证的这批累积补丁」。
            "artifact_id": artifact_id,
            "produced_by": produced_by,
            "caused_by": caused_by,
            "meta": {"note": "dev-accumulated", "kind": "snapshot"},
            "artifact": artifact,
            "request_preview": "",
        }
        runstore.write_json(self.run_dir / f"{self._seq:02d}-dev.json", payload)

    def _record_task_plan(
        self, ir: dict[str, Any], compiled: list[dict[str, Any]], errors: list[dict[str, Any]]
    ) -> None:
        """方案§三十/§三十一：确定性 Task Plan 也落一等 Artifact envelope。

        TaskCompiler 不经 LLM，``_call`` 的 ``_record`` 不会覆盖该阶段，这里补**同口径**
        台账（artifact_log + 阶段快照），投影时即有 ``Task Plan derived_from Architect
        Plan`` 与同阶段再版的 ``supersedes``。纪律：

        * 纯确定性重算且输出 hash 与上一版相同 ⇒ 视为同一版，**不**新增（同一输入不造版本）；
        * 不写 ``llm-calls.jsonl``（没有模型调用，不能污染模型调用台账）；
        * 真值 DERIVED —— 编译器只机械转译架构方案，不产生 ASSERTED/PROVEN 事实（§三十三）。
        """
        if self.run_dir is None:
            return
        stage = "task_plan"
        try:
            input_hash = "h:" + ontology.stable_hash(
                ontology.canonical_json(ir), length=12)
            output_hash = "h:" + ontology.stable_hash(
                ontology.canonical_json({"tasks": compiled, "errors": errors}), length=12)
        except (TypeError, ValueError):
            # 产物理论上都可 JSON 化；不可算 hash 时留空但不阻断台账。
            input_hash = output_hash = ""
        artifact_log = self.state.setdefault("artifact_log", [])
        prev_same = next(
            (e for e in reversed(artifact_log)
             if isinstance(e, dict) and str(e.get("stage") or "") == stage),
            None,
        )
        if prev_same is not None and str(prev_same.get("output_hash") or "") == output_hash:
            return  # 输出与上一版逐字相同：确定性重算，不制造新版本
        self._seq += 1
        chain = self.state.setdefault("artifact_chain", {})
        prev_artifact = str(chain.get(stage) or "")
        revisions = self.state.setdefault("artifact_revisions", {})
        revision = int(revisions.get(stage) or 0) + 1
        revisions[stage] = revision
        artifact_id, produced_by, caused_by = self._provenance(stage)
        supersedes = [prev_artifact] if prev_artifact and prev_artifact != artifact_id else []
        created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        artifact_log.append({
            "artifact_id": artifact_id,
            "stage": stage,
            "produced_by": produced_by.get("stage") or stage,
            "revision": revision,
            "supersedes": supersedes,
            "input_hash": input_hash,
            "output_hash": output_hash,
            "caused_by": caused_by,
            "truth": ontology.TRUTH_DERIVED,
            "created_at": created_at,
        })
        runstore.write_json(
            self.run_dir / f"{self._seq:02d}-task_plan.json",
            {
                "stage": stage,
                "artifact_id": artifact_id,
                "produced_by": produced_by,
                "caused_by": caused_by,
                "artifact_revision": revision,
                "supersedes": supersedes,
                "input_hash": input_hash,
                "output_hash": output_hash,
                "created_at": created_at,
                "truth": ontology.TRUTH_DERIVED,
                "meta": {
                    "kind": "deterministic", "stage": stage,
                    "note": "taskcompiler.compile_plan（不经 LLM 的确定性编译）",
                    "task_count": len(compiled), "error_count": len(errors),
                },
                "artifact": {"tasks": compiled, "errors": errors},
                "request_preview": "",
            },
        )

    def _record_trace(
        self,
        stage: str,
        system: str,
        user: str,
        raw_text: str,
        raw_thinking: str,
        failed_attempts: list[dict],
        meta: dict,
    ) -> None:
        """完整输入输出留档（问题记录 / 元优化的原始素材）。PIPELINE_TRACE=0 可关闭。"""
        if not self.trace_enabled or self.run_dir is None:
            return
        runstore.append_trace(
            self.run_dir,
            {
                "seq": self._seq,
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "stage": stage,
                "tag": meta.get("tag"),
                "num_ctx": meta.get("num_ctx"),
                "think": meta.get("think"),
                "attempt": meta.get("attempt"),
                "system": system,
                "user": user,
                "raw_text": raw_text,
                "raw_thinking": raw_thinking,
                "failed_attempts": failed_attempts,
                "truncated": meta.get("truncated"),
                "human_feedback_used": meta.get("human_feedback_used"),
                "note": meta.get("note"),
                "schema_errors": meta.get("schema_errors"),
                "usage": {
                    "wall_s": meta.get("wall_s"),
                    "load_s": meta.get("load_s"),
                    "prompt_tokens": meta.get("prompt_tokens"),
                    "output_tokens": meta.get("output_tokens"),
                    "prompt_s": meta.get("prompt_s"),
                    "eval_s": meta.get("eval_s"),
                },
            },
        )

    def _human_facts(self) -> list[str]:
        """人工提供过的全部已确认事实与指令 —— 注入所有下游阶段，避免同一问题被反复提出。"""
        facts: list[str] = []
        for items in (self.human_feedback or {}).values():
            for item in items:
                text = str(item).strip()
                if text and text not in facts:
                    facts.append(text)
        for action in self.human_actions or []:
            if action.get("action") != "human_directive":
                continue
            text = str(action.get("text") or "").strip()
            if text and text not in facts:
                facts.append(text)
        # 补强阶段的裁决同样属于「人工已确认的前提」：注入所有下游阶段，
        # 避免架构师/开发/测试/评审又把这些已裁决的问题当成未决项重新提出一遍。
        for item in self.intake_decisions or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("decision") or "").strip()
            if text and text not in facts:
                facts.append(text)
        return facts

    def _log_action(self, action: str, stage: str | None = None, text: str = "", kind: str = "") -> None:
        """人工干预留痕：谁在哪个阶段做了什么、人工自报的问题分类是什么。"""
        self.human_actions.append(
            {
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "action": action,
                "stage": stage,
                "text": text,
                "kind": kind,
                "attempt": self.attempt,
            }
        )

    def _load_preview(self, tag: str) -> list[str]:
        try:
            return [m.get("name", "") for m in self.client.ps()]
        except Exception:  # noqa: BLE001
            return ["<ps 不可用>"]

    def _gpu_stats(self, tag: str) -> dict:
        """调用后立刻读该模型的显存占用：**报告 100% GPU 也可能是假的**。

        真机教训（2026-09-23）：桌面/远程桌面/IDE 把显存吃到 6.7GB 后，ollama 仍报 size_vram==size，
        实际的 14B 被 WDDM 换到共享内存，prefill 从 157 t/s 掉到 17 t/s，一次评审要跑 5 分钟。
        记下 vram_ratio 与吞吐，这类环境退化才会被记录而不是被当成"模型慢"。
        """
        try:
            for model in self.client.ps():
                if str(model.get("name", "")).startswith(tag):
                    size = model.get("size") or 0
                    vram = model.get("size_vram") or 0
                    return {
                        "vram_gb": round(vram / 1024**3, 2) if vram else None,
                        "model_gb": round(size / 1024**3, 2) if size else None,
                        "vram_ratio": round(vram / size, 3) if size else None,
                    }
        except Exception:  # noqa: BLE001
            pass
        return {}

    # ------------------------------------------------------------------ 单次阶段调用
    def _call(
        self,
        stage: str,
        parts: list[str],
        note: str | None = None,
        pin: list[str] | None = None,
        post: Callable[[Any], Any] | None = None,
        *,
        schema: dict | None = None,
        system: str | None = None,
        artifact_stage: str | None = None,
    ) -> Any:
        """一次模型调用（含埋点与快照）。

        `schema` / `system` / `artifact_stage` 是给「**同一阶段的第二次调用**」用的
        （目前只有方案期的**接口骨架**）：模型规格、驻留、token 预算都沿用该阶段
        （所以不额外产生模型切换），只换契约与系统提示；`artifact_stage` 用来给这类
        调用一个**独立的快照文件名与埋点标签**，避免它与该阶段的正式产物抢同名文件
        （续跑时 `latest_artifacts` 按 stage 取名，同名会把骨架当成方案读回来）。
        """
        spec = STAGE_MODELS[stage]
        resident = self._load_preview(spec.tag)
        sched = self.client.ensure_exclusive(spec.tag)
        # 人工意见/人工事实/覆盖审计必须被模型看到，不能被"从尾部截断"吃掉
        pinned = [p for p in (pin or []) if p]
        extra = self.human_feedback.get(stage)
        if extra:
            pinned.append(prompts.human_feedback_block(extra))
            # 建议⑬：本轮消费了人工反馈（如人工审核打回后的修复），事件强制评审要用。
            self.state["human_feedback_consumed_round"] = self.attempt
        if stage != "pm":  # PM 是第一个阶段，此时还没有人工输入
            block = prompts.human_facts_block(self._human_facts())
            if block:
                pinned.append(block)
        # system 提示同样占上下文，必须从预算里扣掉
        system = system or prompts.system_prompt(stage, self.project_type, self.round_kind)
        budget = max(spec.prompt_token_budget - estimate_tokens(system), 400)
        user, truncated = fit_prompt([p for p in parts if p], budget, pin=pinned)
        record_stage = artifact_stage or stage
        self.log(f"  [{record_stage}] {spec.tag} ctx={spec.num_ctx} think={spec.think} 切换={sched['switched']}")
        # prefill 退化护栏：ollama（本机 AMD + Vulkan）会**按请求**退化，约 1/6 请求的 prefill
        # 掉到 1/12~1/30（实测评审 4.4 t/s vs 健康 74~308 t/s），而 `ollama ps` 仍报 100% GPU。
        # 探一下只花 ~1s（退化时 ~40s），却能挡掉一次 1000s 级的白等。见 pipeline/perfguard.py。
        # mock 下不探：没有真实服务，探针必然失败。
        if not isinstance(self.client, MockClient):
            gcheck = perfguard.guard(spec.tag, spec.num_ctx, log=self.log)
            if gcheck.get("checked"):
                self.state.setdefault("prefill_checks", []).append(
                    {"stage": record_stage, "tag": spec.tag, **gcheck}
                )
        t0 = time.time()
        data, meta = self.client.chat_json(
            spec,
            system,
            user,
            schema or STAGE_SCHEMAS[stage],
            # 失败尝试即时进账（日志 + llm-calls + state）。见 `_record_attempt` 的说明：
            # 没有它，"连发 6 次、烧 13 分钟、全部丢弃"在外部只能读成"卡死"。
            on_attempt=lambda rec: self._record_attempt(record_stage, note, rec),
            log=self.log,
        )
        # 调用**之后**再判一次：探针通过不等于这次一定健康（退化是逐请求翻转的，
        # 实测 P(退化|上一请求健康)=0.22 —— 本轮 plan 就是这样：探针通过、真调用只有 10.2 t/s）。
        # 只记录不干预 —— 它只影响这一次调用的耗时，不会污染下游（实测退化时生成速率仍正常）。
        # 注意 `meta["prefill_tps"]` 是在下面的 meta 收尾里才算出来的，这里必须**自己算**，
        # 否则 `_pf` 恒为 None、告警永远不触发（第一版就是这样漏掉的）。
        _pf = meta.get("prefill_tps")
        if not _pf and meta.get("prompt_s") and meta.get("prompt_tokens"):
            _pf = round(float(meta["prompt_tokens"]) / float(meta["prompt_s"]), 1)
        if _pf and float(_pf) < perfguard.baseline_for(spec.tag) * perfguard.DEGRADED_RATIO:
            self.log(
                f"        [prefill 告警] 本次 prefill 仅 {_pf} t/s"
                f"（{spec.tag} 基线 {perfguard.baseline_for(spec.tag):.0f}）"
                f" —— 已白等 {meta.get('prompt_s')}s；退化是逐请求翻转的，下一次大概率恢复正常"
            )
        # 记录用素材：完整 system/user 与模型原始输出（含 thinking），写进 traces.jsonl，不进 llm-calls.jsonl
        raw_text = str(meta.pop("_raw_text", ""))
        raw_thinking = str(meta.pop("_raw_thinking", ""))
        failed_attempts = meta.pop("_failed_attempts", []) or []
        meta.update(
            {
                "kind": "llm",
                "stage": record_stage,
                # 这一版用的是哪份系统提示词：换了提示词后产出质量/返工率的变化才能归因，
                # 否则只能去翻 git（还可能翻错那次运行用的是哪一版）。同阶段的第二次调用
                # （接口骨架）用**另一份**系统提示，版本号必须能区分出来。
                # 任务类型也要进版本标识：返工轮用的是**另一套契约**，不带后缀的话
                # 「换了返工口径」会被误归因到别处（与指纹把三套提示词都算进去同一条理由）。
                # 后缀标明"这次调用用的是哪一份系统提示 / 是哪一次调用"：
                #   · 接口骨架那次用 `SKELETON_SYSTEM`（另一份文本）⇒ 必须能区分；
                #   · 逐张施工图（`dev-T-01`）与补漏（`dev-T-01-repair`）用的是**同一份** dev
                #     提示词，但要能按埋点分开统计。
                # 此前一律写成 `+skeleton`，于是真机 `20260928-095848` 里 6 条 dev 调用
                # 全带 `+skeleton`，"骨架调用"与"逐图调用"在埋点里根本分不开 ——
                # 而本轮的返工口径归因正是靠这个字段（哪些轮用了哪套契约）。
                "prompt_version": prompts.prompt_version(
                    stage, self.project_type, self.round_kind
                )
                + (f"+{artifact_stage}" if artifact_stage else ""),
                "truncated": truncated,
                "switched": sched["switched"],
                "resident_before": resident,
                "total_s": round(time.time() - t0, 2),
                "prompt_over_ctx_ratio": (
                    round(meta["prompt_tokens"] / spec.num_ctx, 3)
                    if meta.get("prompt_tokens") and spec.num_ctx
                    else None
                ),
                "prompt_over_budget": bool(
                    meta.get("prompt_tokens") and meta["prompt_tokens"] > spec.num_ctx * PROMPT_HARD_RATIO
                ),
                "note": note,
                "human_feedback_used": bool(extra),
                "_gpu": self._gpu_stats(spec.tag),
            }
        )
        gpu = meta.pop("_gpu", {}) or {}
        meta.update(gpu)
        if meta.get("prompt_s"):
            meta["prefill_tps"] = round(meta["prompt_tokens"] / meta["prompt_s"], 1)
        if meta.get("eval_s"):
            meta["gen_tps"] = round(meta["output_tokens"] / meta["eval_s"], 1)
        if post is not None:
            # 产物的**机械兜底**必须赶在落快照之前做：页面读的就是这份快照，
            # 只在 state 上整理会让「页面显示的」和「下游消费的」变成两份不同的东西
            # （人工照页面裁决，下游却按另一份跑）。
            data = post(data)
        self.calls.append(meta)
        self._record(
            record_stage, data, meta, user,
            system=system, raw_text=raw_text, raw_thinking=raw_thinking,
        )
        self._record_trace(record_stage, system, user, raw_text, raw_thinking, failed_attempts, meta)
        self.log(
            f"        完成 {meta['wall_s']}s (load {meta['load_s']}s, "
            f"prompt {meta['prompt_tokens']}tok @ {meta.get('prefill_tps') or '-'} t/s, "
            f"out {meta['output_tokens']}tok @ {meta.get('gen_tps') or '-'} t/s)"
            + (f" 契约重试 {meta['attempt']} 次" if meta.get("attempt", 1) > 1 else "")
            + ("  [预算告警]" if meta["prompt_over_budget"] else "")
            + (
                f"  [显存告警] 仅 {meta['vram_ratio']:.0%} 在显存（{meta.get('vram_gb')}/{meta.get('model_gb')}GB）"
                if meta.get("vram_ratio") is not None and meta["vram_ratio"] < 0.99
                else ""
            )
        )
        if meta.get("truncated"):
            self.log("        [注意] prompt 超预算已裁剪片段")
        return data

    # ------------------------------------------------------------------ 事实接地检查
    # field 语义：
    #   path         —— 对象数组里的 path 字段
    #   target_files —— 对象数组里的「路径列表」字段
    #   items        —— 该键**本身就是字符串数组**，元素里可能夹着路径/符号
    #                   （reusable_hooks / forbidden_paths 这类，早期漏配导致编造泛滥）
    _PATH_SPECS: dict[str, list[tuple[str, str]]] = {
        "architect_assess": [
            ("modules", "path"),
            ("reusable_hooks", "items"),
            ("forbidden_paths", "items"),
            ("baseline_tests", "items"),
        ],
        "architect_plan": [("changes", "path"), ("tasks", "target_files")],
        "dev": [("edits", "path")],
        # 测试命令里的路径同样要接地：命令是给人复制执行的，指向一个不存在的文件
        # 会让人白跑一趟（且这类编造以前完全没人核对）。
        "test": [("automated_commands", "command")],
    }

    # 从自由文本里抽「形似路径」的片段：至少一段 `/`，允许结尾带 * 通配（如 pipeline/serialization/*.py）
    _PATHLIKE_RE = re.compile(r"[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-\.\*]+)+")

    def _extract_paths(self, artifact: Any, stage: str) -> list[str]:
        paths: list[str] = []
        if not isinstance(artifact, dict):
            return paths
        for key, spec_field in self._PATH_SPECS.get(stage, []):
            items = artifact.get(key)
            if not isinstance(items, list):
                continue
            if spec_field == "items":
                # 元素是自由文本：只挑出其中的路径片段，纯描述性文字自然被忽略
                for entry in items:
                    if isinstance(entry, str):
                        paths.extend(self._PATHLIKE_RE.findall(entry))
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if spec_field == "path" and item.get("path"):
                    paths.append(str(item["path"]))
                elif spec_field == "target_files":
                    paths.extend(str(p) for p in (item.get("target_files") or []))
                elif isinstance(item.get(spec_field), str):
                    # 其它字段名一律当「自由文本」：从中抽出形似路径的片段
                    # （automated_commands[].command 这类）
                    paths.extend(self._PATHLIKE_RE.findall(str(item[spec_field])))
        return paths

    def _allowed_paths(self, stage: str | None = None) -> set[str]:
        """被证明有依据的路径。

        基线 = 检索到的存量片段 + 需求原文里明确写出的文件路径。
        方案之后的阶段（dev / test）另外把**方案已规划的文件**算进来：新建项目的目标
        文件尚不存在，但方案已明确要创建它们，实现与测试引用这些路径不算编造。
        """
        allowed = {exc.path for exc in self.pool}
        allowed |= {m.group(0).lstrip("./") for m in re.finditer(r"[A-Za-z0-9_\-/\.]+\.[A-Za-z0-9]{1,6}", self.requirement)}
        if stage in ("dev", "test"):
            plan = self.state.get("plan") or {}
            for change in plan.get("changes") or []:
                if isinstance(change, dict) and change.get("path"):
                    allowed.add(str(change["path"]))
            for task in plan.get("tasks") or []:
                if isinstance(task, dict):
                    allowed.update(str(p) for p in (task.get("target_files") or []))
        return allowed

    def _grounding_enabled(self, stage: str) -> bool:
        """是否对该阶段做路径接地校验。

        两种情况不校验：
          · **新建项目**：没有存量代码，方案/实现/测试里的路径本来就是新造的，
            拿检索池去核对必然全是误报；
          · **mock 模式**：产物是按 schema 合成的占位符（`<path>` 之类），路径全无
            意义，校验只会制造无意义的重试。沿用 MockClient 既有的同一条约定
            （它给 ASSESSMENT 置空 modules 也是这个原因）。
        """
        if self.project_type == "new" or isinstance(self.client, MockClient):
            return False
        return stage in ("architect_assess", "architect_plan", "dev", "test")

    #: 失败尝试留在 state 里的上限（够诊断，又不至于把 state 撑大）
    _MAX_ATTEMPT_FAILURES = 40

    def _record_attempt(self, stage: str, note: str | None, rec: dict) -> None:
        """把一次**被丢弃的尝试**即时报出去：日志一行 + 埋点一条 + state 一条。

        为什么必须这么做（真机 20260928-110402，这次的教训）：
        第 2 轮 dev 从 11:26:48 到 11:40 首尾相接发了 6 次调用，单次 8.8s / 172s / 314s / 153s
        （同一 prompt，**逐请求退化** 20~35 倍），全部没过契约、被 `chat_json` 内部重试丢掉。
        而埋点原先只写在"调用成功返回"之后 ⇒ `llm-calls.jsonl` 里**一条都没有**，
        trace 也没有，state 也没动。13 分钟的算力烧掉了，事后连"烧在哪"都答不出来 ——
        外部观测只能读成"卡死"，于是人的反应只能是杀掉它。

        现在每一次被丢弃的尝试都会立刻留下：耗时、输出 token、停止原因（stop/length）、
        当次 prefill 速率、契约错误、原文尾部。`gave_up` 的那条还带**整次调用**的总耗时，
        于是"这一轮为什么慢"可以像别的账一样被数出来，而不是靠猜。
        """
        wall = rec.get("wall_s")
        out = rec.get("output_tokens")
        pf = rec.get("prefill_tps")
        if not pf and rec.get("prompt_s") and rec.get("prompt_tokens"):
            pf = round(float(rec["prompt_tokens"]) / float(rec["prompt_s"]), 1)
        errs = [str(e) for e in (rec.get("schema_errors") or [])]
        gave_up = bool(rec.get("gave_up"))
        head = "调用放弃" if gave_up else "尝试未过契约"
        total = f"，本次调用共 {rec.get('wall_total_s')}s / {rec.get('attempts_used')} 次尝试"
        at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.log(
            f"        [{head}] {stage} 第 {rec.get('attempt')}/{rec.get('attempts_planned')} 次"
            f"（{wall}s，out={out} tok，done={rec.get('done_reason') or '-'}"
            f"，prefill={pf or '-'} t/s）{total if gave_up else ''}"
            + (f"：{errs[0][:90]}" if errs else "")
        )
        record = runstore.normalize_call_record(
            {
                "kind": "llm",
                "at": at,
                "stage": stage,
                "note": f"{note or stage}·{'放弃' if gave_up else '失败尝试'}",
                "mock": isinstance(self.client, MockClient),
                # prefill_tps 兜底：生产里 rec 已自带（chat_json._enrich 算的），这里再算一次
                # 是为了**失败尝试的埋点也绝不会缺这一列**——缺了就没法区分"退化烧掉的"还是
                # "内容不合格"，而这正是真机 20260928-110402 想查却查不出的。
                "prefill_tps": pf,
                **rec,
            }
        )
        if self.run_dir is not None and self.mode != "only":
            with (self.run_dir / "llm-calls.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        rows = self.state.setdefault("call_attempt_failures", [])
        rows.append(
            {
                "at": at,
                "stage": stage,
                "note": note,
                "attempt": rec.get("attempt"),
                "gave_up": gave_up,
                "wall_s": wall,
                "wall_total_s": rec.get("wall_total_s"),
                "output_tokens": out,
                "done_reason": rec.get("done_reason"),
                "num_predict": rec.get("num_predict"),
                "prefill_tps": pf,
                "schema_errors": errs[:3],
            }
        )
        if len(rows) > self._MAX_ATTEMPT_FAILURES:
            del rows[: -self._MAX_ATTEMPT_FAILURES]

    def _grounded_call(
        self,
        stage: str,
        parts: list[str],
        note: str | None = None,
        pin: list[str] | None = None,
        artifact_stage: str | None = None,
    ) -> Any:
        """带事实接地的阶段调用。

        `artifact_stage` 原样透传给 :meth:`_call`：给「同一阶段的多次调用」各自一个
        独立的快照名（逐张施工图、接口骨架都走它），避免它们冒充该阶段的正式产物 ——
        续跑时 `_restore` 会用阶段快照覆盖 `state`，冒充的后果是**累积实现被缩成
        最后一次调用的输出**（真机 20260927-134222）。

        产物里出现的路径必须能在检索池 / 需求原文 / 方案清单里找到依据；找不到就先带
        纠正说明重试一次，仍不合格则记入 grounding_warnings（issues 里是**阻断级**）。

        真机教训：编造的路径不会自己消失 —— 它会被下游当成「上游结论里已有的依据」
        承接下去（assess 编出 pipeline/db/*，方案阶段接着把「数据库查询结果」写进验收标准）。
        """
        data = self._call(stage, parts, note=note, pin=pin, artifact_stage=artifact_stage)
        if not self._grounding_enabled(stage):
            return data
        bad = self._ungrounded(data, stage)
        if not bad:
            return data
        self.log(f"        [事实校正] {stage} 以下路径无依据，重试一次: {bad[:5]}")
        retry_parts = parts + [prompts.fact_correction_block(bad, has_code=bool(self.pool))]
        retry_note = f"{note}+grounding-retry" if note else f"{stage}-grounding-retry"
        data = self._call(stage, retry_parts, note=retry_note, pin=pin, artifact_stage=artifact_stage)
        still = self._ungrounded(data, stage)
        if still:
            self.grounding_warnings.append({"stage": stage, "paths": still})
            self.log(f"        [警告] {stage} 重试后仍有未接地路径: {still[:5]}")
        return data

    @staticmethod
    def _path_stem(path: str) -> str:
        """把路径/符号引用归一成主干再比较。

        模型描述扩展点时常写「文件.函数()」形式（如 `pipeline/schemas.validate()`），
        与真实路径 `pipeline/schemas.py` 指向同一处。不归一就会把合法引用误判成编造，
        重试噪声会淹没真正的问题。
        """
        text = str(path or "").strip().split("::")[0].split("(")[0].rstrip("./").lstrip("./")
        last = text.rsplit("/", 1)[-1]
        if "." in last:
            text = text.rsplit(".", 1)[0]
        return text

    #: 一条禁区规则允许出现的字符（路径、glob、连字符）。出现中文/空格/其它符号 ⇒ 它是
    #: 自由文本描述，不是路径。
    #: 注意必须写 **ASCII 字符类**：Python 正则的 ``\w`` 在 Unicode 下**匹配中文**，
    #: 用 `[\w./\\*\-]` 会把 "game_logic.py中tkinter导入" 当成合法路径放过 —— 实测踩过。
    _PATH_RULE_RE = re.compile(r"^[A-Za-z0-9_./\\*\-]+$")

    @classmethod
    def _path_rule_stem(cls, rule: Any) -> str:
        """把一条**禁区规则**归一成可比路径；只在这条规则本身就是路径时才返回主干。

        为什么必须这么严：`_path_stem` 会按最后一个 `.` 截断，于是自由文本规则被折叠成
        **整个文件**禁改 —— `"game_logic.py中tkinter导入"` → `"game_logic"`，任何对
        `game_logic.py` 的改动都判「改动禁改路径」。真机 job-20260926-154657 的 M-01
        （模块名就叫 game_logic，交付物就是 game_logic.py）因此被同时要求
        「实现 game_logic.py 里的这些类」与「不得改 game_logic.py」—— 开发只能声明
        「改动了禁改路径，因此不实现」，反复返工直到触顶。
        非路径规则（含中文/空格/描述文字）一律**不作机械判负**：机械化不了的就别硬判，
        留给模型与人工看。
        """
        text = str(rule or "").strip()
        if not text or not cls._PATH_RULE_RE.match(text):
            return ""
        return cls._path_stem(text)

    def _ungrounded(self, artifact: Any, stage: str) -> list[str]:
        allowed = self._allowed_paths(stage)
        stems = {self._path_stem(a) for a in allowed if self._path_stem(a)}
        bad: list[str] = []
        for path in self._extract_paths(artifact, stage):
            norm = path.strip().lstrip("./")
            if not norm or norm in allowed:
                continue
            stem = self._path_stem(norm)
            if not stem:
                continue
            # 主干相等，或互为父子目录（`pipeline` ↔ `pipeline/server`）都算有依据
            if stem in stems or any(
                s.startswith(stem + "/") or stem.startswith(s + "/") for s in stems
            ):
                continue
            bad.append(path)
        return sorted(set(bad))

    # ------------------------------------------------------------------ 代码池
    def _build_pool(self, query: str) -> list[Excerpt]:
        if self.repo is None:
            return []
        # 新建项目：目标目录可能还不存在（补丁会去创建文件），此时没有可检索的存量代码，
        # 直接返回空池，让下游明确知道「没有代码依据」，而不是抛 FileNotFoundError。
        if not self.repo.exists():
            self.log(f"  仓库目录不存在（{self.repo}），跳过检索（可能是新建项目）")
            return []
        # 池子里每个文件先留够（要装得下目标函数体），各阶段再按自己的上限二次裁剪
        pool = retrieval.select_excerpts(
            self.repo, query=query, token_budget=20000, per_file_tokens=POOL_PER_FILE_TOKENS
        )
        self.log(f"  检索到存量代码片段 {len(pool)} 个（按相关度排序）")
        return pool

    def _code_budget_left(self, stage: str, parts: list[str], pin: list[str] | None = None) -> int:
        """按「总预算 - system - pin - 其它片段」算出还能留给代码片段多少 token。

        固定写死 `CODE_BUDGET` 的毛病：system 与上游片段会先吃掉预算，写死的值一旦偏大，
        取回来的代码片段就会被 `fit_prompt` 从**尾部截断**（最后一个文件只剩半截）。
        按实际余量取则是文件粒度的取舍 —— 宁可少给一个文件，也让进来的每个文件都是完整的。
        `CODE_BUDGET` 仍作为上限生效。
        """
        spec = STAGE_MODELS[stage]
        used = estimate_tokens(prompts.system_prompt(stage, self.project_type, self.round_kind))
        for part in [*parts, *(pin or [])]:
            if part:
                used += estimate_tokens(part)
        return max(spec.prompt_token_budget - used, 0)

    def _code_text(self, stage: str, budget: int | None = None) -> str:
        """按阶段的 token 预算 + 单文件上限切片。dev 给的单文件上限最大（要看到完整目标函数）。

        `budget` 传入时取它与 `CODE_BUDGET` 的较小值（见 `_code_budget_left`）。
        """
        cap = CODE_BUDGET.get(stage, 0)
        budget = cap if budget is None else min(budget, cap)
        per_file_tokens = STAGE_PER_FILE_TOKENS.get(stage, PER_FILE_TOKENS)
        used = 0
        kept: list[Excerpt] = []
        for exc in self.pool:
            # 字符上限按该片段自身的字符/token 比换算（代码与中文差近 3 倍），
            # 固定常数会让代码片段被过度截断。详见 budget.chars_for_tokens。
            per_file_chars = chars_for_tokens(per_file_tokens, sample=exc.text)
            text = retrieval.fit_excerpt(exc.text, per_file_chars) if per_file_chars else ""
            if not text:
                continue
            cost = estimate_tokens(text)
            if used + cost > budget:
                break
            kept.append(
                Excerpt(
                    path=exc.path,
                    text=text,
                    score=exc.score,
                    truncated=exc.truncated or len(text) < len(exc.text),
                    note=exc.note,
                )
            )
            used += cost
        return retrieval.render_excerpts(kept)

    # ------------------------------------------------------------------ 各阶段
    def _stage_intake(self, requirement: str) -> Any:
        """需求入口补强（阶段 0）：跑在 pm 之前，把原始需求整理成结构化初稿。

        产物落快照前先过一遍 ``prompts.tidy_intake``：合并跨字段重复主题、标出「把问题
        原样退回来」的伪默认值。真机 run 20260925-140707 里同一主题（游戏分辨率 /
        游戏窗口尺寸）在缺失要素与澄清问题里各出一条，人工得填两个框，而两条的「建议」
        都是「建议补充：需明确…」，等于没给建议。
        """
        notes: list[str] = []

        def post(data: Any) -> Any:
            tidied, found = prompts.tidy_intake(data)
            notes.extend(found)
            return tidied

        self.state["intake"] = self._call("intake", prompts.parts_intake(requirement), post=post)
        self.state["intake_warnings"] = notes
        for note in notes:
            self.log(f"  [补强] {note}")
        return self.state["intake"]

    def _intake_high_gaps(self) -> list[str]:
        """补强产物里 importance=high 且**尚未人工裁决**的待确认项。

        走 ``prompts.intake_items`` 而不是直接读某个字段：待确认项已合并为单一列表，
        但旧运行（2026-09-25 之前）的产物里是 missing_elements + clarifying_questions
        两个字段，这个读取口负责两种形状都认。

        **必须排掉已裁决的**：这个闸门现在是强控（续跑时会重新判定，见 ``_execute``），
        若把已裁决的也算进来，人工裁决完再续跑会被同一条卡住 —— 永远出不去。
        """
        art = prompts.apply_intake_decisions(self.state.get("intake") or {}, self.intake_decisions)
        return [
            f"{row.get('element')}（建议取值：{row.get('default_assumption') or '未给出，需人工填写'}）"
            for row in prompts.intake_items(art)
            if str(row.get("importance")) == "high"
            and not str(row.get("final_decision") or "").strip()
        ]

    def _stage_pm(self, requirement: str) -> Any:
        # 复用上游补强产物：PM 不必再重新解析原始需求。
        # 人工裁决已并回补强产物本身（final_decision），这里整份注入即可。
        # 确定性准入：折叠三列重复、剔除技术实现类问题（真机 172150：同一问题出现 3 遍、
        # 混进表结构/浮点精度）。提示词只是第一道防线，**必须在 post 钩子里、落快照之前**
        # 归一 —— 否则 02-pm.json 存的是原始产物，续跑时 _restore 拿快照覆盖 state，
        # 被剔除的技术问题原样复活（真机 run 20260928-180933：归一后的 state 被
        # 含 5 条问题的快照覆盖，「数据库表结构」重新下发给下游）。
        pm_parts = prompts.parts_pm(requirement, self.state.get("intake"))
        self.state["scope"] = self._call(
            "pm",
            pm_parts,
            post=lambda data: prompts.normalize_pm_questions(data, log=self.log),
        )
        # ---- 裁决一致性机械校验（P0①，run 20260929-093329）
        # PM 产物可能自相矛盾：open_questions 已裁决「不自动重排序号」，同一份产物的
        # FR 验收却写「删除后序号自动重置」。矛盾一旦进下游就是双向必错，提示词管不住
        # 相隔很远的两段文本 —— 这里先给 PM **一次**确定性返工；返工后仍冲突的，
        # 由 PM 条件闸门暂停交人工（见 _conditional_gate），下游提示词另有"以裁决为准"
        # 的下钉兜底（pm_assumptions_block）。mock 不做（占位产物，测流程不测内容）。
        if not isinstance(self.client, MockClient):
            conflicts = prompts.pm_decision_conflicts(self.state.get("scope"))
            if conflicts:
                lines = prompts.pm_decision_conflict_lines(conflicts)
                self.log(
                    f"        [裁决一致性] 发现 {len(conflicts)} 处裁决与需求/验收极性冲突 → 让 PM 修正一次"
                )
                fix_block = (
                    "【上一版存在自相矛盾，必须修正】\n"
                    + "\n".join(lines)
                    + "\n人工裁决是最高基准：请修改与之矛盾的功能需求/验收标准文本"
                    "（使陈述与裁决结论一致），open_questions 的 final_decision 与 "
                    "confirmed_facts **保持原样不得改动**；其余内容不要重做。"
                )
                baseline = self.state.get("scope")
                try:
                    self.state["scope"] = self._call(
                        "pm",
                        [*pm_parts, fix_block],
                        note="pm·裁决一致性",
                        post=lambda data: prompts.normalize_pm_questions(data, log=self.log),
                    )
                except Exception as exc:  # noqa: BLE001
                    # 返工调用失败不该跑崩整轮：保留首版，冲突留给人工闸门。
                    self.state["scope"] = baseline
                    self.log(
                        f"        [裁决一致性] 返工调用失败（{type(exc).__name__}：{str(exc)[:80]}）"
                        "→ 保留首版，冲突交人工闸门"
                    )
                left = prompts.pm_decision_conflicts(self.state.get("scope"))
                if left:
                    self.log(
                        f"        [裁决一致性] 返工后仍有 {len(left)} 处冲突 → PM 闸门暂停，交人工裁决"
                    )
                else:
                    self.log("        [裁决一致性] 返工后冲突已消除")
        return self.state["scope"]

    def _stage_assess(self, requirement: str) -> Any:
        scope = self.state.get("scope")
        # 先按「无代码片段」估其它片段的开销，再决定代码能拿多少（避免取了又被尾部截断）
        base = prompts.parts_assess(requirement, scope, "", None)
        code = self._code_text("architect_assess", self._code_budget_left("architect_assess", base))
        parts = prompts.parts_assess(requirement, scope, code, None)
        self.state["assessment"] = self._grounded_call("architect_assess", parts)
        return self.state["assessment"]

    def _stage_plan(self, requirement: str, fixes: list[str] | None = None) -> Any:
        scope = self.state.get("scope")
        assessment = self.state.get("assessment")
        # 上游（评估阶段）的编造路径必须让方案看到：否则它会把「数据库」这类幻觉
        # 当成既有事实承接，写进验收标准（真机出现过）。
        warn_block = prompts.grounding_warning_block(self.grounding_warnings)
        pin = [warn_block] if warn_block else None
        # 回退到方案重跑时（评审把根因判为方案层），把运行验证的失败证据与「上一版方案/实现
        # 声明要产出哪些文件」一并给出 —— 让方案能自己判断「是我漏规划了文件，还是补丁没落地」。
        verify = self.state.get("verify_report")
        prev_plan = self.state.get("plan")
        impl = self.state.get("implementation")
        plan_kwargs = {"verify": verify, "prev_plan": prev_plan, "impl": impl}
        base = prompts.parts_plan(requirement, scope, assessment, "", fixes, **plan_kwargs)
        code = self._code_text("architect_plan", self._code_budget_left("architect_plan", base, pin))
        parts = prompts.parts_plan(requirement, scope, assessment, code, fixes, **plan_kwargs)
        self.state["plan"] = self._grounded_call("architect_plan", parts, pin=pin)
        # ---- 施工图必填字段的强制
        # 写进契约不等于会被填：真机 20260927-002903 的 5 张图全部是空的。
        # 缺 symbols ⇒ 粒度无从判定、开发没有自检清单；缺 test_hint ⇒ 沙箱里常常无命令可跑。
        # mock 下不做：mock 的产物是占位数据，本来就填不出这些字段，
        # 强制重试只会每次多两次方案调用，把「流程机制」的测试整个带偏
        # （与覆盖审计的既有惯例一致：mock 测流程，不测内容质量）。
        if isinstance(self.client, MockClient):
            self.state["plan_contract_gaps"] = self._plan_contract_gaps()
            return self.state["plan"]
        def _file_set(plan_obj: Any) -> frozenset[str]:
            """方案规划到的**文件集合** —— 判断重问有没有把方案重做。"""
            out: set[str] = set()
            for c in (plan_obj or {}).get("changes") or []:
                if isinstance(c, dict) and c.get("path"):
                    out.add(str(c["path"]).replace("\\", "/"))
            for t in (plan_obj or {}).get("tasks") or []:
                if isinstance(t, dict):
                    for p in t.get("target_files") or []:
                        out.add(str(p).replace("\\", "/"))
            return frozenset(out)

        baseline = self.state.get("plan")
        baseline_files = _file_set(baseline)
        for attempt in range(1, PLAN_CONTRACT_RETRIES + 1):
            gaps = self._plan_contract_gaps()
            if not gaps:
                break
            # 补字段 = 让模型把整份方案**再吐一遍**。若每张图都缺（缺项数 ≈ 任务数×2），
            # 说明模型根本没在填这个字段，重吐也只是把一份更长的方案塞进 14B 的 8k 上下文
            # —— 真机 20260927-053046 因此输出被截断并抛错。与其花几分钟打一通注定截断的
            # 调用（慢，且现在只能降级收场），不如直接降级、把预算留给后面真正的施工。
            n_tasks = len([t for t in ((self.state.get("plan") or {}).get("tasks") or []) if isinstance(t, dict)])
            if n_tasks and len(gaps) >= 2 * n_tasks:
                self.log(
                    f"        [施工图字段] {n_tasks} 张图全部缺必填字段（{len(gaps)} 项）"
                    "→ 重吐整份方案必然超出上下文，直接降级为提示"
                )
                break
            self.log(
                f"        [施工图字段] {len(gaps)} 项必填缺失（{'；'.join(gaps[:3])}）→ 让方案补齐"
            )
            # 重问必须**容错**：补字段意味着让模型把整份方案再吐一遍，14B 在 8k 上下文下
            # 极易输出被截断并抛 OllamaError（真机 20260927-053046：prompt 2607 tok /
            # num_ctx 8192）。一次"补字段"的失败不该把整轮跑崩 —— 首版方案是完整的，
            # 字段缺失只是降级，远好过整轮中止。
            try:
                self.state["plan"] = self._grounded_call(
                    "architect_plan",
                    [
                        *parts,
                        "【补齐施工图字段】上一版这些任务缺必填字段，必须补上："
                        + "；".join(gaps[:8])
                        + "。其余内容（changes / tasks 的拆分与编号）**保持原样**，不要重做方案。",
                    ],
                    pin=pin,
                    note=f"architect_plan·补字段{attempt}",
                )
            except Exception as exc:  # noqa: BLE001
                self.state["plan"] = baseline
                self.log(
                    f"        [施工图字段] 重问失败（{type(exc).__name__}：{str(exc)[:80]}）"
                    "→ 保留首版方案，字段缺失降级为提示"
                )
                break
            # 让它补字段，它却把方案重做了 —— 真机 20260927-011207：3 文件/4 任务
            # 被改成 4 文件/6 任务。字段补没补上不知道，方案先被推翻了，代价更大。
            # 文件集合一变就回退到首版（字段仍缺，但至少方案还是原来那个已审过的）。
            if baseline_files and _file_set(self.state.get("plan")) != baseline_files:
                self.state["plan"] = self._revert_plan_files(baseline, self.state.get("plan"), baseline_files)
                self.log(
                    "        [施工图字段] 重问后方案的文件清单变了（被重做）→ 文件清单回退到首版，"
                    "但保留重问后**更细的任务拆解**（见 _revert_plan_files）"
                )
                break
        gaps = self._plan_contract_gaps()
        self.state["plan_contract_gaps"] = gaps
        if gaps:
            # 触顶降级：不无限重做（小模型可能真的填不出来），但必须留痕 ——
            # 否则下游会以为"字段都齐了"，而契约比对其实无从下手。
            self.log(
                f"        [施工图字段] 重做 {PLAN_CONTRACT_RETRIES} 次后仍有 {len(gaps)} 项缺失"
                " → 降级为提示（跨文件契约比对将无从下手，已记入 plan_contract_gaps）"
            )
        self.state["plan"] = self._normalize_plan_ids(self.state["plan"])
        self._finalize_plan_compilation()
        # ---- Design Gate：进开发前的唯一机械闸门（容量 / 虚依赖 / 骨架冲突）
        # mock 路径在上面已提前返回；parts / pin 是首版方案的同一份上下文，闸门返工复用。
        self._run_design_gate(parts, pin)
        return self.state["plan"]

    def _finalize_plan_compilation(self, *, freeze: bool = True) -> list[dict]:
        """方案定稿后的**确定性编译链**（可重复执行，Design Gate 自纠/续跑复核共用）：

            [冻结接口基准] → Plan IR（normalize）→ Task Compiler **无条件**编译

        返回编译器的错误清单（容量类）。为什么从 ``_stage_plan`` 抽出来：闸门自纠
        （架构师返工后）与人工闸门续跑强控都要对（可能被改过的）方案重跑这同一条链，
        两处各写一份迟早漂移。

        ``freeze=False`` 供续跑强控复核：复用 state 里已冻结的骨架，**不触发模型调用**
        （人工没让重跑方案阶段，不该偷跑一次 14B；骨架类阻断请走 ``--from architect_plan``）。

        重入时先清掉上一轮的派生产物（skeleton_gaps / plan_unresolved / plan_conflicts /
        plan_compile_errors），否则旧轮残留会被新一轮误读为当前问题。
        """
        self.state.pop("skeleton_gaps", None)
        self.state.pop("plan_unresolved", None)
        self.state.pop("plan_conflicts", None)
        self.state["plan_compile_errors"] = []
        # 方案定稿前**冻结接口基准**。顺序很关键：它是 Plan IR 的符号索引来源 ——
        # `CLI.add` 这类"类.方法"只有靠它才能与 `db.insert_record`（模块.符号）消歧，
        # 没有它就只能按叶子名兜底匹配，而那正是"悄悄连错、无声漏掉"的来源。
        if freeze:
            self._freeze_skeleton()
        return self._normalize_and_compile()

    def _plan_file_imports(self, plan_obj: Any) -> dict[str, list[str]] | None:
        """二开项目：从 repo **现存源码**机械提取方案内文件间的 import 边（AST，不靠模型）。

        执行 DAG 的依赖来源因此为 contract ∪ symbol ∪ 现存 import 图（建议 §三 的二开口径）。
        新建项目文件还不存在（返回 None，依赖退化为前两路）；repo 不可用或方案文件在仓库里
        都还不存在时同样返回 None —— 不猜、不报错。
        """
        if self.project_type == "new" or not isinstance(self.repo, Path) or not self.repo.is_dir():
            return None
        rels: set[str] = set()
        if isinstance(plan_obj, dict):
            for change in (plan_obj.get("changes") or []):
                if isinstance(change, dict) and change.get("path"):
                    rels.add(str(change["path"]).replace("\\", "/").strip())
            for task in (plan_obj.get("tasks") or []):
                if not isinstance(task, dict):
                    continue
                for p in (task.get("target_files") or []):
                    rel = str(p or "").replace("\\", "/").strip()
                    if rel:
                        rels.add(rel)
        sources: dict[str, str] = {}
        for rel in sorted(r for r in rels if r):
            src_file = self.repo / rel
            try:
                if src_file.is_file():
                    sources[rel] = src_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        if not sources:
            return None
        return planir.existing_import_edges(sorted(rels), sources)

    def _normalize_and_compile(self) -> list[dict]:
        """编译链尾部：Plan IR 归一 → Task Compiler 无条件编译（见 :meth:`_finalize_plan_compilation`）。

        单独存在是因为 Design Gate 的「重冻结骨架」分支：``_freeze_skeleton`` 会回填
        tasks[].symbols 并重算 skeleton_gaps，若之后不重跑本方法，回填只落在上一版
        compiled tasks 上，IR 与执行图仍是旧的 —— 三份口径重新分裂。
        """
        # ---- Normalize：raw_plan → compiler_ir（**编译器的唯一稳定输入**）
        # 合并 / 清洗 / 冲突解决 / 推导全部在这里做完，Compiler 之后只读 IR。
        plan_obj0 = self.state.get("plan")
        file_imports = self._plan_file_imports(plan_obj0)
        # 规格§三十四：人工裁决并回后再取 intake 条目，默认假设 DERIVED、裁决 ASSERTED
        intake_merged = prompts.apply_intake_decisions(
            self.state.get("intake") or {}, self.intake_decisions
        )
        ir = planir.normalize_plan(
            plan_obj0,
            skeleton=self.state.get("skeleton") or {},
            previous=self.state.get("compiler_ir"),
            file_imports=file_imports,
            scope=self.state.get("scope"),
            original_requirement=self.requirement or "",
            intake_rows=prompts.intake_items(intake_merged),
        )
        self.state["compiler_ir"] = ir
        self.state["plan_sources"] = ir.get("plan_sources")
        if file_imports:
            n_edges = sum(len(v) for v in file_imports.values())
            self.log(f"        [Plan IR] 二开现存 import 图贡献 {n_edges} 条文件依赖边（执行 DAG 第三来源）")
        conflicts = ir.get("conflicts") or []
        if conflicts:
            self.state["plan_conflicts"] = conflicts
            self.log(
                f"        [Plan IR] 归一化处理了 {len(conflicts)} 处冲突/合并："
                + "；".join(str(c.get("detail") or c.get("kind")) for c in conflicts[:3])
            )
        unresolved = ir.get("warnings") or []
        if unresolved:
            # unresolved **必须暴露**而不是隐藏：隐藏的依赖错误会在运行时变成
            # `AttributeError`，那时归因成本高一个数量级。
            self.state["plan_unresolved"] = unresolved
            self.log(
                f"        [Plan IR] {len(unresolved)} 条跨文件依赖**解析不了**（不猜，显式暴露）："
                + "；".join(f"{u.get('symbol')}（{u.get('reason')}）" for u in unresolved[:3])
            )
        diff = ir.get("diff_vs_previous") or {}
        if diff.get("removed_units"):
            self.log(
                f"        [Plan IR] 相对上一版方案少了 {len(diff['removed_units'])} 张施工图："
                + "、".join(diff["removed_units"][:4])
            )
        # ---- Task Compiler：施工图的**唯一执行出口**（always compile）
        # 旧实现只在 plan_needs_compile 判"架构师原图不合格"时才编译，结果真机出现过
        # 「架构师改了 main.py 的合并方式，编译器因『无需编译』整轮白算」。现在无论原图
        # 是否合格都从 Plan IR 重新生成 —— 执行的每一张图都必然经过同一套确定性规则；
        # 架构师原图存 ``state.plan_draft_tasks`` 仅作设计溯源。
        plan_obj = self.state.get("plan")
        reasons = taskcompiler.plan_needs_compile(plan_obj)
        # 降级为**纯审计信号**：不再决定要不要编译，只记录"架构师原图与编译器口径的偏差"，
        # 供观察模型拆分质量的趋势（提示词/编译器改动后这个值应趋近于零）。
        self.state["plan_compiled_reasons"] = reasons
        if reasons:
            self.log(
                "        [Task Compiler·审计] 架构师原图与编译口径有偏差（"
                + "；".join(reasons[:3])
                + "）——不影响执行：施工图始终以编译器产物为准"
            )
        if isinstance(plan_obj, dict):
            draft = plan_obj.get("tasks")
            if draft:
                self.state["plan_draft_tasks"] = draft
        result = taskcompiler.compile_plan(
            plan_obj, ir=ir, existing_files=self._existing_repo_files(),
            # 上一版**已编译**施工图（不是架构师 draft）：用于 semantic_task_id 版本链。
            previous_tasks=self.state.get("plan_compiled_tasks"),
        )
        errors = list(result.get("errors") or [])
        compiled = list(result.get("tasks") or [])
        self.state["plan_compile_errors"] = errors
        if compiled:
            self.state["plan_compiled_tasks"] = compiled
        if compiled and isinstance(plan_obj, dict):
            plan_obj["tasks"] = compiled
            plan_obj["tasks_compiled"] = True
            # 重新算一次：编译后的 tasks 应当**字段齐全**
            self.state["plan_contract_gaps"] = self._plan_contract_gaps()
            self.log(f"        [Task Compiler] 从 Plan IR 生成 {len(compiled)} 张施工图（唯一执行出口）")
        for err in errors:
            self.log(f"        [Task Compiler·错误] {err.get('detail')}")
        self.state["plan"] = self._ensure_entry_task(plan_obj)
        # 方案定稿的语义图也留一个只追加版本（Requirement/Claim/PO/Constraint 投影）。
        plan_ont = ir.get("ontology")
        if isinstance(plan_ont, dict):
            self._persist_ontology(ontology.OntologyGraph.from_dict(plan_ont), "architect_plan")
        # 方案§三十：确定性 Task Plan 落 Artifact envelope（derived_from architect_plan）。
        self._record_task_plan(ir, compiled, errors)
        return errors

    #: compile error code → Design Gate 阻断 kind。容量与依赖两类都是
    #: 「编译器机械判定、开发无权修」的方案层硬冲突。
    _COMPILE_ERROR_KINDS = {
        "task_capacity_exceeded": "task_capacity_exceeded",
        "unknown_dependency": "dependency_invalid",
        "self_dependency": "dependency_invalid",
        "dependency_cycle": "dependency_invalid",
    }

    def _design_gate_blockers(self) -> list[dict]:
        """Design Gate 的**机械判据**：阻断全部不依赖模型主观判断。

          ① ``task_capacity_exceeded`` —— 编译器报告：单文件符号总量装不进图上限
             （开发无权重拆方案，带下去每张图都必写不完）；
          ② ``dependency_invalid``      —— 编译器报告：依赖目标不存在 / 自依赖 / 依赖成环
             （执行 DAG 无法确定施工顺序，或开发会引用一张根本不存在的前置图）；
          ③ ``contract_unresolved``     —— Plan IR：施工图引用了产物内不存在的符号
             （stdlib/三方已在 planir 里归入 externals，不会误报）；
          ④ ``skeleton_mismatch``      —— 方案 changes 声明的符号不在刚冻结的接口基准里
             （dev 会同时收到两份互相矛盾的要求）。
        """
        blockers: list[dict] = []
        for err in self.state.get("plan_compile_errors") or []:
            if isinstance(err, dict):
                kind = self._COMPILE_ERROR_KINDS.get(str(err.get("code") or ""), "compile_error")
                blockers.append({"kind": kind, **err})
        ir = self.state.get("compiler_ir")
        if isinstance(ir, dict):
            for unit in ir.get("units") or []:
                if not isinstance(unit, dict):
                    continue
                path = str(unit.get("file") or "")
                for sym in (unit.get("unresolved_uses") or []):
                    blockers.append(
                        {
                            "kind": "contract_unresolved",
                            "file": path,
                            "symbol": str(sym),
                            "detail": f"{path}: 引用了产物内不存在的符号 {sym}（开发无权新增方案外接口）",
                        }
                    )
        for gap in self.state.get("skeleton_gaps") or []:
            blockers.append({"kind": "skeleton_mismatch", "detail": str(gap)})
        # ④' Ontology：冻结骨架相对方案的**越权**（方向与 skeleton_gaps 相反）——
        # 第二次 LLM 调用私自新增文件/扩大符号面，属于方案层自相矛盾，开发无权跟着扩。
        plan_obj = self.state.get("plan")
        skeleton = self.state.get("skeleton")
        if isinstance(plan_obj, dict) and isinstance(skeleton, dict) and skeleton:
            overreach = planir.skeleton_overreach(plan_obj, skeleton)
            for extra_file in overreach.get("extra_files") or []:
                blockers.append({
                    "kind": "skeleton_overreach_file",
                    "file": extra_file,
                    "detail": f"接口骨架新增了方案 changes 未规划的文件 {extra_file}（骨架无权扩大方案边界）",
                })
            for extra_path, names in (overreach.get("extra_symbols") or {}).items():
                blockers.append({
                    "kind": "skeleton_overreach_symbol",
                    "file": extra_path,
                    "symbols": names,
                    "detail": f"{extra_path}: 骨架多出方案未声明的符号 {'、'.join(names)}（以方案为准重冻骨架或回架构师）",
                })
        # ④'' Ontology：PM 的每条 Requirement 必须能确定性挂到至少一个施工单元。
        # 全量挂不上（不是个别 FR）才阻断 —— 个别匹配失败常见于 FR 与文件名/符号完全
        # 无字面重合，留作 ontology_design_problems 暴露；**所有**需求都无落点则方案与
        # 需求无关，是方案层硬矛盾（规格§二十六：Task 必须 implements Requirement）。
        if isinstance(ir, dict):
            links = ir.get("ontology_links")
            if isinstance(links, dict):
                unlinked = list(links.get("unlinked_requirements") or [])
                by_file = links.get("by_file") or {}
                linked_count = len({r for v in by_file.values() for r in (v.get("requirements") or [])})
                self.state["ontology_design_problems"] = {
                    "unlinked_requirements": unlinked,
                    "unlinked_files": list(links.get("unlinked_files") or []),
                    "linked_requirement_count": linked_count,
                }
                scope = self.state.get("scope")
                total = len(ontology.requirement_claims(scope)) if isinstance(scope, dict) else 0
                if total and unlinked and linked_count == 0 and (ir.get("units") or []):
                    blockers.append({
                        "kind": "requirement_unanchored",
                        "detail": (
                            f"PM 的 {total} 条功能需求没有一条能机械挂到施工单元"
                            "（文件/符号/变更描述与需求无字面重合）——方案与需求脱节，"
                            "请回架构师按需求重拆，而不是让开发自行揣测"
                        ),
                    })
        # ⑤ ``uses_cycle`` —— contracts.uses 在任务**之间**成环。
        # g4 会把 uses 翻译成文件头**必须**写的顶层 import：uses 成环 ⇒ 顶层互导 ⇒
        # 运行即 ImportError（真机 20260928-221831：errors↔cli，三轮没修掉）。
        # 这是方案层缺陷（依赖方向画反 / uses 抄串），开发无权改方案，必须在闸门消掉。
        hint_ctx = tasktype.import_hint_context(self.state.get("plan"))
        for tid, mods in (hint_ctx.get("cyclic_modules") or {}).items():
            blockers.append(
                {
                    "kind": "uses_cycle",
                    "detail": (
                        f"{tid} 的 contracts.uses 依赖 {'、'.join(mods)}，"
                        "而对方任务又反向依赖本任务的模块 —— 顶层 import 必然成环。"
                        "请改依赖方向（底层/被引用方不得反向依赖调用方），"
                        "或删掉该 uses（通过参数传对象，而不是互导）。"
                    ),
                }
            )
        # 注意：任务 uses 自己 target_files 里的模块**不阻断** —— 一张施工图合法地可以
        # 同时覆盖多个文件（cli.py 用 database.py 是两文件间的正常导入）。dev 侧提示层
        # 会自行避免"文件 import 自己"那一种（见 task_focus_block 的 own_modules 丢弃）。
        return blockers

    def _design_gate_reask(self, parts: Any, pin: Any, blockers: list[dict]) -> None:
        """带阻断清单回架构师重做一次方案。**允许**新增文件/重拆任务（与补字段不同）：
        容量超限的正解就是把符号摊到更多文件。调用失败不崩 —— 保留现方案，阻断交人工。
        """
        lines = [f"- （{b.get('kind')}）{b.get('detail')}" for b in blockers[:10]]
        kinds = {str(b.get("kind") or "") for b in blockers}
        tail = (
            "\n请据此调整方案：把对不存在符号的引用改成真实接口，或把缺失符号补进对应"
            " 文件的 changes/tasks；单文件符号超过容量时，重新拆分文件边界（允许新增文件），"
            "但不要改动与这些冲突无关的设计。"
        )
        if "dependency_invalid" in kinds:
            # 真机 20260928-160609：阻断是依赖环，而通用指引只讲符号/容量 ——
            # 模型不知道该动 depends_on，两轮自纠都在修不相干的 main()。
            tail += (
                "\n其中依赖类冲突（目标不存在 / 成环）要动的是任务的 **depends_on**："
                "按上面环清单标注的边来源，删除或改向「方案声明」的依赖（底层/被引用方"
                "不得反向依赖调用方）；不要靠新增符号或重命名来回避。"
            )
        if "uses_cycle" in kinds:
            # uses 成环不是 depends_on 问题，要动的是 contracts.uses：
            # 删掉反向引用 / 自引用，或把跨层回调改成参数传入。
            tail += (
                "\n其中 uses_cycle 要动的是相关任务的 **contracts.uses**（不是 depends_on）："
                "删掉导致成环/自引用的条目；调用方若必须通知底层，用参数把回调传进去，"
                "不要让底层模块反向 import 调用方。"
            )
        msg = (
            "【设计闸门阻断】以下是编译器 / 接口基准在方案定稿时机械判定出的硬冲突，"
            "进开发前必须消除 —— 开发被约束在你的 changes 范围内，无权改方案，"
            "带着这些冲突下去必然被判负：\n"
            + "\n".join(lines)
            + tail
        )
        try:
            new_plan = self._grounded_call(
                "architect_plan",
                [*parts, msg],
                pin=pin,
                note="architect_plan·设计闸门返工",
            )
        except Exception as exc:  # noqa: BLE001
            self.log(
                f"        [设计闸门] 返工调用失败（{type(exc).__name__}：{str(exc)[:80]}）"
                " → 保留当前方案，阻断项交人工裁决"
            )
            return
        self.state["plan"] = new_plan
        self.log("        [设计闸门] 架构师已按阻断清单重做方案（允许新增文件 / 重拆任务）")

    def _run_design_gate(self, parts: Any, pin: Any) -> None:
        """进开发前的闸门本体：阻断 → 有限自纠 → 仍阻断则挂人工强控（不带错进 DEV）。"""
        self.state.pop("design_gate_blocked", None)
        self.state.pop("design_gate_attempts", None)
        blockers = self._design_gate_blockers()
        attempts = 0
        # 每次「动手自纠前」见过的阻断类型：最终用于区分「同一类问题两轮没修好」
        # 与「旧冲突已消除、新方案反而引入了新冲突」（真机 160609：假符号→main()→
        # 依赖环，三次是三个不同类型，笼统称「仍未消除」会把问题性质说错）。
        ever_seen: set[str] = set()
        while blockers and attempts < DESIGN_GATE_RETRIES:
            cur_kinds = {str(b.get("kind") or "") for b in blockers}
            ever_seen |= cur_kinds
            if attempts == 0 and cur_kinds == {"skeleton_mismatch"}:
                # 第一选择是怀疑**骨架自己**：它也是一次 14B 调用，可能是它枚举错了。
                # 重冻结成本与方案返工相同但不推翻设计，先做这一步。
                self.log(
                    "        [设计闸门] 方案声明与接口基准冲突 → 先重新冻结一次骨架"
                    "（骨架自身也是模型产物，可能是它错）"
                )
                self.state.pop("skeleton_gaps", None)
                self._freeze_skeleton()
                # 重冻结会回填 symbols、重算 gaps：必须重跑 IR 归一 + 编译，
                # 否则回填只落在上一版 compiled tasks 上，IR / 执行图仍是旧口径。
                self._normalize_and_compile()
            else:
                self.log(
                    f"        [设计闸门] {len(blockers)} 项阻断 → 带清单回架构师自纠"
                    f"（第 {attempts + 1}/{DESIGN_GATE_RETRIES} 次）"
                )
                self._design_gate_reask(parts, pin, blockers)
                self.state["plan"] = self._normalize_plan_ids(self.state["plan"])
                self._finalize_plan_compilation()
            attempts += 1
            blockers = self._design_gate_blockers()
        if blockers:
            self.state["design_gate_blocked"] = blockers
            self.state["design_gate_attempts"] = attempts
            final_kinds = {str(b.get("kind") or "") for b in blockers}
            fresh_kinds = sorted(final_kinds - ever_seen)
            stale_kinds = sorted(final_kinds & ever_seen)
            fresh_note = (
                f"；其中 {('、'.join(fresh_kinds))} 是最后一版方案**新引入**的"
                if fresh_kinds else ""
            )
            stale_note = (
                f"（前序自纠仍未消除：{'、'.join(stale_kinds)}）" if stale_kinds else ""
            )
            self.log(
                f"        [设计闸门] {len(blockers)} 项阻断在 {attempts} 次自纠后仍未放行"
                f"{fresh_note}{stale_note} → 停人工闸门，不带坏方案进开发："
            )
            for item in blockers[:6]:
                # 环类 detail 是多行（含逐边来源），控制台只打首行；全文在 state/handoff 里。
                first_line = str(item.get("detail") or "").splitlines()[0][:160]
                self.log(f"          · [{item.get('kind')}] {first_line}")

    def _design_gate_resume_check(self) -> list[dict]:
        """人工闸门续跑时的**强控复核**：对（可能被人工编辑过的）方案重跑确定性编译链。

        与 pm/intake 闸门同一条纪律 —— 人工什么都不改直接点继续，必须被原地再停一次。
        不调模型：骨架不重新冻结（骨架类阻断需整体重跑方案阶段，用
        ``--from architect_plan``），只用 state 里已冻结的基准重算冲突。
        """
        plan_obj = self.state.get("plan")
        if not isinstance(plan_obj, dict):
            return []
        self._finalize_plan_compilation(freeze=False)
        # finalize(freeze=False) 清掉了旧 gaps：用已冻结的基准对（可能被改过的）方案
        # **机械重算**一次声明差，不重新生成骨架。
        skeleton = self.state.get("skeleton") or {}
        if skeleton:
            gaps = self._declared_vs_skeleton(skeleton)
            if gaps:
                self.state["skeleton_gaps"] = gaps
        return self._design_gate_blockers()

    def _freeze_skeleton(self) -> dict:
        """方案定稿后单独喂一次「只列结构、不写实现」的调用，产出**冻结的接口基准**。

        为什么必须单独一次（真机实证）：`architect_plan` 一次要同时定文件、定接口、拆任务、
        写最小性论证，`tasks[].symbols` **连续多轮全空**（20260927-073518 / 082239 / 060300），
        于是开发没有接口准绳、只能按类名猜，跨文件必然错配；而 verify 的跨文件契约校验
        （``contract_check``）核的是方案里 ``interface`` / ``contracts``，那两项契约
        **只提示不强制**、通常为空 ⇒ 校验形同虚设。把「定接口」拆出来单独喂：模型只需
        **枚举结构**（14B 在清单类输出上稳定得多），且与方案共用同一段 14B 驻留。

        产出（与 ``verify.api_digest`` 同形的摘要）有三个消费方：
          · 钉进 dev 提示词 —— 接口基准，禁止擅自改名/改参数（``prompts.skeleton_block``）；
          · 回填 ``tasks[].symbols`` —— 施工图符号自检、覆盖审计、符号消失检测的共同判据；
          · 交给跨文件契约校验当**基准**（见 ``contract_check`` 的调用点）。

        **失败必须静默降级**：它是增强手段 —— 骨架没生成，方案照样可用，不能因此崩掉整轮。
        """
        plan = self.state.get("plan")
        if isinstance(self.client, MockClient) or self.project_type != "new" or not isinstance(plan, dict):
            return {}
        try:
            data = self._call(
                "architect_plan",
                prompts.parts_skeleton(self.state.get("scope"), plan),
                note="architect_plan·接口骨架（冻结基准）",
                schema=SKELETON,
                system=prompts.SKELETON_SYSTEM,
                artifact_stage="architect_skeleton",
            )
        except Exception as exc:  # noqa: BLE001
            self.log(
                f"        [接口骨架] 生成失败，跳过（方案照常往下走）：{type(exc).__name__}: {str(exc)[:90]}"
            )
            return {}
        digest = verify_mod.skeleton_digest(data)
        if not digest:
            self.log("        [接口骨架] 产物为空或不可解析 → 跳过冻结")
            return {}
        self.state["skeleton"] = digest
        self.log(
            f"        [接口骨架] 冻结 {len(digest)} 个文件 / "
            f"{sum(len(v) for v in digest.values())} 条接口"
        )
        filled = self._backfill_task_symbols(digest)
        if filled:
            self.log(f"        [接口骨架] 回填了 {filled} 张施工图的 symbols（原先为空）")
        # **立刻**核对"方案声明的符号是否都在基准里" —— 而不是等 verify 才发现骨架与产物不一致。
        # 这两份东西会同时喂给 dev 且都被注明"必须逐字一致"，不一致时 dev 怎么写都要挨一边的判。
        gaps = self._declared_vs_skeleton(digest)
        if gaps:
            self.state["skeleton_gaps"] = gaps
            self.log(
                f"        [接口基准] ⚠ 方案声明的 {len(gaps)} 个符号**不在基准里** —— "
                "dev 会同时收到两份互相矛盾的要求（声明 vs 基准），必然被判「漏了声明过的符号」："
            )
            for line in gaps[:4]:
                self.log(f"          - {line[:110]}")
        return digest

    #: Python 入口守卫惯用法（``if __name__ == '__main__':``）—— 它不是类/函数/常量，
    #: 接口骨架（只枚举这三类）天然不含它；方案把它写进 symbols 时不应判 skeleton_mismatch。
    #: 真机 run 20260928-180933：该惯用法连续触发两次误判，白烧一次骨架冻结 + 一次方案返工。
    _MAIN_GUARD_RE = re.compile(r"^if\s+__name__\s*==\s*['\"]__main__['\"]\s*:?$")

    def _is_main_guard_symbol(self, sym: Any) -> bool:
        text = str(sym or "").strip()
        return bool(self._MAIN_GUARD_RE.match(text))

    def _declared_vs_skeleton(self, digest: dict) -> list[str]:
        """方案 ``changes[].symbols`` 里哪些**不在**刚冻结的接口基准里。

        为什么要提前核对（而不是等 verify 的 `skeleton_problems`）：那两份要求是**同时**
        喂给 dev 的，且都被注明"必须逐字一致"。声明有、基准没有 ⇒ dev 无论怎么写都会被
        其中一方判错（真机 `20260928-095848`：施工图要 `main()` / `CLI()`，基准给的是
        `class App` / `def main(args)`，于是 5/5 张图的自检全报"漏了声明过的符号"，
        而"补漏"又因另一个 bug 空转）。提前报出来，才能区分**输入构造的锅**与**模型输出的锅**。

        基准里没有该文件的成员信息时**不下结论** —— 否则会全是假警报（与 resolver 同一条口径）。
        """
        plan = self.state.get("plan")
        if not isinstance(plan, dict):
            return []
        files = [
            str(c.get("path")) for c in (plan.get("changes") or [])
            if isinstance(c, dict) and c.get("path")
        ]
        idx = symbol_resolver.build_index(files, digest)
        members = idx.get("file_members") or {}
        gaps: list[str] = []
        for change in (plan.get("changes") or []):
            if not isinstance(change, dict):
                continue
            path = str(change.get("path") or "").replace("\\", "/")
            known = members.get(path)
            if not known:
                continue
            for sym in (change.get("symbols") or []):
                if self._is_main_guard_symbol(sym):
                    continue  # 入口守卫不是接口符号，骨架不可能枚举它（见 _MAIN_GUARD_RE）
                name = symbol_resolver.clean_symbol(sym)
                if not name:
                    continue
                if name not in known and name.rsplit(".", 1)[-1] not in known:
                    gaps.append(f"{path}: {sym}（基准里没有）")
        return gaps

    def _backfill_task_symbols(self, digest: dict) -> int:
        """用冻结的接口基准回填 ``tasks[].symbols``（**只填空的，不覆盖已声明的**）。

        为什么必须回填：``symbols`` 是施工图符号自检（``_task_symbol_gaps``）、覆盖审计、
        符号消失检测的共同判据 —— 而真机上它长期是空的，空判据等于没有判据。

        只认「类名 + 模块级函数名」两类**对外**符号：方法不进 symbols（它由所属类覆盖），
        否则同一张图会背上整份文件的全部方法名，把自检变成噪声。
        """
        plan = self.state.get("plan")
        if not isinstance(plan, dict):
            return 0

        def _names(path: Any) -> list[str]:
            out: list[str] = []
            for line in digest.get(str(path or "").replace("\\", "/")) or []:
                text = str(line)
                if text.startswith("class "):
                    out.append(text[len("class "):].split("(")[0].strip())
                elif text.startswith("def "):
                    m = re.match(r"def\s+(\w+)", text)
                    if m:
                        out.append(m.group(1))
            return [n for n in out if n]

        filled = 0
        for task in plan.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            if [s for s in (task.get("symbols") or []) if str(s or "").strip()]:
                continue  # 方案自己声明了就不动它
            names: list[str] = []
            for p in task.get("target_files") or []:
                for n in _names(p):
                    if n not in names:
                        names.append(n)
            if names:
                task["symbols"] = names
                filled += 1
        return filled

    #: 被认作「程序入口」的文件名（与 verify 的入口探测保持一致）
    _ENTRY_NAMES = ("main.py", "__main__.py", "run.py", "app.py", "cli.py")

    def _ensure_entry_task(self, plan: Any) -> Any:
        """方案没规划入口文件 ⇒ 补一张「入口」施工图。

        真机反复出现（20260927-073518 / 082239）：方案列了 4~5 个文件却没有 main.py，
        verify 于是只能报「没有任何命令真正执行了交付物 —— 产物是否可运行未被验证」，
        这一条**每次**都把整轮打回去，而纠正它要白白烧掉一整轮（评审提出 → 提到方案层 →
        重跑）。入口是「产物能不能跑」的唯一证据来源，缺它整条链都判不了成，
        所以在方案落盘时就机械补齐 —— 补一条 change + 一张图，开发照常按图施工。
        """
        if not isinstance(plan, dict):
            return plan
        paths = {
            str(c.get("path") or "").replace("\\", "/")
            for c in (plan.get("changes") or [])
            if isinstance(c, dict) and c.get("path")
        }
        if any(any(p == n or p.endswith("/" + n) for n in self._ENTRY_NAMES) for p in paths):
            return plan
        if any(self._repo_has_file(n) for n in self._ENTRY_NAMES):
            return plan  # 仓库里已有入口（二次开发的常态），不重复造
        entry = "main.py"
        ids = [
            str(t.get("id") or "")
            for t in (plan.get("tasks") or [])
            if isinstance(t, dict) and t.get("id")
        ]
        nxt = f"T-{len(ids) + 1:02d}"
        changes = list(plan.get("changes") or [])
        changes.append(
            {
                "path": entry,
                "intent": "程序入口：把各模块接起来并启动",
                "approach": "定义 main() 并在 `if __name__ == '__main__':` 里调用，"
                "实例化其余模块提供的类并启动主流程",
                "minimality_reason": "入口是运行验证唯一的执行对象，缺它无法证明产物可运行",
            }
        )
        tasks = list(plan.get("tasks") or [])
        tasks.append(
            {
                "id": nxt,
                "title": "实现程序入口",
                "target_files": [entry],
                "acceptance": f"`python {entry}` 能启动程序（常驻程序在超时前持续运行即算能跑起来）",
                "depends_on": ids,
            }
        )
        plan["changes"] = changes
        plan["tasks"] = tasks
        self.log(
            f"        [入口补齐] 方案没规划入口文件 → 自动补 {entry}（change + 施工图 {nxt}），"
            "否则运行验证无从证明产物能跑"
        )
        return plan

    def _normalize_plan_ids(self, plan: Any) -> Any:
        """方案落盘前把任务 id（tasks[].id / depends_on）剥掉引号等包装。

        入口清洗一次，下游（覆盖审计、按 task 分派、拓扑排序）就不必各自防御 ——
        真机证明「各自防御」最容易漏掉某一处，而漏掉的那处就是返工级联的起点。
        """
        if not isinstance(plan, dict):
            return plan
        for task in plan.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            if task.get("id"):
                task["id"] = self._clean_task_id(task.get("id"))
            if isinstance(task.get("depends_on"), list):
                task["depends_on"] = [
                    self._clean_task_id(d) for d in task["depends_on"] if str(d or "").strip()
                ]
        return plan

    @staticmethod
    def _revert_plan_files(baseline: Any, retried: Any, baseline_files: frozenset[str]) -> Any:
        """文件清单回退到首版，但**保留重问后更细的任务拆解**。

        原先写的是 ``self.state["plan"] = baseline`` —— 整个方案一起退，任务拆解也跟着
        回到首版。真机 20260927-041422：首版是「**1 个任务扛 5 个文件**」，重问后拆成了
        4~5 个任务（这本身是改进，且 symbols / test_hint 也补上了），却因为文件清单变了
        被整体回退 —— 于是按 1 个任务走两遍模式：7B 一次要写 5 个文件，既写不深，也
        **不满足按 task 分派的前提**（任务数 < 2 就不分派），单条 task 的饱和开发无从谈起。

        所以只回退 ``changes``（文件清单仍以审过的首版为准），``tasks`` 保留重问后那些
        **只碰首版文件**的任务 —— 前提是它确实拆得更细，否则没有价值。
        """
        if not isinstance(baseline, dict):
            return baseline
        out = dict(baseline)
        if not isinstance(retried, dict):
            return out
        keep: list[dict] = []
        for t in retried.get("tasks") or []:
            if not isinstance(t, dict):
                continue
            paths = [str(p).replace("\\", "/") for p in (t.get("target_files") or []) if str(p or "").strip()]
            if paths and all(p in baseline_files for p in paths):
                keep.append(t)
        base_tasks = [t for t in (baseline.get("tasks") or []) if isinstance(t, dict)]
        if len(keep) > len(base_tasks):
            out["tasks"] = keep
        return out

    def _plan_contract_gaps(self) -> list[str]:
        """施工图缺哪些**必填字段**（机械判定，不依赖模型自觉）。

        只强制两项：
          · `symbols` —— 粒度判据 + 开发的自检清单（漏定义会被符号消失检测抓到）；
          · `test_hint` —— 一条可执行的验收命令（没有它，沙箱常常无命令可跑）。
        `interface` / `contracts` 只提示不强制：它们依赖跨文件设计，强行要求会让小模型编造。
        """
        plan = self.state.get("plan") or {}
        # 每个文件在**边界里声明了哪些符号**（架构师的能力范围内）。
        # 没声明的文件（如只有入口逻辑的 main.py）不该被要求"必须有 symbols"——
        # 那等于要求它凭空造出边界里没有的东西。
        declared: dict[str, list[str]] = {}
        for change in (plan.get("changes") or []):
            if not isinstance(change, dict):
                continue
            path = str(change.get("path") or "").replace("\\", "/").strip()
            if path:
                declared.setdefault(path, []).extend(
                    str(s).strip() for s in (change.get("symbols") or []) if str(s).strip()
                )
        gaps: list[str] = []
        for task in (plan.get("tasks") or []):
            if not isinstance(task, dict):
                continue
            tid = str(task.get("id") or "?")
            files = [str(f).replace("\\", "/") for f in (task.get("target_files") or [])]
            expects_symbols = any(declared.get(f) for f in files)
            if expects_symbols and not [s for s in (task.get("symbols") or []) if str(s).strip()]:
                gaps.append(f"{tid} 缺 symbols")
            if not str(task.get("test_hint") or "").strip():
                gaps.append(f"{tid} 缺 test_hint")
            if not str(task.get("change") or "").strip():
                # 「改什么」是 DEV 最需要的一条：objective 说目标、symbols 说涉及符号，
                # 唯独它说"这次具体改成什么样"。schema 已必填，这里再兜一次。
                gaps.append(f"{tid} 缺 change")
        return gaps

    @staticmethod
    def _symbol_set(impl: Any) -> set[str]:
        """实现产物声明的「文件::符号」集合（返工退化检测用）。"""
        out: set[str] = set()
        for edit in (impl or {}).get("edits") or []:
            if not isinstance(edit, dict):
                continue
            path = str(edit.get("path") or "").strip()
            symbol = str(edit.get("target_symbol") or "").strip()
            if path and symbol:
                out.add(f"{path}::{symbol}")
        return out

    def _stage_dev(self, requirement: str, fixes: list[str] | None = None) -> Any:
        # 先作废旧的在制工作区引用：本轮缺陷单 / 首图 current_code 必须以上一轮
        # verify 沙箱为准；新的 dev-wip 在 _dev_by_tasks 开头重建（G4）。
        self._wip_dir = None
        self._wip_head_revision = ""
        prev = self.state.get("implementation") or {}
        # 「这一轮让哪些符号消失了」——必须在下面覆盖 `implementation_symbols_prev` **之前**
        # 算出来：它是"上上轮有、上一轮没了"的差集，也就是返工退化的实证。
        # 只判负不够（判负只说"打回去改"），要点名，见 prompts.dev_regression_block。
        lost_symbols = sorted(
            set(self.state.get("implementation_symbols_prev") or []) - self._symbol_set(prev)
        ) if prev else []
        # 先记下上一轮的符号集合：本轮若让某个符号消失，必须显式声明（见 _audit_implementation）。
        # 真机 run 20260924-185507：第 1 轮产出了 InputHandler，第 2/3 轮它**悄悄消失**，
        # 没人发现，直到第 3 轮评审"通过"、人工实测才发现方向控制没了 —— 返工退化是真实存在的。
        # 只有拿到"当前实现"时才更新基准：人工打回后实现已被 _rewind 清空，
        # 那时必须保留 _rewind 存下的那份，否则基准被清空 = 检测彻底失效。
        prev_symbols = sorted(self._symbol_set(prev))
        if prev_symbols:
            self.state["implementation_symbols_prev"] = prev_symbols
        prev_summary = prev.get("summary") if isinstance(prev, dict) else None
        if isinstance(prev_summary, str) and len(prev_summary) > 200:
            prev_summary = prev_summary[:200]
        code = self._code_text("dev")
        # 当前项目已有的代码正文。新建项目的检索池是空的，开发若不看这个，每一轮都是在
        # **重新发明**上一轮的文件 —— 这是「返工不收敛」的直接原因（见 _current_code_text）。
        current = self._current_code_text()
        scope = self.state.get("scope")
        assessment = self.state.get("assessment")
        plan = self.state.get("plan")
        # 方案审计：开发必须知道方案自身的问题（任务 id 不规范 / 改动文件没被任何任务覆盖 /
        # 触碰了禁改路径），否则会照着有缺陷的方案施工，问题被放大到实现层。
        plan_audit = self._audit_plan()
        self.state["plan_audit"] = plan_audit
        plan_pin = [prompts.plan_audit_block(plan_audit)] if plan_audit.get("change_count") else None
        # 上游编造的路径必须让开发看到：否则它会照着不存在的文件施工。
        # 此前只 pin 给了方案阶段 —— 但**真正写文件的是开发**，它更需要知道
        # 「这些路径是编的、别当依据」（真机案例：assess 编出 pipeline/db/*，
        # 方案把它当既有事实承接，最后写进验收标准）。
        warn_block = prompts.grounding_warning_block(self.grounding_warnings)
        if warn_block:
            plan_pin = [*(plan_pin or []), warn_block]
        # 运行验证的失败证据（真实执行产物）：此前只喂评审，开发死活看不到，
        # 于是只能按评审的文字意见修 —— 而评审经常看不出根因（真机 run 20260924-185507：
        # verify 明说 `No module named 'direction'`，评审只提了「缺 main 入口」，白烧一轮）。
        verify = self.state.get("verify_report")
        # 两遍模式依赖「存在可锚定的既有主函数」。原先用检索池是否为空来判定，于是
        # **新建项目永远被降级为单遍**（池是空的）—— 而它的真实代码其实就在 current 里。
        # 现在只要「有存量代码**或**有上一轮实现」就启用两遍。
        # 返工轮：把 verify/test/review 的机械证据规范化成一张缺陷单。
        # 我们的流水线其实早就产出了缺陷单需要的一切（失败命令、退出码、漏测符号、判负理由），
        # 只是此前一直以"一串裸文本"的形态回喂，dev 只能靠猜。
        bug_block = ""
        if self.round_kind == tasktype.BUGFIX:
            bug_report = tasktype.bug_report_from_state(
                self.state,
                list(self.fixes or []),
                plan=self.state.get("plan"),
                # 当前代码**按文件**给进去：缺陷单要摘出"待改处的逐字原文"，
                # 让修复方不必凭记忆改写位置（与 dev 看到的正文同源，见 _current_candidates）
                sources=self._current_sources(),
            )
            if bug_report.get("type") == tasktype.BUGFIX:
                # 上一轮**已存在**的文件：判断"能不能用 add 整份重吐"的基准
                bug_report["existing_paths"] = sorted(
                    {
                        str(e.get("path"))
                        for e in ((prev or {}).get("edits") or [])
                        if isinstance(e, dict) and e.get("path")
                    }
                )
                self.state["bug_report"] = bug_report
                bug_block = tasktype.format_bug_report(bug_report)
                self.log(
                    f"        [缺陷修复模式] 缺陷单已生成："
                    f"{len(bug_report.get('repro_steps') or [])} 条复现命令、"
                    f"范围 {len(bug_report.get('affected') or {})} 个文件"
                    + (f"（{'、'.join(sorted(bug_report['affected']))[:80]}）" if bug_report.get("affected") else "")
                )

        def _dev_parts(**kw: Any) -> list[str]:
            """dev 的输入片段 —— **把「已产出文件的接口」钉在最前面**。

            `only_paths` 是**按 task 分派**专用：只把这一张施工图相关的文件（接口摘要、
            退化点名、当前正文）喂进去，见 :meth:`_task_paths`。

            为什么放开头而不是末尾：这里是模型的注意力最强位置，而这份摘要是
            「另一份文件里到底有什么」的**唯一准绳** —— 跨文件接口靠记忆猜，猜错就是
            `AttributeError`（真机 run snake-v2：`ui.py` 读并不存在的 `game_logic.score`），
            跨轮还会把整个文件忘掉。要求：**每个文件生成前、每次重问前都强制投喂**。
            摘要是跨轮持久化的（见 `_execution_check` 把它写进 state），所以第 2 轮起
            的 dev 也拿得到上一轮定下来的接口。
            """
            # 按 task 分派时调用方会传裁剪后的 current_code / only_paths：这里**取出**
            # 后再组装，不能让它们再作为关键字传给 parts_dev（会撞成重复关键字参数）。
            only_paths = kw.pop("only_paths", None)
            cur = kw.pop("current_code", current)
            # **结构化判据**（不要用渲染后的 `code` 字符串判断）：池里有真片段、或当前实现里有
            # 真代码，才算"有可锚定的原文"。空池的占位说明曾让这个判据恒真（见 prompts.parts_dev
            # 的 has_code 说明与 retrieval.render_excerpts）。
            code_available = bool((cur or "").strip()) or any(
                str(getattr(e, "text", "") or "").strip() for e in (self.pool or [])
            )
            parts = prompts.parts_dev(
                requirement, scope, assessment, plan, code, fixes, prev_summary,
                verify=verify, current_code=cur, code_available=code_available,
                # 返工轮给**结构化缺陷单**而不是裸文本列表：dev 此前拿到的只有
                # 「有 2 条补丁未能套用」这种话，既不知道是哪个文件、也不知道验收口径，
                # 于是只能猜（真机 20260926-214757：四轮零进展，最后一律触顶）。
                bug_report_block=bug_block,
                # 返工轮同时切到**裁剪视图**：不再喂需求原文/PM 背景/完整方案/检索池，
                # 只留「修这个缺陷必需」的材料（见 prompts._bugfix_parts）
                bugfix=self.round_kind == tasktype.BUGFIX,
                # 三值一起给：`plan_rework` 既不是首轮也不是修缺陷，视图与尾部任务段都不同
                # （此前尾部无条件拼首轮的「按方案实现代码改动」，靠 rework note 去"盖住"它）。
                round_kind=self.round_kind, **kw,
            )
            # 三段都钉在最前面（注意力最强）：**冻结的接口基准** + 已产出接口 + 返工退化点名。
            # 顺序刻意如此：先「应当长什么样」（方案的基准，改它要回方案），后「现在长什么样」
            # （既成事实，用来接续前几轮）—— 混序会让模型分不清冲突该以谁为准。
            head = [
                b for b in (
                    prompts.skeleton_block(self.state.get("skeleton") or {}, only_paths),
                    prompts.api_digest_block(self.state.get("api_digest") or {}, only_paths),
                    prompts.dev_regression_block(lost_symbols, only_paths),
                ) if b
            ]
            return [*head, *parts]

        if lost_symbols:
            self.log(
                f"        [退化点名] {len(lost_symbols)} 个符号在上一轮实现里存在、这一版没了 → 已写进 dev 提示词"
            )

        # **按 task 分派**：一次 dev 只做一张施工图。
        # ① 单次输出只服务一张图，7B 写得更深；② 返工只重做缺陷单指向的那几张图，
        #    「最小改动」由调度保证而不是靠提示词约束；③ 单张图够小，两遍模式不再需要。
        per_task: list[dict] = []
        if DEV_PER_TASK:
            per_task = (
                self._tasks_for_bugfix()
                if self.round_kind == tasktype.BUGFIX
                else self._ordered_plan_tasks()
            )
            # 只有一张图时分派没意义（还多一次提示词开销）；返工轮即使只有一张也要走，
            # 因为"只做这一张"本身就是最小改动的保证。
            if len(per_task) < 2 and self.round_kind != tasktype.BUGFIX:
                per_task = []
        if per_task:
            self.log(f"        [按 task 分派] 共 {len(per_task)} 张施工图，逐张施工")
            merged = self._dev_by_tasks(_dev_parts, plan_pin, per_task)
            repair_kwargs: dict[str, Any] = {}
        elif DEV_TWO_PASS:
            # **条件里的 `(code or current)` 已去掉**：它原本是"有没有可锚定的素材"的判据，
            # 但 `code` 是**渲染后**的检索池 —— 池为空时 `retrieval.render_excerpts([])` 曾返回
            # 一句占位说明（非空），于是这个条件对新建项目**恒真**。那句占位说明现已移到呈现层
            # （`retrieval.render_excerpts` 空池返回空串），若继续留着这个条件，新建项目会被
            # **静默降级为单遍** —— 那是行为变更，而两遍模式本来就是为"没有可锚定材料"设计的
            # （见 `parts_dev` 的「第一遍·脚手架」分支）。这里显式化，保持既有可观测行为。
            # 注：`DEV_PER_TASK` 且方案有 ≥2 张施工图时走上面的分派分支，本分支用不到。
            # 第一遍：只铺辅助函数（脚手架），不动主函数体
            p1 = self._grounded_call(
                "dev",
                _dev_parts(dev_pass=2),
                note="dev 第一遍·脚手架",
                pin=plan_pin,
            )
            # 第二遍：带第一遍产物回填主函数体
            p2 = self._grounded_call(
                "dev",
                _dev_parts(dev_pass=3, pass1_edits=p1.get("edits")),
                note="dev 第二遍·回填",
                pin=plan_pin,
            )
            merged = self._merge_dev(p1, p2)
            repair_kwargs: dict[str, Any] = {"dev_pass": 3, "pass1_edits": p1.get("edits")}
        else:
            merged = self._grounded_call("dev", _dev_parts(), pin=plan_pin)
            repair_kwargs = {}
        # 补丁正文归一：模型常把 Python 的 `\r` 转义写成 JSON 里的单反斜杠，
        # JSON 解码后变成一个**真实回车**，落进源码字符串就是语法错误（单引号字符串跨行）。
        # 必须在进入流水线之前修 —— 模型从失败反馈里看不出这是编码层问题，会一直重写同一处。
        fixed = patches.normalize_implementation(merged)
        if fixed:
            self.log(
                f"        [归一] 补丁正文里有 {fixed} 处裸 CR（模型把 \\r 转义写成了真回车）"
                "→ 已转义为 \\r"
            )
        # anchor 补全：模型常把签名抄缩写（`def hello(self)` 而原文带参数），
        # 于是被判 anchor_not_found 白烧一轮。原文里符号唯一时，从 ast 取真实签名补上。
        anchor_fix = patches.repair_anchors(self.repo, merged)
        if anchor_fix["repaired"]:
            self.log(
                f"        [anchor 补全] {anchor_fix['repaired']} 处签名被缩写，已按原文补全："
                + "、".join(anchor_fix["detail"][:4])
            )
        # 内容自检 + 带问题重问：新增文件写残（`print(f'{` 这类）在 JSON 层是**合法**的，
        # 契约重试抓不到；而只把它记成阻断项的代价是整整一轮（dev+test+verify+review ≈5 分钟
        # + 一次 14B 评审），模型下一轮照样写残 —— 真机 run 20260924-185507 连栽 4 轮。
        # 所以先原地重问，把「写残」这类低级失误在几十秒内解决。
        _shown: frozenset[str] | None = None  # 上一次拿去重问的问题集，用于判「有没有进展」
        _prev_count: int | None = None  # 上一版的问题**条数**，用于判「有没有净进展」
        # 仓库里已有的兄弟模块（新建项目为空；同批新建文件在函数内部自动识别）
        try:
            _repo_py = list(Path(self.repo).glob("*.py")) if self.repo else []
        except OSError:
            _repo_py = []
        _local_modules = {p.stem for p in _repo_py}
        # 存量文件的顶层符号归属：机械补全要能补符号级 `from cli import add`，
        # 而不只是模块级 `import sys`（真机 run 20260928-200631：add/remove 漏 import 五轮）。
        _local_symbols: dict[str, str] = {}
        for _p in _repo_py:
            try:
                for _sym in patches.top_level_defs(_p.read_text(encoding="utf-8", errors="replace")):
                    _local_symbols.setdefault(_sym, _p.stem)
            except OSError:
                continue
        _reasked = False  # 本轮有没有真的发起过带问题重问（决定收尾要不要刷新语义审计）
        for attempt in range(1, DEV_CONTENT_REPAIR_TRIES + 1):
            problems = self._dev_selfcheck(merged)
            if not problems:
                break
            # 先试机械补 import：pyright 已经精确指出「哪个文件用了哪个模块却没 import」，
            # 补一行是确定性操作，没有需要模型判断的地方。真机 run snake-ds-plan 里
            # `main.py` 的 `random` 与 `ui.py` 的 `sys` 连着重问 3 次都没被补上
            # （约 184 秒白烧）—— 能机械修掉就不该消耗重问预算。
            imp_fix = patches.repair_missing_imports(
                merged, self.state.get("semantic_audit"), _local_modules, _local_symbols
            )
            if imp_fix["repaired"]:
                self.log(
                    f"        [import 补全] {imp_fix['repaired']} 处用了却没 import，已机械补上："
                    + "、".join(imp_fix["detail"][:4])
                )
                problems = self._dev_selfcheck(merged)
                if not problems:
                    break
            # 无进展就停：真机连续两次出现「重问 3 次、每次问题一字未变」——
            #   · run snake-ds-plan：main.py 缺 `import random` / ui.py 缺 `import sys`，约 184s
            #   · run snake-impfix：产物自带测试没跑通，约 285s
            # 问题集完全相同说明模型这一轮改不动它，再问只是把预算重复烧一遍；
            # 剩下的交给评审与人工（下面已有「重问 N 次后仍不合法」的落点，证据照样留得下）。
            _now = frozenset(str(p) for p in problems)
            # 无进展就停：判据是**问题条数没有下降**，而不必等到"完全相同"。
            # 真机 `20260928-110402`：4 处 → 重问 → 6 处 → 重问 → 12 处（`补丁校验 12 个问题、
            # 物理套不上 8 个`）→ 第三轮才因"完全相同"停下。**越修越多**说明这一版模型在
            # 自检这条路上改不动它，多问一轮只是把预算重复烧一遍（该运行在阶段级重问上花了
            # 168 秒，其中两次是净损失）。剩下的交给评审与人工，证据照样留得下。
            # 注意比较基准是"上一次**拿去重问时**的条数"：机械补 import 之后条数若真的下降，
            # 这里会放行（那是真进展）。
            if _prev_count is not None and (len(problems) >= _prev_count or _now == _shown):
                why = "与上一版完全相同" if _now == _shown else f"条数没有下降（{_prev_count} → {len(problems)}）"
                self.log(
                    f"        [自检] 重问无进展（问题{why}）→ 停止重问，交给评审与人工"
                )
                break
            _shown = _now
            _prev_count = len(problems)
            self.log(
                f"        [自检] 发现问题 {len(problems)} 处 → 带问题重问 dev"
                f"（第 {attempt}/{DEV_CONTENT_REPAIR_TRIES} 次）"
            )
            # 摘要在 _execution_check 里已按**刚产出的那一版**更新进 state，
            # `_dev_parts` 会把它钉在最前面 —— 重问传的 `current_code` 是**本轮开始之前**
            # 的正文，与「刚写出来的那一版 + 针对它的新问题」对不上号，摘要补的就是这个缺口。
            again = self._grounded_call(
                "dev",
                _dev_parts(repair=problems, **repair_kwargs),
                note=f"dev 重出·内容不合法（第 {attempt} 次）",
                pin=plan_pin,
            )
            _before = copy.deepcopy(merged)
            merged = self._apply_repair(merged, again)
            patches.normalize_implementation(merged)
            # 重出常以 full_symbol 整份重发同一文件，_apply_repair 会用新版**整份替换**旧
            # edit —— 模型的新版照样漏 import，于是循环开头刚机械补进去的 import 被一起
            # 冲掉（真机 run 20260928-180933：补好的 import db/sys 被整份重吐覆盖，问题
            # 条数反弹，随即触发「条数没有下降」误停，带病文件流出 dev）。用最新一轮语义
            # 诊断在替换后的 merged 上**再补一次**，下一轮自检/无进展判据才不会被反弹污染。
            _re_imp = patches.repair_missing_imports(
                merged, self.state.get("semantic_audit"), _local_modules, _local_symbols
            )
            if _re_imp["repaired"]:
                self.log(
                    f"        [import 补全] 重出整份覆盖了 {_re_imp['repaired']} 处 import，"
                    "已重新机械补上：" + "、".join(_re_imp["detail"][:4])
                )
            _reasked = True
            # 「锁基准、定范围、最小改」的机械举证：这一版动到了问题清单没点到的文件吗？
            # 只记告警 —— 先量出真实比例，再决定是否升级成打回（见 _out_of_scope_edits）。
            _oos = self._out_of_scope_edits(_before, merged, problems)
            if _oos:
                self.log(
                    f"        [超范围] 改动碰到了问题清单没点到的 {len(_oos)} 处："
                    + "、".join(_oos[:3])
                )
                self.state.setdefault("out_of_scope_edits", [])
                self.state["out_of_scope_edits"].extend(_oos)
        # 重问产出的那版 anchor 可能又是缩写的，收尾再补一次
        tail_fix = patches.repair_anchors(self.repo, merged)
        if tail_fix["repaired"]:
            self.log(
                f"        [anchor 补全] 重问后又有 {tail_fix['repaired']} 处被补全："
                + "、".join(tail_fix["detail"][:4])
            )
        patches.normalize_implementation(merged)
        left = self._invalid_new_files(merged)
        if left:
            self.log(f"        [自检] 重问 {DEV_CONTENT_REPAIR_TRIES} 次后仍不合法 {len(left)} 处（交给评审与人工）")
        # 语义审计收尾：
        # ① 一直卡在字面检查时（`_invalid_new_files` 有问题就不会跑到语义检查），评审会少了
        #    这条机械证据 —— 审计缺失就补跑一次；
        # ② 只要本轮发起过带问题重问，最后一次 `_apply_repair` 之后 merged 已被替换/补 import，
        #    state 里的审计可能描述的是替换**之前**那版 —— 按最终 merged 重刷，评审不能拿着
        #    陈旧诊断判负（幂等，代价是几秒物化 + pyright）。
        if _reasked or not self.state.get("semantic_audit"):
            self._semantic_problems(merged)
        # 落盘前的红线拦截（机械举证）：补丁即将被物化/交付，先按规则库（pipeline/rules.json）
        # 扫一遍**将要写入的新代码**。放在这里而不是 verify 之后：像 `import keyboard`（自己
        # 发明的依赖）这类问题，真机上要等 verify 跑完一整轮才暴露，而密钥/调试残留/浮动依赖/
        # 破坏性迁移都是毫秒级正则能判的 —— 早一轮发现就省下 test + verify + 一次 14B 评审。
        # `reset=True`：这一轮的红线**只反映这一轮的代码**，绝不带上上一轮的陈旧条目。
        self._rule_findings(merged, reset=True)
        merged_impl = self._merge_impl_across_rounds(prev, merged)
        # ---- 累积兑现率埋点 ----
        # 「上一轮的实现」必须**一条不丢地**活到这一轮（`_merge_impl_across_rounds` 的并集语义）。
        # 真机 20260927-134222：返工轮之后 `state["implementation"]` 只剩 4 条小补丁，
        # 上一轮 add 出来的 collision.py / food.py **整份不见** —— 于是返工轮对它们的 modify
        # 判「目标文件不存在」，沙箱里只剩一个 main.py。这类"越改越少"必须能被**看见**：
        # 只记数字（不判负），下一次就能一眼看出是 prev 空、还是本轮输出把 prev 顶掉了。
        prev_n = len((prev or {}).get("edits") or [])
        cur_n = len((merged or {}).get("edits") or [])
        out_n = len(merged_impl.get("edits") or [])
        prev_keys = {
            (str(e.get("path") or ""), str(e.get("change_type") or ""))
            for e in (prev or {}).get("edits") or []
            if isinstance(e, dict)
        }
        out_keys = {
            (str(e.get("path") or ""), str(e.get("change_type") or ""))
            for e in merged_impl.get("edits") or []
            if isinstance(e, dict)
        }
        lost = sorted(f"{p}({ct})" for p, ct in prev_keys - out_keys)
        self.state["impl_accumulation"] = {
            "prev_edits": prev_n, "round_edits": cur_n, "merged_edits": out_n,
            "lost_from_prev": lost[:20], "lost_count": len(lost),
        }
        self.log(
            f"        [实现累积] 上一轮 {prev_n} 条 + 本轮 {cur_n} 条 → 合并后 {out_n} 条"
            + (f"；⚠ 上一轮有 {len(lost)} 条未进合并结果：{'、'.join(lost[:4])}" if lost else "")
        )
        self.state["implementation"] = merged_impl
        # 把累积实现落成 dev 阶段唯一的产物快照（见 _save_impl_snapshot 的原由：
        # 不落这一次，续跑时 `_restore` 会拿"某次调用的产物"当实现，越改越少）。
        self._save_impl_snapshot(merged_impl)
        return self.state["implementation"]

    @staticmethod
    def _merge_impl_across_rounds(prev: dict | None, cur: dict) -> dict:
        """跨轮累积实现：保留上一轮的全部补丁，用本轮同 (path, 符号, 模式) 的补丁替换之；

        新建文件的 ``add`` 块按**顶层符号做并集**累积 —— 本轮没覆盖到的符号（被模型漏掉的
        类）保留，被覆盖的才替换。这是修复「越改越少」的关键：run 20260925-184300 里
        ``game_logic.py`` 每轮作为整文件 ``add`` 重新吐出，但每次漏掉 ``Snake``/``Food``/
        ``Score`` 之一，``vanished_symbols`` 抓到后机械强制 rework，模型下一轮修好这个又丢掉
        那个，在 ``max_rework`` 前永不收敛。若把 ``add`` 当「权威重写」直接清掉旧块，恰恰是
        把上一轮正确的类丢了；按符号并集累积才能保住它们。

        累积后 ``state["implementation"]`` 始终是完整当前实现：materialize 物化的沙箱也完整，
        verify 不再报符号凭空消失、返工收敛；交付（apply_all 重放）也拿到整棵文件树。
        """
        if not isinstance(prev, dict) or not (prev.get("edits") or prev.get("summary")):
            return cur or {}
        cur = cur or {}

        def _lst(a: dict | None, k: str) -> list:
            v = (a or {}).get(k)
            return v if isinstance(v, list) else []

        def _norm(p: Any) -> str:
            return str(p or "").replace("\\", "/")

        def _top_symbols(patch: Any) -> set[str]:
            """一个 add 块（整文件补丁）里定义的顶层 class/def 符号集合。"""
            out: set[str] = set()
            for ln in str(patch or "").split("\n"):
                m = re.match(r"^(?:async\s+)?def\s+(\w+)|^class\s+(\w+)", ln)
                if m:
                    out.add(m.group(1) or m.group(2))
            return out

        def _is(e: dict, ct: str) -> bool:
            return isinstance(e, dict) and str(e.get("change_type") or "") == ct

        def mod_key(e: dict) -> tuple[str, str, str, tuple[str, ...]]:
            # 末位是「这条补丁服务于哪些任务」：
            #   · **同一任务**重做（返工）⇒ key 相同 ⇒ 后者覆盖前者（修正是替换，不是追加）；
            #   · **不同任务**改同一个文件的同一个符号 ⇒ key 不同 ⇒ 两条并存、依次套用。
            # 没有这一维时，按 task 分派会让后一个任务静默吃掉前一个任务的改动
            # （改同一文件是常态，不是异常），而且**丢得无声无息**。
            return (
                _norm(e.get("path")),
                str(e.get("target_symbol") or ""),
                str(e.get("patch_mode") or ""),
                tuple(sorted(str(t) for t in (e.get("covers_tasks") or []))),
            )

        # 本轮删除的路径：文件应被移除，旧 edit 作废
        deleted_paths = {
            _norm(e.get("path"))
            for e in _lst(cur, "edits")
            if _is(e, "delete") and e.get("path")
        }
        # 本轮 add 块在各路径上覆盖到的顶层符号
        cur_add_syms: dict[str, set[str]] = {}
        for e in _lst(cur, "edits"):
            if _is(e, "add") and e.get("path"):
                cur_add_syms.setdefault(_norm(e.get("path")), set()).update(_top_symbols(e.get("patch")))

        # ⚠ 顺序即语义：物化按 edits 的**列表顺序**依次套用，`modify` 要求目标文件已存在，
        # 而新建项目的文件正是靠同一份 edits 里的 `add` 创建出来的。曾把结果按类型拼成
        # 「modify… + add… + delete…」，于是 modify 全部跑到 add 之前 ⇒ 目标文件还不存在
        # ⇒ 真机 20260927-073518 attempt 2 报「有 4 条补丁未能套用（目标文件不存在，
        # modify 要求文件已在仓库里；新建文件要用 add）」。所以合并必须**保持原顺序**：
        # 先按 prev 的原序铺开，再用 cur 同键替换 / 新键追加。
        def _key(e: dict, ordinals: dict[str, int]) -> tuple:
            p = _norm(e.get("path"))
            if _is(e, "add"):
                # 同一路径的第 n 个 add 块（多块文件按顺序编号，逐块覆盖而非互相顶掉）
                i = ordinals.get(p, 0)
                ordinals[p] = i + 1
                return (p, "__add__", i)
            return mod_key(e)

        def _survivor_key(kept: dict, path: str) -> tuple:
            """给「保留下来的旧 add 块」一个**不会被本轮 add 顶掉**的键。

            真机教训（2026-09-27，`smoke_merge` ③ 长期挂红）：旧的 add 块用**它自己
            列表里的序号**当键（``(path, "__add__", 0)``），而本轮的 add 也是从 0 开始
            编号 ⇒ 两者同键 ⇒ 本轮的块把它**整体顶掉**。于是本轮漏写的类（`Food`）
            连同上一轮**正确的实现**一起消失 —— 正是 ``vanished_symbols`` 要防的
            「越改越少」，却在合并层就把防线拆了。
            """
            n = 0
            while (path, "__add_kept__", n) in kept:
                n += 1
            return (path, "__add_kept__", n)

        def _top_span(lines: list[str], symbol: str) -> tuple[int, int] | None:
            """**零缩进**定义的区间（找不到返回 None）。

            只认顶层：方法名与顶层符号同名时，按「任意缩进」匹配会删错块 ——
            静默丢代码比不删危险得多，所以宁可返回 None 让它原样保留。
            """
            pat = re.compile(rf"^(?:async\s+)?(?:def|class)\s+{re.escape(symbol)}\b")
            for idx, line in enumerate(lines):
                if not pat.match(line):
                    continue
                block = retrieval.symbol_span(lines, idx)
                if not block:
                    return None
                start, end = block[0], block[1]
                while end > start and not lines[end].strip():
                    end -= 1
                return (start, end)
            return None

        def _drop_covered(patch: Any, covered: set[str]) -> str:
            """从整文件 add 块里删掉这些顶层符号的定义，其余（import / 常量 / 别的类）原样保留。

            为什么是「删掉被覆盖的」而不是「只留没被覆盖的」：整文件补丁的头部通常是
            import 与常量，只挑符号会把这些一起丢掉，被保留下来的类就会缺依赖。
            """
            lines = str(patch or "").split("\n")
            spans = sorted({s for s in (_top_span(lines, sym) for sym in covered) if s}, reverse=True)
            for start, end in spans:
                del lines[start : end + 1]
            text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")
            return text + "\n" if text.strip() else ""

        kept: dict[Any, dict] = {}

        # 1) prev 的全部 edit（原顺序），本轮已作废的跳过
        prev_ord: dict[str, int] = {}
        for e in _lst(prev, "edits"):
            if not (isinstance(e, dict) and e.get("path")):
                continue
            p = _norm(e.get("path"))
            if p in deleted_paths:
                continue
            if _is(e, "add") and p in cur_add_syms:
                syms = _top_symbols(e.get("patch"))
                covered = syms & cur_add_syms[p]
                if not covered:
                    # 本轮的 add 没碰这个块里的任何符号 ⇒ 整块保留（独立键，别被顶掉）
                    kept[_survivor_key(kept, p)] = e
                    continue
                rest = syms - covered
                if not rest:
                    continue  # 本轮已完整覆盖该块定义的全部符号 -> 丢弃旧的，避免重复定义
                # 部分覆盖：删掉被覆盖的符号，**保住本轮漏写的那些**（防越改越少）
                trimmed = _drop_covered(e.get("patch"), covered)
                if trimmed.strip():
                    kept[_survivor_key(kept, p)] = {**e, "patch": trimmed}
                continue
            kept[_key(e, prev_ord)] = e

        # 2) cur 的 edit：同键替换（保留 prev 的位置），新键追加到末尾
        cur_ord: dict[str, int] = {}
        for e in _lst(cur, "edits"):
            if isinstance(e, dict) and e.get("path"):
                kept[_key(e, cur_ord)] = e

        out: dict[str, Any] = dict(cur)  # 以本轮为准继承 summary/deviations/self_checks/...
        out["edits"] = list(kept.values())

        # not_implemented：本轮实现的任务，上一轮「未实现」声明作废
        done_ids = {
            str(t)
            for e in _lst(cur, "edits")
            if isinstance(e, dict) and str(e.get("patch") or "").strip()
            for t in (e.get("covers_tasks") or [])
        }
        prev_open = [
            x for x in _lst(prev, "not_implemented")
            if not (isinstance(x, dict) and str(x.get("task") or "") in done_ids)
        ]
        out["not_implemented"] = prev_open + _lst(cur, "not_implemented")
        out["deviations"] = _lst(prev, "deviations") + _lst(cur, "deviations")
        if not out.get("self_checks"):
            out["self_checks"] = _lst(prev, "self_checks")
        if not out.get("uncertainties"):
            out["uncertainties"] = _lst(prev, "uncertainties")
        if not out.get("summary"):
            out["summary"] = prev.get("summary") or ""
        return out

    def _current_candidates(self, only_paths: set[str] | None = None) -> list[tuple[str, str]]:
        """当前已有代码的**逐文件正文**（`[(相对路径, 正文)]`，按"上一轮改过的优先"排序）。

        这是「当前代码从哪来」的**唯一一份实现**：`_current_code_text`（喂给 dev 的正文块）
        与 `_current_sources`（缺陷单里给"待改处的逐字原文"）都走它。
        分成两份实现迟早会漂移 —— 而**同源**在这里是有硬要求的：缺陷单给的原文
        必须与 dev 看到的【当前项目已有代码】是同一份，否则它照着缺陷单抄也对不上。
        """
        edits = [
            e
            for e in ((self.state.get("implementation") or {}).get("edits") or [])
            if isinstance(e, dict)
        ]
        touched = {str(e.get("path") or "").replace("\\", "/") for e in edits}
        candidates: list[tuple[str, str]] = []
        seen_rel: set[str] = set()

        def _scan_dir(folder: Path) -> None:
            # **先扫的优先**：dev 在制工作区（本轮前序施工图的真实产物，G4）比
            # verify/work（上一轮的沙箱）更贴近"当前项目已有代码"；同路径只取第一份。
            if not folder.is_dir():
                return
            for path in sorted(p for p in folder.rglob("*") if p.is_file()):
                if "__pycache__" in path.parts or path.suffix.lower() not in _CURRENT_CODE_SUFFIXES:
                    continue
                rel = str(path.relative_to(folder)).replace("\\", "/")
                if rel in seen_rel:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if text.strip():
                    candidates.append((rel, text))
                    seen_rel.add(rel)

        wip = getattr(self, "_wip_dir", None)
        if wip is not None:
            _scan_dir(wip)
        if self.run_dir is not None:
            _scan_dir(self.run_dir / "verify" / "work")
        if not candidates:
            for edit in edits:
                body = str(edit.get("patch") or "")
                rel = str(edit.get("path") or "").strip()
                if body.strip() and rel:
                    candidates.append((rel, body))
        if only_paths:
            # 按 task 分派：只留这一张施工图相关的文件正文。7B 的上下文要留给
            # 「本任务怎么写」，而不是让它在无关文件里找线索（也避免顺手改到别人负责的文件）。
            wanted = {str(p).replace("\\", "/") for p in only_paths}
            candidates = [kv for kv in candidates if kv[0] in wanted]
        candidates.sort(key=lambda kv: (kv[0] not in touched, kv[0]))
        return candidates

    def _current_sources(self, only_paths: set[str] | None = None) -> dict[str, str]:
        """`{相对路径: 正文}`（**不裁剪**）。

        给缺陷单用：它要从中摘出"待改处的逐字原文"。裁剪会截断正文、让后面的符号取不到，
        而这里只需要几行，不占提示词预算（真正进提示词的只有被摘出来的那几行）。
        """
        return dict(self._current_candidates(only_paths))

    def _current_code_text(self, only_paths: set[str] | None = None) -> str:
        """当前项目里**已有的代码正文** —— 返工时开发必须看到的东西。

        为什么必须有：`_code_text()` 切的是**检索池**，而新建项目的池是空的 —— 于是开发
        写完 2.2KB 代码后，下一轮一个字都看不到，只能照着方案**重新发明**这些文件。
        真机 run 20260924-235001 因此连栽 8 轮：补错 import（`from snake import Food`）、
        丢掉入口点、把已经跑通的实现改坏，返工项也一直修不掉。

        来源优先「物化后的真实文件」（verify 沙箱正是上一轮的真实产物，最接近仓库现状），
        取不到时退回实现产物里的补丁正文。按 dev 的代码预算裁剪，且**上一轮改动过的文件
        排在最前** —— 预算不足时至少保证那些可见。
        """
        candidates = self._current_candidates(only_paths)

        budget = CODE_BUDGET.get("dev", 0) or 8000
        used = 0
        blocks: list[str] = []
        for rel, text in candidates[:_CURRENT_CODE_MAX_FILES]:
            body = text
            if len(body) > _CURRENT_CODE_PER_FILE_CHARS:
                body = body[:_CURRENT_CODE_PER_FILE_CHARS] + "\n# …（本文件较长，已截断）"
            cost = estimate_tokens(body)
            if used + cost > budget:
                continue
            used += cost
            blocks.append(f"=== {rel} ===\n{body}")
        return "\n\n".join(blocks)

    def _own_module_names(self, impl: Any = None) -> set[str]:
        """本项目自己的模块名（**待检实现**里的文件 + 已累积实现 + 仓库顶层条目）。

        这些 import 不该被判成缺依赖。

        **`impl` 必须显式传进来**：以前只读 ``self.state["implementation"]``，而它是在
        ``_stage_dev`` **末尾**才更新的。于是**第 1 轮**（新建项目，state 还是空的）
        会把这一轮刚产出的文件之间的互相 import 全判成「缺依赖」——
        实测拿真机 run snake-v3 的产物复现：5 个文件报了 **6 处**这种假阳性，
        把 dev 的重问预算整个烧在一个不存在的问题上（`[自检] 重问 2 次后仍不合法 5 处`
        就是这么来的），同时也把更贵的语义/执行级检查全挡在门外。
        """
        names: set[str] = set()
        for source in (impl, self.state.get("implementation")):
            if not isinstance(source, dict):
                continue
            for edit in source.get("edits") or []:
                if isinstance(edit, dict):
                    names.add(Path(str(edit.get("path") or "")).stem.lower())
        if self.repo and self.repo.is_dir():
            try:
                for entry in self.repo.iterdir():
                    if entry.name.startswith("."):
                        continue
                    names.add((entry.stem if entry.is_file() else entry.name).lower())
            except OSError:
                pass
        names.discard("")
        return names

    def _semantic_problems(self, impl: Any) -> list[str]:
        """把当前实现**临时物化**后跑一次语义诊断，返回高置信的类型级问题。

        **为什么要在 dev 阶段就物化一次**（verify 之后还会再物化）：
        ast 层只能查字面 —— 语法、跨模块未定义名、import 契约。而真机上最难发现的是
        「属性不存在 / 参数个数不匹配 / 跨函数返回值类型错」，只能靠类型推断发现，
        且那些代码常在被执行路径之外（``run_command`` 也照不到）。
        放在这里而不是等 verify：重问只要几十秒，走完 test+verify+review 是一整轮
        （≈5 分钟 + 一次 14B 评审）。

        **探测不到 pyright 时静默返回空** —— 它是可选增强，不是依赖。
        """
        if not LSP_ENABLED or self.run_dir is None:
            return []
        if not semantics.available():
            # 只提示一次，避免每轮刷屏
            if not self.state.get("_lsp_warned"):
                self.state["_lsp_warned"] = True
                self.log(f"        [语义检查] {semantics.unavailable_reason()}")
            return []
        try:
            audit = patches.analyze_all(self.repo, impl)
        except Exception as exc:  # noqa: BLE001
            self.log(f"        [语义检查] 补丁核对失败，跳过：{type(exc).__name__}: {exc}")
            return []
        probe = self.run_dir / "semantic-probe"
        # 每次重问都先清空：残留的上一版文件会让 pyright 报到已经不存在的符号上
        shutil.rmtree(probe, ignore_errors=True)
        try:
            report = patches.apply_all(self.repo, impl, audit, in_place=False, out_dir=probe)
        except Exception as exc:  # noqa: BLE001
            self.log(f"        [语义检查] 物化失败，跳过：{type(exc).__name__}: {exc}")
            return []
        written = sorted(
            {str(item.get("path") or "") for item in report.get("files") or []} - {""}
        )
        if not written:
            return []
        result = semantics.diagnose(probe, written)
        if result.get("reason"):
            self.log(f"        [语义检查] {result['reason']}")
            return []
        # 存进 state：评审需要看到这条机械证据。dev 的重问只是「尽量修」，
        # 修不掉的部分必须让评审和人工知道，而不是消失在日志里。
        self.state["semantic_audit"] = result
        self.log(f"        [语义检查] {semantics.summary_line(result)}")
        # 只回灌高置信项：推断性结论误报率高，拿去重问会白烧一轮
        return semantics.problem_lines(result)

    #: dev 自检里「跑产物自带测试」的超时（秒）。测试是秒级的，90s 足够；
    #: 真挂住也不该把 dev 阶段拖死 —— 这一步只是「尽量修」，判定仍以 verify 为准。
    _DEV_EXEC_TIMEOUT = 90

    #: 测试产物自检的重问次数（命令自身不可执行 → 带问题重问 test）。与 dev 的重问对称：
    #: 取 2 —— 一次足以把「按类名猜构造」这类系统性错误改过来，多给一次兜底。
    _TEST_REPAIR_TRIES = 2

    def _materialize_probe(self, impl: Any) -> tuple[Path, list[str]] | None:
        """把当前实现物化到 ``runs/<id>/dev-exec-probe``，返回 ``(probe, written)``。

        物化失败 / 无产出返回 None（静默降级：这是增强手段，不是判定依据）。
        dev 自检（跑自带测试）与测试阶段的接口摘要共用它 —— 物化一次、两处受益。
        """
        if self.run_dir is None or not self.repo:
            return None
        try:
            audit = patches.analyze_all(self.repo, impl)
            probe = self.run_dir / "dev-exec-probe"
            shutil.rmtree(probe, ignore_errors=True)
            report = patches.apply_all(self.repo, impl, audit, in_place=False, out_dir=probe)
        except Exception as exc:  # noqa: BLE001
            self.log(f"        [自检·物化] 失败，跳过：{type(exc).__name__}: {exc}")
            return None
        written = sorted(
            {str(item.get("path") or "") for item in report.get("files") or []} - {""}
        )
        return (probe, written) if written else None

    # ===================== 文件创建租约 + 任务事务点（G3/G4） =====================
    # 真机 run 20260929-093329 的两处机械失效：
    #   ① T-01~T-04 对同一个新文件 main.py 都交 change_type=add 整份正文（target_symbol
    #      全是模块名占位 "main"），物化时 new_file 块合并互相覆盖；它们本该用的 3 条
    #      modify 又因 anchor 对不上被 prune —— 最终文件只剩一个任务的内容；
    #   ② 后做的任务看不到前面任务刚写出来的文件（current_code 只读仓库原件 / 上一轮
    #      verify 沙箱），于是只能再整份重写一遍。
    # 机制：编译期给每个新文件定唯一 create owner（taskcompiler._annotate_create_owners）；
    # dev 逐图施工时在 ``dev-wip/`` 在制工作区累积物化，非 owner 的整份 add 机械丢弃，
    # 每图过一次 py_compile，失败带错误只重问一次。mock 路径不启用（测流程不测内容）。

    def _existing_repo_files(self) -> set[str] | None:
        """编译时刻仓库**已存在**的文件相对路径集合；没有仓库（全新项目）返回 None。

        None 是有意区分的三值：编译器据此把所有文件当新建（首图领创建租约）；
        空集合表示仓库存在但为空（同为全新项目语义）。
        """
        if not self.repo:
            return None
        repo = Path(self.repo)
        if not repo.is_dir():
            return None
        out: set[str] = set()
        for p in repo.rglob("*"):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            if any(part in VERIFY_SKIP_DIRS for part in p.relative_to(repo).parts):
                continue
            try:
                out.add(str(p.relative_to(repo)).replace("\\", "/"))
            except ValueError:
                continue
        return out

    def _wip_init(self) -> None:
        """初始化本轮的**在制工作区**基线 ``runs/<id>/dev-wip-base`` + 工作区 ``dev-wip``。

        每次物化都从基线**整份重放**（不是把补丁往旧结果上叠）：否则 owner 那条
        change_type=add 整份新建在文件已存在后会被判 anchor_not_found，后续每个任务
        都收到一条假的"补丁未能套用"。语义与 verify.materialize 完全同源：
        audit 基准=base（仓库 + 上一轮产物种子），out_dir=wip（base 的副本），
        新文件走 new_file 块合并、modify 打在合并结果上。

        第二轮起：上一轮 verify 沙箱（verify/work）里、属于上一版实现产物的文件
        （implementation.edits 的路径，**不含** test 阶段的测试文件）补种子进基线 ——
        否则返工轮的 modify 补丁在空基准上必然 anchor 落空。
        """
        self._wip_dir: Path | None = None
        self._wip_base: Path | None = None
        self._wip_head_revision = ""  # 本轮在制工作区链头（方案§二十三/§二十四）
        if self.run_dir is None:
            return
        base = self.run_dir / "dev-wip-base"
        wip = self.run_dir / "dev-wip"
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(wip, ignore_errors=True)
        base.mkdir(parents=True, exist_ok=True)
        if self.repo and Path(self.repo).is_dir():
            try:
                verify_mod._copy_tree(
                    Path(self.repo),
                    base,
                    VERIFY_COPY_LIMIT_MB * 1024 * 1024,
                    VERIFY_SKIP_DIRS,
                )
            except Exception as exc:  # noqa: BLE001
                self.log(f"        [在制工作区] 仓库复制失败，按空目录起步：{type(exc).__name__}: {exc}")
        prev_work = self.run_dir / "verify" / "work"
        prev_impl = self.state.get("implementation")
        if prev_work.is_dir() and isinstance(prev_impl, dict):
            for e in (prev_impl.get("edits") or []):
                if not isinstance(e, dict):
                    continue
                rel = str(e.get("path") or "").replace("\\", "/").strip()
                if not rel:
                    continue
                src, dst = prev_work / rel, base / rel
                try:
                    if src.is_file() and not dst.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                except OSError:
                    continue
        self._wip_base = base
        self._wip_dir = wip

    def _wip_materialize(self, impl: Any) -> dict:
        """从基线**整份重放**累积实现到在制工作区。

        返回 ``{"written": [相对路径], "problems": [人话问题]}``：problems 含
        ① 新文件内容本身语法错误（analyze 判 ``new_file_syntax_error``，根本没写盘）
        ② 非幂等的补丁套用跳过（anchor 落空等）——这些与 py_compile 一样是事务点要
        带回去重问的机械事实（口径对齐 verify.materialize）。失败静默降级。
        """
        empty = {"written": [], "problems": []}
        wip = getattr(self, "_wip_dir", None)
        base = getattr(self, "_wip_base", None)
        if wip is None or base is None or not isinstance(impl, dict):
            return empty
        if not isinstance(impl.get("edits"), list):
            return empty
        # 重放：工作区还原成基线副本，避免"上一次物化的结果"污染本轮审计口径。
        shutil.rmtree(wip, ignore_errors=True)
        try:
            shutil.copytree(base, wip)
            audit = patches.analyze_all(base, impl)
            report = patches.apply_all(base, impl, audit, in_place=False, out_dir=wip)
        except Exception as exc:  # noqa: BLE001
            self.log(f"        [在制工作区] 物化失败，跳过：{type(exc).__name__}: {exc}")
            return empty
        written = sorted({str(item.get("path") or "") for item in report.get("files") or []} - {""})
        problems: list[str] = []
        syntax_error_paths: set[str] = set()
        for row in (audit.get("edits") or []):
            if isinstance(row, dict) and str(row.get("status") or "") == "new_file_syntax_error":
                syntax_error_paths.add(str(row.get("path") or ""))
                problems.append(
                    f"新文件 {row.get('path')} 内容有语法错误，未能写入在制工作区"
                    f"（{str(row.get('detail') or '')[:160]}）"
                )
        for skip in (report.get("skipped") or []):
            if not isinstance(skip, dict) or patches.is_benign_skip(skip):
                continue
                if str(skip.get("path") or "") in syntax_error_paths:
                    continue
                problems.append(
                    f"补丁未能套用到 {skip.get('path') or '?'}：{str(skip.get('reason') or '')[:200]}"
                )
        return {"written": written, "problems": problems}

    @staticmethod
    def _file_owner_map(tasks: list[dict]) -> dict[str, str]:
        """``{文件相对路径: 创建租约 owner 任务 id}``（取编译器标注的 creates_file）。"""
        owners: dict[str, str] = {}
        for t in tasks or []:
            if not isinstance(t, dict) or not t.get("creates_file"):
                continue
            tid = str(t.get("id") or "").strip()
            for p in (t.get("target_files") or []):
                path = str(p or "").replace("\\", "/").strip()
                if path and tid and path not in owners:
                    owners[path] = tid
        return owners

    def _enforce_file_lease(self, task: dict, data: Any, owner_map: dict[str, str]) -> list[str]:
        """**文件创建租约执法**（G3）：丢弃非 owner 任务对租约文件的整份重写补丁。

        判据刻意收窄，宁可漏判不可误杀合法补丁：
          · change_type 必须是 add；
          · target_symbol 为空或等于文件模块名（真机四份 main.py 全是 ``sym="main"``
            这种"整文件占位"写法）——非 owner 给**自己负责的符号**交 add/full_symbol
            （如 sym=``list``）是合法的定点新增，必须放行。
        """
        if isinstance(self.client, MockClient) or not isinstance(data, dict):
            return []
        edits = data.get("edits")
        if not isinstance(edits, list):
            return []
        tid = str(task.get("id") or "").strip()
        kept: list[dict] = []
        violations: list[str] = []
        for e in edits:
            if not isinstance(e, dict):
                continue
            path = str(e.get("path") or "").replace("\\", "/").strip()
            owner = owner_map.get(path)
            sym = str(e.get("target_symbol") or "").strip().strip("`").rstrip("()")
            is_whole_rewrite = bool(
                owner
                and owner != tid
                and str(e.get("change_type") or "").strip() == "add"
                and (not sym or sym == Path(path).stem)
            )
            if is_whole_rewrite:
                violations.append(
                    f"{path} 的创建租约属于 {owner}：本任务（{tid}）提交的 change_type=add "
                    f"整份重写（target_symbol={sym or '空'}，即整文件占位写法）已被机械丢弃；"
                    "在制文件已存在，只能用 modify / full_symbol 对你负责的符号做定点增补"
                )
                self.state.setdefault("file_lease_violations", []).append(
                    {"task": tid, "path": path, "owner": owner, "round": self.attempt}
                )
                continue
            kept.append(e)
        if violations:
            data["edits"] = kept
        return violations

    def _py_compile_problems(self, rel_paths: list[str]) -> list[str]:
        """在制工作区上的 **py_compile 快检**（G4 事务点）：语法不过带错误重问，不留到 verify。"""
        import py_compile
        import tempfile

        wip = getattr(self, "_wip_dir", None)
        if wip is None:
            return []
        tmp = tempfile.mkdtemp(prefix="devwip_pyc_")
        problems: list[str] = []
        try:
            for rel in rel_paths:
                if not str(rel).endswith(".py"):
                    continue
                src = wip / str(rel)
                if not src.is_file():
                    continue
                try:
                    py_compile.compile(
                        str(src),
                        cfile=os.path.join(tmp, str(abs(hash(rel))) + ".pyc"),
                        doraise=True,
                    )
                except py_compile.PyCompileError as exc:
                    detail = str(exc).replace(str(wip) + os.sep, "").strip()
                    lines = [ln for ln in detail.splitlines() if ln.strip()]
                    problems.append(f"py_compile {rel}：" + " / ".join(lines[-2:])[:400])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return problems

    @staticmethod
    def _manifest_digest(root: Path | None) -> str:
        """工作区内容寻址摘要（规格§四十一）：``sha256(排序后的 相对路径+文件sha256)``。

        纯机械、确定性：同一文件集必得同一 digest，与物化顺序无关。用于 task transaction
        的 base/result WorkspaceRevision 身份。目录不存在/为空 ⇒ 空串（不伪造 revision）。
        """
        if root is None or not root.is_dir():
            return ""
        items: list[str] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            items.append(f"{rel}:{digest}")
        if not items:
            return ""
        return "wsr:wip:" + hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()[:16]

    def _task_txn_checkpoint(
        self,
        task: dict,
        tasks: list[dict],
        parts_fn: Any,
        plan_pin: Any,
        merged: dict,
        data: dict,
        owner_map: dict[str, str],
        rework: list[str],
    ) -> dict:
        """单张施工图的**事务点**（G3+G4）：租约执法 → 在制物化 → py_compile → 最多重问一次。

        返回（可能被重问结果替换/合并过的）本任务产物。修不掉的残留**只留痕升级**
        （state + 日志，交评审/verify），不在同 prompt 上空转 —— 与补符号零生效同口径。
        """
        # mock 路径整体 no-op：占位产物（<path>/<symbols>）过不了任何内容判据，
        # 也没有在制工作区；mock 测流程不测内容，基线不动。
        if isinstance(self.client, MockClient):
            return data
        tid = str(task.get("id") or "?")
        paths = self._task_paths(task, tasks)
        lease = self._enforce_file_lease(task, data, owner_map)
        cand = self._merge_dev(merged, data) if merged else data
        mat = self._wip_materialize(cand)
        touched = [
            str(p).replace("\\", "/")
            for p in (task.get("target_files") or [])
            if (getattr(self, "_wip_dir", None) / str(p)).is_file()
        ]
        compile_errs = self._py_compile_problems(touched)
        problems = [*lease, *mat.get("problems", []), *compile_errs]

        # 规格§四十一：每个 task transaction 记录语义边界 —— 从哪个 workspace revision
        # 起、物化到哪个 revision、是哪个语义任务的哪个补丁、带了哪些机械快检证据。
        # 链式内容寻址：base = 轮次基线 + 本任务之前已累积补丁；result = 本次物化后工作区。
        def _base_manifest_now() -> str:
            return self._manifest_digest(getattr(self, "_wip_base", None))

        def _base_rev() -> str:
            base_manifest = _base_manifest_now()
            try:
                prior = ontology.stable_hash(
                    ontology.canonical_json((merged or {}).get("edits") or []), length=10)
            except (TypeError, ValueError):
                prior = ""
            return f"{base_manifest}+prior:{prior}" if (base_manifest or prior) else ""

        def _ledger(cur_data: Any, cur_mat: dict, probs: list[str], reasked: bool) -> None:
            try:
                patch_id = "patch:" + ontology.stable_hash(
                    ontology.canonical_json(cur_data if isinstance(cur_data, dict) else {}),
                    length=12)
            except (TypeError, ValueError):
                patch_id = ""
            compile_checks = [
                f"py_compile:{rel}:ok" for rel in touched
            ] if not compile_errs else [f"py_compile:{rel}:FAILED" for rel in touched]
            # 方案§二十二：只给**真正物化成功**的文件登记补丁清单（path/符号/模式），
            # 供 Ontology 投影 Patch/Symbol；元数据按路径回连本任务的 edits。
            written_set = {
                str(p).replace("\\", "/") for p in (cur_mat.get("written") or []) if str(p)
            }
            meta_by_path: dict[str, list[dict]] = {}
            if isinstance(cur_data, dict) and isinstance(cur_data.get("edits"), list):
                for e in cur_data["edits"]:
                    if isinstance(e, dict):
                        ep = str(e.get("path") or "").replace("\\", "/")
                        if ep in written_set:
                            meta_by_path.setdefault(ep, []).append(e)

            def _join(rows: list[dict], key: str) -> str:
                return ";".join(sorted({str(r.get(key) or "") for r in rows if str(r.get(key) or "")}))

            patches_manifest: list[dict] = []
            for path in sorted(written_set):
                rows = meta_by_path.get(path, [])
                symbols = sorted({
                    str(r.get("target_symbol") or "") for r in rows
                    if str(r.get("target_symbol") or "")
                })
                targets = symbols or [""]
                for sym in targets:
                    patches_manifest.append({
                        "path": path, "symbol": sym,
                        "change_type": _join(rows, "change_type"),
                        "patch_mode": _join(rows, "patch_mode"),
                    })
            result_rev = self._manifest_digest(getattr(self, "_wip_dir", None))
            parent_rev = getattr(self, "_wip_head_revision", "") if result_rev else ""
            self.state.setdefault("task_transactions", []).append({
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "round": self.attempt,
                "task": tid,
                "task_semantic_id": str(task.get("semantic_task_id") or ""),
                "patch_id": patch_id,
                "base_workspace_revision": _base_rev(),
                "base_manifest": _base_manifest_now(),
                "parent_workspace_revision": parent_rev,
                "result_workspace_revision": result_rev,
                "patches": patches_manifest,
                "verification_evidence_ids": compile_checks + (
                    ["materialization"] if cur_mat.get("written") else []),
                "status": "escalated" if probs else "committed",
                "reasked": reasked,
                "problems": [str(p) for p in probs][:10],
            })
            # 推进本轮在制工作区链头（方案§二十三：真实父子链）。
            if result_rev:
                self._wip_head_revision = result_rev

        if not problems:
            _ledger(cand, mat, [], False)
            return data
        self.log(
            f"        [任务事务点] {tid} 快检发现 {len(problems)} 个问题（租约 {len(lease)} / "
            f"语法 {len(compile_errs)}）→ 带错误只重问一次"
        )
        try:
            again = self._grounded_call(
                "dev",
                [
                    self._task_focus(task, rework_problems=rework),
                    "【任务事务点快检未过】上一版补丁物化为在制文件后发现以下机械问题，"
                    "请**只针对这些问题**重做本任务的 edit（其余内容保持不变）：\n"
                    + "\n".join(f"  · {p}" for p in problems)
                    + "\n注意：①已存在的文件禁止 change_type=add 整份重写，"
                    "用 modify / full_symbol 对你负责的符号定点增补；"
                    "②不要改其他文件；③在制文件当前正文见下方【当前项目已有代码】。",
                    *parts_fn(
                        include_plan=self._task_drawing_is_thin(task),
                        only_paths=paths,
                        current_code=self._current_code_text(paths),
                    ),
                ],
                pin=plan_pin,
                note=f"dev·{tid}·事务点",
                artifact_stage=f"dev-{tid}-txn",
            )
        except Exception as exc:  # noqa: BLE001
            self.log(
                f"        [任务事务点] {tid} 重问调用失败（{type(exc).__name__}："
                f"{str(exc)[:200]}）→ 保留这一版，残留升级"
            )
            again = {}
        if isinstance(again, dict) and again:
            lease2 = self._enforce_file_lease(task, again, owner_map)
            data = self._merge_dev(data, again)
            cand = self._merge_dev(merged, data) if merged else data
            mat = self._wip_materialize(cand)
            compile_errs = self._py_compile_problems(touched)
            problems = [*lease2, *mat.get("problems", []), *compile_errs]
        if problems:
            self.state.setdefault("task_txn_residual", []).append(
                {"task": tid, "round": self.attempt, "problems": problems}
            )
            self.log(
                f"        [任务事务点] {tid} 重问后残留 {len(problems)} 个问题 → 留痕升级"
                f"（不阻塞后续施工图）：{problems[0][:160]}"
            )
        # 重问后的最终事务边界（只记一条终态；首版快检通过已在前面提前记账返回）。
        _ledger(cand, mat, problems, True)
        return data
    # ===================== G3/G4 结束 =====================


    def _ensure_api_digest(self) -> dict:
        """接口摘要（``{相对路径: [成员签名, …]}``）：dev 自检时已产出；缺了就按当前实现补算。

        测试阶段**必须**拿到它：新建项目里仓库为空（【存量代码片段】整段是空占位），这份摘要
        是唯一能告诉它「类 / 函数该怎么调」的东西。没有它，7B 只能按类名猜构造 —— 真机
        20260927-073518：5 条命令全部写成无参构造（`SnakeGame()` 而 `__init__(width, height)`），
        verify 必然 TypeError，整轮拿不到可运行证据。
        """
        cur = self.state.get("api_digest")
        if isinstance(cur, dict) and cur:
            return cur
        got = self._digest_from_probe(self.state.get("implementation"))
        if got:
            self.state["api_digest"] = got
        return got

    def _digest_from_probe(self, impl: Any) -> dict:
        bundle = self._materialize_probe(impl)
        if bundle is None:
            return {}
        probe, written = bundle
        return verify_mod.api_digest(probe, written)

    def _proof_obligations(self) -> list[ontology.ProofObligation]:
        """当前语义底图上的 PO 清单 —— TestCompiler 的**唯一覆盖清单**（方案§七）。

        优先取 Plan IR 投影出的 ontology 图（与 Proof Gate 同一张底图）；
        极端情况下 IR 缺失时从 scope 现场重建（重建结果不回写，纯只读用途）。
        """
        ir = self.state.get("compiler_ir")
        base = ir.get("ontology") if isinstance(ir, dict) else None
        if isinstance(base, dict):
            return list(ontology.OntologyGraph.from_dict(base).obligations.values())
        graph = ontology.OntologyGraph()
        scope = self.state.get("scope")
        ontology.build_requirement_projection(
            graph, scope if isinstance(scope, dict) else {},
            original_requirement=self.requirement or "",
        )
        return list(graph.obligations.values())

    def _build_test_view(self, interfaces: dict) -> dict:
        """构造测试专用视图（P1-1）：behaviors / acceptance / interfaces / symbols / contracts。

        刻意不放代码正文 —— 真机实测整份 implementation 占测试 prompt ≈66%（≈7800 tok），
        而写用例真正需要的五样信息都在方案 / PM 产物 / 接口摘要里，体积只有零头。
        """
        scope = self.state.get("scope") if isinstance(self.state.get("scope"), dict) else {}
        plan = self.state.get("plan") if isinstance(self.state.get("plan"), dict) else {}

        # ① 行为：PM 的功能需求（id/title/验收）+ 项目级验收标准
        behaviors: list[Any] = []
        for fr in (scope.get("functional_requirements") or []):
            if isinstance(fr, dict):
                behaviors.append(
                    {
                        "id": str(fr.get("id") or ""),
                        "title": str(fr.get("title") or fr.get("description") or ""),
                        "acceptance": fr.get("acceptance"),
                    }
                )
        for crit in (scope.get("acceptance_criteria") or []):
            if str(crit or "").strip():
                behaviors.append(str(crit))

        # ② 每张施工图的验收口径（测试命令断言的直接来源）
        acceptance: list[dict] = []
        # ③ 变更符号（按文件）
        symbols: list[dict] = []
        # ④ 跨文件契约
        contracts: list[dict] = []
        for change in (plan.get("changes") or []):
            if not isinstance(change, dict):
                continue
            syms = [str(s) for s in (change.get("symbols") or []) if str(s).strip()]
            if change.get("path") and syms:
                symbols.append({"path": str(change.get("path")), "symbols": syms})
        for task in (plan.get("tasks") or []):
            if not isinstance(task, dict):
                continue
            tid = str(task.get("id") or "")
            if str(task.get("acceptance") or "").strip():
                acceptance.append({"task": tid, "acceptance": str(task.get("acceptance"))})
            c = task.get("contracts") if isinstance(task.get("contracts"), dict) else {}
            exposes = [str(s) for s in (c.get("exposes") or []) if str(s).strip()]
            uses = [str(s) for s in (c.get("uses") or []) if str(s).strip()]
            interface = str(task.get("interface") or "").strip()
            if exposes or uses or interface:
                contracts.append(
                    {"task": tid, "exposes": exposes, "uses": uses, "interface": interface}
                )
        # ⑤ 证明义务清单：automated_commands.target_po 的**唯一合法取值表**（方案§七/§十）。
        #    只给 id/kind/name，模型原样抄写 id 即可，不允许自造 PO 身份。
        proof_obligations: list[dict] = []
        try:
            proof_obligations = [
                {"id": po.id, "kind": po.kind, "name": po.name, "required": po.required}
                for po in self._proof_obligations()
            ]
        except Exception as exc:  # 视图构建永不阻断测试阶段（纯增强信息）
            self.log(f"        [测试视图] 证明义务清单构建失败，忽略：{type(exc).__name__}")
        return {
            "behaviors": behaviors,
            "acceptance": acceptance,
            "interfaces": interfaces if isinstance(interfaces, dict) else {},
            "symbols": symbols,
            "contracts": contracts,
            "proof_obligations": proof_obligations,
        }

    def _test_command_problems(self, report: Any) -> list[str]:
        """测试声明的命令**自身能不能执行**？—— 交给 verify 的静态核对（单一真源）。

        为什么要在落盘前查：命令是模型写出来的文本、本轮从没被执行过，写错了要到 verify 才
        暴露 —— 而那时评审会把失败误读成"实现缺陷"，让开发去改**本来就正确**的实现
        （真机 20260927-073518）。命令只有测试阶段能修，所以在测试阶段就拦住。
        判据（参数个数不足 / 用裸 `None` 顶替对象）见 :func:`verify.command_param_problems`。
        """
        if isinstance(self.client, MockClient):
            return []
        cmds = [c for c in (report or {}).get("automated_commands") or [] if isinstance(c, dict)]
        if not cmds:
            return []
        return verify_mod.command_param_problems(cmds, self.state.get("api_digest") or {})

    def _execution_check(self, impl: Any) -> dict:
        """物化当前实现 → **真跑一遍产物自带的测试** → 顺带产出接口摘要。

        返回 ``{"problems": [...], "digest": {...}, "ran": [...]}``。

        为什么必须有这一步：三轮真机里 ``python -m unittest game_logic_test``
        **一次都没通过过**，而它是唯一能把「行为不对」暴露出来的东西。
        在此之前 dev 阶段的自检全是**文本级**的（写残 / 缺 import / 类型诊断），
        没有任何一步会真的执行代码 —— 于是「测试跑挂」只能等 test→verify 那一整轮
        （≈5 分钟 + 一次 14B 评审）之后才被发现，而模型下一轮照样写挂。

        刻意**只跑产物自带的测试**、不跑 GUI 入口：``python main.py`` 这类常驻程序
        要等满 VERIFY_TIMEOUT（180s）才判定「能起来」，放进重问循环代价太大；
        而测试是秒级的，且恰好覆盖了最常见的失败面。

        任何一步失败都**静默降级**（返回空 problems）—— 它是增强手段，不是判定依据。
        """
        out: dict[str, Any] = {"problems": [], "digest": {}, "ran": []}
        probe_bundle = self._materialize_probe(impl)
        if probe_bundle is None:
            return out
        probe, written = probe_bundle
        # 接口摘要不依赖「有没有测试」：只要有产出就给。
        out["digest"] = verify_mod.api_digest(probe, written)
        # 落进 state：重问要用它当准绳，**下一轮** dev 也要（跨轮投喂，见 _stage_dev 的 _dev_parts）。
        if out["digest"]:
            self.state["api_digest"] = out["digest"]
        problems: list[str] = []
        # ---- ①② 导入正确性（**无条件跑**，与「有没有测试」无关）----
        # 为什么必须**无条件**：真机 20260927-134222/150931 的交付物里，dev 写出的
        # `from tkinter import event`（幻觉导入）、`from main import main`（自导入 + 未定义）
        # 都是**连 import 都过不去**的硬错；而这一档原先只在"存在 test_*.py"时才执行，
        # 这些新建项目一个测试文件都没有 ⇒ dev 自检等于没有执行级检查 ⇒ 硬错一路活到 verify
        # （那时已烧掉 test + verify + 一次 14B 评审）。
        #
        # mock 下不跑（与 `_orphan_modify_files` / 覆盖审计同一条约定）：mock 的 dev 产物是
        # 按 schema 合成的**占位数据**（路径形如 `<path>`），对它做导入核对只会产出噪声、
        # 触发注定修不好的重问，把「流程机制」的测试带偏。mock 测流程，不测内容质量。
        static: list[str] = []
        if not isinstance(self.client, MockClient):
            # ① 纯 AST：本地符号缺失 / 产出文件与标准库同名（毫秒级、零副作用）
            static = verify_mod.import_symbol_problems(probe, written)
            # ② 真跑一次 import：语法合法但 import 时炸的，只有真 import 才知道
            import_spec = verify_mod.import_check_spec(probe, written)
            if import_spec is not None:
                ires = verify_mod.run_command(
                    import_spec,
                    cwd=probe,
                    timeout=self._DEV_EXEC_TIMEOUT,
                    allowed_bins=VERIFY_ALLOWED_BINS,
                    deny_patterns=VERIFY_DENY_PATTERNS,
                )
                istatus = str(ires.get("status") or "")
                self.state["dev_import_audit"] = {
                    "status": istatus,
                    "exit_code": ires.get("exit_code"),
                    "command": import_spec["command"],
                }
                if istatus == "ok":
                    self.log("        [自检·导入] 产出模块全部 import 正常")
                elif istatus != "unavailable":
                    # unavailable = 环境里没有这个程序，与产物无关（verify 里有同一套区分）
                    detail = (
                        str(ires.get("stderr_tail") or "").strip()
                        or str(ires.get("stdout_tail") or "").strip()
                    )
                    self.log(f"        [自检·导入] 产出模块 import 不过（{istatus}）")
                    static.append(
                        f"你本轮产出的模块 **import 不起来**（`{import_spec['command']}` "
                        f"退出码 {ires.get('exit_code')}）。把下面这段原文读完再改 —— "
                        "导入级错误会让整份产物跑不起来，优先级高于任何功能问题：\n"
                        + (detail[-1200:] or "(没有输出)")
                    )
        problems += static
        modules = verify_mod.test_modules(probe, written)
        if not modules:
            out["problems"] = problems
            return out
        out["ran"] = modules
        spec = {
            "command": f"{verify_mod._python_bin()} -m unittest " + " ".join(modules[:8]),
            "source": "dev-selfcheck",
            "display": "dev 自检：跑产物自带的测试",
        }
        res = verify_mod.run_command(
            spec,
            cwd=probe,
            timeout=self._DEV_EXEC_TIMEOUT,
            allowed_bins=VERIFY_ALLOWED_BINS,
            deny_patterns=VERIFY_DENY_PATTERNS,
        )
        status = str(res.get("status") or "")
        self.state["dev_exec_audit"] = {
            "modules": modules,
            "status": status,
            "exit_code": res.get("exit_code"),
            "command": spec["command"],
        }
        if status == "ok":
            self.log(f"        [自检·执行] 产物自带测试通过（{', '.join(modules)}）")
            out["problems"] = problems
            return out
        if status == "unavailable":
            # 环境里没这个程序 —— 与产物无关，不该拿去重问（verify 里有同一套区分）
            out["problems"] = problems
            return out
        detail = str(res.get("stderr_tail") or "").strip() or str(res.get("stdout_tail") or "").strip()
        self.log(f"        [自检·执行] 产物自带测试没跑通（{status}）")
        problems.append(
            f"你本轮产出的测试自己没跑通（`{spec['command']}` 退出码 {res.get('exit_code')}）。"
            "把下面这段原文读完再改 —— **不要改测试去迁就实现**，除非你确认实现才是错的那一方：\n"
            + (detail[-1600:] or "(没有输出)")
        )
        out["problems"] = problems
        return out

    def _dev_selfcheck(self, impl: Any) -> list[str]:
        """dev 阶段的自检 —— **问题累积，不串成短路链**。

          ① 字面（写残 / 未闭合 / 缺依赖）—— 毫秒级
          ② 类型诊断（物化 + pyright）—— 秒级；**只在 ① 干净时跑**：
             语法残片会让 pyright 报一堆连锁错误，把真正的问题淹掉
          ③ 导入正确性（① 纯 AST：本地符号缺失 / 与标准库同名；② 真跑一次 import）
             —— 毫秒~秒级，**无条件跑**（真机：连 import 都过不去的硬错必须在这里拦住，
             否则要等 test + verify + review 一整轮）
          ④ 真跑产物自带的测试 —— 秒级，**无条件跑**
          ⑤ 方案点名的文件有没有产出 —— 毫秒级，**无条件跑**

        ③ 为什么要无条件跑（而不是接在 ①② 后面短路）：拿真机 run snake-v3 的产物回放，
        第 1 轮的字面自检就报了 6 处，于是 ③ **一次都没运行过** —— 而「单测跑不过」
        正是三轮都过不去的坎。执行结果与文本检查查的是完全不同的东西（行为 vs 形式），
        一个有问题不代表另一个不用看；而且它只给一条清晰的 traceback，不会像 pyright
        那样产生连锁噪音，所以不需要为它设前置条件。

        ③ 还顺带产出「已产出文件的接口摘要」（写进 state），供重问与下一轮 dev 当准绳。
        """
        problems = self._invalid_new_files(impl)
        if not problems:
            problems = self._semantic_problems(impl)
        check = self._execution_check(impl)
        # ④ 与前三档查的是**完全不同的东西**：前三档问「写出来的东西好不好」，
        #    这一档问「方案点名的东西有没有写出来」。所以同样无条件跑 —— 一个文件被整个
        #    漏掉时，字面/类型/执行三档**全都干净**（没有那个文件，自然没有它的错误）。
        #    见 :meth:`_missing_plan_files` 里记录的真机失效。
        return [
            *problems,
            *(check.get("problems") or []),
            *self._missing_plan_files(impl),
            # ⑤ 与 ④ 互补：④ 问「方案点名的文件有没有提到」，这一档问「提到的补丁能不能套用」
            #   —— 只 modify 从不 add 时，文件被"提到"了却永远不会被创建出来。
            *self._orphan_modify_files(impl),
        ]

    def _orphan_modify_files(self, impl: Any) -> list[str]:
        """「有 modify 补丁、却从没有 add、仓库里也没有这个文件」—— 补丁物理上无法套用。

        真机 20260927-023153：`main.py` / `game.py` 只有 modify（modify 要求目标文件已存在），
        而仓库是空的新建项目，于是 2 条补丁未能套用、沙箱里根本没有这两个文件 —— 没有入口、
        `python main.py` 直接失败，整轮返工。字面 / 类型 / 执行三档自检**全都查不出来**
        （文件在补丁里"提过"了，没有它自然也没有它的报错），只有把「能不能套用」单独判一遍
        才抓得到。判出来后交给既有的 dev 重问流程，让模型补 add。

        「从未 add」要把上一轮的累积实现一起算进来：返工轮合法地对前几轮新建的文件发 modify，
        那时文件在沙箱里、却还没进仓库（未交付），只按仓库判会误报。
        """
        # mock 下不生效：mock 的 dev 产物是按 schema 合成的 modify 占位数据，
        # 每轮都会命中，白烧重问预算并把「流程机制」的测试带偏（与覆盖审计 /
        # 测试覆盖既有的同一条约定一致：mock 测流程，不测内容质量）。
        if isinstance(self.client, MockClient) or not isinstance(impl, dict):
            return []

        def _adds(container: Any) -> set[str]:
            out: set[str] = set()
            for e in (container or {}).get("edits") or [] if isinstance(container, dict) else []:
                if isinstance(e, dict) and str(e.get("change_type") or "") == "add" and e.get("path"):
                    out.add(str(e.get("path")).replace("\\", "/"))
            return out

        added = _adds(impl) | _adds(self.state.get("implementation"))
        out: list[str] = []
        seen: set[str] = set()
        for e in (impl.get("edits") or []):
            if not isinstance(e, dict) or str(e.get("change_type") or "") != "modify" or not e.get("path"):
                continue
            p = str(e.get("path")).replace("\\", "/")
            if p in added or p in seen or self._repo_has_file(p):
                continue
            seen.add(p)
            out.append(
                f"`{p}` 只有 modify 补丁，但仓库里没有这个文件、方案里也没有 add 新建它 —— "
                "modify 要求目标文件已存在，这条补丁**无法套用**（物化后该文件根本不会出现）。"
                f"请为 `{p}` 补一条 change_type=add 的完整文件补丁（整份内容）。"
            )
        return out

    def _repo_has_file(self, rel: str) -> bool:
        """仓库里是否已存在该相对路径的文件（决定 modify 能否套用）。"""
        if not self.repo or not rel:
            return False
        try:
            return os.path.isfile(os.path.join(self.repo, rel))
        except OSError:
            return False

    def _invalid_new_files(self, impl: Any) -> list[str]:
        """机械自检：新增文件的内容是否合法（写残 / 未闭合 / 空）**且依赖装不装得上**。

        只看 `change_type == "add"` 且给的是**整份内容**的补丁；diff 形态取不全文，
        交给补丁机械校验与运行验证去管。

        两类问题都属「可机械判定、模型无法自查」：模型既看不到编码层（裸 CR 已经被归一）
        也不知道运行环境里装了什么包，所以在 dev 阶段把事实回灌给它，远好过跑完一整轮再回炉。
        """
        own = self._own_module_names(impl)
        out: list[str] = []
        for edit in (impl or {}).get("edits") or []:
            if not isinstance(edit, dict):
                continue
            change_type = str(edit.get("change_type") or "")
            patch_mode = str(edit.get("patch_mode") or "")
            patch = str(edit.get("patch") or "")
            if not patch.strip() or patches.DIFF_RE.search(patch):
                continue
            path = str(edit.get("path") or "")
            label = f"`{path}`（符号 {edit.get('target_symbol') or '-'}）"
            if change_type == "add":
                body = patches._new_file_body(patch)
                problem = patches.check_new_file_content(body, path)
                if problem:
                    out.append(f"{label}：{problem}")
                for item in patches.unavailable_imports(body, path, own):
                    out.append(f"{label}：{item}")
            elif change_type == "modify" and patch_mode == "full_symbol":
                # modify/full_symbol 的补丁在套用前没有任何物化环节能验语法：
                # 它要先靠 anchor/符号定位才能进沙箱，定位失败时 pyright 看到的还是旧文件。
                # 于是「写残的整符号替换」会一路溜到 verify（真机 run 20260928-200631：
                # 换行双重转义压成单行，三补丁 SyntaxError → 永不套用 → 每轮恒定阻断）。
                problem = patches.check_symbol_block_content(
                    patch, path, str(edit.get("target_symbol") or "")
                )
                if problem:
                    out.append(f"{label}：{problem}")
        return out

    @staticmethod
    def _apply_repair(merged: dict, again: Any) -> dict:
        """用重出的那一版**替换**它针对的那些补丁（按 :meth:`_edit_key` 认身份）。

        刻意不走 `_merge_dev`（那是并集）：重出时 anchor / patch_mode 很可能与上一版不同，
        若按并集处理会同时留下"旧版 + 新版"两条同处补丁。

        认领顺序（真机 20260927-150931 的 mock 回放暴露了粗键的两种错法，这里是修法）：
          ① **精确键**（path, 符号, 模式, anchor）—— 同一处改动的两次尝试，直接替换；
          ② 再退一档 **(path, 符号, 模式)** —— 重出时 anchor 常被写得更"正"（原样抄全签名），
             精确键认不出它，但那仍是**同一处改动的修正**：认不出就会把修复丢掉，比不修更糟；
          ③ 都认不到的（重出新增的锚点）**追加** —— 那同样是对问题的修复，丢掉等于白问一次。
        另外：重出给了 `full_symbol` 时，该符号上其它分片补丁一并去掉（留着套用后会有两份定义）。

        反面教材（原先的实现）：只用 (path, 符号) 当键 → 同一符号上合法存在的多条补丁
        （`full_symbol` 整符号替换 + `replace_span` 只改签名、两遍分片重构的多条 replace_span）
        会被**并成一条**，静默丢掉其中一处改动，问题清单里也跟着少一条真问题。
        """
        if not isinstance(merged, dict):
            return merged
        remaining = [e for e in ((again or {}).get("edits") or []) if isinstance(e, dict)]
        if not remaining:
            return merged

        superseded = {
            (str(e.get("path") or ""), str(e.get("target_symbol") or "").strip())
            for e in remaining
            if str(e.get("patch_mode") or "") == "full_symbol" and e.get("target_symbol")
        }

        def _claim(old: dict) -> dict | None:
            """在尚未认领的重出补丁里找 `old` 的对应版（精确键优先，再退 (path,符号,模式)）。"""
            wanted = Orchestrator._edit_key(old)
            loose = (
                str(old.get("path") or ""),
                str(old.get("target_symbol") or "").strip(),
                old.get("patch_mode"),
            )
            for i, cand in enumerate(remaining):
                if Orchestrator._edit_key(cand) == wanted:
                    return remaining.pop(i)
                if loose[1] and loose == (
                    str(cand.get("path") or ""),
                    str(cand.get("target_symbol") or "").strip(),
                    cand.get("patch_mode"),
                ):
                    return remaining.pop(i)
            return None

        out: list[dict] = []
        for edit in merged.get("edits") or []:
            if not isinstance(edit, dict):
                continue
            hit = _claim(edit)
            if hit is not None:
                out.append(hit)
                continue
            pair = (str(edit.get("path") or ""), str(edit.get("target_symbol") or "").strip())
            if pair in superseded:
                continue  # 被整符号替换取代（留着会得到两份定义）
            out.append(edit)
        out.extend(remaining)  # 重出新增的（没认领到的）：留下
        merged["edits"] = out
        return merged

    @staticmethod
    def _edit_key(e: dict) -> tuple:
        """补丁的身份键：**同键 = 同一处改动的两次尝试**（后写覆盖）。

        只用 (path, target_symbol) 是不够的：同一个符号上**合法地**会有多条补丁 ——
        `full_symbol` 整符号替换 + `replace_span` 只改签名、两遍分片重构时同一主函数产出
        多条 `replace_span`（见 `_merge_dev` 的注释）。粗键会把它们并成一条、
        **静默丢掉其中一处改动**。合并、返修替换两处共用这一个键，口径才不会各说各话。
        """
        path = str(e.get("path") or "")
        symbol = str(e.get("target_symbol") or "").strip()
        if symbol:
            return (path, symbol, e.get("patch_mode"), e.get("anchor") or "")
        if str(e.get("change_type") or "") == "add":
            return (path, "__new_file__")
        return (
            path,
            "",
            e.get("patch_mode"),
            e.get("anchor") or "",
            hashlib.md5(str(e.get("patch") or "").encode("utf-8", "replace")).hexdigest(),
        )

    @staticmethod
    def _merge_dev(p1: dict, p2: dict) -> dict:
        """两遍开发的产物合并：edits 取并集（按 (path, target_symbol) 去重、后写覆盖），
        完整性与自检以第二遍为准。后写覆盖可吸收「第二遍又重复定义了辅助函数」的情况，
        保证最终用的是带正确锚点的那一版。"""
        p1 = p1 or {}
        p2 = p2 or {}
        out: dict = {}

        def _lst(a: dict, k: str) -> list:
            v = a.get(k)
            return v if isinstance(v, list) else []

        merged: dict[tuple, dict] = {}

        def _union_covers(a: Any, b: Any) -> list:
            """同键覆盖时合并两张 covers_tasks 声明（保序去重）。"""
            seen: set[str] = set()
            out: list = []
            for x in list(a or []) + list(b or []):
                k = str(x)
                if k and k not in seen:
                    seen.add(k)
                    out.append(x)
            return out

        for e in _lst(p1, "edits") + _lst(p2, "edits"):
            # 注意：`and e.get("target_symbol")` 这个条件曾把**没写符号的 edit 整条丢掉** ——
            # 整份新建文件（尤其 main.py 这种入口）常常不写 target_symbol，于是文件凭空消失：
            # 真机 20260927-030247 的 main.py 在两个 dev 产物里都有 add，最终实现里却一条不剩，
            # 沙箱没有入口、`python main.py` 必然失败。符号**缺失不等于这条 edit 无效**。
            if not (isinstance(e, dict) and e.get("path")):
                continue
            # 身份键统一走 `_edit_key`：合并与返修替换共用一套口径，否则同一件事在两处
            # 会被判成不同的键（真机踩过：返修替换用粗键，把同符号的多条分片并成了一条）。
            # 语义见那里的注释 —— 同 (path, 符号, 模式, anchor) = 同一处改动的两次尝试，
            # 后写（第二遍）覆盖先写；整份新建且没写符号时按路径后写覆盖，避免重复定义。
            key = Orchestrator._edit_key(e)
            prev_e = merged.get(key)
            if prev_e is not None:
                # 后写覆盖的是**内容**，但两张补丁各自的「我覆盖了哪个任务」声明都得留下：
                # 跨任务累加时（T-02/T-03 都对 db.py 出 add/full_symbol/Database），
                # 不合并就等于后一个任务把前一个的 covers 一起抹掉 —— 覆盖审计随即恒定报
                # 「T-02 未被任何补丁覆盖」，即使补丁内容里 T-02 的方法一个不少
                # （真机 run 20260928-200631，console：4 张图合并后 3 条）。
                # 同任务两遍合并时两边 covers 相同，union 是 no-op，不会放大覆盖。
                union_covers = _union_covers(prev_e.get("covers_tasks"), e.get("covers_tasks"))
                if union_covers:
                    e = {**e, "covers_tasks": union_covers}
            merged[key] = e
        out["edits"] = list(merged.values())
        # 第一遍搭脚手架时常先把「待第二遍补齐」的任务声明成 not_implemented，第二遍补上之后
        # 这些声明就过期了。若不清理，同一批 task id 会同时出现在 covered 与 declared_ids 里，
        # 审计据此把已实现任务判成未实现、触发 empty_implementation 误报（2026-09-23 贪吃蛇 run）。
        done_ids = {
            str(t)
            for e in _lst(p2, "edits")
            if isinstance(e, dict) and str(e.get("patch") or "").strip()
            for t in (e.get("covers_tasks") or [])
        }
        p1_still_open = [
            x
            for x in _lst(p1, "not_implemented")
            if not (isinstance(x, dict) and str(x.get("task") or "") in done_ids)
        ]
        out["not_implemented"] = p1_still_open + _lst(p2, "not_implemented")
        out["deviations"] = _lst(p1, "deviations") + _lst(p2, "deviations")
        out["self_checks"] = _lst(p2, "self_checks") or _lst(p1, "self_checks")
        # uncertainties 与 self_checks 同策略：第二遍是最终成果，以它为准。
        # 漏掉这一项会让两遍模式下的产物缺必填字段，直接契约失败。
        out["uncertainties"] = _lst(p2, "uncertainties") or _lst(p1, "uncertainties")
        out["summary"] = p2.get("summary") or p1.get("summary") or ""
        out["dev_passes"] = 2
        out["pass1_edits"] = _lst(p1, "edits")
        out["pass2_edits"] = _lst(p2, "edits")
        return out

    # expected 里出现这些词时**未必**笼统（「旧版存档可正常读取」是合格断言），
    # 所以判据是「把模糊词与标点都剥掉后还剩多少实质内容」，而不是简单的关键词命中。
    _VAGUE_EXPECTED = (
        "正常", "没问题", "功能可用", "工作正常", "无异常", "符合预期", "符合配置", "符合要求",
        "运行正确", "结果正确", "一切正常", "验证通过", "正确运行", "可以正常", "正常显示",
        "正确显示", "正确更新", "无崩溃", "不崩溃", "无错误", "正常工作", "正常运行", "立即改变",
    )

    def _entry_command_gap(self) -> str | None:
        """测试产物有没有声明一条**真的执行交付物入口**的命令？

        为什么单独查这一条：test 阶段是模型自己写 `automated_commands`，而真机
        run snake-detailed 的第 2、3 轮它把第 1 轮的 `python main.py` 换成了三条窄命令
        （`python -c "import game_logic; game_logic.GameLogic().move('Right')"`），
        于是 verify 拿不到任何「产物能跑起来」的证据，三轮不收敛。

        verify 侧已经改成「入口探测强制占一个槽位」把**后果**兜住了（不靠模型自觉），
        但**测试产物本身**仍然缺这条最关键的命令 —— 评审与人工该看到这一点。

        只在「确实存在可识别入口」时才报（add 类补丁的文件里含 `__main__`）；
        库模块形态的交付物没有入口，报了就是误伤。
        """
        impl = self.state.get("implementation") or {}
        entries: list[str] = []
        for edit in impl.get("edits") or []:
            if not isinstance(edit, dict) or str(edit.get("change_type") or "") != "add":
                continue
            body = patches._new_file_body(str(edit.get("patch") or ""))
            if "__main__" not in body:
                continue
            name = Path(str(edit.get("path") or "")).name
            if name and name not in entries:
                entries.append(name)
        if not entries:
            return None
        declared = " ".join(
            str(c.get("command") or "")
            for c in (self.state.get("test_report") or {}).get("automated_commands") or []
            if isinstance(c, dict)
        )
        if any(name in declared for name in entries):
            return None
        return (
            f"automated_commands 里没有一条真正执行交付物入口（{', '.join(entries)}）："
            "全是窄断言的话，「产物到底能不能跑起来」就没有机械证据。"
        )

    #: 判别「这条用例在测异常/边界路径」的标志词。刻意宽松：措辞千变万化，
    #: 机械化的目的只是回答「有没有一条在测坏路径」，不是判定哪一条合格。
    _BOUNDARY_HINTS = (
        "异常", "报错", "失败", "非法", "无效", "越界", "超限", "空", "缺失", "边界", "极值",
        "重复", "冲突", "错误", "不存在", "未授权", "拒绝", "容错",
        "invalid", "empty", "error", "fail", "raise", "boundary", "none", "nil", "negative",
    )

    @staticmethod
    def _behavior_terms(text: str) -> set[str]:
        """业务行为文本 → 匹配词集合（CJK bigram + 长度≥2 的字母数字词）。

        刻意做成**纯字面**匹配：行为覆盖核对是机械判据，不能引入语义模型；
        bigram 对中文短句鲁棒（「新增记录」「增加一条记录」共享 新增/记录 等），
        ascii 词保住命令 / 专有名词（add、REAL、unittest）。
        """
        terms: set[str] = set()
        for word in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", str(text or "")):
            if len(word) >= 2:
                terms.add(word.lower())
        for run in re.findall(r"[\u4e00-\u9fff]+", str(text or "")):
            terms.update(run[i : i + 2] for i in range(len(run) - 1))
        return terms

    #: 行为覆盖判定阈值（bigram/词重叠占比与最少命中数）。宁松勿紧 —— 部分漏测只作
    #: 提示级，只有**所有**业务行为都对不上才阻断（见 _test_blockers），误伤面很小。
    _BEHAVIOR_COVER_RATIO = 0.35
    _BEHAVIOR_MIN_SHARED = 2
    _BEHAVIOR_MIN_TERMS = 4

    @staticmethod
    def _term_is_cjk(term: str) -> bool:
        return bool(re.fullmatch(r"[\u4e00-\u9fff]{2}", term))

    def _behavior_hit(self, terms: set[str], blob_terms: set[str]) -> bool:
        """单条行为是否被单条用例覆盖。

        主通道：重叠词占比 ≥ ``_BEHAVIOR_COVER_RATIO`` 且至少 2 个共享词。
        宽松通道：用例短、FR 文本长时占比天然吃亏，但**专有名词**（add / list /
        REAL 这类命令与类型名，长度 ≥3 的 ascii 词）+ 至少 2 个汉字实义 bigram
        同时命中，已足以说明用例在测该行为 —— 阻断只在全部行为都漏掉时发生，
        宽松判定收窄的是误伤面。
        """
        shared = terms & blob_terms
        if len(shared) >= self._BEHAVIOR_MIN_SHARED and len(shared) / len(terms) >= self._BEHAVIOR_COVER_RATIO:
            return True
        cjk_hit = sum(1 for t in shared if self._term_is_cjk(t))
        ascii_hit = any(t.isascii() and len(t) >= 3 for t in shared)
        return ascii_hit and cjk_hit >= self._BEHAVIOR_MIN_SHARED

    def _audit_test(self) -> dict:
        """确定性核对测试产物：三类用例是否齐全、expected 是否笼统、有没有可执行命令。

        test 曾是唯一没有机械审计的阶段（PM / 架构师 / 开发 / 评审都有各自的确定性核对），
        「三类都要覆盖」只写在提示词里，模型漏掉 compat 时没有任何东西会拦。
        措辞可以绕开提示词纪律，但绕不开「cases[].type 里到底有没有 compat」。
        """
        report = self.state.get("test_report") or {}
        cases = [c for c in (report.get("cases") or []) if isinstance(c, dict)]
        by_type: dict[str, int] = {}
        for case in cases:
            kind = str(case.get("type") or "")
            if kind:
                by_type[kind] = by_type.get(kind, 0) + 1
        missing_types = [t for t in ("new", "regression", "compat") if not by_type.get(t)]
        vague: list[str] = []
        for case in cases:
            text = str(case.get("expected") or "").strip()
            if not text:
                continue
            # 剥掉模糊词与标点，剩下不足 14 字的才算「被模糊词占满」。
            # 阈值放宽一格是有意的：真机上 12 条用例里有 6 条是「游戏持续运行，无崩溃或异常」
            # 这类看似完整、实则无法断言的写法，阈值太紧会一条都抓不到。
            # 「旧版存档可正常读取」剥完还剩 16 字，仍不会被误伤。
            residue = text
            for word in self._VAGUE_EXPECTED:
                residue = residue.replace(word, "")
            residue = re.sub(r"[\s，。、,.;；:：!！?？\-—()（）\"'`]+", "", residue)
            if len(residue) < 14:
                vague.append(f"{case.get('id') or '?'}={text[:30]}")
        # ---- 断言型命令：有没有让机器替我们判断对错的证据 ----
        # `python main.py` 这类「启动一下」退出码 0 证明不了任何行为
        # （真机 run 20260924-235001：三条命令全 ok，但真正执行交付物的那条是空跑）。
        # 只做**计数**、不判负：有些验证靠退出码本身就够（如编译检查），
        # 具体够不够交给评审结合 runnability 证据判断。
        commands = [
            str(c.get("command") or "")
            for c in (report.get("automated_commands") or [])
            if isinstance(c, dict)
        ]
        assertion_count = sum(
            1 for cmd in commands
            if any(hint in cmd.lower() for hint in ("assert", "unittest", "pytest", "doctest"))
        )

        # ---- 符号覆盖：本次改的符号，是不是每条都至少被一条用例测到 ----
        # 真机 run 20260925-184300：15 条用例、5 个被改符号，但用例 target 全写成文件名
        # （`game_logic.py`）而补丁是符号级（`Snake`/`Food`/`Game`…）—— 结果只有 1/5 对得上。
        # 「写测试」和「测到点」是两回事，这里把它变成可机械核对的事实。
        #
        # 刻意只作**提示级**：符号名可能以别的形式出现在 steps/expected 里，
        # 也可能某个符号确实无需单独用例（比如只改了内部常量）。误判成阻断会触发
        # 一整轮无谓返工，所以只把清单交给评审与人工。
        symbols = {
            str(e.get("target_symbol") or "").strip()
            for e in ((self.state.get("implementation") or {}).get("edits") or [])
            if isinstance(e, dict) and str(e.get("target_symbol") or "").strip()
        }
        blobs: list[str] = []
        for case in cases:
            blob = " ".join(
                str(case.get(key) or "")
                for key in ("target", "expected")
            )
            blob += " " + " ".join(str(s) for s in (case.get("steps") or []) if isinstance(s, str))
            blobs.append(blob)
        # ---- 异常/边界覆盖 ----
        # 「每个公开入口至少要有 正常 / 非法 / 边界 三类用例」在技术栈无关的前提下没法
        # 逐入口判，退一步做**整体口径**：这批用例里有没有**任何**一条在测坏路径。
        # 一条都没有 ⇒ 只说「没测坏路径」，**不判负**：措辞千变万化，机械化到
        # 「哪个符号缺哪类」必然误伤，而误伤会逼模型编造用例（与 missing_symbols
        # 必须留申诉出口同一个道理）。
        boundary_count = sum(
            1 for blob in blobs if any(h in blob.lower() for h in self._BOUNDARY_HINTS)
        )
        covered: list[str] = []
        missing_symbols: list[str] = []
        for symbol in sorted(symbols):
            # 先看 target（权威），再看整条用例的文本兜底
            if (any(symbol in str(c.get("target") or "") for c in cases)
                    or any(symbol in blob for blob in blobs)):
                covered.append(symbol)
            else:
                missing_symbols.append(symbol)
        # 申诉通道：符号没进用例，但已在 coverage_gaps 里交代过原因 ⇒ 视为已处理。
        # 刻意复用已有的 coverage_gaps 而不是新加字段：契约不变、消费方不用改，
        # 而且 coverage_gaps 本来就是「我覆盖不到什么、为什么、影响多大」的正式出口。
        gap_text = " ".join(
            " ".join(str(g.get(k) or "") for k in ("gap", "reason", "impact"))
            if isinstance(g, dict)
            else str(g)
            for g in (report.get("coverage_gaps") or [])
        )
        missing_unexplained = [s for s in missing_symbols if s not in gap_text]

        # ---- 一级覆盖：业务行为（FR + PM 验收口径）是否有用例承载 ----
        # 优化建议§十七（run 20260929-093329）：Business Coverage 优先于 Symbol Coverage。
        # 符号漏测只是二级事实（内部 helper 没有单独用例不是缺陷）；真正不可接受的是
        # 「用例写了一堆，但 FR 描述的业务行为一条都没对上」。行为来源 = PM 的
        # functional_requirements（描述 + acceptance）与 acceptance_criteria。
        scope = self.state.get("scope") or {}
        behavior_items: list[dict] = []
        for fr in (scope.get("functional_requirements") or []):
            if not isinstance(fr, dict):
                continue
            parts = [str(fr.get("id") or ""), str(fr.get("description") or fr.get("title") or "")]
            for acc in (fr.get("acceptance") or []):
                parts.append(str(acc or ""))
            behavior_items.append({"id": str(fr.get("id") or ""), "text": " ".join(p for p in parts if p)})
        for crit in (scope.get("acceptance_criteria") or []):
            if str(crit or "").strip():
                behavior_items.append({"id": "", "text": str(crit)})
        blob_terms = [self._behavior_terms(blob) for blob in blobs]
        gap_terms = self._behavior_terms(gap_text)
        covered_behaviors: list[str] = []
        missing_behaviors: list[dict] = []
        behavior_unexplained: list[dict] = []
        for item in behavior_items:
            terms = self._behavior_terms(item["text"])
            # 文本太短（标题型条目）时机械判不可靠，宁漏勿误：不纳入统计。
            if len(terms) < self._BEHAVIOR_MIN_TERMS:
                continue
            hit = any(self._behavior_hit(terms, bt) for bt in blob_terms)
            label = item["id"] or item["text"][:30]
            if hit:
                covered_behaviors.append(label)
            else:
                missing_behaviors.append({"id": item["id"], "text": item["text"][:120]})
                # 申诉通道与符号同级：coverage_gaps 里点名过该行为即豁免
                if not self._behavior_hit(terms, gap_terms):
                    behavior_unexplained.append({"id": item["id"], "text": item["text"][:120]})
        return {
            "case_count": len(cases),
            "by_type": by_type,
            "missing_types": missing_types,
            "vague_expected": vague[:6],
            "vague_count": len(vague),
            "command_count": len(commands),
            "assertion_count": assertion_count,
            "gap_count": len(report.get("coverage_gaps") or []),
            "changed_symbols": sorted(symbols),
            "covered_symbols": covered,
            "missing_symbols": missing_symbols,
            # 既没被用例覆盖、也没在 coverage_gaps 里交代的 —— 这才是真漏测
            "missing_unexplained": missing_unexplained,
            # 一级覆盖（业务行为）：被覆盖/未被覆盖的行为清单
            "behavior_count": len(covered_behaviors) + len(missing_behaviors),
            "covered_behaviors": covered_behaviors,
            "missing_behaviors": missing_behaviors,
            "behavior_unexplained": behavior_unexplained,
            # 有没有一条命令真的执行交付物入口（真机 run snake-detailed 就栽在这上面）
            "entry_gap": self._entry_command_gap(),
            # 异常/边界覆盖：只报事实，不判负（见上面 boundary_count 的说明）
            "boundary_count": boundary_count,
            "boundary_gap": bool(cases) and not boundary_count,
        }

    def _test_blockers(self) -> list[str]:
        """阻断级测试问题，两类：

        ① ``automated_commands`` 是空的 —— 测试产物只有文字、没有任何可执行命令。
           这时 verify 除自带的语法/导入检查之外**无物可跑**，「测试写了但没真跑」
           就是这么发生的。**刻意不留申诉出口**：任何技术栈都至少能声明一条跑测试的
           命令，不存在「确实无法声明」的合法情形。
        ② **业务行为整体脱锚**：PM 的 FR / 验收口径没有一条能与现有用例对上
           （``covered_behaviors`` 为 0 且 coverage_gaps 也未逐条申诉）。这是二级覆盖
           模型（优化建议§十七，run 20260929-093329）的一级判据 —— 真机上测试资源被
           函数覆盖率吞掉，add/list/remove 这些用户真正关心的行为反而一条没测。
           刻意只在「**全部**行为都对不上」时阻断：bigram 字面匹配对部分漏测会有误判，
           部分缺失只作提示级（清单进评审），整体脱锚才是高置信的「用例与需求无关」。
           申诉出口仍是 coverage_gaps：点名行为并说明无法/无需覆盖即豁免。

        **符号漏测不再阻断**（降为纯提示级，清单仍在 audit 里给评审/人工）：内部 helper
        没有单独 testcase 不是缺陷，业务用例自然带到即可；硬拦只会逼模型编造无价值用例。

        为什么「入口没被执行」（``entry_gap``）**不在这里**、只作提示级：它有**合法的
        反例** —— GUI / 常驻程序（本项目的贪吃蛇就是 tkinter）在无显示环境里本来就没法
        跑入口。升为阻断会误伤，正是代码里反复警告的「误判成阻断触发一整轮无谓返工」。
        verify 侧也已经用「入口探测强制占一个槽位」把后果兜住了，不依赖模型自觉。

        **mock 运行下不生效**：mock 的测试产物是占位数据，本来就不带符号级 target，
        每轮都会命中，把「回流预算 / 人工打回」这类流程机制的测试整个带偏。
        mock 测的是流程，不是覆盖质量；覆盖逻辑本身由 smoke_mock 直接调本函数验证。
        """
        if isinstance(self.client, MockClient):
            return []
        audit = self.state.get("test_audit") or self._audit_test()
        out: list[str] = []
        if not audit.get("command_count"):
            out.append(
                "automated_commands 是空的：测试产物只有文字描述，没有任何可执行命令。"
                "按项目实际技术栈补至少一条"
                "（如 `python -m unittest xxx`、`go test ./...`、`npm test`）。"
            )
        behavior_count = int(audit.get("behavior_count") or 0)
        # 全部行为都没对上、也没在 coverage_gaps 申诉 —— 用例集与需求整体脱锚
        if behavior_count and not audit.get("covered_behaviors"):
            unexplained = audit.get("behavior_unexplained") or []
            if len(unexplained) >= behavior_count:
                names = [
                    str(b.get("id") or b.get("text") or "")[:40]
                    for b in unexplained[:6]
                    if isinstance(b, dict)
                ]
                out.append(
                    "测试用例与需求的业务行为整体对不上：PM 列出的 "
                    f"{behavior_count} 条 FR / 验收口径没有一条被任何用例覆盖"
                    + (f"（如 {'、'.join(n for n in names if n)}）" if names else "")
                    + "。请对照 functional_requirements 逐条补业务行为用例"
                    "（可观察行为 + 期望值，正常路径优先）；个别行为确实无法/无需覆盖的，"
                    "在 coverage_gaps 点名并说明原因与影响即可。"
                )
        return out

    def _compile_test_scenarios(self) -> dict:
        """Test LLM 产物 → TestCompiler 唯一编译路径（方案§四/§五/§十，纯确定性）。

        * automated_commands 只是 DERIVED 候选，经 TestCompiler 安全筛 + PO 显式归档后
          才成为可执行 TestScenario；
        * 产物落 ``state['test_scenarios']``，覆盖三分类落 ``state['test_scenario_audit']``；
        * 从 **executable** 场景生成 verify 执行绑定（命令→PO/断言/期望退出码）落
          ``state['verify_command_bindings']``：weak 场景（仅 rc=0 无业务断言）**不绑定**，
          防止 rc=0 冒充行为证明；
        * 模型一条 target_po 都没写时绑定映射为空，verify / Proof Gate 自动走旧兼容路径。
        """
        report = self.state.get("test_report")
        report = report if isinstance(report, dict) else {}
        plan = self.state.get("plan") if isinstance(self.state.get("plan"), dict) else {}
        files = sorted({
            str(f)
            for task in (plan.get("tasks") or []) if isinstance(task, dict)
            for f in (task.get("target_files") or []) if str(f)
        })
        try:
            obligations = self._proof_obligations()
        except Exception:
            obligations = []
        compiled = testcompiler.compile_scenarios(
            obligations=obligations,
            files=files,
            planned_commands=list(report.get("automated_commands") or []),
        )
        self.state["test_scenarios"] = compiled
        audit = testcompiler.audit_po_test_coverage(compiled)
        self.state["test_scenario_audit"] = audit
        bindings: dict[str, dict[str, Any]] = {}
        for sc in compiled.get("scenarios") or []:
            if not isinstance(sc, dict) or sc.get("status") != testcompiler.STATUS_EXECUTABLE:
                continue
            po_id = str(sc.get("target_po") or "")
            if not po_id:
                continue
            for act in (sc.get("actions") or []):
                if not isinstance(act, dict) or not act.get("safe"):
                    continue
                cmd = str(act.get("command") or "").strip()
                if not cmd:
                    continue
                slot = bindings.setdefault(cmd, {
                    "target_po_ids": [],
                    "assertions": [str(x) for x in (act.get("assertions") or [])],
                    "expect_exit": int(act.get("expect_exit") or 0),
                })
                if po_id not in slot["target_po_ids"]:
                    slot["target_po_ids"].append(po_id)
                for assertion in (act.get("assertions") or []):
                    if assertion not in slot["assertions"]:
                        slot["assertions"].append(str(assertion))
        self.state["verify_command_bindings"] = bindings
        if not isinstance(self.client, MockClient):
            self.log(
                f"        [TestCompiler] 场景 {len(compiled.get('scenarios') or [])}："
                f"覆盖 PO {len(audit['covered'])} / weak {len(audit['weak'])} / "
                f"缺口 {len(audit['missing'])} / 绑定命令 {len(bindings)}"
            )
        return compiled

    def _stage_test(self, requirement: str, fixes: list[str] | None = None) -> Any:
        # 接口摘要：新建项目里它是测试**唯一**能知道"类/函数怎么调"的依据（见 _ensure_api_digest）。
        digest = self._ensure_api_digest()
        # 测试视图：行为/验收/接口/变更符号/契约，**不含 implementation 全文**（P1-1）。
        view = self._build_test_view(digest)
        self.state["test_view"] = view

        def _send(repair: list[str] | None, note: str | None) -> None:
            self.state["test_report"] = self._grounded_call(
                "test",
                prompts.parts_test(
                    requirement,
                    self.state.get("scope"),
                    self.state.get("plan"),
                    api_digest=digest,
                    fixes=fixes,
                    repair=repair,
                    test_view=view,
                    # 按需拉代码：只有命令自检发现「光凭接口写不出可执行命令」时，
                    # 重问通道才把相关函数正文补进来（首轮默认无代码正文）。
                    code_on_demand=self._code_text("test") if repair else "",
                ),
                note=note,
            )
            # 机械审计测试产物，结论会 pin 进评审（评审 prompt 要求核对三类是否齐全）
            self.state["test_audit"] = self._audit_test()

        _send(None, None)
        # 自检 + 带问题重问：命令自身不可执行（参数不足/引用了未定义名）**只有测试阶段能修**。
        # 不修就等着 verify 判负 → 评审误判成"实现缺参数" → 让开发去改正确的实现（真机 20260927-073518）。
        _shown: frozenset[str] | None = None
        for attempt in range(1, self._TEST_REPAIR_TRIES + 1):
            problems = self._test_command_problems(self.state.get("test_report"))
            if not problems:
                break
            _now = frozenset(problems)
            if _shown is not None and _now == _shown:
                self.log(
                    f"        [测试自检] 重问无进展（{len(problems)} 条命令仍不可执行，与上一版相同）"
                    "→ 停止重问，交给 verify 与人工"
                )
                break
            _shown = _now
            self.log(
                f"        [测试自检] {len(problems)} 条命令自身不可执行 → 带问题重问 test"
                f"（第 {attempt}/{self._TEST_REPAIR_TRIES} 次）"
            )
            _send(problems, f"test 重出·命令不可执行（第 {attempt} 次）")
        # TestCompiler 主链接入：测试命令（DERIVED 候选）→ 编译场景 → verify 绑定（方案§四/§五）
        self._compile_test_scenarios()
        return self.state["test_report"]

    def _stage_verify(self, requirement: str = "") -> Any:
        """运行验证（非模型阶段）：把补丁物化到沙箱、**真的跑一遍**，产出机械证据。

        这一步存在的理由：评审（8K 上下文的 14B）读代码正文既看不出「能不能跑」，
        也拿不到任何证据。真机教训 run 20260924-135801 —— 机械审计报「6 条补丁全可套用 /
        0 问题」，而其中 4 条指向同一个新文件、逐条写入互相覆盖，落盘只剩 1 个类，
        跑起来必然崩。**这类结论只能靠执行得到**，读文件读不出来。

        安全策略与命令白名单见 ``pipeline/verify.py``；mock 运行只计划命令、不执行。
        """
        started = time.time()
        assert self.run_dir is not None
        impl = self.state.get("implementation")
        audit = self.state.get("patch_audit") or self._audit_patches()
        self.state["patch_audit"] = audit
        mock = isinstance(self.client, MockClient)
        report = verify_mod.verify(
            self.run_dir,
            self.repo,
            impl,
            audit,
            self.state.get("test_report"),
            enabled=VERIFY_ENABLED,
            timeout=VERIFY_TIMEOUT,
            max_commands=VERIFY_MAX_COMMANDS,
            copy_limit_mb=VERIFY_COPY_LIMIT_MB,
            skip_dirs=VERIFY_SKIP_DIRS,
            allowed_bins=VERIFY_ALLOWED_BINS,
            deny_patterns=VERIFY_DENY_PATTERNS,
            mock=mock,
            project_type=self.project_type,
            # TestCompiler 编译出的「命令→PO/断言」绑定：执行结果证据原样继承（方案§十）
            command_meta=self.state.get("verify_command_bindings") if isinstance(
                self.state.get("verify_command_bindings"), dict) else None,
        )
        # new 项目无仓库时，verify 内部建了 verify/base 空基线并按新增文件重新审计；
        # 回写 patch_audit，让下游契约核对/评审看到的审计与物化用的同一份。
        audit_used = report.pop("audit_for_state", None)
        if isinstance(audit_used, dict):
            self.state["patch_audit"] = audit_used
            audit = audit_used
        report["mode"] = "mock" if mock else "real"
        report["elapsed_s"] = round(time.time() - started, 2)
        # 落盘后的第二道红线 + 记录卫生：
        #   · `scan_tree` 查的是「文件本身的状态」（编码合法性、.env 有没有进忽略清单）——
        #     这些在补丁正文里看不出来，只有真文件才有；
        #   · 重复 seq 是「两代产物混存」的机械证据（runstore.duplicate_stage_seqs）。它不判负
        #     （产物能不能跑与它无关），但必须让人看见 —— 真机 job-…-M-01 因此出现阶段列表
        #     重复且乱序，看「最新产物」会读成错的那一代。
        work = str(report.get("sandbox") or "")
        if work and not mock:
            self._rule_findings(work=work, written=list(report.get("materialized") or []))
        dup = runstore.duplicate_stage_seqs(self.run_dir)
        if dup:
            self.state["duplicate_stage_seqs"] = dup
            report["notes"].append(
                "本运行目录里同一 seq 存在多份产物（两代混存，看「最新产物」易读错）："
                + "；".join(dup[:3])
            )
            self.log(f"        [记录卫生] 同一 seq 出现多份产物：{dup[0]}")
        # **跨文件契约比对**（聚合验证的静态核心）：方案声明的 exposes / uses / interface
        # 与产物里真实的符号做毫秒级比对。此前这些只被写、没被核过，于是"接口对不上"
        # 只能等真跑才炸（真机 snake-v2：ui.py 读并不存在的 game_logic.score）。
        # 结果按文件归因 ⇒ 能直接定位到哪张施工图没达标（见 _contract_blockers）。
        if work and not mock:
            contract = verify_mod.contract_check(
                work, list(report.get("materialized") or []), self.state.get("plan")
            )
            # 规格§十八：显式留痕「契约路这轮真跑过」，Interface Freeze 双证据判定需要
            # 区分「检查跑过且无问题(PROVEN)」与「检查根本没跑(UNPROVEN)」。
            self.state["contract_checked"] = True
            self.state["contract_problems"] = list(contract.get("problems") or [])
            if contract.get("problems"):
                self.log(
                    f"        [契约比对] {len(contract['problems'])} 处跨文件接口与方案不符"
                    f"（核对 {contract.get('checked', 0)} 条声明）"
                )
                for line in contract["problems"][:3]:
                    self.log(f"          - {line[:100]}")
            elif contract.get("checked"):
                self.log(f"        [契约比对] {contract['checked']} 条接口声明全部对得上")
            # **冻结接口基准 vs 实际产物**（P0-2）：上面核的是方案里可选的 interface/contracts
            # —— 真机上通常为空，等于没核；这里核**方案期冻结的、非空的**基准。声明的类/函数
            # 在产物里完全不存在，是无歧义的硬缺陷（按基准调用的文件必然 ImportError），
            # 因此与 contract_problems 走同一条机械通道（→ _contract_blockers）。
            conformance = verify_mod.skeleton_conformance(
                work, list(report.get("materialized") or []), self.state.get("skeleton")
            )
            # 规格§十八：完整骨架一致性结果（含 by_file/mismatch）留给 Ontology Proof Gate
            # 做 Interface Freeze 的**第二路证据**；{} 表示本轮无冻结基准（该路 UNPROVEN）。
            self.state["skeleton_conformance"] = dict(conformance)
            if conformance.get("missing"):
                # **不进 `contract_problems`（不判负）**：它核的是「骨架（方案期第二次调用）
                # 与产物」的一致性，而骨架与方案本身可能互相不一致 —— 真机 20260927-123032：
                # 骨架声明 `class Collision`，方案 T-03 要的却是函数 `check_collision`，
                # 开发照方案实现 ⇒ 这里报"类不存在"。那是**两次 14B 输出不一致**，不是实现缺陷；
                # 把它当阻断项会强制返工、白烧一轮。真正该判负的「声明了却没定义」由
                # `verify.contract_check`（核**方案**的 contract/interface）与 `interface_audit`
                # 独立覆盖 —— 那两条才是权威。
                self.state["skeleton_problems"] = list(conformance["missing"])
                self.log(
                    f"        [接口基准] 骨架与产物有 {len(conformance['missing'])} 处不一致"
                    f"（核对 {conformance.get('checked', 0)} 条；**只记录不判负** —— "
                    "骨架与方案可能各说各话，权威以方案的 contract/interface 为准）"
                )
                for line in conformance["missing"][:3]:
                    self.log(f"          - {line[:100]}")
            elif conformance.get("checked"):
                self.log(f"        [接口基准] {conformance['checked']} 条声明与产物一致")
            if conformance.get("mismatch"):
                self.log(
                    f"        [接口基准] {len(conformance['mismatch'])} 处签名与基准不同（只记录，不判负）"
                )
        self.state["verify_report"] = report
        # 记录「最后一个验证通过的版本」：触顶 / 异常结束时交付它能给出**能跑的**产物，
        # 而不是把最后一版（很可能已被下一轮改坏）端出去。
        # 真机 run 20260924-235001：8 轮全 rework 且越改越坏（第 8 轮直接 ImportError），
        # 最终 needs_human、什么都没交付 —— 而更早的轮次明明产出过可运行版本。
        if report.get("verdict") == "pass" and not mock:
            # 建议⑮：验证通过的这一刻把**被验证的那一份字节**固化成一等实体
            # （runs/<id>/verified_workspace/ + verified_manifest.json）。last_good 只存
            # 清单引用，兜底交付直接从该目录逐字节复制 —— 验证对象与交付对象因此天然同一份，
            # 不再依赖「某轮 dev 快照能否凑齐沙箱文件集合」（真机 snake-v2 缺文件的事故根因）。
            frozen = self._freeze_verified_workspace(work, report)
            if frozen:
                self.state["last_good"] = {
                    "verified_manifest_id": frozen["manifest_id"],
                    "manifest_file": "verified_workspace/verified_manifest.json",
                    "verified_digest": frozen["digest"],
                    "attempt": self.attempt,
                    "verify_summary": report.get("summary") or "",
                }
        commands = report.get("commands") or []
        self._record(
            "verify",
            report,
            {
                "kind": "verify",
                "stage": "verify",
                "verdict": report.get("verdict"),
                "commands": len(commands),
                "failures": len([c for c in commands if c.get("status") not in ("ok", "skipped")]),
                "wall_s": report["elapsed_s"],
                "note": report.get("summary") or "",
            },
            "",
        )
        self._log_verify(report)
        return report

    def _log_verify(self, report: dict) -> None:
        verdict = report.get("verdict")
        if verdict == "fail":
            problems = [str(p) for p in (report.get("problems") or [])][:3]
            self.log(f"        [运行验证] 失败：{'；'.join(problems)}")
        elif verdict == "pass":
            self.log(f"        [运行验证] 通过（执行 {len(report.get('commands') or [])} 条命令）")
        else:
            self.log(f"        [运行验证] 未执行：{report.get('summary') or ''}")
        for cmd in (report.get("commands") or [])[:6]:
            state = verify_mod.STATUS_CN.get(str(cmd.get("status")), str(cmd.get("status")))
            tail = f"（{cmd['reason']}）" if cmd.get("reason") else ""
            self.log(f"          - [{state}] {cmd.get('command')}{tail}")
        unverified = [str(x) for x in (report.get("unverified") or []) if str(x).strip()]
        if unverified:
            # 未验证项必须显式打出来：`verdict=pass` 说的是「跑过的都过了」，
            # **不等于**「该验的都验了」。不打出来，人会把 pass 读成"全都验过了"。
            self.log(f"        [未验证] {len(unverified)} 项（pass ≠ 该验的都验了）")
            for item in unverified[:3]:
                self.log(f"          - {item}")
        # **逐项验收**：每条修复项 ↔ 本轮的机械执行结果（转绿 / 仍失败 / 无从核对）。
        # 存进 state 供评审与人工看。没有它，"修好了没"只能整体看 verdict —— 无法逐项追，
        # 而"逐项可追溯"正是返工反复不收敛时最缺的可观测性。
        verdicts = tasktype.defect_verdicts(
            self.state,
            list(getattr(self, "fixes", None) or []),
            plan=self.state.get("plan"),
            # 与缺陷单、与 dev 看到的正文**同源**（见 _current_candidates）
            sources=self._current_sources(),
        )
        self.state["defect_verdicts"] = verdicts
        if verdicts:
            counts = {k: 0 for k in ("green", "red", "unverifiable")}
            for row in verdicts:
                counts[str(row.get("status"))] = counts.get(str(row.get("status")), 0) + 1
            self.log(
                f"        [逐项验收] {len(verdicts)} 条修复项：转绿 {counts.get('green', 0)} / "
                f"仍失败 {counts.get('red', 0)} / 无从核对 {counts.get('unverifiable', 0)}"
            )

    def _stage_review(self, requirement: str, fixes: list[str] | None = None) -> Any:
        parts = prompts.parts_review(
            requirement,
            self.state.get("scope"),
            self.state.get("plan"),
            self.state.get("implementation"),
            self.state.get("test_report"),
            fixes,
            self.state.get("verify_report"),
            # 红线检查的机械证据（含每条规则的反例判据）直接摆到评审面前：
            # 判负理由与"什么能推翻它"都写清楚，评审就没有靠猜测凑条目的空间。
            rules_block=rules.format_block(self.state.get("rule_findings") or []),
            # 逐项验收：本轮每条修复项的机械核对结果（只列仍失败 / 无从核对的）
            defect_block=tasktype.render_defect_verdicts(
                self.state.get("defect_verdicts") or []
            ),
        )
        audit = self.state.get("implementation_audit") or self._audit_implementation()
        # 审计结论必须被看到（预算最紧的正是评审阶段），所以走 pin 而不是普通片段
        pinned = [prompts.implementation_audit_block(audit)] if audit else []
        patch_audit = self.state.get("patch_audit") or self._audit_patches()
        # 有仓库时照常展示；**没有仓库但有判负项时也要展示** —— 新增文件的内容校验与仓库无关，
        # 只看补丁本身（新建项目没给 --repo 时正好走这条路），不展示等于把证据藏起来。
        if patch_audit.get("source_available") or patch_audit.get("problems"):
            pinned.append(prompts.patch_audit_block(patch_audit))
        # 测试审计同样 pin：评审 prompt 要求核对「三类用例是否齐全」，
        # 这里给它机械依据，而不是让评审靠感觉判断
        test_audit = self.state.get("test_audit") or self._audit_test()
        if test_audit.get("case_count"):
            pinned.append(prompts.test_audit_block(test_audit))
        # 影响面：谁在调用本次被改的符号。评审拿它判断「回归测试有没有覆盖到真正的上游」，
        # 而不是靠感觉 —— 这是回归测试从「文本梳理」变成「测到点上」的依据。
        verify = self.state.get("verify_report") or {}
        impact_block = prompts.impact_audit_block(verify.get("impact_audit") or {})
        if impact_block:
            pinned.append(impact_block)
        # 语义检查（pyright）：dev 阶段跑出来的类型级问题。dev 的重问只是「尽量修」，
        # 修不掉的残留在评审看不到的话，就白跑了（且这类问题执行常常覆盖不到）。
        semantic_block = prompts.semantic_audit_block(self.state.get("semantic_audit") or {})
        if semantic_block:
            pinned.append(semantic_block)
        self.state["review"] = self._call("review", parts, pin=pinned)
        return self.state["review"]

    def _audit_plan(self) -> dict:
        """确定性核对方案：changes ↔ tasks 交叉覆盖、禁改路径是否被触碰、task id 是否规范。

        方案是整条链的地基 —— 开发按 tasks 施工、覆盖审计按 tasks[].id 核对、测试按
        acceptance 设计用例。id 起得乱七八糟或某个改动文件没被任何任务覆盖，后面每一步
        都跟着歪。此前这些只靠模型自检（真机证明不可靠），这是 plan 阶段的机械防线。
        """
        plan = self.state.get("plan") or {}
        assessment = self.state.get("assessment") or {}
        changes = [c for c in (plan.get("changes") or []) if isinstance(c, dict)]
        tasks = [t for t in (plan.get("tasks") or []) if isinstance(t, dict)]
        change_paths = [str(c.get("path")) for c in changes if c.get("path")]
        task_ids = [self._clean_task_id(t.get("id")) for t in tasks if t.get("id")]
        task_files: set[str] = set()
        for task in tasks:
            task_files.update(str(p) for p in (task.get("target_files") or []))

        uncovered = [p for p in change_paths if p not in task_files]
        dangling = sorted(f for f in task_files if f not in change_paths)
        bad_ids = [i for i in task_ids if not re.fullmatch(r"[A-Za-z]+-\d{2,}", i)]
        dup_ids = sorted({i for i in task_ids if task_ids.count(i) > 1})
        # depends_on 同样要规范化：带引号的 "T-06" 会在这里被判成「引用了不存在的 id」
        unknown_dep = sorted(
            {
                self._clean_task_id(d)
                for t in tasks
                for d in (t.get("depends_on") or [])
                if self._clean_task_id(d) not in task_ids
            }
        )
        # 任务出口必须**可判定**（acceptance 要能转成断言）；方案必须给出撤销条件（rollback）。
        # 两条在契约里一直是必填，但从前没人核对「填得够不够」—— 真机上出现过
        # acceptance = "功能正常" 这种无法转测试的写法，一路流到 test 阶段才发现写不出用例。
        # 判据与测试审计的「expected 是否笼统」同源：剥掉模糊词后剩不下几个字，就是没内容。
        def _residue(text: str) -> str:
            out = text
            for word in self._VAGUE_EXPECTED:
                out = out.replace(word, "")
            return re.sub(r"[\s，。、,.;；:：!！?？\-—()（）\"'`]+", "", out)

        vague_acceptance = [
            str(t.get("id") or "?")
            for t in tasks
            if str(t.get("acceptance") or "").strip()
            and len(_residue(str(t.get("acceptance")))) < 8
        ]
        rollback_text = str(plan.get("rollback") or "").strip()
        weak_rollback = "" if len(_residue(rollback_text)) >= 12 else (rollback_text or "（空）")
        # 禁改路径：评估阶段给出的 forbidden_paths 一条都不该出现在 changes 里。
        # 规则须先过 `_path_rule_stem` 的「它真是一条路径吗」闸门 —— 自由文本规则
        # （"game_logic.py中tkinter导入"）机械折叠会把整个文件判成禁区，见该函数说明。
        forbidden = [str(p) for p in (assessment.get("forbidden_paths") or []) if str(p).strip()]
        rules = [(rule, self._path_rule_stem(rule)) for rule in forbidden]
        ignored = [rule for rule, key in rules if not key]
        touched: list[str] = []
        for path in change_paths:
            stem = self._path_stem(path)
            if not stem:
                continue
            for _rule, rule_stem in rules:
                if rule_stem and (stem == rule_stem or stem.startswith(rule_stem + "/")):
                    touched.append(path)
                    break
        # ---- 按 task 分派的粒度校验
        # 方案阶段没有代码，唯一**可机械判定**的粒度抓手就是它自己声明的 `symbols` 与
        # `target_files`。没有这一层校验，"粒度合适"只能靠模型自觉 —— 而自觉在真机上
        # 一次次被证明不可靠（拆 8 个模块、一个 task 塞半个项目都出现过）。
        oversized: list[str] = []
        no_symbols: list[str] = []
        for task in tasks:
            tid = str(task.get("id") or "?")
            files = [p for p in (task.get("target_files") or []) if str(p)]
            syms = [s for s in (task.get("symbols") or []) if str(s)]
            if len(files) > MAX_TASK_FILES:
                oversized.append(f"{tid}：{len(files)} 个文件（上限 {MAX_TASK_FILES}）")
            if len(syms) > MAX_TASK_SYMBOLS:
                oversized.append(f"{tid}：{len(syms)} 个符号（上限 {MAX_TASK_SYMBOLS}）")
            if not syms:
                # symbols 不只是粒度依据，还是开发的自检清单：漏定义会被 vanished_symbols 抓到
                no_symbols.append(tid)
        # 拆太碎的**真正判据**是"同一个文件被几张图覆盖"，不是总 task 数：
        # 整体判据（task 数 > 文件数×2）抓不到"3 个文件拆 5 张、其中一个文件独占 3 张"这种情况。
        per_file: dict[str, list[str]] = {}
        for task in tasks:
            tid = str(task.get("id") or "?")
            for path in (task.get("target_files") or []):
                norm = str(path).replace("\\", "/")
                if norm:
                    per_file.setdefault(norm, []).append(tid)
        over_covered = [
            f"{p}：被 {len(v)} 张图覆盖（{'、'.join(v)}）"
            for p, v in sorted(per_file.items())
            if len(v) > MAX_TASKS_PER_FILE
        ]
        over_split = len(tasks) > 2 * max(len(change_paths), 1)
        return {
            "change_count": len(change_paths),
            "task_count": len(task_ids),
            # 粒度（按 task 分派的前提）
            "oversized_tasks": oversized[:6],
            "tasks_without_symbols": no_symbols[:6],
            "over_covered_files": over_covered[:6],
            "over_split": over_split,
            # 任务总数上限：8K/3K 的架构师，task 越多后半段越缩水
            "over_task_count": len(tasks) > MAX_PLAN_TASKS,
            # 施工图**没声明契约字段** ⇒ 后续的跨文件契约比对无从下手。
            # 必须显式记下来：否则"0 条契约问题"会被读成"接口都对得上"，
            # 而真实情况是**根本没得比**（真机 20260927-002903：5 张图字段全空）。
            "contracts_missing": [
                str(t.get("id") or "?")
                for t in tasks
                if not (isinstance(t.get("contracts"), dict) and t.get("contracts"))
                and not str(t.get("interface") or "").strip()
            ][:6],
            "uncovered_changes": uncovered,
            "dangling_files": dangling,
            "bad_task_ids": bad_ids,
            "duplicate_task_ids": dup_ids,
            "unknown_depends_on": unknown_dep,
            # 任务出口是否可判定 / 方案有没有撤销条件（见上面的判据说明）
            "vague_acceptance": vague_acceptance[:6],
            "weak_rollback": weak_rollback,
            "forbidden_touched": sorted(set(touched)),
            "forbidden_count": len(forbidden),
            # 非路径规则（自由文本）列出来，让人知道它们**没有**参与机械判负：
            # 既不冤枉改动，也不假装已经核过
            "forbidden_ignored": ignored,
            # 新建项目必须规划一个可执行入口：没有它，开发受白名单约束**无权**创建入口文件，
            # 而运行验证必然判「没有可执行入口」→ 评审要求加 → 开发改不动 → 死循环。
            # 真机 run 20260925-045404 就卡在这里整整 3 轮。
            "missing_entry": self._plan_missing_entry(),
            # 技术栈一致性（方案层，提示级）：changes 里混用多种主语言时先提醒，
            # 真到实现层再混就是阻断级（见 _patch_blockers 的 mixed_stacks）。
            "languages": sorted(self._languages_of(change_paths)),
            "mixed_languages": (
                sorted(self._languages_of(change_paths))
                if len(self._languages_of(change_paths)) > 1
                else []
            ),
        }

    def _plan_entry_files(self) -> set[str]:
        """方案（changes + tasks.target_files）里规划到的「入口型」文件名。"""
        plan = self.state.get("plan") or {}
        names: set[str] = set()
        for change in plan.get("changes") or []:
            if isinstance(change, dict) and change.get("path"):
                names.add(Path(str(change["path"])).name.lower())
        for task in plan.get("tasks") or []:
            if isinstance(task, dict):
                for path in task.get("target_files") or []:
                    names.add(Path(str(path)).name.lower())
        return names & set(verify_mod.ENTRY_NAMES)

    def _plan_missing_entry(self) -> bool:
        """「可运行的新建项目却没有规划入口」—— 方案层缺陷，开发无权补救。

        只对 ``project_type == "new"`` 生效：二次开发的仓库可能本来就是库（没有入口是正常的），
        那种情况下把「缺入口」判成方案缺陷会误伤。
        """
        if self.project_type != "new":
            return False
        return not self._plan_entry_files()

    # ------------------------------------------------------------ 按 task 分派
    def _plan_tasks(self) -> list[dict]:
        plan = self.state.get("plan") or {}
        return [t for t in (plan.get("tasks") or []) if isinstance(t, dict) and t.get("id")]

    def _ordered_plan_tasks(self) -> list[dict]:
        """按 `depends_on` **拓扑排序**：先写的文件不能引用还没写出来的接口。

        同一文件被多个任务改动时顺序同样重要（后者要基于前者的成果）。用稳定的插入序
        保证同一份方案每次得到同样的顺序 —— 顺序不可复现，返工就无法归因。
        """
        tasks = self._plan_tasks()
        known = {str(t.get("id")) for t in tasks}
        done: list[str] = []
        out: list[dict] = []
        pending = list(tasks)
        while pending:
            progressed = False
            for task in list(pending):
                deps = [str(d) for d in (task.get("depends_on") or [])]
                if all(d in done or d not in known for d in deps):
                    out.append(task)
                    done.append(str(task.get("id")))
                    pending.remove(task)
                    progressed = True
            if not progressed:
                # 存在环（`_audit_plan` 只查"引用了不存在的 id"，环查不到）：
                # 剩下的按原序接上，绝不能在这里死循环
                out.extend(pending)
                break
        return out

    def _tasks_for_bugfix(self) -> list[dict]:
        """返工轮：**只重做缺陷单指向的那些任务**。

        这是「最小改动」的真正保证 —— 不是靠提示词叫模型别乱动，而是**根本不派给它
        别的任务**。它拿到哪张施工图，就只能改哪张图覆盖的文件。
        """
        # **在使用点重算**，不读 `state["bug_report"]`：那份存档可能是过期的
        # —— 真机 20260927-192001 里 state 上的 affected 是空的，但按当前 fixes 重算
        #   明明是 {errors.py, test_all.py, test_command.py}。读过期存档会让
        #   「只重做受影响任务」整个失效，退化成整批重做（最小改动也就没了）。
        report = tasktype.bug_report_from_state(
            self.state, list(getattr(self, "fixes", None) or []), plan=self.state.get("plan")
        )
        ordered = self._ordered_plan_tasks()
        # **精确路径优先**：缺陷单的每条 item 在 `defect_items` 里已带归属施工图
        # （补丁声明的 covers_tasks，或按文件反查），`by_task` 是它的分组视图。
        # 直接用 task 身份定位 ⇒ 同一文件被多张图覆盖时也只重做真正肇事的那张，
        # 不会把同文件的无辜图一起拖下水。
        hit_ids = {str(tid) for tid in (report.get("by_task") or {}) if str(tid).strip()}
        if hit_ids:
            precise = [
                task
                for task in ordered
                if str(task.get("id") or "") in hit_ids
            ]
            if precise:
                return precise
        # 降级路径：缺陷项全部没有 task 归属（只有文件级证据）时，才按受影响文件取交集。
        affected = {str(p).replace("\\", "/") for p in (report.get("affected") or {})}
        if not affected:
            return []
        hit: list[dict] = []
        for task in ordered:
            files = {str(p).replace("\\", "/") for p in (task.get("target_files") or [])}
            if files & affected:
                hit.append(task)
        return hit

    #: 单张施工图「声明了却没写出来的符号」最多补几次。
    #: 只补 1 次：这是**机械可判定**的缺失（符号在不在补丁里），补不回来说明这张图本身
    #: 有问题，继续重试只是烧时间 —— 交给后续阶段判负更划算。
    TASK_SYMBOL_RETRIES = 1

    @staticmethod
    def _task_symbol_gaps(task: dict, data: Any) -> list[str]:
        """本张施工图**声明了却没写出来**的符号。

        为什么值得单做一道：方案阶段把 `symbols` 写进施工图，就是为了让「漏定义」变成
        **可机械判定**的事。等到 verify 才暴露，代价是整轮 test+verify+review（≈5 分钟
        + 一次 14B 评审）；在这里查，代价是几十秒且**只重做这一张图**。
        """
        syms = [str(s).strip() for s in (task.get("symbols") or []) if str(s).strip()]
        if not syms:
            return []  # 施工图没声明 ⇒ 无从判定，不冤枉它
        edits = ((data or {}).get("edits") or []) if isinstance(data, dict) else []
        blob = "\n".join(str(e.get("patch") or "") for e in edits if isinstance(e, dict))
        if not blob.strip():
            return syms
        out: list[str] = []
        for sym in syms:
            # `CLI.add` 这类写法取最后一段做匹配：类里的方法名才是补丁里会出现的形式
            leaf = sym.rsplit(".", 1)[-1]
            if leaf and leaf not in blob:
                out.append(sym)
        return out

    @staticmethod
    def _task_paths(task: dict, all_tasks: list[dict] | None = None) -> set[str]:
        """一张施工图**真正相关**的文件集合。

        按 task 分派时用它裁 dev 的输入：7B 的上下文要"较饱和"地服务于这一张图，
        所以只喂 ① 本任务的 target_files ② 前置任务的文件（要接它们的成果）
        ③ 契约里点名的文件。其余文件的正文/接口/缺陷对这一张图都是噪声 ——
        既吃预算，又让模型惦记"这次不用做"的文件（越界改动的来源之一）。
        """
        out: set[str] = set()

        def _add(p: Any) -> None:
            s = str(p or "").strip().replace("\\", "/")
            if s:
                out.add(s)

        for p in task.get("target_files") or []:
            _add(p)
        ids = {str(task.get("id") or "").strip()}
        ids.update(str(d).strip() for d in (task.get("depends_on") or []))
        for t in all_tasks or []:
            if isinstance(t, dict) and str(t.get("id") or "").strip() in ids:
                for p in t.get("target_files") or []:
                    _add(p)
        contracts = task.get("contracts") if isinstance(task.get("contracts"), dict) else {}
        for v in [*list(contracts.get("uses") or []), *list(contracts.get("exposes") or [])]:
            # 冒号也要算分隔符：契约常写成 `snake.py:move` / `food.py::generate`
            for tok in re.split(r"[\s,;:，、：（）()]+", str(v)):
                if tok.endswith(".py"):
                    _add(tok)
        return out

    @staticmethod
    def _task_drawing_is_thin(task: dict) -> bool:
        """这张施工图是不是**实质为空**（只有 id/title/文件清单，没有可施工的内容）。

        真机 `20260927-150931`：8B 架构师不填 `symbols`/`test_hint`/`contracts`
        （强制重做 2 次也填不出来），施工图里全是「（未声明）」。此时若再把整份方案
        也从 dev 输入里去掉，dev 就**几乎没有任何信息** —— 于是又退回瞎出 `modify` 补丁
        （`modify` 打在不存在的文件上 ⇒ unchecked ⇒ 一条都落不了盘）。

        所以：**施工图够厚就只给施工图，太薄就退回给整份方案**。宁可多给一点，
        也不能让 dev 在真空里施工。
        """
        if not isinstance(task, dict):
            return True
        if [s for s in (task.get("symbols") or []) if str(s).strip()]:
            return False
        if str(task.get("test_hint") or "").strip():
            return False
        contracts = task.get("contracts")
        if isinstance(contracts, dict) and contracts:
            return False
        return True

    def _task_focus(
        self, task: dict, *, rework_problems: list[str] | None = None, symbols: list[str] | None = None
    ) -> str:
        """本张施工图的**聚焦块**（主路径 / 拆半 / 补漏**三处共用一份**）。

        为什么要抽出来（真机 `20260928-095848` 的教训）：补漏重试此前直接引用
        `_dev_task_call` 里的**局部变量** `focus` —— 跨函数引用局部变量编译器不管，
        于是每次补漏都抛 `NameError`。异常被兜底吞掉（那是刻意设计：单张图失败不该
        拖垮整轮），日志里只留一行"补符号这次调用失败"，**而机制在每一张图上空转**
        （该运行 5/5 张全中，等于"补漏符号"这条自检完全没生效）。

        那次还留下第二个教训：这类错误 `ruff` 的 F821 一查就出（定位到行），
        但回归闸门里没跑 lint —— 所以现在 `check_lint` 是闸门的一部分。
        """
        view = {**task, "rework_problems": list(rework_problems or [])}
        if symbols is not None:
            view["symbols"] = list(symbols)
        # G3 文件创建租约：把本张施工图涉及文件的 owner 告诉提示词块
        # （owner 自己要交含文件头的完整文件；非 owner 只能在在制文件上定点增补）。
        view["file_owners"] = self._file_owner_map((self.state.get("plan") or {}).get("tasks") or [])
        # import 清单的**机械校对**：uses 成环（errors↔cli 那种顶层互导）与 verify
        # 已报缺失的导入契约，都不能再无条件写成"文件头必须"（真机 20260928-221831）。
        hint_ctx = tasktype.import_hint_context(
            self.state.get("plan"), self.state.get("verify_report")
        )
        focus = prompts.task_focus_block(
            view, (self.state.get("plan") or {}).get("changes"), hint_ctx
        )
        if symbols is not None:
            focus += (
                f"\n【本图已被**机械拆半**】本次只写这些符号：{'、'.join(symbols)} —— "
                "不要越出原图范围，其余符号由另一半负责。"
            )
        return focus

    def _dev_task_call(
        self,
        task: dict,
        tasks: list[dict],
        parts_fn: Any,
        plan_pin: Any,
        *,
        rework_problems: list[str] | None = None,
        symbols: list[str] | None = None,
        suffix: str = "",
    ) -> dict:
        """按一张（可能被**机械拆半**的）施工图做一次 dev 调用。

        抽出来是为了让**主路径与拆半路径共用同一份组装** —— 两处各写一份，迟早漂移，
        而"同一处逻辑两份实现"正是本项目反复踩的那类坑。
        `symbols` 非空时表示这是拆半后的子图：只写这几个符号，并在施工图里写明。
        """
        tid = str(task.get("id") or "?")
        focus = self._task_focus(task, rework_problems=rework_problems, symbols=symbols)
        # 只喂这张图相关的文件：接口摘要 / 退化点名 / 当前正文都按它裁
        paths = self._task_paths(task, tasks)
        thin = self._task_drawing_is_thin(task)
        parts = parts_fn(
            include_plan=thin, only_paths=paths, current_code=self._current_code_text(paths)
        )
        return self._grounded_call(
            "dev",
            # 施工图放最前：`fit_prompt` 从末尾开始丢片段，而它是本次调用的**全部权威**。
            # include_plan：施工图**太薄**时（架构师填不出 symbols/test_hint/contracts
            # 是常态，不是例外）必须退回给整份方案 —— 否则 dev 在真空里施工，
            # 又退回瞎出 `modify` 补丁（真机 20260927-150931）。够厚才只给施工图。
            [focus, *parts] if focus else parts_fn(include_plan=thin),
            pin=plan_pin,
            note=f"dev·{tid}{suffix}",
            # **别占 dev 的阶段槽位**：续跑时 `_restore` 会用「dev 阶段最后一次快照」
            # 覆盖 `state["implementation"]`（那是**累积实现**），而逐张施工图的产物
            # 只是**这一次调用**的输出 —— 覆盖的后果是累积实现被缩成"最后一张图"，
            # 上一轮 add 出来的文件整份消失，下一轮对它们发 modify 一律判
            # 「目标文件不存在」（真机 20260927-134222 实测：17 条累积 → 4 条）。
            # 给它独立 artifact_stage，文件名与埋点仍留档，但不再冒充 dev 的阶段产物。
            artifact_stage=f"dev-{tid}{suffix}",
        )

    def _dev_split_retry(
        self,
        task: dict,
        exc: Exception,
        tasks: list[dict],
        parts_fn: Any,
        plan_pin: Any,
        rework_problems: list[str] | None = None,
    ) -> dict:
        """单张图因**输出截断**失败时，把它的符号**对半拆**逐半重试；成功则返回合并结果。

        为什么由机制做：`OllamaError` 的原文就在说"让产物更短（拆分任务、减少条目）"，
        但那句话之前只是**写给人看的**——结果就是这张图本轮直接没产出（真机
        `20260928-000351`：T-04 / T-02 / T-05 各失败一次，整轮少了几个文件）。
        拆半是纯机械操作：不需要模型判断，两半都仍在原图的符号范围内（不越界），
        而且**先诊断再拆**——只有截断才拆（重复循环拆了也会再陷）。
        """
        if "截断" not in f"{type(exc).__name__}: {exc}":
            return {}
        symbols = [str(s).strip() for s in (task.get("symbols") or []) if str(s).strip()]
        if len(symbols) < 2:
            return {}
        halves = [symbols[: len(symbols) // 2], symbols[len(symbols) // 2 :]]
        self.log(
            f"        [按 task 分派] {task.get('id')} 输出被截断 → **机械拆半**重试（"
            + " | ".join("、".join(h) for h in halves)
            + "）"
        )
        out: dict = {}
        for half in halves:
            try:
                data = self._dev_task_call(
                    task,
                    tasks,
                    parts_fn,
                    plan_pin,
                    rework_problems=rework_problems,
                    symbols=half,
                    suffix="-split",
                )
            except Exception as exc2:  # noqa: BLE001
                self.log(
                    f"        [按 task 分派] {task.get('id')} 拆半后仍失败"
                    f"（{type(exc2).__name__}：{str(exc2)[:160]}）"
                )
                continue
            out = self._merge_dev(out, data) if out else data
        return out

    def _dev_by_tasks(self, parts_fn: Any, plan_pin: Any, tasks: list[dict]) -> dict:
        """一次 dev 调用只做一张施工图，逐张产出并累积合并。

        每张做完立刻做一次**机械自检**（声明的符号都写出来了没），漏了就只补这一张 ——
        这是「分 task 完成测试、审核后不用到人工」里那个"测试"的落点：它不产生人工闸门。
        """
        merged: dict = {}
        # G3/G4：非 mock 路径启用在制工作区（每图累积物化 + py_compile 快检）与文件创建租约。
        # mock 测流程不测内容（占位数据每条都过不了这些内容判据），基线不动。
        if not isinstance(self.client, MockClient):
            self._wip_init()
        owner_map = self._file_owner_map(tasks)
        # **按施工图归因**（返工轮才有内容）：这张图上一轮到底错了什么。
        # 诉求是"返工要指明哪个 task 的具体什么问题"，而映射本来就是机械可得的
        # （补丁自带 `covers_tasks`、施工图自带 `target_files`），不该让模型或人工去对。
        by_task = tasktype.bug_report_from_state(
            self.state, list(getattr(self, "fixes", None) or []), plan=self.state.get("plan")
        ).get("by_task") or {}
        for idx, task in enumerate(tasks, 1):
            tid = str(task.get("id") or f"#{idx}")
            rework = (by_task.get(tid) or {}).get("problems") or []
            # 单张图的调用失败**不能拖垮整轮**：7B 偶发进入重复生成被 ollama 中止
            # （真机 20260927-060300：`prediction aborted, token repeat limit reached`），
            # 异常冒泡出去会直接终结整个 run，前面几张图的产出全部作废、state 还停在
            # running 变成"隐身运行"。这里吞掉、记一笔，其余施工图照常施工。
            try:
                data = self._dev_task_call(task, tasks, parts_fn, plan_pin, rework_problems=rework)
            except Exception as exc:  # noqa: BLE001
                # **截断 ⇒ 自动拆半重试**：`OllamaError` 的原文就在说"让产物更短
                # （拆分任务、减少条目）"，但那是**写给人看的建议** —— 结果就是这张图
                # 本轮干脆没产出（真机 20260928-000351：T-04 / T-02 / T-05 各失败一次）。
                # 拆半是纯机械操作，不需要模型判断，且两半都仍在原图的符号范围内（不越界）。
                split = self._dev_split_retry(task, exc, tasks, parts_fn, plan_pin, rework)
                if split:
                    # 拆半产物同样过任务事务点（租约/物化/语法），不能绕过。
                    split = self._task_txn_checkpoint(
                        task, tasks, parts_fn, plan_pin, merged, split, owner_map, rework
                    )
                    merged = self._merge_dev(merged, split) if merged else split
                    self.log(f"        [按 task 分派] {tid} 拆半后完成（{idx}/{len(tasks)}）")
                    continue
                self.log(
                    f"        [按 task 分派] {tid} 本次调用失败（{type(exc).__name__}："
                    f"{str(exc)[:300]}）→ 跳过这张，其余图继续施工"
                )
                self.state.setdefault("dev_task_failures", []).append(
                    {"task": tid, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                )
                continue
            # 跨轮「补符号零生效」记录：真机 20260928-221831 里同一批 gap（add_entry 等）
            # 在第 2/3 轮各补问一次（64s/48s/58s），补丁全部因 anchor 不匹配被裁剪，
            # prompt 没带任何新信息 —— 同条件重问必然再废一次，直接跳过、留痕升级。
            no_progress = self.state.setdefault("repair_no_progress", {})
            for retry in range(1, self.TASK_SYMBOL_RETRIES + 1):
                gaps = self._task_symbol_gaps(task, data)
                if not gaps:
                    break
                prev_np = no_progress.get(tid)
                if (
                    isinstance(prev_np, dict)
                    and int(prev_np.get("round") or 0) < self.attempt
                    # 本轮缺的符号**全部**在上轮的零生效记录里（补丁被裁或补问后一个没少）
                    and set(gaps)
                    and set(gaps) <= set(prev_np.get("gaps") or [])
                ):
                    self.log(
                        f"        [施工图自检] {tid} 同一批符号（{'、'.join(gaps[:3])}）"
                        "上一轮已补问过且零生效（补丁未落地）→ 不再同 prompt 空转，"
                        "留给评审/verify 升级处理"
                    )
                    self.state.setdefault("repair_skips", []).append(
                        {"task": tid, "round": self.attempt, "gaps": list(gaps),
                         "prev_round": int(prev_np.get("round") or 0)}
                    )
                    break
                self.log(
                    f"        [施工图自检] {tid} 漏了 {len(gaps)} 个声明过的符号"
                    f"（{'、'.join(gaps[:3])}）→ 只重做这一张"
                )
                try:
                    # 与主路径**同参数**：聚焦块走共用实现（此前这里引用 `_dev_task_call`
                    # 的局部变量 `focus` ⇒ 每次补漏都 `NameError`，机制在整个运行里空转）；
                    # `only_paths` / `current_code` 也按本图裁剪 —— 不传的话补漏调用会看到
                    # **全量代码**（与主路径不一致），容易顺手去改别的文件。
                    paths = self._task_paths(task, tasks)
                    again = self._grounded_call(
                        "dev",
                        [
                            self._task_focus(task, rework_problems=rework),
                            "【补漏】上一版漏掉了施工图里声明的这些符号："
                            f"{'、'.join(gaps)}。请补齐它们的**完整定义**，"
                            "不要改动其他文件、不要重写已完成的部分。",
                            *parts_fn(
                                include_plan=self._task_drawing_is_thin(task),
                                only_paths=paths,
                                current_code=self._current_code_text(paths),
                            ),
                        ],
                        pin=plan_pin,
                        note=f"dev·{tid}·补符号{retry}",
                        # 同上：补符号也是**逐张图的调用**，不占 dev 的阶段槽位
                        artifact_stage=f"dev-{tid}-repair",
                    )
                except Exception as exc:  # noqa: BLE001
                    # **补漏也要吞异常**：它与主调用同属"一次调用"，抛出去会直接终结整个
                    # 运行 —— 真机 20260927-150931 就是这里抛的 `OllamaError`（7B 输出撞上限后
                    # 陷入重复、止损抛错），整轮开发作废、进程退出、state 却还停在 running
                    # （页面上看是"还在跑"），白等 24 分钟才发现。保留这一版，漏的符号留给评审。
                    self.log(
                        f"        [施工图自检] {tid} 补符号这次调用失败（{type(exc).__name__}："
                        f"{str(exc)[:200]}）→ 保留这一版，其余图继续施工"
                    )
                    self.state.setdefault("dev_task_failures", []).append(
                        {"task": tid, "phase": "补符号",
                         "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                    )
                    break
                data = self._merge_dev(data, again)
                # 补完再数一遍：**一个都没少** ⇒ 这次补问零生效（典型：补丁 anchor 对不上
                # 被裁剪）。记下来，下一轮同批 gap 直接不再补问，避免跨轮空烧。
                left = self._task_symbol_gaps(task, data)
                if set(left) >= set(gaps):
                    no_progress[tid] = {"round": self.attempt, "gaps": list(gaps)}
                gaps = left
            # G3/G4 **任务事务点**：文件租约执法 → 在制工作区累积物化 → py_compile 快检
            # （失败带错误重问一次，残留留痕升级）。mock 在该方法内部直接降级为 no-op。
            data = self._task_txn_checkpoint(
                task, tasks, parts_fn, plan_pin, merged, data, owner_map, rework
            )
            merged = self._merge_dev(merged, data) if merged else data
            self.log(f"        [按 task 分派] {tid} 完成（{idx}/{len(tasks)}）")
        return merged

    # ------------------------------------------------------------ 覆盖核对（两处共用）
    @staticmethod
    def _clean_task_id(value: Any) -> str:
        """任务 id 规范化：剥掉模型常顺手包上的引号 / 反引号 / 空白。

        真机 20260927-015956：架构师把 depends_on 写成 JSON 字符串里又套一层引号
        （读出来是带引号的 "T-06"），与 tasks[].id 的 T-06 比不上 —— 审计报
        「depends_on 引用了不存在的任务 id」，dev 照着这条判定 T-01~T-05
        「does not exist」，整轮只吐了 ui.py，其余全部写进 not_implemented。
        一处引号污染级联成整轮返工，必须在入口剥掉。
        """
        s = str(value or "").strip()
        for _ in range(3):  # 可能套多层
            stripped = s.strip("\"'`").strip()
            if stripped == s:
                break
            s = stripped
        return s

    @staticmethod
    def _plan_task_coverage(plan: Any, impl: Any) -> dict:
        """方案任务 vs 实现产物的确定性核对 —— **任务覆盖判定的唯一真源**。

        抽成一个函数是因为两处都要它、且结论必须一致：
          · :meth:`_audit_implementation` —— 落进 state 供评审/人工看，**只记日志**
          · :meth:`_missing_plan_files`   —— dev 自检项，漏文件要**触发原地重问**

        为什么必须有第二处：契约只要求 ``edits`` 非空（``minItems: 1``），
        ``_audit_implementation`` 查的又是**任务**有没有被声明覆盖 —— 两者都拦不住
        「把整个文件忘掉」。真机 run 20260925-184300 丢过 ``game_logic.py``；
        2026-09-26 的模型对照实验里，候选模型 3 次有 2 次只交了硬约束要求的 5 个文件中的
        1 个，契约与审计全程放行，一路走到 verify。
        """
        plan = plan if isinstance(plan, dict) else {}
        impl = impl if isinstance(impl, dict) else {}
        tasks = [t for t in (plan.get("tasks") or []) if isinstance(t, dict) and t.get("id")]
        task_ids = [Orchestrator._clean_task_id(t.get("id")) for t in tasks]
        # target_files 为空的任务（「集成所有模块」/「验收测试」/「打包」这类元任务）
        # 没有可落盘的文件，文件级补丁天然覆盖不到它；算进 missing 就是一个永远修不完
        # 的返工项（真机 20260927-015956：T-08/T-09/T-10 的 target_files 全空）。
        no_file_tasks = {
            Orchestrator._clean_task_id(t.get("id"))
            for t in tasks
            if not [str(p).strip() for p in (t.get("target_files") or []) if str(p).strip()]
        }
        covered: list[str] = []
        unknown: list[str] = []
        edits = [e for e in (impl.get("edits") or []) if isinstance(e, dict)]
        patch_rows: list[dict] = []  # 注意别叫 patches：会和模块名 patches 撞
        for edit in edits:
            patch_rows.append(
                {
                    "path": str(edit.get("path") or ""),
                    "symbol": str(edit.get("target_symbol") or ""),
                    "chars": len(str(edit.get("patch") or "")),
                }
            )
            for task in edit.get("covers_tasks") or []:
                task = Orchestrator._clean_task_id(task)
                if task not in task_ids:
                    unknown.append(task)
                elif task not in covered:
                    covered.append(task)
        declared = [
            f"{item.get('task')}：{item.get('reason')}"
            for item in (impl.get("not_implemented") or [])
            if isinstance(item, dict)
        ]
        declared_ids = {Orchestrator._clean_task_id(item.get("task")) for item in (impl.get("not_implemented") or []) if isinstance(item, dict)}
        missing = [t for t in task_ids if t not in covered and t not in declared_ids and t not in no_file_tasks]
        return {
            "tasks": tasks,
            "task_ids": task_ids,
            "covered": covered,
            "unknown": unknown,
            "edits": edits,
            "patch_rows": patch_rows,
            "declared": declared,
            "declared_ids": declared_ids,
            "missing": missing,
        }

    def _missing_plan_files(self, impl: Any) -> list[str]:
        """方案点名要产出、本轮却**一条补丁都没有**的文件。

        判定刻意收紧到「该文件的任务里有**既没被 covers_tasks 覆盖、也没被声明未实现**的」
        才报 —— 免得把「任务实现了、只是换了个文件写」这类正常偏离误判成漏文件
        （那种情况下任务是 covered 的，这里不会触发）。

        返回的是**直接喂给 dev 重问的问题描述**，所以带上任务号，让它知道该补什么。
        """
        cov = self._plan_task_coverage(self.state.get("plan"), impl)
        if not cov["task_ids"]:
            return []
        # 只在 dev「大体按方案干活」时才报。**一个任务都没覆盖**说明是整体没按方案来
        # （covers_tasks 一个都没对上方案里的任务 id），那是 ``plan_task_uncovered`` /
        # ``implementation_empty`` 的活 —— 它们报的是更强的问题，且已经会驱动评审与返工。
        # 这种情况下逐文件再报一遍既没有新信息，又会把 dev 的重问预算烧掉两轮（mock 夹具
        # 就是这种形态：方案是按 schema 合成的占位，dev 的 covers_tasks 是对不上的占位值）。
        # 我们要抓的是**漏产出**：dev 明明在实现方案，却把某个文件整个忘了。
        if not cov["covered"]:
            return []
        def _n(p: Any) -> str:
            """路径归一：只统一分隔符，**不做 resolve** —— 方案与产出可能一边绝对一边相对，
            resolve 会依赖 cwd，反而不确定。"""
            return str(p or "").replace("\\", "/").strip()

        missing_tasks = set(cov["missing"])
        produced = [_n(e.get("path")) for e in cov["edits"] if str(e.get("path") or "").strip()]

        def _hit(target: str) -> bool:
            """方案里写绝对路径、产出里写相对路径（或反过来）都要算命中 —— 二开场景
            两边口径本来就常常不一致（真机 20260925-222624 的 target_files 是绝对路径，
            而 dev 的 edits.path 是相对路径）。"""
            key = _n(target)
            if not key:
                return True
            return any(
                key == p or key.endswith("/" + p) or p.endswith("/" + key) for p in produced
            )

        out: list[str] = []
        seen: set[str] = set()
        for task in cov["tasks"]:
            tid = str(task.get("id"))
            if tid not in missing_tasks:
                continue
            for raw in task.get("target_files") or []:
                target = str(raw or "").strip()
                if not target or _hit(target) or target in seen:
                    continue
                seen.add(target)
                out.append(
                    f"方案要求的文件 `{_n(target)}` 没有任何补丁产出"
                    f"（任务 {tid} 既没被 covers_tasks 覆盖，也没写进 not_implemented）"
                )
        return out

    @staticmethod
    def _paths_in_text(items: Any) -> set[str]:
        """从问题文本里抽出被点到的文件名（问题清单是「允许改动的范围」的唯一依据）。"""
        out: set[str] = set()
        for item in items or []:
            for m in re.finditer(
                r"[\w./\\-]+\.(?:py|js|ts|tsx|jsx|java|go|rs|html|css|json|md)", str(item)
            ):
                out.add(m.group(0).replace("\\", "/"))
        return out

    @staticmethod
    def _out_of_scope_edits(prev: Any, new: Any, problems: Any) -> list[str]:
        """这一版改动碰到了**问题清单没点到**的文件（返回给调用方记录）。

        「锁基准、定范围、最小改」靠提示词叮嘱是不够的 —— 提示词里第一次生成那套口径
        还在（"按方案实现…"），模型很容易顺手改别的。这里用 diff 机械举证：
        改动了哪些 (文件, 符号)，其中几个不在问题清单点到的文件里。

        **刻意只记告警，不判负**：一个问题的正解有时确实要动别的文件（`ui.py` 的报错
        根因可能在 `game_logic.py`）。先观察一轮真实比例，再决定要不要升级成打回。
        """
        mentioned = Orchestrator._paths_in_text(problems)
        if not mentioned:
            return []

        def _map(impl: Any) -> dict[tuple[str, str], str]:
            out: dict[tuple[str, str], str] = {}
            for edit in (impl or {}).get("edits") or []:
                if not isinstance(edit, dict):
                    continue
                key = (
                    str(edit.get("path") or "").replace("\\", "/"),
                    str(edit.get("target_symbol") or ""),
                )
                out[key] = str(edit.get("patch") or "")
            return out

        before, after = _map(prev), _map(new)
        changed = [k for k, v in after.items() if before.get(k) != v]
        out: list[str] = []
        for path, symbol in sorted(changed):
            base = path.rsplit("/", 1)[-1]
            hit = any(
                path == m or base == m or path.endswith("/" + m) or m.endswith("/" + base)
                for m in mentioned
            )
            if not hit:
                out.append(f"{path}（符号 {symbol or '-'}）")
        return out

    def _audit_implementation(self) -> dict:
        """确定性核对：方案的每个 task 是否被补丁覆盖 / 被显式声明未实现。

        这是「开发谎报完成」的机械防线：模型不可能通过措辞绕开任务清单核对。
        """
        plan = self.state.get("plan") or {}
        impl = self.state.get("implementation") or {}
        cov = self._plan_task_coverage(plan, impl)
        task_ids = cov["task_ids"]
        covered = cov["covered"]
        unknown = cov["unknown"]
        edits = cov["edits"]
        patch_rows = cov["patch_rows"]
        declared = cov["declared"]
        declared_ids = cov["declared_ids"]
        missing = cov["missing"]
        # 反向漏洞（真机发现，2026-09-23）：被评审压紧后，dev 会退化成"把任务全部声明为未实现"，
        # 这样 missing 为空、补丁校验也干净，等于什么都没做却全绿。这里显式识别"零实现"。
        # 判定只看补丁本身有没有实质内容。这里曾额外要求「任务不在 declared_ids 里」，但
        # not_implemented[].task 是自由文本（模型既填任务 id 也填散文描述），两边产物一旦
        # 自相矛盾就会把全部真实补丁误判成未实现，进而让评审错判 rework —— 不要再依赖它。
        real_edits = [row for row in patch_rows if row["chars"] >= 40]
        # 返工退化检测：上一轮声明过的符号本轮不见了，且 `not_implemented` / `deviations` 里
        # 一个字都没提 —— 这就是"越改越少"。声明过就不算（可能是刻意的删除）。
        prev_symbols = {str(x) for x in (self.state.get("implementation_symbols_prev") or [])}
        cur_symbols = self._symbol_set(impl)
        declared_text = " ".join(declared) + " " + " ".join(
            str(x) for x in (impl.get("deviations") or []) if str(x).strip()
        )
        vanished = sorted(
            key
            for key in prev_symbols - cur_symbols
            if key.rsplit("::", 1)[-1] and key.rsplit("::", 1)[-1] not in declared_text
        )
        edit_paths = [str(e.get("path") or "") for e in edits]
        langs = sorted(self._languages_of(edit_paths))
        dup_stems = self._duplicate_stems_across_langs(edit_paths)
        stack_problems: list[str] = []
        if dup_stems:
            stack_problems.append("同一文件名在不同语言里各实现了一份：" + "、".join(dup_stems[:6]))
        # 「主干不同」的多语言同样是发散：真机 run 20260925-123726 产出了 main.py 与
        # game.js/snake.js/…（主干全不同，只按同名判会漏掉）。新建项目本就该单一栈；
        # 二次开发尊重存量可能多语言的现实，只在上面的「同名跨语言」时才算问题。
        if self.project_type == "new" and len(langs) > 1:
            stack_problems.append("新建项目同时产出了多种主语言：" + "、".join(langs))
        return {
            "task_ids": task_ids,
            "covered": covered,
            "missing": missing,
            "vanished_symbols": vanished,
            "empty_implementation": bool(task_ids) and not real_edits,
            "real_edit_count": len(real_edits),
            "declared_not_implemented": declared,
            "declared_ids": sorted(declared_ids),
            "unknown_tasks": sorted(set(unknown)),
            "patches": patch_rows,
            # 技术栈一致性：本次产出的语言集合，以及发散问题（同名跨语言 / 新建项目多语言）
            "languages": langs,
            "mixed_stacks": dup_stems,
            "stack_problems": stack_problems,
        }

    @staticmethod
    def _language_of(path: str) -> str | None:
        """主程序文件的语言族（配置/文档类后缀返回 None）。"""
        ext = Path(str(path)).suffix.lower()
        lang = _LANG_SUFFIXES.get(ext)
        if not lang:
            return None
        return _LANG_FAMILY.get(lang, lang)

    def _languages_of(self, paths: list[str]) -> set[str]:
        return {lang for lang in (self._language_of(p) for p in paths) if lang}

    def _duplicate_stems_across_langs(self, paths: list[str]) -> list[str]:
        """同一文件名主干出现在**不同语言**里 —— 几乎肯定是重复实现（技术栈发散）。

        真机 run 20260925-123726：交付物里同时产出 `snake.py` 与 `snake.js`、
        `main.py` 与 `game.js`，然后测试阶段声明 `node game.js` —— 一个「最小贪吃蛇」
        被实现成了两套。这类发散靠提示词说不清，得由机制点出来。

        判据刻意收窄到「同名主干跨语言」：合法的前后端混用（backend/*.py + web/*.js）
        主干不同，不会被误报。
        """
        by_stem: dict[str, set[str]] = {}
        for path in paths:
            lang = self._language_of(path)
            if not lang:
                continue
            by_stem.setdefault(Path(str(path)).stem.lower(), set()).add(lang)
        return sorted(stem for stem, langs in by_stem.items() if len(langs) > 1)

    def _audit_patches(self) -> dict:
        """机械核对：每条补丁的 anchor 能否在原文定位、声明的语义是否自洽（不依赖模型自觉）。"""
        return patches.analyze_all(self.repo, self.state.get("implementation"))

    def _patch_blockers(self) -> list[str]:
        """阻断级补丁问题：这类补丁无论如何都不该被判 pass。"""
        audit = self.state.get("patch_audit") or self._audit_patches()
        out: list[str] = []
        impl_audit = self.state.get("implementation_audit") or self._audit_implementation()
        if impl_audit.get("empty_implementation"):
            out.append("实现为空：只有「未实现」声明，没有任何覆盖任务的补丁")
        if impl_audit.get("vanished_symbols"):
            out.append(
                "返工让上一轮的这些符号消失了却没有声明："
                + "、".join(impl_audit["vanished_symbols"][:6])
                + "（要么恢复它们，要么写进 not_implemented / deviations 说明理由）"
            )
        for item in impl_audit.get("stack_problems") or []:
            out.append(
                f"技术栈发散：{item} —— 本次交付必须**单一技术栈**，"
                "请删掉多余语言的那一份，并让入口命令与保留语言一致"
            )
        for row in audit.get("edits") or []:
            status = str(row.get("status") or "")
            if status in (
                "patch_incomplete",
                "patch_span_mismatch",
                "anchor_not_found",
                # 同一个新文件里重复定义同一符号：合并后会得到两份定义，必然起不来
                "new_file_duplicate_symbol",
                # 新增文件内容语法就不对（写残/未闭合）：整份写入后必然跑不起来。
                # 标成阻断级是为了让评审**不能给它 pass**（真机 run 20260924-185507 里
                # 这份写残的 renderer.py 一路被判 ok，直到 verify 才炸）。
                "new_file_syntax_error",
            ):
                name = row.get("symbol") or row.get("path")
                # 带上第一条说明：它常常是**定位信息**（如"第 2 行：字符串 ' 未闭合"），
                # 只给状态名的话，拿到返工项的一方不知道该改哪儿。
                detail = "；".join(str(x) for x in (row.get("notes") or [])[:1])
                # **点名施工图**：这句会进评审与 dev 的缺陷单。"哪个 task 出了什么问题"
                # 是机械可得的（补丁自带 covers_tasks），那就必须写出来 ——
                # 否则读的人只能自己去做"文件 ↔ 施工图"的映射，返工项等于无主。
                tids = [str(t) for t in (row.get("tasks") or []) if str(t).strip()]
                out.append(
                    (f"［施工图 {'、'.join(tids[:2])}］" if tids else "")
                    + f"{name}：{patches.STATUS_CN.get(status, status)}"
                    + (f"（{detail}）" if detail else "")
                )
        return out

    def _patch_failures(self) -> list[dict]:
        """判负补丁的**结构化投影**：Failure Analyzer 责任主体三分类的输入（建议⑨）。

        `_patch_blockers` 产出的是给人/模型读的句子；这里保留两个机械事实：
          · ``status`` 原始状态码（unchecked / symbol_not_found / anchor_not_found…）；
          · 目标符号/文件**是否在方案里被点名**（symbol_planned / file_planned）。

        classify 据此区分：开发补丁内容写错（dev_patch）／方案或施工图给的靶子是虚的
        （compiler_target）／物化链或仓库快照故障（patch_runtime），
        避免基础设施 bug 被整批甩给 DEV 重写补丁。
        """
        audit = self.state.get("patch_audit") or self._audit_patches()
        plan = self.state.get("plan") if isinstance(self.state.get("plan"), dict) else {}
        target_files: set[str] = set()
        planned: set[str] = set()
        for task in (plan.get("tasks") or []):
            if not isinstance(task, dict):
                continue
            for p in (task.get("target_files") or []):
                p = str(p or "").replace("\\", "/").strip()
                if p:
                    target_files.add(p)
            planned.update(str(s or "").strip() for s in (task.get("symbols") or []) if str(s).strip())
        for change in (plan.get("changes") or []):
            if isinstance(change, dict):
                planned.update(str(s or "").strip() for s in (change.get("symbols") or []) if str(s).strip())

        def _symbol_planned(symbol: str) -> bool:
            s = symbol.strip()
            if not s:
                return False
            return any(
                s == p or s.endswith("." + p) or p.endswith("." + s)
                for p in planned
            )

        out: list[dict] = []
        for row in audit.get("edits") or []:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "")
            if not status or status == "ok":
                continue
            path = str(row.get("path") or "").replace("\\", "/").strip()
            out.append(
                {
                    "status": status,
                    "path": path,
                    "symbol": str(row.get("symbol") or ""),
                    "notes": list(row.get("notes") or []),
                    # 二开里方案覆盖了某文件、仓库快照却没有它，才是"靶子虚"；
                    # 新建项目本来就没有任何已存文件，modify 缺失只能算开发 add/modify 选错。
                    "symbol_planned": _symbol_planned(str(row.get("symbol") or "")),
                    "file_planned": self.project_type != "new" and path in target_files,
                }
            )
        return out

    def _verify_blockers(self) -> list[str]:
        """运行验证失败 = 机械证据表明交付跑不起来，同样属于阻断级。

        **例外**：失败若经 verify 归因判定**全部来自测试层**（命令自身不可执行 / 拿不到
        可运行证据 —— 见 ``verify_report.impl_fail`` / ``test_defects``），就不算实现级阻断。
        开发无权修改测试命令，逼它返工只会把**本来正确**的实现改坏：真机 20260927-073518
        里评审据此要求「在 snake.py 中补充 width/height 参数」，而实现本来就正确地要求了它们。
        这类问题改由测试阶段的自检重问修（见 ``_stage_test`` / ``_test_command_problems``）。
        """
        report = self.state.get("verify_report")
        if not isinstance(report, dict) or report.get("verdict") != "fail":
            return []
        if report.get("impl_fail") is False and report.get("test_defects"):
            self.log(
                "  [归因] 运行验证失败经判定**全部属测试层缺陷**（命令自身不可执行/无可用运行证据）"
                "→ 不计为实现级阻断，交给测试阶段修命令"
            )
            return []
        problems = [str(p).strip() for p in (report.get("problems") or []) if str(p).strip()]
        return [f"运行验证失败：{item}" for item in problems] or ["运行验证失败：沙箱里执行的命令没有通过"]

    #: 返工项里出现这些字样，就认为它在要求「弄一个能跑的入口」。
    #: 只在 `_plan_missing_entry()` 成立时才用它 —— 有那个强前提兜着，误判代价可控
    #: （就算判重了，结果也只是「回去把入口补进方案」，方向没错）。
    _ENTRY_FIX_HINTS = (
        "入口", "main.py", "__main__", "main 函数", "启动脚本", "可执行", "run.py", "app.py", "cli.py",
    )

    def _looks_like_entry_fix(self, text: str) -> bool:
        low = str(text).lower()
        return any(hint in low for hint in self._ENTRY_FIX_HINTS)

    @staticmethod
    def _looks_like_test_file(path: str) -> bool:
        """`test_*.py` / `*_test.py`（测试阶段产出的文件，**不属于方案的 changes 范围**）。

        为什么要排除：缺陷指向测试文件 ≠ "方案漏规划了一个产品文件"。真机 `192001` 的
        `affected` 里就混着 `test_all.py` / `test_command.py` —— 不排除就会因为"方案里没有
        测试文件"把整个方案打回，那是纯误判。
        """
        name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
        return name.startswith("test_") or name.endswith("_test.py")

    def _plan_uncovered_defects(self) -> list[str]:
        """缺陷指向、但方案里**没有任何 task 覆盖**的文件（方案层漏项）。

        为什么必须由机制判：开发受方案白名单约束（只改 `changes` / `target_files` 里列出的
        文件），方案没规划的文件**它无权创建**。于是"评审要求加 / 开发做不到"两边都没错，
        循环却不收敛 —— 与 `_plan_missing_entry` 是同一条道理，而那条已经用真机验证有效
        （`20260925-045404` 卡 3 轮就是靠它破的）。

        还治另一件事：返工的任务此前只能来自 `plan.tasks`，这些"不属于任何施工图"的缺陷
        **没有任务可派** ⇒ `_tasks_for_bugfix` 返回空 ⇒ 调度落到整批重做，最小改动丢失。
        """
        # mock 下不生效：mock 的返工项是按 schema 合成的占位文本，会指向不存在的文件，
        # 把「流程机制」的测试整个带偏（与覆盖审计 / `_orphan_modify_files` 同一条约定）。
        if isinstance(self.client, MockClient):
            return []
        report = tasktype.bug_report_from_state(
            self.state, list(getattr(self, "fixes", None) or []), plan=self.state.get("plan")
        )
        gaps = tasktype.plan_gap_files(report.get("items") or [], self.state.get("plan"))
        return [f for f in gaps if not self._looks_like_test_file(f)]

    def _verify_missing_entry(self) -> bool:
        """运行验证是否判了「没有可执行入口」。"""
        report = self.state.get("verify_report") or {}
        return any("没有可执行入口" in str(p) for p in (report.get("problems") or []))

    def _rule_findings(
        self,
        impl: Any = None,
        *,
        work: str | Path | None = None,
        written: list[str] | None = None,
        reset: bool = False,
    ) -> list[dict[str, Any]]:
        """按规则库（pipeline/rules.json）扫描「将要写入的新代码」，结论进 state。

        两个调用点各有分工：dev 结束时扫**补丁正文**（落盘前拦截，`reset=True`），
        verify 之后扫**沙箱真文件**（编码合法性、`.env` 是否进忽略清单 —— 补丁正文里
        看不出来）。同一轮里两处结果按 ``scan`` 标签合并：**同类覆盖、异类保留**。

        ⚠ **绝不能跨轮累积**（真机踩到）：第 1 轮的 3 条 `debug_residue` 在第 2 轮代码
        已经改掉之后仍挂在 state 里（旧行号、旧原文），于是下一轮评审继续拿它当返工理由
        —— 这正是我们花大力气要断的"陈旧锚点"自指循环，只不过这次是靠机制自己喂进去的。
        所以 dev 阶段**每轮重置**，verify 阶段只在同轮的 editsfinding 之上补 treefinding。
        """
        edits = [e for e in ((impl if impl is not None else self.state.get("implementation")) or {}).get("edits") or []
                 if isinstance(e, dict)]
        found = rules.scan_edits(edits, entry_paths=self._entry_path_candidates())
        for item in found:
            item.setdefault("scan", "edits")
        if work is not None:
            tree = rules.scan_tree(work, written)
            for item in tree:
                item["scan"] = "tree"
            found.extend(tree)
        kinds = {str(item.get("scan") or "edits") for item in found}
        base = [] if reset else [
            f for f in (self.state.get("rule_findings") or [])
            if isinstance(f, dict) and str(f.get("scan") or "edits") not in kinds
        ]
        merged: dict[tuple, dict[str, Any]] = {}
        for item in [*base, *found]:
            if isinstance(item, dict):
                key = (
                    item.get("rule"), item.get("path"), item.get("line"), item.get("note") or "",
                )
                merged[key] = item
        self.state["rule_findings"] = list(merged.values())
        # 规则库自身的加载问题（正则非法、缺反例判据被降级…）也要留痕：
        # 规则写坏了不能静默变成"检查通过"。
        notes = rules.load_notes()
        if notes:
            self.state["rule_load_notes"] = notes
        blk = rules.blockers(self.state["rule_findings"])
        wrn = rules.warns(self.state["rule_findings"])
        sig = (len(blk), len(wrn))
        if (blk or wrn) and sig != getattr(self, "_rule_log_sig", None):
            self._rule_log_sig = sig
            self.log(f"        [红线] 阻断 {len(blk)} 条 / 提示 {len(wrn)} 条")
            for line in rules.summarize([*blk, *wrn], limit=4):
                self.log(f"          - {line}")
        return self.state["rule_findings"]

    def _entry_path_candidates(self) -> set[str]:
        """项目自己的**入口脚本**路径（供规则豁免用，典型是 `debug_residue`）。

        为什么需要它：入口脚本的 `print`/`console.log` 是**用户可见输出**，不是调试残留；
        但入口文件名是**项目自定义**的 —— 真机校准 20260926-212611 的入口叫 `tempconv.py`，
        固定名单（main/cli/index…）抓不到，于是 3 条 `print` 全被误报成"调试残留"。

        最可靠的机械信号是**需求/方案里点名的可执行命令**：需求写了
        ``python tempconv.py <数值> <方向>``，那个文件就是入口。取不到就是空集，
        此时固定名单（写在规则 JSON 的 exclude_paths_regex 里）照旧生效。
        """
        blob = "\n".join(
            [str(self.requirement or "")]
            + [
                str(c.get("approach") or "")
                for c in (self.state.get("plan") or {}).get("changes") or []
                if isinstance(c, dict)
            ]
            + [
                str(t.get("acceptance") or "")
                for t in (self.state.get("plan") or {}).get("tasks") or []
                if isinstance(t, dict)
            ]
        )
        out: set[str] = set()
        for raw in re.findall(
            r"(?:python[23]?|node)\s+([\w./\\-]+\.(?:py|js|ts|mjs|cjs))", blob, re.I
        ):
            rel = str(raw).replace("\\", "/").lstrip("./")
            if not rel:
                continue
            out.add(rel)
            # 需求常写裸文件名（`tempconv.py`），补丁里却可能带目录（`src/tempconv.py`）——
            # 两种写法都进候选，免得因为路径写法差异漏掉豁免
            out.add(rel.rsplit("/", 1)[-1])
        return out

    def _rule_blockers(self) -> list[str]:
        """规则库判负的阻断项（每条都带定位、命中原文和反例出口，开发据此可改可申诉）。"""
        return rules.blocker_fixes(self.state.get("rule_findings") or [])

    def _mechanical_blockers(self) -> list[str]:
        """机制判定的阻断项 = 补丁机械问题 + 运行验证失败 + 测试漏测 + 工程红线。

        四类都不依赖模型自觉：只要机器能证明「贴不回去」「跑不起来」「改的东西一个没测」
        或「触了工程红线」，即便评审给了 pass 也要改判 rework_dev。
        """
        return [
            *self._patch_blockers(),
            *self._verify_blockers(),
            *self._test_blockers(),
            *self._rule_blockers(),
            *self._bugfix_scope_blockers(),
            *self._contract_blockers(),
        ]

    def _contract_blockers(self) -> list[str]:
        """跨文件契约不符（聚合验证）：方案声明的接口与产物对不上。

        与 dev 阶段的"施工图符号自检"分工：那个查**本 task 该定义的符号写没写**，
        这个查**跨文件调用链对不对得上** —— 后者是"分 task 各自施工"最容易碎的地方，
        因为每张图只看自己的文件，谁都不看全局。
        """
        return [str(x) for x in (self.state.get("contract_problems") or []) if str(x).strip()]

    def _bugfix_scope_blockers(self) -> list[str]:
        """BUG 修复模式的**范围判据**：越界 / 漏改 / 已存在文件被整份重吐。

        这三条是"dev 能不能只做定向修复"的机械保证 —— 光靠提示词说"最小改动"，
        7B 模型仍会顺手重写一遍；这里让**范围由机器给定并机械校验**，不依赖模型自觉。
        """
        if self.round_kind != tasktype.BUGFIX:
            return []
        report = self.state.get("bug_report") or {}
        edits = (self.state.get("implementation") or {}).get("edits") or []
        return tasktype.scope_violations(
            edits,
            tasktype.allowed_scope(report),
            list(report.get("existing_paths") or []),
        )

    #: 「主张代码本身跑不起来 / 贴不回去」的说法。用**模式**而不是精确子串：真机上的说法
    #: 常带修饰词 —— 20260925-184300 第 3 轮的「模块 game_logic 缺少核心符号 Snake，导致
    #: 依赖文件**无法正常运行**」，精确子串 "无法运行" 匹配不到它（中间多了「正常」二字），
    #: 回放历史数据时就是这么发现漏词的（见 §25 的校准小节）。
    #:
    #: 设计 / 兼容性 / 可维护性之类**不在机械证据的射程内，不参与**证伪剔除 ——
    #: 宁可让一条陈旧的阻断项多留一轮，也不能把真问题按"已证伪"抹掉。
    _MECHANICAL_CLAIM_PATTERNS = (
        r"语法|缩进|未闭合|f-?string|syntax\s*error",
        r"解析(失败|不了|错误|异常)|编译(失败|不过|错误)",
        r"无法[\w]{0,6}运行|运行不起来|跑不[\w]{0,4}起来|不能运行",
        r"启动(失败|不了|不起来)|无法[\w]{0,4}启动",
        r"导入[\w]{0,4}(失败|报错|错误|异常)|import[\w]{0,6}(error|失败)|import\s*error|module\s*not\s*found",
        r"缺[\w]{0,6}(少|失)[\w]{0,10}(模块|文件|符号|函数|类|方法|依赖|入口)",
        r"文件不存在|模块不存在|找不到[\w]{0,6}(模块|文件|符号)|符号[\w]{0,6}(缺失|找不到|不存在)",
        r"未定义|undefined|name\s*error|attribute\s*error|type\s*error",
    )
    #: 编译一次（每次评审都要用，别在热路径上重复 join）
    _MECHANICAL_CLAIM_RE = re.compile(
        "|".join(f"(?:{pat})" for pat in _MECHANICAL_CLAIM_PATTERNS), re.I
    )

    def _mechanical_evidence_clean(self) -> tuple[bool, str]:
        """本轮机械证据是否「干净到足以推翻上一轮的失败主张」。

        判据刻意保守：**只有全部机械检查都没问题**才算干净。任何一处有疑点都返回 False——
        这里判错的代价是不对称的：多留一条陈旧阻断项只是多花一轮返工，而把真问题删掉
        会让缺陷直接流到人工/交付。
        """
        report = self.state.get("verify_report")
        if not isinstance(report, dict):
            return False, "本轮没有运行验证结果"
        if report.get("verdict") == "fail":
            return False, "本轮运行验证判了失败"
        if [p for p in (report.get("problems") or []) if str(p).strip()]:
            return False, "本轮运行验证仍有问题项"
        bad_cmds = [
            c for c in (report.get("commands") or [])
            if str(c.get("status")) not in ("ok", "skipped")
        ]
        if bad_cmds:
            return False, "本轮仍有命令未通过"
        audit = self.state.get("patch_audit") or {}
        bad_rows = [
            r for r in (audit.get("edits") or [])
            if str(r.get("status") or "") in (
                "patch_incomplete", "patch_span_mismatch", "anchor_not_found",
                "new_file_syntax_error", "new_file_duplicate_symbol",
            )
        ]
        if bad_rows:
            return False, "本轮补丁机械校验仍有阻断项"
        if self.state.get("semantic_audit", {}).get("errors"):
            return False, "本轮仍有语义（类型级）错误"
        return True, "本轮机械证据全绿"

    def _refute_stale_blockers(self, blockers: list[Any]) -> tuple[list[str], list[str]]:
        """剔除**已被本轮机械证据证伪**的旧阻断项，返回 (保留, 剔除)。

        为什么必须做（真机 run 20260924-185507 复盘）：`_step_review` 会把评审给的
        `blockers` 原样拼进 `self.fixes`，而 `prompts.parts_review` 又用「上一轮已提出的
        修复项（检查是否真的解决了）」把这份 fixes 喂回去 ⇒ 评审拿旧 blocker 当锚点照抄，
        于是「verify 5 条命令全绿、mechanical_blockers=0」的那两轮仍然写着
        「renderer.py 未闭合 f-string」—— 与机械证据直接矛盾，永远 rework_dev、永不收敛。

        现在改成：主张「跑不起来/语法错/导入失败」的旧阻断项，若本轮机械证据全绿，
        就不往下传（并在 state 里留痕 `refuted_blockers` 供复盘）。
        """
        clean, why = self._mechanical_evidence_clean()
        kept: list[str] = []
        dropped: list[str] = []
        for item in blockers or []:
            text = str(item or "").strip()
            if not text:
                continue
            claim = bool(self._MECHANICAL_CLAIM_RE.search(text))
            if clean and claim:
                dropped.append(text)
            else:
                kept.append(text)
        if dropped:
            self.state.setdefault("refuted_blockers", [])
            self.state["refuted_blockers"].extend(dropped)
            self.log(
                f"  [证伪] 本轮机械证据全绿（{why}）→ 剔除 {len(dropped)} 条上一轮遗留的"
                "「跑不起来/语法错」类阻断项，不再喂回评审（避免自指循环）"
            )
            for line in dropped[:2]:
                self.log(f"        - {line[:90]}")
        return kept, dropped

    @staticmethod
    def _as_risk(entry: Any) -> dict | None:
        """把残留风险条目统一成 {issue, reason, impact}。

        两个来源必须同形：模型给的（契约改版后是对象，但可能仍回退成裸字符串）
        与编排器追加的（needs_external 类返工项）。不统一的话 residual_risks 会变成
        dict 与 str 混杂，下游格式化时直接崩。
        """
        if isinstance(entry, dict):
            issue = str(entry.get("issue") or "").strip()
            if not issue:
                return None
            return {
                "issue": issue,
                "reason": str(entry.get("reason") or "").strip(),
                "impact": str(entry.get("impact") or "").strip(),
            }
        text = str(entry or "").strip()
        return {"issue": text, "reason": "", "impact": ""} if text else None

    def _mechanical_review_facts(self) -> dict:
        """评审三层 · **第一层机械事实**（建议⑫）：纯代码汇集，一次取值、处处同源。

        第二层语义 LLM 的 pin 块（补丁审计 / 运行验证 / 测试审计 / 影响面 / 红线 /
        逐项验收 / 语义审计）与第三层确定性裁决（:func:`diagnose.review_decision`）
        必须看同一份事实——历史上这些取值散在多处，改一处漏一处是「机制空转」的常见成因。
        本函数只读 state / 跑既有纯机械核对，不调用模型。
        """
        verify_report = self.state.get("verify_report")
        verify_report = verify_report if isinstance(verify_report, dict) else {}
        clean, clean_reason = self._mechanical_evidence_clean()
        return {
            # 阻断项总表（补丁 / 运行验证 / 测试漏测 / 工程红线 / bugfix 范围 / 契约）
            "blockers": self._mechanical_blockers(),
            "patch_audit": self.state.get("patch_audit") or {},
            "implementation_audit": self.state.get("implementation_audit") or {},
            "test_audit": self.state.get("test_audit") or {},
            "verify_verdict": str(verify_report.get("verdict") or ""),
            "verify_problems": [
                str(p) for p in (verify_report.get("problems") or []) if str(p).strip()
            ],
            "rule_findings": self.state.get("rule_findings") or [],
            "contract_problems": [
                str(p) for p in (self.state.get("contract_problems") or []) if str(p).strip()
            ],
            # 逐项验收（本轮每条上一轮修复项的机械核对结果）
            "defect_verdicts": self.state.get("defect_verdicts") or [],
            # 影响面：谁在调用本轮被改的符号
            "impact_audit": verify_report.get("impact_audit") or {},
            "semantic_audit": self.state.get("semantic_audit") or {},
            # 方案层机械信号
            "plan_gap_files": self._plan_uncovered_defects(),
            "missing_entry": self._plan_missing_entry(),
            "evidence_clean": clean,
            "evidence_clean_reason": clean_reason,
        }

    def _persist_ontology(self, graph: ontology.OntologyGraph, stage: str) -> None:
        """把当前语义图存为只追加的新版本（runstore 只存储，不裁决）。

        ``only`` 模式 / 无 run_dir 时安全跳过。版本描述符登记进 state，供 Transition
        Record 与回放回答「这轮裁决基于哪一版语义图」。
        """
        if self.run_dir is None or self.mode == "only":
            return
        try:
            descriptor = runstore.write_ontology(self.run_dir, graph.to_dict(), stage=stage)
        except OSError as exc:
            self.log(f"  [Ontology] 语义图版本落盘失败（不影响裁决）：{type(exc).__name__}")
            return
        self.state["ontology_revision"] = descriptor
        history = self.state.setdefault("ontology_revisions", [])
        history.append(descriptor)

    def _proof_gate(self, *, blockers: list[str], plan_gap: bool,
                    contract_problems: list[str]) -> dict:
        """规格§二十/§四十五：构建 Requirement→Claim→PO 语义图并做机械三态裁决。

        纯读 state / 纯函数（ontology + ontology_validate），不调用模型。
        mock 运行不进本闸门（mock 只计划不执行，skipped 是其固有形态）。
        """
        scope = self.state.get("scope")
        scope = scope if isinstance(scope, dict) else {}
        # 以 Plan IR 的语义投影为底（Requirement/Claim/PO/Constraint/Invariant），
        # 再叠加编译后 Task —— 与 P0-2 保持**同一张图、同一真源**，不另起炉灶。
        ir = self.state.get("compiler_ir")
        base = ir.get("ontology") if isinstance(ir, dict) else None
        graph = ontology.OntologyGraph.from_dict(base) if isinstance(base, dict) else ontology.OntologyGraph()
        if graph.get("req:root") is None:
            ontology.build_requirement_projection(
                graph, scope, original_requirement=self.requirement or ""
            )
        plan = self.state.get("plan")
        tasks = (plan.get("tasks") or []) if isinstance(plan, dict) else []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            tid = str(task.get("semantic_task_id") or task.get("id") or "")
            if not tid or graph.get(tid) is not None:
                continue
            graph.add(ontology.SemanticObject(
                id=tid, type=ontology.TYPE_TASK, truth=ontology.TRUTH_DERIVED,
                payload={
                    "task_id": str(task.get("id") or ""),
                    "title": str(task.get("title") or ""),
                    "target_files": list(task.get("target_files") or []),
                    "symbols": list(task.get("symbols") or []),
                    "task_revision": task.get("task_revision") or 1,
                    "supersedes": list(task.get("supersedes") or []),
                },
                provenance=[ontology.Provenance(source="taskcompiler", stage="compile_plan")],
            ))
            for req_id in (task.get("implements_requirements") or []):
                if graph.get(str(req_id)) is not None:
                    graph.relate(tid, "implements", str(req_id), truth=ontology.TRUTH_DERIVED)
            for po_id in (task.get("proof_obligations") or []):
                if po_id in graph.obligations:
                    graph.relate(tid, "carries_obligation", str(po_id), truth=ontology.TRUTH_DERIVED)
        # 方案§三十/§三十一：Artifact 溯源链接入语义图（envelope 已在 _record 算好，
        # 这里只投影不重算 hash）：intake→pm→plan→dev→verify 的 derived_from 与版本 supersedes。
        art_count = ontology.project_artifact_chain(
            graph, self.state.get("artifact_log") or []
        )
        if art_count["artifacts"]:
            self.log(
                f"  [Ontology] Artifact 链：对象 {art_count['artifacts']} / "
                f"derived_from 边 {art_count['derived_from']} / supersedes 边 {art_count['supersedes']}"
            )
        # 方案§三十二/§三十三：Intake/PM 同级事实矛盾 → Claim 投影，由 contradictions
        # 校验器复用 reconcile_claims 硬阻断（PM 闸门之外的 Release 兜底闭环）。
        n_conflict_claims = ontology.project_conflict_claims(graph, self._scope_conflict_groups())
        if n_conflict_claims:
            self.log(f"  [Ontology] 需求矛盾 Claim 投影：{n_conflict_claims} 条（同级对立将阻断放行）")
        # 方案§十九~§二十四：Task→Patch→Symbol→WorkspaceRevision 真实父子链投影。
        # 必须在 verify 评估之前：verify revision 要挂到在制链头之下（证据只认链头）。
        chain = ontology.project_workspace_chain(
            graph, self.state.get("task_transactions") or [],
            at=str(self.attempt or ""),
        )
        if chain.get("revisions"):
            self.log(
                f"  [Ontology] 工作区链：revision +{chain['revisions']} / "
                f"patch +{chain['patches']} / symbol +{chain['symbols']}"
            )
        verify_report = self.state.get("verify_report")
        verify_report = verify_report if isinstance(verify_report, dict) else {}
        ontology.evaluate_against_verify(
            graph, verify_report, contract_problems=contract_problems,
            skeleton_conformance=self.state.get("skeleton_conformance"),
            contract_checked=bool(self.state.get("contract_checked")),
            parent_revision=str(chain.get("head_revision") or ""),
            at=str(self.attempt or ""),
        )
        no_power = list(
            ((verify_report.get("negative_control") or {}) or {}).get("no_power") or []
        )
        verdict = str(verify_report.get("verdict") or "skipped")
        proof = ontology.release_proof_status(
            semantic_pass=True,  # 问的是「机械证据是否齐备」，与语义建议解耦
            mechanical_blockers=blockers,
            verify_verdict=verdict,
            obligations=list(graph.obligations.values()),
            workspace_verified=verdict == "pass",
            negative_control_no_power=no_power,
            unresolved_contracts=[],  # 契约问题已在 blockers 中，避免双重计数
            plan_gap=plan_gap,
        )
        # 语义图自身校验：结构化结果（方案§十四）+ 旧版字符串留痕。
        # 方案§十二/§十三：任何 severity=error 的语义完整性问题都必须硬阻断 PASS ——
        # 以前 problems 只展示不拦，现在统一在这里把 Proof Gate 翻成 FAILED。
        problems_by_check = ontology_validate.validate_all_structured(graph)
        integrity = ontology_validate.semantic_integrity_audit(graph, self.state)
        problems_by_check["semantic_integrity"] = integrity
        gate_errors = ontology_validate.blocking_errors(problems_by_check)
        self.state["ontology"] = graph.to_dict()
        self.state["ontology_problems"] = {
            check: [p.message for p in items] for check, items in problems_by_check.items()
        }
        self.state["ontology_problems_structured"] = [
            p.to_dict() for items in problems_by_check.values() for p in items
        ]
        if gate_errors:
            proof["status"] = "FAILED"
            proof["can_pass"] = False
            proof["code"] = "ontology_integrity_error"
            proof["failed"] = list(proof.get("failed") or []) + [
                f"ontology_integrity[{p.source}/{p.code}]：{p.message}" for p in gate_errors
            ]
            proof["ontology_error_count"] = len(gate_errors)
        self.state["proof_gate"] = proof
        self._persist_ontology(graph, "review")
        if proof["status"] != "PROVEN":
            self.log(
                f"  [ProofGate] status={proof['status']} can_pass={proof['can_pass']}"
                f"（缺证 {len(proof['mandatory_missing'])} / 失败 {len(proof['failed'])}"
                f" / 语义 error {len(gate_errors)}）"
            )
        return proof

    def _project_failure_ontology(self, record: dict, led: dict) -> None:
        """规格§二十四/二十六/二十七：开缺陷 + 归因 + 恢复策略投影到**同一张**语义图。

        overlay 纪律：只追加、不裁决、不阻断路由（任何异常仅记日志）；mock 无图可投时
        直接返回。Defect 的责任 Task 由 taskcompiler.resolve_bug_task 机械解析，
        禁止按 T-id 猜；verify/traceback 类缺陷同时关联本轮 FAILED 的 PO。
        """
        try:
            base = self.state.get("ontology")
            if not isinstance(base, dict):
                return
            graph = ontology.OntologyGraph.from_dict(base)
            plan = self.state.get("plan")
            tasks = (plan.get("tasks") or []) if isinstance(plan, dict) else []
            rows = [r for r in (self.state.get("defect_verdicts") or []) if isinstance(r, dict)]
            rows_by_key = {diagnose.defect_key(r): r for r in rows}
            failed_po_ids = [
                po.id for po in graph.obligations.values()
                if po.status == ontology.PO_STATUS_FAILED
            ]
            defect_ids: list[str] = []
            resolutions: dict[str, dict] = {}
            for key in sorted(str(k) for k in (led.get("open") or {}).keys()):
                row = rows_by_key.get(key) or {}
                mapping = taskcompiler.resolve_bug_task(row, tasks)
                resolutions[key] = mapping
                kind = str(row.get("kind") or "")
                source = str(row.get("source") or "")
                po_ids = failed_po_ids if (
                    kind.startswith("traceback") or kind == "verify" or source == "verify"
                ) else []
                did = ontology.project_defect(
                    graph, key=key, row=row,
                    task_id=str(mapping.get("semantic_task_id") or ""),
                    po_ids=po_ids,
                )
                defect_ids.append(did)
            # 解析留痕：matched_by/candidates 供 issues 派生视图与人工审计（不改变路由）
            self.state["defect_task_resolution"] = resolutions

            failure = record.get("failure") if isinstance(record.get("failure"), dict) else {}
            ftype = str(failure.get("type") or "")
            if ftype:
                fid = ontology.failure_id(self.attempt, ftype)
                rid = "rec:" + ontology.stable_hash(
                    ontology.canonical_json([fid, int(self.attempt or 0)]), length=12
                )
                ontology.project_recovery(
                    graph, rid=rid,
                    recovery=record.get("recovery") or {},
                    round_no=int(self.attempt or 0),
                )
                ontology.project_failure(
                    graph, fid=fid, failure=failure,
                    round_no=int(self.attempt or 0),
                    defect_ids=defect_ids, recovery_id=rid,
                )
                self.state["recovery_trace"] = ontology.trace_recovery_owner(graph, fid)
                self.state["ontology_failure_id"] = fid

            self.state["ontology"] = graph.to_dict()
            _problems = ontology_validate.validate_all_structured(graph)
            _problems["semantic_integrity"] = ontology_validate.semantic_integrity_audit(
                graph, self.state
            )
            self.state["ontology_problems"] = {
                check: [p.message for p in items] for check, items in _problems.items()
            }
            self.state["ontology_problems_structured"] = [
                p.to_dict() for items in _problems.values() for p in items
            ]
            self._persist_ontology(graph, "recovery")
        except Exception as exc:  # noqa: BLE001 —— overlay 永不阻断主路由
            self.log(f"  [Ontology] Failure 投影失败（不影响裁决）：{type(exc).__name__}: {exc}")

    def _project_release_decision(self, proof: dict, verdict: str, reason: str) -> None:
        """方案§二十六~§二十八：can_release 唯一放行裁决 → Decision 一等对象投影。

        overlay 纪律同 :meth:`_project_failure_ontology`：只追加、不裁决路由（路由已由
        diagnose.review_decision 定），任何异常仅记日志。pass 时 Decision ``based_on``
        支撑 required PO 的 PROVEN 证据、``applies_to`` 经验证的链头 revision、
        ``resolves`` 图上仍 OPEN 的 Defect；rework 时只挂 FAILED 证据留痕。
        mock 运行无图（proof=None），调用方直接跳过。
        """
        try:
            base = self.state.get("ontology")
            if not isinstance(base, dict):
                return
            graph = ontology.OntologyGraph.from_dict(base)
            gate = ontology.can_release(
                proof_status=proof, review_verdict=verdict, graph=graph,
            )
            if gate["can_pass"]:
                basis = ontology.release_basis(graph)
                evidence_ids = basis["evidence_ids"]
                defect_ids = sorted(
                    o.id for o in graph.objects.values()
                    if o.type == ontology.TYPE_DEFECT and str(o.status or "OPEN") == "OPEN"
                )
            else:
                required_po = {po.id for po in graph.obligations.values() if po.required}
                evidence_ids = sorted({
                    ev.id for ev in graph.evidence.values()
                    if ev.status == ontology.PO_STATUS_FAILED and (
                        not required_po
                        or {str(x) for x in (ev.proof_obligation_ids or [])} & required_po
                    )
                })
                defect_ids = []
            decision_id = ontology.project_decision(
                graph, verdict=gate["verdict"], reason=reason,
                revision=gate["verified_revision"], evidence_ids=evidence_ids,
                defect_ids=defect_ids, round_no=self.attempt, at=str(self.attempt or ""),
            )
            self.state["ontology"] = graph.to_dict()
            self.state["release_gate"] = dict(gate, decision_id=decision_id)
            # Decision 落图后复跑全量校验留痕（pass 无 based_on 的 warning 应于此清除）。
            problems = ontology_validate.validate_all_structured(graph)
            problems["semantic_integrity"] = ontology_validate.semantic_integrity_audit(
                graph, self.state
            )
            self.state["ontology_problems"] = {
                check: [p.message for p in items] for check, items in problems.items()
            }
            self.state["ontology_problems_structured"] = [
                p.to_dict() for items in problems.values() for p in items
            ]
            self._persist_ontology(graph, "decision")
            if not gate["can_pass"]:
                self.log(
                    "  [ReleaseGate] 机器不可放行："
                    + "；".join(str(x) for x in gate["blocking_reasons"][:3])
                )
        except Exception as exc:  # noqa: BLE001 —— overlay 永不阻断主路由
            self.log(f"  [Ontology] Decision 投影失败（不影响裁决）：{type(exc).__name__}: {exc}")

    def _normalize_review(self, review: dict) -> tuple[list[str], list[str], list[str], bool]:
        """把评审的返工项按作用域分三档；全是 needs_external 时强制放行（防止无意义空转）。

        返回 ``(in_material, architect, needs_external, forced_pass)``。

        `architect` 档 = 「方案层才能改」的返工项，调用方据此把下一轮**回退到 architect_plan**。
        不分这一档的话，方案漏规划文件的根因会被当成实现层返工，而开发被约束在方案的
        changes 范围内根本改不动它，只会白烧一轮（真机 run 20260924-185507）。

        注意：机制兜底（补丁校验推翻 pass）**无论模型有没有给返工明细都要执行** ——
        早期版本在"明细为空"时提前 return，等于给了模型一个「返回空明细就能绕过机制」的口子。
        """
        detail = review.get("required_fixes_detail") or []
        residual = [r for r in (self._as_risk(x) for x in (review.get("residual_risks") or [])) if r]
        in_material: list[str] = []
        architect: list[str] = []
        external: list[str] = []
        if detail:
            for item in detail:
                if not isinstance(item, dict):
                    continue
                fix = str(item.get("fix") or "").strip()
                if not fix:
                    continue
                # 带上归属文件再交给开发：不带的话开发拿到的是一串无主的整改文字，
                # 只能靠猜，于是反复去改评审根本没抱怨的文件。
                owner = str(item.get("path") or "").strip()
                if owner:
                    fix = f"[文件 {owner}] {fix}"
                scope = str(item.get("scope") or "").strip()
                if scope == "in_material":
                    in_material.append(fix)
                elif scope == "architect":
                    architect.append(fix)
                else:
                    # 未知/缺失 scope 一律按「需外部确认」处理（保守：不触发返工）——
                    # 与旧契约行为一致，契约回退时不会凭空多出返工。
                    external.append(fix)
            for fix in external:
                residual.append(
                    {
                        "issue": fix,
                        "reason": "需运行系统、访问外部环境或人工确认才能定论",
                        "impact": "本轮材料内无法验证，需在真实环境确认后才能定",
                    }
                )
        else:
            # 兼容旧格式/未给明细：全部当作本轮可改
            in_material = list(review.get("required_fixes") or [])
        review["required_fixes"] = in_material
        review["architect_fixes"] = architect
        review["residual_risks"] = residual
        review["required_fixes_detail"] = detail

        # ==================================================== 第一层：机械事实（纯代码，不问 LLM）
        # 建议⑫：所有「机器能证明」的东西在此一处汇集；第二层（唯一一次语义 LLM）只回答
        # 机械证明不了的问题，第三层（diagnose.review_decision）拿这份事实做确定性裁决。
        facts = self._mechanical_review_facts()
        # 留痕：每层裁决实际看到的同一份机械事实（供回放 / 排障；逐轮覆盖即可）。
        self.state["mechanical_review_facts"] = facts
        blockers = facts["blockers"]
        semantic_verdict = str(review.get("verdict") or "")

        # 机械阻断项入实现层整改清单：旧口径是**仅当语义层判 pass 时**才补入（判 rework_dev 时
        # 评审自己的 blockers 已另走 blockers_kept 通道），保持该门槛不变；紧接着的入口/漏项
        # 迁移可能把指向方案层文件的条目一并捞走（顺序不可换）。
        if blockers and semantic_verdict == "pass":
            in_material = list(in_material) + [f"修复：{item}" for item in blockers]

        # ---------------------------------------------------------- 入口缺口的层级纠正
        # 方案没规划入口文件时，把「要求补入口」的返工项从**实现层提到方案层**。
        # 为什么必须由机制做：开发受方案白名单约束（提示词明写「只改 changes / target_files
        # 里列出的文件」）—— 入口文件不在方案里，它**无权创建**。于是评审要求加、开发做不到，
        # 两边都没错，循环却不收敛（真机 run 20260925-045404 卡了整整 3 轮）。
        # 新增一个文件属于**方案变更**，按作用域本该判 architect，模型却常误判成 in_material。
        # 放在阻断项入列**之后**：阻断项这时才被塞进 in_material，一并把入口那条捞出来归位。
        entry_hit = False
        if facts["missing_entry"]:
            moved = [x for x in in_material if self._looks_like_entry_fix(x)]
            if moved or self._verify_missing_entry():
                in_material = [x for x in in_material if x not in moved]
                architect = list(architect) + [
                    "方案缺少可执行入口：请在 changes 与 tasks 里**规划一个入口文件**"
                    "（main.py / __main__.py 等，带 `if __name__ == '__main__':` 且运行时有输出）。"
                    "方案不规划它，开发就无权创建，运行验证会一直判「没有可执行入口」。"
                ] + moved
                entry_hit = True
                self.log(
                    "  [机制] 方案没规划入口 + 返工项要求补入口 → 提到方案层"
                    f"（从实现层移出 {len(moved)} 项）"
                )

        # ---------------------------------------------------------- 方案漏项的层级纠正
        # 缺陷指向的**文件**方案里没有 ⇒ 判断依据见 `_plan_uncovered_defects`。
        # 这是"返工老是修不好"的一条根：返工的任务此前只能来自 `plan.tasks`
        # （影响面 ∩ target_files），于是"不属于任何施工图"的缺陷（跨文件集成、
        # 方案漏规划的文件）**没有任务可派** —— `_tasks_for_bugfix` 返回空，
        # 调度落到「两遍模式整批重做」，最小改动整个丢掉；而开发受方案白名单约束，
        # 就算派了它也**无权创建方案里没有的文件**。判 rework_dev 只会白烧一轮。
        gap_files = facts["plan_gap_files"]
        if gap_files:
            moved = [x for x in in_material if any(f in str(x) for f in gap_files)]
            in_material = [x for x in in_material if x not in moved]
            architect = list(architect) + [
                f"方案漏项：缺陷指向 {'、'.join(gap_files)}，但方案里**没有任何 task 覆盖**它们。"
                "请在 changes / tasks 里补上这些文件（开发受方案白名单约束，无权创建方案里没有的文件）——"
                "把它硬塞给开发只会白烧一轮。"
            ] + moved
            self.log(
                f"  [机制] 缺陷指向方案未规划的文件（{'、'.join(gap_files)}）"
                "→ 强制 rework_architect（判 dev 它也改不动）"
            )

        # ==================================================== 第三层：确定性裁决（纯函数单一真源）
        # verdict 只是语义层的**建议**；最终去向由机械事实 + 建议按 diagnose.review_decision
        # 的固定规则算出。规则不要散在编排器里（历史上散在 5 段 if 中，改动容易只改一半）。
        # Proof Gate（规格§二十）：mock 运行只计划不执行，skipped 是固有形态，不进闸门；
        # 真机运行必须凭机械证据 PROVEN 才能 pass —— verify skipped 时语义 pass 也翻回 rework_dev。
        proof = None
        if not isinstance(self.client, MockClient):
            proof = self._proof_gate(
                blockers=list(blockers), plan_gap=bool(gap_files),
                contract_problems=list(facts["contract_problems"]),
            )
        decision = diagnose.review_decision(
            semantic_verdict=semantic_verdict,
            blocked=bool(blockers),
            has_in_material=bool(in_material),
            has_architect_fixes=bool(architect),
            plan_gap=bool(gap_files),
            verify_pass=str(facts.get("verify_verdict") or "") == "pass",
            evidence_clean=bool(facts.get("evidence_clean")),
            proof=proof,
        )
        action = decision["action"]
        review["verdict"] = decision["verdict"]
        forced_pass = action in (diagnose.DECISION_PASS_EXTERNAL, diagnose.DECISION_PASS_RESIDUAL)
        review["forced_pass"] = forced_pass
        review["forced_rework"] = (
            decision["forced"] and decision["verdict"] in ("rework_dev", "rework_architect")
        )
        if action == diagnose.DECISION_AMBIGUOUS:
            # 判「方案本身有错」却把返工项全归为 needs_external：分类自相矛盾。
            # 方案错误是**本轮材料内可改**的，不能借外部确认放行；但 in_material 为空，
            # 架构师也拿不到具体指示 ⇒ 转人工裁决（保留 rework_architect，由 classify 路由）。
            review["escalated_ambiguous"] = True

        # ---- 按裁决动作补 reasons / 日志（文案与旧版内联裁决保持一致）----
        reasons = list(review.get("reasons") or [])
        if action == diagnose.DECISION_MECH_DEV:
            reasons.append(
                "机制判定：存在阻断级机械证据（" + "；".join(blockers) + "），不允许判定 pass"
            )
            self.log(f"  [机制] 有阻断级机械证据 → 强制 rework_dev（{len(blockers)} 项）")
        elif action == diagnose.DECISION_PLAN_ARCH:
            if gap_files:
                reasons.append(
                    "机制判定：缺陷指向方案未规划的文件（方案层漏项）→ 强制回流方案"
                )
            else:
                reasons.append(
                    f"机制判定：列出 {len(architect)} 条方案层返工项却判 pass（自相矛盾），强制回流方案"
                )
                self.log(
                    "  [机制] 列了方案层返工项却判 pass → 强制 rework_architect"
                    f"（{len(architect)} 项）"
                )
        elif action == diagnose.DECISION_AMBIGUOUS:
            reasons.append(
                "机制判定：判 rework_architect 但返工项全被归为 needs_external（分类自相矛盾），转人工裁决"
            )
            self.log("  [机制] 判 rework_architect 但返工项全需外部确认（分类矛盾）→ 转人工裁决，不放行")
        elif action == diagnose.DECISION_PASS_EXTERNAL:
            reasons.append(
                "机制判定：返工项全部属于 needs_external，本轮材料内无可执行修改，自动放行并转入残留风险"
            )
            self.log("  [机制] 返工项全部需外部确认 → 强制 pass，已转入 residual_risks")
        elif action == diagnose.DECISION_PASS_RESIDUAL:
            # 机器已证明「能跑、能导入、补丁都套上了」，评审仍判 rework ⇒ 它的返工项是
            # **质量主张**而非可机械核对的缺陷。降级为残留风险，放行到人工审核闸门由人拍板。
            for item in list(in_material):
                residual.append(
                    {"issue": str(item)[:200], "reason": "评审提出，但本轮机械证据全绿、运行验证通过",
                     "impact": "已降级为残留风险，交人工审核闸门裁定"}
                )
            reasons.append(
                "机制判定：机械证据全绿且运行验证通过，评审的实现层返工项降级为残留风险，"
                "放行到人工审核闸门由人工裁定（共 %d 项）" % len(in_material)
            )
            self.log(
                f"  [机制] 机械证据全绿 + verify pass → 评审的 {len(in_material)} 项实现层返工项"
                "降级为残留风险，强制 pass 进人工审核闸门"
            )
        elif action in (diagnose.DECISION_PROOF_UNPROVEN, diagnose.DECISION_PROOF_FAILED):
            # Proof Gate 翻回 rework_dev：给实现轮一条**可执行**的整改内容，
            # 否则空 in_material 会让返工无的放矢（真机 121404：verify skipped 后
            # 评审走完流程，缺陷直到交付才暴露）。
            if proof:
                bucket = (proof["failed"] if action == diagnose.DECISION_PROOF_FAILED
                          else proof["mandatory_missing"])
                head = "机械验证失败" if action == diagnose.DECISION_PROOF_FAILED else "缺少必需机械证据"
                in_material.append(
                    f"修复：{head}（UNPROVEN_REQUIRED_EVIDENCE）—— "
                    + "；".join(str(x) for x in bucket[:4])
                    + "。请补齐：物化工作区可运行、测试/入口命令真实执行且断言成立"
                      "（verify skipped / dev self_check / WAIVED 都不能算 PROVEN）"
                )
            reasons.append(decision["reason"])
            self.log(
                "  [ProofGate] 必需机械证据不成立 → 翻回 rework_dev"
                f"（action={action}）"
            )

        # 入口纠正本身不翻转 verdict，但必须留痕（旧版这条原因错嵌在漏项分支里，一并归位）。
        if entry_hit and action not in (diagnose.DECISION_PLAN_ARCH,):
            reasons.append(
                "机制判定：方案未规划可执行入口，而返工项要求补入口 —— 属于方案层变更，回流方案"
            )

        review["required_fixes"] = in_material
        review["architect_fixes"] = architect
        review["residual_risks"] = residual
        review["required_fixes_detail"] = detail
        review["reasons"] = reasons
        # Proof Gate 裁决随评审产物留痕（mock 运行为 None）。
        review["proof_status"] = proof
        if proof is not None:
            # 方案§二十六~§二十八：机器放行裁决（can_release）与 Decision 对象投影。
            # 路由仍以上方 diagnose 裁决为准；Decision 是该裁决在语义图上的一等留痕，
            # 同时把 PASS 条件收敛到 can_release 一处，供交付闸门与审计直接消费。
            decision_reason = "；".join(
                str(x) for x in ([decision["reason"]] + reasons[-3:]) if str(x).strip()
            )
            self._project_release_decision(
                proof, str(review.get("verdict") or ""), decision_reason
            )
        return in_material, architect, external, forced_pass

    # ------------------------------------------------------------------ 状态持久化
    def _snapshot(self) -> dict:
        return {
            "version": 2,
            "status": self.status,
            "mode": self.mode,
            "run_id": self.run_id,
            "requirement": self.requirement,
            "repo": str(self.repo) if self.repo else None,
            "project_type": self.project_type,
            "max_rework": self.max_rework,
            "review_every": self.review_every,
            "pause_after": sorted(self.pause_after),
            # 创建时（首次 run）的闸门设置：续跑时可以改成「跑到底」，pause_after 会被清空，
            # 页面就看不到这次运行当初到底勾了哪些闸门。这里留一份不可变的原始输入供展示/审计。
            "initial_pause_after": list(self._initial_pause_after),
            "cursor": self.cursor,
            "attempt": self.attempt,
            "last_review_attempt": self.last_review_attempt,
            "fixes": self.fixes,
            "needs_human": self.needs_human,
            "rounds": self.rounds,
            "human_feedback": self.human_feedback,
            "human_actions": self.human_actions,
            "intake_decisions": self.intake_decisions,
            "pm_decisions": self.pm_decisions,
            "grounding_warnings": self.grounding_warnings,
            "calls": self.calls,
            "seq": self._seq,
            "paused_after": self.paused_after,
            "elapsed_s": round(self.elapsed_s, 1),
            "model_switches": sum(1 for c in self.calls if c.get("switched")),
            # 运行模式跟着 run 走：mock 运行续跑时必须自动回到 MockClient，否则会误加载真实模型
            "mock": isinstance(self.client, MockClient),
            "mock_rework_first": int(getattr(self.client, "rework_first", 0) or 0),
            "pool": [
                {"path": e.path, "text": e.text, "score": e.score, "truncated": e.truncated} for e in self.pool
            ],
            "artifacts": self.state,
        }

    def _persist(self) -> None:
        if self.run_dir is None or self.mode == "only":
            return
        if self._presence is not None:
            # 阶段推进时刷一次在心标记：页面能显示「在跑哪个阶段」，
            # 也让心跳在长阶段里保持新鲜（心跳线程另有一份，这里是双保险）。
            self._presence.stage = self.cursor
        runstore.write_state(self.run_dir, self._snapshot())
        self._write_prd()
        # 规格§三十九：issues 是 ontology/state 的**派生视图**，每次持久化都即时重算覆盖 —
        # 不让「state 已 degraded/failure、issues.json 还停在上一版」。pause/end 另有全量。
        self._refresh_issues()

    def _refresh_issues(self) -> None:
        """即时重算并落盘 issues 派生视图（json/jsonl/md）。纯派生、重算即覆盖。

        任何失败都**不阻断**主流水线（issues 只是诊断展示层，不是语义真源，规格§三十八）。
        """
        if self.run_dir is None or self.mode == "only":
            return
        try:
            collected = issues_mod.collect_issues(self._snapshot(), self.run_id)
            issues_mod.write_issues(self.run_dir, self.run_id, collected)
        except (OSError, ValueError, TypeError) as exc:
            self.log(f"  [issues] 派生视图刷新失败（不影响运行）：{type(exc).__name__}")

    def _start_presence(self) -> None:
        """起在场标记（幂等）。写入失败不该影响流水线本身。"""
        if self.run_dir is None or self.mode == "only":
            return
        if self._presence is not None:
            self._presence.stage = self.cursor
            return
        try:
            self._presence = presence.Guard(self.run_dir, stage=self.cursor).start()
        except OSError as exc:  # noqa: BLE001
            self.log(f"  [在场标记] 写入失败（不影响本次运行）：{type(exc).__name__}: {exc}")

    def _stop_presence(self) -> None:
        """停心跳并清标记（幂等）。标记万一残留也不影响判定 —— 读取时会校验 pid。"""
        guard, self._presence = self._presence, None
        if guard is not None:
            guard.stop()

    def _write_prd(self) -> None:
        """把 PM 的结构化产物渲染成标准 PRD（渲染真源在 ``pipeline/prd.py``）。

        随 ``_persist`` 一起刷新：在操作页面改过 scope 的 assumed_answer 之后，prd.md
        会同步更新，人工调假设不必翻 JSON。

        例外：人工在页面上直接改写过 prd.md（留下 ``prd.human`` 标记）时**不再覆盖** ——
        否则人工一次改动就会被下一次持久化冲掉。需要回到产物驱动时，页面点「按产物重新生成」；
        那个按钮现在会**当场**重渲染，不再等下一次持久化 —— 运行暂停/结束后根本不会再持久化，
        旧实现于是等于一个永远不生效的空操作（真机反馈：人工裁决完，PRD 里一个字都没变）。
        """
        if self.run_dir is None:
            return
        prd.write(
            self.run_dir,
            self.requirement,
            self.state.get("scope") or {},
            run_id=self.run_id,
            repo=self.repo or "",
        )

    def _refresh_env(self) -> None:
        """刷新 env.json；若期间提示词/配置指纹变了，把这次变化也记下来（否则事后无法归因）。"""
        if self.run_dir is None or self.mode == "only":
            return
        path = self.run_dir / runstore.ENV_NAME
        current = issues_mod.env_snapshot(
            repo=str(self.repo) if self.repo else None,
            host=str(getattr(self.client, "host", "mock")),
        )
        previous = runstore.read_json_if_exists(path) or {}
        old_hash = (previous.get("fingerprint") or {}).get("pipeline_hash")
        new_hash = current["fingerprint"]["pipeline_hash"]
        if old_hash and old_hash != new_hash:
            previous.setdefault("fingerprint_changes", []).append(
                {"at": current["generated_at"], "from": old_hash, "to": new_hash}
            )
            self.log(f"== [记录] 期间流水线指纹变化：{old_hash} -> {new_hash}")
        merged = {**previous, **current}
        runstore.write_json(path, merged)

    def _restore(self, snap: dict) -> None:
        self.run_id = snap["run_id"]
        self.requirement = snap.get("requirement") or ""
        self.repo = Path(snap["repo"]) if snap.get("repo") else None
        # 续跑必须沿用原项目类型：换了套提示词会让前后阶段的上下文基线不一致
        self.project_type = snap.get("project_type") or "secondary"
        self.max_rework = snap.get("max_rework", self.max_rework)
        self.review_every = max(1, snap.get("review_every", self.review_every))
        self.pause_after = set(snap.get("pause_after") or ())
        # 续跑沿用原始闸门记录（不会被本次 resume 的 pause_after 覆盖）
        self._initial_pause_after = list(snap.get("initial_pause_after") or [])
        self.cursor = snap.get("cursor") or "done"
        self.attempt = snap.get("attempt", 0)
        self.last_review_attempt = snap.get("last_review_attempt", 0)
        self.fixes = snap.get("fixes")
        self.needs_human = snap.get("needs_human", False)
        self.rounds = snap.get("rounds") or []
        self.human_feedback = snap.get("human_feedback") or {}
        self.human_actions = snap.get("human_actions") or []
        self.intake_decisions = list(snap.get("intake_decisions") or [])
        self.pm_decisions = list(snap.get("pm_decisions") or [])
        self.grounding_warnings = snap.get("grounding_warnings") or []
        self.calls = snap.get("calls") or []
        self._seq = snap.get("seq", 0)
        self.elapsed_s = float(snap.get("elapsed_s") or 0.0)
        self.pool = [Excerpt(**item) for item in (snap.get("pool") or [])]
        self.state = dict(snap.get("artifacts") or {})
        # 人工编辑优先：阶段快照文件里的 artifact 覆盖 state.json 中的旧值。
        # **但「逐张施工图的调用产物」不算阶段产物**（见 `_dev_by_tasks` 的 artifact_stage）：
        # 拿它覆盖 `implementation` 会把**累积实现**缩成"最后一张图" —— 于是续跑之后
        # 越改越少：上一轮 add 出来的文件整份消失，本轮对它们发 modify 一律判
        # 「目标文件不存在」，沙箱里只剩一个 main.py，白烧一整轮（真机 20260927-134222
        # 实测：17 条累积 → 4 条）。老 run 的这类快照仍以 `dev` 为阶段名存在，
        # 所以这里按 `meta.note`（`dev·T-01` 这种）精确识别并跳过。
        assert self.run_dir is not None
        skipped_calls: list[str] = []
        for row in runstore.stage_snapshots(self.run_dir):
            stage = str(row.get("stage") or "")
            artifact = row.get("artifact")
            key = runstore.STAGE_STATE_KEY.get(stage)
            if not key or artifact is None:
                continue
            note = str((row.get("meta") or {}).get("note") or "")
            if stage == "dev" and note.startswith("dev·"):
                skipped_calls.append(str(row.get("file") or ""))
                continue
            self.state[key] = artifact
        if skipped_calls:
            self.log(
                f"== [恢复] 跳过 {len(skipped_calls)} 份「逐张施工图」的调用产物"
                f"（{', '.join(skipped_calls[-3:])}）—— 它们不是本轮累积实现，"
                "拿它覆盖 implementation 会把已产出的文件整份丢掉"
            )
        # 人工裁决**在读取时**再并一次（幂等）：裁决的真源是 state，产物只是载体。
        # 保存接口若因任何原因没并回（真机出过两次：子进程用新契约、服务端还是旧代码，
        # 结果一条都没并上），下游就会把**已经裁决过**的条目当成「还没定」再问一遍。
        self._merge_human_decisions()

    def _merge_human_decisions(self) -> None:
        """把 state 里的人工裁决并回产物（幂等）。见 prompts.apply_*_decisions 的说明。"""
        intake = self.state.get("intake")
        if isinstance(intake, dict):
            self.state["intake"] = prompts.apply_intake_decisions(intake, self.intake_decisions)
        scope = self.state.get("scope")
        if isinstance(scope, dict):
            # 同口径兜底：旧 run 的阶段快照可能是**归一前**写入的（post 钩子收口前），
            # 上面刚用它覆盖了 state —— 这里再归一次，技术问题/三列重复不能在续跑后复活。
            scope = prompts.normalize_pm_questions(scope)
            if self.pm_decisions:
                scope = prompts.apply_pm_decisions(scope, self.pm_decisions)
            self.state["scope"] = scope

    # ------------------------------------------------------------------ 评审频率
    def _review_due(self, attempt: int) -> tuple[bool, str]:
        """节奏判定（建议⑬ 的 cadence 半边）：返回 ``(是否到期, 原因码)``。

        首轮与末轮必评审；其余按 review_every 间隔。**事件强制**（defect_closed 等）
        不在本函数内，见 :meth:`_force_review_events` —— 总闸门
        ``cadence_due OR force_review_reason`` 在 `_step_review` 组合。
        """
        if attempt <= 0:
            return False, ""
        if attempt > self.max_rework:  # 末轮：要拿到真实判定而不是直接 needs_human
            return True, "cadence_last_chance"
        if attempt == 1:  # 首轮：早暴露问题
            return True, "cadence_first_round"
        if (attempt - self.last_review_attempt) >= self.review_every:
            return True, f"cadence_every_{self.review_every}"
        return False, ""

    def _plan_file_set(self) -> set[str]:
        """方案边界文件集合（changes.path ∪ tasks.target_files）。"""
        plan = self.state.get("plan") or {}
        out: set[str] = set()
        for change in (plan.get("changes") or []):
            if isinstance(change, dict) and str(change.get("path") or "").strip():
                out.add(str(change.get("path")))
        for task in (plan.get("tasks") or []):
            if isinstance(task, dict):
                out.update(str(p) for p in (task.get("target_files") or []) if str(p).strip())
        return out

    def _force_review_events(self) -> list[str]:
        """建议⑬ 的事件半边：节奏窗口外**必须补评**的五类事件（纯机械信号，不问 LLM）。

          · ``defect_closed``：上轮台账还开着的缺陷，本轮逐项验收转绿（典型：BUG 修复轮
            verify 绿 —— 不能因「今天不是 review 轮」就再空跑一次 dev）；
          · ``regression_recovered``：上轮已转绿这轮又红（回归出现），或**上轮已标回归**
            的缺陷本轮转绿（回归修复待确认）—— 两种状态翻转都值得立刻评审；
          · ``high_risk_task_changed``：被改符号存在**存量上游**调用方
            （impact_audit.callers 中 in_this_round=False），或本轮命中工程红线；
          · ``plan_boundary_changed``：方案 changes/target_files 集合与上次评审时不同；
          · ``human_feedback_resolved``：本轮 dev 消费了人工（打回）反馈。

        台账试算刻意用局部变量：`diagnose.ledger` 是纯函数，正式闭账仍只在评审轮的
        原位置发生一次，跳过窗口不改动台账（证据连续计数口径不变）。
        """
        events: list[str] = []
        rows = [r for r in (self.state.get("defect_verdicts") or []) if isinstance(r, dict)]
        prev = self.state.get("defect_ledger") or {}
        prev_open = {str(k): v for k, v in (prev.get("open") or {}).items() if isinstance(v, dict)}
        if rows:
            trial = diagnose.ledger(prev, rows=rows, round_no=self.attempt)
            green_keys = {
                diagnose.defect_key(r)
                for r in rows
                if str(r.get("status") or "") == "green"
            }
            if green_keys & set(prev_open):
                events.append("defect_closed")
            regressed_open = {k for k, v in prev_open.items() if v.get("regressed")}
            if trial.get("regressions") or (green_keys & regressed_open):
                events.append("regression_recovered")

        impact = (self.state.get("verify_report") or {}).get("impact_audit") or {}
        external_callers = [
            c for c in (impact.get("callers") or [])
            if isinstance(c, dict) and not c.get("in_this_round")
        ]
        if external_callers or self._rule_blockers():
            events.append("high_risk_task_changed")

        last_files = self.state.get("last_review_plan_files")
        if last_files is not None and set(last_files) != self._plan_file_set():
            events.append("plan_boundary_changed")

        if self.state.get("human_feedback_consumed_round") == self.attempt:
            events.append("human_feedback_resolved")
        return events

    def _record_transition(
        self,
        *,
        to: str,
        reason: str,
        defect_ids: list[str],
        evidence: list[str],
        due_reason: str = "",
    ) -> dict:
        """建议⑭：每次离开评审（回 dev / 回 architect / 进 human / done）落一条因果记录。

        字段（与优化建议 §十四 的结构对齐）：``from / to / reason / defect_ids / attempt /
        evidence / plan_version / task_version``，另带 verdict 与评审触发原因便于回放。
        存进 ``state["transitions"]``（随 state.json 持久化），回答「为什么从这里跳回去」
        不再需要翻日志。
        """
        ir = self.state.get("compiler_ir")
        # 规格§三十五：Transition Record 带上语义坐标 —— 基于哪一版语义图、哪个工作区
        # revision、哪些 PO/证据没过、恢复原因。语义图只在真机 Proof Gate 后存在，
        # 缺省一律安全留空（旧 run / mock 不受影响）。overlay 读取永不阻断路由。
        ont_desc = self.state.get("ontology_revision")
        ontology_version = str(ont_desc.get("revision_id") or "") if isinstance(ont_desc, dict) else ""
        workspace_revision = ""
        po_ids: list[str] = []
        ev_ids: list[str] = []
        try:
            ont_state = self.state.get("ontology")
            if isinstance(ont_state, dict):
                graph_now = ontology.OntologyGraph.from_dict(ont_state)
                workspace_revision = str(graph_now.head_revision() or "")
                for po in graph_now.obligations.values():
                    if po.required and po.status != ontology.PO_STATUS_PROVEN:
                        po_ids.append(po.id)
                        ev_ids.extend(e for e in po.evidence_ids if e)
        except (TypeError, ValueError, KeyError):
            pass
        rec = {
            "seq": len(self.state.get("transitions") or []) + 1,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "attempt": self.attempt,
            "from": "review",
            "to": to,
            "reason": str(reason or ""),
            "defect_ids": [str(k) for k in defect_ids][:20],
            "evidence": [str(e) for e in evidence if str(e).strip()][:20],
            "plan_version": planir.plan_digest(self.state.get("plan") or {}),
            "task_version": planir.tasks_digest(
                ir if isinstance(ir, dict) and ir.get("units") else (self.state.get("plan") or {})
            ),
            # Ontology 语义坐标（规格§三十五）：回放「为什么回 dev」能直接定位
            # defect → violates PO → failed evidence → recovery，而不只是一个 reason 字符串。
            "ontology_version": ontology_version,
            "workspace_revision": workspace_revision,
            "proof_obligation_ids": sorted(set(po_ids))[:20],
            "evidence_ids": sorted(set(ev_ids))[:20],
            "recovery_reason": str(reason or ""),
            "review_due_reason": due_reason,
        }
        self.state.setdefault("transitions", []).append(rec)
        self.log(
            f"  [transition] review -> {to}（{rec['reason']}；"
            f"缺陷 {len(rec['defect_ids'])} 条；plan={rec['plan_version']} "
            f"task={rec['task_version']}）"
        )
        return rec

    def _mark_next_round(self, kind: str) -> None:
        """标记**下一次 dev 轮**的任务类型（由路由决策点写入、`_begin_round` 消费）。

        为什么要"待定"而不是当场写 `state.round_kind`：判「回方案」时下一轮跑的是**架构师**
        而不是开发，若当场就把类型改成 plan_rework，方案轮的提示词查询会读到它
        （`architect_plan` 未定义这一档，靠回退侥幸正确 —— 侥幸不算设计）。
        放成待定，由 `_begin_round("dev")` 消费，语义才与"这一轮开发是什么任务"对齐。
        """
        self.state["pending_round_kind"] = kind

    def _take_pending_kind(self) -> str:
        kind = str(self.state.get("pending_round_kind") or "").strip()
        if kind:
            self.state["pending_round_kind"] = ""
        return kind

    def _begin_round(self, entry: str) -> str:
        self.attempt += 1
        if entry == "dev":
            # 落到 dev 轮才算定稿。`_run_architect_plan` 收尾也会推 dev ——
            # 那条路上的类型来自 `_step_review` 的标记（方案返工），不会被方案轮吃掉。
            self.state["round_kind"] = self._take_pending_kind() or tasktype.FEATURE
        route = "架构师方案 -> 开发 -> 测试" if entry == "architect_plan" else "开发 -> 测试"
        tail = " -> 评审" if self._review_due(self.attempt)[0] else "（本轮跳过评审）"
        if entry == "dev":
            tail += f"（本轮口径：{tasktype.round_kind_label(self.state.get('round_kind'))}）"
        self.log(f"== 迭代 {self.attempt}: {route}{tail}")
        return entry

    # ------------------------------------------------------------------ 单步推进
    def _step(self, stage: str) -> str:
        """推进一个游标：执行该阶段的处理函数，返回下一个游标。

        分发是表驱动的（``STEP_METHODS``）：新增阶段时漏写分支不再要等到真机跑到那个游标
        才炸，而是 ``flow.validate()`` 在启动期就报出来（见模块顶部 STEP_METHODS 的注释）。
        """
        # 阶段边界标记：页面按它把运行日志切分到流程图节点上（格式定义见 runstore.stage_marker，
        # 写入端/读取端共用一份，避免两边正则漂移）
        self.log(runstore.stage_marker(stage))
        method = STEP_METHODS.get(stage)
        if method is None:
            raise OrchestratorError(f"未知编排游标: {stage}")
        return getattr(self, method)()

    # --- 各游标的处理函数（由 STEP_METHODS 映射；只做「执行本阶段 + 返回下一游标」）---
    def _run_intake(self) -> str:
        self._stage_intake(self.requirement)
        # 补强闸门（high 重要度缺失要素）已统一到 _gate_after，这里只负责推进游标
        return flow.next_linear("intake")

    def _run_pm(self) -> str:
        self._stage_pm(self.requirement)
        return flow.next_linear("pm")

    def _run_retrieve(self) -> str:
        query = self.requirement
        if self.state.get("scope"):
            query += json.dumps(self.state["scope"], ensure_ascii=False)
        self.pool = self._build_pool(query)
        # 新建项目没有存量代码可评估：assess 阶段语义不成立，
        # 真机上它正是「编造不存在的目录/模块」的源头（空仓库里编出 pipeline/core/*、db/*，
        # 再被 plan 当既有事实承接）。直接进方案阶段，由 plan 承接 PRD 做全新架构设计。
        return "architect_plan" if self.project_type == "new" else "architect_assess"

    def _run_architect_assess(self) -> str:
        if self.project_type == "new":
            # 兜底：续跑时若游标停在 assess（老 run 或人工 --from），同样跳过
            self.log("  [新建项目] 无存量代码可评估，跳过 architect_assess")
            return flow.next_linear("architect_assess")
        self._stage_assess(self.requirement)
        return flow.next_linear("architect_assess")

    def _run_architect_plan(self) -> str:
        self._stage_plan(self.requirement, self.fixes)
        if self.state.get("design_gate_blocked"):
            # 真机 160609：闸门拦截前就 _begin_round —— attempt 自增、「迭代 1」日志已打，
            # 但开发一轮都没跑。轮次必须在**闸门放行真正进入 dev 时**才算开始：
            # 原地续跑放行走 _execute 顶部复核处补开；--from 重放走重放后的本函数。
            return "dev"
        return self._begin_round("dev")

    _PRUNED_DETAIL_RE = re.compile(r"^(.+?)::([^（:]+)（")

    def _record_pruned_no_progress(self, detail: list[str]) -> None:
        """把"被裁剪的补丁"登记成「补符号零生效」证据（供下一轮跳过同 prompt 补问）。

        真机 20260928-221831：补问产出的补丁整份被 prune（anchor 对不上），但补问自检
        只数补丁文本里有没有符号名 ⇒ 误以为补齐了；下一轮又对同一批符号补问一次，纯烧。
        补丁被裁 = 符号实际没进沙箱，这才是零生效的硬证据。按 path→施工图、
        符号名∈该图声明符号 两重收敛后登记，避免误伤同名辅助符号。
        """
        tasks = [t for t in ((self.state.get("plan") or {}).get("tasks") or [])
                 if isinstance(t, dict)]
        rec = self.state.setdefault("repair_no_progress", {})
        for raw in detail:
            m = self._PRUNED_DETAIL_RE.match(str(raw))
            if not m:
                continue
            path, symbol = m.group(1).replace("\\", "/"), m.group(2).strip()
            if not symbol or symbol == "?":
                continue
            for t in tasks:
                tid = str(t.get("id") or "")
                files = {str(p).replace("\\", "/") for p in (t.get("target_files") or [])}
                syms = {str(s) for s in (t.get("symbols") or [])}
                if not tid or path not in files or symbol not in syms:
                    continue
                row = rec.setdefault(tid, {"round": self.attempt, "gaps": []})
                row["round"] = self.attempt
                if symbol not in row["gaps"]:
                    row["gaps"].append(symbol)

    def _run_dev(self) -> str:
        self._stage_dev(self.requirement, self.fixes)
        # **定位失败的补丁不许留在累积实现里**（见 `patches.prune_unappliable` 的真机依据）。
        # 它们永远套用不上（`apply_all` 只收 status=="ok"），却会被 `_patch_blockers` 与
        # verify 当成"交付物不完整"的阻断项 —— 而下一轮 dev 只会再写一条**同样对不上**的
        # anchor（真机 20260927-221511：连续两次重问，问题集一字未变，4 条补丁照样套用不了）。
        # 留着的唯一后果就是"恒定判负"。移除动作**留痕**（日志 + state + handoff），不静默。
        # mock 下不生效：mock 的 dev 产物是按 schema 合成的 modify 占位数据，**每条**都
        # 定位不到；裁掉它们等于把"流程机制"的测试基线一起裁了（与覆盖审计 /
        # `_orphan_modify_files` 的同一条约定一致：mock 测流程，不测内容质量）。
        pruned = (
            {"dropped": 0, "detail": []}
            if isinstance(self.client, MockClient)
            else patches.prune_unappliable(self.repo, self.state.get("implementation"))
        )
        if pruned["dropped"]:
            self.state.setdefault("pruned_patches", []).extend(pruned["detail"])
            # 审计与 edits 是**按位置对齐**的（`apply_all` 用 zip），裁剪后必须重算
            self.state.pop("patch_audit", None)
            self._record_pruned_no_progress(pruned["detail"])
            self.log(
                f"        [补丁裁剪] {pruned['dropped']} 条**定位失败**的补丁（anchor/符号与原文对不上）"
                "已从实现里移除："
                + "、".join(pruned["detail"][:3])
                + " —— 它们永远套用不上，留着只会变成每轮都判负的恒定阻断项（已记入 handoff）"
            )
        audit = self._audit_implementation()
        self.state["implementation_audit"] = audit
        if audit["missing"]:
            self.log(f"        [覆盖审计] 方案任务未被任何补丁覆盖: {audit['missing']}")
        if audit["unknown_tasks"]:
            self.log(f"        [覆盖审计] 引用了不存在的任务 id: {audit['unknown_tasks']}")
        if audit.get("vanished_symbols"):
            self.log(
                f"        [覆盖审计] 返工让符号消失且未声明: {audit['vanished_symbols']}"
                "（可能是越改越少）"
            )
        patch_audit = self._audit_patches()
        self.state["patch_audit"] = patch_audit
        if patch_audit.get("source_available"):
            self.log(
                f"        [补丁校验] 可套用 {patch_audit['ok']} 条 / 有问题 {patch_audit['problems']} 条"
                + (f"：{'；'.join(patch_audit['problem_detail'][:4])}" if patch_audit["problems"] else "")
            )
            written = patches.write_patch_files(
                self.run_dir, self.repo, self.state.get("implementation"), patch_audit  # type: ignore[arg-type]
            )
            if written:
                self.log(f"        [补丁落盘] {len(written)} 个 → runs/{self.run_id}/patches/")
        return flow.next_linear("dev")

    def _run_test(self) -> str:
        self._stage_test(self.requirement, self.fixes)
        return flow.next_linear("test")

    def _run_verify(self) -> str:
        self._stage_verify()
        return flow.next_linear("verify")

    def _run_review(self) -> str:
        return self._step_review()

    def _run_human_review(self) -> str:
        return self._step_human_review()

    # ------------------------------------------------------------------ 护栏
    def _budget_exceeded(self) -> str | None:
        """运行级预算判定（墙钟 / token 累计）；超限则返回原因，未超返回 None。"""
        spent = self.elapsed_s + (time.time() - self._t0)
        if MAX_WALL_S > 0 and spent > MAX_WALL_S:
            return f"墙钟已用 {round(spent)}s，超过上限 {MAX_WALL_S}s"
        if MAX_TOTAL_TOKENS > 0:
            used = sum(
                (c.get("prompt_tokens") or 0) + (c.get("output_tokens") or 0) for c in self.calls
            )
            if used > MAX_TOTAL_TOKENS:
                return f"token 累计 {used}，超过上限 {MAX_TOTAL_TOKENS}"
        return None

    def _stagnating(self) -> str | None:
        """停滞判定：最近 N 轮待修项数量始终 > 0 且没有下降，就认为「改不动」了。"""
        if REWORK_STAGNATION_LIMIT <= 0:
            return None
        counts = [len(r.get("required_fixes_in_material") or []) for r in self.rounds]
        tail = counts[-REWORK_STAGNATION_LIMIT:]
        if len(tail) < REWORK_STAGNATION_LIMIT:
            return None
        # 曾经清零过（那一轮其实已经判 pass）就不算停滞，避免误杀
        if not all(tail):
            return None
        # 判据：**整个窗口有没有净下降**（末项 < 首项 = 在改善 → 放行）。
        # 不能用「比上一轮少」（[...,4,5,4] 末轮确实比上轮少，但那是在原地抖动），
        # 也不能用「比窗口内最小值还小」（[2,1,1] 是从 2 降到 1 的真改善，却会被误杀 ——
        # 真机 run 20260925-045404 就这么被提前叫停过一轮）。
        if tail[-1] < tail[0]:
            return None
        return f"最近 {REWORK_STAGNATION_LIMIT} 轮待修项数量 {tail} 没有净下降"

    def _guard_stop_reason(self) -> str | None:
        return self._budget_exceeded() or self._stagnating()

    def _step_review(self) -> str:
        # 建议⑬：review_due = cadence_due OR force_review_reason（五类事件，见
        # _force_review_events）。节奏窗口外命中事件同样必须补评 —— 典型：BUG 修复轮
        # 台账缺陷刚转绿，不能因「今天不是 review 轮」再空跑一次 dev。
        cadence_due, cadence_reason = self._review_due(self.attempt)
        force_reasons: list[str] = []
        if not cadence_due:
            force_reasons = self._force_review_events()
        if not cadence_due and not force_reasons:
            # 能走到跳评分支，上一次评审的结论必然是 rework_dev（转绿会触发 defect_closed
            # 强制评审；回方案走的是 architect_plan 游标）——也就是说这一轮 dev 干的是
            # 「照缺陷单修复」，口径必须标 BUGFIX。
            # 真机 run 20260928-200631：漏标后 _begin_round 默认回退成 FEATURE，
            # 口径随节奏交替翻转（bugfix→首次开发→bugfix→首次开发），FEATURE 轮
            # 无视「禁止 add 整份」约束，把上一轮修好的 import/入口守卫又整份覆盖回去，
            # 五轮不收敛。
            self._mark_next_round(tasktype.BUGFIX)
            self.log(
                f"  第 {self.attempt} 轮跳过评审（review_every={self.review_every}；首轮与末轮必评审）"
                f"——延续缺陷修复口径（{tasktype.round_kind_label(tasktype.BUGFIX)}）"
            )
            return self._begin_round("dev")
        if cadence_due:
            due_reason = cadence_reason
        else:
            due_reason = "force:" + ",".join(force_reasons)
            self.log(f"  [评审调度] 节奏窗口外强制评审（{due_reason}）")
        self.state["review_due_reason"] = due_reason
        review = self._stage_review(self.requirement, self.fixes) or {}
        self.last_review_attempt = self.attempt
        # 记录本轮回看时的方案边界：下轮 _force_review_events 据此判 plan_boundary_changed。
        self.state["last_review_plan_files"] = sorted(self._plan_file_set())
        in_material, architect_fixes, external, forced = self._normalize_review(review)
        if self.run_dir is not None:
            # 归一化后的评审要回写 NN 文件：_call 内部记录的是原始（未归一化）评审，
            # 续跑时 latest_artifacts 会用原始评审覆盖 state，把 verdict 回退成 rework_dev
            # （人工审核闸门等"评审后再续跑"的场景会暴露此问题）。
            runstore.save_artifact(self.run_dir, "review", review, note="review-normalized")
        verdict = review.get("verdict", "rework_dev")
        # 机械证据**只取一次**：下面既写进轮次记录，又喂给归因（两处必须看到同一份，
        # 否则会出现"轮次记录里有阻断项、归因却说无缺陷"这种自相矛盾的账）。
        patch_blockers = self._patch_blockers()
        mechanical_blockers = self._mechanical_blockers()
        self.rounds.append(
            {
                "attempt": self.attempt,
                "verdict": verdict,
                # 建议⑬：本轮为什么开评（节奏码 / force:事件列表）
                "review_due_reason": due_reason,
                "forced_pass": forced,
                "forced_rework": bool(review.get("forced_rework")),
                "patch_blockers": patch_blockers,
                "mechanical_blockers": mechanical_blockers,
                "required_fixes_in_material": in_material,
                "required_fixes_architect": architect_fixes,
                "required_fixes_external": external,
                # 去向必须与**实际**一致：pass / 全需外部确认时这一轮并不会回 dev，
                # 一律记成 "dev" 会让人读成「明明通过了却又打回去改」——人工排查时踩过。
                "routed_to": (
                    "architect_plan" if (verdict == "rework_architect" or architect_fixes)
                    else ("dev" if verdict == "rework_dev" else verdict)
                ),
                "review": review,
            }
        )
        self.log(
            f"  评审判定: {verdict}"
            + (
                f"（本轮材料内可改 {len(in_material)} 项 / 方案层 {len(architect_fixes)} 项 / "
                f"需外部确认 {len(external)} 项）"
                if not forced
                else ""
            )
        )
        # ---- 缺陷台账：跨轮守恒（仍开 / 本轮转绿 / 不再出现 / 回归）
        # 直接用评审阶段已算好的逐项验收结果（`_stage_review` 写进 state），不重算。
        # 它回答的是返工最缺的那句"到底还剩几条、上次那几条去哪了"。
        # **必须在 done / human_review 提前返回之前记账**：最终确认（pass）轮同样要闭账，
        # 否则上一轮刚转绿的条目在 pass 轮没有任何记录，跨轮守恒永远差最后一页。
        # **先闭账、后归因**：Recovery Policy 要读台账里每条开缺陷的「证据连续计数」。
        led = diagnose.ledger(
            self.state.get("defect_ledger"),
            rows=self.state.get("defect_verdicts") or [],
            round_no=self.attempt,
        )
        self.state["defect_ledger"] = led
        self.log(diagnose.ledger_line(led))

        # 建议⑭：Transition Record 的证据/缺陷部分在路由前备妥（四个出口共用同一份）。
        transition_defect_ids = sorted(str(k) for k in (led.get("open") or {}).keys())
        transition_evidence: list[str] = [f"patch:{b}" for b in patch_blockers]
        transition_evidence += [f"mechanical:{b}" for b in mechanical_blockers]
        for cmd in ((self.state.get("verify_report") or {}).get("commands") or []):
            if isinstance(cmd, dict) and str(cmd.get("status") or "") not in ("ok", "skipped", ""):
                transition_evidence.append(
                    f"verify:{str(cmd.get('command') or '')[:60]}:{cmd.get('status')}"
                )

        # 交付指纹：本批补丁「内容 + 落点」的摘要。Recovery Policy 的
        # 「补丁无实质变化」判据靠它 —— 同一缺陷同证据连失两轮、指纹还一模一样，
        # 说明 DEV 这一轮什么也没改动，再打回一次只是再烧一轮。
        impl_digest = self._delivery_digest(
            self.state.get("implementation") or {}, self.state.get("patch_audit") or {}
        )
        prev_digest = next(
            (str(r.get("impl_digest")) for r in reversed(self.rounds[:-1]) if r.get("impl_digest")),
            "",
        )
        impl_unchanged = bool(prev_digest) and prev_digest == impl_digest
        self.rounds[-1]["impl_digest"] = impl_digest

        # ---- 归因与去向：**一次确定性分类**（替换原先散开的七层 if 链）
        # 判据散在四个函数里时改动容易只改一半 —— 那正是本项目"机制空转"的常见成因。
        # 现在「为什么没修好」与「下一跳去哪」由一处产出，并逐轮落进
        # `state.failure` / `state.failure_history`：返工原因从散落日志变成可查询数据。
        # 收敛 / 预算护栏：决定「再烧一轮」之前先问一句值不值。
        # 真机教训 run 20260924-185507：required_fixes 走势 3→2→0→2→4→4→5→4，
        # 第 3 轮已经 pass 之后又反弹，一路烧到 attempt=11/max=12 —— 期间独占显存
        # （单驻留，无法新建运行）却毫无收敛迹象。光靠 max_rework 只在撞顶那一刻才停。
        stop = self._guard_stop_reason()
        record = diagnose.classify(
            review=review,
            attempt=self.attempt,
            max_rework=self.max_rework,
            guard_stop=stop or "",
            patch_blockers=patch_blockers,
            patch_failures=self._patch_failures(),
            mechanical_blockers=mechanical_blockers,
            # 跨文件契约虚依赖：归因指向方案层，但**路由暂不动它**（改行为要有基线，
            # 见 diagnose.classify 的 owner_mismatch 说明）。
            unresolved=self.state.get("plan_unresolved"),
            missing_entry=self._plan_missing_entry(),
            uncovered_files=self._plan_uncovered_defects(),
            external_fixes=external,
            prior_types=[str(r.get("failure") or "") for r in self.rounds[:-1]],
            # Recovery Policy（Escalation by evidence）
            open_defects=led.get("open") or {},
            impl_unchanged=impl_unchanged,
        )
        self.rounds[-1]["failure"] = record["type"]
        self.rounds[-1]["owner_class"] = record.get("owner_class") or ""
        self.rounds[-1]["recovery"] = record.get("recovery") or {}
        self.state["failure"] = record
        history_rec = {
            key: record.get(key)
            for key in (
                "round", "type", "owner", "owner_class", "recover_stage",
                "needs_human", "stop", "repeat",
            )
        }
        history_rec["recovery_action"] = (record.get("recovery") or {}).get(
            "action", "retry_owner"
        )
        self.state.setdefault("failure_history", []).append(history_rec)
        # Ontology overlay：本轮开缺陷 → Defect、归因 → Failure、策略 → Recovery（只投影不裁决）
        self._project_failure_ontology(record, led)
        for line in diagnose.render(record):
            self.log(line)
        if record["needs_human"]:
            self.needs_human = True
        if stop:
            self.state["guard_stop"] = stop

        target = record["recover_stage"]
        # 路由原因：归因分类的类型（pass / 各类失败 / needs_human 等），即 Transition 的 reason。
        transition_reason = str(record.get("type") or verdict or "")
        if target == "done":
            self._record_transition(
                to="done", reason=transition_reason,
                defect_ids=transition_defect_ids, evidence=transition_evidence,
                due_reason=due_reason,
            )
            return "done"
        if target == "human_review":
            if HUMAN_REVIEW_GATE:
                self._record_transition(
                    to="human_review", reason=transition_reason,
                    defect_ids=transition_defect_ids, evidence=transition_evidence,
                    due_reason=due_reason,
                )
                return "human_review"
            self._record_transition(
                to="done", reason=transition_reason,
                defect_ids=transition_defect_ids, evidence=transition_evidence,
                due_reason=due_reason,
            )
            return "done"

        # 方案层返工项也进 fixes：回退到方案时架构师要能逐条看到「我漏了什么」，
        # 只做路由不带上内容的话，架构师拿不到任何具体指示。
        # 但**上一轮的返工项必须先过证伪**：它会被喂回下一轮评审
        # （prompts.parts_review 的「上一轮已提出的修复项」），原样传下去就是自指循环
        # 的燃料（见 _refute_stale_blockers 的真机复盘）。
        # ⚠ **两条通道都要滤**：契约改版后评审的返工项主要落在 required_fixes_detail
        # （→ in_material），只滤 blockers 等于半开着 —— 真机校准 20260926-212611 的
        # 返工项正是**全在 in_material、blockers 为空**（回放历史数据时才看出来）。
        blockers_kept, dropped_blockers = self._refute_stale_blockers(
            list((review or {}).get("blockers") or [])
        )
        in_material_kept, dropped_in_material = self._refute_stale_blockers(list(in_material))
        dropped = [*dropped_blockers, *dropped_in_material]
        if dropped:
            review["refuted_blockers"] = dropped
        fixes = list(architect_fixes) + in_material_kept + blockers_kept
        self.fixes = fixes
        if target == "architect_plan":
            # 自动回转：判「方案本身有问题」**或**有任一条方案层返工项，都回方案阶段重跑。
            # 之前只看 verdict —— 评审把方案层根因误标成实现层时，就会整轮打回开发，
            # 而开发在方案 changes 范围内改不动它，白烧一轮（真机 run 20260924-185507）。
            why = (
                "方案本身有问题"
                if verdict == "rework_architect"
                else f"评审把 {len(architect_fixes)} 条返工项判为方案层"
            )
            self.log(f"  {why}：回到架构师方案（方案改了实现必然重做）")
            # 下一轮 dev 是**方案返工后施工**，不是"最小改动修缺陷"：方案可能新增了文件与符号，
            # 口径必须跟着换 —— 否则评审要求加文件、开发无权创建，两边都没错却不收敛（真机 L2）。
            self._mark_next_round(tasktype.PLAN_REWORK)
            self._record_transition(
                to="architect_plan", reason=transition_reason,
                defect_ids=transition_defect_ids, evidence=transition_evidence,
                due_reason=due_reason,
            )
            return self._begin_round("architect_plan")
        # 其余回流都是在**既有方案范围内**修东西：按缺陷修复口径（最小改动 + 逐条回应）。
        self._mark_next_round(tasktype.BUGFIX)
        self._record_transition(
            to="dev", reason=transition_reason,
            defect_ids=transition_defect_ids, evidence=transition_evidence,
            due_reason=due_reason,
        )
        return self._begin_round("dev")

    def _extend_budget_for_human(self, reason: str) -> None:
        """人工介入 ⇒ **追加**回流预算，然后才重跑（没有需要时什么都不做）。

        规则：把上限抬到 ``max(当前上限, attempt + HUMAN_REWORK_BUDGET)``（单调不减）。
        为什么是 `attempt + N` 而不是 `+1`：`_begin_round` 会把 attempt 推一格（从
        `architect_plan` 回流会推两格），而触顶判据是绝对比较 `attempt > max_rework` ——
        只加 1 的话下一轮照样立刻触顶。

        只在「已经到/超过上限」时才追加：未触顶的正常路径完全不受影响（行为零变化）。
        """
        if HUMAN_REWORK_BUDGET <= 0 or self.attempt < self.max_rework:
            return
        topups = int(self.state.get("human_budget_topups") or 0)
        if topups >= HUMAN_REWORK_TOPUP_MAX:
            self.log(
                f"  [预算] {reason}：人工追加次数已达上限 {HUMAN_REWORK_TOPUP_MAX}，不再自动追加"
                f"（当前上限 {self.max_rework}；如需继续请用 --max-rework 显式指定）"
            )
            return
        before = self.max_rework
        self.max_rework = max(self.max_rework, self.attempt + HUMAN_REWORK_BUDGET)
        self.state["human_budget_topups"] = topups + 1
        self.log(
            f"  [预算] {reason}：人工介入 → 回流上限 {before} → {self.max_rework}"
            f"（追加 {topups + 1}/{HUMAN_REWORK_TOPUP_MAX} 次）"
        )

    def _step_human_review(self) -> str:
        """交付前人工审核闸门（非模型阶段）：review 通过后才到达。

        首次到达：写占位产物并暂停，等人工在控制台核对 4 项后提交 verdict。
        再次到达（人工已提交，由 resume 触发）：verdict=approve 放行交付；
        verdict=reject 把人工意见注入 dev 回流修复，回到开发重跑。
        """
        if not HUMAN_REVIEW_GATE:
            return "done"
        if self.defer_human_review:
            # 作业模式：模块级验收**延后**到作业统一验收 —— 分模块各停一次没有意义，
            # 人要审的是所有模块合起来的交付物（见 gateway 的作业验收闸门）。
            self.log("  [作业] 模块级验收延后：统一由作业验收一次（人工环节只这一处）")
            return "done"
        art = self.state.get("human_review")
        if not isinstance(art, dict) or not art.get("verdict"):
            placeholder = {
                "verdict": "",
                "core_path_ok": None,
                "no_obvious_errors": None,
                "deliverables_complete": None,
                "requirement_met": None,
                "notes": "",
                "reviewer": "",
                "_placeholder": True,
                "_instruction": (
                    "人工审核闸门：逐项核对——①核心业务路径走通、主流程通顺；"
                    "②无明显低级错误/逻辑硬伤；③交付物完整（代码/文档/说明齐全）；"
                    "④对照最初需求核心诉求已满足。verdict 填 approve 放行交付；"
                    "填 reject 并在 notes 写明问题，将回流到开发修复。"
                ),
            }
            self.state["human_review"] = placeholder
            assert self.run_dir is not None
            self._record(
                "human_review", placeholder,
                {"kind": "gate", "stage": "human_review", "gate": "human_review"}, "",
            )
            # 暂停不在这里做：写完占位产物后**停在原地**，由 _gate_after('human_review')
            # 判成 mandatory 闸门并 _interrupt —— 闸门判定/文案/留痕统一在那一处。
            return "human_review"
        verdict = str(art.get("verdict"))
        if verdict == "approve":
            self._log_action(
                "human_review_approve", "human_review",
                text=art.get("notes") or "",
                kind="core_path_ok" if art.get("core_path_ok") else "",
            )
            self.log("== 人工审核通过，放行交付")
            return "done"
        # reject：回流到开发修复，人工意见作为 dev 阶段的人工反馈注入；清除内存产物避免下次到达时误判为已提交
        notes = art.get("notes") or "（人工审核打回，未填写具体问题）"
        feedback = f"[人工审核打回] {notes}"
        self.human_feedback.setdefault("dev", []).append(feedback)
        self._log_action("human_rework", "human_review", text=notes, kind="human_review_reject")
        self.state.pop("human_review", None)
        self.log(f"== 人工审核打回：回流到开发修复。问题：{notes}")
        # 先追加预算再回流：否则 attempt 一推过上限，下一轮评审立刻判 needs_human 结束
        # （真机 run 20260924-185507 就是"人工打回后只跑一轮就死"）
        self._extend_budget_for_human("人工审核打回")
        # 人工打回是**缺陷修复**轮（不是方案返工）：口径按"最小改动 + 逐条回应人工意见"，
        # 人工意见已注入 `human_feedback["dev"]`。
        self._mark_next_round(tasktype.BUGFIX)
        return self._begin_round("dev")

    def _gate_after(self, stage: str, nxt: str | None = None) -> Interrupt | None:
        """该阶段结束后是否要停下等人工 —— **唯一的闸门判定入口**。

        三类闸门（静态声明见 ``flow.GATE_SPECS``）：
          · ``explicit``    —— 人工在 ``--pause-after`` / 页面勾选里显式指定该阶段，无条件停；
          · ``conditional`` —— 阶段自带触发条件（**强控**）：PM 还有未成为陈述的条目 /
            补强识别出 high 待确认项。判据没被满足就不放行 —— 续跑时会重新判定
            （见 ``_execute`` 开头的复核），所以人工不能"什么都不裁决直接点继续"。
          · ``mandatory``   —— 该节点本身就是人工节点：``human_review`` 首次到达
            （``_step`` 已写占位产物并停在原地，``nxt == stage``），到达即停。
        """
        if stage in self.pause_after:
            return Interrupt(stage, "explicit", "人工闸门")
        if stage == "human_review" and HUMAN_REVIEW_GATE and nxt == "human_review":
            # 首次到达：_step 写了占位产物并停在原地（nxt == stage）。
            # 再显式看一次 verdict：人工已提交时即便被误判到这一支，也不该再停。
            art = self.state.get("human_review")
            if not (isinstance(art, dict) and str(art.get("verdict") or "").strip()):
                spec = flow.gate_spec("human_review")
                return Interrupt(stage, "mandatory", spec.title if spec else "人工审核闸门")
        return self._conditional_gate(stage)

    def _conditional_gate(self, stage: str) -> Interrupt | None:
        """数据驱动的条件闸门（强控）：判据**未满足就不放行**。

        与 ``explicit``（人工勾选，停一次即算数）必须分开：那一条停过一次，
        续跑就是人工在说"我知道了，继续"；这一条是"还有东西没成为陈述"，
        人工不说清楚（裁决 / 把未明确项写成确定结论）就不能往下流。
        """
        if stage == "intake" and INTAKE_PAUSE_ON_GAPS:
            gaps = self._intake_high_gaps()
            if gaps:
                spec = flow.gate_spec("intake")
                return Interrupt(
                    stage,
                    "conditional",
                    spec.title if spec else "需求补强闸门",
                    "以下缺失要素**还没有人工裁决**，不得带进下游 —— " + "；".join(gaps),
                )
        if stage == "pm" and self.pause_on_open_questions:
            left = self._pm_unresolved()
            # 裁决一致性：判据**现算**（人工可能直接编辑了 02-pm.json），与 PM 返工、
            # 作业层 gateway 用同一份 prompts.pm_decision_conflicts。
            conflicts = self._pm_decision_conflicts()
            # 跨字段同级事实矛盾（规格§三十三）：优先级仲裁后仍对立的 CONTRADICTION
            contradictions = self._claim_contradictions()
            if left["pending"] or left["vague"] or conflicts or contradictions:
                bits: list[str] = []
                if left["pending"]:
                    bits.append("未裁决的未决项：" + "；".join(left["pending"][:4]))
                if left["vague"]:
                    bits.append("未明确的条目：" + "；".join(left["vague"][:4]))
                if conflicts:
                    bits.append(
                        "裁决与需求/验收自相矛盾（一律以裁决为准，或直接编辑产物消除矛盾）："
                        + "；".join(prompts.pm_decision_conflict_lines(conflicts)[:4])
                    )
                if contradictions:
                    lines = []
                    for item in contradictions[:4]:
                        fact_txt = " ⨯ ".join(
                            f"{f['where']}「{f['text']}」（{f['polarity']}）"
                            for f in item.get("facts", [])
                        )
                        lines.append(f"同级事实冲突[{ '/'.join(item.get('sources') or []) }]：{fact_txt}")
                    bits.append("事实同级冲突且无法按优先级裁决（必须人工选定，不允许猜）：" + "；".join(lines))
                spec = flow.gate_spec("pm")
                return Interrupt(
                    stage,
                    "conditional",
                    spec.title if spec else "PM 未决项闸门",
                    "以下内容**还不是陈述**，不得带进下游 —— " + " ｜ ".join(bits),
                )
        if stage == "architect_plan" and self.state.get("design_gate_blocked"):
            # Design Gate 强控：首次到达（阻断已在方案阶段算好）与续跑（人工可能改过方案）
            # 都走这里。续跑时重跑**确定性**编译链重新判定 —— 不修改就点继续会被原地再停。
            blockers = self._design_gate_resume_check()
            if blockers:
                self.state["design_gate_blocked"] = blockers
                self._persist()
                detail = (
                    f"以下 {len(blockers)} 项设计阻断未消除，**不得带进开发** —— "
                    + "；".join(str(b.get("detail")) for b in blockers[:4])
                    + f"。处理方式：① 直接续跑 {self.run_id} 重放方案阶段（--from architect_plan "
                    "--feedback \"...\"）；② 在页面/产物里改好方案后续跑（会重新做机械编译校验）。"
                )
                return Interrupt(stage, "conditional", "设计闸门（Design Gate）", detail)
            self.state.pop("design_gate_blocked", None)
            self.state.pop("design_gate_attempts", None)
            self.log("  [强控] 设计阻断项已消除 —— 放行进入开发")
        return None

    def _pm_unresolved(self) -> dict[str, list[str]]:
        """PM 产物里尚未成为陈述的条目 —— 判据本体在模块级 ``pm_unresolved_items``。

        这里只负责把「当前 state 的 scope」与「人工裁决记录」交给它，保证与作业层
        （``gateway.job_pm_blockers``）用的是**同一份判据**。
        """
        return pm_unresolved_items(self.state.get("scope"), self.pm_decisions)

    def _pm_decision_conflicts(self) -> list[dict[str, str]]:
        """裁决 vs 需求/验收的极性冲突 —— 与未决项同一口径：先并人工裁决再现算。

        人工可以直接编辑 02-pm.json 消除矛盾，所以这里**不读缓存**，每次从当前 scope 现算。
        """
        scope = prompts.normalize_pm_questions(self.state.get("scope"))
        merged = prompts.apply_pm_decisions(scope, self.pm_decisions)
        return prompts.pm_decision_conflicts(merged)

    def _claim_contradictions(self) -> list[dict[str, Any]]:
        """规格§三十三：跨字段**同级**事实矛盾（CONTRADICTION 阻断，不猜赢家）。

        流程：``prompts.claim_conflict_groups`` 机械发现极性翻转的事实组 →
        ``ontology.reconcile_claims`` 按事实优先级仲裁：

          * 不同级（人工裁决 vs PM 推断 / 高优先 vs 低优先）⇒ 高者胜，低者记
            ``state.claim_conflicts_resolved``（不因字段先后翻转）；
          * 最高层同级仍对立 ⇒ 返回阻断项，PM 闸门暂停交人工。

        现算（人工可能直接改过产物），与 ``_pm_decision_conflicts`` 同纪律。
        """
        blocking: list[dict[str, Any]] = []
        resolved: list[dict[str, Any]] = []
        for gi, group in enumerate(self._scope_conflict_groups()):
            claims = [
                {"id": f["id"], "subject": f"conflict-group-{gi}",
                 "polarity": f["polarity"], "source": f["source"], "truth": f["truth"]}
                for f in group
            ]
            verdict = ontology.reconcile_claims(claims)
            if verdict["contradictions"]:
                blocking.append({
                    "reason": "same_level_conflict",
                    "sources": sorted({str(f.get("source") or "") for f in group}),
                    "facts": [
                        {"where": f.get("where") or "", "text": f.get("text") or "",
                         "source": f.get("source") or "",
                         "polarity": "肯定" if f.get("polarity") == 1 else "否定"}
                        for f in group
                    ],
                })
            else:
                resolved.extend(verdict["overridden"])
        # 高优先级覆盖记录留痕（只追加，供 issues 派生视图/回放）
        self.state["claim_conflicts_resolved"] = resolved
        return blocking

    def _scope_conflict_groups(self) -> list[list[dict[str, Any]]]:
        """机械发现的跨字段极性冲突事实组（已并回人工裁决），现算。

        PM 闸门（:meth:`_claim_contradictions`）与 Ontology Release 兜底
        （``_proof_gate`` 的 project_conflict_claims）共用这一个真源，
        避免两层各算一遍口径漂移。
        """
        scope = prompts.normalize_pm_questions(self.state.get("scope"))
        scope = prompts.apply_pm_decisions(scope, self.pm_decisions)
        intake = prompts.apply_intake_decisions(
            self.state.get("intake") or {}, self.intake_decisions
        )
        return prompts.claim_conflict_groups(scope, prompts.intake_items(intake))

    def _should_pause(self, stage: str) -> bool:
        """兼容旧签名的薄包装：等价于「该阶段之后是否会 interrupt」。"""
        return self._gate_after(stage) is not None

    def _interrupt(self, it: Interrupt) -> None:
        """执行一次人工闸门：落盘 + 留痕 + 打印可操作提示（暂停文案只有这一份）。"""
        self.status = "paused"
        self.paused_after = it.stage
        self._log_action("human_gate", it.stage)
        self._persist()
        if it.kind == "mandatory":
            self.log(
                f"== {it.title}：停在 {it.stage}，请人工核对 4 项后提交（通过放行 / 打回修复）。"
                f"产物见 runs/{self.run_id}/NN-{it.stage}.json"
            )
            return
        detail = f"{it.detail} " if it.detail else ""
        self.log(
            f"== {it.title}：{it.stage} 阶段结束，已暂停。"
            + detail
            + f"人工确认（可直接编辑 runs/{self.run_id}/NN-{it.stage}.json 里的 artifact）后，"
            f"用 --resume {self.run_id} 继续；打回用 --resume {self.run_id} --from {it.stage} --feedback \"...\""
        )

    def _execute(self) -> None:
        # 在场标记：跑流水线的**这个进程**自己写 pid + 心跳。服务端重启之后仍能据此判定
        # 「这个运行还在跑」（内存注册表会丢，进程不会）。两条入口 run()/resume() 都汇到
        # 这里，所以只在这一处起；停止放在各自的 _finish() 之后（交付也还在跑，别提前清）。
        self._start_presence()
        # 强控复核：上次停在**条件闸门**上时，若判据仍未满足就再停一次，且**不推进任何阶段**。
        # 没有这一步，cursor 早已越过该阶段（`_step` 先推进游标、闸门后判定），人工"什么都不
        # 裁决、直接点继续"就能把未成为陈述的条目原样带进下游 —— 那正是要堵的洞。
        gate_stage = self._resume_gate_stage
        self._resume_gate_stage = None
        if gate_stage:
            it = self._conditional_gate(gate_stage)
            if it is not None:
                self.log(f"  [强控] {gate_stage} 的待裁决项仍未解决 —— 不放行，继续停在这里")
                self._interrupt(it)
                return
            if gate_stage == "architect_plan" and self.cursor == "dev":
                # 方案阶段在闸门拦截时**没有**开轮（见 _run_architect_plan）；
                # 人工改好方案、复核放行的此刻补开 —— attempt/迭代日志与真实轮次对齐。
                # 旧版本暂停在这道闸门前已把 attempt 自增成 1（dev 其实一轮没跑）：
                # 按不变量归一 —— 还没有任何实现产物，下一轮就是第 1 轮。
                if not isinstance(self.state.get("implementation"), dict):
                    self.attempt = 0
                self._begin_round("dev")
        try:
            while self.cursor != "done":
                stage = self.cursor
                nxt = self._step(stage)
                self.cursor = nxt
                # 闸门统一走 interrupt 语义：_gate_after 判「要不要停」，_interrupt 负责
                # 落盘/留痕/打印提示。human_review 首次到达时 _step 返回自身（nxt == stage），
                # 所以这里把 nxt 一并交给判定函数（mandatory 闸门需要它区分「首达 / 人工已提交」）。
                it = self._gate_after(stage, nxt)
                if it is not None:
                    self._interrupt(it)
                    return
                self._persist()
        except Exception as exc:  # noqa: BLE001
            # ---- 任何阶段异常都**不许让运行隐身死掉** ----
            # 以前异常直接冒到 cli 顶层：进程退出，而 `state.json` 还停在 `status=running`、
            # 心跳停摆、页面上看是"还在跑"，实际什么都没发生 —— 真机 20260927-150931 就是
            # 这样白等了 24 分钟（符号补漏调用抛 OllamaError 崩溃）。
            # 这里落成**可恢复的失败态**：留痕（问题记录里看得到）+ 停在人工闸门；
            # `cursor` **不前移**，人工点"继续"就从同一个阶段重新进入。
            detail = f"{type(exc).__name__}: {exc}"
            self.state.setdefault("stage_errors", []).append(
                {"stage": self.cursor, "error": detail[:1200]}
            )
            self.log(
                f"== [中止] {self.cursor} 阶段异常，已停在人工闸门（cursor 不动，续跑可重试）："
                f"{detail[:300]}"
            )
            self.needs_human = True
            self.status = "paused"
            self._persist()

    # ------------------------------------------------------------------ 入口：新运行
    def run(
        self,
        requirement: str,
        stages: list[str] | None = None,
        pause_after: list[str] | None = None,
        run_id: str | None = None,
    ) -> RunResult:
        self.requirement = requirement
        self.grounding_warnings = []
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
        self.run_dir = self.runs_dir / self.run_id
        if run_id is None:  # 显式指定 run_id 时由调用方负责唯一性（操作页面按 run_id 绑定日志）
            suffix = 1
            while self.run_dir.exists():
                suffix += 1
                self.run_dir = self.runs_dir / f"{self.run_id}-{suffix}"
        self.run_id = self.run_dir.name
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # 同一 run_id 的**全新运行**（续跑走 `resume()`，不经过这里）落到已有目录时，
        # 先把上一代阶段快照归档到 superseded/。真机 job-20260926-154657-M-01：被中断后
        # 以同一 run_id 重跑，旧代码不归档 ⇒ seq=3 同时有上一代的 architect_plan 与
        # 这一代的 architect_assess，阶段列表与检查点时间线重复且乱序，人工看「最新产物」
        # 会读错代次。作业侧早就有这道归档（gateway._archive_previous_run），
        # 普通运行（复用 --run-id）一直没有 —— 这里补齐；探测侧另有
        # `runstore.duplicate_stage_seqs` 兜底（归档失效时能报出来，而不是静默混存）。
        # `--only` 不归档：它是「就地重跑某一阶段供复核」，把用户正在看的产物搬走更糟。
        if not stages:
            moved = runstore.archive_stages(self.run_dir, list(runstore.FLOW_ORDER))
            if moved:
                self.log(
                    f"== 同一 run_id 复跑：归档上一代阶段快照 {len(moved)} 个（{runstore.SUPERSEDED_DIR}/）"
                )
        (self.run_dir / "requirement.txt").write_text(requirement, encoding="utf-8")
        if pause_after is not None:
            self.pause_after = set(pause_after)
        # 首次启动：把这次的闸门设置记为「原始输入」（续跑时不会被覆盖）
        self._initial_pause_after = sorted(self.pause_after)
        self._t0 = time.time()
        self.elapsed_s = 0.0
        # 记录当时的提示词/配置指纹：没有它，跨运行的问题对比无法归因
        self._refresh_env()
        self.log(
            f"== run {self.run_id} (repo={self.repo or '未提供'})"
            + (f" 人工闸门: {sorted(self.pause_after)}" if self.pause_after else "")
        )
        if stages:
            return self._run_only(requirement, stages)

        self.mode = "full"
        self.status = "running"
        self.cursor = "intake"
        self._persist()
        self._execute()
        try:
            return self._finish()
        finally:
            # 交付是在 _finish 里做的，所以标记要等它做完再清 ——
            # 否则「正在交付」的那几秒会被页面看成「已中断，可续跑」。
            self._stop_presence()

    def _run_only(self, requirement: str, stages: list[str]) -> RunResult:
        self.mode = "only"
        self.status = "running"
        self.cursor = "done"
        if self.pause_after:
            self.log("  [注意] --only 模式不支持人工闸门（--pause-after 被忽略），也不写 state.json")
        self.pool = self._build_pool(requirement)
        self.log(f"== 仅执行指定阶段: {stages}")
        for stage in stages:
            self.log(runstore.stage_marker(stage))  # 与 _step 一致，页面才能按阶段切日志
            {
                "intake": self._stage_intake,
                "pm": self._stage_pm,
                "architect_assess": self._stage_assess,
                "architect_plan": self._stage_plan,
                "dev": self._stage_dev,
                "test": self._stage_test,
                "verify": self._stage_verify,
                "review": self._stage_review,
            }[stage](requirement)
        return self._finish()

    # ------------------------------------------------------------------ 入口：续跑
    def resume(
        self,
        run_dir: str | Path,
        from_stage: str | None = None,
        feedback: str | list[str] | None = None,
        pause_after: list[str] | None = None,
        review_every: int | None = None,
        max_rework: int | None = None,
        mock: bool | None = None,
        issue_kind: str = "",
        rework_reason: str = "",
        from_checkpoint: int | None = None,
    ) -> RunResult:
        run_dir = Path(run_dir)
        snap = runstore.read_state(run_dir)
        if not snap:
            raise OrchestratorError(f"{run_dir} 下没有 state.json（--only 运行或目录不存在），无法续跑")
        if snap.get("mode") == "only":
            raise OrchestratorError("该运行是 --only 单阶段运行，不支持续跑")

        self.run_dir = run_dir
        self._restore(snap)
        # 运行模式（真实模型 / mock）绑定在 run 上：续跑默认沿用，避免 mock 运行误加载真实模型
        want_mock = bool(snap.get("mock")) if mock is None else mock
        if want_mock and not isinstance(self.client, MockClient):
            self.client = MockClient(rework_first=int(snap.get("mock_rework_first") or 0))
            self.log("== 该运行是 mock 运行：自动恢复 MockClient（不加载任何模型）")
        elif mock and not snap.get("mock"):
            self.log("== [警告] 该运行原本是真实模型运行，本次按 --mock 演练，产物不代表真实结果")
        if review_every:
            self.review_every = max(1, review_every)
        if max_rework is not None and max_rework != self.max_rework:
            # 显式传了才覆盖；否则沿用该 run 原本的上限（早期版本会被 _restore 静默盖掉，等于忽略命令行参数）
            self.log(f"== 本次续跑覆盖回流上限：{self.max_rework} -> {max_rework}")
            self.max_rework = max_rework
        if pause_after is not None:
            self.pause_after = set(pause_after)
        if from_checkpoint is not None:
            # 按检查点回放：比 --from <阶段> 更精确（同一阶段的多个轮次可分别定位）
            stage = self.restore_checkpoint(int(from_checkpoint))
            self._log_action("human_rework", stage, text=rework_reason, kind=issue_kind)
        elif from_stage:
            self._rewind(from_stage)
            self._log_action("human_rework", from_stage, text=rework_reason, kind=issue_kind)
        if from_stage or from_checkpoint is not None:
            # 人工打回某阶段同样算人工介入：已经触顶时若不追加预算，
            # 这一轮跑完评审就立刻判 needs_human —— 人工白救一轮。
            self._extend_budget_for_human(f"人工打回 {from_stage or '检查点'}")
        if feedback:
            items = [feedback] if isinstance(feedback, str) else list(feedback)
            target = feedback_target(self.cursor, from_stage)
            self.human_feedback.setdefault(target, []).extend(items)
            for item in items:
                self._log_action("human_directive", target, text=item, kind=issue_kind)
            self.log(f"== 人工意见已注入 [{target}]: {items}")

        self._refresh_env()
        self.status = "running"
        # 记下"上次停在哪个闸门"再清 paused_after：条件闸门是强控，续跑时必须重新判定
        # （见 `_execute` 开头）。用快照里的值而不是内存值 —— 内存值在这里就被清掉了。
        # 但**显式 --from / 检查点回放不算「原地续跑」**：用户已经决定重放某阶段，
        # 顶部若再拿旧产物复核（如设计闸门用旧方案重算阻断）会在重放开始前就再停一次，
        # 让暂停文案推荐的 `--from architect_plan --feedback` 永远走不到（真机 160609）。
        # 闸门会在重放阶段结束后走正常流程重新判定，不丢强控。
        replaying = bool(from_stage or from_checkpoint is not None)
        self._resume_gate_stage = (
            None if replaying else str(snap.get("paused_after") or "").strip() or None
        )
        self.paused_after = None
        self._t0 = time.time()
        self._persist()
        self.log(
            f"== 恢复 run {self.run_id}  cursor={self.cursor}  attempt={self.attempt}  "
            f"闸门={sorted(self.pause_after)}  repo={self.repo or '未提供'}"
        )
        self._execute()
        try:
            return self._finish()
        finally:
            self._stop_presence()

    def _rewind(self, from_stage: str) -> None:
        """回到指定阶段重跑：作废该阶段及其下游产物，并回退相关计数。"""
        if from_stage not in runstore.FLOW_ORDER:
            raise OrchestratorError(f"未知阶段: {from_stage}，可选 {runstore.FLOW_ORDER}")
        tail = runstore.FLOW_ORDER[runstore.FLOW_ORDER.index(from_stage) :]
        moved = runstore.archive_stages(self.run_dir, tail)  # type: ignore[arg-type]
        # 回退会丢掉实现产物，但「上一轮声明过哪些符号」要留一份 ——
        # 否则人工打回一次（--from dev / 页面「打回重跑本阶段」），返工退化检测就失去比对基准，
        # 「越改越少」再也查不出来。真机教训：run 20260924-185507 的 game_logic.py 在某一轮
        # 丢掉了 `__main__` 入口，因为没有基准，机制全程没吭声。
        stale = self.state.get("implementation")
        if isinstance(stale, dict) and self._symbol_set(stale):
            self.state["implementation_symbols_prev"] = sorted(self._symbol_set(stale))
        for stage in tail:
            self.state.pop(runstore.STAGE_STATE_KEY[stage], None)
        # 设计闸门的结论绑定在「即将重放/跳过的方案」上：重放后 _run_design_gate 会重判
        # （开头即 pop）；人工强制从下游重放则属于显式越过闸门，旧标记继续留着是脏状态。
        self.state.pop("design_gate_blocked", None)
        self.state.pop("design_gate_attempts", None)
        if from_stage in ("pm", "architect_assess", "architect_plan"):
            self.attempt = 0
            self.last_review_attempt = 0
            self.fixes = None
            self.rounds = []
        else:
            # 只重跑回流循环里的某一环：保留轮次，但让下一次评审立即生效
            self.last_review_attempt = 0
            self.rounds = [r for r in self.rounds if r.get("attempt", 0) < self.attempt]
        self.needs_human = False
        self.cursor = from_stage
        self.log(f"== 重跑：{from_stage}（作废旧产物 {len(moved)} 个，已归档到 {runstore.SUPERSEDED_DIR}/）")

    def restore_checkpoint(self, seq: int) -> str:
        """回放到指定检查点（按快照 seq 精确定位，作废其后全部产物）。返回回放到的阶段名。

        与 ``_rewind``（按阶段名作废整条尾巴）的区别：同一阶段在多轮迭代里会有多份快照，
        按 seq 能精确回到「某一轮的那一次」，而不是把该阶段的所有轮次一起作废。
        这是 LangGraph 式 checkpoint/time-travel 语义在本项目里的落地 —— 快照文件即 checkpoint，
        seq 即 checkpoint id。
        """
        run_dir = self.run_dir
        assert run_dir is not None
        ckpts = runstore.checkpoints(run_dir)
        target = next((c for c in ckpts if c["seq"] == seq and not c["superseded"]), None)
        if target is None:
            live = ", ".join(f"{c['seq']}:{c['stage']}" for c in ckpts if not c["superseded"])
            raise OrchestratorError(f"找不到检查点 seq={seq}（在存检查点：{live or '无'}）")
        stage = str(target["stage"])
        if stage not in flow.FLOW_ORDER:
            raise OrchestratorError(f"检查点 seq={seq} 的阶段 {stage} 不可回放（不是流程阶段）")
        # 严格大于：目标检查点自身保留（它就是回放到的状态点），只作废其后产物
        moved = runstore.archive_after_seq(run_dir, seq)
        for ckpt in ckpts:
            if ckpt["seq"] > seq and ckpt["state_key"]:
                self.state.pop(ckpt["state_key"], None)
        if stage in ("intake", "pm", "architect_assess", "architect_plan"):
            self.attempt = 0
            self.last_review_attempt = 0
            self.fixes = None
            self.rounds = []
        else:
            # 只回放回流循环里的某一环：保留轮次，但让下一次评审立即生效
            self.last_review_attempt = 0
            self.rounds = [r for r in self.rounds if r.get("attempt", 0) < self.attempt]
        self.needs_human = False
        self.cursor = stage
        self.paused_after = None
        self._log_action("checkpoint_restore", stage, text=f"seq={seq}")
        self.log(
            f"== 回放检查点 seq={seq}（{stage}）：作废其后产物 {len(moved)} 个，"
            f"已归档到 {runstore.SUPERSEDED_DIR}/"
        )
        return stage

    # ------------------------------------------------------------------ 收尾
    # ------------------------------------------------------------------ 交付落盘
    def _blank_delivery(self) -> dict:
        return {
            "delivered": False,
            # 判定通过、也确实去落盘了，但一条补丁都没能写出去。
            # 与「未达交付条件」不同：这是**该交付却交不出来**，必须让人看见。
            "wrote_nothing": False,
            "mode": None,
            "enabled": bool(DELIVER_ENABLED),
            "target": str(self.repo) if self.repo else None,
            "files": [],
            "skipped": [],
            # 只交付了一部分：有补丁没能套上，或有文件沙箱里有、目标目录里却没有。
            # 与 wrote_nothing 的区别是「写了但没写全」—— 页面不能显示成一个干净的 ✅。
            "partial": False,
            # 沙箱里物化成功、目标目录里却没有的文件（交付缺失的直接证据）
            "missing_vs_sandbox": [],
            "error": None,
            "reason": None,
        }

    def _deliver(self, verdict: str) -> dict:
        """**正式交付**：整条流水线跑完且判定通过时，把补丁写回目标目录。"""
        result = self._blank_delivery()
        if not DELIVER_ENABLED:
            result["reason"] = "交付已关闭（PIPELINE_DELIVER=0），产物仅保留在沙箱"
            return result
        if self.status != "done":
            result["reason"] = f"未达交付条件（status={self.status}）"
            return result
        if verdict != "pass":
            result["reason"] = (
                f"判定为 {verdict}，不把未通过的代码写入目标目录"
                f"（产物见 runs/{self.run_id}/verify/work）"
            )
            return result
        return self._write_target("deliver")

    # ---- 建议⑮：Verified Workspace —— 被验证字节的一等实体 ----
    @staticmethod
    def _sha256_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _freeze_verified_workspace(self, work: str, report: dict) -> dict | None:
        """verify pass 时把沙箱里**真正被验证过**的文件固化为 ``verified_workspace/``。

        只复制 ``materialized`` 列出的项目产物（不是整份沙箱：基座文件本就在用户仓库里），
        逐文件 sha256 进清单，整体再算一个 12 位摘要。之后兜底交付是「照清单复制字节」——
        与验证跑的对象逐字节相同，「验证对象 ≠ 交付对象」（snake-v2 缺文件事故）在结构上消失。
        """
        if not work or self.run_dir is None:
            return None
        src_root = Path(work)
        rels: list[str] = []
        for p in report.get("materialized") or []:
            rel = str(p or "").replace("\\", "/").strip()
            if rel and rel not in rels:
                rels.append(rel)
        if not rels:
            return None
        vdir = self.run_dir / "verified_workspace"
        # 重新通过验证时整体重建：verified_workspace 永远只代表「最近一次通过」。
        if vdir.exists():
            shutil.rmtree(vdir, ignore_errors=True)
        files_meta: list[dict] = []
        for rel in rels:
            src = src_root / rel
            if not src.is_file():
                continue
            dst = vdir / rel
            try:
                if vdir.resolve() not in dst.resolve(strict=False).parents:
                    continue
            except OSError:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            files_meta.append(
                {"path": rel, "sha256": self._sha256_file(dst), "size": dst.stat().st_size}
            )
        if not files_meta:
            return None
        digest = hashlib.sha1(
            json.dumps(files_meta, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:12]
        manifest = {
            "manifest_id": f"vm-{self.attempt}-{digest[:8]}",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "attempt": self.attempt,
            "verify_summary": report.get("summary") or "",
            "digest": digest,
            "files": files_meta,
        }
        runstore.write_json(vdir / "verified_manifest.json", manifest)
        self.state["verified_digest"] = digest
        self.log(f"        [verified] 已固化 {len(files_meta)} 个被验证文件（digest={digest}）")
        return manifest

    def _verified_manifest(self) -> dict | None:
        """读 last_good 指向的固化清单；新结构不存在（如 mock 或旧 run）时返回 None。"""
        lg = self.state.get("last_good") or {}
        name = str(lg.get("manifest_file") or "")
        if not name or self.run_dir is None:
            return None
        mp = self.run_dir / Path(name).name if Path(name).name == name else self.run_dir / name
        try:
            data = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("files") else None

    def _deliver_last_good(self) -> dict:
        """未通过时的**兜底交付**：把 verified_workspace 里被验证过的字节复制到目标目录。

        交付物明确标注「未经放行」。有它总比什么都没有强 —— 用户拿到的是唯一一个
        被机械证据证明过能跑的版本，而不是最后一轮（可能已改坏）的残次品。

        建议⑮后交付源不再是「某一个 dev 快照」，而是 verify pass 当时固化的工作区：
        复制前逐文件比对 sha256，清单里的文件缺失或被改动就**整体拒绝、一个字都不写**。
        """
        result = self._blank_delivery()
        result["mode"] = "last_good"
        if not DELIVER_ENABLED:
            result["reason"] = "交付已关闭（PIPELINE_DELIVER=0），产物仅保留在沙箱"
            return result
        if not self.repo:
            result["reason"] = "未指定目标目录（repo 为空）"
            return result
        manifest = self._verified_manifest()
        if manifest is None or self.run_dir is None:
            result["reason"] = "没有「验证通过」的固化工作区可兜底"
            return result
        vdir = self.run_dir / "verified_workspace"
        entries = [f for f in (manifest.get("files") or []) if isinstance(f, dict) and f.get("path")]
        if not entries:
            result["reason"] = "固化清单为空（verified_manifest.json 无文件记录）"
            return result

        repo = Path(self.repo).expanduser()
        try:
            root = Path(ROOT).resolve()
            resolved = repo.resolve(strict=False)
        except OSError as exc:
            result["error"] = f"目标路径无法解析：{exc}"
            return result
        # 防自伤：与 _write_target 同一道红线。
        if resolved == root or root in resolved.parents:
            result["error"] = f"拒绝交付：目标目录位于流水线目录内（{resolved}）"
            return result

        # ---- 先整体校验、后逐字节复制：任何一个文件缺失/越界/哈希不符都一个字不写 ----
        plans: list[tuple[str, Path, Path]] = []
        bad: list[str] = []
        for item in entries:
            rel = str(item["path"]).replace("\\", "/").strip()
            src = vdir / rel
            try:
                dest = (resolved / rel).resolve(strict=False)
            except OSError:
                bad.append(f"{rel}（路径无法解析）")
                continue
            if resolved != dest and resolved not in dest.parents:
                bad.append(f"{rel}（越出目标目录）")
                continue
            if not src.is_file():
                bad.append(f"{rel}（固化工作区中缺失）")
                continue
            actual = self._sha256_file(src)
            if str(item.get("sha256") or "") and actual != str(item["sha256"]):
                bad.append(f"{rel}（sha256 与清单不符，固化区被改动）")
                continue
            plans.append((rel, src, dest))
        if bad:
            result["reason"] = (
                "放弃兜底交付：固化工作区与清单不一致（" + "；".join(bad[:4])
                + f"，共 {len(bad)} 项）：验证对象已不完整或被改动，写出去无法保证是被验证过的那份。"
            )
            return result

        written: list[dict] = []
        for rel, src, dest in plans:
            dest.parent.mkdir(parents=True, exist_ok=True)
            backup = dest.with_suffix(dest.suffix + ".orig")
            if dest.is_file() and not backup.exists():
                shutil.copy2(dest, backup)
            shutil.copy2(src, dest)
            entry = {"path": rel, "written": str(dest)}
            if dest.is_file() and backup.exists():
                entry["backup"] = str(backup)
            written.append(entry)
        if not written:
            result["wrote_nothing"] = True
            result["reason"] = "固化清单无可交付文件，目标目录未改动"
            return result
        result["delivered"] = True
        result["files"] = written
        result["verified_digest"] = manifest.get("digest") or ""
        result["reason"] = (
            f"该版本逐字节复制自「最后一次通过运行验证」的固化工作区"
            f"（{manifest.get('manifest_id')}，{len(written)} 个文件；"
            "**未经人工放行**；本轮最终判定未通过）"
        )
        return result

    def _deliver_preview(self) -> dict:
        """人工审核闸门处的**预览物化**：先把交付物写进目标目录，让人工有东西可看再决定。

        为什么必须有：闸门要人工核对「交付物完整 / 核心路径走通 / 无明显错误」，
        而按原设计代码只在 pass 之后才落盘 —— 人面对的是**空目录**加一张只有勾选框的表，
        等于盲签。物化之后人工可以直接打开、运行、跑自己的测试再决定放行或打回。

        代价与兜底：文件会在**放行之前**出现在目标目录。若人工打回，下一轮 dev 重新
        生成补丁时 ``apply_all`` 会整份覆盖（已有文件先留 ``*.orig`` 备份），不留残码。
        """
        result = self._blank_delivery()
        if not DELIVER_ENABLED:
            result["reason"] = "交付已关闭（PIPELINE_DELIVER=0），产物仅保留在沙箱"
            return result
        if not self.repo:
            result["reason"] = "未指定目标目录（repo 为空）"
            return result
        return self._write_target("preview")

    @staticmethod
    def _delivery_digest(impl: dict, audit: dict) -> str:
        """交付指纹：这批补丁的**内容 + 落点**的稳定摘要。内容没变就是「同一批交付」。"""
        payload = {
            "edits": [
                {
                    "path": e.get("path"), "patch": e.get("patch"),
                    "mode": e.get("patch_mode"), "type": e.get("change_type"),
                    "symbol": e.get("target_symbol"), "anchor": e.get("anchor"),
                }
                for e in (impl.get("edits") or []) if isinstance(e, dict)
            ],
            "rows": [
                {
                    "path": r.get("path"), "symbol": r.get("symbol"), "status": r.get("status"),
                    "kind": r.get("patch_kind"), "mode_used": r.get("patch_mode_used"),
                    "symbol_span": r.get("symbol_span"), "anchor_span": r.get("anchor_span"),
                }
                for r in (audit.get("edits") or []) if isinstance(r, dict)
            ],
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]

    def _write_target(self, mode: str) -> dict:
        """落盘核心 —— 整条流水线**唯一**会写用户文件系统的地方。

        两个入口（``_deliver`` / ``_deliver_preview``）各自先过自己那道门禁，再走到这里。
        这里再兜一层安全检查：
          1) 每条补丁的目标路径必须收敛在 repo 内（挡掉 ``..`` 越界），**整体拒绝**不写一半；
          2) repo 不得指向流水线自身目录（含 runs/），防止把自己覆盖掉；
          3) 已有文件写前留 ``*.orig`` 备份（``apply_all`` 内部保证）。
        """
        result = self._blank_delivery()
        result["mode"] = mode
        if not self.repo:
            result["reason"] = "未指定目标目录（repo 为空）"
            return result
        impl = self.state.get("implementation") or {}
        audit = self.state.get("patch_audit") or {}
        if not audit.get("source_available"):
            result["reason"] = "没有可套用的补丁审计结果"
            return result

        repo = Path(self.repo).expanduser()
        try:
            root = Path(ROOT).resolve()
            resolved = repo.resolve(strict=False)
        except OSError as exc:
            result["error"] = f"目标路径无法解析：{exc}"
            return result
        # 防自伤：不允许把产物写进流水线自身目录（含 runs/）
        if resolved == root or root in resolved.parents:
            result["error"] = f"拒绝交付：目标目录位于流水线目录内（{resolved}）"
            return result

        # 越界检查：任何一条补丁路径落在 repo 之外都整体拒绝
        for edit in impl.get("edits") or []:
            if not isinstance(edit, dict):
                continue
            rel = str(edit.get("path") or "").strip()
            if not rel:
                continue
            try:
                dest = (resolved / rel).resolve(strict=False)
            except OSError as exc:
                result["error"] = f"拒绝交付：补丁路径无法解析（{rel}）：{exc}"
                return result
            if resolved != dest and resolved not in dest.parents:
                result["error"] = f"拒绝交付：补丁路径越出目标目录（{rel}）"
                return result

        # 重复交付闸：同一批补丁 + 同一目标 = 已经落过盘，直接复用上次结果。
        # 为什么需要：闸门的「预览物化」在人工放行**之前**就写了一遍，放行后的
        # 「正式交付」会拿同一份缓存审计再走一遍（续跑结束同理）。套用层已有幂等闸
        # （内容已在文件里就跳过），但那一层只能报「已写入 0 个文件」——
        # 落到页面/人工清单上会显示成「交付了 0 个文件」，看起来像失败。
        digest = self._delivery_digest(impl, audit)
        done = self.state.get("delivery_done") or {}
        if done.get("digest") == digest and done.get("target") == str(resolved):
            prev = [f for f in (done.get("files") or []) if isinstance(f, dict)]
            # 上次交付的文件还都在（用户没把目录清空）才算数；被删了就要老老实实重写。
            if prev and all(Path(str(f.get("written") or "")).exists() for f in prev):
                result["delivered"] = True
                result["files"] = list(prev)
                result["skipped"] = list(done.get("skipped") or [])
                result["reason"] = (
                    f"同一批补丁已经交付过（{done.get('mode') or '先前一次'}），"
                    "文件内容未变，不重复落盘"
                )
                self.log(f"  [交付] 跳过重复落盘（同一批补丁，{len(result['files'])} 个文件已在目标目录）")
                return result

        try:
            report = patches.apply_all(resolved, impl, audit, in_place=True)
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"落盘失败：{type(exc).__name__}: {exc}"
            self.log(f"  [交付] 落盘失败：{type(exc).__name__}: {exc}")
            return result
        result["files"] = report.get("files") or []
        result["skipped"] = report.get("skipped") or []
        tag = "交付预览（待人工确认）" if mode == "preview" else "交付"
        if not result["files"]:
            # **一条都没写出去，就绝不能报成「已交付」**。这正是 P0 的原始病症换了个形式
            # 回来：跑完看起来成功、目标目录却是空的。以前这里无条件 delivered=True，
            # 页面与人工清单于是显示「✅ 已交付：0 个文件」—— 比直接失败更糟，因为它
            # 让人以为拿到了代码。把跳过原因原样带出来，指明卡在哪：
            # 常见是补丁被判 unchecked（目标文件不存在 / 新建项目却给 modify），
            # 或给的是 unified diff（需手动 patch/git apply）。
            result["delivered"] = False
            result["wrote_nothing"] = True
            why = "；".join(
                str((s or {}).get("reason") or "") for s in result["skipped"][:4]
            ).strip("；")
            result["reason"] = (
                f"没有任何补丁可落盘，目标目录未改动（{len(result['skipped'])} 条被跳过）"
                + (f"：{why}" if why else "")
            )
            self.log(f"  [{tag}] 未写入任何文件：{result['reason']}")
            return result
        result["delivered"] = True
        # ---- 交付完整性核对 ----
        # 原先两条缺口都会让「残缺交付」显示成一个干净的 ✅：
        #   1) 有补丁被 skipped 但 files 非空 ⇒ delivered=True，页面/人工清单看不出少东西；
        #   2) 沙箱里物化成功的文件，目标目录里却没写成 ⇒ 跑的时候才炸。
        # 这两类都是「几轮判 pass、交付却跑不了」的落点，必须显式标出来。
        # 幂等命中（内容本就在目标文件里）不算残缺：预览物化已写过一遍，
        # 放行后的正式交付第二次必然命中 —— 不排除就会把正常的重复交付误标成部分交付。
        real_skipped = [s for s in result["skipped"] if not patches.is_benign_skip(s)]
        if real_skipped:
            result["partial"] = True
            why = "；".join(
                str((s or {}).get("reason") or "") for s in real_skipped[:3]
            ).strip("；")
            result["reason"] = (
                f"仅部分交付：{len(result['files'])} 个文件已写入，"
                f"{len(real_skipped)} 条补丁未能套用" + (f"：{why}" if why else "")
            )
        sandbox_files = {
            str(p).replace("\\", "/")
            for p in ((self.state.get("verify_report") or {}).get("materialized") or [])
            if str(p).strip()
        }
        delivered_files = {
            str(f.get("path") or "").replace("\\", "/")
            for f in result["files"]
            if isinstance(f, dict) and str(f.get("path") or "").strip()
        }
        missing = sorted(sandbox_files - delivered_files)
        if missing:
            result["partial"] = True
            result["missing_vs_sandbox"] = missing
            extra = f"；另有 {len(missing)} 个文件沙箱里有、目标目录却没有（{', '.join(missing[:3])}）"
            result["reason"] = (result["reason"] or "交付不完整") + extra
        if result["partial"]:
            self.log(f"  [{tag}] 部分交付：{result['reason']}")
        # 记下指纹：下次拿同一批补丁再交付（预览 → 放行）时直接跳过，不重复落盘。
        self.state["delivery_done"] = {
            "digest": digest,
            "target": str(resolved),
            "mode": mode,
            "files": result["files"],
            "skipped": result["skipped"],
        }
        self.log(
            f"  [{tag}] 已写入 {len(result['files'])} 个文件 → {resolved}"
            + (f"（跳过 {len(result['skipped'])} 项）" if result["skipped"] else "")
        )
        return result

    def _finish(self) -> RunResult:
        assert self.run_dir is not None
        self.elapsed_s = round(self.elapsed_s + (time.time() - self._t0), 1)
        if self.status == "running":
            self.status = "done"
        switches = sum(1 for c in self.calls if c.get("switched"))
        review_verdict = (self.state.get("review") or {}).get("verdict")
        if self.status == "paused":
            verdict = "paused"
        elif self.needs_human:
            verdict = "needs_human"
        elif self.mode == "only":
            verdict = review_verdict or "partial"
        else:
            verdict = review_verdict or "needs_human"

        # 交付落盘：把沙箱里验证通过的补丁写回用户目标目录。
        # 必须在 summary 之前跑：落盘失败要能把 needs_human 顶上去，让页面如实标出来。
        delivery = self._deliver(verdict)
        # 人工审核闸门：先把交付物**预览物化**到目标目录，让人工有东西可看再决定放行/打回。
        # 否则人面对的是空目录 + 一张只有 4 个勾选项的表，等于盲签。
        if self.status == "paused" and self.paused_after == "human_review":
            delivery = self._deliver_preview()
            self.state["delivery_preview"] = delivery
        # 未通过又没交付（触顶 / needs_human）时兜底：把最后一个验证通过的版本物化出去。
        # 否则跑满 8 轮的结果是「什么都没有」，而可用版本就躺在快照里。
        if not delivery.get("delivered") and self.status == "done" and verdict != "pass":
            fallback = self._deliver_last_good()
            if fallback.get("delivered"):
                delivery = fallback
        # 「该交付却一条都没写出去」「只写了一部分」和报错同等严重。
        # partial 尤其隐蔽：页面显示 ✅ 已交付 N 个文件，看着很正常，
        # 实际少了几条补丁（或沙箱里有的文件没写成） —— 人只有真跑才发现。
        if delivery.get("error") or delivery.get("wrote_nothing") or delivery.get("partial"):
            self.needs_human = True

        summary = {
            "run_id": self.run_id,
            "status": self.status,
            "mode": self.mode,
            "mock": isinstance(self.client, MockClient),
            "repo": str(self.repo) if self.repo else None,
            "cursor": self.cursor,
            "paused_after": self.paused_after,
            "review_every": self.review_every,
            "verdict": verdict,
            "needs_human": self.needs_human,
            "delivery": delivery,
            "attempts": self.attempt,
            "rounds": len(self.rounds),
            "round_details": self.rounds,
            "wall_s": self.elapsed_s,
            "model_switches": switches,
            "grounding_warnings": self.grounding_warnings,
            "total_load_s": round(sum(c.get("load_s", 0) for c in self.calls), 1),
            "total_prompt_tokens": sum(c.get("prompt_tokens", 0) for c in self.calls),
            "total_output_tokens": sum(c.get("output_tokens", 0) for c in self.calls),
            "human_feedback": self.human_feedback,
            "calls": [
                {
                    "stage": c["stage"],
                    "tag": c["tag"],
                    "think": c["think"],
                    "num_ctx": c["num_ctx"],
                    "attempt": c.get("attempt", 1),
                    "switched": c.get("switched"),
                    "load_s": c.get("load_s"),
                    "wall_s": c.get("wall_s"),
                    "prompt_tokens": c.get("prompt_tokens"),
                    "output_tokens": c.get("output_tokens"),
                    "prompt_over_budget": c.get("prompt_over_budget"),
                    "truncated": c.get("truncated"),
                    "human_feedback_used": c.get("human_feedback_used"),
                }
                for c in self.calls
            ],
            "artifacts": {
                "scope": self.state.get("scope"),
                "assessment": self.state.get("assessment"),
                "plan": self.state.get("plan"),
                "implementation": self.state.get("implementation"),
                "plan_audit": self.state.get("plan_audit"),
                "implementation_audit": self.state.get("implementation_audit"),
                "patch_audit": self.state.get("patch_audit"),
                "test_report": self.state.get("test_report"),
                "test_audit": self.state.get("test_audit"),
                "review": self.state.get("review"),
            },
        }

        # 问题记录：状态里的原始信号 → 结构化 issues（jsonl 给机器，md 给人）
        collected = issues_mod.collect_issues(self._snapshot(), self.run_id)
        summary["issues"] = issues_mod.summarize(collected)
        issues_mod.write_issues(self.run_dir, self.run_id, collected)

        if self.mode == "full":
            self._persist()
        if self.status == "done":
            runstore.write_json(self.run_dir / runstore.SUMMARY_NAME, summary)
        self._write_handoff(summary)

        if self.unload_at_end and not isinstance(self.client, MockClient):
            # **收尾不许把运行带崩**（真机 `20260928-095848`）：ollama 在返工轮中途挂掉时，
            # 这一步的 `ps()` 抛 `OllamaError`，异常从 `resume()` 一路冒到 cli 顶层 ——
            # 进程带着 traceback 退出，而 state 停在 paused/needs_human、页面看着像"还在跑"。
            # 这与 `_execute` 里那条"任何阶段异常都不许让运行隐身死掉"是**同一条纪律**，
            # 只是当时只护住了阶段循环，漏了收尾。卸载只是释放显存的辅助动作，失败不该改结论。
            try:
                for model in self.client.ps():
                    name = model.get("name", "")
                    if name:
                        self.client.unload(name)
                self.log("== 已卸载全部模型（显存释放）")
            except Exception as exc:  # noqa: BLE001
                self.log(
                    f"== [收尾] 卸载模型失败（**不影响本次结论**）："
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                )

        self.log(
            f"== {'暂停' if self.status == 'paused' else '完成'}: verdict={summary['verdict']} "
            f"attempts={self.attempt} 切换={switches}次 wall={self.elapsed_s}s 产物={self.run_dir}"
        )
        return RunResult(
            run_id=self.run_id,
            run_dir=self.run_dir,
            summary=summary,
            artifacts=summary["artifacts"],
            paused=self.status == "paused",
            paused_after=self.paused_after,
        )

    def _write_handoff(self, summary: dict) -> None:
        """《待人工确认清单》—— 把机器无法定论的事项集中成一份给人看的清单。"""
        assert self.run_dir is not None
        scope = self.state.get("scope") or {}
        assessment = self.state.get("assessment") or {}
        review = self.state.get("review") or {}
        test = self.state.get("test_report") or {}

        def section(title: str, items: list[Any]) -> list[str]:
            if not items:
                return []
            return [f"## {title}", *[f"- {item}" for item in items], ""]

        lines = [
            f"# 待人工确认清单 — run {self.run_id}",
            "",
            f"- 状态：{self.status}"
            + (f"（停在 `{self.paused_after}` 之后）" if self.paused_after else ""),
            f"- 判定：{summary.get('verdict')}；迭代 {self.attempt} 轮（评审 {len(self.rounds)} 次）",
            f"- 需求：{(self.requirement or '').strip().splitlines()[0][:120] if self.requirement else ''}",
            f"- 仓库：{summary.get('repo') or '未提供'}",
            f"- 问题记录：{summary.get('issues', {}).get('total', 0)} 条"
            f"（阻断 {summary.get('issues', {}).get('blockers', 0)} 条）→ 见 `issues.md` / `issues.jsonl`",
            "",
        ]
        # 交付落盘结果：用户最关心的一行。
        # 历史坑：产物长期只停在 runs/<id>/verify/work 沙箱，目标目录始终是空的，而清单里
        # 对此只字不提 —— 跑完看起来「成功」，其实一行代码都没到用户手里。
        delivery = summary.get("delivery") or {}
        if delivery.get("delivered"):
            files = delivery.get("files") or []
            if delivery.get("mode") == "preview":
                head = (
                    f"- 👀 交付预览：{len(files)} 个文件已写入 `{delivery.get('target')}`"
                    "（人工审核闸门前物化，供你打开/运行核对；放行后正式交付，打回则下一轮整份覆盖）"
                )
            elif delivery.get("mode") == "last_good":
                head = (
                    f"- ⚠ **兜底交付（未经放行）**：{len(files)} 个文件已写入 "
                    f"`{delivery.get('target')}` —— 取自最后一次**通过运行验证**的快照，"
                    "本轮最终判定未通过，请自行核对后再使用。"
                )
            else:
                head = f"- ✅ 已交付：{len(files)} 个文件写入 `{delivery.get('target')}`"
            # 交付成功也可能带 reason（例：同一批补丁已交付过、兜底版本未经放行），
            # 一并显示出来，免得「为什么这次没重写文件」变成一个要翻代码才能懂的谜。
            note = [f"  - 说明：{delivery['reason']}"] if delivery.get("reason") else []
            lines += [
                head,
                *[f"  - `{f.get('path')}`" for f in files[:20]],
                *note,
                "",
            ]
        elif delivery.get("error"):
            lines += [f"- ❌ 交付失败：{delivery['error']}", ""]
        elif delivery.get("reason"):
            lines += [f"- ⚠ 未交付：{delivery['reason']}", ""]
        # 需求补强的待确认项：人工「快速扫一眼」的入口（下游在人工未确认时按建议取值推进）。
        # **一条列表**，不再分「缺失要素」与「澄清问题」—— 那两个列表本就是同一主题的重复
        # 生成源（真机 20260925-140707 里「游戏分辨率」与「游戏窗口尺寸要求」各出一条）。
        # 给不出具体取值的条目单独标出来：那才是真正需要人拍板的东西。
        intake = self.state.get("intake") or {}
        if intake:
            lines += section(
                "需求补强：待确认项（未确认即按建议取值推进）",
                [
                    f"[{row.get('importance')}] {row.get('element')} → "
                    + (
                        f"建议取值：{row.get('default_assumption')}"
                        f"（{row.get('why') or '未说明影响'}）"
                        if str(row.get("default_assumption") or "").strip()
                        else "**未给出建议取值，需人工裁决**"
                        f"（{row.get('why') or '未说明影响'}）"
                    )
                    for row in prompts.intake_items(intake)
                ],
            )
            for warn in self.state.get("intake_warnings") or []:
                lines += [f"- ⚠ 需求补强自检：{warn}", ""]
        for stage, warn in [(w["stage"], w["paths"]) for w in self.grounding_warnings]:
            lines += section(f"未接地的路径（{stage}，未在检索片段中出现，可能是新增文件或臆造）", warn)
        audit = self.state.get("implementation_audit") or {}
        lines += section(
            "方案任务未被补丁覆盖（覆盖审计的机械核对结果）",
            list(audit.get("missing") or []),
        )
        lines += section("开发声明未实现的任务", list(audit.get("declared_not_implemented") or []))
        # 被裁掉的补丁必须让人看到 —— 裁剪是为了"别被一个修不掉的门永远拦住"，
        # 不是"把问题藏起来"。这里与上面的「补丁机械校验未通过」并列展示。
        lines += section(
            "已移除的补丁（定位失败：anchor/符号与原文对不上，物理上套用不了）",
            list(self.state.get("pruned_patches") or []),
        )
        # 逐项验收：**人工最终审核要的就是这张表** —— 哪条修好了、哪条没有、哪条压根没能核对。
        lines += section(
            "逐项验收（每条修复项 ↔ 本轮机械执行结果；「无从核对」= 没有命令能证明它）",
            tasktype.render_defect_verdicts_markdown(self.state.get("defect_verdicts") or []),
        )
        patch_audit = self.state.get("patch_audit") or {}
        if patch_audit.get("source_available"):
            lines += section(
                "补丁机械校验未通过（这些补丁在修好前不能算完成）",
                [f"{patches.STATUS_CN.get(r.get('status'), r.get('status'))}：{r.get('symbol') or r.get('path')}"
                 for r in patch_audit.get("edits") or [] if r.get("status") != "ok"],
            )
            patch_dir = self.run_dir / "patches"
            files = sorted(p.name for p in patch_dir.glob("*.patch")) if patch_dir.exists() else []
            lines += section("已生成的可套用补丁", [f"`patches/{name}`" for name in files])
        # 交付证据表：验收标准 ↔ 用例 ↔ 执行结果 ↔ 红线/未验证项。人审要的材料是散的
        # （PM/测试/验证各一份产物），这张表是**唯一一处把它们对齐**的地方。
        ev = evidence.delivery_evidence(
            self.state.get("scope"),
            self.state.get("test_report"),
            self.state.get("verify_report"),
            self.state.get("rule_findings"),
        )
        ev_md = evidence.render_markdown(ev)
        if ev_md:
            lines += [ev_md, ""]
        lines += section(
            "被本轮机械证据证伪、已不再回灌的旧阻断项",
            list(self.state.get("refuted_blockers") or []),
        )
        lines += section(
            "同一 seq 的多份产物（两代混存：看「最新产物」易读错）",
            list(self.state.get("duplicate_stage_seqs") or []),
        )
        if self.state.get("rule_load_notes"):
            lines += section(
                "规则库加载问题（不判负，但对应的检查可能没生效）",
                list(self.state["rule_load_notes"]),
            )
        lines += section(
            "被机制强制放行的评审（返工项全需外部确认，需人工复核是否真的可以接受）",
            [
                f"第 {r.get('attempt')} 轮：" + "；".join(r.get("required_fixes_external") or [])
                for r in self.rounds
                if r.get("forced_pass")
            ],
        )
        # residual_risks 现为 {issue, reason, impact}；兼容早期运行的裸字符串。
        # 带上 impact，人工才能判断这条风险要不要拦住交付。
        risk_lines: list[str] = []
        for risk in review.get("residual_risks") or []:
            if isinstance(risk, dict):
                text = str(risk.get("issue") or "")
                if risk.get("impact"):
                    text += f"（影响：{risk['impact']}）"
                risk_lines.append(text)
            else:
                risk_lines.append(str(risk))
        lines += section("评审残留风险（residual_risks）", risk_lines)
        lines += section("评审阻断项（blockers）", list(review.get("blockers") or []))
        lines += section("必改项（required_fixes）", list(review.get("required_fixes") or []))
        # coverage_gaps 现为 {gap, reason, impact}；兼容早期运行的裸字符串。
        # 带上 impact，人工才能判断这条缺口要不要为它停下来。
        gap_lines: list[str] = []
        for gap in test.get("coverage_gaps") or []:
            if isinstance(gap, dict):
                text = str(gap.get("gap") or "")
                if gap.get("impact"):
                    text += f"（影响：{gap['impact']}）"
                gap_lines.append(text)
            else:
                gap_lines.append(str(gap))
        lines += section("测试未覆盖（coverage_gaps）", gap_lines)
        # 未决项必须带上 PM 给的默认假设：人工要能一眼看出「流程实际是按什么往下跑的」
        qs = [q for q in (scope.get("open_questions") or []) if isinstance(q, dict)]
        if qs:
            lines += [
                "## PM 未决项与默认假设（下游已按此推进，需人工确认）",
                "",
                f"> 完整 PRD 见本运行目录下的 `{runstore.PRD_NAME}`。",
                "",
            ]
            for q in qs:
                ans = str(q.get("assumed_answer") or "（未给出）").strip()
                rec = str(q.get("recommendation") or "").strip()
                line = f"- {q.get('question')} → **默认按「{ans}」执行**"
                if rec:
                    line += f"（PM 建议：{rec}）"
                lines.append(line)
            lines.append("")
        lines += section("需求未决信息（PM unknowns）", list(scope.get("unknowns") or []))
        lines += section("PM 澄清问题", list(scope.get("clarifying_questions") or []))
        # 架构不确定项是结构化的 {issue, assumption, confidence}：渲染成「疑点 → 推测（可信度）」
        # 一行，人工才能分辨哪条是模型在猜、哪条是确实拿不到依据。
        uncertainties: list[str] = []
        for item in assessment.get("uncertainties") or []:
            if isinstance(item, dict):
                text = str(item.get("issue") or "").strip()
                assumption = str(item.get("assumption") or "").strip()
                confidence = str(item.get("confidence") or "").strip()
                if assumption:
                    text += f" → 推测：{assumption}"
                if confidence:
                    text += f"（可信度 {confidence}）"
                uncertainties.append(text)
            else:
                uncertainties.append(str(item))
        lines += section("架构师不确定项", uncertainties)
        lines += [
            "## 人工操作方式",
            "",
            "```powershell",
            f"python -m pipeline.cli --resume {self.run_id}                 # 从暂停处继续",
            f"python -m pipeline.cli --resume {self.run_id} --from dev       # 打回开发重跑",
            f'python -m pipeline.cli --resume {self.run_id} --from architect_plan --feedback "方案里不要动 config.py"',
            "python -m pipeline.server --port 8787                        # 图形化操作页面",
            "```",
            "",
            "也可以直接编辑 `runs/%s/NN-<阶段>.json` 里的 `artifact`，续跑时以文件为准。" % self.run_id,
        ]
        (self.run_dir / runstore.HANDOFF_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
