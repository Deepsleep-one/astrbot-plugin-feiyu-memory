"""阶段三自测：自动提取、关系日记、工作状态。

只依赖标准库。运行：
    python tests/selftest_stage3.py
"""

from __future__ import annotations

import asyncio
import itertools
import json
import shutil
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugin"))

from astrbot_plugin_feiyu_memory import memory_core, memory_stage3  # noqa: E402
from astrbot_plugin_feiyu_memory.memory_core import MemoryStore, now_dt, now_iso  # noqa: E402
from astrbot_plugin_feiyu_memory.memory_stage3 import (  # noqa: E402
    Stage3,
    _evidence_supported,
    parse_extraction,
)

TMP_ROOT = ROOT / "tests" / "_tmp" / "stage3"
_counter = itertools.count(1)

PASS = 0
FAIL = 0


def check(cond, label: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}")


def fresh_store() -> MemoryStore:
    case = TMP_ROOT / f"case_{next(_counter)}"
    shutil.rmtree(case, ignore_errors=True)
    case.mkdir(parents=True)
    store = MemoryStore(case / "mem.db")
    store.bind_owner("aiocqhttp", "10001", owner_id="primary",
                     owner_label="主人", group_label="主人", public_mode=True)
    return store


CFG = {
    "auto_extract": True,
    "diary_enable": True,
    "diary_hour": 0,
    "diary_minute": 0,
    "diary_min_records": 1,
    "extract_batch_turns": 6,
    "extract_idle_minutes": 5,
    "extract_min_confidence": 0.7,
    "extract_daily_limit": 40,
    "allow_auto_public": False,
    "state_ttl_days": 7,
    "state_max_chars": 200,
    "diary_max_chars": 240,
}

OWNER = {
    "is_owner": True,
    "scene": "private",
    "platform": "aiocqhttp",
    "user_id": "10001",
    "bot_instance": "*",
    "owner": {"owner_id": "primary"},
}
STRANGER = dict(OWNER, is_owner=False)
GROUP = dict(OWNER, scene="group")


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.last_user = ""

    async def __call__(self, system: str, user: str, model: str = "") -> str:
        self.calls += 1
        self.last_user = user
        if not self.responses:
            raise RuntimeError("FakeLLM 没有更多响应")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def candidates_json(items) -> str:
    return json.dumps({"memories": items}, ensure_ascii=False)


TURN_USER = "我从来不吃辣，火锅都点清汤"
TURN_BOT = "好，那我们以后都点清汤锅。"


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #

def test_helpers():
    print("[helpers] 证据定位与 JSON 解析")
    transcript = "主人：我从来不吃辣，火锅都点清汤\n助手：好"
    check(_evidence_supported("我从来不吃辣", transcript), "原文能定位")
    check(not _evidence_supported("我从来不吃辣火锅", transcript), "改写不再冒充原文")
    check(not _evidence_supported("他喜欢爬山", transcript), "编造内容定位不到")
    check(parse_extraction("```json\n{\"memories\": []}\n```") == [], "带围栏空列表")
    check(parse_extraction("随便说点什么") == [], "非 JSON 返回空")
    check(len(parse_extraction(
        '前言 {"memories":[{"kind":"fact","content":"x"}]} 后记'
    )) == 1, "前后有噪音也能解析")


# --------------------------------------------------------------------------- #
# 入队与批处理
# --------------------------------------------------------------------------- #

def test_queue():
    print("[queue] 幂等入队与批处理门槛")
    store = fresh_store()
    stage = Stage3(store, CFG, FakeLLM([]))

    first = stage.capture_turn(session_id="s1", user_text=TURN_USER,
                               assistant_text=TURN_BOT, ident=OWNER)
    second = stage.capture_turn(session_id="s1", user_text=TURN_USER,
                                assistant_text=TURN_BOT, ident=OWNER)
    check(isinstance(first, str) and first.startswith("j_"), "首次入队成功")
    check(second is None, "同内容重复入队被去重")
    check(store.job_counts()["pending"] == 1, "队列里只有一条")
    check(stage.capture_turn(session_id="s2", user_text="你好呀阿鱼",
                             assistant_text="在呢", ident=GROUP) is None,
          "群聊不入队")
    check(stage.capture_turn(session_id="s3", user_text="你好呀阿鱼",
                             assistant_text="在呢", ident=STRANGER) is None,
          "非主人不入队")

    asyncio.run(stage.process_pending())
    check(store.job_counts()["pending"] == 1, "不足一批且刚说完时不处理")

    res = asyncio.run(stage.process_pending(force=True))
    check(res["jobs"] == 1, "force 时处理了这一条")


