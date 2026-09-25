"""阶段产物的 JSON Schema（决策 4：强制契约）与轻量校验器。

不引入 jsonschema 依赖（本机未安装），实现所需的 draft-07 子集：
type / required / properties / items / enum / minItems / maxItems / minimum / maximum。
服务端用 ollama 的 format=schema 强约束，客户端再做一次校验作为兜底。
"""
from __future__ import annotations

from typing import Any

SEVERITY = ["high", "medium", "low"]
RISK = ["high", "medium", "low"]
VERDICT = ["pass", "rework_dev", "rework_architect"]
CASE_TYPE = ["new", "regression", "compat"]
CHANGE_TYPE = ["add", "modify", "delete"]
PATCH_MODE = ["insert_after", "replace_span", "full_symbol"]

# 未决问题：每条都必须带「建议方案」与「默认取值」。
# 真机教训（2026-09-23 贪吃蛇 run）：PM 把 unknowns 抛出来却不给答案，且 unknowns 只被记成
# info 级问题、不阻塞流程，于是下游各阶段各自脑补出一版互不一致的答案，
# 架构师方案里甚至冒出需求根本没提的东西（转盘参与度、黑白进度条…）。
# 现在要求 assumed_answer：人工没确认时下游按它推进，答案由 PM 统一给出而非各处臆测。
# 未决问题的「猜错代价」刻意只留两档：值得返工 vs 小调整。
# 不与通用 SEVERITY（三档，描述影响程度）混用 —— 三档时模型会大量选 medium，
# 这个字段本来要回答的是「要不要现在停下来确认」，中间档等于没回答。
OPEN_SEVERITY = ["high", "low"]

OPEN_QUESTION = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "why_it_matters": {"type": "string"},
        "recommendation": {"type": "string"},
        "assumed_answer": {"type": "string"},
        # 人工裁决值（与 intake 一致）：有则覆盖 assumed_answer，成为确定结论
        "final_decision": {"type": "string"},
        "confirmed": {"type": "boolean"},
        "impact_if_wrong": {"type": "string"},
        "severity": {"type": "string", "enum": OPEN_SEVERITY},
    },
    # severity / why_it_matters 一并设为必填：它们只是「可选」时，模型会整体省略
    # （format=schema 只强制 required 字段，低温下模型更保守）。
    # 后果是 PRD 里看不到严重度，人工无从判断哪条必须先确认 —— 实测踩过，务必保留。
    "required": ["question", "recommendation", "assumed_answer", "severity", "why_it_matters"],
}

# ---------------------------------------------------------------- 阶段 1：产品经理
SCOPE = {
    "type": "object",
    "properties": {
        "change_request": {"type": "string"},
        "in_scope": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        # 至少一条：不写「不做什么」时，范围边界形同虚设，下游容易顺手扩大改动
        "out_of_scope": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "goal": {"type": "string"},
        "background": {"type": "string"},
        "target_users": {"type": "array", "items": {"type": "string"}},
        "functional_requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "priority": {"type": "string", "enum": SEVERITY},
                    "acceptance": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "title", "description", "priority"],
            },
        },
        "impact_areas": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "area": {"type": "string"},
                    "impact": {"type": "string"},
                    "severity": {"type": "string", "enum": SEVERITY},
                },
                "required": ["area", "impact", "severity"],
            },
        },
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "clarifying_questions": {"type": "array", "items": {"type": "string"}},
        # 未决问题的结构化版本：带建议方案与默认取值，下游据此推进（可为空数组）
        "open_questions": {"type": "array", "items": OPEN_QUESTION},
    },
    # clarifying_questions 也进 required：它被 issues（问题记录）、handoff（待人工清单）、
    # PRD 第 7 节三处消费，落成可选项时模型会整段省略，人工就少了一条扫读入口。
    # background / goal / functional_requirements 设为必填：这三块是 PRD 的骨架，
    # 不强制时模型会整段略过，PRD 渲染出来第 1 节空着，只能回去看需求原文。
    "required": [
        "change_request",
        "background",
        "goal",
        "in_scope",
        "out_of_scope",
        "impact_areas",
        "functional_requirements",
        "acceptance_criteria",
        "unknowns",
        "clarifying_questions",
        "open_questions",
    ],
}

