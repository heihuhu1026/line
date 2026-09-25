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
