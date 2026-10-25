# 变更记录

## v0.6.2（2026-10-25）

修一个**只在老库上才会炸**的迁移 bug —— v0.6.1 上线时线上库直接初始化失败，就是它。

- **现象**：容器重启后日志出现
  `[feiyu-memory] 记忆库初始化失败：no such column: version`，插件的记忆库整个没起来（后台循环还在跑，但库是空的）。
- **根因**：v0.6.0 把 `CREATE INDEX IF NOT EXISTS idx_embeddings_meta ON memory_embeddings(model, version, dim)` 写进了 `_SCHEMA`。`_SCHEMA` 在 `_migrate()` **之前**执行，而 `CREATE TABLE IF NOT EXISTS` 不会给已存在的老表补列 —— 老库的 `memory_embeddings` 还没有 `version` 列，于是建索引这一步报 `no such column: version`，整个 `MemoryStore.__init__` 抛异常。
  新建的空库有 `version` 列，所以所有自测都是绿的，**测试没盖到真实世界**。
- **修法**：把这条索引从 `_SCHEMA` 移出，只在 `_ensure_embedding_meta()` 里 `ALTER TABLE ... ADD COLUMN version` 之后再建（原来那里就有一份，重复的那份删掉），并加注释说明为什么不能写回 `_SCHEMA`。
- **新增回归 `tests/selftest_migrate.py`（18 项）**：先造一个「v0.5.0 形态」的老库（去掉 `memory_embeddings.version`、`memory_records.op_hash`、三张新表与新索引），再用新代码打开它，断言：能初始化、补齐列/表/索引、老记录可读且能检索、`vector_health` 仍统计得到、迁移可重入；并静态断言 `_SCHEMA` 里不再有那条索引。
- 六套自测：58 / 49 / stage4 20 组 / 42 / 61 / 18 全绿。

## v0.6.1（2026-10-25）

只修两个「明明配好了却不认」的操作坑，不动检索算法。

- **嵌入服务商负结果不再缓存到进程结束**：`_ensure_embedding()` 改成只有真探到服务商才置 `_embedding_checked = True`；探不到就记 `_embedding_next_try = time.monotonic() + 60` 并在 60 秒后自动重试。
  **改前**：面板里刚配好嵌入服务商，插件到重启前一直当「没有嵌入可用」，语义通道静默消失。
- **新增 `/feiyu 向量 重建`**：`_backfill_vectors(limit=200, rebuild=True)` 会把型号 / 版本 / 维度对不上的旧向量一并重算（用 `vector_health` + `list_stale_embeddings` 取待办，`todo = stale + missing` 去重），回复里报「补了 N 条，重算 M 条」。
- `/feiyu 状态` 的 stale 提示改为指向 `/feiyu 向量 重建`（原来只提示「待重建」但没告诉人怎么重建）。
- 帮助文本加一行：`/feiyu 向量 重建 — 换了嵌入模型后，把旧向量全部重算`。

检索通道、操作协议、反思两段式与 v0.6.0 完全一致；五套自测 58 / 49 / stage4 20 组 / 42 / 61 全绿。

## v0.6.0（2026-10-25）

上一版解决了「记错人」，这一版解决「想不起来」和「改错行」。同样是每一条先写成回归测试、看着红再改。

- **真混合检索**（原 M2 第 1 项）。以前 `search()` 先用 FTS 取词法候选，再在候选里算 cosine——语义通道被词法卡死：库里有「主人钟爱猫咪」，用 `feline` 查是 **0 候选**。现在两条通道各自召回：词法走 FTS 索引，语义 `_semantic_candidates()` 自己扫全表向量（过 `_fetch_authorized` 鉴权，不越权），两路合池后用 RRF 融合 `Σ 1/(60 + rank)` 排序。`_score` 与 `min_score` 仍是准入门槛（没有放松），RRF 只决定顺序；只有一路命中时退化为该路排序，没有查询向量时行为与旧版一致。
- **向量型号校验**（第 2 项）。`memory_embeddings` 加 `version` 列与 `idx_embeddings_meta(model, version, dim)`；`put_embedding()` 同时写 `memory_records.embedding_model / embedding_version`。型号、版本、维度任一不符就不参与打分并计入 `vector_mismatch`——以前换了嵌入模型，旧向量还会被当成可比值照算 cosine。新入口 `vector_health()` / `list_stale_embeddings()`，`/feiyu 状态` 会显示「N 条型号或版本不符待重建」，`/feiyu 向量` 补建。
- **操作协议**（第 3 项）。`add_record` 增加 `op_hash = _payload_hash(...)`：同 `op_key` 同内容 → 幂等返回旧记录；同 `op_key` 不同内容 → 报「操作冲突」，不再悄悄认回旧记录（以前这条路是静默的错误来源）。`correct_record(..., expected_revision=N)` 支持 CAS：修订号不符直接报「记录已被改写（期望 rN，实际 rM）」，两个进程同时改同一条不会再互相覆盖。
- **反思两段式**（第 4 项）。`run()` 拆成 `propose()` → `apply()`：提案阶段只算不写（新增 `memory_proposals` / `memory_proposal_items` 两表，带 manifest），应用阶段逐项执行并落回执 `status / reason / result_id`。应用时**再核一次**来源是否仍有效（`validate_sources` 带 revision）、内容是否撞墓碑；期间被忘记的来源会把整项丢掉并写进回执（「来源失效：…」/「来源内容已被主人遗忘」）。重复 `apply` 返回 0，可以放心重试。
- **检索 trace**（第 5 项）。`search()` 的 stats 现在报告 `mode / lexical_candidates / semantic_candidates / matched / injected / rrf / truncated / truncated_reason`，`build_context` 原样透传——「她为什么没想起来」现在能回答，而不是靠猜。
- `/feiyu 整理` 会先把上一轮还挂着的提案应用掉，再出这一轮的新提案；`/feiyu 状态` 多一行「待受理的整理提案」。
- `_conf_schema.json` 新增 `semantic_min`（语义通道余弦门槛，默认 0.30）。
- 新增回归套件 `tests/selftest_recall.py`（61 项 / 7 组）：语义独立召回、词法命中不被挤掉、RRF 公式与排序、向量型号校验与重建、操作协议、两段式与逐项回执、trace 透传。

