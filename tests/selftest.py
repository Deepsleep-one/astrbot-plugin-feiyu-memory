#!/usr/bin/env python
"""大肥鱼记忆自测：不启动 AstrBot，用临时库验证核心逻辑与插件胶水。

运行：
    python tests\\selftest.py

覆盖：身份锚点、范围隔离（私聊/群/他人）、召回、无证据拒收、
事实键取代、忘记不复活、备份恢复、预算、暂停、插件注入与指令回执。
"""

from __future__ import annotations

import asyncio
import itertools
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "plugin" / "astrbot_plugin_feiyu_memory"
TMP_ROOT = ROOT / "tests" / "_tmp"
TMP_ROOT.mkdir(parents=True, exist_ok=True)
_counter = itertools.count(1)
sys.path.insert(0, str(PLUGIN_DIR))

import memory_core as core  # noqa: E402
from memory_core import MemoryError, MemoryStore, tokenize  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def new_store() -> MemoryStore:
    # 不用 tempfile.mkdtemp：沙箱里 0o700 目录无法写入，sqlite 会打不开文件。
    tmp = TMP_ROOT / f"case_{next(_counter)}"
    tmp.mkdir(parents=True, exist_ok=True)
    return MemoryStore(str(tmp / "test.db"))


# --------------------------------------------------------------------------- #
# 核心
# --------------------------------------------------------------------------- #

def test_identity() -> None:
    print("[身份锚点]")
    store = new_store()
    store.bind_owner("aiocqhttp", "10001", owner_label="主人", group_label="鱼哥")
    check("绑定后可查", store.get_owner("aiocqhttp", "10001", "999") is not None)
    check("别人不是主人", store.get_owner("aiocqhttp", "10002") is None)
    check("平台不同不认", store.get_owner("telegram", "10001") is None)
    check("未绑定时不认", store.get_owner("aiocqhttp", "") is None)

    # 核心
    store.set_core("primary", "address", "主人", title="私聊称呼")
    store.set_core("primary", "relation", "有固定主人的鲸鱼娘助手", title="关系背景",
                   scope_type="owner_public")
    store.set_core("primary", "boundary", "不叫老公", title="不可接受的称呼")
    owner = store.get_owner("aiocqhttp", "10001")

    priv = store.build_context(is_owner=True, scene="private", user_id="10001",
                               owner=owner)
    check("私聊有核心", "主人核心" in priv["memory"] and "不叫老公" in priv["memory"])
    check("状态块格式", priv["state"].startswith("[后端核验的交互状态]")
          and priv["state"].rstrip().endswith("[核验状态结束]"))
    check("参考资料格式", "[参考资料结束]" in priv["memory"])

    grp = store.build_context(is_owner=True, scene="group", group_id="555",
                              user_id="10001", owner=owner)
    check("主人进群不带私密边界", "不叫老公" not in grp["memory"], grp["memory"])
    check("主人进群带公开核心", "有固定主人的鲸鱼娘助手" in grp["memory"])

    other = store.build_context(is_owner=False, scene="private", user_id="10002")
    check("普通用户无主人核心", "主人核心" not in other["memory"])
    check("普通用户状态标明未绑定", "未绑定" in other["state"])
    store.close()


def test_records() -> None:
    print("[记录与召回]")
    store = new_store()
    store.bind_owner("aiocqhttp", "10001")
    owner = store.get_owner("aiocqhttp", "10001")
    r1 = store.add_record(
        owner_id="primary", kind="preference", content="我不吃辣，微辣也不行",
        epistemic_type="owner_asserted", scope_type="owner_private",
        fact_key="food.spicy", importance=4,
    )
    hits = store.search("能吃辣吗", owner_id="primary", is_owner=True,
                        scene="private", user_id="10001")
    check("关键词召回", any("不吃辣" in h["content"] for h in hits), str(hits))

    hits_other = store.search("能吃辣吗", owner_id="primary", is_owner=False,
                              scene="private", user_id="10002")
    check("他人私聊检索不到", hits_other == [], str(hits_other))

    hits_group = store.search("能吃辣吗", owner_id="primary", is_owner=True,
                              scene="group", group_id="555", user_id="10001")
    check("群聊检索不到私密", hits_group == [], str(hits_group))

    pub = store.add_record(
        owner_id="primary", kind="preference", content="主食喜欢白米饭",
        epistemic_type="owner_asserted", scope_type="owner_public", importance=3,
    )
    hits_pub = store.search("白米饭", owner_id="primary", is_owner=True,
                            scene="group", group_id="555", user_id="10001")
    check("群聊能召回公开记录", any(h["id"] == pub["id"] for h in hits_pub))
    hits_other_group = store.search("白米饭", owner_id="primary", is_owner=False,
                                    scene="group", group_id="555", user_id="10002")
    check("别的群友看不到主人的公开记录", hits_other_group == [], str(hits_other_group))

    noise = store.search("今天天气怎么样", owner_id="primary", is_owner=True,
                         scene="private", user_id="10001")
    check("无关问题不乱翻", noise == [], str(noise))

    try:
        store.add_record(
            owner_id="primary", kind="event", content="上次一起去看了海",
            epistemic_type="observed_dialogue", scope_type="owner_private",
        )
        check("无证据拒收", False)
    except MemoryError as exc:
        check("无证据拒收", "证据" in str(exc), str(exc))
    store.close()


