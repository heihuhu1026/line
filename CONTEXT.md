# 二次开发需求流水线 — 上下文记录

- 记录时间：2026-09-23（同日第二轮：人工闸门+续跑、操作页面、评审频率、PM 上下文降档，见 §5.6~5.8 与 §11）
- 项目位置：`D:\AI\line`（原 `E:\CODE\project\流水线`，已整体迁移，旧目录只剩空壳且被 IDE 占用）
- 运行环境：Windows / Ollama 0.34.2 / AMD RX 6700 10GB / 32GB RAM / Python 3.12.4
- ⚠️ 本轮把 PM 运行档从 `qwen3-8b-pm-32k` 换成 `qwen3-8b-pm-16k`，**真机跑之前必须先执行一次**：
  `powershell -NoProfile -ExecutionPolicy Bypass -File models\build_tags.ps1`（秒级，幂等）

---

## 0. 一句话概述

给「存量代码的二次开发需求」搭一条多角色串行流水线：产品经理界定范围 → 架构师评估存量代码与兼容约束 → 架构师出最小侵入方案与任务拆解 → 开发实现 → 测试出用例 → 评审判定交付，不合格则回流。全部跑在本机 Ollama 上，**最硬的约束是 10GB 显存，因此强制一次只驻留一个模型**。

---

## 1. 角色链路

```
用户输入二次开发需求
  ↓
[阶段1] 产品经理            需求变更边界界定 + 影响范围说明        → SCOPE
  ↓
[前置]  架构师              存量代码评估 + 兼容约束输出（事实基础） → ASSESSMENT
  ↓
[阶段2] 架构师              最小侵入变更方案 + 任务拆解            → PLAN
  ↓
[阶段3] 开发工程师          遵循存量规范的编码实现                 → IMPLEMENTATION
  ↓
[阶段4] 测试工程师          新功能 + 回归 + 兼容性 三类用例        → TEST_REPORT
  ↓
[阶段5] 评审                交付判定（pass / rework_dev / rework_architect） → REVIEW
  ↓                        ↑______________ 回流（带 required_fixes，上限 2 轮）______________|
结果交付 / 标记 needs_human
```

每个阶段之后都可以插一个**人工闸门**（`--pause-after` / 页面勾选）：暂停落盘 → 人工看产物/改产物/写意见 → 续跑或打回重跑。编排器本身是**可暂停、可续跑**的（见 §5.6）。

---

## 2. 已拍板的决策（用户确认）

| # | 决策 | 落地位置 |
|---|---|---|
| 1 | 架构师阶段用 **方案 A**：换 `qwen3:14b`（判断质量优先），不用 coder-7B | `pipeline/config.py` `STAGE_MODELS` |
| 2 | **测试合并到开发模型**（同一 tag，只换 prompt/温度），不另建量化档 | `STAGE_MODELS["dev"] == ["test"]` |
| 3 | **强制一次只驻留一个模型**（切换前预卸载） | `ollama_client.ensure_exclusive` + `orchestrator._call` |
| 4 | **强制阶段间 JSON Schema 契约**（服务端 `format=schema` + 客户端再校验 + 失败重试） | `pipeline/schemas.py` + `ollama_client.chat_json` |
| 5 | **评审交 14B** | `STAGE_MODELS["review"]` = `qwen3-14b-arch-8k` |

---

## 3. 模型矩阵与实测数据

| 阶段 | Ollama tag | ctx | think | 显存 | prefill | 生成 | 加载 |
|---|---|---|---|---|---|---|---|
| 产品经理 | `qwen3-8b-pm-16k` | 16384 | 开 | 5.83 GB (100% GPU) | ~523 t/s | ~44.9 t/s | 9.1s |
| 架构师（评估+方案） | `qwen3-14b-arch-8k` | 8192 | 开 | 9.02 GB (100% GPU) | ~190 t/s | ~19 t/s | 10.3s |
| 开发 + 测试 | `qwen2.5-coder-7b-dev-24k` | 24576 | 不下发 | 6.25 GB (100% GPU) | ~212 t/s | ~42 t/s | 7.4s |
| 评审 | `qwen3-14b-arch-8k` | 8192 | 开 | 同上 | ~170 t/s | ~17 t/s | 10.0s |

**tag 溯源**（都是复用已有权重，创建只需写 manifest，秒级）：

- `qwen3-8b-pm-16k` ← `FROM qwen3:8b`（Q4_K_M，8.2B，blob `sha256:a3de86cd…`）；旧 tag `qwen3-8b-pm-32k` 已弃用（可 `ollama rm qwen3-8b-pm-32k` 清理，不删也不占额外磁盘）
- `qwen3-14b-arch-8k` ← `FROM qwen3:14b`（Q4_K_M，14.8B，blob `sha256:a8cc1361…`，本次新下载 9.3GB）
- `qwen2.5-coder-7b-dev-24k` ← `FROM qwen2.5-coder:7b-instruct-q6_K`（7.6B）

### 为什么是这些上下文数值（硬件推导，别再重复试错）

- 卡总容量 10224 MiB，桌面/远程虚拟显示占用后**可用 9426 MiB**。
- 14B Q4_K_M 权重 8423 MiB；`num_ctx=8192` 时 KV 680 MiB + 计算 130 MiB = **9233 MiB → 刚好全量**，余量仅 ~190 MiB。
- `num_ctx=16384` 时 ollama 的自动 fit 守不住 1 GiB 保留区，只上 **34/41 层**（20% CPU），生成速度从 27.5 → **12.4 t/s**（直接腰斩）。
- **12K 会溢出**（权重 8423 + KV 约 1020 + 计算 ≈ 9.57 GB > 9.43 GB 可用），所以 8K 是 14B 全量上 GPU 的上限。
- 8B@16K 约 5.9 GB，余量 ~3.5 GB；coder@24K 只需 6.25 GB。这两个 tag **不需要** `num_gpu 999`，自动 fit 就会全量；**只有 14B 必须强制** `PARAMETER num_gpu 999`（否则自动减层）。
- PM 从 32K 降到 16K 的依据：实测 PM 的 prompt 只有 ~300 token，32K 纯浪费。**已真机实测**：16K 档 = 5.83 GB / 100% GPU / load 9.1s（32K 档是 7.04 GB），省下 1.2 GB。
  **代价**：单次能喂的需求文档从 ~12500 token 降到 ~6250 token。要喂长需求时用 `PIPELINE_PM_CTX=32768` 临时调回（请求级 `num_ctx` 会覆盖 tag 默认值，无需重建 tag）。
- 关于量化 KV cache：`OLLAMA_KV_CACHE_TYPE=q8_0` 能把 KV 内存砍半，理论上可能让 14B 上到 12K+（架构师/评审的 8K 是最紧的瓶颈）。未实测，属后续可选优化（需重启 ollama 服务并重新测量）。

---

## 4. 目录结构与职责

```
D:\AI\line\
├─ CONTEXT.md                      ← 本文档
├─ models\                         模型 tag 定义与校验
│  ├─ qwen3-8b-pm-16k.Modelfile    FROM qwen3:8b + num_ctx 16384
│  ├─ qwen3-14b-arch-8k.Modelfile  FROM qwen3:14b + num_ctx 8192 + num_gpu 999（强制全量）
│  ├─ qwen2.5-coder-7b-dev-24k.Modelfile
│  ├─ build_tags.ps1               幂等创建/更新 3 个 tag
│  └─ verify_tags.py               校验 ctx / 100% GPU / 卸载后 /api/ps 为空
├─ pipeline\                       流水线本体（Python 3.12，仅用标准库）
│  ├─ flow.py                      ★流定义单一真源：节点 / 边 / 回流 / 闸门 + 跨表一致性校验 + Mermaid 导出
│  ├─ verify.py                    ★运行验证：把补丁物化到沙箱后**真的跑一遍**（唯一的命令执行点，
│  │                                白名单 + 危险片段拒执行 + 超时 + 环境清洗 + 输出截断）
│  ├─ config.py                    模型矩阵、上下文预算、回流上限、评审频率、token 估算因子（阶段顺序从 flow 派生）
│  ├─ schemas.py                   6 个 JSON Schema（SCOPE/ASSESSMENT/PLAN/IMPLEMENTATION/TEST_REPORT/REVIEW）+ 轻量校验器
│  ├─ budget.py                    token 估算、截断、上游产物蒸馏、fit_prompt
│  ├─ ollama_client.py             chat_json（结构化输出+校验重试）、ensure_exclusive（单驻留）、MockClient
│  ├─ retrieval.py                 存量代码检索分片（中文 n-gram + IDF + 预算）
│  ├─ prompts.py                   各角色系统提示 + 用户消息片段构造（含事实校正、人工意见片段）
│  ├─ orchestrator.py              游标状态机：串行编排 / 事实接地 / 回流循环 / 人工闸门 / 续跑 / 埋点落盘
│  ├─ runstore.py                  run 目录读写约定（state.json、阶段快照、traces、归档、列表、详情）
│  ├─ issues.py                    ★问题记录：分类采集 / 复发追踪 / 指纹 / 跨运行汇总 / 元优化输入
│  ├─ patches.py                   ★锚定补丁的机械校验 / unified diff 落盘 / 套用（默认只写副本）
│  ├─ server.py                    本地操作页面（http.server，仅标准库）
│  ├─ console.html                 操作页面单页前端（原生 JS，无外部依赖）
│  └─ cli.py                       命令行入口
├─ tools\
│  ├─ smoke_mock.py                离线冒烟：契约 + 回流 + 评审频率 + 闸门续跑 + 编辑生效 + 问题记录（不加载模型）
│  ├─ smoke_console.py             操作页面冒烟：起临时端口跑完整人机闭环（--mock，不加载模型）
│  ├─ smoke_ui.mjs                 ★前端交互冒烟（系统 Edge 无头 + CDP，Node 25 自带 WebSocket）：
│  │                                流程图节点 → 阶段详情/产物/日志联动、点击响应耗时；环境不满足时优雅 SKIP
│  ├─ apply_patches.py             套用运行产出的补丁（默认 dry-run；--in-place 才改仓库并留备份）
│  ├─ report_issues.py             跨运行问题总览（人读 md + 机读 json）
│  ├─ meta_optimize.py             ★把问题记录交给模型，产出「流水线自身的改进建议书」（只建议不改代码）
│  ├─ check_retrieval.py           检索预览调参
│  └─ show_run.py                  某次运行的分阶段成本表（含 prefill/gen 吞吐）
└─ runs\                           运行产物（每次一个时间戳目录）
   ├─ 01-pm.json … NN-review.json  每个阶段：meta + artifact + request_preview（preview 截断 4000 字）
   ├─ state.json                   可续跑的编排状态：游标、产物、代码池、埋点、人工意见、人工动作、闸门
   ├─ summary.json                 终态汇总（跑完才写）：verdict、轮次、切换次数、接地告警、问题统计、全部产物
   ├─ handoff.md                   待人工确认清单（未接地路径 / residual_risks / coverage_gaps / unknowns）
   ├─ traces.jsonl                 ★每次调用的完整 system + user + 模型原始输出 + 契约失败原文
   ├─ env.json                     ★当时的环境与「提示词/配置指纹」（跨运行对比的可比性前提）
   ├─ issues.json / .jsonl / .md   ★结构化问题记录（jsonl 给机器、md 给人；派生视图，可随时重算）
   ├─ patches\*.patch              ★可套用的 unified diff（带真实行号，可直接 git apply -p1）
   ├─ applied\                     apply_patches.py 的 dry-run 结果（副本，不动原仓库）
   ├─ llm-calls.jsonl              每次调用的埋点（追加，重跑不清空）
   ├─ console.log                  操作页面启动子进程时的 stdout 日志
   ├─ requirement.txt              需求原文
   ├─ .inbox\                      操作页面新建运行时的需求文本暂存
   ├─ _reports\                    report_issues.py 的输出
   ├─ _meta\                       meta_optimize.py 的输出（输入材料 + 建议书）
   └─ superseded\                  「打回重跑」作废的旧阶段快照（保留审计）
```

---

## 5. 运行机制要点

### 5.1 单驻留调度（决策 3）
每次模型调用前 `ensure_exclusive(tag)`：读 `/api/ps`，把非目标模型全部 `keep_alive=0` 卸载。埋点记录 `switched`。因此全流程只需 **3 次模型切换**（8B → 14B → coder → 14B）。

### 5.2 上下文预算与蒸馏
- `config.STAGE_MODELS[].prompt_token_budget` 是硬预算，且 **system 提示也占预算**（已从预算中扣除）。
- 上游产物注入前一律 `distill_json` 蒸馏；评审阶段最紧（8K），实现产物只保留「文件清单 + 自检 + 偏差」，丢掉代码正文。
- 代码分片：`CODE_BUDGET`（评估/方案 2600、开发 6000、测试 5000、评审 0），`PER_FILE_TOKENS=700`（**分片必须小，否则单个文件吃满整个预算，其他文件进不来**）。这两个常量在 `config.py`（原先在 orchestrator.py，为了让 `issues.py` 能算指纹而搬家）。
- **取片策略（真机教训后重写）**：早期实现是 `text[:per_file_chars]` 只取文件头 —— 对多文件小模块仓库没问题，对 71KB 的单文件项目等于什么都没给（1120 字符里连一个 `def` 都没有）。现在 `retrieval._snippet_for` 的做法是：
  1. **符号锚点**：需求里写出的标识符（`index_all` 这种）是最强信号，取语料里最稀有的那个，定位它在**文件头之外**的首次出现；中文项目里散文注释的中文 n-gram 命中密度天然高于代码，所以不能只按密度挑锚点。
  2. 否则退回「命中密度最高的行」，且只在文件头之后找。
  3. 输出 = 文件头摘要（≤260 字符）+`…（中间省略）…`+ 命中窗口（按行边界，向上 1/3、向下 2/3）。
  4. 每个片段带 `note`（如「第 329-354 行（命中 `index_all`）+ 文件头」），渲染进 prompt 的 FILE 头，便于事后核对模型到底看到了什么。
- **文件排序加权**：`EXT_WEIGHT` 让代码优先于数据/文档（`.json` 0.35、`.md` 0.35、`.txt` 0.3），`_size_factor` 给超大文件降权（>400KB 降到 0.3）。原因是真机上一份 400KB 的 `zh-cn.json` 翻译表凭海量中文命中挤进 top-N，白吃掉 700 token。
- 调用后若实际 prompt token > 0.85×num_ctx → 埋点标 `prompt_over_budget`。

### 5.3 契约强制（决策 4）
`/api/chat` 带 `format=<JSON Schema>`（服务端强约束，已实测可用），返回后客户端 `schemas.validate` 再校验；失败则把错误列表拼进 prompt 重试一次；再失败抛 `OllamaError`。

### 5.4 事实接地（防幻觉，真机逼出来的）
- 检查 `architect_assess.modules[].path`：不在检索到的代码片段里 → 带 `fact_correction_block` 重试一次；仍不接地 → 记入 `summary.grounding_warnings`。
- `architect_plan / dev` 允许提新文件，因此只做告警不拦截。
- `ASSESSMENT.modules` 允许空数组，避免"必须填路径"逼模型编造。

### 5.5 回流循环 + 评审频率（review_every）
`review.verdict`：`pass` 结束；`rework_dev` → 带 `required_fixes + blockers` 回开发；`rework_architect` → 先回架构师方案（**带本轮评审的必改项**，本轮顺手修掉了原来传旧 `fixes` 的 bug）再走开发。超过 `MAX_REWORK_ROUNDS`（默认 2，即最多 3 次迭代）→ 标记 `needs_human` 并保留全部中间产物。

评审很贵（14B 每次 ≈ 10s 加载 + 70~120s 生成 + 一次模型切换），因此引入 **评审频率** `review_every`（默认 2，env `PIPELINE_REVIEW_EVERY`，CLI `--review-every`）：

- **首轮必评审**（早暴露问题）**＋ 末轮必评审**（要拿到真实判定，而不是直接 needs_human）**＋ 中间按 N 轮间隔**。
- `review_every=2`、上限 2（共 3 轮）时的典型路径：评审第 1、3 轮 → 全流程模型切换从 **7 次降到 5 次**，省掉一次评审（≈90s）。
- 代价：返工后「是否已改好」的发现会晚一轮（判定粒度变粗）。想要每轮都评审就设 `1`（等价旧行为）。

### 5.6 人工闸门与续跑（人机协同）
- **闸门**：`--pause-after pm,architect_plan`（或操作页面勾选）。阶段结束 → 落盘 `state.json` 与 `handoff.md` → 进程退出（顺手卸载模型释放显存）。
- **续跑**：`--resume <run_id>`，从 `state.json` 的游标接着跑。游标指向「下一步」，所以续跑不需要重算已完成的阶段；`llm-calls.jsonl` 是追加的，埋点不丢。
- **人工改产物即生效**：直接编辑 `runs/<id>/NN-<stage>.json` 里的 `artifact`（或页面里改），续跑时 `_restore` 用**阶段快照文件覆盖** `state.json` 里的旧值 → 下游 prompt 拿到的是人工版本。
- **打回重跑某阶段**：`--resume <id> --from dev`，会把该阶段及其下游快照归档到 `superseded/`，并回退计数（上游阶段回退则整轮重来；循环内阶段保留轮次但让下一次评审立即生效）。
- **人工意见**：`--feedback "..."` / 页面上的意见框，写进 `state.json.human_feedback[stage]`，在该阶段 prompt 末尾追加【人工审核意见（优先级最高）】片段（埋点记 `human_feedback_used`）。游标停在 `retrieve` 这类非模型步骤时，落点自动取 `architect_assess`。
- **mode 绑定在 run 上**：mock 运行续跑会自动恢复 `MockClient`（不传 `--mock` 也一样），避免 mock 运行误加载真实模型。
- **`--only` 不参与**：单阶段运行不写 `state.json`，不支持续跑与闸门（会明确报错）。

### 5.7 操作页面（pipeline.server）
`python -m pipeline.server --port 8787` → 打开 `http://127.0.0.1:8787/`。仅标准库 `http.server` + 原生 JS 单页，离线可用、无构建、无 CDN。

