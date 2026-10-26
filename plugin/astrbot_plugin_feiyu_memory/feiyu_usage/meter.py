"""Explicit injected logical-call boundary. No monkeypatch, retry, hook or routing.

Example for a future approved owned call site:
 response = await meter.call(lambda: provider.text_chat(**original_kwargs), info)
 return original_response_processing(response)
This is intentionally NOT wired into any existing plugin in round 1.
"""
from __future__ import annotations
import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import threading
import time
import uuid
from .record import UsageRecordV1, receipt_id, SAFE_ERRORS
from .usage import UsageV1, normalize_usage

PUBLIC_MODELS=frozenset({'unknown','fake-model','deepseek-flash','mimo-v2.6-flash','mimo-v2.5-asr'})

@dataclass(frozen=True)
class CallInfoV1:
    source: str
    operation: str = 'generate'
    provider_key: str = 'unknown'  # transient; never serialized raw
    model: str = 'unknown'
    api_mode: str = 'chat_nonstream'
    codec: str = 'astrbot_reported_v1'
    trace_id: str | None = None
    logical_call_id: str | None = None
    parent_call_id: str | None = None
    scope_key: str | None = None  # transient; never serialized raw

class LogicalMeterV1:
    def __init__(self,sink,*,pseudonym_key: bytes,enabled: bool = False,public_models=PUBLIC_MODELS):
        if not isinstance(pseudonym_key,bytes) or len(pseudonym_key)<32:
            raise ValueError('pseudonym_key_requires_32_bytes')
        self.sink,self.enabled=sink,bool(enabled)
        self._key=pseudonym_key
        self._models=frozenset(public_models)
        self._stats=Counter()
        self._lock=threading.Lock()

    def _count(self,key):
        with self._lock:
            self._stats[key]+=1

    def health(self):
        with self._lock:
            return {'enabled':self.enabled,'counters':dict(self._stats),
                    'boundary':'logical_call','physical_attempts':'unknown'}

    def _pseudo(self,value):
        return 'p_'+hmac.new(self._key,value.encode('utf-8'),hashlib.sha256).hexdigest()[:32]

    async def call(self,invoke,info: CallInfoV1,*,usage_getter=None):
        """Invoke exactly once; preserve return identity, exception and cancellation.

        SDK retries are inside invoke and cannot be counted here. Reusing a call ID
        is only for duplicate completion callbacks, NOT for invoking a new model
        request (each real logical invocation needs a fresh ID).
        """
        if not self.enabled:
            return await invoke()
        started=datetime.now(timezone.utc).isoformat()
        clock=time.monotonic()
        logical_id=info.logical_call_id or str(uuid.uuid4())
        trace=info.trace_id or str(uuid.uuid4())
        self._count('logical_calls_started')
        try:
            response=await invoke()
        except BaseException as exc:
            status='cancelled' if isinstance(exc,asyncio.CancelledError) else 'error'
            error=type(exc).__name__
            if error not in SAFE_ERRORS:
                error='ProviderError'
            try:
                usage=normalize_usage(None,info.codec)
            except Exception:
                self._count('usage_capture_errors')
                usage=UsageV1(usage_state='invalid',issues=('usage_capture_error',))
            self._emit(info,trace,logical_id,started,clock,status,usage,error)
            raise
        try:
            raw=usage_getter(response) if usage_getter else getattr(response,'usage',None)
            usage=normalize_usage(raw,info.codec)
        except Exception:
            self._count('usage_capture_errors')
            usage=UsageV1(usage_state='invalid',issues=('usage_capture_error',))
        self._emit(info,trace,logical_id,started,clock,'success',usage,None)
        return response

    def _emit(self,info,trace,logical_id,started,clock,status,usage,error):
        try:
            record=UsageRecordV1(1,receipt_id(logical_id),trace,logical_id,None,info.parent_call_id,
                info.source,info.operation,self._pseudo(info.provider_key),
                info.model if info.model in self._models else 'unknown',info.api_mode,'logical_call',
                started,datetime.now(timezone.utc).isoformat(),(time.monotonic()-clock)*1000,
                status,usage,error,self._pseudo(info.scope_key) if info.scope_key is not None else None)
            record.validate()
            if not self.sink.try_record(record):
                self._count('sink_rejected')
            else:
                self._count('receipts_submitted')
        except Exception:
            self._count('instrumentation_errors')
            # Observability failure must not mask the response/original exception.
