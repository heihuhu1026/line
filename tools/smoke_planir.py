"""Plan IR（`pipeline/planir.py`）与 Symbol Resolver（`pipeline/symbols.py`）的离线冒烟。

锁的是「编译层」的确定性 —— 它一旦不确定，上游再稳也白搭：

  ① **最长匹配**：`CLI` 与 `CLI.add` 并存 ⇒ 删父节点（分段前缀，`add`≠`add_all` 的前缀）
  ② **多图合并**：多张 draft 图改同一文件 ⇒ 合并成一个 unit，但**来源必须保留**（可追溯）
  ③ **确定性**：同一输入（含 dict 顺序不同）必得逐字节相同的 IR
  ④ **符号归位**：一张图覆盖多文件时，由 resolver 决定每个符号属于哪个文件
  ⑤ **虚依赖暴露**：`database.insert_record`（模块在、成员不在）必须判 unresolved，不许猜
  ⑥ 契约版本 / plan_sources / 指纹
  ⑦ Compiler **只读 IR**：施工图字段透传、稳定身份、依赖翻译
  ⑧ 两版 IR 的差异（历史可解释）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import planir, symbols, taskcompiler  # noqa: E402

PASS = FAIL = 0


def check(cond: bool, name: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  <- {extra}" if extra else ""))


#: 接口基准摘要（`verify.skeleton_digest` 的真实形态：path → 行文本，缩进即语义）
SKELETON = {
    "cli.py": ["class CLI", "    def add(amount, note)", "    def list_all()"],
    "database.py": [
        "class Database",
        "    实例属性: connection",
        "    def create_table()",
        "    def save_record(amount, note)",
        "    def get_all_records()",
    ],
    "main.py": ["def main(args)"],
}


def _plan() -> dict:
    """真机 192001 的形态（脱敏）：changes 无符号、draft 图带契约。"""
    return {
        "strategy": "纯标准库 + sqlite3",
        "changes": [
            {"path": "main.py", "intent": "程序入口", "approach": "argparse 分发"},
            {"path": "database.py", "intent": "持久化", "approach": "sqlite3 建表与增删查"},
            {
                "path": "cli.py",
                "intent": "命令处理",
                "approach": "add/list/remove 三个处理函数",
                "symbols": ["CLI.add", "CLI.list_all"],
            },
        ],
        "tasks": [
            {
                "id": "T-01",
                "title": "数据库",
                "target_files": ["database.py"],
                "symbols": ["Database.save_record"],
                "change": "实现插入记录",
                "acceptance": "插入后能查出来",
                "contracts": {"exposes": ["Database.save_record"]},
                "test_hint": 'python -c "import database"',
            },
            {
                "id": "T-02",
                "title": "CLI",
                "target_files": ["cli.py"],
                "symbols": ["CLI.add"],
                "change": "实现 add 子命令",
                "acceptance": "add 100.5 早餐 成功",
                # 真机的**虚依赖**：`database.py` 里定义的是 `Database.save_record`
                "contracts": {"uses": ["database.insert_record"]},
                "interface": "add(amount: str, note: str) -> None",
                "data_model": "ledger(id, amount, note)",
                "constraints": ["仅标准库"],
                "depends_on": ["T-01"],
                "test_hint": 'python -c "import cli"',
            },
            {
                "id": "T-03",
                "title": "CLI 列表",
                "target_files": ["cli.py"],
                "symbols": ["CLI.list_all"],
                "change": "实现 list 子命令",
                "acceptance": "list 输出三行",
                "depends_on": ["T-02"],
            },
        ],
    }


def main() -> int:
    print("== ① 最长匹配原则（分段前缀，不是字面前缀） ==")
    keep, dropped = planir.drop_parent_symbols(["CLI", "CLI.add", "CLI.list_all", "validate"])
    check(keep == ["CLI.add", "CLI.list_all", "validate"], "父节点 CLI 被删掉", str(keep))
    check(len(dropped) == 1 and dropped[0]["symbol"] == "CLI"
          and "CLI.add" in dropped[0]["covered_by"], "删除动作被记录（不静默丢输入）", str(dropped))
    keep2, dropped2 = planir.drop_parent_symbols(["add", "add_all"])
    check(keep2 == ["add", "add_all"] and not dropped2,
          "`add` **不是** `add_all` 的前缀（字面 startswith 会误删）", str(keep2))
    check(planir.drop_parent_symbols([]) == ([], []), "空输入不崩")

    print("== ② 多图合并到同一文件：来源必须保留 ==")
    ir = planir.normalize_plan(_plan(), skeleton=SKELETON)
    cli = [u for u in ir["units"] if u["file"] == "cli.py"]
    check(len(cli) == 1, "cli.py 只有一个 unit（T-02/T-03 已合并）", str(len(cli)))
    if cli:
        unit = cli[0]
        check(sorted(unit["symbols"]) == ["CLI.add", "CLI.list_all"],
              "两张图的符号都并进了这个 unit", str(unit["symbols"]))
        check(sorted(unit["source_task_ids"]) == ["T-02", "T-03"],
              "来源任务号保留", str(unit["source_task_ids"]))
        acc_sources = {a.get("source") for a in unit["acceptance"]}
        check({"T-02", "T-03"} <= acc_sources,
              "每条验收要求**带来源**（出问题能追溯是谁提的）", str(acc_sources))
        check(unit["interface"] == "add(amount: str, note: str) -> None",
              "施工图字段（interface）汇入 unit", str(unit["interface"]))
    kinds = {c["kind"] for c in ir["conflicts"]}
    check("multi_task_same_file" in kinds, "合并这件事被显式记录", str(kinds))

    print("== ③ 确定性：同一输入必得逐字节相同的 IR ==")
    shuffled = _plan()
    shuffled["tasks"] = list(reversed(shuffled["tasks"]))
    ir_a = planir.normalize_plan(_plan(), skeleton=SKELETON)
    ir_b = planir.normalize_plan(shuffled, skeleton=SKELETON)
    same = json.dumps(ir_a, ensure_ascii=False, sort_keys=False) == json.dumps(
        ir_b, ensure_ascii=False, sort_keys=False
    )
    check(same, "draft 图顺序不同 ⇒ IR 逐字节相同（不依赖 dict 插入顺序）")
    check(planir.normalize_plan(_plan(), skeleton=SKELETON) == ir_a, "重复调用结果稳定")
    files_order = [u["file"] for u in ir["units"]]
    check(files_order == ["main.py", "database.py", "cli.py"],
          "unit 顺序跟随 changes 的声明顺序", str(files_order))

    print("== ④ 符号归位：由 resolver 决定符号属于哪个文件 ==")
    multi = {
        "changes": [{"path": "a.py"}, {"path": "b.py"}],
        "tasks": [{"id": "T-01", "target_files": ["a.py", "b.py"],
                   "symbols": ["a.run", "b.stop"], "change": "x"}],
    }
    ir2 = planir.normalize_plan(multi, skeleton={})
    by_file = {u["file"]: u["symbols"] for u in ir2["units"]}
    check(by_file.get("a.py") == ["run"] and by_file.get("b.py") == ["stop"],
          "`a.run` 归 a.py、`b.stop` 归 b.py（不再一概塞进第一个文件）", str(by_file))
    ambiguous = {
        "changes": [{"path": "a.py"}, {"path": "b.py"}],
        "tasks": [{"id": "T-01", "target_files": ["a.py", "b.py"],
                   "symbols": ["ghost_symbol"], "change": "x"}],
    }
    ir3 = planir.normalize_plan(ambiguous, skeleton={})
    check(any(c["kind"] == "symbol_target_ambiguous" for c in ir3["conflicts"]),
          "定位不了就**记冲突**，不静默乱塞", str([c["kind"] for c in ir3["conflicts"]]))

    print("== ⑤ 虚依赖必须暴露（真机 database.insert_record） ==")
    db = [u for u in ir["units"] if u["file"] == "cli.py"][0]
    gaps = db["unresolved"]
    check(len(gaps) == 1, "`database.insert_record` 判 unresolved（模块在、成员不在）", str(gaps))
    check(bool(gaps) and "insert_record" in str(gaps[0].get("symbol")),
          "点名到具体符号", str(gaps))
    check(bool(gaps) and "不在接口基准里" in str(gaps[0].get("reason")),
          "给出可操作原因（命名可能不一致）", str(gaps))
    check(any(w.get("kind") == "unresolved_dependency" for w in ir["warnings"]),
          "IR 顶层 warnings 里同样暴露（下游能一次拿到全部）", str(ir["warnings"]))
    check(db["depends_on_files"] == [], "虚依赖**不建边**（不连到不存在的符号上）",
          str(db["depends_on_files"]))
    # 真机 20260927-214253 的教训：虚引用留在施工图里，dev 会去"实现"它 —— 重出补丁时把
    # anchor 写成 `DBManager.insert()` 这种调用表达式，4 条 anchor_not_found、首轮即停人工。
    check(db["contracts"]["uses"] == [],
          "解析不了的引用**不再进契约**（否则 dev 会去实现它）", str(db["contracts"]["uses"]))
    check(db["unresolved_uses"] == ["database.insert_record"],
          "转入 unresolved_uses（不丢，交给评审/人工判方案返工）", str(db["unresolved_uses"]))
    cli_task_ir = [t for t in taskcompiler.compile_tasks(_plan(), ir=ir)
                   if t["target_files"][0] == "cli.py"][0]
    check(cli_task_ir.get("unresolved_uses") == ["database.insert_record"],
          "施工图带上 unresolved_uses（供禁止项渲染）", str(cli_task_ir.get("unresolved_uses")))
    from pipeline import prompts as P  # noqa: PLC0415

    focus = P.task_focus_block(cli_task_ir)
    check("不要去理" in focus and "database.insert_record" in focus,
          "施工图**显式叫停**（留白不等于安全，必须明说禁止）", focus[-160:])
    check("不要去理" not in P.task_focus_block({"id": "T-01", "target_files": ["a.py"]}),
          "没有虚引用时不出现该块")
    # 不唯一同样不许猜
    dup = symbols.build_index(
        ["x.py", "y.py"], {"x.py": ["def save()"], "y.py": ["def save()"]}
    )
    r = symbols.resolve("save", files=[], index=dup)
    check(r["resolved"] is False and "多个候选" in r["reason"],
          "同名符号多处定义 ⇒ 不建边、不猜", str(r))
    # 基准缺失时不许假警报
    empty = symbols.build_index(["x.py"], {})
    r2 = symbols.resolve("x.whatever", files=["x.py"], index=empty)
    check(r2["resolved"] is True,
          "基准里没有成员信息时**不算错**（否则会全是假警报）", str(r2))

    print("== ⑤b 文件限定形式（真机 134222 六个文件全用 `main.py:Game` 这种写法） ==")
    idx_ff = symbols.build_index(["cli.py", "database.py"], SKELETON)
    r3 = symbols.resolve("cli.py:CLI.add", files=[], index=idx_ff)
    check(r3["resolved"] and r3["candidates"] == ["cli.py"] and r3["kind"] == "file",
          "`cli.py:CLI.add` 认成「文件:符号」（不被切成模块 cli + `py:...`）", str(r3))
    r3b = symbols.resolve("cli.py:CLI.ghost", files=[], index=idx_ff)
    check(not r3b["resolved"] and "不在接口基准里" in r3b["reason"],
          "文件形式下成员不存在 ⇒ 同样判虚依赖", str(r3b))
    r5 = symbols.resolve("ghost.py:Thing", files=[], index=idx_ff)
    check(not r5["resolved"] and "找不到" in r5["reason"], "文件不存在 ⇒ 明确说找不到", str(r5))
    # 第二种书写：点号把路径和符号串起来（真机 150931 的 `game.py.Game`）
    r3c = symbols.resolve("cli.py.CLI.add", files=[], index=idx_ff)
    check(r3c["resolved"] and r3c["candidates"] == ["cli.py"],
          "`cli.py.CLI.add`（点号式）同样认成文件限定，不被切成模块 cli + py.CLI.add", str(r3c))
    r6 = symbols.resolve(",", files=["cli.py"], index=idx_ff)
    check(not r6["resolved"] and "非法符号" in r6["reason"],
          "单个逗号 ⇒ 判非法、不进依赖图（真机 173023 就是这个）", str(r6))
    # 真机 20260928-160609：架构师把 uses 写成 `db.py的add_record()`（中文「的」限定）。
    # 旧逻辑 stem 前缀吃到扩展名的点，错切成成员 `py的add_record`，对基准必然判虚依赖 ——
    # 该 run 第一轮 7 条阻断全是这种假冲突，白耗一次自纠。
    r_cn = symbols.resolve("database.py的save_record(amount, note)", files=[], index=idx_ff)
    check(r_cn["resolved"] and r_cn["candidates"] == ["database.py"] and r_cn["kind"] == "file",
          "`database.py的save_record()` 认成文件限定成员（中文「的」等价于点号）", str(r_cn))
    r_cn_bad = symbols.resolve("database.py的ghost", files=[], index=idx_ff)
    check(not r_cn_bad["resolved"] and "ghost" in r_cn_bad["reason"]
          and "py的" not in r_cn_bad["reason"],
          "中文限定下成员不存在 ⇒ 判虚依赖且原因里是真实成员名（不再错切成 py的…）",
          str(r_cn_bad))
    r_dot_stem = symbols.resolve("database.save_record", files=[], index=idx_ff)
    check(r_dot_stem["resolved"] and r_dot_stem["candidates"] == ["database.py"],
          "stem 点号式 `database.save_record` 不回归（扩展名点号豁免只挡文件全名形态）",
          str(r_dot_stem))
    ir_cn = planir.normalize_plan(
        {
            "changes": [{"path": "x.py"}, {"path": "y.py"}],
            "tasks": [{"id": "T-01", "target_files": ["x.py"], "symbols": ["run"],
                       "contracts": {"uses": ["y.py的Y.go()"]}, "change": "x"}],
        },
        skeleton={"y.py": ["class Y", "    def go()"]},
    )
    ux_cn = [u for u in ir_cn["units"] if u["file"] == "x.py"][0]
    check(ux_cn["depends_on_files"] == ["y.py"] and not ux_cn["unresolved"],
          "中文「的」形态端到端真能建出依赖边（否则假阻断会把方案打回返工）",
          str(ux_cn["depends_on_files"]) + str(ux_cn["unresolved"]))
    ir_ff = planir.normalize_plan(
        {
            "changes": [{"path": "x.py"}, {"path": "y.py"}],
            "tasks": [{"id": "T-01", "target_files": ["x.py"], "symbols": ["run"],
                       "contracts": {"uses": ["y.py:Y.go"]}, "change": "x"}],
        },
        skeleton={"y.py": ["class Y", "    def go()"]},
    )
    ux = [u for u in ir_ff["units"] if u["file"] == "x.py"][0]
    check(ux["depends_on_files"] == ["y.py"] and not ux["unresolved"],
          "文件限定形式真能建出依赖边（跨文件依赖图的前提）",
          str(ux["depends_on_files"]) + str(ux["unresolved"]))

    print("== ⑤c 文件级依赖与外部依赖（真机 150931：uses 全是 `game.py` 与 `tkinter`） ==")
    idx_fl = symbols.build_index(["game.py", "snake.py"], {"game.py": ["class Game"]})
    r7 = symbols.resolve("game.py", files=["snake.py"], index=idx_fl)
    check(r7["resolved"] and r7["kind"] == "file" and r7["candidates"] == ["game.py"],
          "整个符号就是文件路径 ⇒ 文件级依赖（按文件建边，不当虚依赖）", str(r7))
    r10 = symbols.resolve("game.py.Game", files=["snake.py"], index=idx_fl)
    check(r10["resolved"] and r10["candidates"] == ["game.py"],
          "点号式文件限定同样认（不被切成模块 game + `py.Game`）", str(r10))
    r8 = symbols.resolve("tkinter", files=["game.py"], index=idx_fl)
    r9 = symbols.resolve("tkinter.event", files=["game.py"], index=idx_fl)
    check(r8["resolved"] and r8["kind"] == "external" and r9["resolved"] and r9["kind"] == "external",
          "标准库 ⇒ 外部依赖（**不算虚依赖**，避免稀释真正要修的那几条）",
          f"{r8.get('kind')}/{r9.get('kind')}")
    ir_ext = planir.normalize_plan(
        {"changes": [{"path": "z.py"}],
         "tasks": [{"id": "T-01", "target_files": ["z.py"], "symbols": ["run"],
                    "contracts": {"uses": ["tkinter", "sqlite3.connect"]}, "change": "x"}]},
        skeleton={},
    )
    uz = ir_ext["units"][0]
    check(sorted(uz["externals"]) == ["sqlite3.connect", "tkinter"] and not uz["unresolved"],
          "外部依赖进 externals、**不进** unresolved", str(uz["externals"]) + str(uz["unresolved"]))

    print("== ⑤d 调用写法归一（真机 20260927-214253：uses **全是** `Xxx.method()`） ==")
    check(symbols.clean_symbol("DBManager.insert()") == "DBManager.insert", "去掉尾部调用括号")
    check(symbols.clean_symbol("db.insert(amount, note) -> int") == "db.insert",
          "去掉括号与返回类型注解")
    check(symbols.clean_symbol("game.py:Game()") == "game.py:Game", "文件限定形式同样剥干净")
    check(symbols.clean_symbol("  plain  ") == "plain", "首尾空白")
    idx_cl = symbols.build_index(["cli.py"], SKELETON)
    r11 = symbols.resolve("CLI.add(amount, note)", files=[], index=idx_cl)
    check(r11["resolved"] and r11["candidates"] == ["cli.py"],
          "带参数的调用写法也能解析（否则能对上的引用会被判成虚依赖，淹没真问题）", str(r11))
    r12 = symbols.resolve("sqlite3.connect()", files=["cli.py"], index=idx_cl)
    check(r12["resolved"] and r12["kind"] == "external", "`sqlite3.connect()` 仍归外部依赖", str(r12))
    # 真机 20260927-224002：架构师把 uses 写成 `sqlite3.connect():`，尾部冒号是**排版残留**。
    # 不剥掉它，解析器会把 `sqlite3.connect()` 当成文件名 ⇒ 5 条合法依赖被误报成虚依赖。
    check(symbols.clean_symbol("sqlite3.connect():") == "sqlite3.connect",
          "尾部冒号（排版残留）被剥掉", repr(symbols.clean_symbol("sqlite3.connect():")))
    r13 = symbols.resolve("sqlite3.connect():", files=["cli.py"], index=idx_cl)
    check(r13["resolved"] and r13["kind"] == "external",
          "`sqlite3.connect():` 归外部依赖，**不是**虚依赖（误报会稀释信号）", str(r13))
    check(symbols.clean_symbol("main.py:Game") == "main.py:Game",
          "**内部**冒号（真分隔符）不剥", repr(symbols.clean_symbol("main.py:Game")))
    check(symbols.clean_symbol("main.py:") == "main.py", "尾部冒号的纯文件路径 ⇒ 归文件级依赖")

    print("== ⑥ Compile Contract：版本与来源 ==")
    check(ir["compiler_input_version"] == planir.COMPILER_INPUT_VERSION,
          "IR 自带输入契约版本（跨版本比较才成立）", str(ir["compiler_input_version"]))
    check(ir["plan_sources"] == {"changes": True, "draft_tasks": True},
          "plan_sources 说明本轮吃了哪些源", str(ir["plan_sources"]))
    only_changes = planir.normalize_plan({"changes": [{"path": "a.py"}]}, skeleton={})
    check(only_changes["plan_sources"] == {"changes": True, "draft_tasks": False},
          "只有 changes 时如实标注（双源是过渡形态，必须可见）",
          str(only_changes["plan_sources"]))
    fp = planir.fingerprint()
    check(len(str(fp.get("compiler_hash"))) == 12
          and fp.get("compiler_input_version") == planir.COMPILER_INPUT_VERSION,
          "指纹含 compiler_hash（规则改了必然变）", str(fp))

    print("== ⑥b 字符串字段与书写归一（真机 110402 的两个内容 bug） ==")
    # bug 1：字符串被当可迭代对象**逐字拆开** —— dev 收到的约束成了「必/须/使/用/标/准/库」。
    # bug 2：`changes[].symbols` 带括号、task 那路是裸名 ⇒ 同一符号在清单里出现两次
    #        （`main` 与 `main()` 并存），提示词里就成了"要定义两个东西"。
    str_plan = {
        "changes": [{"path": "main.py", "symbols": ["main()", "parse_args()"]}],
        "tasks": [
            {
                "id": "T-01",
                "target_files": ["main.py"],
                "symbols": "main()",
                "constraints": "必须使用标准库，禁止引入第三方依赖",
                "depends_on": "T-00",
                "contracts": {"uses": "database.add_entry"},
            }
        ],
    }
    ir_str = planir.normalize_plan(str_plan, skeleton={})
    ustr = ir_str["units"][0]
    check(ustr["constraints"] == ["必须使用标准库，禁止引入第三方依赖"],
          "**字符串约束当单项**（逐字拆开会让它彻底失效）", str(ustr["constraints"]))
    check(ustr["depends_on"] == ["T-00"], "字符串 depends_on 同样当单项", str(ustr["depends_on"]))
    check(ustr["symbols"] == ["main", "parse_args"],
          "**两种书写归一为一项**（`main()` 与 `main` 不能并存）", str(ustr["symbols"]))
    check(ustr["contracts"]["uses"] == [], "落不了地的契约仍被过滤（字符串形态也一样）")
    check(planir._as_list("x") == ["x"] and planir._as_list(None) == []
          and planir._as_list({"a": 1}) == [1]
          and planir._as_list(x for x in (1, 2)) == [1, 2],
          "`_as_list` 覆盖 字符串/None/dict/生成器（生成器曾被我漏掉 ⇒ 符号清单整个变空）")

    print("== ⑦ Compiler 只读 IR ==")
    tasks = taskcompiler.compile_tasks(_plan(), ir=ir)
    by_target = {t["target_files"][0]: t for t in tasks}
    check([t["id"] for t in tasks] == ["T-01", "T-02", "T-03"],
          "编号跟随 IR 的 unit 顺序（main → database → cli）", str([t["id"] for t in tasks]))
    check(all(t.get("stable_id") for t in tasks),
          "每张图都有**稳定身份**（不随编号漂移）", str([t.get("stable_id") for t in tasks]))
    cli_task = by_target.get("cli.py") or {}
    check(all(t["id"] not in (t.get("depends_on") or []) for t in tasks),
          "**没有任何自引用依赖**（两图合并成一张时最容易出现）",
          str([(t["id"], t.get("depends_on")) for t in tasks]))
    check(cli_task.get("depends_on") == ["T-02"],
          "依赖翻译成新编号，且指向 database 那张图", str(cli_task.get("depends_on")))
    check(cli_task.get("interface") == "add(amount: str, note: str) -> None"
          and cli_task.get("data_model") and cli_task.get("constraints") == ["仅标准库"],
          "施工图字段**透传给 dev**（编译器不得丢信息）", str(sorted(cli_task.keys())))
    check(cli_task.get("test_hint") == 'python -c "import cli"',
          "test_hint 用方案里那条（不是机械兜底那条）", str(cli_task.get("test_hint")))
    check(sorted(cli_task.get("symbols") or []) == ["CLI.add", "CLI.list_all"],
          "类的点号前缀**保留**（区分同名方法、也是锚点依据）", str(cli_task.get("symbols")))
    all_acc = " ".join(str(t.get("acceptance")) for t in tasks)
    check("（来源 T-" in all_acc, "验收文本带来源（可追溯是谁提的要求）", all_acc[:140])
    check(all(len(t["symbols"]) <= taskcompiler.MAX_SYMBOLS_PER_TASK for t in tasks),
          "单张图符号数不超上限")
    check(tasks == taskcompiler.compile_tasks(_plan(), ir=ir), "编译同样确定性")
    # 兼容路径：没有 IR 时仍能工作（旧调用点/单测）
    legacy = taskcompiler.compile_tasks({"changes": [{"path": "z.py", "symbols": ["f", "g"]}]})
    check(len(legacy) == 1 and legacy[0]["target_files"] == ["z.py"],
          "没有 IR 时退回读 changes（兼容路径不破）", str(legacy))

    print("== ⑧ 两版 IR 的差异（历史可解释） ==")
    fewer = _plan()
    fewer["tasks"] = [t for t in fewer["tasks"] if t["id"] != "T-03"]
    fewer["changes"] = [c for c in fewer["changes"] if c["path"] != "cli.py"] + [
        {"path": "cli.py", "intent": "命令处理", "approach": "只剩 add", "symbols": ["CLI.add"]}
    ]
    ir_next = planir.normalize_plan(fewer, skeleton=SKELETON, previous=ir)
    diff = ir_next.get("diff_vs_previous") or {}
    check(diff.get("removed_units") == [], "cli.py 仍存在 ⇒ 不算删除", str(diff))
    ir_gone = planir.normalize_plan(
        {"changes": [{"path": "main.py", "intent": "入口"}], "tasks": []},
        skeleton=SKELETON, previous=ir,
    )
    gone = (ir_gone.get("diff_vs_previous") or {}).get("removed_units") or []
    check(len(gone) == 2, "消失的施工图被点名（removed_units）", str(gone))

    print(f"\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