def test_supersede() -> None:
    print("[纠正与取代]")
    store = new_store()
    a = store.add_record(
        owner_id="primary", kind="preference", content="我喝美式",
        epistemic_type="owner_asserted", scope_type="owner_private",
        fact_key="drink.coffee",
    )
    b = store.add_record(
        owner_id="primary", kind="preference", content="我改喝拿铁了",
        epistemic_type="owner_asserted", scope_type="owner_private",
        fact_key="drink.coffee",
    )
    check("旧状态失效", store.get_record(a["id"])["status"] == "superseded")
    check("版本递增", b["revision"] == 2, str(b["revision"]))
    check("指向被取代项", b["supersedes_id"] == a["id"])

    c = store.correct_record(b["id"], "其实我只喝白开水")
    check("手工纠正生成新版本", c["revision"] == 3 and c["supersedes_id"] == b["id"])
    hits = store.search("喝什么咖啡", owner_id="primary", is_owner=True,
                        scene="private", user_id="10001")
    check("只召回最新状态", all("咖啡" not in h["content"] or "拿铁" in h["content"]
                                for h in hits), str(hits))
    store.close()


def test_forget_and_restore() -> None:
    print("[忘记与备份]")
    store = new_store()
    rec = store.add_record(
        owner_id="primary", kind="profile", content="住在杭州",
        epistemic_type="owner_asserted", scope_type="owner_private",
    )
    backup = store.backup()
    check("备份文件存在", Path(backup).exists())
    store.forget_record(rec["id"], reason="测试")
    check("忘记后状态", store.get_record(rec["id"])["status"] == "deleted")
    result = store.restore(backup)
    check("恢复后仍是删除", store.get_record(rec["id"])["status"] == "deleted")
    check("墓碑回放", result["reapplied"] >= 1, str(result))
    check("恢复摘要含墓碑数", result["tombstones"] >= 1)
    store.close()


def test_budget_and_pause() -> None:
    print("[预算与开关]")
    store = new_store()
    store.bind_owner("aiocqhttp", "10001")
    owner = store.get_owner("aiocqhttp", "10001")
    for i in range(8):
        store.add_record(
            owner_id="primary", kind="event",
            content=f"第{i}次一起做的事，细节很多很多很多很多很多",
            epistemic_type="owner_asserted", scope_type="owner_private",
            importance=3,
        )
    big = store.build_context(is_owner=True, scene="private", user_id="10001",
                              owner=owner, query="一起做的事",
                              cfg={"max_memory_chars": 1200, "max_recalled": 8})
    small = store.build_context(is_owner=True, scene="private", user_id="10001",
                                owner=owner, query="一起做的事",
                                cfg={"max_memory_chars": 200, "max_recalled": 8})
    check("预算生效", len(small["memory"]) < len(big["memory"]),
          f"{len(small['memory'])} vs {len(big['memory'])}")
    check("预算上限附近", len(small["memory"]) <= 320, str(len(small["memory"])))

    store.set_control("capture_enabled", "0")
    paused = store.build_context(is_owner=True, scene="private", user_id="10001",
                                 owner=owner)
    check("暂停状态可见", "暂停" in paused["state"])

    degraded = store.build_context(is_owner=True, scene="private", user_id="10001",
                                   owner=owner, degraded="超时，本轮只加载核心记忆",
                                   receipt="已写入 r_demo")
    check("降级仍认主", "主人" in degraded["state"])
    check("降级提示", "超时" in degraded["state"])
    check("回执进入状态块", "r_demo" in degraded["state"])
    store.close()


