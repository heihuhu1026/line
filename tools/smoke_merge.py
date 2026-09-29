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

import copy
import json
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

    print("== ③b 部分覆盖：只删被重写的那个类，文件头与别的类都要留住 ==")
    # 为什么单独锁这一条：③ 只查了"Food 还在"。真正的风险是**两个方向**都错 ——
    # 既不能把 Food 丢掉，也不能因为保留了整块而让 Snake 出现两份（重复定义会写残），
    # 同时整文件补丁头部的 import 不能跟着被删（否则保留下来的类缺依赖）。
    prev = {"edits": [{"path": "game.py", "change_type": "add", "patch_mode": "full_symbol",
                       "patch": "import os\n\n\nclass Snake:\n    pass\n\n\nclass Food:\n    pass\n",
                       "covers_tasks": ["T-01"]}]}
    cur = {"edits": [{"path": "game.py", "change_type": "add", "patch_mode": "full_symbol",
                      "patch": "class Snake:\n    def eat(self):\n        return os.getcwd()\n",
                      "covers_tasks": ["T-01"]}]}
    out = merge(prev, cur)
    joined = "\n".join(str(e.get("patch")) for e in out["edits"])
    check("Food" in joined, "漏掉的 Food 保住了", joined[:160])
    check(joined.count("class Snake") == 1,
          "被本轮重写的 Snake 只有一份（不重复定义）", str(joined.count("class Snake")))
    check("import os" in joined, "整文件补丁头部的 import 跟着保留（保留的类不缺依赖）",
          joined[:160])
    check("def eat" in joined, "用的是**本轮**那版 Snake（不是旧的）", joined[:160])

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
    # uses 必须翻译成明确的 import 语句；入口文件强制守卫（真机 run 20260928-200631：
    # 分图模式下 main.py 五轮不写 import / 不带 __main__ 守卫）
    t_entry = {
        "id": "T-01", "title": "接线入口", "target_files": ["main.py"],
        "symbols": ["main"], "contracts": {"uses": ["cli.add", "cli.remove"]},
    }
    blk = P.task_focus_block(t_entry)
    check("from cli import add, remove" in blk, "uses 翻成文件头必须写的 import 语句", blk[:200])
    check("可执行入口文件" in blk and "__main__" in blk, "main.py 被强制要求入口守卫")
    t_lib = {"id": "T-02", "title": "存储", "target_files": ["db.py"],
             "contracts": {"uses": []}}
    check("__main__" not in P.task_focus_block(t_lib), "非入口文件不要求守卫")

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
    ]}, "verify_report": {}}
    o.fixes = ["[文件 cli.py] 修复参数解析"]
    hit = [t["id"] for t in o._tasks_for_bugfix()]
    check(hit == ["T-02"], "返工只重做受影响的 T-02（最小改动由调度保证）", str(hit))
    # 存档过期时也必须能归因（真机 20260927-192001：state 上的 affected 是空的，
    # 但按当前 fixes 重算明明有内容 —— 读过期存档会让"只重做受影响任务"整个失效）
    o.state = {"plan": {"tasks": [
        {"id": "T-01", "target_files": ["db.py"]},
        {"id": "T-02", "target_files": ["cli.py"]},
    ]}, "bug_report": {"affected": {}}, "verify_report": {}}
    o.fixes = ["[文件 cli.py] 修复语法错误"]
    hit = [t["id"] for t in o._tasks_for_bugfix()]
    check(hit == ["T-02"], "**存档过期也能归因**（改用当前 fixes 重算）", str(hit))
    o.fixes = []

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

        # ⑦ exposes 带空括号 / interface 写成「实例方法调用形态」不许假报
        # （真机 run 20260928-200631：exposes=["insert()"] 五轮恒定报"找不到定义"，
        # "Database().insert(a, b)" 被截成 Database/2 参数，db.py 一直正确却每轮 8 条假缺陷）
        wfile("db2.py", "class Database:\n"
                        "    def insert(self, amount, note):\n"
                        "        return 1\n")
        plan_paren = {"tasks": [
            {"id": "T-01", "target_files": ["db2.py"],
             "contracts": {"exposes": ["Database", "insert()"]},
             "interface": "Database().insert(amount: float, note: str) -> None"},
        ]}
        r = V.contract_check(w, ["db2.py"], plan_paren)
        check(r["problems"] == [],
              "exposes 剥空括号、构造调用形态取最后方法名（self 由比对侧扣除）", str(r["problems"]))
        check(V._interface_of("Database().insert(amount: float, note: str) -> None")
              == ("insert", ["amount: float", "note: str"]),
              "接口解析：实例方法调用形态")
        check(V._interface_of("main() -> int") == ("main", []), "接口解析：无参方法")

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
    joined_bug = "\n".join(bug)
    check("写一个命令行记账工具" not in joined_bug, "返工轮同样不含需求原文")
    check("PM 背景" not in joined_bug and "impact_areas" not in joined_bug,
          "返工轮的验收标准只取 acceptance_criteria（不把 PM 背景/影响面带回来）")
    # **方案必须在场** —— 这是此前修错的一处：早先版本把 plan 整份去掉了，理由是
    # "跨文件接口由 api_digest_block 提供"，那是把**接口摘要**当成了**施工图**。
    # 返工比首轮更离不开施工图：不按 task 分派时连 task_focus_block 也不在场，
    # 就变成"无图纸按缺陷单改"。
    marked = {"strategy": "策略标记", "changes": [{"path": "cli.py", "intent": "意图标记"}],
              "tasks": [{"id": "T-01", "title": "标题标记", "target_files": ["cli.py"]}]}
    bug2 = P.parts_dev(req, scope, {}, marked, "", ["修 bug"], verify={"commands": []},
                       current_code="def f(): pass", bug_report_block="【缺陷单】", bugfix=True)
    joined_bug2 = "\n".join(bug2)
    check("策略标记" in joined_bug2 and "意图标记" in joined_bug2 and "标题标记" in joined_bug2,
          "**返工轮带方案**（施工图）：strategy / changes / tasks 都在")
    check(joined_bug2.startswith("【架构师变更方案"),
          "方案排在**最前面**（顺序即优先级；首轮也是这么排的）", joined_bug2[:36])
    bug3 = P.parts_dev(req, scope, {}, marked, "", ["修 bug"], verify={"commands": []},
                       current_code="def f(): pass", bug_report_block="【缺陷单】", bugfix=True,
                       include_plan=False)
    check("策略标记" not in "\n".join(bug3),
          "按 task 分派时返工轮同样**不重复**喂整份方案（施工图已单独给出）")
    check("【缺陷单】" in joined_bug2, "缺陷单仍然在场（本轮范围的权威）")

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
    # 边界里声明了符号 ⇒ symbols 才算必填（没声明的文件不要求它凭空造）
    o3.state = {"plan": {
        "changes": [{"path": "ledger.py", "symbols": ["f"]}, {"path": "cli.py", "symbols": ["g"]}],
        "tasks": [
            {"id": "T-01", "target_files": ["ledger.py"]},
            {"id": "T-02", "target_files": ["cli.py"]},
        ]}}
    g = o3._plan_contract_gaps()
    # 三必填：symbols / test_hint / change（change = 「改什么」，DEV 最需要的一条）
    check(len(g) == 6, "两张图全空 ⇒ 6 项缺失（各缺 symbols + test_hint + change）", str(g))
    check("T-01 缺 symbols" in g and "T-02 缺 test_hint" in g and "T-01 缺 change" in g,
          "点名到任务与字段", str(g))
    # 字段齐全
    o3.state = {"plan": {"tasks": [
        {"id": "T-01", "symbols": ["f"], "test_hint": "python -c x", "change": "新增 f()"}
    ]}}
    check(o3._plan_contract_gaps() == [], "字段齐全 ⇒ 无缺失")
    # 只缺一个字段
    o3.state = {"plan": {"tasks": [
        {"id": "T-01", "symbols": ["f"], "change": "新增 f()"}
    ]}}
    check(o3._plan_contract_gaps() == ["T-01 缺 test_hint"], "只缺 test_hint ⇒ 精确点名")
    # interface/contracts 不强制（避免小模型编造）
    o3.state = {"plan": {"tasks": [
        {"id": "T-01", "symbols": ["f"], "test_hint": "x", "change": "新增 f()"}
    ]}}
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

    print("== ⑯ 返修替换（_apply_repair）：键够细 + 修复不许被丢 ==")
    # 真机 20260927-150931 的 mock 回放暴露的缺陷：原先只用 (path, 符号) 当键，
    # 同一符号上的 `full_symbol`（整符号替换）与 `replace_span`（只改签名）会被**并成一条**，
    # 静默丢掉其中一处改动 —— 连问题清单里的 `patch_incomplete` 都跟着消失（少一条真问题）。
    repair = Orchestrator._apply_repair

    def _edit(symbol, mode, anchor, patch, path="mod.py"):
        return {"path": path, "change_type": "modify", "target_symbol": symbol,
                "patch_mode": mode, "anchor": anchor, "patch": patch}

    merged = {"edits": [
        _edit("index_all", "full_symbol", "def index_all(conn):", "def index_all(conn):\n    return {}\n"),
        _edit("index_all", "replace_span", "def index_all(conn):", "def index_all(conn):\n"),
    ]}
    again = {"edits": [
        # 同一处改动的"修正版"：anchor 抄全了签名，精确键认不出，但必须替换掉旧的那条
        _edit("index_all", "replace_span", "def index_all(conn, roots, batch=300):",
              "def index_all(conn, roots, batch=300):\n"),
    ]}
    out = repair(json.loads(json.dumps(merged)), again)
    modes = [(e.get("target_symbol"), e.get("patch_mode"), e.get("anchor")) for e in out["edits"]]
    check(len(out["edits"]) == 2, "同符号的两条补丁不被并成一条（full_symbol 保留）", str(modes))
    check(any(m[1] == "full_symbol" for m in modes),
          "整符号替换那条**没被吞掉**（否则 patch_incomplete 这类真问题会消失）", str(modes))
    check(any(m[2] == "def index_all(conn, roots, batch=300):" for m in modes),
          "修正版（anchor 变了）认领成功、替换掉旧的那条", str(modes))
    check(not any(m[2] == "def index_all(conn):" and m[1] == "replace_span" for m in modes),
          "旧的那条 replace_span 已被替换（不会新旧并存）", str(modes))

    # 重出给了整符号替换 ⇒ 该符号上其它分片补丁一并去掉（留着套用后会有两份定义）
    merged2 = {"edits": [
        _edit("build", "replace_span", "def build(a):", "def build(a):\n"),
        _edit("build", "replace_span", "def build_extra():", "def build_extra():\n"),
    ]}
    again2 = {"edits": [_edit("build", "full_symbol", "def build(a, b):", "def build(a, b):\n    pass\n")]}
    out2 = repair(json.loads(json.dumps(merged2)), again2)
    check(len(out2["edits"]) == 1 and out2["edits"][0]["patch_mode"] == "full_symbol",
          "整符号替换取代该符号上的分片补丁（防两份定义）",
          str([(e.get("patch_mode"), e.get("anchor")) for e in out2["edits"]]))

    # 重出新增的锚点（旧实现里没有）⇒ 必须留下，丢掉等于白问一次
    merged3 = {"edits": [_edit("run", "replace_span", "def run():", "def run():\n")]}
    again3 = {"edits": [_edit("helper", "insert_after", "def run():", "def helper():\n    pass\n")]}
    out3 = repair(json.loads(json.dumps(merged3)), again3)
    symbols = {e.get("target_symbol") for e in out3["edits"]}
    check(symbols == {"run", "helper"}, "重出新增的补丁被追加（不静默丢弃）", str(sorted(symbols)))

    # 精确键命中 ⇒ 直接替换（既有行为不许退化）
    merged4 = {"edits": [_edit("run", "replace_span", "def run():", "def run():\n    return 1\n")]}
    again4 = {"edits": [_edit("run", "replace_span", "def run():", "def run():\n    return 2\n")]}
    out4 = repair(json.loads(json.dumps(merged4)), again4)
    check(len(out4["edits"]) == 1 and "return 2" in out4["edits"][0]["patch"],
          "同一处改动的修正 ⇒ 替换（不是并存）", str(out4["edits"]))

    print("== ⑯ 施工图太薄时兜底给整份方案（防 dev 在真空里施工） ==")
    thin = Orchestrator._task_drawing_is_thin
    check(thin({}) is True, "空 task ⇒ 判定为薄")
    check(thin({"id": "T-01", "title": "x", "target_files": ["a.py"]}) is True,
          "只有 id/title/文件清单 ⇒ 仍算薄（真机 150931 就是这种）")
    check(thin({"id": "T-01", "symbols": ["f"]}) is False, "有 symbols ⇒ 够厚")
    check(thin({"id": "T-01", "test_hint": "python -c 1"}) is False, "有 test_hint ⇒ 够厚")
    check(thin({"id": "T-01", "contracts": {"uses": ["g"]}}) is False, "有 contracts ⇒ 够厚")

    print("== ⑰ Task Compiler（确定性拆任务） ==")
    from pipeline import taskcompiler as TC
    plan = {"changes": [
        {"path": "ledger/db.py", "intent": "存取记录", "approach": "用 sqlite3 建表与增删查",
         "symbols": ["init_db", "insert", "list_all", "delete"]},
        {"path": "cli/parser.py", "intent": "解析子命令", "approach": "用 argparse 子命令",
         "symbols": ["build_parser", "parse_args", "add", "list_cmd", "remove"]},
        {"path": "main.py", "intent": "入口", "approach": "串联解析与存储"},
    ]}
    t1 = TC.compile_tasks(plan)
    t2 = TC.compile_tasks(plan)
    check(t1 == t2, "**确定性**：同一输入两次生成完全一致")
    check(len(t1) == 4, f"4 符号一组 / 5 符号切两张 + main 一张 = 4 张（实际 {len(t1)}）", str([t["id"] for t in t1]))
    # 符号切分：5 个符号必须切成两张，单张不超上限
    parser_tasks = [t for t in t1 if "cli/parser.py" in t["target_files"]]
    check(len(parser_tasks) == 2, "5 个符号 ⇒ 切成 2 张图", str(len(parser_tasks)))
    check(all(len(t["symbols"]) <= TC.MAX_SYMBOLS_PER_TASK for t in t1),
          "每张图的符号数都不超上限")
    # 无 symbols ⇒ 整文件一张
    check(any(t["target_files"] == ["main.py"] and not t["symbols"] for t in t1),
          "没声明符号的文件 ⇒ 整文件一张")
    # 字段齐全（这正是架构师填不出的部分）
    check(all(t.get("symbols") or not t.get("symbols") for t in t1), "结构正常")
    check(all(str(t.get("change") or "").strip() for t in t1), "每张图都有 change（改什么）")
    check(all(str(t.get("test_hint") or "").strip() for t in t1), "每张图都有 test_hint（机械生成）")
    check(all(str(t.get("acceptance") or "").strip() for t in t1), "每张图都有 acceptance")
    # 单文件不会被多张图之外的图覆盖
    per_file = {}
    for t in t1:
        for f in t["target_files"]:
            per_file.setdefault(f, []).append(t["id"])
    check(all(len(v) == 1 for f, v in per_file.items() if f == "main.py"),
          "main.py 只由一张图负责")
    # **不再人为串成线**：没有真实依赖的跨文件施工图 depends_on 必须为空
    # （旧实现无条件把每张图挂到前一张上，抵消了 DAG 的并行/裁剪价值）。
    by_file = {t["target_files"][0]: t for t in t1}
    check(by_file["ledger/db.py"]["depends_on"] == []
          and parser_tasks[0]["depends_on"] == []
          and by_file["main.py"]["depends_on"] == [],
          "无声明/契约依赖 ⇒ depends_on 为空（不人为串联）",
          str([(t["target_files"], t["depends_on"]) for t in t1]))
    # 同文件多图保留顺序边：后者在前者的成果上继续（同一产物文件，顺序即依赖）
    parser_ids = [t["id"] for t in parser_tasks]
    check(parser_tasks[1]["depends_on"] == [parser_ids[0]],
          "同文件两张图：第二张依赖第一张", str([t["depends_on"] for t in parser_tasks]))

    # 编译后应通过字段强制校验
    o4 = Orchestrator.__new__(Orchestrator)
    o4.state = {"plan": {"changes": plan["changes"], "tasks": t1}}
    check(o4._plan_contract_gaps() == [], "编译出的 tasks **通过**字段强制校验",
          str(o4._plan_contract_gaps()))

    # 什么时候该由编译器接管
    good = {"changes": plan["changes"], "tasks": [
        {"id": "T-01", "symbols": ["f"], "change": "新增 f", "test_hint": "python -c x",
         "target_files": ["a.py"]}]}
    check(TC.plan_needs_compile(good) == [], "合格的 tasks ⇒ 保留架构师自己的（不覆盖）")
    check(TC.plan_needs_compile({"changes": []}) == ["方案没有 tasks"], "没有 tasks ⇒ 接管")
    bad = {"changes": plan["changes"], "tasks": [{"id": "T-01", "target_files": ["a.py"]}]}
    check(bool(TC.plan_needs_compile(bad)), "缺字段 ⇒ 接管", str(TC.plan_needs_compile(bad)))
    many = {"changes": plan["changes"], "tasks": [
        {"id": f"T-{i:02d}", "symbols": ["f"], "change": "x", "test_hint": "y",
         "target_files": ["a.py"]} for i in range(1, 8)]}
    check(any("超过上限" in r for r in TC.plan_needs_compile(many)), "超任务数 ⇒ 接管")

    print("== ⑱ 单文件覆盖上限（真机 192001 的教训） ==")
    from pipeline import taskcompiler as TC
    from pipeline.config import MAX_TASKS_PER_FILE
    # 同一文件被 3 张图覆盖 ⇒ 必须接管
    covered3 = {"changes": [{"path": "a.py", "symbols": ["f", "g", "h"]}],
                "tasks": [{"id": "T-01", "target_files": ["a.py"], "symbols": ["f"],
                           "change": "x", "test_hint": "y"},
                          {"id": "T-02", "target_files": ["a.py"], "symbols": ["g"],
                           "change": "x", "test_hint": "y"},
                          {"id": "T-03", "target_files": ["a.py"], "symbols": ["h"],
                           "change": "x", "test_hint": "y"}]}
    rs = TC.plan_needs_compile(covered3)
    check(any("被 3 张图覆盖" in r for r in rs),
          "同一文件被 3 张图覆盖 ⇒ 编译器接管", str(rs))
    # 编译结果：单文件图数不超上限
    compiled = TC.compile_tasks({"changes": [{"path": "a.py", "symbols": ["f", "g", "h", "i", "j"]}]})
    per_file: dict[str, int] = {}
    for t in compiled:
        for f in (t.get("target_files") or []):
            per_file[f] = per_file.get(f, 0) + 1
    check(all(v <= MAX_TASKS_PER_FILE for v in per_file.values()),
          f"编译结果：单文件图数 ≤ {MAX_TASKS_PER_FILE}", str(per_file))
    check(len(compiled) <= MAX_TASKS_PER_FILE, "5 个符号 ⇒ 4/1 两张，不超单文件上限",
          str(len(compiled)))

    # 容量硬约束（旧 _split_groups 的真实 bug：9/10 个符号被切成 5 个一张）
    over = {"changes": [{"path": "a.py",
                         "symbols": ["f", "g", "h", "i", "j", "k", "l", "m", "n"]}]}
    res = TC.compile_plan(over)
    check(all(len(t["symbols"]) <= TC.MAX_SYMBOLS_PER_TASK for t in res["tasks"]),
          "9 个符号 ⇒ **绝不**产出 5 个符号一张的图（单图硬上限）",
          str([len(t["symbols"]) for t in res["tasks"]]))
    cap_errors = [e for e in res["errors"] if e.get("code") == "task_capacity_exceeded"]
    check(len(cap_errors) == 1 and cap_errors[0]["file"] == "a.py"
          and cap_errors[0]["tasks_needed"] == 3,
          "9 个符号需 3 张图 > 单文件上限 2 ⇒ 显式 task_capacity_exceeded（不靠放宽消化）",
          str(res["errors"]))
    res10 = TC.compile_plan(
        {"changes": [{"path": "b.py", "symbols": [f"s{i}" for i in range(10)]}]}
    )
    check(all(len(t["symbols"]) <= 4 for t in res10["tasks"])
          and any(e.get("code") == "task_capacity_exceeded" for e in res10["errors"]),
          "10 个符号同样硬切 4/4/2 + 容量错误",
          str([len(t["symbols"]) for t in res10["tasks"]]))

    print("== ⑲ 执行 DAG：真实依赖边（contract/import）+ 三项机械校验 ==")
    from pipeline import planir as IR
    # 契约 uses 跨文件 ⇒ 文件级边；声明顺序相反时 Kahn 仍把被依赖方排前面
    dag_plan = {"changes": [
        {"path": "cli.py", "symbols": ["run"]},
        {"path": "db.py", "symbols": ["insert"]},
    ], "tasks": [
        {"id": "T-01", "target_files": ["cli.py"], "symbols": ["run"],
         "contracts": {"uses": ["db.insert"], "exposes": []}},
        {"id": "T-02", "target_files": ["db.py"], "symbols": ["insert"],
         "contracts": {"uses": [], "exposes": []}},
    ]}
    dag_ir = IR.normalize_plan(dag_plan)
    dag_res = TC.compile_plan(dag_plan, ir=dag_ir)
    dag_order = [(t["target_files"][0], t["depends_on"]) for t in dag_res["tasks"]]
    check(dag_res["tasks"][0]["target_files"] == ["db.py"]
          and dag_res["tasks"][1]["depends_on"] == [dag_res["tasks"][0]["id"]],
          "contracts.uses 建文件级边 + Kahn 拓扑序（db 先于 cli）", str(dag_order))
    check(dag_res["errors"] == [], "合法 DAG 无编译错误", str(dag_res["errors"]))
    # 二开现存 import 图：不写契约也能从 AST 机械推出边（含传递序 main→cli→db）
    import_edges = IR.existing_import_edges(
        ["db.py", "cli.py", "main.py"],
        {"db.py": "import os\n", "cli.py": "import db\n", "main.py": "from cli import run\n"},
    )
    check(import_edges == {"cli.py": ["db.py"], "main.py": ["cli.py"]},
          "AST 提取现存 import 边（stdlib/方案外不建边）", str(import_edges))
    imp_plan = {"changes": [
        {"path": "main.py", "symbols": ["main"]},
        {"path": "cli.py", "symbols": ["run"]},
        {"path": "db.py", "symbols": ["insert"]},
    ]}
    imp_ir = IR.normalize_plan(imp_plan, file_imports=import_edges)
    imp_res = TC.compile_plan(imp_plan, ir=imp_ir)
    check([t["target_files"][0] for t in imp_res["tasks"]] == ["db.py", "cli.py", "main.py"],
          "import 边驱动的传递拓扑序", str([(t["id"], t["target_files"]) for t in imp_res["tasks"]]))
    # 未知依赖目标 ⇒ unknown_dependency（旧实现静默丢弃）
    unk_ir = {"units": [
        {"file": "a.py", "symbols": ["a"], "depends_on": ["T-99"],
         "source_task_ids": ["T-01"], "stable_id": "a.py::a"},
    ]}
    unk = TC.compile_plan(None, ir=unk_ir)
    unk_err = [e for e in unk["errors"] if e["code"] == "unknown_dependency"]
    check(len(unk_err) == 1 and unk_err[0]["dep"] == "T-99"
          and "a.py" in unk_err[0]["declared_by"],
          "依赖不存在的任务 id ⇒ unknown_dependency 且点名声明方", str(unk["errors"]))
    # 自依赖（draft 显式声明自己）⇒ self_dependency
    self_ir = {"units": [
        {"file": "a.py", "symbols": ["a"], "depends_on": ["T-01"], "self_depends": ["T-01"],
         "source_task_ids": ["T-01"], "stable_id": "a.py::a"},
    ]}
    self_res = TC.compile_plan(None, ir=self_ir)
    check([e["code"] for e in self_res["errors"]] == ["self_dependency"]
          and all("T-01" not in t["depends_on"] for t in self_res["tasks"]),
          "自依赖 ⇒ self_dependency，且不造出幻影边", str(self_res["errors"]))
    # 合并副产物（T-02/T-03 同文件、T-03 声明依赖 T-02）**不是**自依赖错误，静默滤掉
    merge_plan = {"changes": [{"path": "a.py", "symbols": ["x", "y", "z", "q", "r"]}],
                  "tasks": [
        {"id": "T-02", "target_files": ["a.py"], "symbols": ["x", "y"]},
        {"id": "T-03", "target_files": ["a.py"], "symbols": ["z", "q", "r"],
         "depends_on": ["T-02"]},
    ]}
    merge_ir = IR.normalize_plan(merge_plan)
    merge_res = TC.compile_plan(merge_plan, ir=merge_ir)
    check(not [e for e in merge_res["errors"] if e["code"] == "self_dependency"],
          "同文件合并导致的机械自引用 ⇒ 不报 self_dependency", str(merge_res["errors"]))
    # 环 ⇒ dependency_cycle，且给出一条具体环（不只给节点集合）
    cyc_ir = {"units": [
        {"file": "a.py", "symbols": ["a"], "depends_on": ["T-02"],
         "source_task_ids": ["T-01"], "stable_id": "a.py::a"},
        {"file": "b.py", "symbols": ["b"], "depends_on": ["T-03"],
         "source_task_ids": ["T-02"], "stable_id": "b.py::b"},
        {"file": "c.py", "symbols": ["c"], "depends_on": ["T-01"],
         "source_task_ids": ["T-03"], "stable_id": "c.py::c"},
    ]}
    cyc = TC.compile_plan(None, ir=cyc_ir)
    cyc_err = [e for e in cyc["errors"] if e["code"] == "dependency_cycle"]
    check(len(cyc_err) == 1 and len(cyc_err[0].get("cycle") or []) >= 3
          and cyc_err[0]["cycle"][0] == cyc_err[0]["cycle"][-1],
          "三环 ⇒ dependency_cycle 且 cycle 为首尾闭合的具体路径", str(cyc_err))
    check(len(cyc["tasks"]) == 3, "环上施工图仍照常切出（阻断由 Design Gate 决定，不偷丢图）")
    # 真机 20260928-160609：只报环路径模型无法打断 —— 每条边要带来源、可操作指引与原图编号对照
    ce = cyc_err[0]
    _edges = ce.get("cycle_edges") or []
    check(len(_edges) >= 2 and all(e.get("sources") for e in _edges),
          "环错误逐边标注来源（cycle_edges 非空且每条边有 sources）", str(_edges))
    check(any("方案声明" in s for e in _edges for s in e["sources"]),
          "draft 边标注为「方案声明（…depends_on…）」", str(_edges))
    check("depends_on" in ce["detail"] and "打断" in ce["detail"],
          "环 detail 给出可操作的打断指引", ce["detail"][:200])
    check(all(tid in ce.get("source_tasks", {}) for tid in ce["tasks"]),
          "环上编译编号能对回模型原图任务 id（source_tasks）", str(ce.get("source_tasks")))
    # 真机 160609 第三轮原形态：两文件互相声明依赖（+ uses 推出的文件级边，来源应都列出）
    mut_ir = {"units": [
        {"file": "commands.py", "symbols": ["CLI.add"], "depends_on": ["T-03"],
         "depends_on_files": ["database.py"], "source_task_ids": ["T-02"],
         "stable_id": "commands.py::CLI.add"},
        {"file": "database.py", "symbols": ["Database.insert"], "depends_on": ["T-02"],
         "source_task_ids": ["T-03"], "stable_id": "database.py::Database.insert"},
    ]}
    mut = TC.compile_plan(None, ir=mut_ir)
    me = [e for e in mut["errors"] if e["code"] == "dependency_cycle"]
    check(len(me) == 1 and "T-02" in str(me[0].get("cycle_edges"))
          and any("跨文件引用推出" in s for e in me[0]["cycle_edges"] for s in e["sources"])
          and any("方案声明" in s for e in me[0]["cycle_edges"] for s in e["sources"]),
          "互依环：draft 边与 uses 文件级边分别标注（真机 160609 形态）",
          json.dumps(me[0].get("cycle_edges"), ensure_ascii=False))

    print("== ⑳ 定位失败的补丁不进累积实现（真机 20260927-221511 的恒定判负） ==")
    # 真机链：dev 重问把 anchor 写成 `self.db.add_entry(amount, note)`，而实际代码是
    # `self.db.add_entry(args.amount)` —— **凭记忆改写的近似行**。`apply_all` 只收
    # status=="ok"，这条永远进不了沙箱，却一直挂在实现里当"交付物不完整"的阻断项，
    # 且下一轮只会再写一条同样对不上的 ⇒ 一个修不掉的门永远挂在流水线前面。
    from pipeline import patches as PT
    import tempfile as _tf

    with _tf.TemporaryDirectory() as td:
        (Path(td) / "cli.py").write_text(
            "class CLI:\n    def add(self):\n        self.db.add_entry(args.amount)\n",
            encoding="utf-8",
        )
        impl = {"edits": [
            {"path": "cli.py", "change_type": "modify", "patch_mode": "replace_span",
             "target_symbol": "CLI", "anchor": "self.db.add_entry(amount, note)", "patch": "x"},
            {"path": "new.py", "change_type": "add", "patch_mode": "full_symbol",
             "target_symbol": "G", "anchor": "", "patch": "def g():\n    pass\n"},
        ]}
        audit = PT.analyze_all(td, impl)
        check(audit["problems"] == 1 and audit["ok"] == 1,
              "近似 anchor 判 anchor_not_found（判定本身是对的）", str(audit["problem_detail"]))
        rep = PT.prune_unappliable(td, impl)
        check(rep["dropped"] == 1 and len(impl["edits"]) == 1,
              "**定位失败的那条被移除**，能套用的保留", str(rep["detail"]))
        check(PT.analyze_all(td, impl)["problems"] == 0,
              "裁剪后审计干净（不再有每轮都复现的恒定阻断项）")
        check("cli.py" in rep["detail"][0] and "anchor" in rep["detail"][0],
              "裁剪记录带路径与原因（不静默）", rep["detail"][0])
        # `unchecked`（文件不在仓库）**不许裁** —— 那是 §26 的"文件始终没落盘"，
        # 必须继续当阻断项暴露出来。
        impl2 = {"edits": [{"path": "ghost.py", "change_type": "modify", "patch_mode": "replace_span",
                            "target_symbol": "G", "anchor": "def g():", "patch": "x"}]}
        rep2 = PT.prune_unappliable(td, impl2)
        check(rep2["dropped"] == 0 and len(impl2["edits"]) == 1,
              "`unchecked`（目标文件不在仓库）**不许裁**：那是必须继续阻断的真问题", str(rep2))
        check(PT.prune_unappliable(None, {"edits": []})["dropped"] == 0, "空实现不崩")

    print(f"\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
