"""最终输出的**运行验证**：把补丁物化到沙箱目录，真的跑一遍，把结果当机械证据。

**为什么必须单独一个阶段**：评审（8K 上下文的 14B）读代码正文既看不出「能不能跑」，也拿不到
任何证据。真机教训 run 20260924-135801 —— 机械审计报「6 条补丁全可套用 / 0 问题」，
而其中 4 条指向同一个新文件、逐条写入互相覆盖，落盘只剩 1 个类，跑起来必然崩。
这类结论**只能靠执行得到**，读文件读不出来。

**安全约定**（本模块是整条流水线里唯一会执行外部命令的地方）：
  1. 只在 ``runs/<id>/verify/work`` 沙箱副本里跑，``cwd`` 固定为该目录，**绝不碰原仓库**；
  2. 只跑白名单程序（``config.VERIFY_ALLOWED_BINS``），首 token 不在表里的一律只记录不执行；
  3. 命中危险片段（``config.VERIFY_DENY_PATTERNS``）的命令只记录不执行；
  4. 逐条超时（``config.VERIFY_TIMEOUT``），超时杀掉子进程；
  5. 子进程环境洗掉宿主机的凭据类变量，并置 ``SDL_VIDEODRIVER=dummy`` 让其可在无显示环境跑；
  6. stdout/stderr 只留尾部若干字符，避免把巨型输出灌进 state.json。

mock 运行（``--mock``）下**不执行任何真实命令**，只把「打算跑什么」计划出来并标为 skipped ——
保证离线冒烟是确定性的、且不会在 CI 上乱跑东西。
"""
from __future__ import annotations

import ast
import builtins
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import lsp, patches

#: 单条命令输出保留的尾部字符数（写进 state.json / 喂给评审的都是这个截断后的版本）
OUTPUT_TAIL = 1500
#: 语法检查一次最多带多少个文件（避免命令行过长）
SYNTAX_MAX_FILES = 24
#: 沙箱复制时单文件体积上限（超过的跳过，通常是二进制/资源）
SINGLE_FILE_LIMIT = 20 * 1024 * 1024

