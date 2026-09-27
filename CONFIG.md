# 配置参考

这是 `pipeline/config.py` 的对外说明。**调参不用翻代码**，但改代码时请同步这份表。

三层优先级（后者覆盖前者）：

```
代码默认值（本文件记的就是这层）
  └─ 环境变量            ← 手工/脚本临时调
       └─ config.local.json  ← 操作页面「配置」页写入，落盘持久
```

> ⚠️ 表中「默认值」一律指**代码默认值**。本机若在页面上改过某项，`config.local.json`
> 会覆盖它 —— 想确认当前实际生效值，看页面「配置」页，或读 `pipeline/config.local.json`。
> 典型例子：本仓库的 `config.local.json` 就把三个人工闸门都关了（见下表末尾说明）。

环境变量读取统一走 `_env_int` / `_env_float` / `_env_bool` 三个helper（2026-09-26 起）：
**值非法不会被拦在启动前**，会打印一行告警并降级为默认值。此前是裸 `int(os.getenv(...))`，
`PIPELINE_TIMEOUT=180s` 这种带单位的误输入会让 `config` 在导入时抛 `ValueError`，
表现为「整条流水线起不来」，报错栈还指向 config 内部，看不出是哪个变量的问题。

## 一、流程与返工

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PIPELINE_MAX_REWORK` | `2` | 回流上限。达到上限仍未 pass 则标记 `needs_human` 并保留全部中间产物 |
| `PIPELINE_REVIEW_EVERY` | `2` | 每 N 轮评审一次（`1` = 每轮）。**首轮与末轮必定评审** —— 前者为早暴露问题，后者为拿到真实判定而不是直接 `needs_human`。取 2 时典型 3 轮路径只评审第 1、3 轮，省 2 次模型切换 |
| `PIPELINE_STAGNATION` | `3` | 停滞判定：最近 N 轮「待修项数量」没有净下降就停。`0` = 关闭。判据是**整个窗口末项 < 首项**，不是「比上一轮少」 |
| `PIPELINE_MAX_WALL_S` | `0` | 运行级墙钟上限（秒）。`0` = 不限 |
| `PIPELINE_MAX_TOKENS` | `0` | 运行级 token 上限。`0` = 不限 |
| `PIPELINE_HUMAN_REWORK_BUDGET` | `1` | 人工点一次「打回」时给上限追加的轮数（单调不减）。没有它，人工在 `attempt` 已超上限时打回只会换来一轮必死 |
| `PIPELINE_HUMAN_REWORK_TOPUP_MAX` | `3` | 人工追加次数上限（防无人值守时无限增长）。到顶后需显式给 `--max-rework` |

## 二、人工闸门

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PIPELINE_HUMAN_REVIEW` | `1` | **交付前人工审核闸门**：评审通过后、正式收尾前强制暂停，等人工核对 4 项 |
| `PIPELINE_PAUSE_ON_OPEN_QUESTIONS` | `1` | **PM 未决项闸门**：PM 提出未决问题（已带默认取值）时暂停。没有未决项时不暂停 |
| `PIPELINE_INTAKE_PAUSE` | `0` | **需求补强闸门**：补强识别出 `high` 重要度缺口时暂停 |

> ⚠️ 需求补强的环境变量名是 `PIPELINE_INTAKE_PAUSE`，**不是** `PIPELINE_INTAKE_PAUSE_ON_GAPS` ——
> 按后者设置会**静默无效**。页面「配置」里对应 `intake_pause_on_gaps`。
>
> `INTAKE_PAUSE` 默认关闭是**刻意的**：改成默认开启会打断端到端自动运行
> （冒烟里 21 条「应当跑到 done」的断言会立刻变成 paused）。需要入口卡人的场景用页面覆盖
> 或 `--pause-after intake` 显式指定。

## 三、机械验证（verify 节点）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PIPELINE_VERIFY` | `1` | 是否跑机械验证：物化沙箱 → 计划命令 → **真实执行** → 汇总结论 |
| `PIPELINE_VERIFY_TIMEOUT` | `180` | 单条命令超时（秒）。给足编译/启动时间，又不至于让死循环挂住整条流水线 |
| `PIPELINE_VERIFY_MAX_COMMANDS` | `5` | 最多执行几条命令（含必跑的语法检查） |
| `PIPELINE_VERIFY_COPY_LIMIT_MB` | `1500` | 复制仓库进沙箱的体积上限。**超限会判负**（沙箱不完整则结论不可信） |

