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
import textwrap
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
    # 这个状态有两种完全不同的成因：**没提供仓库** 与 **目标文件不在仓库里**。
    # 文案不能只写前者 —— 真机上「明明传了 --repo 却报没提供仓库」，人会查错方向。
    "unchecked": "未核对（原因见备注）",
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


def check_symbol_block_content(content: str, path: str, symbol: str = "") -> str | None:
    """对 **full_symbol 补丁**（完整顶层符号块）做语法级校验，没问题返回 None。

    与 :func:`check_new_file_content` 的差别：补丁可能被模型整体缩进一层，
    先 ``textwrap.dedent`` 再 compile；不核对「patch 是否定义了声明的符号」——
    那条由 ``analyze_edit`` 的既有校验负责，这里只管「是不是合法 Python 残片」。

    真机 run 20260928-200631：repair 轮模型把所有换行**双重转义**（JSON 里的 ``\\n``
    变成字面反斜杠+n），三份补丁全压成单行；而 dev 自检此前只看 change_type=add，
    modify/full_symbol 一路漏到 verify 的 py_compile 才炸。补丁永远套用不上，
    还沉淀成每轮恒定 3 条 patch_blockers，淹没真正的问题。
    """
    suffix = Path(str(path or "")).suffix.lower()
    if suffix != ".py" or not str(content or "").strip() or DIFF_RE.search(content):
        return None
    body = textwrap.dedent(content)
    try:
        compile(body, str(path or "<symbol block>"), "exec")
    except SyntaxError as exc:
        who = f"符号 `{symbol}` 的 " if symbol else ""
        return f"{who}full_symbol 补丁有语法错误（未物化即可判定无法套用）：第 {exc.lineno or '?'} 行：{exc.msg}"
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
    """核对整份实现产物。repo 为空时返回 unchecked（不产生问题，只说明无法核对）。

    **核对基准是「本份 edits 累积出的工作区」，不是仓库快照**（2026-09-27 修）。
    新建项目的文件正是靠**同一份 edits 里的 `add`** 创建出来的；若 `modify` 仍去仓库
    找原文，它必然判 ``unchecked``（"目标文件不存在"）—— 而 `apply_all` 只收
    ``status == "ok"`` 的行，于是**返工轮的修复被整批静默丢弃**，verify 在未修复的
    代码上报同一个错、再返工、再丢，永远修不上：

      真机 20260927-123032：5 条 `add` + 5 条 `modify`，5 条 modify 全判 unchecked，
      其中一条正是把 `from tkinter import event` 改成 `Event as event` 的修复。
      verify 于是照旧报 ImportError，评审据此再要求 rework_dev —— 8 轮全 fail。
    """
    edits = [e for e in ((impl or {}).get("edits") or []) if isinstance(e, dict)]
    audit: dict[str, Any] = {
        "source_available": bool(repo),
        "edits": [],
        "ok": 0,
        "problems": 0,
        "problem_detail": [],
    }
    repo_path = Path(repo) if repo else None
    cache: dict[str, str | None] = {}

    def _repo_text(path: str) -> str | None:
        """仓库里该文件的原文（不存在 / 读不了 → None，并缓存结果避免重复 IO）。"""
        if path not in cache:
            try:
                cache[path] = (repo_path / path).read_text(encoding="utf-8", errors="replace")  # type: ignore[operator]
            except OSError:
                cache[path] = None
        return cache[path]

    # 「本路径不在仓库、靠本份 edits 里的 add 新建」的补丁正文，按路径聚合。
    # 物化（apply_all）会把同一路径的多个 add 合并成这一个文件，所以 modify 的核对
    # 基准必须是**它们的合并结果** —— 与 apply_all 的 new_file 合并写入完全同源，
    # 两边的"文件长什么样"才不会各说各话。
    new_blocks: dict[str, list[str]] = {}
    if repo_path is not None:
        for edit in edits:
            path = str(edit.get("path") or "")
            if path and str(edit.get("change_type")) == "add" and _repo_text(path) is None:
                new_blocks.setdefault(path, []).append(str(edit.get("patch") or ""))

    for edit in edits:
        path = str(edit.get("path") or "")
        source: str | None = None
        if repo_path is not None:
            if str(edit.get("change_type")) == "add" and _repo_text(path) is None:
                # 新增文件：没有原文可核对，整份写入（见 analyze_edit 的 source == "" 分支）
                source = ""
            elif path in new_blocks:
                # modify 打在**同一份 edits 的 add 新建出来的**文件上：
                # 基准 = 那些 add 的合并结果（= 物化后文件里的真实内容）
                source = merge_new_file_blocks(new_blocks[path])
            else:
                source = _repo_text(path)
        row = analyze_edit(source, edit)
        if source is None and repo_path is not None:
            row["status"] = "unchecked"
            row["notes"].append(
                "目标文件不存在（modify 要求文件已在仓库里；新建文件要用 add）—— 仓库路径本身没问题"
            )
        elif source is None:
            # **真**没提供仓库。与上面那条共用 unchecked 状态，所以原因必须分开写清楚，
            # 否则就成了「传了 --repo 却说没传」（真机 20260926-214757 踩到）。
            row["status"] = "unchecked"
            row["notes"].append(
                "没有提供仓库路径，无法核对（新建项目请把生成目录作为 --repo 传入）"
            )
        # **归因到施工图**：`covers_tasks` 是这条补丁自己声明的"服务于哪张图"。
        # 把它带进行里，判负文案才能说清"是哪一张图的问题"，而不是让人以为整批都要重做 ——
        # 返工要指明「哪个 task、什么具体问题」是**机械可得**的，不该让评审和开发去猜。
        row["tasks"] = [str(t).strip() for t in (edit.get("covers_tasks") or []) if str(t).strip()]
        audit["edits"].append(row)

    # 新增文件的**内容**校验：不依赖原文，只看补丁本身 —— 所以单独跑一遍而不是塞进
    # analyze_edit，这样 repo 缺失（新建项目没给 --repo）时同样生效。
    # 真机 run 20260924-185507：dev 把 renderer.py 写残（`print(f'{`，277 字节），
    # 原先一律判 ok /「整份写入」，一路放行到 verify 才炸 —— 白烧 test+verify+review 一整轮。
    # strict=False 是**刻意的**：audit["edits"] 就是按 edits 逐条生成的，天然等长；
    # 万一将来不等长，这里截断处理比在物化中途抛 ValueError 炸掉整轮要好。
    for row, edit in zip(audit["edits"], edits, strict=False):
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
        # **合并后**的正文也要校验：物化写出去的是 `merge_new_file_blocks` 的结果，
        # 而上面那一遍只看了每一条 add 各自的正文 —— "两段各自配平、拼起来才崩"因此一路
        # 放行到 verify（真机 20260927-173023 的 verify 里就有一条「新增文件的内容本身有
        # 语法错误」走到最后一关才发现，白烧 test+verify+review 一整轮）。
        # 这里按**将要写出去的那一份**判：`audit["edits"]` 与 `edits` 是同序一一对应的，
        # 按索引 zip 取该路径上所有 `new_file` 块的正文再合并。
        payload = [
            str(edit.get("patch") or "")
            for edit, other in zip(edits, audit["edits"], strict=False)
            if str(other.get("patch_mode_used") or "") == "new_file"
            and str(other.get("path") or "") == path
        ]
        merged_problem = check_new_file_content(merge_new_file_blocks(payload), path)
        # 只在「这一遍查出了**新**东西」时记（至少有一条原先判 ok）—— 否则单块文件会在
        # 上一遍的逐条校验与这一遍之间被重复记一次，问题清单噪声变大。
        if merged_problem and any(r["status"] == "ok" for r in rows):
            for row in rows:
                if row["status"] == "ok":
                    row["status"] = "new_file_syntax_error"
                row["notes"].append(
                    f"**合并后**的新文件内容有语法问题：{merged_problem}"
                    "（逐条看都正常，拼起来才崩 —— 物化写出的就是这一份）"
                )

    # 统计放在状态改写之后算，避免计数与被改写后的状态不一致
    for row in audit["edits"]:
        if row["status"] == "ok":
            audit["ok"] += 1
        else:
            audit["problems"] += 1
            detail = STATUS_CN.get(row["status"], row["status"])
            note = "；".join(str(x) for x in (row.get("notes") or [])[:1])
            # 前缀带上施工图号：`［T-03］CLI：anchor 在原文里找不到`。
            # 这句话会原样进评审与 dev 的缺陷单 —— 「哪个 task 出了什么问题」必须在文案里，
            # 否则读的人只能自己去对文件与施工图的映射。
            tids = [str(t) for t in (row.get("tasks") or []) if str(t).strip()]
            audit["problem_detail"].append(
                (f"［{'、'.join(tids[:2])}］" if tids else "")
                + f"{row['symbol'] or row['path']}：{detail}"
                + (f"（{note}）" if note else "")
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
    for index, (edit, row) in enumerate(zip(edits, rows, strict=False), 1):
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

    for index, (edit, row) in enumerate(zip(edits, rows, strict=False), 1):
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
def _locate_span(source_lines: list[str], edit: dict, row: dict) -> tuple[int, int] | None:
    """在**当前**内容里重新定位这条补丁该贴的区间（0-based 含端点）；定位不到返回 None。

    为什么不复用审计里的 ``anchor_span`` / ``symbol_span``（它们是行号）：同一文件上
    有多条补丁是常态（真机 20260927-123032 的 ``main.py`` 上有 4 条 modify），第一条
    套用后行号就全变了，照抄旧行号会**贴错位置** —— 那比不贴更危险（写出残码、
    ``patch_span_mismatch`` 想拦的正是这种）。这里按 anchor / 符号名**重新找**，
    找不到或不唯一一律返回 None，交给调用方记「跳过 + 原因」。

    刻意不复用 :func:`analyze_edit` 的结论：它判的是「这条补丁自身合不合法」
    （基准是**当时**那份原文），而这里要的是「此刻该贴哪儿」。两者基准不同，
    混用就会退回行号错位。
    """
    mode = str(row.get("patch_mode_used") or "insert_after")
    if mode == "full_symbol":
        symbol = str(row.get("symbol") or "")
        return symbol_span(source_lines, symbol) if symbol else None
    hits = find_anchor("\n".join(source_lines), str(edit.get("anchor") or ""))
    if len(hits) != 1:
        # 0 处 = 定位不到；>1 处 = 有歧义。两种都不猜位置（贴错比不贴危险）。
        return None
    start, end = hits[0]
    if mode == "insert_after":
        return (end + 1, end)
    return (start, end)


def _apply_one(source_lines: list[str], edit: dict, span: tuple[int, int]) -> list[str]:
    """把一条补丁贴到 ``span``（0-based 含端点）上。

    区间由 :func:`_locate_span` 在**当前**内容上算出 —— 调用方不要传审计里的旧行号。
    """
    start, end = span
    patch_lines = str(edit.get("patch") or "").splitlines()
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


def symbol_excerpt(source: str, symbol: str, *, context: int = 0) -> dict | None:
    """某符号的**逐字原文 + 行号范围（1-based）**：缺陷单用，让修复方不必凭记忆改写。

    为什么要它（真机 20260927-221511 的根因）：dev 把 anchor 写成
    `self.db.add_entry(amount, note)`，而实际代码是 `self.db.add_entry(args.amount)`
    —— **凭记忆改写的近似行**，永远匹配不上。缺陷单此前只说"哪里错了"，
    **没说"那处现在逐字长什么样"**；而原文是机械可得的（本函数就是）。

    定位**复用已有的** :func:`symbol_span`（行级、多语言的块定位，`analyze_edit` 一直在用它），
    不另起一套 —— "同一个概念两份实现"正是本项目反复踩的坑（也正因如此，先前误把同名函数
    覆盖掉，`analyze_edit` 当场炸在 `ast.parse` 上）。取不到 / 不唯一 ⇒ None（**不猜**）。
    """
    name = str(symbol or "").strip()
    if not name or not source:
        return None
    lines = str(source).splitlines()
    # `CLI.add` 这类限定名取叶子：`symbol_span` 只认定义名
    span = symbol_span(lines, name.rsplit(".", 1)[-1])
    if not span:
        return None
    start0, end0 = int(span[0]), int(span[1])  # 0-based 含端点
    if start0 < 0 or start0 >= len(lines):
        return None
    ctx = max(0, int(context))
    lo = max(0, start0 - ctx)
    hi = min(len(lines) - 1, max(end0, start0) + ctx)
    return {"start": lo + 1, "end": hi + 1, "text": "\n".join(lines[lo : hi + 1])}


def _importable_module(name: str, local_modules: set[str] | None = None) -> bool:
    """这个名字是不是**能被 import 的模块**（标准库 / builtin / 本环境已安装 / 同批新建）。"""
    if not name or name.startswith("_") or not name.isidentifier():
        return False
    stdlib = set(getattr(sys, "stdlib_module_names", frozenset())) | set(sys.builtin_module_names)
    if name in stdlib:
        return True
    # 同批（或仓库里）即将作为兄弟文件存在的本地模块：本函数运行时这些文件还没落盘，
    # find_spec 必然失败 —— 真机 run 20260928-180933：cli.py 用 `db.xxx`，db.py 与它
    # 同批新建，find_spec('db') 为 None，于是「机械补 import」漏掉它，带病文件流出 dev。
    if local_modules and name in local_modules:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError, TypeError, ModuleNotFoundError):
        return False


