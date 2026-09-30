"""语义分析（pyright）：语法 / 导入检查之外的「类型感知」屏障。

**为什么必须有它**：``verify`` 的 ast 层只能做**字面**判定 —— 语法（py_compile）、
跨模块未定义名、import 契约。而真机上最难发现的一类缺陷恰好是它抓不到的：

    g.wrong_method(1)          # 属性不存在
    g.move(1, 2)               # 参数个数不匹配
    h = make_game(); h.foo()   # 跨函数**返回值**的类型推断

2026-09-25 实测：pyright 在 5 文件样本上 2.1s 就抓到上面三条，全部是 ast 抓不到的；
而且 ``run_command`` 也常常覆盖不到它们 —— 那些代码可能在**未被执行的路径**上。

**设计要点**：

1. **可选增强，绝不硬依赖。** pyright 装在全局 npm 目录，换机器就没有。
   :func:`available` 探测不到时，所有接口返回空结果 + 原因，流水线照常跑。
2. **只报本轮产出的文件。** 存量代码的既有诊断一律不报 —— 实测本项目 pipeline 目录
   本身就有 20 条既有 error（reportPossiblyUnboundVariable 等），
   不做这层过滤会把真正要看的问题整个淹掉。
3. **分级。** 只把 :data:`config.LSP_BLOCKING_RULES` 里的高置信规则交给 dev 重问；
   推断性结论只进报告。误报的代价是一整轮返工，比漏报贵得多。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import LSP_BLOCKING_RULES, LSP_MAX_DIAGNOSTICS, LSP_TIMEOUT

#: Windows 下隐藏子进程窗口
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

#: 「允许执行的程序」白名单之外的东西不碰 —— pyright 由本模块自行探测调用，
#: 不走 verify 的命令白名单（它不执行待验证代码，只做静态分析）。
_ENTRY_NAMES = ("pyright.cmd", "pyright", "pyright.ps1")
_LANGSERVER_NAMES = (
    "pyright-langserver.cmd", "pyright-langserver", "pyright-langserver.ps1",
)

_cached_entry: Path | None | bool = False  # False = 还没探测过
_cached_langserver: Path | None | bool = False


def _find(names: tuple[str, ...], which: str) -> Path | None:
    """按名字探测可执行文件。

    Windows 上 npm 全局包装器有 .cmd/.ps1 两种，``shutil.which`` 依赖 PATHEXT，
    所以再补一轮按名字的显式探测 —— 实测环境里 npm 全局目录并不总在 PATH 生效。
    """
    hit = shutil.which(which)
    if hit:
        return Path(hit)
    for base in (
        Path.home() / "AppData" / "Roaming" / "npm",
        Path(os.environ.get("APPDATA", "")) / "npm",
        Path("/usr/local/bin"),
        Path("/usr/bin"),
    ):
        for name in names:
            probe = base / name
            if probe.exists():
                return probe
    return None


def pyright_entry() -> Path | None:
    """定位 pyright CLI（做诊断用）；找不到返回 None（结果会缓存）。"""
    global _cached_entry
    if _cached_entry is not False:
        return _cached_entry  # type: ignore[return-value]
    _cached_entry = _find(_ENTRY_NAMES, "pyright")
    return _cached_entry


def langserver_entry() -> Path | None:
    """定位 pyright 的 LSP server（做引用查找用）；找不到返回 None（结果会缓存）。"""
    global _cached_langserver
    if _cached_langserver is not False:
        return _cached_langserver  # type: ignore[return-value]
    _cached_langserver = _find(_LANGSERVER_NAMES, "pyright-langserver")
    return _cached_langserver


def available() -> bool:
    """pyright 是否可用（供调用方决定走不走语义检查）。"""
    return pyright_entry() is not None


def unavailable_reason() -> str:
    return (
        "未找到 pyright，语义检查已跳过"
        "（安装：npm i -g pyright；或设 PIPELINE_LSP=0 显式关闭）"
    )


def diagnose(
    work: Path | str,
    rel_files: list[str] | None = None,
    *,
    timeout: int = LSP_TIMEOUT,
) -> dict[str, Any]:
    """对 ``work`` 跑一次 pyright，返回**只看 rel_files** 的结构化诊断。

    参数
    ----
    work
        要被分析的目录（通常是物化后的沙箱）。pyright 以它作为项目根。
    rel_files
        本轮产出文件的相对路径。只保留这些文件的诊断（见模块注释第 2 点）。
        传 None 表示不过滤（调用方自己清楚在看什么时才这么用）。
    """
    entry = pyright_entry()
    root = Path(work)
    out: dict[str, Any] = {
        "available": entry is not None,
        "reason": "",
        "diagnostics": [],
        "total": 0,
        "filtered_out": 0,
        "elapsed_s": 0.0,
    }
    if entry is None:
        out["reason"] = unavailable_reason()
        return out
    if not root.is_dir():
        out["reason"] = f"目录不存在：{root}"
        return out

    wanted = (
        {str(p).replace("\\", "/") for p in rel_files}
        if rel_files is not None
        else None
    )

    started = time.time()
    try:
        proc = subprocess.run(  # noqa: S603
            [str(entry), "--outputjson"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        out["reason"] = f"pyright 超过 {timeout}s 未结束（已终止）"
        out["elapsed_s"] = round(time.time() - started, 2)
        return out
    except (OSError, ValueError) as exc:
        # 环境层问题（包装器不可执行等）不该判交付物有罪
        out["reason"] = f"pyright 调用失败：{type(exc).__name__}: {exc}"
        out["elapsed_s"] = round(time.time() - started, 2)
        return out
    out["elapsed_s"] = round(time.time() - started, 2)

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        out["reason"] = (
            f"pyright 输出不是 JSON（rc={proc.returncode}）：{(proc.stdout or '')[:200]}"
        )
        return out

    rows: list[dict[str, Any]] = []
    for item in data.get("generalDiagnostics") or []:
        if not isinstance(item, dict):
            continue
        raw_file = str(item.get("file") or "")
        try:
            rel = Path(raw_file).resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            rel = raw_file.replace("\\", "/")
        if wanted is not None and rel not in wanted:
            out["filtered_out"] += 1
            continue
        rng = (item.get("range") or {}).get("start") or {}
        rule = str(item.get("rule") or "")
        rows.append(
            {
                "file": rel,
                "line": int(rng.get("line", 0)) + 1,  # pyright 是 0-based
                "column": int(rng.get("character", 0)) + 1,
                "severity": str(item.get("severity") or ""),
                "rule": rule,
                # pyright 的消息里带 \xa0（不换行空格）做缩进，进 prompt 会显示成怪字符
                "message": " ".join(
                    str(item.get("message") or "").replace("\xa0", " ").split()
                ),
                # 高置信（可回灌给 dev 触发重问）vs 推断性（只进报告）
                "blocking": rule in LSP_BLOCKING_RULES,
            }
        )
    out["total"] = len(rows)
    # 高置信的排前面：回灌给模型时先给最该修的
    rows.sort(key=lambda r: (not r["blocking"], r["file"], r["line"]))
    out["diagnostics"] = rows[:LSP_MAX_DIAGNOSTICS] if LSP_MAX_DIAGNOSTICS > 0 else rows
    return out


def problem_lines(result: dict[str, Any], limit: int = 6) -> list[str]:
    """把诊断压成给模型看的一行行问题描述（只取高置信项）。"""
    if not result or not result.get("available"):
        return []
    out: list[str] = []
    for item in result.get("diagnostics") or []:
        if not item.get("blocking"):
            continue
        out.append(
            f"`{item['file']}` 第 {item['line']} 行：{item['message']}"
            + (f"（{item['rule']}）" if item.get("rule") else "")
        )
        if len(out) >= limit:
            break
    return out


def summary_line(result: dict[str, Any]) -> str:
    """一行摘要，进日志/报告。"""
    if not result:
        return "语义检查：未运行"
    if not result.get("available"):
        return f"语义检查：跳过（{result.get('reason') or '不可用'}）"
    if result.get("reason"):
        return f"语义检查：{result['reason']}"
    blocking = sum(1 for d in result.get("diagnostics") or [] if d.get("blocking"))
    return (
        f"语义检查：{result.get('total', 0)} 条问题"
        f"（高置信 {blocking} 条），{result.get('elapsed_s', 0)}s"
        + (
            f"，另有 {result['filtered_out']} 条属存量代码（未计入）"
            if result.get("filtered_out")
            else ""
        )
    )


# ---------------------------------------------------------------------------
# Grounded Requirement Contract（P0-1）
# ---------------------------------------------------------------------------
# 为什么挂在 semantics.py：本模块已经是「文本/代码 → 结构化事实」的语义判读处（pyright）。
# 需求原文同样是语义提取，且本轮明确**不新建第二套语义模块**，因此放这里。
#
# 真值红线（与 Ontology 一致）：
#   * 只有能在 original_requirement 里**逐字找到**的东西才配 ASSERTED；
#   * Intake / PM 的输出一律 DERIVED，**绝不**回写进 ASSERTED 集合；
#   * 拿不准 → grounding_errors，宁可报错也不猜（猜出来的"事实"比缺失更危险）。
#
# 实测前提：真机 run 20260930-000332 存在 state.json 里的 requirement **换行符数量为 0**
# （拍平成单行，Markdown 表格靠 `||` 分隔）。所以这里**不能按行解析**，一律按
# `。 ； | 换行` 切成句段；同一套代码对换行完整的原文照样成立。

#: 要识别的交付文件后缀（刻意不含无后缀的模块名 —— `game_logic` 不是文件）
_FILE_EXT_RE = re.compile(r"(?<![\w.])[A-Za-z0-9_][A-Za-z0-9_\-]*\.(?:py|js|ts|json|md)(?![\w])")

#: 硬约束触发词。命中即尝试提升为 hard_constraints
_HARD_MARKERS: tuple[str, ...] = (
    "不得", "必须", "只允许", "禁止", "硬约束", "不允许", "必须能", "只能",
    "绝不能", "固定为", "视为不合格", "硬性要求", "不可", "不应",
)

#: 「这确实是交付文件」的上下文证据词
_FILE_CONTEXT_MARKERS: tuple[str, ...] = (
    "文件", "path", "文件路径", "文件结构", "必须包含", "入口", "模块", "职责", "分层",
)

#: 明确排除（用户说"不做"）
_EXCLUSION_MARKERS: tuple[str, ...] = ("不做", "不需要", "不实现", "不包含", "不支持", "不读写", "不使用")

#: 验收章节标题特征
_ACCEPT_MARKERS: tuple[str, ...] = ("验收依据", "验收条件", "可观察行为")

#: 事实性陈述（技术栈 / 启动方式 / 允许依赖）
_FACT_MARKERS: tuple[str, ...] = (
    "固定为", "只允许", "启动方式", "标准库", "入口", "技术栈", "双击运行", "第三方库",
)

_ASSERTED = "ASSERTED"
_DERIVED = "DERIVED"

_CONF_RANK = {"high": 3, "medium": 2, "low": 1}


def _segments(text: str) -> list[tuple[int, int]]:
    """把原文切成句段索引区间。

    切点含 `|`：真机原文把 Markdown 表格拍平成 ``a.py` | 职责 || `b.py` | ...`，
    不按 `|` 断开的话，「文件表格」会和后面那句硬约束糊成一整段，
    导致 source_quote 变成一大坨表格 —— 那既不proof也不好用。
    返回的切片保证是**原文子串**（source_quote 可逐字校验的前提）。
    """
    spans: list[tuple[int, int]] = []
    start = 0
    for i, ch in enumerate(text):
        if ch in "。；\n|":
            if i > start:
                spans.append((start, i + 1))
            start = i + 1
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _clean(text: str) -> str:
    """去掉 Markdown 强调/代码标记，只用于展示；校验一律用原文字段 source_quote。"""
    return " ".join(text.replace("**", "").replace("`", "").split())


def _in_original(quote: str, text: str) -> bool:
    """source_quote 必须能在原文逐字找到 —— 这是 ASSERTED 的唯一凭据。"""
    return bool(quote) and quote in text


def _section_tail(text: str, *keywords: str) -> str:
    """返回首个命中 keyword 的章节**正文**（keyword 之后，到下一个 `##` 标题为止）。"""
    for kw in keywords:
        idx = text.find(kw)
        if idx < 0:
            continue
        nxt = text.find("##", idx + len(kw))
        body = text[idx + len(kw): nxt if nxt > 0 else len(text)]
        return body
    return ""


def _declared_files(text: str) -> tuple[list[dict], list[dict]]:
    """抽取用户**明确声明**的交付文件。

    提升规则（严格按优先级，越靠前置信越高）：
      1. Markdown 表格 + 代码 span → high
      2. 表格行 / 出现「文件/path/职责/入口」等上下文 → medium
      3. 仅代码 span → medium
      4. 都没有 → **不猜**，进 grounding_errors
    """
    found: dict[str, dict] = {}
    errors: list[dict] = []
    has_table = "||" in text or "|" in text
    for m in _FILE_EXT_RE.finditer(text):
        raw = m.group(0)
        path = raw.replace("\\", "/").strip()
        if not path:
            continue
        s, e = m.span()
        window = text[max(0, s - 60): e + 60]
        in_code = f"`{raw}`" in text
        in_table = has_table and "|" in window
        ctx_hit = next((k for k in _FILE_CONTEXT_MARKERS if k in window), "")
        if in_table and in_code:
            evidence, conf = "markdown_table", "high"
        elif in_table:
            evidence, conf = "table_row", "medium"
        elif ctx_hit:
            evidence, conf = "file_context", "medium"
        elif in_code:
            evidence, conf = "code_span", "medium"
        else:
            errors.append({
                "code": "FILE_NO_CONTEXT",
                "path": path,
                "detail": "出现文件名但无表格/文件上下文证据，未提升为 declared file（不猜）",
            })
            continue
        quote = raw if _in_original(raw, text) else path
        prev = found.get(path)
        if prev is None or _CONF_RANK[conf] > _CONF_RANK[prev["confidence"]]:
            found[path] = {
                "path": path,
                "source": "user",
                "truth": _ASSERTED,
                "source_quote": quote,
                "evidence": evidence,
                "confidence": conf,
            }
    ordered = sorted(found.values(), key=lambda f: (-_CONF_RANK[f["confidence"]], f["path"]))
    return ordered, errors


def _hard_constraints(text: str) -> tuple[list[dict], list[dict]]:
    """抽取硬约束。命中触发词的句段即候选；source_quote 校验不过则降级为 error。"""
    out: list[dict] = []
    errors: list[dict] = []
    for s, e in _segments(text):
        seg = text[s:e]
        hit = [k for k in _HARD_MARKERS if k in seg]
        if not hit:
            continue
        quote = seg.strip().rstrip("。；|").strip()
        # 标题行本身不是约束（真机原文拍平后「## 2. 文件与分层（硬约束）」会自成一段，
        # 它命中「硬约束」这个词；真正的约束是它后面那句，已被 `|` 切成独立段）
        if quote.startswith("#"):
            continue
        if not _in_original(quote, text):
            # 理论不会发生（切片即子串），留着是防御：一旦发生说明切分逻辑被改坏
            errors.append({"code": "CONSTRAINT_QUOTE_NOT_FOUND", "detail": quote[:80]})
            continue
        out.append({
            "id": f"constraint:{len(out) + 1:02d}",
            "text": _clean(quote),
            "truth": _ASSERTED,
            "source_quote": quote,
            "severity": "hard",
            "markers": hit,
        })
    return out, errors


def _acceptance_items(text: str) -> list[dict]:
    """「验收依据」章节的编号条目 —— 用户自己写的验收条件，不靠 Intake/PM 重新生成。"""
    body = _section_tail(text, *_ACCEPT_MARKERS)
    if not body:
        return []
    out: list[dict] = []
    for raw in re.split(r"(?=\d+\.\s)", body):
        item = raw.strip()
        m = re.match(r"^(\d+)\.\s*", item)
        if not m:
            continue
        quote = item.rstrip("。").strip()
        body_text = re.sub(r"^\d+\.\s*", "", quote)
        if not body_text:
            continue
        out.append({
            "id": f"acceptance:{len(out) + 1:02d}",
            "text": _clean(body_text),
            "truth": _ASSERTED if _in_original(quote, text) else _DERIVED,
            "source_quote": quote if _in_original(quote, text) else "",
        })
    return out


def _exclusions(text: str) -> list[dict]:
    """「明确不做」—— 用户主动排除的范围。"""
    body = _section_tail(text, "明确不做", "不做")
    if not body:
        return []
    out: list[dict] = []
    for m in re.finditer(r"[-•]\s*([^。；]{2,80}?)[。；]", body):
        item = m.group(1).strip()
        if not any(k in item for k in _EXCLUSION_MARKERS):
            continue
        quote = m.group(0).strip().rstrip("。；")
        out.append({
            "id": f"exclusion:{len(out) + 1:02d}",
            "text": _clean(item),
            "truth": _ASSERTED if _in_original(quote, text) else _DERIVED,
            "source_quote": quote if _in_original(quote, text) else "",
        })
    return out


def _behavior_claims(text: str) -> list[dict]:
    """玩法/操作/界面等**用户陈述的行为**（后续要变成 ProofObligation 的来源）。"""
    start_keys = ("玩法规则", "玩法", "操作", "界面")
    start, hit_kw = -1, ""
    for kw in start_keys:
        start = text.find(kw)
        if start >= 0:
            hit_kw = kw
            break
    if start < 0:
        return []
    end = len(text)
    for kw in _ACCEPT_MARKERS + ("明确不做",):
        idx = text.find(kw, start)
        if idx > 0:
            end = min(end, idx)
    # 从命中词**之后**开始取，否则第一条声明会带上「玩法规则」这种标题前缀
    region = text[start + len(hit_kw): end]
    out: list[dict] = []
    seen: set[str] = set()
    for part in re.split(r"[。；]", region):
        item = part.strip().lstrip("-•").strip()
        if len(item) < 4:
            continue
        key = _clean(item)
        if key in seen:
            continue
        seen.add(key)
        quote = part.strip().rstrip("。；")
        out.append({
            "id": f"behavior:{len(out) + 1:02d}",
            "text": key,
            "truth": _ASSERTED if _in_original(quote, text) else _DERIVED,
            "source_quote": quote if _in_original(quote, text) else "",
        })
    return out


def _source_facts(text: str) -> list[dict]:
    """技术栈 / 启动方式 / 允许依赖这类事实性陈述。"""
    out: list[dict] = []
    seen: set[str] = set()
    for s, e in _segments(text):
        seg = text[s:e]
        hit = [k for k in _FACT_MARKERS if k in seg]
        if not hit:
            continue
        quote = seg.strip().rstrip("；|").strip().rstrip("。").strip()
        if quote.startswith("#"):
            continue
        key = _clean(quote)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({
            "id": f"fact:{len(out) + 1:02d}",
            "text": key,
            "truth": _ASSERTED if _in_original(quote, text) else _DERIVED,
            "source_quote": quote if _in_original(quote, text) else "",
            "markers": hit,
        })
    return out


def _derived_from_intake(intake: dict) -> list[dict]:
    """Intake 的产物**只**能以 DERIVED 出现，绝不进 ASSERTED 集合。"""
    out: list[dict] = []
    for key in ("background", "target_users", "pending_items", "key_behaviors"):
        if key not in intake:
            continue
        value = intake.get(key)
        if value in (None, "", [], {}):
            continue
        out.append({
            "id": f"derived:{len(out) + 1:02d}",
            "key": key,
            "value": value,
            "truth": _DERIVED,
            "source": "intake",
        })
    return out


def build_requirement_contract(
    original_requirement: str,
    intake: dict | None = None,
) -> dict:
    """把「用户原始需求」编译成可断言的 Grounded Requirement Contract（纯函数，零模型）。

    这是 P0-1 的地基：上游（Intake/PM/Architect）丢文件、丢硬约束的根因是
    **需求事实从来没有被固化成一个可校验的对象**，只散在提示词里，谁都能改写它。

    参数
    ----
    original_requirement
        用户原文（**不是** Intake 的转述）。
    intake
        Intake 产物；只用于生成 `derived_facts`，truth 恒为 DERIVED。

    返回
    ----
    dict，含 ``declared_files`` / ``hard_constraints`` / ``explicit_exclusions`` /
    ``acceptance_items`` / ``behavior_claims`` / ``source_facts``（均为 ASSERTED 或留空）、
    ``derived_facts``（恒 DERIVED）、``grounding_errors``（无法确认的，绝不猜）。
    """
    text = str(original_requirement or "")
    intake = intake if isinstance(intake, dict) else {}

    files, file_errors = _declared_files(text)
    constraints, con_errors = _hard_constraints(text)
    contract: dict[str, object] = {
        "version": 1,
        "declared_files": files,
        "hard_constraints": constraints,
        "explicit_exclusions": _exclusions(text),
        "acceptance_items": _acceptance_items(text),
        "behavior_claims": _behavior_claims(text),
        "source_facts": _source_facts(text),
        "derived_facts": _derived_from_intake(intake),
        "grounding_errors": [],
    }

    errors: list[dict] = list(file_errors) + list(con_errors)
    if not text.strip():
        errors.append({"code": "EMPTY_REQUIREMENT", "detail": "需求原文为空，无法建立契约"})
    # 自查：凡是标了 ASSERTED 的，source_quote 必须逐字可查；查不到就撤销 ASSERTED
    for bucket in ("declared_files", "hard_constraints", "explicit_exclusions",
                   "acceptance_items", "behavior_claims", "source_facts"):
        rows = contract[bucket]  # type: ignore[index]
        assert isinstance(rows, list)
        for row in rows:
            quote = str(row.get("source_quote") or "")
            if row.get("truth") == _ASSERTED and not _in_original(quote, text):
                errors.append({
                    "code": "ASSERTED_WITHOUT_QUOTE",
                    "bucket": bucket,
                    "detail": f"标了 ASSERTED 但原文查不到：{quote[:60]}",
                })
                row["truth"] = _DERIVED
                row["source_quote"] = ""
    contract["grounding_errors"] = errors
    return contract


def _norm_path(value: object) -> str:
    """路径归一（去 `./` 前缀、反斜杠转正斜杠），用于方案与声明文件比对。"""
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def plan_missing_declared_files(contract: dict | None, plan: Any) -> list[str]:
    """用户声明的交付文件里，方案 `changes` **没有覆盖**的那些（P0-2 / Design Gate 硬规则）。

    这是问题 A 的机械兜底：光把契约喂给架构师不够（它仍可能静默删），
    必须在闸门里**确定性**判一次缺失，直接 `rework_architect`，
    而不是等 DEV / Test / Review 才发现测试文件从计划里消失了。

    纯函数：不读 state、不碰模型；契约缺失（老 run 没有该字段）时返回空 —— 向后兼容。
    """
    if not isinstance(contract, dict) or not isinstance(plan, dict):
        return []
    declared: list[str] = []
    for item in contract.get("declared_files") or []:
        if isinstance(item, dict) and item.get("path"):
            declared.append(_norm_path(item["path"]))
    if not declared:
        return []
    changes = [c for c in (plan.get("changes") or []) if isinstance(c, dict)]
    # 方案还没有 changes（None / {} / 空列表）＝ 还没有方案内容，不做判定。
    # 否则会把「方案尚未产出」这个中间态误报成「用户文件全被删了」。
    if not changes:
        return []
    planned: set[str] = set()
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = _norm_path(change.get("path"))
        if path:
            planned.add(path)
            # 方案可能写 `src/game_logic.py` 这种带目录形式，按 basename 也算命中
            planned.add(path.rsplit("/", 1)[-1])
    seen: set[str] = set()
    missing: list[str] = []
    for path in declared:
        if path in seen:
            continue
        seen.add(path)
        if path not in planned and path.rsplit("/", 1)[-1] not in planned:
            missing.append(path)
    return missing


def _selftest() -> int:
    """直接跑 `python -m pipeline.semantics` 时自检一次（排查环境问题用）。"""
    print("python :", sys.executable)
    print("pyright:", pyright_entry() or "(未找到)")
    print("available:", available())
    print("blocking rules:", ", ".join(sorted(LSP_BLOCKING_RULES)))
    return 0 if available() else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_selftest())
