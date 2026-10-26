"""Metadata-only aggregation; never sum overlapping accounting levels."""
from __future__ import annotations
from collections import defaultdict, Counter
from .usage import FIELDS


def usage_report(records,*,health=None):
    groups=defaultdict(list)
    for record in records:
        record.validate()
        groups[(record.source,record.model,record.accounting_level,record.provider_id)].append(record)
    rows=[]
    for (source,model,level,provider),items in sorted(groups.items()):
        # Invalid receipts are visible, but unsafe numbers do not enter sums.
        valid=[r for r in items if r.usage.usage_state in {'known','partial'}]
        fields={}
        known_counts={}
        for field in FIELDS:
            values=[getattr(r.usage,field) for r in valid if getattr(r.usage,field) is not None]
            fields[field]=sum(values) if values else None
            known_counts[field]=len(values)
        states=dict(Counter(r.usage.usage_state for r in items))
        row={'source':source,'model':model,'provider_id':provider,'accounting_level':level,
             'collected_records':len(items), 'logical_calls':len(items) if level=='logical_call' else None,
             'visible_attempts':len(items) if level=='physical_attempt' else None,
             'physical_attempts':'unknown' if level!='physical_attempt' else len(items),
             'usage_states':states,'status_counts':dict(Counter(r.status for r in items)),
             'unknown_usage_records':sum(r.usage.usage_state in {'partial','missing','invalid'} for r in items),
             'field_known_counts':known_counts,
             'sums_are_known_lower_bounds':any(count<len(items) for count in known_counts.values()),
             **fields}
        # Do NOT divide sums taken from mismatched samples to claim cache rate.
        paired=[r for r in valid if r.usage.input_total is not None and r.usage.input_cached is not None]
        denominator=sum(r.usage.input_total for r in paired)
        row['cache_rate_paired_records']=len(paired)
        row['cache_rate_on_paired_receipts']=(sum(r.usage.input_cached for r in paired)/denominator if denominator else None)
        rows.append(row)
    return {'schema_version':1,'groups':rows,'health':health,
            'accounting_levels_must_not_be_added':True,'cost':None,
            'coverage':'collected_receipts_only; see static call inventory for unwired paths',
            'complete_cost_or_savings_claim':False}
