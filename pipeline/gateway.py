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
from .config import (
    GA_MAX_MODULES,
    MAX_REWORK_ROUNDS,
    MULTI_DELIVERABLE_WORDS,
    REVIEW_EVERY,
    RUNS_DIR,
    STAGE_MODELS,
)

#: 节点名取自流定义真源，避免两处硬编码漂移
GA_NODE: str = flow.PRE_NODES[0]

#: GA 这一步的留痕文件名（写在**发起运行**的目录里，与作业目录里的 ga.json 同名同构）。
#: ``_`` 开头不是必须的：它不匹配 ``runstore.STAGE_FILE_RE``（那要求文件名以数字开头），
#: 因此不会被当成阶段快照、也不影响产物读取。
GA_ARTIFACT_NAME: str = "ga.json"

#: 作业目录前缀。``_`` 开头 ⇒ ``runstore.list_runs`` 不会把它当运行列出来
JOBS_DIRNAME = "_jobs"

#: 作业级**统一验收**产物文件名（存作业目录，不是运行目录 —— 它审的是整组产出）。
JOB_REVIEW_NAME: str = "human_review.json"

#: 作业相位的中文名（日志与页面共用一套说法）
JOB_PHASE_CN: dict[str, str] = {
    "pm": "PM 前置阶段（全模块先出 PM，待人工统一确认）",
    "deliver": "下游串行阶段（各模块从方案起跑到底）",
    "review": "统一验收阶段（整合全部模块产出后审一次）",
    "done": "已交付",
}

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


def prejudge(
    requirement: str,
    stats: dict[str, Any] | None = None,
    project_type: str = "secondary",
) -> tuple[bool, list[str]]:
    """**零模型调用**的规模预判：只有疑似大型才值得去调 GA（14B 一次几十秒）。

    这是解决「鸡生蛋」的地方 —— 要判 small/large 才决定是否调 GA，但 GA 本身最贵。

    两类项目的判据**必须分开**（2026-09-26）：

    * 二次开发（有存量）：规则同前 —— 存量规模是强信号；否则需要两条弱信号
      （跨模块字眼 / 枚举条目 / 需求较长）。
    * **新建项目（无存量）：「需求写得详细」不是规模证据。**
      一份把一个小成品写得很细的需求（例：贪吃蛇，1170 字 + 26 条枚举）会让"枚举条目"
      与"字数"两条弱信号同时命中，于是白调一次 14B 还拆出 8 个模块（真机 20260926-170359），
      而交付物始终只是那几个文件。新建项目只有在需求**明确要求多个可独立交付的子系统**
      （前端/后端/微服务/多端/插件系统…见 ``config.MULTI_DELIVERABLE_WORDS``）时才算疑似大型。
    """
    stats = stats or {}
    reasons: list[str] = []
    is_new = str(project_type or "") == "new"

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

    if is_new and not big_repo:
        # 新建项目：只认「多个可独立交付的子系统」这一条硬线索
        cues = sorted({w for w in MULTI_DELIVERABLE_WORDS if str(w) in requirement.lower() or w in requirement})
        if not cues:
            return False, ["新建项目且需求未指明多个可独立交付的子系统（需求详细程度不作为规模证据）"]
        return True, [f"新建项目且需求要求多个可独立交付的子系统：{'、'.join(cues[:6])}"]

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


def ga_granularity_problems(ga: dict[str, Any]) -> list[str]:
    """GA 拆分粒度的**机械校验**：过度拆分 / 职责重叠（判定为"伪拆分"则降级 small）。

    为什么要有它：GA 提示词对模块数量没有任何约束，而 ``derive_scale`` 的判据是
    「模块数 ≥ 2 就 large」。真机 20260926-170359 把一个贪吃蛇拆成 8 个模块
    （核心逻辑/渲染/数据存储/输入/配置/测试框架/扩展接口/性能优化），7 个都直接依赖 M-01，
    8 条完整流水线接力改同一批文件 —— 成本 ≈ ×8，成品还是那几个文件。

    三条判据（前两条判负，第三条只提示）：
      · 模块数 > ``config.GA_MAX_MODULES``（默认 5）—— 超过上限即过度拆分；
      · 两个模块的 ``scope_in`` 高度重叠（Jaccard ≥ 0.6）—— 职责重叠，不是两个交付边界；
      · 「1 主干 + N 附件」（唯一无依赖模块被其余模块全部直接依赖，且附件之间无依赖）
        —— 形状可疑，但可能真是"公共库 + 若干接入"，故只提示不判负。
    """
    modules = [m for m in (ga.get("modules") or []) if isinstance(m, dict)]
    order = [str(x or "").strip() for x in (ga.get("execution_order") or [])]
    problems: list[str] = []
    if len(modules) > GA_MAX_MODULES:
        problems.append(
            f"拆出 {len(modules)} 个模块，超过粒度上限 {GA_MAX_MODULES}（疑似把功能点/横切关注点"
            "当成了模块）"
        )
    keys: dict[str, set[str]] = {}
    for module in modules:
        mid = str(module.get("module_id") or "").strip()
        keys[mid] = _scope_keys(module)
    ids = [mid for mid in keys if mid]
    for idx, left in enumerate(ids):
        for right in ids[idx + 1 :]:
            a, b = keys[left], keys[right]
            if not a or not b:
                continue
            overlap = len(a & b) / len(a | b)
            if overlap >= 0.6:
                problems.append(
                    f"{left} 与 {right} 的 scope_in 重叠度 {overlap:.0%}（不是两个交付边界）"
                )
    return problems


