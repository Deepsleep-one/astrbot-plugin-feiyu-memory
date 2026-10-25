"""M1 记忆可信度回归（2026-10-25）。

这里是定向复现过的五类缺陷，修完必须长期保持绿：

1. 助手自己的话被当成主人的事实证据（没有 speaker 归属）；
2. 近义改写／否定改写冒充原文证据（0.8 二元组兜底）；
3. 整理去重把「喜欢」和「不喜欢」并成一条；
4. 派生失效只断一层（A→B→C 之后 C 还活着）；
5. 已遗忘的证据被重新提取回库、整理落库前不复核来源。

只依赖标准库。运行：
    python tests/selftest_trust.py
    python tools/run_selftests.py trust
"""

from __future__ import annotations

import asyncio
import itertools
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugin"))

from astrbot_plugin_feiyu_memory.memory_core import MemoryError, MemoryStore  # noqa: E402
from astrbot_plugin_feiyu_memory.memory_reflect import MemoryReflector  # noqa: E402
from astrbot_plugin_feiyu_memory.memory_stage3 import (  # noqa: E402
    Stage3,
    _evidence_supported,
    _locate_span,
)

TMP_ROOT = ROOT / "tests" / "_tmp" / "trust"
_counter = itertools.count(1)

PASS = 0
FAIL = 0


