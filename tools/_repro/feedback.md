人类审核意见（对 PM 的 unknowns / clarifying_questions 的答复，均为已核实的存量事实）：

1. 现有库结构（卡片检索器.py 第 217-240 行的 SCHEMA）：
   - cards(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name, dir, size INTEGER, mtime REAL, spec, tags, has_builtin INTEGER DEFAULT 0, blocks INTEGER DEFAULT 0)
   - blocks(id INTEGER PRIMARY KEY, card_id INTEGER, kind, label, content TEXT) + idx_blocks_card / idx_blocks_kind / idx_cards_path
   - 没有任何 run 表或 meta 表，需要新增。
2. 已经有「加列式迁移」的先例可复用：ensure_hash_column(conn) 用 PRAGMA table_info 判断后 ALTER TABLE 加列。新增列请沿用同样方式，不要重建表、不要动 blocks 表结构。
3. 失败原因只需在卡记录上存一列简短的 TEXT 错误串（如 "OSError: ..."/"nocard"），不需要单独的失败明细表。
4. 「文件内容变化」继续沿用 (size, mtime) 判断；「上次处理失败」由新增的状态列区分，两者不要混在一个字段里。
5. db_open 里已经有 conn.executescript(SCHEMA) 与 PRAGMA journal_mode=WAL / synchronous=NORMAL。WAL 下不要改成每张卡一次 fsync，批量提交频率维持现状（batch 默认 300）。
6. GUI 已有后台线程 + queue 的进度通道（_index_worker / _drain_queue），「上次异常退出」的提示请走同一条状态栏通道，不要新开弹窗流程。

