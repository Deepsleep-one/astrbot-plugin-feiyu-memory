"""Privacy-minimized V1 receipt. IDs are random UUIDs, never session identifiers."""
from __future__ import annotations
from dataclasses import dataclass, asdict
import re
import uuid
from datetime import datetime
from .usage import UsageV1, FIELDS, CODECS, ISSUES

SOURCES = frozenset({'chat','companion','memory_extract','memory_diary','memory_reflect',
    'bili_text','bili_vision','screen','summary','other','unknown'})
OPERATIONS=frozenset({'generate','tool_step','compress','extract','diary','reflect','describe','other'})
LEVELS=frozenset({'logical_call','physical_attempt','agent_aggregate'})
API_MODES=frozenset({'chat_nonstream','chat_stream','responses','unknown'})
STATUSES=frozenset({'success','error','cancelled','unknown'})
SAFE_ERRORS=frozenset({'RuntimeError','ValueError','TypeError','TimeoutError','ConnectionError',
    'OSError','PermissionError','CancelledError','ProviderError'})

def valid_uuid(value):
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError,TypeError,AttributeError):
        return False

@dataclass(frozen=True)
class UsageRecordV1:
    schema_version: int
    record_id: str
    trace_id: str
    logical_call_id: str
    attempt_id: str | None
    parent_call_id: str | None
    source: str
    operation: str
    provider_id: str
    model: str
    api_mode: str
    accounting_level: str
    started_at: str
    finished_at: str
    duration_ms: float
    status: str
    usage: UsageV1
    error_class: str | None = None
    scope_pseudonym: str | None = None
    physical_attempts: str | int = 'unknown'

    def validate(self):
        if self.schema_version != 1 or self.source not in SOURCES or self.operation not in OPERATIONS:
            raise ValueError('invalid_record_enums')
        if self.api_mode not in API_MODES or self.accounting_level not in LEVELS or self.status not in STATUSES:
            raise ValueError('invalid_record_enums')
        for value in (self.record_id,self.trace_id,self.logical_call_id):
            if not valid_uuid(value):
                raise ValueError('invalid_random_identifier')
        for value in (self.parent_call_id,self.attempt_id):
            if value is not None and not valid_uuid(value):
                raise ValueError('invalid_random_identifier')
        if (self.accounting_level=='physical_attempt') != (self.attempt_id is not None):
            raise ValueError('attempt_boundary_required')
        if self.physical_attempts != (1 if self.accounting_level=='physical_attempt' else 'unknown'):
            raise ValueError('unproven_physical_attempts')
        for value in (self.provider_id,self.scope_pseudonym):
            if value is not None and re.fullmatch(r'p_[a-f0-9]{32}',value) is None:
                raise ValueError('identifier_not_pseudonymized')
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}',self.model):
            raise ValueError('invalid_model_label')
        if self.error_class is not None and self.error_class not in SAFE_ERRORS:
            raise ValueError('unsafe_error_class')
        start,finish=datetime.fromisoformat(self.started_at),datetime.fromisoformat(self.finished_at)
        if start.tzinfo is None or finish.tzinfo is None or finish<start:
            raise ValueError('invalid_receipt_time')
        if not isinstance(self.duration_ms,(int,float)) or not 0<=self.duration_ms<1e12:
            raise ValueError('invalid_duration')
        if self.usage.usage_state not in {'known','partial','missing','invalid'}:
            raise ValueError('invalid_usage_state')
        if self.usage.usage_semantics_revision not in CODECS | {'unknown'}:
            raise ValueError('unsafe_usage_revision')
        if not isinstance(self.usage.issues,tuple) or any(code not in ISSUES for code in self.usage.issues):
            raise ValueError('unsafe_usage_issue')
        for key in FIELDS:
            value=getattr(self.usage,key)
            if value is not None and (type(value) is not int or not 0<=value<=2**63-1):
                raise ValueError('invalid_usage_number')
        return self

    def to_dict(self):
        data=asdict(self)
        usage=data.pop('usage')
        data.update(usage)
        return data

    @classmethod
    def from_dict(cls,data):
        data=dict(data)
        keys=('usage_state',*FIELDS,'usage_semantics_revision','issues')
        usage={key:data.pop(key) for key in keys}
        usage['issues']=tuple(usage['issues'])
        return cls(**data,usage=UsageV1(**usage)).validate()

    @property
    def accounting_key(self):
        return f'v1:{self.accounting_level}:{self.logical_call_id}:{self.attempt_id or "none"}'


def receipt_id(logical_call_id,level='logical_call',attempt_id=None):
    return str(uuid.uuid5(uuid.NAMESPACE_OID,f'feiyu-usage-v1:{level}:{logical_call_id}:{attempt_id or "none"}'))
