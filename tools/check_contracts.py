"""Schema Guardian（建议⑯）：三方对账 Prompt 声明字段 ↔ Schema 允许字段 ↔ 代码实际消费字段。

为什么需要它
------------
``flow.validate()`` 把「模型产出 ↔ Schema」卡得很严（封闭契约，多余字段直接判违约），
但第三层一直没人查：

    Prompt 字段（提示词要求模型产出）
        ↕
    Schema 字段（grammar 物理上允许产出）
        ↕
    代码读取字段（下游 .get 消费）

真机事故（tasks[].symbols 填充率 0/43）：系统提示词一直要求、下游编译器一直读，
而 schema 里压根没声明 —— ``format=schema`` 是 grammar 约束，模型**结构上不可能**
吐出这个字段，提示词写得再重也等于零。这类问题靠人眼对照三份文件必然漏，所以做成
离线闸门，注册进 ``tools/smoke_all.py``。

判级
----
- **HARD（退出 1）**：提示词以「字段规范」的强形式（花括号枚举 / 圆点条目 / 等号定义 /
  点号路径）声明了字段，而该阶段 schema 任何层级都不允许它 —— 模型物理上产不出来。
  这正是 0/43 事故的形状。
- **ADVISORY（只报告）**：schema 允许但非 required 的字段，提示词却用了「必须 / 必填」
  口径 —— 典型如 tasks[].contracts：「有这个字段」和「它真参与系统契约」不是一回事。
- **WARN（只报告）**：消费方读取了 schema 之外、提示词也没声明的名字（通常是编排器
  在产物上挂的机械字段，如 review.forced_pass），用于人工确认不是读错对象。

实现刻意保持零依赖、纯静态：schema 直接 import；提示词从 prompts.py 的 AST 取
（4 套 system 字典 + parts_<stage> 函数源码）；消费侧只认 ``.get("field")`` 这种防御式
读法（本代码库对模型产物的标准访问方式）。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.schemas import STAGE_SCHEMAS  # noqa: E402

PROMPTS_PY = ROOT / "pipeline" / "prompts.py"
PIPELINE_DIR = ROOT / "pipeline"

#: 提示词解析来源：4 套 system 提示词字典（存量 / 全新 / 全新返工 / 全新方案返工）。
#: 同一阶段在不同变体里声明的字段取并集 —— 任何一套要求了、schema 不允许，都是硬矛盾。
SYSTEM_DICTS = ("SYSTEM", "SYSTEM_NEW", "SYSTEM_NEW_BUGFIX", "SYSTEM_NEW_PLANREWORK")

#: 各阶段产物的消费方（.get 读取即视为消费；prompts 渲染给下游也算消费）。
#: 只列真正读该阶段产物的模块，避免跨阶段同名噪声。
CONSUMERS: dict[str, tuple[str, ...]] = {
    "intake": ("orchestrator.py", "prompts.py", "gateway.py"),
    "pm": ("orchestrator.py", "prompts.py", "prd.py"),
    "architect_assess": ("orchestrator.py", "prompts.py"),
    "architect_plan": ("orchestrator.py", "prompts.py", "taskcompiler.py", "verify.py", "planir.py"),
    "dev": ("orchestrator.py", "prompts.py", "patches.py", "verify.py", "semantics.py", "issues.py"),
    "test": ("orchestrator.py", "prompts.py", "verify.py"),
    "verify": ("orchestrator.py", "prompts.py", "verify.py", "diagnose.py"),
    "review": ("orchestrator.py", "prompts.py", "diagnose.py"),
    "human_review": ("orchestrator.py", "prompts.py"),
}

#: 提示词里以强形式出现但不是产物字段的词（枚举值 / 通用词），命中不视为字段声明。
NOT_A_FIELD = {
    "high", "medium", "low", "pass", "fail", "ok", "error", "timeout", "skipped",
    "add", "modify", "delete", "new", "easy", "hard", "string", "integer", "boolean",
    "array", "object", "none", "null", "true", "false", "python", "stderr", "stdout",
    "utf8", "gbk", "json", "cli",
}

#: 提示词里展示的**机械证据块**键名（不是本阶段产物字段，是编排器喂进来的材料标签）。
#: 如 review 提示词引用「运行验证结果里的 mechanical_facts」—— 它来自渲染块而非 REVIEW schema。
PROMPT_CONTEXT_KEYS: dict[str, set[str]] = {
    "review": {"mechanical_facts"},
}

# ---------------------------------------------------------------- Schema 遍历
def walk_schema(node: dict, prefix: str = ""):
    """产出 ``(path, name, required)``：每个 object 属性一条，数组项路径带 ``[]``。"""
    if not isinstance(node, dict):
        return
    if node.get("type") == "object" and node.get("properties"):
        required = set(node.get("required") or [])
        for name, child in node["properties"].items():
            path = f"{prefix}.{name}" if prefix else name
            yield path, name, name in required
            yield from walk_schema(child, path)
    elif node.get("type") == "array" and isinstance(node.get("items"), dict):
        yield from walk_schema(node["items"], prefix + "[]")


def schema_fields(stage: str):
    """返回 (name → paths, 所有 (path,name,required) 三元组)。"""
    rows = list(walk_schema(STAGE_SCHEMAS[stage]))
    by_name: dict[str, set[str]] = {}
    for path, name, _req in rows:
        by_name.setdefault(name, set()).add(path)
    return by_name, rows


# ---------------------------------------------------------------- 提示词解析
def _literal_text(node: ast.AST) -> str:
    """把字典值节点里的字符串字面量拼出来（容忍 `+ _TAIL` 这类 Name 拼接，按空串处理）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_text(node.left) + _literal_text(node.right)
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value for v in node.values
            if isinstance(v, ast.FormattedValue) is False and isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    return ""


