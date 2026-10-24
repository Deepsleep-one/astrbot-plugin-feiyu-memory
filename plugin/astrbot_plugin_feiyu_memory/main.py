"""大肥鱼长期记忆联动 · AstrBot 插件

阶段一 + 阶段二 + 阶段三：
- 主人身份由本地管理 CLI 绑定（聊天自称无效），插件只读取绑定结果；
- 每轮按“场合 + 身份”先做权限过滤，再注入[后端核验的交互状态]与[记忆参考资料]；
- 私聊主人可显式记忆、查看、纠正、忘记；群聊只注入公开版；
- 记录带证据与删除墓碑，忘记后不复活；
- 检索失败/超时只丢失“旧记录”，不会丢失主人身份；
- 阶段三：工作状态（带有效期）、每天 23:30 的关系日记、按批后台提取
  （默认只在私聊、只写 observed_dialogue、代码侧校验证据）。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from .memory_core import (
    CORE_SECTIONS,
    EPISTEMIC_TAG,
    MemoryError,
    MemoryStore,
    compact,
)
from .memory_stage3 import Stage3

PLUGIN_NAME = "astrbot_plugin_feiyu_memory"

CAPTURE_PATTERNS = [
    re.compile(r"^(?:记住|记一下|记着|帮我记住|帮我记一下|别忘了|你要记得)[：:，,。\s]+(.+)$", re.S),
    re.compile(r"^(?:记住|记一下)[：:]\s*(.+)$", re.S),
]

KIND_BY_KEYWORD = [
    ("commitment", ("约定", "答应", "说好", "提醒我", "别忘")),
    ("preference", ("喜欢", "讨厌", "不爱", "爱吃", "不吃", "怕", "习惯", "偏好", "口味")),
]


@register(
    PLUGIN_NAME,
    "大肥鱼项目",
    "大肥鱼长期记忆联动：主人身份核验、分层记忆注入、显式记忆与纠正/忘记、证据与备份，附工作状态、关系日记与后台提取。",
    "0.2.0",
    "https://github.com/Deepsleep-one/astrbot-plugin-feiyu-memory",
)
class FeiyuMemoryPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        self._store: MemoryStore | None = None
        self._store_error: str = ""
        self._persona_cache: str = ""
        self._embedding = None
        self._embedding_checked = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._stage3: Stage3 | None = None
        self._pending_turns: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        if self._cfg_bool("enabled", True):
            try:
                store = await asyncio.to_thread(self._store_sync)
                logger.info(
                    "[feiyu-memory] 记忆库已就绪：%s（可用记录 %s 条）",
                    store.db_path,
                    store.stats().get("active"),
                )
            except Exception as exc:  # 记忆不可用不能拖垮聊天
                self._store_error = str(exc)
                logger.error("[feiyu-memory] 记忆库初始化失败：%s", exc)
        if self._cfg_bool("enabled", True) and (
            self._cfg_bool("auto_extract", False)
            or self._cfg_bool("diary_enable", True)
        ):
            task = asyncio.create_task(self._stage3_loop())
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
            logger.info(
                "[feiyu-memory] 阶段三循环已启动（auto_extract=%s, diary=%s）",
                self._cfg_bool("auto_extract", False),
                self._cfg_bool("diary_enable", True),
            )

    async def terminate(self) -> None:
        for task in list(self._bg_tasks):
            task.cancel()
        if self._store:
            try:
                self._store.close()
            finally:
                self._store = None

    # ------------------------------------------------------------------ #
    # LLM 请求注入
    # ------------------------------------------------------------------ #

    @filter.on_llm_request()
    async def inject_memory(self, event: AstrMessageEvent, req) -> None:
        if not self._cfg_bool("enabled", True):
            return
        store = self._safe_store()
        if store is None:
            return
        try:
            ident = self._identity(event)
            receipt = ""
            if ident["is_owner"] and ident["scene"] == "private":
                key = str(event.get_session_id() or ident["user_id"])
                self._pending_turns[key] = {
                    "user": event.get_message_str(),
                    "ident": ident,
                }
                if len(self._pending_turns) > 50:
                    for old_key in list(self._pending_turns)[:10]:
                        self._pending_turns.pop(old_key, None)
            if (
                ident["is_owner"]
                and ident["scene"] == "private"
                and self._cfg_bool("auto_capture_owner_private", True)
                and store.capture_enabled()
            ):
                receipt = await asyncio.to_thread(
                    self._capture_owner_statement, event, ident
                )

            timeout = max(0.2, self._cfg_float("search_timeout_ms", 1200) / 1000.0)
            vector = await self._query_vector(event.get_message_str(), timeout)
            kwargs = dict(
                is_owner=ident["is_owner"],
                scene=ident["scene"],
                user_id=ident["user_id"],
                group_id=ident["group_id"],
                owner=ident["owner"],
                query=event.get_message_str(),
                vector=vector,
                cfg=self._cfg_all(),
                receipt=receipt,
            )
            try:
                ctx = await asyncio.wait_for(
                    asyncio.to_thread(store.build_context, **kwargs), timeout=timeout
                )
            except asyncio.TimeoutError:
                logger.warning("[feiyu-memory] 检索超时，降级为核心注入")
                ctx = await asyncio.to_thread(
                    store.build_context,
                    **{**kwargs, "query": "", "vector": None,
                       "degraded": "超时，本轮只加载核心记忆"},
                )
            blocks = f"{ctx['state']}\n\n{ctx['memory']}"
        except Exception as exc:
            logger.exception("[feiyu-memory] 注入失败：%s", exc)
            return

        persona = self._persona()
        parts = []
        if persona:
            parts.append(persona)
        if req.system_prompt:
            parts.append(req.system_prompt)
        parts.append(blocks)
        req.system_prompt = "\n\n".join(parts)
        logger.debug(
            "[feiyu-memory] 注入 owner=%s scene=%s recalled=%s",
            ctx["is_owner"], ident["scene"], len(ctx["recalled"]),
        )

    @filter.on_llm_response()
    async def queue_turn(self, event: AstrMessageEvent, response) -> None:
        """回复成功后，把这一轮放进后台提取队列（不阻塞、不报错给用户）。"""
        try:
            if not self._cfg_bool("auto_extract", False):
                return
            ident = self._identity(event)
            if not ident["is_owner"] or ident["scene"] != "private":
                return
            key = str(event.get_session_id() or ident["user_id"])
            pending = self._pending_turns.pop(key, None)
            if not pending:
                return
            text = (getattr(response, "completion_text", "") or "").strip()
            if not text:
                return
            stage3 = self._stage3_instance()
            if stage3 is None:
                return
            stage3.capture_turn(
                session_id=key,
                user_text=pending.get("user", ""),
                assistant_text=text,
                ident=ident,
            )
        except Exception as exc:
            logger.debug("[feiyu-memory] 入队失败（忽略）：%s", exc)

    async def _stage3_loop(self) -> None:
        """30 秒一次：清理过期状态、回收悬挂任务、按批提取、到点写日记。"""
        try:
            while True:
                await asyncio.sleep(30)
                stage3 = self._stage3_instance()
                if stage3 is None:
                    continue
                try:
                    await stage3.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("[feiyu-memory] 阶段三 tick 失败：%s", exc)
        except asyncio.CancelledError:
            return

    def _stage3_instance(self) -> Stage3 | None:
        if self._stage3 is not None:
            return self._stage3
        store = self._safe_store()
        if store is None:
            return None
        self._stage3 = Stage3(
            store,
            self.config,
            self._llm_call,
            logger=logger,
            after_write=self._schedule_embedding,
        )
        return self._stage3

    async def _llm_call(self, system_prompt: str, user_prompt: str,
                        model: str = "") -> str:
        provider = None
        try:
            provider = await self.context.get_using_provider_async()
        except Exception:
            try:
                provider = self.context.get_using_provider()
            except Exception:
                provider = None
        if provider is None:
            raise RuntimeError("没有可用的对话模型")
        kwargs = {
            "prompt": user_prompt,
            "session_id": "feiyu-stage3",
            "system_prompt": system_prompt,
        }
        if model:
            kwargs["model"] = model
        resp = await provider.text_chat(**kwargs)
        return (getattr(resp, "completion_text", "") or "").strip()

    # ------------------------------------------------------------------ #
    # 聊天指令
    # ------------------------------------------------------------------ #

    @filter.command("feiyu", alias={"飞鱼记忆", "记忆"}, desc="大肥鱼长期记忆管理")
    async def feiyu_command(self, event: AstrMessageEvent, sub: GreedyStr):
        """大肥鱼长期记忆：/feiyu 帮助"""
        store = self._safe_store()
        if store is None:
            yield event.plain_result("记忆库没打开，先看看日志。")
            return

        ident = self._identity(event)
        text = (sub or "").strip()
        parts = text.split(maxsplit=1)
        action = parts[0].lower() if parts else "帮助"
        arg = parts[1].strip() if len(parts) > 1 else ""

        if action in ("帮助", "help", "?"):
            yield event.plain_result(self._help_text(ident))
            return

        if not ident["is_owner"]:
            yield event.plain_result(
                "这个只对已绑定的主人开放。绑定在本地管理入口做，"
                "在聊天里说“我是主人”不算。"
            )
            return

        if ident["scene"] != "private":
            yield event.plain_result(
                "记忆的事私聊再说。群里只带公开版，不在这里翻档案。"
            )
            return

        try:
            if action in ("状态", "status"):
                yield event.plain_result(self._status_text(store))
            elif action in ("档案", "profile", "查看档案"):
                for chunk in self._profile_text(store, ident):
                    yield event.plain_result(chunk)
            elif action in ("查看", "show", "详情"):
                yield event.plain_result(self._show_text(store, arg))
            elif action in ("记住", "记", "remember"):
                yield event.plain_result(
                    await asyncio.to_thread(self._remember_text, store, ident, arg)
                )
            elif action in ("纠正", "correct", "修改"):
                yield event.plain_result(
                    await asyncio.to_thread(self._correct_text, store, ident, arg)
                )
            elif action in ("忘记", "forget", "删除"):
                yield event.plain_result(
                    await asyncio.to_thread(self._forget_text, store, ident, arg)
                )
            elif action in ("撤销", "undo"):
                yield event.plain_result(self._undo_text(store))
            elif action in ("约定", "约定+", "commit"):
                yield event.plain_result(
                    await asyncio.to_thread(self._commit_text, store, ident, arg)
                )
            elif action in ("完成", "done"):
                yield event.plain_result(
                    await asyncio.to_thread(self._done_text, store, ident, arg)
                )
            elif action in ("当前", "在忙", "state"):
                yield event.plain_result(
                    await asyncio.to_thread(self._state_text, store, ident, arg)
                )
            elif action in ("日记", "diary"):
                yield event.plain_result(self._diary_text(store, ident))
            elif action in ("写日记", "diary+"):
                stage3 = self._stage3_instance()
                if stage3 is None:
                    yield event.plain_result("阶段三没就绪，看看日志。")
                    return
                res = await stage3.maybe_write_diary(force=True)
                if res.get("written"):
                    yield event.plain_result("写好了，下次私聊会带上。")
                else:
                    yield event.plain_result(f"没写成：{res.get('reason', '素材不够')}")
            elif action in ("提取", "extract"):
                stage3 = self._stage3_instance()
                if stage3 is None:
                    yield event.plain_result("阶段三没就绪，看看日志。")
                    return
                res = await stage3.process_pending(force=True)
                yield event.plain_result(
                    f"处理 {res.get('processed', 0)} 轮，新记 {res.get('written', 0)} 条，"
                    f"跳过 {res.get('skipped', 0)} 条。"
                )
            elif action in ("暂停", "pause"):
                store.set_control("capture_enabled", "0", actor="owner")
                yield event.plain_result("记录先停了，已有的照常认。发 /feiyu 恢复 再开。")
            elif action in ("恢复", "resume"):
                store.set_control("capture_enabled", "1", actor="owner")
                yield event.plain_result("记录恢复了。")
            else:
                yield event.plain_result(self._help_text(ident))
        except MemoryError as exc:
            yield event.plain_result(f"没成：{exc}")
        except Exception as exc:
            logger.exception("[feiyu-memory] 指令失败：%s", exc)
            yield event.plain_result("这边卡了一下，没写成。别当成已经存好了。")

    # ------------------------------------------------------------------ #
    # 命令实现（同步，在线程里跑）
    # ------------------------------------------------------------------ #

    def _help_text(self, ident: dict) -> str:
        head = (
            "记忆指令（仅私聊、仅已绑定主人）：\n"
            "/feiyu 状态 — 记忆库状态\n"
            "/feiyu 档案 — 我的核心设定与最近记录\n"
            "/feiyu 记住 <内容>\n"
            "/feiyu 查看 <编号>\n"
            "/feiyu 纠正 <编号> <新内容>\n"
            "/feiyu 忘记 <编号>\n"
            "/feiyu 撤销 — 撤回上一条刚存的内容\n"
            "/feiyu 约定 <内容> / 完成 <编号>\n"
            "/feiyu 当前 <在做什么> — 记一条会过期的状态\n"
            "/feiyu 日记 — 最近的关系日记\n"
            "/feiyu 写日记 — 现在写一篇\n"
            "/feiyu 提取 — 立刻处理待提取的对话\n"
            "/feiyu 暂停 / 恢复"
        )
        if not ident["is_owner"]:
            return head + "\n（当前这个账号还没绑定为主人）"
        return head

    def _status_text(self, store: MemoryStore) -> str:
        s = store.stats()
        return (
            "记忆库：{}\n主人绑定：{} 个\n核心设定：{} 条\n"
            "可用记忆：{} 条（已被取代 {}，已删除 {}）\n"
            "证据：{} 条，向量：{} 条\n"
            "当前状态：{} 条，关系日记：{} 条，待提取：{} 轮\n"
            "记录开关：{}"
        ).format(
            s["db_path"], s["owners"], s["core"], s["active"], s["superseded"],
            s["deleted"], s["evidence"], s["embeddings"],
            s.get("states", 0), s.get("diaries", 0), s.get("jobs_pending", 0),
            "开" if s["capture"] else "暂停",
        )

    def _profile_text(self, store: MemoryStore, ident: dict) -> list[str]:
        owner = ident["owner"] or {}
        owner_id = owner.get("owner_id", "primary")
        chunks: list[str] = []
        lines = ["【核心设定】"]
        core = store.list_core(owner_id)
        if not core:
            lines.append("（还没填核心设定，用本地管理工具导入）")
        for row in core:
            title = row["title"] or CORE_SECTIONS.get(row["section"], row["section"])
            scope = "私聊" if row["scope_type"] == "owner_private" else "可公开"
            lines.append(f"· {title}（{scope}）：{compact(row['content'], 80)}")
        chunks.append("\n".join(lines))

        recs = store.list_records(owner_id=owner_id, limit=12)
        lines = ["【最近记录】"]
        if not recs:
            lines.append("（空的）")
        for rec in recs:
            tag = EPISTEMIC_TAG.get(rec["epistemic_type"], "")
            scope = {
                "owner_private": "私聊",
                "owner_public": "可公开",
                "group": "群",
                "member": "群友",
            }.get(rec["scope_type"], rec["scope_type"])
            lines.append(
                f"{rec['id']}｜{rec['kind']}｜{scope}｜{tag}"
                f"{compact(rec['content'], 60)}"
            )
        chunks.append("\n".join(lines))
        return chunks

    def _show_text(self, store: MemoryStore, arg: str) -> str:
        rid = arg.strip().split()[0] if arg.strip() else ""
        if not rid:
            return "给个编号，比如 /feiyu 查看 r_abc123。"
        rec = store.get_record(rid)
        if not rec:
            return "没这条。编号可能记错了。"
        if rec["status"] != "active":
            return f"这条现在是 {rec['status']} 状态，只有管理工具能翻。"
        lines = [
            f"编号：{rec['id']}",
            f"类别：{rec['kind']}｜{EPISTEMIC_TAG.get(rec['epistemic_type'], '')}",
            f"范围：{rec['scope_type']}:{rec['scope_id'] or '-'}",
            f"内容：{rec['content']}",
            f"重要度：{rec['importance']}｜置信：{rec['confidence']:.2f}",
            f"记录于：{rec['asserted_at']}",
            f"版本：v{rec['revision']}",
        ]
        if rec["fact_key"]:
            lines.append(f"主题键：{rec['fact_key']}")
        if rec["evidence"]:
            lines.append("证据：")
            for ev in rec["evidence"][:4]:
                lines.append(
                    f"· [{ev['kind']}] {compact(ev['excerpt'] or ev['ref'], 60)}"
                )
        return "\n".join(lines)

    def _remember_text(self, store: MemoryStore, ident: dict, arg: str) -> str:
        content = (arg or "").strip()
        if not content:
            return "记住什么？写完整点：/feiyu 记住 我不吃辣。"
        rec = self._store_owner_fact(store, ident, content, source="command")
        return (
            f"存好了：{rec['id']}\n范围：私聊｜类别：{rec['kind']}\n"
            f"内容：{compact(rec['content'], 120)}\n"
            "记错了就 /feiyu 纠正 或 /feiyu 忘记。"
        )

    def _correct_text(self, store: MemoryStore, ident: dict, arg: str) -> str:
        parts = (arg or "").split(maxsplit=1)
        if len(parts) < 2:
            return "格式：/feiyu 纠正 <编号> <新内容>"
        rid, new = parts[0], parts[1].strip()
        rec = store.get_record(rid)
        if not rec:
            return "没这条。"
        new_rec = store.correct_record(rid, new, actor="owner")
        return f"改了：{new_rec['id']}（旧版 {rid} 已失效）"

    def _forget_text(self, store: MemoryStore, ident: dict, arg: str) -> str:
        rid = (arg or "").strip().split()[0] if (arg or "").strip() else ""
        if not rid:
            return "给个编号。要清空全部请用本地管理工具，聊天里不做全删。"
        rec = store.get_record(rid)
        if not rec:
            return "没这条。"
        store.forget_record(rid, reason="主人聊天中要求忘记", actor="owner")
        return f"忘了：{rid}。备份恢复也不会把它带回来。"

    def _undo_text(self, store: MemoryStore) -> str:
        rid = store.get_control("last_saved_id", "") or ""
        if not rid:
            return "没有可撤回的记录。"
        rec = store.get_record(rid)
        if not rec or rec["status"] != "active":
            return "上一条已经不在了。"
        store.forget_record(rid, reason="主人撤回", actor="owner")
        store.set_control("last_saved_id", "", actor="owner")
        return f"撤回了：{rid}"

    def _commit_text(self, store: MemoryStore, ident: dict, arg: str) -> str:
        content = (arg or "").strip()
        if not content:
            return "约定什么？/feiyu 约定 周日前把插件装好"
        rec = self._store_owner_fact(
            store, ident, content, source="command", force_kind="commitment"
        )
        return f"记下了，挂账：{rec['id']}\n{compact(rec['content'], 100)}"

    def _done_text(self, store: MemoryStore, ident: dict, arg: str) -> str:
        rid = (arg or "").strip().split()[0] if (arg or "").strip() else ""
        if not rid:
            return "给个编号：/feiyu 完成 r_abc123"
        store.complete_commitment(rid, actor="owner")
        return f"结账了：{rid}"

    def _state_text(self, store: MemoryStore, ident: dict, arg: str) -> str:
        content = (arg or "").strip()
        if not content:
            owner_id = (ident["owner"] or {}).get("owner_id", "primary")
            rows = store.list_records(
                owner_id=owner_id, kinds=["state"], limit=5
            )
            if not rows:
                return "现在没有记着的状态。用法：/feiyu 当前 在改记忆插件"
            return "当前状态：\n" + "\n".join(
                f"· {compact(r['content'], 80)}（{r['id']}，到 "
                f"{(r['expires_at'] or '')[:10]}）"
                for r in rows
            )
        owner_id = (ident["owner"] or {}).get("owner_id", "primary")
        rec = store.set_state(
            owner_id=owner_id,
            content=content,
            ttl_days=self._cfg_int("state_ttl_days", 7),
            platform=ident["platform"],
            bot_instance=ident["bot_instance"],
            created_by="owner",
            evidence=[{
                "kind": "owner_message",
                "ref": f"{ident['platform']}:{ident['user_id']}:command",
                "excerpt": compact(content, 200),
            }],
        )
        self._schedule_embedding(rec["id"], content)
        return (
            f"记下了当前状态：{rec['id']}\n{compact(content, 100)}\n"
            f"到 {(rec['expires_at'] or '')[:10]} 自动过期。"
        )

    def _diary_text(self, store: MemoryStore, ident: dict) -> str:
        owner_id = (ident["owner"] or {}).get("owner_id", "primary")
        rows = store.list_records(
            owner_id=owner_id, kinds=["diary"], limit=3
        )
        if not rows:
            return "还没写过日记。到点会自动写，也可以用 /feiyu 写日记 手动催一篇。"
        lines = ["【最近的关系日记】"]
        for r in rows:
            lines.append(f"· {r['asserted_at'][:10]}：{compact(r['content'], 160)}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #

    def _store_owner_fact(
        self,
        store: MemoryStore,
        ident: dict,
        content: str,
        *,
        source: str,
        force_kind: str | None = None,
    ) -> dict:
        owner = ident["owner"] or {}
        owner_id = owner.get("owner_id", "primary")
        kind = force_kind or self._classify_kind(content)
        scope_type = "owner_public" if self._looks_public(content) else (
            "owner_public"
            if self._cfg_str("owner_private_default_scope", "owner_private")
            == "owner_public"
            else "owner_private"
        )
        rec = store.add_record(
            owner_id=owner_id,
            kind=kind,
            content=content,
            epistemic_type="owner_asserted",
            scope_type=scope_type,
            scope_id="",
            importance=4 if kind == "commitment" else 3,
            platform=ident["platform"],
            bot_instance=ident["bot_instance"],
            created_by="owner",
            evidence=[{
                "kind": "owner_message",
                "ref": f"{ident['platform']}:{ident['user_id']}:{source}",
                "excerpt": compact(content, 200),
            }],
        )
        store.set_control("last_saved_id", rec["id"], actor="owner")
        self._schedule_embedding(rec["id"], content)
        return rec

    def _capture_owner_statement(self, event: AstrMessageEvent, ident: dict) -> str:
        text = (event.get_message_str() or "").strip()
        if len(text) < 3 or len(text) > 300:
            return ""
        content = ""
        for pattern in CAPTURE_PATTERNS:
            m = pattern.match(text)
            if m:
                content = m.group(1).strip()
                break
        if not content or len(content) < 2:
            return ""
        store = self._safe_store()
        if store is None:
            return "本轮写入失败（记忆库不可用），不要声称已经记住。"
        try:
            existing = [
                r for r in store.list_records(
                    owner_id=(ident["owner"] or {}).get("owner_id", "primary"),
                    limit=50,
                )
                if r["content"] == content
            ]
            if existing:
                return (
                    f"这条之前已经存过（{existing[0]['id']}），不必重复写入；"
                    "可以自然回应，但不要输出编号或系统标记。"
                )
            rec = self._store_owner_fact(store, ident, content, source="chat")
        except Exception as exc:
            logger.warning("[feiyu-memory] 捕获失败：%s", exc)
            return "本轮写入失败，不要声称已经记住，也不要许诺下次一定记得。"
        return (
            f"主人本轮明确要求记住的内容已成功写入（{rec['id']}，类别 {rec['kind']}，"
            "范围私聊）。可以自然地确认已存好，但不要输出编号、方括号区块或系统标记；"
            "主人若反悔可以用 /feiyu 撤销。"
        )

    # ------------------------------------------------------------------ #
    # 身份与工具
    # ------------------------------------------------------------------ #

    def _identity(self, event: AstrMessageEvent) -> dict:
        platform = event.get_platform_name() or "unknown"
        user_id = str(event.get_sender_id() or "")
        bot_instance = str(event.get_self_id() or "*")
        group_id = str(event.get_group_id() or "")
        scene = "group" if group_id else "private"
        owner = None
        store = self._safe_store()
        if store is not None and user_id:
            try:
                owner = store.get_owner(platform, user_id, bot_instance)
            except Exception:
                owner = None
        return {
            "platform": platform,
            "scene": scene,
            "group_id": group_id,
            "user_id": user_id,
            "bot_instance": bot_instance,
            "owner": owner,
            "is_owner": owner is not None,
        }

    def _store_sync(self) -> MemoryStore:
        if self._store is not None:
            return self._store
        path = self._cfg_str("db_path", "").strip() or None
        self._store = MemoryStore(path)
        return self._store

    def _safe_store(self) -> MemoryStore | None:
        if self._store is not None:
            return self._store
        if self._store_error:
            return None
        try:
            return self._store_sync()
        except Exception as exc:
            self._store_error = str(exc)
            logger.error("[feiyu-memory] 无法打开记忆库：%s", exc)
            return None

    def _persona(self) -> str:
        if self._persona_cache:
            return self._persona_cache
        if not self._cfg_bool("inject_persona", True):
            return ""
        path = self._cfg_str("persona_file", "").strip()
        if path:
            candidate = Path(path)
        else:
            candidate = Path(__file__).with_name("persona_default.txt")
        try:
            if candidate.is_file():
                self._persona_cache = candidate.read_text(encoding="utf-8").strip()
        except Exception as exc:
            logger.warning("[feiyu-memory] 读取人格文件失败：%s", exc)
        return self._persona_cache

    def _classify_kind(self, content: str) -> str:
        for kind, words in KIND_BY_KEYWORD:
            if any(w in content for w in words):
                return kind
        return "profile"

    @staticmethod
    def _looks_public(content: str) -> bool:
        return any(w in content for w in ("可以公开", "公开说", "群里也能说", "不用保密"))

    # -- 向量（可选） --------------------------------------------------- #

    async def _ensure_embedding(self):
        if self._embedding_checked:
            return self._embedding
        self._embedding_checked = True
        if not self._cfg_bool("vector_search", True):
            return None
        try:
            providers = self.context.get_all_embedding_providers() or []
            if providers:
                self._embedding = providers[0]
                logger.info("[feiyu-memory] 使用嵌入模型：%s", self._embedding.meta().id
                            if hasattr(self._embedding, "meta") else "unknown")
        except Exception as exc:
            logger.info("[feiyu-memory] 无可用嵌入模型，退化为关键词检索：%s", exc)
        return self._embedding

    async def _query_vector(self, text: str, timeout: float):
        provider = await self._ensure_embedding()
        if provider is None or not text:
            return None
        try:
            return await asyncio.wait_for(provider.get_embedding(text[:500]), timeout=max(0.5, timeout))
        except Exception:
            return None

    def _schedule_embedding(self, record_id: str, content: str) -> None:
        if not self._cfg_bool("vector_search", True):
            return
        coro = self._embed_record(record_id, content)
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(coro, loop)

                def _done(f):
                    try:
                        f.exception()
                    except Exception:
                        pass

                future.add_done_callback(_done)
                return
            except Exception:
                coro.close()
                return
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()

    async def _embed_record(self, record_id: str, content: str) -> None:
        provider = await self._ensure_embedding()
        if provider is None:
            return
        try:
            vec = await provider.get_embedding(content[:500])
            store = self._safe_store()
            if store and vec:
                await asyncio.to_thread(
                    store.put_embedding, record_id, self._embedding_name(), vec
                )
        except Exception as exc:
            logger.debug("[feiyu-memory] 向量写入失败：%s", exc)

    def _embedding_name(self) -> str:
        try:
            return str(self._embedding.meta().id)
        except Exception:
            return "astrbot-embedding"

    # -- 配置读取 ------------------------------------------------------- #

    def _cfg_all(self) -> dict:
        return {
            "max_memory_chars": self._cfg_int("max_memory_chars", 1000),
            "core_max_chars": self._cfg_int("core_max_chars", 420),
            "max_recalled": self._cfg_int("max_recalled", 4),
            "min_score": self._cfg_float("min_score", 2.5),
            "state_max_chars": self._cfg_int("state_max_chars", 200),
            "diary_max_chars": self._cfg_int("diary_max_chars", 240),
        }

    def _cfg_str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        return str(value) if value is not None else default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "启用", "开启"}
        return bool(value)

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default
