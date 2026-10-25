"""M2 混合检索与记忆操作协议回归（2026-10-25）。

对应实施方案 §9 M2 与对比报告 §4.1、§5-B/C/D 的五件事：

1. 词法与语义**各自独立召回**，再用 RRF 融合（改前是「先 FTS 取候选、再算 cosine」，
   所以 `feline` 查不到「主人钟爱猫咪」）；
2. 向量读写校验 `embedding_model / version / dim`，不一致不参与打分，并给出
   陈旧向量清单与覆盖率（改前读了 model 但不过滤）；
3. `add_record` 的 `op_key` 幂等带 `payload_hash`：同键不同内容算冲突，
   `correct_record` 支持 `expected_revision`（CAS）；
4. 反思改成「提案 → 复核 → 应用」两段式，提案与逐项回执都落库；
5. 检索 trace：候选数、注入数、截断原因，能回答「她为什么没想起来」。

只依赖标准库。运行：
    python tests/selftest_recall.py
    python tools/run_selftests.py recall
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

from astrbot_plugin_feiyu_memory.memory_core import (  # noqa: E402
    MemoryError,
    MemoryStore,
    _rrf_score,
)
from astrbot_plugin_feiyu_memory.memory_reflect import MemoryReflector  # noqa: E402

TMP_ROOT = ROOT / "tests" / "_tmp" / "recall"
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


OWNER = {"owner_id": "primary", "owner_label": "主人"}
META = {"model": "test-embed", "version": "v1", "dim": 2}

CFG = {
    "reflect_enable": True,
    "reflect_dedupe": 0.88,
    "reflect_fold": 0.45,
    "reflect_min_fold": 2,
    "reflect_min_records": 2,
    "reflect_group_limit": 8,
    "reflect_days": 30,
    "reflect_batch": 120,
}


def fake_llm(reply: str):
    async def _call(system: str, user: str, model: str = "") -> str:
        return reply
    return _call


def add(store: MemoryStore, content: str, *, kind: str = "fact",
        epistemic: str = "owner_asserted", scopes: str = "owner_private",
        **kw):
    return store.add_record(owner_id="primary", kind=kind, content=content,
                            epistemic_type=epistemic, scope_type=scopes, **kw)


def recall(store: MemoryStore, query: str, *, vector=None, meta=None,
           limit: int = 6, mmr: bool = False, min_score: float = 2.5):
    stats: dict = {}
    hits = store.search(
        query, owner_id="primary", is_owner=True, scene="private",
        user_id="10001", limit=limit, mmr=mmr, min_score=min_score,
        vector=vector, vector_meta=meta, stats=stats,
    )
    return hits, stats


# --------------------------------------------------------------------------- #
# [1] 语义独立召回
# --------------------------------------------------------------------------- #

def test_semantic_recall():
    print("\n[1] 语义通道独立召回（词法完全不命中也要能想起来）")
    store = fresh_store()
    cat = add(store, "主人钟爱猫咪", epistemic="owner_asserted")
    spicy = add(store, "主人很能吃辣", epistemic="owner_asserted")
    store.put_embedding(cat["id"], "test-embed", [1.0, 0.0], version="v1")
    store.put_embedding(spicy["id"], "test-embed", [0.0, 1.0], version="v1")

    hits, stats = recall(store, "feline", vector=[1.0, 0.0], meta=META)
    contents = [h["content"] for h in hits]
    check(any("猫咪" in c for c in contents), "词法零命中时靠语义想起来", str(contents))
    check(not any("辣" in c for c in contents),
          "没把不相关的记录一起捞上来", str(contents))
    check(stats.get("semantic_candidates", 0) >= 1, "trace 记了语义候选数", str(stats))
    check(str(stats.get("mode", "")).startswith(("hybrid", "semantic", "vector")),
          "trace 标出本轮用了混合/语义通道", str(stats.get("mode")))

    # 没有向量时仍然只走词法：不能凭空想起
    hits2, stats2 = recall(store, "feline")
    check(not hits2, "没给查询向量时不假装语义召回", str([h["content"] for h in hits2]))


# --------------------------------------------------------------------------- #
# [2] 词法通道不被语义挤掉
# --------------------------------------------------------------------------- #

def test_lexical_kept():
    print("\n[2] 两路各自独立：词法命中的记录不会被语义挤掉")
    store = fresh_store()
    ragdoll = add(store, "主人养了一只布偶猫", epistemic="owner_asserted")
    other = add(store, "主人喜欢吃苹果", epistemic="owner_asserted")
    store.put_embedding(ragdoll["id"], "test-embed", [0.0, 1.0], version="v1")
    store.put_embedding(other["id"], "test-embed", [1.0, 0.0], version="v1")

    hits, stats = recall(store, "布偶", vector=[1.0, 0.0], meta=META, min_score=1.0)
    contents = [h["content"] for h in hits]
    check(any("布偶" in c for c in contents), "词法命中的记录还在", str(contents))
    check(stats.get("lexical_candidates", 0) >= 1, "trace 记了词法候选数", str(stats))


# --------------------------------------------------------------------------- #
# [3] RRF 融合
# --------------------------------------------------------------------------- #

def test_rrf_fusion():
    print("\n[3] RRF 融合：两路都命中的排最前")
    check(abs(_rrf_score([1, 1]) - (1 / 61 + 1 / 61)) < 1e-9,
          "RRF 公式是 Σ 1/(60+rank)", str(_rrf_score([1, 1])))
    check(_rrf_score([1, None]) < _rrf_score([1, 1]),
          "只有一路命中时分数更低", str(_rrf_score([1, None])))

    store = fresh_store()
    a = add(store, "主人喜欢喝冰美式咖啡", epistemic="owner_asserted")
    b = add(store, "主人觉得那种深色热饮太苦", epistemic="owner_asserted")
    c = add(store, "主人每天早上都要喝咖啡", epistemic="owner_asserted")
    store.put_embedding(a["id"], "test-embed", [0.0, 1.0], version="v1")
    store.put_embedding(b["id"], "test-embed", [1.0, 0.0], version="v1")
    store.put_embedding(c["id"], "test-embed", [0.9, 0.1], version="v1")

    hits, stats = recall(store, "咖啡", vector=[1.0, 0.0], meta=META, min_score=1.0)
    contents = [h["content"] for h in hits]
    check(any("冰美式" in x for x in contents), "纯词法命中的也在", str(contents))
    check(any("深色热饮" in x for x in contents), "纯语义命中的也在", str(contents))
    check(contents and "每天早上" in contents[0], "两路都命中的排最前", str(contents))
    check(stats.get("rrf") is True, "trace 标出用了 RRF", str(stats))


# --------------------------------------------------------------------------- #
# [4] 向量型号校验
# --------------------------------------------------------------------------- #

def test_vector_meta():
    print("\n[4] 向量要校验 model/version/dim，不合的不参与打分")
    store = fresh_store()
    rec = add(store, "主人钟爱猫咪", epistemic="owner_asserted")
    store.put_embedding(rec["id"], "old-embed", [1.0, 0.0], version="v0")

    hits, stats = recall(store, "feline", vector=[1.0, 0.0], meta=META)
    check(not hits, "换了嵌入模型后旧向量不参与召回",
          str([h["content"] for h in hits]))
    check(stats.get("vector_mismatch", 0) >= 1, "trace 记了型号不符的条数", str(stats))

    health = store.vector_health(META)
    check(health.get("matching") == 0, "健康表：当前型号匹配 0 条", str(health))
    check(health.get("stale", 0) >= 1, "健康表：陈旧向量 >=1 条", str(health))
    check(health.get("dim") == 2, "健康表带上了期望维度", str(health))

    stale = store.list_stale_embeddings(META)
    check([r["id"] for r in stale] == [rec["id"]], "能列出要重建的记录", str(stale))

    # 维度对不上也一样算陈旧
    health3 = store.vector_health({"model": "test-embed", "version": "v1", "dim": 3})
    check(health3.get("matching") == 0, "维度不符也不匹配", str(health3))

    # 型号对得上就正常参与
    store.put_embedding(rec["id"], "test-embed", [1.0, 0.0], version="v1")
    hits2, stats2 = recall(store, "feline", vector=[1.0, 0.0], meta=META)
    check(bool(hits2), "重建后语义又能召回", str([h["content"] for h in hits2]))
    check(stats2.get("vector_mismatch", 0) == 0, "重建后没有陈旧向量", str(stats2))


# --------------------------------------------------------------------------- #
# [5] 幂等 payload_hash 与 CAS
# --------------------------------------------------------------------------- #

def test_operation_protocol():
    print("\n[5] 操作协议：同键不同内容算冲突，纠正要带 expected_revision")
    store = fresh_store()
    first = add(store, "主人喜欢喝绿茶", op_key="op:tea")
    second = add(store, "主人喜欢喝绿茶", op_key="op:tea")
    check(second["id"] == first["id"], "同键同内容幂等命中同一条", second["id"])
    check(second.get("_idempotent") is True, "第二次标了幂等命中")
    check(len(store.list_records(owner_id="primary", limit=50)) == 1,
          "库里只有一条")

    try:
        add(store, "主人讨厌喝绿茶", op_key="op:tea")
        check(False, "同键不同内容应报冲突")
    except MemoryError as exc:
        check("冲突" in str(exc), "同键不同内容报冲突", str(exc))

    cor = store.correct_record(first["id"], "主人喜欢喝乌龙茶", expected_revision=1)
    check(int(cor["revision"]) == 2, "CAS 命中后修订号 +1", str(cor.get("revision")))
    check(cor["content"] == "主人喜欢喝乌龙茶", "内容已纠正", cor["content"])

    other = add(store, "主人早睡")
    try:
        store.correct_record(other["id"], "主人晚睡", expected_revision=99)
        check(False, "修订号不符应拒绝")
    except MemoryError as exc:
        check("已被改写" in str(exc) or "修订" in str(exc),
              "修订号不符时明确拒绝", str(exc))


# --------------------------------------------------------------------------- #
# [6] 反思两段式
# --------------------------------------------------------------------------- #

def _seed_pair(store: MemoryStore):
    one = add(store, "主人周末常去海边骑自行车", kind="preference",
              epistemic="observed_dialogue",
              evidence=[{"kind": "dialogue", "ref": "job:1#m0",
                         "excerpt": "主人周末常去海边骑自行车"}])
    two = add(store, "主人喜欢在海边骑自行车看落日", kind="preference",
              epistemic="observed_dialogue",
              evidence=[{"kind": "dialogue", "ref": "job:2#m0",
                         "excerpt": "主人喜欢在海边骑自行车看落日"}])
    return one, two


def test_reflect_two_phase():
    print("\n[6] 反思：提案 → 复核 → 应用，逐项回执")
    store = fresh_store()
    one, two = _seed_pair(store)
    reply = json.dumps({"merged": [{
        "fact_key": "weekend.sea",
        "content": "主人喜欢周末去海边骑车看落日",
        "use": [one["id"], two["id"]],
    }]}, ensure_ascii=False)
    refl = MemoryReflector(store, fake_llm(reply), CFG)

    proposal = asyncio.run(refl.propose(force=True))
    check(proposal.get("status") == "pending", "提案落库为 pending", str(proposal))
    check(len(proposal.get("items") or []) == 1, "提案带了 1 个待应用项",
          str(len(proposal.get("items") or [])))
    check(not [r for r in store.list_records(owner_id="primary", limit=50)
               if r.get("created_by") == "reflect"],
          "提案阶段先不写库")
    check(store._conn.execute(
        "SELECT COUNT(*) FROM memory_audit WHERE action='reflect_propose'"
    ).fetchone()[0] == 1, "记了 reflect_propose")

    res = asyncio.run(refl.apply(proposal["proposal_id"]))
    check(res.get("applied") == 1, "复核通过后应用 1 条", str(res))
    folded = [r for r in store.list_records(owner_id="primary", limit=50)
              if r.get("created_by") == "reflect"]
    check(len(folded) == 1 and folded[0]["epistemic_type"] == "derived",
          "归纳记录是 derived", str(folded))
    after = store.get_proposal(proposal["proposal_id"])
    check(after.get("status") == "applied", "提案状态变 applied", str(after.get("status")))
    check((after.get("items") or [{}])[0].get("status") == "applied",
          "逐项回执标了 applied", str(after.get("items")))

    res2 = asyncio.run(refl.apply(proposal["proposal_id"]))
    check(res2.get("applied") == 0, "重复应用不再写", str(res2))
    check(len([r for r in store.list_records(owner_id="primary", limit=50)
               if r.get("created_by") == "reflect"]) == 1, "库里仍只有 1 条归纳")

    # 来源在应用前被忘记 → 整项丢弃并留回执
    store2 = fresh_store()
    a, b = _seed_pair(store2)
    reply2 = json.dumps({"merged": [{
        "fact_key": "weekend.sea",
        "content": "主人喜欢周末去海边骑车看落日",
        "use": [a["id"], b["id"]],
    }]}, ensure_ascii=False)
    refl2 = MemoryReflector(store2, fake_llm(reply2), CFG)
    p2 = asyncio.run(refl2.propose(force=True))
    store2.forget_record(b["id"], reason="用户要求删除")
    res3 = asyncio.run(refl2.apply(p2["proposal_id"]))
    check(res3.get("applied") == 0, "来源失效后不写归纳", str(res3))
    check(res3.get("dropped", 0) >= 1, "失效项被丢弃并计数", str(res3))
    item = (store2.get_proposal(p2["proposal_id"]).get("items") or [{}])[0]
    check(item.get("status") == "dropped", "回执标了 dropped", str(item))
    check("来源" in str(item.get("reason", "")), "回执写了原因", str(item.get("reason")))

    # run() 仍然是一次到位（兼容旧行为）
    store3 = fresh_store()
    c, d = _seed_pair(store3)
    reply3 = json.dumps({"merged": [{
        "fact_key": "weekend.sea",
        "content": "主人喜欢周末去海边骑车看落日",
        "use": [c["id"], d["id"]],
    }]}, ensure_ascii=False)
    res4 = asyncio.run(MemoryReflector(store3, fake_llm(reply3), CFG).run(force=True))
    check(res4.get("ok") is True, "run() 仍然返回 ok", str(res4))
    check(res4.get("merged") == 1, "run() 一次跑完提案与应用", str(res4))


# --------------------------------------------------------------------------- #
# [7] 检索 trace
# --------------------------------------------------------------------------- #

def test_trace():
    print("\n[7] 检索 trace：候选、注入、截断原因")
    store = fresh_store()
    rec = add(store, "主人钟爱猫咪", epistemic="owner_asserted")
    store.put_embedding(rec["id"], "test-embed", [1.0, 0.0], version="v1")

    hits, stats = recall(store, "feline", vector=[1.0, 0.0], meta=META)
    for key in ("mode", "lexical_candidates", "semantic_candidates", "injected",
                "truncated", "truncated_reason", "vector_model", "rrf"):
        check(key in stats, f"trace 里有 {key}", str(sorted(stats)))
    check(stats.get("injected") == len(hits), "注入数与返回条数一致", str(stats))
    check(stats.get("vector_model") == "test-embed", "trace 记了用的嵌入模型", str(stats))

    empty, empty_stats = recall(store, "完全无关的话题xyz", vector=[0.0, 1.0], meta=META)
    check(not empty, "确实没想起来", str([h["content"] for h in empty]))
    check(empty_stats.get("truncated_reason") == "no_match",
          "trace 说明了为什么没想起来", str(empty_stats))

    # 上限截断要能说清是截断而不是「没有记忆」
    for i in range(6):
        add(store, f"主人第{i}次提到周末去海边", kind="event")
    many, many_stats = recall(store, "海边", limit=2, min_score=1.0)
    check(many_stats.get("injected") == len(many), "注入数与实际返回一致", str(many_stats))
    check(not many_stats.get("truncated") or bool(many_stats.get("truncated_reason")),
          "截断时一定带原因", str(many_stats))

    ctx = store.build_context(
        is_owner=True, scene="private", user_id="10001", owner=OWNER,
        query="feline", vector=[1.0, 0.0], vector_meta=META,
        cfg={"max_recalled": 4, "min_score": 2.5, "max_memory_chars": 1000},
    )
    check("猫咪" in ctx["memory"], "语义召回真的进了注入文本", ctx["memory"][:120])
    check("mode" in ctx.get("search", {}), "build_context 把 trace 一起返回",
          str(ctx.get("search")))


def main() -> int:
    print("== 混合检索与操作协议回归 ==")
    test_semantic_recall()
    test_lexical_kept()
    test_rrf_fusion()
    test_vector_meta()
    test_operation_protocol()
    test_reflect_two_phase()
    test_trace()
    print(f"\n通过 {PASS}，失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
