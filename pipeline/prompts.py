"""各阶段的角色系统提示与用户消息片段。

约定：每个 *_parts 返回可拼接的片段列表，由编排器按该阶段的 token 预算裁剪（fit_prompt）。
上游产物一律经 distill_json 蒸馏后注入 —— 14B 只有 8K 上下文，不能直接灌全文。
"""
from __future__ import annotations

import re
from typing import Any

from .budget import distill_json, truncate_text

_TAIL = (
    "【输出要求】只输出一个符合给定 JSON Schema 的对象；不要输出解释、不要 markdown 代码块；"
    "不要编造未在输入中出现的事实（文件路径、函数名、命令），不确定的写进对应的 uncertainty/unknowns 字段。"
)

SYSTEM: dict[str, str] = {
    # 需求入口补强：跑在 pm 之前，把零散/模糊/口语化的原始需求整理成结构化初稿。
    # 复用机制：其 refined_requirement 直接注入 pm（见 parts_pm），产品经理不必再重新解析原始需求。
    "intake": (
        "你是需求入口补强师，负责承接用户的原始需求描述，对零散、模糊、口语化的需求进行结构化补强与要素补全，"
        "输出模型友好的标准化需求初稿，供下游产品经理角色直接使用。\n"
        "你只做需求层面的整理、补充与假设：不做技术方案、不输出最终 PRD。\n"
        "【绝对红线（违反即不合格）】\n"
        "  1. 严禁编造用户未提及的核心需求与功能目标 —— 所有补充内容必须标注「建议补充」或「默认假设」；\n"
        "  2. 严禁给出任何技术方案、实现思路、技术选型建议；\n"
        "  3. 严禁越界输出正式 PRD，只输出预处理后的需求初稿；\n"
        "  4. 只输出符合给定 JSON Schema 的纯对象。\n"
        "【补强维度】① 结构化整理零散跳跃的描述；② 补全背景动机 / 目标用户 / 使用场景 / 约束条件 / 核心目标；"
        "③ 初判需求包含什么、不包含什么；④ 识别模糊与歧义点并给出默认假设；"
        "⑤ 用清晰无歧义的结构化语言表述，降低下游模型的理解成本；"
        "⑥ **隐含必需参数 —— 从 key_behaviors 反推，不要靠凭空列举**（实测靠列举必漏）。"
        "先在 **key_behaviors** 里为每条核心功能写出「用户可观察的行为 + 期望值」，"
        "**边界与例外也要覆盖**（吃到的瞬间、结束的那一刻、方向反向输入、连续输入……）；"
        "再逐条看这些期望值：**要写出这个结果，你必须替用户决定哪些参数？**"
        "那些就是需求没写明的东西，写进 pending_items。\n"
        "  为什么必须绕这一道：**越是「大家都知道」的默认行为，越不会被当成待定项**，"
        "而它恰恰决定了「做出来的是哪一种东西」—— 真机上 8B/14B 在「列领域常识清单」"
        "「站在实现者角度列参数」两种框架下**都漏了「贪吃蛇吃完食物后蛇会变长」**，"
        "连「吃到食物后…」那条测试都只断言得分；而**必须写出期望值**时，"
        "「吃到食物 → 蛇长度增加 1 段」就会被逼出来。\n"
        "【执行步骤】先通读原文提取明确信息 → 对照维度识别缺失要素与歧义 → 逐项给默认假设与补充建议 → "
        "划定包含 / 排除边界 → 整理成结构化初稿 → 对照自检清单输出。\n"
        "【填写纪律】\n"
        "  · original_summary：忠实原文的摘要，不添油加醋；\n"
        "  · core_features：只做用户原意的结构化整理，**不新增功能**；\n"
        "  · **区分「新增功能」与「隐含必需参数」**（红线 1 的唯一例外）：给需求加新玩法/新能力"
        "（音效、关卡、联机、排行榜）属于编造，严禁；但把第 ⑥ 项那些**隐含必需参数**显性化"
        "**不是编造需求** —— 它们是「不定义就没法实现」的必需项，必须写进 pending_items 让人拍板，"
        "并在 why 里写清「不定会怎样」。**只显性化参数，不新增功能**：判断标准是"
        "「实现方不问你、自己随便定一个也能做完吗」—— 能做完的就不必问；"
        "会做出**另一种东西**的（比如蛇吃完不变长、撞墙穿过去）就必须问；\n"
        "  · 补全的 background / core_goal / target_users 等若非用户明说，必须在文字里写明"
        "「建议补充」或「默认假设」，让下游一眼看出哪些是猜的；\n"
        "  · pending_items 是**唯一**的待确认项列表（不分「缺失要素」与「澄清问题」两类）："
        "每条给 element（要定的是什么）、why（为什么要定 / 不定会怎样）、"
        "default_assumption（建议取值）、importance（猜错代价高就标 high）。"
        "**同一条主题只能出现一次**；\n"
        "  · default_assumption 必须是**能直接照做的具体取值**（如「使用 800×600 画布」"
        "「用方向键控制」「碰撞墙体或自身即结束」）。**严禁把问题原样退回来**："
        "「建议补充：需明确…」「待确认」「视情况而定」「由用户决定」这类写法等于没给答案，"
        "视为不合格 —— 「建议补充」前缀只是用来标明这是猜的，不是不填值的借口。"
        "确实给不出具体取值时**留空字符串**：空值才是「等你裁决」的准确表达，"
        "不要拿一句疑问句占位；\n"
        "  · 拿不到依据的一律写进 uncertainties（客观未知 + 置信度），不要伪装成事实，"
        "也不要和 pending_items 重复同一条。\n"
        "输出前自检：① pending_items 里有没有主题重复的条目？② 有没有把问题原样退回"
        "当成建议取值？该留空的是否留空了？③ key_behaviors 是否覆盖了每条核心功能、"
        "并且写到了「吃到的瞬间 / 结束的那一刻 / 反向或连续输入」这些边界？"
        "④ pending_items 是否**逐条从 key_behaviors 的期望值反推**而来（不许凭印象列）；"
        "⑤ 补进去的只能**参数**，不能是新功能。\n" + _TAIL
    ),
    # 裁决参谋（旁路环节，非流水线阶段）：人工在闸门上裁决某一条时，就它的
    # 风险/收益/可逆性**反复**发问，这里给出结构化判断。见 pipeline/advice.py。
    "advice": (
        "你是**裁决参谋**。人工正在为流水线闸门上的「待确认项」做决定，你来帮他判断"
        "风险与收益，让他不必靠猜。\n"
        "你只做判断，**不执行任何改动**：不写代码、不改需求文字、不替人工拍板、"
        "不修改他已有的裁决。\n"
        "【纪律】\n"
        "  · recommendation 必须是**能直接采用的取值**（如「画布固定 800×600」）；"
        "「视情况而定」「建议进一步确认」这类等于没答，视为不合格；\n"
        "  · benefits / risks 各给真正有区分度的 1~3 条。**不要凑数**，也不要写成"
        "通用套话（「提升用户体验」这种没有信息量）；\n"
        "  · reversibility 要说清「选错了以后改起来多贵」：easy＝改一处即可、"
        "moderate＝要动多处下游产物、hard＝要推翻已完成的实现；\n"
        "  · evidence 只写你能从给定材料里指出出处的依据（需求原文 / 阶段产物 / 检索片段）；"
        "**材料里没有依据就明说是推测**，不要编造数字、文件名或第三方事实；\n"
        "  · confidence 保守给：材料不足就给 low，不要装确定。\n"
        "【红线】不得臆造需求里没有的内容；不得给出材料无法支撑的「确定结论」。\n" + _TAIL
    ),
    "pm": (
        "你是资深产品经理，负责需求分析、边界界定与验收标准制定，产出标准化 PRD 供下游架构/开发/测试使用。\n"
        "绝对红线（违反即判不合格）：\n"
        "  1. 不给任何技术方案、架构设计、代码实现思路；\n"
        "  2. 不做任务拆解、排期、人员分工；\n"
        "  3. 不编造需求里没提到的事实、功能、约束。特别注意：本次**没有提供任何存量代码**，"
        "你无从知道真实的模块与文件结构，impact_areas 只能写**能力域**"
        "（如「用户输入处理」「渲染管线」），严禁编造具体文件路径、类名或目录结构；"
        "需要依据代码才能判断的事情，写成 open_questions 并给出默认取值。\n"
        "执行步骤（按序完成，不要跳步）：\n"
        "  1. 通读需求，提取全部明确信息；\n"
        "  2. 找出所有未明确、有歧义、缺失的信息，整理成未决清单；\n"
        "  3. 为每条未决项给出建议方案与默认取值；\n"
        "  4. 拆解功能需求，逐条编写可验证的验收标准；\n"
        "  5. 对照下方自检清单逐项核对；\n"
        "  6. 输出 JSON。\n"
        "字段规范（严格按此填写，不要增删字段）：\n"
        "  · background：需求背景与动机；goal：本次要达成的结果，避免「体验更好」这类空话。"
        "**需求里给了可度量指标就引用它；没给就不要为了凑「量化」而编造数字**"
        "—— 编造的指标会一路传下去，变成下游的假验收基线；\n"
        "  · target_users：目标使用者角色列表；\n"
        "  · in_scope / out_of_scope：必须**完全互斥**且具体；out_of_scope 至少 1 条，写清这次不做什么"
        "（不划边界时下游会顺手扩大改动范围）；\n"
        "  · impact_areas：每项 {area, impact, severity}，area 是可识别的能力域；\n"
        "  · functional_requirements：按 FR-01、FR-02… 编号，每项 {id, title, description, priority, acceptance}，"
        "acceptance 是**字符串数组**，每条要能直接转成测试用例"
        "（FR 编号只用于本字段；out_of_scope 用纯文字描述，不要编号）；\n"
        "  · acceptance_criteria：整体验收标准数组，至少 1 条；"
        "每条都要能被**独立验证**，禁止「通过测试用例验证」这类同义反复；\n"
        "  · priority 取值与含义：high＝不做则需求不成立；medium＝影响核心体验，本次应完成；low＝可延后。\n"
        "未决信息（最容易被忽视、也最致命）：\n"
        "  · 需求没说清、有歧义、或你自行做了默认假设的地方，**全部**进 open_questions，不得遗漏；\n"
        "  · 每条必须给 recommendation（你建议怎么定）与 assumed_answer（未获人工确认时下游按此推进的默认取值）。"
        "只提问题不给答案的条目是无效的 —— 下游拿不到答案只能各自脑补，会产出需求里根本没写的东西；\n"
        "  · severity **必填**，只有两档：high＝猜错会导致大范围返工或方案推翻；"
        "low＝猜错只需小范围调整。拿不准时不要轻易标 high；\n"
        "  · why_it_matters **必填**：说清这条为什么会影响范围或实现；\n"
        "  · impact_if_wrong 说明猜错后的具体影响；\n"
        "  · unknowns 与 clarifying_questions **两个都要填**，内容与 open_questions 一一对应"
        "（供人工快速扫读），但答案落在 open_questions 的 recommendation / assumed_answer。\n"
        "输出前自检（逐条核对后再输出）：\n"
        "  1. 是否写了技术方案、代码思路或任务拆解？有则删除；\n"
        "  2. in_scope 与 out_of_scope 是否完全互斥、无重叠？\n"
        "  3. 每条功能需求的 acceptance 是否都能转成测试用例？有没有「体验好」「速度快」这类模糊说法？\n"
        "  4. 所有未决项都进 open_questions 了吗？每条都有 recommendation 与 assumed_answer 吗？\n"
        "  5. priority / severity 的取值是否都在规定选项内？\n"
        "  6. 是否编造了需求里没有的内容（尤其具体模块名、文件名，以及**没有依据的数字指标**）？\n"
        "  7. clarifying_questions 与 unknowns 是否都填了、且与 open_questions 一一对应？\n" + _TAIL
    ),
    # 注意：本阶段是三个档位里上下文最紧的（num_ctx 8192 = system + 代码片段 + 3072 输出），
    # system 每涨 100 token，架构师就少看约 230 字符的代码。因此这里的纪律要写全但要写短。
    "architect_assess": (
        "你是资深存量代码评估架构师，只做客观事实盘点，为后续「最小侵入变更」提供依据。\n"
        "绝对红线（违反即不合格）：\n"
        "  1. 严禁编造片段里不存在的路径、函数名、接口名、依赖或测试；\n"
        "  2. modules[].path 必须与片段中的相对路径逐字一致；无代码片段时 modules 必须为空数组；\n"
        "  3. 一切推测只能写进 uncertainties，不得混入正式字段；\n"
        "  4. 不输出任何改造建议、优化思路或设计方向。\n"
        "字段规范：\n"
        "  · modules 每项 {path, role, change_risk, risk_details, entry_points, dependencies}；\n"
        "      change_risk：high＝核心或公共依赖，改动易大面积回归；medium＝业务模块，影响局部；"
        "low＝边缘工具；risk_details 说明判断依据。\n"
        "      entry_points / dependencies 只写片段里确实看到的，看不到就留空数组"
        "—— 宁可空着也不要编造函数名；\n"
        "  · reusable_hooks：片段中**已存在**的扩展点/接口，不得设计新接口；\n"
        "  · compatibility_constraints：不能破坏的对外契约（接口格式、数据结构、配置项、依赖版本、调用约定）；\n"
        "  · forbidden_paths：应回避的高风险目录；baseline_tests：可作回归基线的校验点；\n"
        "  · uncertainties 每项 {issue, assumption, confidence}：疑点 / 你的推测 / 可信度(high|medium|low)；"
        "目录结构猜测、未提供的模块、未知的参数与版本都放这里。\n"
        "执行步骤：① 逐行通读代码片段；② 按字段归类，路径与名称逐字核对；③ 自检后输出 JSON。\n"
        "自检：① 每个路径与函数名都在片段里真实出现吗？② 混进改造建议了吗？"
        "③ 无片段时 modules 为空、且所有猜测都进了 uncertainties 吗？\n" + _TAIL
    ),
    "architect_plan": (
        "你是资深架构师，需要给出最小侵入的变更方案与任务拆解，供下游开发按补丁逐步落地。\n"
        "绝对红线（违反即不合格）：\n"
        "  1. 严禁编造代码片段与存量评估结论里不存在的路径、函数名、接口、依赖，"
        "以及数据库表、外部服务、框架这类**技术事实**；"
        "changes[].path 必须能在片段或评估结论里找到依据；\n"
        "  2. 严禁超出产品经理的 in_scope —— 需求没提的功能、优化、可配置项一律不做，"
        "拿不准就写进 uncertainties，不要顺手加上去；\n"
        "  3. 严禁顺手重构：不得触碰 forbidden_paths 里的路径，不得做与任务无关的格式化、重命名、"
        "依赖升级或额外抽象；\n"
        "  4. 每个 changes[].path 必须至少被一个 task 覆盖，且 tasks[].target_files 只能指向 "
        "changes 里出现过的文件 —— 开发要按 covers_tasks 与方案任务机械核对覆盖，"
        "两边对不上会直接判「实现不完整」。\n"
        "  5. 如果本次交付需要**能跑起来**（新增可运行程序 / 脚本 / 服务），方案**必须**规划一个"
        "可执行入口文件（main.py / __main__.py / run.py 等，带 `if __name__ == '__main__':` "
        "且运行时有输出），并把它写进 changes 与某个 task 的 target_files —— "
        "方案不写它，开发受白名单约束**无权创建**，运行验证会一直判「没有可执行入口」。\n"
        "字段规范：\n"
        "  · strategy 必须与 changes / tasks 自洽，并**写明技术栈（语言 + 运行环境）**；\n"
        "  · changes 每项 {path, intent, approach, minimality_reason}：minimality_reason **必须具体**，"
        "要讲清「为什么这是最小改法、相比其它做法少动了什么」，「改动少」这类空话不合格；\n"
        "  · tasks 每项 {id, title, target_files, acceptance, depends_on}：id 按 T-01、T-02… 编号"
        "（开发要用它填 covers_tasks，编号必须规范）；acceptance 必须能直接转成测试用例；"
        "depends_on 只引用本方案里已定义的 id；\n"
        "  · rollback 说明回滚方式；risks 记录本次变更引入的残余风险。\n"
        "步骤：① 读 PM 范围/验收标准与存量评估结论 → ② 逐文件设计最小侵入变更 → "
        "③ 拆任务并编号、给出依赖 → ④ 自检后输出 JSON。\n"
        "自检：① 每个 changes[].path 都有依据吗（没依据就是编造）？② changes 与 tasks 相互覆盖了吗？"
        "③ 有没有超出 in_scope 的改动？④ minimality_reason 够具体吗？⑤ forbidden_paths 没碰吧？"
        "⑥ 上轮 required_fixes 逐条回应了吗？⑦ acceptance 都能转测试用例吗？\n" + _TAIL
    ),
    "dev": (
        "你是资深开发工程师，严格按方案在存量代码上做最小侵入的增量变更，产出可被机械校验并自动套用的补丁。\n"
        "绝对红线（违反即不合格）：\n"
        "  1. 严禁编造存量代码里不存在的文件路径、函数名、类名，"
        "也不得臆造数据库表、外部服务或框架；\n"
        "  2. 严禁修改方案白名单以外的文件，严禁做与任务无关的格式化、重命名或额外优化；\n"
        "  3. 严禁谎报完成：没实现的任务必须逐条写进 not_implemented；\n"
        "  4. 素材里没有给出完整上下文的符号，不要硬改 —— 写进 not_implemented 并说明缺什么。\n"
        "增量粒度纪律：一次 edit ＝ **一个符号（函数/类）的补丁**，不是整文件；不同符号必须拆成多条 edit。\n"
        "  · target_symbol：被改的函数/类名；新增时填新符号名。\n"
        "  · anchor：从【存量代码片段】里**逐字**抄出的 1~3 行原文（含缩进），必须能在文件里唯一定位，"
        "用于把补丁贴回原位；不要改写、不要省略其中的符号。"
        "**只有 `full_symbol` 可以留空 anchor**（它靠 target_symbol 定位），另两种模式留空必然判负。\n"
        "  · anchor 纪律（高频出错点）：函数定义的 anchor 必须抄**完整的 def 首行（含全部参数，"
        "可能跨多行直到冒号）**——绝不要自造或缩写签名。假设原文里的真实签名是 "
        "`def refresh_inventory(sources, force=False, hooks=None, max_retries=3):`，就一字不差地抄这整段；"
        "若自行缩写成 `def refresh_inventory(sources):`，一定匹配不到，会被机械校验直接判负。\n"
        "  · patch_mode（三选一，严禁自定义；声明错了会被机械判负）：\n"
        "      - `insert_after`：patch 是要**插到 anchor 之后**的新代码（新增函数/新增分支用这个）；\n"
        "      - `replace_span`：patch **替换 anchor 覆盖的那几行**（小改动用这个）；\n"
        "      - `full_symbol`：patch 是 target_symbol 的**完整替代实现**（必须含定义行、必须把原函数整段给全）。\n"
        "    ⚠ **原文里还不存在的符号一律用 `insert_after`，绝不要用 `full_symbol`** —— "
        "full_symbol 要求原符号已存在，否则会被判 `symbol_not_found` 直接打回。\n"
        "    给不出完整符号时就用前两种，**不要谎称 full_symbol**（原文该符号有多少行是已知的，写少了会被抓出来）。\n"
        "  · patch：统一 diff，或按 patch_mode 的代码块。**单条补丁控制在 80 行以内**，"
        "超了就拆成多条小补丁（一次写不深长补丁，拆小反而更容易写对）。不允许写“……其余同上”这类省略。\n"
        "  · covers_tasks：这条补丁对应**架构师变更方案**里的哪些 task id —— "
        "填方案 tasks[].id（形如 `T-01`），**不是 PM 的功能需求编号 `FR-01`**；"
        "填错或漏填会让覆盖审计判定任务未被覆盖。\n"
        "诚实性纪律：not_implemented 逐条给 {task, reason}；self_checks 每条都要带 "
        "evidence（文件+符号+行号或可执行命令）—— 谎报完成会被评审直接打回；"
        "与方案不一致之处写进 deviations 的 {item, reason}；拿不准的假设写进 uncertainties 的 "
        "{issue, assumption, confidence}，不要靠猜直接改代码。\n"
        "**必须在 `run` 字段里给出「怎么把它跑起来」的那一条命令**（如 `python main.py`）："
        "运行验证会真的执行它。请确保该命令指向的入口文件确实存在、且带 "
        "`if __name__ == '__main__':`，运行时会产生实际输出（而不是 import 完就退出）。"
        "写不出可运行入口，就说明这轮交付不完整。\n"
        "其余要求：严格遵循存量命名、日志、异常处理与依赖版本约定；只改方案中列出的文件（白名单），"
        "不改无关代码、不做格式化重排。\n"
        "**技术栈必须与方案一致且单一**：不要把同一份能力用两种语言各实现一遍"
        "（例如同时产出 `snake.py` 与 `snake.js`）—— 那会让入口命令、依赖与测试全部对不上。\n"
        "执行步骤：① 读方案任务清单与白名单，确定本次要改的目标符号；② 在片段中定位符号并逐字提取 anchor；"
        "③ 按变更类型选定 patch_mode；④ 编写该粒度的 patch；⑤ 整理 not_implemented / deviations / "
        "self_checks / uncertainties；⑥ 对照下方自检清单后输出 JSON。\n"
        "输出前自检：① anchor 是逐字复制的吗？有没有缩写签名？② patch_mode 是三个枚举值之一吗？"
        "③ patch 完整吗？有没有省略？是否 ≤80 行？④ 只改了白名单内的文件吗？"
        "⑤ 未实现的任务都进 not_implemented 了吗？⑥ 输出是纯 JSON 吗？\n" + _TAIL
    ),
    "test": (
        "你是专业测试工程师，针对本次代码变更产出新功能（new）/ 回归（regression）/ 兼容（compat）三类测试方案。\n"
        "绝对红线（违反即不合格）：\n"
        "  1. 严禁编造不存在的文件路径、函数名、命令、工具或测试数据；automated_commands 必须真实可执行；\n"
        "  2. 严禁隐瞒覆盖缺口：覆盖不到的场景必须逐条写进 coverage_gaps，不得假装全覆盖；\n"
        "  3. 严禁模糊表述：expected 必须可观测、可比对，禁止「验证正常」「功能可用」这类说法；\n"
        "  4. 严禁省略 steps 与 expected。\n"
        "用例规范：\n"
        "  · 三类**缺一不可**（new / regression / compat），每类至少一条；总数控制在 15 条以内，只测本次变更相关；\n"
        "  · id 按类型编号：new 用 NEW-01…，regression 用 REG-01…，compat 用 COMP-01…；\n"
        "  · target **必须写到符号级**（被验证的函数 / 类名，必要时带文件名如 `game_logic.py::Snake.move`）；"
        "只写文件名（如 `game_logic.py`）不合格 —— 编排器要用它和补丁的 target_symbol 机械核对覆盖，"
        "粒度对不上就发现不了「写了很多用例、真正改的东西却没测到」；\n"
        "  · 本次改动的符号务必**逐个**出现在某条用例的 target 里；\n"
        "  · steps 一步一个动作（含操作对象与输入参数）；\n"
        "  · expected 必须能直接转成断言：写成**可观测的具体结果**（数值、状态、返回值、界面元素），"
        "例如「按右键后蛇头坐标从 (3,5) 变为 (4,5)」；"
        "「正常运行」「无异常」「符合配置」「功能正确」这类无法验证的说法一律不合格；\n"
        "  · automated_commands 每项给 {command, description}：命令要能**验证行为**"
        "（跑测试套件、断言脚本、校验命令），而不是「直接启动程序看一眼」；"
        "description 说明它验的是哪条用例；\n"
        "    ⚠ **至少要有一条「断言型」命令**：形如 "
        "`python -c \"import m; assert m.f(2) == 4\"` 或 `python -m unittest`，"
        "让机器用退出码替你判断对不对。只写 `python main.py` 这类「启动一下」的命令，"
        "退出码 0 证明不了任何行为（真机出现过：三条命令全 ok，但交付物其实跑不起来）；\n"
        "    **命令必须能在本环境直接跑起来**：优先用 Python 标准库（`python -m unittest`）"
        "与项目自带依赖，不要声明环境里没装的第三方工具（如 `pytest`）—— "
        "声明了只会记一条「程序不可用」，既验不了东西又白占一条命令位；\n"
        "  · coverage_gaps 每项给 {gap, reason, impact}：缺口是什么、为什么覆盖不了、对结论影响多大；\n"
        "    ⚠ **改动的符号必须逐个被某条用例的 target 覆盖**，否则会被机械判为漏测并直接打回。"
        "确实无需单独用例的（例如只改了内部常量），必须在 coverage_gaps 里写明原因是哪个符号、"
        "为什么不需要 —— 这是唯一的豁免途径，不写就会被当成漏测；\n"
        "  · 拿不准的假设写进 uncertainties 的 {issue, assumption, confidence}；risks 记录本次变更引入的残余风险。\n"
        "执行步骤：① 梳理本次变更的功能点、受影响的存量模块、需兼容的接口；"
        "② 设计 new 用例（覆盖正常 / 边界 / 异常场景）；③ 设计 regression 用例（受影响的存量核心功能）；"
        "④ 设计 compat 用例（接口、数据格式、依赖版本）；⑤ 整理可执行命令与覆盖缺口；⑥ 对照下方自检后输出 JSON。\n"
        "输出前自检：① new / regression / compat 三类都覆盖了吗？② 每条 steps 都可复现吗？"
        "③ expected 具体可验证吗（有没有「正常」「没问题」「功能可用」）？④ 命令都真实可执行吗？"
        "⑤ 覆盖不到的场景都进 coverage_gaps 了吗？⑥ 输出是纯 JSON 吗？\n" + _TAIL
    ),
    "review": (
        "你是评审人（架构师视角），判定本次交付是否可以被接受。\n"
        "判定标准：① 改动是否落在白名单内、是否保持最小侵入；② 是否满足 PM 的验收标准；"
        "③ 测试是否覆盖 new/regression/compat 三类且有可执行命令；④ 是否引入未声明的兼容性风险；"
        "⑤ 实现是否**真的覆盖了方案的任务清单**（见【实现覆盖审计】），补丁粒度是否足以落地。\n"
        "verdict 取值：pass / rework_dev（方案对但实现有问题）/ rework_architect（方案本身有问题）。\n"
        "返工项必须逐条声明作用域 required_fixes_detail[].scope，**它决定下一轮回哪个阶段**：\n"
        "  · in_material —— 实现层就能改掉（改补丁、补测试用例文本…）→ 下一轮回开发；\n"
        "  · architect —— **方案层**才能改：方案漏规划了某个文件、漏定义接口/数据结构、"
        "任务边界划错导致某产出没人负责、依赖没在方案里声明 → 下一轮**直接回架构师方案**；\n"
        "  · needs_external —— 需要运行系统、访问外部环境或人工确认才能定论（接口是否存在、阈值取多少、"
        "字段是否齐全、迁移脚本实际执行结果…），编排器会把这类自动改判进 residual_risks。\n"
        "只要有一条判 architect，下一轮就从**方案**重跑；全是 in_material 才只回开发。\n"
        "另外，每条返工项**尽量填** required_fixes_detail[].path（相对仓库根的文件路径）：\n"
        "它决定这条整改要求被派给哪个文件。不填的话开发只能拿到一串无主的文字、靠猜去改，\n"
        "结果就是反复去改评审根本没抱怨的文件，返工永远收敛不了。\n"
        "⚠ **不要把方案层根因写成 in_material** —— 开发被约束在方案的 changes 范围内，改不动它，"
        "只会白烧一轮（真机教训：方案没规划 `direction` 模块，却被判实现层，下一轮又撞同一个错）。\n"
        "判 architect 最可靠的依据是【运行验证结果】里的 mechanical_facts（机制算出的集合事实："
        "某个缺失的模块在不在方案清单里）—— 直接照着它分区，不要凭感觉。\n"
        "如果**所有**返工项都是 needs_external，那就不要投 rework（会被机制强制放行，等于白投）。\n"
        "可直接判 rework_dev 的硬信号（**以【补丁机械校验】的结果为准，不要凭感觉**）："
        "anchor 在原文里找不到或不唯一；声明 `full_symbol` 但补丁行数远少于原文该符号（说明是片段却谎称完整）；"
        "patch 里没有定义声明的符号；patch 与原文完全相同；covers_tasks 里有方案任务没被任何补丁覆盖；"
        "self_checks 声称完成但补丁规模明显不足以支撑。这些都属于本轮材料内可改（`in_material`），"
        "请写成可执行的返工项（例如：把某个符号的 full_symbol 补丁补全，或把 patch_mode 改声明为 insert_after）。\n"
        "residual_risks 每项给 {issue, reason, impact}。\n"
        "评审顺序：① 先读末尾【补丁机械校验】【实现覆盖审计】【测试覆盖审计】找硬信号；"
        "② 核对 5 条判定标准；③ 按作用域分类；④ 定 verdict 后输出。\n"
        "自检：① 结论与机械校验一致吗？② 作用域分准了吗（external 误判成 in_material 会白烧一轮）？"
        "③ rework_dev（补丁/测试层面的问题）与 rework_architect（方案本身有错）分对了吗？"
        "④ 方案层根因（漏文件 / 漏接口 / 任务边界错）标成 architect 了吗？\n"
        "人工已确认的事实（见【人工已确认的事实】）不得再作为未决项或 rework 依据。\n" + _TAIL
    ),
}


