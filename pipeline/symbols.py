"""**Symbol Resolver**：符号 → 文件。依赖图建立的前提，不是优化。

为什么要显式解析（真机反例，不接受"猜"）
--------------------------------------
最新方案（`20260927-192001`）里架构师写的跨文件契约是：

    contracts.uses = ["database.insert_record", "models.format_amount", "errors.raise_unknown_error"]

而同一轮的接口骨架里，`database.py` 实际定义的类方法是 `Database.save_record` ——
即 `insert_record` **在产物里根本不存在**。于是：

  · 按符号名硬连 ⇒ 连到虚节点，依赖图是假的；
  · 按"叶子名兜底"（我们代码里到处是 `s.rsplit(".", 1)[-1]` 这种兜底）⇒
    `save_record` 与 `insert_record` 被当成两回事，**悄悄漏掉**，没有告警。

结论：**必须显式解析；解析不唯一时必须暴露（unresolved），不许猜。**
隐藏的依赖错误会在运行时变成 `AttributeError`，而那时候归因成本高一个数量级。

点号语义（此前从未定义过 —— 这正是 `CLI.add` 与 `db.insert_record` 被当成同一类的原因）
------------------------------------------------------------------------------------
    `db.insert_record`  → "模块.符号"（第一段命中文件名 / 模块名）
    `CLI.add`           → "类.方法"（第一段命中骨架里的类名）

两者字面同形，必须先消歧：

  ① 第一段命中**本单元文件范围**里的模块名 ⇒ 模块型（范围优先，最可信）；
  ② 第一段命中全局模块索引（唯一）⇒ 模块型；
  ③ 第一段命中已知类名（来自接口骨架）⇒ 类型；
  ④ 否则按**后缀匹配**尝试模块（`db` 匹配 `*/db.py`），**唯一命中**才算；
  ⑤ 都不中 ⇒ unresolved，带原因返回，由上层暴露。

两阶段索引**不同源**：方案阶段还没有代码，索引只能建在「方案声明的文件 + 接口骨架」上；
verify 阶段才有真实代码索引。混用会让解析结果随阶段漂移。
"""
from __future__ import annotations

import ast
import sys
from typing import Any

__all__ = [
    "build_index",
    "clean_symbol",
    "resolve",
    "resolve_symbol_to_file",
    "unresolved_warnings",
]

#: 标准库模块名（3.10+ 由解释器提供）。真机 `contracts.uses` 里的 `tkinter` / `sqlite3`
#: 属于**外部依赖**，不是"本产物内的契约对不上"。把它们混进 unresolved 会稀释信号 ——
#: 而"提示级误报的代价同样是整轮返工"是本项目已经付过学费的教训（见 CONTEXT §25）。
_STDLIB_MODULES = frozenset(getattr(sys, "stdlib_module_names", ()) or ())

#: `clean_symbol` 要剥掉的首尾标点（符号名不会以这些字符开头或结尾）
_PUNCT = " \t\r\n:：,，;；、。\"'`"


def _norm(path: Any) -> str:
    return str(path or "").replace("\\", "/").strip()


def _module_of(path: str) -> str:
    """`ledger/db.py` → `ledger.db`；`db.py` → `db`。"""
    p = _norm(path)
    if p.endswith(".py"):
        p = p[:-3]
    return p.replace("/", ".")


def digest_symbols(records: Any) -> dict[str, list[tuple[str, str]]]:
    """解析**摘要行**（`verify.skeleton_digest` 与 `verify.api_digest` 的产物）。

    为什么解析"给人看的摘要"而不是原始结构：二者**本来就同形**是刻意的设计 ——
    骨架摘要与真实代码摘要用同一套表示，一份基准可被三处消费（提示词准绳、
    参数个数核对、跨文件契约校验）。这里复用同一份表示，就不会出现"第二套表示"。

    行格式：``class X(Y)`` / ``    实例属性: a, b`` / ``    def m(p)`` / ``def f(p)`` / 常量。
    **缩进是语义**：带缩进的方法属于它上面最近的那个类；不带缩进的是模块级函数。

    返回 `{path: [(kind, symbol)]}`，`kind` ∈ {"class", "method", "function", "constant"}。
    """
    out: dict[str, list[tuple[str, str]]] = {}
    if not isinstance(records, dict):
        return out
    for path, lines in records.items():
        p = _norm(path)
        if not p or not isinstance(lines, (list, tuple)):
            continue
        found: list[tuple[str, str]] = []
        current: str = ""
        for line in lines:
            text = str(line or "").rstrip()
            if not text.strip():
                continue
            indented = text[:1] in (" ", "\t")
            token = text.strip()
            if token.startswith("class "):
                name = token[len("class "):].split("(")[0].strip()
                if name:
                    current = name
                    found.append(("class", name))
            elif token.startswith("def "):
                name = token[len("def "):].split("(")[0].strip()
                if not name:
                    continue
                if indented and current:
                    found.append(("method", f"{current}.{name}"))
                else:
                    found.append(("function", name))
            elif token.startswith("实例属性"):
                continue
            else:
                found.append(("constant", token))
        if found:
            out[p] = found
    return out


