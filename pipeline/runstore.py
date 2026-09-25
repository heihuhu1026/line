"""run 目录的持久化约定（编排器落盘 / 操作页面读写共用）。

目录布局（每次运行一个时间戳目录）::

    runs/<run_id>/
      requirement.txt      需求原文
      state.json           可续跑的编排状态：进度游标、产物、代码池、埋点、人工意见
      summary.json         终态汇总（只在跑完时写；暂停中的运行没有这个文件）
      NN-<stage>.json      每阶段快照 {stage, meta, artifact, request_preview}
      llm-calls.jsonl      调用埋点（追加，不因重跑而清空）
      handoff.md           待人工确认清单
      prd.md               产品需求文档（PM 阶段产出，随每次持久化刷新）
      console.log          操作页面启动子进程时的日志
      superseded/          被「重跑本阶段」作废的旧快照（保留审计）

**人工编辑的生效路径**：人工直接改 ``NN-<stage>.json`` 里的 ``artifact`` 后续跑，
编排器以阶段快照文件为准（``latest_artifacts``），因此编辑立即生效。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from . import flow

STAGE_FILE_RE = re.compile(r"^(\d+)-([a-z_]+)\.json$")
STATE_NAME = "state.json"
SUMMARY_NAME = "summary.json"
HANDOFF_NAME = "handoff.md"
PRD_NAME = "prd.md"
# 人工改写过 prd.md 的标记：存在时流水线不再用产物覆盖它（否则人工白改）
PRD_HUMAN_FLAG = "prd.human"
REQ_NAME = "requirement.txt"
LOG_NAME = "console.log"
TRACE_NAME = "traces.jsonl"
ENV_NAME = "env.json"
ISSUES_NAME = "issues.json"
SUPERSEDED_DIR = "superseded"

# 全流程阶段顺序 + 阶段->state.artifacts 键。两者都是 pipeline/flow.py 的转发，
# 真源只有一份 —— 以前这里和 config.FULL_STAGE_ORDER / orchestrator.ONLY_STAGES 各写一份，
# 新增阶段时漏改就会「运行启动即死」（真机教训 20260924-134458）。
FLOW_ORDER = list(flow.FLOW_ORDER)
STAGE_STATE_KEY = dict(flow.STAGE_STATE_KEY)


def write_json(path: Path, payload: Any) -> None:
    """原子写：先写临时文件再 os.replace，避免操作页面读到半截 JSON。

    Windows 上 os.replace 撞上「另一进程正打开该文件读取」会报 ERROR_ACCESS_DENIED
    （操作页面轮询 runs/ 时必然出现），因此这里带重试，全部失败再退化为直接写。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                return
            except OSError:
                if attempt == 7:
                    break
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.unlink(missing_ok=True)
    path.write_text(text, encoding="utf-8")  # 兜底：非原子但保证落盘


def _read_json(path: Path) -> Any:
    for attempt in range(3):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except OSError:
            time.sleep(0.05)
        except ValueError:
            return None
    return None


def read_json_if_exists(path: Path) -> Any:
    """读一个可能不存在的 json（不存在返回 None，不抛异常）。"""
    return _read_json(Path(path))


# --------------------------------------------------------------------- 完整调用留存（traces）
def append_trace(run_dir: Path, record: dict) -> None:
    """把一次调用的**完整**输入输出追加到 traces.jsonl。

    这是「用记录优化流水线」的关键素材：NN-<stage>.json 里的 request_preview 只留 4000 字，
    模型原始输出（含 thinking）原本完全不留。可用 PIPELINE_TRACE=0 关闭。
    """
    path = Path(run_dir) / TRACE_NAME
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_traces(run_dir: Path, limit: int | None = None) -> list[dict]:
    path = Path(run_dir) / TRACE_NAME
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows[-limit:] if limit else rows


