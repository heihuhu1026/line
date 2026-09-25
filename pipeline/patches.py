"""锚定补丁的机械校验、落盘与套用。

**为什么需要它**：契约只能要求模型给出 `anchor` / `patch`，但"这段补丁到底能不能贴回去、贴回去
是不是等于它声称的改动"必须**机械核对** —— 否则评审只能靠猜。
真机教训（2026-09-23，run 20260923-011410）：9 条补丁的 `anchor` 全都指向同一个 `index_all` 签名行，
其中 8 条是新增小函数、1 条声称改 `index_all` 却只给了 587 字符；评审看不懂这些，所以给了 pass。

声明式语义（`patch_mode`，由开发显式声明，机器据此校验与套用）：

| mode | 语义 | 机械校验 |
|---|---|---|
| `insert_after` | 把 patch 插到 anchor 之后 | anchor 必须能唯一定位 |
| `replace_span` | patch 替换 anchor 覆盖的代码 | anchor 唯一 + patch 不能与原文完全相同 |
| `full_symbol` | patch 是该符号（函数/类）的**完整**替代 | anchor 唯一 + patch 必须定义该符号 + patch 行数不得显著小于原文该符号的行数 |

产出：`runs/<id>/patches/*.patch`（带真实行号的 unified diff，可直接 `git apply` / `patch -p1`），
以及 `tools/apply_patches.py`（默认 dry-run，可 `--in-place` 套用到仓库副本或原位）。
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

from . import retrieval

DIFF_RE = re.compile(r"^\s*(?:@@|---|\+\+\+|diff --git)", re.M)
DEF_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class|function|func|sub|public|private|protected|internal|static)\b")
# full_symbol 模式下 patch 行数不得少于原文该符号行数的这个比例，否则判定"补丁不完整"
INCOMPLETE_RATIO = 0.3
CONTEXT_LINES = 3

PATCH_MODES = ["insert_after", "replace_span", "full_symbol"]

# 顶层 import（`import x` / `from x import y`，且不在缩进里）—— 合并新增文件时用来提顶去重
IMPORT_RE = re.compile(r"^(?:import|from)\s")

STATUS_CN = {
    "ok": "可套用",
    "unchecked": "未核对（没提供仓库）",
    "anchor_not_found": "anchor 在原文里找不到",
    "anchor_ambiguous": "anchor 在原文里不唯一",
    "symbol_not_found": "原文里没有这个符号",
    "patch_symbol_missing": "patch 里没有定义声明的符号",
    "patch_incomplete": "声称完整替换但 patch 明显不完整",
    "patch_span_mismatch": "anchor 覆盖范围与补丁内容不匹配（会留残码）",
    "symbol_already_exists": "要新增的符号原文里已存在",
    "patch_no_effect": "patch 与原文完全相同（等于没改）",
    "already_applied": "看起来已经应用过了",
    "new_file_duplicate_symbol": "同一个新文件里重复定义了同一个符号（合并后会出现两份定义）",
    "new_file_syntax_error": "新增文件的内容本身有语法错误（写残/未闭合）",
}

#: 会做语法级校验的「代码类」后缀（只在新增文件上用；非代码文件不碰）
CODE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java",
    ".kt", ".cs", ".c", ".cc", ".cpp", ".h", ".hpp", ".rb", ".php", ".swift", ".m", ".mm",
}


def _balance_problem(text: str) -> str | None:
    """扫描括号 / 引号 / 三引号是否配平，返回人类可读的出错位置（没问题返回 None）。

    为什么不用 AST：这一步要在**补丁还没落盘**时跑，而且要给模型看得懂的定位信息；
    手写扫描器只认 4 类状态，不确定的一律放过（宁可漏报，不要误伤合法代码）。
    """
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[tuple[str, int]] = []
    i = 0
    line = 1
    n = len(text)
    while i < n:
        ch = text[i]
        # 裸 CR 也要算行尾：Python 的编译期把它当换行（universal newlines），
        # 只在 `\n` 上断行的话，`end='<CR>'` 这类会漏报成"配平正常"。
        if ch in "\r\n":
            line += 1
            if ch == "\r" and i + 1 < n and text[i + 1] == "\n":
                i += 1  # CRLF 算一个换行
            i += 1
            continue
        # 注释必须在引号之前判：`# 这里有个单引号` 不是字符串
        if ch == "#":
            nxt = text.find("\n", i)
            i = n if nxt < 0 else nxt
            continue
        if text.startswith('"""', i) or text.startswith("'''", i):
            quote = text[i : i + 3]
            end = text.find(quote, i + 3)
            if end < 0:
                return f"第 {line} 行：三引号 {quote} 未闭合"
            line += text.count("\n", i, end)
            i = end + 3
            continue
        if ch in "\"'":
            j = i + 1
            closed = False
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] in "\r\n":
                    break  # 单行字符串不允许跨行（CR 也算换行）
                if text[j] == ch:
                    closed = True
                    break
                j += 1
            if not closed:
                return f"第 {line} 行：字符串 {ch} 未闭合（行末仍在字符串里）"
            i = j + 1
            continue
        if ch in "([{":
            stack.append((ch, line))
            i += 1
            continue
        if ch in ")]}":
            if not stack:
                return f"第 {line} 行：多余的 {ch}"
            open_ch, open_line = stack.pop()
            if pairs[ch] != open_ch:
                return f"第 {line} 行：{ch} 与第 {open_line} 行的 {open_ch} 不匹配"
            i += 1
            continue
        i += 1
    if stack:
        open_ch, open_line = stack[-1]
        return f"第 {open_line} 行：{open_ch} 未闭合"
    return None