def _requirement_block(requirement: str) -> str:
    return "【用户需求原文】\n" + requirement.strip()


def _upstream(title: str, obj: Any, str_tokens: int = 160, list_items: int = 12) -> str:
    if not obj:
        return f"【{title}】\n（无）"
    return f"【{title}】\n{distill_json(obj, str_tokens=str_tokens, list_items=list_items)}"


def _code_block(excerpts_text: str) -> str:
    return "【存量代码片段（按相关度挑选，可能被截断）】\n" + excerpts_text


def _current_code_block(text: str | None) -> str:
    """上一轮产出后**仓库里真实存在的文件**（新建项目下开发唯一能看见自己产出的途径）。

    真机 run 20260924-235001：新建项目的检索池为空，开发写完 2.2KB 代码后下一轮
    一个字都看不到，只能照着方案重新发明那 5 个文件 —— 于是补错 import、丢入口点、
    把已跑通的实现改坏，返工项也一直修不掉。块标题特意把「在此基础上修改」写死，
    因为默认行为就是重新生成整个文件。
    """
    if not text or not text.strip():
        return ""
    return (
        "【当前项目已有代码（上一轮产出后仓库里的**真实文件**；请在此**基础上修改**，"
        "不要重新发明文件、不要丢掉已有实现、改 import 前先看清符号在哪个文件里）】\n"
        f"{text}"
    )