# ------------------------------------------------- 架构师前置：存量代码评估 + 兼容约束
# 不确定项拆成三元组：把「发现了什么疑点」与「我的推测」分开并标注可信度。
# 原先是一串裸字符串，人工扫读时分不清哪条是模型在猜、哪条是确实拿不到依据 ——
# 而这两者对该不该继续往下跑意义完全不同。
CONFIDENCE = ["high", "medium", "low"]

UNCERTAINTY = {
    "type": "object",
    "properties": {
        "issue": {"type": "string"},
        "assumption": {"type": "string"},
        "confidence": {"type": "string", "enum": CONFIDENCE},
    },
    "required": ["issue", "assumption", "confidence"],
}

ASSESSMENT = {
    "type": "object",
    "properties": {
        # 允许为空：未提供代码片段时，架构师必须留空而不是编造目录结构
        "modules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "role": {"type": "string"},
                    "change_risk": {"type": "string", "enum": RISK},
                    # entry_points / dependencies 刻意**不设必填**：模块里没有明显入口时，
                    # 强制填会直接诱发编造函数名（与「严禁编造」的红线自相矛盾）。
                    # 有依据就填，没依据留空数组即可 —— 由提示词来说明这条取舍。
                    "entry_points": {"type": "array", "items": {"type": "string"}},
                    "dependencies": {"type": "array", "items": {"type": "string"}},
                    # risk_details 必填：改这个模块的代价是下游判断该不该动它的主要依据
                    "risk_details": {"type": "string"},
                },
                "required": ["path", "role", "change_risk", "risk_details"],
            },
        },
        "reusable_hooks": {"type": "array", "items": {"type": "string"}},
        "compatibility_constraints": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "forbidden_paths": {"type": "array", "items": {"type": "string"}},
        "baseline_tests": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": UNCERTAINTY},
    },
    "required": [
        "modules",
        "compatibility_constraints",
        "forbidden_paths",
        "baseline_tests",
        "uncertainties",
    ],
}

# ------------------------------------------------------- 架构师阶段 2：最小侵入方案
PLAN = {
    "type": "object",
    "properties": {
        "strategy": {"type": "string"},
        "changes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "intent": {"type": "string"},
                    "approach": {"type": "string"},
                    "minimality_reason": {"type": "string"},
                },
                "required": ["path", "intent", "approach", "minimality_reason"],
            },
        },
        "tasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "target_files": {"type": "array", "items": {"type": "string"}},
                    "acceptance": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "title", "target_files", "acceptance"],
            },
        },
        "rollback": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["strategy", "changes", "tasks", "rollback"],
}

