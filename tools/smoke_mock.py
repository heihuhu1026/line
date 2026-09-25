"""离线冒烟：验证编排、契约、回流、评审频率、人工闸门与续跑（不加载任何模型）。

用法: python tools/smoke_mock.py
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 隔离**本机配置**（`pipeline/config.local.json`，操作页面「配置」页写入的那份）。
#
# 为什么必须做：本套断言里有 10 条是**针对代码默认闸门行为**的（人工审核闸门不错停、
# mandatory 闸门、打回后追加预算…），而 `config.apply_overrides()` 会在导入时把
# config.local.json 的覆盖应用到全局。于是运营侧只要在页面上把闸门开关一关，
# 冒烟立刻报出 10 条「失败」—— 全是假阴性/假阳性，谁看谁去查一个不存在的 bug
# （真机复现：把三个闸门关掉后 exit=1、FAIL=10；指开配置就恢复 417/0/exit 0）。
#
# 覆盖面只到「默认配置下的行为」这一层，因此这里指到一个**不存在**的路径，
# 让 config 用代码默认值跑。若调用方显式设了 PIPELINE_LOCAL_CONFIG（想验覆盖），
# 就尊重它、不覆盖。
if not os.environ.get("PIPELINE_LOCAL_CONFIG"):
    os.environ["PIPELINE_LOCAL_CONFIG"] = str(
        Path(tempfile.gettempdir()) / "pipeline_smoke_no_local_config.json"
    )

from pipeline import advice, issues, prompts, retrieval, runstore  # noqa: E402
from pipeline.ollama_client import MockClient, OllamaClient  # noqa: E402
from pipeline.orchestrator import Orchestrator  # noqa: E402

REQ = "给客户列表页增加导出 Excel 按钮，导出当前筛选结果"

CASES = [
    # (名称, mock rework 轮数, max_rework, review_every, 期望 verdict, 期望迭代轮数, 期望评审次数)
    ("single-pass", 0, 2, 1, "pass", 1, 1),
    ("rework-once-then-pass", 1, 2, 1, "pass", 2, 2),
    ("review-every-2-cadence", 1, 2, 2, "pass", 3, 2),
    ("rework-exceeds-cap", 1, 0, 1, "needs_human", 1, 1),
]

failures: list[str] = []
checks = 0


def check(cond: bool, label: str, detail: str = "") -> bool:
    global checks
    checks += 1
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    if not cond:
        failures.append(f"{label} {detail}".strip())
    return bool(cond)


class UncoveredClient(MockClient):
    """开发把 covers_tasks 填成不存在的 id 且不声明未实现：覆盖审计必须机械发现。"""

    def chat_json(self, spec, system, user, schema, num_predict=None, attempts=2):
        data, meta = super().chat_json(spec, system, user, schema, num_predict, attempts)
        if spec.role.startswith("开发"):
            for edit in data.get("edits") or []:
                edit["covers_tasks"] = ["task_that_does_not_exist"]
            data["not_implemented"] = []
        return data, meta


class ExternalOnlyReviewClient(MockClient):
    """评审只提「需要外部确认」的返工项：机制应当强制放行，而不是空转到触顶。"""

    def chat_json(self, spec, system, user, schema, num_predict=None, attempts=2):
        data, meta = super().chat_json(spec, system, user, schema, num_predict, attempts)
        if spec.role == "评审":
            data["verdict"] = "rework_dev"
            data["required_fixes"] = ["确认后端导出接口是否存在", "阈值取多少需业务确认"]
            data["required_fixes_detail"] = [
                {"fix": "确认后端导出接口是否存在", "scope": "needs_external", "why": "需运行系统确认"},
                {"fix": "阈值取多少需业务确认", "scope": "needs_external", "why": "需人工确认"},
            ]
            data["blockers"] = []
        return data, meta


PATCH_REPO_SOURCE = (
    "import sqlite3\n\n\n"
    "def index_all(conn, roots, batch=300):\n"
    '    """扫描并写入索引"""\n'
    "    cur = conn.cursor()\n"
    "    total = 0\n"
    "    for i in range(batch):\n"
    "        cur.execute('insert into cards values (?)', (i,))\n"
    "        total += 1\n"
    "    conn.commit()\n"
    "    return {'total': total}\n\n\n"
    "def other():\n"
    "    return 1\n"
)

PATCH_FIXTURE_IMPL = {
    "summary": "mock 实现",
    "edits": [
        {  # 可套用：anchor 唯一 + 语义自洽
            "path": "mod.py", "change_type": "add", "target_symbol": "new_helper",
            "anchor": "def index_all(conn, roots, batch=300):", "patch_mode": "insert_after",
            "patch": "def new_helper(conn):\n    return 1\n",
            "covers_tasks": ["t1"], "rationale": "插入新助手函数",
        },
        {  # 谎称完整替换，但只给 2 行（原文该符号 11 行）
            "path": "mod.py", "change_type": "modify", "target_symbol": "index_all",
            "anchor": "def index_all(conn, roots, batch=300):", "patch_mode": "full_symbol",
            "patch": "def index_all(conn):\n    return {}\n",
            "covers_tasks": ["t2"], "rationale": "改 index_all",
        },
        {  # anchor 根本不在原文里
            "path": "mod.py", "change_type": "modify", "target_symbol": "ghost",
            "anchor": "def ghost_function_that_does_not_exist():", "patch_mode": "replace_span",
            "patch": "def ghost():\n    pass\n",
            "covers_tasks": ["t3"], "rationale": "改不存在的函数",
        },
        {  # replace_span 只框住签名却给了完整定义 → 贴回去会留下原函数体
            "path": "mod.py", "change_type": "modify", "target_symbol": "index_all",
            "anchor": "def index_all(conn, roots, batch=300):", "patch_mode": "replace_span",
            "patch": "def index_all(conn, roots, batch=300):\n    return {'total': 0}\n",
            "covers_tasks": ["t4"], "rationale": "只换签名",
        },
    ],
    "not_implemented": [],
    "self_checks": [{"claim": "改完了", "evidence": "mod.py index_all"}],
    "deviations": [],
}


class PatchClient(MockClient):
    """开发返回一份带三种典型问题的补丁清单，用于验证补丁机械校验与套用。
    开发现在是两遍：第一遍只铺（可套用的）辅助函数，第二遍回填（带问题的）主函数体。
    把 fixture 拆开分别喂两遍——既贴合两遍真实形态，又保住 ok==1/problems==3 的契约。"""

    def chat_json(self, spec, system, user, schema, num_predict=None, attempts=2):
        data, meta = super().chat_json(spec, system, user, schema, num_predict, attempts)
        if spec.role.startswith("开发"):
            fixture = json.loads(json.dumps(PATCH_FIXTURE_IMPL, ensure_ascii=False))
            if "第一遍产物" in user or "第二遍·回填" in user:
                # 第二遍：只给三种典型问题的主函数补丁
                fixture["edits"] = [e for e in fixture["edits"] if e["target_symbol"] != "new_helper"]
            else:
                # 第一遍：只给可套用的辅助函数（不带问题）
                fixture["edits"] = [e for e in fixture["edits"] if e["target_symbol"] == "new_helper"]
            data.update(fixture)
        return data, meta


class AllNotImplementedClient(PatchClient):
    """开发把方案任务全部声明为未实现、不给任何真实补丁（真机上出现过的"退缩"行为）。"""

    def chat_json(self, spec, system, user, schema, num_predict=None, attempts=2):
        data, meta = super().chat_json(spec, system, user, schema, num_predict, attempts)
        if spec.role.startswith("开发"):
            task_ids = re.findall(r'"id"\s*:\s*"([^"]+)"', user) or ["t1"]
            data["edits"] = [
                {
                    "path": "mod.py", "change_type": "modify", "target_symbol": "not_implemented",
                    "anchor": "def index_all(conn, roots, batch=300):", "patch_mode": "insert_after",
                    "patch": "# TODO", "covers_tasks": [task_ids[0]], "rationale": "什么都没做",
                }
            ]
            data["not_implemented"] = [{"task": t, "reason": "上下文不足，先不做"} for t in task_ids]
        return data, meta


class PassAlwaysPatchClient(PatchClient):
    """评审无论看到什么都给 pass（模拟"宽容的评审"）：补丁有阻断级问题时机制必须推翻它。"""

    def chat_json(self, spec, system, user, schema, num_predict=None, attempts=2):
        data, meta = super().chat_json(spec, system, user, schema, num_predict, attempts)
        if spec.role == "评审":
            data["verdict"] = "pass"
            data["required_fixes"] = []
            data["required_fixes_detail"] = []
            data["blockers"] = []
        return data, meta


class ArchitectBlameExternalClient(ExternalOnlyReviewClient):
    """评审判 rework_architect（方案本身有错），却把返工项全归为 needs_external。

    这是分类自相矛盾：方案错误是**本轮材料内可改**的。机制既不能借「需外部确认」
    放行（等于带着已知设计缺陷交付），也不该就此回流（in_material 为空，架构师
    拿不到任何具体指示），应转人工裁决。
    """

    def chat_json(self, spec, system, user, schema, num_predict=None, attempts=2):
        data, meta = super().chat_json(spec, system, user, schema, num_predict, attempts)
        if spec.role == "评审":
            data["verdict"] = "rework_architect"
        return data, meta


def make(root: Path, name: str, **kwargs) -> Orchestrator:
    return Orchestrator(
        client=kwargs.pop("client", MockClient()),
        repo=kwargs.pop("repo", None),
        runs_dir=root / name,
        unload_at_end=False,
        log=lambda *_: None,
        # PM 未决项闸门默认开着 —— 冒烟跑的是调度与契约，每次 run 都停在 pm 会淹没断言。
        # 该闸门由下面 pm-open-questions-gate 那个 case 专门覆盖。
        pause_on_open_questions=kwargs.pop("pause_on_open_questions", False),
        **kwargs,
    )


def dump_ok(run_dir: Path) -> bool:
    files = sorted(p.name for p in run_dir.glob("*.json"))
    for path in run_dir.glob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))
    return bool(files)


def approve_human_review(orch: "Orchestrator", run_dir: Path) -> object:
    """模拟人工在闸门提交「通过」：写一份 approve 产物并续跑。"""
    runstore.save_artifact(
        run_dir,
        "human_review",
        {
            "verdict": "approve",
            "core_path_ok": True,
            "no_obvious_errors": True,
            "deliverables_complete": True,
            "requirement_met": True,
            "notes": "冒烟自动通过",
            "reviewer": "smoke",
        },
    )
    return orch.resume(run_dir)


def settle(orch: "Orchestrator", result: object) -> object:
    """反复通过人工审核闸门（自动 approve），直到不再停在 human_review。"""
    while getattr(result, "paused", False) and getattr(result, "paused_after", None) == "human_review":
        result = approve_human_review(orch, result.run_dir)
    return result


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="pipeline-smoke-"))
    # 测试必须与「**用户可调的策略开关**」解耦：intake 的条件闸门由
    # config.local.json 的 runtime.intake_pause_on_gaps 控制，本机一旦打开，
    # 下面所有「应当一路跑到 done」的断言都会变成 paused（实测 21 条红）。
    # 这里强制关掉，让结果只取决于代码本身。该闸门的**条件逻辑**另有单点验证
    # （见 orchestrator._intake_high_gaps / _gate_after 的回放验证），不靠本套件覆盖；
    # 本套件里所有闸门用例都走显式 pause_after，不受此开关影响。
    import pipeline.orchestrator as _orch_mod

    _orch_mod.INTAKE_PAUSE_ON_GAPS = False
    try:
        # ---------------------------------------------------------- 基础回流与评审频率
        for name, mock_rework, max_rework, review_every, want_verdict, want_attempts, want_reviews in CASES:
            print(f"\n== {name}")
            orch = make(
                root,
                name,
                client=MockClient(rework_first=mock_rework),
                max_rework=max_rework,
                review_every=review_every,
            )
            result = orch.run(REQ)
            result = settle(orch, result)
            s = result.summary
            check(
                (s["verdict"], s["attempts"], s["rounds"]) == (want_verdict, want_attempts, want_reviews),
                f"verdict/attempts/reviews = {want_verdict}/{want_attempts}/{want_reviews}",
                f"实际 {s['verdict']}/{s['attempts']}/{s['rounds']}",
            )
            check(all(s["artifacts"].values()), "六阶段产物齐全")
            check(dump_ok(result.run_dir), "阶段 JSON 均可解析")
            check(runstore.read_state(result.run_dir)["status"] == "done", "state.status = done")
            if not mock_rework and review_every == 1:
                check(len(s["calls"]) == 8, "全程 8 次模型调用（intake + pm + dev 两遍…）", str(len(s["calls"])))
                check(s["model_switches"] == 3, "单驻留切换 3 次（8B→14B→coder→14B）", str(s["model_switches"]))
            if want_verdict == "needs_human":
                check(s["needs_human"], "needs_human 已置位")
                check((result.run_dir / runstore.HANDOFF_NAME).exists(), "生成 handoff.md 待人工清单")

        # ---------------------------------------------------------- 人工闸门 + 续跑
        print("\n== gate-pause-and-resume")
        orch = make(root, "gate", pause_after=["pm", "architect_plan"])
        result = orch.run(REQ)
        gate_run_dir = result.run_dir  # 后面 issue-report 会把 run 目录平铺到 _flat/，这里先记住
        check(result.paused and result.paused_after == "pm", "PM 后暂停", f"paused={result.paused}/{result.paused_after}")
        check(result.summary["cursor"] == "retrieve", "暂停后游标指向下一步", result.summary["cursor"])
        check(len(result.summary["calls"]) == 2, "暂停时调用了 2 次模型（intake + pm）")

        orch2 = make(root, "gate")
        result2 = orch2.resume(result.run_dir)
        check(result2.paused and result2.paused_after == "architect_plan", "方案后再次暂停（闸门沿用）")
        check(result2.summary["cursor"] == "dev", "再次暂停后游标 = dev", result2.summary["cursor"])
        check([c["stage"] for c in result2.summary["calls"]] == ["intake", "pm", "architect_assess", "architect_plan"],
              "续跑保留了上一段的埋点")

        # 故意传一个真实客户端：mock 运行必须自动切回 MockClient（否则会误加载真实模型）
        orch3 = make(root, "gate", client=OllamaClient("http://127.0.0.1:1", timeout=1))
        result3 = orch3.resume(result.run_dir, pause_after=[])
        result3 = settle(orch3, result3)
        check(isinstance(orch3.client, MockClient), "mock 运行续跑自动恢复 MockClient")
        check(result3.summary["verdict"] == "pass" and result3.summary["status"] == "done", "清空闸门后跑完 = pass")
        check(len(result3.summary["calls"]) == 8, "三段续跑累计 8 次调用（无重复执行，含 intake + dev 两遍）",
              str(len(result3.summary["calls"])))
        check(result3.summary["status"] == "done" and "wall_s" in result3.summary, "耗时跨续跑累计并落到 summary")
        check(not (result.run_dir / "superseded").exists(), "正常续跑不作废任何产物")
        check((result.run_dir / runstore.SUMMARY_NAME).exists(), "跑完后写出 summary.json")
        state = runstore.read_state(result.run_dir)
        check(state["status"] == "done" and state["cursor"] == "done", "state 收尾为 done")

        # ---------------------------------------------------------- 人工编辑产物后生效
        print("\n== human-edit-artifact")
        orch = make(root, "edit", pause_after=["pm"])
        result = orch.run(REQ)
        pm_file = sorted(result.run_dir.glob("02-pm.json"))[0]
        payload = json.loads(pm_file.read_text(encoding="utf-8"))
        payload["artifact"]["change_request"] = "EDITED-BY-HUMAN-导出必须走流式"
        pm_file.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

        orch2 = make(root, "edit", client=MockClient())
        result2 = orch2.resume(result.run_dir, feedback="人工意见：不要动 config.py")
        edit_run_dir = result2.run_dir  # 这次运行里既有目标阶段注入，也有后续阶段的事实传导
        assess_file = sorted(result.run_dir.glob("03-architect_assess.json"))[0]
        preview = json.loads(assess_file.read_text(encoding="utf-8"))["request_preview"]
        check("EDITED-BY-HUMAN" in preview, "人工编辑的 artifact 进入了下游 prompt")
        check("不要动 config.py" in preview, "人工意见进入了目标阶段 prompt")
        check("EDITED-BY-HUMAN" in json.dumps(result2.artifacts, ensure_ascii=False), "编辑后的 scope 写入汇总")

        # ---------------------------------------------------------- 打回重跑某阶段
        print("\n== rerun-from-dev")
        orch = make(root, "rerun", client=MockClient(rework_first=1), max_rework=0)
        result = orch.run(REQ)
        check(result.summary["verdict"] == "needs_human", "先跑到 needs_human", result.summary["verdict"])

        orch2 = make(root, "rerun", client=MockClient(rework_first=0))
        result2 = orch2.resume(result.run_dir, from_stage="dev", feedback="按评审意见补测")
        result2 = settle(orch2, result2)
        check(result2.summary["verdict"] == "pass", "打回开发后重跑通过", result2.summary["verdict"])
        check(
            [c["stage"] for c in result2.summary["calls"][-3:]] == ["dev", "test", "review"],
            "只重跑了 dev/test/review",
        )
        superseded = sorted(p.name for p in (result.run_dir / runstore.SUPERSEDED_DIR).glob("*.json"))
        check(len(superseded) == 5, "旧产物归档到 superseded/（dev 两遍多一个文件）", str(superseded))
        check(
            superseded == ["05-dev.json", "06-dev.json", "07-test.json", "08-verify.json", "09-review.json"],
            "归档文件名符合两遍编号（含 verify）",
            str(superseded),
        )
        check(
            any(c.get("human_feedback_used") for c in result2.summary["calls"]),
            "埋点记录该次调用使用了人工意见",
        )

        # ---------------------------------------------------------- 问题记录
        print("\n== issue-record")
        rerun_dir = root / "rerun" / sorted(p.name for p in (root / "rerun").iterdir())[-1]
        for name in ("issues.json", "issues.jsonl", "issues.md", "env.json", "traces.jsonl"):
            check((rerun_dir / name).exists(), f"生成 {name}")

        env = json.loads((rerun_dir / "env.json").read_text(encoding="utf-8"))
        check(
            len(env["fingerprint"]["pipeline_hash"]) == 12 and env["fingerprint"]["prompts_hash"],
            "env.json 记录提示词/配置指纹",
            str(env["fingerprint"]),
        )
        check("pm" in env["prompt_chars"], "env.json 记录各阶段提示词长度")

        traces = runstore.read_traces(rerun_dir)
        calls = json.loads((rerun_dir / "state.json").read_text(encoding="utf-8"))["calls"]
        check(len(traces) >= len(calls), "trace 条数不少于调用数", f"{len(traces)} vs {len(calls)}")
        check(
            all(t.get("system") and t.get("user") and t.get("raw_text") is not None for t in traces),
            "trace 含完整 system/user/模型原始输出",
        )
        check(
            all("failed_attempts" in t and "usage" in t for t in traces),
            "trace 含契约失败原文与用量",
        )
        first = traces[0]
        stage_file = json.loads((rerun_dir / "01-intake.json").read_text(encoding="utf-8"))
        check(
            first["user"] and stage_file["request_preview"].startswith(first["user"][:50]),
            "trace 与 NN-stage.json 记录的是同一次调用的 prompt",
            f"len(trace)={len(first['user'])} len(preview)={len(stage_file['request_preview'])}",
        )
        check(
            first["system"] == prompts.SYSTEM["intake"],
            "trace 保留了完整 system 提示词（request_preview 里没有）",
        )

        rerun_state = json.loads((rerun_dir / "state.json").read_text(encoding="utf-8"))
        collected = issues.collect_issues(rerun_state, "rerun")
        kinds = {i.kind for i in collected}
        check("human_rework" in kinds and "human_directive" in kinds, "识别出人工打回与人工意见", str(sorted(kinds)))
        check(
            any(a["action"] == "human_rework" for a in rerun_state["human_actions"]),
            "human_actions 留痕人工打回",
            str(rerun_state["human_actions"]),
        )

        cap_dir = root / "rework-exceeds-cap" / sorted(p.name for p in (root / "rework-exceeds-cap").iterdir())[-1]
        cap_state = json.loads((cap_dir / "state.json").read_text(encoding="utf-8"))
        cap_issues = issues.collect_issues(cap_state, "cap")
        cap_kinds = {i.kind for i in cap_issues}
        check("review_required_fix" in cap_kinds, "识别出评审必改项", str(sorted(cap_kinds)))
        check("needs_human" in cap_kinds, "识别出回流触顶")
        check(issues.summarize(cap_issues)["blockers"] >= 1, "统计出阻断项",
              str(issues.summarize(cap_issues)["by_severity"]))

        print("\n== issue-recurrence")
        recur = make(root, "recur", client=MockClient(rework_first=3), max_rework=2, review_every=1)
        recur_result = recur.run(REQ)
        check(recur_result.summary["rounds"] == 3, "三轮都评审了", str(recur_result.summary["rounds"]))
        recur_issues = issues.collect_issues(
            json.loads((recur_result.run_dir / "state.json").read_text(encoding="utf-8")), "recur"
        )
        fixes = [i for i in recur_issues if i.kind == "review_required_fix"]
        check(len(fixes) == 3, "三轮各记一条必改项", str(len(fixes)))
        check(
            [i.occurrence for i in fixes] == [1, 2, 3] and all(i.recurred for i in fixes if i.occurrence > 1),
            "同一问题跨轮次判为复发",
            str([(i.occurrence, i.recurred) for i in fixes]),
        )

        print("\n== issue-report")
        # 冒烟里每次用例各占一个目录，先把所有 run 目录平铺到一个根下，模拟真实 runs/
        flat = root / "_flat"
        flat.mkdir(exist_ok=True)
        for case_dir in [d for d in root.iterdir() if d.is_dir() and not d.name.startswith(("_", "."))]:
            for run_dir in [d for d in case_dir.iterdir() if d.is_dir()]:
                shutil.move(str(run_dir), str(flat / f"{case_dir.name}-{run_dir.name}"))
        report = issues.build_report(flat)
        check(report["runs_analyzed"] >= 5, "汇总覆盖所有运行", str(report["runs_analyzed"]))
        check(report["totals"]["issues"] > 0, "汇总出问题总数", str(report["totals"]["issues"]))
        check("by_stage" in report and report["by_stage"], "汇总各阶段耗时")
        markdown = issues.report_markdown(report)
        check("问题总览" in markdown and "by_kind" not in markdown, "生成可读报告（不含内部字段名）")
        meta_prompt = issues.build_meta_prompt(report, {"review": prompts.SYSTEM["review"][:200]})
        check("问题分布" in meta_prompt and "review_required_fix" in meta_prompt, "元优化材料包含问题分类")
        check(issues.META_PROPOSAL_SCHEMA["required"], "元优化 schema 可用")

        print("\n== issue-fingerprint")
        before = issues.pipeline_fingerprint()
        original = prompts.SYSTEM["pm"]
        prompts.SYSTEM["pm"] = original + "\n（临时改动）"
        try:
            after = issues.pipeline_fingerprint()
            check(after["prompts_hash"] != before["prompts_hash"], "改提示词后 prompts_hash 变化")
            check(after["pipeline_hash"] != before["pipeline_hash"], "改提示词后 pipeline_hash 变化")
            check(after["config_hash"] == before["config_hash"], "只改提示词时 config_hash 不变")
        finally:
            prompts.SYSTEM["pm"] = original
        check(issues.pipeline_fingerprint()["pipeline_hash"] == before["pipeline_hash"], "还原后指纹一致")

        original_w = retrieval.EXT_WEIGHT[".json"]
        retrieval.EXT_WEIGHT[".json"] = 0.99
        try:
            tuned = issues.pipeline_fingerprint()
            check(tuned["retrieval_hash"] != before["retrieval_hash"], "改检索权重后 retrieval_hash 变化")
            check(tuned["prompts_hash"] == before["prompts_hash"], "改检索权重不影响 prompts_hash")
        finally:
            retrieval.EXT_WEIGHT[".json"] = original_w
        check(issues.pipeline_fingerprint()["retrieval_hash"] == before["retrieval_hash"], "还原后检索指纹一致")

        # ---------------------------------------------------------- 检索窗口（真机教训的回归）
        print("\n== retrieval-window")
        repo = root / "_repo"
        repo.mkdir(parents=True, exist_ok=True)
        filler = "\n".join(f"def filler_{i}(a, b):\n    return a + b  # 填充 {i}\n" for i in range(400))
        (repo / "big_module.py").write_text(
            "import sqlite3\nimport os\n\n# 模块说明：批量提交到 SQLite 的索引器\n"
            + filler
            + "\n\ndef index_all(conn, roots, batch=300):\n"
            '    """扫描并写入索引"""\n'
            "    cur = conn.cursor()\n"
            "    return {'total': 0}\n",
            encoding="utf-8",
        )
        (repo / "small.py").write_text("def index_all(conn):\n    return conn\n", encoding="utf-8")
        # 补几个无关文件：语料太小会让 IDF 把关键词也当"高频词"丢掉（测试环境问题，不是检索的行为）
        for i in range(6):
            (repo / f"decoy_{i}.py").write_text(f"def unrelated_{i}():\n    return {i}\n", encoding="utf-8")
        query = "index_all 需要崩溃安全：批量提交的 batch 参数要改"
        got = retrieval.select_excerpts(repo, query=query, token_budget=1500, per_file_tokens=300)
        big = next((e for e in got if e.path == "big_module.py"), None)
        small = next((e for e in got if e.path == "small.py"), None)
        check(big is not None, "长文件被选中")
        if big:
            check("index_all" in big.text, "符号锚点：片段命中需求里写的 index_all", big.note)
            check("…（中间省略）…" in big.text, "片段是「文件头 + 命中窗口」两段拼的")
            check("cur = conn.cursor()" in big.text, "片段取到了文件深处的目标函数体")
            check(big.text.startswith("import sqlite3"), "片段保留文件头（导入与说明）")
            check("第 " in big.note and "文件头" in big.note, "片段标注来源行范围", big.note)
        # 内容相同的 .py 与 .json：代码必须优先（真机教训：zh-cn.json 挤进 top-N 白吃预算）
        same = "def index_all(conn, roots, batch=300):\n    return conn\n"
        (repo / "same_py.py").write_text(same, encoding="utf-8")
        (repo / "same_json.json").write_text(json.dumps({"index_all": same, "batch": 300}), encoding="utf-8")
        scores = {e.path: e.score for e in retrieval.select_excerpts(repo, query=query, token_budget=4000, per_file_tokens=300)}
        check(
            scores.get("same_py.py", 0) > scores.get("same_json.json", 0),
            "同内容时 .py 优先级高于 .json",
            f"py={scores.get('same_py.py')} json={scores.get('same_json.json')}",
        )
        check(small is not None, "小文件被选中")
        if small:
            check(not small.truncated and small.note == "", "小文件整份给出、不加标注", f"truncated={small.truncated}")

        # ---------------------------------------------------------- 机制：覆盖审计 + 评审分流
        print("\n== mechanisms")
        audit_orch = make(root, "audit", client=UncoveredClient())
        audit_result = audit_orch.run(REQ)
        audit_state = json.loads((audit_result.run_dir / "state.json").read_text(encoding="utf-8"))
        audit = audit_state["artifacts"]["implementation_audit"]
        check(bool(audit["missing"]), "覆盖审计发现未被覆盖的任务", str(audit["missing"]))
        check(bool(audit["unknown_tasks"]), "覆盖审计发现编造的任务 id", str(audit["unknown_tasks"]))
        audit_issues = {i.kind for i in issues.collect_issues(audit_state, "audit")}
        check("plan_task_uncovered" in audit_issues, "未覆盖任务记成问题", str(sorted(audit_issues)))
        check("fabricated_task_ref" in audit_issues, "编造任务 id 记成问题")
        review_traces = [t for t in runstore.read_traces(audit_result.run_dir) if t["stage"] == "review"]
        check(bool(review_traces) and "实现覆盖审计" in review_traces[-1]["user"], "审计结论进了评审 prompt")
        check("task_that_does_not_exist" in review_traces[-1]["user"], "评审能看到被编造的任务 id")

        ext_orch = make(root, "external-only", client=ExternalOnlyReviewClient(), max_rework=2)
        ext_result = ext_orch.run(REQ)
        ext_result = settle(ext_orch, ext_result)
        ext_state = json.loads((ext_result.run_dir / "state.json").read_text(encoding="utf-8"))
        check(ext_result.summary["attempts"] == 1, "全需外部确认时不空转（1 轮结束）",
              str(ext_result.summary["attempts"]))
        check(ext_result.summary["verdict"] == "pass", "机制放行为 pass", ext_result.summary["verdict"])
        check(not ext_result.summary["needs_human"], "不算触顶")
        ext_review = ext_state["artifacts"]["review"]
        check(ext_review["required_fixes"] == [], "需外部确认的项不再作为必改项",
              str(ext_review["required_fixes"]))
        ext_risks = [r for r in (ext_review["residual_risks"] or []) if isinstance(r, dict)]
        check(
            bool(ext_risks) and all(r.get("issue") for r in ext_risks),
            "残留风险为 {issue, reason, impact} 结构",
            str(ext_review["residual_risks"]),
        )
        check(
            any("需运行系统" in (r.get("reason") or "") for r in ext_risks),
            "需外部确认的项转成残留风险（带原因与影响）",
            str(ext_review["residual_risks"]),
        )
        ext_issues = {i.kind for i in issues.collect_issues(ext_state, "external")}
        check("review_forced_pass" in ext_issues, "记录「被机制放行」", str(sorted(ext_issues)))
        ext_handoff = (ext_result.run_dir / runstore.HANDOFF_NAME).read_text(encoding="utf-8")
        check("被机制强制放行" in ext_handoff, "待人工清单里提示需复核")

        print("\n== rework-architect-external（分类矛盾不放行）")
        amb_orch = make(
            root, "architect-ambiguous", client=ArchitectBlameExternalClient(), max_rework=2
        )
        amb_result = settle(amb_orch, amb_orch.run(REQ))
        amb_state = json.loads((amb_result.run_dir / "state.json").read_text(encoding="utf-8"))
        amb_review = (amb_state.get("artifacts") or {}).get("review") or {}
        check(amb_review.get("verdict") == "rework_architect",
              "判 rework_architect 时不被强制放行", str(amb_review.get("verdict")))
        check(bool(amb_review.get("escalated_ambiguous")), "标记为分类矛盾")
        check(bool(amb_result.summary.get("needs_human")), "转人工裁决")

        print("\n== grounding（事实接地）")
        # mock 模式下接地校验是关的（产物是合成占位符，路径无意义），
        # 这里用非 mock 客户端 + 构造产物直接覆盖校验逻辑本身。
        g = make(root, "grounding", repo=None)
        g.client = OllamaClient("http://127.0.0.1:1", timeout=1)  # 只为让 isinstance 判定为真，不实际调用
        g.requirement = "在 tools/real.py 里增加一个函数"
        g.pool = []
        g.project_type = "secondary"
        g.state = {}
        check(g._grounding_enabled("dev"), "二次开发启用接地校验")
        g.project_type = "new"
        check(not g._grounding_enabled("dev"), "新建项目不校验（路径本来就是新造的）")
        g.project_type = "secondary"
        g.state = {
            "plan": {
                "changes": [{"path": "tools/real.py"}],
                "tasks": [{"id": "T-01", "target_files": ["tools/real.py"]}],
            }
        }
        check(not g._ungrounded({"edits": [{"path": "tools/real.py"}]}, "dev"),
              "方案里规划的文件算有依据")
        check(g._ungrounded({"edits": [{"path": "tools/ghost.py"}]}, "dev") == ["tools/ghost.py"],
              "编造的路径被判未接地")
        check(
            g._ungrounded({"automated_commands": [{"command": "python tools/ghost.py"}]}, "test")
            == ["tools/ghost.py"],
            "测试命令里的编造路径同样能抓到",
        )

        print("\n== pm-open-questions-gate（PM 未决项闸门）")
        # 闸门判定本身：有未决项才停，没有就不该浪费人机交互
        gate_orch = make(root, "pm-gate-check", pause_on_open_questions=True)
        gate_orch.state = {"scope": {"open_questions": []}}
        check(not gate_orch._should_pause("pm"), "无未决项时不触发 PM 闸门")
        gate_orch.state = {"scope": {"open_questions": [{"question": "要不要支持暂停？"}]}}
        check(gate_orch._should_pause("pm"), "有未决项时触发 PM 闸门")
        gate_orch.state = {"scope": {"open_questions": []}}
        gate_orch.pause_after = {"pm"}
        check(gate_orch._should_pause("pm"), "显式 pause_after 仍无条件生效")
        # 端到端：真跑一次，应停在 pm
        gate_run = make(root, "pm-gate-run", pause_on_open_questions=True)
        gate_result = gate_run.run(REQ)
        check(
            bool(getattr(gate_result, "paused", False)) and gate_result.paused_after == "pm",
            "有未决项时真的停在 pm",
            f"paused={gate_result.paused} after={gate_result.paused_after}",
        )
        gate_state = json.loads((gate_result.run_dir / "state.json").read_text(encoding="utf-8"))
        gate_qs = ((gate_state.get("artifacts") or {}).get("scope") or {}).get("open_questions") or []
        check(bool(gate_qs), "停在 pm 时确实产出了未决项", f"{len(gate_qs)} 条")

        print("\n== new-project（0 存量代码）")
        # 新建项目应换用 SYSTEM_NEW 那套提示词，并跳过 architect_assess。
        # 真机教训：assess 在空仓库里会编出不存在的目录（pipeline/core/*、db/*），
        # 再被方案阶段当成既有事实承接。
        new_orch = make(root, "new-project", project_type="new")
        new_result = settle(new_orch, new_orch.run(REQ))
        new_state = json.loads((new_result.run_dir / "state.json").read_text(encoding="utf-8"))
        check(new_state.get("project_type") == "new", "state 记录 project_type=new",
              str(new_state.get("project_type")))
        new_artifacts = new_state.get("artifacts") or {}
        check("assessment" not in new_artifacts, "跳过 architect_assess（无 assessment 产物）",
              str(sorted(new_artifacts)))
        check({"scope", "plan", "implementation", "test_report", "review"}.issubset(new_artifacts),
              "其余阶段照常产出", str(sorted(new_artifacts)))
        new_traces = runstore.read_traces(new_result.run_dir)
        check(not any(t["stage"] == "architect_assess" for t in new_traces),
              "traces 里没有 architect_assess 调用")
        pm_trace = [t for t in new_traces if t["stage"] == "pm"]
        check(bool(pm_trace) and pm_trace[-1]["system"] == prompts.SYSTEM_NEW["pm"],
              "pm 用的是新建项目专用提示词")
        # 续跑必须沿用项目类型：换了套提示词会让前后阶段的上下文基线不一致
        restored = make(root, "new-project-restore", project_type="secondary")
        restored.run_dir = new_result.run_dir
        restored._restore(new_state)
        check(restored.project_type == "new", "续跑沿用 project_type（不被 CLI 默认值覆盖）",
              restored.project_type)

        print("\n== patch-audit")
        from pipeline import patches

        patch_repo = root / "_patch_repo"
        patch_repo.mkdir(parents=True, exist_ok=True)
        (patch_repo / "mod.py").write_text(PATCH_REPO_SOURCE, encoding="utf-8")
        audit = patches.analyze_all(patch_repo, PATCH_FIXTURE_IMPL)
        statuses = [r["status"] for r in audit["edits"]]
        check(audit["ok"] == 1, "anchor 唯一且语义自洽的补丁判为可套用", str(statuses))
        check("patch_incomplete" in statuses, "谎称 full_symbol 但只给 2 行 → 抓到", str(statuses))
        check("anchor_not_found" in statuses, "anchor 不在原文 → 抓到", str(statuses))
        check("patch_span_mismatch" in statuses, "replace_span 只框签名却给完整定义 → 抓到", str(statuses))
        check(audit["problems"] == 3, "问题条数正确", str(audit["problems"]))

        patch_run = root / "patchfiles"
        patch_run.mkdir(parents=True, exist_ok=True)
        written = patches.write_patch_files(patch_run, patch_repo, PATCH_FIXTURE_IMPL, audit)
        check(len(written) == 1, "只为可套用的补丁落盘", str(written))
        patch_text = (patch_run / written[0]["file"]).read_text(encoding="utf-8")
        check("@@ -" in patch_text and "+def new_helper(conn):" in patch_text, "补丁是带行号的 unified diff",
              patch_text.splitlines()[1] if len(patch_text.splitlines()) > 1 else "")

        applied_dir = root / "_applied"
        report = patches.apply_all(patch_repo, PATCH_FIXTURE_IMPL, audit, in_place=False, out_dir=applied_dir)
        applied = (applied_dir / "mod.py").read_text(encoding="utf-8")
        check("def new_helper(conn):" in applied, "插入型补丁被套用到副本")
        check(applied.index("def new_helper") > applied.index("index_all(conn, roots"), "补丁插在 anchor 之后")
        check((patch_repo / "mod.py").read_text(encoding="utf-8") == PATCH_REPO_SOURCE, "原仓库未被改动（dry-run）")
        check(bool(report["skipped"]), "不可套用的补丁被列出跳过原因", str(report["skipped"])[:120])
        try:
            patches.apply_all(patch_repo, PATCH_FIXTURE_IMPL, audit, in_place=False)
            check(False, "非 in_place 时必须要求 out_dir")
        except ValueError:
            check(True, "非 in_place 时强制要求 out_dir（不会误写原仓库）")

        print("\n== patch-audit-in-orchestrator")
        patch_orch = make(root, "patchrun", client=PatchClient(), repo=patch_repo, max_rework=0)
        patch_result = patch_orch.run(REQ)
        patch_state = json.loads((patch_result.run_dir / "state.json").read_text(encoding="utf-8"))
        p_audit = patch_state["artifacts"]["patch_audit"]
        check(p_audit["ok"] == 1 and p_audit["problems"] == 3, "编排器记录了补丁校验结果", str(p_audit["problems"]))
        check((patch_result.run_dir / "patches").exists(), "补丁目录已生成")
        p_issues = {i.kind for i in issues.collect_issues(patch_state, "patchrun")}
        check("patch_incomplete" in p_issues and "anchor_not_found" in p_issues, "补丁问题进了问题记录", str(sorted(p_issues)))
        review_trace = [t for t in runstore.read_traces(patch_result.run_dir) if t["stage"] == "review"][-1]
        check("补丁机械校验" in review_trace["user"], "补丁校验结论 pin 进评审 prompt")
        check("不可能是一次完整替换" in review_trace["user"] or "会留下原函数体" in review_trace["user"],
              "评审能看到补丁问题的具体理由")

        print("\n== empty-implementation-is-blocked")
        empty_orch = make(root, "empty", client=AllNotImplementedClient(), repo=patch_repo, max_rework=0)
        empty_result = empty_orch.run(REQ)
        empty_state = json.loads((empty_result.run_dir / "state.json").read_text(encoding="utf-8"))
        check(empty_state["artifacts"]["implementation_audit"]["empty_implementation"], "识别出「零实现」")
        empty_issues = {i.kind for i in issues.collect_issues(empty_state, "empty")}
        check("implementation_empty" in empty_issues, "零实现记成阻断问题", str(sorted(empty_issues)))
        check(empty_result.summary["needs_human"], "零实现不能蒙过 pass")
        empty_review = empty_state["artifacts"]["review"] or {}
        check(empty_review.get("verdict") == "rework_dev", "被推翻为 rework_dev", str(empty_review.get("verdict")))

        print("\n== forced-rework-on-broken-patch")
        forced_orch = make(root, "forced", client=PassAlwaysPatchClient(), repo=patch_repo, max_rework=0)
        forced_result = forced_orch.run(REQ)
        forced_state = json.loads((forced_result.run_dir / "state.json").read_text(encoding="utf-8"))
        rounds = forced_state["rounds"]
        check(bool(rounds) and rounds[0]["forced_rework"], "评审想给 pass，被补丁校验推翻为 rework_dev",
              str(rounds[0]["verdict"] if rounds else None))
        check(forced_result.summary["needs_human"], "触顶后交人工（补丁问题不能靠放行蒙过去）")
        forced_issues = {i.kind for i in issues.collect_issues(forced_state, "forced")}
        check("review_forced_rework" in forced_issues, "记录「判定被机制推翻」", str(sorted(forced_issues)))
        forced_fixes = (forced_state["artifacts"]["review"] or {}).get("required_fixes") or []
        check(any(f.startswith("修复") for f in forced_fixes), "机制生成的返工项进了必改项", str(forced_fixes)[:160])

        print("\n== resume-overrides")
        ov_orch = make(root, "override", client=MockClient(rework_first=9), max_rework=2, pause_after=["pm"])
        ov_first = ov_orch.run(REQ)
        check(ov_first.paused, "先暂停在 pm")
        ov_keep = make(root, "override", client=MockClient(rework_first=9))
        kept = ov_keep.resume(ov_first.run_dir, pause_after=[])
        check(kept.summary["needs_human"] and kept.summary["attempts"] == 3, "不传时沿用 run 原本的上限（2 → 3 轮）",
              f"attempts={kept.summary['attempts']}")

        ov2_orch = make(root, "override2", client=MockClient(rework_first=9), max_rework=2, pause_after=["pm"])
        ov2_first = ov2_orch.run(REQ)
        ov2 = make(root, "override2", client=MockClient(rework_first=9))
        stopped = ov2.resume(ov2_first.run_dir, pause_after=[], max_rework=0)
        check(stopped.summary["needs_human"] and stopped.summary["attempts"] == 1,
              "续跑时显式 --max-rework 生效（1 轮就交人工）", f"attempts={stopped.summary['attempts']}")

        print("\n== perf-issue-detection")
        slow_state = {
            "calls": [
                {
                    "stage": "architect_assess",
                    "tag": "qwen3-14b-arch-8k",
                    "prefill_tps": 12.0,
                    "gen_tps": 20.0,
                    "vram_gb": 9.02,
                    "model_gb": 9.02,
                    "vram_ratio": 1.0,
                    "wall_s": 600,
                }
            ]
        }
        slow_kinds = {i.kind for i in issues.collect_issues(slow_state, "slow")}
        check("prefill_degraded" in slow_kinds, "吞吐退化被抓成阻断问题", str(sorted(slow_kinds)))
        check("gpu_partial_offload" not in slow_kinds, "报 100% 显存时不误报 gpu_partial_offload")
        healthy = {"calls": [dict(slow_state["calls"][0], prefill_tps=127.0, gen_tps=20.0, wall_s=120)]}
        check("prefill_degraded" not in {i.kind for i in issues.collect_issues(healthy, "ok")},
              "健康吞吐不误报")

        print("\n== pinned-instructions")
        from pipeline.budget import fit_prompt

        filler = ["填充内容占位。" * 60 for _ in range(6)]
        text, truncated = fit_prompt(filler, 200, pin=["【人工已确认的事实】关键事实不得被截断"])
        check("关键事实不得被截断" in text, "pin 的片段在超预算时仍保留")
        check(truncated, "同时如实上报 truncated", str(truncated))
        text2, truncated2 = fit_prompt(["短的片段"], 500, pin=["【人工已确认的事实】X"])
        check("【人工已确认的事实】X" in text2 and not truncated2, "预算够时 pin 也不丢")

        print("\n== human-facts-propagation")
        # issue-report 已把 run 目录平铺到 _flat/，这里按新位置找
        traces_dir = (
            edit_run_dir if edit_run_dir.exists() else root / "_flat" / f"edit-{edit_run_dir.name}"
        )
        edit_traces = runstore.read_traces(traces_dir)  # 该 run 里有过人工意见注入
        later = [t for t in edit_traces if t["stage"] in ("dev", "test", "review")]
        check(bool(later), "存在后续阶段调用", str([t["stage"] for t in later]))
        check(
            all("人工已确认的事实" in t["user"] for t in later),
            "人工事实注入到开发/测试/评审（不只目标阶段）",
            str([t["stage"] for t in later]),
        )
        assess_traces = [t for t in edit_traces if t["stage"] == "architect_assess"]
        check(
            any("按评审意见补测" in t["user"] or "不要动 config.py" in t["user"] for t in assess_traces),
            "人工意见仍然直达目标阶段",
        )

        # ---------------------------------------------------------- 人工审核闸门
        print("\n== human-review-gate")
        g_orch = make(root, "hrev", client=MockClient(rework_first=0))
        g = g_orch.run(REQ)
        check(g.paused and g.paused_after == "human_review", "到达人工审核闸门并暂停",
              f"paused={g.paused}/{g.paused_after}")
        check(
            any(p.name.endswith("-human_review.json") for p in g.run_dir.glob("*.json")),
            "闸门占位产物已落盘",
            str(sorted(p.name for p in g.run_dir.glob("*.json"))[-2:]),
        )

        # 闸门「原始输入」：续跑改成跑到底（pause_after 清空）后，仍要能读出创建时勾了什么
        gi_orch = make(root, "gate-initial", pause_after=["pm"])
        gi_first = gi_orch.run(REQ)
        gi_state = json.loads((gi_first.run_dir / "state.json").read_text(encoding="utf-8"))
        check(gi_state.get("initial_pause_after") == ["pm"], "state 记录创建时的原始闸门",
              str(gi_state.get("initial_pause_after")))
        # 续跑并清空闸门 → pause_after 变空，但 initial 必须保持不变
        gi_orch2 = make(root, "gate-initial")
        gi_orch2.resume(gi_first.run_dir, pause_after=[])
        gi_state2 = json.loads((gi_first.run_dir / "state.json").read_text(encoding="utf-8"))
        check(gi_state2.get("pause_after") == [], "续跑清空后当前闸门为空", str(gi_state2.get("pause_after")))
        check(gi_state2.get("initial_pause_after") == ["pm"], "原始闸门不被续跑覆盖",
              str(gi_state2.get("initial_pause_after")))

        # 打回：写 reject 产物并续跑，应回流到开发修复，再次回到闸门
        runstore.save_artifact(g.run_dir, "human_review",
            {"verdict": "reject", "notes": "主流程有一处未处理异常", "reviewer": "tester"})
        g2 = g_orch.resume(g.run_dir)
        check(g2.paused and g2.paused_after == "human_review", "打回后再次到达人工审核闸门",
              f"paused={g2.paused}/{g2.paused_after}")

        # 二次通过审核后放行交付
        g2 = approve_human_review(g_orch, g2.run_dir)
        check(g2.summary["status"] == "done" and g2.summary["verdict"] == "pass",
              "二次通过审核后放行交付", f"{g2.summary['status']}/{g2.summary['verdict']}")
        dev_traces = [t for t in runstore.read_traces(g2.run_dir) if t["stage"] == "dev"]
        check(any("人工审核打回" in (t.get("user") or "") for t in dev_traces),
              "打回意见注入到开发阶段 prompt")

        # ---------------------------------------------------------- 边界
        print("\n== guards")
        orch = make(root, "only", client=MockClient())
        result = orch.run(REQ, stages=["pm"])
        check(result.summary["verdict"] == "partial" and result.summary["mode"] == "only", "--only 模式 verdict=partial")
        check(not (result.run_dir / runstore.STATE_NAME).exists(), "--only 不写 state.json")
        try:
            make(root, "only").resume(result.run_dir)
            check(False, "--only 运行不可续跑")
        except Exception as exc:  # noqa: BLE001
            check("state.json" in str(exc), "--only 运行拒绝续跑", str(exc))

        # ---------------------------------------------------------- 分片重构合并（pass3 多 edit 不丢）
        print("\n== merge-sharded")
        # 第二遍分片重构：对同一个 index_all 输出 3 条 replace_span（锚点各不相同），
        # 旧的去重键 (path, symbol, mode) 会把它们合并成 1 条、其余分片丢失；新键含 anchor 应全部保留。
        p1 = {
            "edits": [
                {"path": "mod.py", "target_symbol": "_index_batch", "patch_mode": "insert_after",
                 "anchor": "def index_all(conn, roots, batch=300):", "patch": "def _index_batch(conn):\n    pass\n",
                 "covers_tasks": ["t1"]},
                {"path": "mod.py", "target_symbol": "_split_key", "patch_mode": "insert_after",
                 "anchor": "def index_all(conn, roots, batch=300):", "patch": "def _split_key(v):\n    return v\n",
                 "covers_tasks": ["t2"]},
            ],
            "not_implemented": [], "deviations": [], "self_checks": [], "summary": "脚手架",
        }
        p2 = {
            "edits": [
                {"path": "mod.py", "target_symbol": "index_all", "patch_mode": "replace_span",
                 "anchor": "    for row in rows:", "patch": "    for row in _split_key(rows):\n        _index_batch(conn, row)\n",
                 "covers_tasks": ["t1"]},
                {"path": "mod.py", "target_symbol": "index_all", "patch_mode": "replace_span",
                 "anchor": "    conn.commit()", "patch": "    _flush(conn)\n", "covers_tasks": ["t2"]},
                {"path": "mod.py", "target_symbol": "index_all", "patch_mode": "replace_span",
                 "anchor": "    return result", "patch": "    return _summarize(result)\n", "covers_tasks": ["t3"]},
            ],
            "not_implemented": [], "deviations": [], "self_checks": [], "summary": "回填",
        }
        merged = Orchestrator._merge_dev(p1, p2)
        edits = merged["edits"]
        check(len(edits) == 5, "分片重构的 3 条 replace_span 全部保留（不被合并成 1 条）", str(len(edits)))
        anchors = sorted(e.get("anchor") for e in edits if e.get("target_symbol") == "index_all")
        check(
            anchors == ["    conn.commit()", "    for row in rows:", "    return result"],
            "每个分片用各自 anchor 区分、无丢失", str(anchors),
        )
        check(
            sum(1 for e in edits if e.get("target_symbol") == "index_all") == 3,
            "index_all 的分片数正确", str(sum(1 for e in edits if e.get("target_symbol") == "index_all")),
        )
        # 回归：同 (path, symbol, mode, anchor) 的后写仍应覆盖先写
        p2_dup = {
            "edits": [
                {"path": "mod.py", "target_symbol": "index_all", "patch_mode": "replace_span",
                 "anchor": "    conn.commit()", "patch": "    _flush_v2(conn)\n", "covers_tasks": ["t2"]},
            ],
            "not_implemented": [], "deviations": [], "self_checks": [], "summary": "回填",
        }
        merged2 = Orchestrator._merge_dev(p1, p2_dup)
        flush = [e for e in merged2["edits"] if e.get("anchor") == "    conn.commit()"]
        check(len(flush) == 1 and flush[0]["patch"] == "    _flush_v2(conn)\n",
              "同锚点的后写覆盖先写（去重语义不变）", str(flush))

        # ---------------------------------------------------------- 需求入口补强（intake）
        print("\n== intake-stage")
        from pipeline import schemas as schemas_mod

        i_orch = make(root, "intake", client=MockClient())
        i_res = settle(i_orch, i_orch.run(REQ))
        i_state = json.loads((i_res.run_dir / "state.json").read_text(encoding="utf-8"))
        intake = (i_state.get("artifacts") or {}).get("intake")
        check(isinstance(intake, dict) and bool(intake), "补强产物落到 state.artifacts.intake", str(type(intake)))
        check(
            not schemas_mod.validate(intake, schemas_mod.STAGE_SCHEMAS["intake"]),
            "补强产物通过 INTAKE 契约校验",
            str(schemas_mod.validate(intake, schemas_mod.STAGE_SCHEMAS["intake"]))[:160],
        )
        check(
            isinstance((intake or {}).get("refined_requirement"), dict)
            and isinstance((intake or {}).get("preliminary_scope"), dict),
            "补强产物含 refined_requirement / preliminary_scope",
        )
        # 顺序：intake 必须是第一个模型阶段（跑在 pm 之前）
        seq = [t["stage"] for t in runstore.read_traces(i_res.run_dir)]
        check(seq[0] == "intake" and seq[1] == "pm", "intake 跑在 pm 之前", str(seq[:3]))
        # 复用机制：PM 的 prompt 里能看到补强初稿
        pm_trace = [t for t in runstore.read_traces(i_res.run_dir) if t["stage"] == "pm"][-1]
        # 注意：MockClient 会按 schema 给 final_decision 也合成占位值，所以这里只断言
        # 「补强产物确实进了 PM prompt」，初稿/终稿的标题差异用下面手工构造的产物单独验证。
        check("需求补强" in pm_trace["user"], "补强产物注入 PM prompt")
        # 补强用的就是 pm 的模型，不额外增加一次模型切换
        check(
            i_res.summary["model_switches"] == 3,
            "补强复用 pm 模型，切换仍是 3 次",
            str(i_res.summary["model_switches"]),
        )
        # 人工介入：默认假设写进待人工确认清单
        handoff = (i_res.run_dir / runstore.HANDOFF_NAME).read_text(encoding="utf-8")
        check("需求补强" in handoff, "补强的默认假设/待澄清项写进 handoff")
        # CLI 的阶段白名单必须跟着 ONLY_STAGES 走：真机教训 —— 加 intake 后 cli.ALL_STAGES
        # 是硬编码的没同步，`--pause-after intake` 被判「未知阶段」，进程启动即 exit 2。
        from pipeline import cli as cli_mod
        from pipeline.orchestrator import ONLY_STAGES

        want = [s for s in ONLY_STAGES if s != "human_review"]
        check(cli_mod.ALL_STAGES == want, "CLI 阶段白名单与 ONLY_STAGES 同步（防漏改）", str(cli_mod.ALL_STAGES))
        check("intake" in cli_mod.ALL_STAGES, "CLI 接受 --pause-after intake")
        # 人工裁决：必须注入 PM（PM 不进 _human_facts，靠 parts_pm 显式带），并传导所有下游阶段
        # 裁决并回补强产物：条目带 final_decision 后，PM 拿到的是「已裁决终稿」
        # （不再是另外递一份裁决清单 —— 否则模型同时看到两份值不知道听谁的）
        dec = [{"kind": "missing_element", "ref": "导出格式", "decision": "裁决：统一 xlsx"}]
        art_no_dec = {"missing_elements": [{"element": "导出格式", "default_assumption": "默认：csv"}]}
        art_dec = {"missing_elements": [{"element": "导出格式", "default_assumption": "默认：csv",
                                         "final_decision": "裁决：统一 xlsx", "confirmed": True}]}
        check(not prompts.intake_has_decisions(art_no_dec), "未裁决时识别为初稿")
        check(prompts.intake_has_decisions(art_dec), "有 final_decision 时识别为已裁决终稿")
        pm_parts = prompts.parts_pm(REQ, art_dec)
        check(
            any("终稿" in str(p) and "confirmed_facts" in str(p) for p in pm_parts),
            "PM 收到的是已裁决终稿，并被告知 confirmed_facts 为确定结论",
            str([str(p)[:60] for p in pm_parts]),
        )
        check(
            any("初稿" in str(p) for p in prompts.parts_pm(REQ, art_no_dec)),
            "未裁决时 PM 收到的是初稿",
        )
        # 裁决后下游拿到的应是**陈述式结论**，不再是「问 + 建议答案 + 裁决」的问答形式
        full = {
            "refined_requirement": {"core_goal": "g", "constraints": ["必须走流式"]},
            "missing_elements": [
                {"element": "控制方式", "default_assumption": "默认假设使用方向键控制",
                 "final_decision": "使用方向键控制", "confirmed": True},
                {"element": "平台", "default_assumption": "默认仅 Web"},
            ],
            "clarifying_questions": [
                {"question": "是否要触屏？", "suggested_answer": "默认建议不需要",
                 "final_decision": "不需要", "confirmed": True},
            ],
        }
        view = prompts.finalized_intake_view(full)
        joined = json.dumps(view, ensure_ascii=False)
        check(any("使用方向键控制" in str(f) for f in (view.get("confirmed_facts") or [])),
              "已裁决条目收进 confirmed_facts", str(view.get("confirmed_facts")))
        check(
            any(str(c).startswith("已确认：") for c in (view["refined_requirement"]["constraints"])),
            "已确认结论并入约束（陈述式，非问答）", str(view["refined_requirement"]["constraints"]),
        )
        check(len(view["pending_items"]) == 1 and view["pending_items"][0]["element"] == "平台",
              "已裁决的条目不再往下流转（待确认项只剩未裁决的那条）", str(view["pending_items"]))
        check("missing_elements" not in view and "clarifying_questions" not in view,
              "下游视图统一为单条待确认项列表（旧的两类字段已收掉）", str(sorted(view.keys())))
        # 已裁决的不该再带裁决字段流转（未裁决的保留 default_assumption 供 PM 判断，属预期）
        # 注意用带引号的键名匹配，避免误命中 confirmed_facts 这个正常字段
        check("final_decision" not in joined and '"confirmed"' not in joined,
              "下游视图里不再出现 final_decision / confirmed 这类裁决字段")
        check("default_assumption" not in json.dumps(view["pending_items"][0], ensure_ascii=False)
              or view["pending_items"][0]["element"] == "平台",
              "未裁决条目仍保留默认假设供 PM 判断")

        # 待确认项合并（真机 run 20260925-140707）：旧产物的 missing_elements 与
        # clarifying_questions 合成**一条列表**，同一主题不再两处出现；「建议补充：需明确…」
        # 这类把问题退回原样的伪默认值按「未给出取值」处理。
        legacy = {
            "missing_elements": [
                {"element": "游戏分辨率",
                 "default_assumption": "建议补充：需明确窗口尺寸或画布大小", "importance": "medium"},
                {"element": "游戏结束条件",
                 "default_assumption": "默认假设：碰撞墙体或自身即结束", "importance": "high"},
            ],
            "clarifying_questions": [
                {"question": "游戏分辨率", "suggested_answer": "使用 800×600 画布",
                 "impact": "影响布局"},
            ],
        }
        tidied, tidy_notes = prompts.tidy_intake(legacy)
        check("pending_items" in tidied and "missing_elements" not in tidied
              and "clarifying_questions" not in tidied,
              "旧产物的两类合并为单一待确认项列表，旧字段收掉", str(sorted(tidied.keys())))
        check(len(tidied["pending_items"]) == 2,
              "同主题逐字重复的条目被去掉（游戏分辨率只留一处）",
              str([r["element"] for r in tidied["pending_items"]]))
        vague = [r for r in tidied["pending_items"] if r.get("needs_value")]
        check(len(vague) == 1 and vague[0]["default_assumption"] == "",
              "「建议补充：需明确…」这类伪默认值被清空并标记 needs_value", str(vague))
        good = [r for r in tidied["pending_items"] if r["element"] == "游戏结束条件"]
        check(len(good) == 1 and good[0]["default_assumption"].startswith("默认假设：碰撞")
              and not good[0].get("needs_value"),
              "带具体取值的条目原样保留（不被误标为未给值）", str(good))
        check(str(good[0].get("why") or "") == "",
              "旧产物里没有 why 字段时留空而不是编造")
        check(any("未给出取值" in n for n in tidy_notes),
              "整理结果回报给人工（说明里写了未给出取值）", str(tidy_notes))

        # 裁决参谋（旁路环节）：就「待确认项」反复问模型风险/收益。
        # 它不产出阶段产物、不进流程注册表，问答落在 runs/<id>/advice/<stage>.jsonl。
        adv_dir = root / "advice-thread"
        adv_dir.mkdir(parents=True, exist_ok=True)
        two_src = {
            "pending_items": [
                {"element": "分辨率", "default_assumption": "", "importance": "medium",
                 "why": "影响布局"},
                {"element": "结束条件", "default_assumption": "碰撞即结束", "importance": "high",
                 "final_decision": "碰撞墙体或自身即结束"},
            ],
        }
        adv_state = {"intake_decisions": [
            {"kind": "pending_item", "ref": "分辨率", "decision": "800*600"},
        ]}
        adv_items = advice.pending_items("intake", two_src, adv_state)
        check(len(adv_items) == 2 and adv_items[0]["decided"] == "800*600",
              "待确认项归一：产物里没有的裁决从 state 补（两处都取）", str(adv_items))
        check(adv_items[1]["decided"] == "碰撞墙体或自身即结束",
              "产物里的 final_decision 优先于 state", str(adv_items[1]))
        pm_items = advice.pending_items(
            "pm",
            {"open_questions": [{"question": "是否触屏？", "assumed_answer": "不需要",
                                 "severity": "low", "why_it_matters": "影响输入"}]},
            {},
        )
        check(len(pm_items) == 1 and pm_items[0]["element"] == "是否触屏？"
              and pm_items[0]["value"] == "不需要",
              "PM 阶段的待确认项也能归一（open_questions → 通用形状）", str(pm_items))
        check(advice.pending_items("review", {}, {}) == [],
              "没有待确认项的阶段返回空列表而不是抛错")
        check(advice.artifact_text({"a": "x" * 400}, limit=60).endswith("（已截断）"),
              "产物正文按字符预算截断并标注，避免把上下文撑爆")
        from pipeline import config as _adv_cfg  # 局部导入：main() 里另有同名局部变量

        check(advice.spec_for("intake").tag == _adv_cfg.STAGE_MODELS["intake"].tag,
              "默认复用该阶段自己的模型 tag（不额外引入要加载的模型）",
              advice.spec_for("intake").tag)

        turn1 = advice.ask(adv_dir, requirement="REQ", stage="intake", artifact=two_src,
                           state=adv_state, question="怎么办？", focus="分辨率",
                           client=MockClient())
        check(turn1["n"] == 1 and turn1["focus"] == "分辨率" and isinstance(turn1["answer"], dict),
              "问一次参谋：返回本轮记录（MockClient，不碰真模型）", str(turn1)[:160])
        turn2 = advice.ask(adv_dir, requirement="REQ", stage="intake", artifact=two_src,
                           state=adv_state, question="再问一次", client=MockClient())
        check(turn2["n"] == 2 and [t["n"] for t in advice.read_thread(adv_dir, "intake")] == [1, 2],
              "可反复问：线程逐轮追加且能从磁盘读回",
              str([t["n"] for t in advice.read_thread(adv_dir, "intake")]))
        check(advice.read_thread(adv_dir, "nosuchstage") == [],
              "读不存在的线程返回空列表（不抛错）")

        # 「用默认 / 用建议」＝采纳默认值，带入时必须去掉「默认 / 建议」前缀
        import re as _re

        lead = _re.compile(r"^\s*(?:默认假设|默认建议|默认|建议答案|建议)\s*[:：]?\s*")
        check(lead.sub("", "默认假设使用方向键控制").strip() == "使用方向键控制", "采纳默认值时去掉「默认假设」前缀")
        check(lead.sub("", "默认建议不需要").strip() == "不需要", "采纳建议答案时去掉「默认建议」前缀")
        check(lead.sub("", "不需要").strip() == "不需要", "本来就没前缀时保持不变")
        facts_orch = make(root, "intake-facts")
        facts_orch.intake_decisions = dec
        check("裁决：统一 xlsx" in facts_orch._human_facts(), "裁决并入人工已确认事实（传导下游各阶段）")

        # PM 未决项：已裁决 → 确定结论；未裁决 → 才带默认假设（都不会再把问答题抛给下游）
        scope = {
            "open_questions": [
                {"question": "是否要触屏？", "recommendation": "建议不做", "assumed_answer": "不做",
                 "final_decision": "不做，仅键盘", "confirmed": True, "severity": "low"},
                {"question": "要不要存档？", "recommendation": "建议不做", "assumed_answer": "不做",
                 "severity": "high"},
            ]
        }
        blk = prompts.pm_assumptions_block(scope)
        check("确定结论" in blk and "不做，仅键盘" in blk, "已裁决的 PM 未决项作为确定结论下发", blk[:90])
        check("要不要存档？" in blk and "本次默认按此执行" in blk, "未裁决的仍带默认假设供下游推进")
        check(blk.index("已裁决") < blk.index("人工尚未确认"), "已裁决与未裁决分区呈现")

        # ------------------------------------------------------ 流定义真源 / 统一闸门 / 检查点回放
        print("\n== flow（流定义单一真源 + 跨表一致性校验）")
        from pipeline import cli as cli_mod2
        from pipeline import config as config_mod
        from pipeline import flow as flow_mod

        check(flow_mod.validate() == [], "流定义跨表一致性校验通过", str(flow_mod.validate()))
        check(
            runstore.FLOW_ORDER == flow_mod.FLOW_ORDER,
            "runstore.FLOW_ORDER 由流定义派生（不再与 config.FULL_STAGE_ORDER 各写一份）",
            str(runstore.FLOW_ORDER),
        )
        check(config_mod.FULL_STAGE_ORDER == flow_mod.FLOW_ORDER, "config.FULL_STAGE_ORDER 与流定义一致")
        check(cli_mod2.ALL_STAGES == flow_mod.PAUSABLE_NODES, "CLI 白名单 = 流定义的可暂停阶段")
        check(
            set(flow_mod.STAGE_STATE_KEY) == set(flow_mod.FLOW_ORDER),
            "STAGE_STATE_KEY 覆盖全部流程阶段",
            str(sorted(set(flow_mod.FLOW_ORDER) - set(flow_mod.STAGE_STATE_KEY))),
        )
        check("human_review" not in flow_mod.PAUSABLE_NODES, "human_review 不作为人工闸门候选")
        check(flow_mod.next_linear("dev") == "test", "线性后继由流定义给出")
        mm = flow_mod.mermaid()
        check(
            mm.startswith("flowchart TD") and "review -->|rework_dev| dev" in mm,
            "Mermaid 导出含回流边",
            mm.splitlines()[0],
        )
        # 校验器必须**真能**抓到「新增阶段漏登记」——临时摘掉一个模型阶段的登记
        saved_models = dict(config_mod.STAGE_MODELS)
        try:
            config_mod.STAGE_MODELS.pop("dev")
            caught = [p for p in flow_mod.validate() if "STAGE_MODELS" in p]
            check(bool(caught), "漏登记模型阶段会被一致性校验抓出来", str(caught))
        finally:
            config_mod.STAGE_MODELS.clear()
            config_mod.STAGE_MODELS.update(saved_models)
        check(flow_mod.validate() == [], "恢复登记后一致性校验重新通过")

        print("\n== interrupt（统一闸门：explicit / conditional / mandatory）")
        it_orch = make(root, "gate-kinds", pause_on_open_questions=True)
        check(it_orch._gate_after("dev") is None, "无触发条件时不停")
        it_orch.pause_after = {"dev"}
        explicit = it_orch._gate_after("dev")
        check(
            explicit is not None and explicit.kind == "explicit",
            "显式 pause_after → explicit 闸门",
            str(explicit),
        )
        check(it_orch._should_pause("dev"), "旧签名 _should_pause 仍等价可用（兼容）")
        it_orch.pause_after = set()
        it_orch.state = {"scope": {"open_questions": []}}
        check(it_orch._gate_after("pm") is None, "PM 无未决项 → 不触发 conditional 闸门")
        it_orch.state = {"scope": {"open_questions": [{"question": "要不要支持暂停？"}]}}
        cond = it_orch._gate_after("pm")
        check(
            cond is not None and cond.kind == "conditional" and "PM" in cond.title,
            "PM 有未决项 → conditional 闸门",
            str(cond),
        )
        hr_orch = make(root, "gate-hr")
        hr_orch.state = {}
        first = hr_orch._gate_after("human_review", "human_review")
        check(
            first is not None and first.kind == "mandatory",
            "human_review 首次到达 → mandatory 闸门",
            str(first),
        )
        check(hr_orch._gate_after("human_review", "dev") is None, "打回后（nxt=dev）不再重复触发闸门")
        hr_orch.state = {"human_review": {"verdict": "approve"}}
        check(
            hr_orch._gate_after("human_review", "human_review") is None,
            "人工已提交 verdict 后不再触发闸门",
        )

        print("\n== checkpoints（检查点时间线 + 按 seq 回放）")
        # rework_first=1 → dev 会跑多轮，从而在同一阶段留下多份检查点（回放要能分别定位）
        ck_orch = make(root, "ckpt", client=MockClient(rework_first=1))
        ck_res = settle(ck_orch, ck_orch.run(REQ))
        ck_run_dir = ck_res.run_dir
        ck_list = runstore.checkpoints(ck_run_dir)
        check(len(ck_list) >= 2, "检查点时间线列出快照", f"{len(ck_list)} 个")
        check(
            [c["seq"] for c in ck_list] == sorted(c["seq"] for c in ck_list),
            "检查点按 seq 升序",
            str([c["seq"] for c in ck_list]),
        )
        check(all(c["state_key"] for c in ck_list), "每个检查点都带 state_key（可定位回写的产物键）")
        dev_ckpts = [c for c in ck_list if c["stage"] == "dev"]
        check(
            len(dev_ckpts) >= 2,
            "同一阶段多轮各留一个检查点（按 seq 可分别定位）",
            str([c["seq"] for c in dev_ckpts]),
        )
        check(
            len(runstore.run_detail(ck_run_dir).get("checkpoints") or []) == len(ck_list),
            "run_detail 带上检查点时间线（页面选择器的数据源）",
        )
        # 回放到第一份 dev 检查点：目标自身保留，其后产物归档
        target = int(dev_ckpts[0]["seq"])
        ck_orch.restore_checkpoint(target)
        check(ck_orch.cursor == "dev", "回放后游标归位到该检查点阶段", str(ck_orch.cursor))
        live_after = [c for c in runstore.checkpoints(ck_run_dir) if not c["superseded"]]
        check(
            all(c["seq"] <= target for c in live_after),
            "回放只作废目标检查点之后的产物",
            str([c["seq"] for c in live_after]),
        )
        check(
            any(c["seq"] == target for c in live_after),
            "目标检查点自身保留（不被误作废）",
            str([c["seq"] for c in live_after]),
        )
        superseded_files = list((ck_run_dir / runstore.SUPERSEDED_DIR).glob("*.json"))
        check(bool(superseded_files), "被作废的产物归档到 superseded/", f"{len(superseded_files)} 个")
        check("implementation" not in ck_orch.state, "被作废阶段的产物从 state 中移除")
        check(
            ck_orch.human_actions
            and ck_orch.human_actions[-1].get("action") == "checkpoint_restore",
            "回放动作留痕到 human_actions",
            str(ck_orch.human_actions[-1].get("action") if ck_orch.human_actions else ""),
        )

        print("\n== stage-log（运行日志按阶段切分，流程图节点用）")
        log_run = root / "log-split"
        log_run.mkdir(parents=True, exist_ok=True)
        (log_run / runstore.LOG_NAME).write_text(
            "\n".join(
                [
                    "== run 20260101-000000 (repo=未提供)",
                    runstore.stage_marker("intake"),
                    "  补强完成",
                    runstore.stage_marker("pm"),
                    "  PM 完成",
                    runstore.stage_marker("dev"),
                    "  第一轮开发",
                    runstore.stage_marker("dev"),
                    "  第二轮开发",
                    runstore.stage_marker("review"),
                    "  评审判定: pass",
                ]
            ),
            encoding="utf-8",
        )
        secs, preamble = runstore.log_sections(log_run)
        check(
            [s["stage"] for s in secs] == ["intake", "pm", "dev", "dev", "review"],
            "日志按阶段边界标记切分",
            str([s["stage"] for s in secs]),
        )
        check(
            len(preamble) == 1 and preamble[0].startswith("== run"),
            "第一条标记之前的内容归入 preamble",
            str(preamble),
        )
        check(
            [s["index"] for s in secs if s["stage"] == "dev"] == [1, 2],
            "同一阶段多轮编号从 1 递增",
        )
        dev_log = runstore.stage_log(log_run, "dev")
        check(
            dev_log["occurrences"] == 2 and "第一轮开发" in dev_log["text"] and "第二轮开发" in dev_log["text"],
            "多轮日志按执行顺序拼接",
            str(dev_log["occurrences"]),
        )
        pm_log = runstore.stage_log(log_run, "pm")
        check(pm_log["text"].strip().endswith("PM 完成"), "单轮切片边界正确", pm_log["text"][:40])
        check("补强完成" not in pm_log["text"], "切片不会串到别的阶段")
        never = runstore.stage_log(log_run, "test")
        check(
            never["occurrences"] == 0 and never["text"] == "" and never["has_markers"],
            "有标记但该阶段没跑过 → 明确返回空（不退回整段尾部）",
            str(never),
        )
        legacy_run = root / "log-legacy"
        legacy_run.mkdir(parents=True, exist_ok=True)
        (legacy_run / runstore.LOG_NAME).write_text("line1\nline2\nline3\n", encoding="utf-8")
        legacy = runstore.stage_log(legacy_run, "dev", max_lines=2)
        check(
            legacy["has_markers"] is False and "line3" in legacy["text"] and "line1" not in legacy["text"],
            "旧运行（无标记）退回日志尾部并标注 has_markers=False",
            str(legacy),
        )
        # 真跑一次确认编排器确实把标记打出来了。
        # 注意：这里必须自己接 log（make() 的 log 是空实现，而 console.log 只由 server 子进程落盘），
        # 否则 "写了标记" 这件事在离线冒烟里根本观测不到。
        captured: list[str] = []
        marker_orch = Orchestrator(
            client=MockClient(),
            runs_dir=root / "log-marker",
            unload_at_end=False,
            log=captured.append,
            pause_on_open_questions=False,
        )
        settle(marker_orch, marker_orch.run(REQ))
        marked = [
            m.group(1)
            for m in (runstore.STAGE_MARK_RE.match(line.strip()) for line in captured)
            if m
        ]
        check(marked and marked[:3] == ["intake", "pm", "retrieve"],
              "编排器在每个阶段前写入边界标记", str(marked[:6]))
        check(marked.count("dev") >= 1 and marked[-1] == "human_review",
              "标记覆盖到人工审核阶段", str(marked))
        # 写入端（编排器）与读取端（runstore.stage_log）用的是同一份格式 —— 拿真实标记回灌一遍
        replay_log = root / "log-roundtrip"
        replay_log.mkdir(parents=True, exist_ok=True)
        (replay_log / runstore.LOG_NAME).write_text("\n".join(captured), encoding="utf-8")
        rt_secs, _ = runstore.log_sections(replay_log)
        check([s["stage"] for s in rt_secs] == marked,
              "编排器写出的标记能被读取端原样切分（格式不会两边漂移）",
              str([s["stage"] for s in rt_secs][:6]))
        check(runstore.stage_log(replay_log, "dev")["lines"] > 0,
              "切片里确实带上了该阶段的日志内容")

        # ------------------------------------------------------ 同一新文件的多条补丁必须合并
        print("\n== new-file-merge（绿地项目：同一新文件的多条补丁）")
        from pipeline import config as config_mod2
        from pipeline import patches as patches_mod
        from pipeline import verify as verify_mod

        no_repo = root / "no-such-repo"          # 目录不存在 → 全部走 new_file 分支
        merge_edits = [
            {"path": "logic.py", "change_type": "add", "target_symbol": "Snake", "anchor": "",
             "patch_mode": "full_symbol", "covers_tasks": ["T-01"],
             "patch": "from typing import List\n\nclass Snake:\n    def move(self):\n        pass"},
            {"path": "logic.py", "change_type": "add", "target_symbol": "Food", "anchor": "",
             "patch_mode": "full_symbol", "covers_tasks": ["T-02"],
             "patch": "import random\n\nclass Food:\n    def spawn(self):\n        return random.randint(0, 9)"},
            {"path": "logic.py", "change_type": "add", "target_symbol": "Game", "anchor": "",
             "patch_mode": "full_symbol", "covers_tasks": ["T-03"],
             "patch": "from typing import List\n\nclass Game:\n    def run(self):\n        pass"},
        ]
        merge_impl = {"summary": "s", "edits": merge_edits, "self_checks": [], "deviations": [],
                      "not_implemented": []}
        merge_audit = patches_mod.analyze_all(no_repo, merge_impl)
        check(merge_audit["ok"] == 3 and merge_audit["problems"] == 0,
              "同路径的多条新文件补丁单看都算可套用",
              f"ok={merge_audit['ok']} problems={merge_audit['problems']}")
        check(all("合并写入同一份文件" in " ".join(r["notes"]) for r in merge_audit["edits"]),
              "审计里显式标注「会合并写入同一份文件」")
        merge_out = root / "merge-out"
        merge_report = patches_mod.apply_all(no_repo, merge_impl, merge_audit, out_dir=merge_out)
        check(len(merge_report["files"]) == 1 and merge_report["files"][0]["patches"] == 3,
              "3 条补丁合并成 1 次写出（不再后写覆盖先写）", str(merge_report["files"]))
        merged_text = (merge_out / "logic.py").read_text(encoding="utf-8")
        check(all(f"class {name}" in merged_text for name in ("Snake", "Food", "Game")),
              "合并后三个类都在", merged_text.splitlines()[0])
        check(merged_text.count("from typing import List") == 1, "重复 import 被去重",
              str(merged_text.count("from typing import List")))
        head = merged_text.splitlines()[:3]
        check(head[0].startswith("from typing import List") and any("import random" in line for line in head),
              "顶层 import 被提到文件顶部", str(head))
        check(merge_report["skipped"] == [], "合并写入后不再有误导性的「文件不存在」跳过",
              str(merge_report["skipped"]))

        merge_run = root / "merge-run"
        merge_run.mkdir(parents=True, exist_ok=True)
        merge_written = patches_mod.write_patch_files(merge_run, no_repo, merge_impl, merge_audit)
        check(len(merge_written) == 3 and len({w["file"] for w in merge_written}) == 1,
              "落盘的 patch 文件也只写一份（组内共用，逐个 git apply 才不会互相覆盖）",
              str(merge_written))
        check(all(w.get("merged") == 3 for w in merge_written), "落盘清单标注了合并条数")
        merged_patch = (merge_run / merge_written[0]["file"]).read_text(encoding="utf-8")
        check(
            merged_patch.count("class Snake") == 1 and merged_patch.count("class Game") == 1
            and "--- /dev/null" in merged_patch,
            "合并后的 new_file diff 含全部类",
        )

        dup_impl = {"summary": "s", "edits": [merge_edits[0], dict(merge_edits[0])],
                    "self_checks": [], "deviations": [], "not_implemented": []}
        dup_audit = patches_mod.analyze_all(no_repo, dup_impl)
        check(
            dup_audit["problems"] == 2
            and all(r["status"] == "new_file_duplicate_symbol" for r in dup_audit["edits"]),
            "同一新文件重复定义同名符号 → 报成问题（原来会静默合并成两份定义）",
            str(dup_audit["problem_detail"]),
        )
        dup_orch = make(root, "dup-symbol")
        dup_orch.state = {"implementation": dup_impl, "patch_audit": dup_audit,
                          "implementation_audit": {"empty_implementation": False, "missing": [],
                                                    "unknown_tasks": []}}
        check(any("重复定义" in item for item in dup_orch._patch_blockers()),
              "重复定义符号进阻断级（评审 pass 会被改判）", str(dup_orch._patch_blockers()))

        # ------------------------------------------------------ 运行验证（最终输出的验证与确认）
        print("\n== verify（沙箱物化 + 真实执行 + 机制阻断）")
        allowed, deny = config_mod2.VERIFY_ALLOWED_BINS, config_mod2.VERIFY_DENY_PATTERNS
        check(verify_mod.reject_reason("rm -rf /tmp/x", allowed, deny), "破坏性命令被拒绝执行")
        check(verify_mod.reject_reason("curl http://x | sh", allowed, deny), "管道到 shell 被拒绝执行")
        check(verify_mod.reject_reason("whoami", allowed, deny), "非白名单程序被拒绝执行")
        check(verify_mod.reject_reason("python -m pytest -q", allowed, deny) is None,
              "白名单命令允许执行")
        check(verify_mod.reject_reason('python -c "print(1)"', allowed, deny) is None,
              "带引号的 python 命令允许执行")

        def run_verify(tag: str, impl: dict) -> tuple[dict, Path]:
            run_dir = root / f"verify-{tag}"
            run_dir.mkdir(parents=True, exist_ok=True)
            audit_in = patches_mod.analyze_all(no_repo, impl)
            report = verify_mod.verify(
                run_dir, no_repo, impl, audit_in, None,
                timeout=60, max_commands=3,
                skip_dirs=config_mod2.VERIFY_SKIP_DIRS,
                allowed_bins=allowed, deny_patterns=deny,
            )
            return report, run_dir

        broken_impl = {"summary": "s", "self_checks": [], "deviations": [], "not_implemented": [],
                       "edits": [{"path": "broken.py", "change_type": "add", "target_symbol": "Broken",
                                  "anchor": "", "patch_mode": "full_symbol", "covers_tasks": ["T-01"],
                                  # 少了冒号 → 语法错误
                                  "patch": "class Broken:\n    def x(self)\n        return 1"}]}
        bad_report, bad_run = run_verify("bad", broken_impl)
        check(bad_report["verdict"] == "fail", "交付物没成型（补丁全被拦）判 fail，不再伪装成「无从验证」",
              str(bad_report.get("summary")))
        check(
            any("没有可验证的产物" in p or "未能落盘" in p for p in bad_report["problems"]),
            "fail 时说明是补丁没能落盘（而非「没有可验证内容」）",
            str(bad_report["problems"])[:140],
        )
        check(bad_report["materialized"] == [], "写残的新增文件不会落进沙箱（在补丁阶段就被拦下）",
              str(bad_report["materialized"]))
        # 这一轮新增：内容写残的新增文件在**补丁机械校验**阶段就被判出，不必等到 verify 执行
        broken_audit = patches_mod.analyze_all(no_repo, broken_impl)
        check(
            broken_audit["edits"][0]["status"] == "new_file_syntax_error",
            "写残的新增文件在补丁阶段就被判出（省掉一整轮 test+verify+review）",
            str(broken_audit["edits"][0]["status"]),
        )
        check(
            "第 2 行" in " ".join(broken_audit["edits"][0]["notes"]),
            "语法问题带行号定位（拿到返工项的一方知道改哪儿）",
            str(broken_audit["edits"][0]["notes"]),
        )
        check(broken_audit["problems"] == 1, "判负项计入 problems（会被 _patch_blockers 收走）",
              str(broken_audit["problems"]))

        good_impl = {"summary": "s", "self_checks": [], "deviations": [], "not_implemented": [],
                     "edits": [{"path": "ok.py", "change_type": "add", "target_symbol": "Ok",
                                "anchor": "", "patch_mode": "full_symbol", "covers_tasks": ["T-01"],
                                "patch": "class Ok:\n    def x(self):\n        return 1"}]}
        good_report, _ = run_verify("good", good_impl)
        check(good_report["verdict"] == "pass", "能跑的交付判 pass", str(good_report.get("summary")))
        check(good_report["problems"] == [], "pass 时没有阻断问题", str(good_report["problems"]))
        check(any(c.get("source") == "syntax" for c in good_report["commands"]),
              "语法检查是必跑项")
        check(
            good_report.get("interface_audit", {}).get("unparsable") == [],
            "可解析的交付 → 静态接口审计无「无法解析」项",
            str(good_report.get("interface_audit")),
        )

        # 入口脚本「空跑」只记 note、不判 fail：
        # 测试模型会顺手写 `python <库模块>.py`，那是**命令质量问题**，开发无权改命令 ——
        # 判成阻断级就会重演「改不动却一直返工」（真机 run 20260924-185507 刚踩过）。
        noentry_impl = {
            "summary": "s", "self_checks": [], "deviations": [], "not_implemented": [],
            "edits": [{"path": "lib.py", "change_type": "add", "target_symbol": "Lib",
                       "anchor": "", "patch_mode": "full_symbol", "covers_tasks": ["T-01"],
                       "patch": "class Lib:\n    def x(self):\n        return 1\n"}],
        }
        noentry_dir = root / "verify-noentry"
        noentry_dir.mkdir(parents=True, exist_ok=True)
        noentry_report = verify_mod.verify(
            noentry_dir, no_repo, noentry_impl, patches_mod.analyze_all(no_repo, noentry_impl),
            {"automated_commands": [{"command": "python lib.py"}]},
            timeout=60, max_commands=3,
            skip_dirs=config_mod2.VERIFY_SKIP_DIRS,
            allowed_bins=allowed, deny_patterns=deny,
        )
        check(
            noentry_report["verdict"] == "pass",
            "入口脚本空跑不判 fail（命令由测试阶段产出，开发改不动）",
            str(noentry_report.get("summary")),
        )
        check(
            any("证明不了任何行为" in x for x in (noentry_report.get("notes") or [])),
            "但以 note 形式点出来（评审与人工都看得到）",
            str(noentry_report.get("notes"))[:160],
        )

        # 语法合法但用了**没导入**的名字：py_compile 会放过，import 才炸。
        # 真机 run 20260924-135801 的 graphics_renderer.py 就是这个毛病
        # （`def __init__(self, game_area: Tuple[int, int])` 里 Tuple 从未导入）。
        annot_impl = {"summary": "s", "self_checks": [], "deviations": [], "not_implemented": [],
                      "edits": [{"path": "renderer.py", "change_type": "add", "target_symbol": "Renderer",
                                 "anchor": "", "patch_mode": "full_symbol", "covers_tasks": ["T-01"],
                                 "patch": "class Renderer:\n"
                                          "    def __init__(self, size: Tuple[int, int]):\n"
                                          "        self.size = size"}]}
        annot_report, _ = run_verify("annot", annot_impl)
        syntax_cmd = next((c for c in annot_report["commands"] if c.get("source") == "syntax"), None)
        import_cmd = next((c for c in annot_report["commands"] if c.get("source") == "import"), None)
        check(syntax_cmd is not None and syntax_cmd["status"] == "ok",
              "py_compile 对「用了未导入名字」是放过的（它只看语法）")
        check(import_cmd is not None, "计划里加了导入检查")
        check(
            import_cmd is not None and import_cmd["status"] == "fail"
            and "NameError" in (import_cmd["stdout_tail"] + import_cmd["stderr_tail"]),
            "导入检查抓到 NameError 并留下原文",
            str(((import_cmd or {}).get("stdout_tail") or "")[:90]),
        )
        check(annot_report["verdict"] == "fail",
              "读文件看不出来的缺陷，被运行验证判成 fail", str(annot_report.get("summary")))

        mock_report = verify_mod.verify(
            root / "verify-mock", no_repo, good_impl, patches_mod.analyze_all(no_repo, good_impl), None,
            mock=True, allowed_bins=allowed, deny_patterns=deny,
        )
        check(mock_report["verdict"] == "skipped"
              and all(c["status"] == "skipped" for c in mock_report["commands"]),
              "mock 运行只计划命令、不真跑", str(mock_report.get("summary")))

        vf_orch = make(root, "verify-forced")
        vf_orch.state = {
            "verify_report": {"verdict": "fail", "problems": ["失败：`python broken.py`（退出码 1）"],
                              "commands": []},
            "patch_audit": {"edits": [], "ok": 0, "problems": 0},
            "implementation_audit": {"empty_implementation": False, "missing": [], "unknown_tasks": []},
        }
        check(vf_orch._verify_blockers(), "运行验证失败被识别为机制阻断项",
              str(vf_orch._verify_blockers()))
        check(any("运行验证失败" in item for item in vf_orch._mechanical_blockers()),
              "机制阻断项里包含运行验证失败")
        forced_review = {"verdict": "pass", "reasons": ["看着没问题"], "blockers": [],
                         "required_fixes_detail": [], "required_fixes": [], "residual_risks": []}
        vf_orch._normalize_review(forced_review)
        check(
            forced_review["verdict"] == "rework_dev" and forced_review.get("forced_rework") is True,
            "运行验证失败 → 评审给 pass 也被改判 rework_dev",
            str(forced_review["verdict"]),
        )
        check(any("跑不起来" in r or "运行验证失败" in r for r in forced_review["reasons"]),
              "改判理由里写明是运行验证失败", str(forced_review["reasons"])[:120])

        # 真跑一次全流程（mock），确认 verify 阶段进产物、且不执行真实命令
        vrun_orch = make(root, "verify-stage")
        vrun_res = settle(vrun_orch, vrun_orch.run(REQ))
        vrun_state = json.loads((vrun_res.run_dir / "state.json").read_text(encoding="utf-8"))
        vr = (vrun_state.get("artifacts") or {}).get("verify_report") or {}
        check(vr.get("mode") == "mock" and vr.get("verdict") == "skipped",
              "mock 全流程下 verify 阶段落盘且未真跑", str(vr.get("summary")))
        check(
            [s["stage"] for s in runstore.stage_snapshots(vrun_res.run_dir)].count("verify") == 1,
            "verify 阶段产物已落盘（NN-verify.json）",
            str([s["stage"] for s in runstore.stage_snapshots(vrun_res.run_dir)]),
        )

        # ------------------------------------------------------ 入口总闸（全局架构）
        print("\n== gateway（入口总闸：规模判定 / 路由 / 作业 / 越界审计）")
        import pipeline.orchestrator as orchestrator_mod
        from pipeline import gateway as gw
        from pipeline import schemas as schemas_mod

        # 前置节点的登记：漏登必须在启动期就被抓住（与既有阶段同一套校验口径）
        check(gw.GA_NODE in flow_mod.PRE_NODES, "节点名取自 flow.PRE_NODES 真源", gw.GA_NODE)
        check(
            set(schemas_mod.PRE_SCHEMAS) == set(flow_mod.PRE_NODES),
            "PRE_SCHEMAS 与 PRE_NODES 一致",
            str(sorted(schemas_mod.PRE_SCHEMAS)),
        )
        check(gw.GA_NODE in config_mod.STAGE_MODELS, "前置节点登记了独立模型")
        ga_spec = config_mod.STAGE_MODELS[gw.GA_NODE]
        check(
            ga_spec.prompt_token_budget + ga_spec.num_predict <= ga_spec.num_ctx,
            "入口总闸的输入+输出预算不越 num_ctx（否则 JSON 会被静默截断）",
            f"{ga_spec.prompt_token_budget}+{ga_spec.num_predict} <= {ga_spec.num_ctx}",
        )
        check(
            ga_spec.prompt_token_budget < config_mod.STAGE_MODELS["architect_plan"].prompt_token_budget,
            "输入预算比 architect_plan 更紧（输出字段更多，得给它让位）",
            f"{ga_spec.prompt_token_budget} < {config_mod.STAGE_MODELS['architect_plan'].prompt_token_budget}",
        )
        check(
            gw.GA_NODE not in flow_mod.EXEC_ORDER and gw.GA_NODE not in flow_mod.PAUSABLE_NODES,
            "前置节点不进执行顺序、也不是可暂停阶段（旁路时行为零差异）",
        )
        saved_spec = config_mod.STAGE_MODELS.pop(gw.GA_NODE)
        try:
            caught = [p for p in flow_mod.validate() if gw.GA_NODE in p]
            check(bool(caught), "漏登记前置节点会被一致性校验抓出来", str(caught)[:120])
        finally:
            config_mod.STAGE_MODELS[gw.GA_NODE] = saved_spec
        check(flow_mod.validate() == [], "恢复登记后一致性校验重新通过")

        # 禁区来源：配置 + 运行覆盖，去重保序
        check(
            gw.forbidden_paths(["a/x.py", "a/x.py", " b/y.py "]) == ["a/x.py", "b/y.py"],
            "禁区清单去重去空白且保序",
            str(gw.forbidden_paths(["a/x.py", "a/x.py", " b/y.py "])),
        )

        # 规模预判：零模型调用（这是"小需求零额外开销"的关键）
        small_stats = {"top_dirs": ["a"], "files": 10, "lines": 100}
        suspect, why = gw.prejudge("给客户列表加一个导出按钮", small_stats)
        check(not suspect, "小需求 + 小仓库 → 不调全局架构", str(why))
        big_req = "对系统做整体改造：\n1) 拆分权限模块\n2) 新增网关\n3) 多端适配"
        suspect, why = gw.prejudge(big_req, small_stats)
        check(suspect, "跨模块多条目需求 → 疑似大型", str(why))
        suspect, why = gw.prejudge("小改动", {"top_dirs": list("abcdefg"), "files": 500, "lines": 50000})
        check(suspect and "存量规模" in " ".join(why), "存量仓库大 → 强信号命中", str(why))

        def _ga(module_count, contracts=0, order=None, high=0):
            """造一份**自洽**的 GA 产物（判据、自检、路由都基于它）。"""
            modules = [
                {
                    "module_id": f"M-{i + 1:02d}",
                    "module_name": f"模块{i + 1}",
                    "responsibility": "职责",
                    "scope_in": [],
                    "scope_out": [],
                    "risk_level": "high" if i < high else "low",
                    "depends_on": [],
                }
                for i in range(module_count)
            ]
            return {
                "project_summary": "摘要",
                "modules": modules,
                "interface_contracts": [
                    {
                        "interface_id": f"IF-{i + 1:02d}",
                        "from_module": "M-01",
                        "to_module": "M-01",
                        "interface_name": "契约",
                    }
                    for i in range(contracts)
                ],
                "global_constraints": {
                    "forbidden_paths": [],
                    "naming_rules": "",
                    "compatibility_rules": "",
                    "dependency_versions": "",
                },
                "execution_order": order if order is not None else [m["module_id"] for m in modules],
                "integration_checkpoints": [],
                "uncertainties": [],
            }

        # 规模推导：提示词的模板里没有 scale 字段，只能由确定性规则推
        check(gw.derive_scale(_ga(1)).scale == "small", "单模块 → small")
        check(gw.derive_scale(_ga(2)).scale == "large", "多模块 → large")
        check(gw.derive_scale(_ga(1, contracts=1)).scale == "large", "出现跨模块接口 → large")
        check(gw.derive_scale(_ga(3, high=2)).scale == "large", "两个高风险模块 → large")
        check(bool(gw.derive_scale(_ga(2)).reasons), "规模判定理由可解释", str(gw.derive_scale(_ga(2)).reasons))
        uncertain_only = _ga(1)
        uncertain_only["uncertainties"] = [{"issue": "a", "assumption": "b", "impact": "c"}] * 4
        check(
            gw.derive_scale(uncertain_only).scale == "small",
            "不确定项不参与规模升级（信息不足 ≠ 规模大）",
        )

        # 语义自检：JSON Schema 表达不了的部分（唯一性 / 覆盖 / 拓扑序 / 悬空引用）
        check(gw.check_self_consistency(_ga(2)) == [], "自洽产物无问题")
        dup = _ga(2)
        dup["modules"][1]["module_id"] = "M-01"
        check(any("重复" in p for p in gw.check_self_consistency(dup)), "module_id 重复被抓出")
        missing = _ga(2)
        missing["execution_order"] = ["M-01"]
        check(any("漏掉" in p for p in gw.check_self_consistency(missing)), "执行顺序漏模块被抓出")
        bad_order = _ga(2)
        bad_order["execution_order"] = ["M-02", "M-01"]
        bad_order["modules"][1]["depends_on"] = ["M-01"]
        check(any("违例" in p for p in gw.check_self_consistency(bad_order)), "拓扑序违例被抓出")
        dangling = _ga(1)
        dangling["interface_contracts"] = [
            {"interface_id": "IF-01", "from_module": "M-99", "to_module": "M-01", "interface_name": "x"}
        ]
        check(any("不在模块清单" in p for p in gw.check_self_consistency(dangling)), "悬空接口引用被抓出")
        ghost = _ga(1)
        ghost["modules"][0]["depends_on"] = ["M-77"]
        check(any("不存在的模块" in p for p in gw.check_self_consistency(ghost)), "依赖不存在的模块被抓出")

        # 接地校验：GA 报的禁区路径必须真实存在（对应它自己的红线 3）
        grepo = root / "gw-repo"
        (grepo / "core").mkdir(parents=True, exist_ok=True)
        (grepo / "core" / "a.py").write_text("x = 1\n", encoding="utf-8")
        (grepo / "web").mkdir(parents=True, exist_ok=True)
        grounded = _ga(1)
        grounded["global_constraints"]["forbidden_paths"] = ["core/a.py", "core/nope.py"]
        check(
            gw.check_grounding(grounded, grepo) == ["core/nope.py"],
            "编造的禁区路径被接地校验指出（只提示、不阻断）",
            str(gw.check_grounding(grounded, grepo)),
        )

        # 路径归属与越界审计（复用 orchestrator._path_stem 的同一套口径）
        check(
            gw.module_dirs({"module_name": "核心逻辑", "responsibility": "core", "scope_in": []}, ["core", "web"])
            == ["core"],
            "按模块名/职责在顶层目录里做确定性匹配",
        )
        aud = gw.audit_paths(["core/a.py"], forbidden=["core"], owned=["web"], others=["core"])
        check(
            aud["forbidden_touched"] == ["core/a.py"] and aud["cross_module"] == [],
            "踩禁区只算禁区、不重复算越界",
            str(aud),
        )
        aud = gw.audit_paths(["misc/a.py"], forbidden=[], owned=["web"], others=["core"])
        check(
            aud["outside_scope"] == ["misc/a.py"] and not aud["cross_module"],
            "无主目录只记提示、不算越界",
            str(aud),
        )

        class _NoCall(MockClient):
            """断言型客户端：被调用即失败 —— 用来证明旁路路径真的**没碰模型**。"""

            def chat_json(self, *args, **kwargs):  # noqa: ARG002
                raise AssertionError("这条路径不该调用模型")

        jobs_dir = root / "jobs"
        off_route = gw.dispatch("给客户列表加导出按钮", repo=str(grepo), runs_dir=jobs_dir,
                                mode="off", client=_NoCall())
        check(
            off_route.scale == "small" and off_route.source == "off",
            "off 模式直通且不调模型",
            off_route.describe(),
        )
        auto_route = gw.dispatch("给客户列表加导出按钮", repo=str(grepo), runs_dir=jobs_dir,
                                 mode="auto", client=_NoCall())
        check(
            auto_route.scale == "small" and auto_route.source == "prejudge",
            "auto 下小需求由预判拦下、同样不调模型",
            auto_route.describe(),
        )
        forced = gw.dispatch("给客户列表加导出按钮", runs_dir=jobs_dir, mode="always",
                             scale_override="small", client=_NoCall())
        check(
            forced.scale == "small" and forced.source == "forced",
            "人工强制 small 时连 GA 都不调",
            forced.describe(),
        )

        # 大型：调 GA → 建作业
        big_route = gw.dispatch(big_req, repo=str(grepo), runs_dir=jobs_dir, mode="always",
                                client=MockClient(), forbidden=["core/a.py"])
        check(big_route.scale == "large" and big_route.job_id, "大型 → 建作业", big_route.describe())
        job = gw.read_job(jobs_dir, big_route.job_id)
        check(job is not None and len(job["modules"]) == 2, "作业落盘且含模块清单",
              str(len((job or {}).get("modules") or [])))
        base = gw.job_dir(jobs_dir, big_route.job_id)
        check((base / "ga.json").exists() and (base / "job.json").exists(), "作业目录含 ga.json / job.json")
        mod_files = sorted(p.name for p in (base / "modules").glob("*.md"))
        check(len(mod_files) == 2, "每个模块都有落盘的子需求文本", str(mod_files))
        req_text = (base / "modules" / mod_files[0]).read_text(encoding="utf-8")
        check("core/a.py" in req_text, "禁区被写进子需求（提示词不给输入，这里补上）")
        check("M-01" in req_text and "核心逻辑" in req_text, "子需求含模块定位信息")
        check(gw.job_status(job) == "pending", "作业初始状态 pending", gw.job_status(job))
        rows = gw.list_jobs(jobs_dir)
        check(len(rows) == 1 and rows[0]["job_id"] == big_route.job_id, "作业列表可列出", str(rows[:1]))
        (jobs_dir / "20260101-000000").mkdir(parents=True, exist_ok=True)
        gw.link_run(jobs_dir, "20260101-000000", job_id=big_route.job_id,
                    reasons=big_route.reasons, status="running")
        link = gw.read_link(jobs_dir, "20260101-000000")
        check(
            bool(link) and link["job_id"] == big_route.job_id,
            "运行详情能指向作业（否则页面只剩一条空运行记录）",
            str(link),
        )

        class _BadChain(MockClient):
            """语义不自洽：执行顺序指向不存在的模块。"""

            def chat_json(self, spec, system, user, schema, num_predict=None, attempts=2):
                data, meta = super().chat_json(spec, system, user, schema, num_predict, attempts)
                if schema is schemas_mod.GLOBAL_ARCHITECTURE:
                    data["execution_order"] = ["M-99"]
                return data, meta

        degraded = gw.dispatch(big_req, repo=str(grepo), runs_dir=jobs_dir, mode="always",
                               client=_BadChain())
        check(
            degraded.scale == "small" and degraded.source == "degraded",
            "GA 语义不自洽 → 降级 small 直通",
            degraded.describe(),
        )
        check(bool(degraded.notes), "降级原因被记录（便于排查）", str(degraded.notes)[:100])

        class _Garbage(MockClient):
            """输出不合契约：空对象。"""

            def chat_json(self, spec, system, user, schema, num_predict=None, attempts=2):
                if schema is schemas_mod.GLOBAL_ARCHITECTURE:
                    return {}, {"tag": "x"}
                return super().chat_json(spec, system, user, schema, num_predict, attempts)

        broken = gw.dispatch(big_req, repo=str(grepo), runs_dir=jobs_dir, mode="always", client=_Garbage())
        check(
            broken.scale == "small" and broken.source == "degraded",
            "GA 输出不合契约 → 降级 small 直通",
            broken.describe(),
        )

        # 子运行越界审计（只读：读 state.json 里的 plan.changes）
        fake_run = jobs_dir / "fake-run"
        runstore.write_state(fake_run, {"artifacts": {"plan": {"changes": [{"path": "core/a.py"}]}}})
        mod_audit = gw.audit_module_run(fake_run, forbidden=["core/a.py"], owned=["web"], others=["core"])
        check(mod_audit["forbidden_touched"] == ["core/a.py"], "子运行方案踩禁区被审计出", str(mod_audit))

        # blocked 的传播语义：依赖它的跳过，不相关的继续（不阻断整组，也绝不带病开工）
        chain = {
            "modules": [
                {"module_id": "M-01", "status": "blocked", "depends_on": []},
                {"module_id": "M-02", "status": "pending", "depends_on": ["M-01"]},
                {"module_id": "M-03", "status": "pending", "depends_on": []},
            ]
        }
        gw._skip_dependents(chain, "M-01")
        check(
            chain["modules"][1]["status"] == "skipped" and chain["modules"][2]["status"] == "pending",
            "依赖 blocked 模块的后续被跳过，不相关的继续",
            str([m["status"] for m in chain["modules"]]),
        )

        # 端到端：mock 跑完整个两模块作业（关掉交付闸门，冒烟不等人）
        saved_gate = orchestrator_mod.HUMAN_REVIEW_GATE
        orchestrator_mod.HUMAN_REVIEW_GATE = False
        try:
            e2e_client = MockClient()
            e2e_route = gw.dispatch(big_req, repo=str(grepo), runs_dir=jobs_dir, mode="always",
                                    client=e2e_client)
            e2e = gw.run_job(e2e_route.job_id, runs_dir=jobs_dir, client=e2e_client, repo=str(grepo),
                             pause_on_open_questions=False, review_every=1)
            check(gw.job_status(e2e) == "done", "两模块作业全部跑完", gw.job_status(e2e))
            check(
                all(m["status"] == "done" for m in e2e["modules"]),
                "每个模块都独立走完了原有流水线（角色逻辑零改动）",
                str([m["status"] for m in e2e["modules"]]),
            )
            check(
                all(m["run_id"] and (jobs_dir / m["run_id"] / "state.json").exists() for m in e2e["modules"]),
                "模块子运行各有独立 run_id 与 state.json",
                str([m["run_id"] for m in e2e["modules"]]),
            )
            check(all(m.get("audit") is not None for m in e2e["modules"]), "每个模块都做了越界审计")
            check(
                all(not str(r["run_id"]).startswith("_") for r in runstore.list_runs(jobs_dir)),
                "作业目录不进运行列表（_ 前缀约定）",
                str([r["run_id"] for r in runstore.list_runs(jobs_dir)]),
            )
            e2e_report = (gw.job_dir(jobs_dir, e2e_route.job_id) / "report.md").read_text(encoding="utf-8")
            check("集成校验点" in e2e_report, "集成校验点进人读报告（不新增评审节点）")
            check("模块与执行情况" in e2e_report, "报告含模块执行情况")
            view = gw.job_view(jobs_dir, e2e_route.job_id)
            check(
                len(view["module_requirements"]) == 2 and bool(view["report"]),
                "页面视图含子需求全文与报告",
            )
            check(gw.job_view(jobs_dir, "nosuchjob") is None, "未知作业返回 None（页面据此 404）")
        finally:
            orchestrator_mod.HUMAN_REVIEW_GATE = saved_gate

        # ------------------------------------------------------ 返工项归属与自动回转
        print("\n== review-scope（返工项三档归属 + 阻塞自动回转）")
        check(
            schemas_mod.FIX_SCOPE == ["in_material", "architect", "needs_external"],
            "返工项作用域三档（含方案层 architect）",
            str(schemas_mod.FIX_SCOPE),
        )
        spec_review = {
            "verdict": "rework_dev",
            "reasons": ["x"],
            "blockers": [],
            "required_fixes_detail": [
                {"fix": "补方案漏掉的文件", "scope": "architect", "why": "方案漏规划"},
                {"fix": "改补丁", "scope": "in_material", "why": "实现错"},
                {"fix": "问运维", "scope": "needs_external", "why": "要环境"},
            ],
            "required_fixes": [],
            "residual_risks": [],
        }
        check(
            schemas_mod.validate(spec_review, schemas_mod.REVIEW) == [],
            "REVIEW 契约接受 scope=architect",
            str(schemas_mod.validate(spec_review, schemas_mod.REVIEW)),
        )
        bad_scope = json.loads(json.dumps(spec_review))
        bad_scope["required_fixes_detail"] = [{"fix": "x", "scope": "bogus", "why": "w"}]
        check(
            bool(schemas_mod.validate(bad_scope, schemas_mod.REVIEW)),
            "非法 scope 被契约拒绝",
            str(schemas_mod.validate(bad_scope, schemas_mod.REVIEW))[:80],
        )

        rs_orch = make(root, "rs-normalize")
        rs_orch.state = {}
        review3 = json.loads(json.dumps(spec_review))
        in_mat, arch, ext, forced = rs_orch._normalize_review(review3)
        check(
            (in_mat, arch, ext, forced) == (["改补丁"], ["补方案漏掉的文件"], ["问运维"], False),
            "三档分流正确",
            f"{in_mat} / {arch} / {ext} / {forced}",
        )
        check(
            review3["architect_fixes"] == ["补方案漏掉的文件"],
            "方案层返工项单独落进 architect_fixes（供审计与页面）",
            str(review3.get("architect_fixes")),
        )
        check(
            any("问运维" == r["issue"] for r in review3["residual_risks"]),
            "needs_external 仍进 residual_risks（不触发返工）",
            str(review3["residual_risks"])[:100],
        )

        # 守卫①：列了方案层返工项却判 pass ⇒ 不能放行（等于承认方案有缺陷还往下走）
        pass_guard = {
            "verdict": "pass",
            "reasons": ["看着没问题"],
            "blockers": [],
            "required_fixes_detail": [{"fix": "补方案漏掉的文件", "scope": "architect", "why": "w"}],
            "required_fixes": [],
            "residual_risks": [],
        }
        guard1 = make(root, "rs-guard-pass")
        guard1.state = {}
        guard1._normalize_review(pass_guard)
        check(
            pass_guard["verdict"] == "rework_architect" and pass_guard.get("forced_rework") is True,
            "列了方案层返工项却判 pass → 强制回方案",
            str(pass_guard["verdict"]),
        )

        # 守卫②：只有方案层返工项时，不能被「in_material 为空」误判成「无可执行」而强制放行
        only_arch = {
            "verdict": "rework_dev",
            "reasons": ["x"],
            "blockers": [],
            "required_fixes_detail": [{"fix": "补方案漏掉的文件", "scope": "architect", "why": "w"}],
            "required_fixes": [],
            "residual_risks": [],
        }
        guard2 = make(root, "rs-guard-arch")
        guard2.state = {}
        guard2._normalize_review(only_arch)
        check(
            only_arch["verdict"] == "rework_dev" and not only_arch.get("forced_pass"),
            "只有方案层返工项时不会误判成「无可执行」而强制放行",
            str(only_arch["verdict"]),
        )

        # 旧产物兼容：没有 required_fixes_detail ⇒ 沿用旧行为（按实现层）
        legacy = {
            "verdict": "rework_dev",
            "reasons": ["x"],
            "blockers": [],
            "required_fixes": ["改代码"],
            "residual_risks": [],
        }
        legacy_orch = make(root, "rs-legacy")
        legacy_orch.state = {}
        in_mat, arch, ext, forced = legacy_orch._normalize_review(dict(legacy))
        check(
            in_mat == ["改代码"] and arch == [] and ext == [],
            "旧产物（无明细）仍按实现层处理，不被新契约破坏",
            f"{in_mat} / {arch} / {ext}",
        )

        # 端到端：评审把根因判成方案层 ⇒ 下一轮自动回到 architect_plan
        class ArchitectScopeClient(MockClient):
            """判 rework_dev，但把根因标成方案层（真机 run 20260924-185507 的形态）。"""

            def chat_json(self, spec, system, user, schema, num_predict=None, attempts=2):
                data, meta = super().chat_json(spec, system, user, schema, num_predict, attempts)
                if spec.role == "评审":
                    data["verdict"] = "rework_dev"
                    data["reasons"] = ["方案漏规划了一个模块"]
                    data["required_fixes_detail"] = [
                        {"fix": "把漏掉的模块补进 changes", "scope": "architect", "why": "方案漏规划"}
                    ]
                    data["required_fixes"] = ["把漏掉的模块补进 changes"]
                    data["blockers"] = []
                return data, meta

        route_logs: list[str] = []
        route_orch = Orchestrator(
            client=ArchitectScopeClient(),
            repo=None,
            runs_dir=root / "rs-route",
            unload_at_end=False,
            log=route_logs.append,
            pause_on_open_questions=False,
            max_rework=2,
            review_every=1,
        )
        route_res = settle(route_orch, route_orch.run(REQ))
        route_stages = [s["stage"] for s in runstore.stage_snapshots(route_res.run_dir)]
        check(
            bool(route_orch.rounds) and route_orch.rounds[0].get("routed_to") == "architect_plan",
            "方案层返工项 → 自动回转到 architect_plan（不再整轮打回开发）",
            str(route_orch.rounds[0].get("routed_to") if route_orch.rounds else None),
        )
        check(
            route_stages.count("architect_plan") >= 2,
            "第二轮确实从方案阶段重跑",
            str(route_stages),
        )
        check(
            route_orch.rounds[0].get("required_fixes_architect") == ["把漏掉的模块补进 changes"],
            "方案层返工项记进 rounds（可审计）",
            str(route_orch.rounds[0].get("required_fixes_architect")),
        )
        check(
            any("回到架构师方案" in line for line in route_logs),
            "日志写明为什么回方案（人工可查）",
            str([x for x in route_logs if "架构师方案" in x][:1]),
        )

        # ------------------------------------------------------ verify 证据送达开发
        print("\n== verify→dev（机械证据必须送到开发与方案手里）")
        from pipeline import budget as budget_mod
        vr_fail = {
            "verdict": "fail",
            "summary": "fail：执行 2/2 条命令，失败 1 条",
            "problems": ["失败：`python game_logic.py`（退出码 1）"],
            "notes": [],
            "commands": [
                {"command": "python -m py_compile a.py", "status": "ok", "exit_code": 0},
                {
                    "command": "python game_logic.py",
                    "status": "fail",
                    "exit_code": 1,
                    "stderr_tail": (
                        "Traceback (most recent call last):\n"
                        '  File "D:\\work\\game_logic.py", line 1, in <module>\n'
                        "    from input_handler import Direction\n"
                        '  File "D:\\work\\input_handler.py", line 2, in <module>\n'
                        "    from direction import Direction\n"
                        "ModuleNotFoundError: No module named 'direction'\n"
                    ),
                    "stdout_tail": "",
                },
            ],
        }
        key_line = "No module named 'direction'"

        # 关键行必须在 distill（从头截断）之后仍然可见 —— 之前 traceback 的最后一行会被切掉
        compacted = prompts._compact_error(vr_fail["commands"][1]["stderr_tail"][-500:])
        check(
            key_line in compacted and key_line in budget_mod.truncate_text(compacted, 60),
            "命令输出压成「头+尾」后，traceback 末行的异常原因不再被截掉",
            compacted[-60:],
        )
        check(
            len(compacted) <= 200,
            "压缩后不超过 distill 的 200 字符下限（否则等于没修）",
            str(len(compacted)),
        )

        # 机械事实：机制算集合差，把归属判据摆出来（归属仍由评审声明）
        plan_no_dir = {"changes": [{"path": "input_handler.py"}], "tasks": []}
        facts_missing = prompts.verify_facts(vr_fail, plan_no_dir, {"edits": [{"path": "input_handler.py"}]})
        check(
            bool(facts_missing) and "方案层" in facts_missing[0] and "direction" in facts_missing[0],
            "缺模块且方案清单里没有 → 机械事实指向方案层",
            str(facts_missing)[:120],
        )
        facts_planned = prompts.verify_facts(vr_fail, {"changes": [{"path": "direction.py"}], "tasks": []}, None)
        check(
            bool(facts_planned) and "属实现层" in facts_planned[0],
            "方案清单里有该文件 → 机械事实指向实现层（补丁没落地）",
            str(facts_planned)[:120],
        )
        facts_unknown = prompts.verify_facts(vr_fail, None, None)
        check(
            bool(facts_unknown) and "无法判定" in facts_unknown[0],
            "清单缺失时只陈述能确证的部分（事实宁可少说，不能说错）",
            str(facts_unknown)[:120],
        )
        facts_symbol = prompts.verify_facts(
            {"verdict": "fail", "commands": [
                {"command": "x", "status": "fail", "stderr_tail": "ImportError: cannot import name 'Foo' from 'bar'"}
            ]},
            {"changes": []},
            None,
        )
        check(
            any("没有符号 Foo" in f for f in facts_symbol),
            "符号级缺失也被翻成机械事实",
            str(facts_symbol)[:120],
        )

        # 集成：开发阶段真的拿到证据（驱动 _stage_dev，捕获它实际收到的 prompt）
        class CaptureDevClient(MockClient):
            def __init__(self) -> None:
                super().__init__()
                self.dev_prompts: list[str] = []

            def chat_json(self, spec, system, user, schema, num_predict=None, attempts=2):
                if spec.role.startswith("开发"):
                    self.dev_prompts.append(user)
                return super().chat_json(spec, system, user, schema, num_predict, attempts)

        plan_stub = {
            "changes": [
                {
                    "path": "input_handler.py",
                    "change_type": "add",
                    "rationale": "方向控制",
                    "minimality_reason": "只新增本模块",
                }
            ],
            "tasks": [
                {
                    "id": "T-01",
                    "target_files": ["input_handler.py"],
                    "goal": "实现方向控制",
                    "depends_on": [],
                    "acceptance": ["按方向键改变方向"],
                }
            ],
            "approach": "新增输入处理模块",
            "risks": [],
            "uncertainties": [],
        }
        cap = CaptureDevClient()
        dev_orch = make(root, "verify-to-dev", client=cap)
        dev_orch.requirement = REQ
        dev_orch.run_id = "verify-to-dev"
        dev_orch.run_dir = root / "verify-to-dev" / "verify-to-dev"
        dev_orch.run_dir.mkdir(parents=True, exist_ok=True)
        dev_orch.state = {"plan": plan_stub, "verify_report": vr_fail}
        dev_orch._stage_dev(REQ)
        dev_blob = "\n".join(cap.dev_prompts)
        check(bool(cap.dev_prompts), "开发阶段确实发起了一次调用（测试前提）")
        check(
            "运行验证结果" in dev_blob,
            "运行验证证据块进了开发输入（此前只喂评审）",
            dev_blob[:0] or "已注入",
        )
        check(
            key_line in dev_blob,
            "开发能看到**原始报错**（traceback 末行不再被截掉）",
            "已注入" if key_line in dev_blob else dev_blob[:200],
        )
        check(
            "不在方案的改动清单" in dev_blob,
            "开发能看到机械事实（据此判断该改 import 还是请方案补 changes）",
            "已注入" if "不在方案的改动清单" in dev_blob else dev_blob[:200],
        )
        check(
            "py_compile" not in dev_blob,
            "只喂失败的命令（成功的命令不占开发预算）",
            "ok" if "py_compile" not in dev_blob else "混入了成功命令",
        )

        # 方案阶段也带证据（回退到 architect_plan 时才看得到失败原因）
        plan_parts = prompts.parts_plan(REQ, {"goal": "g"}, None, "", ["x"],
                                            verify=vr_fail, prev_plan=plan_stub, impl=None)
        plan_blob = "\n".join(p for p in plan_parts if p)
        check(
            key_line in plan_blob and "方案层" in plan_blob,
            "回退到方案阶段时同样能看到失败原因与归属事实",
            "已注入" if key_line in plan_blob else plan_blob[:200],
        )
        review_parts = prompts.parts_review(
            REQ, {"goal": "g"}, plan_stub,
            {"edits": [{"path": "input_handler.py"}], "summary": "s"},
            {"cases": [], "automated_commands": []}, None, verify=vr_fail,
        )
        review_blob = "\n".join(p for p in review_parts if p)
        check(
            key_line in review_blob and "方案层" in review_blob,
            "评审的 verify 视图带上机械事实（判归属的直接依据）",
            "已注入",
        )

        # ------------------------------------------------------ 机制加固（真机 run 20260924-185507）
        print("\n== 机制加固（人工预算 / 新增文件语法 / 跨文件接口 / 入口脚本 / 返工退化）")

        # ① 人工介入的预算语义：打回后不能立刻触顶
        b_orch = make(root, "budget-topup", max_rework=2)
        b_orch.attempt = 3
        b_orch.max_rework = 2
        b_orch.state = {}
        b_logs: list[str] = []
        b_orch.log = b_logs.append
        b_orch._extend_budget_for_human("人工审核打回")
        check(
            b_orch.max_rework >= b_orch.attempt + 1,
            "人工介入后上限被抬到「够跑完下一轮」（否则打回只换一轮且必然判死）",
            f"attempt={b_orch.attempt} max_rework={b_orch.max_rework}",
        )
        check(any("[预算]" in x for x in b_logs), "追加预算留有日志（人工可查）", str(b_logs[:1]))
        calm = make(root, "budget-calm", max_rework=5)
        calm.attempt = 1
        calm.max_rework = 5
        calm.state = {}
        calm._extend_budget_for_human("x")
        check(calm.max_rework == 5, "未触顶时不动预算（正常路径行为零变化）", str(calm.max_rework))
        capped = make(root, "budget-cap", max_rework=2)
        capped.attempt = 9
        capped.state = {"human_budget_topups": config_mod.HUMAN_REWORK_TOPUP_MAX}
        before_cap = capped.max_rework
        capped._extend_budget_for_human("x")
        check(
            capped.max_rework == before_cap,
            "追加次数到顶后不再自动追加（防无人值守下无限增长）",
            f"{before_cap} -> {capped.max_rework}",
        )

        # 端到端：人工 reject 后那一轮能跑完并再次停到交付闸门（不再立刻 needs_human）
        class _PassOnThird(MockClient):
            """前三轮判 rework_dev、第三轮起判 pass —— 复刻真机那条"超预算时停在交付闸门"的路径。"""

            def __init__(self) -> None:
                super().__init__()
                self.reviews = 0
                self.always_pass = False

            def chat_json(self, spec, system, user, schema, num_predict=None, attempts=2):
                data, meta = super().chat_json(spec, system, user, schema, num_predict, attempts)
                if spec.role == "评审":
                    self.reviews += 1
                    if self.always_pass or self.reviews >= 3:
                        data.update({"verdict": "pass", "required_fixes": [],
                                     "required_fixes_detail": [], "blockers": []})
                    else:
                        data.update({
                            "verdict": "rework_dev",
                            "required_fixes": ["把没做的补上"],
                            "required_fixes_detail": [
                                {"fix": "把没做的补上", "scope": "in_material", "why": "实现缺"}
                            ],
                            "blockers": [],
                        })
                return data, meta

        e2e_client = _PassOnThird()
        e2e_orch = make(root, "budget-e2e", client=e2e_client, max_rework=2, review_every=1)
        first = e2e_orch.run(REQ)
        check(
            first.paused and first.paused_after == "human_review",
            "第 3 轮通过后停在交付闸门（attempt 超上限但不该判死）",
            f"{first.paused_after} attempt={e2e_orch.attempt}",
        )
        e2e_client.always_pass = True
        runstore.save_artifact(
            first.run_dir, "human_review",
            {"verdict": "reject", "notes": "实测不可玩：方向控制没实现、界面没输出",
             "reviewer": "smoke"},
        )
        e2e_logs: list[str] = []
        e2e_orch.log = e2e_logs.append
        after_reject = e2e_orch.resume(first.run_dir)
        check(
            e2e_orch.max_rework > 2,
            "人工打回自动追加了回流预算",
            f"max_rework={e2e_orch.max_rework} attempt={e2e_orch.attempt}",
        )
        check(
            after_reject.paused and after_reject.paused_after == "human_review" and not e2e_orch.needs_human,
            "打回后的那一轮真的跑完了（不再「打回一轮就判死」）",
            f"paused={after_reject.paused} after={after_reject.paused_after} needs_human={e2e_orch.needs_human}",
        )
        check(
            any("人工审核打回" in x for x in e2e_logs) and any("[预算]" in x for x in e2e_logs),
            "日志里能看到「打回 → 追加预算」这条链",
            str([x for x in e2e_logs if "[预算]" in x][:1]),
        )

        # ② 新增文件的语法级校验（原来整份写入就放行）
        check(patches_mod._balance_problem("x = (1, 2)") is None, "配平正确的代码不误报")
        check(patches_mod._balance_problem("# 注释里的 ' 引号不算字符串") is None, "注释里的引号不误报")
        check(patches_mod._balance_problem('s = "a\\"b"') is None, "转义引号不误报")
        check("未闭合" in (patches_mod._balance_problem("y = (1, 2") or ""), "缺右括号被抓出", "")
        check(
            "未闭合" in (patches_mod._balance_problem("print(f'{") or ""),
            "写残的 f-string 被抓出（真机那个 bug 的形态）",
            str(patches_mod._balance_problem("print(f'{")),
        )
        check(
            patches_mod.check_new_file_content("x = 1", "a.md") is None,
            "非代码文件不做内容校验（不误伤 README 之类）",
        )
        check(
            patches_mod.check_new_file_content("def x(:\n    pass", "a.py") is not None,
            "语法错（.py）被 compile 抓出",
            str(patches_mod.check_new_file_content("def x(:\n    pass", "a.py")),
        )
        check(
            patches_mod.check_new_file_content("", "a.py") is not None,
            "新增文件内容为空也被指出",
        )

        # ③ 跨文件接口静态核对（执行类检查会被语法错掩盖，这里一次列全）
        iface_root = root / "iface"
        iface_root.mkdir(parents=True, exist_ok=True)
        (iface_root / "use.py").write_text("from impl import Helper\n\nprint(Helper)\n", encoding="utf-8")
        (iface_root / "impl.py").write_text("class Other:\n    pass\n", encoding="utf-8")
        iface = verify_mod.audit_interfaces(iface_root, ["use.py", "impl.py"])
        check(
            any("并没有定义 Helper" in x for x in iface["missing_symbols"]),
            "跨文件接口不一致被静态抓出",
            str(iface["missing_symbols"]),
        )
        check(
            iface["definitions"].get("impl") == ["Other"],
            "静态审计同时给出「每个文件定义了哪些符号」（喂给评审/开发的清单）",
            str(iface["definitions"]),
        )
        (iface_root / "broke.py").write_text("def x(:\n", encoding="utf-8")
        iface2 = verify_mod.audit_interfaces(iface_root, ["use.py", "impl.py", "broke.py"])
        check(
            iface2["unparsable"] and any("Helper" in x for x in iface2["missing_symbols"]),
            "有文件解析不了时，其它文件的问题照样列出来（不被掩盖）",
            f"unparsable={iface2['unparsable']} missing={iface2['missing_symbols']}",
        )
        check(
            iface2["definitions"].get("broke") is None,
            "解析不了的文件不产出定义清单 —— 也绝不据此断言它缺符号",
            str(sorted(iface2["definitions"])),
        )

        # ③a-2 anchor 自动补全 + symbol_span 的尾随空行修复
        # 真机高频病：模型把 `def hello(self, name, greeting='hi')` 抄成 `def hello(self)`，
        # 定位不到就被判 anchor_not_found 打回，白烧一轮。
        ar_repo = root / "anchor_repair"
        ar_repo.mkdir(parents=True, exist_ok=True)
        AR_SRC = (
            "class Greeter:\n"
            "    def hello(self, name, greeting='hi'):\n"
            "        return f'{greeting}, {name}'\n"
            "\n"
            "    def bye(self):\n"
            "        return 'bye'\n"
        )
        (ar_repo / "mod.py").write_text(AR_SRC, encoding="utf-8")
        # symbol_span 不能把符号后的空行算进来，否则 `span 行数 > anchor 行数` 在
        # Python 里恒成立，模型抄对了完整符号也会被判 patch_span_mismatch
        check(
            patches_mod.symbol_span(AR_SRC.splitlines(), "hello") == (1, 2),
            "symbol_span 不把符号**后的空行**算进范围（否则判据恒成立）",
            str(patches_mod.symbol_span(AR_SRC.splitlines(), "hello")),
        )
        ar_full_patch = (
            "    def hello(self, name, greeting='hi'):\n"
            "        return f'[{greeting}] {name}'\n"
        )
        ar_impl = {"edits": [{
            "path": "mod.py", "change_type": "modify", "target_symbol": "hello",
            "patch": ar_full_patch,
            "anchor": "def hello(self):",       # 缩写，且只覆盖 1 行
            "patch_mode": "replace_span",
        }]}
        check(
            not patches_mod.find_anchor(AR_SRC, "def hello(self):"),
            "缩写 anchor 原本定位不到（复现病症）",
        )
        ar_rep = patches_mod.repair_anchors(ar_repo, ar_impl)
        check(
            ar_rep["repaired"] == 1 and "mod.py::hello" in ar_rep["detail"],
            "缩写 anchor 被自动补全",
            str(ar_rep),
        )
        check(
            "replace_span + 完整符号 patch ⇒ anchor 扩成**整个符号**（不只签名行）",
            "return f'{greeting}, {name}'" in ar_impl["edits"][0]["anchor"],
            repr(ar_impl["edits"][0]["anchor"]),
        )
        ar_status = patches_mod.analyze_all(ar_repo, ar_impl)["edits"][0].get("status")
        check(
            ar_status == "ok",
            "补全后端到端判为可套用（不再 anchor_not_found / patch_span_mismatch）",
            str(ar_status),
        )
        # insert_after 只需签名行定位，不该扩到整个符号（patch 是插进去的新代码）
        ar_ins = {"edits": [{
            "path": "mod.py", "change_type": "modify", "target_symbol": "bye",
            "patch": "def extra():\n    return 1\n",
            "anchor": "def b", "patch_mode": "insert_after",
        }]}
        check(
            patches_mod.repair_anchors(ar_repo, ar_ins)["repaired"] == 1
            and ar_ins["edits"][0]["anchor"].strip().startswith("def bye"),
            "insert_after 补成签名行即可（不扩到整个符号）",
            repr(ar_ins["edits"][0]["anchor"]),
        )
        # 安全闸门：片段补丁不猜范围 / full_symbol 不参与 / 符号不唯一不补 / 符号不存在不补
        ar_frag = {"edits": [{
            "path": "mod.py", "change_type": "modify", "target_symbol": "hello",
            "patch": "        return 'CHANGED'\n",
            "anchor": "def hello(self):", "patch_mode": "replace_span",
        }]}
        check(
            patches_mod.repair_anchors(ar_repo, ar_frag)["repaired"] == 0
            and ar_frag["edits"][0]["anchor"] == "def hello(self):",
            "replace_span + 片段补丁不猜范围（宁可照旧判负）",
        )
        ar_fs = {"edits": [{
            "path": "mod.py", "change_type": "modify", "target_symbol": "hello",
            "patch": "def hello(self):\n    return 'x'\n", "anchor": "",
            "patch_mode": "full_symbol",
        }]}
        check(
            patches_mod.repair_anchors(ar_repo, ar_fs)["repaired"] == 0,
            "full_symbol 不参与补全（anchor 对它只是辅助）",
        )
        ar_dup = root / "anchor_dup"
        ar_dup.mkdir(parents=True, exist_ok=True)
        (ar_dup / "m.py").write_text(
            "class A:\n    def go(self):\n        return 1\n\n\nclass B:\n    def go(self):\n        return 2\n",
            encoding="utf-8",
        )
        ar_dup_impl = {"edits": [{
            "path": "m.py", "change_type": "modify", "target_symbol": "go",
            "patch": "def go(self):\n    return 9\n", "anchor": "def go(self):",
            "patch_mode": "replace_span",
        }]}
        check(
            patches_mod.repair_anchors(ar_dup, ar_dup_impl)["repaired"] == 0,
            "同名符号不止一处时不补（贴错比不贴危险）",
        )
        ar_ghost = {"edits": [{
            "path": "mod.py", "change_type": "modify", "target_symbol": "ghost",
            "patch": "def ghost():\n    return 0\n", "anchor": "def ghost():",
            "patch_mode": "replace_span",
        }]}
        check(
            patches_mod.repair_anchors(ar_repo, ar_ghost)["repaired"] == 0,
            "符号在原文里不存在时不补（真编造该照旧判负）",
        )

        # ③a-3 漏测阻断 + 申诉通道（mock 下 _test_blockers 被跳过，故直接调它验证）
        from pipeline.orchestrator import Orchestrator as _Orch

        class _TbShim:
            _test_blockers = _Orch._test_blockers
            # _test_blockers 会回落到 _audit_test 重算（state 里没缓存时）
            _audit_test = _Orch._audit_test
            _VAGUE_EXPECTED = _Orch._VAGUE_EXPECTED

            def __init__(self, state, client=None):
                self.state = state
                self.client = client

        tb_impl = {"edits": [
            {"path": "a.py", "target_symbol": "Snake"},
            {"path": "b.py", "target_symbol": "Food"},
        ]}
        tb_cases = {"cases": [
            {"id": "NEW-01", "type": "new", "target": "a.py::Snake",
             "steps": ["构造"], "expected": "返回长度为 3 的列表"},
        ], "automated_commands": [], "coverage_gaps": [], "uncertainties": []}

        def _tb(report, **kw):
            return _TbShim({"implementation": tb_impl, "test_report": report}, **kw)._test_blockers()

        blocked = _tb(tb_cases)
        check(
            bool(blocked) and "Food" in blocked[0],
            "改动的符号既没用例覆盖、也没申诉 → 判阻断（强制 rework）",
            str(blocked)[:140],
        )
        # 申诉通道：在 coverage_gaps 里写明原因即可豁免
        appealed = json.loads(json.dumps(tb_cases))
        appealed["coverage_gaps"] = [{
            "gap": "Food 只改了内部常量，无需单独用例",
            "reason": "Food 的行为未变化", "impact": "无",
        }]
        check(
            _tb(appealed) == [],
            "在 coverage_gaps 里交代过原因即豁免（唯一申诉出口）",
            str(_tb(appealed))[:140],
        )
        # 申诉要针对**具体符号**才生效
        wrong_appeal = json.loads(json.dumps(tb_cases))
        wrong_appeal["coverage_gaps"] = [{
            "gap": "环境限制没法测", "reason": "无头环境", "impact": "中",
        }]
        check(
            bool(_tb(wrong_appeal)),
            "泛泛的申诉（没点名符号）不豁免，否则等于没要求",
            str(_tb(wrong_appeal))[:120],
        )
        # mock 运行下跳过：否则占位数据每轮命中，把流程机制的测试带偏
        from pipeline.ollama_client import MockClient as _MC

        check(
            _tb(tb_cases, client=_MC()) == [],
            "mock 运行下跳过错测阻断（占位产物不带符号级 target）",
        )

        # ③a-4 LSP 健壮性：server 中途死掉绝不能把 verify 带崩
        # （pyright 在超大仓库上会 OOM 退出，那时管道早已断开）
        from pipeline import lsp as _lsp_mod

        class _BoomStdin:
            def write(self, *_a):
                raise BrokenPipeError("langserver gone")

            def flush(self):
                pass

        class _BoomProc:
            stdin = _BoomStdin()

            def poll(self):
                return 0

            def kill(self):
                pass

        sess = _lsp_mod._Session.__new__(_lsp_mod._Session)
        sess.proc = _BoomProc()
        sess.error = ""
        sess.broken = False
        sess._next_id = 1
        sess._q = queue.Queue()
        sess.close = lambda: None  # type: ignore[method-assign]
        crashed = False
        try:
            sess._send({"jsonrpc": "2.0", "method": "x", "params": {}})
        except Exception:  # noqa: BLE001
            crashed = True
        check(
            not crashed and sess.broken and "管道已断" in sess.error,
            "管道断掉时 _send 吞异常并标记 broken（不崩 verify）",
            f"crashed={crashed} broken={sess.broken} err={sess.error}",
        )
        check(
            sess.references("game.py", 1, 8) is None,
            "broken 之后 references 直接返回 None，不白等超时",
        )
        check(
            sess.proc is not None,
            "管道断掉后仍保留进程句柄供清理（置空会导致 close() 漏杀）",
        )

        # 语义检查块：只把高置信项摆给评审，推断性结论只报个数
        sem_block = prompts.semantic_audit_block({
            "available": True, "total": 3, "elapsed_s": 2.1, "filtered_out": 5,
            "diagnostics": [
                {"file": "a.py", "line": 3, "message": "属性不存在", "rule": "r1", "blocking": True},
                {"file": "b.py", "line": 9, "message": "类型不匹配", "rule": "r2", "blocking": False},
                {"file": "c.py", "line": 1, "message": "未定义", "rule": "r3", "blocking": True},
            ],
        })
        check(
            "a.py:3" in sem_block and "b.py:9" not in sem_block.split("推断性")[0],
            "语义块只列高置信项（推断性结论不逐条展开）",
            sem_block[:220],
        )
        check(
            "存量代码" in sem_block,
            "语义块说明被过滤掉的存量既有问题（避免误以为是本次引入）",
            sem_block[:200],
        )
        check(
            prompts.semantic_audit_block({"available": False}) == "",
            "语义检查不可用时不产出空块",
        )

        # ③b 物化阶段的两类阻断（「判 pass 却交付残缺」的直接成因）
        # 补丁部分套用失败原先只记 note ⇒ 沙箱缺改动、验证却可能 pass ⇒ 交付到目标目录才现形。
        pa_root = root / "partial_apply"
        pa_repo = pa_root / "repo"
        pa_repo.mkdir(parents=True, exist_ok=True)
        (pa_repo / "good.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
        pa_impl = {
            "edits": [
                {   # 新增文件：一定能写
                    "path": "added.py", "change_type": "add",
                    "target_symbol": "added_fn", "patch": "def added_fn():\n    return 42\n",
                },
                {   # 目标文件与符号都不存在：必然 skipped
                    "path": "ghost.py", "change_type": "modify",
                    "target_symbol": "ghost", "patch": "def ghost():\n    return 0\n",
                    "anchor": "def ghost():",
                },
            ]
        }
        pa_audit = patches_mod.analyze_all(pa_repo, pa_impl)
        pa_res = verify_mod.verify(
            pa_root / "run", pa_repo, pa_impl, pa_audit, None,
            enabled=True, timeout=30, max_commands=2, copy_limit_mb=50,
        )
        check(
            bool(pa_res["materialized"]) and pa_res["verdict"] == "fail",
            "部分补丁没套上时判负（不因「剩下的还能跑通」而 pass）",
            f"written={pa_res['materialized']} verdict={pa_res['verdict']}",
        )
        check(
            any("未能套用" in p for p in pa_res["problems"]),
            "未套用的原因进 problems 而非只进 notes",
            str(pa_res["problems"])[:150],
        )
        # 对照组：全部能套上就不该出现「未能套用」，证明不是无差别判负
        ok_impl = {"edits": [pa_impl["edits"][0]]}
        ok_res = verify_mod.verify(
            pa_root / "run_ok", pa_repo, ok_impl,
            patches_mod.analyze_all(pa_repo, ok_impl), None,
            enabled=True, timeout=30, max_commands=2, copy_limit_mb=50,
        )
        check(
            not any("未能套用" in p for p in ok_res["problems"]),
            "对照组：补丁全能套上时不报「未能套用」",
            str(ok_res["problems"])[:150],
        )

        # 幂等命中（内容已在原文里）是**正确行为**，不能被当成交付物残缺。
        # 闸门「预览物化」写一遍 → 放行后「正式交付」再套一遍，第二次必然全部命中；
        # 若不排除，正常的重复交付会被误标成「部分交付」并顶起 needs_human。
        idem_root = root / "idempotent"
        idem_repo = idem_root / "repo"
        idem_repo.mkdir(parents=True, exist_ok=True)
        (idem_repo / "a.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
        idem_impl = {"edits": [{
            "path": "a.py", "change_type": "modify", "target_symbol": "hello",
            "patch": "def hello():\n    return 'hi there'\n",
            "anchor": "def hello():\n    return 'hi'",  # 覆盖完整函数体，与 patch 规模一致
        }]}
        idem_audit = patches_mod.analyze_all(idem_repo, idem_impl)
        r1 = patches_mod.apply_all(idem_repo, idem_impl, idem_audit, in_place=True)
        r2 = patches_mod.apply_all(idem_repo, idem_impl, idem_audit, in_place=True)
        sk2 = r2.get("skipped") or []
        check(
            len(r1.get("files") or []) > 0 and len(r2.get("files") or []) == 0,
            "同一批补丁套两次：第一次写入、第二次幂等跳过",
            f"files1={len(r1.get('files') or [])} files2={len(r2.get('files') or [])}",
        )
        check(
            bool(sk2) and all(patches_mod.is_benign_skip(s) for s in sk2),
            "幂等命中被判定为良性（不会误标成部分交付）",
            str([s.get("reason") for s in sk2]),
        )
        check(
            (idem_repo / "a.py").read_text(encoding="utf-8").count("def hello") == 1,
            "幂等闸防止了重复追加（方法不会出现两份）",
        )
        ghost_impl = {"edits": [{
            "path": "ghost.py", "change_type": "modify", "target_symbol": "ghost",
            "patch": "def ghost():\n    return 0\n", "anchor": "def ghost():",
        }]}
        rg = patches_mod.apply_all(
            idem_repo, ghost_impl, patches_mod.analyze_all(idem_repo, ghost_impl),
            in_place=False, out_dir=idem_root / "out",
        )
        skg = rg.get("skipped") or []
        check(
            bool(skg) and not any(patches_mod.is_benign_skip(s) for s in skg),
            "真缺失（文件/符号不存在）不被误判成良性",
            str([s.get("reason") for s in skg]),
        )

        # 绝对路径必须被收敛成相对 repo 的路径。
        # pathlib 里 ``out / path`` 遇到绝对路径会**整体覆盖 out**，于是 in_place=False
        # 照样把文件写回原仓库、out_dir 落空 —— 真机 run 20260925-221002：verify 阶段就把
        # 产物写进了用户目录（绕过交付门禁），沙箱却是空的，命令全在空目录里跑。
        abs_root = root / "abs_path"
        abs_repo = abs_root / "repo"
        abs_repo.mkdir(parents=True, exist_ok=True)
        (abs_repo / "keep.py").write_text("def keep():\n    return 1\n", encoding="utf-8")
        abs_out = abs_root / "sandbox"
        abs_impl = {"edits": [{
            "path": str((abs_repo / "made.py").resolve()),  # ← 模型常给的绝对路径
            "change_type": "add", "target_symbol": "made",
            "patch": "def made():\n    return 42\n",
        }]}
        abs_rep = patches_mod.apply_all(
            abs_repo, abs_impl, patches_mod.analyze_all(abs_repo, abs_impl),
            in_place=False, out_dir=abs_out,
        )
        check(
            (abs_out / "made.py").exists(),
            "绝对路径补丁被写进 out_dir（沙箱不再是空的）",
            str(sorted(p.name for p in abs_out.rglob("*") if p.is_file())),
        )
        check(
            not (abs_repo / "made.py").exists(),
            "in_place=False 时原仓库绝不被写入",
            str(sorted(p.name for p in abs_repo.iterdir())),
        )
        check(
            all(not Path(f["path"]).is_absolute() for f in abs_rep.get("files") or []),
            "报告里的 path 是相对路径",
            str([f["path"] for f in abs_rep.get("files") or []]),
        )
        # 越界绝对路径：in_place=False 的语义是「写到 out_dir」，越界路径会绕过它
        out_impl = {"edits": [{
            "path": str((abs_root / "outside.py").resolve()),
            "change_type": "add", "target_symbol": "outside",
            "patch": "def outside():\n    return 0\n",
        }]}
        out_rep = patches_mod.apply_all(
            abs_repo, out_impl, patches_mod.analyze_all(abs_repo, out_impl),
            in_place=False, out_dir=abs_root / "sb3",
        )
        check(
            not (out_rep.get("files") or []) and not (abs_root / "outside.py").exists(),
            "越出仓库的绝对路径被拒绝写入",
            str(out_rep.get("files"))[:120],
        )

        # 影响面扫描：谁在调用本次被改的符号（audit_interfaces 只看产出内部，不扫存量上游）
        im_root = root / "impact"
        im_work = im_root / "work"
        im_work.mkdir(parents=True, exist_ok=True)
        (im_work / "main.py").write_text(
            "from game_logic import Game\n\ng = Game()\ng.move(1)\n", encoding="utf-8"
        )
        (im_work / "game_logic.py").write_text(
            "class Game:\n    def move(self, x):\n        return x\n", encoding="utf-8"
        )
        im_impl = {"edits": [{"path": "game_logic.py", "target_symbol": "Game"}]}
        im = verify_mod.audit_impact(im_work, ["game_logic.py"], im_impl)
        im_up = [c for c in im["callers"] if not c["in_this_round"]]
        check(
            any(c["file"] == "main.py" for c in im_up),
            "影响面扫出**存量**上游调用点（产出之外的调用方）",
            str([(c["file"], c["lineno"], c["kind"]) for c in im_up]),
        )
        check(
            {c["kind"] for c in im_up} >= {"call", "import"},
            "调用点与导入点两类都抓到",
            str(sorted({c["kind"] for c in im_up})),
        )
        check(
            all(c["in_this_round"] for c in im["callers"] if c["file"] == "game_logic.py"),
            "本轮产出自身的调用不算存量上游（不虚增影响面）",
        )
        # 方法级符号（属性调用）
        im2 = verify_mod.audit_impact(
            im_work, ["game_logic.py"], {"edits": [{"path": "game_logic.py", "target_symbol": "move"}]}
        )
        check(
            any(c["symbol"] == "move" and c["file"] == "main.py" for c in im2["callers"]),
            "方法级符号（g.move(...)）也扫得到",
            str([(c["symbol"], c["file"], c["context"]) for c in im2["callers"]]),
        )
        # 解析不了只记、不误判
        (im_work / "broken.py").write_text("def x(:\n", encoding="utf-8")
        im3 = verify_mod.audit_impact(im_work, ["game_logic.py"], im_impl)
        check(
            "broken.py" in im3["unparsable"]
            and any(c["file"] == "main.py" for c in im3["callers"]),
            "解析不了的文件只记 unparsable，不影响其它结果",
            str(im3["unparsable"]),
        )
        # 纯新建：没有存量上游就不谎报
        solo_root = root / "impact_solo"
        solo_root.mkdir(parents=True, exist_ok=True)
        (solo_root / "only.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        im4 = verify_mod.audit_impact(
            solo_root, ["only.py"], {"edits": [{"path": "only.py", "target_symbol": "f"}]}
        )
        check(
            all(c["in_this_round"] for c in im4["callers"]),
            "纯新建场景没有存量上游时不谎报",
            str(im4["callers"]),
        )
        check(
            verify_mod.audit_impact(im_work, [], {"edits": []})["callers"] == [],
            "没声明 target_symbol 时不硬扫",
        )
        # LSP 增强：与 ast **双向对拍**。条件断言 —— 没有 langserver 时只验降级。
        from pipeline import lsp as _lsp_mod

        diff_root = root / "impact_lsp"
        diff_root.mkdir(parents=True, exist_ok=True)
        (diff_root / "game.py").write_text(
            "class Game:\n    def move(self, x):\n        return x\n\n\ndef make_game():\n    return Game()\n",
            encoding="utf-8",
        )
        (diff_root / "other.py").write_text(
            "class Unrelated:\n    def move(self, x):\n        return x\n", encoding="utf-8"
        )
        (diff_root / "use.py").write_text(
            "from game import Game as G, make_game\n"
            "\n"
            "a = G()\n"            # 别名：ast 匹配不到 "Game"
            "a.move(1)\n"
            "h = make_game()\n"    # 间接实例：ast 其实**能**靠 .move 属性名找到
            "h.move(2)\n"
            "\n"
            "u = Unrelated()\n"    # 同名不同物：ast 会误报
            "u.move(3)\n",
            encoding="utf-8",
        )
        diff_impl = {"edits": [
            {"path": "game.py", "target_symbol": "Game"},
            {"path": "game.py", "target_symbol": "move"},
        ]}
        diff = verify_mod.audit_impact(diff_root, ["game.py"], diff_impl)
        ast_pairs = {(c["symbol"], c["file"], c["lineno"]) for c in diff["callers"]}
        check(
            ("move", "use.py", 6) in ast_pairs,
            "ast 靠属性名就能找到「间接实例」调用（h = make_game(); h.move()）"
            "—— 别再以为它找不到",
            str(sorted(ast_pairs)),
        )
        check(
            ("move", "use.py", 9) in ast_pairs,
            "ast 把「同名不同物」也记成上游（这是它的硬伤，要靠 LSP 纠正）",
        )
        info = diff.get("lsp") or {}
        if info.get("available"):
            extra = info.get("extra") or []
            ast_only = info.get("ast_only") or []
            check(
                any(i["symbol"] == "Game" and i["ref_file"] == "use.py" for i in extra),
                "LSP 补上 ast 漏掉的**别名 import**（`from game import Game as G`）",
                str(extra),
            )
            check(
                any(i["symbol"] == "move" and i["lineno"] == 9 for i in ast_only),
                "LSP 标出 ast 的**同名误报**（u.move 指向 Unrelated，不是本次改的符号）",
                str([(i["symbol"], i["lineno"]) for i in ast_only]),
            )
            block = prompts.impact_audit_block(diff)
            check(
                "降权" in block,
                "误报提示被写进评审 pin 块（让评审别被假上游带偏）",
                block[-160:],
            )
        else:
            check(
                info.get("reason") != "" and diff["callers"],
                "无 langserver 时静默降级（只出 ast 结果，不崩）",
                str(info.get("reason"))[:90],
            )
        # 相对路径必须也能跑：真机传进来的沙箱路径就是相对的，
        # `Path.as_uri()` 对相对路径会直接抛 ValueError（踩过）
        rel_cwd = Path.cwd()
        try:
            import os as _os

            _os.chdir(diff_root.parent)
            rel = verify_mod.audit_impact(
                diff_root.relative_to(diff_root.parent), ["game.py"], diff_impl
            )
            check(
                bool(rel.get("callers")),
                "沙箱传**相对路径**时不炸（as_uri 对相对路径会抛异常）",
                str(len(rel.get("callers") or [])),
            )
        finally:
            _os.chdir(rel_cwd)

        # 符号覆盖核对：「写了很多用例」≠「测到了改动之处」。
        # 真机 run 20260925-184300：15 条用例、5 个被改符号，但用例 target 全写成文件名
        # 而补丁是符号级 —— 5 个里只有 1 个对得上。
        from pipeline.orchestrator import Orchestrator as _Orch

        class _AuditShim:
            """只带 _audit_test 所需状态，避免构造完整 Orchestrator。"""

            _VAGUE_EXPECTED = _Orch._VAGUE_EXPECTED
            _audit_test = _Orch._audit_test

            def __init__(self, state):
                self.state = state

        cov_impl = {"edits": [
            {"path": "a.py", "target_symbol": "Snake"},
            {"path": "b.py", "target_symbol": "Food"},
            {"path": "c.py", "target_symbol": "main"},
        ]}
        # 用例 target 写成文件名（真机的失败形态）
        cov_bad = {"cases": [
            {"id": "NEW-01", "type": "new", "target": "a.py",
             "steps": ["调一下"], "expected": "返回长度为 3 的列表"},
        ], "automated_commands": [], "coverage_gaps": [], "uncertainties": []}
        a_bad = _AuditShim({"implementation": cov_impl, "test_report": cov_bad})._audit_test()
        # 注意：这里刻意没有形如 `main.py` 的 target。若写了，"main" 会因文件名
        # 巧合被算作覆盖 —— 这是子串匹配的已知宽松点，但本项只作提示级，可以接受。
        check(
            set(a_bad["missing_symbols"]) == {"Snake", "Food", "main"},
            "用例 target 只写文件名时，没被测到的符号被揪出来",
            f"missing={a_bad['missing_symbols']}",
        )
        check(
            sorted(a_bad["covered_symbols"] + a_bad["missing_symbols"])
            == sorted(a_bad["changed_symbols"]),
            "覆盖 + 缺失 = 全部符号（没算重也没算漏）",
            f"{a_bad['covered_symbols']} + {a_bad['missing_symbols']}",
        )
        # 对照：target 写到符号级（新提示词要求的写法）→ 不该再报
        cov_ok = {"cases": [
            {"id": "NEW-01", "type": "new", "target": "a.py::Snake",
             "steps": ["构造 Snake"], "expected": "返回长度为 3 的列表"},
            {"id": "REG-01", "type": "regression", "target": "b.py::Food",
             "steps": ["构造 Food"], "expected": "返回长度为 3 的列表"},
            {"id": "COMP-01", "type": "compat", "target": "c.py::main",
             "steps": ["调用 main"], "expected": "返回长度为 3 的列表"},
        ], "automated_commands": [], "coverage_gaps": [], "uncertainties": []}
        a_ok = _AuditShim({"implementation": cov_impl, "test_report": cov_ok})._audit_test()
        check(
            not a_ok["missing_symbols"],
            "对照：target 写到符号级后不再报缺失（不是无差别报警）",
            f"missing={a_ok['missing_symbols']}",
        )
        check(
            not a_ok["missing_types"] and not a_bad["missing_types"] is None,
            "三类齐全的用例不报 missing_types（与符号覆盖是两回事）",
            str(a_ok["missing_types"]),
        )

        # 语义诊断（pyright）：抓 ast 抓不到的类型级错误。
        # 刻意做成**条件断言** —— pyright 装在全局 npm 目录，换机器就没有；
        # 测试不能硬依赖它，不可用时只验「静默降级，不崩」。
        from pipeline import semantics as _sem

        sem_root = root / "semantics"
        sem_repo = sem_root / "repo"
        sem_repo.mkdir(parents=True, exist_ok=True)
        sem_run = sem_root / "run"
        sem_run.mkdir(parents=True, exist_ok=True)

        class _SemShim:
            _semantic_problems = _Orch._semantic_problems

            def __init__(self, repo, run_dir):
                self.repo = repo
                self.run_dir = run_dir
                self.state = {}
                self.logs = []

            def log(self, msg):
                self.logs.append(str(msg))

        def _mk(body: str) -> dict:
            return {"edits": [{
                "path": "main.py", "change_type": "add",
                "target_symbol": "Greeter", "patch": body,
            }]}

        bad_body = (
            "class Greeter:\n"
            "    def hello(self):\n"
            "        return 'hi'\n"
            "\n"
            "\n"
            "g = Greeter()\n"
            "print(g.helo())\n"          # 属性名拼错：语法合法、import 无问题
        )
        good_body = bad_body.replace("g.helo()", "g.hello()")

        sem_problems = _SemShim(sem_repo, sem_run)._semantic_problems(_mk(bad_body))
        if _sem.available():
            check(
                any("helo" in p for p in sem_problems),
                "语义诊断抓到属性名拼错（ast / import 检查都抓不到）",
                str(sem_problems)[:160],
            )
            check(
                any("main.py" in p and "行" in p for p in sem_problems),
                "问题描述带文件名与行号（可直接回灌给 dev 重问）",
                str(sem_problems)[:160],
            )
            check(
                not _SemShim(sem_repo, sem_run)._semantic_problems(_mk(good_body)),
                "对照：正确代码不报问题（不是无差别报警）",
            )
        else:
            check(
                sem_problems == [],
                "pyright 不可用时静默返回空（可选增强，不是依赖）",
                str(_sem.unavailable_reason())[:80],
            )
        check(
            (sem_run / "semantic-probe" / "main.py").exists() or not _sem.available(),
            "语义探测确实把实现物化到了 run 目录",
        )

        # 沙箱保真度：源码没复制进来 ⇒ 后面每项检查都失真，判负并提前返回
        sb_root = root / "sandbox_fidelity"
        sb_repo = sb_root / "repo"
        sb_repo.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (sb_repo / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n", encoding="utf-8")
        sb_impl = {"edits": [{
            "path": "m0.py", "change_type": "modify", "target_symbol": "f0",
            "patch": "def f0():\n    return 99\n", "anchor": "def f0():",
        }]}
        sb_res = verify_mod.verify(
            sb_root / "run", sb_repo, sb_impl,
            patches_mod.analyze_all(sb_repo, sb_impl), None,
            enabled=True, timeout=30, max_commands=2,
            copy_limit_mb=0,  # 触发总量上限
        )
        check(
            sb_res["verdict"] == "fail" and any("沙箱" in p for p in sb_res["problems"]),
            "沙箱复制被截断时判负（结论不可信）",
            f"verdict={sb_res['verdict']} problems={sb_res['problems'][:1]}",
        )
        check(
            len(sb_res["commands"]) == 0,
            "沙箱不完整时提前返回，不跑无意义的命令",
            f"commands={len(sb_res['commands'])}",
        )
        # 反向：只跳过二进制资源（非源码）不该判「沙箱不完整」
        res_root = root / "sandbox_resource_only"
        res_repo = res_root / "repo"
        res_repo.mkdir(parents=True, exist_ok=True)
        (res_repo / "core.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        with (res_repo / "asset.bin").open("wb") as fh:
            fh.truncate(21 * 1024 * 1024)  # 超过单文件上限，但它是资源不是源码
        res_impl = {"edits": [{
            "path": "core.py", "change_type": "modify", "target_symbol": "f",
            "patch": "def f():\n    return 2\n", "anchor": "def f():",
        }]}
        res_res = verify_mod.verify(
            res_root / "run", res_repo, res_impl,
            patches_mod.analyze_all(res_repo, res_impl), None,
            enabled=True, timeout=30, max_commands=2, copy_limit_mb=200,
        )
        check(
            not any(("沙箱不完整：" in p or "沙箱复制触发" in p) for p in res_res["problems"]),
            "只跳过二进制资源时不判「沙箱不完整」（避免误伤）",
            str(res_res["problems"])[:150],
        )

        # ④ 入口脚本：rc=0 ≠ 跑起来了
        entry_root = root / "entry"
        entry_root.mkdir(parents=True, exist_ok=True)
        (entry_root / "no_main.py").write_text("class A:\n    pass\n", encoding="utf-8")
        (entry_root / "has_main.py").write_text("if __name__ == '__main__':\n    print('hi')\n", encoding="utf-8")
        entry_problems = verify_mod.entry_script_problems(
            entry_root,
            [
                {"command": "python no_main.py", "status": "ok"},
                {"command": "python has_main.py", "status": "ok"},
                {"command": "python -m pytest -q", "status": "ok"},
                {"command": "python build.py --all", "status": "ok"},
            ],
        )
        check(
            len(entry_problems) == 1 and "no_main.py" in entry_problems[0],
            "rc=0 但没有 __main__ 的入口脚本被指出（假绿）",
            str(entry_problems),
        )
        check(
            verify_mod.entry_script_problems(entry_root, [{"command": "python no_main.py", "status": "fail"}]) == [],
            "已经判失败的命令不再追问（不重复计问题）",
        )

        # ⑤ 返工退化：符号消失必须声明
        van_orch = make(root, "vanished")
        van_orch.state = {
            "plan": {"changes": [{"path": "a.py"}], "tasks": [{"id": "T-01", "target_files": ["a.py"]}]},
            "implementation_symbols_prev": ["a.py::Keep", "a.py::Gone"],
            "implementation": {
                "edits": [{"path": "a.py", "change_type": "add", "target_symbol": "Keep",
                           "patch": "class Keep:\n    pass\n"}],
                "not_implemented": [],
                "deviations": [],
            },
        }
        van_audit = van_orch._audit_implementation()
        check(
            van_audit["vanished_symbols"] == ["a.py::Gone"],
            "返工让上一轮的符号消失且未声明 → 被抓出",
            str(van_audit["vanished_symbols"]),
        )
        check(
            any("消失了却没有声明" in x for x in van_orch._patch_blockers()),
            "符号消失未声明属于阻断级（评审不能给它 pass）",
            str(van_orch._patch_blockers())[:140],
        )
        van_orch.state["implementation"]["not_implemented"] = [{"task": "Gone", "reason": "本轮先不做"}]
        check(
            van_orch._audit_implementation()["vanished_symbols"] == [],
            "显式声明过的消失不算退化（可能是刻意删除）",
            str(van_orch._audit_implementation()["vanished_symbols"]),
        )

        # ⑥ 补丁正文的裸 CR 归一
        # 模型想写 Python 的 `\r` 转义，却在 JSON 里只写了一个反斜杠 → 解码后变成**真回车**，
        # 落进源码字符串就是「单引号字符串跨行」。真机 run 20260924-185507 连续 4 轮栽在这里。
        crlf_text = "a = 1\r\nb = 2\r\n"
        crlf_out, crlf_fixed = patches_mod.normalize_patch_text(crlf_text)
        check(
            crlf_fixed == 0 and crlf_out == crlf_text,
            "CRLF 是正常行尾，不被误改",
            repr(crlf_out[:12]),
        )
        cr_text = "def f():\n    print('x', end='\r', flush=True)\n"
        cr_out, cr_fixed = patches_mod.normalize_patch_text(cr_text)
        check(
            cr_fixed == 1 and "\r" not in cr_out and "\\r" in cr_out,
            "裸 CR 被转义成反斜杠+r 两个字符（否则源码字符串跨行必报语法错）",
            repr(cr_out.strip()),
        )
        check(
            patches_mod.check_new_file_content(cr_text, "a.py") is not None,
            "未归一前：裸 CR 会被内容校验拦下（且给出可读原因）",
            str(patches_mod.check_new_file_content(cr_text, "a.py")),
        )
        check(
            patches_mod.check_new_file_content(cr_out, "a.py") is None,
            "归一后：同一份内容通过校验（且能真的编译）",
            str(patches_mod.check_new_file_content(cr_out, "a.py")),
        )
        check(
            "未闭合" in (patches_mod._balance_problem("x = 'a\rb'\n") or ""),
            "配平扫描器把裸 CR 当行尾（否则会漏报成「配平正常」）",
            str(patches_mod._balance_problem("x = 'a\rb'\n")),
        )
        cr_impl = {"edits": [{"path": "a.py", "patch": cr_text, "anchor": "x\ry"}]}
        check(
            patches_mod.normalize_implementation(cr_impl) == 2,
            "归一同时覆盖 patch 与 anchor（anchor 逐字匹配原文，也怕 CR）",
            str(patches_mod.normalize_implementation(cr_impl)),
        )
        check(
            patches_mod.normalize_implementation({"edits": [{"path": "a.py", "patch": "x = 1\n"}]}) == 0,
            "干净的补丁不动（归一必须是无副作用的）",
        )

        # ⑦ 人工打回后仍保留「上一轮符号基准」——否则返工退化检测形同虚设
        rw_orch = make(root, "rewind-stash")
        rw_orch.state = {
            "implementation": {
                "edits": [
                    {"path": "a.py", "change_type": "add", "target_symbol": "Keep", "patch": "x"},
                    {"path": "a.py", "change_type": "add", "target_symbol": "Gone", "patch": "x"},
                ]
            }
        }
        rw_orch.run_dir = root / "rewind-stash" / "run"
        rw_orch.run_dir.mkdir(parents=True, exist_ok=True)
        rw_orch._rewind("dev")
        check(
            rw_orch.state.get("implementation_symbols_prev") == ["a.py::Gone", "a.py::Keep"],
            "人工打回后仍保留上一轮符号基准（否则「越改越少」查不出来）",
            str(rw_orch.state.get("implementation_symbols_prev")),
        )
        check(
            "implementation" not in rw_orch.state,
            "实现产物本身被作废（需要重跑）",
            str(sorted(rw_orch.state)),
        )
        rw_orch.state["implementation"] = {
            "edits": [{"path": "a.py", "change_type": "add", "target_symbol": "Keep", "patch": "x"}]
        }
        check(
            rw_orch._audit_implementation()["vanished_symbols"] == ["a.py::Gone"],
            "打回重跑后，消失的符号照样被检测到（基准没丢）",
            str(rw_orch._audit_implementation()["vanished_symbols"]),
        )

        # ⑧ 新增文件写残 → 带问题**原地重问** dev（不整轮回炉）
        class _RepairClient(MockClient):
            """每次 dev 都写残 renderer.py，只有带「上一版被判为不合法」的这次才给完整版。"""

            def __init__(self) -> None:
                super().__init__()
                self.dev_calls = 0
                self.repair_prompts = 0

            def chat_json(self, spec, system, user, schema, num_predict=None, attempts=2):
                data, meta = super().chat_json(spec, system, user, schema, num_predict, attempts)
                if spec.role.startswith("开发"):
                    self.dev_calls += 1
                    if "上一版被判为不合法" in user:
                        self.repair_prompts += 1
                        body = "class Renderer:\n    def x(self):\n        return 1\n"
                    else:
                        body = "class Renderer:\n    def x(self)\n"  # 缺冒号 → 语法错
                    data["edits"] = [{
                        "path": "renderer.py",
                        "change_type": "add",
                        "target_symbol": "Renderer",
                        "anchor": "",
                        "patch_mode": "full_symbol",
                        "covers_tasks": ["T-01"],
                        "patch": body,
                    }]
                return data, meta

        repair_stub = {
            "changes": [{"path": "renderer.py", "change_type": "add",
                         "rationale": "渲染", "minimality_reason": "只新增本文件"}],
            "tasks": [{"id": "T-01", "target_files": ["renderer.py"], "goal": "渲染",
                       "depends_on": [], "acceptance": ["画出棋盘"]}],
            "approach": "新增渲染模块",
            "risks": [],
            "uncertainties": [],
        }
        repair_client = _RepairClient()
        repair_orch = make(root, "repair-dev", client=repair_client)
        repair_orch.requirement = REQ
        repair_orch.run_id = "repair-dev"
        repair_orch.run_dir = root / "repair-dev" / "repair-dev"
        repair_orch.run_dir.mkdir(parents=True, exist_ok=True)
        repair_orch.state = {"plan": repair_stub}
        repair_merged = repair_orch._stage_dev(REQ)
        check(
            repair_client.repair_prompts == 1,
            "写残的新增文件 → 带问题原地重问 dev（不整轮回炉）",
            f"dev_calls={repair_client.dev_calls} repair_prompts={repair_client.repair_prompts}",
        )
        check(
            repair_orch._invalid_new_files(repair_merged) == [],
            "重问后内容合法（模型被明确告知哪里写断了）",
            str(repair_orch._invalid_new_files(repair_merged)),
        )
        check(
            repair_merged["edits"][0]["patch"].endswith("return 1\n"),
            "用的是重出的那一版（按 (path, symbol) 替换，不会留两份定义）",
            repr(repair_merged["edits"][0]["patch"]),
        )
        check(
            repair_orch._invalid_new_files(
                {"edits": [{"path": "a.py", "change_type": "modify", "patch": "def x(:\n"}]}
            ) == [],
            "modify 补丁不做内容自检（只有整份新增文件才适用）",
        )

        # ⑨ 依赖可用性：模型会**自己发明**第三方依赖（真机 run 20260924-185507 的 `import keyboard`）
        import importlib.util as _ilu

        check(
            bool(patches_mod.unavailable_imports("import keyboard\n", "a.py", set())),
            "本环境装不上的第三方依赖被抓出（模型不知道环境里有什么）",
            str(patches_mod.unavailable_imports("import keyboard\n", "a.py", set())),
        )
        check(
            patches_mod.unavailable_imports("import os\nimport sys\nfrom json import dumps\n", "a.py", set()) == [],
            "标准库不算缺依赖",
        )
        check(
            patches_mod.unavailable_imports("import renderer\nfrom renderer import R\n", "a.py", {"renderer"}) == [],
            "本项目自己的模块不算缺依赖",
            str(patches_mod.unavailable_imports("import renderer\n", "a.py", {"renderer"})),
        )
        check(
            patches_mod.unavailable_imports("from . import sibling\n", "a.py", set()) == [],
            "相对导入不判（依赖包结构，判不准）",
        )
        check(
            patches_mod.unavailable_imports("import os, sys\n", "notes.md", set()) == [],
            "非 .py 不做依赖校验",
        )
        for _probe in ("pygame", "pytest", "requests", "yaml"):
            if _ilu.find_spec(_probe):
                check(
                    patches_mod.unavailable_imports(f"import {_probe}\n", "a.py", set()) == [],
                    f"已安装的第三方库不算缺依赖（探针 {_probe}）",
                    str(patches_mod.unavailable_imports(f"import {_probe}\n", "a.py", set())),
                )
                break
        check(
            any(
                "keyboard" in x
                for x in repair_orch._invalid_new_files(
                    {"edits": [{"path": "a.py", "change_type": "add", "target_symbol": "A",
                                "patch": "import keyboard\n\nclass A:\n    pass\n"}]}
                )
            ),
            "缺依赖的问题进入自检清单（会被回灌给 dev，而不是等 verify 跑完一轮）",
            str(repair_orch._invalid_new_files(
                {"edits": [{"path": "a.py", "change_type": "add", "target_symbol": "A",
                            "patch": "import keyboard\n\nclass A:\n    pass\n"}]}
            )),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if failures:
        print(f"{checks - len(failures)}/{checks} 通过，失败项:")
        for item in failures:
            print(" -", item)
        return 1
    print(f"全部通过（{checks} 项断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
