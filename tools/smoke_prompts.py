"""角色隔离（提示词层）的离线冒烟：不加载模型，秒级。

验的是**三类任务各自拿到自己的契约**，而且这件事是**机械可测**的：

  ① 三值任务类型与提示词表的键一一对应（两边同值由断言钉住，不靠约定）；
  ② dev / test / review 在返工轮都换口径，且未覆盖的阶段**刻意**回退首轮口径；
  ③ `parts_dev` 的三种视图与**尾部任务段**互斥 —— 这条专治"靠顺序去盖住首轮口径"
     （`_dev_rework_note` 盖 `【任务】按方案实现代码改动`）：顺序一变或被 fit_prompt
     裁掉就会漏回来，而漏回来的后果是"返工轮按首轮纪律整份重吐"（真机四轮零进展的成因）。
  ④ （P0-12）测试用例类别按 `round_kind` 聚焦：必需类别集合只放宽不收紧，
     且提示词与 `_audit_test` 共用 `tasktype` 里那一份真源。

为什么这些断言值得单独一套：它们锁的是**结构性风险**（两类任务共用一套契约），
而结构性风险不会在单测里冒出来，只会在真机上以"白烧一轮"的形式付费。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import prompts, schemas, tasktype  # noqa: E402

PASS = FAIL = 0

REQ = "做一个记账命令行工具"
PLAN = {
    "changes": [{"path": "cli.py", "symbols": ["CLI.add"], "intent": "加命令", "approach": "新增子命令"}],
    "tasks": [{"id": "T-01", "target_files": ["cli.py"], "symbols": ["CLI.add"], "change": "加命令"}],
}
SCOPE = {"acceptance_criteria": ["能加一条记录"]}
ASSESS = {"forbidden_paths": []}
EXCERPTS = "def add(amount):\n    pass"
CURRENT = "def add(amount):\n    return amount"


def check(cond: bool, name: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  <- {extra}" if extra else ""))


def dev_view(round_kind: str, **kw) -> str:
    parts = prompts.parts_dev(
        REQ, SCOPE, ASSESS, PLAN, EXCERPTS,
        fixes=["补 note 参数"], current_code=CURRENT,
        bug_report_block="【缺陷单】失败命令：`python -m cli` 退出码 1",
        round_kind=round_kind, **kw,
    )
    return "\n".join(p for p in parts if p)


def main() -> int:
    print("== 1. 任务类型三值与提示词表一一对应（不靠约定） ==")
    check(prompts.KIND_FEATURE == tasktype.FEATURE
          and prompts.KIND_BUGFIX == tasktype.BUGFIX
          and prompts.KIND_PLAN_REWORK == tasktype.PLAN_REWORK,
          "prompts 的字面量与 tasktype 的常量同值（两处定义不会静默漂移）")
    check(tasktype.ROUND_KINDS == (tasktype.FEATURE, tasktype.PLAN_REWORK, tasktype.BUGFIX),
          "任务类型集合显式声明（新增一档必须同时进 ROUND_KINDS）", str(tasktype.ROUND_KINDS))
    check(set(prompts.SYSTEM_NEW_ROUND) == {tasktype.BUGFIX, tasktype.PLAN_REWORK},
          "按任务类型的提示词表与 ROUND_KINDS 的非首轮档一致（feature 走 SYSTEM_NEW）",
          str(sorted(prompts.SYSTEM_NEW_ROUND)))
    check(tasktype.round_kind_label(tasktype.PLAN_REWORK) == "方案返工后施工"
          and tasktype.round_kind_label("nope") == "nope",
          "标签取不到时原样返回，不抛异常")

    print("== 2. 三类任务的系统提示词各自独立 ==")
    feat_dev = prompts.system_prompt("dev", "new", tasktype.FEATURE)
    bug_dev = prompts.system_prompt("dev", "new", tasktype.BUGFIX)
    plan_dev = prompts.system_prompt("dev", "new", tasktype.PLAN_REWORK)
    check(len({feat_dev, bug_dev, plan_dev}) == 3, "dev 三套文本互不相同")
    check("一律用" in feat_dev and "full_symbol" in feat_dev, "首轮：整份新建纪律")
    check("缺陷修复" in bug_dev and "最小补丁" in bug_dev, "返工：缺陷修复 + 最小补丁")
    check("方案层返工后的施工轮" in plan_dev and "有权创建" in plan_dev,
          "方案返工：允许创建方案新增的文件/符号（与修缺陷正相反）")
    check("其余文件划分一律不动" not in plan_dev,
          "**方案返工轮不再带最小改动措辞**（真机 L2：方案刚重做却按最小改动施工）")

    for stage in ("test", "review"):
        same = prompts.system_prompt(stage, "new", tasktype.BUGFIX) == prompts.system_prompt(
            stage, "new", tasktype.FEATURE
        )
        check(not same, f"返工轮 {stage} 不与首轮同口径（否则会提首轮级要求，与 dev 冲突）")
    test_bug = prompts.system_prompt("test", "new", tasktype.BUGFIX)
    rev_bug = prompts.system_prompt("review", "new", tasktype.BUGFIX)
    check("回归验证" in test_bug and "缺陷是否关闭" in test_bug, "返工轮 test：只做回归证明")
    check("不得重新提出" in rev_bug, "返工轮 review：明写不得重提首轮取舍（自指循环的燃料）")
    check(prompts.system_prompt("test", "new", tasktype.PLAN_REWORK)
          == prompts.system_prompt("test", "new", tasktype.FEATURE),
          "方案返工轮的 test **刻意**按首轮口径（方案重做后就该按首轮标准验收）")

    print("== 2b. P0-12：测试类别按任务类型聚焦（不再每轮硬凑三类） ==")
    check(tuple(tasktype.TEST_CASE_TYPES) == tuple(schemas.CASE_TYPE),
          "tasktype 的类别集合与 schema 的 enum 同源（两处定义不会静默漂移）",
          str(tasktype.TEST_CASE_TYPES))
    check(tasktype.expected_test_types(tasktype.FEATURE) == ("new",)
          and tasktype.expected_test_types(tasktype.FEATURE, has_existing_surface=True)
          == ("new", "regression"),
          "首次开发：首轮只要 new；已有交付面才追加 regression")
    check(tasktype.expected_test_types(tasktype.BUGFIX) == ("regression",),
          "缺陷修复：只看回归（修前失败、修后通过）")
    check(tasktype.expected_test_types(tasktype.PLAN_REWORK) == ("new",)
          and tasktype.suggested_test_types(tasktype.PLAN_REWORK) == ("contract", "interface"),
          "方案返工：必需 new，接口/契约为建议类别（不判负）")
    check(all(t in tasktype.TEST_CASE_TYPES
              for t in tasktype.suggested_test_types(tasktype.PLAN_REWORK)),
          "建议类别必须在 schema enum 内，否则模型物理上产不出来")
    # 必需集合恒为旧三类的子集 —— 只放宽不收紧（否则是"隐性加严"，模型无从满足）
    legacy = {"new", "regression", "compat"}
    check(all(set(tasktype.expected_test_types(k)) <= legacy
              for k in tasktype.ROUND_KINDS),
          "各轮必需类别都是旧三类的子集（P0-12 只放宽，不隐性加严）")
    # 提示词：聚焦块 + 任务段，且旧口径"三类缺一不可"已从两套 test system 里移除
    parts = prompts.parts_test(
        REQ, SCOPE, PLAN, api_digest={}, test_view={"cases": []},
        test_focus=tasktype.test_focus_guidance(tasktype.BUGFIX),
    )
    joined = "\n".join(p for p in parts if p)
    check("本轮测试聚焦" in joined and "缺陷修复" in joined and "必需类别：regression" in joined,
          "test 任务段带上本轮聚焦口径（提示词与审计同源）", joined[-200:])
    check("按上方的**本轮测试聚焦**产出用例" in joined and "三类测试用例" not in joined,
          "任务段改为按聚焦口径（不再写死三类）")
    for pt in ("secondary", "new"):
        sys_test = prompts.system_prompt("test", pt, tasktype.FEATURE)
        check("缺一不可" not in sys_test and "本轮测试聚焦" in sys_test,
              f"{pt} 的 test system 已改为按聚焦口径（不再「三类缺一不可」）")
    # 兼容：不传 test_focus 时回退旧措辞（老复跑脚本不受影响）
    legacy_parts = "\n".join(p for p in prompts.parts_test(REQ, SCOPE, PLAN) if p)
    check("产出新功能/回归/兼容三类测试用例" in legacy_parts,
          "不传 test_focus ⇒ 回退旧的「三类」措辞（向后兼容）")

    print("== 3. dev 输入视图互斥（含尾部任务段） ==")
    feat = dev_view(tasktype.FEATURE)
    bug = dev_view(tasktype.BUGFIX)
    plan = dev_view(tasktype.PLAN_REWORK)
    check("架构师变更方案" in feat and "从需求出发" not in feat,
          "首轮视图：方案是权威，不喂需求原文")
    check("缺陷单" in bug and "最小补丁" in bug, "返工视图：缺陷单 + 最小补丁")
    check("按方案实现代码改动" not in bug,
          "**返工轮尾部不再拼首轮任务段**（此前靠 rework note 去盖住它，顺序一变就漏回来）")
    check("刚更新的方案" in plan and "有权创建" in plan,
          "方案返工视图：按新方案施工、允许创建新增文件")
    check("只改【缺陷单】" not in plan, "方案返工视图不串味成缺陷修复（两者正相反）")
    check("架构师变更方案" in plan, "方案返工仍然要喂施工图（返工比首轮更离不开它）")
    check("按方案实现代码改动" not in plan, "方案返工轮也不拼首轮任务段")

    print("== 3b. 骨架调用必须看到方案声明的符号（真机 095848 的根因） ==")
    sk = "\n".join(p for p in prompts.parts_skeleton(SCOPE, PLAN) if p)
    check("CLI.add" in sk,
          "方案声明的 symbols 进了骨架调用的输入（不喂 ⇒ 骨架只能凭空发明另一套名字）", sk[-200:])
    check("要定的符号" in sk, "并写明「逐个落实」，不是让模型再猜一遍该定哪些名字")
    check("新增子命令" not in sk,
          "**实现细节（approach）仍然不喂** —— 喂了会把注意力拉去复述实现（原有取舍不变）")
    check("cli.py" in sk and "加命令" in sk, "文件清单与 intent 照旧")

    print("== 3c. has_code 判据：空池的占位说明不能算「有代码」（真机 110402） ==")
    from pipeline import retrieval as retrieval_mod
    check(retrieval_mod.render_excerpts([]) == "",
          "空池渲染成**空串**（占位说明属于呈现层，不该污染数据层判据）",
          repr(retrieval_mod.render_excerpts([]))[:60])
    empty_view = "\n".join(p for p in prompts.parts_dev(
        REQ, SCOPE, ASSESS, PLAN, "", current_code="",
        include_plan=True, round_kind="feature",
    ) if p)
    check("没有可锚定的原文" in empty_view and "输出符号级 edits" not in empty_view,
          "空池 + 无当前代码 ⇒ 走「没有可锚定的原文 → 给完整内容」分支"
          "（此前 6/6 张首轮施工图都走了 edits 分支）", empty_view[-180:])
    has_view = "\n".join(p for p in prompts.parts_dev(
        REQ, SCOPE, ASSESS, PLAN, "", current_code=CURRENT,
        include_plan=True, round_kind="feature",
    ) if p)
    check("输出符号级 edits" in has_view, "有真实当前代码 ⇒ 仍走 edits 分支（两条分支都在）")
    flag_view = "\n".join(p for p in prompts.parts_dev(
        REQ, SCOPE, ASSESS, PLAN, "", current_code="",
        include_plan=True, round_kind="feature", code_available=True,
    ) if p)
    check("输出符号级 edits" in flag_view,
          "`code_available` 结构化判据优先（调用方说了算，不受渲染文本影响）")

    print("== 3d. 返工口径按任务类型分（首轮自检重问 ≠ 跨轮返工） ==")
    # 真机 110402：首轮自检重问里注入了跨轮返工口径（"必须基于下面给出的当前产物修改"
    # + "优先定点改 modify"），而同一调用的 system 写的是"一律 add + full_symbol"，
    # 且那时【当前项目已有代码】根本不存在 ⇒ 两句话正面冲突（tasktype 顶部记录的那类）。
    rep_view = "\n".join(p for p in prompts.parts_dev(
        REQ, SCOPE, ASSESS, PLAN, "", current_code="", include_plan=True,
        round_kind=tasktype.FEATURE, repair=["漏了 main（声明过但没定义）"],
    ) if p)
    check("首轮自检重问" in rep_view, "首轮自检重问走**首轮口径**")
    check("必须基于下面给出的**当前产物" not in rep_view,
          "**不再**注入「必须基于下面给出的当前产物修改」（本轮没有当前产物，那是硬矛盾）",
          rep_view[-200:])
    check("没有可锚定的原文" in rep_view and "full_symbol" in rep_view,
          "并与【任务】段的「没有可锚定原文 → 给完整内容」一致")
    plan_view = "\n".join(p for p in prompts.parts_dev(
        REQ, SCOPE, ASSESS, PLAN, "", current_code=CURRENT, include_plan=True,
        round_kind=tasktype.PLAN_REWORK, repair=["方案漏了 db.py"],
    ) if p)
    check("方案返工后施工" in plan_view and "有权创建" in plan_view,
          "方案返工轮的修复说明走**方案返工口径**（允许创建方案新增文件）")

    print("== 4. 兼容：老调用点（只传 bugfix 布尔）行为不变 ==")
    legacy = "\n".join(p for p in prompts.parts_dev(
        REQ, SCOPE, ASSESS, PLAN, EXCERPTS, fixes=["补 note 参数"], current_code=CURRENT,
        bug_report_block="【缺陷单】失败命令：`python -m cli` 退出码 1", bugfix=True,
    ) if p)
    check(legacy == bug, "bugfix=True 与 round_kind='bugfix' 等价（老调用点不受影响）")
    check(dev_view("") == feat, "round_kind 为空 ⇒ 按首轮处理（不抛异常）")
    check(dev_view("nope") == feat, "未知任务类型 ⇒ 落到首轮视图（宁可按首轮，也不炸）")

    print("== 5. 版本标识能区分返工契约 ==")
    check(prompts.prompt_version("dev", "new") == f"dev.{prompts.PROMPT_VERSIONS['dev']}-new",
          "首轮：不带任务类型后缀")
    for stage in ("dev", "test", "review"):
        v = prompts.prompt_version(stage, "new", tasktype.BUGFIX)
        check(v.endswith("-bugfix"), f"{stage} 返工轮版本带 -bugfix（换了口径就不能声称可比）", v)
    check(prompts.prompt_version("dev", "new", tasktype.PLAN_REWORK).endswith("-plan_rework"),
          "方案返工轮版本带 -plan_rework")
    check(prompts.prompt_version("dev", "new", tasktype.PLAN_REWORK)
          != prompts.prompt_version("dev", "new", tasktype.BUGFIX),
          "两档返工不可混为一谈")
    check(prompts.prompt_version("nope") == "nope.v0", "未登记阶段仍退回 v0")

    print("== 6. PM 未决问题准入：折叠三列重复 + 剔除技术实现类（真机 20260928-172150） ==")
    from pipeline.orchestrator import pm_unresolved_items  # noqa: E402

    def q(title, **kw):
        row = {"question": title, "why_it_matters": "", "recommendation": "x",
               "assumed_answer": "默认", "severity": "low"}
        row.update(kw)
        return row

    pm_scope = {
        "open_questions": [
            q("数据库表结构", why_it_matters="需要确定字段名称和约束条件"),
            q("金额格式校验"),
            q("备注长度限制"),
            q("序号有效性判断"),
            q("浮点数精度处理"),
            q("是否记录创建时间"),
            q("金额格式校验？"),  # 行内近重复（仅差问号）
        ],
        "unknowns": ["数据库表结构", "金额格式校验", "备注长度限制",
                     "序号有效性判断", "浮点数精度处理", "是否记录创建时间"],
        "clarifying_questions": [
            "数据库表结构", "是否允许负数金额", "是否限制备注字符数",
            "序号是否必须为整数", "是否需要处理浮点数精度问题", "是否需要记录创建时间",
        ],
    }
    logs: list[str] = []
    z = prompts.normalize_pm_questions(dict(pm_scope), log=logs.append)
    titles = [r["question"] for r in z["open_questions"]]
    check("数据库表结构" not in titles and "浮点数精度处理" not in titles,
          "技术实现类问题不进人工裁决（表结构/浮点精度）", str(titles))
    check("金额格式校验" in titles and "备注长度限制" in titles
          and "序号有效性判断" in titles and "是否记录创建时间" in titles,
          "业务问题一律保留（业务规则/输入边界/业务可见性的创建时间）", str(titles))
    check(titles.count("金额格式校验") == 1,
          "open_questions 行内归一去重（「金额格式校验？」折叠）", str(titles))
    check(z["unknowns"] == [],
          "unknowns 与 open_questions 全等重复全部折叠", str(z["unknowns"]))
    cq = z["clarifying_questions"]
    check("数据库表结构" not in cq and "是否需要处理浮点数精度问题" not in cq,
          "两列未明确项里的技术类同样剔除", str(cq))
    check("是否需要记录创建时间" not in cq,
          "疑问前缀归一后折叠（是否记录创建时间 ↔ 是否需要记录创建时间）", str(cq))
    check("是否允许负数金额" in cq and "序号是否必须为整数" in cq
          and "是否限制备注字符数" in cq and len(cq) == 3,
          "词序交错/字面不像的改写宁可漏判也不误杀（新提示词下新 run 不再产生此形态）",
          str(cq))
    # 安全边界：字面相近的**两个不同业务问题**绝不能被折叠（阈值存在的意义）
    distinct = prompts.normalize_pm_questions({"open_questions": [
        q("金额格式校验"), q("金额上限校验"),
    ]})
    check(len(distinct["open_questions"]) == 2,
          "「金额格式校验」与「金额上限校验」是两个业务问题，不得误折叠",
          str([r["question"] for r in distinct["open_questions"]]))
    near = prompts.normalize_pm_questions({"open_questions": [
        q("金额格式校验"), q("金额的格式校验"),
    ]})
    check(len(near["open_questions"]) == 1,
          "仅差一个虚词的近同构问法要折叠（阈值 0.75 的正向覆盖）",
          str([r["question"] for r in near["open_questions"]]))
    check(any("折叠" in m for m in logs) and any("技术实现类" in m for m in logs),
          "准入动作在日志留痕（折叠/忽略了什么，人工可见）", str(logs))
    pending = pm_unresolved_items(pm_scope, [])
    check(len(pending["pending"]) == 4 and len(pending["vague"]) == 3,
          "闸门判据按准入后的口径计数（旧 run 18 条 → 业务 4 问 + 残余改写 3 条）",
          str(pending))
    # 边界：非 dict / 空 dict / 缺字段都不炸，且不凭空补字段
    check(prompts.normalize_pm_questions("x") == "x", "非 dict 原样返回")
    empty = prompts.normalize_pm_questions({})
    check(isinstance(empty, dict) and not empty, "空 dict 不凭空补字段", str(empty))
    bare = prompts.normalize_pm_questions({"open_questions": []})
    check(bare["open_questions"] == [] and "unknowns" not in bare,
          "原本没有的两列不会被新建", str(bare))
    sys_pm = prompts.system_prompt("pm", "second", tasktype.FEATURE)
    check("一一对应" not in sys_pm and "技术实现类" in sys_pm and "空数组" in sys_pm,
          "二开 PM 提示词已取消三列一一对应、写明技术类禁问与空数组约定")
    sys_pm_new = prompts.system_prompt("pm", "new", tasktype.FEATURE)
    check("技术实现" in sys_pm_new and "空数组" in sys_pm_new,
          "新建 PM 提示词同步同口径约束")

    print("== 6b. confirmed_facts 必须随裁决下发（真机 20260928-180933 BUG-B） ==")
    # 人工对 unknowns 的裁决被 apply_pm_decisions 折叠进 confirmed_facts 后，
    # 渲染只遍历 open_questions ⇒ 裁决「交易时间=需要」到不了架构师。
    scope_facts = {"confirmed_facts": ["是否记录交易时间：需要", "多币种：暂不支持"]}
    blk_f = prompts.pm_assumptions_block(scope_facts)
    check("是否记录交易时间：需要" in blk_f and "多币种：暂不支持" in blk_f,
          "open_questions 空、只有 confirmed_facts 时仍渲染（不提前返回空串）", blk_f[:120])
    check("确定结论" in blk_f, "裁决事实进入「确定结论」分区")
    scope_dup = {
        "open_questions": [
            {"question": "是否记录交易时间", "final_decision": "需要", "confirmed": True},
        ],
        "confirmed_facts": ["是否记录交易时间：需要"],
    }
    blk_dup = prompts.pm_assumptions_block(scope_dup)
    check(blk_dup.count("是否记录交易时间") == 1,
          "同一问题在 open_questions 与 confirmed_facts 各一份时只渲染一次（按 fact_key 去重）",
          str(blk_dup.count("是否记录交易时间")))
    applied = prompts.apply_pm_decisions(
        {"open_questions": [], "unknowns": ["是否记录交易时间"], "clarifying_questions": []},
        [{"ref": "是否记录交易时间", "decision": "需要"}],
    )
    blk_a = prompts.pm_assumptions_block(applied)
    check("是否记录交易时间：需要" in blk_a,
          "unknowns 裁决折叠成 confirmed_facts 后照样到下游", blk_a[:120])

    print("== 7. Phase H：评审机械摘要第一屏（§36）+ llm-calls 语义上下文（§37） ==")
    from pipeline import budget, runstore  # noqa: E402
    summary = {
        "verify": {"verdict": "fail", "commands": 3, "failed": 1},
        "proof": {"obligations": 23, "evidence": 26, "status": "UNPROVEN"},
        "test": {"covered_symbols": 4, "missing_symbols": 2,
                 "expected_types": ["new"], "missing_types": [], "external_required": ["po:ui"]},
        "ontology_errors": 0,
        "workspace": {"revision": "ws-006", "status": "VERIFIED"},
        "defects": {"green": 2, "red": 1, "unverifiable": 0},
        "blockers": ["补丁未能套用到 main.py"],
    }
    blk = prompts.mechanical_summary_block(summary)
    check("机械摘要" in blk and "证明义务：23 条 / 证据 26 条" in blk
          and "机械阻断项：1 条" in blk,
          "机械摘要逐条渲染已成立的机器事实", blk[:160])
    check(prompts.mechanical_summary_block(None) == "",
          "没有机械事实时不渲染空壳标题（不占评审那点紧张预算）")
    rparts = prompts.parts_review(
        REQ, SCOPE, PLAN, {"edits": []}, {"cases": []}, summary_block=blk)
    check(rparts and rparts[0] == blk,
          "机械摘要排在 parts **最前**（fit_prompt 从末尾截断 ⇒ 必然存活）")
    fitted, _ = budget.fit_prompt(rparts, budget_tokens=500)
    check("机械摘要" in fitted, "极小预算下机械摘要仍存活（语义材料先被截）")
    check(rparts[0] == blk and "机械摘要" not in "\n".join(
        prompts.parts_review(REQ, SCOPE, PLAN, {}, {})),
          "不传 summary_block ⇒ 行为不变（老调用点不受影响）")
    check({"proof_obligation_count", "evidence_count"} <= set(runstore.CALL_FIELDS),
          "llm-calls 固定列含语义上下文规模（§37）")
    check(runstore.normalize_call_record({"stage": "dev"})["proof_obligation_count"] is None,
          "缺列补 None（读旧 jsonl 安全，不臆造 0）")

    print(f"\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
