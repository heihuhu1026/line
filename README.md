# 二次开发需求流水线

把一句中文需求（「给客户列表加导出 Excel 按钮」）变成**可交付的代码改动**：
需求补强 → 产品经理 → 架构师 → 开发 → 测试 → 机械验证 → 评审 → 人工闸门 → 落盘交付。

跑在**本机 Ollama** 上，全离线；流水线本体**只用 Python 标准库**（无第三方运行时依赖）。

## 它是什么

- **多阶段 + 强制契约**：每个阶段的输出都过 JSON Schema 校验（服务端 `format=schema` 强约束 + 客户端兜底），不合格就重问，而不是让坏数据往下流。
- **显存单驻留**：10GB 卡只够一个模型常驻。每次调用前显式卸载其它模型，全流程 3 次模型切换。
- **机械验证，不靠模型猜**：`verify` 阶段把补丁物化到沙箱（仓库副本）后**真跑命令**——语法检查、导入检查、测试命令、入口命令，退出码与输出作为证据喂给评审。
- **返工有预算**：`rework_dev` / `rework_architect` 回流，带上限（默认 2 轮）与停滞检测；验证失败即使评审判 pass 也会被改判返工。
- **人工闸门**：可在 PM / 方案 / 交付前暂停，人工确认或打回重跑，意见注入对应阶段的 prompt。

## 运行环境

| 项 | 要求 |
|---|---|
| 操作系统 | Windows（路径与进程管理按 Windows 写；`os.replace` 加了文件锁重试） |
| Python | **3.12** |
| GPU | 建议 ≥10GB 显存（14B 全量上卡需要 9.0GB） |
| Ollama | 本机 `http://localhost:11434` |
| 运行时依赖 | **无**（仅标准库；`ruff`/`mypy` 只作开发态可选工具） |

需要三个模型 tag（由现有权重派生，秒级完成）：

| tag | 上下文 | 用在 |
|---|---|---|
| `qwen3-8b-pm-16k` | 16K | 需求补强 / 产品经理 |
| `qwen3-14b-arch-8k` | 8K | 架构师 / 评审 / 全局架构 |
| `qwen2.5-coder-7b-dev-24k` | 24K | 开发 / 测试 |

首次准备（或换了模型目录后）：

```powershell
cd D:\AI\line
powershell -File models\build_tags.ps1                                    # 建/更新三个 tag
powershell -NoProfile -ExecutionPolicy Bypass -File models\start_ollama.ps1  # 重启 ollama（带上服务参数）
python models\verify_tags.py                                              # 校验 ctx / 是否 100% GPU
```

> `start_ollama.ps1` 会带上 `OLLAMA_MODELS` 等服务参数。直接 `ollama serve` 或托盘冷启动会看到**空的模型列表**。

## 快速开始（约 2 分钟，不加载模型）

```powershell
cd D:\AI\line

python -m tools.smoke_mock       # 离线冒烟：契约 + 回流 + 闸门续跑，秒级，不占 GPU
python -m pipeline.cli --requirement "给列表加一个导出按钮" --mock   # 纯编排演练，不加载模型
```

想看图形界面：

```powershell
python -m pipeline.server --port 8787     # 打开 http://127.0.0.1:8787/（自动开浏览器）
```

或者直接双击根目录的 **`start.bat`**（一键起 Ollama + 操作台并打开浏览器；
`start.bat nobrowser` 只起服务，`start.bat restart` 先重启 Ollama）。

## 真机运行

```powershell
python tools\preflight.py                                  # 跑前预检 30 秒：显存 + prefill 是否正常
python -m pipeline.cli --requirement-file req.md --repo <仓库路径>
```

`preflight.py` 有告警就先别跑 —— 它专门抓「报表显示 100% GPU，其实被换到共享内存、
prefill 掉一个数量级」这种情况（真机踩过：14B 从 157 t/s 掉到 17 t/s，一次评审要 7 分钟）。

一轮全流程约 **4~12 分钟**。产物写在 `runs/<run_id>/`。

## 常用命令

```powershell
# 带人工闸门跑（PM 与方案结束后暂停，等人工确认）
python -m pipeline.cli --requirement-file req.md --repo <仓库路径> --pause-after pm,architect_plan

# 从暂停处继续 / 一路跑到底 / 只跑一轮就交人工
python -m pipeline.cli --resume 20260923-101010
python -m pipeline.cli --resume 20260923-101010 --no-pause
python -m pipeline.cli --resume 20260923-101010 --max-rework 0

# 打回某阶段重跑并带上人工意见
python -m pipeline.cli --resume 20260923-101010 --from architect_plan --feedback "不要动 config.py"

# 列表 / 只跑某几阶段 / 纯编排演练
python -m pipeline.cli --list
python -m pipeline.cli --requirement-file req.md --repo <仓库路径> --only pm,architect_assess
python -m pipeline.cli --requirement "..." --mock --mock-rework 1

# 工具
python tools\check_retrieval.py --repo <仓库路径> --requirement "需求文本" --budget 2600
python tools\show_run.py 20260922-234723          # 分阶段成本
python tools\calibrate_tokens.py                  # token 估算系数校准（见下）
python tools\apply_patches.py --run 20260923-011410            # 默认 dry-run，只写副本
python tools\apply_patches.py --run 20260923-011410 --in-place # 改仓库（先留 *.orig 备份）
python tools\report_issues.py                     # 跨运行问题汇总
python tools\smoke_console.py                     # 操作页面冒烟（临时端口，完整人机闭环）
```

