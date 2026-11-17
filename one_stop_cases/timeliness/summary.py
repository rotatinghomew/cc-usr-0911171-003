from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .model import CaseAttribution, Verdict

VERDICT_ORDER = (
    Verdict.WITHIN_TARGET,
    Verdict.OVERDUE,
    Verdict.IN_PROGRESS,
    Verdict.EVIDENCE_INSUFFICIENT,
    Verdict.CANCELLED,
)


def _empty_bucket() -> dict[str, Any]:
    return {
        'cases': 0,
        'verdicts': {verdict.value: 0 for verdict in VERDICT_ORDER},
        'chargeable_working_seconds': 0,
        'excluded_working_seconds': 0,
        'overdue_seconds': 0,
        'evidence_insufficient_with_cause': Counter(),
        'by_category': Counter(),
        'by_step': defaultdict(lambda: {'working_seconds': 0, 'calendar_seconds': 0, 'case_ids': set()}),
    }


def _step_row(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'cases': len(values.get('case_ids', ())),
        'working_seconds': values['working_seconds'],
        'calendar_seconds': values['calendar_seconds'],
    }


def _finalize(bucket: Mapping[str, Any]) -> dict[str, Any]:
    cases = bucket['cases']
    return {
        'cases': cases,
        'verdicts': dict(bucket['verdicts']),
        'chargeable_working_seconds': bucket['chargeable_working_seconds'],
        'excluded_working_seconds': bucket['excluded_working_seconds'],
        'overdue_seconds': bucket['overdue_seconds'],
        'mean_chargeable_working_seconds': round(bucket['chargeable_working_seconds'] / cases, 3) if cases else None,
        'evidence_insufficient_with_cause': dict(sorted(bucket['evidence_insufficient_with_cause'].items())),
        'by_category': dict(sorted(bucket['by_category'].items())),
        'by_step': {step: _step_row(values) for step, values in sorted(bucket['by_step'].items())},
    }


def _record(bucket: dict[str, Any], attribution: CaseAttribution) -> None:
    bucket['cases'] += 1
    bucket['verdicts'][attribution.verdict.value] += 1
    bucket['chargeable_working_seconds'] += attribution.chargeable_working_seconds
    bucket['excluded_working_seconds'] += attribution.excluded_working_seconds
    bucket['overdue_seconds'] += attribution.overdue_seconds
    if attribution.verdict is Verdict.EVIDENCE_INSUFFICIENT:
        for issue in attribution.issues:
            if issue.blocking:
                bucket['evidence_insufficient_with_cause'][issue.code] += 1
    for name, seconds in attribution.by_category.items():
        bucket['by_category'][name] += seconds
    for step, values in attribution.by_step.items():
        target = bucket['by_step'][step]
        target['working_seconds'] += values['working_seconds']
        target['calendar_seconds'] += values['calendar_seconds']
        target['case_ids'].add(attribution.case_id)


def _group_attributions(attributions: Sequence[CaseAttribution], key) -> dict[Any, dict[str, Any]]:
    groups: dict[Any, dict[str, Any]] = {}
    for attribution in attributions:
        name = key(attribution)
        groups.setdefault(name, _empty_bucket())
        _record(groups[name], attribution)
    return groups