补充两条硬编码在 `verify.py` 里的判据：

- **命令指向的程序在本环境不存在**（如测试阶段声明了 `pytest` 但沙箱没装）→ 记为
  `unavailable`，**不计入交付物成败**。这类失败该由「命令产出方」（测试阶段）负责。
- **沙箱跳过了源码文件** → 直接判负并跳过执行。语法检查查不到它、导入检查报莫名的
  `ImportError`，那种沙箱里得出的结论与「真实交付物能不能跑」是两回事。

## 四、语境预算与检索

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PIPELINE_PM_CTX` | `16384` | PM 上下文。实测 prompt 仅 ~300 token，16K 足够（省约 1.2GB 显存）。要喂 >6000 字的需求文档时临时调到 `32768` |
| `PIPELINE_TOK_ASCII` | `0.28` | token 估算系数：每 ASCII 字符的 token 数 |
| `PIPELINE_TOK_NONASCII` | `0.76` | token 估算系数：每非 ASCII 字符的 token 数 |

**token 估算系数是量出来的，不是猜的。** 旧的固定比例「1.6 字符/token」在真机 41 条样本上
对照 Ollama 实回的 `prompt_eval_count`，**平均高估 40.9%（且全是高估）** —— 近一半预算浪费在
「以为占了、其实没占」的额度上，真正该喂的代码被提前截掉。现按字符类别加权，同一批样本
平均绝对误差 **1.7%**。

换模型（分词器不同）后重新校准：

```powershell
python tools\calibrate_tokens.py
```

页面「配置」页里的 `chars_per_token` 是**反向**换算因子（token 上限 → 字符上限）的兜底值，
与上面两个系数不是一回事，一般不用动。

## 五、单驻留与显存

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PIPELINE_KEEP_ALIVE` | `10m` | 阶段驻留期内的 `keep_alive`。切换模型时显式卸载，**不依赖它** |
| `PIPELINE_TIMEOUT` | `1800` | **单次 HTTP 请求**超时（秒）。注意它挡不住「一轮返工累积很久」，那是 `PIPELINE_MAX_WALL_S` 的事 |

显存不够时先调 `PIPELINE_PM_CTX`（影响最小），不要动 `STAGE_MODELS` 里的 `num_ctx` ——
8K 是 14B 能全量上 10GB 卡的上限，12K 就会溢出到共享内存，prefill 掉一个数量级。

## 六、LSP 增强（可选）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PIPELINE_LSP` | `1` | 是否启用 LSP 增强（影响面扫描的精度）。**探测不到 server 时静默退回 ast，绝不阻断流水线** |
| `PIPELINE_LSP_TIMEOUT` | `120` | 单次诊断超时（秒）。实测 5 文件 2.1s / 20 文件 4.9s |
| `PIPELINE_LSP_MAX_DIAGNOSTICS` | `8` | 单次最多回灌多少条诊断给模型 —— 太多会淹没真正要修的那条 |
| `PIPELINE_LSP_REFERENCE_ROUNDS` | `6` | 引用查找的轮询次数上限 |
| `PIPELINE_LSP_REFERENCE_INTERVAL` | `1.0` | 轮询间隔（秒）。`轮询次数 × 间隔` = 单符号最长等待 |
| `PIPELINE_LSP_MAX_SYMBOLS` | `8` | 一次会话最多查多少个符号（避免把时间花在长尾上） |
| `PIPELINE_LSP_REFERENCE_BUDGET` | `45` | 整段引用查找的墙钟上限（秒）。超了退回 ast 结果，不拖住 verify |

## 七、开发自检与交付

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PIPELINE_DEV_CONTENT_REPAIR_TRIES` | `2` | 新增文件内容不合法时**带问题重问 dev** 的次数（`0` = 关闭）。重问一次几十秒，而只记阻断的代价是整整一轮（≈5 分钟 + 一次 14B 评审） |
| `PIPELINE_DELIVER` | `1` | 是否把补丁落盘到目标仓库 |

## 八、裁决参谋（闸门上的旁路问答）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PIPELINE_ADVICE` | `1` | 是否启用。它不是流水线阶段，是**随闸门可用**的旁路 |
| `PIPELINE_ADVICE_TAG` | *(空)* | 强制指定模型 tag。默认复用该阶段自己的模型 |
| `PIPELINE_ADVICE_HISTORY` | `4` | 带几轮历史进上下文（`0` = 每轮独立）。太长会挤掉阶段产物本身 |
| `PIPELINE_ADVICE_TIMEOUT` | `600` | 单轮墙钟上限（秒）。旁路环节不该把页面吊死 |
| `PIPELINE_ADVICE_CONTEXT_CHARS` | `6000` | 阶段产物注入时的字符预算（不够就从尾部截断） |

