"""汇总跑一遍**离线**回归（不含真机），一条命令看全绿。

为什么要它：套件已经拆到 8 个（`smoke_rules` / `smoke_client` / `smoke_imports` /
`smoke_patch_apply` / `smoke_merge` / `smoke_bugfix` / `smoke_mock` / `smoke_console`），
外加 `check_refs` 文档漂移检查。手动一个个跑总会漏，漏掉的那一套恰恰是最容易退化的一套。

用法：
    python tools/smoke_all.py            # 跑全部
    python tools/smoke_all.py --quick    # 跳过较慢的 smoke_console（它要起临时服务）
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"

#: 顺序按"便宜且基础"在前：先查文档与规则，再查合并/分派，最后才是端到端
SUITES = [
    "check_refs.py",
    "smoke_recovery.py",
    "smoke_rules.py",
    "smoke_client.py",
    "smoke_imports.py",
    "smoke_patch_apply.py",
    "smoke_merge.py",
    "smoke_bugfix.py",
    "smoke_mock.py",
    "smoke_console.py",   # 较慢：会起临时服务
]

QUICK_SKIP = {"smoke_console.py"}


def main() -> int:
    quick = "--quick" in sys.argv
    results: list[tuple[str, int, float]] = []
    for name in SUITES:
        if quick and name in QUICK_SKIP:
            print(f"  -- 跳过 {name}（--quick）")
            continue
        path = TOOLS / name
        if not path.is_file():
            print(f"  ?? 缺失 {name}")
            results.append((name, 1, 0.0))
            continue
        t0 = time.time()
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.time() - t0
        results.append((name, proc.returncode, elapsed))
        tail = (proc.stdout or "").strip().splitlines()
        last = tail[-1] if tail else (proc.stderr or "").strip().splitlines()[-1:] or [""]
        status = "OK  " if proc.returncode == 0 else "FAIL"
        print(f"  [{status}] {name:<22} {elapsed:5.1f}s   {str(last[0])[:60]}")

    bad = [r for r in results if r[1] != 0]
    total = sum(r[2] for r in results)
    print()
    print(f"共 {len(results)} 套，失败 {len(bad)} 套，用时 {total:.1f}s")
    if bad:
        for name, _, _ in bad:
            # ASCII 标记：Windows 控制台可能是 GBK，非 ASCII 会让汇总脚本自己崩掉
            print(f"  [X] {name}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