# --------------------------------------------------------------- 开发：编码实现产物
# 契约要点（真机教训后改成「锚定补丁」）：早期要求 edits[].code 给出「该文件最终内容或可直接应用的补丁」，
# 对 71KB 的单文件项目根本不可能满足 —— dev 只能挤出 1000 字符的示意代码，却在 self_checks 里声称全部完成。
# 现在要求：一次改动 = 一个符号（函数/类）的补丁 + 定位锚点 + 对应哪些任务；未做的必须显式声明。
IMPLEMENTATION = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        # 「怎么把它跑起来」：一条能证明产物可运行的具体命令（如 `python main.py`）。
        # 为什么要有：运行验证此前只能靠**测试阶段**声明的命令，而测试阶段经常只写
        # `python <库模块>.py`（空跑、rc=0 但什么都没做）。让**写代码的人**给出入口命令，
        # 是最可靠的一条证据 —— 而且会逼它自己先确认入口存在（真机 run 20260924-235001
        # 的 5 个文件一个入口都没有，却在 8 轮里没人被要求回答「怎么跑」）。
        "run": {"type": "string"},
        "edits": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "change_type": {"type": "string", "enum": CHANGE_TYPE},
                    "target_symbol": {"type": "string"},  # 被改的函数/类名；add 时填新符号名
                    "anchor": {"type": "string"},  # 原文里能唯一定位的 1~3 行骨架（含缩进），用于把补丁贴回原位
                    "patch_mode": {
                        "type": "string",
                        "enum": PATCH_MODE,
                    },  # 补丁语义：insert_after / replace_span / full_symbol（机器据此校验与套用）
                    "patch": {"type": "string"},  # 统一 diff，或按 patch_mode 的代码块（不要求整文件）
                    "covers_tasks": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "rationale": {"type": "string"},
                },
                "required": [
                    "path",
                    "change_type",
                    "target_symbol",
                    "anchor",
                    "patch_mode",
                    "patch",
                    "covers_tasks",
                    "rationale",
                ],
            },
        },
        "not_implemented": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"task": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["task", "reason"],
            },
        },  # 方案里没做的任务必须显式列出来（诚实优先于"看起来完成"）
        "self_checks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "evidence": {"type": "string"},  # 必须能指到具体位置（文件+符号+行/命令）
                },
                "required": ["claim", "evidence"],
            },
        },
        # 与方案不一致之处：拆成「偏差项 + 原因」。一坨散文里模型常把「解释」和「偏差」混写，
        # 评审要逐条判断是否可接受时很费劲。
        "deviations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"item": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["item", "reason"],
            },
        },
        # 与架构师评估阶段复用同一个 UNCERTAINTY 三元组，保持全局一致
        "uncertainties": {"type": "array", "items": UNCERTAINTY},
    },
    "required": [
        "summary",
        "edits",
        "not_implemented",
        "self_checks",
        "deviations",
        "uncertainties",
    ],
}

# --------------------------------------------------------------- 测试：三类用例报告
# 覆盖缺口拆成 {gap, reason, impact}：原先是一串裸字符串，人工看不出哪条只是缺个边界用例、
# 哪条会让整个结论不成立。impact 才是「要不要为它停下来」的判断依据。
TEST_GAP = {
    "type": "object",
    "properties": {
        "gap": {"type": "string"},
        "reason": {"type": "string"},
        "impact": {"type": "string"},
    },
    "required": ["gap", "reason", "impact"],
}

# 命令拆成 {command, description}：裸命令看不出它验的是哪条用例、该观察什么输出。
TEST_COMMAND = {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "description": {"type": "string"},
    },
    "required": ["command", "description"],
}

TEST_REPORT = {
    "type": "object",
    "properties": {
        "cases": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string", "enum": CASE_TYPE},
                    "target": {"type": "string"},
                    "steps": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "expected": {"type": "string"},
                },
                "required": ["id", "type", "target", "steps", "expected"],
            },
        },
        "automated_commands": {"type": "array", "items": TEST_COMMAND},
        "coverage_gaps": {"type": "array", "items": TEST_GAP},
        "risks": {"type": "array", "items": {"type": "string"}},
        # 与 assess / dev 复用同一个 UNCERTAINTY 三元组，保持全局一致
        "uncertainties": {"type": "array", "items": UNCERTAINTY},
    },
    # automated_commands / uncertainties 一并设为必填：它们是可选项时模型会整段省略，
    # 而「一条可执行命令都没有」本身就是需要被人工看见的结论（有 _audit_test 机械核对）。
    "required": ["cases", "automated_commands", "coverage_gaps", "uncertainties"],
}

# --------------------------------------------------------------------- 评审：交付判定
# 契约要点（把「判定纪律」从提示词改成机制）：返工项必须逐条声明作用域 ——
# `in_material`（本轮材料内就能改）才允许触发返工；`needs_external`（要跑系统/问人/查环境）由编排器
# 自动改判进 residual_risks。真机教训：只靠提示词叮嘱，3 轮都在要求"确认接口是否存在/补 SQL 断言"。
#
# `architect`（2026-09-24 第九轮补）：**作用域三档缺一不可**，因为「谁能改」决定回到哪个阶段。
# 真机教训 run 20260924-185507：dev 产物 import 了一个方案从未规划的模块（`direction`），
# 评审只投了实现层返工项、机制又只能整轮回 dev → 白跑一轮仍撞同一个错。
# 归到 `architect` 的返工项会让编排器把下一轮**直接回到 architect_plan**（方案变了实现必然重做）。
FIX_SCOPE = ["in_material", "architect", "needs_external"]