def _scope_keys(module: dict[str, Any]) -> set[str]:
    """把模块的 ``scope_in`` 归一成关键词集合（用于重叠判定）。

    **只看 scope_in，不看 responsibility**：后者常常是套话（"职责"、""核心逻辑实现""），
    拿它判重叠会误伤 —— 两个模块的职责描述措辞一样不代表它们真的改同一处。
    scope_in 为空则返回空集（无从判定就不判，宁可漏判也不误判）。

    只做确定性归一（按 2 字滑窗切 CJK、按词切 ASCII）：措辞不同不算重叠。
    """
    text = " ".join(str(x) for x in (module.get("scope_in") or []))
    out: set[str] = set()
    for word in re.findall(r"[A-Za-z0-9_]{3,}", text):
        out.add(word.lower())
    cjk = "".join(ch for ch in text if _CJK.match(ch))
    for idx in range(len(cjk) - 1):
        out.add(cjk[idx : idx + 2])
    return out


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
    # 规则侧走 `_path_rule_stem`：只有「它真是一条路径」才参与机械判定 ——
    # 自由文本规则（"game_logic.py中tkinter导入"）按 stem 折叠会变成整个文件禁区，
    # 真机上让模块自己的交付物成了禁改路径（见 orchestrator._path_rule_stem）。
    rule_stem = Orchestrator._path_rule_stem

    def hit(path: str, rules: Any) -> bool:
        target = stem(path)
        if not target:
            return False
        for rule in rules or []:
            base = rule_stem(rule)
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


def _save_ga_artifacts(
    run_dir: str | Path | None,
    *,
    ga: dict[str, Any] | None,
    problems: list[str],
    stats: dict[str, Any],
    scale: str | None,
) -> None:
    """把 GA 这一步的**原始产物与结论**落到发起运行的目录里（没给目录就跳过）。

    为什么必须留痕：GA 跑在编排器之前，它的产物不进 ``traces.jsonl`` / ``llm-calls.jsonl``。
    真机踩过 —— 降级 small 时只剩日志里那几行「语义自检不通过：接口 IC-01 的 to_module=M-02
    不在模块清单里」，原始 ``modules`` / ``interface_contracts`` 一份都没存，事后完全无法
    复盘"模型到底拆成了什么样、为什么不合自检"。
    """
    if not run_dir:
        return
    base = Path(run_dir)
    try:
        base.mkdir(parents=True, exist_ok=True)
        _write_json(
            base / GA_ARTIFACT_NAME,
            {
                # status 让页面能区分「正在判定」与「已出结论」（见 _mark_ga_running）
                "status": "done",
                # None = 没走到规模判定（GA 不可用 / 粒度不通过）
                "scale": scale,
                "problems": list(problems or []),
                "stats": stats,
                "ga": ga,
            },
        )
    except OSError:
        return  # 留痕失败不该拖垮流水线


