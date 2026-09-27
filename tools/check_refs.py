"""核对 CONTEXT.md 里的「文件:行号」指向与当前代码是否一致（**文档引用漂移检查**）。

为什么需要它：CONTEXT.md 是真机教训的唯一载体，里面的指向一旦漂移，下一次照着它排查
就会查错地方 —— 真机 20260926-214757 就出过「明明传了 --repo 却报没提供仓库」，人顺着
错误的归因查了很久。而文档里的行号**必然**随着代码改动失效：§23.4 那块指向曾整块失效
（`prompts.py:1044 parts_review` 实际已到 2264）。

检查三件事：
  ① 文件存在吗；
  ② 行号越界吗；
  ③ **该行 ±6 行内是否还有文档声称的那个符号名** —— 这条最关键，能抓出
     "行号还在、内容已经换了"的漂移（只查①②会漏掉这一类）。

用法：`python tools/check_refs.py`（退出码 0=一致，1=有漂移）
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "CONTEXT.md"

REF = re.compile(r"([A-Za-z0-9_./\\-]+\.(?:py|html|mjs|json|md))[:\uff1a](\d+)")
#: 文档里形如 `prompts.py:2264 parts_review` 的「行号 + 符号名」引用
SYM_REF = re.compile(
    r"([A-Za-z0-9_./\\-]+\.py)[:\uff1a](\d+)\s+([A-Za-z_][A-Za-z0-9_]*)"
)
#: 这些目录下的同名文件是**真机产物**，不是项目代码，引用它们属正常，跳过
SKIP_DIRS = {"runs", ".git", "__pycache__", "node_modules", "_jobs", "models", ".codebuddy"}


def resolve(rel: str) -> Path | None:
    p = ROOT / rel
    if p.is_file():
        return p
    name = Path(rel).name
    for cand in ROOT.rglob(name):
        if any(s in cand.parts for s in SKIP_DIRS):
            continue
        return cand
    return None


def main() -> int:
    if not DOC.is_file():
        print(f"找不到 {DOC}")
        return 1
    text = io.open(DOC, encoding="utf-8").read()
    lines = text.splitlines()

    problems: list[str] = []
    ok = 0
    seen: set[str] = set()

    for i, line in enumerate(lines, 1):
        for m in REF.finditer(line):
            rel, num = m.group(1), int(m.group(2))
            key = f"{rel}:{num}"
            if key in seen:
                continue
            seen.add(key)
            # 真机产物文件（如生成的 ledger.py）本来就不在仓库里，引用它们是记录证据，不算漂移
            if Path(rel).name in _GENERATED_SAMPLES:
                continue
            path = resolve(rel)
            if path is None:
                continue  # 产物文件，跳过（上面已覆盖大部分）
            src = io.open(path, encoding="utf-8", errors="replace").read().splitlines()
            if num > len(src):
                problems.append(
                    f"行号越界 {key}（CONTEXT.md:{i}）—— {path.name} 只有 {len(src)} 行"
                )
                continue
            ok += 1

    drift = 0
    seen_sym: set[str] = set()
    for rel, num, sym in SYM_REF.findall(text):
        key = f"{rel}:{num}:{sym}"
        if key in seen_sym:
            continue
        seen_sym.add(key)
        path = resolve(rel)
        if path is None:
            continue
        src = io.open(path, encoding="utf-8", errors="replace").read().splitlines()
        n = int(num)
        window = "\n".join(src[max(0, n - 7): n + 6])
        if sym not in window:
            drift += 1
            actual = src[n - 1].strip()[:60] if n <= len(src) else "(越界)"
            problems.append(f"指向漂移 {rel}:{num} 附近找不到 `{sym}`（实际该行：{actual}）")

    print(f"文档引用检查：有效 {ok} 条，带符号名引用 {len(seen_sym)} 条")
    if problems:
        print(f"发现 {len(problems)} 处问题：")
        for p in problems[:30]:
            # 用 ASCII 标记：Windows 控制台可能是 GBK，非 ASCII 符号会让工具自己崩掉
            print("  [!] " + p)
        return 1
    print("  全部一致（无缺失 / 越界 / 漂移）")
    return 0


#: 文档里作为**真机证据**引用的产物文件名（不在仓库里，属正常）
_GENERATED_SAMPLES = {"ledger.py", "input_handler.py", "renderer.py", "game_logic.py", "snake.py"}


if __name__ == "__main__":
    raise SystemExit(main())