# pyright 的未定义变量诊断有中英两种形态（不同版本/语言环境不一致，真机都出现过）：
#   中文：未定义 "db" / 未定义"db" / 未定义: db
#   英文（实测）："db" is not defined
_UNDEF_NAME_RES = (
    re.compile(r"未定义\s*[:：]?\s*[“\"']?([\w.]+)"),
    re.compile(r"[“\"']([\w.]+)[“\"']\s+is\s+not\s+defined"),
)


def _undefined_name(message: str) -> str | None:
    """从 pyright 的 reportUndefinedVariable 消息里抽出未定义名字（中英双语兼容）。"""
    text = str(message or "")
    for rx in _UNDEF_NAME_RES:
        m = rx.search(text)
        if m:
            return m.group(1).split(".")[0]
    return None


def top_level_defs(source: str) -> list[str]:
    """源码里**模块级** def/class 的名字（解析失败返回空，不抛）。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [
        str(node.name)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]


def _imports_name(body: str, module: str, name: str) -> bool:
    """``body`` 里是否已经有 ``from <module> import ... <name> ...``（优先 AST，解析失败退化正则）。"""
    try:
        tree = ast.parse(body)
    except SyntaxError:
        rx = re.compile(
            rf"^\s*from\s+{re.escape(module)}\s+import\s+[^\n]*\b{re.escape(name)}\b", re.M
        )
        return bool(rx.search(body))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            if any(alias.name == name for alias in node.names):
                return True
    return False


def _insert_import(body: str, name: str, module: str | None = None) -> str:
    """在文件正文里插入 ``import <name>``（module 给定时插 ``from <module> import <name>``）
    —— 跳过 shebang / 开头注释 / 模块 docstring；已存在同模块的 from-import 则把名字并进去。

    插在 docstring **之后**而不是文件最前面：否则模块 docstring 会退化成一条
    无用的字符串语句（`main.py` 这类入口文件通常有）。
    """
    lines = body.splitlines()
    if module is not None:
        # 已有 `from module import a, b`：把新名字并进同一行（含括号形态也摊平，语法等价）。
        pat = re.compile(
            rf"^(\s*from\s+{re.escape(module)}\s+import\s*)([^\n#]*?)(\s*(?:#.*)?)$", re.M
        )
        m = pat.search(body)
        if m:
            existing = m.group(2).replace("(", "").replace(")", "")
            names = [x.strip() for x in existing.split(",") if x.strip()]
            if name not in names:
                names.append(name)
                newline = f"{m.group(1)}{', '.join(names)}{m.group(3)}"
                lines = body.splitlines()
                line_no = body[: m.start()].count("\n")
                lines[line_no] = newline
                return "\n".join(lines) + "\n"
            return body
        new_line = f"from {module} import {name}"
    else:
        new_line = f"import {name}"
    pos = 0
    while pos < len(lines) and (
        not lines[pos].strip() or lines[pos].lstrip().startswith("#")
    ):
        pos += 1
    if pos < len(lines) and lines[pos].lstrip().startswith(('"""', "'''")):
        quote = lines[pos].lstrip()[:3]
        if lines[pos].lstrip().count(quote) >= 2:
            pos += 1  # 单行 docstring：开合都在这一行（`"""入口"""`），别去找结束引号，
            # 否则会一路走到文件末尾，把 import 追加到最后 —— 顶层语句用到它就 NameError。
        else:
            pos += 1
            while pos < len(lines) and quote not in lines[pos]:
                pos += 1
            if pos < len(lines):
                pos += 1
    lines.insert(pos, new_line)
    return "\n".join(lines) + "\n"