#: 沙箱复制时若**跳过了这些后缀**的文件，验证结论就不可信。
#: 跳过一个 30MB 的贴图或一段日志无所谓（语法/导入/命令执行都不依赖它），
#: 但源码没进沙箱 ⇒ 语法检查查不到它、导入检查报莫名的 ImportError、命令必然跑挂，
#: 于是「沙箱里跑出来的结果」与「真实交付物能不能跑」是两回事。
#: 与 ``_STATIC_SUFFIX`` 区分开：后者是「会被静态解析」的（只有 .py），
#: 这里是「沙箱缺了它结论就失真」的（含会被命令直接执行的其它语言）。
_SANDBOX_CRITICAL_SUFFIXES = frozenset(
    {".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".cs", ".rb"}
)

STATUS_CN = {
    "ok": "通过",
    "fail": "失败",
    "timeout": "超时",
    "error": "无法执行",
    "unavailable": "程序不可用",
    "skipped": "未执行",
}

#: 环境变量里命中了这些词就**不传给子进程**（避免把宿主机的凭据带进去）
_SECRET_HINT = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE", re.I)
#: Windows 下隐藏子进程窗口，避免验证时弹黑框
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# --------------------------------------------------------------------- 沙箱
def _wipe(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _copy_tree(src: Path, dst: Path, limit_bytes: int, skip_dirs: frozenset[str]) -> tuple[int, bool, int, list[str]]:
    """把仓库复制进沙箱（跳过忽略目录与大文件）。

    返回 ``(复制文件数, 是否因超限提前停止, 跳过文件数, 被跳过的**源码**文件样例)``。
    最后一项是「沙箱保真度」的判据：跳过资源/二进制无所谓，跳过源码会让后面
    每一项检查都失真，那种沙箱里得出的结论不可信（判定见 :func:`materialize`）。
    """
    copied = 0
    skipped = 0
    total = 0
    stopped = False
    critical: list[str] = []

    def _note_skip(path: Path) -> None:
        nonlocal skipped
        skipped += 1
        if path.suffix.lower() in _SANDBOX_CRITICAL_SUFFIXES:
            critical.append(path.name)

    for root, dirnames, filenames in os.walk(src, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for name in filenames:
            source = Path(root) / name
            try:
                size = source.stat().st_size
            except OSError:
                _note_skip(source)
                continue
            if size > SINGLE_FILE_LIMIT:
                _note_skip(source)
                continue
            if total + size > limit_bytes:
                stopped = True
                break
            target = dst / source.relative_to(src)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            except OSError:
                _note_skip(source)
                continue
            total += size
            copied += 1
        if stopped:
            break
    return copied, stopped, skipped, critical


def materialize(
    run_dir: Path,
    repo: str | Path | None,
    impl: dict | None,
    audit: dict,
    *,
    copy_limit_mb: int,
    skip_dirs: frozenset[str],
) -> dict:
    """把「仓库 + 补丁」物化成沙箱里的可运行副本。绝不修改原仓库。"""
    root = Path(run_dir) / "verify"
    work = root / "work"
    _wipe(root)
    work.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {
        "work": str(work), "written": [], "notes": [], "error": None,
        "problems": [], "sandbox_incomplete": False,
    }

    repo_path = Path(repo) if repo else None
    if repo_path and repo_path.is_dir():
        copied, stopped, skipped, critical = _copy_tree(repo_path, work, copy_limit_mb * 1024 * 1024, skip_dirs)
        note = f"已复制仓库 {copied} 个文件到沙箱"
        if skipped:
            note += f"（跳过 {skipped} 个：忽略目录 / 单个超 {SINGLE_FILE_LIMIT // 1024 // 1024}MB）"
        if stopped:
            note += f"（已到 {copy_limit_mb}MB 上限，剩余未复制）"
        out["notes"].append(note)
        # 沙箱保真度：跳过资源文件无所谓，但**源码**没进沙箱，后面每项检查都失真 ——
        # 语法检查查不到它、导入检查报莫名的 ImportError、命令必然跑挂。
        # 这种沙箱里跑出来的结论与「真实交付物能不能跑」是两回事，必须判负，
        # 不能让它以 pass 混过评审再让人在目标目录里发现跑不起来。
        if critical:
            out["sandbox_incomplete"] = True
            out["problems"].append(
                f"沙箱不完整：{len(critical)} 个源码文件未被复制进沙箱"
                f"（{', '.join(critical[:5])}）—— 验证结论不可信"
            )
        if stopped:
            out["sandbox_incomplete"] = True
            out["problems"].append(
                f"沙箱复制触发 {copy_limit_mb}MB 总量上限，仓库未被完整复制 —— 验证结论不可信"
            )
    else:
        out["notes"].append("没有可复制的仓库（新建项目或目录不存在）：只物化补丁产出的文件")

    # 套用补丁到沙箱：已有文件 = 原文 + 补丁；新增文件（new_file）= 合并后整份写出。
    # apply_all 非 in_place 模式只写 out_dir，这里 out_dir 就是沙箱。
    read_from = repo_path if (repo_path and repo_path.is_dir()) else (root / "_no_repo")
    try:
        report = patches.apply_all(read_from, impl, audit, in_place=False, out_dir=work)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"物化失败：{type(exc).__name__}: {exc}"
        return out
    out["written"] = sorted({str(item.get("path") or "") for item in report.get("files") or []} - {""})
    if out["written"]:
        out["notes"].append(f"沙箱内已写入/覆盖 {len(out['written'])} 个文件：{', '.join(out['written'][:8])}")
    skipped = report.get("skipped") or []
    # 幂等命中（内容已在原文里）是**正确行为**，不是残缺：同一份补丁可能被套用多次
    # （闸门预览 → 放行后正式交付），第二次必然命中，不能拿它判负。
    real_skipped = [s for s in skipped if not patches.is_benign_skip(s)]
    if skipped and not real_skipped:
        out["notes"].append(f"有 {len(skipped)} 条补丁内容已存在于原文（幂等命中，无需重复套用）")
    if real_skipped:
        # 补丁套用失败是**纯机械事实**（anchor 定位不到 / 目标文件不存在 / 给的是 diff），
        # 不存在误判，因此升为阻断级而不是只记 note。
        #
        # 原先只记 note 的后果：沙箱里缺了这部分改动，但只要剩下的代码还跑得通，
        # verify 就照常 pass —— 交付到目标目录才发现残缺，正是「几轮都通过、
        # 交付却跑不了」的直接成因之一。交付物不完整本来就该判负，
        # 比让它混过去再让人发现代价小得多。
        out["problems"].append(
            "有 %d 条补丁未能套用（交付物不完整）：%s" % (
                len(real_skipped),
                "；".join(str(s.get("reason")) for s in real_skipped[:3]),
            )
        )
    return out


# --------------------------------------------------------------------- 命令计划
def _split_command(command: str) -> list[str]:
    """极简命令拆分：支持引号，不做 shell 展开（不经过 shell，避免命令注入）。"""
    parts: list[str] = []
    buf = ""
    quote = ""
    for ch in str(command or ""):
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf += ch
            continue
        if ch in "\"'":
            quote = ch
        elif ch.isspace():
            if buf:
                parts.append(buf)
                buf = ""
        else:
            buf += ch
    if buf:
        parts.append(buf)
    return parts


def _binary_name(token: str) -> str:
    name = Path(token.strip().strip('"')).name.lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1", ".sh"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def reject_reason(command: str, allowed_bins: frozenset[str], deny_patterns: tuple[str, ...]) -> str | None:
    """判断这条命令能不能跑；能跑返回 None，不能跑返回原因（只记录、不执行）。"""
    argv = _split_command(command)
    if not argv:
        return "命令为空"
    raw = str(command).lower()
    for pattern in deny_patterns:
        if pattern in raw:
            return f"命中危险片段 `{pattern.strip()}`，按安全约定不执行"
    # 管道 / 重定向 / 命令替换 / 变量展开：一律不跑。
    # 注意**不放行** `;`：我们从不经过 shell（argv 直调），`;` 在这里是惰性的，
    # 而 import 检查那类 `python -c "..."` 脚本正需要它分句。
    if re.search(r"[|&<>`$]", str(command)):
        return "含管道/重定向/命令替换等 shell 语法，按安全约定不执行"
    if _binary_name(argv[0]) not in allowed_bins:
        return f"程序 `{argv[0]}` 不在白名单里（{', '.join(sorted(allowed_bins))}）"
    return None


def _is_python_command(argv: list[str]) -> bool:
    return bool(argv) and _binary_name(argv[0]) in ("python", "python3", "py")


#: 导入检查脚本：逐个 import 物化出来的模块。
#: py_compile 只查语法，抓不到「用了没导入的名字」——真机 run 20260924-135801 的
#: graphics_renderer.py 在 `def __init__(self, game_area: Tuple[int, int])` 里用了
#: 未导入的 `Tuple`，语法完全合法，**import 时才会炸**。这一步用 stdlib 就能补上。
_IMPORT_CHECK = (
    "import importlib, sys\n"
    "bad = []\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        importlib.import_module(name)\n"
    "        print('OK', name)\n"
    "    except BaseException as exc:\n"
    "        print('FAIL', name, type(exc).__name__, exc)\n"
    "        bad.append(name)\n"
    "print('IMPORT_CHECK', 'FAILED' if bad else 'OK', len(bad))\n"
    "sys.exit(1 if bad else 0)\n"
)


#: 这些「文件名」不算遮蔽标准库：`__init__.py` 本来就是包入口，`__main__.py` 是入口约定。
_SHADOW_EXEMPT = {"__init__", "__main__"}


def _module_name(work: Path, rel: str) -> str | None:
    """把沙箱里的相对路径换算成可 import 的点分模块名（不合法的返回 None）。"""
    path = Path(rel)
    if path.suffix != ".py":
        return None
    parts = list(path.with_suffix("").parts)
    if not parts or parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(p.isidentifier() for p in parts):
        return None
    if not (work / path).exists():
        return None
    return ".".join(parts)


def _python_bin() -> str:
    """解释器路径带引号 —— 本机装的是 `C:\\Program Files\\Python312\\python.exe`，
    不引号会被按空格拆成两个 token，白名单判定直接把这唯一必跑的命令拒掉。"""
    return f'"{sys.executable}"' if " " in sys.executable else sys.executable


def import_check_spec(work: Path, written: list[str]) -> dict | None:
    """产出模块的**导入探针**命令（真跑一次 import），没有可导入的模块时返回 None。

    py_compile 只查语法，抓不到「用了没导入的名字」—— 真机 run 20260924-135801 的
    graphics_renderer.py 在签名里用了未导入的 `Tuple`，语法完全合法、**import 才炸**。

    抽成函数是为了让**两处共用同一套口径**：verify 的命令计划，以及 **dev 阶段的自检**。
    后者是关键：只在 verify 跑，这类硬错要等 test + verify + review 一整轮（≈5 分钟 +
    一次 14B 评审）之后才暴露；放进 dev 自检，几十秒就能带原文重问。
    """
    modules = [name for name in (_module_name(work, rel) for rel in written) if name]
    modules = [m for m in modules if m.split(".")[-1] not in _SHADOW_EXEMPT]
    if not modules:
        return None
    return {
        "command": f'{_python_bin()} -c "{_IMPORT_CHECK}" ' + " ".join(modules[:SYNTAX_MAX_FILES]),
        "source": "import",
        "display": "导入检查（能抓到语法合法但用了未导入名字的模块）",
    }


def _defined_names(tree: ast.Module, self_module: str = "") -> set[str]:
    """模块里出现过的「顶层可导入名」（保守：宁可多认，不可误报）。

    刻意用 ``ast.walk`` 收集（**连函数体里的定义也算**）—— 这是**故意放宽**：
    漏判几个真缺失，代价是"少拦一次"；而把其实有定义的名字报成缺失，代价是
    把**正确的实现**打回去重做（正是我们要根除的白跑）。

    `self_module` 用于**掐掉"自我作证"**：文件里那句 `from main import main` 会把
    `main` 这个名字加进本模块的命名空间，若照单全收，它就替自己证明了"main 有定义"
    —— 于是 `from main import main`（真机 20260927-123032 的形态）永远抓不到。
    传本模块名即可把这类自导入引入的名字排除。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                base = (alias.asname or alias.name).split(".")[0]
                if (
                    self_module
                    and isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.split(".")[0] == self_module
                ):
                    continue  # 自导入不能给自己作证
                names.add(base)
    return names


def import_symbol_problems(work: Path, written: list[str]) -> list[str]:
    """产出文件里的 `from X import Y` 有没有毛病（**纯 AST、零执行、零副作用**）。

    只查两类**确定性**问题（真机反复踩，且都能毫秒级判出来）：

      ① `from m import f`，而 `m` 是本轮产出的文件、里面**没有** `f`
         —— 真机 20260927-123032：`main.py` 里 `from main import main`，而 main.py
         根本没定义 `main`（自导入 + 未定义，两条叠在一起）。
      ② 产出文件与**标准库同名**（`importlib.py` / `random.py` / `typing.py`）
         —— 它在沙箱里会**遮蔽**标准库：`import importlib` 会 import 到这个文件，
         导入探针、pyright 甚至测试自己都会跟着失真（真机 20260927-134222 的缺陷单里
         出现过 `importlib.py`）。

    刻意**不查外部符号是否存在**（`from tkinter import event` 这一类）：判它要么真 import
    （有副作用，`antigravity` 那种还会弹浏览器），要么需要 stubs。而那类错误
    **导入探针已经覆盖**（它真跑一次 import，拿到的是执行级证据）。
    """
    out: list[str] = []
    local: dict[str, Path] = {}
    for rel in written:
        rel = str(rel)
        if rel.endswith(".py"):
            local.setdefault(Path(rel).stem, work / rel)
    stdlib = set(getattr(sys, "stdlib_module_names", frozenset()))
    seen_shadow: set[str] = set()
    for rel in written:
        rel = str(rel)
        if not rel.endswith(".py"):
            continue
        stem = Path(rel).stem
        if stem in stdlib and stem not in _SHADOW_EXEMPT and stem not in seen_shadow:
            seen_shadow.add(stem)
            out.append(
                f"`{rel}` 与标准库模块 `{stem}` 同名 —— 在沙箱里它会**遮蔽**标准库"
                f"（`import {stem}` 会 import 到这个文件），导入检查与类型检查都会失真。"
                f"请换个文件名（例如 `{stem}_util.py`）。"
            )
        path = work / rel
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue  # 文件读不到 / 语法坏掉由别的档去报，这里不重复报
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module or node.level:
                continue  # 相对导入（from . import x）不在这里判
            target = local.get(node.module.split(".")[0])
            if target is None:
                continue  # 外部依赖：交给导入探针
            try:
                defined = _defined_names(
                    ast.parse(target.read_text(encoding="utf-8", errors="replace")),
                    # 传目标模块名：掐掉它内部的自导入"自我作证"
                    Path(node.module.split(".")[0]).stem,
                )
            except (OSError, SyntaxError):
                continue
            for alias in node.names:
                if alias.name == "*" or alias.name in defined:
                    continue
                out.append(
                    f"`{rel}` 里写了 `from {node.module} import {alias.name}`，"
                    f"但 `{target.name}` 里没有定义 `{alias.name}` —— import 时必然 ImportError。"
                )
    return out


def plan_commands(
    work: Path,
    written: list[str],
    test_report: dict | None,
    *,
    max_commands: int,
    impl: dict | None = None,
) -> list[dict]:
    """决定在沙箱里跑什么：语法检查（必跑）→ 测试阶段声明的命令 → 兜底探测。"""
    specs: list[dict] = []
    py_files = [w for w in written if w.endswith(".py") and (work / w).exists()]
    if py_files and max_commands > 0:
        specs.append(
            {
                "command": " ".join([_python_bin(), "-m", "py_compile", *py_files[:SYNTAX_MAX_FILES]]),
                "source": "syntax",
                "display": "语法检查（py_compile：只解析不执行，零副作用）",
            }
        )
        spec_import = import_check_spec(work, written)
        if spec_import and len(specs) < max_commands:
            specs.append(spec_import)

    # 开发自己声明的入口命令。为什么值得单独一档：测试阶段的命令常写成
    # `python <库模块>.py`（空跑、rc=0 却什么都没做），而**写代码的人**最清楚该怎么跑。
    # 这条会真的执行 —— 跑不起来就是开发自己的锅，也不能再拿「命令质量问题」推脱。
    dev_run = str((impl or {}).get("run") or "").strip()
    if dev_run and len(specs) < max_commands:
        specs.append({"command": dev_run, "source": "dev-run", "display": "开发声明的入口命令"})

    # 兜底探测 —— **必须排在「测试阶段声明的命令」之前**。
    #
    # 命令槽位是有限的（max_commands），把「跑交付物入口」和模型随手写的窄命令放进同一个
    # 池子里抢槽位，模型一多写两条就把入口挤掉了。真机 2026-09-26（run snake-detailed）
    # 第 2、3 轮正是如此：test 阶段把第 1 轮的 `python main.py` 换成了 3 条
    # `python -c "import game_logic; game_logic.GameLogic().move('Right')"`，
    # 语法 + 导入 + 那 3 条 = 5，**入口探测一次机会都没有** → 于是
    # runnability_problems 只能报「没有任何命令真正执行交付物」→ rework → 再来一轮还是
    # 被挤掉 → 三轮不收敛，最后 needs_human、什么都不交付。
    #
    # 「产物到底能不能跑起来」是这套验证里最重要的一条证据，它必须优先于模型随手写的命令。
    if len(specs) < max_commands:
        probe = _entry_probe(work)
        if probe:
            specs.append(probe)

    declared = [
        str(c.get("command") or "").strip()
        for c in (test_report or {}).get("automated_commands") or []
        if isinstance(c, dict) and str(c.get("command") or "").strip()
    ]
    # **带参真跑入口**的 planned 命令要排最前（稳定排序，同档内保持模型原序）。
    # 真机 run 20260928-200631：test 给了 8~9 条命令，机械自检 + 裸 probe 占掉 3 槽后，
    # 前两个 planned 槽被 `python -c "import db; db.Database()...."` 这类窄命令占满，
    # 真正验证 CLI 行为的 `python main.py add/list/remove` 一轮都没轮上 —— 假绿/行为盲飞。
    entry_names = {Path(str(n)).name for n in entry_targets(work, written)}

    def _runs_named_entry(command: str) -> bool:
        rel = _python_script_arg(command)
        if rel and Path(str(rel).replace("\\", "/")).name in entry_names:
            return True
        # `-m pkg` 等形态的兜底；明确排除 `python -c "import main"` 这类只导入不执行的。
        return " -c " not in command and any(name in command for name in entry_names)

    declared_specs = [
        {"command": command, "source": "planned", "display": "测试阶段声明的命令"}
        for command in declared
    ]
    declared_specs.sort(key=lambda s: 0 if _runs_named_entry(str(s["command"])) else 1)
    for spec in declared_specs:
        if len(specs) >= max_commands:
            break
        specs.append(spec)
    # 去重：开发声明的入口命令与测试阶段声明的命令常常是同一条，别跑两遍白等一轮
    seen: set[str] = set()
    deduped: list[dict] = []
    for spec in specs:
        key = str(spec.get("command") or "").strip()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(spec)
    return deduped[:max_commands]


def _python_script_arg(command: str) -> str | None:
    """若命令形如「<python> 单个脚本.py」，返回该脚本；否则 None（保守判定，宁漏不错）。"""
    text = str(command or "").strip()
    for prefix in (_python_bin(), "python", "python.exe", '"python"'):
        if text.startswith(prefix + " "):
            rest = text[len(prefix) + 1 :].strip()
            if rest and " " not in rest and rest.endswith(".py"):
                return rest
            return None
    return None


def entry_script_problems(work: Path, commands: list[dict]) -> list[str]:
    """对「直接执行脚本」类命令，追问一句：它到底有没有入口 —— rc=0 也可能是**什么都没做**。

    真机 run 20260924-185507 第 3 轮：verify 三条命令全过、判定 pass，交付物却不可玩。
    `python x.py` 退出码为 0 有两种截然不同的成因：真的跑完退出，或
    **文件里根本没有 `if __name__ == '__main__':`** —— 定义完类就结束，同样返回 0。
    后者属于「假绿」，这里把它显式点出来（只在命令被判为通过时才追问）。
    """
    out: list[str] = []
    seen: set[str] = set()
    for cmd in commands:
        if str(cmd.get("status")) != "ok":
            continue
        rel = _python_script_arg(str(cmd.get("command") or ""))
        if not rel or rel in seen:
            continue
        seen.add(rel)
        path = work / rel
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "__main__" not in source:
            out.append(
                f"`{rel}` 没有 `if __name__ == '__main__':` 入口：直接执行它退出码 0 "
                "证明不了任何行为（定义完类就退出了）。两种可能：该文件本该有入口（交付缺陷），"
                "或者这条测试命令选得不对（命令质量问题）—— 两者都要人看一眼，故只作提示不判失败。"
            )
    return out


#: 本轮产出里会被静态解析的后缀
_STATIC_SUFFIX = ".py"

#: 「一看就知道能当入口」的文件名（没有 __main__ 时退而求其次认这些）。
#: 公开给编排器复用：判定「方案有没有规划入口」以及演示面板挑入口都要它。
ENTRY_NAMES = ("main.py", "__main__.py", "run.py", "app.py", "cli.py")


#: 无参执行 CLI 入口时，程序**有意**打印用法并以非零码退出的典型输出。
#: 真机 run 20260928-180933：`python main.py` 无参 → 打印 `Usage: …` + rc=1，
#: 这是 argparse 式 CLI 的标准设计，却被入口探针当成「产物失败」判负。
_USAGE_EXIT_RE = re.compile(
    r"usage\s*:|用法|unrecognized arguments?|required positional argument|"
    r"arguments? are required|no command|子命令|缺少参数|参数(?:错误|不足|个数|不对)",
    re.I,
)
_TRACEBACK_MARK_RE = re.compile(r"traceback \(most recent call last\)", re.I)
#: 裸入口调用：`python main.py`（脚本名后没有任何参数）
_BARE_SCRIPT_RE = re.compile(r'^\s*"?[^"\n]*python[\w.]*"?\s+(\S+\.py)\s*$', re.I)


def is_usage_exit(cmd: dict) -> bool:
    """入口探针的非零退出是不是「无参打印用法」的**有意设计**（而非崩溃）。

    四个条件全满足才算（宁漏不错：漏判只是多一次返工，误判会把真崩溃洗成通过）：
      ① harness 自己生成的入口探针（``source == "probe"``），非测试阶段声明的命令；
      ② 裸入口调用（脚本名后没有参数）；
      ③ 输出含用法提示特征；
      ④ 输出里**没有** Python traceback（崩溃栈在就一定是实现失败）。
    """
    if str(cmd.get("source") or "") != "probe":
        return False
    if str(cmd.get("status") or "") != "fail":
        return False
    if not _BARE_SCRIPT_RE.match(str(cmd.get("command") or "")):
        return False
    out = f"{cmd.get('stderr_tail') or ''}\n{cmd.get('stdout_tail') or ''}"
    if _TRACEBACK_MARK_RE.search(out):
        return False
    return bool(_USAGE_EXIT_RE.search(out))


def _entry_probe(work: Path) -> dict[str, Any] | None:
    """「一看就知道怎么跑」的入口探测，宁缺毋滥。

    **优先约定入口脚本、其次才是 pytest**：前者真正执行了应用，是「产物能不能跑」的直接
    证据；pytest 的输出只证明测试跑过（而且沙箱里未必装了 pytest，会落进 unavailable）。
    真机 run 20260926 的教训是入口探测被槽位挤掉，所以这里的优先级也要顺过来。
    """
    for name in ENTRY_NAMES:
        if (work / name).is_file():
            return {"command": f"{_python_bin()} {name}", "source": "probe", "display": f"探测到 {name}"}
    has_tests = any(
        p.name.startswith(("test_", "_test.py")) and p.suffix == ".py"
        for p in work.rglob("*.py")
        if "__pycache__" not in p.parts
    ) or (work / "tests").is_dir()
    if has_tests:
        return {"command": f"{_python_bin()} -m pytest -q", "source": "probe", "display": "探测到测试目录"}
    return None


#: 常驻类交付物（游戏主循环 / 桌面窗口）的验证超时。
#:
#: 这类程序**跑满超时是正常的** —— 下面 ``long_running`` 的判定就是把它当作
#: 「能跑起来」的正面证据（真机 run 20260925-110258 的贪吃蛇 `python main.py` 跑满
#: 180s 被强杀；若算失败，任何常驻形态的交付物都永远过不了 verify）。
#: 既然结论与「跑 180s」还是「跑 20s」无关，让它跑满就是纯浪费：真机 run snake-impfix
#: 第 1 轮里 `python main.py` 两条各卡满 180s，一轮白扔约 6 分钟。
#: 20s 足够证明「进程起来了、没立刻崩」，判定口径一字不变。
RESIDENT_ENTRY_TIMEOUT = 20

#: 用到这些模块 ⇒ 产物是桌面/游戏形态，启动后不会自己退出
_RESIDENT_MODULES = frozenset(
    {"pygame", "tkinter", "pyglet", "arcade", "curses", "PyQt5", "PySide6", "wx"}
)
#: 主循环的常见写法（补上「没直接写模块名」的情况，比如经由封装间接调用）
_RESIDENT_MARKERS = ("mainloop()", "app.exec", "clock.tick", "display.flip", "exec_()")


def is_resident_entry(work: Path, rel: str) -> bool:
    """这个入口脚本是不是「启动后一直运行」的常驻程序（游戏主循环 / 桌面窗口）。

    **只在能确定时才返回 True**（宁漏不错）：漏判只是多花一点时间（退回完整超时），
    误判会把一个本该正常退出的 CLI 当成常驻 —— 于是用短超时把它判成「一直在跑」，
    把一个真的失败掩盖成通过。
    """
    path = work / str(rel or "")
    if not path.is_file() or path.suffix.lower() != ".py":
        return False
    src = path.read_text(encoding="utf-8", errors="replace")
    for module in _RESIDENT_MODULES:
        if re.search(rf"^\s*(?:import|from)\s+{re.escape(module)}\b", src, re.M):
            return True
    return any(marker in src for marker in _RESIDENT_MARKERS)


def _bound_names(tree: ast.Module) -> set[str]:
    """文件里所有**被绑定**的名字：模块级定义/赋值/导入 + 各级函数参数/局部变量等。

    口径是「宁可多算（少报）也别少算（误报）」—— 多算只会漏掉真问题，
    少算会把正常代码判成缺陷，后者会让开发去修一个不存在的问题。
    """
    bound: set[str] = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
    return bound


def _undefined_cross_module(
    defs: dict[str, set[str]],
    trees: dict[str, ast.Module],
    bound: dict[str, set[str]],
) -> list[str]:
    """抓「用了别的文件里的东西，却没 import」—— 真机 run 20260924-235001 的元凶。

    `game_loop.py` 里直接写 `Snake()` / `Food()` / `Score()`，但 5 个文件之间**零 import**。
    它躲过了当时所有检查：`py_compile` 只解析不执行、import 检查只 import 不调用，
    于是整批产物一路 pass 到人工闸门，人一跑就是 `NameError: name 'Snake' is not defined`。

    判定刻意收得很窄，**只报**同时满足这三条的名字，避免误伤：
      1) 本文件里既没定义也没导入（连局部变量/参数都不是）；
      2) 不是内置名；
      3) **同批次另一个文件里确实定义了它** —— 这才是「漏 import」的确证。
    只满足前两条的（第三方包没装、拼错的名字）交给导入检查去报，这里不碰。
    """
    owners: dict[str, list[str]] = {}
    for module, names in defs.items():
        for name in names:
            owners.setdefault(name, []).append(module)

    out: list[str] = []
    for module, tree in sorted(trees.items()):
        local = bound.get(module) or set()
        used: dict[str, int] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                used.setdefault(node.id, node.lineno)
        for name, lineno in sorted(used.items(), key=lambda kv: kv[1]):
            if name in local:
                continue
            srcs = [m for m in owners.get(name, []) if m != module]
            if not srcs:
                continue
            out.append(
                f"{module}.py 第 {lineno} 行用了 `{name}`，本文件既未定义也未导入，"
                f"而 {srcs[0]}.py 定义了它 —— 几乎可以肯定是漏了 import"
            )
    return out


def entry_files(work: Path, written: list[str]) -> list[str]:
    """这批交付物里哪些文件能被当成「可执行入口」。"""
    out: list[str] = []
    for rel in written:
        if not str(rel).endswith(_STATIC_SUFFIX):
            continue
        path = work / rel
        if not path.is_file():
            continue
        if "__main__" in path.read_text(encoding="utf-8", errors="replace"):
            out.append(rel)
    return out


def entry_targets(work: Path, written: list[str]) -> set[str]:
    """可作为入口的文件名集合（带 `__main__` 的 + 约定入口名）。"""
    names = set(entry_files(work, written))
    names |= {n for n in ENTRY_NAMES if (work / n).is_file()}
    return names


def runs_entry(work: Path, written: list[str], command: str) -> bool:
    """这条命令是不是在跑「交付物自己的入口」。"""
    text = str(command or "")
    rel = _python_script_arg(text)
    if rel and rel in entry_targets(work, written):
        return True
    return any(name in text for name in entry_targets(work, written))


def runs_guarded_entry(work: Path, written: list[str], command: str) -> bool:
    """这条命令是不是在跑**真正带入口守卫**（`if __name__ == "__main__"`）的交付文件。

    与 :func:`runs_entry` 的区别：后者把「文件名叫 main.py」也算入口，但同名文件
    可能只是个函数库 —— `python main.py` 定义完函数静默 rc=0、输出为空，
    这不是「跑起来了」的证据（真机 run 20260928-200631：最终 main.py 无 import、
    无守卫，verify 却 5/5 全绿）。非空 stdout 那条证据由调用方另行接受，不在这里。
    """
    guarded = {str(n).replace("\\", "/") for n in entry_files(work, written)}
    if not guarded:
        return False
    text = str(command or "")
    rel = _python_script_arg(text)
    if rel and str(rel).replace("\\", "/") in guarded:
        return True
    # 兜底：命令行没按脚本参数形态写（引号、-m 等），用守卫文件的名字做子串匹配。
    return any(str(n).replace("\\", "/") in text for n in guarded)


def _sig_args(args: ast.arguments, *, drop_self: bool) -> str:
    """把函数签名渲染成 ``(a, b=1, *args, **kw)`` 形式（只留形状，不留注解细节）。"""
    parts: list[str] = []
    pos = [*args.posonlyargs, *args.args]
    defaults: list[ast.expr | None] = [None] * (len(pos) - len(args.defaults)) + list(args.defaults)
    for i, (arg, dflt) in enumerate(zip(pos, defaults, strict=True)):
        if drop_self and i == 0 and arg.arg in ("self", "cls"):
            continue
        parts.append(arg.arg + (f"={ast.unparse(dflt)}" if dflt is not None else ""))
    if args.vararg:
        parts.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        parts.append("*")
    for arg, dflt in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        parts.append(arg.arg + (f"={ast.unparse(dflt)}" if dflt is not None else ""))
    if args.kwarg:
        parts.append("**" + args.kwarg.arg)
    return "(" + ", ".join(parts) + ")"


# --------------------------------------------------------------------- 命令质量核对
# 测试阶段声明的 `automated_commands` 是**模型写出来的文本**，从没被执行过就流到 verify。
# 下面这套是**静态**核对（零执行、零副作用），回答一个问题：这条命令**自己跑得起来吗**？
# 判它干什么：命令写错 ⇒ verify 拿不到可运行证据 ⇒ 评审把失败误读成"实现缺陷" ⇒
# 让开发去改**本来正确**的实现（真机 20260927-073518 实测）。命令只有测试阶段能改。

#: 从 `python -c "<code>"` 里抠代码正文。**刻意保守**：只认单行的双/单引号参数，
#: 匹配不到就返回空串（宁可不判，也不要因为解析偏差误伤一条合法命令）。
_C_PAYLOAD_RE = re.compile(r"""(?:^|\s)-c\s+("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')""")


