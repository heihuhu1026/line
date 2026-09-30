"""操作页面（pipeline.server）的离线冒烟：起一个临时端口的服务，用 --mock 驱动完整人机闭环。

覆盖：新建运行 -> 人工闸门暂停 -> 查看产物 -> 保存人工编辑 -> 继续执行 -> 跑完；
以及非法输入（未知阶段 / 非法 run_id）的拒绝路径。不加载任何模型。

用法: python tools/smoke_console.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 隔离**本机配置**（`pipeline/config.local.json`，操作页面「配置」页写入的那份）。
#
# 为什么必须做：本套断言里有多条是**针对代码默认闸门行为**的 —— 例如「10 个阶段产物
# （含人工审核）」「旧产物归档 6 份」。而 `config.apply_overrides()` 会在导入时把
# config.local.json 的覆盖应用到全局，**服务端起的子进程也照样读到它**：
# 本机把 human_review_gate 关掉后，human_review 阶段根本不会出现，那两条断言立刻失败
# （实测 123/127；指开配置即恢复 —— 与 smoke_mock 头部记录的是同一类假阴性）。
# 覆盖机制本身另有断言去验，这里只保证「代码默认配置下的行为」这一层是干净的。
if not os.environ.get("PIPELINE_LOCAL_CONFIG"):
    os.environ["PIPELINE_LOCAL_CONFIG"] = str(
        Path(tempfile.gettempdir()) / "pipeline_smoke_console_no_local_config.json"
    )

from pipeline import prompts, runstore, server  # noqa: E402

failures: list[str] = []
checks = 0


def check(cond: bool, label: str, detail: str = "") -> bool:
    global checks
    checks += 1
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    if not cond:
        failures.append(f"{label} {detail}".strip())
    return bool(cond)


def call(port: int, path: str, payload: dict | None = None, method: str = "GET") -> tuple[int, dict | str]:
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json; charset=utf-8"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body.startswith(("{", "[")) else body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, body


def _stuck_detail(run_id: str, want: set[str], timeout: float, last: dict) -> str:
    """超时断言的消息：把**所有能解释"为什么没到"的字段都摊开**。

    真机教训：这条断言偶发失败过两次（mock 运行 0.3s 就跑完，却等满 90s），
    当时的消息只打了一坨 state，看不出是"进程没起来""被别人占着"还是"续跑被拒"。
    自诊断比事后猜便宜得多 —— 下次它再出现，消息里就有答案。
    """
    state = last.get("state") or {}
    return (
        f"{run_id} 未在 {timeout}s 内进入 {want}；"
        f"status={state.get('status')!r} running={last.get('running')!r} "
        f"orphaned={last.get('orphaned')!r} presence={last.get('presence')!r} "
        f"busy_with={last.get('busy_with')!r} resume_error={last.get('resume_error')!r}；"
        f"最后状态={state}"
    )


def wait_done(port: int, run_id: str, want: set[str], timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        code, detail = call(port, f"/api/runs/{run_id}")
        if code == 200 and isinstance(detail, dict):
            last = detail
            state = detail.get("state") or {}
            if state.get("status") in want and not detail.get("running"):
                return detail
        time.sleep(0.35)
    raise AssertionError(_stuck_detail(run_id, want, timeout, last))


def wait_paused(port: int, run_id: str, stage: str, timeout: float = 60.0) -> dict:
    """等到该运行**再次停在**指定闸门上（PM 强控复核用它：判据没解决就不放行）。"""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        code, detail = call(port, f"/api/runs/{run_id}")
        if code == 200 and isinstance(detail, dict):
            last = detail
            state = detail.get("state") or {}
            if (
                state.get("status") == "paused"
                and state.get("paused_after") == stage
                and not detail.get("running")
            ):
                return detail
        time.sleep(0.35)
    raise AssertionError(f"{run_id} 未停在 {stage}；最后状态={last.get('state')}")


def wait_done_with_gate(port: int, run_id: str, want: set[str], timeout: float = 90.0) -> dict:
    """wait_done 的变体：途中若停在人工审核闸门，自动提交「通过」并续跑，直到进入 want。"""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        code, detail = call(port, f"/api/runs/{run_id}")
        if code == 200 and isinstance(detail, dict):
            last = detail
            state = detail.get("state") or {}
            if state.get("status") in want and not detail.get("running"):
                return detail
            if state.get("status") == "paused" and state.get("paused_after") == "human_review":
                call(
                    port,
                    f"/api/runs/{run_id}/artifact",
                    {
                        "stage": "human_review",
                        "artifact": {
                            "verdict": "approve",
                            "core_path_ok": True,
                            "no_obvious_errors": True,
                            "deliverables_complete": True,
                            "requirement_met": True,
                            "notes": "冒烟自动通过",
                            "reviewer": "smoke",
                        },
                    },
                    "POST",
                )
                code2, payload2 = call(port, f"/api/runs/{run_id}/resume", {}, "POST")
                # 续跑被拒必须**说出来**：单驻留下 409（已有别的运行在跑）以前被无声吞掉，
                # 于是"续跑没生效"表现成 90 秒后的一个超时，完全看不出原因。
                if code2 != 200:
                    last["resume_error"] = {"code": code2, "body": payload2}
        time.sleep(0.35)
    raise AssertionError(_stuck_detail(run_id, want, timeout, last))


def check_frontend_syntax() -> None:
    """用 node --check 校验 console.html 的内联脚本（没装 node 就跳过）。"""
    html = (Path(server.__file__).resolve().parent / "console.html").read_text(encoding="utf-8")
    body = re.search(r"(?s)<script>(.*?)</script>", html)
    if not body:
        check(False, "console.html 里找不到内联脚本")
        return
    if not shutil.which("node"):
        print("  [SKIP] 未找到 node，跳过前端脚本语法校验")
        return
    tmp = Path(tempfile.gettempdir()) / "pipeline_console_check.js"
    tmp.write_text(body.group(1), encoding="utf-8")
    try:
        proc = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True, encoding="utf-8")
        check(proc.returncode == 0, "前端脚本语法通过 node --check", (proc.stderr or "").strip()[:200])
    finally:
        tmp.unlink(missing_ok=True)


def check_control_plane(port: int, run_id: str) -> None:
    """交付控制塔（P1 §32/§33/§43）：后端派生视图 + live 端点 + 前端卡片。"""
    print("\n== 交付控制塔（control_plane）")
    code, detail = call(port, f"/api/runs/{run_id}")
    cp = (detail or {}).get("control_plane") if isinstance(detail, dict) else None
    check(isinstance(cp, dict), "详情返回 control_plane 派生视图", f"HTTP {code}")
    if not isinstance(cp, dict):
        return
    for key in ("release_gate", "proof_summary", "proofs", "evidence_summary",
                "evidence", "ontology", "workspace", "tasks", "test",
                "decision", "issues", "next_action"):
        check(key in cp, f"control_plane 含 {key}")
    check("can_pass" in (cp.get("release_gate") or {}),
          "release_gate 含 can_pass（机器放行结论）")
    # 最重要的一条认知：LLM 评审 PASS ≠ 放行
    check((cp.get("decision") or {}).get("semantic_review_is_candidate_only") is True,
          "语义评审 PASS 被显式标为候选（不会被读成放行）")
    # ---- live 轮询：轻量、不得带整份明细
    code, live = call(port, f"/api/runs/{run_id}/live")
    ok = code == 200 and isinstance(live, dict)
    check(ok, "live 轮询端点可用", f"HTTP {code}")
    if ok:
        for key in ("running", "status", "cursor", "proof_status", "proof_counts",
                    "ontology_error_count", "workspace_revision", "can_pass"):
            check(key in live, f"live 含 {key}")
        check("proofs" not in live and "evidence" not in live,
              "live 不得携带整份证明/证据明细（轻量轮询）")
    # ---- 前端卡片存在（脚本语法由 check_frontend_syntax 的 node --check 保证）
    html = (Path(server.__file__).resolve().parent / "console.html").read_text(encoding="utf-8")
    for cid in ("d-control-card", "d-proof-card", "d-evidence-card",
                "d-ontology-card", "d-workspace-card"):
        check(f'id="{cid}"' in html, f"前端存在卡片 {cid}")
    check("renderControlPlane(d)" in html, "renderDetail 会调用 renderControlPlane")


def main() -> int:
    check_frontend_syntax()
    root = Path(tempfile.mkdtemp(prefix="pipeline-console-"))
    server.Handler.runs_dir = root
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"临时操作页面: http://127.0.0.1:{port}/  runs={root}")
    try:
        print("\n== 首页与静态文件")
        code, html = call(port, "/")
        check(code == 200 and "操作台" in str(html), "GET / 返回操作台页面", f"HTTP {code}")
        page = str(html)
        check('id="dg-max"' in page, "页面有「回流上限」输入框（放宽预算的入口）")
        check(
            "已触顶待人工裁决" in page and "st.needs_human" in page,
            "页面把 needs_human 当作可操作相位（否则触顶的运行在页面上无路可走）",
        )
        check("人工审核打回" in page or "回流上限" in page, "页面文案提到人工介入与预算的关系")
        # 缺 state.json 不等于「不能续跑」：入口总闸（GA）期间的运行、以及被拆成作业的运行，
        # 都**本来就没有** state.json（真机 20260926-154413 因此被页面劝退到「只能查看」）。
        check(
            "正在<b>入口总闸</b>（规模判定）阶段" in page,
            "进程还在跑但缺 state 时，文案指向入口总闸而不是「不能续跑」",
        )
        check(
            'st === "job"' in page and "拆分为作业" in page,
            "列表区分「拆分为作业」与「启动失败」（不再把跑得好的大运行说成失败）",
        )
        # 子进程 stdout 重定向到文件时 Python 会块缓冲（8KB 才落盘）—— 不显式关掉，
        # 页面日志视图整段滞后，GA 那几分钟更是一个字节都没有。
        _real_popen = server.subprocess.Popen
        _spawn_seen: dict = {}

        class _CapturePopen:
            def __init__(self, args, **kwargs):
                _spawn_seen["args"] = args
                _spawn_seen["env"] = kwargs.get("env") or {}
                self.returncode = 0

            def poll(self):
                return None

        server.subprocess.Popen = _CapturePopen
        try:
            server._spawn("smoke-spawn", ["python", "-c", "pass"],
                          root / "smoke-spawn" / runstore.LOG_NAME)
        finally:
            server.subprocess.Popen = _real_popen
            server._JOBS.pop("smoke-spawn", None)
            shutil.rmtree(root / "smoke-spawn", ignore_errors=True)
        check(
            (_spawn_seen.get("env") or {}).get("PYTHONUNBUFFERED") == "1",
            "子进程带 PYTHONUNBUFFERED=1（页面日志才能实时跟随，而不是攒够 8KB 才刷新）",
            f"PYTHONUNBUFFERED={( _spawn_seen.get('env') or {}).get('PYTHONUNBUFFERED')}",
        )

        print("\n== 新建运行（人工闸门 pm）")
        code, created = call(
            port,
            "/api/runs",
            {
                "requirement": "给客户列表页增加导出 Excel 按钮，导出当前筛选结果",
                "pause_after": ["pm"],
                "review_every": 1,
                "mock": True,
            },
            "POST",
        )
        check(code == 200 and isinstance(created, dict) and created.get("run_id"), "POST /api/runs", f"HTTP {code} {created}")
        run_id = created["run_id"]

        detail = wait_done(port, run_id, {"paused"})
        check(detail["state"]["cursor"] == "retrieve", "PM 后停在游标 retrieve", str(detail["state"]["cursor"]))
        check(detail["state"]["paused_after"] == "pm", "记录 paused_after=pm")
        check(
            len(detail["stages"]) == 2
            and [s["stage"] for s in detail["stages"]] == ["intake", "pm"],
            "暂停时已有 intake + pm 两个阶段产物",
            str([s["stage"] for s in detail["stages"]]),
        )
        check(detail["handoff"], "handoff.md 已生成并返回")

        print("\n== 列表与日志")
        code, rows = call(port, "/api/runs")
        check(code == 200 and any(r["run_id"] == run_id for r in rows["runs"]), "GET /api/runs 含本次运行")
        row = next(r for r in rows["runs"] if r["run_id"] == run_id)
        check(row["status"] == "paused" and row["paused_after"] == "pm", "列表显示 paused/暂停点")
        code, log = call(port, f"/api/runs/{run_id}/log")
        check(code == 200 and "人工闸门" in str(log), "日志包含暂停提示")

        print("\n== 需求补强 · 待确认问题裁决（并回补强产物）")
        # 用补强产物里**真实存在的条目 ref** 提交裁决，才能验证「并回 NN-intake.json」
        detail0 = call(port, f"/api/runs/{run_id}")[1]
        snap0 = next((s for s in detail0["stages"] if s["stage"] == "intake"), None)
        art0 = (snap0 or {}).get("artifact") or {}
        payload = []
        # 用**产品自己的访问器**读待确认项：契约已从「missing_elements + clarifying_questions
        # 两个列表」合并为「pending_items 一个列表」，intake_items() 两种形状都认。
        # 以前这里直接读旧字段名 —— 契约迁移后本段永远拿到空列表，于是
        # 「裁决并回补强产物」这条路径**静默失去覆盖**（后面几条断言退化成 0==0 恒真，
        # 而需要真有条目才能成立的两条则恒假）。
        for x in prompts.intake_items(art0):
            if x.get("element"):
                payload.append({"kind": "missing_element", "ref": str(x["element"]),
                                "decision": "裁决-" + str(x["element"])})
        real = len(payload)
        check(real > 0, "补强产物里能读出待确认项（否则本段测试形同虚设）",
              str(prompts.intake_items(art0)[:2]))
        payload.append({"kind": "clarifying_question", "ref": "__不存在的条目__", "decision": "未匹配也应保留"})
        payload.append({"kind": "clarifying_question", "ref": "（这条没填裁决）", "decision": "   "})

        code, saved_dec = call(port, f"/api/runs/{run_id}/intake-decisions", {"decisions": payload}, "POST")
        check(code == 200 and len(saved_dec["decisions"]) == real + 1,
              "保存裁决并丢弃空白条目（未匹配的也保留）", f"HTTP {code} real={real}")
        check(saved_dec.get("merged") == real, "裁决按 ref 并回补强产物条目", str(saved_dec.get("merged")))

        detail1 = call(port, f"/api/runs/{run_id}")[1]
        snap1 = next((s for s in detail1["stages"] if s["stage"] == "intake"), None)
        art1 = (snap1 or {}).get("artifact") or {}
        decided = [x for x in prompts.intake_items(art1) if x.get("final_decision")]
        check(len(decided) == real, "补强产物（终稿）里带上 final_decision", str(len(decided)))
        check(
            bool(decided) and str(decided[0].get("default_assumption") or decided[0].get("suggested_answer") or ""),
            "原默认假设/建议答案保留作审计痕迹",
            str(decided[:1]),
        )
        check(
            (detail1["state"].get("intake_decisions") or []) and
            any(d["decision"] == "未匹配也应保留" for d in detail1["state"]["intake_decisions"]),
            "state 里保留结构化副本供页面回显",
        )
        # 采纳默认值时，「默认 / 建议」前缀必须被去掉（否则下游拿到的是猜测口吻，等于没裁决）
        ref0 = payload[0]["ref"] if payload else None
        if ref0:
            code, saved2 = call(
                port,
                f"/api/runs/{run_id}/intake-decisions",
                {"decisions": [dict(payload[0], decision="默认假设" + payload[0]["decision"])]},
                "POST",
            )
            got = next((d["decision"] for d in saved2["decisions"] if d["ref"] == ref0), None)
            check(bool(got) and not got.startswith("默认"), "采纳默认值时去掉「默认」前缀", str(got))
        check(
            (art1.get("confirmed_facts") or []) and
            not any(str(f).startswith("默认") for f in art1["confirmed_facts"]),
            "confirmed_facts 是陈述式结论（不含默认/建议措辞）", str(art1.get("confirmed_facts"))[:160],
        )
        code, bad = call(port, f"/api/runs/{run_id}/intake-decisions", {"decisions": "不是数组"}, "POST")
        check(code == 400, "非数组的 decisions 被拒绝", f"HTTP {code}")

        print("\n== PM 未决问题裁决（与补强裁决同一套逻辑）")
        detail_pm = call(port, f"/api/runs/{run_id}")[1]
        pm_snap = next((s for s in detail_pm["stages"] if s["stage"] == "pm"), None)
        pm_art = (pm_snap or {}).get("artifact") or {}
        qs = [q for q in (pm_art.get("open_questions") or []) if isinstance(q, dict) and q.get("question")]
        pm_payload = [{"kind": "pm_question", "ref": str(q["question"]),
                       "decision": "默认" + str(q.get("assumed_answer") or "X")} for q in qs]
        code, pm_saved = call(port, f"/api/runs/{run_id}/pm-decisions", {"decisions": pm_payload}, "POST")
        check(code == 200, "保存 PM 未决项裁决", f"HTTP {code} {pm_saved}")
        check(pm_saved.get("merged") == len(qs), "裁决按 question 并回 PM 产物", str(pm_saved.get("merged")))
        check(
            bool(pm_saved["decisions"]) and not pm_saved["decisions"][0]["decision"].startswith("默认"),
            "PM 采纳默认取值时去掉「默认」前缀", str(pm_saved["decisions"][:1]),
        )
        detail_pm2 = call(port, f"/api/runs/{run_id}")[1]
        pm_art2 = (next((s for s in detail_pm2["stages"] if s["stage"] == "pm"), None) or {}).get("artifact") or {}
        check(
            bool(pm_art2.get("confirmed_facts")), "PM 产物生成陈述式 confirmed_facts",
            str(pm_art2.get("confirmed_facts"))[:120],
        )
        check(
            any(q.get("final_decision") for q in (pm_art2.get("open_questions") or [])),
            "裁决写入 open_questions.final_decision",
        )

        print("\n== PM 保存裁决时对旧 run 脏快照先归一（BUG-A 第三通道）")
        # 旧 run 的 NN-pm 快照可能是归一钩子收口前落的（含技术实现类问题）。保存裁决时
        # 若不归一就回写，会把脏数据重新固化，续跑覆盖干净 state（真机 180933 即此路径）。
        raw_pm = {
            "open_questions": [
                {"question": "数据库表结构", "recommendation": "x", "assumed_answer": "y",
                 "severity": "low"},
            ],
            "unknowns": ["数据库表结构", "交易时间要不要记录"],
            "clarifying_questions": [],
        }
        runstore.save_artifact(Path(root) / run_id, "pm", raw_pm, note="pre-normalize-raw")
        code, raw_saved = call(
            port, f"/api/runs/{run_id}/pm-decisions",
            {"decisions": [{"kind": "pm_question", "ref": "交易时间要不要记录",
                            "decision": "需要"}]},
            "POST",
        )
        check(code == 200 and raw_saved.get("merged") == 1,
              "unknowns 条目裁决并回（merged=1）", f"HTTP {code} {raw_saved.get('merged')}")
        raw_detail = call(port, f"/api/runs/{run_id}")[1]
        raw_art = (next((s for s in raw_detail["stages"] if s["stage"] == "pm"), None) or {}
                   ).get("artifact") or {}
        raw_titles = [str(q.get("question")) for q in (raw_art.get("open_questions") or [])
                      if isinstance(q, dict)]
        raw_unknowns = [str(x) for x in (raw_art.get("unknowns") or [])]
        check("数据库表结构" not in raw_titles and "数据库表结构" not in raw_unknowns,
              "保存裁决时先归一：技术问题没有被重新固化进快照",
              f"{raw_titles} / {raw_unknowns}")
        check(any("交易时间要不要记录：需要" == str(f) for f in (raw_art.get("confirmed_facts") or [])),
              "业务裁决折叠进 confirmed_facts（下游拿得到）", str(raw_art.get("confirmed_facts"))[:160])

        print("\n== PRD 查看与编辑")
        code, prd_saved = call(port, f"/api/runs/{run_id}/prd", {"content": "# 人工改写的 PRD\n\n测试内容"}, "POST")
        check(code == 200, "保存人工改写的 prd.md", f"HTTP {code}")
        check("# 人工改写的 PRD" in call(port, f"/api/runs/{run_id}")[1]["prd"], "详情可读回改写后的 PRD")
        check((Path(root) / run_id / "prd.human").exists(), "留下 prd.human 标记，避免被产物覆盖")
        code, bad = call(port, f"/api/runs/{run_id}/prd", {"content": "   "}, "POST")
        check(code == 400, "空 PRD 被拒绝", f"HTTP {code}")
        code, regen = call(port, f"/api/runs/{run_id}/prd", {"regenerate": True}, "POST")
        check(code == 200 and not (Path(root) / run_id / "prd.human").exists(),
              "重新生成会清掉人工改写标记", f"HTTP {code}")

        print("\n== 人工编辑产物并保存")
        pm_stage = next(s for s in detail["stages"] if s["stage"] == "pm")
        edited = dict(pm_stage["artifact"])
        edited["change_request"] = "EDITED-VIA-CONSOLE-导出必须走流式"
        code, saved = call(port, f"/api/runs/{run_id}/artifact", {"stage": "pm", "artifact": edited}, "POST")
        check(code == 200 and not saved.get("warnings"), "保存合法产物无告警", str(saved))

        broken = dict(edited)
        broken.pop("in_scope", None)
        code, saved_bad = call(port, f"/api/runs/{run_id}/artifact", {"stage": "pm", "artifact": broken}, "POST")
        check(code == 200 and saved_bad.get("warnings"), "缺字段保存时返回契约告警", str(saved_bad))
        call(port, f"/api/runs/{run_id}/artifact", {"stage": "pm", "artifact": edited}, "POST")  # 改回合法值

        code, _ = call(port, f"/api/runs/{run_id}/artifact", {"stage": "review", "artifact": {}}, "POST")
        check(code == 409, "未落盘的阶段拒绝保存", f"HTTP {code}")
        code, _ = call(port, f"/api/runs/{run_id}/artifact", {"stage": "nope", "artifact": {}}, "POST")
        check(code == 400, "未知阶段被拒绝", f"HTTP {code}")

        print("\n== PM 强控：未明确项没解决就不放行")
        # 到这里 open_questions 已裁决，但产物里还有「未明确项」（unknowns /
        # clarifying_questions）—— 它们同样"不是陈述"，强控要求解决完才放行。
        code, _ = call(port, f"/api/runs/{run_id}/resume", {"pause_after": []}, "POST")
        check(code == 200, "未解决未明确项时请求继续执行", f"HTTP {code}")
        held = wait_paused(port, run_id, "pm")
        check(
            (held.get("state") or {}).get("paused_after") == "pm",
            "还有未明确项时续跑被原地挡住（不放行，也不推进任何阶段）",
        )
        # 人工把未明确项写成确定结论：等价动作是把这两列清空（写成陈述后它们就不该再有内容）
        pm_now = next((s for s in held["stages"] if s["stage"] == "pm"), None) or {}
        fixed = dict(pm_now.get("artifact") or {})
        cleared = sum(len(fixed.get(f) or []) for f in ("unknowns", "clarifying_questions"))
        for field in ("unknowns", "clarifying_questions"):
            fixed[field] = []
        code, _ = call(port, f"/api/runs/{run_id}/artifact", {"stage": "pm", "artifact": fixed}, "POST")
        check(
            code == 200 and cleared > 0,
            "人工把未明确项写成陈述（清空 unknowns / clarifying_questions）",
            f"{cleared} 条",
        )

        print("\n== 带人工意见继续执行")
        code, resumed = call(
            port,
            f"/api/runs/{run_id}/resume",
            {"feedback": "人工意见：不要动 config.py", "pause_after": []},
            "POST",
        )
        check(code == 200, "POST /resume", f"HTTP {code} {resumed}")
        detail = wait_done_with_gate(port, run_id, {"done"})
        check(detail["summary"].get("verdict") == "pass", "跑完 verdict=pass", str(detail["summary"].get("verdict")))
        # dev 会落 **3** 份快照：两遍模式的两次调用 + 末尾的**累积实现**
        # （见 `_save_impl_snapshot` —— 它才是 dev 阶段的产物，续跑时按它恢复实现）
        check(len(detail["stages"]) == 11,
              "11 个阶段产物（补强/pm/评估/方案/dev两遍+累积实现/test/运行验证/评审/人工审核）",
              str(len(detail["stages"])))
        check(any(s["stage"] == "verify" for s in detail["stages"]), "运行验证产物在页面上可见",
              str([s["stage"] for s in detail["stages"]]))
        check(any(s["stage"] == "intake" for s in detail["stages"]), "补强阶段产物在页面上可见",
              str([s["stage"] for s in detail["stages"]]))
        check("需求补强" in (detail.get("handoff") or ""), "补强的默认假设写进待人工确认清单")
        assess = next(s for s in detail["stages"] if s["stage"] == "architect_assess")
        check("EDITED-VIA-CONSOLE" in assess["request_preview"], "人工编辑进入下游 prompt")
        check("不要动 config.py" in assess["request_preview"], "人工意见注入 architect_assess")
        check(
            (detail["state"].get("human_feedback") or {}).get("architect_assess") == ["人工意见：不要动 config.py"],
            "人工意见留痕在 state.json",
        )

        print("\n== 打回重跑某阶段")
        code, _ = call(
            port, f"/api/runs/{run_id}/resume", {"from": "dev", "feedback": "打回：补测大结果集"}, "POST"
        )
        check(code == 200, "POST /resume --from dev", f"HTTP {code}")
        detail = wait_done_with_gate(port, run_id, {"done"})
        check(detail["superseded"] == 7,
              "dev/test/运行验证/review/人工审核 旧产物归档（dev 两遍+累积实现+闸门占位）",
              str(detail["superseded"]))
        dev = next(s for s in detail["stages"] if s["stage"] == "dev")
        check("打回：补测大结果集" in dev["request_preview"], "打回意见注入 dev")
        check(detail["summary"].get("verdict") == "pass", "重跑后仍为 pass")

        print("\n== 问题记录与原始留存")
        # 全程（暂停→续跑→打回）共 9 次模型调用，trace 是逐次追加的审计流水，不会因重跑被覆盖
        check(detail["trace_calls"] >= 6, "原始调用留存逐次追加", f"trace={detail['trace_calls']}")
        check(bool(((detail.get("env") or {}).get("fingerprint") or {}).get("pipeline_hash")),
              "详情带流水线指纹", str((detail.get("env") or {}).get("fingerprint")))
        check(detail["issue_summary"]["total"] > 0, "详情带问题记录", str(detail["issue_summary"]))
        kinds = {i["kind"] for i in detail["issues"]}
        check("human_edit" in kinds, "人工改产物被记成问题事件", str(sorted(kinds)))
        check("human_rework" in kinds or "human_directive" in kinds, "人工打回/意见被记录", str(sorted(kinds)))
        check(
            any(k["kind"] == "review_required_fix" for k in detail["issue_kinds"]),
            "详情提供人工分类下拉选项",
        )
        row = next(r for r in call(port, "/api/runs")[1]["runs"] if r["run_id"] == run_id)
        check(row["issues"]["total"] > 0, "列表带问题计数", str(row["issues"]))

        code, report = call(port, "/api/report")
        check(code == 200 and "问题总览" in report["markdown"], "问题总览接口", f"HTTP {code}")
        check(report["report"]["runs_analyzed"] >= 1, "总览统计到运行", str(report["report"]["runs_analyzed"]))
        check(report["report"]["totals"]["issues"] > 0, "总览统计到问题", str(report["report"]["totals"]))
        code, kinds_api = call(port, "/api/kinds")
        check(code == 200 and len(kinds_api["kinds"]) > 5, "问题分类接口", f"HTTP {code}")

        print("\n== 人工分类随打回一起记录")
        code, _ = call(
            port,
            f"/api/runs/{run_id}/resume",
            {"from": "test", "feedback": "打回：覆盖大结果集场景", "issue_kind": "test_gap", "pause_after": []},
            "POST",
        )
        check(code == 200, "带分类打回", f"HTTP {code}")
        detail = wait_done_with_gate(port, run_id, {"done"})
        actions = [a for a in (detail["state"].get("human_actions") or []) if a["action"] == "human_rework"]
        check(actions and actions[-1].get("kind") == "test_gap", "人工分类写入 human_actions", str(actions[-1:]))
        tagged = [
            i for i in detail["issues"]
            if i["kind"] == "human_rework" and i.get("evidence", {}).get("declared_kind") == "test_gap"
        ]
        check(bool(tagged), "问题记录里带上人工分类", str(tagged[:1]))

        print("\n== 续跑参数（回流上限可放宽）")
        code, started = call(
            port,
            f"/api/runs/{run_id}/resume",
            {"from": "dev", "pause_after": [], "max_rework": 5},
            "POST",
        )
        argv = started.get("argv") or []
        check(
            code == 200 and "--max-rework" in argv and "5" in argv,
            "续跑可放宽回流上限（页面「回流上限」输入透传进子进程）",
            str(argv[-4:]),
        )
        detail = wait_done_with_gate(port, run_id, {"done"})
        check(
            (detail.get("state") or {}).get("max_rework") == 5,
            "放宽后的上限落进 state（后续按新上限评估）",
            str((detail.get("state") or {}).get("max_rework")),
        )

        print("\n== 拒绝路径")
        code, err = call(port, "/api/runs", {"requirement": "  "}, "POST")
        check(code == 400, "空需求被拒绝", f"HTTP {code} {err}")
        code, err = call(port, "/api/runs/../etc", None)
        check(code in (400, 404), "非法 run_id 被拒绝", f"HTTP {code}")
        code, err = call(port, "/api/runs/20990101-000000")
        check(code == 404, "不存在的运行返回 404", f"HTTP {code}")
        code, err = call(port, f"/api/runs/{run_id}/resume", {"from": "nope"}, "POST")
        check(code == 400, "未知阶段打回被拒绝", f"HTTP {code}")
        code, err = call(port, f"/api/runs/{run_id}/stop", {}, "POST")
        check(code == 200, "stop 幂等（未运行时直接返回）", f"HTTP {code}")

        print("\n== 运行列表维护（删除历史测试数据）")
        # 造两份"历史运行"目录 + 对应的 .inbox 伴随文件（需求原文 / 项目类型）
        for name in ("smoke-del-a", "smoke-del-b"):
            (root / name).mkdir()
            (root / name / "state.json").write_text('{"run_id":"%s"}' % name, encoding="utf-8")
        inbox = root / ".inbox"
        inbox.mkdir(exist_ok=True)
        (inbox / "smoke-del-a.md").write_text("需求原文", encoding="utf-8")
        (inbox / "smoke-del-a.ptype").write_text("secondary", encoding="utf-8")

        code, res = call(port, "/api/runs/smoke-del-a", None, "DELETE")
        check(code == 200 and res.get("ok"), "DELETE 单个运行", f"HTTP {code} {res}")
        check(not (root / "smoke-del-a").exists(), "运行目录已删除")
        check(
            not (inbox / "smoke-del-a.md").exists() and not (inbox / "smoke-del-a.ptype").exists(),
            "同名 .inbox 需求/项目类型文件一并清理（不留孤儿）",
        )
        code, _ = call(port, "/api/runs/smoke-del-a", None, "DELETE")
        check(code == 404, "重复删除返回 404", f"HTTP {code}")
        code, _ = call(port, "/api/runs/../etc", None, "DELETE")
        check(code in (400, 404), "非法 run_id 删除被拒绝", f"HTTP {code}")

        # 运行中的运行不允许删除：子进程还在往目录里写，删了会留下半截产物
        class _FakeProc:
            def poll(self): return None  # 永远"运行中"

        (root / "smoke-del-running").mkdir(exist_ok=True)
        server._JOBS["smoke-del-running"] = {
            "proc": _FakeProc(), "log": root / "smoke-del-running" / "console.log",
            "started": time.time(), "argv": [], "running": True,
        }
        code, err = call(port, "/api/runs/smoke-del-running", None, "DELETE")
        check(code == 409, "运行中的运行拒绝删除", f"HTTP {code} {err}")
        check((root / "smoke-del-running").exists(), "拒绝删除时目录仍在（没有被误删）")
        server._JOBS.pop("smoke-del-running", None)
        shutil.rmtree(root / "smoke-del-running", ignore_errors=True)

        # 批量删除：存在的删掉、不存在的单独报失败，互不影响
        code, res = call(port, "/api/runs/delete", {"run_ids": ["smoke-del-b", "smoke-del-ghost"]}, "POST")
        check(code == 200 and res.get("removed") == ["smoke-del-b"], "批量删除存在的运行", str(res))
        check(
            any(f["run_id"] == "smoke-del-ghost" for f in (res.get("failed") or [])),
            "不存在的运行单独报失败（不影响其余删除）", str(res.get("failed")),
        )
        check(not (root / "smoke-del-b").exists(), "批量删除的目录已消失")
        code, _ = call(port, "/api/runs/delete", {"run_ids": []}, "POST")
        check(code == 400, "空 run_ids 被拒绝", f"HTTP {code}")

        # 启动即失败的运行（只有 console.log，既没 state.json 也没 summary.json）：
        # 必须仍出现在列表里 —— 否则人工看不到失败原因，也没法从页面删掉它。
        dead = root / "smoke-dead"
        dead.mkdir()
        (dead / runstore.LOG_NAME).write_text("--pause-after 中含未知阶段: ['intake']\n", encoding="utf-8")
        code, rows = call(port, "/api/runs")
        dead_row = next((r for r in rows["runs"] if r["run_id"] == "smoke-dead"), None)
        check(dead_row is not None, "启动即失败的运行仍出现在列表（不会隐身）",
              str([r["run_id"] for r in rows["runs"]]))
        check(bool(dead_row) and dead_row["status"] == "failed", "列表标记为启动失败",
              str(dead_row and dead_row["status"]))
        # 被入口总闸拆成作业的运行同样只有 console.log，但多一份 gateway.json 指针：
        # 它**根本不会有** state.json（产物在 runs/_jobs/<job_id>/），必须与「启动失败」分开，
        # 否则页面会把一个跑得好好的大运行说成失败（真机 20260926-154413 就是这样）。
        split = root / "smoke-split"
        split.mkdir()
        (split / runstore.LOG_NAME).write_text(
            "== 规模路由：large（gateway）→ 拆分为 5 个模块\n", encoding="utf-8"
        )
        (split / runstore.GATEWAY_LINK_NAME).write_text(
            json.dumps({
                "job_id": "job-smoke-1",
                "scale": "large",
                "reasons": ["拆出 5 个模块"],
                "status": "running",
                "modules": 5,
                "job_dir": str(root / "_jobs" / "job-smoke-1"),
            }),
            encoding="utf-8",
        )
        code, rows = call(port, "/api/runs")
        split_row = next((r for r in rows["runs"] if r["run_id"] == "smoke-split"), None)
        check(bool(split_row) and split_row["status"] == "job",
              "拆成作业的运行标为 job（不再误报「启动失败」）",
              str(split_row and split_row["status"]))
        code, split_det = call(port, "/api/runs/smoke-split")
        check(code == 200 and (split_det.get("gateway") or {}).get("job_id") == "job-smoke-1",
              "详情带上作业指针（页面据此给正解，而不是「不能续跑」）",
              str(split_det.get("gateway")))
        # 入口总闸的「正在判定」痕迹与日志来源：前者让 GA 期间不再是一条空运行，
        # 后者让模块运行（没有自己的 console.log）能回退显示作业日志。
        check("ga" in split_det and "log_source" in split_det,
              "详情带入口总闸痕迹字段与日志来源字段",
              f"ga={split_det.get('ga')!r} log_source={split_det.get('log_source')!r}")
        shutil.rmtree(split, ignore_errors=True)

        # 页面读的是 run_detail 的**白名单 state 视图**（不是整份快照）：产物层的键必须被摊平
        # 上来，否则会出现「流水线写了、日志也打了、页面一片干净」（真机校准踩到）。
        ruled = root / "smoke-rules"
        ruled.mkdir()
        runstore.write_json(ruled / runstore.STATE_NAME, {
            "status": "done",
            "cursor": "done",
            "artifacts": {
                "rule_findings": [{"rule": "secret_in_code", "severity": "blocker",
                                   "title": "硬编码密钥", "path": "config.py", "line": 1}],
                "refuted_blockers": ["旧的语法错阻断项"],
                "verify_report": {"verdict": "pass", "unverified": ["覆盖率未测量"]},
            },
        })
        code, ruled_det = call(port, "/api/runs/smoke-rules")
        ruled_state = (ruled_det or {}).get("state") or {}
        check(
            code == 200 and bool(ruled_state.get("rule_findings"))
            and bool(ruled_state.get("refuted_blockers")),
            "详情把产物层的红线/被证伪项摊平给页面（防「页面一片干净」）",
            f"rule_findings={len(ruled_state.get('rule_findings') or [])} "
            f"refuted={len(ruled_state.get('refuted_blockers') or [])}",
        )
        shutil.rmtree(ruled, ignore_errors=True)

        # 规则库清单接口：它是「补丁能不能落盘」的判负来源，必须能被看到与审阅
        # （以前 catalog() 写好了却没人调用 —— 哪些红线在管你只能翻 json）。
        code, rule_payload = call(port, "/api/rules")
        rules_rows = rule_payload.get("rules") if isinstance(rule_payload, dict) else None
        check(code == 200 and len(rules_rows or []) >= 12,
              "规则库清单接口可用（配置页据此渲染）", str(len(rules_rows or [])))
        check(
            all(str(r.get("negative") or "").strip() for r in (rules_rows or [])),
            "清单里每条规则都带反例判据（可证伪才允许判负）",
        )
        check(
            any(r.get("severity") == "blocker" for r in (rules_rows or [])),
            "清单能区分阻断级与提示级",
        )
        # 无关空目录不能被当成运行列进来
        (root / "smoke-junk").mkdir()
        code, rows2 = call(port, "/api/runs")
        check(not any(r["run_id"] == "smoke-junk" for r in rows2["runs"]), "无关空目录不被当成运行")
        # 可见即可删：从页面删掉这个失败的运行
        code, res = call(port, "/api/runs/smoke-dead", None, "DELETE")
        check(code == 200 and not dead.exists(), "失败的运行可以从页面删除", f"HTTP {code}")
        shutil.rmtree(root / "smoke-junk", ignore_errors=True)

        code, rows = call(port, "/api/runs")
        left = {r["run_id"] for r in rows["runs"]}
        check(not ({"smoke-del-a", "smoke-del-b"} & left), "列表里已看不到被删的运行", str(sorted(left)))

        print("\n== 流定义与检查点接口")
        code, fl = call(port, "/api/flow")
        check(code == 200 and fl.get("ok") is True, "GET /api/flow 一致性校验通过",
              f"HTTP {code} {fl.get('problems')}")
        check(str(fl.get("mermaid", "")).startswith("flowchart TD"), "流定义返回 Mermaid 拓扑")
        check(
            fl.get("pausable_nodes")
            == ["intake", "pm", "architect_assess", "architect_plan", "dev", "test", "verify", "review"],
            "可暂停阶段由流定义给出、且按执行顺序（human_review 不在内）",
            str(fl.get("pausable_nodes")),
        )
        check(
            any(g["stage"] == "human_review" and g["kind"] == "mandatory" for g in (fl.get("gates") or [])),
            "闸门声明带 kind（human_review=mandatory）", str(fl.get("gates")),
        )

        code, ck = call(port, f"/api/runs/{run_id}/checkpoints")
        check(code == 200 and len(ck.get("checkpoints") or []) >= 5, "GET 检查点列表", f"HTTP {code}")
        ck_list = ck.get("checkpoints") or []
        check(
            all(("state_key" in c and "superseded" in c and "seq" in c) for c in ck_list),
            "检查点带 seq / state_key / superseded 标记",
        )
        detail = call(port, f"/api/runs/{run_id}")[1]
        check(len(detail.get("checkpoints") or []) == len(ck_list), "详情接口与检查点列表一致")

        # 校验路径
        code, err = call(port, f"/api/runs/{run_id}/replay", {}, "POST")
        check(code == 400, "replay 缺 seq 被拒绝", f"HTTP {code}")
        code, err = call(port, f"/api/runs/{run_id}/replay", {"seq": "5"}, "POST")
        check(code == 400, "replay 非整数 seq 被拒绝", f"HTTP {code}")
        code, err = call(port, f"/api/runs/{run_id}/replay", {"seq": 999999}, "POST")
        check(code == 404, "replay 未知检查点被拒绝", f"HTTP {code}")

        # 真回放一次：回到最后一个在存 dev 检查点（其后 test/review/人工审核都被作废）
        dev_seqs = [c["seq"] for c in ck_list if c["stage"] == "dev" and not c["superseded"]]
        check(bool(dev_seqs), "在存检查点里含 dev（回放目标）", str(dev_seqs))
        target_seq = max(dev_seqs)
        before_superseded = detail.get("superseded") or 0
        code, started = call(port, f"/api/runs/{run_id}/replay", {"seq": target_seq}, "POST")
        check(
            code == 200 and started.get("seq") == target_seq,
            "POST 回放到指定检查点", f"HTTP {code} {started}",
        )
        detail = wait_done_with_gate(port, run_id, {"done"})
        check(
            (detail.get("superseded") or 0) > before_superseded,
            "回放把目标检查点之后的产物归档到 superseded/",
            f"{before_superseded} -> {detail.get('superseded')}",
        )
        actions = [a["action"] for a in (detail["state"].get("human_actions") or [])]
        check("checkpoint_restore" in actions, "回放动作留痕到 human_actions",
              str([a for a in actions if a == "checkpoint_restore"]))
        check(detail["summary"].get("verdict") == "pass", "回放后续跑跑到 pass",
              str(detail["summary"].get("verdict")))

        print("\n== 入口总闸与作业接口")
        check(
            fl.get("pre_nodes") == ["global_architecture_analysis"],
            "流定义视图带前置节点（入口总闸）",
            str(fl.get("pre_nodes")),
        )
        check(
            "global_architecture_analysis" in (fl.get("model_nodes") or []),
            "入口总闸的模型登记进了模型阶段表（漏登在启动期就会报错）",
            str(fl.get("model_nodes")),
        )
        check(
            ((fl.get("pre_edges") or {}).get("global_architecture_analysis") or {}).get("small") == "intake",
            "入口总闸的 small 分支指向 intake（直通原有流水线）",
            str(fl.get("pre_edges")),
        )
        check(
            (fl.get("gateway") or {}).get("modes") == ["auto", "always", "off"],
            "流定义视图带入口总闸模式",
            str((fl.get("gateway") or {}).get("modes")),
        )

        code, jobs_res = call(port, "/api/jobs")
        check(code == 200 and "jobs" in jobs_res, "GET /api/jobs", f"HTTP {code}")
        check(
            "mode" in jobs_res and "forbidden" in jobs_res,
            "作业列表带当前模式与禁区（页面据此显示提示）",
            str(sorted(jobs_res)),
        )
        code, _ = call(port, "/api/jobs/bad%20id")
        check(code == 400, "非法 job_id 被拒绝", f"HTTP {code}")
        code, _ = call(port, "/api/jobs/nosuchjob")
        check(code == 404, "未知作业返回 404", f"HTTP {code}")
        code, _ = call(port, "/api/jobs/nosuchjob/resume", {}, "POST")
        check(code == 404, "续跑未知作业返回 404", f"HTTP {code}")

        # 续跑作业的参数透传：argv 是唯一可信观察点。
        # 此前这条链**无条件**发 --no-pause，且从不带 --project-type —— 于是作业原本的
        # 闸门被清空、新建项目的模块被当成二次开发重跑（真机 job-20260926-154657）。
        job_id = "job-smoke-resume"
        job_root = root / "_jobs" / job_id
        job_root.mkdir(parents=True, exist_ok=True)
        (job_root / "job.json").write_text(
            json.dumps({
                "version": 1, "job_id": job_id, "modules": [], "execution_order": [],
                "project_type": "new", "review_every": 3, "max_rework": 5,
                "pause_after": ["pm"], "repo": None, "forbidden": [],
            }),
            encoding="utf-8",
        )

        def _wait_job_child(rid: str) -> None:
            for _ in range(80):
                j = server._job(rid)
                if not j or not j.get("running"):
                    server._JOBS.pop(rid, None)
                    return
                time.sleep(0.25)

        code, started = call(port, f"/api/jobs/{job_id}/resume", {}, "POST")
        argv = (started or {}).get("argv") or []
        _wait_job_child(job_id)
        check(
            code == 200 and "--resume-job" in argv and job_id in argv
            and "--project-type" not in argv and "--no-pause" not in argv,
            "续跑作业：没显式给参数时不冒充（不再无条件 --no-pause，也不覆盖项目类型）",
            str(argv),
        )
        code, started2 = call(
            port,
            f"/api/jobs/{job_id}/resume",
            {"project_type": "new", "review_every": 3, "max_rework": 7, "pause_after": ["pm"]},
            "POST",
        )
        argv2 = (started2 or {}).get("argv") or []
        _wait_job_child(job_id)
        check(
            code == 200 and "--project-type" in argv2 and "new" in argv2
            and "--review-every" in argv2 and "--max-rework" in argv2
            and "--pause-after" in argv2 and "--no-pause" not in argv2,
            "续跑作业：显式参数照样下传（含闸门，而不是一律 --no-pause）",
            str(argv2),
        )
        code, started3 = call(port, f"/api/jobs/{job_id}/resume", {"pause_after": []}, "POST")
        argv3 = (started3 or {}).get("argv") or []
        _wait_job_child(job_id)
        check(code == 200 and "--no-pause" in argv3, "显式给空闸门＝跑到底（--no-pause）", str(argv3))

        # 作业三阶段：相位视图 + PM 统一人工环节的闸门 + 统一验收接口
        pm_job = "job-smoke-pm"
        pm_root = root / "_jobs" / pm_job
        (pm_root / "modules").mkdir(parents=True, exist_ok=True)
        (pm_root / "modules" / "01-M-01.md").write_text("需求：模块一", encoding="utf-8")
        (pm_root / "job.json").write_text(
            json.dumps({
                "version": 1, "job_id": pm_job, "phase": "pm",
                "modules": [{"module_id": "M-01", "status": "pm_paused", "run_dir": None}],
                "execution_order": ["M-01"], "repo": None, "forbidden": [],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        code, err = call(port, f"/api/jobs/{pm_job}/resume", {}, "POST")
        check(
            code == 409 and "PM 待确认项" in str(err),
            "PM 没确认完就续跑 → 409 并说明原因（统一人工环节的闸门）",
            f"HTTP {code} {err}",
        )
        code, view = call(port, f"/api/jobs/{pm_job}")
        check(
            view.get("phase") == "pm" and "PM 前置阶段" in str(view.get("phase_cn"))
            and "M-01" in (view.get("pm_blockers") or {}),
            "作业视图带相位与 PM 待确认聚合（页面据此显示「待你确认 PM」）",
            f"phase={view.get('phase')} blockers={sorted((view.get('pm_blockers') or {}))}",
        )
        code, err = call(port, f"/api/jobs/{pm_job}/review", {"verdict": "approve"}, "POST")
        check(code == 409, "模块还没跑完就提交统一验收 → 409", f"HTTP {code} {err}")
        code, err = call(port, f"/api/jobs/{pm_job}/review", {"verdict": "bogus"}, "POST")
        check(code == 400, "统一验收 verdict 非法被拒绝", f"HTTP {code}")
        (pm_root / "human_review.json").write_text(
            json.dumps({"verdict": "", "modules": []}, ensure_ascii=False), encoding="utf-8"
        )
        code, err = call(port, f"/api/jobs/{pm_job}/review", {"verdict": "reject"}, "POST")
        check(code == 400, "统一验收打回必须写明问题（否则各模块不知道改什么）", f"HTTP {code}")
        code, res = call(
            port, f"/api/jobs/{pm_job}/review", {"verdict": "approve", "notes": "整体通过"}, "POST"
        )
        check(
            code == 200 and res.get("phase") == "done" and res.get("status") == "done",
            "统一验收通过 → 作业 phase=done / status=done",
            str(res),
        )

        code, err = call(port, "/api/runs", {"requirement": "x", "gateway": "bogus"}, "POST")
        check(code == 400, "未知 gateway 模式被拒绝", f"HTTP {code}")
        code, err = call(port, "/api/runs", {"requirement": "x", "scale": "huge"}, "POST")
        check(code == 400, "未知 scale 被拒绝", f"HTTP {code}")

        # argv 是「参数真的透传给了子进程」的唯一可信观察点
        code, started = call(
            port,
            "/api/runs",
            {
                "requirement": "给客户列表加导出按钮（用于验证入口总闸参数透传）",
                "mock": True,
                "gateway": "off",
                "scale": "small",
                "forbidden": ["core/x.py"],
                "pause_after": [],
            },
            "POST",
        )
        argv = started.get("argv") or []
        check(code == 200 and "--gateway" in argv, "启动参数透传入口总闸模式", str(argv))
        check("off" in argv and "small" in argv, "模式与强制规模都写进 argv", str(argv))
        check(
            "--forbidden" in argv and "core/x.py" in argv,
            "禁区清单写进 argv（成为全局架构的输入）",
            str(argv),
        )
        gw_run = started.get("run_id")
        call(port, f"/api/runs/{gw_run}/stop", {}, "POST")
        # 终止是异步的（Windows 上 terminate 之后 poll 不一定立刻可见），
        # 不等它真的不在跑就删会被 409 挡下 —— 这不是被测行为，别让它污染断言。
        deadline = time.time() + 15
        while time.time() < deadline:
            _, probe = call(port, f"/api/runs/{gw_run}")
            if isinstance(probe, dict) and not probe.get("running"):
                break
            time.sleep(0.3)
        code, _ = call(port, f"/api/runs/{gw_run}", None, "DELETE")
        check(code == 200, "旁路验证用的运行已清理", f"HTTP {code}")

        print("\n== 最终输出运行验证")
        vfy = (detail.get("artifacts") or {}).get("verify")
        check(vfy is not None, "运行验证产物在详情里可见")
        check(
            bool(vfy) and vfy.get("verdict") == "skipped" and vfy.get("mode") == "mock",
            "mock 运行只计划命令、不真跑（离线冒烟不会乱执行东西）",
            str(vfy and vfy.get("summary")),
        )
        check(
            all(c.get("status") == "skipped" for c in (vfy or {}).get("commands") or []),
            "mock 下每条命令都标为未执行",
        )
        # 本次冒烟建运行时没给 repo：补丁无法核对/物化，运行验证应**如实说明并跳过**，
        # 而不是空转出一堆「未能套用」。（给了 repo 的真实路径由 smoke_mock 覆盖）
        check(
            any("没有提供仓库路径" in str(n) for n in (vfy or {}).get("notes") or []),
            "没有仓库路径时如实说明无法验证",
            str((vfy or {}).get("notes")),
        )

        check_control_plane(port, run_id)

        print("\n== 阶段日志切片接口（流程图节点用）")
        code, lg = call(port, f"/api/runs/{run_id}/log?stage=intake&lines=50")
        check(
            code == 200 and lg.get("has_markers") is True and lg.get("occurrences") >= 1,
            "GET /log?stage=intake", f"HTTP {code} {lg}",
        )
        check("== STAGE intake ==" in (lg.get("text") or ""), "切片带阶段边界标题")
        code, lgdev = call(port, f"/api/runs/{run_id}/log?stage=dev&lines=300")
        check(lgdev.get("occurrences") >= 2, "dev 跑过多轮，切片合并全部轮次",
              str(lgdev.get("occurrences")))
        check("== STAGE intake ==" not in (lgdev.get("text") or ""), "阶段切片互不串台")
        code, err = call(port, f"/api/runs/{run_id}/log?stage=bogus")
        check(code == 400, "未知阶段被拒绝", f"HTTP {code}")
        code, raw = call(port, f"/api/runs/{run_id}/log?lines=500")
        check(
            code == 200 and isinstance(raw, str) and "== STAGE" in raw,
            "不带 stage 时仍返回整段日志文本（兼容旧用法）",
            f"HTTP {code} {type(raw).__name__} len={len(raw) if isinstance(raw, str) else '-'}",
        )

        # 端到端收尾：把本次冒烟的主运行也删掉（真实产物目录，含 8 个阶段快照与补丁）
        code, res = call(port, f"/api/runs/{run_id}", None, "DELETE")
        check(code == 200, "删除本次冒烟的主运行", f"HTTP {code} {res}")
        code, _ = call(port, f"/api/runs/{run_id}")
        check(code == 404, "主运行删除后详情返回 404", f"HTTP {code}")
    except Exception as exc:  # noqa: BLE001
        check(False, "断言过程抛出异常", f"{type(exc).__name__}: {exc}")
        for log in sorted(root.glob("*/console.log")):
            print(f"\n--- 子进程日志 {log.parent.name} (尾部 40 行) ---")
            print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]))
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.shutdown_jobs()  # 别把子进程留在后台（真实模型运行会占显存）
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