def build_index(files: Any, skeleton: Any = None) -> dict:
    """建**方案阶段**的符号索引：模块名 → 文件、类名 → 文件、符号 → 文件。

    `files`：方案声明的文件清单（`changes[].path` 等）。
    `skeleton`：接口基准摘要（`state["skeleton"]`，即 `verify.skeleton_digest` 的产物：
    `{"db.py": ["class DB", "    def save(p)"]}`）。它是**唯一**能提供"类.方法"层级的
    事实来源 —— 没有它，`CLI.add` 与 `db.insert` 无法消歧。

    兼容两种输入：摘要 dict（运行时真实形态）与原始 `{"files": [...]}`
    （单测与排障时手工构造，更直观）。
    """
    modules: dict[str, list[str]] = {}
    classes: dict[str, list[str]] = {}
    members: dict[str, list[str]] = {}  # "类.方法" / 顶层函数名 → 文件

    def _reg(bucket: dict[str, list[str]], key: str, path: str) -> None:
        if not key or not path:
            return
        lst = bucket.setdefault(key, [])
        if path not in lst:
            lst.append(path)

    def _finish() -> dict:
        # **成员归属索引**：`path → {成员名…}`（含 `类.方法` 与叶子名）。
        # 它让解析器能回答"模块存在，但成员不存在吗" —— 真机里 `database.insert_record`
        # 就是这种**虚依赖**：`database.py` 确实存在，可它定义的是 `Database.save_record`。
        # 少了这一层，依赖图会连到虚节点上（看着解析成功，其实指向不存在的符号）。
        file_members: dict[str, set[str]] = {}
        for name, paths in members.items():
            leaf = name.rsplit(".", 1)[-1]
            for p in paths:
                file_members.setdefault(p, set()).update({name, leaf})
        return {
            "modules": modules,
            "classes": classes,
            "members": members,
            "file_members": file_members,
        }

    def _reg_module(path: str) -> None:
        if not path.endswith(".py"):
            return
        full = _module_of(path)
        _reg(modules, full, path)
        _reg(modules, full.rsplit(".", 1)[-1], path)  # basename：`db` 也能命中

    for path in [p for p in (_norm(f) for f in (files or [])) if p]:
        _reg_module(path)

    if isinstance(skeleton, dict) and skeleton.get("files"):
        # 原始结构形态
        for entry in skeleton.get("files") or []:
            if not isinstance(entry, dict):
                continue
            path = _norm(entry.get("path"))
            if not path:
                continue
            _reg_module(path)
            for cls in (entry.get("classes") or []):
                if not isinstance(cls, dict):
                    continue
                cname = str(cls.get("name") or "").strip()
                if not cname:
                    continue
                _reg(classes, cname, path)
                _reg(members, cname, path)
                for m in (cls.get("methods") or []):
                    mname = str((m or {}).get("name") or "").strip() if isinstance(m, dict) else str(m).strip()
                    if mname:
                        _reg(members, f"{cname}.{mname}", path)
            for fn in (entry.get("functions") or []):
                fname = str((fn or {}).get("name") or "").strip() if isinstance(fn, dict) else str(fn).strip()
                if fname:
                    _reg(members, fname, path)
        return _finish()

    for path, rows in digest_symbols(skeleton).items():
        _reg_module(path)
        for kind, name in rows:
            if kind == "class":
                _reg(classes, name, path)
                _reg(members, name, path)
            elif kind in ("method", "function"):
                _reg(members, name, path)
    return _finish()