def _feedback_block(label: str, items: list[str] | None) -> str:
    if not items:
        return ""
    return f"【{label}】\n" + "\n".join(f"- {x}" for x in items)


# ----------------------------------------------------------------- 各阶段片段构造
def parts_intake(requirement: str) -> list[str]:
    return [
        _requirement_block(requirement),
        "【任务】按角色定义逐步执行，输出结构化需求初稿（纯 JSON，字段严格按 Schema 填写）。",
    ]


def parts_advice(
    requirement: str,
    stage_label: str,
    pending: list[dict],
    question: str,
    focus: str = "",
    history: list[dict] | None = None,
    artifact_text: str = "",
) -> list[str]:
    """裁决参谋的输入片段（旁路环节，见 pipeline/advice.py）。

    材料顺序刻意「待确认项在前、阶段产物在后」：``fit_prompt`` 是从**尾部**裁剪的，
    产物细节被裁掉可以接受，但「人工在问哪一条、这条现在是什么状态」绝不能被裁掉 ——
    裁掉它模型就会答非所问。

    ``pending`` 是**通用形状**的待确认项（由 ``advice.pending_items`` 把各阶段产物
    归一化而来）：``{"element","value","decided","importance","why"}``。
    """
    parts = [_requirement_block(requirement)]
    lines = [f"【当前环节】{stage_label}", ""]
    if focus:
        lines += [f"【人工正在问的这一条】{focus}", ""]
    if pending:
        lines += ["【该环节的全部待确认项】（已被人工裁决的以「裁决＝」标出）", ""]
        for i, it in enumerate(pending, 1):
            decided = str(it.get("decided") or "").strip()
            lines.append(
                f"  {i}. {it.get('element')}"
                f"｜建议取值：{it.get('value') or '（补强未给出）'}"
                + (f"｜**裁决＝{decided}**" if decided else "｜尚未裁决")
                + (f"｜影响：{it.get('why')}" if it.get("why") else "")
            )
        lines.append("")
    else:
        lines += ["【该环节的待确认项】无（本次属于补充提问）", ""]
    if history:
        lines += ["【此前的问答（保持一致，不要自相矛盾）】", ""]
        for h in history:
            ans = h.get("answer")
            summary = ""
            if isinstance(ans, dict):
                summary = str(ans.get("recommendation") or ans.get("answer") or "")
            else:
                summary = str(ans or "")
            lines.append(f"  问：{h.get('question')}")
            lines.append(f"  答：{summary[:400]}")
        lines.append("")
    lines += [
        "【本次问题】" + (question or "请就上述待确认项给出你的判断与建议。"),
        "",
        "【任务】按角色定义输出结构化判断（纯 JSON，字段严格按 Schema 填写）。"
        "直接回答本次问题，建议必须具体到能直接采用。",
    ]
    parts.append("\n".join(lines))
    if artifact_text:
        parts.append("【该环节的产物正文（判断依据，可能被截断）】\n" + artifact_text)
    return parts


def intake_has_decisions(intake: Any) -> bool:
    """补强产物里是否已有人工裁决（任一条待确认项带 final_decision）。

    裁决是**并回补强产物本身**的（见 server._save_intake_decisions），
    所以判断依据直接看产物，不再另存一份平行的裁决清单。
    """
    return any(str(row.get("final_decision") or "").strip() for row in intake_items(intake))


#: 「把问题原样退回」的伪默认值标记：出现这些词、又拿不出任何具体取值，就等于没给答案。
_INTAKE_RESTATE = (
    "建议补充", "需明确", "需要明确", "待明确", "需要澄清", "需澄清",
    "待确认", "需要确认", "待定", "未明确", "未提供", "未指定",
    "视情况", "由用户决定", "由用户确认", "需要进一步",
)
#: 命中任一即视为给出了「能直接照做」的具体取值（哪怕前面还带着「建议补充：」）。
_INTAKE_CONCRETE = r"\d|×|使用|采用|默认|即|例如|如：|="


def _intake_restating(*texts: Any) -> bool:
    """是不是「把问题原样退回来」的伪默认值（带着标记词、却没有具体取值）。"""
    joined = " ".join(str(t or "") for t in texts if t)
    if not joined:
        return False
    if not any(mark in joined for mark in _INTAKE_RESTATE):
        return False
    return not re.search(_INTAKE_CONCRETE, joined)


def _intake_key(text: Any) -> str:
    """主题指纹：去掉标记词与非文字字符，只留内容 —— 用来发现逐字重复。"""
    out = str(text or "")
    for mark in _INTAKE_RESTATE:
        out = out.replace(mark, "")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", out.lower())


def intake_items(intake: Any) -> list[dict]:
    """补强产物的**待确认项**（单一列表）—— 新旧两种产物形状都读得懂。

    新形状：``pending_items``。
    旧形状（2026-09-25 之前）：``missing_elements``（element/default_assumption/
    importance）+ ``clarifying_questions``（question/suggested_answer/impact）。两者语义
    重叠，真机上同一主题会在两边各出一条（游戏分辨率 / 游戏窗口尺寸要求），人工得在两个
    框里各填一遍。这里**合并**成同一形状（question→element、impact→why、
    suggested_answer→default_assumption），旧运行不必迁移就能照常显示与裁决。

    只在没有 ``pending_items`` 时才回退读旧字段：两种形状同时存在时以新字段为准，
    避免同一批条目被读两遍。
    """
    if not isinstance(intake, dict):
        return []
    rows: list[dict] = []

    def push(row: Any) -> None:
        if not isinstance(row, dict):
            return
        item = {
            "element": row.get("element") or row.get("question") or "",
            "why": row.get("why") or row.get("impact") or "",
            "default_assumption": row.get("default_assumption") or row.get("suggested_answer") or "",
            "importance": row.get("importance") or "medium",
        }
        for carry in ("final_decision", "confirmed", "needs_value"):
            if row.get(carry):
                item[carry] = row[carry]
        rows.append(item)

    fresh = [r for r in intake.get("pending_items") or [] if isinstance(r, dict)]
    if fresh:
        for row in fresh:
            push(row)
        return rows
    for row in intake.get("missing_elements") or []:
        push(row)
    for row in intake.get("clarifying_questions") or []:
        push(row)
    return rows


def apply_intake_decisions(intake: Any, decisions: Any) -> Any:
    """把人工裁决**在读取时**并回补强产物（幂等；无匹配则原样返回）。

    为什么读取时也要做一次：原本只有 HTTP 保存接口在**写入时**并回，那一步一旦因为
    任何原因没生效（真机出过两次——前端提交的 kind 与服务端匹配规则错位），产物里就
    没有 ``final_decision``，下游随即把**已经裁决过**的条目当成「还没定」再问一遍，
    而 ``state.intake_decisions`` 里明明存着。

    裁决的真源是 state，产物只是它的载体。在消费点再并一次，这类信息丢失就再也
    传不下去 —— 与「补丁幂等闸」「视图幂等」是同一种加固思路。
    """
    if not isinstance(intake, dict):
        return intake
    by_ref = {
        str(row["ref"]): str(row.get("decision") or "").strip()
        for row in (decisions or [])
        if isinstance(row, dict) and row.get("ref") and str(row.get("decision") or "").strip()
    }
    if not by_ref:
        return intake
    changed = False
    items: list[dict] = []
    for row in intake_items(intake):
        item = dict(row)
        topic = str(item.get("element") or "")
        if topic in by_ref and not str(item.get("final_decision") or "").strip():
            item["final_decision"] = by_ref[topic]
            item["confirmed"] = True
            changed = True
        items.append(item)
    if not changed:
        return intake
    out = dict(intake)
    out["pending_items"] = items
    out.pop("missing_elements", None)   # 合并进 pending_items 后收掉旧字段，避免两种形状并存
    out.pop("clarifying_questions", None)
    out["confirmed_facts"] = [
        f"{r.get('element')}：{r.get('final_decision')}" for r in items if r.get("final_decision")
    ]
    return out


def apply_pm_decisions(scope: Any, decisions: Any) -> Any:
    """同上，对象是 PM 的 ``open_questions``。"""
    if not isinstance(scope, dict):
        return scope
    by_ref = {
        str(row["ref"]): str(row.get("decision") or "").strip()
        for row in (decisions or [])
        if isinstance(row, dict) and row.get("ref") and str(row.get("decision") or "").strip()
    }
    if not by_ref:
        return scope
    changed = False
    rows: list[dict] = []
    for row in scope.get("open_questions") or []:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        topic = str(item.get("question") or "")
        if topic in by_ref and not str(item.get("final_decision") or "").strip():
            item["final_decision"] = by_ref[topic]
            item["confirmed"] = True
            changed = True
        rows.append(item)
    if not changed:
        return scope
    out = dict(scope)
    out["open_questions"] = rows
    out["confirmed_facts"] = [
        f"{r.get('question')}：{r.get('final_decision')}" for r in rows if r.get("final_decision")
    ]
    return out


def tidy_intake(intake: Any) -> tuple[Any, list[str]]:
    """补强产物的机械兜底，返回 ``(整理后的产物, 给人工看的说明)``。

    三件**确定性**的事（依据真机 run 20260925-140707）：
      1) **两类合并为一条列表**：同一主题此前会在 missing_elements 与
         clarifying_questions 里各出一条，人工得填两个框 —— 合并后结构上不可能重复；
      2) 同主题**逐字重复**的条目去重（归一化后完全相同才算）；
      3) 「把问题原样退回来」的建议取值（「建议补充：需明确…」）**改成空串**：空串是
         「补强也拿不准、等你裁决」的唯一表达，界面据此显示「补强没给具体取值」，而不是
         让人误以为有个能直接用的默认值。新形状下空串本就是正常值，这条主要用于收敛旧
         产物、以及兜住模型没守纪律的情况。

    刻意**不做**模糊/语义合并：「分辨率」与「窗口尺寸」这类换了说法、主题相同的情况文本
    无法可靠判定（「键盘控制」与「触屏控制」的字符重叠度同样很高）。宁可漏并，也不能把
    两个真的不同的问题并成一条 —— 那比重复更坏。语义去重交给提示词的纪律。
    """
    if not isinstance(intake, dict):
        return intake, []
    legacy = not (intake.get("pending_items") or []) and bool(
        intake.get("missing_elements") or intake.get("clarifying_questions")
    )
    kept: list[dict] = []
    seen: set[str] = set()
    dup = 0
    restated = 0
    for row in intake_items(intake):
        key = _intake_key(row.get("element"))
        if key and key in seen:
            dup += 1
            continue
        if key:
            seen.add(key)
        if _intake_restating(row.get("default_assumption")):
            row["default_assumption"] = ""
            restated += 1
        if not str(row.get("default_assumption") or "").strip():
            row["needs_value"] = True
        kept.append(row)

    out = dict(intake)
    out["pending_items"] = kept
    # 旧字段合并进 pending_items 后必须去掉：留着会让「两种形状同时存在」，
    # 下游一旦读旧字段就等于绕过这里的去重。
    out.pop("missing_elements", None)
    out.pop("clarifying_questions", None)

    notes: list[str] = []
    if legacy:
        notes.append("已把「缺失要素」与「澄清问题」合并为单一的待确认项列表（旧产物形状）")
    if dup:
        notes.append(f"去掉主题重复 {dup} 条（同一主题只保留一处）")
    if restated:
        notes.append(f"{restated} 条建议取值其实是把问题退了回来，已按「未给出取值」处理")
    needs = sum(1 for r in kept if r.get("needs_value"))
    if needs:
        notes.append(f"{needs} 条未给出具体取值，需人工填写")
    return out, notes


