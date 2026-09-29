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

    print("== 5. 缺陷台账：跨轮守恒（v2 身份：行号只是证据） ==")
    rows_r1 = [
        {"path": "cli.py", "symbol": "add", "line": 12, "what": "参数漏了 note", "status": "red",
         "where": "cli.py:12"},
        {"path": "db.py", "symbol": "save", "line": 30, "what": "表名写错", "status": "red",
         "where": "db.py:30"},
    ]
    led1 = D.ledger(None, rows=rows_r1, round_no=1)
    check(len(led1["open"]) == 2 and led1["fresh"] == 2 and not led1["dropped"],
          "首轮：两条都进台账", str(led1["fresh"]))
    check(next(iter(led1["open"])).startswith(D.DEFECT_KEY_VERSION + "::"),
          "台账键带 v2 版本前缀", str(next(iter(led1["open"]))))

    rows_r2 = [
        # 第一条转绿；第二条行号从 30 漂到 31、**问题不变**（v2：行漂移不换身份）；
        # 另加一条真正的新缺陷（ui.py 漏文件）
        {"path": "cli.py", "symbol": "add", "line": 12, "what": "参数漏了 note", "status": "green",
         "where": "cli.py:12"},
        {"path": "db.py", "symbol": "save", "line": 31, "what": "表名写错", "status": "red",
         "where": "db.py:31"},
        {"path": "ui.py", "symbol": "draw", "line": 1, "what": "方案外文件被引用", "status": "red",
         "where": "ui.py:1"},
    ]
    led2 = D.ledger(led1, rows=rows_r2, round_no=2)
    check(len(led2["fixed_verified"]) == 1 and len(led2["open"]) == 2,
          "第二轮：一条转绿、两条仍开", f"green={led2['fixed_verified']} open={len(led2['open'])}")
    check(not led2["dropped"],
          "行号 30→31、问题不变 ⇒ **同一条缺陷**（v2 把行号降为证据，不再误报'新出现'）",
          str(led2["dropped"]))
    db_rec = next(rec for rec in led2["open"].values() if "表名写错" in str(rec.get("what")))
    check(db_rec.get("evidence_streak") == 2,
          "同一缺陷相同证据连续 2 轮 ⇒ 证据 streak=2（Recovery Policy 的输入）",
          str(db_rec.get("evidence_streak")))
    check(led2["fresh"] == 1, "只有 ui.py 那条算新出现", str(led2["fresh"]))

    # 第三轮：上一轮转绿的那条**又红了**（回归），上一轮仍开的 db/ui 这次不再出现。
    rows_r3 = [{"path": "cli.py", "symbol": "add", "line": 12, "what": "参数漏了 note",
                "status": "red", "where": "cli.py:12"}]
    led3 = D.ledger(led2, rows=rows_r3, round_no=3)
    check(len(led3["open"]) == 1 and led3["fresh"] == 0,
          "转绿后又红了 ⇒ 是**回归**不是新出现（首现轮次等历史从 fixed 明细带回）",
          f"open={len(led3['open'])} fresh={led3['fresh']}")
    check(led3["regressions"] == list(led3["open"]),
          "**标出回归**：上轮已转绿又红的，是台账最有价值的一条信号", str(led3["regressions"]))
    reopened_rec = next(iter(led3["open"].values()))
    check(reopened_rec.get("first_round") == 1 and reopened_rec.get("reopened_round") == 3
          and reopened_rec.get("evidence_streak") == 1,
          "回归条目首现轮次保留、重开轮次登记、证据 streak 重新计（新一轮失败发作）",
          str({k: reopened_rec.get(k) for k in
               ("first_round", "reopened_round", "evidence_streak")}))
    check(not led3["fixed_verified"], "回归条目从 fixed_verified 摘除（绿过不是永远绿）",
          str(led3["fixed_verified"]))
    check(any(D._key_path(d) == "db.py" for d in led3["dropped"]),
          "上轮仍开、这轮不再出现 ⇒ 如实报出来（v2 多段键仍能取到文件名）",
          str([D._key_path(d) for d in led3["dropped"]]))
    check(any(D._key_path(d) == "ui.py" for d in led3["dropped"]),
          "ui.py 那条同样进 dropped（不替人下结论）", str(led3["dropped"]))
    line3 = D.ledger_line(led3)
    check("仍开 1 条" in line3 and "缺陷台账" in line3, "台账日志说清还剩几条", line3)
    line2 = D.ledger_line(led2)
    check("不等于已修复" not in line2, "第二轮没有 dropped ⇒ 日志不带 dropped 段", line2[:150])

    # ---- 跨轮守恒（旧实现的洞：转绿条目在没有记录的轮次会被整笔抹掉）----
    # 场景：r2 已转绿一条；r3 是最终确认轮，defect_verdicts 没有任何行（pass 轮常态）。
    led_pass = D.ledger(led2, rows=[], round_no=3)
    check(len(led_pass["fixed_verified"]) == 1,
          "本轮没有任何核对记录 ⇒ 历史已转绿条目**继续在账**（守恒，不清零）",
          str(led_pass["fixed_verified"]))
    green_key = led2["fixed_verified"][0]
    check((led_pass.get("fixed_detail") or {}).get(green_key, {}).get("fixed_round") == 2,
          "转绿明细保留 fixed_round（回归时身份与历史都能恢复）",
          str((led_pass.get("fixed_detail") or {}).get(green_key)))

    print("== 6. defect_key v2：类型+check+文件+符号，line/what 只是证据 ==")
    def tb(line, what, exc="ImportError", command="python -m unittest"):
        return {"path": "main.py", "symbol": "main", "line": line, "what": what,
                "status": "red", "source": "traceback", "kind": f"traceback:{exc}",
                "command": command}
    k1 = D.defect_key(tb(20, "模块无法导入"))
    k2 = D.defect_key(tb(27, "import 失败"))
    check(k1 == k2 == f"{D.DEFECT_KEY_VERSION}::traceback:ImportError::main.py::main::"
                      "python -m unittest",
          "行号漂移 + 措辞变化 + stderr 不同 ⇒ 仍是同一条缺陷", f"{k1} | {k2}")
    k3 = D.defect_key(tb(20, "断言不成立", exc="AssertionError"))
    check(k3 != k1, "异常类型不同（ImportError vs AssertionError）⇒ 不同缺陷", f"{k1} | {k3}")
    k4 = D.defect_key(tb(20, "模块无法导入", command="python main.py"))
    check(k4 != k1, "验收命令不同 ⇒ 不同缺陷（check_id 是身份的一部分）", f"{k1} | {k4}")
    pk1 = D.defect_key({"path": "cli.py", "symbol": "add", "what": "anchor 在原文里找不到（第 2 行）",
                        "source": "patch_audit", "kind": "patch:anchor_not_found"})
    pk2 = D.defect_key({"path": "cli.py", "symbol": "add", "what": "anchor 在原文里找不到（第 9 行）",
                        "source": "patch_audit", "kind": "patch:anchor_not_found"})
    check(pk1 == pk2 and pk1.endswith("patch:anchor_not_found::cli.py::add::"),
          "同一符号同状态的补丁判负 ⇒ 同一缺陷（备注差异是证据）", f"{pk1} | {pk2}")
    rk1 = D.defect_key({"path": "x.py", "what": "评审要求改 A", "source": "review", "kind": "review"})
    rk2 = D.defect_key({"path": "x.py", "what": "评审要求改 B", "source": "review", "kind": "review"})
    check(rk1 != rk2, "评审行无机械口径 ⇒ 退用摘要文本，措辞不同仍保守地新开一条", "")

    print("== 7. Failure owner 三分类：不把基础设施/虚靶甩给 DEV ==")
    check(D.patch_owner_class("anchor_not_found") == D.OWNER_DEV_PATCH,
          "anchor 对不上 ⇒ dev_patch（补丁内容写错）", "")
    check(D.patch_owner_class("new_file_syntax_error") == D.OWNER_DEV_PATCH,
          "新文件语法错 / 补丁残缺 / 重复定义 ⇒ dev_patch", "")
    check(D.patch_owner_class("symbol_not_found", symbol_planned=True)
          == D.OWNER_COMPILER_TARGET,
          "符号在方案里被点名、基准里没有 ⇒ compiler_target（靶子是虚的）", "")
    check(D.patch_owner_class("symbol_not_found", symbol_planned=False) == D.OWNER_DEV_PATCH,
          "符号不在方案里 ⇒ 开发自己写错名 ⇒ dev_patch", "")
    check(D.patch_owner_class("unchecked", note="没有提供仓库路径，无法核对")
          == D.OWNER_PATCH_RUNTIME,
          "没拿到仓库快照 ⇒ patch_runtime（基础设施）", "")
    check(D.patch_owner_class("unchecked", note="目标文件不存在", file_planned=True)
          == D.OWNER_COMPILER_TARGET,
          "方案覆盖的文件仓库里缺失 ⇒ compiler_target", "")
    check(D.patch_owner_class("unchecked", note="目标文件不存在", file_planned=False)
          == D.OWNER_DEV_PATCH,
          "方案外文件 modify 缺失 ⇒ 开发 add/modify 选错 ⇒ dev_patch", "")

    dev_case = D.classify(
        review={"verdict": "rework_dev"},
        patch_blockers=["add：anchor 在原文里找不到"],
        patch_failures=[{"status": "anchor_not_found"}],
    )
    check(dev_case["owner"] == D.OWNER_DEV and dev_case["owner_class"] == D.OWNER_DEV_PATCH
          and dev_case["recover_stage"] == "dev",
          "纯 DEV 补丁错误 ⇒ 归开发、回开发",
          f"{dev_case['owner']}/{dev_case['owner_class']}")

    tgt_case = D.classify(
        review={"verdict": "rework_dev"},
        patch_blockers=["save：原文里没有这个符号"],
        patch_failures=[{"status": "symbol_not_found", "symbol_planned": True}],
    )
    check(tgt_case["owner"] == D.OWNER_ARCHITECT
          and tgt_case["owner_class"] == D.OWNER_COMPILER_TARGET
          and tgt_case["recover_stage"] == "dev" and tgt_case["owner_mismatch"],
          "虚靶 ⇒ 归因指向方案层，但首轮路由仍按既有规则走（显式标 mismatch）",
          f"{tgt_case['owner']}/{tgt_case['recover_stage']}")

    rt_case = D.classify(
        review={"verdict": "rework_dev"},
        patch_blockers=["未核对：没有提供仓库路径"],
        patch_failures=[{"status": "unchecked", "notes": ["没有提供仓库路径，无法核对"]}],
    )
    check(rt_case["owner"] == D.OWNER_PATCH_RUNTIME
          and rt_case["owner_class"] == D.OWNER_PATCH_RUNTIME,
          "仓库快照缺失 ⇒ 责任主体=运行时基础设施（不归 DEV）",
          f"{rt_case['owner']}/{rt_case['owner_class']}")

    print("== 8. Recovery Policy：Escalation by evidence（不是按轮数） ==")
    # 实现层：同缺陷+同证据连续 2 轮 + 交付无变化 ⇒ 停止打回 DEV，转方案复查
    vrows = [{"path": "cli.py", "symbol": "add", "what": "导入失败", "status": "red",
              "source": "verify", "kind": "verify", "command": "python -m unittest"}]
    vl1 = D.ledger(None, rows=vrows, round_no=1)
    vl2 = D.ledger(vl1, rows=vrows, round_no=2)
    check(next(iter(vl2["open"].values())).get("evidence_streak") == 2, "构造：同证据连续 2 轮", "")
    esc_plan = D.classify(
        review={"verdict": "rework_dev"},
        mechanical_blockers=["运行验证失败：退出码 1"],
        open_defects=vl2["open"], impl_unchanged=True,
    )
    check(esc_plan["recovery"]["action"] == "escalate_plan"
          and esc_plan["recover_stage"] == "architect_plan" and not esc_plan["needs_human"],
          "同缺陷同证据连失 2 轮且补丁无变化 ⇒ 停止打回 DEV，转方案复查",
          str(esc_plan["recovery"]))
    changed = D.classify(
        review={"verdict": "rework_dev"},
        mechanical_blockers=["运行验证失败：退出码 1"],
        open_defects=vl2["open"], impl_unchanged=False,
    )
    check(changed["recovery"]["action"] == "retry_owner" and changed["recover_stage"] == "dev",
          "交付**有变化** ⇒ 不升级（开发确实在动，再给一轮）", str(changed["recovery"]))
    early = D.classify(
        review={"verdict": "rework_dev"},
        mechanical_blockers=["运行验证失败：退出码 1"],
        open_defects=vl1["open"], impl_unchanged=True,
    )
    check(early["recovery"]["action"] == "retry_owner",
          "只连续 1 轮 ⇒ 不升级（阈值=2，避免偶发失败触发方案返工）", str(early["recovery"]))

    # 方案层：同类归因连续 2 轮 ⇒ 架构师自查也没解决，交人工
    esc_arch = D.classify(
        review={"verdict": "rework_dev"},
        unresolved=[{"symbol": "db.insert", "reason": "成员不在基准里"}],
        prior_types=[D.CONTRACT_UNRESOLVED],
    )
    check(esc_arch["recovery"]["action"] == "escalate_human"
          and esc_arch["needs_human"] and esc_arch["stop"] == "recovery_escalation"
          and esc_arch["recover_stage"] == "done",
          "契约虚依赖连续 2 轮 ⇒ 升级人工（不再 dev↔architect 空转）",
          str(esc_arch["recovery"]))

    # 运行时：基础设施故障连续 2 轮 ⇒ 停止 Agent
    esc_rt = D.classify(
        review={"verdict": "rework_dev"},
        patch_blockers=["未核对：没有提供仓库路径"],
        patch_failures=[{"status": "unchecked", "notes": ["没有提供仓库路径，无法核对"]}],
        prior_types=[D.PATCH_UNAPPLIABLE],
    )
    check(esc_rt["recovery"]["action"] == "escalate_human" and esc_rt["needs_human"],
          "物化/仓库快照故障连续 2 轮 ⇒ 停止 Agent，按流水线错误交人工",
          str(esc_rt["recovery"]))

    # 护栏优先级高于 Recovery：触顶/护栏已决定交人工时，Recovery 不重复记账
    guard_first = D.classify(
        review={"verdict": "rework_dev"},
        mechanical_blockers=["x"], guard_stop="停滞",
        open_defects=vl2["open"], impl_unchanged=True,
    )
    check(guard_first["stop"] == "guard_stop"
          and guard_first["recovery"]["action"] == "retry_owner",
          "护栏已触发 ⇒ Recovery 不再叠加裁决（去向只记一条因果）", guard_first["stop"])

    rlines = "\n".join(D.render(esc_plan))
    check("Recovery Policy" in rlines and "方案复查" in rlines,
          "Recovery 裁决有独立日志行", rlines.replace("\n", " ")[:160])

    print(f"\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
