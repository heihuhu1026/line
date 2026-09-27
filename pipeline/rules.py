"""声明式工程红线规则库（机械可判定部分）的加载与执行引擎。

**为什么要有这个模块**

在此之前，本项目每加一条"机器能证明的工程缺陷"都要写一段 Python（`_path_rule_stem`、
`unavailable_imports`、`check_new_file_content`、`entry_script_problems`…），于是：
① 同一条判据在"落盘前拦截"与"评审提示词"两处各写一遍，迟早漂移；
② 想加一条规则就得改代码、加分支、再补测试，加一条要半天，实际结果是**根本没加**。
真机上的代价很直接：`import keyboard`（自己发明的依赖）要到 verify 跑完整轮才暴露，
而这类问题本来是毫秒级的本地正则能抓的。

本模块把"规则内容"搬进 `rules.json`，代码只留**执行引擎**。同一条规则同时服务三处：
  ① `orchestrator._rule_findings()` —— 补丁落盘前的拦截（机械举证，不依赖模型自觉）；
  ② 运行验证/交付前的静态扫描；
  ③ 评审提示词里的机械证据块（`format_block`）。

**两条款式约束**（这是它区别于"随便加个 regex 检查"的地方）

1. **每条规则必须同时写出「凭什么判负」（evidence）与「什么反例能推翻它」（negative）**。
   negative 会被送进评审提示词当作证伪判据 —— 评审要主张某条不成立，必须给出符合该判据的
   具体反例（输入 + 实际行为），**只复述规则文字或照抄上一轮结论不作为证据**。
   这正是治「评审自指循环」的机制：上一轮的结论不再是锚点，反例才是。
2. **只有「机器已证明 + 开发改得动」的规则才允许是 blocker**。会误伤合法写法的一律 warn
   —— 本项目反复踩过"误判成阻断 → 触发一整轮无谓返工"，代价是一轮 8B/14B 的调用与被推后的收敛。

**栈无关是硬前提**：同一条流水线既跑 tkinter 贪吃蛇、也跑 FastAPI 服务，所以 Web/数据库
专用规则必须带 `applies_when` 条件触发；不满足条件时既不判负、也不假装验过（与
`orchestrator` 里 `forbidden_ignored` 的语义一致）。

引擎的安全约定：**任何异常都不得影响流水线**（规则文件写坏、正则非法 → 该条跳过并记录），
与 dev-expert 的 hook 设计原则同源。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

RULES_NAME = "rules.json"
DEFAULT_PATH = Path(__file__).with_name(RULES_NAME)

#: 参与「编码/换行」类文本检查的后缀。二进制资源（图片/字体/压缩包）不在其列：
#: 对它们做 UTF-8 校验必然误报。
TEXT_SUFFIXES = frozenset({
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".html", ".htm",
    ".css", ".scss", ".json", ".jsonl", ".md", ".rst", ".txt", ".cfg", ".ini", ".toml",
    ".yaml", ".yml", ".sql", ".sh", ".ps1", ".bat", ".java", ".kt", ".go", ".rs", ".c",
    ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".lua", ".xml", ".env", ".gitignore",
})

SEVERITY_CN = {"blocker": "阻断", "warn": "提示"}


@dataclass(frozen=True)
class Rule:
    """一条规则。字段与 `rules.json` 一一对应，`negative` 与 `evidence` 不允许为空串。"""

    id: str
    title: str
    layer: str = "A"                 # A=无条件；B=条件触发（applies_when 必须非空）
    severity: str = "warn"           # blocker / warn
    kind: str = "regex"              # regex / manifest / require / encoding / env_unignored
    applies_when: dict[str, Any] = field(default_factory=dict)
    patterns: tuple[str, ...] = ()
    require_patterns: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    exclude_paths_regex: str = ""
    #: 是否豁免「入口文件」。入口名是**项目自定义**的（真机校准 20260926-212611 的入口
    #: 就叫 `tempconv.py`），固定名单抓不到 ⇒ 3 条 `print` 全被误报成调试残留。
    #: 调用方把"入口候选路径"传进来（见 `scan_edits(entry_paths=...)`）。
    exempt_entry_paths: bool = False
    message: str = ""
    evidence: str = ""
    negative: str = ""
    source: str = ""

    @property
    def is_blocker(self) -> bool:
        return self.severity == "blocker"


_CACHE: list[Rule] | None = None
_LOAD_NOTES: list[str] = []


def load(path: str | Path | None = None, *, force: bool = False) -> list[Rule]:
    """读取并校验规则库。**读坏了不抛异常**，只返回能用的部分（流水线必须能继续）。"""
    global _CACHE
    target = Path(path) if path else DEFAULT_PATH
    if _CACHE is not None and not force and target == DEFAULT_PATH:
        return _CACHE
    out: list[Rule] = []
    notes: list[str] = []
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _LOAD_NOTES[:] = [f"规则库读取失败（{target.name}）：{type(exc).__name__}: {exc}"]
        if target == DEFAULT_PATH:
            _CACHE = []
        return []
    for item in raw.get("rules") or []:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id") or "").strip()
        if not rid:
            notes.append("跳过一条没有 id 的规则")
            continue
        severity = str(item.get("severity") or "warn").strip().lower()
        if severity not in ("blocker", "warn"):
            notes.append(f"{rid}：未知 severity={severity}，按 warn 处理")
            severity = "warn"
        rule = Rule(
            id=rid,
            title=str(item.get("title") or rid),
            layer=str(item.get("layer") or "A").upper(),
            severity=severity,
            kind=str(item.get("kind") or "regex").strip().lower(),
            applies_when=dict(item.get("applies_when") or {}),
            patterns=tuple(str(x) for x in (item.get("patterns") or []) if str(x).strip()),
            require_patterns=tuple(str(x) for x in (item.get("require_patterns") or []) if str(x).strip()),
            exclude=tuple(str(x) for x in (item.get("exclude") or []) if str(x).strip()),
            exclude_paths_regex=str(item.get("exclude_paths_regex") or ""),
            exempt_entry_paths=bool(item.get("exempt_entry_paths")),
            message=str(item.get("message") or ""),
            evidence=str(item.get("evidence") or ""),
            negative=str(item.get("negative") or ""),
            source=str(item.get("source") or ""),
        )
        # 缺「反例判据」的规则是**半成品**：它无法被证伪，也就不该参与判负。
        # 与其悄悄放行，不如在这里降级并留痕（见模块 docstring 的条款 1）。
        if not rule.negative:
            notes.append(f"{rid}：缺 negative（反例判据）→ 降级为 warn（不可判负）")
            rule = Rule(**{**rule.__dict__, "severity": "warn"})
        if rule.kind == "require" and not rule.require_patterns:
            notes.append(f"{rid}：kind=require 但没有 require_patterns → 跳过")
            continue
        if rule.kind in ("regex", "manifest") and not rule.patterns:
            notes.append(f"{rid}：没有任何 patterns → 跳过")
            continue
        for pat in (*rule.patterns, *rule.require_patterns, *rule.exclude, rule.exclude_paths_regex):
            try:
                re.compile(pat)
            except re.error as exc:
                notes.append(f"{rid}：正则非法（{pat[:40]}…）：{exc}")
        out.append(rule)
    _LOAD_NOTES[:] = notes
    if target == DEFAULT_PATH:
        _CACHE = out
    return out


def load_notes() -> list[str]:
    """上一次加载遇到的问题（给页面/日志看）。空列表 = 规则库完全可用。"""
    return list(_LOAD_NOTES)


def catalog() -> list[dict[str, str]]:
    """规则清单（不含正则），供页面展示与人工审阅。"""
    return [
        {
            "id": r.id,
            "title": r.title,
            "layer": r.layer,
            "severity": r.severity,
            "kind": r.kind,
            "negative": r.negative,
            "source": r.source,
        }
        for r in load()
    ]


# --------------------------------------------------------------- 触发条件与应用


def _norm_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").lstrip("./").strip()


def _applies(rule: Rule, *, path: str, suffix: str, text: str) -> bool:
    """`applies_when` 求值：同一层内各键是 **AND**，`any_of` 是其中任一子条件成立即可。

    单条补丁的上下文（路径 + 该补丁的新代码正文）已足够判定绝大多数栈相关规则：
    CORS 配置、迁移文件、`localStorage` 调用都出现在**正在写的这段代码里**。
    """
    cond = rule.applies_when or {}
    if not cond:
        return True

    def _atom(part: dict[str, Any]) -> bool:
        suffixes = [str(s).lower() for s in (part.get("suffixes") or [])]
        if suffixes and suffix not in suffixes:
            return False
        if part.get("paths_regex") and not re.search(str(part["paths_regex"]), path):
            return False
        if part.get("content_regex") and not re.search(str(part["content_regex"]), text):
            return False
        return True

    for key, value in cond.items():
        if key == "any_of":
            if not any(_atom(p) for p in (value or []) if isinstance(p, dict)):
                return False
        elif key in ("suffixes", "paths_regex", "content_regex"):
            if not _atom({key: value}):
                return False
    return True


def _excluded_line(rule: Rule, line: str) -> bool:
    """豁免按**命中行**判定，而不是整份文件。

    规则误报几乎都长这样：同一行里既有违规写法、也有合法写法
    （`password = os.environ["PGPASSWORD"]`）。按行豁免既不放过真问题，
    也不会因为文件里出现过一次 `os.environ` 就把整个文件变成"已豁免"。
    """
    return any(re.search(pat, line, re.M) for pat in rule.exclude)


def _content_of(edit: dict) -> str:
    """取「这条补丁将要写进文件的新代码」。

    三种形态：new_file/add 取全文正文；modify 取替换块（就是新代码）；
    unified diff 无法逐行归因（`+` 前缀会污染正则），**明确跳过**而不是猜。
    """
    patch = str(edit.get("patch") or "")
    if not patch.strip():
        return ""
    try:
        from . import patches as _patches
    except Exception:  # noqa: BLE001 - 导入失败不能影响检查
        _patches = None  # type: ignore[assignment]
    if _patches is not None and _patches.DIFF_RE.search(patch):
        return ""
    mode = str(edit.get("patch_mode") or "")
    change = str(edit.get("change_type") or "")
    if mode == "new_file" or change == "add":
        if _patches is not None:
            try:
                return _patches._new_file_body(patch)
            except Exception:  # noqa: BLE001
                return patch
    return patch


def _finding(rule: Rule, *, path: str, line_no: int, excerpt: str, note: str = "") -> dict[str, Any]:
    return {
        "rule": rule.id,
        "title": rule.title,
        "severity": rule.severity,
        "layer": rule.layer,
        "path": path,
        "line": line_no,
        "excerpt": excerpt.strip()[:160],
        "message": rule.message,
        "evidence": rule.evidence,
        "negative": rule.negative,
        "source": rule.source,
        "note": note,
    }


def _line_at(text: str, pos: int) -> tuple[int, str]:
    line_no = text.count("\n", 0, pos) + 1
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    line = text[start: end if end >= 0 else len(text)]
    return line_no, line


def _scan_text(rule: Rule, path: str, text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for pat in rule.patterns:
        try:
            matches = list(re.finditer(pat, text, re.M))
        except re.error:
            continue
        for m in matches:
            line_no, line = _line_at(text, m.start())
            if _excluded_line(rule, line):
                continue
            if (path, line_no) in seen:
                continue
            seen.add((path, line_no))
            out.append(_finding(rule, path=path, line_no=line_no, excerpt=line))
    return out


def _scan_require(rule: Rule, path: str, text: str) -> list[dict[str, Any]]:
    for pat in rule.require_patterns:
        try:
            if re.search(pat, text, re.M):
                return []
        except re.error:
            return []
    return [_finding(rule, path=path, line_no=1, excerpt="（整个文件内未命中）")]


def scan_edits(
    edits: Iterable[Any],
    *,
    rules: list[Rule] | None = None,
    entry_paths: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """扫一批补丁（＝即将写入的新代码），返回机械finding 列表。

    只做**纯函数式**判定：不读盘、不改状态、不抛异常。调用方决定 findings 的处置
    （blocker 进机械阻断项，warn 进提示）。

    ``entry_paths``：项目自己的**入口脚本**路径集合（调用方从需求/方案里提取，见
    `orchestrator._entry_path_candidates`）。带 `exempt_entry_paths` 的规则会跳过它们 ——
    典型是 `debug_residue`：入口脚本里的 `print` 是**用户可见输出**，不是调试残留。
    """
    active = rules if rules is not None else load()
    if not active:
        return []
    entries = {_norm_path(p) for p in (entry_paths or ()) if str(p).strip()}
    out: list[dict[str, Any]] = []
    for edit in edits or []:
        if not isinstance(edit, dict):
            continue
        path = _norm_path(edit.get("path"))
        if not path:
            continue
        suffix = Path(path).suffix.lower()
        text = _content_of(edit)
        if not text.strip():
            continue
        for rule in active:
            try:
                if rule.kind not in ("regex", "manifest", "require"):
                    continue
                if rule.exempt_entry_paths and path in entries:
                    continue
                if rule.exclude_paths_regex and re.search(rule.exclude_paths_regex, path, re.I):
                    continue
                if not _applies(rule, path=path, suffix=suffix, text=text):
                    continue
                if rule.kind == "require":
                    out.extend(_scan_require(rule, path, text))
                else:
                    out.extend(_scan_text(rule, path, text))
            except Exception as exc:  # noqa: BLE001 - 单条规则出错绝不能拖垮流水线
                out.append(_finding(rule, path=path, line_no=0, excerpt="",
                                    note=f"规则执行异常被跳过：{type(exc).__name__}"))
    return out


def scan_tree(root: str | Path, written: Iterable[str] | None = None,
              *, rules: list[Rule] | None = None) -> list[dict[str, Any]]:
    """对**真实文件**做扫描：编码合法性、.env 是否进忽略清单。

    与 `scan_edits` 分开是因为这两类的判据是"文件本身的状态"，不是"补丁正文"：
    补丁里看不出 GBK 混入（patch 是 str），也看不出仓库里那份 .env。
    """
    active = rules if rules is not None else load()
    base = Path(root)
    out: list[dict[str, Any]] = []
    for rule in active:
        try:
            if rule.kind == "encoding":
                targets = [str(x) for x in (written or []) if str(x).strip()]
                if not targets:
                    targets = [
                        str(p.relative_to(base)).replace("\\", "/")
                        for p in base.rglob("*") if p.is_file()
                    ]
                for rel in targets:
                    if Path(rel).suffix.lower() not in TEXT_SUFFIXES:
                        continue
                    fp = base / rel
                    try:
                        data = fp.read_bytes()
                    except OSError:
                        continue
                    try:
                        data.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        out.append(_finding(rule, path=_norm_path(rel), line_no=0,
                                            excerpt=f"首个非法字节在偏移 {exc.start}"))
            elif rule.kind == "env_unignored":
                env = base / ".env"
                if not env.exists():
                    continue
                ignore = base / ".gitignore"
                text = ""
                try:
                    text = ignore.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
                if not re.search(r"(?m)^\s*\.env\s*$", text):
                    out.append(_finding(rule, path=".env", line_no=0,
                                        excerpt=".gitignore 里没有 `.env` 规则"))
        except Exception as exc:  # noqa: BLE001
            out.append(_finding(rule, path="", line_no=0, excerpt="",
                                note=f"规则执行异常被跳过：{type(exc).__name__}"))
    return out


def scan(edits: Iterable[Any] | None = None, *, work: str | Path | None = None,
         written: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """一次扫全：补丁正文 + （给了 work 时）真实文件。"""
    out = list(scan_edits(edits or []))
    if work is not None:
        out.extend(scan_tree(work, written))
    return out


# --------------------------------------------------------------------- 结果视图


def blockers(findings: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in findings if str(f.get("severity")) == "blocker" and not f.get("note")]


def warns(findings: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in findings if str(f.get("severity")) == "warn" and not f.get("note")]


def errors(findings: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """规则引擎自身的异常（不算判负，但要让人看见规则库坏了）。"""
    return [f for f in findings if f.get("note")]


def _where(f: dict[str, Any]) -> str:
    path = str(f.get("path") or "")
    line = int(f.get("line") or 0)
    return f"{path}:{line}" if line else path


def summarize(findings: Iterable[dict[str, Any]], *, limit: int = 12) -> list[str]:
    """一行一条的紧凑摘要（进 state / 日志 / 页面用）。"""
    out: list[str] = []
    for f in findings:
        tag = SEVERITY_CN.get(str(f.get("severity")), str(f.get("severity")))
        head = f"[{tag}] {f.get('title')} {_where(f)}"
        if f.get("note"):
            head += f"（{f['note']}）"
        out.append(head)
        if len(out) >= limit:
            break
    return out


def blocker_fixes(findings: Iterable[dict[str, Any]]) -> list[str]:
    """把阻断项转成**给开发看的整改要求**：定位 + 命中原文 + 反例出口。

    反例出口不能省：没有它，开发遇到"我这么写是对的"就只能硬编造，
    或者干脆顶着不做（真机上出现过 dev 因约束不可能满足而整轮空实现）。
    """
    out: list[str] = []
    for f in blockers(findings):
        out.append(
            f"修红线规则 {f.get('rule')}（{f.get('title')}）@ {_where(f)}："
            f"命中 `{f.get('excerpt') or '（见文件）'}`。"
            f"要么改掉它，要么在 deviations 里按反例判据说明为何不成立"
            f"（判据：{f.get('negative')}）"
        )
    return out


def format_block(findings: Iterable[dict[str, Any]], *, limit: int = 6) -> str:
    """把 findings 变成评审提示词里的机械证据块。

    末尾那段"要求"就是**证伪门禁**落到规则上的样子：主张某条不成立必须给反例，
    复述规则或照抄上一轮结论不算证据 —— 自指循环正是靠"照抄"维持的。
    """
    rows = [f for f in findings if not f.get("note")]
    notes = errors(findings)
    if not rows and not notes:
        return ""
    lines = ["【机械红线检查（机制算出来的事实，不是推测；判负依据与反例判据都列在这里）】"]
    for f in rows[:limit]:
        tag = SEVERITY_CN.get(str(f.get("severity")), str(f.get("severity")))
        lines.append(f"- [{tag}] {f.get('title')} · {_where(f)}")
        if f.get("excerpt"):
            lines.append(f"    · 命中：`{f.get('excerpt')}`")
        if f.get("message"):
            lines.append(f"    · 问题：{f.get('message')}")
        if f.get("evidence"):
            lines.append(f"    · 凭什么判负：{f.get('evidence')}")
        if f.get("negative"):
            lines.append(f"    · 反例判据：{f.get('negative')}")
        if f.get("source"):
            lines.append(f"    · 规则 {f.get('rule')}（出处：{f.get('source')}）")
    rest = len(rows) - limit
    if rest > 0:
        lines.append(f"（另有 {rest} 条同类finding，见 state.rule_findings）")
    for f in notes[:3]:
        lines.append(f"- 规则库自身异常（不算判负，但请人工看一眼）：{f.get('note')}")
    lines.append(
        "要求：① 阻断项**不得**被判 pass；② 若要主张某条不成立，必须按上面的「反例判据」"
        "给出具体反例（输入 + 实际行为/实测输出）；**只复述规则文字、或照抄上一轮已提出的"
        "结论，不作为证据，该条将被机制剔除**；③ 不要因为这些规则去改方案范围以外的东西。"
    )
    return "\n".join(lines)