def _top_level_imports(text: str) -> set[str]:
    """取源码里出现的 import 基础模块名；解析失败返回空集（交给别的检查报）。"""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(str(alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入不判（依赖包结构，判不准）
                continue
            if node.module:
                names.add(str(node.module).split(".")[0])
    return names


def unavailable_imports(content: str, path: str, own_modules: set[str] | None = None) -> list[str]:
    """找出「既不是标准库、本环境也装不上、又不是本项目文件」的 import。

    真机教训（run 20260924-185507 第 6 轮）：7B **自己发明了第三方依赖** `import keyboard`
    —— 方案里从没提过、环境里也没装（`pygame` 是装了的），结果整份交付 import 就崩，
    只能等 verify 跑完一整轮才发现。

    这类问题**必须由机制举证**：模型根本不知道运行环境里装了什么，靠提示词叮嘱无效
    （与"裸 CR"同一类：可机械判定、模型无法自查）。判据三条，全部本地、毫秒级：
      ① 在 `sys.stdlib_module_names` / `sys.builtin_module_names` 里 → 放过
      ② 在 `own_modules`（本项目自己的文件/包）里 → 放过
      ③ `importlib.util.find_spec(name)` 能解析到 → 放过；否则就是**装不上**的依赖
    """
    if Path(str(path or "")).suffix.lower() != ".py":
        return []
    own = {str(x).strip().lower() for x in (own_modules or set()) if str(x).strip()}
    stdlib = set(getattr(sys, "stdlib_module_names", frozenset())) | set(sys.builtin_module_names)
    out: list[str] = []
    for name in sorted(_top_level_imports(content)):
        if name in stdlib or name.lower() in own or name.startswith("_"):
            continue
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError, AttributeError, TypeError):
            found = False
        if not found:
            out.append(f"`{name}`：既不是标准库、本环境也没安装，方案里也没声明这个依赖")
    return out


def normalize_patch_text(text: str) -> tuple[str, int]:
    """把补丁正文里的**裸 CR**（回车）转义成 ``\\r``，返回 ``(新文本, 修了几处)``。

    为什么必须在机制层修：模型想写 Python 的 ``\\r`` 转义（例如
    ``print(..., end='\\r')`` 让光标回到行首重画），但在 JSON 里只写了一个反斜杠
    ``"...end='\\r'..."`` —— **JSON 解码后那是一个真实的 CR 字符**，
    落进源码的字符串里就变成「单引号字符串跨行」的语法错误。

    真机 run 20260924-185507 连续 4 轮栽在同一处（``end='<CR>'``）：模型从失败反馈里
    根本看不出这是编码层问题，只会一遍遍重写同一处代码，永远修不掉。这类问题只能在
    补丁进入流水线之前归一 —— 归属判断（是模型写错还是我们转码错）对结果没有影响，
    因为**裸 CR 落在源码字符串里必然是语法错误，不存在「合法」的解读**。

    只处理**不跟在 ``\\n`` 前面的 CR**（裸 CR）：CRLF 是正常行尾，不动它。
    """
    fixed = 0
    out: list[str] = []
    for index, ch in enumerate(text):
        if ch == "\r" and (index + 1 >= len(text) or text[index + 1] != "\n"):
            out.append("\\r")
            fixed += 1
            continue
        out.append(ch)
    return "".join(out), fixed


def normalize_implementation(impl: dict | None) -> int:
    """对整份实现产物做补丁正文归一，返回修正处数（0 = 没动过）。

    ``patch`` 与 ``anchor`` 都要过一遍：anchor 是「逐字复制原文」，如果原文里有 CR，
    锚点匹配（`_norm` 只 strip 首尾）同样会失配。
    """
    total = 0
    for edit in (impl or {}).get("edits") or []:
        if not isinstance(edit, dict):
            continue
        for key in ("patch", "anchor"):
            value = edit.get(key)
            if isinstance(value, str) and "\r" in value:
                new, count = normalize_patch_text(value)
                if count:
                    edit[key] = new
                    total += count
    return total


def check_new_file_content(content: str, path: str) -> str | None:
    """对**新增文件**的内容做语法级校验，返回问题描述（没问题返回 None）。

    没有原文可核对，**不等于内容不用看**。真机 run 20260924-185507：dev 把 `renderer.py`
    写残（停在 `print(f'{`，整份只有 277 字节），而这里原先一律判 ok /「整份写入」，
    一路放行到 verify 才炸 —— 白烧 test + verify + review 一整轮（那台机器上 ≈5 分钟 + 一次 14B 评审）。

    两道检查都是标准库、毫秒级：
      ① 括号 / 引号 / 三引号配平（语法错误里最常见的就是这个，且能给出人类可读定位）
      ② `compile()` 语法检查（仅 .py；其余语言只做①）
    """
    suffix = Path(str(path or "")).suffix.lower()
    if suffix not in CODE_SUFFIXES:
        return None
    if not content.strip():
        return "新增文件的内容为空"
    problem = _balance_problem(content)
    if problem:
        return problem
    if suffix == ".py":
        try:
            compile(content, str(path or "<new file>"), "exec")
        except SyntaxError as exc:
            return f"第 {exc.lineno or '?'} 行：{exc.msg}"
        except ValueError as exc:  # 例如源码里含空字节
            return f"无法编译：{exc}"
    return None


def _norm(line: str) -> str:
    return line.strip()


def find_anchor(text: str, anchor: str) -> list[tuple[int, int]]:
    """按「逐行去掉首尾空白后完全一致」匹配 anchor，返回 [(起始行, 结束行)]（0-based，含端点）。

    **空行在两侧都忽略**。早先的写法只把 anchor 的空行丢掉、却保留原文的空行，
    于是 src 里有个 '' 而 anc 里没有，逐行比对必然不等 ——
    **任何跨空行的 anchor 永远匹配不上**。Python 里方法之间隔一个空行是常态，
    等于让这类补丁一律被判 ``anchor_not_found``；也让 ``already_applied``
    对多行补丁形同失效（同一改动会被重复套用）。
    现在两侧都按「非空行」序列比对，命中后再映射回**原文真实行号**，
    调用方拿到的 span 仍可直接用来切片。
    """
    raw = text.splitlines()
    keep = [i for i, line in enumerate(raw) if _norm(line)]
    src = [_norm(raw[i]) for i in keep]
    anc = [_norm(line) for line in str(anchor or "").splitlines() if _norm(line)]
    if not anc or not src:
        return []
    hits: list[tuple[int, int]] = []
    span = len(anc)
    for idx in range(len(src) - span + 1):
        if src[idx : idx + span] == anc:
            hits.append((keep[idx], keep[idx + span - 1]))
    return hits


def _symbol_matches(lines: list[str], symbol: str) -> list[int]:
    pattern = re.compile(rf"^\s*(?:async\s+)?(?:def|class|function|func|sub|public|private|protected|internal|static)\b[^\n]*\b{re.escape(symbol)}\b")
    return [idx for idx, line in enumerate(lines) if pattern.match(line)]


def symbol_span(lines: list[str], symbol: str) -> tuple[int, int] | None:
    """找符号的定义区间（用缩进判断块边界）。

    **比 retrieval 的块提取少一层：去掉尾随空行。** 那块逻辑是给「展示代码片段」用的，
    把符号后的空行一起带上无可厚非；但这里的调用方是 ``patch_span_mismatch`` 的判据 ——
    它拿这个区间和 anchor 比**行数**，多算一个空行就会让 span 凭空大 1。

    而 Python 里方法后面跟一个空行是**常态**，于是判据 ``span 行数 > anchor 行数``
    恒成立：模型老老实实把整个符号抄进 anchor，照样被判「会留残码」打回
    （2026-09-25 实测复现：anchor 定位到 (1,2) 两行，symbol_span 却是 (1,3) 三行）。
    ``full_symbol`` 模式的替换范围同样受益 —— 不再顺手删掉符号之间的空行。
    """
    for idx in _symbol_matches(lines, symbol):
        block = retrieval.symbol_span(lines, idx)
        if block:
            start, end = block[0], block[1]
            while end > start and not lines[end].strip():
                end -= 1
            return start, end
    return None


def _new_file_body(patch_text: str) -> str:
    """从「新增文件」的补丁里取出文件正文。

    约定（见 ``render_new_file_patch`` / ``merge_new_file_blocks``）：新增文件的 patch
    **就是文件正文**，不是 diff。但模型偶尔会直接给 unified diff，所以这里兼容一下：
    是 diff 时取 ``+``/`` `` 行、跳过 ``+++``/``---``/``@@`` 头。
    """
    if not DIFF_RE.search(patch_text):
        return patch_text
    lines: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith(("+", " ")):
            lines.append(line[1:])
    return "\n".join(lines) + "\n"


def analyze_edit(source: str | None, edit: dict) -> dict:
    """核对单条补丁：anchor 能否定位、与声明语义是否自洽。"""
    patch_text = str(edit.get("patch") or "")
    out: dict[str, Any] = {
        "path": str(edit.get("path") or ""),
        "symbol": str(edit.get("target_symbol") or ""),
        "change_type": str(edit.get("change_type") or ""),
        "patch_mode": str(edit.get("patch_mode") or ""),
        "patch_chars": len(patch_text),
        "patch_lines": len([line for line in patch_text.splitlines() if line.strip()]),
        "patch_kind": "diff" if DIFF_RE.search(patch_text) else "block",
        "covered_tasks": list(edit.get("covers_tasks") or []),
        "status": "unchecked",
        "notes": [],
    }
    if source is None:
        return out
    if not patch_text.strip():
        out["status"] = "patch_no_effect"
        out["notes"].append("patch 为空")
        return out
    if source == "":
        # 内容校验不在这里做：它**与仓库无关**，统一放到 analyze_all 的独立一遍里跑，
        # 这样 repo 缺失（新建项目没给 --repo）时也能拦住语法垃圾。
        out["status"] = "ok"
        out["patch_mode_used"] = "new_file"
        out["notes"].append("新增文件：没有原文可核对，整份写入")
        return out
    if DIFF_RE.search(patch_text):
        # 已经给了 diff：anchor 只用于展示/校验位置，不做块级替换
        out["notes"].append("patch 本身就是 unified diff，套用时直接用它")
        out["status"] = "ok" if (out["symbol"] and out["symbol"] in patch_text) else "ok"
        if out["patch_mode"] == "full_symbol":
            out["notes"].append("声明 full_symbol 但给的是 diff（可接受，以 diff 为准）")
        return out

    source_lines = source.splitlines()
    mode = out["patch_mode"] or ("insert_after" if out["change_type"] == "add" else "replace_span")
    out["patch_mode_used"] = mode
    # full_symbol 的 patch 自带完整符号定义、定位靠 target_symbol，anchor 对它只是辅助信息；
    # 其余两种模式必须靠 anchor 才能贴回原位，缺了就无从套用。
    # （曾经无条件要求 anchor 命中，于是 full_symbol 补丁留空 anchor 时被误判成
    #   anchor_not_found —— 明明能靠符号定位却判负，是纯误伤）
    anchor_text = str(edit.get("anchor") or "")
    if mode != "full_symbol" or anchor_text.strip():
        hits = find_anchor(source, anchor_text)
        out["anchor_hits"] = len(hits)
        if not hits:
            out["status"] = "anchor_not_found"
            return out
        if len(hits) > 1:
            out["status"] = "anchor_ambiguous"
            out["anchor_spans"] = [[a + 1, b + 1] for a, b in hits]
            return out
        start, end = hits[0]
        out["anchor_span"] = [start + 1, end + 1]
    else:
        out["anchor_hits"] = 0
        out["notes"].append("full_symbol 未给 anchor：按 target_symbol 定位")

    if find_anchor(source, patch_text):
        out["status"] = "already_applied"
        out["notes"].append("patch 的内容已经能在原文中找到")
        return out

    symbol = out["symbol"]
    if mode == "full_symbol":
        if symbol and not _symbol_matches(patch_text.splitlines(), symbol):
            out["status"] = "patch_symbol_missing"
            out["notes"].append(f"patch 里没有 `{symbol}` 的定义")
            return out
        span = symbol_span(source_lines, symbol) if symbol else None
        if span is None:
            out["status"] = "symbol_not_found" if symbol else "patch_symbol_missing"
            return out
        out["symbol_span"] = [span[0] + 1, span[1] + 1]
        old_lines = span[1] - span[0] + 1
        if old_lines >= 6 and out["patch_lines"] < old_lines * INCOMPLETE_RATIO:
            out["status"] = "patch_incomplete"
            out["notes"].append(
                f"`{symbol}` 在原文有 {old_lines} 行，patch 只有 {out['patch_lines']} 行"
                f"（<{int(INCOMPLETE_RATIO * 100)}%），不可能是一次完整替换"
            )
            return out
        out["status"] = "ok"
        return out

    # insert_after / replace_span 的精确检查
    symbol_exists = bool(symbol) and bool(_symbol_matches(source_lines, symbol))
    if mode == "insert_after" and out["change_type"] == "add" and symbol_exists:
        out["status"] = "symbol_already_exists"
        out["notes"].append(f"原文里已经有 `{symbol}`，insert_after 会造成重复定义（改用 replace_span / full_symbol）")
        return out
    if mode == "replace_span" and symbol_exists:
        span = symbol_span(source_lines, symbol)
        if span and _symbol_matches(patch_text.splitlines(), symbol) and (span[1] - span[0] + 1) > (end - start + 1):
            out["status"] = "patch_span_mismatch"
            out["symbol_span"] = [span[0] + 1, span[1] + 1]
            out["notes"].append(
                f"patch 里给的是 `{symbol}` 的完整定义，但 anchor 只覆盖第 {start + 1}-{end + 1} 行"
                f"（`{symbol}` 在原文有第 {span[0] + 1}-{span[1] + 1} 行）：替换后会留下原函数体，"
                "必须改用 full_symbol，或把整个函数作为 anchor"
            )
            return out
    if out["change_type"] == "add" and symbol and not _symbol_matches(patch_text.splitlines(), symbol):
        out["notes"].append(f"patch 里没有 `{symbol}` 的定义行（新增符号建议带上定义）")
    if mode == "replace_span" and _norm("".join(source_lines[start : end + 1])) == _norm(patch_text):
        out["status"] = "patch_no_effect"
        return out
    out["status"] = "ok"
    return out


def merge_new_file_blocks(patches_text: list[str]) -> str:
    """把指向**同一个新文件**的多段 patch 合并成一份文件内容。

    为什么必须合并：方案常把多个类放进同一个新文件（例如 game_logic.py 里的
    Snake/Food/Collision/Game），开发就会各出一条 `full_symbol` 补丁；目标文件不存在时
    它们都是 ``new_file``「整份写入」。逐条 write 会**后写覆盖先写、静默丢代码**
    （真机教训 run 20260924-135801：4 条补丁落盘后只剩 Game 一个类，而审计报 0 问题）。

    顺带把各段里的**顶层 import** 提到文件顶部并去重 —— 分段生成时每段都会重写一遍
    `from typing import ...`，不提顶就会得到一堆重复导入。缩进里的 import 原地保留。
    """
    imports: list[str] = []
    bodies: list[str] = []
    for text in patches_text:
        lines = str(text or "").splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        body: list[str] = []
        for line in lines:
            if line[:1].isspace() or not IMPORT_RE.match(line):
                body.append(line)
                continue
            if line not in imports:
                imports.append(line)
        while body and not body[0].strip():
            body.pop(0)
        while body and not body[-1].strip():
            body.pop()
        if body:
            bodies.append("\n".join(body))
    parts: list[str] = []
    if imports:
        parts.append("\n".join(imports))
    parts.extend(bodies)
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n"


def analyze_all(repo: str | Path | None, impl: dict | None) -> dict:
    """核对整份实现产物。repo 为空时返回 unchecked（不产生问题，只说明无法核对）。"""
    edits = [e for e in ((impl or {}).get("edits") or []) if isinstance(e, dict)]
    audit: dict[str, Any] = {
        "source_available": bool(repo),
        "edits": [],
        "ok": 0,
        "problems": 0,
        "problem_detail": [],
    }
    cache: dict[str, str | None] = {}
    for edit in edits:
        path = str(edit.get("path") or "")
        source: str | None = None
        if repo:
            if path not in cache:
                file_path = Path(repo) / path
                try:
                    cache[path] = file_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    cache[path] = None
            source = cache[path]
            if source is None:
                # 允许新增文件（add）：此时没有原文可核对
                source = "" if edit.get("change_type") == "add" else None
        row = analyze_edit(source, edit)
        if source is None and repo:
            row["status"] = "unchecked"
            row["notes"].append("目标文件不存在（可能是新增文件），无法核对")
        audit["edits"].append(row)

    # 新增文件的**内容**校验：不依赖原文，只看补丁本身 —— 所以单独跑一遍而不是塞进
    # analyze_edit，这样 repo 缺失（新建项目没给 --repo）时同样生效。
    # 真机 run 20260924-185507：dev 把 renderer.py 写残（`print(f'{`，277 字节），
    # 原先一律判 ok /「整份写入」，一路放行到 verify 才炸 —— 白烧 test+verify+review 一整轮。
    for row, edit in zip(audit["edits"], edits):
        if row.get("patch_kind") != "block" or str(edit.get("change_type") or "") != "add":
            continue  # diff 取不全文；非 add 交给各自的检查
        if row["status"] not in ("ok", "unchecked"):
            continue  # 已经有更具体的问题，不再叠一条
        problem = check_new_file_content(_new_file_body(str(edit.get("patch") or "")), row["path"])
        if problem:
            row["status"] = "new_file_syntax_error"
            row["notes"].append(f"新增文件内容有语法问题：{problem}（整份写入后跑不起来）")

    # 跨补丁核对：同一个**新文件**上的多条补丁必须能合并写入。
    # 单条看都没问题，但「同一路径被整份写入多次」是跨补丁的冲突 —— 逐条校验看不见它，
    # 所以要在这里单独查，否则会静默丢代码（真机教训 run 20260924-135801）。
    new_paths: dict[str, list[dict]] = {}
    for row in audit["edits"]:
        if row.get("patch_mode_used") == "new_file":
            new_paths.setdefault(row["path"], []).append(row)
    for path, rows in new_paths.items():
        seen: dict[str, dict] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "")
            if not symbol:
                continue
            first = seen.get(symbol)
            if first is None:
                seen[symbol] = row
                continue
            for dup in (first, row):
                if dup["status"] == "ok":
                    dup["status"] = "new_file_duplicate_symbol"
                if "合并后会是两份定义" not in " ".join(dup["notes"]):
                    dup["notes"].append(
                        f"`{symbol}` 在同一个新文件 `{path}` 里被重复定义（合并后会得到两份定义）"
                    )
        if len(rows) > 1:
            for row in rows:
                row["notes"].append(
                    f"新增文件 `{path}`：与另外 {len(rows) - 1} 条补丁**合并写入同一份文件**"
                    "（各自单独套用会互相覆盖）"
                )

    # 统计放在状态改写之后算，避免计数与被改写后的状态不一致
    for row in audit["edits"]:
        if row["status"] == "ok":
            audit["ok"] += 1
        else:
            audit["problems"] += 1
            audit["problem_detail"].append(
                f"{row['symbol'] or row['path']}：{STATUS_CN.get(row['status'], row['status'])}"
            )
    return audit


