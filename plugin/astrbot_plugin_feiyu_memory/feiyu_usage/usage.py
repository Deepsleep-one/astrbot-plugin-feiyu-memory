"""Explicit, versioned usage semantics; never inspect prompts or estimate tokens."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from collections.abc import Mapping

FIELDS = ('input_total','input_cached','input_uncached','output_total','reasoning_tokens')
CODECS = frozenset({'openai_chat_v1', 'deepseek_chat_v1', 'astrbot_reported_v1'})
ISSUES = frozenset({'unsupported_semantics','invalid_numeric_field','ambiguous_host_zero_default',
    'conflicting_cache_fields','cached_exceeds_input','uncached_exceeds_input',
    'cache_partition_mismatch','reasoning_exceeds_output','total_mismatch',
    'delta_without_stable_event_id','cumulative_usage_regressed','cancelled_partial_receipt',
    'usage_capture_error','cumulative_partial_snapshot'})

@dataclass(frozen=True)
class UsageV1:
    usage_state: str = 'missing'
    input_total: int | None = None
    input_cached: int | None = None
    input_uncached: int | None = None
    output_total: int | None = None
    reasoning_tokens: int | None = None
    usage_semantics_revision: str = 'unknown'
    issues: tuple[str, ...] = ()

    def to_dict(self):
        return asdict(self)


def _get(obj, key):
    if obj is None:
        return None
    return obj.get(key) if isinstance(obj, Mapping) else getattr(obj, key, None)


def normalize_usage(raw, codec: str) -> UsageV1:
    """Known describes reported totals, NOT proof of provider billing completeness.

    AstrBot's zero-filled default usage cannot prove an upstream zero-token receipt;
    conservatively leave an all-zero object missing. For native OpenAI, no cache
    details means cached/miss unknown. Reasoning is a subset, never added to output.
    Unknown vendor formats must obtain a new explicit semantics revision.
    """
    if type(codec) is not str or codec not in CODECS:
        return UsageV1(usage_state='invalid', issues=('unsupported_semantics',))
    if raw is None:
        return UsageV1(usage_semantics_revision=codec)
    issues = []
    def number(obj, key):
        value = _get(obj, key)
        if value is None:
            return None
        if type(value) is not int or value < 0:
            issues.append('invalid_numeric_field')
            return None
        return value
    if codec == 'astrbot_reported_v1':
        cached = number(raw,'input_cached')
        uncached = number(raw,'input_other')
        output = number(raw,'output')
        total = cached + uncached if cached is not None and uncached is not None else None
        reasoning = None
        native_total = None
        if not issues and cached == uncached == output == 0:
            return UsageV1(usage_semantics_revision=codec, issues=('ambiguous_host_zero_default',))
    else:
        total = number(raw,'prompt_tokens')
        output = number(raw,'completion_tokens')
        details = _get(raw,'prompt_tokens_details')
        cached = number(details,'cached_tokens')
        reasoning = number(_get(raw,'completion_tokens_details'),'reasoning_tokens')
        native_total = number(raw,'total_tokens')
        uncached = None
        if codec == 'deepseek_chat_v1':
            native_cached = number(raw,'prompt_cache_hit_tokens')
            uncached = number(raw,'prompt_cache_miss_tokens')
            if native_cached is not None:
                if cached is not None and cached != native_cached:
                    issues.append('conflicting_cache_fields')
                cached = native_cached
            if total is None and cached is not None and uncached is not None:
                total = cached + uncached
    if total is not None and uncached is not None and uncached > total:
        issues.append('uncached_exceeds_input')
    if total is not None and cached is not None:
        if cached > total:
            issues.append('cached_exceeds_input')
        elif uncached is None:
            uncached = total - cached
        elif uncached + cached != total:
            issues.append('cache_partition_mismatch')
    if output is not None and reasoning is not None and reasoning > output:
        issues.append('reasoning_exceeds_output')
    if total is not None and output is not None and native_total is not None:
        if total + output != native_total:
            issues.append('total_mismatch')
    state = ('invalid' if issues else 'known' if total is not None and output is not None
             else 'partial' if any(v is not None for v in (total,cached,uncached,output,reasoning)) else 'missing')
    return UsageV1(state,total,cached,uncached,output,reasoning,codec,tuple(sorted(set(issues))))


class StreamUsageV1:
    """Offline accumulator, NOT a host stream hook. Finalize once at call completion.

    Cumulative chunks merge known fields (missing is not zero); deltas require a provider-stable event_id
    (not one generated on reception). Repeated callbacks can't add tokens twice.
    Tokens are not inferred from text. Incremental usage without IDs is rejected.
    """
    def __init__(self, codec: str, mode: str = 'cumulative'):
        if mode not in {'cumulative','delta'}:
            raise ValueError('invalid_stream_mode')
        self.codec, self.mode = codec if type(codec) is str and codec in CODECS else 'unknown', mode
        self.current = normalize_usage(None, self.codec)
        self._seen = set()
        self._closed = False
        self._updates = 0

    def update(self, raw, *, event_id: str | None = None):
        if self._closed:
            return self.current
        if event_id is not None and event_id in self._seen:
            return self.current
        if self.mode == 'delta' and not event_id:
            self.current = UsageV1(usage_state='invalid', usage_semantics_revision=self.codec,
                                   issues=('delta_without_stable_event_id',))
            return self.current
        if event_id is not None:
            self._seen.add(event_id)
        new = normalize_usage(raw,self.codec)
        if raw is None or new.usage_state == 'missing':
            return self.current
        if self.current.usage_state == 'invalid':
            return self.current
        if self.mode == 'delta' and self._updates:
            values={k:(getattr(self.current,k)+getattr(new,k)
                       if getattr(self.current,k) is not None and getattr(new,k) is not None else None)
                    for k in FIELDS}
            state='invalid' if new.usage_state=='invalid' else ('known' if values['input_total'] is not None and values['output_total'] is not None else 'partial')
            self.current=UsageV1(usage_state=state,usage_semantics_revision=self.codec,
                                 issues=new.issues,**values)
        else:
            if self.mode == 'cumulative' and self._updates:
                for key in FIELDS:
                    old_value, new_value=getattr(self.current,key),getattr(new,key)
                    if old_value is not None and new_value is not None and new_value < old_value:
                        self.current=UsageV1(usage_state='invalid',usage_semantics_revision=self.codec,
                                             issues=('cumulative_usage_regressed',))
                        return self.current
            if self.mode == 'cumulative' and self._updates and new.usage_state != 'invalid':
                values={k:getattr(new,k) if getattr(new,k) is not None else getattr(self.current,k)
                        for k in FIELDS}
                issues=list(new.issues)
                for field,issue in [('input_cached','cached_exceeds_input'),('input_uncached','uncached_exceeds_input')]:
                    if values['input_total'] is not None and values[field] is not None and values[field]>values['input_total']:
                        issues.append(issue)
                if all(values[k] is not None for k in ('input_total','input_cached','input_uncached')):
                    if values['input_cached']+values['input_uncached']!=values['input_total']:
                        issues.append('cache_partition_mismatch')
                if values['reasoning_tokens'] is not None and values['output_total'] is not None:
                    if values['reasoning_tokens']>values['output_total']:
                        issues.append('reasoning_exceeds_output')
                state='invalid' if issues else new.usage_state
                if state!='invalid' and new.usage_state=='partial':
                    issues.append('cumulative_partial_snapshot')
                self.current=UsageV1(usage_state=state,usage_semantics_revision=self.codec,
                                     issues=tuple(sorted(set(issues))),**values)
            else:
                self.current = new
        self._updates += 1
        return self.current

    def finalize(self, *, cancelled: bool = False):
        self._closed = True
        if cancelled and self.current.usage_state == 'known':
            data=self.current.to_dict()
            data.update(usage_state='partial',issues=(*self.current.issues,'cancelled_partial_receipt'))
            self.current=UsageV1(**data)
        return self.current
