"""Explicit local sidecar, bounded non-blocking submission; no import-time I/O."""
from __future__ import annotations
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import queue
import sqlite3
import threading
import time
from .record import UsageRecordV1

@dataclass(frozen=True)
class LedgerLimits:
    queue_size: int = 256
    busy_timeout_ms: int = 20
    max_rows: int = 100000
    retention_days: int = 7
    max_bytes: int = 64*1024*1024

    def __post_init__(self):
        if not 1<=self.queue_size<=4096 or not 0<=self.busy_timeout_ms<=50:
            raise ValueError('invalid_queue_or_timeout')
        if not 1<=self.max_rows<=1000000 or not 1<=self.retention_days<=365:
            raise ValueError('invalid_retention_or_rows')
        if not 65536<=self.max_bytes<=1024**3:
            raise ValueError('invalid_size_cap')

APPLICATION_ID = 0x46595531  # FYU1; an empty database is the only auto-init target.
CREATE_SQL = ('CREATE TABLE usage_records ('
              'record_id TEXT PRIMARY KEY,accounting_key TEXT NOT NULL UNIQUE,'
              'finished_at TEXT NOT NULL,payload TEXT NOT NULL)')


def _owned_schema(conn):
    version=conn.execute('PRAGMA user_version').fetchone()[0]
    app=conn.execute('PRAGMA application_id').fetchone()[0]
    objects=conn.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'").fetchall()
    if app==0 and version==0 and not objects:
        return False
    if app!=APPLICATION_ID or version!=1 or objects!=[('table','usage_records',CREATE_SQL)]:
        raise ValueError('refuse_foreign_or_unsupported_ledger')
    return True


class LocalLedger:
    """Synchronous storage primitive; use QueuedLedger off the chat request path.

    Single schema v1, reject unknown schemas, no ALTER/DROP of existing user data.
    max_page_count bounds the DB (rollback journal and OS latency have separate
    limits); no WAL or unbounded connection pool. Old receipts reject after TTL.
    """
    def __init__(self,path: Path,limits: LedgerLimits | None = None):
        self.path=Path(path)
        self.limits=limits or LedgerLimits()

    def write(self,record: UsageRecordV1):
        record.validate()
        cutoff=(datetime.now(timezone.utc)-timedelta(days=self.limits.retention_days)).isoformat()
        if datetime.fromisoformat(record.finished_at)<datetime.fromisoformat(cutoff):
            return 'expired'
        payload=json.dumps(record.to_dict(),sort_keys=True,separators=(',',':'),ensure_ascii=True)
        # No directory auto-creation: deployment must explicitly provision a sidecar.
        with closing(sqlite3.connect(self.path,timeout=self.limits.busy_timeout_ms/1000)) as conn, conn:
            conn.execute('PRAGMA busy_timeout=%d' % self.limits.busy_timeout_ms)
            conn.execute('BEGIN IMMEDIATE')
            owned=_owned_schema(conn)  # Check before schema/data/page-limit mutations.
            page_size=conn.execute('PRAGMA page_size').fetchone()[0]
            conn.execute('PRAGMA max_page_count=%d' % (self.limits.max_bytes//page_size))
            if not owned:
                conn.execute(CREATE_SQL)
                conn.execute('PRAGMA application_id=%d' % APPLICATION_ID)
                conn.execute('PRAGMA user_version=1')
            previous=conn.execute('SELECT payload FROM usage_records WHERE accounting_key=? OR record_id=?',
                                   (record.accounting_key,record.record_id)).fetchall()
            if previous:
                return 'duplicate' if all(row[0]==payload for row in previous) else 'conflict'
            # ISO timestamps are normalized by the meter; timezone-aware values from
            # imported records use julianday so alternative offsets cannot evade TTL.
            conn.execute('DELETE FROM usage_records WHERE julianday(finished_at)<julianday(?)',(cutoff,))
            if conn.execute('SELECT COUNT(*) FROM usage_records').fetchone()[0]>=self.limits.max_rows:
                return 'full'
            conn.execute('INSERT INTO usage_records VALUES(?,?,?,?)',
                         (record.record_id,record.accounting_key,record.finished_at,payload))
        return 'persisted'

    def read_records(self):
        # readonly reporting must never create an empty DB accidentally.
        uri=self.path.resolve().as_uri()+'?mode=ro'
        with closing(sqlite3.connect(uri,uri=True,timeout=self.limits.busy_timeout_ms/1000)) as conn:
            conn.execute('PRAGMA query_only=ON')
            if not _owned_schema(conn):
                raise ValueError('uninitialized_ledger')
            return [UsageRecordV1.from_dict(json.loads(row[0])) for row in
                    conn.execute('SELECT payload FROM usage_records ORDER BY finished_at,record_id')]


class QueuedLedger:
    """One explicitly started worker; constructing/importing starts nothing.

    try_record never waits for SQLite. Saturation/failure is observable in health,
    never replaced by a zero-token receipt. drain/close are maintenance operations,
    not used by the model adapter. A closed instance must not be reused after reload.
    """
    def __init__(self,store: LocalLedger):
        self.store=store
        self._queue=queue.Queue(maxsize=store.limits.queue_size)
        self._guard=threading.Lock()
        self._stats=Counter()
        self._stop=threading.Event()
        self._worker=None
        self._accepting=False

    def start(self):
        with self._guard:
            if self._worker is not None:
                if self._worker.is_alive() and self._accepting:
                    return False
                raise RuntimeError('ledger_instance_already_closed')
            self._accepting=True
            self._worker=threading.Thread(target=self._run,name='feiyu-usage-ledger-v1',daemon=True)
            self._worker.start()
        return True

    def try_record(self,record):
        # Metadata only, no prompt/response references kept in queue.
        with self._guard:
            if not self._accepting:
                self._stats['dropped_not_running']+=1
                return False
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                self._stats['dropped_queue_full']+=1
                return False
            self._stats['submitted']+=1
        return True

    def _run(self):
        while not self._stop.is_set() or not self._queue.empty():
            try:
                record=self._queue.get(timeout=.02)
            except queue.Empty:
                continue
            try:
                outcome=self.store.write(record)
                if outcome not in {'persisted','duplicate','conflict','expired','full'}:
                    outcome='invalid_store_outcome'
                with self._guard:
                    self._stats[outcome]+=1
            except Exception:
                with self._guard:
                    self._stats['write_errors']+=1
                # Never persist/log arbitrary exception text or paths.
            finally:
                self._queue.task_done()

    def drain(self,timeout=2.0):
        deadline=time.monotonic()+max(0,timeout)
        while self._queue.unfinished_tasks and time.monotonic()<deadline:
            self._stop.wait(.005)
        return self._queue.unfinished_tasks==0

    def close(self,timeout=2.0):
        with self._guard:
            self._accepting=False
            self._stop.set()
            worker=self._worker
        if worker:
            worker.join(max(0,timeout))
        return worker is None or not worker.is_alive()

    def health(self):
        with self._guard:
            return {'schema_version':1,'accepting':self._accepting,
                'worker_alive':bool(self._worker and self._worker.is_alive()),
                'queue_size':self._queue.qsize(),'pending':self._queue.unfinished_tasks,
                'counters':dict(self._stats),'raw_error_text_stored':False}