# --------------------------------------------------------------------- 运行日志按阶段切分
# 阶段边界标记：编排器在每个阶段开跑前写一行，接口/页面据此把 console.log 切到流程图节点上。
# 格式只在这里定义一次 —— 写入端（orchestrator._step）与读取端（server 的 /log?stage=）共用，
# 避免两边各写一套正则后悄悄漂移。
STAGE_MARK_RE = re.compile(r"^==\s*STAGE\s+([a-z_]+)\s*==\s*$")


def stage_marker(stage: str) -> str:
    """阶段边界标记的规范写法（编排器写入用）。"""
    return f"== STAGE {stage} =="


def read_log_lines(run_dir: Path) -> list[str]:
    path = Path(run_dir) / LOG_NAME
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def log_sections(run_dir: Path) -> tuple[list[dict], list[str]]:
    """把 console.log 按阶段边界标记切成若干段，返回 ``(sections, preamble)``。

    每段形如 ``{"stage": "dev", "lines": [...], "index": 2}``（``index`` 是该阶段第几次出现，
    从 1 起 —— 回流循环里 dev/test/review 会出现多轮）；``preamble`` 是第一条标记之前的内容
    （run 启动信息、环境指纹等）。没有标记的旧运行返回空 sections，调用方需退回「整段尾部」。
    """
    sections: list[dict] = []
    preamble: list[str] = []
    seen: dict[str, int] = {}
    current: dict | None = None
    for line in read_log_lines(run_dir):
        match = STAGE_MARK_RE.match(line.strip())
        if match:
            stage = match.group(1)
            seen[stage] = seen.get(stage, 0) + 1
            current = {"stage": stage, "lines": [], "index": seen[stage]}
            sections.append(current)
            continue
        if current is None:
            preamble.append(line)
        else:
            current["lines"].append(line)
    return sections, preamble


def stage_log(run_dir: Path, stage: str, max_lines: int = 400) -> dict:
    """取某阶段的日志切片（该阶段跑了多轮就返回全部轮次，按执行顺序拼接）。

    返回值给接口用：``text`` 是切片正文，其余字段是给页面提示的元信息
    （``occurrences`` 执行了几次 / ``has_markers`` 该运行是否带标记 / ``truncated`` 是否截断）。
    """
    lines = read_log_lines(run_dir)
    sections, _ = log_sections(run_dir)
    hits = [s for s in sections if s["stage"] == stage]
    if not hits:
        if sections:
            # 有标记但该阶段没跑过：明确返回空，**不能**退回整段尾部
            # （否则页面会把「整个运行的日志」误当成「这个阶段的日志」）
            return {
                "stage": stage,
                "text": "",
                "occurrences": 0,
                "has_markers": True,
                "truncated": False,
                "lines": 0,
            }
        tail = lines[-max_lines:]
        return {
            "stage": stage,
            "text": "\n".join(tail),
            "occurrences": 0,
            "has_markers": False,
            "truncated": len(lines) > len(tail),
            "lines": len(tail),
        }
    out: list[str] = []
    for sec in hits:
        head = stage_marker(sec["stage"]) + (f"  （第 {sec['index']} 次执行）" if sec["index"] > 1 or len(hits) > 1 else "")
        out.append(head)
        out.extend(sec["lines"])
        out.append("")
    truncated = len(out) > max_lines
    if truncated:
        out = out[-max_lines:]
    return {
        "stage": stage,
        "text": "\n".join(out).strip("\n"),
        "occurrences": len(hits),
        "has_markers": True,
        "truncated": truncated,
        "lines": len(out),
    }


# --------------------------------------------------------------------- 状态
def read_state(run_dir: Path) -> dict | None:
    return _read_json(Path(run_dir) / STATE_NAME)


def write_state(run_dir: Path, state: dict) -> None:
    write_json(Path(run_dir) / STATE_NAME, state)


def read_summary(run_dir: Path) -> dict | None:
    return _read_json(Path(run_dir) / SUMMARY_NAME)


