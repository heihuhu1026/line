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
import sys
from typing import Any

#: 标准库 + 内置模块名（用于把"外部依赖"从"产物文件"里摘出来）
_EXTERNAL_MODULES = frozenset(getattr(sys, "stdlib_module_names", ()) or ()) | frozenset(
    sys.builtin_module_names
)

FEATURE = "feature"
BUGFIX = "bugfix"
#: **方案层返工后的施工轮**：按**刚重做的方案**做（方案可能新增了文件与符号，本次有权创建），
#: 与"在既有方案范围内做最小改动"是两类任务，必须分开。
#:
#: 为什么不能沿用 `BUGFIX`（真机 L2）：架构师返工后 `fixes` 里有 architect_fixes，
#: 旧判据「有没有 fixes」于是把这一轮判成 bugfix —— 而 bugfix 口径写着
#: "只改缺陷单指到的部分、其余文件划分一律不动"。方案刚重做过，dev 拿到的却是
#: 最小改动纪律 ⇒ 与"按新方案施工"正面对抗（dev 无权创建方案新增的文件，
#: 评审要求·开发做不到，白烧一轮）。
PLAN_REWORK = "plan_rework"

#: 全部任务类型（顺序即"信息量从少到多"，用于校验与展示）
ROUND_KINDS: tuple[str, ...] = (FEATURE, PLAN_REWORK, BUGFIX)

_ROUND_KIND_LABELS = {
    FEATURE: "首次开发",
    PLAN_REWORK: "方案返工后施工",
    BUGFIX: "缺陷修复",
}


def round_kind_label(kind: Any) -> str:
    """任务类型的人话标签（日志/页面用；未登记的原样返回，不抛异常）。"""
    text = str(kind or "")
    return _ROUND_KIND_LABELS.get(text, text)

#: 返工项里形如「[文件 cli.py]」的前缀（orchestrator 拼 fixes 时加的）
_FILE_PREFIX = re.compile(r"^\s*\[\s*文件\s*([^\]]+?)\s*\]")
#: traceback 里的位置行：`File "…/cli.py", line 17`
_TB_SITE = re.compile(r'File\s+"([^"]+)"\s*,\s*line\s+(\d+)')
#: `from X import …`（先挖掉，避免把被导入的符号当成模块名）
_FROM_IMPORT = re.compile(r"\bfrom\s+([A-Za-z_][\w.]*)\s+import\b")
#: 裸 `import a, b as c`：**逗号列表**也要逐个取（verify 的导入自检正是这种形式）
_BARE_IMPORT = re.compile(r"\bimport\s+([^;\"'\n]+)")
#: `python -c "import cli; ..."` / `python cli.py ...` → 命中的模块
_IMPORT_MODULE = re.compile(r"import\s+([A-Za-z_][\w.]*)")
_RUN_PY = re.compile(r"\bpython[0-9.]*\s+([^\s\"']+\.py)")


def _norm(path: Any) -> str:
    return str(path or "").replace("\\", "/").strip()