def c_payload(command: str) -> str:
    """取 `python -c "<code>"` 里的代码正文（不是这种形式返回空串）。"""
    m = _C_PAYLOAD_RE.search(str(command or ""))
    if not m:
        return ""
    # 只还原转义引号；**不能**用 unicode_escape —— 那会把中文弄成乱码。
    return m.group(1)[1:-1].replace('\\"', '"').replace("\\'", "'")


def sig_arity(args_text: str) -> tuple[int, bool] | None:
    """从 ``(a, b=1)`` 这样的签名正文算「必需参数个数」与「是否接受可变参数」。

    借 ``ast`` 解析而不是按逗号切分 —— 默认值里可能自带逗号 / 元组，切分必错。
    解析失败返回 None（调用方据此跳过，不做判断）。
    """
    try:
        fn = ast.parse(f"def _f({args_text}):\n    pass").body[0]
    except (SyntaxError, ValueError):
        return None
    if not isinstance(fn, ast.FunctionDef):
        return None
    a = fn.args
    positional = [*a.posonlyargs, *a.args]
    required = len(positional) - len(a.defaults)
    required += sum(1 for d in a.kw_defaults if d is None)  # 无默认值的关键字参数也是必需的
    return (max(required, 0), bool(a.vararg or a.kwarg))


def digest_arities(digest: Any) -> dict[str, tuple[int, bool]]:
    """从接口摘要（``api_digest`` 的产物）抽「符号名 → (必需参数个数, 是否可变参数)」。

    摘要每项形如 ``class SnakeGame`` / ``    def __init__(width, height)`` / ``def main()``
    （类方法已由 :func:`api_digest` 去掉 ``self``）。**缩进 0** 的是模块级函数，带缩进的是
    类的方法 —— 只把 ``__init__`` 的必需参数记到**类名**上（没有 ``__init__`` 即 0 个必需
    参数，与 Python 默认构造一致）。
    """
    out: dict[str, tuple[int, bool]] = {}
    if not isinstance(digest, dict):
        return out
    for members in digest.values():
        if not isinstance(members, list):
            continue
        current = ""
        for raw in members:
            text = str(raw or "")
            stripped = text.strip()
            if stripped.startswith("class "):
                current = stripped[len("class "):].split("(")[0].strip()
                out.setdefault(current, (0, False))
                continue
            m = re.match(r"def\s+(\w+)\s*\((.*)\)\s*$", stripped)
            if not m:
                continue
            name, args_text = m.group(1), m.group(2)
            arity = sig_arity(args_text)
            if arity is None:
                continue
            if text.startswith(" ") and current:
                if name == "__init__":  # 构造函数：把必需参数记到类名上
                    out[current] = arity
            else:  # 模块级函数
                out.setdefault(name, arity)
    return out


def _callee(func: ast.expr) -> str:
    """调用表达式里的被调名：``Cls(...)`` → ``Cls``；``m.Cls(...)`` → ``Cls``。"""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _payload_calls(command: str) -> list[ast.Call] | None:
    """命令里那段 `-c` 代码里的所有调用；不是 `-c` 形式 / 解析失败返回 None。"""
    payload = c_payload(command)
    if not payload:
        return None
    try:
        tree = ast.parse(payload)
    except (SyntaxError, ValueError):
        return None
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _passes_none_literal_call(call: ast.Call) -> bool:
    """这个调用有没有把裸 ``None`` 当实参传进来（**位置实参与关键字实参都算**）。

    关键字形式必须一并认：真机 20260927-100009 的命令写的是 `snake.Snake(canvas=None)` ——
    只查位置实参会漏掉它，于是这条「命令自身不可执行」被算成实现缺陷。
    """
    return any(
        isinstance(a, ast.Constant) and a.value is None
        for a in (*call.args, *(k.value for k in call.keywords))
    )


def passes_none_literal(command: str) -> bool:
    """命令里有没有把**裸 ``None``** 当实参传给某个调用（拿它顶替必需对象）。"""
    calls = _payload_calls(command)
    if not calls:
        return False
    return any(_passes_none_literal_call(call) for call in calls)


