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
    # 未覆盖的阶段应回退，不能取不到
    check(prompts.system_prompt("test", "new", tasktype.BUGFIX) == prompts.system_prompt("test", "new", tasktype.FEATURE),
          "未覆盖阶段回退到原提示词（不报错）")

    print(f"\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
