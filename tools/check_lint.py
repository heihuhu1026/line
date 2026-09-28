"""静态检查闸门：把 `ruff` 里**会真实致损**的规则接进回归。

为什么需要它（真机 `20260928-095848` 的教训）：那次运行里"补漏符号"这条自检
**在 5/5 张施工图上全部空转**，原因是补漏重试引用了另一个函数的局部变量 `focus`
（跨函数引用局部变量，编译器不管），触发 `NameError` —— 而异常按设计被兜底吞掉
（单张图失败不该拖垮整轮），于是日志里只有一行"补符号这次调用失败"，机制却已死。
`ruff` 的 F821 一行就定位到 `orchestrator.py:3356`，但**闸门里没跑 lint**，所以它走完了
真机 7 分钟的方案调用 + 5 张图才被发现。

**只拦会致损的规则，风格问题只统计**：
  · 阻断：`F821` 未定义名 / `F811` 重复定义 / `F402` 循环变量遮蔽导入
    —— 这三类都会在**运行时**变成 NameError / 静默用错对象；
  · 非阻断：`F841`（未使用变量）、`SIM`/`C4`（可简化写法）等，当前有二十余条。
    把它们也设成阻断，闸门会长期是红的，人就会习惯性忽略它 —— 那比没有闸门更糟。
    所以这里只报数量，逼着"欠账可见"，但不阻塞。

未安装 ruff（项目刻意保持运行时零依赖）时**跳过并打印原因**，退出 0：
闸门对"没装开发依赖"的环境不该判负，但也不能装作跑过了。
"""
from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 会真实致损的规则（运行时 NameError / 用错对象）。改这个集合要写清理由。
BLOCKING = ("F821", "F811", "F402")
#: 扫描范围（与 `python -m ruff check` 的默认用法一致，配置取自 pyproject.toml）
TARGETS = ("pipeline", "tools")

#: concise 输出形如 `pipeline\orchestrator.py:3356:29: F821 Undefined name \`focus\``
#: 注意 `: F821 ` 里**冒号后有空格**（第一版正则漏了它，结果把"有 26 条非阻断"读成 0 条）
LINE_RE = re.compile(r":\s*([A-Z]{1,3}\d{3,4})\s+(.*)$")


def run_ruff() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", *TARGETS, "--output-format", "concise"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    rc, out = run_ruff()
    if "No module named" in out or "not recognized" in out:
        print("  -- 跳过：未安装 ruff（pyproject 的 dev 依赖可选，运行时保持零依赖）")
        print("     装上才有这道闸：pip install ruff")
        return 0

    blocking: list[str] = []
    others: Counter[str] = Counter()
    for raw in out.splitlines():
        line = raw.strip()
        m = LINE_RE.search(line)
        if not m:
            continue
        code = m.group(1)
        if code in BLOCKING:
            blocking.append(f"{line}")
        else:
            others[code] += 1

    total_other = sum(others.values())
    print(f"  扫描 {TARGETS}：阻断类问题 {len(blocking)} 条 / 非阻断 {total_other} 条")
    if total_other:
        top = "、".join(f"{code}×{n}" for code, n in others.most_common(6))
        print(f"  （非阻断，仅登记欠账：{top}）")
    if blocking:
        print(f"  发现 {len(blocking)} 条**会致损**的静态问题：")
        for item in blocking[:20]:
            print("    [X] " + item)
        print("  修好再跑（这三类在运行时分别是 NameError / 静默用错对象）")
        return 1
    if rc != 0 and not others:
        print("  -- ruff 以非零退出但没有可解析的问题行，原文如下：")
        print("  " + (out.strip()[:400] or "(空)"))
        return 1
    print("  阻断类规则全部通过（F821 / F811 / F402）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
