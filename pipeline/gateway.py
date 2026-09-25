"""全局架构网关（作业层）：规模判定 → 路由 → 子流水线调度 → 越界审计。

它是 ``flow.PRE_NODES[0]``（``global_architecture_analysis``）的执行者，跑在 ``intake`` 之前：

  * 判 **small** → 原样透传，走**原有流水线**（行为零差异；``auto`` 模式下甚至不调模型）；
  * 判 **large** → 把项目拆成模块，按 ``execution_order`` **串行**送进同一套流水线。

为什么是「作业层」而不是新阶段
------------------------------
它刻意**不是** orchestrator 的阶段：不动任何角色逻辑、不进 ``EXEC_ORDER``、
不污染 ``runs/<run_id>/state.json``（作业落 ``runs/_jobs/<job_id>/``，沿用
「``_`` 前缀不进运行列表」的既有约定）。也就是说 —— 把 gateway 关掉，
原有流水线的行为与产物一字不差（``--gateway off`` / ``--show-flow`` 可自证）。

提示词不可修改 ⇒ 三个缺口只能在这一层补
--------------------------------------
提示词与字段模板由使用方给定、要求逐字复用（见 ``ga_prompt.py``），所以：

  1. **规模判定**：模板里没有 scale 字段
     ⇒ :func:`derive_scale` 用**可解释的确定性规则**从 GA 产物推导，而不是让 8K 上下文
     的模型去估「改动多少行」（它也估不准，还会漂）。
  2. **路径级信息**：GA 只划模块边界、不给 target 路径（这正是它的职责边界，符合提示词）
     ⇒ :func:`module_dirs` 从存量目录树做确定性匹配，补出「候选路径 / 禁止越界目录」，
     精确落点仍交给子流水线自己的 retrieve 阶段。
  3. **禁区来源**：提示词把 ``forbidden_paths`` 当**产出**，不给输入模型只能编造
     （正好违它自己的红线 3）⇒ :func:`forbidden_paths` 提供来源（本地配置 + 运行时覆盖），
     产出后再用 :func:`check_grounding` 做存在性接地校验。
  4. **集成校验点悬空**：``integration_checkpoints`` 没有消费方（明确不新增集成评审节点）
     ⇒ 写进作业的 ``report.md`` 供人工核对，**不**新建流水线节点。

异常一律降级
------------
:func:`dispatch` 的每一处失败（模型不可用、契约不符、语义不自洽、结构坏掉）
都退回 **small 直通 + 记 note**，绝不把原有流水线拖下水。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import flow, ga_prompt, local_config, runstore, schemas
from .budget import estimate_tokens, fit_prompt
from .config import MAX_REWORK_ROUNDS, RUNS_DIR, REVIEW_EVERY, STAGE_MODELS

#: 节点名取自流定义真源，避免两处硬编码漂移
GA_NODE: str = flow.PRE_NODES[0]

#: 作业目录前缀。``_`` 开头 ⇒ ``runstore.list_runs`` 不会把它当运行列出来
JOBS_DIRNAME = "_jobs"

MODES = ("auto", "always", "off")
SCALES = ("small", "large")

#: 扫描存量代码时跳过的目录（依赖/构建/缓存/虚拟环境，都不是被改造的对象）
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", "__pycache__", "node_modules",
    ".venv", "venv", "env", "dist", "build", "target", "vendor", ".mypy_cache",
    ".pytest_cache", ".next", ".nuxt", "coverage", "site-packages", "bin", "obj",
}

#: 计入「代码行」的后缀。用途只是给规模预判一个量级感，不做精确统计。
CODE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".java", ".kt", ".go", ".rs",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".m",
    ".mm", ".scala", ".sh", ".ps1", ".sql", ".html", ".css", ".scss", ".less",
}

#: 预判用的「跨模块 / 全局性」信号词。命中越多越可能该拆。
SCOPE_WORDS = [
    "模块", "架构", "全局", "重构", "改造", "迁移", "升级", "插件", "体系",
    "权限", "多端", "前后端", "整体", "统一", "多个", "批量", "调度", "网关",
    "接口协议", "清单", "流程", "编排",
]

#: 单文件超过此大小就不数行（避免为了统计把整个仓库读爆）
_LINE_COUNT_MAX_BYTES = 2 * 1024 * 1024
#: 数行的文件数上限（超出就只数文件个数）
_LINE_COUNT_MAX_FILES = 3000


class GatewayError(RuntimeError):
    pass


# ===================================================================== 禁区来源
def forbidden_paths(override: list[str] | None = None) -> list[str]:
    """全局禁区的**唯一来源**：本地配置 ``guard.forbidden_paths`` + 本次运行覆盖。

    去重保序。提示词的输入槽位需要它 —— 不喂进去，模型只能凭空写路径，
    那就正好撞它自己的红线 3（严禁编造不存在的路径）。
    """
    rows: list[str] = []
    cfg = local_config.guard().get("forbidden_paths")
    if isinstance(cfg, list):
        rows.extend(str(x) for x in cfg)
    if isinstance(override, list):
        rows.extend(str(x) for x in override)
    seen: set[str] = set()
    out: list[str] = []
    for item in rows:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


# ===================================================================== 存量概览
def _top_dirs(repo: Path) -> list[str]:
    try:
        return sorted(
            entry.name
            for entry in repo.iterdir()
            if entry.is_dir() and entry.name not in SKIP_DIRS and not entry.name.startswith(".")
        )
    except OSError:
        return []


def repo_stats(repo: str | Path | None) -> dict[str, Any]:
    """存量仓库的**静态统计**（零模型调用）：文件数 / 目录数 / 代码行 / 顶层目录。

    只用于两件事：规模预判（:func:`prejudge`）与喂给 GA 的概览。
    成本控制：跳过依赖/构建目录；行数只数 ``CODE_SUFFIXES`` 且文件数封顶。
    """
    empty = {"files": 0, "dirs": 0, "code_files": 0, "lines": 0, "top_dirs": []}
    if not repo:
        return empty
    root = Path(repo)
    if not root.exists() or not root.is_dir():
        return empty

    files = dirs = code_files = lines = 0
    counted = 0
    try:
        for current, sub_dirs, names in os.walk(root):
            sub_dirs[:] = [d for d in sub_dirs if d not in SKIP_DIRS and not d.startswith(".")]
            dirs += len(sub_dirs)
            for name in names:
                files += 1
                if Path(name).suffix.lower() not in CODE_SUFFIXES:
                    continue
                code_files += 1
                if counted >= _LINE_COUNT_MAX_FILES:
                    continue
                path = Path(current) / name
                try:
                    if path.stat().st_size > _LINE_COUNT_MAX_BYTES:
                        continue
                    counted += 1
                    with path.open("r", encoding="utf-8", errors="ignore") as handle:
                        lines += sum(1 for _ in handle)
                except OSError:
                    continue
    except OSError:
        pass

    return {
        "files": files,
        "dirs": dirs,
        "code_files": code_files,
        "lines": lines,
        "top_dirs": _top_dirs(root),
    }


def build_overview(
    repo: str | Path | None,
    *,
    max_dirs: int = 30,
    max_files: int = 5,
    max_chars: int = 4000,
) -> str:
    """把存量仓库压成一段**目录树 + 统计**（绝不塞代码正文）。

    为什么刻意这么"抠"：本节点的 ``prompt_token_budget`` 只有 3800 —— 输出字段比
    ``architect_plan`` 多得多，input 必须让位。而且全局架构要做的是**划边界**，
    不是读实现；读实现是子流水线 retrieve 阶段的事。
    """
    if not repo:
        return "（未提供存量仓库：本次为全新项目，无存量模块可复用）"
    root = Path(repo)
    if not root.exists():
        return f"（存量仓库路径不存在：{root}）"

    stats = repo_stats(root)
    lines_out = [
        f"仓库根：{root}",
        f"总量：文件 {stats['files']} 个 / 代码文件 {stats['code_files']} 个 / "
        f"代码行约 {stats['lines']} / 目录 {stats['dirs']} 个",
        "顶层结构：",
    ]
    top = stats["top_dirs"]
    for name in top[:max_dirs]:
        sub = root / name
        sub_stats = repo_stats(sub) if sub.is_dir() else {}
        child_dirs = [
            d.name
            for d in sorted(sub.iterdir())
            if d.is_dir() and d.name not in SKIP_DIRS and not d.name.startswith(".")
        ][:max_files] if sub.is_dir() else []
        sample = [
            f.name
            for f in sorted(sub.iterdir())
            if f.is_file() and f.suffix.lower() in CODE_SUFFIXES
        ][:max_files] if sub.is_dir() else []
        lines_out.append(
            f"  {name}/  文件 {sub_stats.get('files', 0)} / 行约 {sub_stats.get('lines', 0)}"
            + (f"  子目录：{', '.join(child_dirs)}" if child_dirs else "")
            + (f"  样例：{', '.join(sample)}" if sample else "")
        )
    if len(top) > max_dirs:
        lines_out.append(f"  …（另有 {len(top) - max_dirs} 个顶层目录未列出）")
    text = "\n".join(lines_out)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（概览已截断）"
    return text


# ===================================================================== 规模判定
@dataclass
class Scale:
    """规模判定结果。``reasons`` 必须可解释 —— 判错时人工要能看出错在哪一条。"""

    scale: str
    reasons: list[str] = field(default_factory=list)
    source: str = ""  # off / forced / prejudge / gateway / degraded


def prejudge(requirement: str, stats: dict[str, Any] | None = None) -> tuple[bool, list[str]]:
    """**零模型调用**的规模预判：只有疑似大型才值得去调 GA（14B 一次几十秒）。

    这是解决「鸡生蛋」的地方 —— 要判 small/large 才决定是否调 GA，但 GA 本身最贵。
    规则保守（宁可多调一次 GA，也不漏拆）：**强信号**命中即疑似大型，
    否则需要**两条弱信号**。
    """
    stats = stats or {}
    reasons: list[str] = []

    # 强信号：存量本身就大 —— 大仓库上的任何需求都值得先划边界
    top_dirs = list(stats.get("top_dirs") or [])
    big_repo = (
        len(top_dirs) >= 6
        or int(stats.get("files") or 0) >= 300
        or int(stats.get("lines") or 0) >= 20000
    )
    if big_repo:
        reasons.append(
            f"存量规模偏大（顶层目录 {len(top_dirs)} / 文件 {stats.get('files')} / 行 {stats.get('lines')}）"
        )

    # 弱信号：需求文本里的跨模块/全局性字眼
    hits = sorted({w for w in SCOPE_WORDS if w in requirement})
    if hits:
        reasons.append(f"需求含跨模块/全局性表述：{'、'.join(hits[:6])}")

    # 弱信号：需求是「多条目」的（枚举式列出若干功能点）
    items = [line for line in requirement.splitlines() if re.match(r"^\s*(?:\d+[.、)]|[-*・])\s*\S", line)]
    if len(items) >= 3:
        reasons.append(f"需求含 {len(items)} 条枚举式条目")

    # 弱信号：需求本身就长（长需求通常描述的是系统级改造）
    if len(requirement) >= 400:
        reasons.append(f"需求文本较长（{len(requirement)} 字）")

    weak = len(reasons) - (1 if big_repo else 0)
    return (big_repo or weak >= 2), reasons


def derive_scale(ga: dict[str, Any]) -> Scale:
    """从 GA 产物推导规模 —— 确定性规则，每条判据都进 ``reasons``。

    四条判据（任一命中即 large）：
      · 模块数 ≥ 2                 —— "划出了模块边界"本身就说明不是一个整体
      · 跨模块接口契约 ≥ 1         —— 存在跨边界契约，单模块不可能有
      · 执行顺序 ≥ 2               —— 至少两段串行交付
      · risk_level=high 的模块 ≥ 2 —— 多处高风险，值得拆开独立验收/独立回滚

    刻意**不**把 ``uncertainties`` 计入：它是「信息不足」，不是「规模大」。
    拿它升级会让模型一犹豫就触发拆分，噪声很大。
    """
    modules = ga.get("modules") or []
    contracts = ga.get("interface_contracts") or []
    order = ga.get("execution_order") or []
    high = [m for m in modules if str(m.get("risk_level") or "") == "high"]

    reasons: list[str] = []
    if len(modules) >= 2:
        reasons.append(f"拆出 {len(modules)} 个模块")
    if len(contracts) >= 1:
        reasons.append(f"存在 {len(contracts)} 条跨模块接口契约")
    if len(order) >= 2:
        reasons.append(f"执行顺序含 {len(order)} 段串行交付")
    if len(high) >= 2:
        reasons.append(f"{len(high)} 个模块为高风险（值得拆开独立验收/回滚）")

    return Scale("large" if reasons else "small", reasons, "gateway")


def check_self_consistency(ga: dict[str, Any]) -> list[str]:
    """GA 产物的**语义自检** —— JSON Schema 表达不了的那部分。

    现有轻量校验器支持 type/required/enum/items/minItems/maxItems/minimum/maximum，
    但**没有** ``uniqueItems`` / ``$ref`` / ``anyOf``，所以下面这些只能自己查：
    模块 ID 唯一、执行顺序覆盖且无重复、执行顺序真的是依赖的合法拓扑序、接口引用不悬空。
    漏一个模块 = 静默丢需求，因此这类问题比字段缺漏更严重。
    """
    problems: list[str] = []
    modules = ga.get("modules") or []
    ids = [str(m.get("module_id") or "").strip() for m in modules]
    if any(not i for i in ids):
        problems.append("有模块缺少 module_id")
    dup = sorted({i for i in ids if i and ids.count(i) > 1})
    if dup:
        problems.append(f"module_id 重复：{dup}")
    known = {i for i in ids if i}

    order = [str(x or "").strip() for x in (ga.get("execution_order") or [])]
    unknown = sorted(set(order) - known)
    if unknown:
        problems.append(f"execution_order 含未知模块：{unknown}")
    missing = sorted(known - set(order))
    if missing:
        problems.append(f"execution_order 漏掉模块：{missing}")
    if len(order) != len(set(order)):
        problems.append("execution_order 有重复项")

    pos = {mid: idx for idx, mid in enumerate(order)}
    for module in modules:
        mid = str(module.get("module_id") or "").strip()
        for dep_raw in module.get("depends_on") or []:
            dep = str(dep_raw or "").strip()
            if not dep:
                continue
            if dep not in known:
                problems.append(f"{mid} 依赖了不存在的模块 {dep}")
            elif mid in pos and dep in pos and pos[dep] > pos[mid]:
                problems.append(f"执行顺序违例：{dep} 是 {mid} 的前置，却排在它后面")

    for contract in ga.get("interface_contracts") or []:
        iid = str(contract.get("interface_id") or "?")
        for role in ("from_module", "to_module"):
            ref = str(contract.get(role) or "").strip()
            if ref and ref not in known:
                problems.append(f"接口 {iid} 的 {role}={ref} 不在模块清单里")
    return problems


def check_grounding(ga: dict[str, Any], repo: str | Path | None) -> list[str]:
    """接地校验：GA 报的 ``forbidden_paths`` 必须**真实存在**。

    对应它自己的红线 3（严禁编造不存在的存量模块/路径）。只提示、不阻断 ——
    因为这里判"假"的风险是路径写法差异，不值得把整个作业拦下。
    """
    if not repo:
        return []
    root = Path(repo)
    if not root.exists():
        return []
    bad: list[str] = []
    for raw in (ga.get("global_constraints") or {}).get("forbidden_paths") or []:
        text = str(raw or "").strip()
        if not text:
            continue
        if not (root / text.replace("\\", "/")).exists():
            bad.append(text)
    return bad


# ===================================================================== 路径归属
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _tokens(text: str) -> set[str]:
    """把中英混排文本切成可比较的词元：英文词（≥2 字）+ 中文二元组。

    与 ``retrieval.py`` 的中文 n-gram 思路一致 —— 中文没有空格，
    不切二元组就永远匹配不上目录名里的中文。
    """
    out: set[str] = set()
    for word in re.findall(r"[A-Za-z0-9_]{2,}", text or ""):
        out.add(word.lower())
    chinese = "".join(ch for ch in (text or "") if _CJK.match(ch))
    for idx in range(len(chinese) - 1):
        out.add(chinese[idx : idx + 2])
    return out


def module_dirs(module: dict[str, Any], dirs: list[str]) -> list[str]:
    """按模块名/职责/范围内涵在顶层目录里做**确定性**关键词匹配，得到候选路径。

    GA 不给 target 路径（符合它的职责边界），但子流水线需要一个落点起点。
    这里只给"候选"，真正的文件级定位仍由子流水线的 retrieve 阶段完成。
    """
    text = " ".join(
        [
            str(module.get("module_name") or ""),
            str(module.get("responsibility") or ""),
            " ".join(str(x) for x in (module.get("scope_in") or [])),
        ]
    )
    want = _tokens(text)
    if not want:
        return []
    hits: list[str] = []
    for name in dirs:
        if _tokens(name.replace("/", " ").replace("-", " ")) & want:
            hits.append(name)
    return hits


def audit_paths(
    paths: list[str],
    *,
    forbidden: list[str] = (),
    owned: list[str] = (),
    others: list[str] = (),
) -> dict[str, list[str]]:
    """越界审计：子流水线实际改动的路径是否踩了禁区 / 踩到别的模块。

    复用 ``orchestrator._path_stem`` 的归一与前缀匹配口径 —— 与既有的
    ``plan_forbidden_touched`` 判定**同一套算法**，避免出现"两处判定不一致"。
    """
    from .orchestrator import Orchestrator  # 延迟导入：本模块要能被纯查询路径轻量加载

    stem = Orchestrator._path_stem

    def hit(path: str, rules: Any) -> bool:
        target = stem(path)
        if not target:
            return False
        for rule in rules or []:
            base = stem(rule)
            if base and (target == base or target.startswith(base + "/")):
                return True
        return False

    forbidden_hits = sorted({p for p in paths if hit(p, forbidden)})
    return {
        "total": [p for p in paths if str(p or "").strip()],
        "forbidden_touched": forbidden_hits,
        # 与禁区分开计数：踩禁区的路径若恰好也在别人的目录里，不该被算成两条不同的问题
        "cross_module": sorted({p for p in paths if hit(p, others) and not hit(p, forbidden)}),
        # 无主目录：既不属于本模块、也不是别人的地盘、更不是禁区的路径 —— 仅提示
        "outside_scope": sorted(
            {p for p in paths if not hit(p, owned) and not hit(p, others) and not hit(p, forbidden)}
        ),
    }


# ===================================================================== GA 调用
def _forbidden_block(forbidden: list[str]) -> str:
    if not forbidden:
        return (
            "已知禁区（forbidden_paths）：\n"
            "  （未提供禁区清单。若你判定需要设置禁区，只允许引用上面目录树里**真实存在**的路径；"
            "无法确认的写进 uncertainties，不要编造。）"
        )
    return "已知禁区（forbidden_paths，下列路径不得出现在任何模块的改动范围内）：\n" + "\n".join(
        f"  - {item}" for item in forbidden
    )


def build_input(
    requirement: str,
    *,
    repo: str | Path | None = None,
    forbidden: list[str] | None = None,
    stats: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    """拼 GA 的用户消息（含提示词预留的输入槽位标题）。返回 (文本, 是否被裁剪)。"""
    spec = STAGE_MODELS[GA_NODE]
    forbidden = forbidden or []

    req_block = "项目整体需求：\n" + (requirement.strip() or "（空）")
    guard_block = _forbidden_block(forbidden)
    overview_block = "存量代码模块概览：\n" + build_overview(repo)
    if stats:
        overview_block += (
            f"\n（参考量级：顶层目录 {len(stats.get('top_dirs') or [])} 个 / "
            f"文件 {stats.get('files')} 个 / 代码行约 {stats.get('lines')}）"
        )
    tail_block = (
        "约束与要求：\n"
        "  - 输入仅含需求与目录级概览，**没有代码正文**：拿不准的模块内实现、接口细节、"
        "依赖版本一律写进 uncertainties，不要编造。\n"
        "  - 项目类型："
        + ("全新项目（无存量代码，modules 可按新建子系统划分）"
           if not repo else "既有仓库的二次开发（优先复用存量模块，最小侵入）")
        + "\n  - 只做**模块级**拆分与全局契约；不要拆分到任务级，不要写任何代码实现。\n"
        "  - 模块编号从 M-01 起依次递增；execution_order 必须覆盖全部模块且满足 depends_on。\n"
        "  - 严格按系统提示词给出的 JSON 结构输出，不要输出任何额外文字。"
    )

    parts = [req_block, guard_block, overview_block, tail_block]
    # 需求原文与禁区是「必须被模型看到」的硬指令，pin 住不让裁剪（沿用既有惯例）
    text, truncated = fit_prompt(parts, spec.prompt_token_budget, pin=[req_block, guard_block])
    return ga_prompt.INPUT_HEADER + "\n\n" + text, truncated


def analyze(
    client: Any,
    requirement: str,
    *,
    repo: str | Path | None = None,
    forbidden: list[str] | None = None,
    stats: dict[str, Any] | None = None,
    logger: Callable[[str], None] = print,
) -> tuple[dict[str, Any] | None, dict[str, Any], list[str], list[str]]:
    """调 GA 并做双层校验。返回 ``(产物, 调用元数据, 致命问题, 接地提示)``。

    致命问题非空 ⇒ 调用方应降级为 small（不是"报错终止"）。
    """
    spec = STAGE_MODELS[GA_NODE]
    user, truncated = build_input(requirement, repo=repo, forbidden=forbidden, stats=stats)
    logger(
        f"== 入口总闸：调用 {spec.tag}（prompt 预算 {spec.prompt_token_budget} tok，"
        f"实际约 {estimate_tokens(user)} tok{'' if not truncated else '，已裁剪概览'}）"
    )
    try:
        data, meta = client.chat_json(
            spec, ga_prompt.system(), user, schemas.GLOBAL_ARCHITECTURE, attempts=2
        )
    except Exception as exc:  # noqa: BLE001 - 任何异常都降级，不能把流水线拖下水
        return None, {}, [f"GA 调用失败：{type(exc).__name__}: {exc}"], []

    errors = schemas.validate(data, schemas.GLOBAL_ARCHITECTURE)
    if errors:
        return None, meta, [f"契约校验失败：{err}" for err in errors[:6]], []

    problems = check_self_consistency(data)
    if problems:
        return None, meta, [f"语义自检不通过：{p}" for p in problems[:6]], []

    return data, meta, [], check_grounding(data, repo)


# ===================================================================== 子需求渲染
def render_requirement(
    ga: dict[str, Any],
    module: dict[str, Any],
    *,
    job_id: str,
    index: int,
    total: int,
    owned: list[str],
    others: list[str],
    forbidden: list[str],
    done_deps: dict[str, str] | None = None,
) -> str:
    """把 GA 的结构化产物**渲染成子流水线的需求文本**。

    为什么是文本而不是字段：既有流水线的自由输入通道**只有** requirement 文本
    （``run()`` 落 ``requirement.txt`` 后注入各阶段）—— 角色提示词不可改，
    也就不可能给 intake/pm 加结构化入参。所以"对齐后续节点输入"只能靠这里
    做到**信息无丢失**，而不是字面字段对齐。
    """
    constraints = ga.get("global_constraints") or {}
    lines: list[str] = [
        "【本模块在全局架构中的定位】",
        f"- 作业 {job_id}；模块 {module.get('module_id')} ／ 共 {total} 个；执行顺序第 {index} 位",
        f"- 模块名称：{module.get('module_name')}",
        f"- 核心职责与边界：{module.get('responsibility')}",
        f"- 变更风险等级：{module.get('risk_level')}",
        "- 项目整体需求摘要：" + str(ga.get("project_summary") or ""),
        "",
        "【本模块范围】",
        "包含：",
    ]
    lines += [f"- {x}" for x in (module.get("scope_in") or [])] or ["- （未给出）"]
    lines.append("明确不包含：")
    lines += [f"- {x}" for x in (module.get("scope_out") or [])] or ["- （未给出）"]
    lines += [
        "",
        "【硬约束（来自全局架构，不得突破）】",
        "- 本次改动的落点**限定**在下列候选范围内（精确文件由本模块的检索与方案阶段确定）：",
    ]
    lines += [f"  · {x}" for x in owned] or [
        "  · （未匹配到专属目录 ⇒ 本模块不独占任何目录：请在检索结果里按职责定位落点，"
        "并在方案阶段说明依据；不越界到**其它模块已认领**的目录即可）"
    ]
    if others:
        lines.append("- 严禁改动其它模块的目录（越界会被机械审计拦下）：")
        lines += [f"  · {x}" for x in others]
    if forbidden:
        lines.append("- 全局禁区（任何情况下不得触碰）：")
        lines += [f"  · {x}" for x in forbidden]
    if constraints.get("naming_rules"):
        lines.append(f"- 命名规范：{constraints['naming_rules']}")
    if constraints.get("compatibility_rules"):
        lines.append(f"- 兼容性约束：{constraints['compatibility_rules']}")
    if constraints.get("dependency_versions"):
        lines.append(f"- 统一依赖版本：{constraints['dependency_versions']}")

    deps = [str(d) for d in (module.get("depends_on") or [])]
    if deps:
        done_deps = done_deps or {}
        lines += ["", "【上游依赖（应先于本模块完成）】"]
        for dep in deps:
            where = done_deps.get(dep)
            lines.append(f"- {dep}" + (f"（产物目录：{where}）" if where else ""))

    mid = str(module.get("module_id") or "").strip()
    related = [
        c for c in (ga.get("interface_contracts") or [])
        if mid in (str(c.get("from_module") or ""), str(c.get("to_module") or ""))
    ]
    if related:
        lines += ["", "【跨模块接口契约（本模块涉及部分，必须遵守）】"]
        for contract in related:
            lines.append(
                f"- {contract.get('interface_id')} {contract.get('interface_name')}"
                f"（{contract.get('from_module')} → {contract.get('to_module')}）"
            )
            if contract.get("input_format"):
                lines.append(f"    输入：{contract['input_format']}")
            if contract.get("output_format"):
                lines.append(f"    输出：{contract['output_format']}")
            codes = contract.get("error_codes") or []
            if codes:
                lines.append("    错误码：" + "；".join(str(c) for c in codes))

    checkpoints = ga.get("integration_checkpoints") or []
    if checkpoints:
        lines += ["", "【集成校验点（全量集成后会被核对，本模块需为其提供条件）】"]
        for item in checkpoints:
            lines.append(
                f"- {item.get('checkpoint')}：{item.get('verification_method')}"
            )

    notes = ga.get("uncertainties") or []
    if notes:
        lines += ["", "【全局不确定项（已知信息缺口，不要当成事实去补全）】"]
        for item in notes:
            lines.append(
                f"- {item.get('issue')}（假设：{item.get('assumption')}；影响：{item.get('impact')}）"
            )

    return "\n".join(lines)


# ===================================================================== 作业落盘
def _jobs_root(runs_dir: str | Path) -> Path:
    return Path(runs_dir) / JOBS_DIRNAME


def job_dir(runs_dir: str | Path, job_id: str) -> Path:
    return _jobs_root(runs_dir) / job_id


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # Windows 上 os.replace 覆盖已有文件是允许的


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_job(runs_dir: str | Path, data: dict[str, Any]) -> None:
    _write_json(job_dir(runs_dir, str(data["job_id"])) / "job.json", data)


def read_job(runs_dir: str | Path, job_id: str) -> dict[str, Any] | None:
    payload = _read_json(job_dir(runs_dir, job_id) / "job.json")
    return payload if isinstance(payload, dict) else None


def list_jobs(runs_dir: str | Path) -> list[dict[str, Any]]:
    """作业列表（供页面）：按创建时间倒序。作业目录在 ``_jobs/`` 下，不进运行列表。"""
    root = _jobs_root(runs_dir)
    rows: list[dict[str, Any]] = []
    if not root.exists():
        return rows
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        data = read_job(runs_dir, entry.name)
        if not data:
            continue
        modules = data.get("modules") or []
        rows.append(
            {
                "job_id": data.get("job_id") or entry.name,
                "created": data.get("created") or 0,
                "scale": data.get("scale"),
                "mode": data.get("mode"),
                "modules": len(modules),
                "status": job_status(data),
                "reasons": data.get("reasons") or [],
                "pending": [m.get("module_id") for m in modules if m.get("status") in (None, "pending")],
            }
        )
    rows.sort(key=lambda row: row.get("created") or 0, reverse=True)
    return rows


def job_status(data: dict[str, Any]) -> str:
    """作业级状态：blocks 优先 —— 有 blocked 就说明需要人工裁决。"""
    modules = data.get("modules") or []
    if not modules:
        return "empty"
    statuses = [str(m.get("status") or "pending") for m in modules]
    if any(s == "blocked" for s in statuses):
        return "blocked"
    if all(s in ("done", "skipped") for s in statuses):
        return "done" if all(s == "done" for s in statuses) else "partial"
    if any(s == "running" for s in statuses):
        return "running"
    if any(s == "paused" for s in statuses):
        return "paused"
    if any(s == "pending" for s in statuses):
        return "pending"
    return "unknown"


def create_job(
    *,
    requirement: str,
    ga: dict[str, Any],
    runs_dir: str | Path,
    repo: str | Path | None,
    mode: str,
    reasons: list[str],
    forbidden: list[str],
    stats: dict[str, Any],
    grounding: list[str] | None = None,
    notes: list[str] | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """把 GA 产物落成一个**作业**：模块进度表 + 子需求文本 + 概览。

    子需求文本（``modules/NN-<id>.md``）刻意落盘：人工要能一眼看到"到底送进子流水线了什么"，
    否则一旦跑歪，排查只能靠猜。
    """
    modules = ga.get("modules") or []
    order = [str(x or "").strip() for x in (ga.get("execution_order") or [])]
    by_id = {str(m.get("module_id") or "").strip(): m for m in modules}
    ordered = [by_id[mid] for mid in order if mid in by_id]

    dirs = list(stats.get("top_dirs") or [])

    # 目录归属表：一个目录若被某个模块认领，就属于它。
    # **未被任何模块认领**的目录是"无主"，谁都可以改（记提示），不构成越界 ——
    # 否则匹配不上关键词的模块会把整个仓库都当成"别人的地盘"，首个改动就被误判 blocked。
    claims: dict[str, list[str]] = {}
    for module in ordered:
        mid = str(module.get("module_id") or "").strip()
        for name in module_dirs(module, dirs):
            claims.setdefault(name, [])
            if mid not in claims[name]:
                claims[name].append(mid)
    unclaimed = [d for d in dirs if d not in claims]
    ambiguous = {d: owners for d, owners in claims.items() if len(owners) > 1}

    rows: list[dict[str, Any]] = []
    for index, module in enumerate(ordered, start=1):
        mid = str(module.get("module_id") or "").strip()
        owned = sorted(name for name, owners in claims.items() if mid in owners)
        others = sorted(name for name, owners in claims.items() if mid not in owners)
        rows.append(
            {
                "module_id": mid,
                "module_name": module.get("module_name"),
                "responsibility": module.get("responsibility"),
                "risk_level": module.get("risk_level"),
                "depends_on": [str(d) for d in (module.get("depends_on") or [])],
                "scope_in": module.get("scope_in") or [],
                "scope_out": module.get("scope_out") or [],
                "owned_dirs": owned,
                "other_dirs": others,
                "status": "pending",
                "run_id": None,
                "run_dir": None,
                "audit": None,
                "issues": [],
            }
        )

    job_id = job_id or f"job-{time.strftime('%Y%m%d-%H%M%S')}"
    data: dict[str, Any] = {
        "version": 1,
        "job_id": job_id,
        "created": time.time(),
        "mode": mode,
        "scale": "large",
        "reasons": reasons,
        "requirement": requirement,
        "repo": str(repo) if repo else None,
        "forbidden": forbidden,
        "stats": stats,
        "ga": ga,
        "modules": rows,
        "grounding_warnings": grounding or [],
        "notes": notes or [],
        "execution_order": [row["module_id"] for row in rows],
        # 归属解析结果：无主目录（谁都能改，仅提示）与重叠目录（两个模块都认领 → 边界不清）
        "unclaimed_dirs": unclaimed,
        "ambiguous_dirs": ambiguous,
    }
    if unclaimed:
        data["notes"] = list(data["notes"]) + [
            "以下目录未被任何模块认领（不属于越界判定范围，仅作提示）：" + "、".join(unclaimed)
        ]
    if ambiguous:
        data["notes"] = list(data["notes"]) + [
            "以下目录被多个模块同时认领（边界可能重叠，建议人工确认）："
            + "、".join(f"{d}({'/'.join(owners)})" for d, owners in ambiguous.items())
        ]

    base = job_dir(runs_dir, job_id)
    write_job(runs_dir, data)
    _write_json(base / "ga.json", ga)
    done_deps: dict[str, str] = {}
    for index, row in enumerate(rows, start=1):
        text = render_requirement(
            ga,
            by_id[row["module_id"]],
            job_id=job_id,
            index=index,
            total=len(rows),
            owned=row["owned_dirs"],
            others=row["other_dirs"],
            forbidden=forbidden,
            done_deps=done_deps,
        )
        (base / "modules").mkdir(parents=True, exist_ok=True)
        (base / "modules" / f"{index:02d}-{row['module_id']}.md").write_text(text + "\n", encoding="utf-8")
    return data


def refresh_module_requirements(runs_dir: str | Path, data: dict[str, Any]) -> None:
    """按**当前**已完成依赖重写子需求文本（上游产物目录要写进去，方便下游引用）。"""
    ga = data.get("ga") or {}
    by_id = {
        str(m.get("module_id") or "").strip(): m for m in (ga.get("modules") or [])
    }
    done_deps = {
        str(row["module_id"]): str(row.get("run_dir"))
        for row in (data.get("modules") or [])
        if row.get("status") == "done" and row.get("run_dir")
    }
    base = job_dir(runs_dir, str(data["job_id"]))
    for index, row in enumerate(data.get("modules") or [], start=1):
        module = by_id.get(str(row["module_id"]))
        if not module:
            continue
        text = render_requirement(
            ga,
            module,
            job_id=str(data["job_id"]),
            index=index,
            total=len(data.get("modules") or []),
            owned=row.get("owned_dirs") or [],
            others=row.get("other_dirs") or [],
            forbidden=data.get("forbidden") or [],
            done_deps=done_deps,
        )
        (base / "modules").mkdir(parents=True, exist_ok=True)
        (base / "modules" / f"{index:02d}-{row['module_id']}.md").write_text(text + "\n", encoding="utf-8")


def write_report(runs_dir: str | Path, data: dict[str, Any]) -> Path:
    """人读汇总（``report.md``）。``integration_checkpoints`` 的消费方就是这里。

    明确不新增集成评审节点 —— 那些校验点只是**列出来供人工核对**。
    """
    ga = data.get("ga") or {}
    constraints = ga.get("global_constraints") or {}
    lines = [
        f"# 全局架构作业 {data.get('job_id')}",
        "",
        f"- 规模判定：large（{'；'.join(data.get('reasons') or []) or '人工强制'}）",
        f"- 调用模式：{data.get('mode')}",
        f"- 存量仓库：{data.get('repo') or '（无 / 全新项目）'}",
        f"- 模块数：{len(data.get('modules') or [])}",
        "",
        "## 项目摘要",
        str(ga.get("project_summary") or ""),
        "",
        "## 模块与执行情况",
        "| 顺序 | 模块 | 名称 | 风险 | 状态 | 子运行 | 审计 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index, row in enumerate(data.get("modules") or [], start=1):
        audit = row.get("audit") or {}
        flags = []
        if audit.get("forbidden_touched"):
            flags.append(f"踩禁区 {len(audit['forbidden_touched'])} 处")
        if audit.get("cross_module"):
            flags.append(f"越界 {len(audit['cross_module'])} 处")
        lines.append(
            f"| {index} | {row.get('module_id')} | {row.get('module_name')} | "
            f"{row.get('risk_level')} | {row.get('status')} | {row.get('run_id') or '-'} | "
            f"{'；'.join(flags) or '-'} |"
        )
    lines += ["", "## 全局约束"]
    for key, title in (
        ("forbidden_paths", "禁区"),
        ("naming_rules", "命名规范"),
        ("compatibility_rules", "兼容性约束"),
        ("dependency_versions", "依赖版本"),
    ):
        value = constraints.get(key)
        if isinstance(value, list):
            value = "；".join(str(x) for x in value) if value else "（无）"
        lines.append(f"- {title}：{value or '（无）'}")

    lines += ["", "## 集成校验点（全量集成后人工核对，本流水线不新增评审节点）"]
    for item in ga.get("integration_checkpoints") or []:
        lines.append(f"- **{item.get('checkpoint')}**：{item.get('verification_method')}")

    if data.get("grounding_warnings"):
        lines += ["", "## 接地提示（GA 报了但存量里找不到的路径）"]
        lines += [f"- {x}" for x in data["grounding_warnings"]]
    uncertainties = ga.get("uncertainties") or []
    if uncertainties:
        lines += ["", "## 全局不确定项"]
        for item in uncertainties:
            lines.append(
                f"- {item.get('issue')}（假设：{item.get('assumption')}；影响：{item.get('impact')}）"
            )
    if data.get("notes"):
        lines += ["", "## 备注"] + [f"- {x}" for x in data["notes"]]

    path = job_dir(runs_dir, str(data["job_id"])) / "report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ===================================================================== 路由
@dataclass
class Route:
    """一次路由决策的结果。``scale=small`` 时调用方应走**原有**流水线（一字不改）。"""

    scale: str
    reasons: list[str] = field(default_factory=list)
    source: str = ""
    job_id: str | None = None
    ga: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    forbidden: list[str] = field(default_factory=list)

    @property
    def direct(self) -> bool:
        return self.scale == "small"

    def describe(self) -> str:
        head = "直通原有流水线" if self.direct else f"拆分为 {len((self.ga or {}).get('modules') or [])} 个模块"
        return f"{self.scale}（{self.source}）→ {head}"


def dispatch(
    requirement: str,
    *,
    repo: str | Path | None = None,
    runs_dir: str | Path = RUNS_DIR,
    mode: str | None = None,
    scale_override: str | None = None,
    client: Any = None,
    forbidden: list[str] | None = None,
    logger: Callable[[str], None] = print,
) -> Route:
    """入口总闸的**唯一决策入口**：判规模 → 调 GA（必要时）→ 建作业 / 请求直通。

    每一步失败都降级为 small 直通 + 记 note —— 原有流水线的可用性优先级最高。
    """
    guard = local_config.guard()
    mode = (mode if mode is not None else str(guard.get("mode") or "auto")).strip() or "auto"
    if mode not in MODES:
        return Route("small", [f"未知 --gateway 模式 {mode}，按 off 处理"], "degraded")
    forced = str(scale_override if scale_override is not None else (guard.get("scale") or "")).strip()
    if forced and forced not in SCALES:
        return Route("small", [f"未知 scale={forced}，按 off 处理"], "degraded")
    fbd = forbidden_paths(forbidden)

    # 这两条是纯旁路：**连仓库都不扫**（大仓库 os.walk 一遍并不便宜），保证"关掉即零开销"
    if forced == "small":
        return Route("small", ["人工强制 scale=small"], "forced", forbidden=fbd)
    if mode == "off" and forced != "large":
        return Route("small", ["入口总闸关闭（--gateway off）"], "off", forbidden=fbd)

    stats = repo_stats(repo)

    if forced != "large" and mode == "auto":
        suspect, why = prejudge(requirement, stats)
        if not suspect:
            return Route(
                "small",
                why or ["预判未命中大型信号"],
                "prejudge",
                stats=stats,
                forbidden=fbd,
            )
        logger(f"== 入口总闸：预判疑似大型（{'；'.join(why)}），调用全局架构")

    if client is None:
        return Route("small", ["没有可用模型客户端，降级直通"], "degraded", stats=stats, forbidden=fbd)

    ga, meta, problems, grounding = analyze(
        client, requirement, repo=repo, forbidden=fbd, stats=stats, logger=logger
    )
    if ga is None:
        logger("== 入口总闸：全局架构不可用，降级为小项目直通")
        for item in problems:
            logger(f"   - {item}")
        return Route("small", ["GA 不可用，降级直通"], "degraded", notes=problems, stats=stats, forbidden=fbd)

    scale = derive_scale(ga)
    notes = [f"接地提示：{x}" for x in grounding]
    if scale.scale == "small":
        logger(f"== 入口总闸：全局架构判定为单模块（{'；'.join(scale.reasons) or '无拆分理由'}），直通")
        return Route("small", scale.reasons, "gateway", ga=ga, notes=notes, stats=stats, forbidden=fbd)

    job = create_job(
        requirement=requirement,
        ga=ga,
        runs_dir=runs_dir,
        repo=repo,
        mode=mode,
        reasons=scale.reasons,
        forbidden=fbd,
        stats=stats,
        grounding=grounding,
        notes=notes,
    )
    logger(
        f"== 入口总闸：判定大型（{'；'.join(scale.reasons)}），"
        f"拆出 {len(job.get('modules') or [])} 个模块，作业 {job['job_id']}"
    )
    for item in notes:
        logger(f"   - {item}")
    return Route(
        "large",
        scale.reasons,
        "gateway",
        job_id=str(job["job_id"]),
        ga=ga,
        notes=notes,
        stats=stats,
        forbidden=fbd,
    )


# ===================================================================== 调度执行
def _plan_paths(run_dir: Path) -> list[str]:
    """从子运行的 state.json 里取方案声明的改动路径（越界审计的输入）。"""
    state = runstore.read_state(run_dir) or {}
    plan = (state.get("artifacts") or {}).get("plan") or {}
    paths: list[str] = []
    for change in plan.get("changes") or []:
        if isinstance(change, dict) and str(change.get("path") or "").strip():
            paths.append(str(change["path"]).strip())
    return paths


def audit_module_run(
    run_dir: Path,
    *,
    forbidden: list[str],
    owned: list[str],
    others: list[str],
) -> dict[str, Any]:
    """对一个已跑完（或暂停）的子运行做**只读**越界审计。"""
    paths = _plan_paths(Path(run_dir))
    audit = audit_paths(paths, forbidden=forbidden, owned=owned, others=others)
    audit["run_dir"] = str(run_dir)
    return audit


def _skip_dependents(data: dict[str, Any], blocked: str) -> None:
    """把依赖 blocked 模块的后续模块标为 skipped（不阻断整组，但绝不带病开工）。"""
    changed = True
    while changed:
        changed = False
        for row in data.get("modules") or []:
            if row.get("status") != "pending":
                continue
            for dep in row.get("depends_on") or []:
                dep_row = next(
                    (r for r in data.get("modules") or [] if r.get("module_id") == dep), None
                )
                if dep_row and dep_row.get("status") in ("blocked", "skipped"):
                    row["status"] = "skipped"
                    row["issues"] = list(row.get("issues") or []) + [
                        f"前置模块 {dep} 未通过（{dep_row.get('status')}），本模块未开工"
                    ]
                    changed = True
                    break


def run_job(
    job_id: str,
    *,
    runs_dir: str | Path = RUNS_DIR,
    client: Any,
    repo: str | Path | None = None,
    pause_after: list[str] | None = None,
    review_every: int | None = None,
    max_rework: int | None = None,
    project_type: str = "secondary",
    pause_on_open_questions: bool | None = None,
    logger: Callable[[str], None] = print,
) -> dict[str, Any]:
    """按 ``execution_order`` **串行**执行作业的各模块子流水线。

    串行是硬约束（``OLLAMA_MAX_LOADED_MODELS=1`` + 单卡）：并行只会导致反复换模，
    比串行更慢。这一点与既有 ``ensure_exclusive`` 的设计前提一致。

    每个模块都是一次**独立的、原封不动的** ``Orchestrator.run()`` —— 角色逻辑零改动。
    """
    from .orchestrator import Orchestrator

    data = read_job(runs_dir, job_id)
    if not data:
        raise GatewayError(f"找不到作业 {job_id}")
    rows = data.get("modules") or []
    by_id = {str(row.get("module_id")): row for row in rows}
    order = [str(x) for x in (data.get("execution_order") or [])]

    for index, mid in enumerate(order):
        row = by_id.get(mid)
        if not row or row.get("status") in ("done", "blocked", "skipped"):
            continue
        unmet = [
            dep for dep in (row.get("depends_on") or [])
            if (by_id.get(dep) or {}).get("status") != "done"
        ]
        if unmet:
            row["status"] = "skipped"
            row["issues"] = list(row.get("issues") or []) + [f"前置模块未完成：{unmet}"]
            write_job(runs_dir, data)
            continue

        refresh_module_requirements(runs_dir, data)
        base = job_dir(runs_dir, job_id)
        req_path = base / "modules" / f"{index + 1:02d}-{mid}.md"
        requirement = req_path.read_text(encoding="utf-8")

        row["status"] = "running"
        write_job(runs_dir, data)
        logger(f"\n===== 作业 {job_id} · 模块 {mid}（{index + 1}/{len(order)}）开始 =====")

        last = index == len(order) - 1
        orch_kwargs: dict[str, Any] = dict(
            client=client,
            repo=repo,
            runs_dir=runs_dir,
            max_rework=max_rework if max_rework is not None else MAX_REWORK_ROUNDS,
            unload_at_end=False,  # 中途不卸载，整组跑完再卸（省下反复加载）
            log=logger,
            review_every=review_every if review_every is not None else REVIEW_EVERY,
            pause_after=pause_after or [],
            project_type=project_type,
        )
        if pause_on_open_questions is not None:
            orch_kwargs["pause_on_open_questions"] = pause_on_open_questions
        orch = Orchestrator(**orch_kwargs)
        try:
            result = orch.run(requirement, run_id=f"{job_id}-{mid}")
        except Exception as exc:  # noqa: BLE001 - 单个模块失败不该毁掉整组
            row["status"] = "failed"
            row["issues"] = list(row.get("issues") or []) + [
                f"子流水线异常：{type(exc).__name__}: {exc}"
            ]
            write_job(runs_dir, data)
            _skip_dependents(data, mid)
            write_job(runs_dir, data)
            continue

        row["run_id"] = result.run_id
        row["run_dir"] = str(result.run_dir)
        row["paused"] = bool(result.paused)
        row["paused_after"] = result.paused_after
        row["verdict"] = (result.summary or {}).get("verdict")
        row["status"] = "paused" if result.paused else "done"
        audit = audit_module_run(
            result.run_dir,
            forbidden=data.get("forbidden") or [],
            owned=row.get("owned_dirs") or [],
            others=row.get("other_dirs") or [],
        )
        row["audit"] = audit
        if audit["forbidden_touched"] or audit["cross_module"]:
            # 处置力度：该模块 **blocked** + 记 issue，依赖它的模块 skipped；
            # 其余不相关的模块继续跑（不阻断整组）——删掉半成品比带病继续更贵。
            row["status"] = "blocked"
            if audit["forbidden_touched"]:
                row["issues"] = list(row.get("issues") or []) + [
                    "方案改动了全局禁区：" + "、".join(audit["forbidden_touched"][:6])
                ]
            if audit["cross_module"]:
                row["issues"] = list(row.get("issues") or []) + [
                    "方案越界到其它模块目录：" + "、".join(audit["cross_module"][:6])
                ]
            logger(f"== 模块 {mid} 审计不通过，标记 blocked：{row['issues']}")
            _skip_dependents(data, mid)
        write_job(runs_dir, data)

        if last and result.paused:
            logger(f"== 作业 {job_id}：末个模块停在人工闸门 {result.paused_after}，待人工处理")
        if result.paused:
            logger(f"== 作业 {job_id}：模块 {mid} 暂停，后续模块本轮不再推进（先处理人工闸门）")
            break
        if row["status"] != "done":
            continue

    report = write_report(runs_dir, data)
    logger(f"\n== 作业 {job_id} 收尾：状态 {job_status(data)}，报告 {report}")
    return data


def resume_job(
    job_id: str,
    *,
    runs_dir: str | Path = RUNS_DIR,
    client: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """续跑作业：先把已暂停的模块各自续跑完，再往下推进未开始的模块。"""
    from .orchestrator import Orchestrator

    data = read_job(runs_dir, job_id)
    if not data:
        raise GatewayError(f"找不到作业 {job_id}")
    logger = kwargs.pop("logger", print)

    for row in data.get("modules") or []:
        if row.get("status") != "paused" or not row.get("run_dir"):
            continue
        logger(f"\n===== 作业 {job_id} · 续跑模块 {row['module_id']} =====")
        orch = Orchestrator(
            client=client,
            repo=data.get("repo"),
            runs_dir=runs_dir,
            unload_at_end=False,
            log=logger,
            review_every=kwargs.get("review_every") or 1,
            pause_after=kwargs.get("pause_after") or [],
        )
        try:
            result = orch.resume(Path(row["run_dir"]))
        except Exception as exc:  # noqa: BLE001
            row["status"] = "failed"
            row["issues"] = list(row.get("issues") or []) + [
                f"续跑异常：{type(exc).__name__}: {exc}"
            ]
            write_job(runs_dir, data)
            continue
        row["status"] = "paused" if result.paused else "done"
        row["paused_after"] = result.paused_after
        row["verdict"] = (result.summary or {}).get("verdict")
        audit = audit_module_run(
            result.run_dir,
            forbidden=data.get("forbidden") or [],
            owned=row.get("owned_dirs") or [],
            others=row.get("other_dirs") or [],
        )
        row["audit"] = audit
        if audit["forbidden_touched"] or audit["cross_module"]:
            row["status"] = "blocked"
            _skip_dependents(data, str(row["module_id"]))
        write_job(runs_dir, data)
        if row["status"] == "paused":
            return data  # 还是停着，等人工再处理

    payload = {
        k: v for k, v in kwargs.items() if k in ("pause_after", "review_every", "max_rework", "project_type")
    }
    return run_job(job_id, runs_dir=runs_dir, client=client, logger=logger, **payload)


def link_run(
    runs_dir: str | Path,
    run_id: str,
    *,
    job_id: str,
    reasons: list[str] | None = None,
    status: str | None = None,
    modules: int | None = None,
) -> None:
    """在「页面发起的那次运行」与**作业**之间写一条指针（``runs/<run_id>/gateway.json``）。

    页面发起运行时先给一个 ``run_id``，并把子进程日志重定向到
    ``runs/<run_id>/console.log``。若这次需求被判为 large，真正的产物落在**作业目录**下，
    那个 ``run_id`` 目录就只剩一份日志 —— 人工看到的是一条"什么都没有"的运行记录，
    根本不知道东西去哪了。写个指针进去，详情页据此提示「已拆分为作业 job-xxx」。
    """
    base = Path(runs_dir) / run_id
    if not base.exists():
        return
    _write_json(
        base / "gateway.json",
        {
            "job_id": job_id,
            "scale": "large",
            "reasons": reasons or [],
            "status": status,
            "modules": modules,
            "job_dir": str(job_dir(runs_dir, job_id)),
        },
    )


def read_link(runs_dir: str | Path, run_id: str) -> dict[str, Any] | None:
    payload = _read_json(Path(runs_dir) / run_id / "gateway.json")
    return payload if isinstance(payload, dict) else None


# ===================================================================== 页面视图
def job_view(runs_dir: str | Path, job_id: str) -> dict[str, Any] | None:
    """作业视图（供页面）：作业字段 + 子需求文本（人工要能看到到底送进去了什么）。"""
    data = read_job(runs_dir, job_id)
    if not data:
        return None
    base = job_dir(runs_dir, job_id)
    files: list[dict[str, str]] = []
    modules_dir = base / "modules"
    if modules_dir.exists():
        for path in sorted(modules_dir.glob("*.md")):
            files.append({"name": path.name, "text": path.read_text(encoding="utf-8")})
    report = base / "report.md"
    view = dict(data)
    view["status"] = job_status(data)
    view["module_requirements"] = files
    view["report"] = report.read_text(encoding="utf-8") if report.exists() else ""
    view["dir"] = str(base)
    return view