def repair_missing_imports(
    impl: dict | None,
    semantic_audit: dict | None = None,
    local_modules: set[str] | None = None,
    local_symbols: dict[str, str] | None = None,
) -> dict[str, Any]:
    """机械补上「用了但没 import」的名字（原地修改 impl）。

    支持两种形态：
      · 模块级 ``import sys`` —— 未定义名本身是可导入模块；
      · 符号级 ``from cli import add`` —— 未定义名是**兄弟模块顶层定义的符号**。
        符号→模块的归属来自：调用方传入的仓库存量 ``local_symbols``，以及本批 impl
        里 add 全文 / modify+full_symbol 补丁的 AST 解析。

    ``local_modules``：仓库里已存在 / 本批 impl 即将新建的兄弟模块名。这些模块在
    ``find_spec`` 看来尚不可导入，但补 ``import <name>`` 仍然成立（见
    :func:`_importable_module` 的真机说明）。

    真机教训（run snake-ds-plan 第 1 轮，2026-09-26）：pyright 连报三轮
    ``main.py:17 未定义 "random"`` / ``ui.py:28 未定义 "sys"``，流水线按
    「带问题重问 dev」处理了 3 次（约 184 秒），**一次都没修掉** —— 模型每次
    重写整份文件，惟独没加上那行 import。

    符号级缺口（run 20260928-200631 第 5 轮）：main.py 用了 cli.py 定义的
    add/remove，verify 静态检查明说「cli.py 定义了它，几乎肯定漏 import」，
    但旧版只认「未定义名=模块名」，于是机械补全只连补 ``import sys``，
    add/remove 五轮 NameError。

    安全闸门（全过才动，任何一条不满足就原样返回）：
      1. 只认 pyright 的 ``reportUndefinedVariable`` —— 用的是**它的判定**，不是猜的；
      2. 那个名字要么**确实是可导入的模块**，要么是兄弟模块（仓库/本批）顶层定义的
         符号，且 owner 不是当前文件自己 —— 补的 import 一定成立；
      3. 只处理 ``change_type == "add"`` 且 patch 是**整份正文**（非 diff）——
         只有拿到全文才能安全地在头部插入；
      4. 该文件里**还没有**这个 import（幂等，重复调用不会重复插）。
    """
    report: dict[str, Any] = {"repaired": 0, "detail": []}
    if not impl or not semantic_audit:
        return report
    # 本批 impl 里以 add 形式新建的 .py 文件，它们的模块名一定可导入（兄弟文件）。
    batch_modules = {
        Path(str(e.get("path") or "")).stem
        for e in (impl.get("edits") or [])
        if isinstance(e, dict)
        and str(e.get("change_type") or "") == "add"
        and str(e.get("path") or "").endswith(".py")
    }
    importable_as = (local_modules or set()) | batch_modules
    # 符号 → owner 模块。仓库存量优先；同批补丁（add 全文 / modify+full_symbol 整块）
    # 用 setdefault 补位 —— 同一符号在存量里已有定义时不被同批声明抢走归属。
    symbol_owner: dict[str, str] = dict(local_symbols or {})

    def _index(source: str, stem: str) -> None:
        for sym in top_level_defs(source):
            symbol_owner.setdefault(sym, stem)

    for e in (impl.get("edits") or []):
        if not isinstance(e, dict) or not str(e.get("path") or "").endswith(".py"):
            continue
        stem = Path(str(e.get("path") or "")).stem
        patch_text = str(e.get("patch") or "")
        if not patch_text.strip() or DIFF_RE.search(patch_text):
            continue
        if str(e.get("change_type") or "") == "add":
            _index(_new_file_body(patch_text), stem)
        elif (
            str(e.get("change_type") or "") == "modify"
            and str(e.get("patch_mode") or "") == "full_symbol"
        ):
            _index(textwrap.dedent(patch_text), stem)

    # path -> 模块名集合（插 import x）；path -> {owner 模块: 符号集合}（插 from x import y）
    wanted_modules: dict[str, set[str]] = {}
    wanted_symbols: dict[str, dict[str, set[str]]] = {}
    for diag in semantic_audit.get("diagnostics") or []:
        if not isinstance(diag, dict):
            continue
        if str(diag.get("rule") or "") != "reportUndefinedVariable":
            continue
        name = _undefined_name(str(diag.get("message") or ""))
        if not name:
            continue
        path = str(diag.get("file") or "").replace("\\", "/").strip()
        if not name or not path or Path(path).suffix.lower() != ".py":
            continue
        if _importable_module(name, importable_as):
            wanted_modules.setdefault(path, set()).add(name)
            continue
        owner = symbol_owner.get(name)
        # owner 必须真是个兄弟模块（防止按同批补丁里解析出的残缺归属瞎 import），
        # 且不能是文件自己（本文件内的定义缺失不是 import 能解决的）。
        if owner and owner != Path(path).stem and owner in importable_as:
            wanted_symbols.setdefault(path, {}).setdefault(owner, set()).add(name)
    if not wanted_modules and not wanted_symbols:
        return report

    def _key(p: str) -> str:
        return str(p or "").replace("\\", "/").strip()

    def _same_file(k: str, path: str) -> bool:
        # pyright 的 file 字段有时是沙箱绝对路径（/work/main.py）、有时是相对路径，
        # 同一文件会落成两个键 —— 必须把匹配上的键**全部**聚合，不能 next 取第一个。
        return k == path or k.endswith("/" + path) or path.endswith("/" + k)

    for edit in impl.get("edits") or []:
        if not isinstance(edit, dict):
            continue
        path = _key(edit.get("path"))
        if not path or str(edit.get("change_type") or "") != "add":
            continue
        mod_names: set[str] = set()
        sym_map: dict[str, set[str]] = {}
        for k, v in wanted_modules.items():
            if _same_file(k, path):
                mod_names |= v
        for k, v in wanted_symbols.items():
            if _same_file(k, path):
                for owner, names in v.items():
                    sym_map.setdefault(owner, set()).update(names)
        if not mod_names and not sym_map:
            continue
        patch_text = str(edit.get("patch") or "")
        if not patch_text.strip() or DIFF_RE.search(patch_text):
            continue  # diff 形态拿不到全文，不碰
        body = _new_file_body(patch_text)
        repaired_before = report["repaired"]
        for name in sorted(mod_names or set()):
            if re.search(rf"^\s*(import\s+{re.escape(name)}\b|from\s+{re.escape(name)}\b)",
                         body, re.M):
                continue  # 已经有这个 import
            body = _insert_import(body, name)
            report["repaired"] += 1
            report["detail"].append(f"{path}: import {name}")
        for owner in sorted(sym_map or {}):
            for name in sorted(sym_map[owner]):
                if _imports_name(body, owner, name):
                    continue  # 已经从该模块 import 过这个符号
                body = _insert_import(body, name, owner)
                report["repaired"] += 1
                report["detail"].append(f"{path}: from {owner} import {name}")
        if report["repaired"] > repaired_before:
            edit["patch"] = body
    return report


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


