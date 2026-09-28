"""失败归因（diagnose）的离线冒烟：不加载模型，秒级。

验的是三件事：
  ① **归因优先序**（根因优先于症状：补丁定位不上 > 沙箱跑不通 > 契约虚依赖 > 方案层 > 实现层）；
  ② **去向与历史 `_step_review` 完全一致**（这一层是行为，不是重构：pass → 分类矛盾 →
     触顶 → 护栏 → 方案层 → 实现层）；
  ③ **缺陷台账跨轮守恒**（仍开 / 转绿 / 不再出现，且"不再出现"不被自动判为已修复）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import diagnose as D  # noqa: E402

PASS = FAIL = 0


def check(cond: bool, name: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  <- {extra}" if extra else ""))


def main() -> int:
    print("== 1. 归因优先序：根因优先于症状 ==")
    both = D.classify(
        review={"verdict": "rework_dev"},
        patch_blockers=["cli.py 的 anchor 在原文里找不到"],
        mechanical_blockers=["运行验证失败：`python main.py` 退出码 1"],
    )
    check(both["type"] == D.PATCH_UNAPPLIABLE and both["owner"] == D.OWNER_DEV,
          "补丁定位不上 + 跑不通同时出现 ⇒ 判**根因**（补丁），不是症状", str(both["type"]))
    check(both["recover_stage"] == "dev", "实现层归因 ⇒ 回开发", both["recover_stage"])

    only_mech = D.classify(
        review={"verdict": "rework_dev"}, mechanical_blockers=["运行验证失败：退出码 1"]
    )
    check(only_mech["type"] == D.VERIFY_FAILED, "只有沙箱证据 ⇒ 判沙箱跑不通", only_mech["type"])

    unresolved = D.classify(
        review={"verdict": "rework_dev"},
        unresolved=[{"symbol": "database.insert_record", "reason": "模块 database 存在，但成员不在基准里"}],
    )
    check(unresolved["type"] == D.CONTRACT_UNRESOLVED and unresolved["owner"] == D.OWNER_ARCHITECT,
          "契约虚依赖 ⇒ 归因指向方案层（实现层改不动跨文件契约）",
          f"{unresolved['type']}/{unresolved['owner']}")
    check(unresolved["recover_stage"] == "dev" and unresolved["owner_mismatch"],
          "**但路由按既有规则走**（这一条还没接进路由：改行为要有基线）→ 显式标 owner_mismatch",
          f"{unresolved['recover_stage']}/{unresolved['owner_mismatch']}")
    check("database.insert_record" in " ".join(unresolved["evidence"]), "证据里点名到具体符号")

    # 方案漏项在真实流程里已被 `_normalize_review` 强制改成 rework_architect（见该处 4133-4143），
    # 所以这里按**真实输入**断言，而不是按"原始 verdict"造一个流程里不存在的组合。
    gap = D.classify(review={"verdict": "rework_architect"}, uncovered_files=["ui.py"])
    check(gap["type"] == D.PLAN_GAP and gap["recover_stage"] == "architect_plan"
          and not gap["owner_mismatch"],
          "方案漏规划文件 ⇒ 方案层，且路由一致（机制已把它接进路由）",
          f"{gap['type']}/{gap['recover_stage']}")

    entry = D.classify(
        review={"verdict": "rework_dev", "architect_fixes": ["补 main.py"]}, missing_entry=True
    )
    check(entry["type"] == D.MISSING_ENTRY, "缺入口单独成一类（便于观测）", entry["type"])

    print("== 2. 去向与历史 _step_review 一致 ==")
    ok = D.classify(review={"verdict": "pass"})
    check(ok["type"] == D.NONE and ok["recover_stage"] == "human_review" and not ok["needs_human"],
          "pass ⇒ 交人工审核闸门（不标 needs_human）", f"{ok['type']}/{ok['recover_stage']}")

    amb = D.classify(review={"verdict": "rework_architect", "escalated_ambiguous": True})
    check(amb["needs_human"] and amb["recover_stage"] == "done"
          and amb["stop"] == "ambiguous" and amb["type"] == D.AMBIGUOUS,
          "分类自相矛盾 ⇒ 交人工（且归因记为「矛盾」而不是「方案层」）",
          f"{amb['type']}/{amb['stop']}")

    top = D.classify(
        review={"verdict": "rework_dev", "required_fixes": ["改 X"]},
        attempt=3, max_rework=2, patch_blockers=["anchor 对不上"],
    )
    check(top["needs_human"] and top["stop"] == "rework_limit",
          "触顶 ⇒ 交人工", str(top["stop"]))
    check(top["type"] == D.PATCH_UNAPPLIABLE,
          "**触顶不覆盖归因**（否则最后一轮的账就丢了：改不动的原因必须留下）", top["type"])

    guard = D.classify(
        review={"verdict": "rework_dev", "required_fixes": ["改 X"]},
        guard_stop="最近 3 轮待修项没有净下降",
    )
    check(guard["needs_human"] and guard["stop"] == "guard_stop", "护栏 ⇒ 交人工", str(guard["stop"]))

    arch = D.classify(review={"verdict": "rework_architect"})
    check(arch["recover_stage"] == "architect_plan", "判方案有错 ⇒ 回方案", arch["recover_stage"])
    arch2 = D.classify(review={"verdict": "rework_dev", "architect_fixes": ["方案漏规划 db.py"]})
    check(arch2["recover_stage"] == "architect_plan",
          "有方案层返工项（哪怕 verdict 是 rework_dev）⇒ 回方案", arch2["recover_stage"])

    qual = D.classify(review={"verdict": "rework_dev", "required_fixes": ["补注释"]})
    check(qual["type"] == D.REVIEW_QUALITY and qual["recover_stage"] == "dev",
          "无机械证据的实现层返工 ⇒ 回开发", f"{qual['type']}/{qual['recover_stage']}")

    print("== 3. 重复信号：只记录、不改路由 ==")
    rep = D.classify(review={"verdict": "rework_dev", "required_fixes": ["补注释"]},
                     prior_types=[D.REVIEW_QUALITY] * 2)
    check(rep["repeat"] == 3 and rep["escalate_suggested"],
          "同型连续 3 轮 ⇒ 标建议升级（但去向仍是 dev，本轮不擅自改行为）",
          f"repeat={rep['repeat']} esc={rep['escalate_suggested']} to={rep['recover_stage']}")
    check(rep["recover_stage"] == "dev", "建议升级不改变去向（改行为要有基线）", rep["recover_stage"])
    mixed = D.classify(review={"verdict": "rework_dev", "required_fixes": ["补注释"]},
                       prior_types=[D.PLAN_GAP, D.REVIEW_QUALITY])
    check(mixed["repeat"] == 2 and not mixed["escalate_suggested"],
          "类型交替 ⇒ 不算同类重复（避免'原地抖动'被当成无进展）",
          f"repeat={mixed['repeat']}")

    print("== 4. 日志形态 ==")
    lines = "\n".join(D.render(rep))
    check("同类连续第 3 轮" in lines and "建议升级去向" in lines, "重复与升级建议都出现在日志里", lines[:120])
    check(all(line.strip() for line in lines.splitlines()), "不留空行")

    print("== 5. 缺陷台账：跨轮守恒 ==")
    rows_r1 = [
        {"path": "cli.py", "symbol": "add", "line": 12, "what": "参数漏了 note", "status": "red",
         "where": "cli.py:12"},
        {"path": "db.py", "symbol": "save", "line": 30, "what": "表名写错", "status": "unverifiable",
         "where": "db.py:30"},
    ]
    led1 = D.ledger(None, rows=rows_r1, round_no=1)
    check(len(led1["open"]) == 2 and led1["fresh"] == 2 and not led1["dropped"],
          "首轮：两条都进台账", str(led1["fresh"]))

    rows_r2 = [
        # 第一条转绿；第二条措辞微调（⇒ 按保守口径算"新出现"，见 defect_key 的说明）
        {"path": "cli.py", "symbol": "add", "line": 12, "what": "参数漏了 note", "status": "green",
         "where": "cli.py:12"},
        {"path": "db.py", "symbol": "save", "line": 31, "what": "表名写错", "status": "red",
         "where": "db.py:31"},
    ]
    led2 = D.ledger(led1, rows=rows_r2, round_no=2)
    check(len(led2["fixed_verified"]) == 1 and len(led2["open"]) == 1,
          "第二轮：一条转绿、一条仍开", f"green={led2['fixed_verified']} open={len(led2['open'])}")
    check(len(led2["dropped"]) == 1,
          "位置变了 ⇒ 旧条目算'不再出现'（**不自动判为已修复**）", str(led2["dropped"]))

    # 第三轮：上一轮转绿的那条**又红了**（回归），上一轮仍开的 db.py 这次不再出现。
    rows_r3 = [{"path": "cli.py", "symbol": "add", "line": 12, "what": "参数漏了 note",
                "status": "red", "where": "cli.py:12"}]
    led3 = D.ledger(led2, rows=rows_r3, round_no=3)
    check(len(led3["open"]) == 1 and led3["fresh"] == 1,
          "转绿后又红了 ⇒ 算本轮新出现（台账不假设'绿过就永远绿'）", str(led3["fresh"]))
    check(led3["regressions"] == list(led3["open"]),
          "**标出回归**：上轮已转绿又红的，是台账最有价值的一条信号", str(led3["regressions"]))
    check("db.py::save" in " ".join(led3["dropped"]),
          "上轮仍开、这轮不再出现 ⇒ 如实报出来（既不自动判修复，也不静默消失）",
          str(led3["dropped"]))
    line3 = D.ledger_line(led3)
    check("仍开 1 条" in line3 and "缺陷台账" in line3, "台账日志说清还剩几条", line3)
    line2 = D.ledger_line(led2)
    check("不等于已修复" in line2, "日志明写'不再出现'不等于已修复（不替人下结论）", line2[:150])

    print(f"\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
