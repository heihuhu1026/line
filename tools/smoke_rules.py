"""工程红线规则库（pipeline/rules.py + rules.json）的离线冒烟。

**为什么规则库要有自己的冒烟**：它现在是"落盘前拦截 + 评审机械证据"的判负来源，
一条规则写错有两个方向都致命的后果 —— 漏判（等于没这个检查）与误判（把合法写法判成
阻断 → 触发一整轮无谓返工，本项目反复踩过）。两者都只有**成对的正反用例**能挡住，
所以这里每个规则都同时验「该命中的命中」与「该放过的放过」。

覆盖：① 每条规则的正向命中；② 反例/豁免真的生效（os.environ、占位符、测试目录、注释）；
③ 触发条件（栈无关：tkinter 项目的补丁不该命中 CORS/迁移规则）；④ 规则库自身的契约
（每条必须有 evidence + negative，layer=B 必须有 applies_when）；⑤ 引擎健壮性
（规则文件写坏 / 正则非法 → 不抛异常、留痕、流水线照跑）。

用法: python tools/smoke_rules.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import rules  # noqa: E402

failures: list[str] = []
checks = 0


def check(cond: bool, label: str, detail: str = "") -> bool:
    global checks
    checks += 1
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    if not cond:
        failures.append(f"{label} {detail}".strip())
    return bool(cond)


def edit(path: str, patch: str, *, change: str = "add") -> dict:
    return {"path": path, "change_type": change, "patch_mode": "new_file", "patch": patch}


#: (说明, 补丁, 期望命中, 期望**不**命中)
CASES: list[tuple[str, dict, tuple[str, ...], tuple[str, ...]]] = [
    # --- 该命中的 ---
    (
        "硬编码密钥",
        edit("config.py", 'API_KEY = "sk-live-abcdef123456"\n'),
        ("secret_in_code",), ("debug_residue",),
    ),
    (
        "日志里打出口令",
        edit("db.py", 'logger.info("token=%s", token)\n'),
        ("log_sensitive",), (),
    ),
    (
        "直接打印敏感变量（无引号）",
        edit("db.py", "print(password)\n"),
        ("log_sensitive",), (),
    ),
    (
        "调试残留 print（非入口模块）",
        edit("utils.py", 'print("debug", x)\n'),
        ("debug_residue",), (),
    ),
    (
        "泛型异常",
        edit("svc.py", 'raise Exception("boom")\n'),
        ("generic_error",), (),
    ),
    (
        "硬编码本机地址",
        edit("client.py", 'BASE = "http://localhost:8000/api"\n'),
        ("hardcoded_localhost",), (),
    ),
    (
        "浮动依赖",
        edit("requirements.txt", "flask==latest\n"),
        ("floating_dependency",), ("loose_dependency",),
    ),
    (
        "只有下界的依赖",
        edit("requirements.txt", "requests>=2.31.0\n"),
        ("loose_dependency",), ("floating_dependency",),
    ),
    (
        "单步破坏性迁移",
        edit("migrations/0002_x.py", "def upgrade():\n    op.drop_column('users', 'nick')\n"),
        ("destructive_migration",), (),
    ),
    (
        "迁移没有回滚路径",
        edit("migrations/0003_y.py", "def upgrade():\n    op.add_column('users', sa.Column('x'))\n"),
        ("migration_irreversible",), ("destructive_migration",),
    ),
    (
        "建索引未加 CONCURRENTLY",
        edit("migrations/0004_z.sql", "CREATE INDEX idx_users ON users (email);\n"),
        ("blocking_create_index",), (),
    ),
    (
        "令牌进 localStorage（前端栈）",
        edit("src/auth.ts", 'localStorage.setItem("auth_token", jwt);\n'),
        ("token_in_storage",), (),
    ),
    (
        "CORS 通配",
        edit("app/server.py", 'app.add_middleware(CORSMiddleware, allow_origins=["*"])\n'),
        ("cors_wildcard",), (),
    ),
    (
        "SQL f-string 插值",
        edit("repo.py", 'cursor.execute(f"SELECT * FROM users WHERE id = {uid}")\n'),
        ("sql_string_concat", "select_star"), (),
    ),
    # --- 该放过的 ---
    (
        "口令来自环境变量",
        edit("db.py", 'password = os.environ["PGPASSWORD"]\n'),
        (), ("secret_in_code",),
    ),
    (
        "占位符密钥",
        edit("config.py", 'API_KEY = "<YOUR_API_KEY>"\nSECRET = "changeme"\n'),
        (), ("secret_in_code",),
    ),
    (
        "测试夹具里的假口令（形如 test_* 的假值）",
        edit("tests/conftest.py", 'password = "test_password_123"\n'),
        (), ("secret_in_code",),
    ),
    (
        "日志只是提到 token 这个词（不是打印它的值）",
        edit("db.py", 'logger.info("token set")\n'),
        (), ("log_sensitive",),
    ),
    (
        "测试目录里的 print 不算调试残留",
        edit("tests/test_a.py", 'print(result)\n'),
        (), ("debug_residue",),
    ),
    (
        "入口文件里的 print 是用户可见输出（不算调试残留）",
        edit("main.py", 'print("摄氏 100 = 华氏 212")\n'),
        (), ("debug_residue",),
    ),
    (
        "JS 入口 index.js 里的 console.log 同样豁免",
        edit("index.js", 'console.log("usage: node index.js <value> <unit>")\n'),
        (), ("debug_residue",),
    ),
    (
        "注释里的写法不算违规",
        edit("migrations/0005_a.py", "# 下次可以考虑 op.drop_column('users','x')\ndef upgrade():\n    pass\n"),
        ("migration_irreversible",), ("destructive_migration",),
    ),
    (
        "参数化 SQL 不该被判注入",
        edit("repo.py", 'cursor.execute("SELECT id FROM users WHERE name = %s", (name,))\n'),
        (), ("sql_string_concat",),
    ),
    (
        "栈无关：Python 项目不该命中前端/迁移规则",
        edit("game_logic.py", "class Snake:\n    def move(self):\n        self.x += 1\n"),
        (), ("token_in_storage", "cors_wildcard", "destructive_migration", "floating_dependency"),
    ),
    (
        "栈无关：纯 tkinter 项目不该因为 'select' 字样命中 SQL 规则",
        edit("view.py", 'def select_row(self):\n    return self.rows[0]\n'),
        (), ("select_star",),
    ),
]


def wiring_checks() -> None:
    """接线断言：规则库接进流水线后，**判负 / 证伪 / 账本 / 人审清单 / 验收**五条链路真的通了。

    为什么必须单独验：上面的用例只证明"规则能判"，证明不了"判了之后会被拦下来、会被记进
    账本、会出现在人审清单里"。而"机制写好了却没接上"在真机上极难发现 —— 日志一片安静，
    看起来正像"没问题"（本项目吃过这个亏：给 agent 看的清单里从没提"哪些没验"）。
    """
    print("== 接线：判负 / 证伪 / 账本 / 人审清单 / 验收")
    from pipeline import gateway, issues, runstore  # noqa: PLC0415
    from pipeline.ollama_client import MockClient  # noqa: PLC0415
    from pipeline.orchestrator import Orchestrator  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        run_dir = root / "run-wire"
        run_dir.mkdir(parents=True)
        orch = Orchestrator(client=MockClient(), repo=None, runs_dir=root, log=lambda _m: None)
        orch.run_id = "run-wire"
        orch.run_dir = run_dir
        bad_impl = {"summary": "s", "edits": [edit("config.py", 'API_KEY = "sk-live-abcdef123456"\n')]}

        # ① 落盘前的红线拦截 → 机械阻断项（评审给 pass 也会被改判 rework）
        orch.state = {"implementation": bad_impl}
        found = orch._rule_findings(bad_impl)
        check(bool(found), "dev 阶段扫出红线finding", f"{len(found)} 条")
        rule_fixes = orch._rule_blockers()
        check(bool(rule_fixes) and "secret_in_code" in rule_fixes[0],
              "红线判负进入机械阻断项", (rule_fixes[0][:60] if rule_fixes else ""))
        orch.state.update({
            "patch_audit": {"source_available": False, "edits": []},
            "implementation_audit": {"empty_implementation": False, "vanished_symbols": [], "stack_problems": []},
        })
        mech = orch._mechanical_blockers()
        check(any("secret_in_code" in x for x in mech),
              "机械阻断项总表带上红线（评审判 pass 也会被改判）", f"{len(mech)} 项")
        # 红线**不跨轮累积**：真机上第 2 轮代码已改掉，第 1 轮的 3 条仍挂在 state 里，
        # 于是下一轮评审继续拿陈旧红线当返工理由 —— 正是要断的自指循环。
        orch.state = {"implementation": bad_impl}
        orch._rule_findings(bad_impl, reset=True)
        round1 = len(orch.state.get("rule_findings") or [])
        clean_impl = {"summary": "s", "edits": [edit("config.py", "VALUE = 1\n")]}
        orch.state["implementation"] = clean_impl
        orch._rule_findings(clean_impl, reset=True)
        round2 = len(orch.state.get("rule_findings") or [])
        check(round1 > 0 and round2 == 0,
              "红线不跨轮累积（上一轮修好后这一轮必须清空）",
              f"第1轮 {round1} 条 → 第2轮 {round2} 条")

        # ② 证伪剔除：只剔「机械类主张」，且只在本轮机械证据全绿时
        clean = {"verdict": "pass", "problems": [], "commands": [{"status": "ok"}]}
        orch.state = {"verify_report": clean, "patch_audit": {"edits": []}}
        kept, dropped = orch._refute_stale_blockers(["renderer.py 未闭合 f-string 语法错误"])
        check(not kept and len(dropped) == 1,
              "机械证据全绿时，遗留的「语法错/跑不起来」类阻断项被剔除（断自指循环）")
        orch.state = {"verify_report": {"verdict": "fail", "problems": ["X 导不进来"],
                                        "commands": [{"status": "fail"}]}, "patch_audit": {"edits": []}}
        kept, dropped = orch._refute_stale_blockers(["renderer.py 未闭合 f-string 语法错误"])
        check(len(kept) == 1 and not dropped, "机械证据不干净时一律保留（宁可多留一轮，也不删真问题）")
        orch.state = {"verify_report": clean, "patch_audit": {"edits": []}}
        kept, _dropped = orch._refute_stale_blockers(["与既有模块的分层约定不一致"])
        check(len(kept) == 1, "设计/兼容性类主张不参与证伪剔除（不在机械证据射程内）")
        # 关键词表要覆盖**带修饰词的说法**（回放历史运行 20260925-184300 时发现的漏词：
        # 「导致依赖文件无法正常运行」——精确子串 "无法运行" 匹配不到它）
        for text, expect_drop in (
            ("renderer.py 未闭合 f-string 语法错误", True),
            ("模块game_logic缺少核心符号Snake，导致依赖文件无法正常运行", True),
            ("导入 tempconv 失败：ModuleNotFoundError", True),
            ("tempconv.py 的 print 语句应改为结构化日志", False),
            ("与既有模块的分层约定不一致", False),
        ):
            orch.state = {"verify_report": clean, "patch_audit": {"edits": []}}
            _kept, _dropped = orch._refute_stale_blockers([text])
            got = bool(_dropped)
            check(got == expect_drop,
                  f"机械类主张识别：{text[:34]}",
                  f"实际={'剔除' if got else '保留'}，期望={'剔除' if expect_drop else '保留'}")

        # ③ 账本：五类新信号都要入库，且带误判复盘三栏
        ledger_state = {
            "rule_findings": [{
                "rule": "secret_in_code", "severity": "blocker", "title": "硬编码密钥",
                "path": "config.py", "line": 1, "excerpt": 'API_KEY = "…"',
                "message": "代码里出现了看起来是真实值的密钥", "negative": "来自环境变量即为反例",
                "source": "fullstack-dev §2",
            }],
            "refuted_blockers": ["renderer.py 未闭合 f-string"],
            "verify_report": {"unverified": ["覆盖率未测量"]},
            "duplicate_stage_seqs": ["seq 3：a.json、b.json"],
            "rule_load_notes": ["x：缺 negative（反例判据）→ 降级为 warn"],
        }
        got = {i.kind: i for i in issues.collect_issues(ledger_state, "run-wire")}
        # **快照形状**（read_state / state_of 的真实返回：产物在 artifacts 里）也必须读得到 ——
        # 这正是真机校准抓到的那个 bug：同一个键名在两层不同，读错层就**静默为空**
        # （日志打了 5 条红线，账本里 0 条）。这条断言就是为它加的。
        snap_got = {
            i.kind: i
            for i in issues.collect_issues(
                {"status": "running", "cursor": "review", "artifacts": dict(ledger_state)},
                "run-wire",
            )
        }
        missing_in_snap = [
            k for k in ("rule_violation", "refuted_stale_fix", "unverified_claim",
                        "stage_seq_conflict", "rule_load_problem")
            if k not in snap_got
        ]
        check(not missing_in_snap,
              "账本在**快照形状**下也能读到产物层信号（防「读错层⇒静默为空」回归）",
              "、".join(missing_in_snap))
        for kind in ("rule_violation", "refuted_stale_fix", "unverified_claim",
                     "stage_seq_conflict", "rule_load_problem"):
            check(kind in got, f"账本收录 {kind}", "" if kind in got else "未收录")
        row = got.get("rule_violation")
        check(bool(row) and row.severity == "blocker", "红线判负在账本里也是阻断级")
        check(bool(row) and row.misjudged_signal and row.correct_criterion,
              "账本带误判复盘三栏（错判点 / 正确判据）",
              (row.misjudged_signal[:40] if row else ""))

        # ④ 人审清单（handoff）：红线与**未验证项**必须露出来
        orch.state = {"rule_findings": ledger_state["rule_findings"],
                      "verify_report": {"unverified": ["覆盖率未测量：本环境没有接入覆盖率工具"]}}
        orch.status = "done"
        orch._write_handoff({"verdict": "pass"})
        text = (run_dir / runstore.HANDOFF_NAME).read_text(encoding="utf-8")
        check("工程红线" in text and "硬编码密钥" in text, "待人工确认清单有「工程红线」一节")
        check("未验证项" in text and "覆盖率未测量" in text,
              "待人工确认清单**强制披露未验证项**（pass ≠ 该验的都验了）")

        # ⑤ 作业交付就绪度（4 门 × 0–2）：全绿满分，红线阻断扣到禁止放行
        mod = root / "job-wire-M-01"
        mod.mkdir()
        data = {"job_id": "job-wire", "modules": [
            {"module_id": "M-01", "status": "done", "verdict": "pass", "run_dir": str(mod)},
        ]}
        # 写成**快照形状**（产物在 artifacts 里）—— 那才是 `read_state()` 真正给出来的形状。
        # 这里以前写成"产物层摊平"，于是没测到「读错层 ⇒ 静默为空」，只有真机才暴露。
        runstore.write_json(mod / runstore.STATE_NAME, {
            "status": "done",
            "artifacts": {
                "verify_report": {"verdict": "pass", "problems": [], "unverified": ["覆盖率未测量"]},
                "delivery": {"delivered": True, "files": [{"path": "a.py", "written": "a.py"}]},
                "rule_findings": [],
            },
        })
        ready = gateway.job_readiness(root, data)
        check(ready["score"] == ready["max"], "四门全过 → 满分（可放行）",
              f"{ready['score']}/{ready['max']} {ready['decision']}")
        check(any("覆盖率未测量" in str(x) for x in ready["unverified"]),
              "就绪度把未验证项原样带出来")
        runstore.write_json(mod / runstore.STATE_NAME, {
            "status": "done",
            "artifacts": {
                "verify_report": {"verdict": "pass", "problems": []},
                "delivery": {"delivered": True, "files": [{"path": "a.py"}]},
                "rule_findings": [{"rule": "secret_in_code", "severity": "blocker",
                                   "title": "硬编码密钥", "path": "config.py", "line": 1}],
            },
        })
        bad_ready = gateway.job_readiness(root, data)
        check(bad_ready["score"] < ready["score"] and "禁止放行" in bad_ready["decision"],
              "红线阻断 → 就绪度扣到禁止放行", f"{bad_ready['score']} {bad_ready['decision'][:12]}")
        check(any(g["key"] == "redline" and g["score"] == 0 for g in bad_ready["gates"]),
              "扣分能定位到具体哪一门（redline）")

        # ⑦ 交付证据 / 覆盖率 / 负向对照 / 入口总闸痕迹（本轮新增的机制，逐条验）
        print("== 接线：交付证据 / 覆盖率 / 负向对照 / 入口总闸痕迹")
        from pipeline import evidence, gateway as gw_mod, verify as verify_mod  # noqa: PLC0415

        ev = evidence.delivery_evidence(
            {"acceptance_criteria": ["导出按钮能导出当前筛选结果", "空结果集导出时给出提示"]},
            {"cases": [{"id": "C-01", "type": "new", "target": "export_rows",
                        "expected": "导出当前筛选结果的文件"}]},
            {"commands": [{"command": "python -m unittest", "status": "ok"},
                          {"command": "python -m pytest", "status": "unavailable"}],
             "unverified": ["覆盖率未测量"]},
            [{"rule": "secret_in_code", "severity": "blocker", "title": "硬编码密钥",
              "path": "config.py", "line": 3, "negative": "来自环境变量即为反例"}],
        )
        check(ev["criteria_count"] == 2 and ev["covered_count"] == 1,
              "证据表把验收标准与用例对上（启发式匹配）",
              f"{ev['covered_count']}/{ev['criteria_count']}")
        check(len(ev["uncovered"]) == 1 and "空结果集" in ev["uncovered"][0],
              "**没人验**的验收标准被单独列出来（这才是要人拍板的）",
              "、".join(ev["uncovered"])[:40])
        ev_md = evidence.render_markdown(ev)
        check(all(k in ev_md for k in ("验收标准覆盖", "没有成功跑过的命令", "未验证项", "工程红线")),
              "证据表渲染齐四块（覆盖 / 命令 / 未验证 / 红线）")
        check("启发式" in (ev.get("match_note") or ""),
              "证据表标明匹配是启发式（不许被当成「已核对」）")

        cov = verify_mod.coverage_fact([{
            "command": "python -m pytest --cov", "status": "ok",
            "stdout_tail": "Name Stmts Miss Cover\nTOTAL 120 18 85%",
        }])
        check(cov.get("percent") == 85.0, "覆盖率从**真实输出**里摘出来（不是估算）", str(cov.get("percent")))
        check(verify_mod.coverage_fact([{"stdout_tail": "no numbers here"}])["percent"] is None,
              "摘不到覆盖率就返回 None（不编数字）")
        # 门槛判定单独可测：真机上"有数字且低于 80%"这一支要凑齐三件事才出现，离线钉死它
        low = verify_mod.coverage_below_threshold({"percent": 62.5, "command": "pytest --cov"})
        check(bool(low) and "低于门槛" in low, "低于门槛时给出判断（提示级，不判负）", low[:40])
        check(verify_mod.coverage_below_threshold({"percent": 80.0}) == "",
              "刚好达标不算低于门槛")
        check(verify_mod.coverage_below_threshold({"percent": None}) == "",
              "没有数字时不判门槛（那是「未验证」，不是「不达标」）")
        check("低于门槛 90" in verify_mod.coverage_below_threshold({"percent": 70}, minimum=90),
              "门槛值可指定（默认 80）")
        _exec_one = [{"command": "python -m unittest", "status": "ok"}]
        _tst = {"automated_commands": [{"command": "python -m unittest"}]}
        check(
            not any("覆盖率未测量" in c for c in
                    verify_mod.unverified_claims(root, [], _exec_one, _tst, cov)),
            "有实测覆盖率时不再把它列进未验证项（避免自相矛盾）",
        )
        check(
            any("覆盖率未测量" in c for c in
                verify_mod.unverified_claims(root, [], _exec_one, _tst, {"percent": None})),
            "没有覆盖率数字时才说「未测量」",
        )

        nc = root / "nc"
        nc.mkdir()
        (nc / "a.txt").write_text("patched\n", encoding="utf-8")
        nc_repo = root / "nc-repo"
        nc_repo.mkdir()
        (nc_repo / "a.txt").write_text("original\n", encoding="utf-8")
        _real_run = verify_mod.run_command
        verify_mod.run_command = lambda spec, **kw: {"status": "ok", "command": spec.get("command")}
        try:
            ctl = verify_mod.negative_control(
                nc, nc_repo, ["a.txt"], [{"command": "python -c assert x", "status": "ok"}],
                timeout=10, allowed_bins=frozenset({"python"}), deny_patterns=(),
            )
        finally:
            verify_mod.run_command = _real_run
        check(ctl["checked"] == 1 and bool(ctl["no_power"]),
              "负向对照：撤掉改动后断言**仍然通过** ⇒ 判为对本次交付没有判别力")
        check((nc / "a.txt").read_text(encoding="utf-8").strip() == "patched",
              "负向对照跑完必须把沙箱还原（否则污染后续检查）")

        # 入口脚本豁免：**项目自定义**的入口名（真机校准里叫 `tempconv.py`）也必须豁免，
        # 否则 CLI 的正经输出会被当成"调试残留"刷满每一轮（真机实测 3 条全误报）。
        cli_one = edit("tempconv.py", 'print(f"{value}C = {converted}F")\n')
        check(bool(rules.scan_edits([cli_one])),
              "非入口判断：不带入口信息时，脚本里的 print 仍算残留（豁免是有条件的）")
        check(not rules.scan_edits([cli_one], entry_paths={"tempconv.py"}),
              "入口脚本（需求里点名的 `python tempconv.py`）里的 print 被豁免")
        orch.requirement = "命令行形如 `python tempconv.py 100 C2F`，输出保留 1 位小数"
        orch.state = {}
        cands = orch._entry_path_candidates()
        check("tempconv.py" in cands,
              "从需求里的可执行命令提取入口脚本（项目自定义名也能认出来）",
              "、".join(sorted(cands))[:60])

        # `unchecked` 的文案：同一状态被两种成因共用（没提供仓库 / 目标文件不存在），
        # 只写前者会把排查引到错方向（真机 20260926-214757：传了 --repo 却报「没提供仓库」）。
        from pipeline import patches as patches_mod  # noqa: PLC0415
        check("没提供仓库" not in patches_mod.STATUS_CN["unchecked"],
              "unchecked 的展示文案不再断言「没提供仓库」",
              patches_mod.STATUS_CN["unchecked"])
        with tempfile.TemporaryDirectory() as rp:
            a1 = patches_mod.analyze_all(rp, {"edits": [{
                "path": "cli.py", "change_type": "modify", "target_symbol": "CLI",
                "patch_mode": "replace_span", "patch": "class CLI:\n    pass\n",
            }]})
            note1 = " ".join(a1["edits"][0].get("notes") or [])
            check("目标文件不存在" in note1 and "仓库路径本身没问题" in note1,
                  "有仓库但文件不在仓库里 → 备注说清真正原因（别让人去查 --repo）", note1[:50])
            check("目标文件不存在" in " ".join(a1.get("problem_detail") or []),
                  "审计明细也带上真正原因", str(a1.get("problem_detail"))[:60])
        a2 = patches_mod.analyze_all(None, {"edits": [{
            "path": "cli.py", "change_type": "modify", "target_symbol": "CLI",
            "patch_mode": "replace_span", "patch": "x\n",
        }]})
        note2 = " ".join(a2["edits"][0].get("notes") or [])
        check("没有提供仓库路径" in note2, "真没提供仓库时，备注也说得明白", note2[:50])

        gw_dir = root / "ga-run"
        gw_dir.mkdir()
        gw_mod._mark_ga_running(gw_dir, {"files": 3, "lines": 120})
        mid = runstore.read_json_if_exists(gw_dir / gw_mod.GA_ARTIFACT_NAME) or {}
        check(mid.get("status") == "running" and mid.get("stage") == "gateway",
              "调 GA 之前先落「正在判定」痕迹（页面据此说清当前阶段）", str(mid.get("status")))
        gw_mod._save_ga_artifacts(gw_dir, ga={"modules": []}, problems=[], stats={}, scale="small")
        done = runstore.read_json_if_exists(gw_dir / gw_mod.GA_ARTIFACT_NAME) or {}
        check(done.get("status") == "done" and done.get("scale") == "small",
              "GA 出结论后痕迹变 done（含 scale，页面可区分「在判定」与「已结论」）",
              str(done.get("status")))

        # ⑥ 高频坑 → 建议升规则（量化触发：≥8 次 且 跨 ≥3 次运行）
        flat = root / "_scan"
        for i in range(3):
            d = flat / f"r{i}"
            d.mkdir(parents=True)
            runstore.write_json(d / runstore.STATE_NAME, {
                "rounds": [{"attempt": 1, "verdict": "rework_dev", "review": {
                    "required_fixes": ["补测试", "去掉重复定义", "说明偏差"],
                }}],
            })
        esc = issues.escalation_candidates(flat, min_occurrences=8, min_runs=3)
        check(bool(esc) and esc[0]["kind"] == "review_required_fix",
              "反复出现的问题跨 3 次运行后成为「建议升规则」候选",
              f"{esc[0]['occurrences']} 次/{esc[0]['runs']} 次运行" if esc else "无候选")
        check(not issues.escalation_candidates(flat, min_occurrences=100),
              "没到阈值就不建议（避免噪音）")


def main() -> int:
    print("== 规则库契约")
    loaded = rules.load(force=True)
    check(len(loaded) >= 12, "规则库能加载且规则数合理", f"{len(loaded)} 条")
    check(not rules.load_notes(), "默认规则库无加载问题", "; ".join(rules.load_notes()[:2]))
    by_id = {r.id: r for r in loaded}
    missing_contract = [
        r.id for r in loaded
        if not r.evidence.strip() or not r.negative.strip()
    ]
    check(not missing_contract, "每条规则都有 evidence（凭什么判负）与 negative（反例判据）",
          "、".join(missing_contract[:3]))
    bad_layer_b = [r.id for r in loaded if r.layer == "B" and not r.applies_when]
    check(not bad_layer_b, "layer=B 的规则都写了 applies_when（否则会在不相干项目里全红）",
          "、".join(bad_layer_b[:3]))
    bad_blockers = [r.id for r in loaded if r.is_blocker and not r.negative.strip()]
    check(not bad_blockers, "阻断级规则必须有反例判据（可证伪才允许判负）", "、".join(bad_blockers[:3]))
    check(
        by_id.get("destructive_migration") is not None and by_id["destructive_migration"].layer == "B",
        "破坏性迁移规则是条件触发（只在迁移文件里生效）",
    )
    check(
        by_id.get("debug_residue") is not None and not by_id["debug_residue"].is_blocker,
        "调试残留只是提示级（CLI 的 print 是合法输出，判负会误伤）",
    )

    print("== 正反用例")
    for label, one, expect_hit, expect_miss in CASES:
        hits = {f["rule"] for f in rules.scan_edits([one]) if not f.get("note")}
        missing = [r for r in expect_hit if r not in hits]
        wrong = [r for r in expect_miss if r in hits]
        check(
            not missing and not wrong,
            label,
            f"命中={sorted(hits)}" if (missing or wrong) else "",
        )

    print("== 拦截口径")
    secret = rules.scan_edits([edit("config.py", 'API_KEY = "sk-live-abcdef123456"\n')])
    fixes = rules.blocker_fixes(secret)
    check(
        bool(fixes) and "反例判据" in fixes[0] and "deviations" in fixes[0],
        "阻断项转成整改要求时带定位、命中原文与反例出口",
        fixes[0][:70] if fixes else "",
    )
    block = rules.format_block(secret)
    check("反例判据" in block and "不得" in block, "评审证据块里给出反例判据与「不得 pass」要求")
    check("照抄上一轮" in block, "证据块明确否掉「照抄上一轮结论」这种伪证据")
    check(rules.format_block([]) == "", "没有 finding 时不产生空块（不浪费评审预算）")
    check(
        rules.summarize(secret) and "阻断" in rules.summarize(secret)[0],
        "摘要带严重级别（便于日志/页面一眼分辨）",
    )

    print("== 引擎健壮性（规则库写坏也不能拖垮流水线）")
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "rules.json"
        bad.write_text(json.dumps({"rules": [
            {"id": "no_negative", "title": "缺反例判据", "severity": "blocker",
             "patterns": ["BOOM"], "evidence": "x"},
            {"id": "bad_regex", "title": "非法正则", "severity": "warn",
             "patterns": ["("], "evidence": "x", "negative": "y"},
            {"id": "require_empty", "title": "require 没给要求", "kind": "require",
             "severity": "warn", "evidence": "x", "negative": "y"},
            {"title": "没有 id"},
        ]}), encoding="utf-8")
        got = rules.load(bad, force=True)
        ids = {r.id for r in got}
        check("no_negative" in ids and not next(r for r in got if r.id == "no_negative").is_blocker,
              "缺 negative 的规则被降级为 warn（不可判负）")
        check("require_empty" not in ids, "kind=require 却没写 require_patterns 的被跳过")
        check(bool(rules.load_notes()), "加载问题被留痕（不静默）", "; ".join(rules.load_notes()[:1]))
        check(rules.scan_edits([edit("a.py", "BOOM\n")], rules=got) is not None,
              "规则库有问题时 scan 依然返回结果、不抛异常")
        broken = Path(tmp) / "broken.json"
        broken.write_text("{ not json", encoding="utf-8")
        check(rules.load(broken, force=True) == [], "规则文件不是 JSON 时返回空表而不是抛异常")
        check(rules.scan_edits([edit("a.py", "x = 1\n")], rules=[]) == [],
              "空规则表下 scan 返回空（流水线照跑）")
    rules.load(force=True)  # 复原缓存，避免影响同进程后续断言

    wiring_checks()

    print(f"\n{'全部通过' if not failures else '有失败项'}：{checks - len(failures)}/{checks}")
    for item in failures:
        print(f"  - {item}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
