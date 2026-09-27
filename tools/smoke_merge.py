"""跨轮合并语义（``Orchestrator._merge_impl_across_rounds``）的离线冒烟。

这是「按 task 分派」的**硬前提**：按 task 分派之后，同一个文件被多个任务改是常态。
若合并逻辑把「不同任务改同一文件」当成覆盖，后一个任务会静默吃掉前一个任务的改动 ——
而且丢得无声无息，等到 verify 才表现为"符号没了"，那时已经烧掉好几轮。

锁定的三条语义：
  ① 不同任务 → **并存**（依次套用）
  ② 同一任务重做 → **替换**（返工的修正是替换，不是追加）
  ③ add 块按**符号并集**累积（防"越改越少"，既有行为不许退化）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.orchestrator import Orchestrator  # noqa: E402

merge = Orchestrator._merge_impl_across_rounds

PASS = FAIL = 0


def check(cond: bool, name: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  <- {extra}" if extra else ""))


def mod(path, symbol, task, patch="    # 改动"):
    return {
        "path": path,
        "change_type": "modify",
        "patch_mode": "replace_span",
        "target_symbol": symbol,
        "patch": patch,
        "covers_tasks": [task],
    }


def main() -> int:
    print("== ① 不同任务改同一文件 ⇒ 并存 ==")
    prev = {"edits": [mod("cli.py", "CLI.add", "T-01", "    # T-01 的改动")]}
    cur = {"edits": [mod("cli.py", "CLI.add", "T-02", "    # T-02 的改动")]}
    out = merge(prev, cur)
    bodies = [str(e.get("patch")) for e in out["edits"]]
    check(len(out["edits"]) == 2, "两条改动都保留（不再互相覆盖）", str(len(out["edits"])))
    check(any("T-01" in b for b in bodies) and any("T-02" in b for b in bodies),
          "两个任务的改动内容都在", str(bodies))

    print("== ② 同一任务重做 ⇒ 替换（不叠加） ==")
    prev = {"edits": [mod("cli.py", "CLI.add", "T-01", "    # 旧写法（有 bug）")]}
    cur = {"edits": [mod("cli.py", "CLI.add", "T-01", "    # 新写法（修好了）")]}
    out = merge(prev, cur)
    bodies = [str(e.get("patch")) for e in out["edits"]]
    check(len(out["edits"]) == 1, "只剩一条，不是两条叠加", str(len(out["edits"])))
    check(bodies == ["    # 新写法（修好了）"], "保留的是**修正后**的那一版", str(bodies))

    print("== ③ add 块符号并集（既有行为不许退化） ==")
    prev = {"edits": [{"path": "game.py", "change_type": "add", "patch_mode": "full_symbol",
                       "patch": "class Snake:\n    pass\nclass Food:\n    pass\n",
                       "covers_tasks": ["T-01"]}]}
    cur = {"edits": [{"path": "game.py", "change_type": "add", "patch_mode": "full_symbol",
                      "patch": "class Snake:\n    pass\n",   # 本轮漏了 Food
                      "covers_tasks": ["T-01"]}]}
    out = merge(prev, cur)
    joined = "\n".join(str(e.get("patch")) for e in out["edits"])
    check("Food" in joined, "本轮漏掉的 Food 仍被保留（防越改越少）", joined[:120])

    print("== ④ 不同任务改同一文件的**不同符号** ⇒ 本来就并存 ==")
    prev = {"edits": [mod("cli.py", "CLI.add", "T-01")]}
    cur = {"edits": [mod("cli.py", "CLI.list", "T-02")]}
    out = merge(prev, cur)
    check(len(out["edits"]) == 2, "不同符号两条并存", str(len(out["edits"])))

    print("== ⑤ delete 优先：本轮删掉的文件，旧改动作废 ==")
    prev = {"edits": [mod("old.py", "f", "T-01"), {"path": "keep.py", "change_type": "add",
                                                   "patch": "def g():\n    pass\n", "covers_tasks": ["T-02"]}]}
    cur = {"edits": [{"path": "old.py", "change_type": "delete", "covers_tasks": ["T-03"]}]}
    out = merge(prev, cur)
    paths = {str(e.get("path")) for e in out["edits"]}
    check("old.py" not in paths or all(str(e.get("change_type")) == "delete" for e in out["edits"]
                                       if e.get("path") == "old.py"), "被删文件的旧改动已作废")
    check("keep.py" in paths, "未涉及的文件不受影响", str(paths))

    print("== ⑥ 施工图片段（按 task 分派时 dev 的唯一权威） ==")
    from pipeline import prompts as P
    task = {
        "id": "T-02", "title": "CLI 子命令", "target_files": ["cli.py"],
        "symbols": ["CLI.add", "CLI.list"],
        "interface": "add(args: list[str]) -> None",
        "contracts": {"uses": ["db.insert_record"], "exposes": ["CLI.add"]},
        "data_model": "ledger(id, amount, note)",
        "constraints": ["仅标准库"], "acceptance": ["add 后 list 能看到"],
        "test_hint": "python -c \"import cli\"", "depends_on": ["T-01"],
    }
    block = P.task_focus_block(task)
    check("T-02" in block and "cli.py" in block, "含任务号与目标文件")
    check("CLI.add" in block and "CLI.list" in block, "含要定义的符号清单")
    check("db.insert_record" in block, "含跨文件契约（依赖谁）")
    check("python -c" in block, "含可执行的验收命令")
    check("T-01" in block, "含前置任务")
    check("其他任务的文件一个都不要碰" in block, "明确禁止越界到别的任务")
    check(P.task_focus_block(None) == "" and P.task_focus_block({}) == "",
          "无施工图时不注入空块")

    print("== ⑦ 任务拓扑排序与返工只重做受影响任务 ==")
    o = Orchestrator.__new__(Orchestrator)
    o.state = {"plan": {"tasks": [
        {"id": "T-01", "target_files": ["db.py"]},
        {"id": "T-02", "target_files": ["cli.py"], "depends_on": ["T-01"]},
        {"id": "T-03", "target_files": ["main.py"], "depends_on": ["T-02"]},
    ]}}
    order = [t["id"] for t in o._ordered_plan_tasks()]
    check(order == ["T-01", "T-02", "T-03"], "按 depends_on 拓扑排序", str(order))
    # 乱序输入也要排对
    o.state = {"plan": {"tasks": [
        {"id": "T-03", "target_files": ["main.py"], "depends_on": ["T-02"]},
        {"id": "T-01", "target_files": ["db.py"]},
        {"id": "T-02", "target_files": ["cli.py"], "depends_on": ["T-01"]},
    ]}}
    order = [t["id"] for t in o._ordered_plan_tasks()]
    check(order == ["T-01", "T-02", "T-03"], "乱序输入同样排对", str(order))
    # 环不能死循环
    o.state = {"plan": {"tasks": [
        {"id": "T-01", "depends_on": ["T-02"]}, {"id": "T-02", "depends_on": ["T-01"]},
    ]}}
    check(len(o._ordered_plan_tasks()) == 2, "存在环时不死循环（全部接上）")
    # 返工只重做缺陷单指向的任务
    o.state = {"plan": {"tasks": [
        {"id": "T-01", "target_files": ["db.py"]},
        {"id": "T-02", "target_files": ["cli.py"]},
        {"id": "T-03", "target_files": ["main.py"]},
    ]}, "bug_report": {"affected": {"cli.py": []}}}
    hit = [t["id"] for t in o._tasks_for_bugfix()]
    check(hit == ["T-02"], "返工只重做受影响的 T-02（最小改动由调度保证）", str(hit))

    print("== ⑧ 施工图机械自检（漏定义符号的判定） ==")
    gaps = Orchestrator._task_symbol_gaps
    t = {"id": "T-02", "symbols": ["CLI.add", "CLI.list"]}
    d_full = {"edits": [{"path": "cli.py", "patch": "class CLI:\n    def add(self): ...\n    def list(self): ...\n"}]}
    d_part = {"edits": [{"path": "cli.py", "patch": "class CLI:\n    def add(self): ...\n"}]}
    d_none = {"edits": []}
    check(gaps(t, d_full) == [], "符号都写了 ⇒ 无缺口", str(gaps(t, d_full)))
    check(gaps(t, d_part) == ["CLI.list"], "漏了 list ⇒ 精确指出", str(gaps(t, d_part)))
    check(gaps(t, d_none) == ["CLI.add", "CLI.list"], "一条补丁都没有 ⇒ 全部算漏")
    check(gaps({"id": "T-02"}, d_none) == [], "施工图没声明 symbols ⇒ 不判（不冤枉它）")
    check(gaps(t, None) == ["CLI.add", "CLI.list"], "产物为空也不崩")

    print("== ⑨ 跨文件契约比对（聚合验证的静态核心） ==")
    import tempfile
    from pipeline import verify as V

    with tempfile.TemporaryDirectory() as td:
        w = Path(td)

        def wfile(name, text):
            (w / name).write_text(text, encoding="utf-8")

        # 全部对得上的情况
        wfile("db.py", "def insert_record(amount, note):\n    return 1\n")
        wfile("cli.py", "class CLI:\n    def add(self, args):\n        return db.insert_record(1, 'x')\n")
        plan = {"tasks": [
            {"id": "T-01", "target_files": ["db.py"],
             "contracts": {"exposes": ["insert_record"]},
             "interface": "insert_record(amount: float, note: str) -> int"},
            {"id": "T-02", "target_files": ["cli.py"],
             "contracts": {"exposes": ["CLI.add"], "uses": ["insert_record"]},
             "interface": "add(args: list[str]) -> None"},
        ]}
        r = V.contract_check(w, ["db.py", "cli.py"], plan)
        check(r["problems"] == [], "接口全部对得上 ⇒ 无问题", str(r["problems"]))
        check(r["checked"] >= 4, f"核对了 {r['checked']} 条声明")

        # ① 声明提供却没写
        plan_bad = {"tasks": [{"id": "T-01", "target_files": ["db.py"],
                               "contracts": {"exposes": ["ghost_func"]}}]}
        r = V.contract_check(w, ["db.py"], plan_bad)
        check(len(r["problems"]) == 1 and "ghost_func" in r["problems"][0],
              "声明要提供却不存在的符号 ⇒ 判出", str(r["problems"]))

        # ② 声明要用、目标侧没有（snake-v2 那一类）
        plan_bad = {"tasks": [{"id": "T-02", "target_files": ["cli.py"],
                               "contracts": {"uses": ["db.nonexistent"]}}]}
        r = V.contract_check(w, ["cli.py"], plan_bad)
        check(len(r["problems"]) == 1 and "跨文件接口对不上" in r["problems"][0],
              "依赖了不存在的接口 ⇒ 判出", str(r["problems"]))

        # ③ 签名不符
        plan_bad = {"tasks": [{"id": "T-01", "target_files": ["db.py"],
                               "interface": "insert_record(a, b, c) -> int"}]}
        r = V.contract_check(w, ["db.py"], plan_bad)
        check(len(r["problems"]) == 1 and "签名与方案不符" in r["problems"][0],
              "参数个数与声明不符 ⇒ 判出", str(r["problems"]))

        # ④ 归因：问题挂在哪个文件上
        r = V.contract_check(w, ["db.py"], {"tasks": [
            {"id": "T-01", "target_files": ["db.py"], "contracts": {"exposes": ["ghost"]}}]})
        check("db.py" in (r.get("by_file") or {}), "问题按文件归因（便于定位到哪张施工图）",
              str(r.get("by_file")))

        # ⑤ 边界：没有可解析文件 / 没有 tasks ⇒ 不误报
        check(V.contract_check(w, [], plan)["problems"] == [], "沙箱无文件 ⇒ 不误判（那是更前置的问题）")
        check(V.contract_check(w, ["db.py"], {})["problems"] == [], "方案无 tasks ⇒ 不判")
        # ⑥ 语法坏掉的文件不能让比对崩
        wfile("bad.py", "def broken(:\n")
        r = V.contract_check(w, ["bad.py"], {"tasks": [
            {"id": "T-01", "target_files": ["bad.py"], "contracts": {"exposes": ["x"]}}]})
        check(isinstance(r["problems"], list), "文件语法坏掉时比对不崩")

    print("== ⑩ dev 的输入：方案是唯一权威（不再喂需求原文与 PM 物料） ==")
    from pipeline import prompts as P
    req = "写一个命令行记账工具，纯 Python 标准库 + SQLite"
    scope = {"acceptance_criteria": ["三个子命令可用"], "background": "PM 背景", "impact_areas": ["x"]}
    plan = {"changes": [{"path": "cli.py"}], "tasks": [{"id": "T-01", "target_files": ["cli.py"]}]}
    parts = P.parts_dev(req, scope, {"forbidden": ["a.py"]}, plan, "", None,
                        verify={"commands": []}, current_code="def f(): pass")
    joined = "\n".join(parts)
    check("写一个命令行记账工具" not in joined, "不再含**需求原文**")
    check("产品经理范围说明" not in joined and "PM 背景" not in joined,
          "不再含 PM 范围说明与背景")
    check("架构师变更方案" in joined, "含方案（权威）")
    check("cli.py" in joined, "方案内容真的注入了")
    # 顺序即优先级：方案必须在最前
    first_upstream = min(
        (i for i, p in enumerate(parts) if "架构师变更方案" in p),
        default=99,
    )
    check(first_upstream == 0, "方案排在**最前面**（顺序即优先级）", str(first_upstream))
    # 禁改约束不能丢
    check("禁改路径" in joined, "禁改约束保留（约束≠需求，不能一起丢）")
    # 返工轮同样不含需求原文
    bug = P.parts_dev(req, scope, {}, plan, "", ["修 bug"], verify={"commands": []},
                      current_code="def f(): pass", bug_report_block="【缺陷单】", bugfix=True)
    check("写一个命令行记账工具" not in "\n".join(bug), "返工轮同样不含需求原文")

    print("== ⑪ 方案粒度与契约字段的机械判据（真机 20260927-002903 复现） ==")
    # 真机形态：3 个文件拆成 5 张图，cli.py 独占 3 张，且字段全空
    o2 = Orchestrator.__new__(Orchestrator)
    o2.project_type = "new"
    o2.state = {"plan": {"changes": [{"path": "ledger.py"}, {"path": "cli.py"}, {"path": "main.py"}],
                         "tasks": [{"id": "T-01", "target_files": ["ledger.py"]},
                                   {"id": "T-02", "target_files": ["cli.py"]},
                                   {"id": "T-03", "target_files": ["cli.py"]},
                                   {"id": "T-04", "target_files": ["cli.py"]},
                                   {"id": "T-05", "target_files": ["main.py"]}]}}
    try:
        audit = o2._audit_plan()
        ok = True
    except Exception as exc:  # noqa: BLE001
        audit, ok = {}, False
        check(False, "_audit_plan 可调用", f"{type(exc).__name__}: {exc}")
    if ok:
        cov = audit.get("over_covered_files") or []
        check(any("cli.py" in x and "3 张" in x for x in cov),
              "cli.py 被 3 张图覆盖 ⇒ 判「拆太碎」", str(cov))
        check(audit.get("over_split") is False,
              "旧的「总 task 数」判据确实抓不到它（对照组）")
        check(len(audit.get("contracts_missing") or []) == 5,
              "5 张图都没声明契约 ⇒ 全部点名", str(audit.get("contracts_missing")))
        check(len(audit.get("tasks_without_symbols") or []) == 5,
              "5 张图都没声明 symbols ⇒ 全部点名")

    # 契约无从比对 ≠ 都对得上
    r = V.contract_check(Path(tempfile.gettempdir()), [], {"tasks": [{"id": "T-01"}]})
    check(bool(r.get("unresolved")), "没有任何声明时明确记「无从比对」，不静默通过")
    with tempfile.TemporaryDirectory() as td2:
        (Path(td2) / "x.py").write_text("def f():\n    pass\n", encoding="utf-8")
        r2 = V.contract_check(Path(td2), ["x.py"], {"tasks": [{"id": "T-01"}]})
    check(bool(r2.get("unresolved")) and "没有声明" in "".join(r2.get("unresolved")),
          "有文件但没声明 ⇒ 同样记「无从比对」", str(r2.get("unresolved")))

    print("== ⑫ 架构师的输入：只吃 PM 的关键字段，不再吃需求原文 ==")
    from pipeline import prompts as P
    req = "写一个命令行记账工具，纯 Python 标准库 + SQLite"
    scope = {
        "goal": "一个命令行记账工具",
        "in_scope": ["add/list/remove 三个子命令"],
        "out_of_scope": ["Web 界面"],
        "acceptance_criteria": ["三个子命令可用"],
        "background": "PM 背景叙述",
        "impact_areas": [{"area": "数据库操作", "severity": "high"}],
        "functional_requirements": ["FR-01 add 子命令"],
        "risks": ["风险 X"],
        "confirmed_facts": ["金额是否允许负数：允许负数"],
        "open_questions": [{"question": "金额是否允许负数", "final_decision": "允许负数"}],
    }
    plan_parts = P.parts_plan(req, scope, None, "", None, None, None, None)
    joined = "\n".join(str(p) for p in plan_parts)
    check("写一个命令行记账工具" not in joined, "不再含**需求原文**")
    check("PM 背景叙述" not in joined and "风险 X" not in joined,
          "不再含 PM 的背景/风险（纯叙述）")
    check("数据库操作" in joined, "保留 impact_areas（模块划分的直接依据）")
    check("FR-01" in joined, "保留 functional_requirements（需求明细）")
    check("in_scope" in joined or "add/list/remove" in joined, "保留 in_scope（边界）")
    check("out_of_scope" in joined or "Web 界面" in joined, "保留 out_of_scope（边界）")
    check("三个子命令可用" in joined, "保留 acceptance_criteria（验收）")
    check("一个命令行记账工具" in joined, "保留 goal（一句话目标）")
    # 裁决类信息只能有一个来源：pm_assumptions_block 已渲染"问题 → 已裁决：结论"
    check("confirmed_facts" not in joined, "不再含 confirmed_facts（与假设块重复）")
    check("已裁决" in joined, "裁决结论仍通过 pm_assumptions_block 给出（只此一处）")

    print("== ⑬ 施工图必填字段的强制（symbols / test_hint） ==")
    o3 = Orchestrator.__new__(Orchestrator)
    # 全空（真机 20260927-002903 的形态）
    o3.state = {"plan": {"tasks": [
        {"id": "T-01", "target_files": ["ledger.py"]},
        {"id": "T-02", "target_files": ["cli.py"]},
    ]}}
    g = o3._plan_contract_gaps()
    check(len(g) == 4, "两张图全空 ⇒ 4 项缺失（各缺 symbols + test_hint）", str(g))
    check("T-01 缺 symbols" in g and "T-02 缺 test_hint" in g, "点名到任务与字段", str(g))
    # 只缺一个字段
    o3.state = {"plan": {"tasks": [{"id": "T-01", "symbols": ["f"], "test_hint": "python -c x"}]}}
    check(o3._plan_contract_gaps() == [], "字段齐全 ⇒ 无缺失")
    o3.state = {"plan": {"tasks": [{"id": "T-01", "symbols": ["f"]}]}}
    check(o3._plan_contract_gaps() == ["T-01 缺 test_hint"], "只缺 test_hint ⇒ 精确点名")
    # interface/contracts 不强制（避免小模型编造）
    o3.state = {"plan": {"tasks": [{"id": "T-01", "symbols": ["f"], "test_hint": "x"}]}}
    check(o3._plan_contract_gaps() == [], "interface/contracts 缺失**不**判负（只提示）")
    # 没有 tasks / 空产物不崩
    o3.state = {"plan": {}}
    check(o3._plan_contract_gaps() == [], "方案无 tasks ⇒ 不判")

    print("== ⑭ 按 task 分派时不再喂整份方案（只给那一张施工图） ==")
    from pipeline import prompts as P
    plan2 = {"changes": [{"path": "MARKER_A.py"}, {"path": "MARKER_B.py"}],
             "tasks": [{"id": "T-01", "target_files": ["MARKER_A.py"]},
                       {"id": "T-02", "target_files": ["MARKER_B.py"]}]}
    full = P.parts_dev("需求", {}, {}, plan2, "", None, verify={"commands": []},
                       current_code="def f(): pass")
    scoped = P.parts_dev("需求", {}, {}, plan2, "", None, verify={"commands": []},
                         current_code="def f(): pass", include_plan=False)
    check(any("MARKER_A" in str(p) for p in full), "默认（整批施工）仍给整份方案")
    check(not any("MARKER_" in str(p) for p in scoped),
          "分派模式**不含**整份方案（避免与施工图重复）")
    # 分派时施工图由 task_focus_block 单独提供，两者不重叠
    focus = P.task_focus_block({"id": "T-02", "target_files": ["MARKER_B.py"], "symbols": ["g"]})
    check("MARKER_B" in focus and "T-02" in focus, "施工图单独给出本张图的目标文件")

    print("== ⑮ 空片段不渲染 + 返工注入上一版方案 ==")
    from pipeline import prompts as P
    # 新建项目：assessment / excerpts / verify 全空，不该渲染空标题
    empty = P.parts_plan("需求", {"acceptance_criteria": ["x"]}, None, "", None, None, None, None)
    joined_empty = "\n".join(str(p) for p in empty)
    check("存量代码评估" not in joined_empty, "空的存量评估不再渲染标题")
    check("存量代码片段" not in joined_empty, "空的检索池不再渲染标题")
    # dev 同样：空 excerpts 不渲染标题
    dev_parts = P.parts_dev("需求", {}, {}, {"changes": []}, "", None, verify=None, current_code="")
    joined_dev = "\n".join(str(p) for p in dev_parts)
    # 只查那个空标题：dev 的新建项目警告里合法地含"没有提供任何存量代码片段"这句，不算
    check("【存量代码片段（按相关度挑选" not in joined_dev, "dev 的空检索池同样不渲染空标题")
    # 返工轮注入上一版方案
    prev = {"changes": [{"path": "ledger.py"}], "tasks": [{"id": "T-01", "target_files": ["ledger.py"]}]}
    rework = P.parts_plan("需求", {"acceptance_criteria": ["x"]}, None, "", ["修方案"], None, prev, None)
    joined_re = "\n".join(str(p) for p in rework)
    check("上一版方案" in joined_re and "ledger.py" in joined_re,
          "返工轮喂入上一版方案（差分修改的基础）")

    print(f"\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