# 残留风险拆成 {issue, reason, impact}：原先是一串裸字符串，人工在 handoff 里看不出
# 哪条只是记录一下、哪条需要为它停下来。impact 才是「要不要拦住交付」的依据。
# 与测试阶段的 coverage_gaps（{gap, reason, impact}）保持同一形状，便于人工统一扫读。
RESIDUAL_RISK = {
    "type": "object",
    "properties": {
        "issue": {"type": "string"},
        "reason": {"type": "string"},
        "impact": {"type": "string"},
    },
    "required": ["issue", "reason", "impact"],
}

REVIEW = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": VERDICT},
        "reasons": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "blockers": {"type": "array", "items": {"type": "string"}},
        "required_fixes_detail": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fix": {"type": "string"},
                    # 归属文件（相对仓库根）。**强烈建议填**：填了才能把整改要求路由到
                    # 具体文件；不填的话下游只能把要求当无主文本交给开发，开发就会
                    # 反复去改评审根本没抱怨的文件（真机 run 20260924-185507：
                    # score_system 被补 3 次，而真正坏的 game_logic / input_handler
                    # 在第 2 轮之后再没被碰过）。
                    "path": {"type": "string"},
                    "scope": {"type": "string", "enum": FIX_SCOPE},
                    "why": {"type": "string"},
                },
                "required": ["fix", "scope", "why"],
            },
        },
        "required_fixes": {"type": "array", "items": {"type": "string"}},
        "residual_risks": {"type": "array", "items": RESIDUAL_RISK},
    },
    "required": ["verdict", "reasons", "blockers", "required_fixes_detail", "residual_risks"],
}

# --------------------------------------------------- 人工审核闸门（交付前最后一道关）
# 非模型阶段：由人工在控制台核对 4 项后提交 verdict。schema 仅做轻量校验，
# 4 项核对为可选布尔（不强制），verdict 必填。
HUMAN_REVIEW = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "reject", ""]},
        "core_path_ok": {"type": "boolean"},
        "no_obvious_errors": {"type": "boolean"},
        "deliverables_complete": {"type": "boolean"},
        "requirement_met": {"type": "boolean"},
        "notes": {"type": "string"},
        "reviewer": {"type": "string"},
    },
    "required": ["verdict"],
}

# --------------------------------------------- 阶段 0：需求入口补强（intake，跑在 pm 之前）
# 定位：承接用户原始需求（零散/模糊/口语化），做结构化补强与要素补全，产出**需求初稿**，
# 供下游产品经理直接消费（复用机制：PM 不必再重新解析原始需求）。
# 红线由 schema 兜底：字段里没有任何技术方案/选型的位置；所有补充内容在提示层被要求标注
# 「建议补充」/「默认假设」，`pending_items` / `uncertainties` 则是人工「快速扫一眼确认
# 默认假设」的入口。
# 人工裁决后，裁决结果**直接并回这一条**（而不是另外递一份裁决清单给下游）：
# `default_assumption` 保留补强当初的猜测作为审计痕迹，`final_decision` 是人工确认值，
# 下游一律以它为准。这样「补强最终产物」就是唯一真源。
#
# **待确认项只有一个列表**（此前是 missing_elements + clarifying_questions 两个）。
# 合并理由（真机 run 20260925-140707）：
#   1) 对人工来说两类没有区别 —— 都要在框里填一条裁决，代价一样；区分只对下游有意义
#      （有具体默认值 → 可按此推进；没有 → 必须等裁决）；
#   2) 而这个信息**能从条目本身推导**：default_assumption 是具体取值就能推进，留空即
#      「补强也拿不准，等你定」。信息量 = 一个列表 + 这个标记，不需要两个结构；
#   3) 关键：两个列表本身就是**重复的生成源** —— 模型面对「这算缺失要素、还是澄清问题」
#      这个模糊判断题只能瞎猜，于是同一主题两边各写一次（游戏分辨率 / 游戏窗口尺寸要求），
#      人工得填两个框。合并成一条列表从**结构上**消灭这类重复，比靠提示词去重可靠。
PENDING_ITEM = {
    "type": "object",
    "properties": {
        "element": {"type": "string"},             # 要定的是什么（主题，一句话说清）
        "why": {"type": "string"},                 # 为什么要定 / 不定会怎样（原 clarifying.impact）
        # 建议取值。**必须是能直接照做的具体值**（如「使用 800×600 画布」）；
        # 确实给不出具体值时留空字符串 —— 空串就是「等你裁决」的唯一标记，
        # 不允许用「需明确…」「待确认」把问题原样退回来（那等于没给答案）。
        "default_assumption": {"type": "string"},
        "importance": {"type": "string", "enum": SEVERITY},
        "final_decision": {"type": "string"},  # 人工裁决值；有则覆盖 default_assumption
        "confirmed": {"type": "boolean"},      # 是否已由人工裁决
    },
    "required": ["element", "why", "default_assumption", "importance"],
}

