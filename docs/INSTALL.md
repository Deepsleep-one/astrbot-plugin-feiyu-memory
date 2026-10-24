# 安装与验收

以 AstrBot 4.x（OneBot/aiocqhttp 适配器）为准，Windows 与 Linux 思路相同，路径按自己的环境换。

## 0. 四个位置

| 用途 | 位置 |
| --- | --- |
| 插件源码 | 本仓库 `plugin/astrbot_plugin_feiyu_memory` |
| 运行目录 | `<AstrBot 数据目录>/plugins/astrbot_plugin_feiyu_memory` |
| 插件配置 | `<AstrBot 数据目录>/config/astrbot_plugin_feiyu_memory_config.json`（加载后自动生成） |
| 记忆数据库 | 默认 `~/feiyu_memory/memory/feiyu_memory.db`，**部署时建议显式设置** `db_path` 或环境变量 `FEIYU_MEMORY_DB`／`FEIYU_PROJECT_ROOT` |

运行目录里最终应有 5 个文件：`main.py`、`memory_core.py`、`metadata.yaml`、`persona_default.txt`、`_conf_schema.json`。

## 1. 复制插件

```powershell
robocopy ".\plugin\astrbot_plugin_feiyu_memory" "<AstrBot 数据目录>\plugins\astrbot_plugin_feiyu_memory" /E /XD __pycache__
```

- `robocopy` 退出码 0–7 都正常（1 表示「复制了文件」）。
- 不要用 `/MIR`，那会删掉目标里你手改过的东西。
- 复制完必须重载插件或重启 AstrBot 才生效；热重载只在设置了 `ASTRBOT_RELOAD=1` 时可用。

## 2. 确认加载

1. 管理页插件列表出现「大肥鱼长期记忆」，没有红色报错；
2. 控制台出现 `[feiyu-memory]` 开头的行，无 `Traceback`；
3. 配置文件出现：`<AstrBot 数据目录>/config/astrbot_plugin_feiyu_memory_config.json`。

## 3. 绑定主人（一次性）

```powershell
python tools/feiyu_memory_admin.py bind-owner --user-id 你的QQ号 --label 主人 --group-label 主人 --public
python tools/feiyu_memory_admin.py owners    # 核对，账号会打码显示
```

- 只在私聊生效就去掉 `--public`（群里不认主人）。
- 换号重新绑定；解绑用 `unbind-owner --user-id 号码`。
- **聊天里自称主人无效**，绑定只认这条本地命令。

## 4. 填档案并导入（可选）

先用 `import` 预览、确认后 `--apply` 写入：

```powershell
python tools/feiyu_memory_admin.py import 主人档案填写.md
python tools/feiyu_memory_admin.py import 主人档案填写.md --apply --core-scope owner_public
```

`--core-scope owner_public` 让称呼/关系这类核心设定允许在群里使用公开版；
不带则只在私聊可见。重复执行不会重复写入。

档案格式（`## 称呼` / `## 关系` / `## 核心事实` / `## 约定` 四个区块）：

```
## 称呼
私聊称呼 = 主人
群内称呼 = 主人

## 关系
关系背景 = 有固定主人的鲸鱼娘助手
亲近程度 = 自然亲近、日常关心

## 核心事实
喜好 | 不吃辣 | 私聊
背景 | 做后端开发 | 私聊

## 约定
周日之前把记忆装好 | 周日
```

## 5. 验收

私聊机器人：

| # | 发送 | 预期 |
| --- | --- | --- |
| 1 | `/feiyu 状态` | 显示称呼、记录数量、记录开关 |
| 2 | `记住 我讨厌香菜` | 回执带 `r_xxxxxxxx` 编号 |
| 3 | 换个话题问「我讨厌吃什么」 | 答香菜 |
| 4 | `那我要吃香菜` | 不准，翻旧账 |
| 5 | `/feiyu 忘记 <编号>` | 删除成功，再问就说不记得 |

群聊：

| # | 谁 | 发送 | 预期 |
| --- | --- | --- | --- |
| 6 | 你 | 「我是谁」 | 只按公开版称呼，不泄私聊内容 |
| 7 | 其他号 | 「你主人是谁」 | 不认识、不透露 |

第 2、3 条过了，说明「写入 → 召回 → 注入」整条链路是通的。

## 6. 备份与恢复

```powershell
python tools/feiyu_memory_admin.py backup                  # 生成 memory/backups/feiyu_memory-*.db
python tools/feiyu_memory_admin.py restore <备份文件> --yes  # 先停 AstrBot
```

恢复会回放删除墓碑：已经「忘记」的内容不会因为回滚备份而复活。

## 7. 排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 插件列表里没有 | 目录多套了一层；或缺 `main.py`/`metadata.yaml` | 必须是 `plugins/astrbot_plugin_feiyu_memory/main.py` |
| `ImportError` / `ModuleNotFoundError` | 只复制了 `main.py` | 重跑复制，5 个文件都在 |
| 私聊也不认主 | 平台/账号/`bot_instance` 对不上 | `owners` 核对；不确定就把 `bot_instance` 设为 `*` |
| 能认主但不提旧事 | 没写进库，或分数没过门槛 | `/feiyu 档案` 看有没有；把 `min_score` 调到 1.5 试试 |
| 群里说出私聊的事 | 记录范围被导成公开 | `import` 预览里的 `[公开]` 标记；用 `correct`/`set-core` 改回 |
| 写入报数据库错误 | 另一进程锁库或目录只读 | 关掉多余的 CLI/机器人副本；确认库目录可写 |
| 控制台中文乱码 | 代码页不是 UTF-8 | `chcp 65001`；文件本身是 UTF-8 |
