"""把某次运行的「每次调用的完整输入 / 原始输出」导出成便于对比的文本（优化提示词用）。

为什么单独做个工具：页面上是一个阶段一个折叠块，横向对比（同一阶段的第 1 轮 vs 第 3 轮、
或 architect_plan 与 dev 拿到的上游是否一致）要来回点，而且复制出来会丢结构。
这里直接落成文件，**按阶段分文件 + 一份合并稿**，方便贴进编辑器或 diff。

用法：
    python tools/export_io.py 20260927-192001                # 导出到 runs/<id>/io/
    python tools/export_io.py 20260927-192001 --out D:\\tmp\\io
    python tools/export_io.py 20260927-192001 --stage dev    # 只看某阶段（前缀匹配，如 dev-T-02）
    python tools/export_io.py 20260927-192001 --last 3       # 只看最后 3 次调用
    python tools/export_io.py 20260927-192001 --no-raw       # 只要输入，不要输出

产物（默认落在 runs/<id>/io/）：
    _index.md        一览表：序号 / 阶段 / 各字段字数 / 是否模型调用
    01-intake.md     每次调用一份：系统提示词 / 用户输入 / 原始输出 / thinking
    ...
    _all.md          全部拼接（一份文件贴进编辑器）
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGE_FILE = re.compile(r"^(\d+)-([A-Za-z0-9_\-]+)\.json$")


def load_snapshots(run_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(glob.glob(str(run_dir / "*.json"))):
        m = STAGE_FILE.match(os.path.basename(path))
        if not m:
            continue
        try:
            payload = json.load(io.open(path, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        rows.append(
            {
                "seq": int(m.group(1)),
                "stage": payload.get("stage") or m.group(2),
                "name": os.path.basename(path),
                "meta": payload.get("meta") or {},
                "system": payload.get("system_prompt") or "",
                "user": payload.get("request_preview") or "",
                "raw": payload.get("response_text") or "",
                "thinking": payload.get("response_thinking") or "",
            }
        )
    rows.sort(key=lambda r: r["seq"])
    return rows


def is_model_call(row: dict) -> bool:
    if str(row["meta"].get("kind") or "") == "snapshot":
        return False
    return bool(row["system"] or row["user"] or row["raw"])


def render(row: dict, *, include_raw: bool) -> str:
    note = str(row["meta"].get("note") or "")
    lines = [
        f"# {row['seq']:02d} · {row['stage']}",
        "",
        f"- 说明：{note or '（首轮）'}",
        f"- 类型：{'模型调用' if is_model_call(row) else '**非模型调用**（运行验证 / 累积快照，无提示词与模型输出）'}",
        f"- system：{len(row['system'])} 字 | user：{len(row['user'])} 字"
        + (f" | raw：{len(row['raw'])} 字 | thinking：{len(row['thinking'])} 字" if include_raw else ""),
        f"- 元数据：tag={row['meta'].get('tag')} ctx={row['meta'].get('num_ctx')} "
        f"attempt={row['meta'].get('attempt')} truncated={row['meta'].get('truncated')}",
        "",
        "## 系统提示词",
        "",
        row["system"] or "（无）",
        "",
        "## 用户输入",
        "",
        row["user"] or "（无）",
    ]
    if include_raw:
        lines += ["", "## 原始输出（thinking）", "", row["thinking"] or "（无）",
                  "", "## 原始输出（正文，解析前）", "", row["raw"] or "（无）"]
    lines += ["", "---", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="导出某次运行的完整输入/输出（优化提示词用）")
    ap.add_argument("run_id")
    ap.add_argument("--out", default="", help="导出目录（默认 runs/<run_id>/io）")
    ap.add_argument("--stage", default="", help="只导出名字含该前缀的阶段（如 dev / dev-T-02 / architect_plan）")
    ap.add_argument("--last", type=int, default=0, help="只导出最后 N 次调用")
    ap.add_argument("--no-raw", action="store_true", help="只导出输入，不导模型输出")
    args = ap.parse_args()

    run_dir = ROOT / "runs" / args.run_id
    if not run_dir.is_dir():
        print(f"找不到运行目录：{run_dir}")
        return 1
    rows = load_snapshots(run_dir)
    if args.stage:
        rows = [r for r in rows if r["stage"].startswith(args.stage)]
    if args.last:
        rows = rows[-args.last:]
    if not rows:
        print("没有匹配的阶段快照")
        return 1

    include_raw = not args.no_raw
    out_dir = Path(args.out) if args.out else run_dir / "io"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 索引
    idx = ["# 输入 / 输出一览", "", f"运行：`{args.run_id}`　共 {len(rows)} 次调用", "",
           "| 序号 | 阶段 | 说明 | 模型调用 | system | user | raw | thinking |",
           "|---|---|---|---|---|---|---|---|"]
    all_parts = [f"# {args.run_id} 全部调用（完整输入 / 原始输出）", ""]
    for row in rows:
        idx.append(
            f"| {row['seq']:02d} | {row['stage']} | {str(row['meta'].get('note') or '')[:24]} | "
            f"{'是' if is_model_call(row) else '否'} | {len(row['system'])} | {len(row['user'])} | "
            f"{len(row['raw'])} | {len(row['thinking'])} |"
        )
        text = render(row, include_raw=include_raw)
        (out_dir / f"{row['seq']:02d}-{row['stage']}.md").write_text(text, encoding="utf-8")
        all_parts.append(text)
    (out_dir / "_index.md").write_text("\n".join(idx) + "\n", encoding="utf-8")
    (out_dir / "_all.md").write_text("\n".join(all_parts), encoding="utf-8")

    total = sum(len(r["system"]) + len(r["user"]) + (len(r["raw"]) + len(r["thinking"]) if include_raw else 0)
                for r in rows)
    print(f"已导出 {len(rows)} 次调用 → {out_dir}")
    print(f"  单次文件：{len(rows)} 个；索引：_index.md；合并稿：_all.md（共约 {total:,} 字）")
    print(f"  其中非模型调用 {sum(1 for r in rows if not is_model_call(r))} 个（运行验证 / 累积快照）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