def finalized_intake_view(intake: Any) -> Any:
    """裁决后的下游视图：把「问答模式」整理成**陈述式结论**。

    已裁决的条目不再以「要素 + 默认假设 + 裁决」的形式流转 —— 那种问答形式会让下游
    （以及 PM 自己）继续把它当成待确认项反复提问。这里：
      · 已裁决 → 收进 `confirmed_facts`，并并入 `refined_requirement.constraints`（标「已确认：」）；
      · 未裁决 → 仍留在 `pending_items`（这是真的还没定的）。
    """
    if not isinstance(intake, dict):
        return intake
    facts: list[str] = []
    open_items: list[dict] = []
    for row in intake_items(intake):
        decision = str(row.get("final_decision") or "").strip()
        if decision:
            facts.append(f"{row.get('element')}：{decision}")
        else:
            open_items.append(row)
    if not facts:
        return intake
    view = dict(intake)
    view["confirmed_facts"] = list(intake.get("confirmed_facts") or facts)
    view["pending_items"] = open_items
    view.pop("missing_elements", None)
    view.pop("clarifying_questions", None)
    req = dict(view.get("refined_requirement") or {})
    constraints = list(req.get("constraints") or [])
    # **幂等**：这个视图每次注入下游都会重算一遍，而 constraints 里可能已经带着上一次
    # 追加的「已确认：…」—— 不去重就会越滚越多。真机复现：裁决并回产物后，
    # 4 条「已确认：」在产物里已存在，这里又追加一遍，变成 8 条。
    seen_confirmed = {str(c) for c in constraints}
    for text in facts:
        line = f"已确认：{text}"
        if line not in seen_confirmed:
            seen_confirmed.add(line)
            constraints.append(line)
    req["constraints"] = constraints
    view["refined_requirement"] = req
    return view


def parts_pm(requirement: str, intake: Any = None) -> list[str]:
    """产品经理输入：原始需求 + 上游补强产物（初稿或已裁决终稿）。

    复用机制：intake 已完成结构化与要素补全，PM 不必再重新解析原始需求。
    人工裁决**并回在条目里**（`final_decision` 覆盖 `default_assumption` / `suggested_answer`），
    因此这里只需要在标题里说明「这是一份已裁决的终稿」——下游读这一份就够了，
    不再额外递一份裁决清单（否则模型同时看到两份值，反而不知道听谁的）。
    """
    parts = [_requirement_block(requirement)]
    if intake:
        decided = intake_has_decisions(intake)
        body = finalized_intake_view(intake) if decided else intake
        title = (
            "需求补强终稿（已由人工裁决：confirmed_facts 与「已确认：」约束是**确定结论**，"
            "直接采纳即可；仍列在 pending_items 里的才是尚未裁决、需要你判断的）"
            if decided
            else "需求补强初稿（上游 intake 产出：已结构化并标注默认假设；人工未确认前按默认取值推进）"
        )
        parts.append(_upstream(title, body, str_tokens=200, list_items=16))
    parts.append("【任务】界定本次变更的边界与影响范围，产出范围说明与验收标准。")
    return parts


def _scope_view(scope: Any) -> Any:
    """去掉 scope 里对**模型**冗余的原始问题文本。

    `open_questions` 已由 pm_assumptions_block 完整呈现（带建议与默认取值），
    scope 里再带一遍 unknowns / clarifying_questions 是纯冗余 —— 这两个字段
    本是为人工扫读准备的（PRD 第 7 节仍会完整展示）。

    架构师（assess/plan）与评审共用 8K 上下文，删掉这段能给代码片段腾出预算
    （实测每阶段约省 130 tok）。dev / test 有 24K 预算，不为此做裁剪。
    """
    if not isinstance(scope, dict):
        return scope
    return {k: v for k, v in scope.items() if k not in ("unknowns", "clarifying_questions")}


def pm_assumptions_block(scope: Any) -> str:
    """把 PM 对未决项给出的「默认假设」原样注入所有下游阶段。

    以前未决项以裸问题的形式往下传（unknowns 只被登记成 info 级问题、不阻塞），
    下游拿不到答案，只能各自脑补出一版互不一致的理解 ——
    需求里根本没写的东西（转盘参与度、黑白进度条…）就是这么混进方案里的。
    现在 PM 必须给出 assumed_answer，各阶段按同一套假设推进；
    有异义不允许擅自改动，写进 deviations / uncertainties 由人工裁决。
    """
    if not isinstance(scope, dict):
        return ""
    items = [q for q in (scope.get("open_questions") or []) if isinstance(q, dict)]
    if not items:
        return ""
    # 已裁决的不再以「问题 + 建议 + 默认取值」的问答形式流转 —— 那样下游（和评审）
    # 会继续把它当未决项反复提出。裁决结果直接作为确定结论给出，未裁决的才走默认假设。
    decided: list[str] = []
    pending: list[str] = []
    for q in items:
        question = str(q.get("question") or "").strip()
        if not question:
            continue
        decision = str(q.get("final_decision") or "").strip()
        if decision:
            decided.append(f"- {question} → 已裁决：{decision}")
            continue
        pending.append(f"- {question}")
        rec = str(q.get("recommendation") or "").strip()
        ans = str(q.get("assumed_answer") or "").strip()
        if rec:
            pending.append(f"  建议：{rec}")
        if ans:
            pending.append(f"  本次默认按此执行：{ans}")
    blocks: list[str] = []
    if decided:
        blocks.append("【PM 未决项（已由人工裁决，以下是**确定结论**，直接采纳）】\n" + "\n".join(decided))
    if pending:
        blocks.append(
            "【PM 未决项的默认假设（人工尚未确认，但下游一律按此推进）】\n"
            + "\n".join(pending)
            + "\n与上述假设冲突时不得擅自推翻，请写进 deviations / uncertainties 并说明理由。"
        )
    return "\n\n".join(blocks)


def parts_assess(requirement: str, scope: Any, excerpts_text: str, fixes: list[str] | None = None) -> list[str]:
    return [
        _requirement_block(requirement),
        _upstream("产品经理范围说明", _scope_view(scope), str_tokens=120, list_items=10),
        pm_assumptions_block(scope),
        _code_block(excerpts_text),
        _feedback_block("事实校正（上一次输出中的问题，必须修正）", fixes),
        "【任务】评估存量代码：识别受影响模块与改动风险，输出可复用扩展点、兼容性约束、禁改路径与回归基线。",
    ]


def fact_correction_block(missing_paths: list[str], has_code: bool) -> str:
    """检测到编造路径时的纠正片段（作为额外 prompt 片段触发一次重试）。"""
    listing = "\n".join(f"- {p}" for p in missing_paths[:12])
    if has_code:
        head = "【事实校正】以下路径在你上次的输出里出现了，但它们并不在提供的代码片段中："
    else:
        head = "【事实校正】本次没有提供任何代码片段，因此你无法知道真实的目录结构；以下路径属于臆造："
    return (
        f"{head}\n{listing}\n"
        "请重做：modules[].path 只保留代码片段中真实出现的路径；"
        "若没有任何依据，modules 置为空数组，并把这些猜测写入 uncertainties。"
    )


def human_facts_block(items: list[str]) -> str:
    """人工已确认的事实与指令：注入所有下游阶段。

    真机教训：人工在评估阶段答复了"库里没有 run 表"，但评审看不到，于是又把同一问题当残留风险提出；
    方案也没遵守"加列式迁移、不要重建表"的人工约束 —— 信息必须在整条链上传导，不能只进一个阶段。
    """
    lines: list[str] = []
    used = 0
    for item in items[:8]:
        text = re.sub(r"\s+", " ", str(item)).strip()[:300]
        if not text:
            continue
        if used + len(text) > 1200:  # 8K 的架构师/评审必须先装得下代码，人工事实不能无限膨胀
            break
        lines.append(f"- {text}")
        used += len(text)
    if not lines:
        return ""
    return (
        "【人工已确认的事实与指令（视为已知前提：不得再当作未决项、不确定项或返工理由）】\n"
        + "\n".join(lines)
    )


def implementation_audit_block(audit: dict) -> str:
    """实现覆盖审计：编排器用确定性规则核对方案任务清单，不是模型的判断。"""
    lines = [
        "【实现覆盖审计（编排器机械核对方案任务清单的结果，请据此判定，不要忽略；"
        "与上方材料不一致时以本结果为准）】"
    ]
    if audit.get("covered"):
        lines.append(f"- 已被补丁覆盖的任务：{', '.join(audit['covered'])}")
    if audit.get("missing"):
        lines.append(
            f"- **未被任何补丁覆盖的任务**：{', '.join(audit['missing'])}"
            "（既没进 covers_tasks，也没进 not_implemented）"
        )
    if audit.get("empty_implementation"):
        lines.append(
            "- **本次实现没有任何覆盖任务的补丁**（只有未实现声明）：这不是「完成」，"
            "必须判 rework_dev，并要求开发至少把能做的任务真正实现出来"
        )
    if audit.get("declared_not_implemented"):
        lines.append(f"- 开发自己声明未实现：{'；'.join(audit['declared_not_implemented'])}")
    if audit.get("unknown_tasks"):
        lines.append(f"- 引用了方案里不存在的任务 id（属于编造）：{', '.join(audit['unknown_tasks'])}")
    if audit.get("patches"):
        detail = "；".join(f"{p['symbol'] or p['path']}={p['chars']}字符" for p in audit["patches"])
        lines.append(f"- 每条补丁规模：{detail}")
    return "\n".join(lines)


def semantic_audit_block(audit: dict) -> str:
    """语义检查结果（pyright 类型诊断）：ast 抓不到、而执行也常覆盖不到的那类缺陷。

    只把**高置信**项摆到评审面前（符号/属性不存在、参数不匹配），推断性结论只报个数 ——
    后者误报率高，据此判 rework 会白烧一轮。
    """
    if not audit or not audit.get("available"):
        return ""
    diags = audit.get("diagnostics") or []
    blocking = [d for d in diags if d.get("blocking")]
    if not diags:
        return ""
    lines = [
        "【语义检查（pyright 类型诊断，编排器机械核对的结果；与上方材料不一致时以本结果为准）】",
        f"- 共 {audit.get('total', 0)} 条问题（高置信 {len(blocking)} 条），"
        f"耗时 {audit.get('elapsed_s', 0)}s"
        + (
            f"；另有 {audit['filtered_out']} 条属存量代码既有问题，未计入"
            if audit.get("filtered_out")
            else ""
        ),
    ]
    if blocking:
        lines.append("  **高置信问题**（ast 与执行都很难发现，含未被执行到的代码路径）：")
        for item in blocking[:8]:
            lines.append(f"    · {item.get('file')}:{item.get('line')} {item.get('message')}")
    rest = [d for d in diags if not d.get("blocking")]
    if rest:
        lines.append(
            f"- 另有 {len(rest)} 条推断性结论（类型不匹配之类，误报率较高），"
            "只在确认改动确实破坏契约时才据此判 rework"
        )
    return "\n".join(lines)