def test_extract_validation():
    print("[extract] 候选校验与写入")
    store = fresh_store()
    payload = candidates_json([
        {"kind": "preference", "content": "不吃辣", "confidence": 0.9,
         "evidence": "我从来不吃辣，火锅都点清汤", "fact_key": "spice"},
        {"kind": "preference", "content": "喜欢下雨天", "confidence": 0.3,
         "evidence": "我从来不吃辣", "fact_key": ""},
        {"kind": "event", "content": "周末去爬山了", "confidence": 0.9,
         "evidence": "周末去爬山了", "fact_key": ""},
        {"kind": "fact", "content": "密码是 abc123", "confidence": 0.9,
         "evidence": "我从来不吃辣", "fact_key": ""},
        {"kind": "diary", "content": "今天聊了吃辣", "confidence": 0.9,
         "evidence": "我从来不吃辣", "fact_key": ""},
    ])
    llm = FakeLLM([payload])
    stage = Stage3(store, CFG, llm)
    stage.capture_turn(session_id="s1", user_text=TURN_USER,
                       assistant_text=TURN_BOT, ident=OWNER)
    res = asyncio.run(stage.process_pending(force=True))

    check(res["written"] == 1, f"只写入 1 条（实际 {res['written']}）")
    check(res["skipped"] == 4, f"跳过 4 条（实际 {res['skipped']}）")
    recs = store.list_records(owner_id="primary", kinds=["preference"])
    check(len(recs) == 1 and recs[0]["content"] == "不吃辣", "写入的是那条偏好")
    rec = store.get_record(recs[0]["id"])
    check(rec["epistemic_type"] == "observed_dialogue", "来源是 observed_dialogue")
    check(rec["created_by"] == "extractor", "写入者是 extractor")
    check(bool(rec["evidence"]), "带证据")
    check(rec["evidence"][0]["excerpt"].startswith("我从来不吃辣"), "证据摘录正确")
    check(rec["proof_status"] if "proof_status" in rec else True, "字段完整")
    check(rec["scope_type"] == "owner_private", "默认只写私聊范围")

    # 第二轮同样内容 → 判重
    llm2 = FakeLLM([payload])
    stage.call_llm = llm2
    stage.capture_turn(session_id="s2", user_text=TURN_USER,
                       assistant_text="嗯嗯", ident=OWNER)
    res2 = asyncio.run(stage.process_pending(force=True))
    check(res2["written"] == 0, "重复内容不再写")


def test_retry():
    print("[extract] 模型失败后退回队列")
    store = fresh_store()
    llm = FakeLLM([RuntimeError("boom")])
    stage = Stage3(store, CFG, llm)
    stage.capture_turn(session_id="s1", user_text=TURN_USER,
                       assistant_text=TURN_BOT, ident=OWNER)
    res = asyncio.run(stage.process_pending(force=True))
    check(res["processed"] == 0, "失败批次不算处理")
    counts = store.job_counts()
    check(counts["pending"] == 1 and counts["running"] == 0, "任务退回 pending")

    llm.responses.append(candidates_json([
        {"kind": "preference", "content": "不吃辣", "confidence": 0.9,
         "evidence": "我从来不吃辣，火锅都点清汤"},
    ]))
    res2 = asyncio.run(stage.process_pending(force=True))
    check(res2["written"] == 1, "重试成功后写入")
    check(store.job_counts()["done"] == 1, "任务标 done")


def test_daily_quota():
    print("[extract] 每日配额")
    store = fresh_store()
    store.set_control("extract_daily", now_dt().strftime("%Y-%m-%d") + ":40")
    stage = Stage3(store, CFG, FakeLLM([candidates_json([])]))
    stage.capture_turn(session_id="s1", user_text=TURN_USER,
                       assistant_text=TURN_BOT, ident=OWNER)
    res = asyncio.run(stage.process_pending(force=True))
    check(res["written"] == 0 and res["jobs"] == 0, "配额用尽时不取任务")
    check(store.job_counts()["pending"] == 1, "任务留在队列里等明天")


def test_deletion_epoch():
    print("[extract] 删除纪元让旧任务作废")
    store = fresh_store()
    stage = Stage3(store, CFG, FakeLLM([candidates_json([])]))
    stage.capture_turn(session_id="s1", user_text=TURN_USER,
                       assistant_text=TURN_BOT, ident=OWNER)
    rec = store.add_record(owner_id="primary", kind="fact", content="旧事实",
                           epistemic_type="owner_asserted",
                           scope_type="owner_private")
    store.forget_record(rec["id"], reason="测试")
    jobs = store.claim_jobs(limit=5)
    check(jobs == [], "纪元推进后任务不会被取出")
    check(store.job_counts()["dropped"] == 1, "旧任务标 dropped")


# --------------------------------------------------------------------------- #
# 工作状态
# --------------------------------------------------------------------------- #

