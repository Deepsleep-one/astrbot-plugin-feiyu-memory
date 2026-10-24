# 变更记录

## v0.2.0（2026-10-24）

- 阶段三：工作状态（`kind=state`，默认 7 天有效期，到期自动归档）、关系日记（每天 23:30，`derived`，只私聊注入，源记录失效时连带失效）、后台自动提取（默认关闭；每 6 轮或空闲 5 分钟一批，独立 JSON 契约，代码侧校验证据必须原文可定位、置信度 ≥0.7、敏感词拒收、日限 40 条，只写 `observed_dialogue`）。
- 新增指令：`/feiyu 当前 <在做什么>`、`/feiyu 日记`、`/feiyu 写日记`、`/feiyu 提取`。
- 新增配置 14 项：`state_max_chars`/`state_ttl_days`、`diary_*`、`extract_*`、`allow_auto_public` 等。
- `memory_jobs` 增加 `attempts`、`dedupe_key` 与部分唯一索引；`deletion_epoch` 推进后旧任务自动作废。
- 新增 `memory_stage3.py` 与 `tests/selftest_stage3.py`（49 项）。

## v0.1.0（2026-10-24）

- 阶段一/二：主人绑定（本地 CLI）、分层记忆注入、显式记住/查看/纠正/忘记、证据链与删除墓碑、中文关键词 + 可选向量混合检索、备份与恢复。
