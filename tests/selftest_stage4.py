"""feiyu_memory 阶段四自测 + 检索层回归（临时库，不碰真实记忆库）。

覆盖 20 组：索引同步、FTS 命中、idf、MMR、单值约束、忘记后出索引、重建、
启动自愈、3000 条规模、截断上报、群聊隔离、build_context 报告、提取冲突留痕、
写入幂等、merge_records、整理删重复、整理归纳、编造被丢、主人原话不被覆盖、向量辅助。

跑法（在仓库根目录）：
  python tests/selftest_stage4.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

PLUGIN = (
    Path(os.environ["FEIYU_PLUGIN_DIR"])
    if os.environ.get("FEIYU_PLUGIN_DIR")
    else Path(__file__).resolve().parents[1] / "plugin" / "astrbot_plugin_feiyu_memory"
)
sys.path.insert(0, str(PLUGIN.parent))

import astrbot_plugin_feiyu_memory.memory_core as mc  # noqa: E402
from astrbot_plugin_feiyu_memory.memory_core import (  # noqa: E402
    MemoryStore, index_terms, fts_match_expr, text_similarity,
)
from astrbot_plugin_feiyu_memory.memory_stage3 import Stage3  # noqa: E402

MemoryError = mc.MemoryError

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)


def new_store(tmp: Path) -> MemoryStore:
    return MemoryStore(tmp / "t.db")


def add(store, content, *, kind="preference", fact_key="", imp=3,
        epi="owner_asserted", scope="owner_private", owner="primary"):
    return store.add_record(
        owner_id=owner, kind=kind, content=content, epistemic_type=epi,
        scope_type=scope, fact_key=fact_key, importance=imp,
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="feiyu_test_"))
    print(f"临时目录：{tmp}")
    try:
        run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{', '.join(FAILS)}")
        return 1
    print("全部通过")
    return 0


def run(tmp: Path) -> None:
    print("\n[1] 建库 / 索引随写入同步")
    store = new_store(tmp)
    store.bind_owner("aiocqhttp", "10086", owner_label="主人")
    add(store, "主人喜欢在深夜写代码，配着黑咖啡", fact_key="like.night")
    add(store, "主人养了一只叫团子的橘猫", fact_key="pet.cat")
    add(store, "主人对花生过敏", fact_key="health.peanut")
    s = store.stats()
    check("active=3", s["active"] == 3, str(s["active"]))
    check("索引可用", s["index_ok"] is True)
    check("索引条数=active", s["index_rows"] == s["active"], f"{s['index_rows']}/{s['active']}")

    print("\n[2] 检索命中")
    st: dict = {}
    hits = store.search("团子最近怎么样", owner_id="primary", is_owner=True,
                        scene="private", limit=4, stats=st)
    check("走 fts 路径", st.get("mode") == "fts", str(st))
    check("命中猫那条", any("橘猫" in h["content"] for h in hits),
          str([h["content"][:12] for h in hits]))

    print("\n[3] idf：生僻词的权重高于常见词")
    add(store, "主人今天又说了一次代码的事", fact_key="tmp.code1")
    add(store, "主人说代码要写好注释", fact_key="tmp.code2")
    add(store, "主人提到那只橘猫掉毛", fact_key="tmp.cat2")
    add(store, "主人提过花生酱不能吃", fact_key="tmp.peanut2")
    w = store._idf_weights(index_terms("代码 花生"))
    check("花生 idf > 代码 idf", w.get("花生", 0) > w.get("代码", 0),
          f"花生={w.get('花生'):.3f} 代码={w.get('代码'):.3f}")

    print("\n[4] MMR：相近的两条不会占满名额")
    store2 = new_store(tmp)
    store2.bind_owner("aiocqhttp", "10086")
    add(store2, "主人喜欢深夜写代码配黑咖啡", fact_key="a1")
    add(store2, "主人喜欢深夜写代码，还要一杯黑咖啡", fact_key="a2")
    add(store2, "主人写代码时讨厌被打断", fact_key="b1")
    st2: dict = {}
    plain = store2.search("深夜写代码", owner_id="primary", is_owner=True,
                          scene="private", limit=2, mmr=False, stats=st2)
    st3: dict = {}
    div = store2.search("深夜写代码", owner_id="primary", is_owner=True,
                        scene="private", limit=2, mmr=True, stats=st3)
    check("不开 MMR 时两条都在", len(plain) == 2)
    check("MMR 生效标记", st3.get("mmr") is True, str(st3))
    check("MMR 结果不重复", len({h["fact_key"] for h in div}) == len(div),
          str([h["fact_key"] for h in div]))

    print("\n[5] single_value：同主题自动取代，绕过 API 直插会被库拒绝")
    first = add(store, "主人最喜欢的颜色是蓝色", kind="preference",
                fact_key="fav.color")
    second = add(store, "主人最喜欢的颜色改成绿色", kind="preference",
                 fact_key="fav.color")
    check("新记录接到旧记录", second.get("supersedes_id") == first["id"],
          str(second.get("supersedes_id")))
    alive = [r for r in store.list_records(owner_id="primary", limit=50)
             if r["fact_key"] == "fav.color" and r["status"] == "active"]
    check("同主题只剩一条 active", len(alive) == 1, str(len(alive)))
    try:
        with store._tx() as c:
            c.execute(
                "INSERT INTO memory_records(id, owner_id, subject_id, platform, "
                "bot_instance, scope_type, scope_id, kind, epistemic_type, fact_key, "
                "content, source_ids, derived_from_ids, asserted_at, valid_from, "
                "confidence, importance, status, revision, deletion_epoch, "
                "persona_version, created_at, updated_at, created_by) VALUES("
                "'r_dup_test','primary','primary','','*','owner_private','','preference',"
                "'owner_asserted','fav.color','重复的一条','[]','[]','2026-01-01',"
                "'2026-01-01',0.9,3,'active',1,0,'v3','2026-01-01','2026-01-01','owner')"
            )
        check("直接插重复 active 被拒", False, "居然插进去了")
    except sqlite3.IntegrityError:
        check("直接插重复 active 被拒", True)
    except Exception as exc:  # noqa: BLE001
        check("直接插重复 active 被拒", False, f"{type(exc).__name__}: {exc}")
    # 回滚掉上面那次失败的插入
    try:
        store._conn.rollback()
    except sqlite3.Error:
        pass

    print("\n[6] 忘记 / 取代之后索引里不再有它")
    victim = add(store, "主人临时说想学做菜", fact_key="tmp.cook")
    store.forget_record(victim["id"], actor="test")
    st4: dict = {}
    hits = store.search("学做菜", owner_id="primary", is_owner=True,
                        scene="private", limit=4, stats=st4)
    check("忘记后搜不到", not any(h["id"] == victim["id"] for h in hits),
          str([h["content"][:8] for h in hits]))
    row = store._conn.execute(
        "SELECT COUNT(*) FROM memory_search WHERE record_id=?", (victim["id"],)
    ).fetchone()
    check("忘记后索引里也没有", row[0] == 0, str(row[0]))

    print("\n[7] 索引可重建，且与权威表对齐")
    store.rebuild_search_index()
    s = store.stats()
    check("重建后条数=active", s["index_rows"] == s["active"],
          f"{s['index_rows']}/{s['active']}")

    print("\n[8] 删索引文件后启动自愈")
    with store._tx() as c:
        c.execute("DELETE FROM memory_search")
    store2 = MemoryStore(tmp / "t.db")  # 重新打开跑 _ensure_search_index
    s = store2.stats()
    check("重开后索引自动补回", s["index_rows"] == s["active"],
          f"{s['index_rows']}/{s['active']}")

    print("\n[9] 规模：3000 条下检索仍走索引且有界")
    big = new_store(tmp)
    big.bind_owner("aiocqhttp", "10086")
    t0 = time.time()
    with big._tx() as c:
        for i in range(3000):
            words = ["代码", "猫", "咖啡", "雨", "吉他", "做饭", "旅行", "电影"]
            text = f"第{i}条测试记录 关于{words[i % 8]}和{words[(i * 3) % 8]}"
            c.execute(
                "INSERT INTO memory_records(id, owner_id, subject_id, platform, "
                "bot_instance, scope_type, scope_id, kind, epistemic_type, fact_key, "
                "content, source_ids, derived_from_ids, asserted_at, valid_from, "
                "confidence, importance, status, revision, deletion_epoch, "
                "persona_version, created_at, updated_at, created_by) VALUES("
                "?,'primary','primary','','*','owner_private','','fact',"
                "'owner_asserted','big.'||? ,?,'[]','[]','2026-01-01','2026-01-01',"
                "0.9,3,'active',1,0,'v3','2026-01-01','2026-01-01','owner')",
                (f"r_big{i}", i, text),
            )
    big.rebuild_search_index()
    build = time.time() - t0
    st5: dict = {}
    t1 = time.time()
    hits = big.search("吉他 旅行", owner_id="primary", is_owner=True,
                      scene="private", limit=4, stats=st5)
    cost = (time.time() - t1) * 1000
    check("3000 条下仍走 fts", st5.get("mode") == "fts", str(st5))
    check("检索耗时 < 50ms", cost < 50, f"{cost:.1f}ms")
    check("返回条数受 limit 限制", len(hits) <= 4, str(len(hits)))
    print(f"       建索引 {build:.2f}s，检索 {cost:.1f}ms，候选 {st5.get('candidates')}")

    print("\n[10] candidate_limit 截断会显式上报")
    st6: dict = {}
    big.search("代码", owner_id="primary", is_owner=True, scene="private",
               limit=4, candidate_limit=5, stats=st6)
    check("截断标记为真", st6.get("truncated") is True, str(st6))
    st7: dict = {}
    big.search("代码", owner_id="primary", is_owner=True, scene="private",
               limit=4, candidate_limit=2000, stats=st7)
    check("放大上限后不再截断", not st7.get("truncated"), str(st7))

    print("\n[11] 群聊只看公开版")
    add(store, "主人的私密偏好：不喜欢被叫小名", fact_key="secret.nick",
        scope="owner_private")
    add(store, "主人可以公开说：喜欢下雨天", fact_key="public.rain",
        scope="owner_public")
    st8: dict = {}
    grp = store.search("小名 下雨天", owner_id="primary", is_owner=True,
                       scene="group", group_id="g1", limit=6, stats=st8)
    contents = " ".join(h["content"] for h in grp)
    check("群聊不含私密条目", "小名" not in contents, contents[:40])
    check("群聊含公开条目", "下雨天" in contents, contents[:40])

    print("\n[12] build_context 的报告字段")
    ctx = store.build_context(
        is_owner=True, scene="private", user_id="10086",
        owner={"owner_id": "primary", "owner_label": "主人"},
        query="猫", cfg={}, degraded="",
    )
    check("返回 search 统计", isinstance(ctx.get("search"), dict), str(ctx.get("search")))
    check("state 里有身份行", "主人" in ctx["state"])
    check("memory 有参考资料头", "记忆参考资料" in ctx["memory"])

    print("\n[13] 自动提取：撞上主人说法时留痕而不是静默丢")
    s3store = new_store(tmp)
    s3store.bind_owner("aiocqhttp", "10086")
    add(s3store, "主人最喜欢的颜色是蓝色", kind="preference", fact_key="fav.color")
    s3 = Stage3(s3store, {}, lambda *a, **k: "")
    transcript = "主人说：其实我现在更喜欢绿色了"
    job = {"id": "job_test"}

    cand = {
        "kind": "preference",
        "content": "主人更喜欢绿色了",
        "confidence": 0.9,
        "evidence": "其实我现在更喜欢绿色了",
        "fact_key": "fav.color",
    }
    try:
        s3._write_candidate(cand, "primary", "aiocqhttp", "*", transcript, job)
        check("撞 fact_key 被拒", False, "写进去了")
    except MemoryError as exc:
        check("撞 fact_key 被拒", "待确认冲突" in str(exc), str(exc))
    rows = s3store._conn.execute(
        "SELECT COUNT(*) FROM memory_audit WHERE action='extract_conflict'"
    ).fetchone()
    check("冲突已记审计", rows[0] == 1, str(rows[0]))

    dup = {
        "kind": "preference",
        "content": "主人最喜欢的颜色是蓝色",
        "confidence": 0.9,
        "evidence": "其实我现在更喜欢绿色了",
        "fact_key": "",
    }
    try:
        s3._write_candidate(dup, "primary", "aiocqhttp", "*", transcript, job)
        check("完全重复被拒", False, "写进去了")
    except MemoryError as exc:
        check("完全重复被拒", "重复" in str(exc), str(exc))

    fresh = {
        "kind": "fact",
        "content": "主人今天想去吃面",
        "confidence": 0.9,
        "evidence": "其实我现在更喜欢绿色了",
        "fact_key": "tmp.noodle",
    }
    rec = s3._write_candidate(fresh, "primary", "aiocqhttp", "*", transcript, job)
    check("正常候选能写入", bool(rec and rec.get("id")), str(rec and rec.get("id")))

    run_stage4(tmp)


# --------------------------------------------------------------------------- #
# 阶段四：幂等 / 合并 / 整理
# --------------------------------------------------------------------------- #

def _fake_llm(reply: str):
    async def _call(system: str, user: str, model: str = "") -> str:
        return reply
    return _call


def run_stage4(tmp: Path) -> None:
    import asyncio
    from astrbot_plugin_feiyu_memory.memory_reflect import MemoryReflector

    # 这一节测的是检索/整理，不关心证据纪律：给转述类记录补一条自测证据。
    _orig_add = MemoryStore.add_record

    def _add_with_evidence(self, **kw):
        if not kw.get("evidence") and kw.get("epistemic_type") == "observed_dialogue":
            kw["evidence"] = [{
                "kind": "dialogue", "ref": "job:selftest",
                "excerpt": str(kw.get("content", ""))[:60],
            }]
        return _orig_add(self, **kw)

    MemoryStore.add_record = _add_with_evidence

    print("\n[14] 写入幂等（同一 op_key 不写第二条）")
    st = MemoryStore(tmp / "idem.db")
    st.bind_owner("aiocqhttp", "10086", owner_label="主人")
    a = st.add_record(owner_id="primary", kind="event", content="主人今天去看了海",
                      epistemic_type="observed_dialogue",
                      scope_type="owner_private", op_key="extract:aaa")
    b = st.add_record(owner_id="primary", kind="event", content="主人今天去看了海",
                      epistemic_type="observed_dialogue",
                      scope_type="owner_private", op_key="extract:aaa")
    check("同一个 op_key 返回同一条", b.get("id") == a.get("id"), str(b.get("id")))
    check("标了 _idempotent", b.get("_idempotent") is True)
    check("库里只有一条", len(st.list_records(owner_id="primary")) == 1,
          str(len(st.list_records(owner_id="primary"))))
    c = st.add_record(owner_id="primary", kind="event", content="主人今天去看了海",
                      epistemic_type="observed_dialogue",
                      scope_type="owner_private", op_key="extract:bbb")
    check("不同 op_key 各自入库", c.get("id") != a.get("id"))
    try:
        st._conn.execute(
            "INSERT INTO memory_records(id, owner_id, subject_id, platform,"
            " bot_instance, scope_type, scope_id, kind, epistemic_type, fact_key,"
            " content, source_ids, derived_from_ids, occurred_at, asserted_at,"
            " valid_from, valid_to, confidence, importance, status, expires_at,"
            " revision, supersedes_id, deletion_epoch, persona_version, created_at,"
            " updated_at, created_by, op_key) VALUES"
            "('rx','primary','primary','','','owner_private','','event',"
            "'observed_dialogue','','x','[]','[]','','','','',0.5,1,'active',"
            "NULL,1,NULL,0,1,'','','t','extract:aaa')"
        )
        st._conn.commit()
        check("直接插重复 op_key 被拒", False, "居然插进去了")
    except sqlite3.IntegrityError:
        check("直接插重复 op_key 被拒", True)

    print("\n[15] merge_records：归档重复并继承证据")
    keep = st.add_record(owner_id="primary", kind="fact", content="主人住在这个城市",
                         epistemic_type="observed_dialogue", scope_type="owner_private",
                         evidence=[{"kind": "dialogue", "ref": "job:1", "excerpt": "证据一"}])
    drop = st.add_record(owner_id="primary", kind="fact", content="主人住在本地",
                         epistemic_type="observed_dialogue", scope_type="owner_private",
                         evidence=[{"kind": "dialogue", "ref": "job:2", "excerpt": "证据二"}])
    merged_n = st.merge_records(keep["id"], [drop["id"]])
    check("归档 1 条", merged_n == 1, str(merged_n))
    check("被并的已 superseded",
          st.get_record(drop["id"])["status"] == "superseded",
          st.get_record(drop["id"])["status"])
    ev = st._conn.execute(
        "SELECT COUNT(*) FROM memory_evidence WHERE record_id=?", (keep["id"],)
    ).fetchone()[0]
    check("证据合到保留的那条", ev == 2, str(ev))

    print("\n[16] 整理：删重复（不需要模型）")
    st2 = MemoryStore(tmp / "reflect.db")
    st2.bind_owner("aiocqhttp", "10086", owner_label="主人")
    st2.add_record(owner_id="primary", kind="fact", content="主人喜欢喝黑咖啡",
                   epistemic_type="observed_dialogue", scope_type="owner_private")
    st2.add_record(owner_id="primary", kind="fact", content="主人喜欢喝黑咖啡。",
                   epistemic_type="observed_dialogue", scope_type="owner_private")
    st2.add_record(owner_id="primary", kind="fact", content="主人周五要去看牙医",
                   epistemic_type="observed_dialogue", scope_type="owner_private")
    refl = MemoryReflector(st2, _fake_llm('{"merged":[]}'), {"reflect_enable": True, "reflect_min_records": 2})
    res = asyncio.run(refl.run(force=True))
    check("整理跑成功", res.get("ok") is True, str(res))
    check("归档了重复", res.get("deduped", 0) >= 1, str(res.get("deduped")))
    alive = st2.list_records(owner_id="primary")
    check("重复只剩一条", len([r for r in alive if "黑咖啡" in r["content"]]) == 1,
          str([r["content"] for r in alive]))
    check("不相关的没动", any("牙医" in r["content"] for r in alive))
    check("审计记了 reflect_pass", st2._conn.execute(
        "SELECT COUNT(*) FROM memory_audit WHERE action='reflect_pass'"
    ).fetchone()[0] == 1)

    print("\n[17] 整理：归纳同主题（模型给出合并句）")
    ids = [r["id"] for r in st2.list_records(owner_id="primary")]
    pair = []
    pair.append(st2.add_record(owner_id="primary", kind="preference",
                               content="主人喝咖啡只喝冰的",
                               epistemic_type="observed_dialogue",
                               scope_type="owner_private"))
    pair.append(st2.add_record(owner_id="primary", kind="preference",
                               content="主人喝咖啡不加糖",
                               epistemic_type="observed_dialogue",
                               scope_type="owner_private"))
    reply = ('{"merged":[{"fact_key":"coffee","content":"主人喝咖啡只喝冰的、不加糖",'
             '"use":["%s","%s"]}]}' % (pair[0]["id"], pair[1]["id"]))
    refl2 = MemoryReflector(st2, _fake_llm(reply), {"reflect_enable": True,
                                                    "reflect_dedupe": 0.99,
                                                    "reflect_min_records": 2})
    res2 = asyncio.run(refl2.run(force=True))
    check("归纳出 1 条", res2.get("merged", 0) == 1, str(res2))
    folded = [r for r in st2.list_records(owner_id="primary")
              if r.get("created_by") == "reflect"]
    check("归纳记录是 derived", bool(folded) and folded[0]["epistemic_type"] == "derived",
          folded[0]["epistemic_type"] if folded else "none")
    import json as _json
    raw_ids = folded[0]["derived_from_ids"] if folded else []
    got = set(_json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids)
    check("带着来源 id", got == {pair[0]["id"], pair[1]["id"]},
          f"{sorted(got)} vs {sorted([pair[0]['id'], pair[1]['id']])}")
    st_orig = [st2.get_record(p["id"])["status"] for p in pair]
    check("被归纳的两条已归档", st_orig == ["superseded", "superseded"], str(st_orig))

    print("\n[18] 整理：模型编造的内容被丢弃")
    st3 = MemoryStore(tmp / "guard.db")
    st3.bind_owner("aiocqhttp", "10086", owner_label="主人")
    g = [st3.add_record(owner_id="primary", kind="preference",
                        content="主人爱吃辣", epistemic_type="observed_dialogue",
                        scope_type="owner_private"),
         st3.add_record(owner_id="primary", kind="preference",
                        content="主人爱吃川菜", epistemic_type="observed_dialogue",
                        scope_type="owner_private")]
    bad = ('{"merged":[{"fact_key":"x","content":"主人养了三只哈士奇",'
           '"use":["%s","%s"]}]}' % (g[0]["id"], g[1]["id"]))
    refl3 = MemoryReflector(st3, _fake_llm(bad), {"reflect_enable": True,
                                                  "reflect_dedupe": 0.99,
                                                  "reflect_min_records": 2})
    res3 = asyncio.run(refl3.run(force=True))
    check("一条都没写", res3.get("merged", 0) == 0, str(res3))
    check("记了丢弃", res3.get("dropped", 0) >= 1, str(res3.get("dropped")))
    check("库里没有哈士奇",
          not any("哈士奇" in r["content"] for r in st3.list_records(owner_id="primary")))

    print("\n[19] 整理：不覆盖主人亲口说的话")
    st4 = MemoryStore(tmp / "owner.db")
    st4.bind_owner("aiocqhttp", "10086", owner_label="主人")
    o = st4.add_record(owner_id="primary", kind="preference",
                       content="主人说自己爱吃甜", epistemic_type="owner_asserted",
                       scope_type="owner_private", fact_key="taste.sweet")
    d1 = st4.add_record(owner_id="primary", kind="preference",
                        content="主人吃了一块蛋糕", epistemic_type="observed_dialogue",
                        scope_type="owner_private", fact_key="taste.sweet.cake")
    d2 = st4.add_record(owner_id="primary", kind="preference",
                        content="主人又吃了一块蛋糕", epistemic_type="observed_dialogue",
                        scope_type="owner_private", fact_key="taste.sweet.cake2")
    reply4 = ('{"merged":[{"fact_key":"taste.sweet","content":"主人爱吃甜蛋糕",'
              '"use":["%s","%s"]}]}' % (d1["id"], d2["id"]))
    refl4 = MemoryReflector(st4, _fake_llm(reply4), {"reflect_enable": True,
                                                     "reflect_dedupe": 0.99,
                                                     "reflect_min_records": 2})
    asyncio.run(refl4.run(force=True))
    owner_rec = st4.get_record(o["id"])
    check("主人原话仍然 active", owner_rec["status"] == "active", owner_rec["status"])
    check("主人原话没被改写", owner_rec["content"] == "主人说自己爱吃甜",
          owner_rec["content"])

    print("\n[20] 向量通道辅助方法")
    st5 = MemoryStore(tmp / "vec.db")
    st5.bind_owner("aiocqhttp", "10086", owner_label="主人")
    r1 = st5.add_record(owner_id="primary", kind="fact", content="主人有一台旧相机",
                        epistemic_type="observed_dialogue", scope_type="owner_private")
    st5.add_record(owner_id="primary", kind="fact", content="主人喜欢胶片",
                   epistemic_type="observed_dialogue", scope_type="owner_private")
    miss = st5.list_missing_embeddings(limit=10)
    check("缺失列表含全部记录", len(miss) == 2, str(len(miss)))
    st5.put_embedding(r1["id"], "text-embedding-v4", [0.1, 0.2, 0.3])
    miss2 = st5.list_missing_embeddings(limit=10)
    check("补过的不再出现", r1["id"] not in {m["id"] for m in miss2}, str(len(miss2)))
    cov = st5.vector_coverage()
    check("覆盖率 1/2", cov == {"active": 2, "covered": 1}, str(cov))
    s5 = st5.stats()
    check("stats 有 vector_ready", s5.get("vector_ready") is True, str(s5.get("vector_ready")))
    check("stats 有 covered/missing", (s5.get("covered"), s5.get("vector_missing")) == (1, 1),
          f"{s5.get('covered')}/{s5.get('vector_missing')}")



if __name__ == "__main__":
    raise SystemExit(main())