- 左栏：运行列表（状态/判定/游标/轮次/耗时）+ 新建运行（需求、仓库、闸门勾选、review_every、max_rework、mock）。
- 右栏：概要、阶段时间线（每个阶段可展开：artifact 可编辑保存、输入预览、调用元数据、打回重跑带意见）、`handoff.md`、实时日志（运行中每 2.5s 轮询）。
- 服务端把流水线跑成**子进程**（`python -m pipeline.cli ...`），日志写到 `runs/<id>/console.log`，并强制「同一时刻只允许一个流水线进程」（显存单驻留）。
- 保存 artifact 时做契约校验，不合契约会**告警但允许保存**（人工可能故意简化）。
- 端点：`GET /api/runs`、`GET /api/runs/<id>`、`GET /api/runs/<id>/log`、`POST /api/runs`（新建）、`POST /api/runs/<id>/artifact`、`POST /api/runs/<id>/resume`、`POST /api/runs/<id>/stop`。

### 5.8 handoff.md（待人工确认清单）
每次暂停/结束时自动生成，汇总机器无法定论的事项：未接地路径、`residual_risks`、`blockers`、`required_fixes`、`coverage_gaps`、PM `unknowns`/`clarifying_questions`、架构师 `uncertainties`，并附上人工操作命令（对应原待办 §9.3）。

---

## 6. 实测全流程（run `20260922-234723`，需求：客户列表导出 Excel，仓库=流水线自身）

```
 # stage            tag                       think switch  load_s wall_s prompt pref_t/s   out gen_t/s
 1 pm               qwen3-8b-pm-32k           True  -          7.0   34.1    298 522.8   1094  44.9
 2 architect_assess qwen3-14b-arch-8k         True  Y         10.3   70.8   2062 198.5    921  19.2
 3 architect_plan   qwen3-14b-arch-8k         True  -          0.0   79.5   2249 190.4   1272  19.4
 4 dev              qwen2.5-coder-7b-dev-24k  None  Y          7.5   47.9   4910 213.5    648  42.2
 5 test             同 dev                     None  -          0.0   36.4   4891 213.7    474  41.7
 6 review           qwen3-14b-arch-8k         True  Y         10.3   67.0   2250 190.2    807  18.8
 7 dev              qwen2.5-coder-7b-dev-24k  None  Y          7.3   48.8   5057 210.1    647  41.9
 8 test             同 dev                     None  -          0.0   47.4   4995 212.5    915  42.0
 9 review           qwen3-14b-arch-8k         True  Y         10.0   79.2   2800 172.8    897  17.7
10 dev              qwen2.5-coder-7b-dev-24k  None  Y          7.3   51.6   5049 211.4    770  41.9
11 test             同 dev                     None  -          0.0   47.2   5110 210.0    871  41.9
12 review           qwen3-14b-arch-8k         True  Y         10.0  116.5   2894 169.2   1493  17.1
-------------------------------------------------------------------------------------------------
合计: wall 726.3s(12.1min) | load 69.7s(9.6%) | 切换 7 次 | prompt 42565 tok | output 10809 tok
判定: needs_human（3 轮触顶）
```

结论：
- **回流边真实工作**：第一轮评审给出 `rework_dev` + 3 条可执行 `required_fixes`（补测后端导出接口存在性、明确大结果集阈值、验证字段支持），并带着 fixes 重进开发。
- 单轮约 4 分钟；端到端 12 分钟。
- 7 次切换开销 70s（9.6%），是可优化项。
- `grounding_warnings`：plan/dev 提出了 `frontend/api/customer_service.js` 等路径（本仓库是纯 Python，明显是编造/新文件）——告警生效，待人工确认。
- 本次需求与仓库不匹配（需求讲客户列表页，仓库是流水线本体），所以评审反复要求"确认后端接口"属正常反应。**换真实目标仓库才有参考价值。**

---

## 7. 开发过程中真机暴露并已修掉的问题

1. **中文检索零命中**：原先取"最大中文串"作检索词，永远匹配不到代码（首次跑 prompt 仅 292 token、代码片段为空）→ 改为 2/3/4 字混合 n-gram + IDF 过滤高频词（`阶段`/`流水线` 这类）→ 现在能命中 `prompts.py / orchestrator.py / cli.py`。
2. **预算分配把文件挤光**：单文件切片 1500 token 会吃满 2200 预算，只能进 1 个文件 → 切片降到 700、评估/方案预算提到 2600 → 同预算下进 3 个文件。
3. **14B 无代码时编造路径**：真机产出过 `pipeline/stages/security_audit`（不存在）→ 三重加固（系统提示硬性纪律 + modules 可空 + 事实接地重试与告警）→ 修后评估阶段模块全部为真实文件。
4. **评审过严导致空转**：3 轮都是 `rework_dev`，理由是"需确认后端接口是否存在/阈值取多少"，这些在给定上下文内无法验证 → 给评审加"判定纪律"：只有本轮材料内就能改掉的问题才可投 rework，需环境/人工确认的一律进 `residual_risks`。
   ⚠️ **这条改动仍未复验**（要等真实仓库跑一次全流程）。
5. **`rework_architect` 带着旧 fixes 重跑方案**（本轮代码审查发现）：原循环里 `fixes = review.required_fixes` 的赋值在 `if verdict == "rework_architect": self._stage_plan(requirement, fixes)` **之后**，所以方案重跑拿到的是上一轮的 fixes（首轮时为 `None`）→ 已改为先用本轮 fixes 重跑方案，再进开发。
6. **Windows 原子写竞争**（操作页面冒烟时真机暴露）：`os.replace` 撞上「操作页面正在读同一个 json」会报 `ERROR_ACCESS_DENIED`，直接把流水线子进程打挂（表现为卡在某个 cursor 不动）。`runstore.write_json` 已加 8 次退避重试 + 兜底直接写；`_read_json` 也带重试。
7. **mock 运行续跑会误加载真实模型**（同一轮冒烟暴露）：`--resume` 不传 `--mock` 时 CLI 默认造 `OllamaClient`，把 mock 运行当成真实运行跑了 14B（38s/次）。
   → 运行模式（`mock` / `mock_rework_first`）写进 `state.json`，`Orchestrator.resume` 按 run 的模式自动恢复 `MockClient`。

---

## 8. 常用命令

```powershell
cd D:\AI\line

# 建/更新三个模型 tag（复用现有权重，秒级完成）—— PM 档换成 16K 后必须先跑一次
powershell -File models\build_tags.ps1

# 重启 ollama（带上 OLLAMA_MODELS=D:\AI\Models 等服务参数）
#   ⚠️ 不这样做的话，直接 ollama serve / 托盘冷启动会看到"空的模型列表"
#   ⚠️ 跑真机前建议重启一次：服务连续跑数小时后 prefill 会掉一个数量级（§16.3）
powershell -NoProfile -ExecutionPolicy Bypass -File models\start_ollama.ps1

# 校验 ctx、是否 100% GPU、卸载后是否真的空
python models\verify_tags.py

# 跑前预检（30 秒）：三个档位是否全量在显存、prefill 是否正常 —— 有告警就先别跑
python tools\preflight.py

# 离线冒烟（不加载模型，秒级）：契约 + 回流 + 评审频率 + 闸门续跑 + 人工编辑生效
python tools\smoke_mock.py

# 操作页面冒烟（起临时端口跑完整人机闭环，不占用 GPU）
python tools\smoke_console.py

# 检索调参（看命中哪些文件、占多少 token）
python tools\check_retrieval.py --repo <仓库路径> --requirement "需求文本" --budget 2600

# 全流程（约 4~12 分钟）
python -m pipeline.cli --requirement-file req.md --repo <仓库路径>

# 全流程 + 人工闸门（PM 与方案结束后暂停，等人工确认）
python -m pipeline.cli --requirement-file req.md --repo <仓库路径> --pause-after pm,architect_plan

# 从暂停处继续 / 一路跑到底（清空闸门）/ 只跑一轮就交人工
python -m pipeline.cli --resume 20260923-101010
python -m pipeline.cli --resume 20260923-101010 --no-pause
python -m pipeline.cli --resume 20260923-101010 --max-rework 0

# 打回某阶段重跑，并带上人工意见
python -m pipeline.cli --resume 20260923-101010 --from architect_plan --feedback "不要动 config.py"

# 列出所有运行 / 只跑某几个阶段 / 纯编排演练
python -m pipeline.cli --list
python -m pipeline.cli --requirement-file req.md --repo <仓库路径> --only pm,architect_assess
python -m pipeline.cli --requirement "..." --mock --mock-rework 1

# 图形化操作页面（默认 http://127.0.0.1:8787/，自动开浏览器）
python -m pipeline.server --port 8787

# 问题记录（跨运行汇总 → 用记录优化流水线本身）
python tools\report_issues.py                     # 写 runs\_reports\issues-<时间戳>.md
python tools\report_issues.py --json              # 只打印 JSON
python tools\meta_optimize.py --mock              # 离线演练元优化
python tools\meta_optimize.py                     # 真机：14B 读记录 → 产出建议书（只建议，不改代码）

# 套用某次运行产出的补丁（默认 dry-run，只写 runs\<id>\applied\ 副本；原仓库不动）
python tools\apply_patches.py --run 20260923-011410
python tools\apply_patches.py --run 20260923-011410 --in-place   # 改仓库（先留 *.orig 备份）
# 也可以直接用生成的 diff：git apply -p1 --directory=<仓库> runs\<id>\patches\*.patch

# 回看某次运行的分阶段成本
python tools\show_run.py 20260922-234723
```

可选环境变量：`PIPELINE_MAX_REWORK`（回流上限，默认 2）、`PIPELINE_REVIEW_EVERY`（评审频率，默认 2）、`PIPELINE_PM_CTX`（PM 上下文，默认 16384）、`PIPELINE_TRACE`（完整调用留存，默认 1；置 0 关闭以省磁盘）、`PIPELINE_RUNS_DIR`、`OLLAMA_HOST`、`PIPELINE_TIMEOUT`。

---

## 9. 未完成 / 待决策

1. **评审「判定纪律」仍然不够**（已在真实项目上复验，见 §13）：即便加了纪律提示词，3 轮仍然都是 rework，理由从"确认后端接口是否存在"变成"测试报告要补 SQL 断言"。**结论：光靠提示词管不住，得改机制**——例如让评审必须把 required_fixes 逐条标注「本轮材料内可改 / 需环境确认」，后者自动改判 residual_risks（结构化输出 + 客户端过滤），而不是靠模型自觉。
2. ✅ **成本优化（已做）**：`review_every` 默认 2（首轮/末轮必评审），典型 3 轮路径的模型切换 7→5 次、少一次评审 ≈90s。测试与评审合并这条路暂不做。
3. ✅ **`needs_human` 落地动作（已做）**：自动生成 `handoff.md`《待人工确认清单》+ 操作页面可人工编辑/打回/续跑。
4. ✅ **真实目标仓库（已定）**：`E:\CODE\project\1.18.0 source code`，见 §13。自指测试阶段结束。
5. ✅ **PM 上下文 32K→16K（已做）**：需要先跑一次 `models\build_tags.ps1` 创建新 tag；喂超长需求时用 `PIPELINE_PM_CTX=32768` 临时调回。
6. （可选，收益可能更大）**量化 KV cache**：设 `OLLAMA_KV_CACHE_TYPE=q8_0` 重启 ollama，KV 内存砍半，可能让 14B（架构师/评审）从 8K 上到 12K+，那是当前真正的瓶颈（8K 逼着上游做重蒸馏）。需重启服务并重新测量，未做。
7. ✅ **真实模型下的闸门演练（已做）**：真机上跑过「闸门暂停 → 人工答 PM unknowns → 续跑 → 清空闸门跑完」，见 §13。
8. ✅ **真机跑 `meta_optimize.py`（已做）**：14B 读真实记录产出了带 run_id 证据的建议书，并独立复现了已知的评审空转问题，见 §13.4。
9. ✅ **人工意见的传导（已做）**：注入所有下游阶段（`human_facts_block`）+ 提示词明确「人工已确认的事实不得再当未决项」；顺带修掉"末尾指令被从尾部截断吃掉"的缺陷（`fit_prompt(pin=...)`）。见 §14.1。
10. ✅ **实现粒度（已做，见 §14.1/14.2）**：契约改成**锚定补丁**（符号 + anchor + patch + covers_tasks + not_implemented + 带 evidence 的 self_checks），检索按符号取整个函数体，并加**实现覆盖审计**。同一需求下返工从 3 轮降到 0 轮。
    仍差最后一步：补丁还是片段、评审对片段仍宽容 → 见 §14.3 的 ①②③。

### 新对话的接续建议
1. 先跑 `python models\verify_tags.py`、`python tools\smoke_mock.py`、`python tools\smoke_console.py` 确认环境完好（约 1 分钟，前两个不占 GPU）。
2. 给出真实目标仓库路径，用 `--only pm` 或 `--only architect_assess` 做小步验证。
3. 然后跑一次带闸门的全流程：`--pause-after pm,architect_plan`，用操作页面看产物、改 PM 的范围说明、再续跑。

---

## 10. 环境备忘（踩过的坑，别再踩）

- **Ollama 模型库实际在 `D:\AI\Models`**（`C:\Users\Administrator\.ollama\models` 是空的）。本机已有 `qwen3:1.7b/4b/8b/14b`、`qwen3.5:4b/9b`、`qwen2.5-coder:7b-instruct-q4_K_M/q6_K`、若干 abliterated 与 novel 微调。
- **14B 全量必须写 `PARAMETER num_gpu 999`**；8B/coder 不需要（自动 fit 就会全量）。
- **非思考模型（qwen2.5-coder）不要下发 `think` 参数**，配置里用 `think=None` 表示不下发。
- **结构化输出已实测可用**：`/api/chat` 传 `format=<JSON Schema>`，本机 ollama 支持。
- **PowerShell 调 API 传中文必须用 UTF-8 字节**：`Invoke-RestMethod -Body $json`（字符串）会把中文变成 `?`（表现为模型答"你发的是很多问号"）。正确做法：`[System.Text.Encoding]::UTF8.GetBytes($json)` + `charset=utf-8`。
- **不要在 PowerShell 里用内联 `python -c "..."`**：引号会被吞掉（本次踩过两次）。用 `tools\` 下的脚本。
- **写 `.ps1` 要小心编码**：Windows PowerShell 5.1 默认按 GBK 读 `.ps1`（不看文件是 UTF-8 无 BOM）。中文只出现在**注释**里还能跑（乱码但不报错），一旦出现在**字符串**里就会因为奇数字节的 UTF-8 序列吞掉后面的引号，报 `The string is missing the terminator`。本轮 `build_tags.ps1` 就这么挂过一次，现在改成纯 ASCII 脚本了。要写中文就存成 UTF-8 **带 BOM**。
- **传中文需求用 `--requirement-file`**，别用命令行参数。
- **HF 直连超时，`hf-mirror.com` 秒通**（本次实测：`huggingface.co` API 超时，`hf-mirror.com` 正常）。要 Qwen3-8B 的 Q5_K_M 等非 ollama 官方档位需要从 HF 下 GGUF 再用 Modelfile 导入。
- **ollama 官方库没有 qwen3 8B 的 Q5/Q6 档**（只有 `qwen3:8b`(Q4_K_M) 和 `qwen3:8b-q8_0`）。
- **迁移用 robocopy 时源目录若被 IDE 占用**，文件会迁走但源目录删不掉（报 ERROR 32），属正常，切完工作区手动删即可。
- 别在 `C:\` 盘放大模型：C 盘只剩约 51 GB，D 盘约 238 GB 空闲。
- **Windows 上「原子写 JSON」不是免费的**：`os.replace` 撞上别的进程正在 `open()` 读同一个文件会 `ERROR_ACCESS_DENIED`。任何「一边跑一边被轮询」的 json 都要带重试（见 `runstore.write_json`）。
- **操作页面请在服务进程内清理子进程**：`server.shutdown_jobs()`；手写脚本起服务时记得在 finally 调用，否则会留下跑真实模型的孤儿进程占显存（本轮踩过，用
  `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*pipeline.cli*' }` 查、`Stop-Process -Id <pid> -Force` 清）。

---

## 11. 本轮（2026-09-23 第二轮）改动清单与验证状态

| 改动 | 文件 | 离线验证 |
|---|---|---|
| 编排改为游标状态机 + `state.json` 可续跑 | `pipeline/orchestrator.py` | `tools/smoke_mock.py`（当时 44 项，现 105 项断言全绿） |
| 人工闸门 `--pause-after` / `--resume` / `--from` / `--feedback` / `--no-pause` / `--list` / `--run-id` / `--review-every` | `pipeline/cli.py` | 同上 |
| 人工意见注入（prompt 片段 + 埋点 `human_feedback_used`） | `pipeline/prompts.py`、`pipeline/orchestrator.py` | 同上 |
| 评审频率（首轮/末轮必评审） | `pipeline/config.py`、`pipeline/orchestrator.py` | 同上 |
| run 目录读写约定（原子写+重试、归档、列表、详情） | `pipeline/runstore.py`（新增） | 同上 |
| 操作页面（HTTP API + 单页前端） | `pipeline/server.py`、`pipeline/console.html`（新增） | `tools/smoke_console.py`（28 项断言全绿） |
| 操作页面冒烟 | `tools/smoke_console.py`（新增） | 本身 |
| PM 16K 运行档 | `models/qwen3-8b-pm-16k.Modelfile`、`build_tags.ps1`、`verify_tags.py`、`pipeline/config.py` | ✅ `build_tags.ps1` 已执行；`verify_tags.py` 实测 ctx=16384 / 5.83 GB / 100% GPU / 单驻留成立 |
| `handoff.md` 待人工确认清单 | `pipeline/orchestrator.py` | `tools/smoke_mock.py`（needs_human 用例断言生成） |
| 问题记录（17 类分类 + 复发追踪 + 指纹 + 跨运行汇总） | `pipeline/issues.py`（新增） | `tools/smoke_mock.py`（含「改提示词→指纹变化」断言） |
| 完整调用留存 traces.jsonl / env.json | `pipeline/runstore.py`、`pipeline/ollama_client.py`、`orchestrator._record_trace` | 同上（断言 trace 含完整 system/user/raw） |
| 人工干预留痕 human_actions（含人工自报分类） | `orchestrator._log_action`、`server._append_human_action` | `tools/smoke_console.py` |
| 跨运行问题总览 | `tools/report_issues.py`（新增） | 真机跑通：6 次运行 / 76 条问题 |
| 元优化建议书（只建议不改代码） | `tools/meta_optimize.py`（新增） | `--mock` 跑通（材料 3521 tok / 预算 4466）；真机未跑 |
| 页面：问题记录卡片 / 问题总览 / 人工分类下拉 | `pipeline/console.html`、`server.py` | `tools/smoke_console.py`（含 `node --check` 前端语法） |

