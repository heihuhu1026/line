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
from . import patches, prd, presence, prompts, retrieval, runstore
from . import semantics
from . import verify as verify_mod
from .budget import estimate_tokens, fit_prompt
from .config import (
    CHARS_PER_TOKEN,
    CODE_BUDGET,
    DELIVER_ENABLED,
    DEV_CONTENT_REPAIR_TRIES,
    DEV_TWO_PASS,
    HUMAN_REVIEW_GATE,
    HUMAN_REWORK_BUDGET,
    HUMAN_REWORK_TOPUP_MAX,
    INTAKE_PAUSE_ON_GAPS,
    LSP_ENABLED,
    MAX_REWORK_ROUNDS,
    MAX_TOTAL_TOKENS,
    MAX_WALL_S,
    REWORK_STAGNATION_LIMIT,
    PAUSE_ON_OPEN_QUESTIONS,
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

from .ollama_client import MockClient, OllamaClient
from .retrieval import Excerpt
from .schemas import STAGE_SCHEMAS

# 人工可指定的阶段白名单：真源在 pipeline/flow.py（新增阶段只改那一处即可，cli/server 都从它派生）
ONLY_STAGES = list(flow.ONLY_STAGES)

# 游标指向"下一步"时可能停在非模型步骤（retrieve）或已结束，人工意见需要一个真实阶段作落点
CURSOR_FEEDBACK_TARGET = {"retrieve": "architect_assess", "done": "dev"}


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
        pause_on_open_questions: bool = PAUSE_ON_OPEN_QUESTIONS,
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
        self.elapsed_s = 0.0
        self.trace_enabled = os.getenv("PIPELINE_TRACE", "1") != "0"
        self._t0 = time.time()

    # ------------------------------------------------------------------ 埋点落盘
    def _record(self, stage: str, artifact: Any, meta: dict, request_preview: str) -> None:
        self._seq += 1
        assert self.run_dir is not None
        payload = {"stage": stage, "meta": meta, "artifact": artifact, "request_preview": request_preview[:4000]}
        runstore.write_json(self.run_dir / f"{self._seq:02d}-{stage}.json", payload)
        with (self.run_dir / "llm-calls.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"stage": stage, **meta}, ensure_ascii=False) + "\n")

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
    ) -> Any:
        spec = STAGE_MODELS[stage]
        resident = self._load_preview(spec.tag)
        sched = self.client.ensure_exclusive(spec.tag)
        # 人工意见/人工事实/覆盖审计必须被模型看到，不能被"从尾部截断"吃掉
        pinned = [p for p in (pin or []) if p]
        extra = self.human_feedback.get(stage)
        if extra:
            pinned.append(prompts.human_feedback_block(extra))
        if stage != "pm":  # PM 是第一个阶段，此时还没有人工输入
            block = prompts.human_facts_block(self._human_facts())
            if block:
                pinned.append(block)
        # system 提示同样占上下文，必须从预算里扣掉
        system = prompts.system_prompt(stage, self.project_type)
        budget = max(spec.prompt_token_budget - estimate_tokens(system), 400)
        user, truncated = fit_prompt([p for p in parts if p], budget, pin=pinned)
        self.log(f"  [{stage}] {spec.tag} ctx={spec.num_ctx} think={spec.think} 切换={sched['switched']}")
        t0 = time.time()
        data, meta = self.client.chat_json(spec, system, user, STAGE_SCHEMAS[stage])
        # 记录用素材：完整 system/user 与模型原始输出（含 thinking），写进 traces.jsonl，不进 llm-calls.jsonl
        raw_text = str(meta.pop("_raw_text", ""))
        raw_thinking = str(meta.pop("_raw_thinking", ""))
        failed_attempts = meta.pop("_failed_attempts", []) or []
        meta.update(
            {
                "stage": stage,
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
        self._record(stage, data, meta, user)
        self._record_trace(stage, system, user, raw_text, raw_thinking, failed_attempts, meta)
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
        for key, field in self._PATH_SPECS.get(stage, []):
            items = artifact.get(key)
            if not isinstance(items, list):
                continue
            if field == "items":
                # 元素是自由文本：只挑出其中的路径片段，纯描述性文字自然被忽略
                for entry in items:
                    if isinstance(entry, str):
                        paths.extend(self._PATHLIKE_RE.findall(entry))
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if field == "path" and item.get("path"):
                    paths.append(str(item["path"]))
                elif field == "target_files":
                    paths.extend(str(p) for p in (item.get("target_files") or []))
                elif isinstance(item.get(field), str):
                    # 其它字段名一律当「自由文本」：从中抽出形似路径的片段
                    # （automated_commands[].command 这类）
                    paths.extend(self._PATHLIKE_RE.findall(str(item[field])))
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

    def _grounded_call(
        self,
        stage: str,
        parts: list[str],
        note: str | None = None,
        pin: list[str] | None = None,
    ) -> Any:
        """带事实接地的阶段调用。

        产物里出现的路径必须能在检索池 / 需求原文 / 方案清单里找到依据；找不到就先带
        纠正说明重试一次，仍不合格则记入 grounding_warnings（issues 里是**阻断级**）。

        真机教训：编造的路径不会自己消失 —— 它会被下游当成「上游结论里已有的依据」
        承接下去（assess 编出 pipeline/db/*，方案阶段接着把「数据库查询结果」写进验收标准）。
        """
        data = self._call(stage, parts, note=note, pin=pin)
        if not self._grounding_enabled(stage):
            return data
        bad = self._ungrounded(data, stage)
        if not bad:
            return data
        self.log(f"        [事实校正] {stage} 以下路径无依据，重试一次: {bad[:5]}")
        retry_parts = parts + [prompts.fact_correction_block(bad, has_code=bool(self.pool))]
        retry_note = f"{note}+grounding-retry" if note else f"{stage}-grounding-retry"
        data = self._call(stage, retry_parts, note=retry_note, pin=pin)
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
        used = estimate_tokens(prompts.system_prompt(stage, self.project_type))
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
        per_file_chars = int(STAGE_PER_FILE_TOKENS.get(stage, PER_FILE_TOKENS) * CHARS_PER_TOKEN)
        used = 0
        kept: list[Excerpt] = []
        for exc in self.pool:
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
        """补强产物里 importance=high 的待确认项 —— 建议取值猜错代价最高的那些。

        走 ``prompts.intake_items`` 而不是直接读某个字段：待确认项已合并为单一列表，
        但旧运行（2026-09-25 之前）的产物里是 missing_elements + clarifying_questions
        两个字段，这个读取口负责两种形状都认。
        """
        art = self.state.get("intake") or {}
        return [
            f"{row.get('element')}（建议取值：{row.get('default_assumption') or '未给出，需人工填写'}）"
            for row in prompts.intake_items(art)
            if str(row.get("importance")) == "high"
        ]

    def _stage_pm(self, requirement: str) -> Any:
        # 复用上游补强产物：PM 不必再重新解析原始需求。
        # 人工裁决已并回补强产物本身（final_decision），这里整份注入即可。
        self.state["scope"] = self._call("pm", prompts.parts_pm(requirement, self.state.get("intake")))
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
        return self.state["plan"]

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
        prev = self.state.get("implementation") or {}
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
        if DEV_TWO_PASS and (code.strip() or current.strip()):
            # 第一遍：只铺辅助函数（脚手架），不动主函数体
            p1 = self._grounded_call(
                "dev",
                prompts.parts_dev(
                    requirement, scope, assessment, plan, code, fixes, prev_summary,
                    dev_pass=2, verify=verify, current_code=current,
                ),
                note="dev 第一遍·脚手架",
                pin=plan_pin,
            )
            # 第二遍：带第一遍产物回填主函数体
            p2 = self._grounded_call(
                "dev",
                prompts.parts_dev(
                    requirement, scope, assessment, plan, code, fixes, prev_summary,
                    dev_pass=3, pass1_edits=p1.get("edits"), verify=verify, current_code=current,
                ),
                note="dev 第二遍·回填",
                pin=plan_pin,
            )
            merged = self._merge_dev(p1, p2)
            repair_kwargs: dict[str, Any] = {"dev_pass": 3, "pass1_edits": p1.get("edits")}
        else:
            merged = self._grounded_call(
                "dev",
                prompts.parts_dev(
                    requirement, scope, assessment, plan, code, fixes, prev_summary,
                    verify=verify, current_code=current,
                ),
                pin=plan_pin,
            )
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
        for attempt in range(1, DEV_CONTENT_REPAIR_TRIES + 1):
            problems = self._invalid_new_files(merged)
            if not problems:
                # 便宜的字面检查干净了，再上贵一档的语义检查（物化 + pyright）。
                # 顺序不能反：语法残片会让 pyright 报一堆连锁错误，淹没真正的问题。
                problems = self._semantic_problems(merged)
            if not problems:
                break
            self.log(
                f"        [自检] 发现问题 {len(problems)} 处 → 带问题重问 dev"
                f"（第 {attempt}/{DEV_CONTENT_REPAIR_TRIES} 次）"
            )
            again = self._grounded_call(
                "dev",
                prompts.parts_dev(
                    requirement, scope, assessment, plan, code, fixes, prev_summary,
                    verify=verify, repair=problems, current_code=current, **repair_kwargs,
                ),
                note=f"dev 重出·内容不合法（第 {attempt} 次）",
                pin=plan_pin,
            )
            merged = self._apply_repair(merged, again)
            patches.normalize_implementation(merged)
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
        # 兜底：循环里若一直卡在字面检查上（`_invalid_new_files` 有问题时不会走到语义检查），
        # 评审就少了这条机械证据。这里补跑一次（幂等，代价是几秒物化 + pyright）。
        if not self.state.get("semantic_audit"):
            self._semantic_problems(merged)
        self.state["implementation"] = self._merge_impl_across_rounds(prev, merged)
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

        mod_key = lambda e: (
            _norm(e.get("path")),
            str(e.get("target_symbol") or ""),
            str(e.get("patch_mode") or ""),
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

        # 1) modify/其它非 add 非 delete 的 edit：按 (path, 符号, 模式) 累积，cur 覆盖 prev
        mod_kept: dict[tuple, dict] = {}
        for e in _lst(prev, "edits") + _lst(cur, "edits"):
            if not (isinstance(e, dict) and e.get("path") and not _is(e, "add") and not _is(e, "delete")):
                continue
            if _norm(e.get("path")) in deleted_paths:
                continue
            mod_kept[mod_key(e)] = e  # cur 在后遍历，自然覆盖 prev

        # 2) add 块：按路径做「符号并集」——本轮没覆盖的符号保留，避免越改越少
        add_kept: list[dict] = []
        for e in _lst(prev, "edits"):
            if not (_is(e, "add") and e.get("path")):
                continue
            p = _norm(e.get("path"))
            if p in deleted_paths:
                continue
            if p in cur_add_syms and _top_symbols(e.get("patch")) <= cur_add_syms[p]:
                continue  # 本轮已完整覆盖该块定义的全部符号 -> 丢弃旧的，避免重复定义
            add_kept.append(e)
        add_kept += [e for e in _lst(cur, "edits") if _is(e, "add") and e.get("path") and _norm(e.get("path")) not in deleted_paths]

        # 3) delete 块：只保留本轮的（上轮删过的文件，本轮若又 add 即重建，无需再删）
        del_kept = [e for e in _lst(cur, "edits") if _is(e, "delete") and e.get("path")]

        out: dict[str, Any] = dict(cur)  # 以本轮为准继承 summary/deviations/self_checks/...
        out["edits"] = list(mod_kept.values()) + add_kept + del_kept

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

    def _current_code_text(self) -> str:
        """当前项目里**已有的代码正文** —— 返工时开发必须看到的东西。

        为什么必须有：`_code_text()` 切的是**检索池**，而新建项目的池是空的 —— 于是开发
        写完 2.2KB 代码后，下一轮一个字都看不到，只能照着方案**重新发明**这些文件。
        真机 run 20260924-235001 因此连栽 8 轮：补错 import（`from snake import Food`）、
        丢掉入口点、把已经跑通的实现改坏，返工项也一直修不掉。

        来源优先「物化后的真实文件」（verify 沙箱正是上一轮的真实产物，最接近仓库现状），
        取不到时退回实现产物里的补丁正文。按 dev 的代码预算裁剪，且**上一轮改动过的文件
        排在最前** —— 预算不足时至少保证那些可见。
        """
        edits = [
            e
            for e in ((self.state.get("implementation") or {}).get("edits") or [])
            if isinstance(e, dict)
        ]
        touched = {str(e.get("path") or "").replace("\\", "/") for e in edits}
        candidates: list[tuple[str, str]] = []
        if self.run_dir is not None:
            work = self.run_dir / "verify" / "work"
            if work.is_dir():
                for path in sorted(p for p in work.rglob("*") if p.is_file()):
                    if "__pycache__" in path.parts or path.suffix.lower() not in _CURRENT_CODE_SUFFIXES:
                        continue
                    try:
                        text = path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    if text.strip():
                        candidates.append((str(path.relative_to(work)).replace("\\", "/"), text))
        if not candidates:
            for edit in edits:
                body = str(edit.get("patch") or "")
                rel = str(edit.get("path") or "").strip()
                if body.strip() and rel:
                    candidates.append((rel, body))
        candidates.sort(key=lambda kv: (kv[0] not in touched, kv[0]))

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

    def _own_module_names(self) -> set[str]:
        """本项目自己的模块名（补丁里的文件 + 仓库顶层条目）—— 这些 import 不该被判成缺依赖。"""
        names = {
            Path(str(e.get("path") or "")).stem.lower()
            for e in ((self.state.get("implementation") or {}).get("edits") or [])
            if isinstance(e, dict)
        }
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

    def _invalid_new_files(self, impl: Any) -> list[str]:
        """机械自检：新增文件的内容是否合法（写残 / 未闭合 / 空）**且依赖装不装得上**。

        只看 `change_type == "add"` 且给的是**整份内容**的补丁；diff 形态取不全文，
        交给补丁机械校验与运行验证去管。

        两类问题都属「可机械判定、模型无法自查」：模型既看不到编码层（裸 CR 已经被归一）
        也不知道运行环境里装了什么包，所以在 dev 阶段把事实回灌给它，远好过跑完一整轮再回炉。
        """
        own = self._own_module_names()
        out: list[str] = []
        for edit in (impl or {}).get("edits") or []:
            if not isinstance(edit, dict) or str(edit.get("change_type") or "") != "add":
                continue
            patch = str(edit.get("patch") or "")
            if not patch.strip() or patches.DIFF_RE.search(patch):
                continue
            path = str(edit.get("path") or "")
            body = patches._new_file_body(patch)
            label = f"`{path}`（符号 {edit.get('target_symbol') or '-'}）"
            problem = patches.check_new_file_content(body, path)
            if problem:
                out.append(f"{label}：{problem}")
            for item in patches.unavailable_imports(body, path, own):
                out.append(f"{label}：{item}")
        return out

    @staticmethod
    def _apply_repair(merged: dict, again: Any) -> dict:
        """用重出的那一版**替换**对应 (path, target_symbol) 的补丁。

        刻意不走 `_merge_dev`：重出时 anchor / patch_mode 很可能与上一版不同，
        按「后写覆盖」的键合并会同时留下两条同符号补丁（合并写新文件时会得到两份定义）。
        """
        fresh = {
            (str(e.get("path") or ""), str(e.get("target_symbol") or "")): e
            for e in ((again or {}).get("edits") or [])
            if isinstance(e, dict)
        }
        if not isinstance(merged, dict) or not fresh:
            return merged
        merged["edits"] = [
            fresh.get((str(e.get("path") or ""), str(e.get("target_symbol") or ""))) or e
            for e in merged.get("edits") or []
        ]
        return merged

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
        for e in _lst(p1, "edits") + _lst(p2, "edits"):
            if isinstance(e, dict) and e.get("path") and e.get("target_symbol"):
                # 同 (path, 符号, 模式, anchor) 视为「同一处改动的两次尝试」，后写（第二遍）覆盖先写。
                # 第二遍分片重构时会对同一主函数输出多条 replace_span（锚点各不相同），必须带上 anchor 区分，
                # 否则会被去重成一条、其余分片丢失，主函数重组不完整。
                merged[(e["path"], e["target_symbol"], e.get("patch_mode"), e.get("anchor") or "")] = e
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
        covered: list[str] = []
        missing_symbols: list[str] = []
        for symbol in sorted(symbols):
            # 先看 target（权威），再看整条用例的文本兜底
            if any(symbol in str(c.get("target") or "") for c in cases):
                covered.append(symbol)
            elif any(symbol in blob for blob in blobs):
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
        }

    def _test_blockers(self) -> list[str]:
        """阻断级测试问题：改动的符号既没被测到、也没申诉说明。

        为什么升为阻断：``missing_symbols`` 原先只作提示，结果是「写了很多用例、
        真正改的东西一个没测」也能一路 pass —— 而那正是回归测试形同虚设的根因
        （真机 run 20260925-184300：5 个被改符号只有 1 个对得上）。

        **必须留申诉出口**：符号可能确实无需单独用例（只改了内部常量），
        只要在 ``coverage_gaps`` 里写明原因即可豁免；否则模型会被逼着编造用例。

        **mock 运行下不生效**：mock 的测试产物是占位数据，本来就不带符号级 target，
        每轮都会命中，把「回流预算 / 人工打回」这类流程机制的测试整个带偏。
        mock 测的是流程，不是覆盖质量；覆盖逻辑本身由 smoke_mock 直接调本函数验证。
        """
        if isinstance(self.client, MockClient):
            return []
        audit = self.state.get("test_audit") or self._audit_test()
        missing = list(audit.get("missing_unexplained") or [])
        if not missing:
            return []
        return [
            "测试没覆盖到本次改动的这些符号，且 coverage_gaps 里也没说明："
            + "、".join(str(s) for s in missing[:6])
            + "（用例 target 要写到符号级；确实无需用例的，在 coverage_gaps 里写明原因即可）"
        ]

    def _stage_test(self, requirement: str, fixes: list[str] | None = None) -> Any:
        self.state["test_report"] = self._grounded_call(
            "test",
            prompts.parts_test(
                requirement,
                self.state.get("scope"),
                self.state.get("plan"),
                self.state.get("implementation"),
                self._code_text("test"),
                fixes,
            ),
        )
        # 机械审计测试产物，结论会 pin 进评审（评审 prompt 要求核对三类是否齐全）
        self.state["test_audit"] = self._audit_test()
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
        )
        report["mode"] = "mock" if mock else "real"
        report["elapsed_s"] = round(time.time() - started, 2)
        self.state["verify_report"] = report
        # 记录「最后一个验证通过的版本」：触顶 / 异常结束时交付它能给出**能跑的**产物，
        # 而不是把最后一版（很可能已被下一轮改坏）端出去。
        # 真机 run 20260924-235001：8 轮全 rework 且越改越坏（第 8 轮直接 ImportError），
        # 最终 needs_human、什么都没交付 —— 而更早的轮次明明产出过可运行版本。
        if report.get("verdict") == "pass" and not mock:
            devs = [s for s in runstore.stage_snapshots(self.run_dir) if s.get("stage") == "dev"]
            if devs:
                self.state["last_good"] = {
                    "file": devs[-1]["file"],
                    "seq": devs[-1]["seq"],
                    "audit": audit,
                    "verify_summary": report.get("summary") or "",
                }
        commands = report.get("commands") or []
        self._record(
            "verify",
            report,
            {
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

    def _stage_review(self, requirement: str, fixes: list[str] | None = None) -> Any:
        parts = prompts.parts_review(
            requirement,
            self.state.get("scope"),
            self.state.get("plan"),
            self.state.get("implementation"),
            self.state.get("test_report"),
            fixes,
            self.state.get("verify_report"),
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
        task_ids = [str(t.get("id")) for t in tasks if t.get("id")]
        task_files: set[str] = set()
        for task in tasks:
            task_files.update(str(p) for p in (task.get("target_files") or []))

        uncovered = [p for p in change_paths if p not in task_files]
        dangling = sorted(f for f in task_files if f not in change_paths)
        bad_ids = [i for i in task_ids if not re.fullmatch(r"[A-Za-z]+-\d{2,}", i)]
        dup_ids = sorted({i for i in task_ids if task_ids.count(i) > 1})
        unknown_dep = sorted(
            {str(d) for t in tasks for d in (t.get("depends_on") or []) if str(d) not in task_ids}
        )
        # 禁改路径：评估阶段给出的 forbidden_paths 一条都不该出现在 changes 里
        forbidden = [str(p) for p in (assessment.get("forbidden_paths") or []) if p]
        touched: list[str] = []
        for path in change_paths:
            stem = self._path_stem(path)
            if not stem:
                continue
            for rule in forbidden:
                rule_stem = self._path_stem(rule)
                if rule_stem and (stem == rule_stem or stem.startswith(rule_stem + "/")):
                    touched.append(path)
                    break
        return {
            "change_count": len(change_paths),
            "task_count": len(task_ids),
            "uncovered_changes": uncovered,
            "dangling_files": dangling,
            "bad_task_ids": bad_ids,
            "duplicate_task_ids": dup_ids,
            "unknown_depends_on": unknown_dep,
            "forbidden_touched": sorted(set(touched)),
            "forbidden_count": len(forbidden),
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

    def _audit_implementation(self) -> dict:
        """确定性核对：方案的每个 task 是否被补丁覆盖 / 被显式声明未实现。

        这是「开发谎报完成」的机械防线：模型不可能通过措辞绕开任务清单核对。
        """
        plan = self.state.get("plan") or {}
        impl = self.state.get("implementation") or {}
        task_ids = [str(t.get("id")) for t in (plan.get("tasks") or []) if isinstance(t, dict) and t.get("id")]
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
                task = str(task)
                if task not in task_ids:
                    unknown.append(task)
                elif task not in covered:
                    covered.append(task)
        declared = [
            f"{item.get('task')}：{item.get('reason')}"
            for item in (impl.get("not_implemented") or [])
            if isinstance(item, dict)
        ]
        declared_ids = {str(item.get("task")) for item in (impl.get("not_implemented") or []) if isinstance(item, dict)}
        missing = [t for t in task_ids if t not in covered and t not in declared_ids]
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
                out.append(
                    f"{name}：{patches.STATUS_CN.get(status, status)}" + (f"（{detail}）" if detail else "")
                )
        return out

    def _verify_blockers(self) -> list[str]:
        """运行验证失败 = 机械证据表明交付跑不起来，同样属于阻断级。"""
        report = self.state.get("verify_report")
        if not isinstance(report, dict) or report.get("verdict") != "fail":
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

    def _verify_missing_entry(self) -> bool:
        """运行验证是否判了「没有可执行入口」。"""
        report = self.state.get("verify_report") or {}
        return any("没有可执行入口" in str(p) for p in (report.get("problems") or []))

    def _mechanical_blockers(self) -> list[str]:
        """机制判定的阻断项 = 补丁机械问题 + 运行验证失败 + 测试漏测。

        三类都不依赖模型自觉：只要机器能证明「贴不回去」「跑不起来」或
        「改的东西一个没测」，即便评审给了 pass 也要改判 rework_dev。
        """
        return [*self._patch_blockers(), *self._verify_blockers(), *self._test_blockers()]

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

        # 机制兜底：有阻断级问题（补丁 anchor 找不到 / 谎称完整 / 贴回去会留残码，
        # 或运行验证失败）时不允许 pass —— 机器已经证明贴不回去或跑不起来，模型说 pass 也不算。
        blockers = self._mechanical_blockers()
        if blockers and review.get("verdict") == "pass":
            in_material = list(in_material) + [f"修复：{item}" for item in blockers]
            review["verdict"] = "rework_dev"
            review["forced_rework"] = True
            review["required_fixes"] = in_material
            review["reasons"] = list(review.get("reasons") or []) + [
                "机制判定：存在阻断级机械证据（" + "；".join(blockers) + "），不允许判定 pass"
            ]
            self.log(f"  [机制] 有阻断级机械证据 → 强制 rework_dev（{len(blockers)} 项）")

        # 同类矛盾：列了**方案层**返工项却判 pass ⇒ 不允许放行（等于承认方案有缺陷还继续往下走）。
        # 放在机械兜底之后：方案层比实现层更根本，两者同时命中时以方案层为准。
        if architect and review.get("verdict") == "pass":
            review["verdict"] = "rework_architect"
            review["forced_rework"] = True
            review["required_fixes"] = list(architect) + list(in_material)
            review["reasons"] = list(review.get("reasons") or []) + [
                f"机制判定：列出 {len(architect)} 条方案层返工项却判 pass（自相矛盾），强制回流方案"
            ]
            self.log(f"  [机制] 列了方案层返工项却判 pass → 强制 rework_architect（{len(architect)} 项）")

        # ---------------------------------------------------------- 入口缺口的层级纠正
        # 方案没规划入口文件时，把「要求补入口」的返工项从**实现层提到方案层**。
        # 为什么必须由机制做：开发受方案白名单约束（提示词明写「只改 changes / target_files
        # 里列出的文件」）—— 入口文件不在方案里，它**无权创建**。于是评审要求加、开发做不到，
        # 两边都没错，循环却不收敛（真机 run 20260925-045404 卡了整整 3 轮）。
        # 新增一个文件属于**方案变更**，按作用域本该判 architect，模型却常误判成 in_material。
        # 放在机械兜底**之后**：阻断项这时才被塞进 in_material，一并把入口那条捞出来归位。
        if self._plan_missing_entry():
            moved = [x for x in in_material if self._looks_like_entry_fix(x)]
            if moved or self._verify_missing_entry():
                in_material = [x for x in in_material if x not in moved]
                architect = list(architect) + [
                    "方案缺少可执行入口：请在 changes 与 tasks 里**规划一个入口文件**"
                    "（main.py / __main__.py 等，带 `if __name__ == '__main__':` 且运行时有输出）。"
                    "方案不规划它，开发就无权创建，运行验证会一直判「没有可执行入口」。"
                ] + moved
                review["required_fixes"] = in_material
                review["architect_fixes"] = architect
                review["reasons"] = list(review.get("reasons") or []) + [
                    "机制判定：方案未规划可执行入口，而返工项要求补入口 —— 属于方案层变更，回流方案"
                ]
                self.log(
                    "  [机制] 方案没规划入口 + 返工项要求补入口 → 提到方案层"
                    f"（从实现层移出 {len(moved)} 项）"
                )

        forced = False
        if not in_material and not architect and review.get("verdict") == "rework_architect" and not blockers:
            # 自相矛盾：判「方案本身有错」，却把返工项全归为「需外部确认」。方案错误是
            # **本轮材料内可改**的，不能借外部确认放行（等于带着已知设计缺陷交付）；
            # 但也不能就这么回流 —— in_material 为空，架构师拿不到任何具体指示。
            # 标记转人工裁决，由人决定是改方案还是接受现状。
            review["escalated_ambiguous"] = True
            review["reasons"] = list(review.get("reasons") or []) + [
                "机制判定：判 rework_architect 但返工项全被归为 needs_external（分类自相矛盾），转人工裁决"
            ]
            self.log("  [机制] 判 rework_architect 但返工项全需外部确认（分类矛盾）→ 转人工裁决，不放行")
        elif not in_material and not architect and review.get("verdict") == "rework_dev" and not blockers:
            forced = True
            review["verdict"] = "pass"
            review["forced_pass"] = True
            review["reasons"] = list(review.get("reasons") or []) + [
                "机制判定：返工项全部属于 needs_external，本轮材料内无可执行修改，自动放行并转入残留风险"
            ]
            self.log("  [机制] 返工项全部需外部确认 → 强制 pass，已转入 residual_risks")
        return in_material, architect, external, forced

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
        # 人工编辑优先：阶段快照文件里的 artifact 覆盖 state.json 中的旧值
        assert self.run_dir is not None
        for stage, artifact in runstore.latest_artifacts(self.run_dir).items():
            key = runstore.STAGE_STATE_KEY.get(stage)
            if key:
                self.state[key] = artifact
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
        if isinstance(scope, dict) and self.pm_decisions:
            self.state["scope"] = prompts.apply_pm_decisions(scope, self.pm_decisions)

    # ------------------------------------------------------------------ 评审频率
    def _review_due(self, attempt: int) -> bool:
        """首轮与末轮必评审；其余按 review_every 间隔。"""
        if attempt <= 0:
            return False
        if attempt > self.max_rework:  # 末轮：要拿到真实判定而不是直接 needs_human
            return True
        if attempt == 1:  # 首轮：早暴露问题
            return True
        return (attempt - self.last_review_attempt) >= self.review_every

    def _begin_round(self, entry: str) -> str:
        self.attempt += 1
        route = "架构师方案 -> 开发 -> 测试" if entry == "architect_plan" else "开发 -> 测试"
        tail = " -> 评审" if self._review_due(self.attempt) else "（本轮跳过评审）"
        self.log(f"== 迭代 {self.attempt}: {route}{tail}")
        return entry

    # ------------------------------------------------------------------ 单步推进
    def _step(self, stage: str) -> str:
        # 阶段边界标记：页面按它把运行日志切分到流程图节点上（格式定义见 runstore.stage_marker，
        # 写入端/读取端共用一份，避免两边正则漂移）
        self.log(runstore.stage_marker(stage))
        if stage == "intake":
            self._stage_intake(self.requirement)
            # 补强闸门（high 重要度缺失要素）已统一到 _gate_after，这里只负责推进游标
            return flow.next_linear("intake")
        if stage == "pm":
            self._stage_pm(self.requirement)
            return flow.next_linear("pm")
        if stage == "retrieve":
            query = self.requirement
            if self.state.get("scope"):
                query += json.dumps(self.state["scope"], ensure_ascii=False)
            self.pool = self._build_pool(query)
            # 新建项目没有存量代码可评估：assess 阶段语义不成立，
            # 真机上它正是「编造不存在的目录/模块」的源头（空仓库里编出 pipeline/core/*、db/*，
            # 再被 plan 当既有事实承接）。直接进方案阶段，由 plan 承接 PRD 做全新架构设计。
            return "architect_plan" if self.project_type == "new" else "architect_assess"
        if stage == "architect_assess":
            if self.project_type == "new":
                # 兜底：续跑时若游标停在 assess（老 run 或人工 --from），同样跳过
                self.log("  [新建项目] 无存量代码可评估，跳过 architect_assess")
                return flow.next_linear("architect_assess")
            self._stage_assess(self.requirement)
            return flow.next_linear("architect_assess")
        if stage == "architect_plan":
            self._stage_plan(self.requirement, self.fixes)
            return self._begin_round("dev")
        if stage == "dev":
            self._stage_dev(self.requirement, self.fixes)
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
        if stage == "test":
            self._stage_test(self.requirement, self.fixes)
            return flow.next_linear("test")
        if stage == "verify":
            self._stage_verify()
            return flow.next_linear("verify")
        if stage == "review":
            return self._step_review()
        if stage == "human_review":
            return self._step_human_review()
        raise OrchestratorError(f"未知编排游标: {stage}")

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
        if not self._review_due(self.attempt):
            self.log(
                f"  第 {self.attempt} 轮跳过评审（review_every={self.review_every}；首轮与末轮必评审）"
            )
            return self._begin_round("dev")
        review = self._stage_review(self.requirement, self.fixes) or {}
        self.last_review_attempt = self.attempt
        in_material, architect_fixes, external, forced = self._normalize_review(review)
        if self.run_dir is not None:
            # 归一化后的评审要回写 NN 文件：_call 内部记录的是原始（未归一化）评审，
            # 续跑时 latest_artifacts 会用原始评审覆盖 state，把 verdict 回退成 rework_dev
            # （人工审核闸门等"评审后再续跑"的场景会暴露此问题）。
            runstore.save_artifact(self.run_dir, "review", review, note="review-normalized")
        verdict = review.get("verdict", "rework_dev")
        self.rounds.append(
            {
                "attempt": self.attempt,
                "verdict": verdict,
                "forced_pass": forced,
                "forced_rework": bool(review.get("forced_rework")),
                "patch_blockers": self._patch_blockers(),
                "mechanical_blockers": self._mechanical_blockers(),
                "required_fixes_in_material": in_material,
                "required_fixes_architect": architect_fixes,
                "required_fixes_external": external,
                "routed_to": "architect_plan" if (verdict == "rework_architect" or architect_fixes) else "dev",
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
        if verdict == "pass":
            return "human_review" if HUMAN_REVIEW_GATE else "done"
        if review.get("escalated_ambiguous"):
            self.needs_human = True
            self.log("  评审结论自相矛盾（判方案有错却又全需外部确认），标记 needs_human 交人工裁决")
            return "done"
        if self.attempt > self.max_rework:
            self.needs_human = True
            self.log(f"  已达回流上限 {self.max_rework}，标记 needs_human")
            return "done"
        # 收敛 / 预算护栏：决定「再烧一轮」之前先问一句值不值。
        # 真机教训 run 20260924-185507：required_fixes 走势 3→2→0→2→4→4→5→4，
        # 第 3 轮已经 pass 之后又反弹，一路烧到 attempt=11/max=12 —— 期间独占显存
        # （单驻留，无法新建运行）却毫无收敛迹象。光靠 max_rework 只在撞顶那一刻才停。
        stop = self._guard_stop_reason()
        if stop:
            self.needs_human = True
            self.state["guard_stop"] = stop
            self.log(f"  [护栏] {stop} → 停止返工，转人工裁决（不再继续烧）")
            return "done"
        # 方案层返工项也进 fixes：回退到方案时架构师要能逐条看到「我漏了什么」，
        # 只做路由不带上内容的话，架构师拿不到任何具体指示。
        fixes = list(architect_fixes) + list(in_material) + list((review or {}).get("blockers") or [])
        self.fixes = fixes
        if verdict == "rework_architect" or architect_fixes:
            # 自动回转：判「方案本身有问题」**或**有任一条方案层返工项，都回方案阶段重跑。
            # 之前只看 verdict —— 评审把方案层根因误标成实现层时，就会整轮打回开发，
            # 而开发在方案 changes 范围内改不动它，白烧一轮（真机 run 20260924-185507）。
            why = (
                "方案本身有问题"
                if verdict == "rework_architect"
                else f"评审把 {len(architect_fixes)} 条返工项判为方案层"
            )
            self.log(f"  {why}：回到架构师方案（方案改了实现必然重做）")
            return self._begin_round("architect_plan")
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
            self._record("human_review", placeholder, {"stage": "human_review", "gate": "human_review"}, "")
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
        return self._begin_round("dev")

    def _gate_after(self, stage: str, nxt: str | None = None) -> Interrupt | None:
        """该阶段结束后是否要停下等人工 —— **唯一的闸门判定入口**。

        三类闸门（静态声明见 ``flow.GATE_SPECS``）：
          · ``explicit``    —— 人工在 ``--pause-after`` / 页面勾选里显式指定该阶段，无条件停；
          · ``conditional`` —— 阶段自带触发条件：PM 提出了未决项 / 补强识别出 high 待确认项。
            这些默认值一旦猜错，下游方案/实现/测试全都建在错误前提上，返工成本远高于
            停下来问一句；而**没有**触发条件时停下只是白白浪费人机交互，所以是条件停。
          · ``mandatory``   —— 该节点本身就是人工节点：``human_review`` 首次到达
            （``_step`` 已写占位产物并停在原地，``nxt == stage``），到达即停。
        """
        if stage in self.pause_after:
            return Interrupt(stage, "explicit", "人工闸门")
        if stage == "intake" and INTAKE_PAUSE_ON_GAPS:
            gaps = self._intake_high_gaps()
            if gaps:
                spec = flow.gate_spec("intake")
                return Interrupt(
                    stage,
                    "conditional",
                    spec.title if spec else "需求补强闸门",
                    "以下缺失要素的默认假设需要人工确认 —— " + "；".join(gaps),
                )
        if stage == "pm" and self.pause_on_open_questions:
            scope = self.state.get("scope") or {}
            has_open = any(
                isinstance(q, dict) and str(q.get("question") or "").strip()
                for q in (scope.get("open_questions") or [])
            )
            if has_open:
                spec = flow.gate_spec("pm")
                return Interrupt(
                    stage,
                    "conditional",
                    spec.title if spec else "PM 未决项闸门",
                    spec.detail if spec else "",
                )
        if stage == "human_review" and HUMAN_REVIEW_GATE and nxt == "human_review":
            # 首次到达：_step 写了占位产物并停在原地（nxt == stage）。
            # 再显式看一次 verdict：人工已提交时即便被误判到这一支，也不该再停。
            art = self.state.get("human_review")
            if not (isinstance(art, dict) and str(art.get("verdict") or "").strip()):
                spec = flow.gate_spec("human_review")
                return Interrupt(stage, "mandatory", spec.title if spec else "人工审核闸门")
        return None

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

    def _last_good_impl(self) -> tuple[dict | None, dict]:
        """取「最后一个验证通过的版本」（那时的 dev 产物 + 通过时的补丁审计）。"""
        lg = self.state.get("last_good") or {}
        name = str(lg.get("file") or "")
        if not name or self.run_dir is None:
            return None, {}
        try:
            payload = json.loads((self.run_dir / Path(name).name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, {}
        impl = payload.get("artifact")
        if not isinstance(impl, dict) or not impl.get("edits"):
            return None, {}
        audit = lg.get("audit")
        return impl, (audit if isinstance(audit, dict) else {})

    def _deliver_last_good(self) -> dict:
        """未通过时的**兜底交付**：把最后一个验证通过的版本物化到目标目录。

        交付物明确标注「未经放行」。有它总比什么都没有强 —— 用户拿到的是唯一一个
        被机械证据证明过能跑的版本，而不是最后一轮（可能已改坏）的残次品。
        """
        result = self._blank_delivery()
        result["mode"] = "last_good"
        if not DELIVER_ENABLED:
            result["reason"] = "交付已关闭（PIPELINE_DELIVER=0），产物仅保留在沙箱"
            return result
        if not self.repo:
            result["reason"] = "未指定目标目录（repo 为空）"
            return result
        impl, audit = self._last_good_impl()
        if impl is None:
            result["reason"] = "没有「验证通过」的历史版本可兜底"
            return result
        saved = (self.state.get("implementation"), self.state.get("patch_audit"))
        self.state["implementation"] = impl
        self.state["patch_audit"] = audit or patches.analyze_all(self.repo, impl)
        try:
            out = self._write_target("last_good")
        finally:
            self.state["implementation"], self.state["patch_audit"] = saved
        if out.get("delivered"):
            lg = self.state.get("last_good") or {}
            out["reason"] = (
                f"该版本取自「最后一次通过运行验证」的快照 {lg.get('file')}"
                "（**未经人工放行**；本轮最终判定未通过）"
            )
        return out

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
            for model in self.client.ps():
                name = model.get("name", "")
                if name:
                    self.client.unload(name)
            self.log("== 已卸载全部模型（显存释放）")

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
