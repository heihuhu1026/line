"""**任务类型**：把「首次开发」与「返工修缺陷」分成两类任务。

为什么要分（真机 run 20260926-214757 的四轮零进展）：
首次开发的输入是需求与方案，目标是"从 0 到 1"，产出允许整份新建；
返工修缺陷的输入是**缺陷证据**，目标是"从有问题到符合预期"，要求最小改动、不扩范围。
把两者塞进同一套契约，会出现正面冲突的两句话：

    「全新项目一律用 add + full_symbol 整份内容」        （prompts.py:2014/2017）
    「优先定点改：change_type 用 modify + replace_span」（prompts.py:1399-1401）

模型取折中输出 `modify` + 片段 ⇒ 补丁无原文可套用（`unchecked`）⇒ 文件不落盘
⇒ `import xxx` 失败 ⇒ 判负 ⇒ 再返工。

这里的职责**只是把机械证据规范化成一张「缺陷单」**，并给出可机械判定的范围：
它不生成提示词文案（`prompts` 负责），也不做路由（`orchestrator` 负责）。

设计上刻意不做的事：
- **不给每个 BUG 建一套东西**：BUG 千变万化，角色按**能力**分（feature/bugfix 两种模式），
  不按具体 BUG 分。
- **不新增复现/定位阶段**：每个阶段都是一次 LLM 调用（dev 一轮 40~100s），本地小模型
  24K 上下文，拆细了时间爆炸。复现由已有的 `verify`（真跑命令）承担。
"""

from __future__ import annotations

import re
from typing import Any

FEATURE = "feature"
BUGFIX = "bugfix"

#: 返工项里形如「[文件 cli.py]」的前缀（orchestrator 拼 fixes 时加的）
_FILE_PREFIX = re.compile(r"^\s*\[\s*文件\s*([^\]]+?)\s*\]")
#: `python -c "import cli; ..."` / `python cli.py ...` → 命中的模块
_IMPORT_MODULE = re.compile(r"import\s+([A-Za-z_][\w.]*)")
_RUN_PY = re.compile(r"\bpython[0-9.]*\s+([^\s\"']+\.py)")


def _norm(path: Any) -> str:
    return str(path or "").replace("\\", "/").strip()


def _short(text: Any, limit: int = 160) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _failed_commands(verify: dict) -> list[dict]:
    """verify 里**真正跑失败**的命令 —— 这就是可复现步骤。"""
    out: list[dict] = []
    for cmd in verify.get("commands") or []:
        if not isinstance(cmd, dict):
            continue
        status = str(cmd.get("status") or "")
        if status not in ("fail", "error", "failed"):
            continue
        out.append(
            {
                "command": _short(cmd.get("command"), 200),
                "status": status,
                "exit_code": cmd.get("exit_code"),
                "output": _short(cmd.get("output") or cmd.get("error") or "", 300),
            }
        )
    return out


def _module_to_path(module: str) -> str:
    return module.replace(".", "/") + ".py"


def _paths_from_command(command: str) -> list[str]:
    """从一条失败命令里反推"大概是哪几个文件的问题"。"""
    out: list[str] = []
    for m in _RUN_PY.finditer(command):
        out.append(_norm(m.group(1)))
    for m in _IMPORT_MODULE.finditer(command):
        out.append(_module_to_path(m.group(1)))
    seen: list[str] = []
    for p in out:
        if p and p not in seen:
            seen.append(p)
    return seen


def bug_report_from_state(state: dict[str, Any], fixes: list[str] | None = None) -> dict:
    """把 verify / test / review 的机械证据规范化成一张**缺陷单**。

    我们的流水线其实**天然就产出了缺陷单所需的一切**（失败命令、退出码、漏测符号、
    判负理由），只是此前一直以"一串裸文本"的形态喂回 dev，于是 dev 只能靠猜。
    """
    state = _as_dict(state)
    verify = _as_dict(state.get("verify_report"))
    test = _as_dict(state.get("test_report"))
    review = _as_dict(state.get("review"))

    failed = _failed_commands(verify)
    problems = [_short(p, 200) for p in (verify.get("problems") or []) if str(p).strip()]
    missing = [_short(s, 80) for s in (test.get("missing_symbols") or []) if str(s).strip()]

    # 受影响文件：① 返工项里的「[文件 X]」前缀（最可靠）；② 失败命令反推
    affected: dict[str, set[str]] = {}

    def touch(path: str, symbol: str = "") -> None:
        p = _norm(path)
        if not p:
            return
        affected.setdefault(p, set())
        if symbol:
            affected[p].add(str(symbol))

    for item in fixes or []:
        m = _FILE_PREFIX.match(str(item or ""))
        if m:
            touch(m.group(1))
    for cmd in failed:
        for p in _paths_from_command(str(cmd.get("command") or "")):
            touch(p)
    for sym in missing:
        # 漏测符号形如 "cli.CLI" / "main" —— 取第一段当文件名候选
        head = str(sym).split(".")[0]
        if head:
            touch(_module_to_path(head), str(sym))

    title = ""
    if problems:
        title = problems[0]
    elif failed:
        title = f"命令失败：{failed[0].get('command')}"
    elif fixes:
        title = _short(fixes[0], 160)

    return {
        "type": BUGFIX if (failed or problems or fixes or missing) else FEATURE,
        "title": title,
        "environment": str(verify.get("sandbox") or ""),
        "repro_steps": [c["command"] for c in failed],
        "expected": "命令退出码 0",
        "actual": (
            f"退出码 {failed[0].get('exit_code')}" if failed and failed[0].get("exit_code") is not None
            else ("命令执行失败" if failed else "")
        ),
        "logs": problems or [c["output"] for c in failed if c.get("output")],
        "failing_items": [*(c["command"] for c in failed), *missing],
        "affected": {p: sorted(s) for p, s in sorted(affected.items())},
        "fixes": [ _short(f, 200) for f in (fixes or []) if str(f).strip() ],
        "materialized": [ _norm(p) for p in (verify.get("materialized") or []) ],
        "verdict": str(review.get("verdict") or verify.get("verdict") or ""),
    }