def command_param_problems(commands: Any, digest: Any) -> list[str]:
    """测试声明的命令**自身能不能执行**？—— 用接口摘要做静态核对。

    真机 20260927-073518：新建项目下仓库为空（测试阶段看不到任何源码片段），实现信息只有
    几百 token 的摘要，于是 7B 按类名猜出 `SnakeGame()` 这类**无参构造**，而 `__init__`
    需要参数 —— verify 里必然 `TypeError`，拿不到可运行证据，评审再把它误判成"实现缺参数"。
    补上摘要后它改成了 `SnakeGame(None)`：个数对了，但拿 `None` 顶替 Tk 对象，运行时照样
    `AttributeError`。两种形态都是**命令缺陷**，都在这里拦。

    只做两档、都极确定（宁可漏，不可误伤）：
      ① 实参个数少于接口声明的必需形参个数；
      ② 给调用传了**裸 `None`**（顶替必需对象，运行时必炸）。
    名字未知、含 `*args`/`**kwargs`/`**kw`、非 `-c` 形式（如 `python main.py`）的一律不判。
    """
    table = digest_arities(digest)
    if not table:
        return []
    out: list[str] = []
    for c in commands or []:
        if not isinstance(c, dict):
            continue
        cmd = str(c.get("command") or "")
        payload = c_payload(cmd)
        if not payload:
            continue
        try:
            tree = ast.parse(payload)
        except SyntaxError as exc:
            out.append(f"`{cmd}`：命令里的 Python 片段有语法错误（{exc.msg}），无法执行。")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _callee(node.func)
            spec = table.get(name) if name else None
            if not spec:
                continue
            required, varargs = spec
            if any(isinstance(a, ast.Starred) for a in node.args) or any(
                k.arg is None for k in node.keywords
            ):
                continue
            provided = len(node.args) + len([k for k in node.keywords if k.arg])
            if provided < required:
                out.append(
                    f"`{cmd}`：`{name}` 至少需要 {required} 个参数，命令里只传了 {provided} 个"
                    " —— 命令自身无法执行，请按【本轮已产出文件的接口】把参数补齐。"
                )
                continue
            if _passes_none_literal_call(node):
                out.append(
                    f"`{cmd}`：`{name}` 被传了 `None` 顶替必需对象 —— 参数个数虽够，运行时会因"
                    " `'NoneType' object has no attribute ...` 失败（命令自身无法执行）。"
                    "请改为断言**不需要显示器/网络等外部环境**的纯逻辑（别直接实例化 GUI 对象）。"
                )
    return out