## 环境变量

全部可选。数值型变量写错不会被拦在启动前 —— 会打印告警并降级为默认值。

| 变量 | 默认 | 说明 |
|---|---|---|
| `PIPELINE_MAX_REWORK` | `2` | 回流上限；达到后标记 `needs_human` 并保留全部中间产物 |
| `PIPELINE_REVIEW_EVERY` | `2` | 每 N 轮评审一次（1 = 每轮）。首轮与末轮必定评审 |
| `PIPELINE_HUMAN_REVIEW` | `1` | 交付前人工审核闸门 |
| `PIPELINE_PAUSE_ON_OPEN_QUESTIONS` | `1` | PM 提出未决问题时暂停等确认 |
| `PIPELINE_INTAKE_PAUSE` | `0` | 需求补强发现 high 重要度缺口时暂停（**注意变量名**，不是 `..._ON_GAPS`） |
| `PIPELINE_VERIFY` | `1` | 是否跑机械验证（物化沙箱 + 真实执行） |
| `PIPELINE_DELIVER` | `1` | 是否落盘到目标仓库 |
| `PIPELINE_TIMEOUT` | `1800` | 单次 HTTP 请求超时（秒） |
| `PIPELINE_MAX_WALL_S` | `0` | 运行级墙钟上限（秒），0 = 不限 |
| `PIPELINE_MAX_TOKENS` | `0` | 运行级 token 上限，0 = 不限 |
| `PIPELINE_PM_CTX` | `16384` | PM 上下文；需求文档很长时调到 `32768` |
| `PIPELINE_KEEP_ALIVE` | `10m` | 阶段驻留期内的 keep_alive |
| `PIPELINE_RUNS_DIR` | `./runs` | 产物根目录 |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama 地址 |
| `PIPELINE_TOK_ASCII` | `0.28` | token 估算系数：每 ASCII 字符 token 数 |
| `PIPELINE_TOK_NONASCII` | `0.76` | token 估算系数：每非 ASCII 字符 token 数 |

更多可调项（含页面「配置」页可改的项）见 `pipeline/config.py` 注释，
`config.local.json` 的本地覆盖优先于代码默认值。

## 产物在哪

```text
runs/<run_id>/
├─ console.log          运行日志（页面按阶段切分到流程图节点）
├─ state.json           游标 + 全部阶段产物（续跑的唯一真源）
├─ NN-<stage>.json      各阶段产物（含评审意见、测试用例、验证报告）
├─ llm-calls.jsonl      每次模型调用的埋点（token 数、耗时、是否换模）
├─ traces.jsonl         完整 system/user 与模型原始输出（含 thinking）
├─ patches/             补丁 + *.patch（可直接 git apply）
└─ verify/work/         验证沙箱（仓库副本 + 已应用补丁；不写真实仓库）
```

## token 估算为什么要校准

预算裁剪需要估算 prompt 的 token 数。旧的固定「1.6 字符/token」在真机 41 条样本上
实测**平均高估 40.9%**（且全是高估）——近一半预算浪费在「以为占了、其实没占」的额度上，
真正该喂的代码被提前截掉。

现在按字符类别加权（ASCII 0.28 / 非 ASCII 0.76 token/字），同一批样本平均绝对误差 **1.7%**。
换模型后可重新校准：

```powershell
python tools\calibrate_tokens.py
```

它会打印可直接粘贴的系数值（或对应的 `PIPELINE_TOK_*` 环境变量）。

## 目录结构

```text
pipeline/          流水线本体（仅标准库）
  flow.py            流程定义真源（阶段、边、派生表）+ validate() 一致性校验
  orchestrator.py    编排器：单步推进、返工回流、闸门、交付
  config.py          模型矩阵、预算、开关（含环境变量容错读取）
  prompts.py         各阶段系统提示词
  schemas.py         阶段产物 JSON Schema + 轻量校验器
  verify.py          机械验证：物化沙箱 → 计划命令 → 真实执行 → 结论
  budget.py          token 估算、截断、蒸馏
  retrieval.py       存量代码检索（相关度打分 + 片段裁剪）
  patches.py         补丁定位与应用
  gateway.py         入口总闸：规模判定、模块拆分、作业编排
  server.py          本地操作页面（http.server，无外部依赖）
models/            Ollama 模型 tag 的建/校验/启动脚本
tools/             冒烟测试与运维脚本
runs/              运行产物（可随时删；不参与版本管理）
CONTEXT.md         设计与真机教训的完整记录（很详细，改动前建议先读对应章节）
```

## 更多文档

- **`CONTEXT.md`** —— 设计决策、模型矩阵实测数据、以及**每条教训的来龙去脉**。
  改这个项目之前建议先读对应章节：注释里大量「为什么这么写」的结论都源自那里的真机记录。
