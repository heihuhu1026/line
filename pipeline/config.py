"""流水线全局配置：模型矩阵、上下文预算、回流上限、评审频率。

模型矩阵（方案 A + 测试合并到开发模型，评审交 14B）：
    pm                qwen3-8b-pm-16k            16K   think=True   实测 100% GPU / ~7.0GB
    architect_*       qwen3-14b-arch-8k            8K   think=True   实测 100% GPU / 9.0GB（余量仅 ~190MB）
    dev / test        qwen2.5-coder-7b-dev-24k    24K   think=None   实测 100% GPU / 6.3GB
    review            qwen3-14b-arch-8k            8K   think=True

8K 是 14B 能全量上 GPU 的上限（12K 就会溢出），所以架构师/评审的输入必须做蒸馏，
prompt 预算通过 prompt_token_budget 硬约束。
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

from . import flow

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = Path(os.getenv("PIPELINE_RUNS_DIR", str(ROOT / "runs")))
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# 阶段驻留期内的 keep_alive，切换模型时显式卸载
KEEP_ALIVE = os.getenv("PIPELINE_KEEP_ALIVE", "10m")
REQUEST_TIMEOUT = int(os.getenv("PIPELINE_TIMEOUT", "1800"))

# 回流上限：达到上限仍未 pass 则标记 needs_human 并保留全部中间产物
MAX_REWORK_ROUNDS = int(os.getenv("PIPELINE_MAX_REWORK", "2"))

# 人工介入的预算语义：人工点一次「打回」= 一次明确要求再修一轮，理应给它预算。
# 为什么必须有：`_begin_round` 会把 attempt 推进一格，而 `_step_review` 用的是**绝对**判据
# `attempt > max_rework`。真机 run 20260924-185507：人工在 attempt=3 打回，下一轮 attempt=4 > 2
# → 那一轮评审一判返工就直接 needs_human 结束 —— 人工介入只换来一轮、且必然判死，等于白救。
# 规则：人工介入时把上限抬到 `max(当前上限, attempt + 该值)`（单调不减）。
HUMAN_REWORK_BUDGET = int(os.getenv("PIPELINE_HUMAN_REWORK_BUDGET", "1"))
# 人工追加次数上限（防止无人值守时无限增长）；到顶后需显式给 --max-rework
HUMAN_REWORK_TOPUP_MAX = int(os.getenv("PIPELINE_HUMAN_REWORK_TOPUP_MAX", "3"))

# 评审频率：每 N 轮评审一次（1 = 每轮都评审）。
# 首轮与「末轮（触顶那次）」必定评审 —— 前者为了早暴露问题，后者为了拿到真实判定而不是直接 needs_human。
# 取值 2 时典型回流路径（上限 2，共 3 轮）只评审第 1、3 轮，省掉 2 次模型切换。
REVIEW_EVERY = max(1, int(os.getenv("PIPELINE_REVIEW_EVERY", "2")))

# 各档位的 prefill 健康基线（token/s）：tools/preflight.py 与 issues 的问题判定共用这一份。
# 口径是「数量级」——探针短则快、长则慢，所以只用来抓「掉了一个数量级」的情况：
# 实测同一台机器上 14B 的 prefill 健康时 ~127 t/s（2.5K prompt），服务退化后只有 6~18 t/s。
BASELINE_PREFILL: dict[str, int] = {
    "qwen3-8b-pm-16k": 300,
    "qwen3-14b-arch-8k": 150,
    "qwen2.5-coder-7b-dev-24k": 200,
}
DEFAULT_BASELINE_PREFILL = 120

# PM 上下文：实测 prompt 仅 ~300 token，16K 足够，省下约 1.2GB 显存。
# 若要喂很长的需求文档（>6000 字），用 PIPELINE_PM_CTX=32768 临时调回（tag 里的 num_ctx 会被请求覆盖）。
PM_NUM_CTX = int(os.getenv("PIPELINE_PM_CTX", "16384"))

# 字符 -> token 的启发式因子（实测中文约 1.5~1.6 字符/token，取 1.6 略偏保守）
CHARS_PER_TOKEN = 1.6

# 单次调用结束后，若实际 prompt token 超过该比例 * num_ctx，记录告警（便于回头调预算）
PROMPT_HARD_RATIO = 0.85


@dataclass(frozen=True)
class ModelSpec:
    role: str
    tag: str
    num_ctx: int
    prompt_token_budget: int
    think: bool | None = None  # None = 不下发该参数（非思考模型）
    temperature: float = 0.6
    num_predict: int = 4096

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return f"{self.role}@{self.tag}(ctx={self.num_ctx})"


STAGE_MODELS: dict[str, ModelSpec] = {
    # 需求补强：跑在 pm 之前，**刻意复用 pm 的同一个 tag** —— 两者相邻执行，
    # 同 tag 不会触发 ensure_exclusive 卸载重载，全流程仍是 3 次模型切换。
    # 同为「读需求 → 填字段」的结构化提取任务，temperature 与 pm 同档（0.15）。
    #
    # 曾试过换成 14B（2026-09-25），**已退回**：换上去并没有解决它要解决的问题
    # （漏掉「吃完食物蛇变长」这类「太显然」的隐含参数），却实测 intake 一次要
    # 123s（其中 load 72s）—— 见 prompts.SYSTEM["intake"] 里那段说明：
    # 真正的解法是「让模型先写验收测试」，而且必须是一次**以写测试为唯一任务**的
    # 调用；把这条指令塞进多用途提示词里（8B/14B 都试过）不生效。
    "intake": ModelSpec("需求补强", "qwen3-8b-pm-16k", PM_NUM_CTX, 10000, True, 0.15, 4096),
    # temperature 0.15：PM 是「结构化提取」而非创作任务（读需求 → 填字段），
    # 高温度只会让字段取值漂移（同一需求两次运行给出不同的 priority/severity），
    # 下游却要按它推进。实测参考 dev=0.2、review=0.3，PM 应更低。
    "pm": ModelSpec("产品经理", "qwen3-8b-pm-16k", PM_NUM_CTX, 10000, True, 0.15, 4096),
    # 两个架构师阶段共用同一个 tag，但 temperature 是**请求级**参数，可以分开设：
    #   assess＝事实盘点，不需要创造性，压到 0.15（与 PM 同档）；
    #   plan＝方案设计，需要一定的发散，但也不能像 0.6 那样飘，取 0.4。
    "architect_assess": ModelSpec("架构师", "qwen3-14b-arch-8k", 8192, 4800, True, 0.15, 3072),
    "architect_plan": ModelSpec("架构师", "qwen3-14b-arch-8k", 8192, 4800, True, 0.4, 3072),
    "dev": ModelSpec("开发工程师", "qwen2.5-coder-7b-dev-24k", 24576, 12000, None, 0.2, 6144),
    "test": ModelSpec("测试工程师", "qwen2.5-coder-7b-dev-24k", 24576, 12000, None, 0.2, 4096),
    "review": ModelSpec("评审", "qwen3-14b-arch-8k", 8192, 4800, True, 0.3, 3072),
    # 前置「全局架构」节点（pipeline/gateway.py 调用，见 flow.PRE_NODES）—— 入口总闸。
    # 同样跑在 14B-arch-8k 上（沿用同一 tag ⇒ 与 architect/review 之间不额外换模），
    # 但**预算分配刻意与 architect_* 不同**：
    #   architect_* 是 4800 + 3072 = 7872（留 320 富余）；
    #   而本节点的输出字段数明显更多（modules + interface_contracts + execution_order
    #   + integration_checkpoints + uncertainties），3~5 个模块的方案 JSON 就要 1200~2500 token，
    #   沿用 3072 很容易撞顶被截断 ⇒ schema 校验失败 ⇒ 重试一次仍失败 ⇒ 降级为 small，
    #   表现是「这个节点等于没干活」，是最难排查的一类故障。
    # 因此把输入预算压到 3800、输出提到 4096（3800 + 4096 = 7896 < num_ctx 8192）。
    # 输入变紧是**故意的**：它倒逼存量概览只喂「目录树 + 文件/行数统计 + 顶层符号索引」，
    # 绝不塞代码正文 —— 全局架构要做的是划边界，不是读实现。
    "global_architecture_analysis": ModelSpec(
        "全局架构", "qwen3-14b-arch-8k", 8192, 3800, True, 0.2, 4096
    ),
}

# 全量执行顺序（架构师两次调用共用同一模型驻留，因此只需 3 次模型切换）。
# 从流定义真源派生，见 pipeline/flow.py —— 以前这里与 runstore.FLOW_ORDER 各写一份，容易漏改。
FULL_STAGE_ORDER = list(flow.FLOW_ORDER)

# 交付前人工审核闸门：review 通过之后、正式收尾之前，强制暂停等人工核对 4 项
# （核心路径通顺 / 无明显低级硬伤 / 交付物齐全 / 对照需求核心诉求满足），
# 通过则放行交付，打回则回流到开发修复。设为 False 可关闭（跳过该闸门直接交付）。
HUMAN_REVIEW_GATE = os.getenv("PIPELINE_HUMAN_REVIEW", "1") not in ("0", "false", "False")

# PM 未决项闸门：PM 一旦提出未决问题（open_questions 非空），就在 pm 阶段后强制暂停，等人工确认。
# 这些未决项是**带着默认取值**往下走的：默认值一旦猜错，下游方案/实现/测试全都建在错误前提上，
# 返工成本远高于停下来问一句。没有未决项时不暂停 —— 无条件停在 pm 只是白白浪费人机交互。
# 设为 0 可关闭（PIPELINE_PAUSE_ON_OPEN_QUESTIONS=0）。
PAUSE_ON_OPEN_QUESTIONS = os.getenv("PIPELINE_PAUSE_ON_OPEN_QUESTIONS", "1") not in ("0", "false", "False")

# 需求补强闸门：补强阶段识别出 **high 重要度**的待确认项时，在 intake 后强制暂停等人工确认。
# 与 PM 的 open_questions 闸门同理 —— 建议取值一旦猜错，下游 PM/架构/开发/测试全建在错误前提上。
#
# ⚠️ 环境变量名是 **PIPELINE_INTAKE_PAUSE**，不是 PIPELINE_INTAKE_PAUSE_ON_GAPS ——
#    按后者设置会静默无效（已有人踩过）。页面「配置」里对应 `intake_pause_on_gaps`。
#
# 关于默认值（2026-09-25 的结论）：**保持关闭**。
#   · 曾一度想把默认改成开启，理由是判定条件本身已经**只筛 high**
#     （见 _gate_after → _intake_high_gaps），并不存在原注释所说的「无脑停」；
#     而真机 run 20260925-153925 就是踩在这上面 —— 补强给出 3 条 high
#     （蛇的移动速度 / 网格尺寸 / 蛇身长度增长方式），却因开关关闭直接流到 PM，
#     人工到 PM 阶段才发现「补强漏了东西没人问」。
#   · 但改默认会连带打断**端到端的自动化运行**（mock 冒烟里 21 条「应当跑到 done」
#     的断言立刻变成 paused），说明「不打断即可跑完」是被测试固化的既定默认行为。
#   · 所以：**代码默认不动**，需要入口卡人的场景（需求普遍模糊）用**本地覆盖**
#     （config.local.json 的 runtime.intake_pause_on_gaps，页面「配置」里勾）或
#     `--pause-after intake` 显式指定。本地覆盖只影响这台机器，改完无需动代码。
INTAKE_PAUSE_ON_GAPS = os.getenv("PIPELINE_INTAKE_PAUSE", "0") not in ("0", "false", "False")

# ------------------------------------------------------------------ 运行验证（verify 阶段）
# 「最终输出结果的验证与确认」：把补丁物化到沙箱（runs/<id>/verify/work）后真的跑一遍，
# 把退出码与输出当**机械证据**喂给评审 —— 而不是让模型读文件猜「能不能跑」。
# 安全约定：只在沙箱副本里跑、只跑白名单程序、逐条超时、输出截断、命中危险片段不执行；
# 验证失败即使在评审里被判 pass 也会被改判 rework_dev。设为 0 可关闭（PIPELINE_VERIFY=0）。
VERIFY_ENABLED = os.getenv("PIPELINE_VERIFY", "1") not in ("0", "false", "False")
# 单条命令超时（秒）：给足编译/启动时间，又不至于让死循环挂住整条流水线
VERIFY_TIMEOUT = int(os.getenv("PIPELINE_VERIFY_TIMEOUT", "180"))
# 最多执行几条命令（含必跑的语法检查）
VERIFY_MAX_COMMANDS = int(os.getenv("PIPELINE_VERIFY_MAX_COMMANDS", "5"))
# 复制仓库进沙箱的体积上限（MB）：超了就只物化补丁涉及的文件，并记一条 note
VERIFY_COPY_LIMIT_MB = int(os.getenv("PIPELINE_VERIFY_COPY_LIMIT_MB", "1500"))
# 允许执行的程序（首 token，按小写、去扩展名比对）；不在表里的命令只记录、不执行
VERIFY_ALLOWED_BINS = frozenset(
    {"python", "pytest", "node", "npm", "npx", "go", "cargo", "dotnet", "java", "mvn", "gradle"}
)
# 沙箱复制时跳过的目录（体积大 / 与验证无关）
# 注意 "runs"：默认 RUNS_DIR 就是仓库根的 runs/，而 verify 沙箱本身位于
#   runs/<id>/verify/work
# 里 —— 不跳过它，复制过程会一边遍历 runs/ 一边在 runs/ 下新建沙箱目录，
# 形成「边复制边给自己造新文件」的递归式膨胀（真机表现为 verify 长时间不返回）。
# 全局架构作业会按模块数把它放大 N 倍，所以这里必须排掉。
VERIFY_SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
        "dist", "build", ".next", ".nuxt", "target", ".idea", ".vscode",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".gradle",
        "runs",
    }
)
# 明显危险/破坏性的片段：命中就**不执行**，只把「打算跑什么 + 为什么没跑」记进报告
VERIFY_DENY_PATTERNS = (
    "rm -rf", "rm -fr", "rd /s", "rmdir /s", "del /s", "del /q", "format ",
    "mkfs", "shutdown", "reboot", "diskpart", "reg delete", "reg add",
    "taskkill", "stop-process", "invoke-expression", "iex(", "iex ",
    "start-process", "curl |", "wget |", "| sh", "| bash", "chmod 777",
    "sudo ", "runas", "net user", "sc delete", "bcdedit", "> /dev/sd",
)

# ------------------------------------------------------------------ 语义分析（pyright）
# ast 层只能做**字面**判定（语法 / 跨模块未定义名 / import 契约），而真机上最贵的一类
# 缺陷恰好是它抓不到的：属性不存在、参数个数不匹配、跨函数返回值的类型推断。
# 2026-09-25 实测：pyright 在 5 文件样本上 2.1s 抓到上述三条，全部是 ast 抓不到的。
#
# **刻意做成可选增强**：pyright 装在全局 npm 目录（`npm i -g pyright`），换机器/换环境
# 就没有。探测不到时各接口返回空结果并说明原因，绝不让流水线因此失败。
LSP_ENABLED = os.getenv("PIPELINE_LSP", "1") not in ("0", "false", "False")
# 单次诊断超时（秒）。实测：5 文件 2.1s、20 文件 4.9s、整个项目根 5.7s。
LSP_TIMEOUT = int(os.getenv("PIPELINE_LSP_TIMEOUT", "120"))
# 单次最多回灌多少条诊断给模型 —— 太多会淹没真正要修的那条
LSP_MAX_DIAGNOSTICS = int(os.getenv("PIPELINE_LSP_MAX_DIAGNOSTICS", "8"))
# 这些 rule 属「高置信」：符号/属性不存在、参数与调用不匹配 —— 几乎不可能是误报，
# 可以进 dev 的重问清单；其余（类型推断、可选值下标等）只记进报告让评审与人工看。
# 引用查找（LSP）：首次请求要等 pyright 索引完工作区，实测约 2~3s（且**不需要**逐个
# didOpen，server 会自己扫盘），所以做成「启动一次会话、多个符号复用 + 轮询到有结果」。
# 轮询上限 × 间隔 = 单符号最多等多久。
LSP_REFERENCE_ROUNDS = int(os.getenv("PIPELINE_LSP_REFERENCE_ROUNDS", "6"))
LSP_REFERENCE_INTERVAL = float(os.getenv("PIPELINE_LSP_REFERENCE_INTERVAL", "1.0"))
# 一次会话最多查多少个符号：每个符号要单独一轮请求，避免把时间花在长尾上
LSP_MAX_SYMBOLS = int(os.getenv("PIPELINE_LSP_MAX_SYMBOLS", "8"))
# 整段引用查找的墙钟上限（秒）：超了就放弃 LSP、退回 ast 结果，不拖住 verify。
# 实测瓶颈在 server 启动 + 首次索引（约 3s），会话建立后每个符号只要零点几秒。
LSP_REFERENCE_BUDGET = int(os.getenv("PIPELINE_LSP_REFERENCE_BUDGET", "45"))
LSP_BLOCKING_RULES = frozenset(
    {
        "reportAttributeAccessIssue",
        "reportUndefinedVariable",
        "reportCallIssue",
        "reportGeneralTypeIssues",
        "reportMissingImports",
        "reportIndexIssue",
    }
)

# 各阶段可分配给"存量代码片段"的 token 预算（14B 只有 8K，必须省着用）
CODE_BUDGET: dict[str, int] = {
    "architect_assess": 2600,
    "architect_plan": 2600,
    "dev": 6000,
    "test": 5000,
    "review": 0,
}

# 单文件分片上限：取小值才能让多个文件同时入场（1500 会让一个文件吃满整个预算）
PER_FILE_TOKENS = 700

# 检索池里每个文件先留多一些（要装得下目标函数体），各阶段再按自己的上限二次裁剪。
# 真实项目教训：71KB 的单文件里，目标函数 index_all 本身就 ~1900 token，池子里只留 700 是装不下的。
POOL_PER_FILE_TOKENS = 2600

# 各阶段单文件切片上限：
#   架构师/评审只有 8K，必须省着用；开发要看到**完整目标函数**才能给出可应用的补丁，所以给大值。
STAGE_PER_FILE_TOKENS: dict[str, int] = {
    "architect_assess": 900,
    "architect_plan": 900,
    "dev": 2600,
    "test": 2200,
    "review": 0,
}

# 开发分两遍：第一遍只新增辅助函数（搭脚手架），第二遍带第一遍产物回填主函数体。
# 真机教训：7B coder + 单文件 2600 token 素材，一次写不深一个 100 行函数；
# 拆成「先铺小函数、再薄薄地串起来」后，full_symbol 的主函数更容易写完整、写对。
DEV_TWO_PASS = True

# 新增文件内容不合法时，**带问题重问 dev 的次数**（0 = 关闭）。
# 为什么必须重问而不是只记阻断：7B 在「回填」那遍会把文件写断（真机 run 20260924-185507 的
# renderer.py 连续 4 轮停在 `print(f'{`），而它的 JSON 本身是合法的 → 契约重试不会触发。
# 只记阻断的代价是**整整一轮**（dev+test+verify+review ≈5 分钟 + 一次 14B 评审），
# 而模型下一轮照样写断 —— 典型的「改不动却一直返工」。重问一次只要几十秒。
DEV_CONTENT_REPAIR_TRIES = int(os.getenv("PIPELINE_DEV_CONTENT_REPAIR_TRIES", "2"))


# --------------------------------------------------------------------------- 交付落盘
# 把沙箱里验证通过的补丁**真正写回用户目标目录**。
# 为什么必须有这一项：apply_all 长期只被 verify 阶段调用一次且固定 in_place=False，
# 产物只落在 runs/<id>/verify/work —— 真机 run 20260924-185507 / 20260924-135801
# 跑完 8 轮评审后目标目录 D:\AI\CODE 仍是空的，等于全部算力白烧。
# 设为 0 则退回「只验证不交付」（产物仅留在沙箱，便于审查后再手动物化）。
DELIVER_ENABLED = os.getenv("PIPELINE_DELIVER", "1") not in ("0", "false", "False")

# --------------------------------------------------------------------------- 收敛与预算护栏
# 停滞护栏：最近 N 轮「待修项数量」没有下降就停止返工、转人工。
# 真机 run 20260924-185507 的 required_fixes 走势 3→2→0→2→4→4→5→4 —— 第 3 轮已 pass
# 之后又反弹，一路烧到 attempt=11/max=12。光靠 max_rework 只能在撞顶时才停，
# 而这一路并没有变好，纯粹在烧算力与独占显存（单驻留期间无法新建运行）。
# 0 = 不做停滞判定（只保留 max_rework）。
REWORK_STAGNATION_LIMIT = int(os.getenv("PIPELINE_STAGNATION", "3"))

# 运行级预算护栏（0 = 不限）。注意与 REQUEST_TIMEOUT 区分：后者是**单次 HTTP 请求**超时，
# 挡不住「一轮返工累积很久」这种情况。
MAX_WALL_S = int(os.getenv("PIPELINE_MAX_WALL_S", "0"))
MAX_TOTAL_TOKENS = int(os.getenv("PIPELINE_MAX_TOKENS", "0"))

# --------------------------------------------------------------------------- 裁决参谋
# 人工在闸门上裁决时，可以就某一条**反复**向模型提问（风险 / 收益 / 可逆性 / 建议）。
# 为什么要有它：裁决本质是「判断」而不是「提取」—— 让人对着两行占位文字做决定，
# 等于逼他猜。真机 run 20260925-140707 的「游戏分辨率」补强只给了「建议补充：需明确
# 窗口尺寸或画布大小」，人工要么自己拍、要么去别处问。
#
# 它**不是流程节点**：不进 EXEC_ORDER / STAGE_MODELS / STAGE_SCHEMAS 等注册表，
# 也不产出阶段 artifact —— 问答逐轮追加到 runs/<id>/advice/<stage>.jsonl（可读、可审计），
# 是「随闸门可用」的旁路环节。
ADVICE_ENABLED = os.getenv("PIPELINE_ADVICE", "1") not in ("0", "false", "False")
# 用哪个模型：留空 = 复用**该阶段自己的模型**（建议要落在该阶段的视角上，
# 也避免多引入一个要加载的 tag）。想要更强的判断力时设 PIPELINE_ADVICE_TAG
# （例如 qwen3-14b-arch-8k）。
ADVICE_TAG = os.getenv("PIPELINE_ADVICE_TAG", "").strip()
# 带几轮历史进上下文（0 = 每轮独立）。裁决问答是**多轮**的，但历史太长会挤掉
# 阶段产物本身 —— 那才是判断的依据。
ADVICE_MAX_HISTORY = int(os.getenv("PIPELINE_ADVICE_HISTORY", "4"))
# 单轮墙钟上限（秒）。旁路环节不该因为一次调用把页面吊死。
ADVICE_TIMEOUT = int(os.getenv("PIPELINE_ADVICE_TIMEOUT", "600"))
# 阶段产物注入裁决参谋时的字符预算（不够就从尾部截断）
ADVICE_CONTEXT_CHARS = int(os.getenv("PIPELINE_ADVICE_CONTEXT_CHARS", "6000"))


# --------------------------------------------------------------------------- 本地覆盖
# 「配置键 -> 模块变量」映射：恢复默认值与应用覆盖共用，保证 _apply_local_overrides 可重复调用。
RUNTIME_SCALARS: dict[str, str] = {
    "review_every": "REVIEW_EVERY",
    "max_rework": "MAX_REWORK_ROUNDS",
    "request_timeout": "REQUEST_TIMEOUT",
    "pm_num_ctx": "PM_NUM_CTX",
    "chars_per_token": "CHARS_PER_TOKEN",
    "prompt_hard_ratio": "PROMPT_HARD_RATIO",
    "per_file_tokens": "PER_FILE_TOKENS",
    "pool_per_file_tokens": "POOL_PER_FILE_TOKENS",
    "verify_timeout": "VERIFY_TIMEOUT",
    "verify_max_commands": "VERIFY_MAX_COMMANDS",
    "rework_stagnation": "REWORK_STAGNATION_LIMIT",
    "max_wall_s": "MAX_WALL_S",
    "max_total_tokens": "MAX_TOTAL_TOKENS",
    "advice_max_history": "ADVICE_MAX_HISTORY",
    "advice_context_chars": "ADVICE_CONTEXT_CHARS",
    "lsp_timeout": "LSP_TIMEOUT",
    "lsp_max_diagnostics": "LSP_MAX_DIAGNOSTICS",
}
RUNTIME_FLAGS: dict[str, str] = {
    "dev_two_pass": "DEV_TWO_PASS",
    "human_review_gate": "HUMAN_REVIEW_GATE",
    # 此前只认环境变量 PIPELINE_PAUSE_ON_OPEN_QUESTIONS，漏登记在这张表里 ——
    # 后果是「PM 未决项闸门」在页面「配置」里看不到、也写不进 config.local.json，
    # 于是想让运行无人值守的人把 human_review_gate / intake_pause_on_gaps 都关了，
    # 它照样在 PM 结束后停下等人（真机 run 20260925-153925 就停在这里）。
    "pause_on_open_questions": "PAUSE_ON_OPEN_QUESTIONS",
    "intake_pause_on_gaps": "INTAKE_PAUSE_ON_GAPS",
    "verify": "VERIFY_ENABLED",
    "deliver": "DELIVER_ENABLED",
    "advice": "ADVICE_ENABLED",
    "lsp": "LSP_ENABLED",
}
BUDGET_DICTS = ("CODE_BUDGET", "STAGE_PER_FILE_TOKENS")


def apply_overrides() -> None:
    """应用 `pipeline/config.local.json`（操作页面「配置」页写入）。

    必须在**模块初始化末尾**执行：下游 `orchestrator` 是用
    `from .config import (DEV_TWO_PASS, MAX_REWORK_ROUNDS, ...)` 绑定值的，
    晚一步它们手里那份就还是代码默认值。

    注意 `STAGE_MODELS` 是「原地替换元素」而不是换掉整个字典 ——
    持有该字典引用的代码（如 issues.pipeline_fingerprint）才能看到变化。
    """
    from . import local_config  # 局部导入，避免与同包模块形成循环依赖

    rt = local_config.runtime()

    # 0) 先恢复代码默认值：这样本函数可重复调用（页面保存/还原后 server 会重新调用它），
    #    也让「删除某项覆盖」能真正退回默认值 —— 否则会残留上一次的覆盖结果。
    for stage, fields in (CODE_DEFAULTS.get("models") or {}).items():
        STAGE_MODELS[stage] = ModelSpec(**fields)
    base_runtime = CODE_DEFAULTS.get("runtime") or {}
    for key, name in RUNTIME_SCALARS.items():
        globals()[name] = base_runtime.get(key)
    for key, name in RUNTIME_FLAGS.items():
        globals()[name] = base_runtime.get(key)
    for name in BUDGET_DICTS:
        target = globals()[name]
        target.clear()
        target.update((CODE_DEFAULTS.get("budgets") or {}).get(name) or {})

    # 1) 运行时标量
    for key, name in RUNTIME_SCALARS.items():
        if rt.get(key) is not None:
            globals()[name] = rt[key]
    # 这两个的最小值与原表达式保持一致，避免覆盖进 0 之类非法值
    globals()["REVIEW_EVERY"] = max(1, int(globals()["REVIEW_EVERY"]))
    globals()["MAX_REWORK_ROUNDS"] = max(0, int(globals()["MAX_REWORK_ROUNDS"]))
    for key, name in RUNTIME_FLAGS.items():
        if rt.get(key) is not None:
            globals()[name] = bool(rt[key])

    # 2) 逐阶段的 token 预算（dict，原地更新）
    for name in BUDGET_DICTS:
        incoming = rt.get(name)
        if isinstance(incoming, dict):
            globals()[name].update(incoming)

    # 3) 各阶段模型：ModelSpec 是 frozen dataclass，字段变更只能整体重建
    model_over = local_config.models()
    for stage, fields in model_over.items():
        base = STAGE_MODELS.get(stage)
        if base is None:
            continue
        try:
            STAGE_MODELS[stage] = ModelSpec(
                role=str(fields.get("role", base.role)),
                tag=str(fields.get("tag", base.tag)),
                num_ctx=int(fields.get("num_ctx", base.num_ctx)),
                prompt_token_budget=int(fields.get("prompt_token_budget", base.prompt_token_budget)),
                think=fields.get("think", base.think),
                temperature=float(fields.get("temperature", base.temperature)),
                num_predict=int(fields.get("num_predict", base.num_predict)),
            )
        except (TypeError, ValueError) as exc:
            # 配置是手写的，写坏了要能降级而不是让流水线起不来
            print(f"[local_config] models.{stage} 覆盖无效，已沿用默认值：{exc}")

    # PM 上下文：改 PIPELINE 的 pm_num_ctx 也要同步进 STAGE_MODELS['pm']，
    # 否则这一项改了却不生效（STAGE_MODELS 在模块定义时就把 PM_NUM_CTX 固化了）
    if rt.get("pm_num_ctx") is not None and "num_ctx" not in (model_over.get("pm") or {}):
        base = STAGE_MODELS["pm"]
        STAGE_MODELS["pm"] = ModelSpec(
            role=base.role,
            tag=base.tag,
            num_ctx=int(rt["pm_num_ctx"]),
            prompt_token_budget=base.prompt_token_budget,
            think=base.think,
            temperature=base.temperature,
            num_predict=base.num_predict,
        )


# 代码默认值快照：必须在应用覆盖**之前**记录，否则页面无法区分
# 「当前值」与「默认值」，也就无从判断是否被本地配置改动过。
CODE_DEFAULTS: dict[str, Any] = {
    "models": {stage: asdict(spec) for stage, spec in STAGE_MODELS.items()},
    "runtime": {
        "review_every": REVIEW_EVERY,
        "max_rework": MAX_REWORK_ROUNDS,
        "request_timeout": REQUEST_TIMEOUT,
        "pm_num_ctx": PM_NUM_CTX,
        "dev_two_pass": DEV_TWO_PASS,
        "human_review_gate": HUMAN_REVIEW_GATE,
        # 必须与 RUNTIME_FLAGS 一一对应：缺这一项时页面「还原默认」会把
        # PAUSE_ON_OPEN_QUESTIONS 还原成 None（falsy），等于**静默关掉闸门**。
        "pause_on_open_questions": PAUSE_ON_OPEN_QUESTIONS,
        "intake_pause_on_gaps": INTAKE_PAUSE_ON_GAPS,
        "chars_per_token": CHARS_PER_TOKEN,
        "prompt_hard_ratio": PROMPT_HARD_RATIO,
        "per_file_tokens": PER_FILE_TOKENS,
        "pool_per_file_tokens": POOL_PER_FILE_TOKENS,
        "verify_timeout": VERIFY_TIMEOUT,
        "verify_max_commands": VERIFY_MAX_COMMANDS,
        "verify": VERIFY_ENABLED,
        "deliver": DELIVER_ENABLED,
        "lsp": LSP_ENABLED,
        "lsp_timeout": LSP_TIMEOUT,
        "lsp_max_diagnostics": LSP_MAX_DIAGNOSTICS,
        "rework_stagnation": REWORK_STAGNATION_LIMIT,
        "max_wall_s": MAX_WALL_S,
        "max_total_tokens": MAX_TOTAL_TOKENS,
        "advice": ADVICE_ENABLED,
        "advice_max_history": ADVICE_MAX_HISTORY,
        "advice_context_chars": ADVICE_CONTEXT_CHARS,
    },
    "budgets": {
        "CODE_BUDGET": dict(CODE_BUDGET),
        "STAGE_PER_FILE_TOKENS": dict(STAGE_PER_FILE_TOKENS),
    },
}


apply_overrides()