def _declared_symbols(text: str) -> dict[str, list[str]]:
    """一个文件里**对外可用**的符号 → 形参名列表（模块级 def/class/常量 + 类的方法）。

    与 `api_digest` 同源（都是 ast），但那个是给模型看的**描述**，这份是给机械断言用的
    **事实**：键是符号名（`foo` / `Class.method`），值是除 `self`、`cls` 之外的形参名。

    **模块级赋值也要收**（`SNAKE_DIRECTIONS = {...}`）：方案的 `contracts.uses` 里常写
    「我要用 `snake.SNAKE_DIRECTIONS`」这种**常量**，只收 def/class 会让它永远被判
    「产物里没有这个符号」—— 真机 20260927-123032 就是这么误报的（而那条已经进阻断项）。
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {}
    out: dict[str, list[str]] = {}

    def _params(node: ast.AST) -> list[str]:
        args = getattr(node, "args", None)
        if args is None:
            return []
        names = [a.arg for a in getattr(args, "args", [])]
        return [n for n in names if n not in ("self", "cls")]

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = _params(node)
        elif isinstance(node, ast.ClassDef):
            out[node.name] = []
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{node.name}.{sub.name}"] = _params(sub)
                    out.setdefault(sub.name, _params(sub))
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out.setdefault(tgt.id, [])
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.setdefault(node.target.id, [])
    return out


def _interface_of(text: str) -> tuple[str, list[str]]:
    """从方案里写的 `add(amount: float, note: str) -> None` 取出 (名字, 形参数)。

    按括号深度切分，避免默认值 `f(x, y=(1, 2))` 里的逗号被当成参数分隔符。

    还要兼容「实例方法的调用形态」``Database().insert(amount, note) -> None``：
    真正的接口是最后一个 ``insert``，不能在第一个 ``(`` 处截断把名字取成 ``Database``
    （真机 run 20260928-200631：编译器把 T-02 的 interface 写成这个形态，
    五轮恒定误报「Database 签名不符：声明 2 个参数，实际 0 个」）。
    """
    s = str(text or "").strip()
    if "->" in s:
        s = s.split("->", 1)[0].strip()
    if "(" not in s:
        return s, []
    # 接口名一定是「最后一个 名字(」里的名字；前面允许任意接收者前缀（Database(). / x.）。
    m = re.match(r"^(?:.*[^A-Za-z0-9_])?([A-Za-z_]\w*)\s*\((.*)\)\s*$", s, re.S)
    if not m:
        return s, []
    name, inner = m.group(1), m.group(2)
    depth, cur, parts = 0, "", []
    for ch in inner:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return name, [p.strip() for p in parts if p.strip()]


def skeleton_conformance(work: str | Path, written: list[str], skeleton: Any) -> dict[str, Any]:
    """**冻结接口基准 vs 实际产物**：基准声明的对外符号，产物里到底有没有。

    为什么单做一份（而不是只靠 :func:`contract_check`）：那个核的是方案里
    ``contracts.exposes`` / ``contracts.uses`` / ``interface`` —— 那两项契约**只提示不强制**，
    真机上通常为空 ⇒ 跨文件契约校验长期形同虚设。冻结骨架一出，基准就**非空且权威**。

    只把「**声明了却完全不存在**」（``missing``）算硬缺陷：无歧义 —— 任何按基准调用的
    文件都会 `ImportError` / `AttributeError`。签名差异（``mismatch``）只**记录**不判负：
    调用方可能已被同步改过，属于可讨论的偏差（开发应在 ``deviations`` 里说明）。
    """
    out: dict[str, Any] = {"missing": [], "mismatch": [], "by_file": {}, "checked": 0}
    if not isinstance(skeleton, dict) or not skeleton:
        return out
    root = Path(work)
    # 遍历**并集**而不是只看产物：只遍历产物会漏掉最高价值的一档 ——
    # 「基准冻结了接口，产物里根本没有这个文件」（那正是"文件凭空消失"）。
    paths = {str(p).replace("\\", "/") for p in (written or [])}
    paths.update(str(p).replace("\\", "/") for p in skeleton)
    for rel in sorted(paths):
        declared = skeleton.get(rel)
        if not isinstance(declared, list) or not declared:
            continue
        path = root / rel
        if not path.is_file():
            out["missing"].append(f"{rel}：方案冻结了接口，产物里却没有这个文件")
            out["by_file"].setdefault(rel, []).append("文件缺失")
            continue
        try:
            actual = _declared_symbols(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        names = set(actual)
        cls = ""
        for line in declared:
            text = str(line)
            if text.startswith("class "):
                cls = text[len("class "):].split("(")[0].strip()
                if not cls:
                    continue
                out["checked"] += 1
                if cls not in names and not any(k.startswith(cls + ".") for k in names):
                    out["missing"].append(f"{rel}：基准声明了类 `{cls}`，产物里没有")
                    out["by_file"].setdefault(rel, []).append(cls)
                continue
            m = re.match(r"\s{4}def\s+(\w+)\s*\((.*)\)\s*$", text)
            if m and cls:  # 类的方法：只记签名差异
                key = f"{cls}.{m.group(1)}"
                params = actual.get(key)
                if params is None:
                    continue
                declared_arity = sig_arity(m.group(2))
                out["checked"] += 1
                if declared_arity and declared_arity[0] != len(params):
                    out["mismatch"].append(
                        f"{rel}：`{key}` 基准声明 {declared_arity[0]} 个参数，产物里是 {len(params)} 个"
                    )
                continue
            m = re.match(r"def\s+(\w+)\s*\((.*)\)\s*$", text)
            if m:  # 模块级函数
                out["checked"] += 1
                if m.group(1) not in names:
                    out["missing"].append(f"{rel}：基准声明了函数 `{m.group(1)}`，产物里没有")
                    out["by_file"].setdefault(rel, []).append(m.group(1))
    return out


def contract_check(work: str | Path, written: list[str], plan: Any) -> dict:
    """**跨文件契约比对**（聚合验证的核心，静态、不依赖 repo / LSP / 运行）。

    方案里的 `contracts.exposes` 说"我提供什么"、`contracts.uses` 说"我用谁的什么"、
    `interface` 说"签名长什么样"。这些**从来只被写、没被核过** —— 于是跨文件接口只能
    等真跑才暴露：真机 run snake-v2 的 `ui.py` 读并不存在的 `game_logic.score`，
    `AttributeError` 一直拖到 verify 跑入口才炸（整整一轮之后）。

    这里把它变成**毫秒级静态断言**，且每条都能按文件归因到具体 task ⇒ 只重做那一张图。

    刻意**不查** `symbols`（那是"本 task 要定义什么"）：它由 dev 阶段的施工图自检覆盖，
    这里重复判只会让同一问题在两处各报一次、互相干扰归因。
    """
    out: dict[str, Any] = {"problems": [], "checked": 0, "by_file": {}, "unresolved": []}
    tasks = [t for t in ((plan or {}).get("tasks") or []) if isinstance(t, dict)]
    if not tasks:
        return out
    root = Path(work)
    actual: dict[str, dict[str, list[str]]] = {}
    for rel in [str(w) for w in written or []]:
        path = root / rel
        if not path.is_file() or not rel.endswith(".py"):
            continue
        try:
            actual[str(rel).replace("\\", "/")] = _declared_symbols(
                path.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            continue
    if not actual:
        # 一个文件都没落盘 ⇒ 不是"契约对不上"，是更前置的问题，别在这里重复判
        out["unresolved"].append("沙箱里没有可解析的 Python 文件，契约比对无法进行")
        return out

    #: 本项目产出的文件主干名 —— 用来区分「项目内符号」与「外部依赖」。
    project_stems = {
        str(rel).replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".py") for rel in actual
    }
    #: 标准库模块名（3.10+）。模型在 `contracts.uses` 里常写 `random`/`tkinter.event`
    #: 这类外部依赖，它们不该被当作"跨文件接口"去核对。
    _STDLIB = set(getattr(sys, "stdlib_module_names", ()) or ())

    def _norm(p: Any) -> str:
        return str(p or "").replace("\\", "/")

    def _find(symbol: str, preferred: list[str]) -> tuple[str | None, list[str] | None]:
        """优先在声明的文件里找；找不到再全局找（宽松，但不静默放过）。

        声明侧是模型/编译器写的文本，两种噪声必须先剥：
          · 结尾空括号 ``insert()``（编译器把 exposes 写成「调用形态」）；
          · ``Database.insert`` 这类类前缀（AST 表同时收 ``Database.insert``
            与裸 ``insert``，裸键在全局兜底里必须能命中）。
        真机 run 20260928-200631：``exposes=["insert()"]`` 逐字比对键 ``insert``
        五轮全败 —— db.py 一直实现正确，却每轮收到 6 条「找不到定义」的假缺陷。
        """
        raw = str(symbol or "").strip()
        bare = re.sub(r"\s*\(\s*\)\s*$", "", raw).strip()
        leaf = bare.rsplit(".", 1)[-1]
        keys: list[str] = []
        for k in (raw, bare, leaf):
            if k and k not in keys:
                keys.append(k)
        for cand in preferred:
            table = actual.get(_norm(cand)) or {}
            for key in keys:
                if key in table:
                    return _norm(cand), table[key]
        for path, table in actual.items():
            for key in (bare, leaf):
                if key in table:
                    return path, table[key]
        return None, None

    for task in tasks:
        tid = str(task.get("id") or "?")
        files = [_norm(f) for f in (task.get("target_files") or []) if f]
        contracts = task.get("contracts") if isinstance(task.get("contracts"), dict) else {}
        # ① 声明提供、实际没定义
        for sym in [str(s) for s in (contracts.get("exposes") or []) if str(s).strip()]:
            out["checked"] += 1
            where, _ = _find(sym, files)
            if where is None:
                msg = f"{tid} 声明要提供 `{sym}`，但产物里找不到它的定义"
                out["problems"].append(msg)
                out["by_file"].setdefault(files[0] if files else "?", []).append(msg)
        # ② 声明要用、目标侧没有（snake-v2 那一类）
        for sym in [str(s) for s in (contracts.get("uses") or []) if str(s).strip()]:
            # **外部依赖不算**：`uses` 里模型经常顺手写标准库/第三方（真机 20260927-123032：
            # `random`、`tkinter.event`），把它们当"项目内符号"去核，报出来的全是假阳性
            # —— 而这条已经进了 `_contract_blockers`（会强制返工），噪声代价很大。
            #
            # 判据演进：最初写的是「带点号 ⇒ 前缀必须在**本批产出**里，否则跳过」。于是
            # 兄弟模块只要这轮没被写进 written（`db.py` 不在列表里），它声明的
            # `db.nonexistent` 就被一起放过了 —— 契约校验直接漏判
            # （`smoke_merge` ⑨「依赖了不存在的接口 ⇒ 判出」长期挂红）。
            # 现在按「**这个前缀是不是可导入的模块**」判：
            #   `tkinter` / `random` 可导入 ⇒ 外部依赖，跳过；
            #   `db` 不可导入、也不在本批产出里 ⇒ 项目内（或模型笔误的模块名），照核。
            head = sym.split(".", 1)[0].strip()
            if head in project_stems:
                pass  # 本项目文件 -> 必须核
            elif patches._importable_module(head):
                continue  # 外部依赖（标准库 / 本环境已装的包）
            elif sym == head and (head in _STDLIB or hasattr(builtins, head)):
                continue  # 裸名字且是标准库/builtin
            out["checked"] += 1
            where, _ = _find(sym, [])
            if where is None:
                msg = f"{tid} 声明要用 `{sym}`，但产物里没有这个符号（跨文件接口对不上）"
                out["problems"].append(msg)
                out["by_file"].setdefault(files[0] if files else "?", []).append(msg)
        # ③ 签名与声明不符
        name, params = _interface_of(task.get("interface") or "")
        if name and params:
            out["checked"] += 1
            where, actual_params = _find(name, files)
            if where is not None and actual_params is not None and len(actual_params) != len(params):
                msg = (
                    f"{tid} 的 `{name}` 签名与方案不符：方案声明 {len(params)} 个参数"
                    f"（{task.get('interface')}），实际 {len(actual_params)} 个（{where}）"
                )
                out["problems"].append(msg)
                out["by_file"].setdefault(where, []).append(msg)
    if not out["checked"]:
        # **没得比**必须说清楚：真机 run 20260927-002903 里 5 张施工图的
        # interface/contracts 全是空的，比对返回 0 条问题 —— 那不是"接口都对得上"，
        # 是**根本没有可比的声明**。沉默通过比报错更危险。
        out["unresolved"].append(
            f"{len(tasks)} 张施工图都没有声明 interface / contracts ⇒ 跨文件契约无从比对"
        )
    return out


def _instance_attrs(node: ast.ClassDef) -> list[str]:
    """类里 ``self.x = ...`` / ``self.x: T = ...`` 形式的实例属性（去重、保序）。

    这一项是关键：属性不存在（``'GameLogic' object has no attribute 'score'``）
    是 ast 层最难自查、而运行必炸的一类 —— 真机 run snake-v2 就是它。
    """
    seen: list[str] = []
    for sub in ast.walk(node):
        target: ast.expr | None = None
        if isinstance(sub, ast.Assign) and len(sub.targets) == 1:
            target = sub.targets[0]
        elif isinstance(sub, ast.AnnAssign):
            target = sub.target
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in ("self", "cls")
            and target.attr not in seen
        ):
            seen.append(target.attr)
    return seen


def api_digest(
    work: Path, written: list[str], *, max_files: int = 40, max_members: int = 28
) -> dict[str, list[str]]:
    """把本轮产出文件里**对外可用的接口**摘成短清单：``{相对路径: [成员签名, ...]}``。

    为什么需要它：新建项目的**第一次 dev** 既没有检索池（``pool=0``）也没有沙箱
    （``verify/work`` 还不存在），是在一片空白里一次写出全部文件的 —— 于是
    「**同一次生成内部前后不一致**」成了最难自查的缺陷。真机 run snake-v2：
    ``ui.py`` 读 ``self.game_logic.score``，而 ``game_logic.py`` 里没有这个属性，
    ``AttributeError`` 一直到 verify 真跑入口才暴露（那是整整一轮之后）。

    而重问时喂回去的 ``current_code`` 是**本轮开始之前**的正文，不是刚产出的那一版 ——
    模型拿到的是「旧代码 + 新问题」，对不上号。这份摘要给的正是
    「**你刚刚写出来的接口到底长什么样**」，且只占几十行预算。

    刻意只收「别人会引用的东西」：模块级 def/class 的签名 + 类的实例属性（见
    :func:`_instance_attrs`）。实现细节不进摘要 —— 摘要是给模型当准绳用的，不是 code review。
    """
    out: dict[str, list[str]] = {}
    for rel in [str(w) for w in written][:max_files]:
        if not rel.endswith(_STATIC_SUFFIX):
            continue
        path = work / rel
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError, ValueError):
            continue
        members: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                members.append(f"def {node.name}{_sig_args(node.args, drop_self=False)}")
            elif isinstance(node, ast.ClassDef):
                bases = ", ".join(ast.unparse(b) for b in node.bases)
                members.append(f"class {node.name}" + (f"({bases})" if bases else ""))
                attrs = _instance_attrs(node)
                if attrs:
                    members.append("    实例属性: " + ", ".join(attrs))
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        members.append(f"    def {sub.name}{_sig_args(sub.args, drop_self=True)}")
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id.isupper():
                        members.append(f"{tgt.id} = {ast.unparse(node.value)[:60]}")
        if members:
            out[rel] = members[:max_members]
    return out


def skeleton_digest(skeleton: Any) -> dict[str, list[str]]:
    """把「方案·接口骨架」产物转成与 :func:`api_digest` **同形**的摘要。

    同形是刻意的：这样一份基准可以直接被现成的三处消费方复用 ——
      · ``prompts.api_digest_block``（钉进提示词当准绳）；
      · :func:`digest_arities`（测试命令的参数个数核对）；
      · :func:`contract_check`（跨文件契约校验的基准）。
    不需要第二套表示，也就不会出现「两处表示不一致」。

    产出与 ``api_digest`` 逐行对齐：``class X`` / ``    实例属性: a, b`` / ``    def m(p)`` /
    ``def f(p)`` —— 这样 :func:`digest_arities` 的缩进语义（0 = 模块级、带缩进 = 类方法）
    对两者完全一致。

    **类方法要去掉开头的 ``self``/``cls``**：``api_digest`` 是从 AST 取的、天然不含 self，
    而骨架是模型写的、常把 ``self`` 写进来；不去掉会让参数个数核对多算一个，把合法命令
    误判成「参数不足」。
    """
    out: dict[str, list[str]] = {}
    if not isinstance(skeleton, dict):
        return out

    def _strip_self(params: str) -> str:
        text = str(params or "").strip()
        m = re.match(r"\s*(self|cls)\s*(?:,|$)", text)
        if not m:
            return text
        return text[m.end():].strip()

    for item in skeleton.get("files") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").replace("\\", "/").strip()
        if not path:
            continue
        lines: list[str] = []
        for cls in item.get("classes") or []:
            if not isinstance(cls, dict) or not str(cls.get("name") or "").strip():
                continue
            name = str(cls["name"]).strip()
            bases = str(cls.get("bases") or "").strip()
            lines.append(f"class {name}" + (f"({bases})" if bases else ""))
            attrs = [str(a).strip() for a in (cls.get("attributes") or []) if str(a or "").strip()]
            if attrs:
                lines.append("    实例属性: " + ", ".join(attrs))
            for m in cls.get("methods") or []:
                if isinstance(m, dict) and str(m.get("name") or "").strip():
                    params = _strip_self(str(m.get("params") or ""))
                    lines.append(f"    def {str(m['name']).strip()}({params})")
        for fn in item.get("functions") or []:
            if isinstance(fn, dict) and str(fn.get("name") or "").strip():
                lines.append(f"def {str(fn['name']).strip()}({str(fn.get('params') or '').strip()})")
        for c in item.get("constants") or []:
            if str(c or "").strip():
                lines.append(str(c).strip())
        if lines:
            out[path] = lines
    return out


def test_modules(work: Path, written: list[str]) -> list[str]:
    """本轮产出里**测试模块**的模块名（``python -m unittest`` 能直接吃的形式）。

    只认两种命名约定：``test_*.py`` 与 ``*_test.py``。
    **刻意不认** ``tests/`` 目录下的任意文件名 —— 那些未必是用例，乱跑会得到
    一堆与产物无关的失败，把真正要修的东西淹掉。
    """
    out: list[str] = []
    for rel in written:
        name = Path(str(rel)).name
        if not name.endswith(_STATIC_SUFFIX):
            continue
        stem = name[: -len(_STATIC_SUFFIX)]
        if not (stem.startswith("test_") or stem.endswith("_test")):
            continue
        module = _module_name(work, str(rel))
        if module and module not in out:
            out.append(module)
    return out


def demo_command(work: Path, written: list[str]) -> str | None:
    """控制台「演示」该跑哪条命令：优先带 `__main__` 的交付文件，其次约定入口名。

    返回的命令会经 ``run_command`` 同一套安全约定（白名单 / 危险片段 / 超时），
    而且只在沙箱里跑 —— 与 verify 唯一的区别是用途（给人看）而非放宽限制。
    """
    entries = entry_files(work, written)
    if entries:
        return f"{_python_bin()} {entries[0]}"
    for name in ENTRY_NAMES:
        if (work / name).is_file():
            return f"{_python_bin()} {name}"
    return None


def runnability_problems(work: Path, written: list[str], commands: list[dict]) -> list[str]:
    """「产物到底跑起来过没有」—— 光 rc=0 不算证据。

    真机 run 20260924-235001：verify 三条命令全 ok、判定 pass，
    但其中真正执行交付物的那条 `python game_loop.py` 是**空跑** —— 文件没有入口，
    定义完类就退出，退出码 0、输出为空。剩下两条是 `py_compile` 与 import 检查，
    都只证明「能解析 / 能导入」，证明不了「能运行」。

    规则：必须存在一条命令，要么**真的执行了带入口的交付文件**，要么**产生了非空输出**
    （跑了 pytest 也算）。两者都没有 ⇒ 本轮没有任何「产物可运行」的证据，判为问题。

    只对**多文件**产出生效：单文件无法区分「库模块」与「脚本」，不判负（见函数内注释）。
    """
    py_written = [w for w in written if str(w).endswith(_STATIC_SUFFIX)]
    if len(py_written) < 2:
        # **单文件不判负**：它可能是库模块（本该被 import 而不是直接执行），
        # 判负等于逼开发去补一个它并不需要的入口 —— 正是 §21 修掉的
        # 「改不动却一直返工」（真机 run 20260924-185507 刚踩过）。
        # 这种情况沿用 entry_script_problems 的 note：评审与人工都看得到，但不阻断。
        return []
    entries = entry_files(work, written)
    named = [n for n in ENTRY_NAMES if (work / n).is_file()]
    evidence = False
    for cmd in commands:
        status = str(cmd.get("status"))
        if str(cmd.get("source")) in ("syntax", "import"):
            continue  # 内置的语法/导入检查：证明不了运行行为
        # **超时也算跑起来了**：长时间运行的程序（游戏主循环、服务）本来就不会自己退出。
        # 真机 run 20260925-110258：贪吃蛇的 `python main.py` 跑满 180s 被强杀 —— 那正是
        # 「它真的在运行」的最好证据；把它当成失败会让**任何常驻程序**永远过不了 verify。
        if status == "timeout" and runs_guarded_entry(work, written, str(cmd.get("command") or "")):
            evidence = True
            break
        if status != "ok":
            continue
        # 必须跑的是**带守卫的真入口**：光文件名叫 main.py 不够（可能只是函数库，
        # 定义完静默 rc=0）。真机 run 20260928-200631 即此形态的假绿。
        if runs_guarded_entry(work, written, str(cmd.get("command") or "")):
            evidence = True
            break
        if (str(cmd.get("stdout_tail") or "")).strip():
            evidence = True
            break
    if evidence or not py_written:
        return []
    if not entries and not named:
        return [
            f"交付物没有可执行入口：{len(py_written)} 个 .py 文件里没有任何 "
            "`if __name__ == '__main__':`，也不存在 "
            + " / ".join(ENTRY_NAMES)
            + "。直接执行这类文件退出码也是 0（定义完就退出），证明不了产物能运行。"
        ]
    if not entries and named:
        return [
            "存在约定入口名文件 "
            + " / ".join(named)
            + "，但文件里没有 `if __name__ == '__main__':` 守卫：直接执行只是"
            "「定义完函数就退出」（rc=0、无输出），本轮没有任何命令真正跑起过产物。"
        ]
    return [
        "没有任何命令真正执行了交付物：已执行的命令只覆盖语法/导入检查，"
        "或执行了不带入口的文件（空跑）。产物是否可运行未被验证。"
    ]


def audit_impact(work: Path, written: list[str], impl: dict | None = None) -> dict[str, Any]:
    """**纯静态**扫描「谁在调用本轮被改的符号」（ast 解析，零执行、零模型调用）。

    为什么必须有它：:func:`audit_interfaces` 只核对**本轮产出文件之间**的 import 契约，
    对存量代码里「谁在调用这次被改的东西」完全不看。而增量开发最致命的失败形态
    恰恰在这里 —— 补丁本身逻辑没问题，但改了签名 / 参数 / 返回值之后**上游调用者崩了**，
    回归用例却因为「不知道该测哪些上游」而测不到点上
    （真机 run 20260925-184300：回归用例 target 与补丁符号只对得上 1/5）。

    只做确定性判定：某文件 import 了这个符号、或出现了对它的调用 ⇒ 记为上游调用点。
    解析不了的文件只记「无法解析」，**绝不**据此断言（宁可漏报，不要误报）。

    **刻意只产出清单、不下结论**：引用存在 ≠ 上游会崩（可能参数兼容、可能是同名变量）。
    误报的代价是一整轮返工（dev+test+verify+review ≈5 分钟 + 一次 14B 评审），
    所以结论交给评审与人工，本函数只负责把事实摆出来。
    """
    # 被改的符号：取开发明确声明的 target_symbol。
    # 刻意**不**把产出文件里的所有顶层符号都算进来 —— 那会把「产出内部互相调用」
    # 全当成影响面，噪音淹没真正的存量上游。
    symbols: set[str] = set()
    for edit in ((impl or {}).get("edits") or []):
        if isinstance(edit, dict):
            name = str(edit.get("target_symbol") or "").strip()
            if name:
                symbols.add(name)
    if not symbols:
        return {
            "symbols": [], "callers": [], "scanned_files": 0,
            "unparsable": [], "note": "没有声明 target_symbol，无从扫描影响面",
        }

    written_set = {str(w).replace("\\", "/") for w in written}
    callers: list[dict[str, Any]] = []
    unparsable: list[str] = []
    scanned = 0
    for path in sorted(work.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(work).as_posix()
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=rel)
        except (SyntaxError, ValueError):
            unparsable.append(rel)
            continue
        scanned += 1
        lines = source.splitlines()
        is_new = rel in written_set

        # 把本轮变量显式绑成默认参数：_record 只在本轮内被同步调用（下面紧接着就用），
        # 但「闭包捕获循环变量」本身是缺陷模式 —— 绑定后既消除隐患，也避免以后有人
        # 把 _record 存起来延后调用时踩到晚绑定。
        def _record(node: ast.AST, kind: str, hit: str,
                    lines: list[str] = lines, rel: str = rel, is_new: bool = is_new) -> None:
            lineno = getattr(node, "lineno", 0) or 0
            snippet = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
            callers.append({
                "symbol": hit, "file": rel, "lineno": lineno,
                "kind": kind, "context": snippet[:120],
                "in_this_round": is_new,
            })

        for node in ast.walk(tree):
            # 调用：f(...) 或 obj.f(...)
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in symbols:
                    _record(node, "call", func.id)
                elif isinstance(func, ast.Attribute) and func.attr in symbols:
                    _record(node, "call", func.attr)
            # 导入：`from m import X` / `import m`（名字命中即记，类型由调用点补）
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    base = (alias.asname or alias.name).split(".")[0]
                    if base in symbols:
                        _record(node, "import", base)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    base = alias.asname or alias.name.split(".")[0]
                    if base in symbols:
                        _record(node, "import", base)
    # 去重（同一文件同一行的同名命中可能来自 import 与 call 两条路径）
    seen: set[tuple] = set()
    unique: list[dict[str, Any]] = []
    for item in callers:
        key = (item["symbol"], item["file"], item["lineno"], item["kind"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    out: dict[str, Any] = {
        "symbols": sorted(symbols),
        "callers": unique,
        "scanned_files": scanned,
        "unparsable": unparsable,
    }

    # ---- LSP 增强：补上 ast 的两处硬伤 ----
    # 2026-09-25 实测对拍（同一份代码跑两边）纠正了一个想当然的判断：
    # ast **能**找到 `h = make_game(); h.move(2)` 这种「间接实例」调用 —— 它匹配的是
    # `.move` 这个属性名，不关心 h 的类型。LSP 真正的增量是另外两条：
    #   1. **排除同名不同物**：`u.move(3)` 里的 u 是 Unrelated，ast 照样记成上游（误报），
    #      LSP 靠类型推断把它排除掉了 —— 这才是对「别让评审被假上游带偏」最有用的一条；
    #   2. **补别名 import**：`from game import Game as G` 里头名字是 G，ast 匹配不到。
    # 做成**可选增强**：探测不到 langserver 或超预算就只留 ast 结果，绝不阻断。
    targets = [
        (str(edit.get("path") or "").replace("\\", "/"), str(edit.get("target_symbol") or ""))
        for edit in ((impl or {}).get("edits") or [])
        if isinstance(edit, dict)
        and str(edit.get("path") or "").strip()
        and str(edit.get("target_symbol") or "").strip()
    ]
    if targets:
        lsp_result = lsp.find_references(work, targets)
        lsp_pairs = {
            (str(item.get("symbol")), str(item.get("ref_file")), int(item.get("ref_line") or 0))
            for item in (lsp_result.get("references") or [])
        }
        # 双向对拍：两个方向都有信息量，别只算一个。
        #   extra    = LSP 有而 ast 没有 → ast 漏掉的（别名 import 之类）
        #   ast_only = ast 有而 LSP 没有 → **疑似同名误报**，评审据此降权判断
        lsp_result["extra"] = [
            item
            for item in (lsp_result.get("references") or [])
            if (str(item.get("symbol")), str(item.get("ref_file")), int(item.get("ref_line") or 0))
            not in {(str(i["symbol"]), str(i["file"]), int(i["lineno"])) for i in unique}
        ]
        lsp_result["ast_only"] = [
            item
            for item in unique
            if (str(item["symbol"]), str(item["file"]), int(item["lineno"])) not in lsp_pairs
        ]
        out["lsp"] = lsp_result
    return out


def audit_interfaces(work: Path, written: list[str]) -> dict[str, Any]:
    """**纯静态**核对产出文件之间的 import 契约（ast 解析，零执行、零模型调用）。

    为什么必须有它：执行类检查会被**语法错短路**。真机 run 20260924-185507 里
    `renderer.py` 内容残缺（`print(f'{`）导致 `game_logic` 连 import 都失败，
    于是「某个文件从别处导入了不存在的符号」这类问题被完全掩盖 —— 只能等下一轮
    把渲染器修好之后再炸一次，而每轮 ≈5 分钟 + 一次 14B 评审。
    静态核对不依赖执行顺序，一次把所有文件的 import 契约**列全**。

    只做确定性判定：`from M import A` 且 M 就在本轮产出里 ⇒ A 必须真实存在于 M。
    解析不了的文件只记「无法解析」，**绝不**据此断言它缺符号（宁可漏报，不要误报）。
    """
    modules: dict[str, set[str]] = {}
    defs: dict[str, set[str]] = {}
    bound: dict[str, set[str]] = {}
    trees: dict[str, ast.Module] = {}
    #: 模块名 → 沙箱相对路径（结构化缺陷归因要精确指到**定义方文件**）
    rel_of: dict[str, str] = {}
    unparsable: list[str] = []
    for rel in written:
        if not str(rel).endswith(_STATIC_SUFFIX):
            continue
        path = work / rel
        if not path.is_file():
            continue
        module = _module_name(work, rel)
        if not module:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source, filename=str(rel))
        except SyntaxError as exc:
            unparsable.append(f"{rel}（第 {exc.lineno} 行：{exc.msg}）")
            continue
        trees[module] = tree
        rel_of[module] = str(rel).replace("\\", "/")
        names: set[str] = set()
        defined: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
                        defined.add(target.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
        modules[module] = names
        defs[module] = defined
        bound[module] = _bound_names(tree)

    missing: list[str] = []
    missing_details: list[dict] = []
    for module, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level:
                continue  # 相对导入不做判定（解析口径太脆）
            target = str(node.module or "")
            base = target.split(".")[0]
            if base not in modules:
                continue  # 不是本轮产出的模块（第三方/存量），交给导入检查去报
            for alias in node.names:
                if alias.name == "*" or alias.name in modules[base]:
                    continue
                missing.append(
                    f"{module}.py 里 `from {target} import {alias.name}`，"
                    f"但 {base}.py 并没有定义 {alias.name}"
                )
                # **结构化**明细：归因不能只靠正则解析上面那句人读文本
                # （真机 run 20260928-221831：评审把根因误归到导入方 cli.py，
                # 真正的肇事方 database.py 三轮没被派活）。
                missing_details.append(
                    {
                        # 写这行 import 的文件（受害方；它本身没写错）
                        "importer": rel_of.get(module, f"{module}.py"),
                        # 被导入的模块名 / **应该定义该符号的文件**（根因方）
                        "module": base,
                        "definer": rel_of.get(base, f"{base}.py"),
                        "name": alias.name,
                        "line": int(getattr(node, "lineno", 0) or 0),
                    }
                )
    return {
        "files": sorted(modules),
        "definitions": {key: sorted(value) for key, value in sorted(modules.items())},
        "unparsable": unparsable,
        "missing_symbols": sorted(set(missing)),
        # 去重：同一 (importer, definer, name) 在多处 import 只算一条（line 取首条）
        "missing_symbol_details": [
            {
                "importer": imp,
                "module": mod,
                "definer": defn,
                "name": name,
                "line": line,
            }
            for (imp, mod, defn, name), line in sorted(
                {
                    (d["importer"], d["module"], d["definer"], d["name"]): d["line"]
                    for d in missing_details
                }.items()
            )
        ],
        "undefined_names": _undefined_cross_module(defs, trees, bound),
    }


# --------------------------------------------------------------------- 执行
def _child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not _SECRET_HINT.search(k)}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # 无显示环境下让 pygame/SDL 程序也能跑：渲染变空操作，但主循环与逻辑照常执行
    env["SDL_VIDEODRIVER"] = "dummy"
    env["SDL_AUDIODRIVER"] = "dummy"
    return env


def run_command(spec: dict, *, cwd: Path, timeout: int, allowed_bins: frozenset[str],
                deny_patterns: tuple[str, ...], mock: bool = False) -> dict:
    """执行一条命令并采集结果（不经过 shell；被拒绝/超时/mock 都如实记录）。"""
    command = str(spec.get("command") or "")
    out: dict[str, Any] = {
        "command": command,
        "source": spec.get("source") or "",
        "display": spec.get("display") or "",
        "status": "skipped",
        "exit_code": None,
        "duration_s": 0.0,
        "stdout_tail": "",
        "stderr_tail": "",
        "reason": "",
    }
    if mock:
        out["reason"] = "mock 运行：不执行真实命令（只计划）"
        return out
    reason = reject_reason(command, allowed_bins, deny_patterns)
    if reason:
        out["reason"] = reason
        return out
    argv = _split_command(command)
    started = time.time()
    try:
        proc = subprocess.run(  # noqa: S603
            argv,
            cwd=str(cwd),
            env=_child_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        out["exit_code"] = proc.returncode
        out["status"] = "ok" if proc.returncode == 0 else "fail"
        out["stdout_tail"] = (proc.stdout or "")[-OUTPUT_TAIL:]
        out["stderr_tail"] = (proc.stderr or "")[-OUTPUT_TAIL:]
    except subprocess.TimeoutExpired as exc:
        out["status"] = "timeout"
        out["reason"] = f"超过 {timeout}s 未结束（已终止）"
        out["stdout_tail"] = _as_text(exc.stdout)[-OUTPUT_TAIL:]
        out["stderr_tail"] = _as_text(exc.stderr)[-OUTPUT_TAIL:]
    except FileNotFoundError as exc:
        # **程序不可用 ≠ 交付物有问题**：命令指向的程序在本环境里不存在（真机
        # run 20260925-120354：测试阶段声明了 `pytest`，沙箱里根本没装）。
        # 这类失败由「命令产出方」（测试阶段）负责，开发改不动它 —— 与入口脚本的
        # 「空跑」同一类，故单独归类，不参与交付物成败判定。
        out["status"] = "unavailable"
        out["reason"] = (
            f"本环境没有这个程序（{exc}）。命令由测试阶段产出，属**命令质量问题**，"
            "不是交付物的缺陷；请改成本环境可直接执行的命令（优先 Python 标准库，如 "
            "`python -m unittest`），或把依赖写进方案并先安装。"
        )
    except (OSError, ValueError) as exc:
        out["status"] = "error"
        out["reason"] = f"{type(exc).__name__}: {exc}"
    out["duration_s"] = round(time.time() - started, 2)
    return out


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


# --------------------------------------------------------------------- 对外入口
#: 覆盖率门槛。**只在能从真实输出里摘到数字时才判**（摘不到就老实归入"未验证"）——
#: 非 Python 项目、没装覆盖率工具的项目，硬套一个门槛只会得到假阴性。
COVERAGE_MIN_PCT = 80.0
#: 断言型命令的识别口径（与 orchestrator._audit_test 同源，两处别漂移）
_ASSERT_HINTS = ("assert", "unittest", "pytest", "doctest")
#: 负向对照最多重跑几条断言（每条都是秒级命令；上限是为了别把一轮 verify 拖长）
NEGATIVE_CONTROL_MAX = 2
_COVERAGE_PATTERNS = (
    re.compile(r"(?im)^TOTAL\s+\d+\s+\d+\s+(\d+(?:\.\d+)?)%"),          # pytest-cov / coverage
    re.compile(r"(?i)all files[^\n]*?\|\s*(\d+(?:\.\d+)?)"),            # jest --coverage / nyc
    re.compile(r"(?i)\bcoverage[:=]?\s*(\d+(?:\.\d+)?)\s*%"),           # 通用 "coverage: 87%"
    re.compile(r"(?i)total coverage[^\n]*?(\d+(?:\.\d+)?)"),            # go test -cover
)


def coverage_fact(commands: list[dict]) -> dict[str, Any]:
    """从**命令的真实输出**里摘覆盖率数字。摘不到就说摘不到 —— 绝不给估算值。

    为什么要单独做：覆盖率是"测试够不够"的唯一量化口径，但它**必须来自实测**。
    本项目不接入覆盖率工具（技术栈无关）：有数字就判门槛，没数字就进"未验证项"清单
    由人工判断能否接受。两种都诚实，唯独"估一个数"不行。
    """
    for cmd in commands or []:
        text = f"{cmd.get('stdout_tail') or ''}\n{cmd.get('stderr_tail') or ''}"
        for pattern in _COVERAGE_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            try:
                pct = float(match.group(1))
            except (TypeError, ValueError):
                continue
            if 0.0 <= pct <= 100.0:
                return {"percent": pct, "command": str(cmd.get("command") or ""),
                        "matched": match.group(0).strip()[:80]}
    return {"percent": None, "command": "", "matched": ""}


def coverage_below_threshold(
    coverage: dict[str, Any] | None, minimum: float = COVERAGE_MIN_PCT
) -> str:
    """覆盖率低于门槛时给出一句可读判断；不达标才返回文本，否则返回 ""。

    处置口径：**提示级、不判负**。覆盖率低的可修面在"补用例"（测试阶段），
    而阻断级返工会把这件事压到开发头上（它改不动测试），正是 §21 修掉的
    「改不动却一直返工」。把它摆给评审与人工，由人决定补还是接受。

    单独抽成函数是为了**能离线断言**：真机上要出现"有覆盖率数字且低于门槛"这一支，
    得同时满足"测试阶段声明了覆盖率命令 + 本环境装了该工具 + 跑出来真的低于 80%"，
    三件事凑齐很难 —— 抽出来就能用合成数据把这一支钉死。
    """
    pct = (coverage or {}).get("percent")
    if pct is None:
        return ""
    try:
        value = float(pct)
    except (TypeError, ValueError):
        return ""
    if value >= float(minimum):
        return ""
    return (
        f"覆盖率 {value:g}% 低于门槛 {float(minimum):g}%"
        f"（实测，来自 `{(coverage or {}).get('command')}`）—— 用例覆盖面偏窄，"
        "请判断是补用例还是接受为已知缺口"
    )


def negative_control(
    work: Path,
    repo: str | Path | None,
    written: list[str],
    commands: list[dict],
    *,
    timeout: int,
    allowed_bins: frozenset[str],
    deny_patterns: tuple[str, ...],
    mock: bool = False,
) -> dict[str, Any]:
    """**负向对照**（Red→Green→Red 的可做版）：把补丁撤掉之后，那些断言还过得去吗？

    为什么要它：`跑通 ≠ 正确`。一条断言如果在**没有本次改动**的代码上也能通过，那它对
    这次交付**没有任何判别力** —— 它证明的是既有行为，不是新行为。真机上"测试全绿、
    功能没实现"就是这么漏过去的。

    做法（刻意便宜）：不重建沙箱，只把本次**写入的文件**按来源还原（新增的删掉、
    修改的用仓库原文覆盖），再重跑**断言型**命令。断言型命令都是秒级的
    `python -c "assert …"`，不碰常驻/图形入口，所以不会引入分钟级开销。

    处置是**提示级**、且附给评审：有些断言本来就该在改动前后都成立（不变量、既有契约的
    回归保护），把它判成阻断会让开发去改一条本来就正确的断言 —— 那又是一轮无谓返工。

    任何一步出错都**静默跳过**：这是附加检查，绝不能因为它自己出问题而影响交付判定。
    """
    if mock or not written:
        return {"checked": 0, "no_power": [], "skipped": "mock 运行 / 没有写入文件"}
    targets = [
        c for c in (commands or [])
        if str(c.get("status")) == "ok"
        and any(h in str(c.get("command") or "").lower() for h in _ASSERT_HINTS)
    ]
    if not targets:
        return {"checked": 0, "no_power": [], "skipped": "本轮没有通过的断言型命令"}
    repo_path = Path(repo) if repo else None
    backups: list[tuple[Path, bytes | None]] = []
    no_power: list[str] = []
    checked = 0
    try:
        for rel in list(written)[:80]:
            dest = work / rel
            if not dest.exists():
                continue
            backups.append((dest, dest.read_bytes()))
            src = (repo_path / rel) if repo_path else None
            if src is not None and src.is_file():
                dest.write_bytes(src.read_bytes())
            else:
                dest.unlink(missing_ok=True)
        for spec in targets[:NEGATIVE_CONTROL_MAX]:
            checked += 1
            row = run_command(
                spec, cwd=work, timeout=min(int(timeout), 60),
                allowed_bins=allowed_bins, deny_patterns=deny_patterns, mock=False,
            )
            if row.get("status") == "ok":
                no_power.append(str(spec.get("command")))
    except Exception as exc:  # noqa: BLE001 - 附加检查，坏了也不能影响判定
        return {"checked": checked, "no_power": no_power,
                "skipped": f"负向对照未完成：{type(exc).__name__}: {exc}"}
    finally:
        for dest, data in backups:
            try:
                if data is None:
                    dest.unlink(missing_ok=True)
                else:
                    dest.write_bytes(data)
            except OSError:
                pass
    return {"checked": checked, "no_power": no_power, "skipped": ""}


#: 与断言型命令无关的通用未验证项。**刻意固定列出来**：覆盖率/性能/并发这类东西，
#: 一条「未接入」就代表「没验」，写成必列项才不会随着报告变"干净"而被忘掉。
GENERIC_UNVERIFIED = (
    "性能与并发副作用未测量",
)


def unverified_claims(
    work: Path,
    written: list[str],
    executed: list[dict],
    test_report: dict | None,
    coverage: dict[str, Any] | None = None,
) -> list[str]:
    """**显式列出本轮「没有验证到」的事项**（交付前强制披露）。

    为什么必须单列：流水线最容易出的错不是"报错"，而是**没验的部分被当成验过了** ——
    报告一片安静，人工以为都过了。真机上的对应现象：8 轮全部"通过"，却没人发现入口丢了、
    方向控制消失。一句"已验证"背后到底验了什么、没验什么，必须能被机器列出来，
    否则「该不该放行」只能靠感觉。

    口径：只列**事实**（没有断言命令 / 找不到测试文件 / 未接入覆盖率），不写评价。
    """
    out: list[str] = []
    if not executed:
        out.append("没有任何命令被实际执行：交付物「能不能跑」没有被验证")
    elif all(str(c.get("status")) in ("skipped", "unavailable") for c in executed):
        out.append("所有命令都未真正执行（被安全约定跳过或程序不存在）：没有任何实测证据")
    if not any("assert" in str(s.get("command") or "") for s in executed):
        out.append("没有断言型命令：退出码 0 只能说明「启动了」，不能说明「行为正确」")
    if not test_modules(work, written):
        out.append("沙箱里找不到可识别的测试文件：回归能力未被验证")
    if not (test_report or {}).get("automated_commands"):
        out.append("测试阶段没有声明 automated_commands：测试方案可能只是文字描述")
    # 覆盖率：**有实测数字就不算"未验证"**（门槛判定在 verify 里单独做）；
    # 没数字才是"未验证" —— 这两件事必须分开，否则要么撒谎（假装达标），
    # 要么把已经量化的东西又说成"没验"。
    if (coverage or {}).get("percent") is None:
        out.append(
            "覆盖率未测量：本轮输出里没有可解析的覆盖率数字（未接入覆盖率工具的项目属正常），"
            "因此不给数字，也不假装达标"
        )
    out.extend(GENERIC_UNVERIFIED)
    return out


def verify(
    run_dir: Path,
    repo: str | Path | None,
    impl: dict | None,
    audit: dict,
    test_report: dict | None,
    *,
    enabled: bool = True,
    timeout: int = 180,
    max_commands: int = 5,
    copy_limit_mb: int = 1500,
    skip_dirs: frozenset[str] = frozenset(),
    allowed_bins: frozenset[str] = frozenset(),
    deny_patterns: tuple[str, ...] = (),
    mock: bool = False,
    project_type: str = "secondary",
) -> dict:
    """物化 → 计划 → 执行 → 汇总结论。返回可直接落盘/进 prompt 的 verify_report。"""
    report: dict[str, Any] = {
        "verdict": "skipped",
        "summary": "",
        "sandbox": "",
        "materialized": [],
        "commands": [],
        "problems": [],
        "notes": [],
        # 未验证项（强制披露）：见 unverified_claims
        "unverified": [],
        # Proof Gate 事实（规格§二十）：executed=False 时 verdict 必然不是 PROVEN，
        # review 语义 pass 也必须被机械闸门拦下。
        "executed": False,
        "reason_code": "",
        "baseline_dir": "",
    }
    if not enabled:
        report["notes"].append("运行验证已关闭（PIPELINE_VERIFY=0）")
        report["summary"] = "未启用运行验证"
        report["reason_code"] = "disabled"
        return report
    if not impl or not (impl.get("edits") or []):
        report["notes"].append("没有实现产物（dev 阶段未跑或没产出补丁），无可验证内容")
        report["summary"] = "没有可验证的实现产物"
        report["reason_code"] = "no_implementation"
        return report

    repo_path = Path(repo) if repo else None
    if not audit.get("source_available"):
        # 注意：source_available=True 但 repo 目录不存在（truthy Path）是历史既有的
        # 「空基线」调用约定（analyze_all 按新增合并、materialize 走 _no_repo），
        # 下游 smoke 与内部调用在用 —— 不能拦。这里只处理**明确没有 repo**（None）。
        #
        # 新建项目（规格§二十 P0-2）：没有仓库不是"无法验证"的理由 —— 自动建立
        # ``runs/<id>/verify/base/`` 空基线，把所有 add 当新文件物化进沙箱后**真跑**。
        # mock 不走这条（mock 只计划不执行，空基线物化会改变它既有产物形态）。
        if project_type == "new" and not mock:
            baseline = Path(run_dir) / "verify" / "base"
            # analyze 需要 truthy 路径：文件在基线下不存在 ⇒ add 全部按新增合并，
            # 返工轮同一文件的 modify 也以"本份 edits 的合并结果"为基准（见 analyze_all 注释）。
            # 注意 materialize 会整体清空 verify/，audit 文本在此处一次性承载，基线目录
            # 在物化完成后重建（见下方），保证 runs/<id>/verify/base/ 稳定可查。
            audit = patches.analyze_all(baseline, impl)
            repo = None  # 物化走 _no_repo 只读分支；内容以 audit 文本为准
            report["baseline_dir"] = str(baseline)
            report["notes"].append(
                "新建项目无仓库基线：已建立空基线 verify/base/，补丁按新增文件物化为完整工作区后真跑"
            )
        else:
            # 二次开发无仓库：可以 skipped，但 Proof Gate 会在 review 处机械阻断 pass
            # （skipped != PROVEN；规格§二十）。
            report["notes"].append(
                "没有提供仓库路径：补丁无法核对与物化，运行验证无法进行"
                "（新建项目请把生成目录作为 --repo 传入）"
            )
            report["summary"] = "没有仓库路径，运行验证无法进行"
            report["reason_code"] = "no_repo"
            return report

    mat = materialize(run_dir, repo, impl, audit, copy_limit_mb=copy_limit_mb, skip_dirs=skip_dirs)
    if report["baseline_dir"]:
        # materialize 整体清空过 verify/，这里把空基线目录重建为稳定锚点。
        Path(report["baseline_dir"]).mkdir(parents=True, exist_ok=True)
        # 交给编排器回写 state.patch_audit（落盘前由编排器 pop，不进 verify 产物正文）。
        report["audit_for_state"] = audit
    report["sandbox"] = mat["work"]
    report["materialized"] = list(mat["written"])
    report["notes"].extend(mat["notes"])
    # 物化阶段的问题分两类，处置不同：
    #   · 沙箱不完整（源码没复制进来）⇒ 后面每项检查都失真，结论无论 pass/fail
    #     都不可信，直接判负返回，不再浪费一轮执行去产生误导性错误；
    #   · 补丁没全套上 ⇒ 沙箱**部分**可用，剩余代码仍值得验证（能暴露更多问题），
    #     但记入 blocking 让最终 verdict 必然判负。
    blocking: list[str] = list(mat.get("problems") or [])
    report["problems"].extend(blocking)
    if mat["error"]:
        report["problems"].append(mat["error"])
        report["verdict"] = "fail"
        report["summary"] = mat["error"]
        return report
    if mat.get("sandbox_incomplete"):
        report["verdict"] = "fail"
        report["summary"] = "沙箱不完整，验证结论不可信（源码未被完整复制）"
        return report

    work = Path(mat["work"])
    # 有该落盘的补丁、却一条都没写成 ⇒ 这不是「没有可验证内容」，而是**明确的失败**：
    # 交付物根本没成型（多半是补丁机械校验判负被跳过，见 report.notes）。
    # 之前这种情况会落进"没有可执行的验证命令"、verdict 停在 skipped —— 把失败说成了"无从验证"。
    writable_edits = [
        e for e in (impl.get("edits") or [])
        if isinstance(e, dict) and str(e.get("change_type") or "") != "delete"
    ]
    if writable_edits and not mat["written"]:
        report["problems"].append("补丁一条都没能落盘（见补丁机械校验）：沙箱里没有可验证的产物")
        report["verdict"] = "fail"
        report["summary"] = "补丁未能落盘，交付物没成型"
        return report

    specs = plan_commands(
        work, list(mat["written"]), test_report, max_commands=max_commands, impl=impl
    )
    if not specs:
        report["notes"].append("没有可执行的命令（测试阶段也没声明 automated_commands）")
        report["summary"] = "没有可执行的验证命令，无法确认能否运行"
        report["reason_code"] = "no_commands"
        return report
    for spec in specs:
        # 常驻类入口（游戏/桌面窗口）跑满完整超时是白等：它的「跑满」本来就被
        # 判成「能跑起来」（见 RESIDENT_ENTRY_TIMEOUT 的注释）。给它一个短超时，
        # 结论不变，一轮省下好几分钟。
        cmd_timeout = timeout
        _script = _python_script_arg(str(spec.get("command") or ""))
        if _script and is_resident_entry(work, _script):
            cmd_timeout = min(timeout, RESIDENT_ENTRY_TIMEOUT)
        report["commands"].append(
            run_command(
                spec,
                cwd=work,
                timeout=cmd_timeout,
                allowed_bins=allowed_bins,
                deny_patterns=deny_patterns,
                mock=mock,
            )
        )

    if mock:
        report["notes"].append("mock 运行：只计划命令、不执行")
        report["summary"] = "mock 运行，未真正执行"
        report["reason_code"] = "mock"
        return report

    # 静态检查（零执行）：不依赖执行顺序，因此**不会**被语法错短路 —— 一次把所有问题列全。
    # 放在这里（mock 早退之后）是刻意的：mock 产物是占位，静态检查对它没有意义。
    interfaces = audit_interfaces(work, list(mat["written"]))
    report["interface_audit"] = interfaces
    # 影响面：谁在调用这次被改的符号。**刻意不计入 problems**（引用存在 ≠ 上游会崩），
    # 只作为事实清单交给评审与人工 —— 见 audit_impact 的注释。
    report["impact_audit"] = audit_impact(work, list(mat["written"]), impl)
    static_problems = [f"跨文件接口不一致：{item}" for item in interfaces["missing_symbols"]]
    static_problems += [f"产出文件无法解析（会掩盖其它问题）：{item}" for item in interfaces["unparsable"]]
    # 「用了别处的东西却没 import」：静态就能查出来，且不会像执行类检查那样被语法错短路
    static_problems += [f"引用未导入：{item}" for item in interfaces["undefined_names"]]
    # 「from X import Y 而 Y 根本不存在」/「产出文件与标准库同名会遮蔽标准库」：
    # 纯 AST 判定，不受执行路径影响 —— 导入探针只覆盖"这一条真的被执行到时"的错。
    static_problems += import_symbol_problems(work, list(mat["written"]))
    # 「产物到底跑起来过没有」：只有 rc=0 但零输出/无入口 ⇒ 视为没有可运行的证据。
    # **单独留一份**：它既可能是产物真有问题，也可能只是**测试命令质量差**（命令写错 ⇒
    # 一条都没真跑起来）。归因要靠"有没有真正的产物失败"来定，见下方的 test_defects / impl_fail。
    runnability = runnability_problems(work, list(mat["written"]), list(report["commands"]))
    static_problems += runnability
    report["problems"].extend(static_problems)
    # 入口脚本的「空跑」只记 note，**不判 fail**：
    #   · 它多半是**测试命令的质量问题**（测试模型顺手写 `python <库模块>.py`），
    #     不是交付物缺陷。判成阻断级会让开发去修一个它无权修的东西（命令是测试阶段产出的），
    #     正是 §21 修掉的「改不动却一直返工」——真机上刚踩过一次（run 20260924-185507：
    #     因为 `input_handler.py`/`renderer.py` 是库模块而被判验证失败）；
    #   · 真·「入口丢了」这种**可改**的缺陷由**返工退化检测**（符号消失）精确兜住。
    report["notes"].extend(entry_script_problems(work, list(report["commands"])))

    executed = [
        c for c in report["commands"]
        if c["status"] in ("ok", "fail", "timeout", "error", "unavailable")
    ]
    # 覆盖率（只在能从真实输出摘到数字时判门槛）与**负向对照**。
    # 负向对照放在这里而不是更早：它会把沙箱里"本次写入的文件"还原成原文，
    # 而上面的静态检查（接口/影响面/可运行性/入口探测）都要看**打完补丁**的沙箱。
    coverage = coverage_fact(executed)
    report["coverage"] = coverage
    note = coverage_below_threshold(coverage)
    if note:
        report["notes"].append(note)
    control = negative_control(
        work, repo, list(mat["written"]), executed,
        timeout=timeout, allowed_bins=allowed_bins, deny_patterns=deny_patterns, mock=mock,
    )
    report["negative_control"] = control
    if control.get("no_power"):
        # 这是"跑通≠正确"的机械证据：撤掉改动后断言还过，说明它没在验证本次交付。
        report["notes"].append(
            "负向对照：以下断言在**撤掉本次改动后依然通过** ⇒ 对这次交付没有判别力"
            "（要么断言写的是既有行为，要么根本没有真正覆盖改动）："
            + "；".join(f"`{c}`" for c in control["no_power"])
        )
    elif control.get("skipped"):
        report["notes"].append(f"负向对照未执行：{control['skipped']}")
    # 常驻程序（游戏主循环 / 服务）跑满超时是**正常现象**，不是失败：它已经启动并持续运行，
    # 这比「退出码 0」更能证明产物能跑。真机 run 20260925-110258 的贪吃蛇 `python main.py`
    # 跑满 180s 被强杀 —— 若把它当失败，任何常驻形态的交付物都永远过不了 verify。
    long_running = [
        c for c in executed
        if c["status"] == "timeout"
        and runs_entry(work, list(mat["written"]), str(c.get("command") or ""))
    ]
    if long_running:
        report["notes"].append(
            "以下命令在超时内持续运行未退出（常驻程序属正常，已视为「能跑起来」并强杀）："
            + "；".join(f"`{c['command']}`" for c in long_running)
        )
    # 「本环境没装这个程序」不算交付物失败：命令是**测试阶段**产出的，开发改不动它。
    # 真机 run 20260925-120354：测试阶段声明 `pytest`，沙箱里没有 → 若计入失败，
    # 开发会为一个它无权修改的东西反复返工（与入口脚本「空跑」同一类问题）。
    unavailable = [c for c in executed if c["status"] == "unavailable"]
    if unavailable:
        report["notes"].append(
            "以下命令因**本环境没有对应程序**而无法执行（命令质量问题，不计入交付物成败）："
            + "；".join(f"`{c['command']}`" for c in unavailable)
        )
    # 「命令自己写错了」同样不算交付物失败 —— 与 unavailable 同理：命令是测试阶段
    # 产出的，开发改不动它。真机 20260927-050907：`snake.Snake()`、`game.Game()`
    # 没传 `__init__` 要求的参数；更早的 20260927-033201 里 `snake.Snake(canvas, ...)`
    # 的 `canvas` 根本没定义。这类命令**必然**失败，计入判负就等于让开发为一件它
    # 无权修改的东西反复返工，一轮都收敛不了。
    def _miswritten(cmd: dict) -> bool:
        if str(cmd.get("source") or "") != "planned":
            return False  # 只豁免测试阶段声明的命令；产物自己的错误照常判负
        if str(cmd.get("status") or "") not in ("fail", "error"):
            return False
        err = f"{cmd.get('stderr_tail') or ''}\n{cmd.get('stdout_tail') or ''}"
        if "required positional argument" in err:
            return True
        # 拿裸 `None` 顶替必需对象：`'NoneType' object has no attribute ...`。
        # **必须**确认命令里真的写了裸 None —— 否则可能是实现自己把属性留成了 None，
        # 那就该判实现（不能一律放过）。
        if "NoneType' object has no attribute" in err and passes_none_literal(str(cmd.get("command") or "")):
            return True
        m = re.search(r"NameError: name '([^']+)' is not defined", err)
        if m and m.group(1) not in {Path(p).stem for p in mat["written"]}:
            return True
        return False

    miswritten = [c for c in executed if _miswritten(c)]
    if miswritten:
        mis_ids = {id(c) for c in miswritten}
        report["notes"].append(
            "以下命令**自身不可执行**（构造参数不足或引用了未定义的名字）—— 属**测试层缺陷**"
            "（命令是测试阶段产出的，开发无权修改），不计入交付物成败，也不得据此要求修改实现签名；"
            "应由测试阶段修正命令："
            + "；".join(f"`{c['command']}`" for c in miswritten)
        )
    else:
        mis_ids: set[int] = set()

    # 裸入口探针的「无参打印用法 + 非零退出」是 CLI 的标准设计（argparse 如此），不是崩溃：
    # 没有 traceback、输出是用法提示 —— 不计入交付物失败。但它同样没证明行为正确，
    # runnability 仍会诚实判「没有可运行证据」，测试阶段必须补带参数的真跑命令。
    usage_exits = [c for c in executed if is_usage_exit(c)]
    usage_ids = {id(c) for c in usage_exits}
    if usage_exits:
        report["notes"].append(
            "以下入口探测**不带参数**执行，程序打印用法提示后以非零码退出（CLI 标准行为，"
            "无 traceback）—— 不视为交付物失败；但无参执行证明不了行为正确，"
            "测试阶段应补带参数的真跑命令："
            + "；".join(f"`{c['command']}`" for c in usage_exits)
        )

    failed = [
        c for c in executed
        if c["status"] not in ("ok", "unavailable")
        and c not in long_running
        and id(c) not in mis_ids
        and id(c) not in usage_ids
    ]

    # ------------------------------------------------------------------ 归因：谁的错？
    # 交付物**自身**的问题（接口不一致 / 无法解析 / 引用未导入 / 命令真的失败 / 补丁没套上）
    # 才算「实现层失败」。剩下的「拿不到可运行证据」如果是被**测试命令写错**拖累的，
    # 就是测试层缺陷 —— 判负依然诚实（确实没验证），但**不能**把责任推给实现。
    impl_static = [p for p in static_problems if p not in runnability]
    impl_fail = bool(failed or blocking or impl_static)
    test_defects: list[str] = [
        f"测试命令自身不可执行（参数不足 / 引用了未定义的名字）：`{c['command']}`"
        for c in miswritten
    ]
    if not impl_fail and runnability:
        test_defects += [f"没有取得可运行证据（测试命令质量问题）：{p}" for p in runnability]
    if not impl_fail and usage_exits:
        test_defects += [
            f"入口无参执行仅打印用法（退出码非零但无 traceback），需补带参数的真跑命令：`{c['command']}`"
            for c in usage_exits
        ]
    report["impl_fail"] = impl_fail
    report["test_defects"] = test_defects
    if test_defects and not impl_fail:
        report["notes"].append(
            "本轮运行验证的失败**全部归因于测试层**（命令自身不可执行 / 无可用运行证据），"
            "实现侧没有任何机械证据表明有问题 —— 请修测试命令，不要改实现签名。"
        )
    # blocking（补丁没全套上）必须参与判定：否则「部分套用 + 剩余代码能跑通」
    # 会落到下面的 elif 分支判成 pass，把残缺的交付物放过去。
    if failed or static_problems or blocking:
        report["verdict"] = "fail"
        report["executed"] = True
        report["reason_code"] = "verify_failed"
        for cmd in failed:
            label = STATUS_CN.get(cmd["status"], cmd["status"])
            report["problems"].append(
                f"{label}：`{cmd['command']}`"
                + (f"（退出码 {cmd['exit_code']}）" if cmd["exit_code"] is not None else "")
                + (f" —— {cmd['reason']}" if cmd["reason"] else "")
            )
    elif any(c["status"] == "ok" for c in executed):
        report["verdict"] = "pass"
        report["executed"] = True
        report["reason_code"] = "verify_passed"
    else:
        report["verdict"] = "skipped"
        report["reason_code"] = report.get("reason_code") or "no_effective_command"
        report["notes"].append(
            "没有任何命令成功执行（被安全约定跳过 / 程序不可用），无法确认产物能否运行"
        )

    ran = len(executed)
    report["summary"] = (
        f"{report['verdict']}：执行 {ran}/{len(report['commands'])} 条命令"
        + (f"，失败 {len(failed)} 条" if failed else "")
        + (f"，静态检查发现 {len(static_problems)} 项问题" if static_problems else "")
        + (f"，物化阶段 {len(blocking)} 项阻断问题" if blocking else "")
    )
    # 未验证项与 verdict 平级输出：verdict=pass 说的是「跑过的都过了」，
    # **不等于**「该验的都验了」。两者必须分开呈现，否则 pass 会被读成"全都验过了"。
    report["unverified"] = unverified_claims(
        work, list(mat["written"]), executed, test_report, coverage
    )
    return report