#: 「可观察行为 + 期望值」二元组（见 INTAKE.key_behaviors 的说明）
KEY_BEHAVIOR = {
    "type": "object",
    "properties": {
        "behavior": {"type": "string"},     # 用户能观察到的行为，一句话
        "expectation": {"type": "string"},  # 该行为的期望结果（含具体取值）
    },
    "required": ["behavior", "expectation"],
}

INTAKE = {
    "type": "object",
    "properties": {
        "original_summary": {"type": "string"},
        "refined_requirement": {
            "type": "object",
            "properties": {
                "background": {"type": "string"},
                "core_goal": {"type": "string"},
                "target_users": {"type": "array", "items": {"type": "string"}},
                "main_scenarios": {"type": "array", "items": {"type": "string"}},
                # 忠实用户原意结构化整理，不新增功能（红线 1 由提示词负责，这里只保证非空）
                "core_features": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "constraints": {"type": "array", "items": {"type": "string"}},
            },
            # background / core_goal / core_features 必填：它们是 PM 写 PRD 的骨架，
            # 可选项时模型会整段略过，补强等于白做（与 SCOPE 同样的取舍）。
            "required": ["background", "core_goal", "core_features"],
        },
        "preliminary_scope": {
            "type": "object",
            "properties": {
                "suggested_in_scope": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "suggested_out_of_scope": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["suggested_in_scope", "suggested_out_of_scope"],
        },
        # 关键行为：把核心功能写成「可观察行为 + 期望值」，它是反推 pending_items 的依据。
        #
        # 为什么要设这个必填字段（真机 run 20260925-140707）：靠**凭空列举**「这类需求按常识
        # 必然涉及、而原文没写」的参数必漏 —— 8B 与 14B 在「列领域常识清单」和「站在实现者
        # 角度列你必须自己决定的参数」两种框架下**都漏掉了「吃完食物蛇变长」**，甚至连
        # 「吃到食物后…」那条验收测试都只断言了得分。原因：越是「大家都知道」的默认行为，
        # 越不会被当成待定项。
        # 但换成「**以写验收测试为唯一任务**」的调用，14B 立刻写出「吃到食物 → 蛇长度增加 1 段」，
        # 还顺带提出「速度是否随长度变化」。
        # 结论：这一步不能靠长提示词里的散文步骤（模型不会真的执行），必须由 **schema 强制输出**。
        # 只要模型不得不填「吃到食物的期望值」，「蛇变长」就会被逼出来。
        "key_behaviors": {"type": "array", "items": KEY_BEHAVIOR, "minItems": 3},
        "pending_items": {"type": "array", "items": PENDING_ITEM},
        # 复用全局 UNCERTAINTY 三元组，与 assess / dev / test 保持一致。
        # 刻意**不并进 pending_items**：uncertainties 是「客观未知 + 置信度」，不是要人工
        # 拍板的待确认项（且被 assess/dev/test 共用同一形状）。
        "uncertainties": {"type": "array", "items": UNCERTAINTY},
    },
    "required": [
        "original_summary",
        "refined_requirement",
        "preliminary_scope",
        "key_behaviors",
        "pending_items",
        "uncertainties",
    ],
}

# --------------------------------------------- 运行验证（verify，跑在 review 之前）
# 非模型阶段：把补丁物化到沙箱目录，**真的执行一遍**，把退出码/输出当机械证据。
# 这是「最终输出结果的验证与确认」——不是让模型读文件，而是让它跑起来。
# 契约只描述结果形状；命令来源与安全策略在 pipeline/verify.py 里（白名单 + 超时 + 沙箱）。
VERIFY_COMMAND = {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "source": {"type": "string"},          # planned（测试阶段声明）/ syntax / probe / test_suite
        "status": {"type": "string", "enum": ["ok", "fail", "timeout", "error", "skipped"]},
        "exit_code": {"type": "integer"},
        "duration_s": {"type": "number"},
        "stdout_tail": {"type": "string"},
        "stderr_tail": {"type": "string"},
        "reason": {"type": "string"},          # skipped 时说明为什么没跑
    },
    "required": ["command", "status"],
}