def impact_audit_block(audit: dict) -> str:
    """影响面清单：谁在调用本次被改的符号（编排器静态扫描的结果，不是模型的判断）。

    **刻意只给事实、不下结论**：引用存在 ≠ 上游会崩（可能参数兼容，也可能只是同名）。
    评审要拿它去判断「回归测试有没有覆盖到这些上游」，而不是直接据此判 rework_dev ——
    误判的代价是一整轮返工（dev+test+verify+review ≈5 分钟 + 一次 14B 评审）。
    """
    if not audit or not audit.get("symbols"):
        return ""
    lines = [
        "【影响面扫描（编排器静态扫出的上游调用点，与上方材料不一致时以本结果为准）】",
        f"- 本次改动的符号：{', '.join(str(s) for s in audit['symbols'])}",
    ]
    callers = audit.get("callers") or []
    # 存量上游（本轮产出之外的）才是回归测试真正该覆盖的地方，排在前面
    upstream = [c for c in callers if not c.get("in_this_round")]
    inside = [c for c in callers if c.get("in_this_round")]
    if not callers:
        lines.append(
            f"- 扫了沙箱里 {audit.get('scanned_files', 0)} 个文件，**没有发现任何调用点**"
            "（可能是新建项目、或这些符号确实没有上游）"
        )
    if upstream:
        lines.append(f"- **存量上游调用点 {len(upstream)} 处**（回归测试应优先覆盖这些）：")
        for item in upstream[:12]:
            lines.append(
                f"    · {item['symbol']} ← {item['file']}:{item['lineno']}"
                f"（{item['kind']}）{item.get('context') or ''}"
            )
        if len(upstream) > 12:
            lines.append(f"    · ……另有 {len(upstream) - 12} 处")
    if inside:
        lines.append(
            f"- 本轮产出内部的调用点 {len(inside)} 处（属产出自身的结构，回归优先级低于上面的存量上游）"
        )
    if audit.get("unparsable"):
        lines.append(
            f"- 有 {len(audit['unparsable'])} 个文件解析不了，未纳入扫描（不作为判定依据）"
        )
    lsp_info = audit.get("lsp") or {}
    if lsp_info.get("available"):
        extra = lsp_info.get("extra") or []
        ast_only = lsp_info.get("ast_only") or []
        if extra:
            lines.append(
                f"- **LSP 类型推断补上了 {len(extra)} 处 ast 匹配不到的上游**"
                "（多为 `from m import X as Y` 这类别名 import）："
            )
            for item in extra[:8]:
                lines.append(
                    f"    · {item.get('symbol')} ← {item.get('ref_file')}:{item.get('ref_line')}"
                )
        if ast_only:
            # 这是 LSP 最有价值的一条：ast 只看名字，会把同名不同物的调用也记成上游
            lines.append(
                f"- **下列 {len(ast_only)} 处上游经 LSP 类型核对并不指向本次改动的符号**"
                "（疑似同名不同物，ast 按名字匹配会误报）—— 判断影响面时请把它们降权："
            )
            for item in ast_only[:8]:
                lines.append(
                    f"    · {item.get('symbol')} ← {item.get('file')}:{item.get('lineno')}"
                    f"（{item.get('context') or ''}）"
                )
        if not extra and not ast_only:
            lines.append(
                f"- LSP 引用查找（{lsp_info.get('elapsed_s', 0)}s）与 ast 结果完全一致，无增无减"
            )
    elif lsp_info:
        lines.append(f"- LSP 引用查找不可用，上表仅含 ast 结果（{lsp_info.get('reason') or ''}）")
    lines.append(
        "- 请注意：这只是「引用存在」的事实清单，**不代表上游一定会崩**；"
        "据此判 rework 前请确认改动是否真的破坏了调用契约（参数个数/名称/返回值）。"
    )
    return "\n".join(lines)


def test_audit_block(audit: dict) -> str:
    """测试产物审计：编排器用确定性规则核对三类用例是否齐全，不是模型的判断。

    真机教训：test 曾是唯一没有机械审计的阶段（PM/架构师/dev/评审都有），
    「三类都要覆盖」只写在提示词里，模型漏掉 compat 时没有任何东西会拦。
    """
    lines = [
        "【测试覆盖审计（编排器机械核对的结果，请据此判定，不要忽略；"
        "与上方材料不一致时以本结果为准）】"
    ]
    by_type = audit.get("by_type") or {}
    lines.append(
        f"- 用例总数 {audit.get('case_count', 0)}：new={by_type.get('new', 0)}、"
        f"regression={by_type.get('regression', 0)}、compat={by_type.get('compat', 0)}"
    )
    if audit.get("missing_types"):
        lines.append(
            f"- **缺少的用例类型**：{', '.join(audit['missing_types'])}"
            "（三类缺一不可，缺失即视为测试不完整）"
        )
    if audit.get("vague_expected"):
        lines.append(
            f"- **expected 过于笼统的用例（共 {audit.get('vague_count', 0)} 条）**："
            + "；".join(audit["vague_expected"])
            + " —— 这类表述无法转成断言，应要求补成可观测结果"
        )
    if audit.get("missing_symbols"):
        # 「写了很多用例」≠「测到了改动之处」：用例 target 写文件名、补丁是符号级时，
        # 覆盖对不上（真机 run 20260925-184300 只有 1/5 对得上）。这条把它摆到评审面前。
        explained = set(audit.get("missing_symbols") or []) - set(
            audit.get("missing_unexplained") or []
        )
        lines.append(
            f"- **本次改动的符号里，有 {len(audit['missing_symbols'])} 个没出现在任何用例中**："
            + "、".join(str(s) for s in audit["missing_symbols"])
        )
        if explained:
            lines.append(
                f"    · 其中 {len(explained)} 个已在 coverage_gaps 里交代过原因（视为豁免）："
                + "、".join(sorted(explained))
            )
    if audit.get("missing_unexplained"):
        lines.append(
            f"- **漏测（既没用例覆盖、也没在 coverage_gaps 申诉）**："
            + "、".join(str(s) for s in audit["missing_unexplained"])
            + " —— 属阻断级，已由机制强制判 rework_dev；用例 target 要写到符号级"
            "（写文件名会与补丁的 target_symbol 对不上）"
        )
    if not audit.get("command_count"):
        lines.append("- **没有给出任何可执行命令**：automated_commands 为空，测试方案无法被直接执行")
    elif not audit.get("assertion_count"):
        # 有命令但全是「启动一下」：退出码 0 证明不了行为（真机 run 20260924-235001 踩过）
        lines.append(
            f"- **{audit['command_count']} 条命令里没有一条是断言型的**："
            "「启动一下、退出码 0」证明不了任何行为，请要求补 `python -c \"...assert...\"` "
            "或 `python -m unittest` 这类让机器判断对错的命令"
        )
    if audit.get("gap_count"):
        lines.append(f"- 自述覆盖缺口 {audit['gap_count']} 条（请判断其影响是否可接受）")
    return "\n".join(lines)


def grounding_warning_block(warnings: list[dict]) -> str:
    """把上游「事实接地」的告警传给下游，避免编造被当成既有事实承接。

    真机教训：architect_assess 在空仓库里编出 `pipeline/core/*`、`pipeline/db/*`，
    重试后仍在；手工核对能看到告警，但**方案阶段看不到**，于是把「数据库」当成
    评估结论里已有的依据，最后写进验收标准（"与数据库查询结果一致"）。
    上游结论不可信这件事，必须显式告诉下游。
    """
    rows = [w for w in (warnings or []) if isinstance(w, dict) and w.get("paths")]
    if not rows:
        return ""
    lines = [
        "【上游事实接地告警（以下路径在提供的代码片段里找不到依据，**不得作为方案依据**）】"
    ]
    for warn in rows[:4]:
        stage = warn.get("stage") or "?"
        paths = [str(p) for p in (warn.get("paths") or [])][:10]
        lines.append(f"- {stage} 产出：{'、'.join(paths)}")
    lines.append(
        "这些路径很可能是上游编造的。不要引用它们、不要把由它们推出的结论（表结构、"
        "外部服务、依赖、模块）当作既有事实；确有需要就写进 uncertainties 并说明依据缺失。"
    )
    return "\n".join(lines)


def plan_audit_block(audit: dict) -> str:
    """方案审计：编排器用确定性规则核对 changes / tasks / 禁改路径，不是模型的判断。"""
    lines = [
        "【方案审计（编排器机械核对的结果，请据此执行；与上方方案描述不一致时以本结果为准）】"
    ]
    lines.append(
        f"- 改动文件 {audit.get('change_count', 0)} 个 / 任务 {audit.get('task_count', 0)} 个"
    )
    if audit.get("uncovered_changes"):
        lines.append(
            f"- **没有任何任务覆盖的改动文件**：{'、'.join(audit['uncovered_changes'])}"
            "（要么给它补一个任务，要么从方案里删掉）"
        )
    if audit.get("dangling_files"):
        lines.append(
            f"- **任务引用了方案 changes 里没有的文件**：{'、'.join(audit['dangling_files'])}"
        )
    if audit.get("bad_task_ids"):
        lines.append(
            f"- **任务编号不符合 T-01 规范**：{'、'.join(audit['bad_task_ids'])}"
            "（开发要用它填 covers_tasks，编号必须规范）"
        )
    if audit.get("duplicate_task_ids"):
        lines.append(f"- 重复的任务 id：{'、'.join(audit['duplicate_task_ids'])}")
    if audit.get("unknown_depends_on"):
        lines.append(
            f"- depends_on 引用了不存在的任务 id：{'、'.join(audit['unknown_depends_on'])}"
        )
    if audit.get("forbidden_touched"):
        lines.append(
            f"- **改动了禁改路径**：{'、'.join(audit['forbidden_touched'])}"
            "（评估阶段已列为 forbidden_paths，必须换方案）"
        )
    if audit.get("mixed_languages"):
        lines.append(
            "- **方案混用了多种主语言**：" + "、".join(audit["mixed_languages"])
            + "。除非需求明确要求前后端分离，否则本次交付应当是**单一技术栈** —— "
            "同一份能力不要用两种语言各写一遍。"
        )
    if audit.get("missing_entry"):
        lines.append(
            "- **方案没有规划可执行入口**：新建的可运行项目必须有入口文件"
            "（main.py / __main__.py / run.py 等，带 `if __name__ == '__main__':` 且运行时有输出），"
            "且它要出现在 `changes` 与某个 task 的 `target_files` 里 —— "
            "方案不写它，开发就**无权创建**（受白名单约束），运行验证会一直判「没有可执行入口」。"
        )
    return "\n".join(lines)


def patch_audit_block(audit: dict) -> str:
    """补丁机械校验结果：anchor 能否定位、声明语义是否自洽（编排器算的，不是模型判断）。"""
    from .patches import STATUS_CN  # 局部导入：避免 prompts 与 patches 的导入顺序耦合

    lines = ["【补丁机械校验（编排器按原文逐条核对的结果，判定请以此为准）】"]
    if not audit.get("source_available"):
        lines.append("- 未提供仓库，无法核对补丁能否贴回原文")
        return "\n".join(lines)
    for row in audit.get("edits") or []:
        status = row.get("status")
        name = row.get("symbol") or row.get("path")
        if status == "ok":
            detail = f"anchor 命中第 {row['anchor_span'][0]}-{row['anchor_span'][1]} 行" if row.get("anchor_span") else "diff 形式"
            if row.get("patch_mode_used"):
                detail += f"，语义 {row['patch_mode_used']}"
            lines.append(f"- ✅ {name}：可套用（{detail}）")
        else:
            lines.append(f"- ❌ {name}：{STATUS_CN.get(status, status)}" + (f" —— {row['notes'][0]}" if row.get("notes") else ""))
    lines.append(
        f"（合计 {audit.get('ok', 0)} 条可套用 / {audit.get('problems', 0)} 条有问题；"
        "有问题的补丁在修好之前不应算作完成）"
    )
    return "\n".join(lines)


def _repair_block(items: list[str] | None) -> str:
    """「上一版被判不合法，请重出」—— 机制自检出来的问题回灌给**同一阶段**。

    与 `fixes` 的区别：`fixes` 是上游评审提的待改项，这是**本阶段刚产出的内容过不了机械校验**
    （写残、未闭合），只需要模型原地重写一版，不涉及任何范围变更。
    """
    if not items:
        return ""
    listing = "\n".join(f"- {x}" for x in items[:4])
    return (
        "【上一版被判为不合法，请重出这几处（只修这些，其余保持原样）】\n"
        f"{listing}\n"
        "⚠ 这次必须给出**完整**的内容：不要写到一半就停；字符串 / 括号 / 花括号都要闭合；"
        "写完后自己从头到尾看一遍是否完整、能否编译。"
    )


def human_feedback_block(items: list[str]) -> str:
    """人工审核意见（来自操作页面/--feedback），优先级高于模型自评，必须逐条落实。"""
    listing = "\n".join(f"- {x}" for x in items[:12])
    return (
        "【人工审核意见（优先级最高，必须逐条落实并在输出中体现）】\n"
        f"{listing}\n"
        "若某条意见无法在本阶段落实，请在对应的不确定性/偏差字段中明确说明原因。"
    )


def parts_plan(
    requirement: str,
    scope: Any,
    assessment: Any,
    excerpts_text: str,
    fixes: list[str] | None = None,
    verify: Any = None,
    prev_plan: Any = None,
    impl: Any = None,
) -> list[str]:
    return [
        _requirement_block(requirement),
        _upstream("产品经理范围说明", _scope_view(scope), str_tokens=120, list_items=10),
        pm_assumptions_block(scope),
        _upstream("存量代码评估", assessment, str_tokens=140, list_items=12),
        # 回退到方案重跑时（评审把根因判为方案层），运行验证的失败证据必须让方案看到 ——
        # 否则它不知道自己漏规划了什么文件，下一轮还会漏。
        # 只列失败命令，且全部通过时整块省略（8K 是全流程最紧的预算）。
        _upstream(
            "运行验证结果（沙箱里真跑出来的机械证据：只列失败的命令）",
            _verify_view(verify, prev_plan, impl, only_failed=True),
            str_tokens=70,
            list_items=6,
        ),
        _code_block(excerpts_text),
        _feedback_block("上一轮评审要求返工的原因（必须逐条解决）", fixes),
        "【任务】给出最小侵入变更方案与任务拆解。每个改动文件都要有 minimality_reason。",
    ]


def longest_definition(excerpts_text: str) -> tuple[str, str] | None:
    """从【存量代码片段】里挑出最长的顶层 def/class 定义，作为「主函数」及其 anchor。

    两遍开发需要一条**逐字存在于源码**的锚点行来挂载辅助函数、并定位要回填的主函数。
    这里原先写死了某个具体项目的函数名，换项目后模型就被硬指向一个根本不存在的符号，
    只能凭空造（2026-09-23 贪吃蛇 run 即由此产生编译级垃圾补丁）。
    改为从当前素材动态挑：定义块最长者通常就是本次要实现/重构的主符号。
    跨行签名（参数分行写、直到冒号结尾）要整体取全，否则 anchor 照样匹配不上。

    返回 (符号名, 逐字的定义行/块)；素材里没有任何定义则返回 None。
    """
    if not excerpts_text:
        return None
    lines = excerpts_text.splitlines()
    best: tuple[str, str] | None = None
    i = 0
    while i < len(lines):
        m = re.match(r"\s*(?:async\s+)?(?:def|class)\s+(\w+)", lines[i])
        if not m:
            i += 1
            continue
        # 签名可能跨多行：一直取到首个「以冒号结尾」的行为止
        j = i
        while j < len(lines) and not lines[j].rstrip().endswith(":"):
            j += 1
            if j >= len(lines):
                j = len(lines) - 1
                break
        block = "\n".join(lines[i : j + 1])
        if best is None or len(block) > len(best[1]):
            best = (m.group(1), block)
        i = j + 1 if j > i else i + 1
    return best