## 九、路径与外部依赖

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PIPELINE_RUNS_DIR` | `<仓库>/runs` | 产物根目录 |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama 地址 |
| `PIPELINE_LOCAL_CONFIG` | `<pipeline>/config.local.json` | 本地覆盖文件路径。**冒烟测试把它指到不存在的路径以隔离本机配置** |
| `PIPELINE_TRACE` | `1` | 是否写 `traces.jsonl`（完整 system/user + 模型原始输出）。置 `0` 省磁盘 |

## 十、模型矩阵

改这里要三处同步：`config.STAGE_MODELS`、`schemas.STAGE_SCHEMAS`、`flow.MODEL_NODES` ——
`flow.validate()` 会在启动期核对，漏改会直接报错（不会等到真机跑挂）。

| 阶段 | tag | ctx | 输入预算 | 最大输出 | think | temp |
|---|---|---|---|---|---|---|
| `intake` | `qwen3-8b-pm-16k` | 16384 | 10000 | 4096 | ✓ | 0.15 |
| `pm` | `qwen3-8b-pm-16k` | 16384 | 10000 | 4096 | ✓ | 0.15 |
| `architect_assess` | `qwen3-14b-arch-8k` | 8192 | 4800 | 3072 | ✓ | 0.15 |
| `architect_plan` | `qwen3-14b-arch-8k` | 8192 | 4800 | 3072 | ✓ | 0.4 |
| `dev` | `qwen2.5-coder-7b-dev-24k` | 24576 | 12000 | 6144 | — | 0.2 |
| `test` | `qwen2.5-coder-7b-dev-24k` | 24576 | 12000 | 4096 | — | 0.2 |
| `review` | `qwen3-14b-arch-8k` | 8192 | 4800 | 3072 | ✓ | 0.3 |
| `global_architecture_analysis`（入口总闸，非阶段） | `qwen3-14b-arch-8k` | 8192 | 3800 | 4096 | ✓ | 0.2 |

**输入预算 + 最大输出必须 < ctx**。最紧的是 14B 那三档（4800 + 3072 = 7872 < 8192，余 320 ≈ 3.9%）——
token 估算的误差上限是 3.2%，所以余量刚好够，**但别再往上加预算**。

`temperature` 是**请求级**参数，所以两个架构师阶段共用 tag 也能各设各的
（`assess`＝事实盘点 0.15；`plan`＝方案设计 0.4）。

## 十一、页面「配置」页可改的键

这些键写进 `config.local.json`，**键缺失 = 沿用代码默认值**（升级新增参数不会让旧配置失效）。

**标量**：`review_every` `max_rework` `request_timeout` `pm_num_ctx` `chars_per_token`
`prompt_hard_ratio` `per_file_tokens` `pool_per_file_tokens` `verify_timeout`
`verify_max_commands` `rework_stagnation` `max_wall_s` `max_total_tokens`
`advice_max_history` `advice_context_chars` `lsp_timeout` `lsp_max_diagnostics`

**开关**：`dev_two_pass` `human_review_gate` `pause_on_open_questions`
`intake_pause_on_gaps` `verify` `deliver` `advice` `lsp`

**预算字典**：`CODE_BUDGET` `STAGE_PER_FILE_TOKENS`

> ⚠️ 开关类只认这些写法为「假」：`0 / false / no / off / none / disable / disabled`（大小写不敏感）。
> 旧实现只认 `"0" / "false" / "False"`，写成 `No` 或 `off` 会被当成**真** ——
> 想关掉闸门却关不掉，而且是静默的。
>
> ⚠️ 关掉闸门会改变**冒烟测试的前提**：`tools/smoke_console.py` 起的是真服务、真子进程，
> 会读到 `config.local.json`。为此它已在导入前把 `PIPELINE_LOCAL_CONFIG` 指到不存在的路径
> （与 `tools/smoke_mock.py` 同一套做法）。你自己写的检查脚本也要注意这一点。

## 十二、相关文档

- `CONTEXT.md` —— 设计决策与真机教训的完整记录（**改代码前建议先读对应章节**）
- `README.md` —— 快速开始与常用命令
- `pipeline/config.py` —— 每个参数上方的注释都写了「为什么是这个值」，那才是第一手依据