def check(cond, label: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        suffix = f"  [{extra}]" if extra else ""
        print(f"  FAIL {label}{suffix}")


def fresh_store() -> MemoryStore:
    case = TMP_ROOT / f"case_{next(_counter)}"
    shutil.rmtree(case, ignore_errors=True)
    case.mkdir(parents=True)
    store = MemoryStore(case / "mem.db")
    store.bind_owner("aiocqhttp", "10001", owner_id="primary",
                     owner_label="主人", group_label="主人", public_mode=True)
    return store


OWNER = {
    "is_owner": True, "scene": "private", "platform": "aiocqhttp",
    "bot_instance": "*", "user_id": "10001",
    "owner": {"owner_id": "primary", "owner_label": "主人"},
}

CFG = {
    "auto_extract": True,
    "extract_batch_turns": 6,
    "extract_idle_minutes": 0,
    "extract_min_confidence": 0.7,
    "extract_daily_limit": 40,
    "allow_auto_public": False,
    "reflect_enable": True,
    "reflect_dedupe": 0.88,
    "reflect_fold": 0.45,
    "reflect_min_fold": 2,
    "reflect_min_records": 2,
    "reflect_group_limit": 8,
    "reflect_days": 30,
    "reflect_batch": 120,
}

DIALOGUE = {"kind": "dialogue", "ref": "case", "excerpt": "自测用"}


def fake_llm(reply: str):
    async def _call(system: str, user: str, model: str = "") -> str:
        return reply
    return _call


def extraction(*items):
    return fake_llm(json.dumps({"memories": list(items)}, ensure_ascii=False))


def reflection(*items):
    return fake_llm(json.dumps({"merged": list(items)}, ensure_ascii=False))


def add(store: MemoryStore, content: str, *, kind="fact", epistemic="observed_dialogue",
        derived_from=(), evidence=None):
    return store.add_record(
        owner_id="primary", kind=kind, content=content, epistemic_type=epistemic,
        scope_type="owner_private", derived_from_ids=list(derived_from),
        evidence=evidence if evidence is not None else [dict(DIALOGUE)],
    )


def audit_count(store: MemoryStore, action: str) -> int:
    row = store._conn.execute(
        "SELECT COUNT(*) FROM memory_audit WHERE action=?", (action,)
    ).fetchone()
    return int(row[0])


# --------------------------------------------------------------------------- #
# 1. 证据必须逐字落在原话里
# --------------------------------------------------------------------------- #

def test_evidence_precision():
    print("\n[1] 证据必须逐字落在原话里")
    transcript = "主人：我从来不吃辣，火锅都点清汤\n助手：好，那我们以后都点清汤锅。"
    check(_evidence_supported("我从来不吃辣", transcript), "原文片段能定位")
    check(not _evidence_supported("我从来不吃辣火锅", transcript), "近义改写不再冒充原文")
    check(not _evidence_supported("我不喜欢周末去海边骑自行车",
                                  "主人：我喜欢周末去海边骑自行车"),
          "否定句不被当成肯定原文")
    check(not _evidence_supported("他喜欢爬山", transcript), "编造内容定位不到")

    span = _locate_span("我从来不吃辣", transcript)
    check(span is not None and transcript[span[0]:span[1]] == "我从来不吃辣",
          "跨度切回原字符", str(span))
    spaces = _locate_span("我从来不吃辣", "主人：我从来 不 吃辣，火锅都点清汤")
    check(spaces is not None and spaces[0] > 0, "空白差异仍能定位", str(spaces))


# --------------------------------------------------------------------------- #
# 2. 助手自述不能成为主人的事实证据
# --------------------------------------------------------------------------- #

def test_speaker_attribution():
    print("\n[2] 助手自述不能成为主人的事实证据")
    store = fresh_store()
    stage = Stage3(store, CFG, lambda *a, **k: "")
    transcript = "主人：随便聊聊\n助手：主人养了企鹅"
    job = {"id": "j_probe"}
    base = {"kind": "fact", "confidence": 0.9, "fact_key": ""}

    try:
        stage._write_candidate(dict(base, content="主人养了企鹅", evidence="主人养了企鹅"),
                               "primary", "aiocqhttp", "*", transcript, job)
        check(False, "助手原句当证据被拒", "写进去了")
    except MemoryError as exc:
        check("助手" in str(exc), "助手原句当证据被拒", str(exc))

    try:
        stage._write_candidate(dict(base, content="主人出生在南方", evidence="主人养了企鹅"),
                               "primary", "aiocqhttp", "*", transcript, job)
        check(False, "助手原句不能支撑别的说法", "写进去了")
    except MemoryError as exc:
        check(True, "助手原句不能支撑别的说法", str(exc)[:40])

    rec = stage._write_candidate(
        dict(base, content="主人今天在加班", evidence="随便聊聊"),
        "primary", "aiocqhttp", "*", transcript, job)
    check(bool(rec), "主人自己说的话能写入")
    ev = store.get_record(rec["id"])["evidence"][0]
    check(ev.get("speaker") == "owner", "证据落库带说话人 owner", str(ev.get("speaker")))
    check(bool(ev.get("content_hash")), "证据落库带正文哈希")
    start, end = ev.get("span_start"), ev.get("span_end")
    check(start is not None and end is not None, "证据落库带跨度", f"{start}-{end}")
    check("随便聊聊"[start:end] == "随便聊聊", "跨度对得上原话",
          f"{start}-{end}")

    spoken = stage._write_candidate(
        dict(base, content="主人今天想早点睡", evidence="其实想早点睡"),
        "primary", "aiocqhttp", "*", "主人说：其实想早点睡\n助手：好", {"id": "j_say"})
    check(bool(spoken), "「主人说：」前缀也认作主人")


# --------------------------------------------------------------------------- #
# 3. 无前缀的旧调用仍可写（单说话者兼容）
# --------------------------------------------------------------------------- #

def test_legacy_single_speaker():
    print("\n[3] 无说话人前缀的旧调用仍可写入")
    store = fresh_store()
    stage = Stage3(store, {}, lambda *a, **k: "")
    rec = stage._write_candidate(
        {"kind": "fact", "content": "主人今天只想躺着", "confidence": 0.9,
         "evidence": "今天只想躺着"},
        "primary", "aiocqhttp", "*", "我今天只想躺着，什么都不想做", {"id": "j_legacy"})
    check(bool(rec), "单说话者转写能写入")


# --------------------------------------------------------------------------- #
# 4. 端到端：助手编造的内容不落库
# --------------------------------------------------------------------------- #

def test_end_to_end_speaker():
    print("\n[4] 整条提取链路：助手编造的内容不落库")
    store = fresh_store()
    stage = Stage3(store, CFG, extraction({
        "kind": "fact", "content": "主人养了企鹅", "confidence": 0.95,
        "evidence": "主人养了企鹅", "fact_key": "",
    }))

    async def _run():
        stage.capture_turn(session_id="s1", user_text="随便聊聊",
                           assistant_text="主人养了企鹅", ident=OWNER)
        return await stage.process_pending(force=True)

    out = asyncio.run(_run())
    check(out.get("written") == 0, "助手编造的内容没被写入", str(out))
    rows = store.list_records(owner_id="primary", limit=10)
    check(not any("企鹅" in r["content"] for r in rows), "库里没有企鹅")


# --------------------------------------------------------------------------- #
# 5. 提取侧：相反说法进待确认，不静默落库
# --------------------------------------------------------------------------- #

def test_extract_conflict():
    print("\n[5] 提取侧：极性相反的近似观察不落库、记待确认")
    store = fresh_store()
    add(store, "主人喜欢周末去海边骑自行车", kind="preference", epistemic="owner_asserted")
    stage = Stage3(store, CFG, lambda *a, **k: "")
    transcript = "主人：我周末不去海边骑车了\n助手：好"
    printed = {"kind": "preference", "confidence": 0.9,
               "content": "主人不喜欢周末去海边骑自行车",
               "evidence": "我周末不去海边骑车了"}

    try:
        stage._write_candidate(dict(printed), "primary", "aiocqhttp", "*", transcript, {"id": "j_p"})
        check(False, "相反说法被拒", "写进去了")
    except MemoryError as exc:
        check("待确认" in str(exc), "相反说法被拒并留待确认", str(exc))
    check(audit_count(store, "extract_conflict") >= 1, "冲突已记审计",
          str(audit_count(store, "extract_conflict")))
    check(len(store.list_records(owner_id="primary", limit=10)) == 1, "库里仍只有一条")

    try:
        stage._write_candidate({"kind": "preference", "confidence": 0.9,
                                "content": "主人喜欢周末去海边骑自行车",
                                "evidence": "我周末不去海边骑车了"},
                               "primary", "aiocqhttp", "*", transcript, {"id": "j_d"})
        check(False, "完全重复被拒", "写进去了")
    except MemoryError as exc:
        check("重复" in str(exc), "完全重复被拒", str(exc))


# --------------------------------------------------------------------------- #
# 6. 整理侧：相反意见不被去重并掉
# --------------------------------------------------------------------------- #

def test_reflect_dedupe():
    print("\n[6] 整理侧：相反意见不被去重并掉")
    store = fresh_store()
    left = add(store, "主人喜欢周末去海边骑自行车", kind="preference")
    right = add(store, "主人不喜欢周末去海边骑自行车", kind="preference")
    reflect = MemoryReflector(store, fake_llm("{}"), CFG)
    recs = store.list_records(owner_id="primary", limit=50)
    archives, dropped = reflect._dedupe(recs)

    check(archives == 0, "相反意见一条都没被归档", str(archives))
    check(dropped == 0, "也没有别的记录被顺手丢掉", str(dropped))
    check(store.get_record(left["id"])["status"] == "active", "「喜欢」仍然 active")
    check(store.get_record(right["id"])["status"] == "active", "「不喜欢」仍然 active")
    check(audit_count(store, "reflect_conflict") >= 1, "相反意见记了冲突审计",
          str(audit_count(store, "reflect_conflict")))

    twin_store = fresh_store()
    add(twin_store, "主人周末爱去海边骑自行车", kind="preference")
    add(twin_store, "主人周末爱去海边骑自行车", kind="preference")
    twin = MemoryReflector(twin_store, fake_llm("{}"), CFG)
    merged, _ = twin._dedupe(twin_store.list_records(owner_id="primary", limit=50))
    check(merged == 1, "真重复仍然合并成一条", str(merged))


# --------------------------------------------------------------------------- #
# 7. 派生失效：多层、环、共享子图
# --------------------------------------------------------------------------- #

def test_derived_cascade():
    print("\n[7] 派生失效：多层、环、共享子图")
    store = fresh_store()
    src = add(store, "源事实：主人去过哈尔滨", epistemic="owner_asserted")
    first = add(store, "一层派生：主人在北方待过", derived_from=[src["id"]], epistemic="derived")
    second = add(store, "二层派生：主人习惯北方的冷", derived_from=[first["id"]], epistemic="derived")
    store.forget_record(src["id"], reason="主人要求忘掉")
    check(store.get_record(first["id"])["status"] == "superseded", "一层派生被连带失效")
    check(store.get_record(second["id"])["status"] == "superseded", "二层派生也被连带失效")

    ring = fresh_store()
    a = add(ring, "环 A", epistemic="derived")
    b = add(ring, "环 B", derived_from=[a["id"]], epistemic="derived")
    ring.link_dependency(a["id"], b["id"])
    touched = ring.invalidate_derived(a["id"])
    check(touched >= 1, "环不会让失效遍历死循环", str(touched))
    check(ring.get_record(b["id"])["status"] == "superseded", "环上的另一条被归档")

    shared = fresh_store()
    root = add(shared, "共享源：主人常去海边", epistemic="owner_asserted")
    branch1 = add(shared, "分支一", derived_from=[root["id"]], epistemic="derived")
    branch2 = add(shared, "分支二", derived_from=[root["id"]], epistemic="derived")
    leaf = add(shared, "分支二的下游", derived_from=[branch2["id"]], epistemic="derived")
    shared.forget_record(root["id"], reason="主人要求忘掉")
    statuses = [shared.get_record(x["id"])["status"] for x in (branch1, branch2, leaf)]
    check(statuses == ["superseded"] * 3, "共享子图全部失效", str(statuses))


# --------------------------------------------------------------------------- #
# 8. 遗忘之后，旧证据不能再把同一件事写回来
# --------------------------------------------------------------------------- #

def test_tombstone_suppression():
    print("\n[8] 遗忘之后，旧证据不能再把同一件事写回来")
    store = fresh_store()
    old = add(store, "主人养了企鹅", evidence=[{"kind": "dialogue", "ref": "job:old",
                                            "excerpt": "我养了企鹅"}])
    store.forget_record(old["id"], reason="主人要求忘掉")
    check(store.tombstoned("主人养了企鹅"), "墓碑能按正文命中")

    stage = Stage3(store, CFG, lambda *a, **k: "")
    try:
        stage._write_candidate({"kind": "fact", "content": "主人养了企鹅", "confidence": 0.9,
                                "evidence": "我养了企鹅"},
                               "primary", "aiocqhttp", "*",
                               "主人：我养了企鹅\n助手：好可爱", {"id": "j_new"})
        check(False, "遗忘的内容被重新写入", "写进去了")
    except MemoryError as exc:
        check("遗忘" in str(exc), "遗忘的内容被再次拒收", str(exc))
    check(audit_count(store, "tombstone_block") >= 1, "墓碑拦截记了审计",
          str(audit_count(store, "tombstone_block")))


# --------------------------------------------------------------------------- #
# 9. 整理落库前复核来源
# --------------------------------------------------------------------------- #

def test_reflect_recheck_sources():
    print("\n[9] 整理落库前复核来源")
    store = fresh_store()
    left = add(store, "主人周末常去海边骑自行车")
    right = add(store, "主人喜欢在海边骑自行车看落日")
    reply = reflection({"fact_key": "", "content": "主人喜欢海边骑车",
                        "use": [left["id"], right["id"]]})
    reflect = MemoryReflector(store, reply, CFG)
    recs = [store.get_record(left["id"]), store.get_record(right["id"])]
    store.forget_record(right["id"], reason="主人要求忘掉")
    merged, dropped = asyncio.run(reflect._fold("primary", recs, "2026-10-25"))
    check(merged == 0, "来源失效后不再写归纳", str(merged))
    check(dropped >= 1, "失效簇被丢弃并计数", str(dropped))


# --------------------------------------------------------------------------- #
# 10. 依赖边表：写入即记边，旧库启动回填
# --------------------------------------------------------------------------- #

def test_edges_backfill():
    print("\n[10] 依赖边表：写入即记边，旧库启动回填")
    store = fresh_store()
    parent = add(store, "甲")
    child = add(store, "乙", derived_from=[parent["id"]], epistemic="derived")
    edges = store._conn.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0]
    check(int(edges) >= 1, "写派生记录时记了依赖边", str(edges))

    store._conn.execute("DELETE FROM memory_edges")
    store._conn.commit()
    path = store.db_path
    store.close()
    again = MemoryStore(path)
    backfilled = again._conn.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0]
    check(int(backfilled) >= 1, "重启时从 derived_from_ids 回填依赖边", str(backfilled))
    again.forget_record(parent["id"], reason="回填后的边也要能用")
    check(again.get_record(child["id"])["status"] == "superseded", "回填的边也能多层失效")
    again.close()


# --------------------------------------------------------------------------- #
# 11. 采集边界没有被放松
# --------------------------------------------------------------------------- #

def test_scope_guards():
    print("\n[11] 采集边界没被放松")
    store = fresh_store()
    stage = Stage3(store, CFG, extraction({
        "kind": "fact", "content": "随便什么", "confidence": 0.9, "evidence": "随便什么"}))
    group = stage.capture_turn(session_id="g1", user_text="我今天很累",
                               assistant_text="早点休息", ident=dict(OWNER, scene="group"))
    check(group is None, "群聊不入队")
    other = stage.capture_turn(session_id="p1", user_text="我今天很累",
                               assistant_text="早点休息", ident=dict(OWNER, is_owner=False))
    check(other is None, "非主人不入队")


def main() -> int:
    test_evidence_precision()
    test_speaker_attribution()
    test_legacy_single_speaker()
    test_end_to_end_speaker()
    test_extract_conflict()
    test_reflect_dedupe()
    test_derived_cascade()
    test_tombstone_suppression()
    test_reflect_recheck_sources()
    test_edges_backfill()
    test_scope_guards()
    print(f"\n通过 {PASS}，失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
