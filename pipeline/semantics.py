"""语义分析（pyright）：语法 / 导入检查之外的「类型感知」屏障。

**为什么必须有它**：``verify`` 的 ast 层只能做**字面**判定 —— 语法（py_compile）、
跨模块未定义名、import 契约。而真机上最难发现的一类缺陷恰好是它抓不到的：

    g.wrong_method(1)          # 属性不存在
    g.move(1, 2)               # 参数个数不匹配
    h = make_game(); h.foo()   # 跨函数**返回值**的类型推断

2026-09-25 实测：pyright 在 5 文件样本上 2.1s 就抓到上面三条，全部是 ast 抓不到的；
而且 ``run_command`` 也常常覆盖不到它们 —— 那些代码可能在**未被执行的路径**上。

**设计要点**：

1. **可选增强，绝不硬依赖。** pyright 装在全局 npm 目录，换机器就没有。
   :func:`available` 探测不到时，所有接口返回空结果 + 原因，流水线照常跑。
2. **只报本轮产出的文件。** 存量代码的既有诊断一律不报 —— 实测本项目 pipeline 目录
   本身就有 20 条既有 error（reportPossiblyUnboundVariable 等），
   不做这层过滤会把真正要看的问题整个淹掉。
3. **分级。** 只把 :data:`config.LSP_BLOCKING_RULES` 里的高置信规则交给 dev 重问；
   推断性结论只进报告。误报的代价是一整轮返工，比漏报贵得多。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import LSP_BLOCKING_RULES, LSP_MAX_DIAGNOSTICS, LSP_TIMEOUT

#: Windows 下隐藏子进程窗口
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

#: 「允许执行的程序」白名单之外的东西不碰 —— pyright 由本模块自行探测调用，
#: 不走 verify 的命令白名单（它不执行待验证代码，只做静态分析）。
_ENTRY_NAMES = ("pyright.cmd", "pyright", "pyright.ps1")
_LANGSERVER_NAMES = (
    "pyright-langserver.cmd", "pyright-langserver", "pyright-langserver.ps1",
)

_cached_entry: Path | None | bool = False  # False = 还没探测过
_cached_langserver: Path | None | bool = False


def _find(names: tuple[str, ...], which: str) -> Path | None:
    """按名字探测可执行文件。

    Windows 上 npm 全局包装器有 .cmd/.ps1 两种，``shutil.which`` 依赖 PATHEXT，
    所以再补一轮按名字的显式探测 —— 实测环境里 npm 全局目录并不总在 PATH 生效。
    """
    hit = shutil.which(which)
    if hit:
        return Path(hit)
    for base in (
        Path.home() / "AppData" / "Roaming" / "npm",
        Path(os.environ.get("APPDATA", "")) / "npm",
        Path("/usr/local/bin"),
        Path("/usr/bin"),
    ):
        for name in names:
            probe = base / name
            if probe.exists():
                return probe
    return None


def pyright_entry() -> Path | None:
    """定位 pyright CLI（做诊断用）；找不到返回 None（结果会缓存）。"""
    global _cached_entry
    if _cached_entry is not False:
        return _cached_entry  # type: ignore[return-value]
    _cached_entry = _find(_ENTRY_NAMES, "pyright")
    return _cached_entry


def langserver_entry() -> Path | None:
    """定位 pyright 的 LSP server（做引用查找用）；找不到返回 None（结果会缓存）。"""
    global _cached_langserver
    if _cached_langserver is not False:
        return _cached_langserver  # type: ignore[return-value]
    _cached_langserver = _find(_LANGSERVER_NAMES, "pyright-langserver")
    return _cached_langserver


def available() -> bool:
    """pyright 是否可用（供调用方决定走不走语义检查）。"""
    return pyright_entry() is not None


def unavailable_reason() -> str:
    return (
        "未找到 pyright，语义检查已跳过"
        "（安装：npm i -g pyright；或设 PIPELINE_LSP=0 显式关闭）"
    )


def diagnose(
    work: Path | str,
    rel_files: list[str] | None = None,
    *,
    timeout: int = LSP_TIMEOUT,
) -> dict[str, Any]:
    """对 ``work`` 跑一次 pyright，返回**只看 rel_files** 的结构化诊断。

    参数
    ----
    work
        要被分析的目录（通常是物化后的沙箱）。pyright 以它作为项目根。
    rel_files
        本轮产出文件的相对路径。只保留这些文件的诊断（见模块注释第 2 点）。
        传 None 表示不过滤（调用方自己清楚在看什么时才这么用）。
    """
    entry = pyright_entry()
    root = Path(work)
    out: dict[str, Any] = {
        "available": entry is not None,
        "reason": "",
        "diagnostics": [],
        "total": 0,
        "filtered_out": 0,
        "elapsed_s": 0.0,
    }
    if entry is None:
        out["reason"] = unavailable_reason()
        return out
    if not root.is_dir():
        out["reason"] = f"目录不存在：{root}"
        return out

    wanted = (
        {str(p).replace("\\", "/") for p in rel_files}
        if rel_files is not None
        else None
    )

    started = time.time()
    try:
        proc = subprocess.run(  # noqa: S603
            [str(entry), "--outputjson"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        out["reason"] = f"pyright 超过 {timeout}s 未结束（已终止）"
        out["elapsed_s"] = round(time.time() - started, 2)
        return out
    except (OSError, ValueError) as exc:
        # 环境层问题（包装器不可执行等）不该判交付物有罪
        out["reason"] = f"pyright 调用失败：{type(exc).__name__}: {exc}"
        out["elapsed_s"] = round(time.time() - started, 2)
        return out
    out["elapsed_s"] = round(time.time() - started, 2)

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        out["reason"] = (
            f"pyright 输出不是 JSON（rc={proc.returncode}）：{(proc.stdout or '')[:200]}"
        )
        return out

    rows: list[dict[str, Any]] = []
    for item in data.get("generalDiagnostics") or []:
        if not isinstance(item, dict):
            continue
        raw_file = str(item.get("file") or "")
        try:
            rel = Path(raw_file).resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            rel = raw_file.replace("\\", "/")
        if wanted is not None and rel not in wanted:
            out["filtered_out"] += 1
            continue
        rng = (item.get("range") or {}).get("start") or {}
        rule = str(item.get("rule") or "")
        rows.append(
            {
                "file": rel,
                "line": int(rng.get("line", 0)) + 1,  # pyright 是 0-based
                "column": int(rng.get("character", 0)) + 1,
                "severity": str(item.get("severity") or ""),
                "rule": rule,
                # pyright 的消息里带 \xa0（不换行空格）做缩进，进 prompt 会显示成怪字符
                "message": " ".join(
                    str(item.get("message") or "").replace("\xa0", " ").split()
                ),
                # 高置信（可回灌给 dev 触发重问）vs 推断性（只进报告）
                "blocking": rule in LSP_BLOCKING_RULES,
            }
        )
    out["total"] = len(rows)
    # 高置信的排前面：回灌给模型时先给最该修的
    rows.sort(key=lambda r: (not r["blocking"], r["file"], r["line"]))
    out["diagnostics"] = rows[:LSP_MAX_DIAGNOSTICS] if LSP_MAX_DIAGNOSTICS > 0 else rows
    return out


def problem_lines(result: dict[str, Any], limit: int = 6) -> list[str]:
    """把诊断压成给模型看的一行行问题描述（只取高置信项）。"""
    if not result or not result.get("available"):
        return []
    out: list[str] = []
    for item in result.get("diagnostics") or []:
        if not item.get("blocking"):
            continue
        out.append(
            f"`{item['file']}` 第 {item['line']} 行：{item['message']}"
            + (f"（{item['rule']}）" if item.get("rule") else "")
        )
        if len(out) >= limit:
            break
    return out


def summary_line(result: dict[str, Any]) -> str:
    """一行摘要，进日志/报告。"""
    if not result:
        return "语义检查：未运行"
    if not result.get("available"):
        return f"语义检查：跳过（{result.get('reason') or '不可用'}）"
    if result.get("reason"):
        return f"语义检查：{result['reason']}"
    blocking = sum(1 for d in result.get("diagnostics") or [] if d.get("blocking"))
    return (
        f"语义检查：{result.get('total', 0)} 条问题"
        f"（高置信 {blocking} 条），{result.get('elapsed_s', 0)}s"
        + (
            f"，另有 {result['filtered_out']} 条属存量代码（未计入）"
            if result.get("filtered_out")
            else ""
        )
    )


def _selftest() -> int:
    """直接跑 `python -m pipeline.semantics` 时自检一次（排查环境问题用）。"""
    print("python :", sys.executable)
    print("pyright:", pyright_entry() or "(未找到)")
    print("available:", available())
    print("blocking rules:", ", ".join(sorted(LSP_BLOCKING_RULES)))
    return 0 if available() else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_selftest())