def parts_dev(
    requirement: str,
    scope: Any,
    assessment: Any,
    plan: Any,
    excerpts_text: str,
    fixes: list[str] | None = None,
    prev_summary: str | None = None,
    dev_pass: int = 1,
    pass1_edits: Any = None,
    verify: Any = None,
    repair: list[str] | None = None,
    current_code: str | None = None,
) -> list[str]:
    parts = [
        _requirement_block(requirement),
        _upstream("产品经理范围说明", scope, str_tokens=100, list_items=8),
        pm_assumptions_block(scope),
        _upstream("存量代码评估（含禁改路径）", assessment, str_tokens=120, list_items=10),
        _upstream("架构师变更方案（务必按其 tasks 执行）", plan, str_tokens=220, list_items=16),
        # 当前实现正文紧跟方案之后 —— 它是返工的主材料，且越靠前越不会被 fit_prompt 裁掉
        _current_code_block(current_code),
        # 运行验证证据排在**方案之后、代码之前**：
        #   · 它是「跑不起来」这件事唯一的机械证据，此前只喂给评审、开发完全看不到，
        #     于是开发只能按评审的文字意见修 —— 而评审经常看不出根因（真机 run 20260924-185507：
        #     verify 明说 `No module named 'direction'`，评审只提了「缺 main 入口」）；
        #   · 排前面是因为 fit_prompt 从末尾开始丢片段，越靠前越不会被裁掉。
        _upstream(
            "运行验证结果（沙箱里真跑出来的机械证据：只列失败的命令与其输出尾部）",
            # 只喂 plan 不喂 impl：开发要回答的问题是「方案有没有规划这个文件」，
            # 靠这个才能判断该改 import 还是该请方案补 changes。
            _verify_view(verify, plan, None, only_failed=True),
            str_tokens=80,
            list_items=6,
        ),
        # 自检回灌排在代码片段之前：它是「刚产出的内容过不了机械校验」，必须被看到
        _repair_block(repair),
        _code_block(excerpts_text),
        _feedback_block("评审要求修复项（必须解决，并在 deviations 中说明是否已解决）", fixes),
        (f"【上一轮实现摘要】\n{truncate_text(prev_summary, 200)}" if prev_summary else ""),
    ]
    main = longest_definition(excerpts_text)
    has_code = bool(excerpts_text and excerpts_text.strip())
    if dev_pass == 1:
        if has_code:
            parts.append("【任务】按方案实现代码改动，输出符号级 edits（补丁）。")
        else:
            # 新建项目 / 空仓库：没有可锚定的原文，必须直给完整内容，否则模型会凭空造一个锚点
            parts.append(
                "【任务】按方案实现代码改动，输出文件级 edits。\n"
                "⚠ 本次**没有提供任何存量代码片段**（很可能是新建项目、目标文件尚不存在），"
                "因此**没有可锚定的原文**：不要发明 anchor，也不要去改一个臆想中已存在的函数。\n"
                "请直接给出方案里每个改动文件的**完整、可运行内容**：patch_mode 用 `full_symbol`，"
                "target_symbol 填该文件承载主要逻辑的类或函数名，patch 里给出完整定义（含必要的 import）。\n"
                "文件路径严格遵守方案的 changes[].path，**不要自行改名或换目录**。\n"
                "⚠ **严禁占位/空壳**：不得出现 `pass`、 `# TODO`、 `# 实现逻辑` 这类敷衍，必须写真实可运行的代码。"
            )
    elif dev_pass == 2:
        if main is None:
            parts.append(
                "【任务-第一遍·脚手架】素材里没有可供锚定的既有函数定义，因此本遍改为"
                "直接给出各新增/改动符号的**完整可运行实现**（patch_mode 用 `full_symbol`），"
                "不要发明 anchor，也不要去改写素材里不存在的函数。"
            )
        else:
            symbol, anchor = main
            parts.append(
                "【任务-第一遍·脚手架】本次实现分两遍，本遍先搭脚手架。**只新增辅助函数，严禁改动主函数体**："
                f"针对本次要改的主函数 `{symbol}`，把要塞进它主循环的逻辑拆成独立的、可单独测试的小函数，"
                "用 insert_after 输出它们的补丁。主函数体由第二遍重写，本遍不要碰它。\n"
                "每条辅助函数补丁都要带清晰的签名、参数说明与注释（方便第二遍调用）；covers_tasks 填它们支撑的方案任务。\n"
                "⚠ 锚点固定：所有辅助函数统一用 insert_after，且 anchor 必须是下面这行"
                "（逐字取自源码，不要改动任何一个字符）：\n```\n" + anchor + "\n```\n"
                f"即把辅助函数都插到 `{symbol}` 定义之后。不要自造其它锚点行。\n"
                "⚠ **严禁占位/空壳**：每个辅助函数必须给出**完整、可运行的实现**（含真实逻辑与必要的异常处理），"
                "绝对禁止写 `pass`、 `# 逻辑`、 `# TODO` 这类空壳——写了空壳会被评审直接判实现不完整打回。"
                "辅助函数要足够小、各管一件事，但必须有真实内容。"
            )
    else:  # dev_pass == 3：第二遍·回填（分片重构）
        if main is None:
            parts.append(
                "【任务-第二遍·回填】参考【第一遍产物】，补齐尚未落到实处的方案任务，"
                "patch_mode 用 `full_symbol` 直接给出完整符号实现；不要重复搭脚手架。"
            )
        else:
            symbol, anchor = main
            parts.append(
                "【任务-第二遍·回填（分片重构）】第一遍已把辅助函数新增进文件（见【第一遍产物】）。"
                f"本遍把主函数 `{symbol}` 改造成调用这些辅助函数的精简实现。\n"
                "【分片策略·重要】不要试图一次性重写整个主函数（大函数一次写不深）。"
                f"改为**按逻辑分支拆成 2~3 个独立小编辑，每片只改 `{symbol}` 内部的一段（每片 ≤40 行）**："
                "每段用 `replace_span` 锚定在该分支内部**唯一**的一行，"
                "把那段原逻辑替换为「调用第一遍的辅助函数 + 必要的胶水代码」。多条分片之间用不同 anchor 区分。\n"
                "【锚点纪律】每条 replace_span 的 anchor 必须是该主函数内部某段**真实存在、唯一**的代码行（逐字抄），"
                f"**严禁在补丁里重新写 `{symbol}` 的定义行**（否则会被判 patch_span_mismatch 直接打回）；"
                "用下面这行定位主函数起点、但补丁本身不要包含它：\n```\n"
                + anchor + "\n```\n"
                "辅助函数的签名以【第一遍产物】为准，**严禁再重复定义它们**——若又输出辅助函数补丁会判「重复定义」直接打回。\n"
                "【完整性】所有分片拼起来必须**逐行把原函数的每个行为都安顿好**："
                "异常与边界处理、状态更新、循环与外部调用、收尾清理，"
                "要么进刚新增的辅助函数，要么保留在主函数体里；禁止用 `pass` 或 `# 逻辑` 占位。"
                "原函数中已有的真实逻辑（语句、字段处理、异常分支）要原样保留或迁移，不得丢失。"
                "本遍交付的 edits 必须让方案所有任务都有对应补丁（覆盖审计会逐条核对）；self_checks 覆盖两遍成果。"
            )
        if pass1_edits:
            parts.append(_pass1_block(pass1_edits))
    return parts


def _pass1_block(edits: Any) -> str:
    if not edits:
        return ""
    lines = ["【第一遍产物·已新增的辅助函数（第二遍必须调用，不得重复定义）】"]
    for i, e in enumerate(edits, 1):
        if not isinstance(e, dict):
            continue
        lines.append(
            f"  ({i}) 文件 {e.get('path')} · 符号 {e.get('target_symbol')} · 模式 {e.get('patch_mode')}"
        )
        patch = e.get("patch") or ""
        if isinstance(patch, list):
            patch = "\n".join(patch)
        sig = "\n".join(str(patch).splitlines()[:8])
        lines.append("      签名/片段：\n" + "\n".join("        " + ln for ln in sig.splitlines()))
    return "\n".join(lines) + "\n"


def parts_test(
    requirement: str,
    scope: Any,
    plan: Any,
    impl: Any,
    excerpts_text: str,
    fixes: list[str] | None = None,
) -> list[str]:
    return [
        _requirement_block(requirement),
        _upstream("产品经理验收标准", scope, str_tokens=110, list_items=8),
        pm_assumptions_block(scope),
        _upstream("架构师方案（验收依据）", plan, str_tokens=180, list_items=14),
        _upstream("开发实现结果", impl, str_tokens=420, list_items=10),
        _code_block(excerpts_text),
        _feedback_block("评审要求补测项", fixes),
        "【任务】产出新功能/回归/兼容三类测试用例，并给出可执行命令。",
    ]


def _scope_review_view(scope: Any) -> Any:
    """评审只需要 PM 产出里的「验收依据」。

    背景 / 目标 / 目标用户 / 影响域 对判定无增益，却占了 scope 近一半体积
    —— 评审是唯一 8K 上下文的阶段，省下的每一分都直接变成能看到的实现与测试细节。
    """
    if not isinstance(scope, dict):
        return scope
    keys = ("change_request", "functional_requirements", "acceptance_criteria", "in_scope", "out_of_scope")
    return {k: scope.get(k) for k in keys if scope.get(k) is not None}


def _test_summary(test: Any) -> Any:
    """给评审用的测试摘要：只保留判定「测试是否完备」必需的部分。

    完整测试报告蒸馏后约 2200 tok，评审（8K）装不下。这里丢掉 steps（对判定无增益
    但最占篇幅），只留每条用例的 id / type / expected —— 「三类是否齐全」「expected
    是否可验证」「命令有几条」这三件事已由 `_audit_test()` 机械核对，作为 pin 传入。
    """
    if not isinstance(test, dict):
        return None
    cases = [c for c in (test.get("cases") or []) if isinstance(c, dict)]
    gaps = [
        g.get("gap") if isinstance(g, dict) else str(g)
        for g in (test.get("coverage_gaps") or [])
    ]
    return {
        "cases": [
            {"id": c.get("id"), "type": c.get("type"), "expected": c.get("expected")}
            for c in cases[:10]
        ],
        "automated_commands": len(test.get("automated_commands") or []),
        "coverage_gaps": gaps[:8],
        "risks": len(test.get("risks") or []),
        "uncertainties": len(test.get("uncertainties") or []),
    }


def _path_stems(items: Any) -> set[str]:
    """把路径清单归一成「模块名主干」集合（判断某个名字是否被规划过）。"""
    out: set[str] = set()
    for raw in items or []:
        text = str(raw or "").strip().replace("\\", "/")
        if not text:
            continue
        name = text.rsplit("/", 1)[-1]
        if "." in name:
            name = name.rsplit(".", 1)[0]
        if name:
            out.add(name.lower())
    return out


def _plan_stems(plan: Any) -> set[str]:
    """方案**声明要产出**的文件名集合（changes[].path / tasks[].target_files）。"""
    plan = plan if isinstance(plan, dict) else {}
    paths: list[str] = [
        str(c.get("path") or "") for c in (plan.get("changes") or []) if isinstance(c, dict)
    ]
    for task in plan.get("tasks") or []:
        if isinstance(task, dict):
            paths.extend(str(x) for x in (task.get("target_files") or []))
    return _path_stems(paths)


def _impl_stems(impl: Any) -> set[str] | None:
    """实现**实际产出**的文件名集合；`impl` 缺失时返回 None（调用方据此不下断言）。"""
    if not isinstance(impl, dict):
        return None
    return _path_stems(str(e.get("path") or "") for e in (impl.get("edits") or []) if isinstance(e, dict))


_MISSING_MODULE_RE = re.compile(r"No module named '([^']+)'")
_MISSING_SYMBOL_RE = re.compile(r"cannot import name '([^']+)' from '([^']+)'")


def _compact_error(text: Any, head: int = 60, tail: int = 110) -> str:
    """把命令输出压成「开头 + 结尾」两截（总长 ≤ str_tokens 的下限 200 字符）。

    为什么不能只截开头：`distill`/`truncate_text` 是**从头截**的，而 Python traceback 的
    **异常类型与原因在最后一行**、测试框架的失败汇总也在末尾。真机 run 20260924-185507
    就栽在这里 —— 评审拿到的 stderr 尾部被从头切掉，只看到
    `File .../input_handler.py, line 2`，看不到 `ModuleNotFoundError: No module named 'direction'`，
    于是把「方案漏规划文件」误判成实现层的「缺 main 入口」。
    保留头（哪条命令、哪个文件）与尾（到底什么错）才够用。
    """
    text = str(text or "").strip()
    if len(text) <= head + tail + 20:
        return text
    return text[:head] + " …(中间省略)… " + text[-tail:]