# --------------------------------------------------------------------- 阶段快照
def stage_snapshots(run_dir: Path, include_superseded: bool = False) -> list[dict]:
    """按 seq 升序返回所有阶段快照。"""
    run_dir = Path(run_dir)
    found: list[tuple[int, Path]] = []
    for path in run_dir.glob("*.json"):
        match = STAGE_FILE_RE.match(path.name)
        if match:
            found.append((int(match.group(1)), path))
    if include_superseded:
        for path in (run_dir / SUPERSEDED_DIR).glob("*.json"):
            match = STAGE_FILE_RE.match(path.name)
            if match:
                found.append((int(match.group(1)), path))
    rows: list[dict] = []
    for seq, path in sorted(found, key=lambda item: item[0]):
        payload = _read_json(path)
        if not isinstance(payload, dict):
            continue
        rows.append(
            {
                "seq": seq,
                "stage": payload.get("stage") or path.name.split("-", 1)[-1][:-5],
                "file": path.name,
                "path": path,
                "meta": payload.get("meta") or {},
                "artifact": payload.get("artifact"),
                "request_preview": payload.get("request_preview") or "",
            }
        )
    return rows


def latest_artifacts(run_dir: Path) -> dict[str, Any]:
    """每个阶段取最后一次快照的 artifact（人工编辑过的文件即最后一份，天然生效）。"""
    out: dict[str, Any] = {}
    for snap in stage_snapshots(run_dir):
        if snap["artifact"] is not None:
            out[snap["stage"]] = snap["artifact"]
    return out


def save_artifact(run_dir: Path, stage: str, artifact: Any, note: str = "human-edit") -> bool:
    """把人工编辑后的 artifact 写回该阶段最后一份快照，并同步 state.json。"""
    run_dir = Path(run_dir)
    snaps = [s for s in stage_snapshots(run_dir) if s["stage"] == stage]
    if not snaps:
        return False
    target = snaps[-1]
    payload = _read_json(target["path"]) or {}
    payload["artifact"] = artifact
    meta = payload.setdefault("meta", {})
    meta["human_edited"] = True
    meta["human_edit_note"] = note
    write_json(target["path"], payload)

    state = read_state(run_dir)
    if isinstance(state, dict):
        key = STAGE_STATE_KEY.get(stage)
        if key:
            state.setdefault("artifacts", {})[key] = artifact
            write_state(run_dir, state)
    return True


def archive_stages(run_dir: Path, stages: list[str]) -> list[str]:
    """把指定阶段的快照移入 superseded/（重跑前作废旧产物，保留审计）。"""
    run_dir = Path(run_dir)
    dest = run_dir / SUPERSEDED_DIR
    moved: list[str] = []
    wanted = set(stages)
    for snap in stage_snapshots(run_dir):
        if snap["stage"] not in wanted:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / snap["file"]
        if target.exists():
            target.unlink()
        os.replace(snap["path"], target)
        moved.append(snap["file"])
    return moved


def archive_after_seq(run_dir: Path, seq: int) -> list[str]:
    """把 ``seq > 给定值`` 的全部**在存**快照移入 superseded/（回放检查点前作废其后产物）。

    注意是**严格大于**：给定 seq 的那份检查点自身要保留（它就是回放的目标状态点）。

    与 ``archive_stages``（按阶段名作废整条尾巴）的区别：这里按**检查点序号**切，
    因此同一阶段在多个轮次里各留一份时可以精确回放到某一轮。
    """
    run_dir = Path(run_dir)
    dest = run_dir / SUPERSEDED_DIR
    moved: list[str] = []
    for snap in stage_snapshots(run_dir):
        if snap["seq"] <= seq:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / snap["file"]
        if target.exists():
            target.unlink()
        os.replace(snap["path"], target)
        moved.append(snap["file"])
    return moved


