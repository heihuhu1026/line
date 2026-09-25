"""裁决参谋：闸门上的旁路问答环节。

定位
----
人工在闸门上裁决「待确认项」时，常常对某一条的**风险 / 收益**拿不准。让他对着
「建议补充：需明确窗口尺寸或画布大小」这种占位文字做决定，等于逼他猜
（真机 run 20260925-140707 就是这么发生的）。这个环节让人**反复**向模型发问，
拿到 风险 / 收益 / 可逆性 / 具体建议，再决定填什么。

为什么它不是一个流水线阶段
--------------------------
* 它不产出阶段 artifact，也不参与阶段序列 —— 它是**随闸门可用**的旁路；
* 所以刻意不去动 ``EXEC_ORDER`` / ``STAGE_MODELS`` / ``STAGE_SCHEMAS`` /
  ``STAGE_STATE_KEY`` 这些注册表（动一张就要同步五张，``flow.validate`` 会核对）；
* 问答逐轮追加到 ``runs/<id>/advice/<stage>.jsonl``：可读、可审计、可重放，
  也不污染「阶段产物是唯一真源」这条语义。

模型选择
--------
默认复用**该阶段自己的模型**（建议要落在该阶段的视角上，也避免多加载一个 tag）；
``config.ADVICE_TAG`` 可强制指定。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import config, prompts
from .budget import estimate_tokens, fit_prompt
from .ollama_client import MockClient, OllamaClient
from .schemas import ADVICE

#: 问答线程的相对目录（放在 run 目录下，与 verify/ patches/ 同级）
THREAD_DIR = "advice"

#: 各阶段在提示词里的自称（没有专门 spec 的阶段退回 review 的视角）
_FALLBACK_STAGE = "review"


# --------------------------------------------------------------------------- 线程读写
def thread_path(run_dir: str | Path, stage: str) -> Path:
    return Path(run_dir) / THREAD_DIR / f"{stage}.jsonl"


def read_thread(run_dir: str | Path, stage: str) -> list[dict]:
    """读该阶段的问答线程（按写入顺序）。

    文件不存在、某一行损坏都只是跳过 —— 线程是**留痕**，不该因为一行脏数据
    让整个裁决环节打不开。
    """
    path = thread_path(run_dir, stage)
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _append(run_dir: str | Path, stage: str, turn: dict) -> None:
    path = thread_path(run_dir, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(turn, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- 模型规格
def spec_for(stage: str) -> config.ModelSpec:
    """裁决参谋的模型规格：复用该阶段 spec 的 tag/num_ctx，只调采样参数。

    temperature 0.3：判断类任务要稳，不要发挥（与 review 同档）。
    num_predict 2048：结构化判断不需要长输出 —— 这条链路是**交互式**的，
    每次省下的时间都直接体现在「人工等多久」上。
    """
    base = config.STAGE_MODELS.get(stage) or config.STAGE_MODELS[_FALLBACK_STAGE]
    return config.ModelSpec(
        role=f"{base.role}·裁决参谋",
        tag=config.ADVICE_TAG or base.tag,
        num_ctx=base.num_ctx,
        prompt_token_budget=max(800, base.num_ctx - 2048),
        think=base.think,
        temperature=0.3,
        num_predict=2048,
    )


# --------------------------------------------------------------------------- 待确认项归一
def pending_items(stage: str, artifact: Any, state: Any = None) -> list[dict]:
    """把各阶段的「待确认项」归一到通用形状，供提示词与前端复用。

    通用形状：``{"element","value","decided","importance","why"}``。

    「人工裁决」**两处都要取**：产物里的 ``final_decision`` 优先，其次 state 里按
    主题存的裁决记录（``intake_decisions`` / ``pm_decisions``）。只看产物会漏 ——
    真机上出现过「前端提交的 kind 与服务端匹配错位，4 条裁决只并回 1 条」，
    那样参谋会以为人工什么都没定，给出的建议就跑偏了。
    """
    art = artifact if isinstance(artifact, dict) else {}
    st = state if isinstance(state, dict) else {}
    decisions: dict[str, str] = {}
    for key in ("intake_decisions", "pm_decisions"):
        for row in st.get(key) or []:
            if isinstance(row, dict) and row.get("ref"):
                decisions[str(row["ref"])] = str(row.get("decision") or "")

    def decided_of(row: dict, topic: str) -> str:
        return str(row.get("final_decision") or "").strip() or decisions.get(topic, "")

    out: list[dict] = []
    if stage == "intake":
        for row in prompts.intake_items(art):
            topic = str(row.get("element") or "")
            out.append({
                "element": topic,
                "value": str(row.get("default_assumption") or "").strip(),
                "decided": decided_of(row, topic),
                "importance": str(row.get("importance") or ""),
                "why": str(row.get("why") or ""),
            })
    elif stage == "pm":
        for row in art.get("open_questions") or []:
            if not isinstance(row, dict):
                continue
            topic = str(row.get("question") or "")
            out.append({
                "element": topic,
                "value": str(row.get("assumed_answer") or "").strip(),
                "decided": decided_of(row, topic),
                "importance": str(row.get("severity") or ""),
                "why": str(row.get("why_it_matters") or ""),
            })
    else:
        # 其余阶段没有专门的「待确认项」契约，退回 uncertainties（客观未知 + 置信度）：
        # 它同样是「还没定的事」，人工一样可能想问风险。
        for row in art.get("uncertainties") or []:
            if not isinstance(row, dict):
                continue
            out.append({
                "element": str(row.get("issue") or ""),
                "value": str(row.get("assumption") or "").strip(),
                "decided": "",
                "importance": str(row.get("confidence") or ""),
                "why": "",
            })
    return [r for r in out if r["element"]]


def artifact_text(artifact: Any, limit: int | None = None) -> str:
    """阶段产物的正文（判断依据）。按配置的字符预算从**尾部**截断。"""
    if artifact in (None, {}, []):
        return ""
    try:
        text = json.dumps(artifact, ensure_ascii=False, indent=1)
    except (TypeError, ValueError):
        text = str(artifact)
    cap = limit if limit is not None else config.ADVICE_CONTEXT_CHARS
    if cap > 0 and len(text) > cap:
        return text[:cap] + "\n…（已截断）"
    return text


# --------------------------------------------------------------------------- 提问
def ask(
    run_dir: str | Path,
    *,
    requirement: str,
    stage: str,
    artifact: Any,
    question: str,
    focus: str = "",
    state: Any = None,
    client: Any = None,
) -> dict:
    """问一次裁决参谋：构建上下文 → 调模型 → 追加线程 → 返回本轮记录。

    用真模型还是 MockClient 由 ``client`` 决定（调用方按运行是否 mock 传入）——
    与流水线一致：mock 运行不该偷偷去调真模型。
    """
    pending = pending_items(stage, artifact, state)
    thread = read_thread(run_dir, stage)
    history = thread[-config.ADVICE_MAX_HISTORY:] if config.ADVICE_MAX_HISTORY > 0 else []

    spec = spec_for(stage)
    system = prompts.system_prompt("advice")
    base_role = (config.STAGE_MODELS.get(stage) or config.STAGE_MODELS[_FALLBACK_STAGE]).role
    parts = prompts.parts_advice(
        requirement,
        f"{base_role}（阶段 {stage}）",
        pending,
        question,
        focus=focus,
        history=history,
        artifact_text=artifact_text(artifact),
    )
    budget = max(spec.prompt_token_budget - estimate_tokens(system), 400)
    user, truncated = fit_prompt(parts, budget)

    cli = client if client is not None else OllamaClient(config.OLLAMA_HOST, timeout=config.ADVICE_TIMEOUT)
    t0 = time.time()
    data, meta = cli.chat_json(spec, system, user, ADVICE)
    turn = {
        "n": len(thread) + 1,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stage": stage,
        "focus": focus,
        "question": question,
        "answer": data,
        "model": spec.tag,
        "wall_s": meta.get("wall_s") if isinstance(meta, dict) else round(time.time() - t0, 2),
        "truncated": truncated,
    }
    _append(run_dir, stage, turn)
    return turn