#: 「**定位失败**」类状态：模型把 anchor / 位置**凭记忆改写**了，物理上套用不了。
#: 与「内容有问题」不同（那种再写一次能好），这一类是"它看不到逐字原文"导致的，
#: 重问大概率产出**同样对不上**的新 anchor（真机实测：连续两次重问，问题集一字未变）。
UNLOCATABLE = ("anchor_not_found", "anchor_ambiguous", "patch_span_mismatch",
               "already_applied", "patch_no_effect")


def prune_unappliable(repo: str | Path | None, impl: dict | None) -> dict:
    """丢掉**定位失败**的补丁（原地修改 impl），返回 ``{dropped, detail}``。

    为什么必须丢（真机 20260927-221511 的实测链）：

      1) dev 重问时把 anchor 写成 ``self.db.add_entry(amount, note)``，
         而实际代码是 ``self.db.add_entry(args.amount)`` —— **凭记忆改写的近似行**；
      2) ``analyze_all`` 判 ``anchor_not_found`` ⇒ ``apply_all`` 只收 ``status == "ok"``，
         这条补丁**永远进不了沙箱**；
      3) 但它一直挂在累积实现里，被 ``_patch_blockers`` 与 verify 当成
         「交付物不完整」的**阻断项**；
      4) 于是它每轮都判负，而**任何"再改一次"都救不了它**（下一轮只会再写一条
         同样对不上的新 anchor）⇒ 一个修不掉的门永远挂在流水线前面，即"恒定判负"。

    丢掉它之后：沙箱拿到的是**能真正物化的那部分**，流水线不再被这个门拦住；
    而"有 N 条补丁没能落地"这条事实由调用方记进 ``grounding_warnings`` / state，
    评审与人工照样看得到（**不静默**）。

    注意：**只丢"定位失败"这一类**。``unchecked``（文件不在仓库）与
    ``patch_symbol_missing`` / ``patch_incomplete`` 属于"内容问题"，重问能修、
    也必须继续当阻断项（§26 的"文件始终没落盘"正是靠它们暴露的）。
    """
    rows = (analyze_all(repo, impl).get("edits") or []) if isinstance(impl, dict) else []
    edits = impl.get("edits") if isinstance(impl, dict) else None
    if not isinstance(edits, list) or not rows:
        return {"dropped": 0, "detail": []}
    # rows 与 dict 型 edits **同序**（analyze_all 也先按 isinstance(e, dict) 过滤）
    dict_edits = [e for e in edits if isinstance(e, dict)]
    keep: list[Any] = []
    detail: list[str] = []
    dropped_ids: set[int] = set()
    for idx, edit in enumerate(dict_edits):
        row = rows[idx] if idx < len(rows) else {}
        if str(row.get("status") or "") in UNLOCATABLE:
            dropped_ids.add(idx)
            detail.append(
                f"{edit.get('path')}::{edit.get('target_symbol') or '?'}"
                f"（{STATUS_CN.get(str(row.get('status')), row.get('status'))}）"
            )
    if not detail:
        return {"dropped": 0, "detail": []}
    counter = -1
    for edit in edits:
        if not isinstance(edit, dict):
            keep.append(edit)
            continue
        counter += 1
        if counter in dropped_ids:
            continue
        keep.append(edit)
    impl["edits"] = keep
    return {"dropped": len(detail), "detail": detail}


