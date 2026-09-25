"""命令行入口。

示例:
    # mock 跑通全流程（不加载模型），验证契约与回流
    python -m pipeline.cli --requirement "给客户列表加一个导出 Excel 按钮" --mock

    # 单阶段真机验证（例如只跑产品经理）
    python -m pipeline.cli --requirement-file req.md --repo D:/AI/project --only pm

    # 全流程 + 人工闸门（PM 与方案结束后暂停，等人工确认）
    python -m pipeline.cli --requirement-file req.md --repo D:/AI/project --pause-after pm,architect_plan

    # 从上次暂停处继续
    python -m pipeline.cli --resume 20260923-101010

    # 打回某个阶段重跑，并带上人工意见
    python -m pipeline.cli --resume 20260923-101010 --from architect_plan --feedback "不要动 config.py"

    # 列出所有运行
    python -m pipeline.cli --list

    # 入口总闸（全局架构岗）：large 需求自动拆模块、逐个跑同一条流水线
    python -m pipeline.cli --requirement-file req.md --repo D:/AI/project --gateway auto

    # 完全旁路（行为与未接入时一致）/ 人工强制规模 / 追加全局禁区
    python -m pipeline.cli --requirement-file req.md --gateway off
    python -m pipeline.cli --requirement-file req.md --scale large --forbidden src/core,tools/codegen

    # 列出作业 / 续跑作业（作业目录在 runs/_jobs/ 下，不进运行列表）
    python -m pipeline.cli --list-jobs
    python -m pipeline.cli --resume-job job-20260924-120000

    # 打印流定义（含前置节点）与一致性校验结果
    python -m pipeline.cli --show-flow

参数说明见 python -m pipeline.cli --help
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import flow
from . import gateway
from . import runstore
from .config import MAX_REWORK_ROUNDS, OLLAMA_HOST, REQUEST_TIMEOUT, REVIEW_EVERY, RUNS_DIR
from .ollama_client import MockClient, OllamaClient
from .orchestrator import ONLY_STAGES, Orchestrator, OrchestratorError, RunResult

# 从 ONLY_STAGES 派生，避免新增阶段时这里漏改（真机教训：加 intake 后本表没同步，
# 导致 `--pause-after intake` 被判「未知阶段」、进程直接 exit 2，运行刚启动就死掉）。
# human_review 是评审通过后自动触发的交付闸门，不能由人工指定，故排除（与 server.PAUSE_STAGES 一致）。
ALL_STAGES = [s for s in ONLY_STAGES if s != "human_review"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="二次开发需求流水线（单驻留串行编排，支持人工闸门与续跑）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--requirement", help="需求文本")
    src.add_argument("--requirement-file", help="需求文本文件（UTF-8）；传中文需求优先用这个")
    src.add_argument("--resume", metavar="RUN_ID", help="续跑已有运行（runs/<RUN_ID>），从暂停处的游标继续")
    src.add_argument("--list", action="store_true", help="列出 runs/ 下的所有运行后退出")
    src.add_argument(
        "--resume-job",
        metavar="JOB_ID",
        help="续跑一个全局架构作业（runs/_jobs/<JOB_ID>）：先续跑暂停的模块，再推进后续模块",
    )
    src.add_argument("--list-jobs", action="store_true", help="列出 runs/_jobs/ 下的所有作业后退出")

    parser.add_argument(
        "--gateway",
        choices=list(gateway.MODES),
        default=None,
        help=(
            "入口总闸（全局架构岗）模式：auto=先做零模型调用的预判，疑似大型才调全局架构（默认）；"
            "always=每次先调；off=完全旁路（行为与未接入时一致）。不传则取 config.local.json 的 guard.mode"
        ),
    )
    parser.add_argument(
        "--scale",
        choices=list(gateway.SCALES),
        default=None,
        help="人工强制规模判定（small=直通原有流水线 / large=强制拆模块），用于判错时纠正",
    )
    parser.add_argument(
        "--forbidden",
        help="全局禁区（逗号分隔，追加到 config.local.json 的 guard.forbidden_paths 之上）",
    )

    parser.add_argument("--repo", help="存量代码仓库路径（用于检索分片；不提供则不做检索）")
    parser.add_argument(
        "--project-type",
        choices=["secondary", "new"],
        default="secondary",
        help=(
            "secondary＝基于存量仓库的二次开发（默认）；"
            "new＝从零生成的全新项目：换用专用系统提示词，并跳过存量代码评估阶段"
        ),
    )
    parser.add_argument("--run-id", help="指定运行目录名（默认按时间戳生成；操作页面用它绑定日志）")
    parser.add_argument("--out", default=str(RUNS_DIR), help=f"产物输出目录，默认 {RUNS_DIR}")
    parser.add_argument("--mock", action="store_true", help="不加载模型，按 schema 合成占位产物")
    parser.add_argument("--mock-rework", type=int, default=0, help="mock 模式下前 N 次评审返回 rework_dev（验证回流）")
    parser.add_argument(
        "--max-rework",
        type=int,
        default=None,
        help=f"回流轮次上限（新运行默认 {MAX_REWORK_ROUNDS}；续跑时不传则沿用该 run 原本的上限）",
    )
    parser.add_argument(
        "--review-every",
        type=int,
        default=REVIEW_EVERY,
        help=f"每 N 轮评审一次（1=每轮都评审）；首轮与末轮必评审，默认 {REVIEW_EVERY}",
    )
    parser.add_argument(
        "--only",
        help="只执行指定阶段，逗号分隔：intake,pm,architect_assess,architect_plan,dev,test,review",
    )
    parser.add_argument(
        "--pause-after",
        help="人工闸门：在这些阶段结束后暂停并落盘（逗号分隔，同 --only 的阶段名）；续跑时默认沿用",
    )
    parser.add_argument("--no-pause", action="store_true", help="清空人工闸门（续跑时想一路跑到底用这个）")
    parser.add_argument("--from", dest="from_stage", help="仅用于 --resume：回到该阶段重跑（作废其下游产物）")
    parser.add_argument(
        "--from-checkpoint",
        type=int,
        help="仅用于 --resume：回放到指定检查点（快照 seq，用 --list 或页面查看），作废其后产物后继续",
    )
    parser.add_argument("--feedback", help="仅用于 --resume：人工审核意见，注入目标阶段的 prompt（优先级最高）")
    parser.add_argument("--issue-kind", help="仅用于 --resume：人工干预的问题分类（写入 issues 记录，见 pipeline/issues.py 的 KINDS）")
    parser.add_argument("--keep-warm", action="store_true", help="结束后不卸载模型")
    parser.add_argument("--json", action="store_true", help="最后只打印 summary JSON")
    parser.add_argument(
        "--show-flow",
        action="store_true",
        help="打印流定义（Mermaid 拓扑）与一致性校验结果后退出（不启动模型）",
    )
    return parser.parse_args(argv)


def _parse_list(raw: str | None) -> list[str]:
    """逗号分隔清单（如 --forbidden a,b）。"""
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _print_job(data: dict, as_json: bool = False) -> None:
    """作业收尾摘要。"""
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return
    modules = data.get("modules") or []
    print()
    print(f"作业         : {data.get('job_id')}（{gateway.job_status(data)}）")
    print(f"规模判定     : large（{'；'.join(data.get('reasons') or [])}）")
    print(f"模块数       : {len(modules)}")
    for index, row in enumerate(modules, start=1):
        audit = row.get("audit") or {}
        flags = []
        if audit.get("forbidden_touched"):
            flags.append("踩禁区")
        if audit.get("cross_module"):
            flags.append("越界")
        print(
            f"  {index}. {row.get('module_id'):<6} {str(row.get('module_name') or ''):<14} "
            f"{str(row.get('status')):<8} {str(row.get('run_id') or '-'):<28} {' '.join(flags)}"
        )
        for issue in row.get("issues") or []:
            print(f"       ! {issue}")
    base = gateway.job_dir(RUNS_DIR, str(data.get("job_id")))
    print(f"作业目录     : {base}")
    print(f"人读报告     : {base / 'report.md'}")
    print(f"续跑作业     : python -m pipeline.cli --resume-job {data.get('job_id')}")


def _parse_stages(raw: str | None, label: str) -> list[str] | None:
    if not raw:
        return None
    stages = [s.strip() for s in raw.split(",") if s.strip()]
    unknown = [s for s in stages if s not in ALL_STAGES]
    if unknown:
        print(f"{label} 中含未知阶段: {unknown}，可选: {ALL_STAGES}", file=sys.stderr)
        return None
    return stages


def _print_runs() -> int:
    rows = runstore.list_runs(RUNS_DIR)
    if not rows:
        print(f"{RUNS_DIR} 下没有运行记录")
        return 1
    print(f"{'run_id':<20}{'status':<9}{'verdict':<13}{'cursor':<17}{'att':>4}{'rev':>4}{'wall_s':>8}  paused_after")
    print("-" * 92)
    for row in rows:
        print(
            f"{row['run_id']:<20}{str(row['status']):<9}{str(row['verdict']):<13}"
            f"{str(row['cursor']):<17}{str(row['attempts'] or '-'):>4}{str(row['reviewed_rounds'] or '-'):>4}"
            f"{str(row['wall_s'] or '-'):>8}  {row['paused_after'] or ''}"
        )
    print()
    print("继续某次运行:  python -m pipeline.cli --resume <run_id>")
    print("图形化操作页面: python -m pipeline.server --port 8787")
    return 0


def _print_jobs() -> int:
    """列出全局架构作业（``runs/_jobs/``）。作业目录 ``_`` 开头，所以不在 --list 里。"""
    rows = gateway.list_jobs(RUNS_DIR)
    if not rows:
        print(f"{RUNS_DIR / gateway.JOBS_DIRNAME} 下没有作业记录")
        return 1
    print(f"{'job_id':<24}{'status':<9}{'scale':<7}{'mode':<8}{'mods':>5}  reasons")
    print("-" * 92)
    for row in rows:
        print(
            f"{row['job_id']:<24}{str(row['status']):<9}{str(row['scale']):<7}"
            f"{str(row['mode']):<8}{row['modules']:>5}  {'；'.join(row['reasons'])[:44]}"
        )
    print()
    print("续跑某作业:  python -m pipeline.cli --resume-job <job_id>")
    print("逐模块子运行: python -m pipeline.cli --resume <job_id>-M-01")
    return 0


def _print_result(result: RunResult, as_json: bool) -> None:
    s = result.summary
    if as_json:
        print(json.dumps(s, ensure_ascii=False, indent=1))
        return
    print()
    if result.paused:
        print(f"状态         : 已暂停（人工闸门停在 {result.paused_after} 之后）")
        print(f"下一步       : python -m pipeline.cli --resume {result.run_id}")
        print(f"打回重跑     : python -m pipeline.cli --resume {result.run_id} --from <阶段> --feedback \"...\"")
        print(f"产物目录     : {result.run_dir}")
        print(f"待人工清单   : {result.run_dir / 'handoff.md'}")
        return
    print(f"verdict      : {s['verdict']}{'（需要人工介入）' if s['needs_human'] else ''}")
    print(f"迭代轮次     : {s['attempts']}（评审 {s['rounds']} 次，review_every={s['review_every']}）")
    print(f"模型切换次数 : {s['model_switches']}  累计加载耗时: {s['total_load_s']}s")
    print(f"总耗时       : {s['wall_s']}s")
    print(f"tokens       : prompt {s['total_prompt_tokens']} / output {s['total_output_tokens']}")
    print(f"产物目录     : {result.run_dir}")
    if s.get("grounding_warnings"):
        print(f"待人工确认   : {result.run_dir / 'handoff.md'}")
    for call in s["calls"]:
        flags = []
        if call["switched"]:
            flags.append("切换")
        if call["attempt"] > 1:
            flags.append(f"契约重试x{call['attempt']}")
        if call["truncated"]:
            flags.append("已裁剪")
        if call["prompt_over_budget"]:
            flags.append("预算告警")
        if call.get("human_feedback_used"):
            flags.append("含人工意见")
        print(
            f"  - {call['stage']:<17} {call['tag']:<26} "
            f"{call['wall_s']:>6.1f}s load {call['load_s']:>5.1f}s "
            f"prompt {call['prompt_tokens']:>6} out {call['output_tokens']:>5} {' '.join(flags)}"
        )


def _print_flow() -> int:
    """打印流定义（Mermaid）+ 跨表一致性校验结果（--show-flow）。"""
    problems = flow.validate()
    print("== 流定义（单一真源：pipeline/flow.py）")
    print(flow.mermaid())
    print()
    print(f"节点       : {', '.join(flow.NODES)}")
    print(f"前置节点   : {', '.join(flow.PRE_NODES)}（入口总闸，不进执行顺序）")
    for node, table in flow.PRE_EDGES.items():
        print(f"  分支      : {node} → " + " / ".join(f"{k}={v}" for k, v in table.items()))
    print(f"模型阶段   : {', '.join(flow.MODEL_NODES + flow.PRE_NODES)}")
    print(f"可暂停阶段 : {', '.join(flow.PAUSABLE_NODES)}")
    print(f"人工闸门   : {', '.join(f'{s.stage}({s.kind})' for s in flow.GATE_SPECS)}")
    print(f"入口总闸   : {', '.join(gateway.MODES)}（当前 {gateway.local_config.guard().get('mode')}）"
          f"；禁区 {len(gateway.forbidden_paths())} 条")
    if problems:
        print("\n== 一致性校验：不通过")
        for item in problems:
            print(f"  - {item}")
        return 4
    print("\n== 一致性校验：通过（STAGE_MODELS / STAGE_SCHEMAS / STAGE_STATE_KEY / 闸门声明均与流定义一致）")
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    # --show-flow 是纯查询：不受「必须给需求」的互斥组约束，也不加载任何模型
    if "--show-flow" in argv:
        return _print_flow()
    args = parse_args(argv)
    # 启动期校验流定义：新增阶段漏登记时，在这里直接报错而不是等真机跑挂
    flow.assert_valid()

    if args.list:
        return _print_runs()
    if args.list_jobs:
        return _print_jobs()

    pause_after = _parse_stages(args.pause_after, "--pause-after")
    if args.pause_after and pause_after is None:
        return 2
    if args.no_pause:
        pause_after = []
    if args.no_pause and args.pause_after:
        print("--no-pause 与 --pause-after 不能同时用", file=sys.stderr)
        return 2
    only = _parse_stages(args.only, "--only")
    if args.only and only is None:
        return 2
    if args.from_stage and args.from_stage not in ALL_STAGES:
        print(f"--from 未知阶段: {args.from_stage}，可选: {ALL_STAGES}", file=sys.stderr)
        return 2
    if args.from_stage and args.from_checkpoint is not None:
        print("--from 与 --from-checkpoint 不能同时用", file=sys.stderr)
        return 2
    if (args.from_stage or args.feedback or args.from_checkpoint is not None) and not args.resume:
        print("--from / --from-checkpoint / --feedback 只能和 --resume 一起用", file=sys.stderr)
        return 2

    client = MockClient(rework_first=args.mock_rework) if args.mock else OllamaClient(OLLAMA_HOST, timeout=REQUEST_TIMEOUT)
    orch = Orchestrator(
        client=client,
        repo=args.repo,
        runs_dir=args.out,
        max_rework=MAX_REWORK_ROUNDS if args.max_rework is None else args.max_rework,
        unload_at_end=not args.keep_warm,
        review_every=args.review_every,
        pause_after=pause_after or [],
        project_type=args.project_type,
    )

    try:
        if args.resume_job:
            data = gateway.resume_job(
                args.resume_job,
                runs_dir=args.out,
                client=client,
                logger=print,
                pause_after=pause_after or [],
                review_every=args.review_every,
                max_rework=args.max_rework,
                project_type=args.project_type,
            )
            _print_job(data, args.json)
            return 0
        if args.resume:
            run_dir = Path(args.resume)
            if not run_dir.exists():
                run_dir = Path(args.out) / args.resume
            if not run_dir.exists():
                print(f"找不到运行目录: {args.resume}", file=sys.stderr)
                return 2
            result = orch.resume(
                run_dir,
                from_stage=args.from_stage,
                feedback=args.feedback,
                pause_after=pause_after,
                review_every=args.review_every,
                max_rework=args.max_rework,  # None = 沿用该 run 原本的上限
                mock=True if args.mock else None,  # 不传则沿用该 run 自己的模式
                issue_kind=(args.issue_kind or "").strip(),
                from_checkpoint=args.from_checkpoint,
            )
        else:
            requirement = (
                Path(args.requirement_file).read_text(encoding="utf-8") if args.requirement_file else args.requirement
            )
            if not requirement or not requirement.strip():
                print("需求为空", file=sys.stderr)
                return 2
            if only:
                # `--only` 是调试手段（只跑指定阶段），此时拆模块没有意义：跳过入口总闸
                print("== 规模路由：small（off）→ --only 调试模式，跳过入口总闸")
            else:
                route = gateway.dispatch(
                    requirement,
                    repo=args.repo,
                    runs_dir=args.out,
                    mode=args.gateway,
                    scale_override=args.scale,
                    client=client,
                    forbidden=_parse_list(args.forbidden),
                )
                print(f"== 规模路由：{route.describe()}")
                for item in route.reasons:
                    print(f"   - {item}")
                for item in route.notes:
                    print(f"   ! {item}")
                if route.scale == "large":
                    assert route.job_id is not None
                    if args.run_id:
                        # 页面发起的那次运行只剩日志：先写指针，让它立刻知道东西在作业目录里
                        gateway.link_run(
                            args.out,
                            args.run_id,
                            job_id=route.job_id,
                            reasons=route.reasons,
                            status="running",
                            modules=len((route.ga or {}).get("modules") or []),
                        )
                    data = gateway.run_job(
                        route.job_id,
                        runs_dir=args.out,
                        client=client,
                        repo=args.repo,
                        pause_after=pause_after or [],
                        review_every=args.review_every,
                        max_rework=args.max_rework,
                        project_type=args.project_type,
                    )
                    if args.run_id:
                        gateway.link_run(
                            args.out,
                            args.run_id,
                            job_id=route.job_id,
                            reasons=route.reasons,
                            status=gateway.job_status(data),
                            modules=len(data.get("modules") or []),
                        )
                    _print_job(data, args.json)
                    return 0
            result = orch.run(requirement, stages=only, run_id=args.run_id)
    except KeyboardInterrupt:
        print("\n已中断（产物已落盘，可用 --resume 续跑）", file=sys.stderr)
        return 130
    except OrchestratorError as exc:
        print(f"编排失败: {exc}", file=sys.stderr)
        return 4

    _print_result(result, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