def _short(text: Any, limit: int = 160) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _cmd_text(text: Any, limit: int = 240) -> str:
    """命令文本的**保形**截断：保留换行，只压行尾空白，首尾各留一半。

    **不能用 `_short`**：它把 `\\n` 压成空格，而 verify 的导入自检命令正是一段
    **多行 Python 脚本**（`python -c "import importlib, sys\\nbad = []\\n…"`）——
    压平之后语法就废了，模型看到的是"一条不可能跑通的命令"（真机 `20260928-095848`
    的缺陷单里正是这样：`import importlib, sys bad = [] for name in …` 外加一个 `…`）。

    截断改成首尾各留一半：命令的**尾部常带真正的报错行**，只留头会把关键信息丢掉。
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    out = "\n".join(line.rstrip() for line in raw.split("\n")).strip()
    if len(out) <= limit:
        return out
    head = limit // 2
    return out[:head] + "\n…（中略）…\n" + out[-(limit - head):]


#: harness **自己造的**机械自检命令（导入检查 / 语法检查）。
#:
#: 它们出现在 verify 的失败命令里，但**不是"能复现的失败行为"**：dev 既修不了检查脚本本身，
#: 也不该把它当成复现步骤。真机 `20260928-095848` 的缺陷单把两条都塞进"复现"，
#: 其中第一条还是被压平过的多行脚本 ⇒ 让模型去"修一条它造不出来的命令"，纯噪声。
#: 所以分开呈现：真复现步骤进 `repro_steps`，机械自检进 `mechanical_checks`（只作证据）。
_MECHANICAL_MARKERS = ("IMPORT_CHECK", "py_compile", "compileall")


def _is_mechanical_command(command: Any) -> bool:
    text = str(command or "")
    return any(marker in text for marker in _MECHANICAL_MARKERS)


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
                # 命令用 `_cmd_text`（保形）而不是 `_short`（压平）：多行 `python -c` 脚本
                # 一旦被压成一行就语法作废，喂给模型的是一条跑不通的"复现步骤"。
                "command": _cmd_text(cmd.get("command"), 240),
                "status": status,
                "exit_code": cmd.get("exit_code"),
                "output": _short(cmd.get("output") or cmd.get("error") or "", 300),
            }
        )
    return out


def _module_to_path(module: str) -> str:
    return module.replace(".", "/") + ".py"


def _is_external_module(name: str) -> bool:
    """标准库 / 内置模块 —— **不是本产物的文件**。

    真机教训（`20260928-000351`）：verify 的导入自检命令形如
    `python -c "import importlib, sys; …"`，被 `_paths_from_command` 反推成 `importlib.py`，
    于是产生一条"缺陷指向方案未规划的文件（importlib.py）" ⇒ 机制**强制 rework_architect**，
    白烧一轮架构师。误报的代价从来不是"多打一行字"（§25 已付过学费）。
    """
    return str(name or "").split(".")[0] in _EXTERNAL_MODULES


def _imported_modules(command: str) -> list[str]:
    """从命令里抽出被 import 的**顶层模块名**。

    为什么不能只用一条正则：verify 的导入自检命令恰恰是 `import a, b, c` 这种**逗号列表**
    （形如 `python -c "import importlib, sys; …"`），原先的正则只抓到**第一个** ——
    于是 `affected`（进而 `allowed_scope`）偏窄，返工范围被无谓地缩小。
    这里两类写法都处理：`from X import …` 取 `X`，裸 `import a, b` 逐个取（各取顶层包名）。
    """
    text = str(command or "")
    out: list[str] = []
    # 先把 `from X import …` 整段挖掉（否则后面会把 `from cli import CLI` 里的 `CLI` 当模块名）
    for m in _FROM_IMPORT.finditer(text):
        out.append(m.group(1))
    masked = _FROM_IMPORT.sub(" ", text)
    for m in _BARE_IMPORT.finditer(masked):
        for part in m.group(1).split(","):
            name = part.strip().split(" as ")[0].strip()
            if re.match(r"^[A-Za-z_][\w.]*$", name):
                out.append(name)
    tops: list[str] = []
    for name in out:
        top = name.split(".")[0]
        if top and top not in tops:
            tops.append(top)
    return tops


def _paths_from_command(command: str) -> list[str]:
    """从一条失败命令里反推"大概是哪几个文件的问题"。

    **标准库模块必须排除**：`import importlib` 不是"产物里少了一个 importlib.py"
    （真机 `20260928-000351` 就因为这个误报强制了一轮架构师返工）。
    """
    out: list[str] = []
    for m in _RUN_PY.finditer(command):
        out.append(_norm(m.group(1)))
    for name in _imported_modules(command):
        if _is_external_module(name):
            continue
        out.append(_module_to_path(name))
    seen: list[str] = []
    for p in out:
        if p and p not in seen:
            seen.append(p)
    return seen


def bug_report_from_state(
    state: dict[str, Any],
    fixes: list[str] | None = None,
    plan: Any = None,
    sources: dict[str, str] | None = None,
) -> dict:
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
        # **修复项**：位置 + 问题 + 当前逐字原文 + 验收口径 + 归属施工图（见 defect_items）
        "items": defect_items(state, fixes, plan, sources),
        # **归因到施工图**（同一批修复项的另一个视图，共用一份实现）
        "by_task": by_task_attribution(state, fixes, plan, sources),
        "environment": str(verify.get("sandbox") or ""),
        # 复现步骤只给**用户行为**那类命令（`python main.py add 100`）；harness 自己的
        # 机械自检（导入检查/语法检查）另列 `mechanical_checks` —— 它们不是可复现的失败，
        # dev 也修不了检查脚本本身。没有真复现命令时才退回它，至少还有个证据。
        "repro_steps": [
            c["command"] for c in failed if not _is_mechanical_command(c.get("command"))
        ] or [c["command"] for c in failed],
        "mechanical_checks": [
            c["command"] for c in failed if _is_mechanical_command(c.get("command"))
        ],
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


def _files_to_tasks(plan: Any, path: str) -> list[str]:
    """文件 → 覆盖它的施工图号（按 `tasks[].target_files`）。信息不足返回空 —— **不猜**。"""
    target = _norm(path)
    if not target:
        return []
    out: list[str] = []
    for task in (_as_dict(plan).get("tasks") or []):
        if not isinstance(task, dict):
            continue
        files = {_norm(p) for p in (task.get("target_files") or []) if _norm(p)}
        tid = str(task.get("id") or "").strip()
        if tid and target in files and tid not in out:
            out.append(tid)
    return out


def _tasks_to_files(plan: Any, tid: str) -> list[str]:
    """施工图号 → 它覆盖的文件（`_files_to_tasks` 的反查）。"""
    want = str(tid or "").strip()
    if not want:
        return []
    out: list[str] = []
    for task in (_as_dict(plan).get("tasks") or []):
        if not isinstance(task, dict) or str(task.get("id") or "").strip() != want:
            continue
        for p in (task.get("target_files") or []):
            norm = _norm(p)
            if norm and norm not in out:
                out.append(norm)
    return out


def _match_known(raw: str, known: list[str]) -> str:
    """把 traceback 里的路径收敛成**已知文件**；收敛不了取末尾两段。

    必须收敛：traceback 给的是沙箱绝对路径
    （`D:\\AI\\line\\runs\\<id>\\verify\\work\\cli.py`），而缺陷单要用的是仓库相对路径
    （`cli.py`）—— 拿绝对路径去对 `target_files` 做归因，一条都对不上。
    """
    p = _norm(raw)
    low = p.lower()
    # **标准库 / 第三方 / 冻结帧不是产物文件**：`<frozen importlib._bootstrap>`、
    # `…/Python312/importlib/__init__.py`、`…/site-packages/xxx.py` 都可能出现在 traceback 里
    # （`from cli import CLI` 失败时一定有 importlib 的帧）。误当产物会触发"方案漏项"误判。
    if p.startswith("<") or "<frozen" in low or "site-packages" in low or "/lib/python" in low:
        return ""
    for marker in ("/verify/work/", "/verify/", "/work/"):
        if marker in p:
            p = p.split(marker, 1)[1]
            break
    for known_path in known:
        if p == known_path or p.endswith("/" + known_path):
            return known_path
    # 收敛不到已知文件时，**只有相对路径**才敢用末尾两段。绝对路径（盘符或前导斜杠）
    # 说明它在沙箱之外 ⇒ 不是产物文件 —— **不猜**（真机 `20260928-000351` 就是在这里放过了
    # 一个标准库路径，代价是一整轮架构师返工）。
    if re.match(r"^[A-Za-z]:", p) or p.startswith("/"):
        return ""
    parts = [x for x in p.split("/") if x]
    return "/".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "")


def plan_gap_files(items: Any, plan: Any) -> list[str]:
    """修复项指向的、**方案里没有任何 task 覆盖**的文件。

    这是"返工老是修不好"的一条根：返工的任务此前**只能**来自 `plan.tasks`（影响面 ∩
    target_files），于是"不属于任何施工图"的缺陷——跨文件集成问题、方案漏规划的文件——
    **没有任务可派**，调度就退化成整批重做（`_tasks_for_bugfix` 返回空 ⇒ 走两遍模式），
    最小改动整个丢掉。

    判出来的用途是**回方案补 changes / tasks**，不是硬塞给开发：开发受方案白名单约束，
    **它无权创建方案里没有的文件**（与 `_plan_missing_entry` 同一条道理——那边也是
    "方案没规划 ⇒ 开发做不到 ⇒ 两边都没错却不收敛"）。
    """
    covered = {
        _norm(p)
        for task in (_as_dict(plan).get("tasks") or [])
        if isinstance(task, dict)
        for p in (task.get("target_files") or [])
        if _norm(p)
    }
    out: list[str] = []
    for item in (items or []):
        if not isinstance(item, dict):
            continue
        path = _norm(item.get("path"))
        if path and path not in covered and path not in out:
            out.append(path)
    return out


def _numbered(text: str, start: int) -> str:
    """给原文加上**行号前缀**（`17| …`）：贴进提示词后，行号与文件能直接对上。"""
    return "\n".join(f"{start + i:>4}| {line}" for i, line in enumerate(str(text).splitlines()))


def defect_items(
    state: dict,
    fixes: list[str] | None = None,
    plan: Any = None,
    sources: dict[str, str] | None = None,
) -> list[dict]:
    """本轮**修复项**：每条都带「位置 / 问题 / 当前逐字原文 / 验收口径 / 归属施工图」。

    为什么要有这一层（root cause）：返工的修复方需要的是**可施工的描述** —— 改哪个文件、
    哪一处、那处现在**逐字**长什么样、怎么算修好。这四样全是机械可得的，之前一样都没给，
    于是模型只能凭记忆改写位置：

      真机 20260927-221511：anchor 被写成 `self.db.add_entry(amount, note)`，实际代码是
      `self.db.add_entry(args.amount)` ⇒ 永远套用不上 ⇒ 被累积成恒定判负项。

    来源（全部机械）：
      ① 补丁审计的非 ok 行（自带 path / symbol / status / notes / tasks）；
      ② verify 失败命令的 traceback（`File "…", line N` → 文件:行 → 取那几行原文）；
      ③ 评审返工项的「[文件 X]」前缀；
      ④ `implementation_audit.missing`（整张施工图一条补丁都没有）。
    """
    from . import patches as patches_mod  # 局部导入，避免环

    state = _as_dict(state)
    src = {_norm(k): str(v) for k, v in (sources or {}).items() if _norm(k)}
    audit = _as_dict(state.get("patch_audit"))
    impl_audit = _as_dict(state.get("implementation_audit"))
    verify = _as_dict(state.get("verify_report"))
    known = sorted(src)
    items: list[dict] = []
    seen: set[tuple] = set()

    def add(item: dict) -> None:
        path = _norm(item.get("path"))
        key = (path, str(item.get("symbol") or ""), item.get("line"), _short(item.get("what"), 90))
        if not path or key in seen:
            return
        seen.add(key)
        item["path"] = path
        # 归属施工图：① 补丁自己声明的 covers_tasks；② 按文件反查（都没有 ⇒ 空，不猜）
        tids = [str(t).strip() for t in (item.pop("_tasks", None) or []) if str(t).strip()]
        if not tids:
            tids = _files_to_tasks(plan, path)
        item["task"] = tids[0] if tids else ""
        # **当前逐字原文**：有符号就按符号取（最准），否则按行号取上下文
        verbatim = ""
        text = src.get(path)
        if text:
            excerpt = None
            if item.get("symbol"):
                excerpt = patches_mod.symbol_excerpt(text, str(item["symbol"]), context=1)
            line = item.get("line")
            if excerpt is None and isinstance(line, int) and line > 0:
                lines = text.splitlines()
                lo, hi = max(1, line - 1), min(len(lines), line + 1)
                excerpt = {"start": lo, "end": hi, "text": "\n".join(lines[lo - 1 : hi])}
            if excerpt:
                item["line"] = excerpt["start"]
                item["line_end"] = excerpt["end"]
                verbatim = _numbered(excerpt["text"], int(excerpt["start"]))
        if verbatim:
            item["verbatim"] = verbatim
        item["where"] = f"{path}:{item['line']}" if item.get("line") else path
        items.append(item)

    # ① 补丁判负
    for row in audit.get("edits") or []:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "")
        if status in ("", "ok"):
            continue
        detail = patches_mod.STATUS_CN.get(status, status)
        note = "；".join(str(x) for x in (row.get("notes") or [])[:1])
        symbol = str(row.get("symbol") or "")
        add(
            {
                "path": row.get("path"),
                "symbol": symbol,
                "what": f"{detail}：{symbol or row.get('path')}" + (f"（{note}）" if note else ""),
                "check": f"这条补丁要能被套用（不再出现「{detail}」）",
                "source": "patch_audit",
                "_tasks": row.get("tasks") or [],
            }
        )
    # ② verify 失败的 traceback：把"跑不起来"落到**具体行**
    for cmd in verify.get("commands") or []:
        if not isinstance(cmd, dict) or str(cmd.get("status") or "") not in ("fail", "error", "failed"):
            continue
        output = str(cmd.get("output") or cmd.get("error") or "")
        command = _short(cmd.get("command"), 120)
        for raw, line in _TB_SITE.findall(output):
            path = _match_known(raw, known)
            if not path:
                continue
            add(
                {
                    "path": path,
                    "line": int(line),
                    "what": f"运行验证失败：`{command}` 崩在这里（退出码 {cmd.get('exit_code')}）",
                    "check": f"命令 `{command}` 退出码 0",
                    # 存**完整**命令：逐项核对靠它精确匹配（展示用的是短版）
                    "command": str(cmd.get("command") or ""),
                    "source": "traceback",
                }
            )
        if not _TB_SITE.search(output):
            for path in _paths_from_command(str(cmd.get("command") or "")):
                add(
                    {
                        "path": path,
                        "what": f"运行验证失败：`{command}` 未通过（无 traceback 定位）",
                        "check": f"命令 `{command}` 退出码 0",
                        "command": str(cmd.get("command") or ""),
                        "source": "verify",
                    }
                )
    # ③ 评审返工项
    for item in fixes or []:
        m = _FILE_PREFIX.match(str(item or ""))
        if not m:
            continue
        # 去掉「[文件 X]」前缀：位置已经由 path 决定，前缀只会重复一遍
        bare = _short(_FILE_PREFIX.sub("", str(item or "")).strip(), 140)
        # 有些返工项本身就以"评审要求"开头 —— 别叠成"评审要求：评审要求…"
        head = "" if bare.startswith(("评审", "机制")) else "评审要求："
        add(
            {
                "path": m.group(1),
                "what": f"{head}{bare}",
                "check": "评审点到的这个问题必须消失",
                "source": "review",
            }
        )
    # ④ 整张施工图没做
    for tid in impl_audit.get("missing") or []:
        for path in _tasks_to_files(plan, str(tid)):
            add(
                {
                    "path": path,
                    "what": "本轮**没有任何补丁**覆盖这张图（等于这张图没做）",
                    "check": "这张图覆盖的符号都要被定义出来",
                    "source": "coverage",
                    "_tasks": [str(tid)],
                }
            )
    return items


def _cmd_key(command: str) -> str:
    """命令的归一化键（比对"是不是同一条命令"用）。"""
    return " ".join(str(command or "").split()).lower()


def defect_verdicts(
    state: dict,
    fixes: list[str] | None = None,
    plan: Any = None,
    sources: dict[str, str] | None = None,
) -> list[dict]:
    """每条修复项**逐项核对**：转绿了 / 仍失败 / **无从核对**。

    为什么必须有（"逐项可追溯"）：缺陷单给了 `check`（"命令 X 退出码 0"），但**没有人回来
    核对它** —— 于是"修好了没"只能整体看 verify 的 verdict，人工最终审核时无法逐项追
    "哪条修好了、哪条没修"。而这正是返工反复不收敛时最缺的可观测性。

    三类状态，**不允许第四类**（不许把"没核对"说成"通过"）：
      · `green`：匹配到的命令本轮**全部通过**；
      · `red`：匹配到的命令本轮**仍有失败**；
      · `unverifiable`：**没有任何命令能核对这一项** —— 显式披露。给不出数字时说"未测量"
        是本项目既有纪律（覆盖率就是这么办的），这里照同一条办。
    """
    verify = _as_dict(_as_dict(state).get("verify_report"))
    cmds = [c for c in (verify.get("commands") or []) if isinstance(c, dict)]
    by_cmd = {_cmd_key(str(c.get("command") or "")): c for c in cmds}
    out: list[dict] = []
    for item in defect_items(state, fixes, plan, sources):
        command = str(item.get("command") or "")
        key = _cmd_key(command)
        matched = [by_cmd[key]] if key and key in by_cmd else []
        if not matched:
            # 兜底：哪条命令**碰得到**这个文件（命令由测试阶段重新生成时，字符串对不上）
            path = str(item.get("path") or "")
            matched = [c for c in cmds if path and path in str(c.get("command") or "")]
        if not matched:
            status, detail = "unverifiable", "没有任何命令能核对这一项（未测量，不假装通过）"
        else:
            passed = all(str(c.get("status") or "") in ("ok", "pass", "passed") for c in matched)
            status = "green" if passed else "red"
            detail = "；".join(
                f"`{_short(c.get('command'), 80)}` → {c.get('status')}" for c in matched[:2]
            )
        out.append({**item, "status": status, "detail": detail})
    return out


def render_defect_verdicts(rows: Any) -> str:
    """逐项验收的**提示词形态**：只列**没转绿的**（转绿的不占评审预算）。

    给评审用。措辞要硬，但要给得起依据 —— 本项目反对的是"拿不出证据却判通过"，
    不反对"有证据地判通过"。
    """
    picked = [
        r for r in (rows or [])
        if isinstance(r, dict) and str(r.get("status") or "") in ("red", "unverifiable")
    ]
    if not picked:
        return ""
    lines = ["【逐项验收（本轮每条修复项的机械核对结果；**仍失败 / 无从核对**的都在这里）】"]
    for row in picked[:8]:
        label = "仍失败" if str(row.get("status")) == "red" else "无从核对"
        lines.append(f"- [{label}] {row.get('where')}：{_short(row.get('what'), 140)}")
        if row.get("check"):
            lines.append(f"    验收口径：{_short(row.get('check'), 120)}")
        if row.get("detail"):
            lines.append(f"    机械证据：{_short(row.get('detail'), 140)}")
    lines.append(
        "—— 判 pass 前必须**逐条**给出机械依据（说明它为什么不再是问题）。"
        "拿不出依据的，按 `in_material` 写进 required_fixes。"
    )
    return "\n".join(lines)


def render_defect_verdicts_markdown(rows: Any, *, limit: int = 12) -> list[str]:
    """逐项验收的**人读形态**（handoff 用）：三类都列，转绿的也留痕（可追溯）。"""
    out: list[str] = []
    for row in (rows or [])[:limit]:
        if not isinstance(row, dict):
            continue
        status = {"green": "转绿", "red": "仍失败", "unverifiable": "无从核对"}.get(
            str(row.get("status") or ""), str(row.get("status") or "")
        )
        line = f"- [{status}] {row.get('where')}：{_short(row.get('what'), 140)}"
        if row.get("detail"):
            line += f" —— {_short(row.get('detail'), 120)}"
        out.append(line)
    return out


def by_task_attribution(
    state: dict, fixes: list[str] | None = None, plan: Any = None,
    sources: dict[str, str] | None = None,
) -> dict[str, dict]:
    """把修复项**按施工图分组**（`{task_id: {files, problems}}`）。

    与 :func:`defect_items` **共用同一份实现**：分组只是同一批修复项的另一个视图。
    分成两套实现迟早会漂移，而"两处口径对不上"正是本项目反复踩的那类坑
    （§26 的 PM 闸门、§27 的补丁核对都是这么出事的）。
    """
    out: dict[str, dict] = {}
    for item in defect_items(state, fixes, plan, sources):
        tid = str(item.get("task") or "").strip()
        if not tid:
            continue
        row = out.setdefault(tid, {"files": [], "problems": []})
        path = _norm(item.get("path"))
        if path and path not in row["files"]:
            row["files"].append(path)
        text = str(item.get("what") or "").strip()
        if text and text not in row["problems"]:
            row["problems"].append(text)
    return out



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
    if not (
        r.get("repro_steps") or r.get("logs") or r.get("fixes")
        or r.get("affected") or r.get("items")
    ):
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
    if r.get("mechanical_checks"):
        # 机械自检单独列，并**明说它不是要你修的东西** —— 否则模型会去"修命令"。
        lines.append("- 机械自检失败（**harness 自己跑的命令，别改它本身**，看它报的错）：")
        for cmd in r["mechanical_checks"][:3]:
            lines.append(f"    · {cmd}")
    if r.get("expected") and r.get("actual"):
        lines.append(f"- 期望/实际：{r['expected']} / {r['actual']}")
    if r.get("logs"):
        lines.append("- 日志与判负理由：")
        for log in r["logs"][:5]:
            lines.append(f"    · {log}")
    items = [x for x in (r.get("items") or []) if isinstance(x, dict)]
    if items:
        # **修复项**：这一段是返工描述的主体。每条都给"在哪儿、什么问题、**那处现在逐字
        # 长什么样**、怎么算修好"。给逐字原文是刻意的：不给就只能凭记忆改写位置
        # （真机 20260927-221511 的 anchor 就是近似行 ⇒ 永远套用不上）。
        grouped: dict[str, list[dict]] = {}
        for item in items:
            grouped.setdefault(str(item.get("task") or ""), []).append(item)
        lines.append("- **修复项**（按施工图分组；没被点到的图本轮不要碰）：")
        for tid in sorted(grouped, key=lambda k: (k == "", k)):
            group = grouped[tid]
            files = "、".join(sorted({str(x.get("path")) for x in group if x.get("path")}))
            label = f"{tid}（{files}）" if tid else f"（**不属于任何施工图**：{files}）"
            lines.append(f"    · {label}：")
            for item in group[:4]:
                lines.append(f"        - [{item.get('where')}] {_short(item.get('what'), 160)}")
                if item.get("verbatim"):
                    lines.append("          当前原文（**逐字对齐，不要凭记忆改写**）：")
                    for raw in str(item["verbatim"]).splitlines()[:6]:
                        lines.append(f"          {raw}")
                if item.get("check"):
                    lines.append(f"          验收：{_short(item.get('check'), 120)}")
    elif r.get("by_task"):
        # 兜底：没有修复项（老产物/信息不足）时，退回只列"哪个 task 有什么问题"
        lines.append("- **按施工图归因**（哪个 task、什么具体问题；没被点到的图本轮不要碰）：")
        for tid, row in list(r["by_task"].items())[:8]:
            files = "、".join(row.get("files") or []) or "（未记录文件）"
            lines.append(f"    · {tid}（{files}）：")
            for problem in (row.get("problems") or [])[:3]:
                lines.append(f"        - {problem}")
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