def apply_all(repo: str | Path, impl: dict | None, audit: dict, in_place: bool = False,
              out_dir: str | Path | None = None) -> dict:
    """按审计结果套用补丁。

    **安全约定**：只有显式 `in_place=True` 才会写回原仓库，且会先留 `*.orig` 备份；
    否则必须给 `out_dir`（把结果写到副本里），绝不动原仓库。

    **读基准是"累积写入的副本"，不是仓库快照**（2026-09-27 修）：新建项目的文件由
    本份 edits 里的 `add` 创建，若 `modify` 去仓库找原文，它永远找不到 —— 修复被
    静默丢弃，verify 在未修复的代码上判负、再返工、再丢（真机 20260927-123032：
    5 条 modify 全丢，其中一条正是修 `from tkinter import event` 的那条）。现在：
      1) 新增文件仍**同路径合并**后整份写出（防互相覆盖，见 merge_new_file_blocks）；
      2) 其余补丁按 edits **列表顺序**逐条套用，每条都在"此刻的内容"上重新定位
         （见 _locate_span）—— 读的是 out_dir 里的既有内容，没有再退回仓库。
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
    for edit, row in zip(edits, rows, strict=False):
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

    # ------------------------------------------------------------------ 逐条顺序套用
    # **顺序即语义**：按 edits 的**列表顺序**依次套，每条都在"此刻的内容"上重新定位
    # （见 _locate_span）。不再按文件分组、也不再按旧行号倒序 —— 那套的前提是
    # "行号来自同一份原文"，同一文件多条补丁时必然错位。
    #
    # 读基准：**先看 out_dir**（本次已经写进去的，含上面 new_file 的合并写入），
    # 没有再退回仓库。这一条是"新建文件被同批 modify 修改"能成立的前提 ——
    # 以前固定读 `repo / path`，而新建项目的仓库是空的，于是 modify 全部跳过、
    # 返修静默丢失（真机 20260927-123032：5 条修复全丢）。
    write_root = repo if in_place else Path(out_dir)  # type: ignore[arg-type]
    contents: dict[str, list[str]] = {}
    applied_count: dict[str, int] = {}

    def _read_current(path: str) -> list[str] | None:
        for root in (write_root, repo):
            candidate = root / path
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        return None

    for edit, row in zip(edits, rows, strict=False):
        path = str(edit.get("path") or "")
        if not path or path in blocked:
            continue
        # new_file 的行不进这里：目标文件本来就不在仓库里，逐条套用只会得到
        # 一堆误导性的「跳过：文件不存在」，真正该做的是上面的合并写入。
        if row.get("status") != "ok" or row.get("patch_kind") != "block":
            continue
        if row.get("patch_mode_used") == "new_file":
            continue
        source_lines = contents.get(path)
        if source_lines is None:
            source_lines = _read_current(path)
        if source_lines is None:
            report["skipped"].append(
                {"path": path, "symbol": row.get("symbol"),
                 "reason": "文件不存在（本批里没有 add 创建它，仓库里也没有）"}
            )
            continue
        # 幂等闸：内容已经在文件里了就别再套一遍。
        # 同一份审计会被套用多次（闸门预览 + 放行后正式交付、续跑再结束），
        # 第二次读到的已经是改过的文件 —— 直接照套会把同一个改动追加第二遍
        # （真机复现：full_symbol 连套两次，方法出现两份）。
        if is_already_applied(source_lines, edit):
            report["skipped"].append({"path": path, "symbol": row.get("symbol"),
                                      # status 是给消费方判定「良性 / 真缺失」用的：
                                      # 内容本就在文件里 ⇒ 无需再套，不算交付物残缺。
                                      "status": "already_applied",
                                      "reason": STATUS_CN["already_applied"]})
            continue
        span = _locate_span(source_lines, edit, row)
        if span is None:
            # 定位不到 / 有歧义：**宁可跳过也不猜位置**。原因是真实的模型侧缺陷
            # （anchor 与产物对不上，或被前一条补丁改掉了），交给重问去修。
            report["skipped"].append(
                {"path": path, "symbol": row.get("symbol"),
                 "reason": "在当前内容里定位不到 anchor（不唯一 / 已被前一条补丁改动），"
                           "无法确定该贴哪儿 —— 请重新给出可定位的 anchor"}
            )
            continue
        contents[path] = _apply_one(source_lines, edit, span)
        applied_count[path] = applied_count.get(path, 0) + 1

    # 交付清单**按路径去重**：同一文件可能先由 new_file 整份写出、再被 modify 改动
    # （新建项目里的常态）。不去重的话列表会写「6 个文件」而实际只有 5 个 ——
    # 人工闸门看到的第一个信号就自相矛盾。
    seen_files = {str(item.get("path")): item for item in report["files"]}
    for path, applied in applied_count.items():
        text = "\n".join(contents[path]) + "\n"
        if in_place:
            target = repo / path
            backup = target.with_suffix(target.suffix + ".orig")
            if target.is_file() and not backup.exists():
                backup.write_text(target.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        else:
            target = write_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        existing = seen_files.get(path)
        if existing is not None:
            existing["patches"] = int(existing.get("patches") or 0) + applied
            existing["modified_after_create"] = True
            continue
        entry: dict[str, Any] = {"path": path, "written": str(target), "patches": applied}
        if in_place:
            entry["backup"] = str(backup)
        report["files"].append(entry)
        seen_files[path] = entry
    for _edit, row in zip(edits, rows, strict=False):
        if row.get("status") != "ok":
            # 把行内备注一并带出来：只给「未核对」三个字，人会去猜 —— 真机上就有人
            # 按「没提供仓库」去查 --repo，而真实原因是目标文件不在仓库里。
            note = "；".join(str(x) for x in (row.get("notes") or [])[:1])
            report["skipped"].append(
                {"path": row.get("path"), "symbol": row.get("symbol"),
                 "reason": STATUS_CN.get(row.get("status"), row.get("status"))
                 + (f"（{note}）" if note else "")}
            )
        elif row.get("patch_kind") == "diff":
            report["skipped"].append(
                {"path": row.get("path"), "symbol": row.get("symbol"),
                 "reason": "给的是 unified diff，请用 patch/git apply 套用（见 patches/ 目录）"}
            )
    return report