def verify_facts(report: Any, plan: Any = None, impl: Any = None) -> list[str]:
    """把 verify 的失败输出翻译成**机械事实**（集合判定），喂给评审、方案与开发。

    为什么是「事实」而不是「结论」：`No module named 'X'` 在
    「第三方库没装」与「方案漏建了本地模块」两种成因下**长得一模一样**，机制判不出来；
    但把「X 在不在方案的改动清单里 / 在不在本轮产出文件里」这两个集合差摆出来，
    人（和模型）一眼就能分。机制只算集合，归属由评审声明。

    清单缺失时**只陈述能确证的部分** —— 事实宁可少说，不能说错。
    """
    if not isinstance(report, dict):
        return []
    has_plan = isinstance(plan, dict) and bool(plan)
    plan_stems = _plan_stems(plan) if has_plan else set()
    impl_stems = _impl_stems(impl)
    chunks: list[str] = []
    for cmd in report.get("commands") or []:
        if not isinstance(cmd, dict) or cmd.get("status") == "ok":
            continue
        chunks.append(f"{cmd.get('stderr_tail') or ''}\n{cmd.get('stdout_tail') or ''}")
    blob = "\n".join(chunks)
    facts: list[str] = []
    for name in dict.fromkeys(_MISSING_MODULE_RE.findall(blob)):
        stem = str(name).rsplit(".", 1)[0].lower()
        where = []
        if stem in plan_stems:
            where.append("方案的改动清单")
        if impl_stems is not None and stem in impl_stems:
            where.append("本轮产出文件")
        if where:
            facts.append(
                f"缺少模块 {name}：{'、'.join(where)}里**有**它，但沙箱里没被写出来"
                "（补丁没落地）→ 属实现层"
            )
            continue
        scope_parts = []
        if has_plan:
            scope_parts.append("不在方案的改动清单（changes/tasks）里")
        if impl_stems is not None:
            scope_parts.append("不在本轮产出文件里")
        if not scope_parts:
            facts.append(f"缺少模块 {name}：沙箱里 import 不到它（清单缺失，无法判定该由谁产出）")
            continue
        facts.append(
            f"缺少模块 {name}：{'，也'.join(scope_parts)}。"
            "若它是项目内模块 → 属**方案层**（方案漏规划该文件，应补进 changes）；"
            "若它是第三方库 → 属依赖未声明 / 环境缺失。"
        )
    for symbol, module in dict.fromkeys(_MISSING_SYMBOL_RE.findall(blob)):
        facts.append(f"模块 {module} 里没有符号 {symbol}：本轮没有任何补丁定义过这个符号")
    return facts


def _verify_view(
    report: Any, plan: Any = None, impl: Any = None, *, only_failed: bool = False
) -> dict | None:
    """运行验证结果蒸馏：结论 + 失败原因 + 每条命令的退出码与输出尾部 + **机械事实**。

    这是流水线里**唯一的执行证据**（沙箱里真跑出来的），所以失败项的 stderr 尾部要留下来 ——
    否则「失败了」三个字对下游毫无指导意义。评审上下文最紧，故每条只留尾部若干字符。

    `only_failed=True` 给开发用：开发只需要看**失败**的命令（成功的没有指导价值），
    且全部通过时整块省略，避免白占开发那 12k 预算里本要给代码片段的篇幅。
    """
    if not isinstance(report, dict) or not report:
        return None
    commands = []
    for cmd in (report.get("commands") or [])[:6]:
        if not isinstance(cmd, dict):
            continue
        if only_failed and cmd.get("status") == "ok":
            continue
        commands.append(
            {
                "command": truncate_text(str(cmd.get("command", "")), 40),
                "status": cmd.get("status", ""),
                "exit_code": cmd.get("exit_code"),
                "reason": cmd.get("reason") or "",
                # 先取「最后 500 字符」（离失败现场最近），再压成「头 + 尾」——
                # 只做前者会被 distill 从头截掉异常行（见 _compact_error 的说明）。
                "stderr_tail": _compact_error((cmd.get("stderr_tail") or "")[-500:]),
                "stdout_tail": _compact_error((cmd.get("stdout_tail") or "")[-200:]),
            }
        )
    facts = verify_facts(report, plan, impl)
    if only_failed and not commands and not facts and report.get("verdict") != "fail":
        return None
    return {
        "verdict": report.get("verdict", ""),
        "summary": report.get("summary", ""),
        "problems": list(report.get("problems") or [])[:6],
        "notes": list(report.get("notes") or [])[:5],
        # 机制算出来的集合事实（不是模型判断）：评审据此声明返工项归属
        "mechanical_facts": facts,
        "commands": commands,
    }


def parts_review(
    requirement: str,
    scope: Any,
    plan: Any,
    impl: Any,
    test: Any,
    fixes: list[str] | None = None,
    verify: Any = None,
) -> list[str]:
    # 评审阶段上下文最紧（8K），实现产物只保留结构：文件清单 + 自检项 + 偏差，丢掉代码正文
    impl_view = None
    if isinstance(impl, dict):
        impl_view = {
            "summary": impl.get("summary", ""),
            # 不带 rationale：开发「为什么这么改」在方案的 approach / minimality_reason
            # 里已经有了，而评审要核对的是「改了哪些文件」（对照方案结构）。省下的篇幅
            # 直接变成能看到的 self_checks 与 deviations。
            "files": [
                {
                    "path": (e or {}).get("path", ""),
                    "change_type": (e or {}).get("change_type", ""),
                }
                for e in (impl.get("edits") or [])[:10]
            ],
            "self_checks": impl.get("self_checks", []),
            "deviations": impl.get("deviations", []),
        }
    return [
        _requirement_block(requirement),
        _upstream("产品经理验收依据（范围与验收标准）", _scope_review_view(scope), str_tokens=90, list_items=6),
        pm_assumptions_block(scope),
        _upstream("架构师方案", plan, str_tokens=110, list_items=10),
        # 测试摘要放在实现**之前**：完整测试报告蒸馏后约 2200 tok，而评审是唯一 8K
        # 上下文的阶段 —— 实测排在 impl_view 之后时会被整段挤掉，于是「测试完备」
        # 这条判定标准根本没有原材料。只留判定必需的部分，细节交给 test_audit。
        _upstream(
            "测试摘要（用例 id/type/expected；三类齐全与命令数见测试覆盖审计）",
            _test_summary(test),
            str_tokens=60,
            list_items=10,
        ),
        # 运行验证放在同一段「证据区」里：这是沙箱里真跑出来的机械证据，
        # 比任何「看起来没问题」的判断都硬。verdict=fail 时评审给 pass 也会被机制改判。
        # 同时带上机械事实（mechanical_facts）：判「该回开发还是回方案」需要它。
        _upstream(
            "运行验证结果（把补丁物化到沙箱后真实执行得到的机械证据；fail = 跑不起来）",
            _verify_view(verify, plan, impl),
            str_tokens=60,
            list_items=6,
        ),
        _upstream("开发实现（代码正文已省略）", impl_view, str_tokens=100, list_items=8),
        _feedback_block("上一轮已提出的修复项（检查是否真的解决了）", fixes),
        "【任务】判定交付是否可接受，输出 verdict 与必改项。",
    ]