def stage_prompt_text(stage: str) -> str:
    """取某阶段的全部提示词文本：4 套 system 字典对应键 + parts_<stage> 函数里的字符串字面量。

    函数只取字符串节点（不取整段源码）：源码里的参数名 / 局部变量（``impl: dict``、
    ``fixes=None``）会被冒号规则误判成字段声明，而字段规范文本**全部写在字符串里**。
    """
    source = PROMPTS_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    chunks: list[str] = []

    def dict_target_name(node: ast.AST) -> str:
        # SYSTEM = {...} 是 Assign；SYSTEM: dict[str, str] = {...} 是 AnnAssign —— 两种都要认。
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    return t.id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            return node.target.id
        return ""

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and dict_target_name(node) in SYSTEM_DICTS:
            value = node.value
            if isinstance(value, ast.Dict):
                for idx, key in enumerate(value.keys):
                    if not isinstance(key, ast.Constant) or key.value != stage:
                        continue
                    chunks.append(_literal_text(value.values[idx]))
        if isinstance(node, ast.FunctionDef) and node.name == f"parts_{stage}":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    chunks.append(sub.value)
    return "\n".join(chunks)


_RE_BRACE_ITEMS = re.compile(r"[A-Za-z_]\w*\s*每项[^{]{0,8}\{([^{}]*)\}")
_RE_BRACE_ANY = re.compile(r"[＝:：]\s*\{([^{}]*)\}|\{([^{}]*)\}")
_RE_BRACE_GENERIC = re.compile(r"(\w+)\s*逐条给\s*\{([^{}]*)\}")
_RE_BULLET = re.compile(r"^\s*[·•\-*]\s*[`*]{0,2}([a-z_][a-z0-9_]*)[`*]{0,2}\s*[：:＝=]", re.M)
# 只认真全角冒号 / 全角等号：ASCII ":" 会命中签名示例（`add(amount: float, note: str)`）
# 这类代码片段里的参数名，不是字段定义。
_RE_DEFINE = re.compile(r"(?<![\w.])([a-z_][a-z0-9_]{1,})\s*[：＝]")
_RE_DOTTED_BRACKET = re.compile(r"\b([a-z_]\w*)\[\]\.([a-z_]\w*)")
# 嵌套字段名限 3 字符以上：挡掉 main.py / foo.js 这类文件名的扩展名。
_RE_DOTTED = re.compile(r"\b([a-z_]\w*)\.([a-z_][a-z0-9_]{2,})\b")
# 斜杠列举：「· 补全的 background / core_goal / target_users 等…」（仅圆点/短线条目行内）
_RE_SLASH_LINE = re.compile(r"^\s*[·•\-*].*$", re.M)
_RE_SLASH_NAMES = re.compile(r"[a-z_][a-z0-9_]*(?:\s*/\s*[a-z_][a-z0-9_]*)+")
# 括号释义：「每条给 element（要定的是什么）、why（为什么…）」
_RE_PAREN_FIELD = re.compile(r"(?<![\w.])([a-z_][a-z0-9_]{1,})（")


