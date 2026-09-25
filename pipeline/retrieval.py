"""存量代码检索分片（决策：按预算选文件，替代"把整个仓库塞进 prompt"）。

实现要点（都是被真机测试逼出来的）：
1. 中文没有空格，必须切 n-gram；且 2 字词不能被长度截断挤掉（否则几乎零命中）。
2. 轻量 IDF：命中出现在超过 35% 文件里的词（如"阶段""流水线"）基本无区分度，直接丢弃。
3. 扫描有字节上限，避免大仓库一次性把内存吃满。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .budget import estimate_tokens
from .config import CHARS_PER_TOKEN

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    "dist", "build", "target", "out", ".idea", ".vscode", ".pytest_cache", ".mypy_cache",
    "site-packages", ".trade-data", "runs", ".next", ".cache", "coverage",
}
TEXT_EXT = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".java", ".go", ".rs",
    ".cs", ".cpp", ".cc", ".c", ".h", ".hpp", ".php", ".rb", ".kt", ".swift", ".scala",
    ".sql", ".sh", ".ps1", ".bat", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".json",
    ".md", ".txt", ".html", ".css", ".scss",
}
MAX_FILE_BYTES = 400_000
MAX_FILES_SCANNED = 3000
MAX_TOTAL_BYTES = 60_000_000
GRAM_QUOTA = {4: 40, 3: 40, 2: 60}
DF_RATIO_CUTOFF = 0.35

# 扩展名权重：这是「改代码」的流水线，代码文件应当优先。真机教训（2026-09-23，真实项目
# `1.18.0 source code`）：一份 400KB 的 zh-cn.json 翻译表凭海量中文命中挤进了 top-N，
# 白吃掉 2600 token 代码预算里的 ~700 token，模型却什么也拿不到。
EXT_WEIGHT: dict[str, float] = {
    ".py": 1.0, ".js": 1.0, ".mjs": 1.0, ".cjs": 1.0, ".ts": 0.95, ".tsx": 0.95, ".jsx": 0.95,
    ".java": 1.0, ".go": 1.0, ".rs": 1.0, ".cs": 1.0, ".cpp": 1.0, ".cc": 1.0, ".c": 1.0,
    ".h": 0.9, ".hpp": 0.9, ".php": 1.0, ".rb": 1.0, ".kt": 1.0, ".swift": 1.0, ".scala": 1.0,
    ".vue": 0.9, ".html": 0.7, ".css": 0.5, ".scss": 0.5, ".sql": 0.7,
    ".sh": 0.6, ".ps1": 0.6, ".bat": 0.6,
    ".yaml": 0.55, ".yml": 0.55, ".toml": 0.55, ".ini": 0.55, ".cfg": 0.55,
    ".json": 0.35,
    ".md": 0.35, ".txt": 0.3,
}
EXT_WEIGHT_DEFAULT = 0.5


def _size_factor(chars: int) -> float:
    """超大文件降权：>400KB 的文件本来也不可能整体改动，只截 700 token 给它，性价比最低。"""
    if chars <= 20_000:
        return 1.0
    if chars <= 60_000:
        return 0.8
    if chars <= 200_000:
        return 0.5
    return 0.3

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\.\-]{1,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_CJK_STOP = {
    "一个", "这个", "那个", "可以", "需要", "进行", "如果", "因为", "所以", "以及", "并且",
    "或者", "然后", "我们", "你们", "他们", "就是", "不是", "没有", "什么", "怎么", "如何",
    "必须", "应该", "能够", "同时", "目前", "现在", "这些", "那些", "为了", "由于", "基于",
}


@dataclass
class Excerpt:
    path: str
    text: str
    score: float
    truncated: bool
    note: str = ""  # 片段来源说明（如「第 335-380 行（命中行 341）+ 文件头」）


def _ident_terms(query: str) -> list[str]:
    """需求里写出的标识符（`index_all` / `sqlite3` 这类符号名）—— 定位代码位置的最强信号。"""
    out: list[str] = []
    seen: set[str] = set()
    for match in _IDENT_RE.finditer(query or ""):
        token = match.group(0).lower()
        if len(token) >= 2 and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _query_terms(query: str) -> list[str]:
    """抽取检索词：标识符原样保留；中文按 2/3/4 字 n-gram 混合，避免只看长词导致零命中。"""
    if not query:
        return []
    grams: dict[int, list[str]] = {4: [], 3: [], 2: []}
    seen: set[str] = set()
    for token in _ident_terms(query):
        seen.add(token)
        grams[3].append(token)
    for match in _CJK_RE.finditer(query):
        run = match.group(0)
        for size in (4, 3, 2):
            for i in range(len(run) - size + 1):
                gram = run[i : i + size]
                if gram in _CJK_STOP or gram in seen:
                    continue
                seen.add(gram)
                grams[size].append(gram)
    ordered: list[str] = []
    for size in (4, 3, 2):
        ordered.extend(grams[size][: GRAM_QUOTA[size]])
    return ordered


def _iter_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_EXT:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield path


def _load_corpus(root: Path) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """返回 [(相对路径, 文本)] 与词 -> 文档频次。"""
    corpus: list[tuple[str, str]] = []
    total = 0
    for path in _iter_files(root):
        if len(corpus) >= MAX_FILES_SCANNED or total >= MAX_TOTAL_BYTES:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        total += len(text)
        corpus.append((path.relative_to(root).as_posix(), text))
    return corpus, {}


_BLOCK_START_RE = re.compile(
    r"^\s*(?:async\s+)?(?:def|class|function|func|sub|public|private|protected|internal|static|export)\b"
)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _block_range(lines: list[str], anchor: int, max_chars: int) -> tuple[int, int, str] | None:
    """把锚点行扩展到它所在的代码块（def/class/function…）整体。

    开发阶段要给出「可直接应用的补丁」，必须看到目标函数的完整签名与函数体，
    只给中心窗口是不够的（真机教训：dev 对 71KB 单文件只能给 1000 字符示意代码）。
    返回 (起行, 止行, 符号名)，找不到块则返回 None。
    """
    if anchor >= len(lines):
        return None
    anchor_indent = _indent(lines[anchor])
    start = None
    for idx in range(anchor, -1, -1):
        line = lines[idx]
        if not line.strip():
            continue
        if idx == anchor or _indent(line) < anchor_indent:
            if _BLOCK_START_RE.match(line):
                start = idx
                break
    if start is None:
        return None
    name_match = re.search(r"(?:def|class|function|func|sub)\s+([A-Za-z_][A-Za-z0-9_]*)", lines[start])
    name = name_match.group(1) if name_match else ""
    base_indent = _indent(lines[start])
    end = start
    used = len(lines[start])
    while end + 1 < len(lines) and used + len(lines[end + 1]) <= max_chars:
        nxt = lines[end + 1]
        if nxt.strip() and _indent(nxt) <= base_indent:
            break
        end += 1
        used += len(nxt)
    return start, end, name


def symbol_span(lines: list[str], anchor: int) -> tuple[int, int, str] | None:
    """公开版：把锚点行扩到它所在的代码块（长度不限）。补丁校验用它定位"整个符号"。"""
    return _block_range(lines, anchor, 10**9)


def fit_excerpt(text: str, limit_chars: int) -> str:
    """把已经拼好的片段再压到 limit_chars：优先保「命中窗口」，必要时丢掉文件头摘要。"""
    if len(text) <= limit_chars:
        return text
    marker = "\n…（中间省略）…\n"
    if marker in text:
        head, body = text.split(marker, 1)
        if len(body) >= limit_chars:
            return body[:limit_chars]
        keep_head = max(limit_chars - len(body) - len(marker), 0)
        return head[:keep_head] + marker + body
    return text[:limit_chars]


def _snippet_for(
    text: str,
    terms: list[str],
    budget_chars: int,
    idents: tuple[str, ...] | list[str] = (),
    df: dict | None = None,
) -> tuple[str, bool, str]:
    """截取文件片段：命中点不在文件头时返回「文件头摘要 + 命中窗口」，而不是只给文件头。

    真机教训（2026-09-23，真实项目 `卡片检索器.py`）：71KB 单文件项目只取文件头等于什么都没给 ——
    1120 字符里连一个 `def` 都没有，而需求要改的 `index_all` 在第 335 行。

    锚点选择（按可靠性排序）：
    1. **需求里写出的标识符**（`index_all` 这种符号名是最强信号）—— 取语料里最稀有的那个，
       定位它在文件头之外的首次出现。中文项目里散文式注释的中文 n-gram 命中密度天然高于代码，
       所以不能只按密度挑。
    2. 退化为「命中密度最高的行」（只在文件头之后找，因为文件头已经作为前缀给过了）。
    """
    if len(text) <= budget_chars:
        return text, False, ""
    lines = text.splitlines(keepends=True)
    if not lines:
        return text[:budget_chars], True, ""

    lowered = [line.lower() for line in lines]
    # 文件头是白给的前缀，锚点不该落在这里（否则窗口等于白花）
    head_zone = min(260, budget_chars // 4)
    offset, head_end_idx = 0, 0
    while head_end_idx < len(lines) and offset < head_zone:
        offset += len(lines[head_end_idx])
        head_end_idx += 1

    anchor: int | None = None
    reason = ""
    low_text = text.lower()
    if idents:
        ranked = sorted(
            (t for t in {i.lower() for i in idents} if t in low_text),
            key=lambda t: ((df or {}).get(t, 1), -len(t)),  # 语料里越稀有、名字越长的符号越优先
        )
        for ident in ranked[:4]:
            hits = [i for i, low in enumerate(lowered) if ident in low]
            outside = [i for i in hits if i >= head_end_idx]
            if outside:
                anchor, reason = outside[0], f"命中 `{ident}`"
                break
            if hits and not reason:
                anchor, reason = hits[0], f"命中 `{ident}`（在文件头内）"
    if anchor is None:
        best_idx, best_score = head_end_idx, 0.0
        for idx in range(head_end_idx, len(lines)):
            if not lines[idx].strip():
                continue
            low = lowered[idx]
            score = sum(1.0 + 0.5 * len(term) for term in terms if term in low)
            if score > best_score:
                best_idx, best_score = idx, score
        if best_score <= 0:
            return text[:budget_chars], True, "文件头（无有效命中）"
        anchor, reason = best_idx, "词命中密度最高"

    best_idx = anchor
    head_chars = 0 if best_idx == 0 else head_zone
    window_chars = max(budget_chars - head_chars, budget_chars // 2)

    # 优先取「锚点所在的整个代码块」——开发要给出可应用的补丁，必须看到完整函数体
    block = _block_range(lines, best_idx, window_chars)
    if block:
        start_idx, end_idx, symbol = block
        reason += f" 的 `{symbol}` 函数体" if symbol else " 所在代码块"
    else:
        start_idx, used = best_idx, len(lines[best_idx])
        while start_idx > 0 and used + len(lines[start_idx - 1]) <= max(window_chars // 3, 1):
            start_idx -= 1
            used += len(lines[start_idx])
        end_idx = best_idx
        while end_idx + 1 < len(lines) and used + len(lines[end_idx + 1]) <= window_chars:
            end_idx += 1
            used += len(lines[end_idx])
    window = "".join(lines[start_idx : end_idx + 1])
    line_range = f"第 {start_idx + 1}-{end_idx + 1} 行"

    if start_idx == 0:
        return window, True, f"{line_range}（{reason}）"

    head_parts: list[str] = []
    head_used = 0
    for line in lines[:start_idx]:
        if head_used + len(line) > head_chars:
            break
        head_parts.append(line)
        head_used += len(line)
    marker = "\n…（中间省略）…\n"
    room = budget_chars - head_used - len(marker)
    if room <= 0:
        return window[:budget_chars], True, f"{line_range}（{reason}）"
    return (
        "".join(head_parts) + marker + window[:room],
        True,
        f"{line_range}（{reason}）+ 文件头",
    )


def select_excerpts(
    repo: Path | None,
    query: str,
    token_budget: int,
    per_file_tokens: int = 1200,
    max_files: int = 20,
) -> list[Excerpt]:
    """在 repo 内挑出与 query 最相关的若干文件片段，总量不超过 token_budget。"""
    if repo is None:
        return []
    root = Path(repo)
    if not root.exists():
        raise FileNotFoundError(f"repo 不存在: {root}")

    terms = _query_terms(query)
    if not terms:
        return []

    corpus, _ = _load_corpus(root)
    if not corpus:
        return []

    lowered = [(rel, text, text.lower()) for rel, text in corpus]
    total_docs = len(lowered)

    # 文档频次 + IDF：只保留有区分度的词
    df = Counter()
    for _, _, low in lowered:
        for term in terms:
            if term in low:
                df[term] += 1
    cutoff = max(1, int(total_docs * DF_RATIO_CUTOFF))
    effective = [t for t in terms if 0 < df[t] <= cutoff]
    if not effective:
        # 兜底：取出现次数最少的词。**必须过滤掉 df==0**，否则下面算 IDF 会除零
        # （真机踩过：需求与仓库没有任何共同词时 —— 比如中文需求 + 纯英文仓库 —— 直接崩）
        effective = [t for t in sorted(terms, key=lambda t: df.get(t, 0)) if df.get(t, 0) > 0][:20]
    if not effective:
        return []  # 需求与仓库完全没有共同词：不做检索，让下游明确知道"没有代码依据"

    scored: list[tuple[float, str, str]] = []
    for rel, text, low in lowered:
        low_rel = rel.lower()
        score = 0.0
        for term in effective:
            idf = math.log(1 + total_docs / max(df[term], 1))
            weight = 1.0 + 0.5 * len(term)
            if term in low_rel:
                score += 10.0 * idf * weight
            hits = low.count(term)
            if hits:
                score += min(hits, 10) * idf * weight
        if score <= 0:
            continue
        # 代码优先 + 超大文件降权（翻译表/数据文件不该挤掉源码的预算）
        suffix = Path(rel).suffix.lower()
        score *= EXT_WEIGHT.get(suffix, EXT_WEIGHT_DEFAULT) * _size_factor(len(text))
        scored.append((score, rel, text))

    scored.sort(key=lambda item: item[0], reverse=True)

    excerpts: list[Excerpt] = []
    used = 0
    per_file_chars = int(per_file_tokens * CHARS_PER_TOKEN)
    idents = _ident_terms(query)
    for score, rel, text in scored[:max_files]:
        snippet, truncated, note = _snippet_for(text, effective, per_file_chars, idents=idents, df=df)
        cost = estimate_tokens(snippet)
        if used + cost > token_budget:
            break
        excerpts.append(
            Excerpt(path=rel, text=snippet, score=round(score, 1), truncated=truncated, note=note)
        )
        used += cost
    return excerpts


def render_excerpts(excerpts: list[Excerpt]) -> str:
    if not excerpts:
        return "（未提供存量代码；只能基于需求描述作答，任何涉及具体文件的事实都必须列入不确定性）"
    blocks = []
    for item in excerpts:
        marks = [f"score={item.score}"]
        if item.note:
            marks.append(item.note)
        if item.truncated:
            marks.append("已截断")
        head = f"===== FILE: {item.path} ({', '.join(marks)}) ====="
        blocks.append(f"{head}\n{item.text}")
    return "\n\n".join(blocks)