def clean_symbol(sym: Any) -> str:
    """把**调用写法**归一成符号名：`DBManager.insert()` → `DBManager.insert`、
    `db.insert(amount, note) -> int` → `db.insert`。

    真机证据（`20260927-214253`）：该运行 `contracts.uses` 里的 6 条引用**全是**调用形态。
    这是**书写归一，不是猜测** —— 不做这一步，本来能对上的引用会被判成虚依赖，
    真问题被淹没（而"误报把信号稀释掉"是本项目已经付过学费的事）。

    只剥**尾部**的调用括号、返回类型注解与**首尾标点**：符号名本身不可能含 `()` / `->`，
    也不可能以 `:` / `,` 结尾。

    首尾标点必须剥（真机 20260927-224002 实测）：架构师把 `contracts.uses` 写成了
    ``["sqlite3.connect():", "DBManager.add_entry():"]`` —— 尾部那个冒号是**排版残留**，
    不是"文件:符号"的分隔符。不剥它，解析器会把 `sqlite3.connect()` 当成**文件名**，
    于是 5 条本来合法的依赖被报成"虚依赖" —— 而误报会稀释信号（§25 已付过学费）。
    """
    text = str(sym or "").strip()
    text = text.strip(_PUNCT)
    if "->" in text:
        text = text.split("->", 1)[0].strip()
    while text.endswith(")") and "(" in text and text.rindex("(") > 0:
        text = text[: text.rindex("(")].strip()
    return text.strip(_PUNCT)


def _split_file_hint(sym: str) -> tuple[str, str]:
    """切出**文件限定**形式：`main.py:Game` / `ledger/db.py:DB.insert` → (路径, 成员)。

    真机 `20260927-134222` 的 `contracts.uses` **六个文件全是这种写法** —— 它是模型最
    自然的表达（"用 main.py 里的 Game"）。不认它，`main.py:Game` 会被 `partition(".")`
    切成"模块 `main` + 符号 `py:Game`"，于是**静默解析到 main.py 上**：
    看着成功、其实全错。**错误被藏起来比解析失败更糟**，所以这一层必须显式先处理。
    """
    for sep in (":", "："):
        if sep in sym:
            left, _, right = sym.partition(sep)
            return left.strip(), right.strip()
    return "", ""