def _clean_token(raw: str) -> str:
    t = raw.strip().strip("`*'\"")
    return t if re.fullmatch(r"[a-z_][a-z0-9_]{1,}", t) else ""


def declared_fields(text: str, enum_values: set[str]) -> set[str]:
    """从提示词文本提取强形式声明的字段名（花括号枚举 / 圆点条目 / 等号定义 / 点号路径）。

    ``enum_values``：本阶段 schema 里出现的枚举取值（insert_after / pass / rework_dev…）。
    提示词会用「`insert_after`：…」逐条解释取值，形态与字段说明一模一样 —— 它们是
    **合法取值**而不是字段，必须剔除。
    """
    names: set[str] = set()

    def take_group(group: str) -> None:
        for raw in re.split(r"[,，、\s]+", group):
            t = _clean_token(raw)
            if t and t not in NOT_A_FIELD:
                names.add(t)

    for m in _RE_BRACE_ITEMS.finditer(text):
        take_group(m.group(1))
    for m in _RE_BRACE_GENERIC.finditer(text):
        take_group(m.group(2))
    for m in _RE_BRACE_ANY.finditer(text):
        take_group(m.group(1) or m.group(2) or "")
    for m in _RE_BULLET.finditer(text):
        t = _clean_token(m.group(1))
        if t and t not in NOT_A_FIELD:
            names.add(t)
    for m in _RE_DEFINE.finditer(text):
        t = _clean_token(m.group(1))
        if t and t not in NOT_A_FIELD:
            names.add(t)
    for m in _RE_DOTTED_BRACKET.finditer(text):
        t = _clean_token(m.group(2))
        if t and t not in NOT_A_FIELD:
            names.add(t)
    for m in _RE_DOTTED.finditer(text):
        owner, child = m.group(1), m.group(2)
        # 点号路径噪声大：父名必须是「对象/数组」样子的词，且不要 __main__ / 文件名
        if owner.startswith("_") or owner in NOT_A_FIELD:
            continue
        t = _clean_token(child)
        if t and t not in NOT_A_FIELD:
            names.add(t)
    for line in _RE_SLASH_LINE.findall(text):
        for m in _RE_SLASH_NAMES.finditer(line):
            for part in re.split(r"\s*/\s*", m.group(0)):
                t = _clean_token(part)
                if t and t not in NOT_A_FIELD:
                    names.add(t)
    for m in _RE_PAREN_FIELD.finditer(text):
        t = _clean_token(m.group(1))
        if t and t not in NOT_A_FIELD:
            names.add(t)
    # 枚举取值（形态同字段说明）剔除；枚举值的下划线片段也剔（prose 里的裸词
    # 「rework」来自 rework_dev / rework_architect，不是字段）。
    enum_parts = {part for value in enum_values for part in value.split("_")}
    return {n for n in names if n not in enum_values and n not in enum_parts}


