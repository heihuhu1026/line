"""BUG 修复模式（tasktype）的离线冒烟：不加载模型，秒级。

验的是「首次开发」与「返工修缺陷」被真正分开，且范围由机器给定而非模型自觉。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import prompts, tasktype  # noqa: E402

PASS = FAIL = 0


def check(cond: bool, name: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  <- {extra}" if extra else ""))


def state_with(commands=None, problems=None, materialized=None):
    return {
        "verify_report": {
            "sandbox": "runs/x/verify/work",
            "commands": commands or [],
            "problems": problems or [],
            "materialized": materialized or [],
        },
        "test_report": {"missing_symbols": ["cli.CLI"]},
        "review": {"verdict": "rework_dev"},
    }


def main() -> int:
    print("== 1. 缺陷单抽取 ==")
    st = state_with(
        commands=[
            {"status": "ok", "command": "py_compile ledger.py", "exit_code": 0},
            {"status": "fail", "command": 'python -c "import cli; cli.CLI().add()"', "exit_code": 1,
             "output": "ModuleNotFoundError: No module named 'cli'"},
            {"status": "fail", "command": "python cli.py list", "exit_code": 1, "output": "boom"},
        ],
        problems=["有 2 条补丁未能套用（交付物不完整）"],
        materialized=["ledger.py"],
    )
    rep = tasktype.bug_report_from_state(st, ["[文件 cli.py] 补全 cli.py 模块实现"])
    check(rep["type"] == tasktype.BUGFIX, "有失败证据 ⇒ 判为 bugfix", rep["type"])
    check(len(rep["repro_steps"]) == 2, "只抽失败命令（ok 的不算复现步骤）", str(rep["repro_steps"]))
    check("cli.py" in rep["affected"], "失败命令 + [文件 X] 前缀 ⇒ 推出受影响文件 cli.py", str(rep["affected"]))
    check(rep["materialized"] == ["ledger.py"], "带上已物化清单（判断哪些文件真存在）")
    check(rep["actual"] == "退出码 1", "期望/实际 从退出码得出", str(rep["actual"]))
    check(bool(rep["logs"]), "日志/判负理由非空")

    empty = tasktype.bug_report_from_state({"verify_report": {}, "test_report": {}}, [])
    check(empty["type"] == tasktype.FEATURE, "无缺陷证据 ⇒ 仍是首次开发", empty["type"])

    print("== 2. 允许范围 ==")
    check(tasktype.allowed_scope(rep) == ["cli.py"], "allowed_scope = 受影响文件", str(tasktype.allowed_scope(rep)))
    check(tasktype.allowed_scope(empty) == [], "信息不足时不限范围（宁可不限制，也不瞎限制）")

    print("== 3. 范围机械判据 ==")
    existing = ["cli.py", "main.py", "ledger.py"]
    # 越界
    v = tasktype.scope_violations(
        [{"path": "cli.py", "change_type": "modify"}, {"path": "unrelated.py", "change_type": "add"}],
        ["cli.py"], existing,
    )
    check(any("越界" in x for x in v), "提交了范围外的文件 ⇒ 判越界", str(v))
    # 漏改
    v = tasktype.scope_violations([{"path": "other.py", "change_type": "modify"}], ["cli.py"], existing)
    check(any("漏改" in x for x in v), "范围内的文件没提交 ⇒ 判漏改", str(v))
    # 已存在文件用 add 整份重吐
    v = tasktype.scope_violations([{"path": "cli.py", "change_type": "add"}], ["cli.py"], existing)
    check(any("重复定义" in x for x in v), "已存在文件用 add ⇒ 判重复定义风险", str(v))
    # 正确做法：modify 定点改
    v = tasktype.scope_violations([{"path": "cli.py", "change_type": "modify"}], ["cli.py"], existing)
    check(v == [], "范围内 + modify 定点改 ⇒ 无判负", str(v))
    # 新增文件（不在 existing）用 add 是允许的
    v = tasktype.scope_violations([{"path": "cli.py", "change_type": "add"}], ["cli.py"], [])
    check(v == [], "文件确实不存在时用 add 合法", str(v))

    print("== 4. 缺陷单文案 ==")
    block = tasktype.format_bug_report(rep)
    check(bool(block) and "只允许" in block, "文案里有范围约束")
    check("禁止" in block and "modify" in block, "文案里写明已存在文件只能定点改")
    check(len(block) < 3000, f"篇幅受控（{len(block)} 字符，dev 预算约 8K）")
    check(tasktype.format_bug_report(empty) == "", "无缺陷时不注入空块")

    print("== 5. 系统提示词按任务类型切换 ==")
    feat = prompts.system_prompt("dev", "new", tasktype.FEATURE)
    bug = prompts.system_prompt("dev", "new", tasktype.BUGFIX)
    check(feat != bug, "首轮与返工**不是同一套**提示词")
    check("一律用" in feat and "full_symbol" in feat, "首轮：整份新建纪律")
    check("缺陷修复" in bug and "最小补丁" in bug, "返工：缺陷修复 + 最小补丁")
    check("禁止" in bug and "add" in bug, "返工：禁止对已存在文件整份重吐")
    # 返工轮的 test / review 也**必须**换口径 —— 这一条原先断言的恰恰是缺陷本身：
    # 它们与首轮逐字相同（实测 2029 / 1702 字），于是返工轮里 test 仍要求"补全新功能测试"、
    # review 仍重提首轮取舍，而 dev 只被授权改缺陷单范围 ⇒ 两条口径对撞、白烧一轮。
    check(prompts.system_prompt("test", "new", tasktype.BUGFIX)
          != prompts.system_prompt("test", "new", tasktype.FEATURE),
          "返工轮 test 换口径（回归证明，不再要求补新功能测试）")
    check(prompts.system_prompt("review", "new", tasktype.BUGFIX)
          != prompts.system_prompt("review", "new", tasktype.FEATURE),
          "返工轮 review 换口径（判缺陷是否关闭，不重提首轮取舍）")
    # 未覆盖的阶段仍应回退，不能取不到（这是设计：方案轮没有返工变体，按首轮标准走）
    check(prompts.system_prompt("architect_plan", "new", tasktype.BUGFIX)
          == prompts.system_prompt("architect_plan", "new", tasktype.FEATURE),
          "未覆盖阶段回退到原提示词（不报错）")

    print("== 6. 缺陷单的命令呈现：保形 + 机械自检与真复现分开 ==")
    # 真机 20260928-095848：缺陷单把 harness 的多行导入自检脚本当成"复现步骤"，
    # 且被 `_short` 压平（`import importlib, sys bad = [] for name in …`）—— 语法已废、还带 `…`。
    mech_cmd = (
        'python -c "import importlib, sys\n'
        "bad = []\n"
        "for name in sys.argv[1:]:\n"
        "    print(name)\n"
        'sys.exit(1)"   # IMPORT_CHECK'
    )
    check("\n" in tasktype._cmd_text(mech_cmd),
          "多行命令**保留换行**（压平会让脚本语法作废）", tasktype._cmd_text(mech_cmd)[:60])
    check("…（中略）…" in tasktype._cmd_text("x" * 400, 100),
          "超长命令改为首尾各留一半（尾部常带真正的报错行）")
    check(tasktype._is_mechanical_command(mech_cmd)
          and not tasktype._is_mechanical_command("python main.py"),
          "能识别 harness 自己的机械自检命令")

    st_cmd = state_with(commands=[
        {"command": mech_cmd, "status": "fail", "exit_code": 1, "output": "FAIL cli"},
        {"command": "python main.py", "status": "fail", "exit_code": 1, "output": "usage"},
    ])
    rep_cmd = tasktype.bug_report_from_state(st_cmd, ["[文件 main.py] 修导入符号"])
    check(all("IMPORT_CHECK" not in c for c in rep_cmd["repro_steps"])
          and any("main.py" in c for c in rep_cmd["repro_steps"]),
          "复现步骤只留**用户行为**命令（模型修不了检查脚本本身）", str(rep_cmd["repro_steps"]))
    check(any("IMPORT_CHECK" in c for c in rep_cmd["mechanical_checks"]),
          "机械自检单独列出，不冒充复现步骤")
    blk_cmd = tasktype.format_bug_report(rep_cmd)
    check("别改它本身" in blk_cmd, "缺陷单明说机械自检不是要改的东西", blk_cmd[:160])
    check("import importlib, sys\nbad = []" in blk_cmd,
          "缺陷单里那条命令仍是多行原文（可读）", blk_cmd[-220:])
    check(len(blk_cmd) < 3000, f"篇幅仍受控（{len(blk_cmd)} 字符）")

    print("== N. 归因到施工图：哪个 task、什么具体问题 ==")
    # 诉求：返工要指明"哪个 task 的具体什么问题"。映射本来就是机械可得的
    # （补丁自带 covers_tasks、施工图自带 target_files），不该让人或模型去对。
    plan = {"tasks": [
        {"id": "T-01", "target_files": ["database.py"]},
        {"id": "T-03", "target_files": ["cli.py"]},
        {"id": "T-05", "target_files": ["main.py"]},
    ]}
    st2 = {
        "patch_audit": {"edits": [
            # ① 补丁自己声明了 covers_tasks ⇒ 直接归因，最可靠
            {"path": "cli.py", "symbol": "CLI", "status": "new_file_syntax_error",
             "tasks": ["T-03"], "notes": ["第 2 行：字符串未闭合"]},
            # ② 没声明 covers_tasks ⇒ 按 path + 施工图 target_files 归因
            {"path": "main.py", "symbol": "App", "status": "anchor_not_found",
             "tasks": [], "notes": []},
        ]},
        "implementation_audit": {"missing": ["T-05"]},
        "verify_report": {"verdict": "fail", "problems": ["有 4 条补丁未能套用"]},
    }
    rep = tasktype.bug_report_from_state(
        st2,
        ["[文件 cli.py] 修参数解析"],
        plan=plan,
        # **当前代码**：缺陷单要从中摘"待改处的逐字原文"
        sources={"cli.py": "import sys\n\n\nclass CLI:\n    def add(self, args):\n"
                           "        self.db.add_entry(args.amount)\n"},
    )
    by = rep.get("by_task") or {}
    check("T-03" in by and "T-05" in by, "两个施工图都被归因到", str(sorted(by)))
    check(by["T-03"]["files"] == ["cli.py"], "T-03 关联到 cli.py", str(by["T-03"]["files"]))
    check(any("语法错误" in p for p in by["T-03"]["problems"]),
          "T-03 的问题写清了是什么（写残/未闭合）", str(by["T-03"]["problems"]))
    check(any("anchor" in p for p in by["T-05"]["problems"]),
          "没声明 covers_tasks 的补丁也能按文件归因到 T-05", str(by["T-05"]["problems"]))
    check(any("没有任何补丁" in p for p in by["T-05"]["problems"]),
          "「这张图一条补丁都没有」也点名", str(by["T-05"]["problems"]))
    check(any(p.startswith("评审要求：修参数解析") for p in by["T-03"]["problems"]),
          "评审返工项归因并**去掉冗余的 [文件 X] 前缀**", str(by["T-03"]["problems"]))
    check("T-01" not in by, "没有证据的施工图**不**被点名（不扩大返工范围）", str(sorted(by)))
    rendered = tasktype.format_bug_report(rep)
    check("修复项" in rendered and "按施工图分组" in rendered and "T-03（cli.py）" in rendered,
          "缺陷单渲染出「修复项」段并按施工图分组", rendered[:140])
    check("（未记录文件）" not in rendered, "归因段带得上文件就不写占位")
    # **逐字原文**：这是本轮根治的核心 —— 不给原文，修复方只能凭记忆改写位置
    # （真机 20260927-221511 的 anchor 就是近似行 ⇒ 永远套用不上）。
    check("逐字对齐" in rendered and "self.db.add_entry(args.amount)" in rendered,
          "修复项带上**当前逐字原文**（含行号）", rendered[:400])
    check("    6| " in rendered or "|     def add" in rendered, "原文带行号前缀", rendered[:400])
    check("验收：" in rendered, "每条修复项都有验收口径")
    check("不属于任何施工图" not in rendered,
          "都在施工图覆盖范围内时不产生「无主」分组", rendered[:200])
    # 施工图本身也要看到"本张图的问题"（按 task 分派时，这次调用只做这一张）
    focus = prompts.task_focus_block(
        {"id": "T-03", "target_files": ["cli.py"], "symbols": ["CLI.add"],
         "rework_problems": by["T-03"]["problems"]}
    )
    check("本张施工图上一轮的具体问题" in focus and "语法错误" in focus,
          "施工图里直接列出本张图的问题", focus[-160:])
    check("本张施工图上一轮的具体问题"
          not in prompts.task_focus_block({"id": "T-01", "target_files": ["a.py"]}),
          "没有问题时不渲染该块")

    print("== M. 逐项验收：修好了没，必须逐条可追 ==")
    # 断言：三类状态，且**不许把"没核对"说成"通过"**。
    st3 = {
        "patch_audit": {"edits": [
            # 这条修复项的文件**没有被任何命令碰到** ⇒ 无从核对
            {"path": "db.py", "symbol": "DB.save", "status": "anchor_not_found",
             "tasks": ["T-01"], "notes": []},
        ]},
        "verify_report": {
            "verdict": "pass",
            "commands": [
                {"status": "ok", "command": "python cli.py add 1 x", "exit_code": 0},
            ],
        },
    }
    verdicts = tasktype.defect_verdicts(st3, [], plan=plan)
    by_where = {str(v.get("where")): v for v in verdicts}
    check(any(v["status"] == "unverifiable" for v in verdicts),
          "没有命令能核对的修复项 ⇒ 显式判「无从核对」（不假装通过）",
          str([(v.get("where"), v.get("status")) for v in verdicts]))
    check(all(v["status"] in ("green", "red", "unverifiable") for v in verdicts),
          "只有三类状态（不存在第四类「看着像过了」）", str({v["status"] for v in verdicts}))
    # 命令能对上时：全过 ⇒ 转绿；有失败 ⇒ 仍失败
    st4 = {
        "patch_audit": {"edits": [
            {"path": "cli.py", "symbol": "CLI.add", "status": "anchor_not_found",
             "tasks": ["T-03"], "notes": []},
        ]},
        "verify_report": {"verdict": "fail", "commands": [
            {"status": "fail", "command": "python cli.py add 1 x", "exit_code": 1, "output": ""},
        ]},
    }
    v4 = tasktype.defect_verdicts(st4, [], plan=plan)
    red = [v for v in v4 if v["status"] == "red"]
    check(bool(red), "命令仍失败 ⇒ 判「仍失败」", str([(v.get("where"), v.get("status")) for v in v4]))
    st5 = dict(st4)
    st5["verify_report"] = {"verdict": "pass", "commands": [
        {"status": "ok", "command": "python cli.py add 1 x", "exit_code": 0},
    ]}
    v5 = tasktype.defect_verdicts(st5, [], plan=plan)
    check(any(v["status"] == "green" for v in v5), "命令转绿 ⇒ 判「转绿」",
          str([(v.get("where"), v.get("status")) for v in v5]))
    # 评审形态：只列没转绿的；转绿的不占评审预算
    block = tasktype.render_defect_verdicts(v4)
    check("逐项验收" in block and "仍失败" in block, "评审块列了仍失败的项", block[:120])
    check(tasktype.render_defect_verdicts(v5) == "" or "仍失败" not in tasktype.render_defect_verdicts(v5),
          "全转绿时评审块不列失败项")
    human = tasktype.render_defect_verdicts_markdown(v5)
    check(any("转绿" in x for x in human), "人读形态**保留转绿的**（可追溯，不是只报坏消息）",
          str(human[:2]))

    print("== K. 方案漏项判据不许被标准库路径误触发（真机 20260928-000351） ==")
    # 真机事实：verify 的导入自检命令里有 `import importlib`，被反推成 `importlib.py`，
    # 于是"缺陷指向方案未规划的文件（importlib.py）" ⇒ 机制强制 rework_architect，
    # **白烧一整轮架构师**。误报的代价不是多打一行字。
    check(tasktype._is_external_module("importlib") is True, "`importlib` 判为标准库")
    check(tasktype._is_external_module("sqlite3") is True, "`sqlite3` 判为标准库")
    check(tasktype._is_external_module("cli") is False, "产物模块**不**被误判为外部")
    paths = tasktype._paths_from_command('python -c "import importlib, sys, cli; x"')
    check("importlib.py" not in paths and "cli.py" in paths,
          "命令反推路径时**排除标准库**、保留产物模块（逗号列表要逐个取）", str(paths))
    check(tasktype._paths_from_command('python -c "from cli import CLI"') == ["cli.py"],
          "`from cli import CLI` 取的是**模块** cli，不是符号 CLI",
          str(tasktype._paths_from_command('python -c "from cli import CLI"')))
    check(tasktype._match_known("C:/Program Files/Python312/importlib/__init__.py", ["cli.py"]) == "",
          "标准库绝对路径**不认**（不是产物文件）")
    check(tasktype._match_known("<frozen importlib._bootstrap>", ["cli.py"]) == "",
          "冻结帧**不认**")
    check(tasktype._match_known("D:/other/place/mod.py", ["cli.py"]) == "",
          "**沙箱外**的绝对路径不认（收敛不到已知文件就不猜）")
    check(tasktype._match_known("D:/AI/line/runs/x/verify/work/pkg/mod.py", ["cli.py"]) == "pkg/mod.py",
          "沙箱内剥掉前缀后是**相对路径** ⇒ 可用（那是产物里方案没覆盖的文件）")
    check(tasktype._match_known("D:/AI/line/runs/x/verify/work/cli.py", ["cli.py"]) == "cli.py",
          "沙箱内的已知文件正常收敛")
    check(tasktype._match_known("pkg/mod.py", ["cli.py"]) == "pkg/mod.py",
          "本来就相对的路径仍可用末尾两段兜底")

    print(f"\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