def resolve(symbol: str, *, files: Any = None, index: dict | None = None) -> dict:
    """解析一个符号指向哪个文件。

    返回**永远是**同形的 dict（成功与失败都在同一形里，便于上层统一处理）：

        {"symbol", "kind", "candidates", "resolved", "reason"}

      · `kind` ∈ {"file", "module", "class", "member", "bare", "external", "unknown"}
        `external` = 标准库/第三方依赖：**算解析成功**（它确实是有意义的依赖），
        但不参与"产物内契约对不上"的核对 —— 把 `tkinter` 报成虚依赖只会稀释信号。
      · `resolved=False` 时 `reason` 是人话，且 `candidates` 保留全部候选
        （**多个候选就是"不唯一"，不建边、不猜**）。
    """
    raw = str(symbol or "").strip()
    sym = clean_symbol(raw)
    idx = index if isinstance(index, dict) else {}
    modules = idx.get("modules") or {}
    classes = idx.get("classes") or {}
    members = idx.get("members") or {}
    scope = [p for p in (_norm(f) for f in (files or [])) if p]
    file_members = idx.get("file_members") or {}
    known_files = sorted({*scope, *(p for ps in modules.values() for p in ps)})

    def _member_gap(path: str, head: str, tail: str) -> str:
        """模块存在时，成员是否也在接口基准里？返回 "" = 核对通过；否则返回原因。

        这是**防虚依赖**的那一道关：`database.insert_record` 里 `database.py` 确实存在，
        但它定义的是 `Database.save_record` —— 少了这一关，依赖图会"看着解析成功"
        地连到一个不存在的符号上，直到运行时 `AttributeError` 才暴露。
        基准里没有该文件的成员信息时（骨架缺失/为空）**不算错**，否则会全是假警报。
        """
        known = file_members.get(path)
        if not known or tail in known:
            return ""
        return f"模块 `{head}` 存在，但 `{tail}` 不在接口基准里（命名可能不一致）"

    out = {"symbol": sym or raw, "kind": "unknown", "candidates": [], "resolved": False, "reason": ""}
    if not sym:
        # 区分"本来就空"与"清洗后为空"：后者要让人看到**原文长什么样**（真机里就是单个逗号），
        # 否则排查时只看到"空符号"，不知道是被谁吞掉的。
        out["reason"] = f"非法符号（清洗后为空：{raw!r}）" if raw else "空符号"
        return out
    if not any(ch.isalpha() or ch == "_" for ch in sym):
        # 真机 `20260927-173023` 的 `contracts.uses` 就是单个逗号 —— 模型吐出的垃圾。
        # 这类输入既不该进依赖图，更不该被"兜底匹配"恰好匹配到某个文件上。
        out["reason"] = f"非法符号（不含标识符字符：{sym!r}）"
        return out

    def _by_file(base_raw: str, member: str) -> dict:
        """按「文件 + 成员」解析（两种书写都汇聚到这里，见下面的 ⑥）。"""
        base = _norm(base_raw)
        cands: list[str] = []
        if base.endswith(".py"):
            cands = [p for p in known_files if p == base or p.endswith("/" + base)]
        if not cands:
            stem = base[:-3] if base.endswith(".py") else base
            cands = [p for p in known_files if p == f"{stem}.py" or p.endswith(f"/{stem}.py")]
        if not cands:
            cands = list(modules.get(base) or modules.get(base.rsplit("/", 1)[-1]) or [])
        if len(cands) == 1:
            gap = _member_gap(cands[0], base, member)
            out.update(kind="file", candidates=cands, resolved=not gap, reason=gap)
            return out
        if len(cands) > 1:
            out.update(kind="file", candidates=sorted(set(cands)), reason="多个候选（不唯一，拒绝猜测）")
            return out
        out.update(kind="file", reason=f"找不到 `{base}` 这个文件")
        return out

    # ⑥ **文件限定形式**。真机里出现过两种书写，都必须**先于点号处理**：
    #      `main.py:Game`   —— 134222，六个文件全部用冒号
    #      `game.py.Game`   —— 150931，用点号把路径与符号串起来
    # 不先处理后者，它会被 `partition(".")` 切成"模块 `game` + 符号 `py.Game`"：
    # 看着解析成功、其实全错 —— **错误被藏起来，比解析失败更糟**。
    hint, member = _split_file_hint(sym)
    if hint:
        if not member:
            out.update(kind="file", reason=f"只给了文件 `{hint}`，没给成员名")
            return out
        return _by_file(hint, member)

    # ⑦ **文件级依赖**：整个符号就是一个文件路径（真机 150931 的 `uses` 全是 `game.py`）。
    # 这是有意义的依赖（"我用 game.py"），按文件建边即可；但它没有成员名，
    # **不能**拿去当符号级契约核对。判定必须精确到"整个符号 == 已知文件"，
    # 否则 `ledger/db.py.DB.insert` 这种路径+符号的写法也会被误吞。
    _base = _norm(sym)
    if _base.endswith(".py") or "/" in _base or "\\" in _base:
        _stem = _base[:-3] if _base.endswith(".py") else _base
        _exact = [
            p for p in known_files
            if p == _base or p == f"{_stem}.py" or p.endswith(f"/{_stem}.py")
        ]
        if len(_exact) == 1:
            out.update(kind="file", candidates=_exact, resolved=True,
                       reason="文件级依赖（未指定成员）")
            return out
        if len(_exact) > 1:
            out.update(kind="file", candidates=sorted(set(_exact)), reason="多个候选（不唯一，拒绝猜测）")
            return out
        out.update(kind="file", reason=f"找不到 `{_base}` 这个文件")
        return out

    head, _, tail = sym.partition(".")
    if tail:
        # 点号式文件限定：`game.py.Game` / `cli.CLI.add` / `ledger/db.py.DB.insert`。
        # 注意 `partition(".")` **只切第一个点**，head 永远是第一段（`game`），
        # 所以不能拿 head 判断"是不是文件"；要拿已知文件清单去试**最长前缀**。
        # 每个文件取"被切掉后剩下的最短"那一刀（= 最长前缀），最后按文件去重：
        # 唯一命中才解析，多文件同命中 ⇒ **不猜**。
        best: dict[str, str] = {}
        for path in [*scope, *(p for p in known_files if p not in scope)]:
            stem = path[:-3] if path.endswith(".py") else path
            # 两种前缀（完整路径 / 模块名 stem）× 两种限定符：
            #   点号 —— `db.py.add_record` / `db.add_record`
            #   中文「的」—— 真机 20260928-160609 架构师写成 `db.py的add_record()`
            # 都按「文件限定 + 成员」汇聚到 _by_file，复用同一套不唯一/虚成员判定。
            for cand, sep in ((path, "."), (stem, "."), (path, "的"), (stem, "的")):
                # stem + "." 不得吃**文件扩展名**那个点：`db.py的add_record` 里 `db.`
                # 后面是 `py`（整个文件名还在符号里），那不是成员点号 ——
                # 旧逻辑据此错切成成员 `py的add_record`，报出模型无法理解的假冲突。
                if (
                    sep == "."
                    and cand == stem
                    and path.endswith(".py")
                    and sym.startswith(path)
                ):
                    continue
                if cand and sym.startswith(cand + sep):
                    rest = sym[len(cand) + len(sep):]
                    if path not in best or len(rest) < len(best[path]):
                        best[path] = rest
        if len(best) == 1:
            path, rest = next(iter(best.items()))
            return _by_file(path, rest)
        if len(best) > 1:
            out.update(
                kind="file",
                candidates=sorted(best),
                reason="多个候选（不唯一，拒绝猜测）",
            )
            return out
        # ① 本单元文件范围内命中模块 ⇒ 最可信
        scoped_modules = {_module_of(p): p for p in scope if p.endswith(".py")}
        for name, path in scoped_modules.items():
            if name == head or name.rsplit(".", 1)[-1] == head:
                gap = _member_gap(path, head, tail)
                out.update(kind="module", candidates=[path], resolved=not gap, reason=gap)
                return out
        # ② 全局模块索引，唯一命中才算
        hit = [p for p in (modules.get(head) or [])]
        if len(hit) == 1:
            gap = _member_gap(hit[0], head, tail)
            out.update(kind="module", candidates=hit, resolved=not gap, reason=gap)
            return out
        # ③ 第一段是类名 ⇒ "类.方法"
        if head in classes:
            cls_hit = list(classes.get(head) or [])
            full = f"{head}.{tail}"
            if full in members:
                cls_hit = list(members[full] or cls_hit)
            if len(cls_hit) == 1:
                out.update(kind="class", candidates=cls_hit, resolved=True)
                return out
            if len(cls_hit) > 1:
                out.update(kind="class", candidates=cls_hit, reason="多个候选文件（类名不唯一）")
                return out
            out.update(kind="class", reason=f"类 `{head}` 有定义，但方法 `{tail}` 不在接口骨架里")
            return out
        # ④ 模块后缀匹配：第一段完全未知时，按"路径尾段"匹配（`db` → `*/db.py`）
        suffix = [p for p in known_files if p.endswith(f"/{head}.py")] or [
            p for p in known_files if p.endswith(f"{head}.py")
        ]
        if len(suffix) == 1:
            out.update(kind="module", candidates=suffix, resolved=True)
            return out
        # ⑤ 全局符号索引兜底：只认**唯一**命中
        bare = [p for p in (members.get(tail) or [])]
        if len(bare) == 1:
            out.update(kind="member", candidates=bare, resolved=True)
            return out
        if len(hit) > 1 or len(bare) > 1:
            out.update(
                kind="module",
                candidates=sorted(set(hit or bare)),
                reason="多个候选（不唯一，拒绝猜测）",
            )
            return out
        if head in _STDLIB_MODULES:
            out.update(kind="external", resolved=True,
                       reason=f"外部依赖 `{head}`（标准库），不参与产物内契约核对")
            return out
        out["reason"] = f"模块 `{head}` 与符号 `{tail}` 都定位不到"
        return out

    # 无点号：全局符号索引，唯一命中
    hit = [p for p in (members.get(sym) or [])]
    if len(hit) == 1:
        out.update(kind="bare", candidates=hit, resolved=True)
        return out
    if len(hit) > 1:
        out.update(kind="bare", candidates=sorted(hit), reason="多个候选（不唯一，拒绝猜测）")
        return out
    if sym in _STDLIB_MODULES:
        out.update(kind="external", resolved=True,
                   reason=f"外部依赖 `{sym}`（标准库），不参与产物内契约核对")
        return out
    # 也可能是模块名本身，但那个不该出现在依赖图里（依赖的是"谁的什么"）
    out.update(kind="bare", candidates=sorted(modules.get(sym) or []), reason="未在接口骨架里找到该符号")
    return out


