### 16.5 两遍开发实验（治「实现偏浅」）—— 结论：结构可行，但 7B 填不进逻辑

用户选「两遍开发」治理 §16.2 偏浅。已实现并真机验证（见 `runs/twopass4-20260923`、`runs/twopass5-20260923`）：

- 机制：`config.DEV_TWO_PASS=True` 开启；`prompts.parts_dev` 按 `dev_pass=2/3` 输出两遍任务（第一遍只铺辅助函数、第二遍带第一遍产物回填主函数体）；`orchestrator._stage_dev` 调两次 dev 并由 `_merge_dev` 合并（edits 按 `(path,target_symbol,patch_mode)` 去重、后写覆盖）。
- 真机暴露并修复两个问题：① 两遍拆分后模型丢失 anchor 纪律（自造 `def index_all(path):` 而非真实签名）→ 全 `anchor_not_found`；修复＝dev 系统提示强化「逐字抄完整 def 首行」＋两遍统一用源码真实存在的 `INDEX_ALL_ANCHOR` 作锚点＋pass2 严令只输出 index_all 一个 edit、禁重复定义辅助函数；② 合并把 pass1 坏锚点 edit 与 pass2 重定义混入 → 去重键加入 `patch_mode`。锚点问题已解决：twopass4/5 的 `anchor 在原文里找不到` 从 5 降到 0，补丁类问题归零。

**根因不在结构，在 7B 容量**：twopass4、twopass5 的 `_index_batch/_split_key/_normalize_entry` 全是 `# 逻辑` + `pass` 空壳，index_all 仅 39~44 行骨架且调用空壳；即便显式禁令 `pass`/`# 逻辑` 占位，7B 仍写空壳——填不出原 103 行函数的真实逻辑。评审据此判 rework_dev，两轮后 `needs_human=True`。

反观**单遍基线 `20260923-065850` 首轮 pass、0 阻断**：它自然选了 7B 能完成的「加几个小而独立辅助函数（schema/SQL 检查）」，**绕开了大循环重写**。即：

> 「实现偏浅」是 **7B coder 的容量天花板**，非流水线结构问题。两遍把重构形状摆对，但 7B 无能力填逻辑；单遍能过，恰恰因它没碰那个 103 行大循环。

**结论/建议（待拍板）**：两遍代码保留（开关默认 True），机制正确，锚点修复对单遍也有益（已留在 `SYSTEM["dev"]`）。真要更深实现须换**更大 coder 模型**（如 14B coder，dev 单文件 2600 token 足以看到完整函数）；7B 上两遍反劣于单遍（出空壳→打回）。短期可选：(a) 关 `DEV_TWO_PASS` 回单遍（已知可过、偏浅但诚实）；(b) 改两遍策略为「加小而可完成辅助函数 + 在 index_all 小插入调用」，而非 full_symbol 重写大循环。

| `twopass5-20260923` | 新 + 两遍开发(禁空壳) | needs_human（评审两轮打回） | 锚点修复有效；但 7B 出空壳，偏浅是容量问题 |