def _mark_ga_running(run_dir: str | Path | None, stats: dict[str, Any]) -> None:
    """在调用 GA 模型**之前**写一份「正在判定」的轻量痕迹。

    为什么需要：GA 跑在流水线主循环之前，这段时间既没有 ``state.json``、日志也可能还没
    刷出来（尤其没设 ``PYTHONUNBUFFERED`` 时）。页面于是只能显示一条空运行，人看不出
    它是在判定还是死了。这份痕迹只回答一个问题：**现在到哪一步了**。
    """
    if not run_dir:
        return
    base = Path(run_dir)
    try:
        base.mkdir(parents=True, exist_ok=True)
        _write_json(
            base / GA_ARTIFACT_NAME,
            {
                "status": "running",
                "stage": "gateway",
                "started_at": _now(),
                "stats": stats,
            },
        )
    except OSError:
        return  # 留痕失败不该拖垮流水线


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


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
    """作业级状态：blocks 优先 —— 有 blocked 就说明需要人工裁决。

    两个**待人工**的相位要单独报出来（页面据此显示「待你确认 PM」/「待你统一验收」）：
      · ``awaiting_pm``     —— 有模块停在 pm 上（PM 前置阶段产出了待裁决项）；
      · ``awaiting_review`` —— 所有模块都跑完了，等作业级统一验收。
    """
    modules = data.get("modules") or []
    if not modules:
        return "empty"
    phase = str(data.get("phase") or "")
    # 相位是**权威**：人工已经通过统一验收（phase=done）时，模块行的 `paused` 只是
    # 打回/复核留下的历史痕迹，不该把整组说成"已暂停"
    if phase == "done":
        return "done"
    statuses = [str(m.get("status") or "pending") for m in modules]
    if any(s == "blocked" for s in statuses):
        return "blocked"
    if any(s == "running" for s in statuses):
        return "running"
    if any(s == "pm_paused" for s in statuses):
        return "awaiting_pm"
    if any(s == "paused" for s in statuses):
        return "paused"
    if phase == "review":
        return "awaiting_review"
    if all(s in ("done", "skipped") for s in statuses):
        return "done" if all(s == "done" for s in statuses) else "partial"
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
    project_type: str = "secondary",
    review_every: int | None = None,
    max_rework: int | None = None,
    pause_after: list[str] | None = None,
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
    for _index, module in enumerate(ordered, start=1):
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
        # 相位：新建的作业一律从 **PM 前置阶段** 开始（全模块先出 PM → 人工统一确认 → 下游）。
        "phase": "pm",
        # 运行参数必须**落盘**：续跑作业走的是另一条进程链（`--resume-job` 只带 job_id），
        # 没存就只能退回代码默认值 —— 新建项目的模块会被用「二次开发」提示词重跑，
        # max_rework 也会掉回默认（真机 job-20260926-154657：project_type 丢失、5→2）。
        "project_type": project_type if project_type in ("new", "secondary") else "secondary",
        "review_every": review_every,
        "max_rework": max_rework,
        "pause_after": list(pause_after or []),
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
    project_type: str | None = None,
    review_every: int | None = None,
    max_rework: int | None = None,
    pause_after: list[str] | None = None,
    #: 发起运行（页面那次 run）的目录：GA 的原始产物与结论写进这里留痕。
    #: 不传则不落盘（纯 CLI 场景没有 run 目录）。
    run_dir: str | Path | None = None,
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
        suspect, why = prejudge(requirement, stats, project_type or "secondary")
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

    # 调模型**之前**先落一份「正在判定」的痕迹：GA 一次几十秒到几分钟，而这段时间
    # 没有任何 state.json（GA 跑在流水线主循环之前）—— 页面上只剩一条 0 字节日志的空运行，
    # 人无法区分「在判定」与「卡死」（真机 20260926-154413 就卡在这个认知盲区里）。
    _mark_ga_running(run_dir, stats)
    ga, meta, problems, grounding = analyze(
        client, requirement, repo=repo, forbidden=fbd, stats=stats, logger=logger
    )
    if ga is None:
        logger("== 入口总闸：全局架构不可用，降级为小项目直通")
        for item in problems:
            logger(f"   - {item}")
        _save_ga_artifacts(run_dir, ga=None, problems=problems, stats=stats, scale=None)
        return Route("small", ["GA 不可用，降级直通"], "degraded", notes=problems, stats=stats, forbidden=fbd)

    # 粒度机械校验：GA 自己对模块数量没有任何约束，而「模块数 ≥ 2 就 large」——
    # 不拦一下就会出现"一个贪吃蛇拆 8 个模块"（真机 20260926-170359）。
    granularity = ga_granularity_problems(ga)
    if granularity:
        logger("== 入口总闸：GA 拆分粒度不通过，降级为小项目直通")
        for item in granularity:
            logger(f"   - {item}")
        _save_ga_artifacts(run_dir, ga=ga, problems=granularity, stats=stats, scale=None)
        return Route(
            "small",
            ["GA 拆分粒度不通过（伪拆分），按单模块跑"],
            "degraded",
            ga=ga,
            notes=granularity,
            stats=stats,
            forbidden=fbd,
        )

    scale = derive_scale(ga)
    notes = [f"接地提示：{x}" for x in grounding]
    if scale.scale == "small":
        logger(f"== 入口总闸：全局架构判定为单模块（{'；'.join(scale.reasons) or '无拆分理由'}），直通")
        _save_ga_artifacts(run_dir, ga=ga, problems=[], stats=stats, scale="small")
        return Route("small", scale.reasons, "gateway", ga=ga, notes=notes, stats=stats, forbidden=fbd)

    _save_ga_artifacts(run_dir, ga=ga, problems=[], stats=stats, scale="large")

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
        # 运行参数一并落盘：作业续跑只带 job_id，没存就只能用代码默认值
        project_type=project_type or "secondary",
        review_every=review_every,
        max_rework=max_rework,
        pause_after=pause_after,
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


def _run_options(
    data: dict[str, Any],
    *,
    repo: Any = None,
    pause_after: Any = None,
    review_every: Any = None,
    max_rework: Any = None,
    project_type: Any = None,
) -> dict[str, Any]:
    """合并运行参数：**显式传入 > 作业落盘值 > 代码默认**。

    为什么必须有它：续跑作业是**另一条进程链**（``--resume-job`` 只带 job_id），
    cli 的 argparse 默认值（``--project-type secondary``、review_every / max_rework 的默认）
    会冒充"用户显式选择"，把作业原本的配置静默覆盖。真机 job-20260926-154657 就是这样
    把「新建项目」用**二次开发**提示词重跑、max_rework 从 5 掉到 2 的 ——
    于是开发满口「存量代码评估 / 禁改路径」并因"禁区"拒绝实现，反复返工到触顶。
    所以这些参数一律默认 ``None``：「没传」与「传了默认值」必须能区分。
    """
    def pick(value: Any, key: str, fallback: Any) -> Any:
        # 注意这里是 `is not None`（空列表也算显式）：`--no-pause` 传的就是 `[]`，
        # 语义是「清空人工闸门」，不能被当成「没传」而退回落盘值。
        if value is not None and value != "":
            return value
        stored = data.get(key)
        return fallback if stored in (None, "", []) else stored

    return {
        "repo": pick(repo, "repo", None),
        "pause_after": list(pick(pause_after, "pause_after", []) or []),
        "review_every": pick(review_every, "review_every", REVIEW_EVERY),
        "max_rework": pick(max_rework, "max_rework", MAX_REWORK_ROUNDS),
        "project_type": pick(project_type, "project_type", "secondary"),
    }