def test_state():
    print("[state] 工作状态注入与过期")
    store = fresh_store()
    store.set_state("primary", "正在改记忆插件", ttl_days=7)
    ctx = store.build_context(is_owner=True, scene="private", user_id="10001",
                              group_id="", owner=store.list_owners()[0],
                              query="", cfg=CFG)
    check("当前状态" in ctx["memory"], "私聊注入当前状态")
    check("正在改记忆插件" in ctx["memory"], "内容出现在注入块")

    group_ctx = store.build_context(is_owner=True, scene="group", user_id="10001",
                                    group_id="777", owner=store.list_owners()[0],
                                    query="", cfg=CFG)
    check("当前状态" not in group_ctx["memory"], "群聊不注入私密状态")

    hits = store.search("记忆插件", owner_id="primary", is_owner=True,
                        scene="private", user_id="10001")
    check(all(r["kind"] != "state" for r in hits), "状态不参与召回")

    with store._lock:
        store._conn.execute(
            "UPDATE memory_records SET expires_at=? WHERE kind='state'",
            ((now_dt() - timedelta(days=1)).isoformat(),),
        )
        store._conn.commit()
    expired = store.expire_states()
    check(expired == 1, "过期状态被归档")
    ctx2 = store.build_context(is_owner=True, scene="private", user_id="10001",
                               group_id="", owner=store.list_owners()[0],
                               query="", cfg=CFG)
    check("当前状态" not in ctx2["memory"], "过期后不再注入")

    # 同 fact_key 的新状态取代旧状态
    store.set_state("primary", "在写测试", ttl_days=3)
    store.set_state("primary", "在收尾", ttl_days=3)
    active = store.active_states("primary", [("owner_private", "")], 6)
    check(len(active) == 1 and active[0]["content"] == "在收尾", "新状态取代旧状态")


# --------------------------------------------------------------------------- #
# 关系日记
# --------------------------------------------------------------------------- #

def test_diary():
    print("[diary] 生成、派生失效与注入范围")
    store = fresh_store()
    store.add_record(owner_id="primary", kind="fact", content="不吃辣",
                     epistemic_type="owner_asserted", scope_type="owner_private")
    store.add_record(owner_id="primary", kind="profile", content="做后端开发",
                     epistemic_type="owner_asserted", scope_type="owner_private")
    store.add_record(owner_id="primary", kind="event",
                     content="周末一起看了历史纪录片",
                     epistemic_type="observed_dialogue",
                     scope_type="owner_private",
                     evidence=[{"kind": "dialogue", "ref": "t", "excerpt": "看纪录片"}])
    llm = FakeLLM(["今天他说不吃辣，我记下了；还一起看了历史纪录片，挺安静的。", "再来一篇"])
    stage = Stage3(store, CFG, llm)

    res = asyncio.run(stage.maybe_write_diary(force=True))
    check(res.get("written") is True, "日记写入成功")
    diaries = store.list_records(owner_id="primary", kinds=["diary"])
    check(len(diaries) == 1, "库里有一篇日记")
    d = store.get_record(diaries[0]["id"])
    check(d["epistemic_type"] == "derived", "日记是派生类型")
    check(d["scope_type"] == "owner_private", "日记只在私聊可见")
    derived = d["derived_from_ids"]
    if isinstance(derived, str):
        derived = json.loads(derived or "[]")
    check(len(derived) >= 1, "记下了来源 id")

    ctx = store.build_context(is_owner=True, scene="private", user_id="10001",
                              group_id="", owner=store.list_owners()[0],
                              query="", cfg=CFG)
    check("关系日记" in ctx["memory"], "私聊注入日记")
    group_ctx = store.build_context(is_owner=True, scene="group", user_id="10001",
                                    group_id="777", owner=store.list_owners()[0],
                                    query="", cfg=CFG)
    check("关系日记" not in group_ctx["memory"], "群聊不注入日记")

    # 当天不重复写
    res2 = asyncio.run(stage.maybe_write_diary())
    check(res2.get("written") is not True, "同一天不重复写")

    # 源记录被忘记 → 日记连带失效
    source = store.list_records(owner_id="primary", kinds=["fact"])[0]
    store.forget_record(source["id"], reason="测试")
    d2 = store.get_record(diaries[0]["id"])
    check(d2["status"] == "superseded", "源记录失效后日记被连带取代")


# --------------------------------------------------------------------------- #
# tick
# --------------------------------------------------------------------------- #

def test_tick():
    print("[tick] 周期任务整体可用")
    store = fresh_store()
    stage = Stage3(store, CFG, FakeLLM([candidates_json([])]))
    out = asyncio.run(stage.tick())
    check(isinstance(out, dict) and "expired" in out, "tick 返回结构正常")
    # 关掉日记与提取也不会抛
    cfg_off = dict(CFG, auto_extract=False, diary_enable=False)
    stage2 = Stage3(store, cfg_off, FakeLLM([]))
    out2 = asyncio.run(stage2.tick())
    check("extract" not in out2, "关闭提取时不做提取")


def main() -> int:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    test_helpers()
    test_queue()
    test_extract_validation()
    test_retry()
    test_daily_quota()
    test_deletion_epoch()
    test_state()
    test_diary()
    test_tick()
    print(f"\n通过 {PASS}，失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