def checkpoints(run_dir: Path) -> list[dict]:
    """检查点时间线：每份 ``NN-<stage>.json`` 都是一个可回放的状态点（含 superseded/ 归档）。

    语义对齐 LangGraph 的 checkpoint —— ``stage_snapshots`` 只给「当前有效」的快照，
    这里额外把被作废的归档按 seq 一起排进时间线，并标出 ``superseded`` 与 ``state_key``，
    页面据此给「回放到此处」选择器。
    """
    run_dir = Path(run_dir)

    def _scan(root: Path, superseded: bool) -> list[dict]:
        rows: list[dict] = []
        if not root.exists():
            return rows
        for path in root.glob("*.json"):
            match = STAGE_FILE_RE.match(path.name)
            if not match:
                continue
            payload = _read_json(path)
            if not isinstance(payload, dict):
                continue
            meta = payload.get("meta") or {}
            try:
                mtime = int(path.stat().st_mtime)
            except OSError:
                mtime = 0
            rows.append(
                {
                    "seq": int(match.group(1)),
                    "stage": payload.get("stage") or path.name.split("-", 1)[-1][:-5],
                    "file": path.name,
                    "superseded": superseded,
                    "state_key": STAGE_STATE_KEY.get(payload.get("stage") or ""),
                    "tag": meta.get("tag"),
                    "note": meta.get("note"),
                    "human_edited": bool(meta.get("human_edited")),
                    "has_artifact": payload.get("artifact") is not None,
                    "mtime": mtime,
                }
            )
        return rows

    rows = _scan(run_dir, False) + _scan(run_dir / SUPERSEDED_DIR, True)
    rows.sort(key=lambda row: (row["seq"], row["superseded"]))
    for index, row in enumerate(rows):
        row["index"] = index
    return rows


# --------------------------------------------------------------------- 列表
def list_runs(runs_dir: Path) -> list[dict]:
    """操作页面用：按时间倒序列出所有运行。"""
    rows: list[dict] = []
    runs_dir = Path(runs_dir)
    if not runs_dir.exists():
        return rows
    for entry in runs_dir.iterdir():
        # "." 开头是页面用的临时区，"_" 开头是报告/元优化产物，都不是运行
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        state = read_state(entry) or {}
        summary = read_summary(entry) or {}
        if not state and not summary:
            # 启动即失败的运行（例：--pause-after 含未知阶段 → 子进程 exit 2）：目录里
            # 只有 console.log，既没 state.json 也没 summary.json。以前直接跳过 ——
            # 后果是它在列表里彻底消失，人工既看不到失败原因，也没法从页面删掉这个空目录。
            # 这里照常列出并标成 failed（有 console.log / requirement.txt 才认，避免把
            # runs/ 下的无关目录也列进来）。
            if not (entry / LOG_NAME).exists() and not (entry / REQ_NAME).exists():
                continue
            rows.append(
                {
                    "run_id": entry.name,
                    "status": "failed",
                    "verdict": None,
                    "needs_human": False,
                    "cursor": None,
                    "paused_after": None,
                    "attempts": None,
                    "reviewed_rounds": None,
                    "wall_s": None,
                    "model_switches": None,
                    "mtime": int(entry.stat().st_mtime),
                    "stages": 0,
                    "issues": {},
                    "human_actions": 0,
                    "has_state": False,
                }
            )
            continue
        verdict = summary.get("verdict") or (state.get("artifacts", {}).get("review") or {}).get("verdict")
        issues_file = _read_json(entry / ISSUES_NAME) or {}
        rows.append(
            {
                "run_id": entry.name,
                "status": state.get("status") or ("done" if summary else "unknown"),
                "verdict": verdict,
                "needs_human": bool(summary.get("needs_human") or state.get("needs_human")),
                "cursor": state.get("cursor"),
                "paused_after": state.get("paused_after"),
                "attempts": state.get("attempt"),
                "reviewed_rounds": len(state.get("rounds") or []) or summary.get("rounds"),
                "wall_s": summary.get("wall_s") or state.get("elapsed_s"),
                "model_switches": summary.get("model_switches") or state.get("model_switches"),
                "mtime": int(entry.stat().st_mtime),
                "stages": len(stage_snapshots(entry)),
                "issues": issues_file.get("summary") or summary.get("issues") or {},
                "human_actions": len(state.get("human_actions") or []),
            }
        )
    rows.sort(key=lambda row: row["mtime"], reverse=True)
    return rows