def resolve_symbol_to_file(
    symbol: str,
    *,
    changes_files: Any = (),
    existing_files: Any = (),
    skeleton: Any = None,
) -> dict[str, Any]:
    """TaskCompiler 用的符号→文件解析（规格§十三）：**多候选就是 unresolved，禁止猜第一个**。

    在 ``resolve()`` 外面包一层面向调用方的稳定结果：

      * ``{"status": "resolved", "file": "db.py", "kind": ..., "reason": ""}``
      * ``{"status": "external", "file": "", "kind": "external", "reason": ...}``
      * ``{"status": "unresolved", "file": "", "candidates": [...],
            "reason": "ambiguous_symbol" | "not_found" | ...}``

    解析顺序沿用 resolve()：① 显式 path ② 模块精确 ③ 类名 ④ 路径后缀 ⑤ 全局唯一。
    唯一候选但成员对不上（模块确定、方法不在骨架里）仍判 ``resolved``——文件边是确定的，
    成员缺口是另一个问题，由契约校验报告，不在这里吞掉。
    """
    files = [_norm(p) for p in (changes_files or ()) if p]
    existing_norm = [_norm(p) for p in (existing_files or ()) if p]
    for p in existing_norm:
        if p not in files:
            files.append(p)
    index = build_index(files, skeleton)
    row = resolve(symbol, files=files, index=index)
    candidates = sorted({_norm(c) for c in (row.get("candidates") or []) if c})
    kind = str(row.get("kind") or "")
    reason = str(row.get("reason") or "")
    if kind == "external" and not candidates:
        return {"status": "external", "file": "", "kind": kind, "reason": reason,
                "candidates": []}
    if len(candidates) == 1:
        return {"status": "resolved", "file": candidates[0], "kind": kind,
                "reason": reason, "candidates": candidates}
    if len(candidates) > 1:
        return {"status": "unresolved", "file": "", "kind": kind,
                "reason": "ambiguous_symbol", "candidates": candidates}
    return {"status": "unresolved", "file": "", "kind": kind,
            "reason": reason or "not_found", "candidates": []}


