"""老库升级回归（2026-10-25，因 v0.6.1 上线时线上库初始化失败而补）。

真实故障：线上库是 v0.4.0/v0.5.0 建的，`memory_embeddings` 没有 `version` 列。
v0.6.0 把 `CREATE INDEX idx_embeddings_meta ON memory_embeddings(model, version, dim)`
写进了 `_SCHEMA`，而 `CREATE TABLE IF NOT EXISTS` **不会**给老表补列——
于是 executescript 在 `no such column: version` 处停下，整个 `MemoryStore.__init__`
抛异常，插件的记忆库初始化直接失败（日志：`记忆库初始化失败：no such column: version`）。

这个套件专门盯「用新代码打开老库」这条路：新建库永远绿，老库才是真实世界。
只依赖标准库。运行：
    python tests/selftest_migrate.py
    python tools/run_selftests.py migrate
"""

from __future__ import annotations

import itertools
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugin"))

from astrbot_plugin_feiyu_memory.memory_core import (  # noqa: E402
    MemoryStore,
    _SCHEMA,
)

TMP_ROOT = ROOT / "tests" / "_tmp" / "migrate"
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


def case_dir() -> Path:
    case = TMP_ROOT / f"case_{next(_counter)}"
    shutil.rmtree(case, ignore_errors=True)
    case.mkdir(parents=True)
    return case


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def make_legacy_db(path: Path) -> None:
    """造一个「v0.5.0 形态」的老库：新列、新表、新索引全部去掉。"""
    con = sqlite3.connect(str(path))
    try:
        con.executescript(_SCHEMA)
        con.execute("DROP INDEX IF EXISTS idx_embeddings_meta")
        con.execute("ALTER TABLE memory_embeddings DROP COLUMN version")
        con.execute("ALTER TABLE memory_records DROP COLUMN op_hash")
        for name in ("memory_proposal_items", "memory_proposals", "memory_edges"):
            con.execute(f"DROP TABLE IF EXISTS {name}")
        # 一条老记录 + 一条老向量，模拟线上真实数据
        con.execute(
            "INSERT INTO memory_records (id, owner_id, subject_id, platform, bot_instance,"
            " scope_type, scope_id, kind, epistemic_type, fact_key, content, source_ids,"
            " derived_from_ids, occurred_at, asserted_at, valid_from, valid_to, confidence,"
            " importance, status, expires_at, revision, supersedes_id, deletion_epoch,"
            " persona_version, embedding_model, embedding_version, created_at, updated_at,"
            " created_by)"
            " VALUES ('r-old','primary','','aiocqhttp','*','owner_private','',"
            " 'preference','owner_asserted','feiyu.test.legacy','主人钟爱猫咪','[]','[]',"
            " '2026-10-24T10:00:00+08:00','2026-10-24T10:00:00+08:00',"
            " '2026-10-24T10:00:00+08:00',NULL,0.9,3,'active',NULL,1,NULL,0,'','','',"
            " '2026-10-24T10:00:00+08:00','2026-10-24T10:00:00+08:00','owner')"
        )
        con.execute(
            "INSERT INTO memory_embeddings (record_id, model, dim, vector, updated_at)"
            " VALUES ('r-old','legacy-embed',2,?, '2026-10-24T10:00:00+08:00')",
            (b"\x00\x00\x80\x3f" + b"\x00\x00\x00\x00",),
        )
        con.commit()
    finally:
        con.close()


def test_legacy_opens() -> None:
    print("[1] 用新代码打开老库：不再「no such column: version」")
    case = case_dir()
    db = case / "legacy.db"
    make_legacy_db(db)

    con = sqlite3.connect(str(db))
    try:
        check("version" not in columns(con, "memory_embeddings"), "老库确实没有 version 列")
        check("op_hash" not in columns(con, "memory_records"), "老库确实没有 op_hash 列")
    finally:
        con.close()

    store = None
    try:
        store = MemoryStore(db)
        check(True, "MemoryStore 能在老库上初始化")
    except Exception as exc:  # noqa: BLE001 - 这条就是要抓任何异常
        check(False, "MemoryStore 能在老库上初始化", f"{type(exc).__name__}: {exc}")
        return

    con = sqlite3.connect(str(db))
    try:
        check("version" in columns(con, "memory_embeddings"), "升级后补上了 version 列")
        check("op_hash" in columns(con, "memory_records"), "升级后补上了 op_hash 列")
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        check("memory_edges" in tables, "升级后建好了 memory_edges")
        check("memory_proposals" in tables, "升级后建好了 memory_proposals")
        check("memory_proposal_items" in tables, "升级后建好了 memory_proposal_items")
        idx = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        check("idx_embeddings_meta" in idx, "升级后建好了 idx_embeddings_meta")
        row = con.execute(
            "SELECT version FROM memory_embeddings WHERE record_id='r-old'").fetchone()
        check(row is not None and row[0] == "", "老向量补成 version=''", f"得到 {row}")
    finally:
        con.close()

    rec = store.get_record("r-old")
    check(rec is not None and rec["content"] == "主人钟爱猫咪", "老记录仍在且可读")
    check(store.validate_sources(["r-old"])[0] is True, "老记录能通过来源复核")
    store.close()


def test_reopen_and_use() -> None:
    print("[2] 升级后的库能继续正常用（不是「能打开但用不了」）")
    case = case_dir()
    db = case / "legacy.db"
    make_legacy_db(db)

    store = MemoryStore(db)
    health = store.vector_health({"model": "legacy-embed"})
    check(health["active"] >= 1, "还能统计到 active 记录", str(health))
    check(health["covered"] >= 1, "老向量仍算「有向量」", str(health))

    hits = store.search("猫咪", owner_id="primary", is_owner=True, scene="private",
                        group_id="", user_id="owner")
    check(any(h["id"] == "r-old" for h in hits), "升级后检索仍能命中老记录")

    store.put_embedding("r-old", "legacy-embed", [1.0, 0.0], version="v2")
    check(store.get_embedding("r-old") is not None, "升级后能重写向量")
    store.close()

    store2 = MemoryStore(db)
    check(store2.get_record("r-old") is not None, "再次打开仍然正常（迁移可重入）")
    store2.close()


def test_index_not_in_schema() -> None:
    print("[3] 守住根因：建索引的语句必须在新列之后，不能写进 _SCHEMA")
    lowered = _SCHEMA.lower().replace(" ", "").replace("\n", "")
    check("createindexifnotexistsidx_embeddings_meta" not in lowered,
          "_SCHEMA 里没有 CREATE INDEX idx_embeddings_meta（否则老库必崩）")


def main() -> int:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    test_legacy_opens()
    print()
    test_reopen_and_use()
    print()
    test_index_not_in_schema()
    print()
    print(f"通过 {PASS}，失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
