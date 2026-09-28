"""把 `CONTEXT.md` 里 `file:line symbol` 的行号按符号真实位置回填（`check_refs` 的修复端）。

为什么需要它：`check_refs` 会在每次"在引用点之上插入代码"后报漂移 —— 这是**必然**的，
不是谁写错了文档。真机教训的载体是 `CONTEXT.md`，里面几十条 `文件:行号 符号`；
靠手改既慢又容易只改一半。这个工具用与 `check_refs` **同一套**解析与判定窗口（±6 行），
所以跑完再跑 `check_refs` 必然一致。

它**不在** `smoke_all` 里：那是"检查"，这是"改动文档"的动作，不该在回归里偷偷改文件。
用法：`python tools/fix_refs.py`（没有可回填的就什么都不做）。

找不到符号的引用**原样保留并打印** —— 宁可留给人看，也不猜一个行号填进去（猜错比空着更难查）。
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "CONTEXT.md"
#: 与 check_refs 保持同一套（改一处必须改另一处，因此两边都留了这句话）
SYM_REF = re.compile(r"([A-Za-z0-9_./\\-]+\.py)[:\uff1a](\d+)\s+([A-Za-z_][A-Za-z0-9_]*)")
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
    text = DOC.read_text(encoding="utf-8")
    fixed: list[tuple[str, int, int, str]] = []
    missed: list[tuple[str, int, str]] = []
    for rel, num, sym in SYM_REF.findall(text):
        path = resolve(rel)
        if path is None:
            continue
        src = path.read_text(encoding="utf-8", errors="replace").splitlines()
        n = int(num)
        window = "\n".join(src[max(0, n - 7): n + 6])
        if sym in window:
            continue  # 仍然对得上，不动
        cand = [
            i for i, line in enumerate(src, 1)
            if re.match(rf"\s*(def|class)\s+{re.escape(sym)}\b", line)
        ] or [i for i, line in enumerate(src, 1) if re.search(rf"\b{re.escape(sym)}\b", line)]
        if not cand:
            missed.append((rel, n, sym))
            continue
        new = cand[0]
        old_ref, new_ref = f"{rel}:{n} {sym}", f"{rel}:{new} {sym}"
        if old_ref in text:
            text = text.replace(old_ref, new_ref)
            fixed.append((rel, n, new, sym))
    if fixed:
        DOC.write_text(text, encoding="utf-8")
    for rel, old, new, sym in fixed:
        print(f"  {rel}:{old} -> {new}   {sym}")
    print(f"回填 {len(fixed)} 处；找不到符号（原样保留）{len(missed)} 处")
    for rel, n, sym in missed:
        print(f"  !! {rel}:{n} {sym}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