**验证方式说明**：本轮闸门/续跑/页面/评审频率的验证都在 `--mock` 下完成（不加载模型、秒级、可重复）；三个 tag 的 ctx/显存/单驻留走的是真机 `models\verify_tags.py`。
真机（14B + coder）**全流程**尚未重跑，因为还缺真实目标仓库。

---

## 12. 问题记录与「用记录优化流水线自身」

### 12.1 为什么需要它（改造前的缺口）
改造前的问题信号是**散**的：`llm-calls.jsonl` 的 `attempt/truncated/prompt_over_budget`、`summary.grounding_warnings`、
`review.required_fixes/blockers/residual_risks`、`test.coverage_gaps`、`state.human_feedback` 五处各存一份。
人能拼出来，但**无法统计、无法判断同一问题是否复发、无法交给模型分析**。更致命的是两个数据缺口：

1. **没有完整输入输出**：`NN-<stage>.json` 的 `request_preview` 被截断到 4000 字，**模型原始输出（含 thinking）完全不存**。
   想让别的模型分析「这段 prompt 哪里不清晰」却没有原始样本对。
2. **没有可比性**：改了 `prompts.py` 之后，问题的增减到底是因为改动，还是因为需求/仓库不同，无法归因。

### 12.2 现在记什么（每次运行自动完成）
| 文件 | 内容 | 用途 |
|---|---|---|
| `traces.jsonl` | 每次调用的完整 `system` + `user` + 模型原始输出 + thinking + **契约失败时的原始输出** + 用量 | 样本对，语义分析素材 |
| `env.json` | Python/平台、仓库、ollama host、**提示词指纹 / 配置指纹 / 合成指纹**、各阶段提示词长度；续跑期间指纹变化也会追加记录 | 跨运行可比性 |
| `issues.jsonl` / `issues.json` / `issues.md` | 结构化问题事件（17 类，带阶段/严重度/来源/证据/复发次数） | 统计与人工复盘 |
| `state.json.human_actions` | 人工干预留痕（闸门暂停/改产物/打回/意见 + 人工自报的问题分类） | 区分「模型的问题」与「人工干预」 |
| `summary.json.issues` | 本次运行的问题统计（总数/分类/严重度/复发/阻断） | 一眼看结果 |

问题分类（`pipeline/issues.KINDS`）：契约违约、prompt 裁剪、prompt 逼近上限、调用过慢；
路径未接地；评审要求返工方案/必改项/阻断项/残留风险；测试未覆盖；需求未决；架构不确定；
人工闸门/改产物/打回/意见；回流触顶。
**复发判定**：同一 `(分类, 阶段, 标题)` 第二次出现即标 `recurred` —— 这正是「上一轮的处理没解决问题」的信号。

