"""操作页面（pipeline.server）的离线冒烟：起一个临时端口的服务，用 --mock 驱动完整人机闭环。

覆盖：新建运行 -> 人工闸门暂停 -> 查看产物 -> 保存人工编辑 -> 继续执行 -> 跑完；
以及非法输入（未知阶段 / 非法 run_id）的拒绝路径。不加载任何模型。

用法: python tools/smoke_console.py
"""
from __future__ import annotations

import json
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

from pipeline import server, runstore  # noqa: E402

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
    raise AssertionError(f"{run_id} 未在 {timeout}s 内进入 {want}；最后状态={last.get('state')}")


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
                call(port, f"/api/runs/{run_id}/resume", {}, "POST")
        time.sleep(0.35)
    raise AssertionError(f"{run_id} 未在 {timeout}s 内进入 {want}；最后状态={last.get('state')}")


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
        for x in art0.get("missing_elements") or []:
            if isinstance(x, dict) and x.get("element"):
                payload.append({"kind": "missing_element", "ref": str(x["element"]),
                                "decision": "裁决-" + str(x["element"])})
        for x in art0.get("clarifying_questions") or []:
            if isinstance(x, dict) and x.get("question"):
                payload.append({"kind": "clarifying_question", "ref": str(x["question"]),
                                "decision": "裁决-" + str(x["question"])})
        real = len(payload)
        payload.append({"kind": "clarifying_question", "ref": "__不存在的条目__", "decision": "未匹配也应保留"})
        payload.append({"kind": "clarifying_question", "ref": "（这条没填裁决）", "decision": "   "})

        code, saved_dec = call(port, f"/api/runs/{run_id}/intake-decisions", {"decisions": payload}, "POST")
        check(code == 200 and len(saved_dec["decisions"]) == real + 1,
              "保存裁决并丢弃空白条目（未匹配的也保留）", f"HTTP {code} real={real}")
        check(saved_dec.get("merged") == real, "裁决按 ref 并回补强产物条目", str(saved_dec.get("merged")))

        detail1 = call(port, f"/api/runs/{run_id}")[1]
        snap1 = next((s for s in detail1["stages"] if s["stage"] == "intake"), None)
        art1 = (snap1 or {}).get("artifact") or {}
        decided = [x for x in (art1.get("missing_elements") or []) + (art1.get("clarifying_questions") or [])
                   if isinstance(x, dict) and x.get("final_decision")]
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
        check(len(detail["stages"]) == 10,
              "10 个阶段产物（补强/pm/评估/方案/dev两遍/test/运行验证/评审/人工审核）",
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
        check(detail["superseded"] == 6,
              "dev/test/运行验证/review/人工审核 旧产物归档（dev 两遍+闸门占位）", str(detail["superseded"]))
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
