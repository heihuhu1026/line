"""把「问题记录」交给模型做元优化，产出**流水线自身的改进建议书**。

它读的是历史运行的问题汇总（pipeline/issues.build_report）+ 受影响阶段的当前系统提示词，
让模型诊断流水线设计缺陷并给出最小改动建议。**只产出建议，不自动修改任何代码或提示词。**

用法:
    python tools/meta_optimize.py --mock                 # 离线演练（不加载模型）
    python tools/meta_optimize.py                        # 用默认 14B（架构师/评审档）
    python tools/meta_optimize.py --tag qwen2.5-coder-7b-dev-24k   # 换模型（上下文更大，能看完整提示词）
    python tools/meta_optimize.py --limit 30 --prompt-chars 3000   # 控制材料规模
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import issues, runstore  # noqa: E402
from pipeline.budget import estimate_tokens, fit_prompt  # noqa: E402
from pipeline.config import OLLAMA_HOST, REQUEST_TIMEOUT, RUNS_DIR, STAGE_MODELS  # noqa: E402
from pipeline.ollama_client import MockClient, OllamaClient, OllamaError  # noqa: E402


def pick_stages(report: dict, limit: int = 3) -> list[str]:
    """问题最集中的几个阶段（决定给模型看哪几段提示词）。"""
    counts: dict[str, int] = {}
    for row in report.get("runs") or []:
        for issue in row.get("top_issues") or []:
            stage = issue.get("stage")
            if stage in STAGE_MODELS:
                counts[stage] = counts.get(stage, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    return [stage for stage, _ in ranked] or ["review"]


def render_proposal(proposal: dict, report: dict, tag: str) -> str:
    lines = [
        "# 流水线元优化建议书",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 分析模型：`{tag}`",
        f"- 材料来源：{report['runs_analyzed']} 次运行、{report['totals']['issues']} 条问题记录"
        f"；当前流水线指纹 `{report['current_fingerprint']['pipeline_hash']}`",
        "",
        "> 本文件是**建议**，不自动改动任何文件。落地前请人工判断，并记录改动后的新指纹以便对比。",
        "",
        "## 诊断",
        "",
        str(proposal.get("diagnosis") or "（无）"),
        "",
        "## 问题与建议",
        "",
    ]
    problems = sorted(proposal.get("problems") or [], key=lambda p: p.get("priority") or 9)
    for index, item in enumerate(problems, 1):
        lines += [
            f"### {index}. [P{item.get('priority', '?')}] {item.get('problem', '')}",
            "",
            f"- 根因：{item.get('root_cause', '')}",
            f"- 证据：{item.get('evidence', '（未给）')}",
            f"- 改动位置：`{item.get('target', '')}`",
            f"- 建议改法：{item.get('proposed_change', '')}",
            f"- 风险：{item.get('risk', '（未评估）')}",
            "",
        ]
    lines += ["## 提示词改法", ""]
    edits = proposal.get("prompt_edits") or []
    if not edits:
        lines.append("（无）")
    for edit in edits:
        lines += [
            f"### stage `{edit.get('stage', '')}` — {edit.get('issue', '')}",
            "",
            f"理由：{edit.get('rationale', '（未给）')}",
            "",
            "```text",
            str(edit.get("proposed_text", "")),
            "```",
            "",
        ]
    lines += ["## 验证计划（怎么证明改动有效）", ""]
    lines += [f"- {item}" for item in (proposal.get("measurement_plan") or ["（无）"])]
    lines += ["", "## 仍不确定", ""]
    lines += [f"- {item}" for item in (proposal.get("unknowns") or ["（无）"])]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="用问题记录做流水线元优化（只产出建议）")
    parser.add_argument("--runs-dir", default=str(RUNS_DIR), help=f"runs 目录，默认 {RUNS_DIR}")
    parser.add_argument("--limit", type=int, default=200, help="统计最近 N 次运行")
    parser.add_argument("--tag", help="用哪个模型做分析（默认取评审档 14B）")
    parser.add_argument("--prompt-chars", type=int, default=1500, help="每段提示词最多给多少字符")
    parser.add_argument("--out", help="输出目录（默认 runs/_meta/<时间戳>/）")
    parser.add_argument("--mock", action="store_true", help="离线演练，不加载模型")
    args = parser.parse_args()

    runs_dir = Path(args.runs_dir)
    if not runs_dir.exists():
        print(f"找不到 runs 目录: {runs_dir}", file=sys.stderr)
        return 1

    report = issues.build_report(runs_dir, limit_runs=args.limit)
    if not report["runs_analyzed"]:
        print("没有任何运行记录可分析（先跑一次流水线）", file=sys.stderr)
        return 1

    stages = pick_stages(report)
    all_prompts = issues.system_prompts()
    prompts_excerpt = {
        stage: all_prompts[stage][: args.prompt_chars] for stage in stages if all_prompts.get(stage)
    }
    user = issues.build_meta_prompt(report, prompts_excerpt)

    base = STAGE_MODELS["review"]
    spec = dataclasses.replace(base, role="元优化分析", tag=args.tag or base.tag, num_predict=3072)
    budget = max(spec.prompt_token_budget - estimate_tokens(issues.META_SYSTEM), 1200)
    user, truncated = fit_prompt([user], budget)

    out_dir = Path(args.out) if args.out else runs_dir / "_meta" / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "input-report.md").write_text(issues.report_markdown(report), encoding="utf-8")
    (out_dir / "input-prompt.txt").write_text(
        f"=== SYSTEM ({spec.tag}) ===\n{issues.META_SYSTEM}\n\n=== USER ===\n{user}\n", encoding="utf-8"
    )

    print(f"材料：{report['runs_analyzed']} 次运行 / {report['totals']['issues']} 条问题；"
          f"涉及阶段 {stages}；prompt {estimate_tokens(user)} tok（预算 {budget}{'，已裁剪' if truncated else ''}）")
    print(f"分析模型：{spec.tag}（ctx={spec.num_ctx}）")

    client = MockClient() if args.mock else OllamaClient(OLLAMA_HOST, timeout=REQUEST_TIMEOUT)
    try:
        sched = client.ensure_exclusive(spec.tag)
        print(f"单驻留切换：{sched['switched']}  卸载：{sched['unloaded']}")
        proposal, meta = client.chat_json(spec, issues.META_SYSTEM, user, issues.META_PROPOSAL_SCHEMA)
    except OllamaError as exc:
        print(f"调用失败: {exc}", file=sys.stderr)
        return 3
    finally:
        if not args.mock:
            for model in client.ps():
                if model.get("name"):
                    client.unload(model["name"])

    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tag": spec.tag,
        "mock": args.mock,
        "report_summary": report["totals"],
        "fingerprint": report["current_fingerprint"],
        "stages_in_prompt": stages,
        "truncated": truncated,
        "usage": {k: meta.get(k) for k in ("wall_s", "load_s", "prompt_tokens", "output_tokens")},
        "proposal": proposal,
    }
    runstore.write_json(out_dir / "proposal.json", payload)
    (out_dir / "proposal.md").write_text(render_proposal(proposal, report, spec.tag), encoding="utf-8")

    print()
    print(f"诊断：{proposal.get('diagnosis', '')[:400]}")
    for item in sorted(proposal.get("problems") or [], key=lambda p: p.get("priority") or 9)[:5]:
        print(f"  [P{item.get('priority')}] {item.get('problem')}  ->  {item.get('proposed_change')}")
    print()
    print(f"建议书：{out_dir / 'proposal.md'}")
    print("注意：这是建议，不自动改代码；落地后记得对比新指纹下的问题分布。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