def allowed_scope(report: dict) -> list[str]:
    """本轮**允许改动**的文件清单。空列表 = 不限（信息不足时宁可不限制，也不瞎限制）。"""
    return sorted(_as_dict(report).get("affected") or {})


def scope_violations(
    edits: list[dict] | None,
    allowed: list[str] | None,
    existing_paths: list[str] | None,
) -> list[str]:
    """BUG 修复模式的**机械判据**（不依赖模型自觉）。

    三条，每条都对应一个真机踩过的坑：
      1. 越界：提交了缺陷单没指向的文件 ⇒ 范围扩散，评审通过的部分被推翻；
      2. 已存在的文件却用 `add` 整份重吐 ⇒ 跨轮合并走「符号并集」分支
         （`orchestrator._merge_impl_across_rounds`），旧块保留 + 新块追加 =
         **同一符号两份定义** ⇒ `new_file_duplicate_symbol`；
      3. 缺陷单指向的文件一条 edit 都没交 ⇒ 本轮等于没修。
    """
    out: list[str] = []
    edits = [e for e in (edits or []) if isinstance(e, dict)]
    if not edits:
        return out
    allowed_set = {_norm(p) for p in (allowed or []) if _norm(p)}
    existing = {_norm(p) for p in (existing_paths or []) if _norm(p)}
    touched = {_norm(e.get("path")) for e in edits if e.get("path")}

    if allowed_set:
        for p in sorted(touched - allowed_set):
            out.append(
                f"越界改动：{p} 不在本轮缺陷单指向的范围内（允许：{'、'.join(sorted(allowed_set))}）；"
                "未列出的文件不要提交 edit，跨轮合并会保留它们"
            )
        missing = sorted(allowed_set - touched)
        if missing:
            out.append(
                f"漏改：缺陷单指向 {'、'.join(missing)}，但本轮没有对应的 edit"
            )
    for e in edits:
        p = _norm(e.get("path"))
        if p in existing and str(e.get("change_type") or "") == "add":
            out.append(
                f"重复定义风险：{p} 已存在，却用 change_type=add 整份重吐 —— "
                "跨轮合并会保留旧块并与新块叠加，产生两份同名符号；已存在文件只能用 modify 定点改"
            )
    return out


def format_bug_report(report: dict) -> str:
    """缺陷单的提示词形态。控制篇幅：dev 的预算只有 8K 左右。"""
    r = _as_dict(report)
    if not (r.get("repro_steps") or r.get("logs") or r.get("fixes") or r.get("affected")):
        return ""
    lines = ["【缺陷单（本轮是 BUG 修复模式：只修下列缺陷，不是重新开发一遍）】"]
    if r.get("title"):
        lines.append(f"- 缺陷：{r['title']}")
    if r.get("environment"):
        lines.append(f"- 环境/沙箱：{r['environment']}")
    if r.get("repro_steps"):
        lines.append("- 复现（真实执行失败的命令）：")
        for cmd in r["repro_steps"][:5]:
            lines.append(f"    · {cmd}")
    if r.get("expected") and r.get("actual"):
        lines.append(f"- 期望/实际：{r['expected']} / {r['actual']}")
    if r.get("logs"):
        lines.append("- 日志与判负理由：")
        for log in r["logs"][:5]:
            lines.append(f"    · {log}")
    if r.get("affected"):
        lines.append("- 本轮**只允许**改动这些文件（其余文件一条 edit 都不要提交）：")
        for path, syms in list(r["affected"].items())[:10]:
            lines.append(f"    · {path}" + (f"（符号：{'、'.join(syms[:4])}）" if syms else ""))
    if r.get("materialized") is not None:
        lines.append(
            f"- 沙箱里已实际写入的文件：{'、'.join(r['materialized']) or '（无 —— 说明上一轮补丁没落盘）'}"
        )
    if r.get("fixes"):
        lines.append("- 评审/机制的返工项（逐条）：")
        for f in r["fixes"][:8]:
            lines.append(f"    · {f}")
    lines.append(
        "- 硬约束：① 已存在的文件只能 `modify` 定点改，**禁止** `add` 整份重吐（会重复定义）；"
        "② 不许重构、不许格式化无关代码、不许删测试或弱化断言来绕过；"
        "③ 修完以「上述失败命令转绿」为验收口径。"
    )
    return "\n".join(lines)
