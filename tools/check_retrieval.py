"""预览存量代码检索结果，用于调参（相关度打分与 token 预算是否合适）。

用法:
    python tools/check_retrieval.py --repo <路径> --requirement "需求文本" [--budget 3000]
    python tools/check_retrieval.py --repo <路径> --requirement-file req.md   # 中文需求优先用文件
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import retrieval  # noqa: E402
from pipeline.budget import estimate_tokens  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="预览检索分片结果")
    parser.add_argument("--repo", required=True)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--requirement", help="需求文本")
    src.add_argument("--requirement-file", help="需求文本文件（UTF-8）")
    parser.add_argument("--budget", type=int, default=3000, help="token 预算")
    parser.add_argument("--per-file", type=int, default=1500, help="单文件 token 上限")
    args = parser.parse_args()
    requirement = (
        Path(args.requirement_file).read_text(encoding="utf-8") if args.requirement_file else args.requirement
    )

    terms = retrieval._query_terms(requirement)  # noqa: SLF001
    print(f"检索词 {len(terms)} 个: {terms[:20]}{' ...' if len(terms) > 20 else ''}")
    excerpts = retrieval.select_excerpts(
        Path(args.repo), query=requirement, token_budget=args.budget, per_file_tokens=args.per_file
    )
    total = sum(estimate_tokens(e.text) for e in excerpts)
    print(f"命中 {len(excerpts)} 个文件，合计约 {total} tokens（预算 {args.budget}）")
    for item in excerpts:
        print(f"  - {item.path:<50} score={item.score:<8} tokens≈{estimate_tokens(item.text)}"
              f"{' [已截断]' if item.truncated else ''} {item.note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