def _archive_previous_run(
    run_dir: Path, logger: Callable[[str], None], run_id: str
) -> int:
    """重跑一个模块前，把上一代残留的阶段快照归档到 ``superseded/``。

    为什么必须做：作业被中断后 ``--resume-job`` 会用**同一个 run_id** 重跑该模块，
    而 ``Orchestrator.run()`` 对已存在的运行目录只做 ``mkdir(exist_ok=True)``、不归档。
    于是两代产物混在一个目录里 —— 真机 job-20260926-154657-M-01 就有 seq=3 两份
    （上一代 architect_plan / 这一代 architect_assess）、seq=4 两份（dev / architect_plan），
    阶段列表与检查点时间线重复且乱序，看「最新产物」极易读错。
    归档口径复用 runstore 既有的那个（人工 ``--from`` 打回走的也是它）。
    """
    if not run_dir.exists():
        return 0
    moved = runstore.archive_stages(run_dir, list(runstore.FLOW_ORDER))
    if moved:
        logger(f"== 模块 {run_id}：归档上一代阶段快照 {len(moved)} 个（superseded/）")
    return len(moved)


def _pm_scope_of(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """从模块运行的 state.json 里取 (PM 产物, 人工裁决记录)。"""
    state = runstore.read_state(run_dir) or {}
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    scope = artifacts.get("scope") if isinstance(artifacts, dict) else None
    if not isinstance(scope, dict):
        scope = state.get("scope") if isinstance(state.get("scope"), dict) else {}
    decisions = state.get("pm_decisions") or []
    return scope, list(decisions)


def job_pm_blockers(runs_dir: str | Path, data: dict[str, Any]) -> dict[str, list[str]]:
    """**PM 统一人工环节**的判据：{module_id: [还没解决的人话条目]}，空 = 可以进下游。

    与模块内那套是同一份实现（``orchestrator.pm_unresolved_items``）：读该模块的 PM 产物
    与人工裁决，列出「未裁决的 open_questions」与「未明确的 unknowns/clarifying_questions」。
    作业层据此做到**一次人工处理全部模块**：只要还有一个模块没确认完，就不放行进下游。
    """
    from .orchestrator import pm_unresolved_items

    out: dict[str, list[str]] = {}
    for row in data.get("modules") or []:
        mid = str(row.get("module_id") or "")
        status = str(row.get("status") or "")
        if status == "done":
            continue  # 已交付的模块不必再确认
        run_dir = row.get("run_dir")
        if not run_dir or not Path(str(run_dir)).exists():
            out[mid] = ["该模块还没跑过 PM 前置阶段（没有产物）"]
            continue
        scope, decisions = _pm_scope_of(Path(str(run_dir)))
        left = pm_unresolved_items(scope, decisions)
        items = [f"未裁决：{q}" for q in left["pending"]] + [f"未明确：{v}" for v in left["vague"]]
        if items:
            out[mid] = items
    return out


def job_pm_blockers_text(blockers: dict[str, list[str]]) -> str:
    """把 ``job_pm_blockers`` 的结果拼成一句给人看的话（报错/日志共用）。"""
    parts = [f"{mid}（{len(items)} 项）" for mid, items in blockers.items()]
    return "还有模块的 PM 待确认项没解决：" + "、".join(parts) + " —— 请先在作业页逐模块确认完再继续"


#: 交付就绪度的四道门（每门 0–2 分）。见 `job_readiness`。
READINESS_GATES: tuple[tuple[str, str], ...] = (
    ("functional", "功能落地"),
    ("verified", "机械验证"),
    ("redline", "工程红线"),
    ("delivered", "交付成型"),
)
#: 可放行 / 带例外 / 禁止放行。满分为 门数×2。
READINESS_SHIP = 2 * len(READINESS_GATES)
READINESS_WITH_EXCEPTIONS = READINESS_SHIP - 1


def job_readiness(runs_dir: str | Path, data: dict[str, Any]) -> dict[str, Any]:
    """作业级**交付就绪度**（4 门 × 0–2 分，全部机械算出，不含模型自评）。

    出处与裁剪：fullstack-dev `release-checklist.md` 的「6 Gate + Readiness Score」——
    每门 0–2 分、低于阈值**禁止放行**、中间档必须写明例外与责任人。
    这里裁到 4 门，因为另外两门（发布执行 / 上线后观测）本项目不做发布动作，
    硬凑只会得到一排"不适用"的 0 分，反而让总分失真。

    为什么要量化：人工统一验收现在拿到的只有各模块 status/verdict —— 那回答的是
    "跑完了没有"，不是"能不能收"。换成可比的分数后，人一眼能看出该不该放行，
    被追问时也能说出**是哪一门扣的分、扣在哪**（每门都带 evidence）。

    口径一律取**最弱一环**（min）：一个模块 verify 失败，整组就不能说"验证通过"。
    宁可保守 —— 这一层判松的代价是整组废品被收下。
    """
    runs_dir = Path(runs_dir)
    rows = list(data.get("modules") or [])
    gates: dict[str, dict[str, Any]] = {key: {"score": 2, "evidence": []} for key, _ in READINESS_GATES}
    unverified: list[str] = []

    def downgrade(key: str, score: int, note: str) -> None:
        gate = gates[key]
        gate["score"] = min(int(gate["score"]), score)
        gate["evidence"].append(note)

    if not rows:
        downgrade("functional", 0, "作业里没有任何模块")
    for row in rows:
        mid = str(row.get("module_id") or "?")
        status = str(row.get("status") or "")
        verdict = str(row.get("verdict") or "")
        run_dir = Path(row.get("run_dir") or (runs_dir / f"{data.get('job_id')}-{mid}"))
        # 产物层：read_state 给的是整份快照，产物在 artifacts 里（读错层会**静默为空**）
        state = runstore.artifact_view(runstore.read_state(run_dir))
        verify = state.get("verify_report") or {}
        delivery = state.get("delivery") or {}

        if status == "done" and verdict == "pass":
            pass
        elif status in ("running", "pending", "paused"):
            downgrade("functional", 1, f"{mid}：状态 {status}（还没跑完/在等人工）")
        else:
            downgrade("functional", 0, f"{mid}：状态 {status}，verdict {verdict or '空'}")

        v_verdict = str(verify.get("verdict") or "")
        if v_verdict == "pass":
            pass
        elif v_verdict in ("skipped", ""):
            downgrade("verified", 1, f"{mid}：运行验证未执行（{verify.get('summary') or '无结果'}）")
        else:
            problems = [str(p) for p in (verify.get("problems") or [])][:2]
            downgrade("verified", 0, f"{mid}：运行验证 {v_verdict} —— {'；'.join(problems) or '见 report'}")
        for item in verify.get("unverified") or []:
            unverified.append(f"{mid}：{item}")

        findings = state.get("rule_findings") or []
        blk = [f for f in findings if f.get("severity") == "blocker" and not f.get("note")]
        wrn = [f for f in findings if f.get("severity") == "warn" and not f.get("note")]
        if blk:
            downgrade("redline", 0, f"{mid}：{len(blk)} 条红线阻断（例：{blk[0].get('title')} @ {blk[0].get('path')}）")
        elif wrn:
            downgrade("redline", 1, f"{mid}：{len(wrn)} 条提示级红线（例：{wrn[0].get('title')}）")

        if delivery.get("delivered") and not delivery.get("wrote_nothing"):
            pass
        elif delivery.get("delivered"):
            downgrade("delivered", 1, f"{mid}：交付记录存在但没写出任何文件")
        else:
            downgrade("delivered", 0, f"{mid}：未交付（{delivery.get('reason') or delivery.get('error') or '无交付记录'}）")
        if state.get("duplicate_stage_seqs"):
            downgrade("redline", 1, f"{mid}：同一 seq 多份产物（两代混存）")

    total = sum(g["score"] for g in gates.values())
    if total >= READINESS_SHIP:
        decision = "可放行"
    elif total >= READINESS_WITH_EXCEPTIONS:
        decision = "带例外放行：必须在验收备注里写明例外项与责任人"
    else:
        decision = "禁止放行：先返工（按各门 evidence 指到的问题逐条处理）"
    return {
        "score": total,
        "max": READINESS_SHIP,
        "decision": decision,
        "gates": [
            {"key": key, "label": label, **gates[key]} for key, label in READINESS_GATES
        ],
        # 未验证项**原样带出来**：验收最有价值的不是"哪里绿了"，而是"哪里根本没验"。
        "unverified": unverified,
    }


def job_evidence_md(runs_dir: str | Path, data: dict[str, Any]) -> str:
    """把各模块的**交付证据**合并成一份作业级材料（人审只看这一份）。

    为什么要合并：逐模块各审一次早就被判定没有意义（真正要人看的是整组交付物），
    而"整组"的证据此前只有各模块的 status/verdict —— 那回答的是"跑完了没有"。
    验收标准、用例、真实执行结果、红线、未验证项合到一起，人才有判断"能不能收"的材料。

    合并口径：验收标准按模块加 `[M-0x]` 前缀（合并后必须还看得出它属于谁），
    其余（用例/命令/未验证项）直接汇总。匹配仍是启发式，标注沿用单模块那份说明。
    """
    from . import evidence as evidence_mod  # 延迟导入：本模块要能被纯查询路径轻量加载

    runs_dir = Path(runs_dir)
    criteria: list[str] = []
    requirements: list[Any] = []
    cases: list[Any] = []
    commands: list[Any] = []
    unverified: list[str] = []
    no_power: list[str] = []
    coverage: list[str] = []
    findings: list[dict[str, Any]] = []
    for row in data.get("modules") or []:
        mid = str(row.get("module_id") or "?")
        run_dir = Path(row.get("run_dir") or (runs_dir / f"{data.get('job_id')}-{mid}"))
        state = runstore.artifact_view(runstore.read_state(run_dir))
        scope = state.get("scope") or {}
        criteria += [f"[{mid}] {c}" for c in (scope.get("acceptance_criteria") or [])]
        requirements += list(scope.get("functional_requirements") or [])
        cases += list((state.get("test_report") or {}).get("cases") or [])
        verify = state.get("verify_report") or {}
        commands += list(verify.get("commands") or [])
        unverified += [f"{mid}：{x}" for x in (verify.get("unverified") or [])]
        no_power += [
            f"{mid}：{x}" for x in ((verify.get("negative_control") or {}).get("no_power") or [])
        ]
        pct = (verify.get("coverage") or {}).get("percent")
        if pct is not None:
            coverage.append(f"{mid} {float(pct):g}%")
        findings += [f for f in (state.get("rule_findings") or []) if isinstance(f, dict)]
    ev = evidence_mod.delivery_evidence(
        {"acceptance_criteria": criteria, "functional_requirements": requirements},
        {"cases": cases},
        {
            "commands": commands,
            "unverified": unverified,
            "negative_control": {"no_power": no_power},
        },
        findings,
    )
    body = evidence_mod.render_markdown(ev)
    if coverage:
        body = "### 各模块实测覆盖率\n- " + "；".join(coverage) + "\n\n" + body
    return body


def _open_job_review(base: Path, data: dict[str, Any], logger: Callable[[str], None]) -> None:
    """全部模块跑完 → 打开**作业级统一验收**（材料是所有模块产出的整合）。"""
    # 模块运行落在 runs/<job_id>-<mid>/（不是 runs/_jobs/ 下），所以先按已有模块行反推，
    # 反推不到再按目录结构上跳两级（base = runs/_jobs/<job_id>）。
    runs_root = base.parent.parent
    for m in data.get("modules") or []:
        if m.get("run_dir"):
            runs_root = Path(str(m["run_dir"])).parent
            break
    readiness = job_readiness(runs_root, data)
    if not (base / JOB_REVIEW_NAME).exists():
        _write_json(
            base / JOB_REVIEW_NAME,
            {
                "verdict": "",
                "notes": "",
                "reviewer": "",
                "_placeholder": True,
                "_instruction": (
                    "作业统一验收：这是**所有模块跑完之后**的一次性人工验收（不是逐模块各审一次）。"
                    "请对照模块清单、各模块 verdict 与集成校验点核对**整体**交付；"
                    "approve = 整组交付完成；reject = 把 notes 里的问题分发到各模块开发阶段重跑。"
                ),
                "modules": [
                    {
                        "module_id": m.get("module_id"),
                        "status": m.get("status"),
                        "verdict": m.get("verdict"),
                    }
                    for m in (data.get("modules") or [])
                ],
                # 交付就绪度（机械算出的 4 门打分）：回答"能不能收"，与"跑完了没有"是两件事
                "readiness": readiness,
                # 交付证据表：验收标准 ↔ 用例 ↔ 真实执行 ↔ 红线/未验证项（见 evidence.py）
                "evidence_md": job_evidence_md(runs_root, data),
            },
        )
        logger(
            f"== 作业 {data.get('job_id')}：全部模块已跑完，等待**统一验收**"
            "（一次，材料为全组产出；见作业页的验收卡片）"
        )
        logger(
            f"   交付就绪度 {readiness['score']}/{readiness['max']} → {readiness['decision']}"
        )
        for gate in readiness["gates"]:
            if gate["score"] < 2:
                logger(f"     - {gate['label']} {gate['score']}/2："
                       + "；".join(str(x) for x in gate["evidence"][:2]))
        if readiness["unverified"]:
            logger(f"     - 未验证项 {len(readiness['unverified'])} 条（pass ≠ 该验的都验了）")
    data["phase"] = "review"


def finish_job_review(
    runs_dir: str | Path,
    job_id: str,
    *,
    verdict: str,
    notes: str = "",
    reviewer: str = "",
) -> dict[str, Any]:
    """人工提交**作业级统一验收**。

    · ``approve`` ⇒ 整组交付完成（phase=done）；
    · ``reject``  ⇒ 把意见分发到**各模块的开发阶段**（phase 回到 deliver，逐模块
      ``--from dev`` + 人工意见重跑）—— 统一验收打回是"整组返工"，不是只改某一个模块。
    """
    data = read_job(runs_dir, job_id)
    if not data:
        raise GatewayError(f"找不到作业 {job_id}")
    base = job_dir(runs_dir, job_id)
    art = _read_json(base / JOB_REVIEW_NAME)
    if not isinstance(art, dict):
        raise GatewayError("该作业还没有统一验收记录（模块还没全部跑完）")
    if verdict not in ("approve", "reject"):
        raise GatewayError(f"未知 verdict：{verdict}")
    art["verdict"] = verdict
    art["notes"] = notes
    art["reviewer"] = reviewer
    art["_placeholder"] = False
    _write_json(base / JOB_REVIEW_NAME, art)
    if verdict == "approve":
        data["phase"] = "done"
    else:
        data["phase"] = "deliver"
        for row in data.get("modules") or []:
            if not row.get("run_dir"):
                continue
            row["status"] = "paused"           # 让 deliver 阶段重新捡起来
            row["rework_from"] = "dev"         # 从开发阶段重跑（走既有的打回语义）
            row["rework_feedback"] = f"[统一验收打回] {notes or '（未填写具体问题）'}"
            row["issues"] = list(row.get("issues") or []) + [
                f"统一验收打回：{notes or '（未填写具体问题）'}"
            ]
    write_job(runs_dir, data)
    return data


def run_job(
    job_id: str,
    *,
    runs_dir: str | Path = RUNS_DIR,
    client: Any,
    repo: str | Path | None = None,
    pause_after: list[str] | None = None,
    review_every: int | None = None,
    max_rework: int | None = None,
    project_type: str | None = None,
    pause_on_open_questions: bool | None = None,
    phase: str | None = None,
    logger: Callable[[str], None] = print,
) -> dict[str, Any]:
    """作业执行 —— **分三阶段**（2026-09-26 起）。串行是硬约束（单卡 + 单驻留模型）。

    ``job.json.phase``：

    * ``pm``（先跑）——**每个模块各自跑到 pm 结束就停**，把待裁决项一次性摆出来；
      人工环节只在 PM 集中处理**一次**，而不是每个模块各停一次。
    * ``deliver`` —— 人工把**全部**模块的待确认项解决完之后，各模块从下游继续串行跑完；
      模块级人工审核**延后**（``defer_human_review``），不在每个模块各停一次。
    * ``review`` —— 全部跑完后**统一验收一次**：材料是所有模块产出的**整合**
      （见 ``_open_job_review`` 与作业页的验收卡片），不是逐模块各审一遍。

    每个模块仍是一次独立的、原封不动的 ``Orchestrator`` 流水线 —— 角色逻辑零改动。
    """
    from .orchestrator import Orchestrator

    data = read_job(runs_dir, job_id)
    if not data:
        raise GatewayError(f"找不到作业 {job_id}")
    opts = _run_options(
        data, repo=repo, pause_after=pause_after, review_every=review_every,
        max_rework=max_rework, project_type=project_type,
    )
    repo = opts["repo"]
    review_every = opts["review_every"]
    max_rework = opts["max_rework"]
    project_type = opts["project_type"]
    rows = data.get("modules") or []
    by_id = {str(row.get("module_id")): row for row in rows}
    order = [str(x) for x in (data.get("execution_order") or [])]

    phase = str(phase or data.get("phase") or "pm").strip() or "pm"
    if phase not in ("pm", "deliver"):
        # review / done：没有可推进的模块（统一验收由人工在作业页提交，见 finish_job_review）
        logger(f"== 作业 {job_id} 处于 {JOB_PHASE_CN.get(phase, phase)}，没有需要推进的模块")
        return data
    if phase == "deliver":
        # 进下游前必须确认**所有**模块的 PM 都解决了 —— 这是"统一人工环节"的闸门
        blockers = job_pm_blockers(runs_dir, data)
        if blockers:
            raise GatewayError(job_pm_blockers_text(blockers))
    data["phase"] = phase
    write_job(runs_dir, data)
    logger(
        f"== 作业运行参数：项目类型={project_type}  review_every={review_every} "
        f"max_rework={max_rework} repo={repo or '未提供'}"
        "（显式传入 > 作业落盘 > 代码默认）"
    )
    logger(f"== 作业 {job_id} 阶段：{JOB_PHASE_CN.get(phase, phase)}")
    if phase == "deliver":
        # 人工环节只有两处（PM 统一确认 / 统一验收），模块内部的闸门统一关闭 ——
        # 否则每个模块各停一次，人工被拖进来 N 次，正是这次要改掉的
        logger("  （模块级人工闸门统一关闭：人工只处理 PM 一次与最终验收一次）")

    for index, mid in enumerate(order):
        row = by_id.get(mid)
        if not row:
            continue
        if phase == "pm":
            # PM 前置阶段：**不看依赖**（PM 只依赖需求与全局契约，与别的模块产物无关），
            # 已产出过 PM 的模块也不重跑 —— 人工可能已经裁决，重跑会把裁决结果冲掉。
            if row.get("run_dir") and row.get("status") in ("pm_paused", "paused", "done", "blocked"):
                continue
        else:
            # 终态只有 done 与 blocked。`skipped` 是**派生状态**（前置模块本轮没完成），
            # 不能被当成终态：模块失败后下游会被跳过，等失败原因修好再续跑时必须能重新评估 ——
            # 否则一次失败就把整组永久冻住（真机 job-20260926-154657）。blocked 才是真终态。
            if row.get("status") in ("done", "blocked"):
                continue
            unmet = [
                dep for dep in (row.get("depends_on") or [])
                if (by_id.get(dep) or {}).get("status") != "done"
            ]
            if unmet:
                row["status"] = "skipped"
                note = f"前置模块未完成：{unmet}"
                issues = list(row.get("issues") or [])
                if note not in issues:  # 幂等：反复续跑不该把这句说明堆成好几条
                    issues.append(note)
                row["issues"] = issues
                write_job(runs_dir, data)
                continue

        refresh_module_requirements(runs_dir, data)
        base = job_dir(runs_dir, job_id)
        req_path = base / "modules" / f"{index + 1:02d}-{mid}.md"
        requirement = req_path.read_text(encoding="utf-8")

        row["status"] = "running"
        write_job(runs_dir, data)
        logger(f"\n===== 作业 {job_id} · 模块 {mid}（{index + 1}/{len(order)}）开始 =====")

        # 参数已在 `_run_options` 里合并好（显式传入 > 作业落盘 > 代码默认），此处直接用
        orch_kwargs: dict[str, Any] = {
            "client": client,
            "repo": repo,
            "runs_dir": runs_dir,
            "max_rework": max_rework,
            "unload_at_end": False,  # 中途不卸载，整组跑完再卸（省下反复加载）
            "log": logger,
            "review_every": review_every,
            "pause_after": ["pm"] if phase == "pm" else [],
            "project_type": project_type,
            # 模块级人工审核延后到**作业统一验收**（两个阶段都这样：pm 阶段到不了那一步）
            "defer_human_review": True,
        }
        if pause_on_open_questions is not None:
            orch_kwargs["pause_on_open_questions"] = pause_on_open_questions
        orch = Orchestrator(**orch_kwargs)
        run_id = f"{job_id}-{mid}"
        run_dir = Path(str(row.get("run_dir") or (Path(runs_dir) / run_id)))
        try:
            if phase == "pm":
                _archive_previous_run(Path(runs_dir) / run_id, logger, run_id)
                result = orch.run(requirement, run_id=run_id)
            else:
                # 统一验收打回：从开发阶段重跑（走既有的 --from 语义 + 人工意见）
                rework_from = str(row.pop("rework_from", "") or "") or None
                feedback = str(row.pop("rework_feedback", "") or "") or None
                result = orch.resume(
                    run_dir, from_stage=rework_from, feedback=feedback, pause_after=[]
                )
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
        if phase == "pm":
            # PM 前置阶段：正常就是"停在 pm"（显式闸门）—— 状态单列，页面据此显示"待确认"
            row["status"] = "pm_paused" if result.paused else "done"
            write_job(runs_dir, data)
            logger(f"        模块 {mid} 的 PM 已产出（{'待人工确认' if result.paused else '无待确认项'}）")
            continue

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
        if result.paused:
            logger(f"== 作业 {job_id}：模块 {mid} 暂停，后续模块本轮不再推进（先处理闸门）")
            break

    if phase == "pm":
        blockers = job_pm_blockers(runs_dir, data)
        total = sum(len(v) for v in blockers.values())
        logger(
            f"\n== 作业 {job_id}：PM 前置阶段结束 —— {len(blockers)} 个模块共 {total} 条待人工确认。"
            "请在作业页逐模块确认（未明确项要写成确定结论），全部确认完再点「进入下游」。"
        )
    elif phase == "deliver" and job_status(data) in ("done", "partial"):
        # 全部模块跑完 → 打开作业级**统一验收**（下一相位）
        _open_job_review(job_dir(runs_dir, job_id), data, logger)
        write_job(runs_dir, data)

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
    """续跑作业 —— 按 ``job.json.phase`` 决定该推哪一段。

    * 还在 ``pm`` 阶段（各模块都停在 PM 上等人工）：**先核对全部模块的 PM 是否都确认完**，
      没确认完直接报错不放行；确认完就切到 ``deliver``，从下游串行跑完。
    * 已是 ``deliver`` 阶段：把停在闸门上的模块各自续跑完（模块级人工审核已延后，
      不会每模块停一次）。
    * ``review`` / ``done``：没有可推的模块（统一验收由作业页提交）。
    """
    from .orchestrator import Orchestrator

    data = read_job(runs_dir, job_id)
    if not data:
        raise GatewayError(f"找不到作业 {job_id}")
    logger = kwargs.pop("logger", print)
    # 运行参数：显式传入 > 作业落盘 > 代码默认。续跑这条链只带 job_id，
    # 落盘值就是唯一能还原「原配置」的地方（见 `_run_options` 的说明）。
    opts = _run_options(
        data,
        repo=kwargs.pop("repo", None),
        pause_after=kwargs.pop("pause_after", None),
        review_every=kwargs.pop("review_every", None),
        max_rework=kwargs.pop("max_rework", None),
        project_type=kwargs.pop("project_type", None),
    )

    phase = str(data.get("phase") or "pm").strip() or "pm"
    if phase == "pm":
        blockers = job_pm_blockers(runs_dir, data)
        if blockers:
            # 统一人工环节没做完就不放行（这是"人工只需要处理一次"的前提：
            # 处理完再一次放行，而不是每模块各停一次）
            raise GatewayError(job_pm_blockers_text(blockers))
        logger("== 作业：PM 待确认项已全部解决 → 进入下游串行流转")
        return run_job(
            job_id, runs_dir=runs_dir, client=client, logger=logger, phase="deliver", **opts
        )

    for row in data.get("modules") or []:
        if row.get("status") != "paused" or not row.get("run_dir"):
            continue
        logger(f"\n===== 作业 {job_id} · 续跑模块 {row['module_id']} =====")
        orch = Orchestrator(
            client=client,
            repo=opts["repo"],
            runs_dir=runs_dir,
            unload_at_end=False,
            log=logger,
            review_every=opts["review_every"],
            pause_after=opts["pause_after"],
            # 续跑沿用该 run 自己的项目类型（_restore 还会再按 state 校正一次）
            project_type=opts["project_type"],
            # 模块级人工审核延后到作业统一验收
            defer_human_review=True,
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

    return run_job(
        job_id,
        runs_dir=runs_dir,
        client=client,
        logger=logger,
        # 合并后的值继续往下传（run_job 内的 `_run_options` 会再合并一次，幂等）
        **opts,
    )


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
        base / runstore.GATEWAY_LINK_NAME,
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
    payload = _read_json(Path(runs_dir) / run_id / runstore.GATEWAY_LINK_NAME)
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
    view["phase"] = str(data.get("phase") or "pm")
    view["phase_cn"] = JOB_PHASE_CN.get(view["phase"], view["phase"])
    view["module_requirements"] = files
    view["report"] = report.read_text(encoding="utf-8") if report.exists() else ""
    view["dir"] = str(base)
    # PM 统一人工环节：每个模块还差几条待确认（空 = 可以进下游；页面据此显示与放行）
    try:
        view["pm_blockers"] = job_pm_blockers(runs_dir, data)
    except Exception:  # noqa: BLE001 - 视图查询不该因为一个坏模块整体失败
        view["pm_blockers"] = {}
    # 作业级**统一验收**产物（全部模块跑完后才有）
    review = _read_json(base / JOB_REVIEW_NAME)
    view["human_review"] = review if isinstance(review, dict) else None
    return view