def _bottlenecks(attributions: Sequence[CaseAttribution], minimum_cases: int = 1) -> list[dict[str, Any]]:
    """Rank steps by chargeable working time spent waiting or reworking.

    Only attributable, evidence-sufficient cases contribute; otherwise a
    missing timestamp would masquerade as a bottleneck.
    """
    step_totals: dict[str, dict[str, Any]] = defaultdict(lambda: {
        'cases': set(),
        'blocked_predecessor_seconds': 0,
        'rework_seconds': 0,
        'compensation_seconds': 0,
        'material_correction_seconds': 0,
        'pause_seconds': 0,
        'handling_seconds': 0,
    })
    for attribution in attributions:
        # A case whose timeline cannot be verified must not inflate a bottleneck
        # ranking just because its time could not be attributed.
        if attribution.verdict is Verdict.CANCELLED or not attribution.evidence_sufficient:
            continue
        for segment in attribution.segments:
            step = segment.step_id or '<case>'
            bucket = step_totals[step]
            bucket['cases'].add(attribution.case_id)
            key = segment.category.value + '_seconds'
            if key in bucket:
                bucket[key] += segment.working_seconds
    rows = []
    for step, bucket in step_totals.items():
        if len(bucket['cases']) < minimum_cases:
            continue
        avoidable = bucket['blocked_predecessor_seconds'] + bucket['rework_seconds'] + bucket['compensation_seconds']
        rows.append({
            'step_id': step,
            'cases': len(bucket['cases']),
            'chargeable_wait_seconds': avoidable,
            'blocked_predecessor_seconds': bucket['blocked_predecessor_seconds'],
            'rework_seconds': bucket['rework_seconds'],
            'compensation_seconds': bucket['compensation_seconds'],
            'material_correction_seconds': bucket['material_correction_seconds'],
            'pause_seconds': bucket['pause_seconds'],
            'handling_seconds': bucket['handling_seconds'],
        })
    rows.sort(key=lambda row: (-row['chargeable_wait_seconds'], row['step_id']))
    return rows


def summarize(attributions: Iterable[CaseAttribution], minimum_bottleneck_cases: int = 1) -> dict[str, Any]:
    items = tuple(attributions)
    overall = _empty_bucket()
    for item in items:
        _record(overall, item)
    by_version = _group_attributions(items, lambda item: item.service_version or '<unknown>')
    by_batch = _group_attributions(items, lambda item: item.batch_id or '<unassigned>')
    without_version = sum(1 for item in items if item.service_version is None)
    without_batch = sum(1 for item in items if item.batch_id is None)
    evidence_insufficient = [item for item in items if item.verdict is Verdict.EVIDENCE_INSUFFICIENT]
    return {
        'cases': len(items),
        'verdicts': dict(overall['verdicts']),
        'evidence_sufficient_cases': len(items) - len(evidence_insufficient),
        'evidence_insufficient_cases': len(evidence_insufficient),
        'evidence_insufficient_reasons': dict(sorted(overall['evidence_insufficient_with_cause'].items())),
        'cases_without_service_version': without_version,
        'cases_without_batch': without_batch,
        'chargeable_working_seconds': overall['chargeable_working_seconds'],
        'excluded_working_seconds': overall['excluded_working_seconds'],
        'overdue_seconds': overall['overdue_seconds'],
        'by_category': dict(sorted(overall['by_category'].items())),
        'by_step': {step: _step_row(values) for step, values in sorted(overall['by_step'].items())},
        'by_service_version': {key: _finalize(value) for key, value in sorted(by_version.items())},
        'by_batch': {key: _finalize(value) for key, value in sorted(by_batch.items())},
        'bottlenecks': _bottlenecks(items, minimum_bottleneck_cases),
    }


@dataclass(frozen=True)
class TimelinessReport:
    as_of: str | None
    summary: Mapping[str, Any]
    cases: tuple[CaseAttribution, ...]

    def as_dict(self, include_segments: bool = True) -> dict[str, Any]:
        cases = []
        for item in self.cases:
            payload = item.as_dict()
            if not include_segments:
                payload.pop('segments', None)
            cases.append(payload)
        return {
            'as_of': self.as_of,
            'summary': dict(self.summary),
            'cases': cases,
        }

    def case(self, case_id: str) -> CaseAttribution:
        for item in self.cases:
            if item.case_id == case_id:
                return item
        raise KeyError(case_id)

    @property
    def by_case(self) -> dict[str, CaseAttribution]:
        return {item.case_id: item for item in self.cases}