### 12.3 两条使用路径
```powershell
# ① 人看：跨运行问题总览
python tools\report_issues.py          # → runs\_reports\issues-<时间戳>.md（含按指纹分组的前后对比表）

# ② 模型看：把记录交给模型做元优化，产出「流水线自身的改进建议书」
python tools\meta_optimize.py --mock   # 离线演练
python tools\meta_optimize.py          # 真机 14B；材料 ~3.5K token（8K 上下文装得下）
```
`meta_optimize.py` 的输出是 `runs\_meta\<时间戳>\`：`input-report.md`（输入材料）、`input-prompt.txt`（完整 prompt）、
`proposal.json`（结构化）、`proposal.md`（人读）。建议书要求每条结论**绑定证据**（问题类型/阶段/次数/run_id）、
**指向具体位置**（如 `prompts.py SYSTEM['review']`）、给出**验证计划**。

> 安全边界（刻意设计）：元优化**只产出建议，不自动修改任何文件**。落地改动由人来做，改完会得到新的
> `pipeline_hash`，下一个运行周期的问题分布就能和旧指纹直接对比 —— 这就是闭环。

### 12.4 界面
操作页面里：阶段卡片下面多了「问题记录」卡片（按分类分组，标严重度与复发次数）；
「问题总览」按钮看跨运行汇总；打回/续跑时可从下拉里选**问题分类**，让统计知道人工为什么介入。
接口：`GET /api/report`（跨运行汇总 + markdown）、`GET /api/kinds`（分类清单）。

### 12.5 还没做的（本轮之前）
1. **没有自动改**：不打算做自动改 prompt —— 已经能给出带证据的建议，自动应用的风险与收益不成比例。
2. **没有成本记账**：只记 token 与耗时，没换算出电费/时间预算。需要的话在 `issues.build_report` 里加聚合即可。
3. **老运行（本轮之前）只有 summary.json**：`build_report` 会用 summary 兜底（`record_source=summary`），
   但拿不到 `schema_errors` 与完整 trace，所以那几次运行的「契约违约」类问题是空的。

---

## 13. 真实项目验证（2026-09-23，第一次用真仓库跑）

**目标仓库**：`E:\CODE\project\1.18.0 source code` —— 3 个单文件 Python 工具（卡片检索器.py 71KB / 世界书生成器.py 49KB / 角色卡编辑器.py 70KB）+ 内嵌 SillyTavern 应用树 + 1.5GB sqlite 索引库，共 21854 个文件。
**需求**：给「卡片检索器」的索引过程加崩溃安全（运行状态表 + 每卡处理结果 + 异常退出自检），要求最小侵入、不改查询语法与 GUI、不引第三方库（需求原文存于 `runs/20260923-003733/requirement.txt`）。
**跑法**：操作页面发起，闸门 `pm,architect_plan` → 人工审 PM 产物并把 PM 的 4 个 unknowns 用**存量事实**答复（`--feedback` 注入架构师评估）→ 清空闸门跑完。

### 13.1 真实数据（run 20260923-003733）
| # | 阶段 | tag | wall | prompt | out | 备注 |
|---|---|---|---|---|---|---|
| 1 | pm | qwen3-8b-pm-16k | 35.1s | 693 | 1061 | 闸门暂停 |
| 2 | architect_assess | qwen3-14b-arch-8k | 92.7s | 3043 | 1100 | 含人工意见 |
| 3 | architect_plan | qwen3-14b-arch-8k | 107.4s | 2938 | 1570 | 闸门暂停 |
| 4-6 | dev/test/review | coder / 14B | 53.9 / 41.6 / 95.0s | 5731 / 5390 / 2875 | 541 / 462 / 1170 | 第 1 轮 rework |
| 7-8 | dev/test | coder | 51.7 / 44.7s | 5874 / 5426 | 476 / 638 | 第 2 轮**跳过评审**（review_every=2 生效） |
| 9-11 | dev/test/review | coder / 14B | 22.0 / 36.5 / 114.1s | 5930 / 5577 / 3139 | 746 / 534 / 1440 | 第 3 轮触顶 |

11 次调用合计 694.6s（11.6min）；端到端 wall **910.9s（15.2min）**，差额是模型加载/切换开销与三段之间的等待。
模型切换 5 次（跳过第 2 轮评审省了 1 次评审 + 2 次切换），
`grounding_warnings` **0 条**，`contract_violation` 1 次（dev 第 3 次调用重试 2 次后通过），问题记录 32 条（阻断 5），判定 `needs_human`。

### 13.2 结论：结构层跑通了
- **人工闸门 + 续跑 + 人工意见注入**在真机上按设计工作（PM 产物可审、可答复 unknowns、可继续）。
- **事实接地有效**：评估阶段引用的路径全部真实（`卡片检索器.py` / `启动卡片检索器.bat`），没有编造。
- **评审频率生效**：第 2 轮明确跳过评审。
- **契约强制生效**：dev 有一次输出不合 schema，重试后通过，并被记成 `contract_violation` 问题。
- **问题记录/指纹/traces 齐全**：`issues.md` 32 条、`traces.jsonl` 11 条完整样本、跨运行总览里这次运行与历史运行按指纹分列。

### 13.3 真实项目暴露的问题（这才是这次测试的价值）
| # | 问题 | 状态 |
|---|---|---|
| 1 | **取片只取文件头**：71KB 单文件项目里，给模型的 1120 字符中连一个 `def` 都没有（`index_all` 在第 335 行） | ✅ 已修：符号锚点 + 命中窗口（`retrieval._snippet_for`） |
| 2 | **非代码文件挤占代码预算**：`SillyTavern/public/locales/zh-cn.json`（400KB 翻译表）凭海量中文命中进 top-N，白吃 ~700 token | ✅ 已修：`EXT_WEIGHT` + `_size_factor`；修完 top-4 全是项目文件 |
| 3 | **人工意见只进目标阶段，评审看不到**：我在评估阶段答复了"库里没有 run 表"，评审仍把同一问题当 `residual_risks` 提出；方案也没遵守"加列式迁移、不要重建表"的人工约束（反而新建 `run_log`/`card_status` 表） | ❌ 未修（见 §9.9） |
| 4 | **大单文件项目的实现输出严重缩水**：dev 三次都只给 1001~1441 字符的"示意代码"，却在 `self_checks` 里声称 5 个任务全部完成；契约写的是「该文件的最终内容或可直接应用的补丁」，对 2000 行文件根本不可能满足；评审无法验证 → 空转到触顶 | ❌ 未修（见 §9.10，是当前最大的质量瓶颈） |

一句话：**瓶颈已经从「检索不到」转移到「实现粒度 + 评审纪律」**。

### 13.4 元优化真机运行（第一次用真实记录喂模型）
`python tools\meta_optimize.py`（14B，材料 4139 token / 预算 4435）产出的建议书里，
模型**独立地**重新发现了我们已知的「评审把需外部确认的事项投成 rework」问题，并给出了指向
`pipeline/prompts.py SYSTEM['review']` 的具体改法，每条都带 run_id 证据。产物见 `runs/_meta/`。
两次真机运行对比也证明了两件事：① 材料里加入「被改造仓库」信息前，模型会把目标仓库的文件名
当成流水线的文件（已修：材料头部明确列出被改造仓库 + 要求 target 指向 `pipeline/`）；
② `risk`/`rationale` 字段原本常被模型留空，已改成 schema 必填，填得住了。

### 13.5 复现这次测试（第一轮，旧契约）
```powershell
# 需求原文（agent 依据代码结构写的，不含任何业务数据内容）
# 仓库：E:\CODE\project\1.18.0 source code
python tools\check_retrieval.py --repo "E:\CODE\project\1.18.0 source code" --requirement-file req.md --budget 2600 --per-file 700
python -m pipeline.cli --requirement-file req.md --repo "E:\CODE\project\1.18.0 source code" --pause-after pm,architect_plan
python -m pipeline.server --port 8787   # 用自己的眼睛看产物、改产物、打回或继续
python tools\report_issues.py && python tools\meta_optimize.py
```

---

## 14. 「让交付能真正被采纳」这一轮（2026-09-23 第三轮）

针对 §13.3 暴露的问题 3/4，把「靠提示词叮嘱」换成「靠机制」，并修掉一个自己引入的截断缺陷。

### 14.1 改了哪些机制
| 原来 | 现在 | 位置 |
|---|---|---|
| `edits[].code` = 整个文件最终内容（71KB 单文件不可能满足） | **锚定补丁**：一次 edit = 一个符号 `target_symbol` + 唯一 `anchor`（原文 1~3 行）+ `patch`（diff 或该符号替换后的完整块） + `covers_tasks`；没做的必须写 `not_implemented`；`self_checks` 每条必须带 `evidence` | `schemas.IMPLEMENTATION`、`prompts.SYSTEM['dev']` |
| 检索只喂文件头/固定窗口（喂不进目标函数体） | **按符号取整个函数块**（`_block_range`）；池子单文件上限 2600 token，各阶段再按 `STAGE_PER_FILE_TOKENS` 二次裁剪（dev 2600 / 架构师 900） | `retrieval`、`config` |
| 「谎报完成」无防线 | **实现覆盖审计**（确定性）：`plan.tasks[].id` × `edits[].covers_tasks` 机械核对；未覆盖、编造 id、未实现项都记成问题，并把审计结论 **pin 进评审 prompt** | `orchestrator._audit_implementation` |
| 评审靠"判定纪律"自觉，仍空转到触顶 | 返工项必须声明 `scope`：`in_material` 才触发返工，`needs_external` 由编排器**自动改判**进 `residual_risks`；**全是 external 时强行 pass 并留痕**（记 `review_forced_pass`，handoff 里提示复核） | `schemas.REVIEW`、`orchestrator._normalize_review` |
| 人工意见只进指定阶段，评审看不到 | `human_facts_block` 注入**所有下游阶段** + 提示词明确「人工已确认的事实不得再当未决项」 | `prompts`、`orchestrator._human_facts` |
| 追加在末尾的指令会被"从尾部截断"吃掉 | `fit_prompt(..., pin=[...])`：人工意见、人工事实、覆盖审计**永不因超预算被丢**（真机发现评审阶段把整块事实截掉了，导致评审又去质疑"表结构未知"） | `budget.fit_prompt` |
| 环境退化被误当成"模型慢" | 每次调用后读 `/api/ps` 记 `vram_ratio` + `prefill_tps`/`gen_tps`；<99% 记 `gpu_partial_offload` 阻断项；报告里加各阶段吞吐 | `orchestrator._gpu_stats`、`issues` |

### 14.2 A/B（同一仓库、同一需求、同一份人工意见）
| 指标 | 旧契约 `20260923-003733` | 新契约 `20260923-011410` |
|---|---|---|
| 迭代轮次 | 3 轮 → **needs_human（触顶）** | **1 轮 → pass** |
| dev 产物 | 1 个 edit / 1001~1441 字符 / 没有任务对应关系 | **9 个符号级 edit**，每条带 `covers_tasks`（覆盖审计通过）、`not_implemented=[]` |
| 评审 | 3 次 rework_dev（"补 SQL 断言"），阻断 5 条 | 1 次 pass，**阻断 0 条** |
| 问题记录 | 32 条（阻断 5） | 24 条（阻断 0） |
| 端到端 | 910.9s | ~1032s（**但 14B 被环境拖慢了 3 倍**，见 §14.4） |

抽取的答案（同一需求）：**同一台机器、同一个模型，只改机制，返工从 3 轮降到 0 轮。**

### 14.2b 真机验证「人工事实传导 + pin」（同一 run 只重跑评审，`--from review`）
| | 修复前的那次评审 | 只重跑评审（pin 生效后） |
|---|---|---|
| 评审 prompt 是否含人工事实 | ❌ 整块被截断（prompt 6615 字符 / 3515 token，末尾事实块被丢） | ✅ 含（prompt 6891 字符，`cards(id …` 与覆盖审计都在，`truncated=True` 但被截的是中间的非 pin 片段） |
| 残留风险 | "数据库表结构未知可能导致迁移脚本需调整" ←**人工已经答过** | "index_all 批量处理逻辑未在测试报告中明确验证"、"状态栏提示实现方式未验证" ←**真的缺口** |
| 判定 | pass | pass |

结论：**人工已确认的事实现在真的会被采纳**，评审不再把已答过的问题当未决项（对应 §13.3 的问题 3 已闭环）。
另外注意：评审阶段的 prompt 已经**贴着 8K 上限**（`prompt_truncated` 仍会出现），要再塞东西就得动 KV 量化（§3 的 `OLLAMA_KV_CACHE_TYPE`）或进一步精简蒸馏。

### 14.3 仍未解决（下一步优先级）
> 第 1、2 条已在 §15 落地（补丁落盘 + 套用 + 机械校验 + 判定被推翻）；下面保留当时的分析，便于回看。
1. **补丁还是"片段"**：9 条补丁 130~587 字符，不是契约里说的"该符号替换后的完整代码块"，`anchor` 9 条全都指向同一个 `index_all` 签名。要真做到"可直接应用"，应继续做：
   ① 把补丁落盘成 `.patch` 文件并在页面上展示/下载；② 提供 `--apply`（用 anchor 做模糊定位，diff 或整块替换）；③ 评审核对"patch 能否贴回 anchor"。
2. **评审对"片段补丁"仍然宽容**（给了 pass）。提示词规则没触发 → 应改成**机械校验**：在编排器里做 anchor 匹配（在原文里找 anchor；找不到就记 issue 并 pin 进评审 prompt）。
3. 桌面/远程桌面占显存无法由流水线解决，只能提前告警（已做 `gpu_partial_offload`）；也可以考虑给它加一个"启动前预检"命令（把三个 tag 用 1 token 试载一遍，~30s）。

### 14.4 环境告警（重要，别再误判成"模型变傻"）
本轮实测：**没有任何模型驻留时**，显卡 Dedicated VRAM 已被桌面/IDE/远程桌面占走 **6.76GB**（Shared 1.06GB）；此时 14B（9.02GB）被 WDDM 换到共享内存，`prompt_eval` 从 **157 t/s 掉到 17 t/s（10 倍）**，而 ollama 的 `/api/ps` 仍然报 `size_vram == size`、**"100% GPU"是假象**。
8B/coder 体积小（5.3/5.9GB）受影响较小（201/276 t/s），所以只有 14B 阶段会突然变成"每次 5 分钟"。
→ 真机跑之前先关掉占显存的程序；跑完看 `issues.md` 里有没有 `gpu_partial_offload` 或连片 `slow_call`。

---

## 15. 「让补丁真的能用」这一轮（2026-09-23 第四轮）

针对 §14.3 的第 1、2 条：把「补丁能不能贴回去」从"评审自己去猜"改成**机械可判定**，并给交付一条真正的落地路径。

### 15.1 新增机制
| 机制 | 说明 | 位置 |
|---|---|---|
| **补丁语义声明** `patch_mode` | 每条补丁必须声明：`insert_after`（插到 anchor 之后）/ `replace_span`（替换 anchor 覆盖的行）/ `full_symbol`（该符号的完整替代） | `schemas.IMPLEMENTATION`、`prompts.SYSTEM['dev']` |
| **补丁机械校验** | 逐条核对：anchor 能否在原文唯一定位；`full_symbol` 是否真的定义了该符号、行数够不够（<30% 判为片段）；`replace_span` 的 anchor 是否覆盖了整个符号（否则贴回去会留残码）；patch 是否与原文相同（等于没改）；要新增的符号是否已存在 | `pipeline/patches.py` |
| **问题分类** | `anchor_not_found` / `anchor_ambiguous` / `patch_incomplete` / `patch_span_mismatch` / `symbol_already_exists` / `patch_symbol_missing` / `patch_no_effect` / `patch_already_applied`（前四类为阻断级或重要项） | `pipeline/issues.py` |
| **判定被机制推翻** | 补丁有阻断级问题时，**评审给 pass 也会被改判为 rework_dev**，返工项由审计自动生成（「修复补丁问题：xxx」）。**无论模型有没有给返工明细都会执行** —— 早期版本在"明细为空"时提前 return，等于留了"返回空明细即可绕过机制"的口子 | `orchestrator._patch_blockers` / `_normalize_review` |
| **补丁落盘** | `runs/<id>/patches/NN-<symbol>.patch`，带真实行号的 unified diff，可直接 `git apply -p1`；新增文件用 `--- /dev/null` | `patches.write_patch_files` |
| **套用工具** | `tools/apply_patches.py --run <id>`：默认 **dry-run**（只写 `runs/<id>/applied/` 副本），`--in-place` 才改仓库并留 `*.orig` 备份；非 in_place 时**强制要求 out_dir**（防止误写原仓库） | `tools/apply_patches.py` |
| **页面展示** | 新增「补丁机械校验与落盘」卡片：逐条状态/语义/行号/问题理由 + 补丁文件可点开查看；接口 `GET /api/runs/<id>/patch?file=...` | `console.html`、`server.py` |
| **跑前预检** | `tools/preflight.py`：30 秒弄清楚"是不是环境问题"——三个档位逐个 1-token 探针 + 读 `/api/ps` 的 `size_vram/size` + 测 prefill，低于基线一半或有档位没全量上显存就告警并给出结论（对应 §14.4） | `tools/preflight.py` |

### 15.2 真机数据（run 20260923-011410，重跑 dev 后）
新契约下 dev 产出 **10 条补丁**，机械校验：**9 条可套用 / 1 条阻断**。被抓住的正是真问题：

```
[patch_span_mismatch] index_all  mode=replace_span  lines=24  span=[335, 336]
  └ patch 里给的是 `index_all` 的完整定义，但 anchor 只覆盖第 335-336 行
    （`index_all` 在原文有第 335-437 行，共 103 行）：替换后会留下原函数体，
    必须改用 full_symbol，或把整个函数作为 anchor
```
→ 上轮那个"587 字符就敢声称改完 `index_all`"的问题，现在会被机械抓出来，且**不允许 pass**。
另外 3 条带提示"patch 里没有该符号的定义行"——它们是纯插入片段，属可接受。

### 15.2b 真机链条（同一条 run 重跑后）
```
--from review（补丁校验已 pin 进评审）：
  评审 493s → **rework_dev**，2 条 in_material 返工项（上一轮它给的是 pass）
  第 2 轮：跳过评审（review_every=2）
  第 3 轮：评审 → rework_dev，4 条 in_material 返工项 → attempt 3 > 上限 2 → needs_human
最终：status=done attempts=3 needs_human=True，wall 2901s（14B 被环境拖慢，见 §14.4）
```
返工项已经是**可执行、可核对**的（"实现 index_run 表创建逻辑和 cards 表字段扩展"/"补 process_status='success' 跳过逻辑"/"补搜索回归的基准 SQL"），
不再是上几轮那种"确认接口是否存在"式的空转 —— 对应 §13.3 的问题 4 也闭环了。

### 15.2c 另一个真机发现：dev 会"退缩成零实现"
第 3 轮的 dev 只给了 1 条 `patch` = `# TODO` 的补丁，并把 4 个任务**全部**写进 `not_implemented`。
按"诚实优先"的设计，覆盖审计（missing 为空）和补丁校验（1 条 ok）**都会放它过** —— 等于什么都没干却全绿。
这是被评审压紧之后的真实退化行为，已堵：
- `implementation_audit` 增加 `empty_implementation`：没有任何"≥40 字符且覆盖非未实现任务"的补丁时判为**零实现**；
- 零实现进阻断级 → **不允许 pass**（`_patch_blockers`）→ 记 `implementation_empty` 问题 → 写进评审 prompt 与 handoff。

### 15.3 顺带修的真 bug（测试逼出来的）
`retrieval.select_excerpts` 在「需求与仓库没有任何共同词」时会 **`ZeroDivisionError` 直接崩**（中文需求 + 纯英文仓库、或指向一个不相关的小仓库就会触发）：
兜底词表用 `sorted(terms, key=df.get)` 取"出现次数最少"的词，其中包含 `df==0` 的词，随后算 IDF 时除零。
已修：兜底也过滤 `df>0`，全为 0 时直接返回空片段（让下游明确知道"没有代码依据"）；IDF 再加 `max(df,1)` 兜底。

第二个：**续跑时 `--max-rework` 被静默忽略**。`Orchestrator.resume` 先 `_restore(snap)`，state 里的 `max_rework` 把命令行传的值盖掉了
（`review_every`/`pause_after` 有显式覆盖逻辑，`max_rework` 漏了）。真机上表现为"传了 `--max-rework 0` 却仍然跑满 3 轮"。
已修：CLI 的 `--max-rework` 默认改为 `None`（不传 = 沿用该 run 的上限），显式传了才覆盖并打日志。

### 15.5 验证（本轮之前）
- `tools/smoke_mock.py` **133 项断言**（新增：补丁校验三态、补丁落盘内容、dry-run 不动原仓库、强制返工、零实现阻断、续跑参数覆盖、pin 不被截断）
- `tools/smoke_console.py` **43 项断言**（含 `node --check` 前端语法）
- 真机：§15.2 / §15.2b / §15.2c 三段都是在真实仓库 + 真实模型上跑出来的，不是 mock。

### 15.6 还没做的
1. **没在真实仓库上跑过 `--in-place`**：套用只在副本上验证过（副本与原始内容逐字比对，确认原仓库未被改动）。第一次对真实仓库动手前建议先 `git diff` 或先 dry-run 看一遍。
2. 补丁仍是"符号级片段"而非完整文件：这是有意的（大文件不可能整份改写），但**合入后仍需人工 review 上下文的连贯性**。
3. 评审 prompt 已贴 8K 上限（`prompt_truncated` 常驻）；要再塞内容得先做 KV 量化（§9.6）。

---

## 16. 新契约下的第一次完整真机流程（2026-09-23 第五轮）

**同一仓库、同一需求、同一份人工意见**，端到端一次跑完：run `20260923-065850`。

### 16.1 结果：首轮 pass，0 阻断

| 阶段 | wall | prefill | gen | 说明 |
|---|---|---|---|---|
| pm | 36.4s | 533 t/s | 42.7 | 闸门暂停，人工答复 4 条 unknowns |
| architect_assess | 303.5s | **18.0 t/s** | 7.7 | 带人工事实；此时 ollama 已退化（见 §16.3） |
| architect_plan | 707.8s | **18.1 t/s** | 6.1 | 同上，11.8 分钟纯属环境问题 |
| dev | 62.0s | 213.1 t/s | 40.8 | **重启 ollama 之后**，恢复正常 |
| test | 40.8s | 205.8 t/s | 40.6 | |
| review | 87.4s | 141.0 t/s | 17.9 | **判定 pass** |

- `attempt=1`、`needs_human=false`、`wall=1293.9s`（其中 17 分钟是那两次退化的 14B 调用）。
- 问题记录 20 条、**阻断 0 条**：pm_unknown 8 / arch_uncertainty 3 / human_gate 2 / review_residual_risk 2 / slow_call 2 / test_gap 2 / prompt_truncated 1。
- **实现覆盖审计**：`covered=TC1,TC2,TC3,TC4`、`missing=∅`、`empty_implementation=false`、`real_edits=5`。
- **补丁机械校验**：5 条全部 `ok`，落盘 `patches/01..05-*.patch`。
- **评审的两条残留风险正是"该交给人的"**：SQLite 索引策略对新字段查询性能的影响、run 表与 cards 表的一致性约束（外键）。
  上一轮同类事项会把评审逼成 3 轮 rework；现在它按 scope 自动进了 residual_risks（§14.1 的机制生效）。

### 16.2 仍然不足（诚实记录）
dev 给的是 **5 条小补丁（161~409 字符）**，全部 `insert_after` 挂在 `index_all` 签名之后：即"加几个辅助函数"，
而不是真正重写 `index_all` 的循环体。机械校验抓不到这种"语义上不够深"，评审也没有判 rework。
根因是 7B coder + dev 阶段单文件 2600 token 的素材量，不足以让它重写一个 100 行的函数体。
可选方向：① dev 单文件上限 2600 → 4000（CODE_BUDGET 6000 塞得下）；② 两遍开发（先插辅助函数，再带"新符号已存在"回填 `index_all`）；
③ 换更大的 coder（显存不够，暂缓）。

### 16.3 本轮最重要的环境发现：ollama 服务跑久了会退化
实测证据（同一台机器、同一个 14B tag、同量级 prompt）：

| 时间 | 状态 | prefill | gen |
|---|---|---|---|
| 00:33（服务已运行数小时） | 正常 | 157 t/s | 20 t/s |
| 01:14 之后 | 退化 | 18 t/s | 7.7 t/s |
| 07:14 本轮 assess/plan | 退化 | 18 → 6.3 t/s | 6~7 t/s |
| 07:2x **重启 ollama 后**（2.5K prompt） | 恢复 | 127 t/s | — |
| 07:3x 重启后 PM / coder / review | 恢复 | 533 / 213 / 141 t/s | 42 / 41 / 18 t/s |

- **`/api/ps` 全程报 `size_vram == size`（"100% GPU"），`vram_ratio=1.0` —— 这个指标看不出退化**。
- 退化时 `decode` 还算正常（20 t/s），**`prefill` 掉 10~30 倍**；表现为"评估/方案一次要 5~12 分钟"，而重启后开发/测试/评审秒回。
- 处置：**重启 ollama 即恢复**。已固化三件套：
  - `models/start_ollama.ps1`（杀进程 → 设好 `OLLAMA_MODELS` 等服务参数 → 起 `ollama serve` → 打印可见 tag → 提示跑 preflight）；
  - `tools/preflight.py` 30 秒体检（显存是否全量 + 探针吞吐），跑真机前先跑；
  - 流水线把这种退化记成 **`prefill_degraded`（阻断级）**，附"重启服务 + 跑 preflight"的处置建议（低于该 tag 基线 30% 触发；基线在 `config.BASELINE_PREFILL`，与 preflight 共用一份）。

> ⚠️ **重启陷阱（务必知道）**：模型权重在 `D:\AI\Models`（57GB），而 `OLLAMA_MODELS` **既没写进用户/系统环境变量，启动快捷方式也没带**。
> 直接 `ollama serve` 或用托盘图标冷启动，会看到一个**空的模型列表**（本轮踩过：`ollama list` 全空、`/api/chat` 全 404）。
> 恢复：`powershell -NoProfile -ExecutionPolicy Bypass -File models\start_ollama.ps1`；想一劳永逸就执行一次
> `setx OLLAMA_MODELS D:\AI\Models`（然后重新登录）。

### 16.4 验证
- `tools/smoke_mock.py` **136 项断言**（新增吞吐退化检测三态：退化告警 / 报 100% 显存时不误报 / 健康不误报）
- `tools/smoke_console.py` **43 项断言**
- 真机：本轮 6 次调用全部走完，产物见 `runs/20260923-065850/`（含 `patches/`、`traces.jsonl`、`issues.md`、`env.json`）

---

## 附：历次真机运行对照

| run | 契约 | 结果 | 关键区别 |
|---|---|---|---|
| `20260922-234723` | 旧（edits[].code = 整文件） | 3 轮触顶 needs_human | 评审空转（"确认接口是否存在"） |
| `20260923-003733` | 旧 | 3 轮触顶 needs_human | dev 只给 1001~1441 字符示意代码；检索只喂文件头 |
| `20260923-011410` | 新（锚定补丁 + 覆盖审计 + 评审分流 + 人工事实） | 先首轮 pass，补丁校验上线后同一条评审改判 rework_dev | 检索按符号取整个函数体；补丁校验 pin 进评审 |
| `20260923-065850` | 新 + 补丁落盘/套用 + 全量机制 | **首轮 pass，0 阻断，5/5 补丁可套用** | 尚存"实现偏浅"（§16.2） |
### 16.5 两遍开发实验（治「实现偏浅」）—— 结论：结构可行，但 7B 填不进逻辑

用户选「两遍开发」治理 §16.2 偏浅。已实现并真机验证（见 `runs/twopass4-20260923`、`runs/twopass5-20260923`）：

- 机制：`config.DEV_TWO_PASS=True` 开启；`prompts.parts_dev` 按 `dev_pass=2/3` 输出两遍任务（第一遍只铺辅助函数、第二遍带第一遍产物回填主函数体）；`orchestrator._stage_dev` 调两次 dev 并由 `_merge_dev` 合并（edits 按 `(path,target_symbol,patch_mode)` 去重、后写覆盖）。
- 真机暴露并修复两个问题：① 两遍拆分后模型丢失 anchor 纪律（自造 `def index_all(path):` 而非真实签名）→ 全 `anchor_not_found`；修复＝dev 系统提示强化「逐字抄完整 def 首行」＋两遍统一用源码真实存在的 `INDEX_ALL_ANCHOR` 作锚点＋pass2 严令只输出 index_all 一个 edit、禁重复定义辅助函数；② 合并把 pass1 坏锚点 edit 与 pass2 重定义混入 → 去重键加入 `patch_mode`。锚点问题已解决：twopass4/5 的 `anchor 在原文里找不到` 从 5 降到 0，补丁类问题归零。

**根因不在结构，在 7B 容量**：twopass4、twopass5 的 `_index_batch/_split_key/_normalize_entry` 全是 `# 逻辑` + `pass` 空壳，index_all 仅 39~44 行骨架且调用空壳；即便显式禁令 `pass`/`# 逻辑` 占位，7B 仍写空壳——填不出原 103 行函数的真实逻辑。评审据此判 rework_dev，两轮后 `needs_human=True`。

反观**单遍基线 `20260923-065850` 首轮 pass、0 阻断**：它自然选了 7B 能完成的「加几个小而独立辅助函数（schema/SQL 检查）」，**绕开了大循环重写**。即：

> 「实现偏浅」是 **7B coder 的容量天花板**，非流水线结构问题。两遍把重构形状摆对，但 7B 无能力填逻辑；单遍能过，恰恰因它没碰那个 103 行大循环。

**结论/建议（待拍板）**：两遍代码保留（开关默认 True），机制正确，锚点修复对单遍也有益（已留在 `SYSTEM["dev"]`）。真要更深实现须换**更大 coder 模型**（如 14B coder，dev 单文件 2600 token 足以看到完整函数）；7B 上两遍反劣于单遍（出空壳→打回）。短期可选：(a) 关 `DEV_TWO_PASS` 回单遍（已知可过、偏浅但诚实）；(b) 改两遍策略为「加小而可完成辅助函数 + 在 index_all 小插入调用」，而非 full_symbol 重写大循环。

| `twopass5-20260923` | 新 + 两遍开发(禁空壳) | needs_human（评审两轮打回） | 锚点修复有效；但 7B 出空壳，偏浅是容量问题 |

### 16.5.1 pass3 分片重构改造（2026-09-23 后续，对应建议里的分片重构）

针对 §16.5 结论 (b)「不要 full_symbol 重写 103 行大循环，改为小插入」，把 pass3 从强制单 edit full_symbol 改成分片重构：

- prompts.parts_dev 的 dev_pass==3 分支（prompts.py）：去掉「本遍只允许输出 index_all 一个 edit（full_symbol）」硬约束；改为「按逻辑分支拆成 2~3 个独立小编辑、每片 ≤40 行，每段用 replace_span 锚定在该分支内部唯一的一行，把那段原逻辑替换为调用第一遍辅助函数 + 胶水代码」。保留锚点纪律（严禁补丁里重写 def index_all 定义行，否则 patch_span_mismatch 打回）、完整性约束（原 103 行每个行为都要安顿）、严禁空壳。
- 配套修复 orchestrator._merge_dev 去重键（orchestrator.py）：由 (path, target_symbol, patch_mode) 改为 (path, target_symbol, patch_mode, anchor)。否则 pass3 对同一个 index_all 出的多条 replace_span（锚点不同）会被合并成 1 条、其余分片丢失，主函数重组不完整。同 (…, anchor) 的后写仍覆盖先写（去重语义不变）。
- 适配性：apply_all 本就按路径内 anchor 行号从后往前套用多条 block 编辑，分片 replace_span 天然适配；patches.py 的 patch_span_mismatch 正好拦住「分片补丁误带 def index_all」的错误写法。
- 测试：tools/smoke_mock.py 新增 merge-sharded 用例——构造 pass3 出 3 条同符号不同 anchor 的 replace_span，断言合并后仍为 5 条（2 helper + 3 分片）、3 个分片 anchor 全保留；并回归「同锚点后写覆盖先写」。
### 16.6 交付前人工审核闸门（human_review）

新增需求：测试通过、正式收尾/归档交付之前，加一道**人工审核闸门**。

**机制**：
- 阶段流序 `runstore.FLOW_ORDER` 在 `review` 之后加入 `human_review`（config.FULL_STAGE_ORDER 同步）。这是一个**非模型阶段**——不调 LLM，只是暂停等人工在控制台核对后提交 verdict。
- 开关 `config.HUMAN_REVIEW_GATE`（env `PIPELINE_HUMAN_REVIEW`，默认开）。设为 `0/false` 可整个跳过闸门。
- 流程衔接：review 判定 pass 时，`_step_review` 返回 `human_review`（开关关则返回 `done`）。`_execute` 在 `_step` 后若 `status=="paused"` 立即停。
- `_step_human_review`：
  - 首次到达：写占位产物 `NN-human_review.json`（`_record`），置 `status=paused`、`paused_after=human_review`，停等人工。
  - 人工提交后（resume）再次到达：读 `state["human_review"]` 的 `verdict`：
    - `approve` → 返回 `done`，正常收尾交付；
    - `reject` → 把 `notes` 以 `[人工审核打回] …` 注入 `human_feedback["dev"]`，清除内存中的 human_review 产物，返回 `_begin_round("dev")` 回流到开发重跑（之后再次回到本闸门）。
- 人工核对 4 项（与需求逐字对应）：①核心业务路径走通、主流程通顺；②无明显低级错误/逻辑硬伤；③交付物完整（代码/文档/说明齐全）；④对照最初需求核心诉求已满足。schema `HUMAN_REVIEW` 仅强校验 `verdict`，4 项布尔可选。
- 控制台：console.html 新增 `d-hrev-card` 面板（4 个勾选 + 通过/打回单选 + 备注 + 审核人），`submitHumanReview()` 写产物并 resume；`STAGES`/`STAGE_CN` 已含 `human_review`。

**顺带修掉的隐患**：`_normalize_review` 会原地改 `state["review"].verdict`（如强制 pass），但 `_call` 内部记录的 NN-review.json 是**原始**输出；续跑时 `latest_artifacts` 会用原始评审覆盖 state，把 verdict 回退成 `rework_dev`。人工审核闸门（"评审后再续跑"）让此问题暴露。修复：`_step_review` 归一化后 `runstore.save_artifact(run_dir,"review",...)` 把归一化评审回写 NN 文件。

**测试**：mock 冒烟新增 `human-review-gate` 用例（暂停→reject 回流→二次 approve 放行，且打回意见进入 dev prompt）；全量用例用 `settle()` 在末尾自动通过闸门。console 冒烟用 `wait_done_with_gate()` 自动通过闸门并校验阶段数（8）与归档数（5）。mock 142 + console 43 全绿。

**设计取舍**：闸门放在 `review` 之后、交付之前，即「AI 评审通过后的最终放行关」。若希望严格「测试一通过就卡人工」（不管 AI 评审是否还在返工），可把 `human_review` 移到 `test` 之后——但那样 AI 评审的返工会让人工审核结果失真，故默认放在最后。

### 16.7 二级优化建议（针对 7B dev「实现偏浅」）落地进度

背景：用户提了两级 Prompt / 流水线优化建议，要求「先分析适配性、再动手」。结论是**大部分建议流水线已用更强方式实现**，只 3 点真正可补：

| 建议 | 判定 | 状态 |
|---|---|---|
| 一级① 自底向上重构（先子函数再拼主函数） | 已具备：`DEV_TWO_PASS` 两遍（pass2 铺辅助函数、pass3 回填主函数） | 已有 |
| 一级② 强制逻辑前置 / 注释先行 | 部分：现有只要求「辅助函数带注释、禁空壳」，无「先输出逻辑清单」脚手架 | **待定②**：在 dev schema 加 `analysis_outline` 结构化字段（避免破坏「只输出 JSON」纪律） |
| 一级③ 缩小上下文聚焦任务 | 部分：检索已是符号级，dev 上下文最宽（24K / 预算 12k），非瓶颈 | 已有 |
| 二级① 空壳自动检测 + 重试 + 拆粒度 + 超次转人工 | 已具备且更强：机械校验（`empty_implementation` / `patch_incomplete` INCOMPLETE_RATIO=0.3 / `_patch_blockers` 强制 rework / `MAX_REWORK_ROUNDS`→needs_human） | 已有 |
| 二级② TwoPass 改「逻辑校验 + 代码生成」两阶段 | 是现有两遍的变体，收益不明、需新增 artifact | 暂缓 |
| 二级③ 分片重构（>80 行函数拆 2~3 片，每片 ≤40 行，最后拼接） | 直击真机痛点（pass3 原强制「index_all 单 edit full_symbol」= 逼 7B 一次写大函数） | **已做①（见 §16.5.1）** |

**根因提醒（决定要不要继续②）**：真机 `run 20260923-065850` 偏浅主因是 **7B 容量**（dev 24K、预算 12k、实际输出才几百字符），不是上下文溢出；瓶颈在模型执行力，纯 prompt 改动收益有限。真正兜底的是已有的机械校验 + 强制返工闭环。所以优先级：① 分片已落地 → 视真机偏浅是否缓解再决定 ②。

### 16.8 新建项目（greenfield）兼容性结论

用户问「当前结构是否兼容新建项目」。结论：**机械上能跑，语义上不兼容从零新建**——当前结构是为「二次开发存量仓库」定制的。

- **能跑的前提**：`--repo` 可选（`cli.py:50`「不提供则不做检索」）；`orchestrator._build_pool` / `retrieval.select_excerpts` 在 `repo is None` 返回空；新建文件有 `new_file` / `change_type:"add"` 通道（`patches.analyze_edit` 对 `source==""` 走整份写入）；`empty_implementation` / 补丁校验对新建仍生效。
- **三处不兼容（针对从零新建）**：
  1. **dev 两遍重构硬编码 `INDEX_ALL_ANCHOR`**（目标仓库 `index_all` 的真实签名），两遍都强制「辅助函数 insert_after 这一行、第二遍重写 index_all」。换仓库或新建 → pass2 全部锚点 `anchor_not_found` → 强制 rework → `needs_human`。（`prompts.py` 注释自承「若目标仓库变化，需同步更新此处」）
  2. **所有角色 prompt 是「存量 / 最小侵入 / 锚定现有符号 / 遵循存量命名」框架**；新建需要「从零设计目录结构 / 整文件新建」。
  3. **评审判据「是否最小侵入、是否落在白名单」对新建失真**（没有「被侵入」的对象）。
- **两种「新建项目」含义的区分**：
  - 同存量仓库提新需求 → **完全兼容**（每次 `run` 独立，新需求 + 同仓库即可）；
  - 从零生成全新代码库 → **需 greenfield 模式**，否则上面 3 点会让链路崩。
- **greenfield 模式最小改动清单（待拍板，未动手）**：① 两遍锚点动态化（从方案/检索真实入口符号推导）或 greenfield 下 `DEV_TWO_PASS=False` + dev 走整文件新建；② 新增 greenfield 版 system prompt 组（assess→从零设计结构、dev→创建新文件无需 anchor、review→是否满足需求而非最小侵入），用开关（如 `PIPELINE_MODE=greenfield`）切换；③ dev schema 的 `anchor` 在新建文件下改为可选。

> 待办（接下一窗口）：二级②（analysis_outline 字段）、二级③补充空壳正则弱信号、以及是否做 greenfield 模式。

### 16.9 运行列表维护：删除历史测试数据（前端 + 后端）

需求：前端支持维护运行列表，能删除历史测试数据（冒烟/调试攒下的老 run）。

**后端（`pipeline/server.py`）**
- 单个删除：`DELETE /api/runs/<run_id>`（新增 `do_DELETE`）；
- 批量删除：`POST /api/runs/delete`，body `{run_ids:[...]}`，**逐条独立处理**并回报 `removed` / `failed`（某条不存在或正在运行，不影响其余删除）；
- 两者统一走 `_remove_run_dir()`：`shutil.rmtree` 删目录 + 清理 `.inbox/<run_id>.md` 与 `.inbox/<run_id>.ptype` 伴随文件（`_create_run` 写的同名文件，不跟着删会越积越多）+ 从 `_JOBS` 摘除。
- 安全约束：run_id 必须匹配 `RUN_ID_RE`；必须是 `runs_dir` 的**直接子目录**（`run_dir.parent.resolve() == runs_dir.resolve()`，防路径穿越）；**运行中（`job.running`）一律 409 拒绝**，需先「中断」再删——否则子进程还在往目录里写，会留半截产物。

**前端（`pipeline/console.html`）**
- 每行右侧「×」删除按钮（运行中置灰并提示先中断），点击走 `askConfirm` 二次确认后删单条；按钮 `onclick` 里 `stopPropagation`，避免冒泡触发「打开该运行」。
- 「管理」按钮切换管理模式：显示行内勾选框 + 「全选（不含运行中）」+「删除选中 (n)」批量删除。
- 选中态存 `S.pick`（Set）+ `S.manage`，跨列表重渲保留；删除后若删掉的正是当前打开的运行，`clearDetailIfGone()` 清空详情回空态。

**测试（`tools/smoke_console.py`）**：新增「运行列表维护（删除历史测试数据）」段，覆盖单条删除 + `.inbox` 孤儿清理 + 重复删除 404 + 非法 run_id 拒绝 + **运行中拒绝删除(409)且目录仍在** + 批量删除部分失败互不影响 + 空 `run_ids` 400 + 列表不再显示已删项 + 端到端删除主运行后详情 404。
**回归**：console 冒烟 43 → **57 全绿**（含 `node --check` 前端脚本语法校验）；mock 冒烟 **166 全绿**（删除是页面层能力，不触碰编排）。

### 16.10 需求入口补强阶段（intake，跑在 pm 之前）

需求：承接零散/模糊/口语化的原始需求，前置完成结构化补强与要素补全，产出**需求初稿**供 PM 直接消费（来自用户给的「需求入口补强师」角色定义，按当前代码结构适配落地）。

- **位置**：`intake → pm → retrieve → …`。游标新增 `intake`；`config.FULL_STAGE_ORDER` / `runstore.FLOW_ORDER` / `STAGE_STATE_KEY` / `orchestrator.ONLY_STAGES` / console `STAGES`-`STAGE_CN` 同步；`--only intake` 可用。
- **模型**：刻意复用 pm 的 tag `qwen3-8b-pm-16k` —— 两者相邻执行，同 tag 不触发 `ensure_exclusive` 卸载重载，**全流程仍是 3 次模型切换**（冒烟有断言守着）。temperature 与 pm 同档 0.15（同为「读需求 → 填字段」的结构化提取，不是创作）。
- **契约**：`schemas.INTAKE`：`original_summary` / `refined_requirement{background,core_goal,target_users,main_scenarios,core_features,constraints}` / `preliminary_scope{suggested_in_scope,suggested_out_of_scope}` / `missing_elements{element,default_assumption,importance}` / `clarifying_questions{question,suggested_answer,impact}` / `uncertainties`（复用全局 UNCERTAINTY 三元组）。required 的取舍与 SCOPE 一致：`background`/`core_goal`/`core_features` 必填（PM 写 PRD 的骨架，可选项时模型会整段略过，补强等于白做）。
- **提示词**：`prompts.SYSTEM["intake"]` 承载角色定位 + 4 条红线（不编造核心需求、不给技术方案、不越界出 PRD、只出 JSON）+ 5 个补强维度 + 执行步骤 + 填写纪律；片段构造 `parts_intake`。未给 `SYSTEM_NEW` 单独一套 —— 补强是纯需求层整理，不碰代码，两种项目类型共用即可。
- **复用机制**：`parts_pm(requirement, intake)` 把补强初稿以【需求补强初稿】注入 PM，PM 不必再重新解析原始需求。
- **人工介入点**（对应角色定义里的「仅需快速扫一眼」）：① `intake` 已入 `ONLY_STAGES`，可作为 `--pause-after intake` 或页面闸门勾选项；② 可选自动闸门 `config.INTAKE_PAUSE_ON_GAPS`（env `PIPELINE_INTAKE_PAUSE`，**默认 0**）在出现 `importance=high` 的缺失要素时暂停（默认关闭的理由：多数补强出的缺失项是中低重要度，无脑停会白白增加人机交互）；③ 补强的默认假设与待澄清项写进 `handoff.md` 两个小节。
- **测试**：mock +7 条（产物落 `state.artifacts.intake`、过 INTAKE 契约、顺序 `intake→pm`、注入 PM prompt、切换仍 3 次、进 handoff）；console +2 条（阶段产物可见、handoff 含需求补强）。**mock 173 / console 59 全绿**。
- **顺带修正的编号偏移**：intake 前移使 `NN-<stage>.json` 整体后移（pm 01→02、assess 02→03、dev 04/05→05/06、human_review 08→09），冒烟里的阶段文件编号与调用次数（7→8）均已同步更新。

### 16.10.1 PM 未决项裁决 + PRD 可编辑（与补强裁决统一）

需求：PM 阶段的「未决问题与默认取值」前端操作逻辑做成和裁决一致；不需要「猜错代价」，严重度仅展示；PRD 要能查看与编辑，目标是把各阶段人工调整 / 裁决结果以**完整输出文件**给到下游。

- **PM 未决项裁决**（与需求补强同一套交互）：新增卡片 `d-pmq-card`（有 open_questions 时显示），逐条列出 `question` + 为何重要 + 建议 + 默认取值 + **严重度徽标（仅展示）**，配裁决输入框与「用默认」一键采纳。
  - **刻意不展示 `impact_if_wrong`**（猜错代价）：裁决阶段不需要它，只占版面；字段仍在 schema 里，只是不进页面。
  - 后端 `POST /api/runs/<id>/pm-decisions`：按 `question` 定位写入 `final_decision` / `confirmed`，整理出陈述式 `confirmed_facts` 回写 `NN-pm.json`；「用默认」采纳 `assumed_answer` 时同样剥掉「默认 / 本次默认按此执行」前缀。
  - **下发形态**（`prompts.pm_assumptions_block`）：已裁决 → 【PM 未决项（已由人工裁决，以下是**确定结论**，直接采纳）】；未裁决 → 【PM 未决项的默认假设（人工尚未确认，但下游一律按此推进）】。**不再把已裁决的当问答题抛给下游**，避免评审/后续阶段反复提问。
- **PRD 查看 + 编辑**：`prd.md` 从只读渲染改为可编辑（`POST /api/runs/<id>/prd`，body `{content}`）。
  - 人工改写后会留下 `prd.human` 标记，`orchestrator._write_prd` 见到该标记**不再用产物覆盖** —— 否则人工改一次就被下一次 `_persist` 冲掉。
  - 「按产物重新生成」= `{regenerate:true}` 删标记，回到产物驱动。
  - 这样各阶段的人工调整（改产物）与裁决结果（intake/pm 的 `final_decision` + `confirmed_facts`）都会体现在 PRD 这份完整输出文件里。
- **测试**：mock +3（已裁决作确定结论下发、未裁决仍带默认假设、两区呈现）；console +10（按 question 合并、去掉默认前缀、confirmed_facts、final_decision 落盘、PRD 保存/读回/空值拒绝/标记存在/重新生成清标记）。**mock 195 / console 81 全绿**。

### 16.11 续跑闸门的「原始输入」展示（锁定但可读）

反馈：续跑区的人工闸门在运行中/已结束时被锁定（disabled）是对的，但**看不出当初勾了什么**。

- **根因是可见性而非数据**：`runstore.run_detail` 的 state 白名单里本来就有 `pause_after`（数据到得了前端，勾选框其实已按它勾上），问题是浏览器把 `disabled` 控件整体压暗，深色底上几乎分不出选中与否。
- **修复①（CSS）**：`input[type=checkbox]:disabled{opacity:1;accent-color:#6ea8fe}` —— 锁定态不再降透明度并给明确强调色，保持 disabled 语义不变。
- **修复②（只读读数行）**：`d-gates-ro`，仅在**非 editable**（运行中 / 已结束）时显示，把闸门直接列成徽标；editable（暂停态）时隐藏，避免和真正的输入控件重复。
- **修复③（原始输入持久化）**：续跑时勾成「跑到底」会把 `pause_after` 清空，之后就再也读不到这次运行当初勾了什么。新增 `state.initial_pause_after`（`_snapshot` 写入、`_restore` 沿用、`run()` 首次启动时记录、**不被 resume 覆盖**），并在 `run_detail` 白名单透出。页面读数**优先展示 initial**，若与当前不一致再补一句「当前已改为：…」。
- **测试**：mock +3 条（创建时记录 initial、续跑清空后 pause_after 为空、initial 不被覆盖）。**mock 176 / console 59 全绿**。

### 16.12 真机故障：新增阶段后 CLI 硬编码白名单漏改（运行启动即死）

现象：`runs/20260924-134458` 目录里**只有 console.log**，没有 state.json、没有任何阶段产物 —— 启动后立刻退出。日志只有一行：
`--pause-after 中含未知阶段: ['intake']，可选 ['pm','architect_assess','architect_plan','dev','test','review']`

- **根因**：`cli.py` 里 `ALL_STAGES` 是**硬编码列表**，加 intake 时同步了 `ONLY_STAGES` / `FULL_STAGE_ORDER` / console `STAGES`，唯独漏了它。`--pause-after` / `--only` / `--from` 都拿它做校验，于是页面勾了「需求补强」闸门启动后，子进程直接 `exit 2`，运行刚建目录就死。
- **修复**：`ALL_STAGES` 改为**从 `ONLY_STAGES` 派生**（`[s for s in ONLY_STAGES if s != "human_review"]`，与 `server.PAUSE_STAGES` 同一口径），以后再加阶段不会再漏。
- **防回归**：mock 冒烟加 2 条断言（`ALL_STAGES == ONLY_STAGES 去掉 human_review`、`intake` 在其中）。**mock 178 / console 59 全绿**。
- **排查提示**：某次运行「只有 console.log、没有 state.json」＝ 参数校验在启动阶段就失败了，先看 `console.log` 的头几行，不要去查模型。

### 16.13 启动即失败的运行会在列表里隐身（连带修）

现象：那次失败的运行刷新后**从列表里消失了**——人工看不到失败原因，也没法从页面删掉它那个空目录。

- **根因**：`runstore.list_runs` 原有 `if not state and not summary: continue`，而启动即失败的运行既没 `state.json` 也没 `summary.json`，于是被整条跳过。
- **修复**：目录里有 `console.log` 或 `requirement.txt` 就照常列出，标 `status="failed"`；两者都没有的杂目录仍然跳过（避免把 `runs/` 下的无关目录当运行）。前端 `statusBadge` 增加「启动失败」红色徽标，且**无 state 的运行自动展开日志卡**（否则只看到「无 state.json，只能查看」却不知道为什么）。
- **连带收益**：可见即可删 —— 失败的运行现在能从页面「×」/管理批量删掉。
- **测试**：console +4 条（失败运行仍出现在列表、标记为 failed、无关空目录不被当运行、可从页面删除）。**mock 178 / console 63 全绿**。

### 16.14 需求补强裁决录入页（人工确认默认假设的入口）

反馈：补强跑完后**没有入口录入「待确认问题的裁决」** —— 补强给的是默认假设/建议答案，没有人工确认就流入下游，等于白补了一半。

- **页面**：运行详情新增「需求补强 · 待确认问题裁决」卡片（`d-intake-card`，仅当存在 intake 产物时显示）。上半部分是只读的补强结论（核心目标/背景/核心功能/目标用户/场景/约束），下半部分逐条列出：
  - **缺失要素与默认假设**：要素名 + 重要度徽标 + 默认假设 + 裁决输入框 + 「用默认」一键填入；
  - **待澄清问题与建议答案**：问题 + 建议答案 + 不澄清的影响 + 裁决输入框 + 「用建议」一键填入。
  - 裁决按 `ref`（要素名 / 问题原文）回显已保存值；**留空的条目不落盘**，避免下游看到一堆空结论。
- **后端**：`POST /api/runs/<run_id>/intake-decisions`，body `{decisions:[{kind,ref,decision}]}`。校验 `decisions` 必须是数组、必须有 state.json（否则 409，提示先用 `--pause-after intake` 停住）；清洗空条目后写入 `state.intake_decisions`，并往 `human_actions` 追加 `intake_decision` 留痕。
- **裁决不是另递一份清单，而是并回补强产物本身**（用户纠正后的设计）：
  - `server._save_intake_decisions` 按 `ref`（要素名 / 问题原文）定位补强产物里对应条目，写入 `final_decision` + `confirmed=true`，再用 `runstore.save_artifact` 回写 `NN-intake.json` —— 该文件由「初稿」变为「**已裁决终稿**」，**成为下游唯一真源**。
  - 原来的 `default_assumption` / `suggested_answer` **保留**作补强当初的猜测（审计痕迹），由标题明确"final_decision 覆盖它们"。
  - `prompts.parts_pm` 不再输出独立的【人工裁决】块，只在标题里区分「初稿 / 已裁决终稿」（`prompts.intake_has_decisions` 判断）。理由：同时给两份值，模型反而不知道听谁的。
  - `state.intake_decisions` 仍保留一份结构化副本（页面回显 + 编排器 `_restore` 读取），并往 `human_actions` 追加 `intake_decision` 留痕。
  - **其余阶段**（assess/plan/dev/test/review 看不到补强产物）：由 `orchestrator._human_facts()` 把裁决并入【人工已确认的事实与指令】注入，防止下游把已裁决的问题又当未决项重提。
- **真机教训（run 20260924-135801）**：裁决确实进了 PM 的 prompt（5 条），但每条的值就是默认文案本身（人工点了「用默认」按钮），等于没给新信息，PM 于是照旧提问。
- **「用默认 / 用建议」＝人工采纳该默认值**（不是再复制一遍猜测）：带入时必须剥掉 `默认假设 / 默认建议 / 默认 / 建议答案 / 建议` 前缀（正则 `^\s*(?:默认假设|默认建议|默认|建议答案|建议)\s*[:：]?\s*`）。前后端都剥：前端填充/提交时剥，服务端保存时再剥一次兜底。剥完才是「使用方向键控制」而不是「默认假设使用方向键控制」。
- **裁决后的下游输出要「调整过」，不能继续问答模式流转**：`prompts.finalized_intake_view` 把已裁决条目收进 `confirmed_facts`（陈述式：`控制方式：使用方向键控制`），并并入 `refined_requirement.constraints` 标「已确认：」；已裁决的条目**不再出现在** `missing_elements` / `clarifying_questions` 里（未裁决的才留下，那才是真的没定）。这样 PM 拿到的是"确定结论 + 少量待定项"，而不是一份问卷，不会再把已裁决的问题重问一遍。
- **测试**：mock +5（未裁决识别为初稿、有 final_decision 识别为终稿、PM 收到终稿且被告知覆盖规则、未裁决时收到初稿、裁决并入 `_human_facts`）；console +6（按真实 ref 提交、合并条数正确、补强产物带 final_decision、原默认值保留、未匹配条目仍留 state、非数组被拒）。**mock 195 / console 81 全绿**（含早期 §16.12/§16.13 批量删除与失败运行可见性、§16.11 续跑闸门原始输入展示、§16.10 补强阶段等累计断言）。
- **用法**：要让裁决在 PM 之前生效，新建运行时在闸门里勾选「需求补强」（或 `PIPELINE_INTAKE_PAUSE=1`），补强结束会停住 → 录入裁决 → 点「继续执行」。不勾闸门则 intake→pm 直接跑完，此时录入的裁决从下一次重跑/续跑开始生效。

---

## 17. 架构收敛这一轮（2026-09-24 第六轮）：流定义单一真源 / 统一闸门 / 检查点回放

背景：与 LangGraph 做对照评估后（结论是**不换框架**——本项目的价值在领域契约层：锚定补丁、覆盖审计、事实接地、问题分级改判、检索池，LangGraph 全不提供），
决定**只吸收它的三个设计模式**，不引入任何新依赖（仍是纯标准库）。

### 17.1 流定义单一真源 `pipeline/flow.py`（新增）

- **问题**：同一份流程知识以前散在 5 处 —— `runstore.FLOW_ORDER`、`config.FULL_STAGE_ORDER`、`orchestrator.ONLY_STAGES`、`orchestrator._step` 的 if 链，外加 `cli.ALL_STAGES` / `server.PAUSE_STAGES` 两份白名单。真机故障 §16.12（加 intake 后 CLI 白名单漏改，`--pause-after intake` 被判未知阶段、进程 exit 2）就是这个结构缺陷的产物。
- **做法**：`flow.py` 声明 `EXEC_ORDER` / `MODEL_NODES` / `PLAIN_NODES` / `GATE_NODES` / `LINEAR_EDGES` / `CONDITIONAL_EDGES` / `LOOP_EDGES` / `GATE_SPECS` / `STAGE_STATE_KEY`（术语对齐 LangGraph：node / edge / interrupt），其余模块**派生**：
  - `runstore.FLOW_ORDER`、`runstore.STAGE_STATE_KEY` ← `flow`
  - `config.FULL_STAGE_ORDER` ← `flow.FLOW_ORDER`
  - `orchestrator.ONLY_STAGES` ← `flow.ONLY_STAGES`；`cli.ALL_STAGES` / `server.PAUSE_STAGES` 仍旧从 `ONLY_STAGES` 派生（不动）
  - `orchestrator._step` 的**线性跳转**改为 `flow.next_linear(...)`（intake→pm、pm→retrieve、assess→plan、dev→test、test→review）；条件分支（`project_type`、评审判定、人工 verdict）仍显式写在代码里，但边已在图中声明
- **`flow.validate()`**：跨表一致性校验（边目标合法 / 线性边与 `EXEC_ORDER` 自洽 / `STAGE_MODELS` 与 `MODEL_NODES` 一致 / `STAGE_SCHEMAS` 覆盖模型阶段 / `STAGE_STATE_KEY` 覆盖流程阶段 / 闸门声明合法 / human_review 不在可暂停阶段）。`flow.assert_valid()` 在 **CLI 与 server 启动期**调用，漏登记直接报错而不是等真机跑挂。
- **导出**：`flow.mermaid()`；`python -m pipeline.cli --show-flow` 打印拓扑 + 校验结果；`GET /api/flow` 给页面。
- **页面**：详情页新增「流水线拓扑」折叠卡片（`d-flow-card`），显示校验状态 + Mermaid 源码 + 一键复制（离线环境不引 CDN 渲染，只给源码）。

### 17.2 统一闸门为 interrupt 语义（`orchestrator`）

- **问题**：以前「该不该停下等人工」在三处各判一遍 —— `_should_pause`（显式 + PM 条件闸门）、`_step` 里 intake 的内联判断、`_step_human_review` 的自暂停（自己写 `status="paused"` 再由 `_execute` 特判 `status=="paused"` 提前 return）。
- **做法**：新增 `Interrupt`（stage/kind/title/detail）与两个入口：
  - `_gate_after(stage, nxt) -> Interrupt | None`：**唯一判定**。三类 kind 对齐 `flow.GATE_SPECS` —— `explicit`（`--pause-after`/页面显式勾选，无条件停）、`conditional`（PM 有未决项 / 补强有 high 缺失要素）、`mandatory`（`human_review` 首次到达：`nxt == stage` 且尚无 verdict）。
  - `_interrupt(it)`：**唯一执行**。统一做 `status/paused_after` 落置 + `human_gate` 留痕 + `_persist()` + 打印可操作提示（暂停文案只剩一份）。
  - `_should_pause()` 保留为薄包装（`_gate_after(...) is not None`），历史测试与外部引用不受影响。
  - `_step_human_review` / `_step` 不再自己暂停；`_execute` 从「特判 status==paused + `_should_pause`」简化为「`_step` → `_gate_after(stage, nxt)` → 命中则 `_interrupt` 并返回」。
- **注意**：`mandatory` 闸门显式再查一次 `verdict`（双保险），避免「打回后 `nxt=dev`」被误判。

### 17.3 检查点（checkpoint）语义化 + 按 seq 回放

- **问题**：`NN-<stage>.json` 一直就是状态快照，但只有「按阶段名打回重跑」（`--from <stage>`）这一种用法 —— 同一阶段在多轮迭代里各有一份，按阶段名只能整条尾巴一起作废，无法精确回到「某一轮的那一次」。
- **做法**（把快照正式当作 checkpoint，`seq` 即 checkpoint id）：
  - `runstore.checkpoints(run_dir)`：检查点时间线，**含 `superseded/` 归档**，每条带 `seq / stage / file / superseded / state_key / tag / note / human_edited / has_artifact / mtime`，并补 `index`。`run_detail()` 新增 `checkpoints` 字段。
  - `runstore.archive_after_seq(run_dir, seq)`：把 **严格大于** seq 的在存快照归档（目标检查点自身保留 —— 这就是它和 `archive_stages` 的关键区别）。
  - `orchestrator.restore_checkpoint(seq)`：定位检查点 → 归档其后产物 → 弹出其后阶段的 state 产物键（目标检查点保留）→ 回退轮次计数 → 游标归位到该阶段 → 留痕 `checkpoint_restore`。
  - `resume(..., from_checkpoint=N)` 与 CLI `--from-checkpoint N`（与 `--from` 互斥）。
  - HTTP：`GET /api/runs/<id>/checkpoints`、`POST /api/runs/<id>/replay` `{seq}`（校验 400/404，走与「打回重跑」同一条子进程链路，单驻留约束不变；可选 `feedback` 走「写 state 让编排器 `_restore` 读」的既有路径）。
  - **页面**：详情页新增「检查点时间线」卡片（`d-ckpt-card`），逐条显示 `#seq + 阶段 + 标记（已作废/人工改过/当前状态点）+ tag`，每条一个「回放到此处」（运行中/已结束态置灰，已作废的检查点不可回放）。
- **真机教训（本轮自查）**：`archive_from_seq` 最初写成 `seq >= 目标`，会把**目标检查点自己**也归档掉 —— 已改名为 `archive_after_seq` 并明确「严格大于」语义，同时加了「目标检查点自身保留」的断言兜住。

### 17.4 验证状态

- `python -m pipeline.cli --show-flow` → 拓扑 + `一致性校验：通过`。
- **smoke_mock 224 断言全绿**（本轮 +29：流定义派生与校验器真能抓漏登记、Mermaid 含回流边、interrupt 三类 kind 与 human_review 首达/已提交判定、检查点时间线排序与 state_key、按 seq 回放只作废其后产物且目标保留、回放留痕）。（后续 §18 又追加了断言，**当前总数见 §18**。）
- **smoke_console 96 断言全绿**（本轮 +15：`/api/flow` 一致性、检查点列表字段、详情与列表一致、replay 的 400/404 校验、真回放一次后 superseded 增加 + `checkpoint_restore` 留痕 + 跑回 pass）。
- 流定义白名单派生后，`cli.ALL_STAGES == flow.PAUSABLE_NODES` 有断言守着（§16.12 那类漏改在结构上被消除）。

---

## 18. 前端交互这一轮（2026-09-24 第七轮）：流程图节点 ↔ 阶段产物/日志/状态

需求：把各阶段产生的**产物与日志绑到流程图的阶段节点上** —— 点节点看该阶段的产物文件列表、执行日志、状态信息；要求可视化清晰、点击响应及时、详情直观易读。

### 18.1 后端：运行日志按阶段切分（`runstore` + `orchestrator` + `server`）

- **阶段边界标记**：`runstore.stage_marker(stage)` 产出 `== STAGE <stage> ==`，由 `orchestrator._step` 在**每个阶段开跑前**写一行（`_run_only` 同样写，保持一致）。格式**只在 runstore 定义一次**，写入端与读取端共用，避免两边正则漂移（本轮专门加了「写入标记回灌给读取端」的往返断言）。
- `runstore.log_sections(run_dir)` → `(sections, preamble)`：按标记切段，每段带 `stage` 与 `index`（同阶段第几次执行，回流循环里 dev/test/review 会出现多轮）；第一条标记之前的内容归 `preamble`（run 启动信息）。
- `runstore.stage_log(run_dir, stage, max_lines)` → 该阶段的切片正文 + 元信息（`occurrences` / `has_markers` / `truncated` / `lines`）。**关键分支**：有标记但该阶段没跑过 → 明确返回空，**不退回整段尾部**（否则页面会把「整个运行的日志」误当成「这个阶段的日志」）；只有旧运行（完全没有标记）才退回日志尾部并标 `has_markers=false`，页面据此显示「该运行没有阶段标记（旧运行）」。
- HTTP：`GET /api/runs/<id>/log?stage=<stage>&lines=N` → JSON（阶段名先校验，非法 400）；**不带 `stage` 时行为不变**，仍返回 `text/plain` 的整段日志尾部（兼容原有日志卡片）。

### 18.2 前端：交互式流程图 + 阶段详情面板（`console.html`）

- **纯手绘 SVG，零依赖**：离线环境不能引 CDN，也刻意不引任何布局/绘图库。节点坐标按真实拓扑在 `GRAPH_POS` 里手工定位成 3 行；表里没有的阶段（未来在 `flow.py` 新增）自动落到兜底网格，**不会从图上消失**。节点图标/文案/边全部由 `flow` 的数据渲染：节点列表取 `flow.nodes`，边取 `flow.linear` + `flow.conditional`，因此**图与流定义真源始终一致**（`/api/flow` 不可用时退回内置兜底拓扑，保证图永远画得出来）。
- **节点状态着色**（`graphStatus`）：已完成（绿）/ 执行中（蓝，呼吸动画）/ 等待人工（黄）/ 需人工介入（红）/ 已跳过（暗）/ 未开始（灰）。「已到达」是**单调推进**算出来的：以各阶段快照 + 当前游标 / `paused_after` 里最靠后的那个位置为界，之前的都算到达；`needs_human` 的收尾运行**不会**把没跑到的阶段一律标绿。节点副标题显示「N 次 · 耗时」；`×N` 角标标出多轮；无产物但确实跑过的阶段（检索、交付完成）显示「已执行 · 无产物」，不再是自相矛盾的「未执行」。
- **边的走线与标签**：同排向前直连；同排反向（回流）从下方绕；跨排走三次贝塞尔。**标签拥挤时自动改拱线**（中间隔着节点、或两节点间距 < 96px 塞不下标签）—— 这是实测发现的问题：`retrieve→architect_assess` 只隔 18px，直连的标签会被节点框压住。**同一对节点的多条条件边会合并成一条**（`review→done` 同时有 `needs_human` 与 `escalated_ambiguous`，不合并会画两条完全重叠的线、叠两段字）。标签统一走 `EDGE_CN`（键为「源阶段|条件」）中文化：通过 / 回流开发 / 回流方案 / 需人工介入 / 转人工 / 放行 / 打回 / 新建项目 / 二次开发。当前阶段发出的边点亮（`e-hot`），一眼看出走到哪了。
- **阶段详情面板**（点节点后出现在右侧）：三块 —— ①**状态信息**（阶段名、状态徽标、执行次数、模型 tag + num_ctx、最近耗时含加载、prompt/out token、prefill/gen 吞吐、切换/契约重试/裁剪/预算告警/人工改过等标记）；②**产物文件列表**（该阶段全部检查点，含已作废与人工改过标记，每条一个「在阶段产物中定位」）；③**执行日志**（该阶段切片 + 执行轮次/是否截断的说明 + 「刷新日志」）。
- **响应及时性**：点节点先同步渲染状态与产物列表，**日志再异步填入**（`#gp-log` 先显示「加载中…」），所以点击不卡在 IO 上；实测「点击 → 详情可读」**29ms**。日志按 `运行:阶段` 缓存，运行中才自动重读；重读时若用户没往上滚就自动跟到底部（不打断回看）。
- **与「阶段产物与操作」联动**：`locateStage()` 在下方产物卡片里滚动定位、自动展开并高亮该阶段的所有产物块（`.stage.hl`），把「流程图节点」和「可编辑产物」这两处理念上绑起来。
- **轮询友好**：图与面板都做**签名去抖**（节点状态/轮次/选中项未变则不重渲），避免 2.5s 轮询把展开状态、滚动位置和正在看的日志冲掉；手动「刷新」会清签名与日志缓存强制重取。
- 原「流水线拓扑（Mermaid 源码）」独立卡片并入本卡片的折叠区（校验状态 + Mermaid 源码 + 复制按钮保留）。

### 18.3 测试

- **新增 `tools/smoke_ui.mjs`**（Edge 无头 + CDP，Node 25 自带 WebSocket；本机 playwright 的 Chromium 因沙箱权限起不来，见环境备忘）。22 条断言：节点/边数与流定义一致、回流边虚线、标签全中文、节点悬停说明、状态着色（含「未到达的终止节点不被误标为已完成」）、点击展开详情与高亮、三块内容齐全、日志切片带边界标题且切换节点互不串台、产物定位高亮与自动展开、点击响应 < 1200ms、页面无 console.error。
  - **它自己造数据**：优先 `POST /api/runs`（`mock=true`）建一个临时运行、跑完**自动删掉**（`RUN_ID=...` 可指定复用已有运行）。原因：旧运行没有阶段标记，「切片带边界标题」这类断言会误报；有运行在进行中（单驻留 409）或建不了时才退回「挑一个已有运行」，并把标记相关断言自动放宽为 SKIP。
  - 环境不满足（没 Edge / 服务没起 / 没有可验证的运行）时打印 SKIP 并以 0 退出 —— UI 冒烟是可选增强，不该让整体回归变红。`SHOT=<path>` 可顺带导出卡片截图做肉眼复核。
  - 注意这是**唯一需要 Node 的工具**，且不在 Python 冒烟套件里，按需手动跑。
- **`smoke_mock` 236 断言**（本轮 +12）：日志切分边界、preamble 归集、多轮编号递增、单轮切片边界、不串台、「有标记但没跑过 → 空」、旧运行退回尾部、编排器确实写标记、写入标记能被读取端原样切分、切片带内容。
- **`smoke_console` 102 断言**（本轮 +6）：`/log?stage=intake` 带边界标题、`dev` 多轮合并、切片互不串台、非法阶段 400、不带 `stage` 时仍是整段文本（兼容）。
- 实机截图肉眼复核确认：节点无重叠、边标签不被压、图例紧贴图、面板信息完整。

### 18.4 口径与文案（避免误读，实机复核时改的）

- **「N 条产物」而不是「N 次执行」**：产物条数 = 该阶段的模型调用次数。开发有两遍（`config.DEV_TWO_PASS`，先铺辅助函数再回填主函数体）、回流循环还会多轮，两者都会各留一份快照 —— 写「执行轮次」会让人误以为 dev 跑了两轮。节点副标题、`×N` 角标、图例、面板行（改名「模型调用」）统一按这个口径。
- **「已执行 · 无产物」而不是「未执行」**：`retrieve`（检索，结果进 `state.pool`，不落文件）与 `done`（终态）本来就没有快照，标成「未执行」会与绿色的「已完成」徽标自相矛盾。「已跳过」留给「到达了但确实没跑」的情形（如新建项目跳过的 `architect_assess`）。
- 节点的 `title`（悬停说明）统一为「中文名（阶段名）　状态」。

---

## 19. 「最终输出要能跑」这一轮（2026-09-24 第八轮）：新文件补丁合并 + 运行验证阶段

起因是复查 run `20260924-135801`（绿地项目：新建贪吃蛇）时实测出的两个真问题：
① 4 条补丁指向同一个新文件、逐条写入互相覆盖，落盘只剩 1 个类，而审计报「6 条可套用 / 0 问题」；
② 评审只是**读文件**，对「能不能跑」毫无证据 —— 用户明确要求「增加运行的机制，做最终输出结果的验证和确认」。

### 19.1 同一新文件的多条补丁必须合并（`patches.py`）

- **问题**：方案常把多个类放进同一个新文件（`game_logic.py` 里的 Snake/Food/Collision/Game），
  开发就各出一条 `full_symbol` 补丁；目标文件不存在时它们都被判为 `new_file`「整份写入」，
  而 `apply_all` 是**逐条 write_text** —— 后写覆盖先写，**静默丢代码**。
  单条看都没问题，这是**跨补丁**的冲突，逐条校验看不见它。
- **`merge_new_file_blocks(patches)`**：把同一路径的多段 patch 合并成一份，并把各段的**顶层 import 提顶去重**
  （分段生成时每段都会重写一遍 `from typing import ...`；缩进里的 import 原地保留）。
- `apply_all`：同一新路径**合并成一次写出**（`patches: N`），并且不再产生误导性的「跳过：文件不存在」。
- `write_patch_files`：同一新路径只落**一份** diff（文件名取路径名、序号取组内第一条），
  组内所有行共用它 —— 否则逐个 `git apply` 照样互相覆盖。
- `analyze_all`：新增跨补丁核对 —— 同一个新文件里**重复定义同名符号**报 `new_file_duplicate_symbol`
  （合并后会得到两份定义），并纳入 `_patch_blockers()` 的阻断级（评审 pass 也会被改判）。
- `flow.validate()` 之外，这条也是「机制兜底」家族的一员：机器能证明的东西不交给模型判断。

### 19.2 新的机械阶段 `verify`（运行验证，跑在 test 与 review 之间）

- **定位**：不是让模型「评审文件」，而是把补丁物化到沙箱后**真的执行一遍**，把退出码/输出当机械证据。
  流程变为 `… → dev → test → verify → review → human_review`；`verify` 在 `flow.py` 里属于
  **`CHECK_NODES`**（不调模型、但产出工件），因此不进 `STAGE_MODELS`，但要进 `STAGE_SCHEMAS`
  与 `STAGE_STATE_KEY`（`verify_report`），且**可暂停/可 --only**（它是「看一眼真实运行结果」的最佳停点）。
- **`pipeline/verify.py`**（唯一会执行外部命令的模块）：
  - **物化**：`runs/<id>/verify/work` 沙箱副本。有仓库就先按忽略表复制（体积上限 `VERIFY_COPY_LIMIT_MB`），
    再用 `apply_all(out_dir=沙箱)` 把补丁结果覆盖上去；绿地项目只物化补丁产出的文件。**绝不碰原仓库**。
  - **命令计划**：① `py_compile` 语法检查（必跑，只解析不执行）；② **导入检查**（`importlib` 逐个 import）；
    ③ 测试阶段声明的 `automated_commands`；④ 兜底探测（有测试目录 → `pytest -q`，有 `main.py` → 跑它）。
  - **导入检查为什么必须有**：`py_compile` 只看语法。真机 `graphics_renderer.py`
    在 `def __init__(self, game_area: Tuple[int, int])` 里用了**从未导入**的 `Tuple` —— 语法完全合法，
    **import 时才炸**（`NameError`）。这一步用标准库就补上了「读文件读不出来」的洞。
  - **安全约定**：只跑白名单程序（`VERIFY_ALLOWED_BINS`）；命中危险片段（`VERIFY_DENY_PATTERNS`）或
    含管道/重定向/命令替换（`[|&<>` + "`" + `$]`）的命令**只记录不执行**；逐条超时（`VERIFY_TIMEOUT`）；
    子进程环境洗掉凭据类变量并置 `SDL_VIDEODRIVER=dummy`（无显示环境下 pygame 类程序也能跑逻辑）；
    输出只留尾部 1500 字符。注意**不放行 `;` 之外的 shell 元字符、但放行 `;`** —— 我们从不经过 shell（argv 直调），
    `;` 在这里是惰性的，而 `-c` 脚本正需要它分句。
  - **结论**：任一命令失败/超时 → `fail`；全通过 → `pass`；没命令可跑（含没给仓库路径）→ `skipped` 并说明原因。
    **没给 `repo` 时直接跳过并说明**，而不是空转出一堆「未能套用」。
- **证据进评审**：`prompts._verify_view` 把结论 + 失败原因 + 每条命令的退出码与输出尾部蒸馏后
  注入 `parts_review`（位于测试摘要之后、实现之前 —— 和测试摘要同理，评审是最紧的 8K 上下文）。
- **机制阻断**：`_verify_blockers()` 与 `_patch_blockers()` 合并为 `_mechanical_blockers()`，
  **运行验证失败时即便评审判 pass 也改判 `rework_dev`**（`rounds[].mechanical_blockers` 留痕）。
- **配置**（页面「⚙ 配置」页可改）：`VERIFY_ENABLED`(默认开) / `VERIFY_TIMEOUT` / `VERIFY_MAX_COMMANDS` /
  `VERIFY_COPY_LIMIT_MB`；`VERIFY_ALLOWED_BINS` 等策略常量在 `config.py` 里。
- **mock 运行**：只**计划**命令、一律标 `skipped`（离线冒烟保持确定性，且不会在 CI 上乱跑东西）。
- **页面**：新增「最终输出 · 运行验证」卡片（结论徽标 / 沙箱路径 / 每条命令的状态·退出码·耗时·输出尾部 /
  问题列表），流程图多了「运行验证」节点（`GRAPH_POS` 同步，画布加宽到 1004）。

### 19.3 真机复核（对 run `20260924-135801` 直接跑新机制）

```
verdict = fail（执行 4/4 条，失败 2 条）
materialized = ['game_logic.py', 'graphics_renderer.py', 'input_handler.py']
[syntax] py_compile                → ok     ← 合并修复生效：4 个类都在同一份文件里且语法合法
[import] importlib 逐个 import      → fail   ← OK game_logic / FAIL graphics_renderer NameError: name 'Tuple' is not defined / OK input_handler
[planned] python game_logic.py      → ok     （模块级只有类定义）
[planned] python graphics_renderer.py → fail  NameError: name 'Tuple' is not defined（line 4）
```

即：**合并修复让绿地产物第一次真正成型**，而运行验证抓到了「读文件看不出来、模型评审也漏掉」的硬伤，
并会自动把这类交付改判为返工。

### 19.4 测试

- **smoke_mock 271 断言**（本轮 +35）：同路径补丁合并成一次写出、三个类都在、import 去重提顶、
  落盘只有一份 diff、重复定义符号报问题并进阻断级；危险命令/非白名单/管道被拒、白名单与带引号命令放行、
  语法错误判 fail 且留下原文、能跑的判 pass、**语法合法但用了未导入名字被导入检查抓到**、
  mock 只计划不跑、运行验证失败把评审 pass 改判 rework_dev。
- **smoke_console 107 断言**（本轮 +4）：运行验证产物在详情可见、mock 下每条命令都未执行、
  没给仓库路径时如实说明无法验证。
- **smoke_ui 22 断言**：流程图节点/边数与流定义一致（含新增的 verify 节点），点击交互照旧。

---

## 20. 入口总闸这一轮（2026-09-24 第九轮）：全局架构岗 + 大型项目模块化拆分

### 20.1 要解决的问题

前八轮把「**单个需求 → 一次受控改造**」打磨到位了，但缺一个**项目级入口**：

- 来一个大需求（跨模块、多端、要拆子系统）时，只能整包丢给一次流水线 —— 方案会贪多、
  上下文会挤爆、出了问题整包返工；
- 也没有任何机制回答「这个需求该不该拆」，更谈不上「拆完怎么逐个送进同一套流水线」。

这一轮引入 `flow.PRE_NODES` 里的 **`global_architecture_analysis`（入口总闸）**：
小需求原样透传，大需求拆成模块、逐个跑同一条流水线。

### 20.2 关键决策：为什么是「作业层」而不是新阶段

| 决策 | 理由 |
|---|---|
| 它是 `PRE_NODES`，**不进** `EXEC_ORDER` / `PAUSABLE_NODES` | 旁路时（`--gateway off`）行为与产物**一字不差**；`resume`/游标/闸门语义完全不受影响 |
| 产物落 `runs/_jobs/<job_id>/`，不进 `state.json` | 沿用「`_` 前缀不进运行列表」的既有约定，作业与运行互不污染 |
| 每个模块 = 一次**原封不动**的 `Orchestrator.run()` | 角色逻辑、提示词、闸门、审计全部零改动 —— 拆模块不该顺带改引擎 |
| 串行执行（不并行） | `OLLAMA_MAX_LOADED_MODELS=1` + 单卡：并行只会反复换模，比串行更慢 |

新增/改动文件：`pipeline/gateway.py`（新）、`pipeline/ga_prompt.py`（提示词**逐字归档**，
与 `prompts.py` 分离，可用 `config.local.json` 的 `prompts.global_architecture_analysis` 覆盖）、
`flow.py`（`PRE_NODES`/`PRE_EDGES` + 校验口径）、`schemas.py`（`GLOBAL_ARCHITECTURE`/`PRE_SCHEMAS`）、
`config.py`（专用 `ModelSpec`）、`cli.py`/`server.py`/`console.html`（入口与页面）、
`local_config.py`（`guard` scope）。

### 20.3 提示词是外部给定的 ⇒ 三个缺口只能在闸门这一层补

提示词与字段模板由使用方给定、要求**逐字复用**。它自带红线、自检清单与 JSON 模板，
但**模板里没有 scale 字段、也没有路径级信息**，所以：

| 缺口 | 补齐方式 |
|---|---|
| **规模判定** | `gateway.derive_scale()`：从 GA 产物按**四条确定性判据**推导（模块数 ≥2 / 跨模块接口 ≥1 / 执行顺序 ≥2 / 高风险模块 ≥2），每条判据进 `reasons` 供审计。刻意**不**把 `uncertainties` 计入 —— 信息不足 ≠ 规模大 |
| **路径级信息** | `gateway.module_dirs()`：按模块名/职责在顶层目录里做确定性关键词匹配（中文二元组 + 英文词），得到「候选落点」；精确文件仍由子流水线的 `retrieve` 阶段定位 |
| **禁区来源** | `gateway.forbidden_paths()`：本地配置 `guard.forbidden_paths` + `--forbidden` 覆盖，**作为输入喂给 GA**（不给输入它只能编造，正好违它自己的红线 3）；产出后再用 `check_grounding()` 做存在性接地校验（只提示、不阻断） |
| **集成校验点悬空** | 明确不新增集成评审节点：`integration_checkpoints` 写进作业的 `report.md` 供**人工**核对 |

### 20.4 「鸡生蛋」与零开销旁路

要判 small/large 才决定是否调 GA，但 GA 本身最贵（14B 一次几十秒）。所以给三档模式：

| 模式 | 行为 | 开销 |
|---|---|---|
| `auto`（默认） | 先做**零模型调用**的预判（`prejudge()`：强信号=存量规模大；弱信号=跨模块字眼 / 多条目 / 长需求，需命中两条），疑似大型才调 GA | 小需求 **0 次额外调用，连仓库都不扫** |
| `always` | 每次先调 GA | 小需求也多一次 14B |
| `off` | 完全旁路 | 与接入前**行为一致** |

预判之后仍用 `derive_scale()` **复核**最终规模；并提供 `--scale small|large` / 页面「强制规模」
用于判错时纠正（不必改代码、不必重跑）。

### 20.5 预算：一个会「静默失效」的坑

`architect_*` 是 `num_ctx=8192 / prompt 4800 / num_predict 3072`。GA 的**输出字段数明显多于**
`architect_plan`（modules + interface_contracts + execution_order + integration_checkpoints +
uncertainties），沿用 3072 很容易撞顶 ⇒ JSON 被截断 ⇒ 契约失败 ⇒ 降级 small ⇒ 表现是
「这个节点等于没干活」，最难排查。因此单独一条 `ModelSpec`：

```
global_architecture_analysis: qwen3-14b-arch-8k, num_ctx=8192, prompt=3800, num_predict=4096
```

`3800 + 4096 = 7896 < 8192` ✓。输入变紧是**故意的** —— 它倒逼存量概览只喂
「目录树 + 文件/行数统计 + 顶层文件样例」，绝不塞代码正文（`build_overview()`，默认 4000 字符上限）。

### 20.6 越界审计与目录归属（一处必须说清的语义）

子流水线自己还会再划一次范围（pm 的 `in_scope`、plan 的 `changes`），可能越界。
角色逻辑不可改，所以只能在闸门层做**只读审计**：

- `audit_module_run()` 读子运行的 `state.json.artifacts.plan.changes`，复用
  `orchestrator._path_stem` 的**同一套**归一与前缀匹配口径（避免「两处判定不一致」）；
- 三类结论分开计数：`forbidden_touched`（踩全局禁区）、`cross_module`（踩**别的模块认领的**目录）、
  `outside_scope`（无主目录，仅提示）；
- **目录归属表**：一个目录被某模块认领才属于它；**没人认领的目录谁都能改** ——
  否则匹配不上关键词的模块会把整个仓库当成「别人的地盘」，首个改动就被误判 blocked；
- 处置力度：命中禁区/越界 ⇒ 该模块标 **`blocked`**、依赖它的模块标 `skipped`，
  **不阻断整组**（删掉半成品比带病继续更贵）。

### 20.7 页面与入口

- **CLI**：`--gateway auto|always|off`、`--scale small|large`、`--forbidden a,b`、
  `--list-jobs`、`--resume-job <JOB_ID>`；`--show-flow` 增加前置节点与入口总闸状态。
- **HTTP**：`GET /api/jobs`、`GET /api/jobs/<id>`、`POST /api/jobs/<id>/resume`；
  `POST /api/runs` 接受 `gateway`/`scale`/`forbidden`（**在写任何文件之前**校验）。
- **页面**：侧栏「作业（全局架构）」列表 → 作业详情（规模判定 / 模块表 + 审计徽标 /
  全局约束 / 集成校验点 / **送进各模块的子需求全文** / report.md）；新建表单加了模式与强制规模；
  运行详情若被拆成作业，会显示「已拆分为作业 job-xxx」并可跳转
  （页面发起的那次运行只剩日志，不提示的话人工只看到一条空记录）。
- **指针**：`gateway.link_run()` 往 `runs/<run_id>/gateway.json` 写作业指针，`/api/runs/<id>` 会带上它。

### 20.8 真机隐患顺手修掉一个

`verify` 把仓库复制进沙箱时**没有跳过 `runs/`**，而沙箱本身位于 `runs/<id>/verify/work` ——
等于一边遍历 `runs/` 一边在 `runs/` 下新建沙箱目录，形成「边复制边给自己造文件」的膨胀
（真机表现：`verify` 长时间不返回，实测目录递归到十几层）。作业会按模块数把它放大 N 倍，
故 `VERIFY_SKIP_DIRS` 加入 `runs`。

### 20.9 测试

- **smoke_mock 325 断言**（本轮 +54）：前置节点登记与校验口径（摘掉 GA 会被一致性校验抓出）、
  预算不越 `num_ctx`、禁区去重保序、预判（小需求/大仓库/多条目三态）、规模推导五态
  （含「不确定项不升级」）、语义自检五类（重复 ID / 漏模块 / 拓扑违例 / 悬空接口 / 幽灵依赖）、
  接地校验、路径归属与三类审计结论、**off/auto/forced 三条路径断言「根本没调模型」**（`_NoCall` 客户端）、
  大型建作业（ga.json / job.json / 子需求落盘 / 禁区写进子需求 / 作业列表 / 运行指针）、
  两种降级（语义不自洽 / 输出不合契约）、子运行越界审计、`blocked` 传播语义、
  **端到端跑完两模块作业**（各自独立 run_id 与 state.json、作业目录不进运行列表、报告含集成校验点）。
- **smoke_console 122 断言**（本轮 +15）：流定义视图带前置节点 / 分支边 / 入口总闸模式、
  `GET /api/jobs`、非法与未知 job_id 的 400/404、`POST /api/jobs/<id>/resume` 的 404、
  未知 gateway/scale 被拒，以及**参数真的透传进 argv**（`--gateway off --scale small --forbidden core/x.py`）。
- **smoke_ui**：流程图节点/边数与流定义一致，点击交互照旧。

### 20.10 已知边界（明确不做）

- **不做并行**：单卡单驻留下并行无收益；多卡再谈。
- **不预置模块 `forbidden_paths` 到子运行**：目前靠「子需求文本里的硬约束 + 事后只读审计」，
  没有改 `assessment` 产物（那属于改引擎产物，风险高于收益）。
- **不新增集成评审节点**：`integration_checkpoints` 只做人工核对清单。
- **子运行逐个跑完整流水线** ⇒ 大需求的耗时与模型调用量按模块数线性增长，这是「逐个送入
  原有流水线」的固有代价；要压只能从模块粒度入手（GA 的拆分质量）。

---

## 21. 「阻断要能自己找对门」这一轮（2026-09-24 第十轮）：返工项归属三档 + verify 证据送达

### 21.1 起因：一次「白烧一轮」的真机复盘（run `20260924-185507`，新建项目·贪吃蛇）

第 1 轮跑完的状态：`verify=fail`（3 条命令，2 条失败），评审判 `rework_dev`。
但两者的「返工项」**都没指向真根因**：

| 来源 | 它说了什么 | 它漏了什么 |
|---|---|---|
| 评审 `required_fixes` | 加 `__main__` 入口 / 补测试用例 / 得分系统异常处理 | 真根因是 `input_handler.py:2` 的 `from direction import Direction` —— 一行**多余且错误**的 import（`Direction` 就在同文件第 4 行定义） |
| 机制 `mechanical_blockers` | `运行验证失败：… FAIL input_handler …` | 这条**根本没进开发输入**（只写进 `rounds` 与评审材料） |

离线复现确认：删掉那一行后 `py_compile` 全过、`IMPORT_CHECK OK 0`、两条失败命令都转绿。
也就是说 —— **机制手上有确凿证据，开发手上没有；而方案阶段（唯一能改「方案漏规划了什么」的地方）
更是完全不知情**。于是下一轮只能整轮回开发，改不动，白烧一轮。

### 21.2 契约：返工项作用域从两档扩到三档

```python
# schemas.FIX_SCOPE
["in_material", "architect", "needs_external"]
```

`architect` = **方案层才能改**（方案漏规划文件 / 漏定义接口 / 任务边界划错 / 依赖没声明）。
**作用域决定回到哪个阶段**，这句现在写进了两套评审提示词（二开 + 新建），并明确：

> ⚠ 不要把方案层根因写成 `in_material` —— 开发被约束在方案的 `changes` 范围内，改不动它。

选择「扩枚举」而不是「新增字段」的原因：`blockers` 是裸字符串数组，而轻量校验器**不支持 `anyOf`**，
把 `blockers` 改成对象数组会让旧格式直接校验失败；扩枚举则向后兼容（旧值仍是合法子集）。

### 21.3 机制侧的三处修复（不依赖模型自觉的部分）

1. **`_normalize_review` 三档分流** → 返回 `(in_material, architect, needs_external, forced_pass)`，
   `architect` 落进 `review.architect_fixes` 供审计；未知/缺失 scope 一律按 `needs_external` 处理
   （保守：不凭空触发返工，与旧契约行为一致）。
2. **路由**：`verdict == rework_architect` **或** `architect_fixes` 非空 ⇒ 下一轮回 `architect_plan`。
   之前只看 verdict，评审把方案层根因误标成实现层时就整轮打回开发（本次即此）。
   `rounds[].routed_to` 记录实际去向，日志写明理由（人工可查）。
3. **两条自相矛盾守卫**：
   - 列了方案层返工项却判 `pass` ⇒ 强制 `rework_architect`（不允许承认方案有缺陷还往下走）；
   - 只有方案层返工项时，不再被「`in_material` 为空」误判成「无可执行修改」而**强制放行**。

### 21.4 证据送达：verify 结果进开发与方案（原来只进评审）

- `parts_dev` 新增 `verify` 参数：只列**失败**的命令（成功的没有指导价值，且全部通过时整块省略），
  排在方案之后、代码之前（越靠前越不会被 `fit_prompt` 裁掉）。
- `parts_plan` 新增 `verify` / `prev_plan` / `impl`：**回退到方案时，方案要能看到「我漏了什么」**。
- 新增 `prompts.verify_facts(report, plan, impl)`：把失败输出翻成**机械事实**（集合判定）——
  「缺少模块 X：不在方案的改动清单里，也不在本轮产出文件里」。
  **机制只算集合、不下结论**：`No module named 'X'` 在「第三方库没装」与「方案漏建本地模块」两种
  成因下长得一模一样，机械判不了；但把集合差摆出来，归属就一目了然。清单缺失时只陈述能确证的部分。

### 21.5 顺带挖出一个真 bug：traceback 的末行被截断

`distill`/`truncate_text` 是**从头截**的，而 Python traceback 的**异常类型与原因在最后一行**、
测试框架的失败汇总也在末尾。用真机数据实测（`python game_logic.py` 的 stderr）：

| | 长度 | 含 `No module named 'direction'` |
|---|---|---|
| 修复前（`truncate_text(raw[-500:], 60)`） | 207 | ❌ 结尾停在 `File "D:\AI\line\runs\20260924-1855…（已截断）` |
| 修复后（`_compact_error`：头 60 + 尾 110） | 180 | ✅ 结尾就是 `…duleNotFoundError: No module named 'direction'` |

新增 `prompts._compact_error()`：压成「开头 + 结尾」两截，总长压到 200 字符以内
（正好是 `truncate_text` 的下限，因此不会再被削）。这一条独立于归属问题 —— 任何阶段的失败
证据都受益。

### 21.6 测试

- **smoke_mock 351 断言**（本轮 +26）：三档分流与落盘、非法 scope 被契约拒绝、
  两条自相矛盾守卫（含「只有方案层项时不得强制放行」）、旧产物兼容（无明细→实现层 / 未知 scope→外部）、
  `verify_facts` 四种输入（清单没有→方案层 / 清单有→实现层 / 清单缺失→不乱断言 / 符号级缺失）、
  `_compact_error` 的末行保留与长度上限、**端到端自动回转**
  （评审判方案层 → `routed_to=architect_plan` → `stage_snapshots` 里 `architect_plan` 出现两次）、
  **集成级证据送达**（驱动 `_stage_dev` 捕获开发实际收到的 prompt：含原始报错 + 机械事实 +
  不含已成功的命令）、方案阶段与评审阶段同样带上证据。
- **smoke_console 122 断言**：页面返工项编辑器的作用域选项同步为三档。

### 21.7 已知边界

- **不做「按错误类型机械判归属」**：第三方库缺失与方案漏文件在错误信息上不可区分，
  强行机判会产生误路由；改为「机制给事实、评审给归属」。若评审仍判错，人工可在页面直接改 scope。
- **`blockers` 仍是裸字符串**（无归属）：它随 verdict 走；要给它加归属需要 `anyOf`，
  而校验器不支持，收益不足以为此改校验器。
- **方案层返工项进 `fixes` 时排在最前**：回退到方案时架构师必须逐条看到「我漏了什么」，
  只做路由不带上内容等于没给指示。

---

## 22. 机制加固这一轮（2026-09-24 第十一轮）：人工预算 / 新增文件语法 / 跨文件接口 / 入口脚本 / 返工退化

### 22.1 起因：把 run `20260924-185507` 完整复盘了一遍

那一次最终以 `needs_human` 结束（第 4 轮评审判 `rework_dev`，而 `attempt(4) > max_rework(2)`），
产物停在 `renderer.py` 只有 277 字节、`print(f'{` 未闭合的状态。逐条挖出 6 个缺口：

| 缺口 | 证据 |
|---|---|
| **人工打回只换来一轮且必然判死** | 21:54 人工 reject → attempt 3→4 → 第 4 轮评审一判返工就触发 `4 > 2` → 结束 |
| **新增文件的内容不校验** | 写残的 `renderer.py` 被判 `ok`（"新增文件：没有原文可核对，整份写入"），一路放行到 verify |
| **执行类检查会被语法错掩盖** | renderer 语法错 → game_logic 连 import 都失败 → 其它问题要等下一轮才炸（每轮 ≈5 分钟 + 一次 14B） |
| **`python x.py` 的假绿** | 第 3 轮 verify **3/3 通过**却不可玩：没有 `__main__` 时 rc=0 只代表"定义完类就退出" |
| **返工退化没人管** | 第 1 轮有 `InputHandler`，第 2/3 轮悄悄消失，直到人工实测才发现方向控制没了 |
| **触顶后页面无路可走** | `editable = phase === "paused"` → `needs_human`（status=done）的运行按钮全灰 |

### 22.2 人工介入的预算语义

`reject` 与 `--from` / `--from-checkpoint` 人工打回时，调用 `_extend_budget_for_human()`：
把上限抬到 `max(当前上限, attempt + HUMAN_REWORK_BUDGET)`（**单调不减**）。
为什么是 `attempt + N` 而不是 `+1`：`_begin_round` 会推进一格（从 `architect_plan` 回流是两格），
而触顶判据是**绝对**比较 `attempt > max_rework` —— 只加 1 的话下一轮照样立刻触顶。
**只在已触顶时才追加**，所以未触顶的正常路径行为零变化。
新增 `HUMAN_REWORK_TOPUP_MAX`（默认 3）防止无人值守下无限增长；到顶后日志提示用 `--max-rework`。

### 22.3 新增文件的语法级校验

`patches.check_new_file_content()`：对 `change_type=add` 的补丁内容做两道零成本检查 ——
括号/引号/三引号配平（手写扫描器，给人类可读定位）+ `compile()`（仅 `.py`）。
放在 `analyze_all` 的**独立一遍**里而不是 `analyze_edit`：它**与仓库无关**，
这样 repo 缺失（新建项目没给 `--repo`）时同样生效。
新增状态 `new_file_syntax_error`，并进 `_patch_blockers`（阻断级 ⇒ 评审不能给 pass）。

`issues.py` 同步两处口径：登记这两个新 kind（否则未知状态会被误标成 `patch_no_effect`），
并把 `new_file_duplicate_symbol` / `new_file_syntax_error` 一起列为 `blocker`
（原先编排器判它阻断、问题记录里却只记 `warn`，口径不一致会让人按记录误放行）。

### 22.4 跨文件接口静态审计 + 入口脚本行为断言

都在 `verify.py`，都是**零模型调用**：

- `audit_interfaces()`：ast 解析本轮产出，给出「每个文件定义了哪些符号」，
  并核对 `from M import A`（M 在本轮产出里）⇒ A 必须真实存在。
  **不依赖执行顺序，因此不被语法错短路**，一次把所有文件的 import 契约列全；
  解析不了的文件只记「无法解析」，**绝不**据此断言它缺符号。
- `entry_script_problems()`：对「`<python> 单个脚本.py`」这类命令，若脚本被判为通过
  却没有 `if __name__ == '__main__':`，明确指出「rc=0 不能证明任何行为」。
  只在命令已经 ok 时才追问；`python -m pytest` / 带参数的命令一律不碰（保守判定）。
- 两者并入 `verify_report.problems` ⇒ 一并进入 `_verify_view`、随证据链喂给开发/评审/方案。
- 另修一处误导：有该落盘的补丁却**一条都没写成**时，原先落进「没有可执行的验证命令」、
  verdict 停在 `skipped`（把失败说成了"无从验证"）—— 现在明确判 `fail`。

### 22.5 返工退化防线

`_stage_dev` 开始时记下上一轮的「文件::符号」集合，`_audit_implementation` 比对本轮：
**上一轮有、本轮没了、且 `not_implemented` / `deviations` 里一个字都没提** ⇒ 进
`vanished_symbols` ⇒ `_patch_blockers()` 收走（阻断级）。声明过就不算（可能是刻意删除）。

### 22.6 页面：触顶的运行也能救

- `editable = paused || (done && needs_human)`：**触顶后 status=done 也必须可操作** ——
  那正是「需要人工介入」的场景，否则页面上只能去命令行敲 `--from`。
- 顶部控制卡新增「回流上限 max_rework」输入（留空=沿用），随续跑请求下发；
  `server._resume` 相应透传 `--max-rework`（此前只有创建运行支持它）。
- 提示文案改成「已触顶待人工裁决：可打回某个阶段重跑，并可在下方放宽回流上限」。

### 22.7 测试

- **smoke_mock 379 断言**（本轮 +28）：预算语义（触顶才追加 / 未触顶零变化 / 追加次数封顶 /
  **[端到端]** 第 3 轮通过停在交付闸门 → 人工 reject → 自动追加预算 → 那一轮真的跑完并再次停在闸门）、
  配平扫描器（正常不误报 / 注释里的引号不误报 / 转义引号不误报 / 缺括号 / 写残的 f-string /
  非代码后缀跳过 / 语法错与空内容）、跨文件接口审计（不一致被抓出 / 定义清单 / 有文件解析不了时
  其它问题照样列出 / 绝不据解析失败断言缺符号）、入口脚本断言（无 `__main__` 被指出 / 已失败的不追问）、
  返工退化（消失被抓出 / 阻断级 / 声明过就不算）。
- **smoke_console 127 断言**（本轮 +5）：首页含「回流上限」输入与触顶可操作文案、
  续跑 `max_rework` 真的透传进子进程 argv、放宽后的上限落进 state。
- 既有 5 项断言按新契约更新：写残的新增文件**在补丁阶段**就被判出（不再靠 verify 执行），
  因此对应的「verify 执行报错原文 / 语法检查必跑项」断言迁到能落盘的交付上。

### 22.8 已知边界

- **同一轮的"语法错掩盖"只能缓解不能根除**：静态审计只覆盖 import 契约，
  属性/方法缺失（`renderer.draw_score` 不存在）仍要等运行到那一行才暴露 —— 需要类型推断，暂不做。
- **入口脚本断言是保守启发式**：只认「python + 单个 .py」这一种形态；带参数、`-m` 模块一律放过。
- **预算追加不是无限**：到 `HUMAN_REWORK_TOPUP_MAX` 次后需显式 `--max-rework`（这是刻意的刹车）。

---

## §23 本次会话移交上下文（2026-09-24 晚，含一次机器重启）

### 23.1 当前运行快照：20260924-185507（贪吃蛇·新建项目）

- 状态：`status=running`、`cursor=dev`、`attempt=10`（续跑前卡在 9）、`max_rework=12`、`needs_human=False`。
- 链路：新建项目 → intake(需求补强) → PM → 检索 → 架构师方案 → 开发 → 测试 → verify → 评审 →
  人工审核（**无** architect_assess，新建项目本就跳过）。
- **它仍在跑**：重启后我通过接口 `POST /api/runs/20260924-185507/resume`（body `{}`）续跑成功，
  `llama-server`（qwen2.5-coder-7b-dev-24k）实测在推理、`traces.jsonl` 持续增长。
- 预期归宿：再跑 1~2 轮撞 `max_rework=12` → 自动 `needs_human` 停住，等人工（打回某阶段或放宽上限）。
- 服务：重启后是 `python -m pipeline.server --port 8787 --no-browser`（PID 19836，监听 127.0.0.1:8787）；
  Ollama 三个 tag 均在（qwen3-8b-pm-16k / qwen3-14b-arch-8k / qwen2.5-coder-7b-dev-24k）。

### 23.2 之前那次"为啥就结束了"的真相（已定位，未改代码）

**评审自指循环 = 永不收敛根因**，不是模型真跑不通：

1. 铁证：attempt 7、8 的 `verify` 5 条命令全绿（`py_compile` + `import` 全过）、`mechanical_blockers=0`，
   评审却仍报「`renderer.py` 未闭合 f-string 语法错误」——与机械证据直接矛盾。
2. 机制：`_step_review` 把上一轮 `blockers` 拼进 `self.fixes`（`orchestrator.py:1630`），
   评审提示词又用「上一轮已提出的修复项（检查是否真的解决了）」把这份 fixes 喂回（`prompts.py:1095`）。
   评审拿旧 blocker 当锚点照抄 → 永远 `rework_dev`，无法收敛。

### 23.3 待办改进点（用户要求"先不动手"，仅列此清单）

1. **重启/崩溃恢复（本次直接踩中，优先级最高）**：
   无活进程但 `state.status=running` → 前端 `runPhase`（`console.html:772`）只看 `state.status`，
   把运行判成"运行中"：`btn-resume` 置灰、`btn-stop` 可点却无进程可停 → UI 死路，只能 API/命令行续跑。
   建议：服务启动期扫描"有 state 但无对应活进程"的运行，把 `running` 降级为 `paused`（或加 `stale` 相位允许续跑）。
2. **评审反自指**：语法/导入类 blocker 必须附**可定位证据**（行号/报错原文）；对本轮机械证据已证伪的旧
   blocker 显式剔除或标注「已由 verify 证伪，勿重复引用」，而不是原样塞回 `fixes`。
3. **dev 重写退化**：attempt 3 已 pass，第 4 轮却把 `renderer.py` 写残、`__main__` 入口丢失（"越改越少"）；
   虽已有 `new_file_syntax_error` / `vanished_symbols` 两道防线（`§22.3`、`§22.5`），但当时尚未生效（是后加的）。
4. **小问题**：`rounds[].routed_to` 在 `pass` 时也记成 `"dev"`（误导）；`console.log` 走 stdout 块缓冲（8KB），
   运行中看尾日志会滞后（看 `traces.jsonl`/`llm-calls.jsonl` 的 mtime 更准）。

> 已闭环、不用再动的部分：`entry_script_problems` 库模块入口探测已从"判失败"改为"只提示"（`§22.4`），
> attempt 7/8 的 `mech=0` 即证明生效。

### 23.4 如何继续

- 看状态：`Invoke-RestMethod -Uri http://127.0.0.1:8787/api/runs/20260924-185507`
- 续跑/打回：`POST /api/runs/20260924-185507/resume`（body 可选 `from`/`feedback`/`max_rework`/`pause_after`），
  或直接命令行 `cd D:\AI\line && python -m pipeline.cli --resume 20260924-185507`。
- 关键文件行号：评审提示词 `prompts.py:1044 parts_review`（fixes 注入在 `:1095`）；
  fix 列表污染源 `orchestrator.py:1630`；运行态相位 `console.html:772 runPhase`、续跑按钮 `:988`；
  入口探测 `verify.py:307 entry_script_problems`。
- 测试：mock 端到端 `tools/smoke_mock.py`、控制台 `tools/smoke_console.py`（增量编辑 CONTEXT.md，勿整份覆盖）。