def test_tokenizer() -> None:
    print("[分词]")
    latin, grams = tokenize("我 Go 后端, 不吃辣")
    check("拉丁词", "go" in latin, str(latin))
    check("中文二元组", "吃辣" in grams and "不吃" in grams, str(grams))
    check("停用二元组被滤", "什么" not in tokenize("什么")[1])
    check("无意义的空串", tokenize("")[1] == set())


# --------------------------------------------------------------------------- #
# 插件胶水（用假 astrbot 模块，不启动 AstrBot）
# --------------------------------------------------------------------------- #

def install_fake_astrbot() -> None:
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        info=print, warning=print, error=print, debug=lambda *a, **k: None,
        exception=print,
    )
    api.AstrBotConfig = dict

    class FakeFilter:
        def on_llm_request(self, *a, **k):
            return lambda fn: fn

        def on_llm_response(self, *a, **k):
            return lambda fn: fn

        def on_agent_done(self, *a, **k):
            return lambda fn: fn

        def after_message_sent(self, *a, **k):
            return lambda fn: fn

        def command(self, *a, **k):
            return lambda fn: fn

        def llm_tool(self, *a, **k):
            return lambda fn: fn

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = object
    event_mod.filter = FakeFilter()

    star_mod = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

    star_mod.Context = object
    star_mod.Star = Star
    star_mod.register = lambda *a, **k: (lambda cls: cls)

    core_star = types.ModuleType("astrbot.core.star")
    core_star_filter = types.ModuleType("astrbot.core.star.filter")
    command_mod = types.ModuleType("astrbot.core.star.filter.command")

    class GreedyStr(str):
        pass

    command_mod.GreedyStr = GreedyStr

    sys.modules.update({
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": api,
        "astrbot.api.event": event_mod,
        "astrbot.api.star": star_mod,
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.star": core_star,
        "astrbot.core.star.filter": core_star_filter,
        "astrbot.core.star.filter.command": command_mod,
    })


class FakeEvent:
    def __init__(self, text: str, user_id: str = "10001",
                 group_id: str = "", bot_id: str = "9999"):
        self._text = text
        self._user = user_id
        self._group = group_id
        self._bot = bot_id
        self.replies: list[str] = []

    def get_message_str(self):
        return self._text

    def get_platform_name(self):
        return "aiocqhttp"

    def get_sender_id(self):
        return self._user

    def get_self_id(self):
        return self._bot

    def get_group_id(self):
        return self._group

    def get_session_id(self):
        if self._group:
            return f"aiocqhttp:GroupMessage:{self._group}"
        return f"aiocqhttp:FriendMessage:{self._user}"

    def is_private_chat(self):
        return not self._group

    def get_sender_name(self):
        return "测试者"

    def plain_result(self, text):
        self.replies.append(text)
        return text


class FakeRequest:
    def __init__(self, system_prompt="BASE"):
        self.system_prompt = system_prompt


class FakeContext:
    def get_all_embedding_providers(self):
        return []


async def collect(agen):
    items = []
    async for item in agen:
        items.append(item)
    return items


def load_plugin_module(tmp_db: str):
    install_fake_astrbot()
    pkg = types.ModuleType("feiyu_test_pkg")
    pkg.__path__ = [str(PLUGIN_DIR)]
    sys.modules["feiyu_test_pkg"] = pkg
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "feiyu_test_pkg.main", PLUGIN_DIR / "main.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["feiyu_test_pkg.main"] = module
    spec.loader.exec_module(module)
    return module