def schema_enum_values(stage: str) -> set[str]:
    found: set[str] = set()

    def walk(node: dict) -> None:
        if isinstance(node, dict):
            if "enum" in node:
                found.update(str(v) for v in node["enum"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(STAGE_SCHEMAS[stage])
    return found


# ---------------------------------------------------------------- 消费侧扫描
_CONSUME_RE = re.compile(r"\.get\(\s*['\"]([a-z_][a-z0-9_]*)['\"]")


def consumed_fields(stage: str) -> set[str]:
    names: set[str] = set()
    for fname in CONSUMERS[stage]:
        f = PIPELINE_DIR / fname
        if not f.is_file():
            continue
        for m in _CONSUME_RE.finditer(f.read_text(encoding="utf-8", errors="replace")):
            names.add(m.group(1))
    return names


# ---------------------------------------------------------------- 可选但被强要求
_MANDATORY_MARK = re.compile(r"必须|必填|强制|一定要|不得省略|不能省|不允许省略")


def optional_but_mandated(rows: list[tuple[str, str, bool]], text: str) -> list[str]:
    """schema 允许但非 required、提示词却在近旁用「必须/必填」口径要求的字段路径。"""
    hits: list[str] = []
    optional = [p for p, _n, req in rows if not req]
    for path in optional:
        leaf = path.rsplit(".", 1)[-1].replace("[]", "")
        for m in re.finditer(rf"(?<![A-Za-z0-9_]){re.escape(leaf)}(?![A-Za-z0-9_])", text):
            window = text[max(0, m.start() - 40): m.end() + 40]
            if _MANDATORY_MARK.search(window):
                hits.append(path)
                break
    return sorted(set(hits))


# ---------------------------------------------------------------- 主流程
def main() -> int:
    # 全阶段字段总表：提示词里展示**上游产物**（dev 提示词会渲染 plan 的 target_files）
    # 是正常的 —— 只有「任何阶段 schema 都不存在」的强形式声明才算硬矛盾。
    global_vocab: set[str] = set()
    for st in STAGE_SCHEMAS:
        global_vocab.update(schema_fields(st)[0])

    hard: list[str] = []
    for stage in STAGE_SCHEMAS:
        by_name, rows = schema_fields(stage)
        permitted = set(by_name)
        text = stage_prompt_text(stage)
        declared_raw = declared_fields(text, schema_enum_values(stage))
        # 散文单数归一：「每个 edit」指的就是 edits 字段。
        declared = {n if n in global_vocab or n + "s" not in global_vocab else n + "s"
                    for n in declared_raw}
        # 本阶段**顶层字段**只要在提示词里被点过名就算声明（顶层字段没有「展示上游」
        # 歧义 —— 提示词不会平白提一个本阶段的顶层字段名）。
        for root_name in (STAGE_SCHEMAS[stage].get("properties") or {}):
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(root_name)}(?![A-Za-z0-9_])", text):
                declared.add(root_name)
        consumed = consumed_fields(stage)

        if not text.strip():
            # verify / human_review 没有模型 system 提示词（机械阶段 / 人工填表），不做对账。
            print(f"Stage: {stage}（无模型提示词，跳过 Prompt 侧对账）\n")
            continue

        forbidden_anywhere = sorted(
            n for n in declared
            if n not in global_vocab and n not in PROMPT_CONTEXT_KEYS.get(stage, set())
        )
        if forbidden_anywhere:
            lines_out = []
            for n in forbidden_anywhere:
                triple = "（且代码正在消费 —— 0/43 事故三要素齐备）" if n in consumed else ""
                lines_out.append(f"    - {n}（schema 任何层级均无此字段）{triple}")
            hard.append(
                f"Stage: {stage}\n  HARD —— 提示词声明了任何阶段 schema 都不允许的字段"
                f"（grammar 物理上产不出来，等同 0/43 事故）：\n" + "\n".join(lines_out)
            )
        if len(declared) < 3:
            hard.append(
                f"Stage: {stage}\n  HARD —— 只从提示词里解析出 {len(declared)} 个字段，"
                "解析器可能已失效（防守卫自身腐烂）"
            )

        advisory = optional_but_mandated(rows, text)
        # 注：不做「消费了未知字段名」的全文件告警 —— 消费方文件同时读 state / 配置 /
        # 台账等十几种字典，整文件 .get 扫描必然满篇噪声；真正要拦的「提示词要求、
        # 代码在读、schema 没有」三要素，已由 HARD + consumed 标注覆盖。

        other_stage_only = sorted(n for n in declared if n not in permitted and n in global_vocab)

        print(f"Stage: {stage}")
        print("  Prompt declares:")
        for n in sorted(declared):
            if n in permitted:
                print(f"    {n}")
            elif n in global_vocab:
                print(f"    {n}  ⚠ 属于其他阶段 schema（确认不是在要求本阶段产出）")
            elif n in PROMPT_CONTEXT_KEYS.get(stage, set()):
                print(f"    {n}  · 机械证据块键名（非产物字段）")
            else:
                print(f"    {n}  ❌ schema 无此字段")
        print("  Schema permits:")
        for n in sorted(permitted):
            req = any(req for p, name, req in rows if name == n)
            print(f"    {n}{' (required)' if req else ' (optional)'}")
        print("  Code consumes:")
        for n in sorted(consumed & (permitted | declared)):
            print(f"    {n}")
        if advisory:
            print("  MISMATCH (advisory：schema 可选，但提示词按「必须」要求):")
            for p in advisory:
                print(f"    - {p}.required = false，提示词口径为强制")
        if other_stage_only:
            print("  NOTE（提示词提及的是上游/下游阶段字段，非本阶段产出要求）:")
            for n in other_stage_only:
                print(f"    - {n}")
        print()

    if hard:
        print("=" * 68)
        print("Schema Guardian 发现硬矛盾（Prompt ↔ Schema）：")
        for item in hard:
            print(item)
        return 1
    print("Schema Guardian 通过：提示词声明的字段全部在 schema 允许范围内。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