# --------------------------------------------------------------------- 落盘
def render_patch(source: str, edit: dict, row: dict) -> str:
    """把一条补丁渲染成带真实行号的 unified diff（可直接 git apply / patch -p1）。"""
    patch_text = str(edit.get("patch") or "")
    if row.get("patch_kind") == "diff":
        body = patch_text if patch_text.endswith("\n") else patch_text + "\n"
        return f"--- a/{row['path']}\n+++ b/{row['path']}\n{body}"

    lines = source.splitlines()
    mode = row.get("patch_mode_used") or "insert_after"
    if mode == "full_symbol" and row.get("symbol_span"):
        start, end = row["symbol_span"][0] - 1, row["symbol_span"][1] - 1
    else:
        anchor_start, anchor_end = (row.get("anchor_span") or [1, 1])[0] - 1, (row.get("anchor_span") or [1, 1])[1] - 1
        start, end = (anchor_end + 1, anchor_end) if mode == "insert_after" else (anchor_start, anchor_end)

    ctx_start = max(start - CONTEXT_LINES, 0)
    lead = [f" {line}" for line in lines[ctx_start:start]]
    added = [f"+{line}" for line in patch_text.splitlines()]
    if end >= start:  # 替换：标出被删掉的原行 + 尾部上下文
        removed = [f"-{line}" for line in lines[start : end + 1]]
        ctx_end = min(end + CONTEXT_LINES, len(lines) - 1)
        tail = [f" {line}" for line in lines[end + 1 : ctx_end + 1]]
        header_old = len(lead) + len(removed) + len(tail)
        header_new = len(lead) + len(added) + len(tail)
        body = [*lead, *removed, *added, *tail]
    else:  # 纯插入：只带前置上下文
        header_old = len(lead)
        header_new = len(lead) + len(added)
        body = [*lead, *added]
    head = f"@@ -{ctx_start + 1},{header_old} +{ctx_start + 1},{header_new} @@"
    return "\n".join([f"--- a/{row['path']}", f"+++ b/{row['path']}", head, *body]) + "\n"


