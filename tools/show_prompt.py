"""查看某次运行里**真正喂给模型的原始内容**（system + user + 上下文元数据）。

为什么要单独一个工具：调提示词的第一步永远是"这次到底喂了什么"，而不是先猜模型能力。
本项目已经为此付过学费 —— `_record` 早期把 `request_preview` 截到 4000 字且**不存 system**，
照那种预览调提示词等于盲人摸象。`traces.jsonl` 现在存的是完整原文，缺的只是一个顺手的查看器。

用法：
    python tools/show_prompt.py --run 20260928-095848 --list
    python tools/show_prompt.py --run 20260928-095848 --stage dev        # 最后一次 dev
    python tools/show_prompt.py --run 20260928-095848 --stage review --out runs/_audit
可选：
    --index N    第 N 次该阶段的调用（1 起；负数从末尾数，默认 -1）
    --note xxx   按 note 子串筛（如 `dev·T-01`）
    --system     连 system 一起打印（默认也打印，--no-system 可关）
    --out DIR    额外把 system/user 各写一个文件到 DIR（便于全文阅读/对比）
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_traces(run: str) -> list[dict]:
    path = ROOT / "runs" / run / "traces.jsonl"
    if not path.is_file():
        raise SystemExit(f"找不到 {path}")
    return [json.loads(line) for line in io.open(path, encoding="utf-8") if line.strip()]


def brief(row: dict) -> str:
    return "%-20s %-30s sys=%-5d user=%-6d ctx=%-6s think=%-5s 截断=%s" % (
        str(row.get("stage"))[:20],
        str(row.get("note") or "-")[:30],
        len(str(row.get("system") or "")),
        len(str(row.get("user") or "")),
        row.get("num_ctx"),
        row.get("think"),
        row.get("truncated"),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--stage")
    ap.add_argument("--note")
    ap.add_argument("--index", type=int, default=-1)
    ap.add_argument("--out")
    ap.add_argument("--no-system", action="store_true")
    ap.add_argument(
        "--outline",
        action="store_true",
        help="只列**段落骨架**（【…】标题 + 各段字数）：快速判断喂了哪些段、有没有缺段",
    )
    args = ap.parse_args()

    rows = load_traces(args.run)
    if args.list or not args.stage:
        print(f"共 {len(rows)} 次调用（按发生顺序）：")
        for i, row in enumerate(rows, 1):
            print(f"  [{i:2}] {brief(row)}")
        if not args.stage:
            return 0

    picked = [r for r in rows if str(r.get("stage")) == args.stage]
    if args.note:
        picked = [r for r in picked if args.note in str(r.get("note") or "")]
    if not picked:
        raise SystemExit(f"没有 stage={args.stage} note~{args.note} 的调用")
    # `--index` 对人友好：**1 起**（负数从末尾数，与切片一致）。
    # 第一版直接 `picked[args.index]` —— 于是 `--index 1` 想取第一次却越界（取到第二次）。
    idx = args.index if args.index < 0 else args.index - 1
    try:
        row = picked[idx]
    except IndexError:
        raise SystemExit(
            f"index={args.index} 越界：stage={args.stage} 共 {len(picked)} 次调用"
        ) from None
    print(f"== stage={row.get('stage')} note={row.get('note')} 第 {args.index} 次（该阶段共 {len(picked)} 次）")
    print(f"   模型={row.get('tag')} ctx={row.get('num_ctx')} think={row.get('think')} 截断={row.get('truncated')}")
    print(f"   usage={json.dumps(row.get('usage') or {}, ensure_ascii=False)}")
    print(f"   schema_errors={row.get('schema_errors')} failed_attempts={row.get('failed_attempts')}")
    print()
    if not args.no_system:
        print("---------------- system（角色契约）----------------")
        print(str(row.get("system") or ""))
        print()
    user = str(row.get("user") or "")
    if args.outline:
        # 段落骨架：一眼看出"喂了哪些段、每段多重、有没有该在的段不在"。
        # 调提示词时这一步通常比读全文更快定位问题（缺段 / 顺序不对 / 某段吃掉预算）。
        print("---------------- 段落骨架（user）----------------")
        blocks: list[tuple[str, int]] = []
        current = "（开头无标题段）"
        size = 0
        for line in user.split("\n"):
            stripped = line.strip()
            if stripped.startswith("【") and "】" in stripped:
                blocks.append((current, size))
                current = stripped[:60]
                size = 0
            size += len(line) + 1
        blocks.append((current, size))
        total = sum(n for _, n in blocks)
        for title, size in blocks:
            if size <= 0 and title == "（开头无标题段）":
                continue
            print(f"  {size:>6} 字  {title}")
        print(f"  {total:>6} 字  合计（{len([b for b in blocks if b[1] > 0])} 段）")
        print(f"  预算：ctx={row.get('num_ctx')} prompt_tokens={row.get('usage', {}).get('prompt_tokens')}")
        return 0
    print("---------------- user（本轮输入）----------------")
    print(user)

    if args.out:
        out_dir = ROOT / args.out
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{row.get('stage')}__{str(row.get('note') or 'x').replace('·', '_').replace(' ', '')}"
        (out_dir / f"{stem}.system.txt").write_text(str(row.get("system") or ""), encoding="utf-8")
        (out_dir / f"{stem}.user.txt").write_text(str(row.get("user") or ""), encoding="utf-8")
        print(f"\n（已写出 {out_dir}/{stem}.system.txt / .user.txt）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
