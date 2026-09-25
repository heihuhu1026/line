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
        modules = [name for name in (_module_name(work, rel) for rel in py_files) if name]
        if modules and len(specs) < max_commands:
            specs.append(
                {
                    "command": f'{_python_bin()} -c "{_IMPORT_CHECK}" ' + " ".join(modules[:SYNTAX_MAX_FILES]),
                    "source": "import",
                    "display": "导入检查（能抓到语法合法但用了未导入名字的模块）",
                }
            )

    # 开发自己声明的入口命令。为什么值得单独一档：测试阶段的命令常写成
    # `python <库模块>.py`（空跑、rc=0 却什么都没做），而**写代码的人**最清楚该怎么跑。
    # 这条会真的执行 —— 跑不起来就是开发自己的锅，也不能再拿「命令质量问题」推脱。
    dev_run = str((impl or {}).get("run") or "").strip()
    if dev_run and len(specs) < max_commands:
        specs.append({"command": dev_run, "source": "dev-run", "display": "开发声明的入口命令"})

    declared = [
        str(c.get("command") or "").strip()
        for c in (test_report or {}).get("automated_commands") or []
        if isinstance(c, dict) and str(c.get("command") or "").strip()
    ]
    for command in declared:
        if len(specs) >= max_commands:
            break
        specs.append({"command": command, "source": "planned", "display": "测试阶段声明的命令"})

    if len(specs) < max_commands:
        # 兜底探测：只挑「一看就知道怎么跑」的入口，宁缺毋滥
        has_tests = any(
            p.name.startswith("test_") and p.suffix == ".py"
            for p in work.rglob("*.py")
            if "__pycache__" not in p.parts
        ) or (work / "tests").is_dir()
        if has_tests:
            specs.append(
                {"command": f"{_python_bin()} -m pytest -q", "source": "probe", "display": "探测到测试目录"}
            )
        elif (work / "main.py").exists():
            specs.append({"command": f"{_python_bin()} main.py", "source": "probe", "display": "探测到 main.py"})
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
        if status == "timeout" and runs_entry(work, written, str(cmd.get("command") or "")):
            evidence = True
            break
        if status != "ok":
            continue
        if runs_entry(work, written, str(cmd.get("command") or "")):
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
    if not evidence:
        return [
            "没有任何命令真正执行了交付物：已执行的命令只覆盖语法/导入检查，"
            "或执行了不带入口的文件（空跑）。产物是否可运行未被验证。"
        ]
    return []


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

        def _record(node: ast.AST, kind: str, hit: str) -> None:
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
    return {
        "files": sorted(modules),
        "definitions": {key: sorted(value) for key, value in sorted(modules.items())},
        "unparsable": unparsable,
        "missing_symbols": sorted(set(missing)),
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
    }
    if not enabled:
        report["notes"].append("运行验证已关闭（PIPELINE_VERIFY=0）")
        report["summary"] = "未启用运行验证"
        return report
    if not impl or not (impl.get("edits") or []):
        report["notes"].append("没有实现产物（dev 阶段未跑或没产出补丁），无可验证内容")
        report["summary"] = "没有可验证的实现产物"
        return report
    if not audit.get("source_available"):
        # 没有仓库路径时，补丁既无法机械核对也无从物化 —— 与其空转出一堆
        # 「未能套用」，不如直接把原因说清楚（新建项目要把生成目录作为 --repo 传进来）。
        report["notes"].append(
            "没有提供仓库路径：补丁无法核对与物化，运行验证无法进行"
            "（新建项目请把生成目录作为 --repo 传入）"
        )
        report["summary"] = "没有仓库路径，运行验证无法进行"
        return report

    mat = materialize(run_dir, repo, impl, audit, copy_limit_mb=copy_limit_mb, skip_dirs=skip_dirs)
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
        return report
    for spec in specs:
        report["commands"].append(
            run_command(
                spec,
                cwd=work,
                timeout=timeout,
                allowed_bins=allowed_bins,
                deny_patterns=deny_patterns,
                mock=mock,
            )
        )

    if mock:
        report["notes"].append("mock 运行：只计划命令、不执行")
        report["summary"] = "mock 运行，未真正执行"
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
    # 「产物到底跑起来过没有」：只有 rc=0 但零输出/无入口 ⇒ 视为没有可运行的证据
    static_problems += runnability_problems(work, list(mat["written"]), list(report["commands"]))
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
    failed = [
        c for c in executed
        if c["status"] not in ("ok", "unavailable") and c not in long_running
    ]
    # blocking（补丁没全套上）必须参与判定：否则「部分套用 + 剩余代码能跑通」
    # 会落到下面的 elif 分支判成 pass，把残缺的交付物放过去。
    if failed or static_problems or blocking:
        report["verdict"] = "fail"
        for cmd in failed:
            label = STATUS_CN.get(cmd["status"], cmd["status"])
            report["problems"].append(
                f"{label}：`{cmd['command']}`"
                + (f"（退出码 {cmd['exit_code']}）" if cmd["exit_code"] is not None else "")
                + (f" —— {cmd['reason']}" if cmd["reason"] else "")
            )
    elif any(c["status"] == "ok" for c in executed):
        report["verdict"] = "pass"
    else:
        report["verdict"] = "skipped"
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
    return report