def unresolved_warnings(rows: Any) -> list[dict]:
    """把解析结果里**没解析成功**的转成可展示的告警（`dependency_warning` 形态）。

    设计要点：unresolved **必须暴露**而不是隐藏 —— 隐藏的依赖错误会在运行时变成
    `AttributeError`，那时归因成本高一个数量级；暴露出来则能在方案阶段就计入提示。
    """
    out: list[dict] = []
    for row in (rows or []):
        if not isinstance(row, dict) or row.get("resolved"):
            continue
        out.append(
            {
                "kind": "unresolved_dependency",
                "symbol": row.get("symbol"),
                "reason": row.get("reason") or "无法解析",
                "candidates": list(row.get("candidates") or []),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Symbol Manifest（P0-8）与符号冲突检测（P0-9）
# ---------------------------------------------------------------------------

def _skeleton_symbols(skeleton: Any) -> dict[str, list[str]]:
    """从冻结骨架里取出 {path: [symbols]}（骨架有多种历史形态，逐个兼容）。"""
    out: dict[str, list[str]] = {}
    if not isinstance(skeleton, dict):
        return out
    files = skeleton.get("files")
    if isinstance(files, dict):
        for path, val in files.items():
            key = _norm(path)
            if not key:
                continue
            if isinstance(val, dict):
                names = [str(s) for s in (val.get("symbols") or []) if str(s).strip()]
            elif isinstance(val, list):
                names = [str(s) for s in val if str(s).strip()]
            else:
                names = []
            if names:
                out.setdefault(key, []).extend(names)
    for ch in skeleton.get("changes") or []:
        if not isinstance(ch, dict):
            continue
        key = _norm(ch.get("path"))
        names = [str(s) for s in (ch.get("symbols") or []) if str(s).strip()]
        if key and names:
            out.setdefault(key, []).extend(names)
    return out


def build_symbol_manifest(tasks: Any, skeleton: Any = None) -> dict:
    """DEV **之前**生成的确定性符号清单（P0-8）。

    来源优先：TaskCompiler 的施工图 + 冻结骨架；**不从 DEV 代码反推设计** ——
    反推等于让实现给自己发证（实现缺什么，清单就少什么，永远对得上）。
    DEV 产出后用这里的 planned 与实际的 actual_symbols 做机械 diff。

    返回 ``{"version": 1, "files": {path: [symbols]}, "sources": [...]}``。
    """
    manifest: dict[str, list[str]] = {}
    sources: list[str] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        names = [str(s).strip() for s in (task.get("symbols") or []) if str(s).strip()]
        if not names:
            continue
        for raw in task.get("target_files") or []:
            path = _norm(raw)
            if not path:
                continue
            bucket = manifest.setdefault(path, [])
            for name in names:
                if name not in bucket:
                    bucket.append(name)
        if "taskcompiler" not in sources:
            sources.append("taskcompiler")
    for path, names in _skeleton_symbols(skeleton).items():
        bucket = manifest.setdefault(path, [])
        for name in names:
            if name not in bucket:
                bucket.append(name)
        if "skeleton" not in sources:
            sources.append("skeleton")
    return {"version": 1, "files": manifest, "sources": sources}


def planned_vs_actual(manifest: dict, actual: dict[str, list[str]]) -> dict:
    """计划符号 vs 实际产出符号的机械 diff（P0-8 的下半段）。

    只看**缺失**（声明了却没写出来）；实际多出来的不判罪 ——
    实现里允许有辅助符号，硬卡"只多不少"会把合法实现打成缺陷。
    """
    planned_files = (manifest or {}).get("files") or {}
    missing: dict[str, list[str]] = {}
    for path, names in planned_files.items():
        got = {str(s).strip() for s in (actual or {}).get(path) or []}
        lack = [n for n in names if n not in got]
        if lack:
            missing[path] = lack
    return {"missing": missing, "missing_count": sum(len(v) for v in missing.values())}


def validate_symbol_collisions(files: dict[str, str]) -> list[dict]:
    """**确定性 AST 检查**（P0-9）：DEV 刚产出就越权/自相矛盾的符号定义。

    真机出现过的形态：``Game.is_game_over`` 既是属性（``self.is_game_over = False``）
    又是方法（``def is_game_over(self)``）—— 运行期方法被属性值覆盖，
    不能等 pyright / review 才发现。

    检查项：
      * ``SYMBOL_MEMBER_COLLISION`` —— 同类中同名「self 属性赋值 + 方法」
      * ``DUPLICATE_TOP_LEVEL_SYMBOL`` —— 顶层类/函数/变量重名
      * ``DUPLICATE_METHOD`` —— 同类中方法重名
    语法错误不在这里报（交给语法档），解析不了就跳过该文件。
    """
    out: list[dict] = []
    for path, source in (files or {}).items():
        if not isinstance(source, str) or not source.strip():
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            continue
        # ---- 顶层重名
        top: dict[str, int] = {}
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                top[node.name] = top.get(node.name, 0) + 1
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        top[target.id] = top.get(target.id, 0) + 1
        for name, count in top.items():
            if count > 1:
                out.append({
                    "code": "DUPLICATE_TOP_LEVEL_SYMBOL",
                    "file": str(path), "symbol": name, "severity": "block",
                    "detail": f"{path}: 顶层符号 {name} 被定义 {count} 次（后定义的会覆盖先定义的）",
                })
        # ---- 类内冲突
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            methods: dict[str, int] = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods[item.name] = methods.get(item.name, 0) + 1
            attrs: set[str] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if (isinstance(target, ast.Attribute)
                                and isinstance(target.value, ast.Name)
                                and target.value.id == "self"):
                            attrs.add(target.attr)
                elif isinstance(sub, ast.AnnAssign) and sub.value is not None:
                    target = sub.target
                    if (isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"):
                        attrs.add(target.attr)
            for name in sorted(set(methods) & attrs):
                out.append({
                    "code": "SYMBOL_MEMBER_COLLISION",
                    "file": str(path), "symbol": f"{node.name}.{name}", "severity": "block",
                    "detail": (
                        f"{path}: {node.name} 里 {name} 同时被当作**属性**（self.{name} = …）"
                        f"和**方法**（def {name}）定义 —— 运行期方法会被属性值覆盖"
                        "（真机 Game.is_game_over 的形态）"
                    ),
                })
            for name, count in methods.items():
                if count > 1:
                    out.append({
                        "code": "DUPLICATE_METHOD",
                        "file": str(path), "symbol": f"{node.name}.{name}", "severity": "block",
                        "detail": f"{path}: {node.name} 里方法 {name} 被定义 {count} 次",
                    })
    return out