def render_new_file_patch(path: str, patch_text: str) -> str:
    """新增文件的 unified diff（--- /dev/null）。"""
    body = [f"+{line}" for line in str(patch_text or "").splitlines() or [""]]
    return "\n".join(["--- /dev/null", f"+++ b/{path}", f"@@ -0,0 +1,{len(body)} @@", *body]) + "\n"


def write_patch_files(run_dir: Path, repo: str | Path | None, impl: dict | None, audit: dict) -> list[dict]:
    """把可套用的补丁写成 runs/<id>/patches/NN-<symbol>.patch，返回落盘清单。

    同一个**新文件**上的多条补丁会**合并成一份** diff（否则逐个 `git apply` 会互相覆盖）：
    文件名取该路径名、序号取这组第一条的序号，组内所有行都指向同一个文件。
    """
    if not repo or not impl:
        return []
    out_dir = Path(run_dir) / "patches"
    written: list[dict] = []
    edits = [e for e in (impl.get("edits") or []) if isinstance(e, dict)]
    rows = audit.get("edits") or []

    new_groups: dict[str, list[tuple[int, dict, dict]]] = {}
    for index, (edit, row) in enumerate(zip(edits, rows), 1):
        if row.get("status") == "ok" and row.get("patch_mode_used") == "new_file":
            new_groups.setdefault(str(edit.get("path") or ""), []).append((index, edit, row))
    for path, group in new_groups.items():
        stem = re.sub(r"[^A-Za-z0-9_\-]", "_", Path(path).stem) or "new_file"
        name = f"{group[0][0]:02d}-{stem}.patch"
        merged = merge_new_file_blocks([str(e.get("patch") or "") for _, e, _ in group])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / name).write_text(render_new_file_patch(path, merged), encoding="utf-8")
        for _, _, row in group:
            row["patch_file"] = f"patches/{name}"
            row["patch_file_merged"] = len(group)
            written.append({"file": row["patch_file"], "symbol": row.get("symbol"),
                            "path": path, "merged": len(group)})

    for index, (edit, row) in enumerate(zip(edits, rows), 1):
        if row.get("status") != "ok" or row.get("patch_mode_used") == "new_file":
            continue
        path = str(edit.get("path") or "")
        try:
            source = (Path(repo) / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            source = ""
        if not source:
            continue
        text = render_patch(source, edit, row)
        out_dir.mkdir(parents=True, exist_ok=True)
        symbol = re.sub(r"[^A-Za-z0-9_\-]", "_", row.get("symbol") or "patch") or "patch"
        name = f"{index:02d}-{symbol}.patch"
        (out_dir / name).write_text(text, encoding="utf-8")
        row["patch_file"] = f"patches/{name}"
        written.append({"file": row["patch_file"], "symbol": row.get("symbol"), "path": path})
    return written


# --------------------------------------------------------------------- 套用
def _apply_one(source_lines: list[str], edit: dict, row: dict) -> list[str]:
    mode = row.get("patch_mode_used") or "insert_after"
    patch_lines = str(edit.get("patch") or "").splitlines()
    if mode == "full_symbol" and row.get("symbol_span"):
        start, end = row["symbol_span"][0] - 1, row["symbol_span"][1] - 1
    else:
        start, end = (row.get("anchor_span") or [1, 1])[0] - 1, (row.get("anchor_span") or [1, 1])[1] - 1
        if mode == "insert_after":
            start, end = end + 1, end
    return [*source_lines[:start], *patch_lines, *source_lines[end + 1 :]]


def _symbol_text(source: str, symbol: str, *, whole: bool) -> str | None:
    """从原文里取某符号的文本：``whole`` 时取**整个符号**，否则只取**定义签名**。

    取不到、或同名符号不止一处时返回 None —— 宁可让原逻辑照旧判负，
    也绝不猜一个位置贴上去（贴错比不贴危险得多）。
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    hits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == symbol
    ]
    if len(hits) != 1:
        return None
    node = hits[0]
    lines = source.splitlines()
    start = int(getattr(node, "lineno", 0) or 0)
    if start <= 0 or start > len(lines):
        return None
    if whole:
        end = int(getattr(node, "end_lineno", 0) or start)
    else:
        # 签名可能跨多行（参数换行 / 返回类型注解换行）：取到函数体第一行之前
        body = getattr(node, "body", None) or []
        body_start = int(getattr(body[0], "lineno", start + 1) or (start + 1)) if body else start + 1
        end = max(start, body_start - 1)
    end = min(max(end, start), len(lines))
    text = "\n".join(lines[start - 1 : end]).rstrip()
    return text or None


def repair_anchors(repo: str | Path | None, impl: dict | None) -> dict[str, Any]:
    """用 ``target_symbol`` 从原文**补全被缩写的 anchor**（原地修改 impl）。

    真机高频病：模型把 ``def hello(self, name, greeting='hi')`` 抄成 ``def hello(self)``，
    ``find_anchor`` 自然找不到 → 判 ``anchor_not_found`` 打回，白烧一轮。
    而原文里那个符号是**唯一**的，从 ast 就能拿到真实签名 —— 这类返工纯属浪费。

    三道安全闸门（全过才替换）：
      1. 只处理 ``replace_span`` / ``insert_after`` —— ``full_symbol`` 靠 target_symbol
         定位，anchor 对它本来就是辅助信息；
      2. 符号在原文里**唯一**出现；
      3. 补全后的 anchor 确实能在原文里**唯一定位**。

    任何一条不满足就原样返回，让原来的判负逻辑照常生效。
    """
    report: dict[str, Any] = {"repaired": 0, "detail": []}
    if not repo or not impl:
        return report
    root = Path(repo)
    for edit in (impl.get("edits") or []):
        if not isinstance(edit, dict):
            continue
        symbol = str(edit.get("target_symbol") or "").strip()
        if not symbol or str(edit.get("patch_mode") or "") == "full_symbol":
            continue
        path = str(edit.get("path") or "")
        if not path:
            continue
        try:
            source = (root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        anchor = str(edit.get("anchor") or "")
        # 已经能唯一定位：不动（哪怕抄得跟原文不完全一样，只要定位得到就不碰）
        if anchor and len(find_anchor(source, anchor)) == 1:
            continue
        mode = str(edit.get("patch_mode") or "")
        patch_lines = str(edit.get("patch") or "").splitlines()
        # replace_span 的 anchor 就是**要被替换的范围**：
        #   · patch 给的是该符号的完整定义 ⇒ anchor 必须覆盖整个符号，
        #     只补签名行会变成「换掉 def 那行、原函数体留成残码」（judged patch_span_mismatch）；
        #   · patch 是片段 ⇒ 无从推断它想替换哪几行，**不猜**，让原逻辑照旧判负。
        patch_is_whole = bool(_symbol_matches(patch_lines, symbol))
        if mode == "replace_span":
            if not patch_is_whole:
                continue
            fixed = _symbol_text(source, symbol, whole=True)
        else:
            # insert_after 只是拿 anchor 定位（patch 是插进去的新代码），签名行就够
            fixed = _symbol_text(source, symbol, whole=False)
        if not fixed or len(find_anchor(source, fixed)) != 1:
            continue
        edit["anchor"] = fixed
        report["repaired"] += 1
        report["detail"].append(f"{path}::{symbol}")
    return report


#: 幂等命中：内容本就在目标文件里，跳过是**正确行为**而不是交付物残缺。
#: 同一份审计会被套用多次（闸门预览 → 放行后正式交付、续跑再结束各一次），
#: 第二次必然全部命中 —— 若把它当「没能套上」，重复交付会被误判成部分交付。
BENIGN_SKIP_STATUS = "already_applied"


def _rel_under(repo: Path | str, path: str) -> str:
    """把补丁路径规范成**相对 repo** 的形式。

    为什么必须有：模型非常喜欢给绝对路径，而 pathlib 里 ``out / path`` 遇到绝对路径时
    **直接返回 path 本身**（绝对路径整体吃掉前面的 out）。后果是指定的 ``out_dir``
    什么都没收到，文件反而被写回了原仓库 —— 即使调用方明确传了 ``in_place=False``。

    真机 run 20260925-221002：verify 阶段就把产物写进了用户目标目录（绕过交付门禁），
    沙箱 ``verify/work`` 却是空的，于是后续命令全在一个空目录里跑，
    验证结论与真实交付物完全脱节 —— 这正是「几轮都判过、交付却跑不了」的成因。

    不在 repo 内的绝对路径原样返回：那种情况由交付环节的越界检查拦截，
    这里不擅自改写调用方语义。
    """
    text = str(path or "").strip()
    if not text:
        return text
    candidate = Path(text)
    if not candidate.is_absolute():
        return text.replace("\\", "/")
    try:
        rel = candidate.resolve().relative_to(Path(repo).resolve())
    except (ValueError, OSError):
        return text
    return rel.as_posix()


def _normalize_edit_path(repo: Path | str, edit: dict) -> dict:
    """返回 path 已归一（必要时）的 edit 副本；无需改动时返回原对象。"""
    raw = str(edit.get("path") or "")
    rel = _rel_under(repo, raw)
    return edit if rel == raw else {**edit, "path": rel}


def is_benign_skip(item: dict | None) -> bool:
    """这条「跳过」是否属于幂等命中（无需再套，不算残缺）。"""
    if not isinstance(item, dict):
        return False
    if str(item.get("status") or "") == BENIGN_SKIP_STATUS:
        return True
    # 兜底：早期产物/其它调用路径可能没带 status，退回文案匹配
    return STATUS_CN[BENIGN_SKIP_STATUS] in str(item.get("reason") or "")


def is_already_applied(source_lines: list[str], edit: dict) -> bool:
    """这条补丁的内容是否已经在**当前**文件里了（套用前的幂等闸）。

    判据与 analyze 阶段的 ``already_applied`` 完全一致（``find_anchor`` 逐行去空白匹配），
    区别只在于看的是哪一份原文：analyze 看的是**当时**那份，这里看的是**此刻**这份。

    为什么必须在套用前再查一次：同一份审计会被套用多次 ——
      1) 人工审核闸门的「预览物化」先写一遍，人工放行后的「正式交付」又拿同一份
         缓存审计写第二遍（``_deliver_preview`` → ``_deliver``）；
      2) 运行结束落一次，续跑再结束时又落一次。
    第二次读到的已经是改过的文件，而行号/符号都还原封不动地留在审计里，
    于是同一个改动被**追加第二遍**。真机复现：``full_symbol`` 补丁连套两次 →
    同一个方法在文件里出现两份（``def mul`` 出现 2 次）。
    """
    return bool(find_anchor("\n".join(source_lines), str(edit.get("patch") or "")))


def apply_all(repo: str | Path, impl: dict | None, audit: dict, in_place: bool = False,
              out_dir: str | Path | None = None) -> dict:
    """按审计结果套用补丁。

    **安全约定**：只有显式 `in_place=True` 才会写回原仓库，且会先留 `*.orig` 备份；
    否则必须给 `out_dir`（把结果写到副本里），绝不动原仓库。
    """
    repo = Path(repo)
    if not in_place and out_dir is None:
        raise ValueError("非 in_place 模式必须指定 out_dir（不要把结果写回原仓库）")
    edits = [e for e in ((impl or {}).get("edits") or []) if isinstance(e, dict)]
    # 路径归一必须在这里做：``out / path`` 遇到绝对路径会整体覆盖 out，
    # 于是 in_place=False 照样写回原仓库、out_dir 落空（见 _rel_under 的注释）。
    # 只改 path 字段、不增删条目 —— edits 与 audit["edits"] 是靠 zip 一一对应的。
    edits = [_normalize_edit_path(repo, e) for e in edits]
    rows = audit.get("edits") or []
    by_path: dict[str, list[tuple[dict, dict]]] = {}
    for edit, row in zip(edits, rows):
        # new_file 的行不进这条路径：目标文件本来就不在仓库里，逐条套用只会得到
        # 一堆误导性的「跳过：文件不存在」，真正该做的是下面的合并写入。
        if (row.get("status") == "ok" and row.get("patch_kind") == "block"
                and row.get("patch_mode_used") != "new_file"):
            by_path.setdefault(str(edit.get("path") or ""), []).append((edit, row))

    report: dict[str, Any] = {"in_place": in_place, "files": [], "skipped": []}
    # 越界路径：绝对路径且不在 repo 内。``in_place=False`` 的语义是「写到 out_dir」，
    # 而绝对路径会整体吃掉 out_dir、写到文件系统任意位置 —— 一律拒绝并记 skipped。
    # （交付环节另有越界检查拦在前面，这里兜住的是物化/沙箱这条路径。）
    blocked: set[str] = {
        str(e.get("path") or "") for e in edits
        if str(e.get("path") or "") and Path(str(e.get("path") or "")).is_absolute()
    }
    for path in sorted(blocked):
        report["skipped"].append(
            {"path": path, "reason": f"路径越出仓库（绝对路径且不在 {repo} 内），拒绝写入"}
        )
    # 新增文件：**同一路径的多条补丁合并成一份**再写出。
    # 不能逐条 write_text —— 后写会把先写覆盖掉、静默丢代码
    # （真机教训 run 20260924-135801：game_logic.py 的 Snake/Food/Collision/Game
    #  四条补丁落盘后只剩 Game 一个类）。
    new_paths: dict[str, list[tuple[dict, dict]]] = {}
    for edit, row in zip(edits, rows):
        if row.get("status") == "ok" and row.get("patch_mode_used") == "new_file":
            new_paths.setdefault(str(edit.get("path") or ""), []).append((edit, row))
    for path, items in new_paths.items():
        if path in blocked:
            continue
        text = merge_new_file_blocks([str(e.get("patch") or "") for e, _ in items])
        out = repo if in_place else Path(out_dir)  # type: ignore[arg-type]
        dest = out / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        report["files"].append(
            {"path": path, "written": str(dest), "patches": len(items), "new_file": True}
        )

    for path, items in by_path.items():
        if path in blocked:
            continue
        target = repo / path
        if not target.exists():
            report["skipped"].append({"path": path, "reason": "文件不存在"})
            continue
        source_lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        # 同一文件多条补丁：从后往前应用，避免行号失效
        ordered = sorted(items, key=lambda item: (item[1].get("symbol_span") or item[1].get("anchor_span"))[0], reverse=True)
        applied = 0
        for edit, row in ordered:
            # 幂等闸：内容已经在文件里了就别再套一遍。
            # 同一份审计会被套用多次（闸门预览 + 放行后正式交付、续跑再结束），
            # 而审计里的行号/符号是**第一次**那份原文的 —— 直接照套会把同一个
            # 改动追加第二遍（真机复现：full_symbol 连套两次，方法出现两份）。
            if is_already_applied(source_lines, edit):
                report["skipped"].append({"path": path, "symbol": row.get("symbol"),
                                          # status 是给消费方判定「良性 / 真缺失」用的：
                                          # 内容本就在文件里 ⇒ 无需再套，不算交付物残缺。
                                          "status": "already_applied",
                                          "reason": STATUS_CN["already_applied"]})
                continue
            try:
                source_lines = _apply_one(source_lines, edit, row)
                applied += 1
            except Exception as exc:  # noqa: BLE001
                report["skipped"].append({"path": path, "reason": f"{type(exc).__name__}: {exc}"})
        if not applied:
            # 一条都没真套上（全都已应用过 / 全异常）：**不要重写文件**。
            # 照写会得到一个「内容没变、但报告说已写入 N 个文件」的假成功。
            continue
        text = "\n".join(source_lines) + "\n"
        if in_place:
            backup = target.with_suffix(target.suffix + ".orig")
            if not backup.exists():
                backup.write_text(target.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            target.write_text(text, encoding="utf-8")
            report["files"].append({"path": path, "written": str(target), "backup": str(backup),
                                    "patches": applied})
        else:
            out = Path(out_dir) if out_dir else repo  # 调用方必须给 out_dir
            dest = out / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
            report["files"].append({"path": path, "written": str(dest), "patches": applied})
    for edit, row in zip(edits, rows):
        if row.get("status") != "ok":
            report["skipped"].append(
                {"path": row.get("path"), "symbol": row.get("symbol"),
                 "reason": STATUS_CN.get(row.get("status"), row.get("status"))}
            )
        elif row.get("patch_kind") == "diff":
            report["skipped"].append(
                {"path": row.get("path"), "symbol": row.get("symbol"),
                 "reason": "给的是 unified diff，请用 patch/git apply 套用（见 patches/ 目录）"}
            )
    return report