已知未修（M3 起）：陪伴运行时与权限/预算还没有；跨插件提案端口只有 memory 侧的存储形态，尚无 companion 侧调用方。

## v0.5.0（2026-10-25）

「记得对」比「记得多」重要。这一版不增加任何新功能，只修四类会让她记错人的缺陷——每一项都先写成回归测试，看着它红，再改代码让它绿。

- **证据必须属于主人**（P0）。以前自动提取会把「主人：…\n助手：…」拼成一段 transcript，只确认证据在整段里存在，不区分谁说的——主人只说「随便聊聊」、助手顺着说「主人养了企鹅」，后一句就能被当成主人的事实写进库。现在 `capture_turn` 存结构化的 `turns=[{role, text}]`，证据必须落在**主人那条消息**里逐字命中；只落在助手行 → 拒收（「证据属于助手自述」）。另加一条独立检查：候选内容若只出现在助手嘴里而主人没说过（`_content_borrowed`），同样拒。`memory_evidence` 新增 `speaker / content_hash / span_start / span_end` 四列，证据从此可追溯到具体那条消息的具体位置。老库按 `PRAGMA table_info` 自动补列。
- **取消模糊证据**（P0）。`_evidence_supported` 原来在子串失败后允许「二元组覆盖率 ≥0.8」，于是「我不喜欢周末去海边骑自行车」可以拿「我喜欢周末去海边骑自行车」当证据——差一个「不」字，意思完全相反。现在只保留「归一空白后逐字子串」这一条路，新增 `_locate_span` 把命中位置还原成原文坐标。`tokenize` 从本模块的 import 里去掉了。
- **相似不等于重复**（P0）。提取侧：逐字一致才算「重复」；相似度 ≥0.6 但极性相反 → 写 `extract_conflict` 审计并判「待确认冲突」（原来的 0.85 门槛拦不住 0.786 的「喜欢/不喜欢」）。整理侧：`owner_asserted` 只按逐字归一一致合并，其余要求「重叠度达阈值**且**极性相同」，重叠 0.92 但一肯一否的两条都留着，写 `reflect_conflict` 审计。
- **多层失效 + 旧证据抑制 + 落库前复核**（P0）。新增 `memory_edges(child_id, parent_id)` 依赖边表（`add_record` 写 `derived_from_ids` 时同步落边，启动时从旧库的 `derived_from_ids` 幂等回填）。`invalidate_derived` 改成带 `visited` 的广度优先遍历（`max_depth=8`）：A→B→C 现在会一路断到 C，环不会卡住，共享子图只处理一次；旧库没有边时用 `LIKE` 兜底。新增 `MemoryStore.tombstoned()`：主人要求忘掉的内容，同一段旧对话再被提取一次也写不回来（`tombstone_block` 审计）。新增 `MemoryStore.validate_sources()`：反思归纳落库前复核来源仍在、仍 active、revision 没被改写，期间被改过就整簇丢弃。
- **新增回归套件** `tests/selftest_trust.py`（42 项 / 11 组）与测试入口 `tools/run_selftests.py`（逐套件独立子进程，自动把 `tempfile.mkdtemp` 指到工作区内——沙箱里 0o700 临时目录不可用）。
- 翻转一条旧期望：`selftest_stage3.py` 里「近义改写能定位」改成「改写不再冒充原文」。它断言的正是本版修掉的 bug。

已知未修（M2）：语义向量仍只是词法候选的**内部重排**，不是独立召回；`op_key` 幂等不校验 payload；`correct_record` 没有 CAS。

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