def test_plugin() -> None:
    print("[插件胶水]")
    tmp = TMP_ROOT / "plugin_case"
    tmp.mkdir(parents=True, exist_ok=True)
    db = str(tmp / "test.db")
    module = load_plugin_module(db)

    config = {
        "db_path": db,
        "inject_persona": True,
        "auto_capture_owner_private": True,
        "vector_search": False,
        "max_memory_chars": 1000,
    }
    plugin = module.FeiyuMemoryPlugin(FakeContext(), config)

    async def scenario():
        await plugin.initialize()
        store = plugin._store
        store.bind_owner("aiocqhttp", "10001", owner_label="主人")
        store.set_core("primary", "relation", "有固定主人的鲸鱼娘", title="关系背景")

        # 1. 主人私聊自然语言记住 + 注入
        event = FakeEvent("记住 我喜欢喝冰美式")
        req = FakeRequest("BASE")
        await plugin.inject_memory(event, req)
        prompt = req.system_prompt
        check("注入状态块", "[后端核验的交互状态]" in prompt)
        check("注入参考资料块", "[记忆参考资料" in prompt)
        check("注入人格", "鲸鱼娘" in prompt)
        check("回执带编号但要求不输出",
              "r_" in prompt.split("[记忆参考资料")[0]
              and "不要输出编号" in prompt)
        saved = [r for r in store.list_records(limit=10) if "冰美式" in r["content"]]
        check("自然语言已入库", len(saved) == 1, str(saved))

        # 2. 同一句重复说不重复存
        event2 = FakeEvent("记住 我喜欢喝冰美式")
        req2 = FakeRequest("BASE")
        await plugin.inject_memory(event2, req2)
        saved2 = [r for r in store.list_records(limit=10) if "冰美式" in r["content"]]
        check("重复不重复存", len(saved2) == 1, str(saved2))

        # 3. 群聊不泄露私聊记录
        group_event = FakeEvent("我喜欢喝什么", group_id="555")
        greq = FakeRequest("BASE")
        await plugin.inject_memory(group_event, greq)
        check("群聊不含私密记录", "冰美式" not in greq.system_prompt)

        # 4. 主人指令
        cmd_event = FakeEvent("")
        replies = await collect(plugin.feiyu_command(cmd_event, "记住 我讨厌香菜"))
        check("指令写入回执", replies and "存好了" in replies[0], str(replies))
        rid = store.get_control("last_saved_id")
        check("回执带编号", rid and rid in replies[0])

        # 5. 纠正
        replies = await collect(plugin.feiyu_command(
            FakeEvent(""), f"纠正 {rid} 我其实不讨厌香菜"
        ))
        check("指令纠正", replies and "改了" in replies[0], str(replies))

        # 6. 忘记
        replies = await collect(plugin.feiyu_command(FakeEvent(""), f"忘记 {rid}"))
        check("指令忘记", replies and "忘了" in replies[0], str(replies))
        check("忘记后失效", store.get_record(rid)["status"] != "active")

        # 7. 非主人被拒
        stranger = FakeEvent("", user_id="20002")
        replies = await collect(plugin.feiyu_command(stranger, "状态"))
        check("非主人拒绝", replies and "主人" in replies[0] and "绑定" in replies[0],
              str(replies))

        # 8. 群聊指令拒绝
        replies = await collect(plugin.feiyu_command(
            FakeEvent("", group_id="555"), "档案"
        ))
        check("群聊指令拒绝", replies and "私聊" in replies[0], str(replies))

        # 9. 暂停后不再捕获
        await collect(plugin.feiyu_command(FakeEvent(""), "暂停"))
        before = len(store.list_records(limit=100))
        await plugin.inject_memory(FakeEvent("记住 我养了一只猫"), FakeRequest("BASE"))
        after = len(store.list_records(limit=100))
        check("暂停后不写入", before == after, f"{before}->{after}")

        # 10. 工作状态指令
        replies = await collect(plugin.feiyu_command(FakeEvent(""), "当前 在接线阶段三"))
        check("状态指令回执", replies and "当前状态" in replies[0], str(replies))
        states = store.active_states("primary", [("owner_private", ""), ("owner_public", "")], 6)
        check("状态已入库", any("接线阶段三" in s["content"] for s in states), str(states))
        replies = await collect(plugin.feiyu_command(FakeEvent(""), "当前"))
        check("状态列表可看", replies and "接线阶段三" in replies[0], str(replies))

        # 11. 日记指令（还没有日记时可看提示，有则能列出）
        replies = await collect(plugin.feiyu_command(FakeEvent(""), "日记"))
        check("空日记有提示", replies and ("还没写" in replies[0] or "关系日记" in replies[0]),
              str(replies))
        store.add_record(
            owner_id="primary", kind="diary", content="今天一起接线了阶段三。",
            epistemic_type="derived", scope_type="owner_private",
            derived_from_ids=["r_placeholder"], created_by="diary",
        )
        replies = await collect(plugin.feiyu_command(FakeEvent(""), "日记"))
        check("日记可列出", replies and "阶段三" in replies[0], str(replies))

        await plugin.terminate()

    asyncio.run(scenario())


def main() -> int:
    import shutil

    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    test_tokenizer()
    test_identity()
    test_records()
    test_supersede()
    test_forget_and_restore()
    test_budget_and_pause()
    test_plugin()
    print()
    print(f"通过 {PASS}，失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