def run_detail(run_dir: Path) -> dict:
    """操作页面用：单次运行的完整视图。"""
    run_dir = Path(run_dir)
    state = read_state(run_dir) or {}
    summary = read_summary(run_dir) or {}
    # 审计类产物在 state.artifacts 里（与各阶段产物同层），页面按顶层取用会取不到，这里摊平
    artifacts = state.get("artifacts") or {}
    for key in ("implementation_audit", "patch_audit"):
        if key not in state and artifacts.get(key):
            state[key] = artifacts[key]
    snaps = stage_snapshots(run_dir)
    artifacts = {s["stage"]: s["artifact"] for s in snaps if s["artifact"] is not None}
    handoff_path = run_dir / HANDOFF_NAME
    prd_path = run_dir / PRD_NAME
    # 需求原文：state 里带着就用它；--only 或早期运行没写进 state 时退回 requirement.txt。
    # 这是「这次运行到底要做什么」的唯一源头，页面必须能看到，否则只能翻文件。
    req_path = run_dir / REQ_NAME
    requirement = state.get("requirement") or (
        req_path.read_text(encoding="utf-8", errors="replace").strip() if req_path.exists() else ""
    )
    return {
        "run_id": run_dir.name,
        "state": {
            key: state.get(key)
            for key in (
                "status",
                "verdict",
                "cursor",
                "paused_after",
                "attempt",
                "review_every",
                "max_rework",
                "needs_human",
                "pause_after",
                "initial_pause_after",
                "fixes",
                "rounds",
                "human_feedback",
                "human_actions",
                "intake_decisions",
                "pm_decisions",
                "implementation_audit",
                "patch_audit",
                "verify_report",
                "grounding_warnings",
                "elapsed_s",
                "model_switches",
                "repo",
                # 项目类型必须透出：二开与新建项目是两套提示词/两套流程，
                # 页面与汇总若不区分，跨运行对比时问题增减无法归因。
                "project_type",
                "mode",
                "mock",
                "requirement",
            )
        },
        "requirement": requirement,
        "summary": summary,
        "artifacts": artifacts,
        "stages": [
            {
                "seq": snap["seq"],
                "stage": snap["stage"],
                "file": snap["file"],
                "meta": snap["meta"],
                "artifact": snap["artifact"],
                "request_preview": snap["request_preview"],
            }
            for snap in snaps
        ],
        # 检查点时间线（含 superseded/ 归档）：页面的「回放到此处」选择器数据源
        "checkpoints": checkpoints(run_dir),
        "superseded": len(list((run_dir / SUPERSEDED_DIR).glob("*.json")))
        if (run_dir / SUPERSEDED_DIR).exists()
        else 0,
        "handoff": handoff_path.read_text(encoding="utf-8") if handoff_path.exists() else "",
        # PRD 同样是人工阅读材料（PM 的未决问题与默认假设都在第 6 节），
        # 之前只透出 handoff，页面上根本看不到它。
        "prd": prd_path.read_text(encoding="utf-8") if prd_path.exists() else "",
        "has_state": bool(state),
        "env": _read_json(run_dir / ENV_NAME),
        "patches": [
            {"file": f"patches/{path.name}", "size": path.stat().st_size}
            for path in sorted((run_dir / "patches").glob("*.patch"))
        ]
        if (run_dir / "patches").exists()
        else [],
        "trace_calls": sum(1 for _ in (run_dir / TRACE_NAME).open(encoding="utf-8", errors="replace"))
        if (run_dir / TRACE_NAME).exists()
        else 0,
        "issues_file": _read_json(run_dir / ISSUES_NAME),
    }