VERIFY_REPORT = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail", "skipped"]},
        "summary": {"type": "string"},
        "sandbox": {"type": "string"},
        "materialized": {"type": "array", "items": {"type": "string"}},
        "commands": {"type": "array", "items": VERIFY_COMMAND},
        "problems": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "commands"],
}

# ------------------------------------------------- 前置节点：全局架构（入口总闸）
# 项目级拆分岗的产物契约。字段与提示词模板**逐字对应**，一个不多一个不少：
# 提示词由使用方给定且要求「完整复用、不得修改」，所以补齐缺口只能落在提示词之外
# （规模判定、路径级信息、禁区来源都由 gateway 侧用确定性规则补，见 pipeline/gateway.py）。
GLOBAL_ARCHITECTURE_MODULE = {
    "type": "object",
    "properties": {
        "module_id": {"type": "string"},
        "module_name": {"type": "string"},
        "responsibility": {"type": "string"},
        "scope_in": {"type": "array", "items": {"type": "string"}},
        "scope_out": {"type": "array", "items": {"type": "string"}},
        "risk_level": {"type": "string", "enum": RISK},
        "depends_on": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["module_id", "module_name", "responsibility", "scope_in", "scope_out", "risk_level"],
}

GLOBAL_ARCHITECTURE_CONTRACT = {
    "type": "object",
    "properties": {
        "interface_id": {"type": "string"},
        "from_module": {"type": "string"},
        "to_module": {"type": "string"},
        "interface_name": {"type": "string"},
        "input_format": {"type": "string"},
        "output_format": {"type": "string"},
        "error_codes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["interface_id", "from_module", "to_module", "interface_name"],
}

GLOBAL_ARCHITECTURE_CHECKPOINT = {
    "type": "object",
    "properties": {
        "checkpoint": {"type": "string"},
        "verification_method": {"type": "string"},
    },
    "required": ["checkpoint", "verification_method"],
}

# 不确定项：与既有 UNCERTAINTY 不同 —— 提示词给定的是 {issue, assumption, impact} 三字段，
# 不能替换成项目里那套带 confidence 的版本（那会背离「字段严格对应模板」的要求）。
GLOBAL_ARCHITECTURE_UNCERTAINTY = {
    "type": "object",
    "properties": {
        "issue": {"type": "string"},
        "assumption": {"type": "string"},
        "impact": {"type": "string"},
    },
    "required": ["issue", "assumption", "impact"],
}

GLOBAL_ARCHITECTURE = {
    "type": "object",
    "properties": {
        "project_summary": {"type": "string"},
        # 至少一个模块：拆不出模块的「大型项目」是无意义的，宁可在校验期就拦下
        "modules": {"type": "array", "items": GLOBAL_ARCHITECTURE_MODULE, "minItems": 1},
        "interface_contracts": {"type": "array", "items": GLOBAL_ARCHITECTURE_CONTRACT},
        "global_constraints": {
            "type": "object",
            "properties": {
                "forbidden_paths": {"type": "array", "items": {"type": "string"}},
                "naming_rules": {"type": "string"},
                "compatibility_rules": {"type": "string"},
                "dependency_versions": {"type": "string"},
            },
            "required": [
                "forbidden_paths",
                "naming_rules",
                "compatibility_rules",
                "dependency_versions",
            ],
        },
        "execution_order": {"type": "array", "items": {"type": "string"}},
        "integration_checkpoints": {
            "type": "array",
            "items": GLOBAL_ARCHITECTURE_CHECKPOINT,
        },
        "uncertainties": {"type": "array", "items": GLOBAL_ARCHITECTURE_UNCERTAINTY},
    },
    "required": [
        "project_summary",
        "modules",
        "interface_contracts",
        "global_constraints",
        "execution_order",
        "integration_checkpoints",
        "uncertainties",
    ],
}

# --------------------------------------------- 裁决参谋（旁路环节，**不是**流水线阶段）
# 人工在闸门上裁决「待确认项」时，可以就某一条反复向模型要**风险 / 收益 / 可逆性**判断。
# 它可以服务任意阶段，所以**不登记进 STAGE_SCHEMAS / PRE_SCHEMAS**（那两张表都要求与
# flow 的节点表一一对应，flow.validate 会核对）；这里单独定义，由 pipeline/advice.py 使用。
#
# 契约刻意不给它任何「改需求 / 写代码」的位置：它是参谋，不是执行者。
ADVICE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},          # 直接回答人工问的那个问题
        "recommendation": {"type": "string"},  # 建议采用的**具体取值**（不能是「视情况而定」）
        "benefits": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "alternatives": {"type": "array", "items": {"type": "string"}},
        # 选错以后改动的代价：easy=改一处配置 / moderate=要动多处下游 / hard=要推翻已完成的实现
        "reversibility": {"type": "string", "enum": ["easy", "moderate", "hard"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "evidence": {"type": "string"},        # 依据出处；没有依据就明说是推测
    },
    "required": ["answer", "recommendation", "benefits", "risks", "reversibility", "confidence"],
}

#: 前置节点的契约登记表（不是流水线阶段，故与 STAGE_SCHEMAS 分开；
#: ``flow.validate`` 会核对它与 ``flow.PRE_NODES`` 一致）
PRE_SCHEMAS: dict[str, dict[str, Any]] = {
    "global_architecture_analysis": GLOBAL_ARCHITECTURE,
}

STAGE_SCHEMAS: dict[str, dict[str, Any]] = {
    "intake": INTAKE,
    "pm": SCOPE,
    "architect_assess": ASSESSMENT,
    "architect_plan": PLAN,
    "dev": IMPLEMENTATION,
    "test": TEST_REPORT,
    "verify": VERIFY_REPORT,
    "review": REVIEW,
    "human_review": HUMAN_REVIEW,
}

# --------------------------------------------------------------- 轻量校验器
_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


def _type_ok(value: Any, want: str) -> bool:
    if want == "boolean":
        return isinstance(value, bool)
    if want in ("integer", "number"):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    expect = _TYPE_MAP.get(want)
    return expect is None or isinstance(value, expect)


def validate(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """返回错误列表，空列表表示通过。"""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors

    want = schema.get("type")
    if want and not _type_ok(instance, want):
        return [f"{path}: 期望 {want}, 实际 {type(instance).__name__}"]

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: 取值 {instance!r} 不在 {schema['enum']} 内")

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: 缺少必填字段 {key}")
        for key, sub in schema.get("properties", {}).items():
            if key in instance:
                errors.extend(validate(instance[key], sub, f"{path}.{key}"))
    elif isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: 至少需要 {schema['minItems']} 项，实际 {len(instance)}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: 最多 {schema['maxItems']} 项，实际 {len(instance)}")
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(instance):
                errors.extend(validate(item, item_schema, f"{path}[{i}]"))
    elif isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} > {schema['maximum']}")

    return errors