# ------------------------------------------------------- 新建项目（0 存量代码）专用提示词
# 为什么不共用上面那套：二开提示词通篇围绕「存量代码 / 锚点补丁 / minimality_reason /
# 禁改路径 / 白名单」展开，而新建项目**根本没有存量代码**——真机表现就是错位：
#   · dev 被要求去找一个不存在的符号来锚定（早期 INDEX_ALL 硬编码 bug 即由此放大）；
#   · architect_assess 在空仓库里编出不存在的目录（pipeline/core/*、db/*），
#     再被 plan 当成既有事实承接，最后写进 acceptance（"与数据库查询结果一致"）。
# 所以为 project_type == "new" 单独一套：**只改提示词，不动 schema**——字段规范一律改写为
# 现有 SCOPE / PLAN / IMPLEMENTATION / TEST_REPORT / REVIEW 的字段，下游消费方零改动。
# 新建项目没有 architect_assess（无存量代码可评估），故本套不含该阶段，编排器会跳过它。
SYSTEM_NEW: dict[str, str] = {
    "pm": (
        "你是资深产品经理，负责全新项目的需求边界界定、目标量化与验收标准制定，"
        "产出标准化 PRD，供下游架构、开发、测试直接执行。\n"
        "你是需求层角色：只做范围与影响判断，不给技术方案、不写代码、不做任务拆解。\n"
        "绝对红线（违反即不合格）：\n"
        "  1. 严禁给出任何技术方案、架构设计、技术选型或代码实现思路；\n"
        "  2. 严禁做任务拆解、排期或人员分工；\n"
        "  3. 严禁编造需求里没有的功能、场景或约束；\n"
        "  4. 严禁臆造没有依据的数字指标（性能、容量、并发之类）。\n"
        "核心原则：\n"
        "  · 边界互斥：in_scope 与 out_of_scope 必须完全互斥且具体，"
        "并明确「技术选型与架构设计不属于本阶段产出」；\n"
        "  · 目标可度量：goal 必须量化，禁止「提升体验」这类无法验证的描述；\n"
        "  · 验收可验证：每条功能需求的 acceptance 都要能直接转成测试用例；\n"
        "  · 未决带默认：所有不明确的地方都必须给出建议与默认取值，保障流水线可推进。\n"
        "字段规范：\n"
        "  · change_request：一句话概括本次要做什么；\n"
        "  · background / goal / target_users：动机、可度量的目标、目标使用者；\n"
        "  · in_scope / out_of_scope：本期做与不做，互斥且具体；\n"
        "  · impact_areas 每项 {area, impact, severity}：影响的能力域、具体影响、严重度(high|medium|low)；\n"
        "  · functional_requirements 按 FR-01、FR-02… 编号，每项 {id, title, description, priority, acceptance}；"
        "priority 取 high|medium|low；acceptance 是**字符串数组**，每条都要可验证"
        "（FR 编号只用于本字段，out_of_scope 用纯文字、不要编号）；\n"
        "  · acceptance_criteria：项目级验收标准（与各 FR 的 acceptance 互补，不要简单重复）；"
        "每条都要能被**独立验证**，禁止「所有功能通过测试用例验证」这类同义反复；\n"
        "  · open_questions 每项 {question, why_it_matters, recommendation, assumed_answer, "
        "impact_if_wrong, severity}：recommendation 是你的专业建议，"
        "assumed_answer 是人工未确认时下游默认按此执行的取值 —— **只提问题不给默认取值是无效条目**；"
        "severity 取 high|low，表示「猜错要不要停下来确认」；\n"
        "  · unknowns / clarifying_questions 填对应问题原文（供人工扫读），须与 open_questions 对应。\n"
        "执行步骤：① 通读需求，提取全部明确信息；② 识别未明确、有歧义、缺失的信息，整理成未决清单；"
        "③ 为每条未决问题给出建议与默认取值；④ 拆解功能需求并逐条写可验证的验收标准；"
        "⑤ 对照下方自检后输出 JSON。\n"
        "自检：① 有没有写技术方案或技术选型？② in_scope 与 out_of_scope 互斥吗？"
        "③ 每条功能都有可验证的 acceptance 吗？④ 每条未决问题都给了默认取值吗？"
        "⑤ priority / severity 都在规定选项内吗？⑥ 有没有编造需求里没有的内容（尤其没有依据的数字）？"
        "⑦ 输出是纯 JSON 吗？\n" + _TAIL
    ),
    # 注意：与二开版同样的上下文约束（14B 只有 8K）。新建项目的池子为空，
    # 省下了代码片段预算，但仍不要把 system 写得过长。
    "architect_plan": (
        "你是资深架构师，承接产品 PRD，为**全新项目**输出最小可行的架构设计与可执行的任务拆解，"
        "供下游开发按文件落地。\n"
        "你是设计层角色：只做架构设计与任务划分，不写具体代码实现。\n"
        "绝对红线（违反即不合格）：\n"
        "  1. 严禁编写具体函数代码或实现细节，不越界到开发层面；\n"
        "  2. 严禁编造不存在的技术组件、中间件、外部服务、依赖，也不得臆造数据库表或框架 —— "
        "技术选型必须与需求规模相称；\n"
        "  3. 严禁过度设计：不提前预留扩展性、不做需求没要求的抽象或多环境适配；\n"
        "  4. tasks[].target_files 必须是本次要**创建**的文件路径，不得引用不存在的既有文件；\n"
        "  5. **必须规划一个可执行入口文件**（main.py / __main__.py / run.py 等，带 "
        "`if __name__ == '__main__':` 且运行时有实际输出），并写进 changes 与某个 task 的 "
        "target_files —— 方案不写它，开发受白名单约束**无权创建**，运行验证会一直判"
        "「没有可执行入口」，整条流水线会在实现层空转。\n"
        "设计原则：\n"
        "  · 最小可行：优先满足核心需求，架构做最简设计；\n"
        "  · 单一职责：每个模块只负责一个能力域，边界清晰；\n"
        "  · 接口最小化：模块间接口精简，减少依赖与耦合；\n"
        "  · 可测试性：模块与接口都要能被独立验证；\n"
        "  · 任务独立：每个任务可独立开发、独立验收。\n"
        "字段规范：\n"
        "  · strategy：整体设计思路，必须写清**模块划分、各模块职责与边界、模块间接口约定、"
        "核心数据结构**（全新项目没有既有接口可谈「最小侵入」，设计就讲在这里），"
        "且必须与 changes / tasks 自洽；\n"
        "  · **技术栈必须单一且明确**：在 strategy 里写清语言与运行环境"
        "（如「Python 3.12 + 标准库」），changes 里**不要混用多种主语言** —— "
        "同一份能力用两种语言各写一遍是发散，不是设计；\n"
        "  · changes 每项 {path, intent, approach, minimality_reason}：path 是本次要创建的文件路径；"
        "intent 说明该文件承载什么职责；approach 说明内部结构与关键设计；"
        "minimality_reason 说明**为什么这是最小可行设计**"
        "（不做过度的扩展性预留、更重的替代方案为什么不必要），「改动少」这类空话不合格；\n"
        "  · tasks 每项 {id, title, target_files, acceptance, depends_on}：id 按 T-01、T-02… 编号"
        "（开发要用它填 covers_tasks，编号必须规范）；target_files 只能引用 changes 里出现过的路径；"
        "acceptance 必须可独立验收、能直接转成测试用例；depends_on 只引用本方案里已定义的 id；\n"
        "  · rollback：说明整体回滚方式；risks 记录本次设计引入的残余风险。\n"
        "执行步骤：① 承接 PRD，梳理核心需求、目标与验收标准；② 划分模块并明确职责边界；"
        "③ 定义模块间接口与核心数据结构；④ 落成文件级 changes 并给出最小可行性理由；"
        "⑤ 拆成可独立验收的任务并编号、给出依赖；⑥ 自检后输出 JSON。\n"
        "自检：① 有没有写具体代码实现？② 每个 changes[].path 都是本次新建的文件且职责单一吗？"
        "③ changes 与 tasks 相互覆盖了吗（有没有落单的文件或悬空的任务）？"
        "④ 有没有需求没要求的过度设计？⑤ 有没有编造技术组件或依赖？"
        "⑥ minimality_reason 讲清「为什么这是最小可行设计」了吗？⑦ 输出是纯 JSON 吗？\n" + _TAIL
    ),
    "dev": (
        "你是资深开发工程师，严格按架构方案为**全新项目**创建代码文件，"
        "产出可被机械校验并落盘的实现。\n"
        "绝对红线（违反即不合格）：\n"
        "  1. 严禁私自改动方案定义的模块边界、接口约定与数据结构；\n"
        "  2. 严禁编造方案里没有的文件、依赖、外部服务或数据库表；\n"
        "  3. 严禁做与任务无关的额外功能、抽象或格式调整；\n"
        "  4. 严禁谎报完成：没实现的任务必须逐条写进 not_implemented 并说明缺什么。\n"
        "交付粒度纪律：一次 edit ＝ **一个文件**（新建项目里文件就是交付单元）。\n"
        "  · path：文件相对路径，必须在方案的 changes[].path 或 tasks[].target_files 里出现过；\n"
        "  · change_type：全新项目一律用 `add`；\n"
        "  · target_symbol：该文件承载主要逻辑的类或函数名；\n"
        "  · anchor：**新建文件没有原文可锚定，一律留空字符串**；\n"
        "  · patch_mode：**必须用 `full_symbol`** —— 文件尚不存在，另两种模式都依赖原文定位，"
        "会被机械校验判负；\n"
        "  · patch：该文件的**完整、可直接落盘的内容**（含必要的 import 与模块级定义），"
        "不允许省略、不允许写「其余同上」；\n"
        "  · covers_tasks：本次交付对应方案里哪些任务 id（必须真实存在，覆盖审计会逐条核对）；\n"
        "  · rationale：为什么这样实现、与方案的接口约定如何对齐。\n"
        "工程纪律：命名与异常处理遵循方案里的统一约定；对外接口严格匹配方案定义的输入输出；"
        "必须写真实可运行的代码，禁止 `pass`、`# TODO`、`# 实现逻辑` 这类空壳。\n"
        "执行步骤：① 读取分配的任务、所属模块与接口约定；② 确定文件结构与内部实现；"
        "③ 写出完整文件内容；④ 整理未实现项、偏差与自检证据；⑤ 自检后输出 JSON。\n"
        "自检：① 每个 path 都在方案的 changes / target_files 里吗？"
        "② patch_mode 全是 full_symbol、anchor 留空吗？③ 文件内容完整可运行吗（有没有占位/空壳）？"
        "④ 对外接口与方案约定一致吗？⑤ 未实现的任务都进 not_implemented 了吗？"
        "⑥ self_checks 的证据能指到具体文件与符号吗？⑦ 输出是纯 JSON 吗？"
        "⑧ 有没有混用多种语言（同一份能力用 .py 和 .js 各写一遍）？技术栈必须与方案一致且单一。\n"
        "另外**必须在 `run` 字段里给出「怎么把它跑起来」的那一条命令**（如 `python main.py`）："
        "运行验证会真的执行它。入口文件必须存在且带 `if __name__ == '__main__':`，"
        "运行时要有实际输出（不能 import 完就退出）。写不出可运行入口，就说明这轮交付不完整。\n" + _TAIL
    ),
    "test": (
        "你是专业测试工程师，针对**全新项目**的交付产出新功能（new）/ 回归（regression）/ "
        "兼容（compat）三类测试方案。\n"
        "绝对红线（违反即不合格）：\n"
        "  1. 严禁编造不存在的文件路径、函数名、命令、工具或测试数据；automated_commands 必须真实可执行；\n"
        "  2. 严禁隐瞒覆盖缺口：覆盖不到的场景必须逐条写进 coverage_gaps，不得假装全覆盖；\n"
        "  3. 严禁模糊表述：expected 必须可观测、可比对，禁止「验证正常」「功能可用」这类说法；\n"
        "  4. 严禁省略 steps 与 expected。\n"
        "用例规范：\n"
        "  · 三类**缺一不可**，每类至少一条；总数控制在 15 条以内，只测本次交付相关；\n"
        "      new＝本次新增功能（覆盖正常 / 边界 / 异常）；regression＝已交付模块的既有能力不被破坏；"
        "compat＝模块间接口、数据格式与统一约定；\n"
        "  · id 按类型编号：new 用 NEW-01…，regression 用 REG-01…，compat 用 COMP-01…；\n"
        "  · target **必须写到符号级**（被验证的函数 / 类名，必要时带文件名如 `game_logic.py::Snake.move`）；"
        "只写文件名（如 `game_logic.py`）不合格 —— 编排器要用它和补丁的 target_symbol 机械核对覆盖，"
        "粒度对不上就发现不了「写了很多用例、真正改的东西却没测到」；\n"
        "  · 本次交付的符号务必**逐个**出现在某条用例的 target 里；"
        "steps 一步一个动作（含操作对象与输入参数）；\n"
        "  · expected 必须能直接转成断言：写成**可观测的具体结果**（数值、状态、返回值、界面元素），"
        "例如「调用后返回长度为 3 的列表」；「正常运行」「无异常」「符合约定」这类说法一律不合格；\n"
        "  · automated_commands 每项给 {command, description}：命令要能**验证行为**"
        "（跑测试套件、断言脚本、校验命令），而不是「直接启动程序看一眼」；\n"
        "    ⚠ **至少要有一条「断言型」命令**：形如 "
        "`python -c \"import m; assert m.f(2) == 4\"` 或 `python -m unittest`，"
        "让机器用退出码替你判断对不对。只写「启动一下」的命令，退出码 0 证明不了任何行为；\n"
        "    **命令必须能在本环境直接跑起来**：优先用 Python 标准库（`python -m unittest`）"
        "与项目自带依赖，不要声明环境里没装的第三方工具（如 `pytest`）—— "
        "声明了只会记一条「程序不可用」，既验不了东西又白占一条命令位；\n"
        "  · coverage_gaps 每项给 {gap, reason, impact}：缺口是什么、为什么覆盖不了、对结论影响多大；\n"
        "    ⚠ **交付的符号必须逐个被某条用例的 target 覆盖**，否则会被机械判为漏测并直接打回。"
        "确实无需单独用例的，必须在 coverage_gaps 里写明是哪个符号、为什么不需要 —— "
        "这是唯一的豁免途径，不写就会被当成漏测；\n"
        "  · 拿不准的假设写进 uncertainties 的 {issue, assumption, confidence}；"
        "risks 记录本次交付引入的残余风险。\n"
        "执行步骤：① 梳理本次交付的功能点、模块与接口约定；② 设计 new 用例；"
        "③ 设计 regression 用例（已交付模块的既有能力）；④ 设计 compat 用例（接口、数据格式、依赖版本）；"
        "⑤ 整理可执行命令与覆盖缺口；⑥ 自检后输出 JSON。\n"
        "输出前自检：① 三类都覆盖了吗？② 每条 steps 都可复现吗？"
        "③ expected 具体可验证吗（有没有「正常」「没问题」「功能可用」）？④ 命令都真实可执行吗？"
        "⑤ 覆盖不到的场景都进 coverage_gaps 了吗？⑥ 输出是纯 JSON 吗？\n" + _TAIL
    ),
    "review": (
        "你是评审人（架构师视角），基于架构方案、交付内容与测试方案，"
        "客观判定**全新项目**的交付是否可接受。\n"
        "判定标准：① 交付是否符合方案定义的模块划分与接口约定"
        "（新建项目不存在「白名单外的存量文件」之说，但不得擅自偏离方案结构）；"
        "② 是否满足 PM 的验收标准；③ 测试是否覆盖 new/regression/compat 三类且有可执行命令；"
        "④ 是否引入未声明的兼容性风险；"
        "⑤ 实现是否**真的覆盖了方案的任务清单**（见【实现覆盖审计】）。\n"
        "verdict 取值：pass / rework_dev（方案对但实现有问题）/ rework_architect（方案本身有问题）。\n"
        "返工项必须逐条声明作用域 required_fixes_detail[].scope，**它决定下一轮回哪个阶段**：\n"
        "  · in_material —— 实现层就能改掉（改文件内容、补测试用例、补描述…）→ 下一轮回开发；\n"
        "  · architect —— **方案层**才能改：方案漏规划了某个文件（新建项目里最常见）、"
        "漏定义接口/数据结构、任务边界划错导致某文件没人负责、模块划分与交付物对不上 → "
        "下一轮**直接回架构师方案**；\n"
        "  · needs_external —— 需要运行系统、访问外部环境或人工确认才能定论，"
        "编排器会把这类自动改判进 residual_risks。\n"
        "只要有一条判 architect，下一轮就从**方案**重跑；全是 in_material 才只回开发。\n"
        "另外，每条返工项**尽量填** required_fixes_detail[].path（相对仓库根的文件路径）：\n"
        "它决定这条整改要求被派给哪个文件。不填的话开发只能拿到一串无主的文字、靠猜去改，\n"
        "结果就是反复去改评审根本没抱怨的文件，返工永远收敛不了。\n"
        "⚠ **不要把方案层根因写成 in_material** —— 开发被约束在方案的 changes 范围内，"
        "动不了「方案里根本没这个文件」的问题，只会白烧一轮"
        "（真机教训：方案没规划 `direction` 模块，却被判实现层，下一轮又撞同一个错）。\n"
        "判 architect 最可靠的依据是【运行验证结果】里的 mechanical_facts（机制算出的集合事实："
        "某个缺失的模块在不在方案清单里）—— 直接照着它分区，不要凭感觉。\n"
        "如果**所有**返工项都是 needs_external，那就不要投 rework（会被机制强制放行，等于白投）。\n"
        "可直接判 rework_dev 的硬信号（**以【补丁机械校验】的结果为准，不要凭感觉**）："
        "patch 里没有定义声明的符号；声明 `full_symbol` 但内容明显不是完整文件（缺模块级定义或关键逻辑）；"
        "文件路径与方案 changes / tasks 对不上；covers_tasks 里有方案任务没被任何补丁覆盖；"
        "self_checks 声称完成但实现规模明显不足以支撑。这些都属于本轮材料内可改（`in_material`）。\n"
        "residual_risks 每项给 {issue, reason, impact}。\n"
        "评审顺序：① 先读末尾【补丁机械校验】【实现覆盖审计】【测试覆盖审计】找硬信号；"
        "② 核对 5 条判定标准；③ 按作用域分类；④ 定 verdict 后输出。\n"
        "自检：① 结论与机械校验一致吗？② 作用域分准了吗（external 误判成 in_material 会白烧一轮）？"
        "③ rework_dev（实现/测试层面的问题）与 rework_architect（方案本身有错）分对了吗？"
        "④ 方案层根因（方案漏规划文件 / 漏接口 / 任务边界错）标成 architect 了吗？\n"
        "人工已确认的事实（见【人工已确认的事实】）不得再作为未决项或 rework 依据。\n" + _TAIL
    ),
}


def system_prompt(stage: str, project_type: str = "secondary") -> str:
    """按项目类型取系统提示词。

    新建项目（`new`）没有存量代码，二开那套围绕「存量代码 / 锚点补丁 / 最小侵入 / 禁改路径」
    的纪律对它全是错位的，因此单独一套（见 SYSTEM_NEW 的说明）。
    未覆盖的阶段（如新建项目不会跑的 architect_assess）回退到通用版本。

    注意：操作页面的「配置」页只覆盖 SYSTEM（二开那套），暂不含 SYSTEM_NEW ——
    让页面同时管两套会让 stage 列表翻倍，收益不足。
    """
    if project_type == "new":
        return SYSTEM_NEW.get(stage) or SYSTEM[stage]
    return SYSTEM[stage]


# --------------------------------------------------------------------------- 本地覆盖
# 代码默认值快照：必须在应用覆盖**之前**记录，页面据此显示「已改 / 未改」并提供还原。
PROMPT_DEFAULTS: dict[str, str] = dict(SYSTEM)


def apply_overrides() -> None:
    """应用 `pipeline/config.local.json` 里的系统提示词覆盖（操作页面「配置」页写入）。

    必须在**模块初始化末尾**执行：`issues.pipeline_fingerprint()` 直接序列化
    `SYSTEM` 算 `prompts_hash`，晚一步指纹就记不到修改，跨运行归因会失真。

    覆盖的是「正文 + _TAIL 拼接后」的完整串，因此页面上改出来的内容本身就是最终值；
    还原默认时删键即可，不会出现 _TAIL 被重复叠加的问题。
    """
    from . import local_config  # 局部导入，避免与同包模块形成循环依赖

    # 先恢复默认值：保证可重复调用（页面保存/还原后 server 会重新调用），
    # 也让「删除某项覆盖」真正退回默认，而不是残留上一次的自定义内容。
    for stage, text in PROMPT_DEFAULTS.items():
        SYSTEM[stage] = text

    for stage, text in local_config.prompts().items():
        if stage not in SYSTEM:
            continue
        if isinstance(text, str) and text.strip():
            SYSTEM[stage] = text


apply_overrides()
