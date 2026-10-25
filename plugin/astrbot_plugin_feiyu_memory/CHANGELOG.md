# 变更记录

## v0.4.0（2026-10-25）

语义通道真正接上，并且开始「回看」已有的记忆——不只是往里写。三件事都按「宁可不动，也不要写错」的原则做。

- **向量通道可用**：新增 `_backfill_vectors()` 与后台补齐循环（启动 90 秒后跑一轮，之后每 30 分钟补缺）。以前 `memory_embeddings` 一直是空的，因为只在写入新记录时才有机会存向量，历史记录永远补不上；现在能补齐、能看覆盖率（`/feiyu 向量` 手动触发，`/feiyu 状态` 显示「已覆盖 x/y」）。没有配置嵌入模型时不再静默，注入里会明说。
- **记忆整理（阶段四）**：新增 `memory_reflect.py`。每晚（`reflect_hour`，默认 4 点）回看近 `reflect_days` 天的记录，做两件事——① 删重复：同类别、说法几乎一样的归档成一条（`merge_records`，证据并入保留的那条，不产生新内容）；② 归纳：说法相近但零散的几条，交给模型合成一句更完整的（如「爱川菜」「吃火锅」「不吃甜」→「口味偏辣、怕甜」）。三条硬约束：只能引用输入里出现过的条目（`use` 至少要两个且在组内，合成句与来源几乎无重合就判为编造直接丢）、必须写 `reflect_pass` 审计、拿不准就什么都不改。归纳出的记录是 `derived`，只会取代同样来自观察的旧条目，**不会覆盖主人亲口说过的话**（撞上就放弃并计数）。`/feiyu 整理` 可手动触发。
- **写入幂等**：`add_record` 新增 `op_key`，`memory_records(op_key)` 加部分唯一索引。自动提取与整理重跑、消息重投都不会写第二条；不再靠 0.85 相似度去猜——那会误杀「今天吃了麻辣香锅」和「今天吃了酸菜鱼」这种本该并存的记录。证据表同时按 `(record_id, kind, ref)` 去重。
- **让模型能追问**：注册 `feiyu_recall` 工具（`recall_tool_enable`）。模型觉得上下文里的记忆不够时，可以自己再查一次旧记忆；每轮最多一次，返回结果明确标注「只作参考，不要整段复述」，查不到也不代表没有。参数只有 `query`，条数与门槛由配置固定，模型不能自己放大。
- 分组的现实约束：单值唯一索引保证同一 `fact_key` 只会有一条 active，所以「同主题多条」在库里本来就散在不同 `fact_key` 上，整理只能按内容相似度聚类（`reflect_fold`，默认 0.45），不能按 `fact_key` 分组。
- `_conf_schema.json` 新增 13 键：`reflect_enable` / `reflect_hour` / `reflect_days` / `reflect_batch` / `reflect_min_records` / `reflect_min_fold` / `reflect_fold` / `reflect_group_limit` / `reflect_dedupe` / `reflect_model` / `recall_tool_enable` / `recall_tool_limit` / `recall_tool_min_score`。
- `stats()` 增加 `vector_ready` / `covered` / `vector_missing`；`/feiyu 状态` 增加索引与向量两行。
- 自测扩到 20 组：新增幂等、`merge_records`、整理删重复、整理归纳、编造被丢、主人原话不被覆盖、向量辅助方法。

## v0.3.0（2026-10-25）

检索层重做。权威表 `memory_records` 仍然只有一个真相，新增的 `memory_search` 只是派生索引：随时可删、可重建，写错也只是少召回，不会改到记忆本身。

- 新增 SQLite FTS5 派生索引（`memory_search`）。每条记录写入 / 纠正 / 忘记 / 取代时同步维护；`memory_records` 的触发器不需要，索引条数与 active 条数不一致时启动自动重建。老版本 SQLite 没有 FTS5 就静默退回全表扫描（`/feiyu 状态` 会显示当前走哪条路）。
- 候选截断从「按重要度和时间取最近 400 条」改成「按相关度取前 N 条（默认 2000，可配 `candidate_limit`）」。这是一次静默丢失的修复：以前库一过 400 条，旧记忆就再也进不了候选，而且没有任何提示。现在被截断会在注入里明写一行。
- 打分加入 idf 权重：`花生` 这类生僻词比 `代码` 这类口头禅更有分量，常见词不再能把分数顶满。
- 排序后过 MMR（`mmr_enable` / `mmr_lambda`，默认开、0.7）：说同一件事的多条记忆不会把名额占满。相似度用分词 Jaccard，不依赖向量。
- 单值语义落到库层：`(owner_id, fact_key, scope_type, scope_id)` 上加了部分唯一索引（只覆盖 `profile/preference/fact/relationship` 且带 `fact_key` 的 active 行）。以前靠 `add_record` 自觉取代，绕过 API 或并发写入就能造出两条「主人最喜欢 X」；现在库里直接拒绝，启动时还会把历史上已有的重复修成一条（留最新的，写 `single_value_repair` 审计）。
- 自动提取的重复检测不再只比最近 200 条，改用 `find_similar()`（索引找候选 + 逐条相似度）。
- 自动提取撞上主人本人确认过的同主题记录时，不再静默丢弃：写一条 `extract_conflict` 审计，`/feiyu 状态` 会显示「待确认冲突」条数。
- 降级不再隐形：没有可用的嵌入模型时，注入里明说「语义召回未启用，本轮只用关键词检索」（以前只是 debug 日志，模型根本不知道自己少了语义通道）。
- 新指令 `/feiyu 维护`：手工重建检索索引，记忆本身不动。
- `SCHEMA_VERSION` 1 → 2；`stats()` 增加 `index_ok` / `index_rows` / `conflicts`。
- 新增 `_analyze/mem_selftest.py` 式的自测覆盖：索引同步、FTS 命中、idf、MMR、单值约束、忘记后出索引、重建、启动自愈、3000 条规模、截断上报、群聊隔离。

## v0.2.0（2026-10-24）

- 阶段三：工作状态（`kind=state`，默认 7 天有效期，到期自动归档）、关系日记（每天 23:30，`derived`，只私聊注入，源记录失效时连带失效）、后台自动提取（默认关闭；每 6 轮或空闲 5 分钟一批，独立 JSON 契约，代码侧校验证据必须原文可定位、置信度 ≥0.7、敏感词拒收、日限 40 条，只写 `observed_dialogue`）。
- 新增指令：`/feiyu 当前 <在做什么>`、`/feiyu 日记`、`/feiyu 写日记`、`/feiyu 提取`。
- 新增配置 14 项：`state_max_chars`/`state_ttl_days`、`diary_*`、`extract_*`、`allow_auto_public` 等。
- `memory_jobs` 增加 `attempts`、`dedupe_key` 与部分唯一索引；`deletion_epoch` 推进后旧任务自动作废。
- 新增 `memory_stage3.py` 与 `tests/selftest_stage3.py`（49 项）。

## v0.1.0（2026-10-24）

- 阶段一/二：主人绑定（本地 CLI）、分层记忆注入、显式记住/查看/纠正/忘记、证据链与删除墓碑、中文关键词 + 可选向量混合检索、备份与恢复。
