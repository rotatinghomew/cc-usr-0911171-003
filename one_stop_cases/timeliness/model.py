from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class TimelinessError(ValueError):
    pass


class AttributionCategory(str, Enum):
    """Time buckets used by the timeliness attribution ledger.

    ``segments`` always carry both calendar and working seconds so the
    non-working part of a window (weekends, holidays, off-hours) stays visible
    without being charged against the committed handling time.
    """

    HANDLING = 'handling'
    BLOCKED_PREDECESSOR = 'blocked_predecessor'
    PAUSE = 'pause'
    MATERIAL_CORRECTION = 'material_correction'
    COMPENSATION = 'compensation'
    REWORK = 'rework'
    QUEUE = 'queue'
    UNATTRIBUTED = 'unattributed'


class Verdict(str, Enum):
    WITHIN_TARGET = 'within_target'
    OVERDUE = 'overdue'
    IN_PROGRESS = 'in_progress'
    EVIDENCE_INSUFFICIENT = 'evidence_insufficient'
    CANCELLED = 'cancelled'


EVENT_KINDS = frozenset({
    'opened',
    'closed',
    'step_started',
    'step_completed',
    'step_failed',
    'pause_started',
    'pause_ended',
    'correction_requested',
    'correction_received',
    'compensation_started',
    'compensation_completed',
    'rework_started',
    'rework_completed',
})

# Categories that stop the statutory handling clock by default.  This mirrors
# the pause-extension semantics implemented by
# ``intake.service_clock.ServiceClock.deadline``: a suspended period extends the
# committed due time by the same amount of business time.
DEFAULT_EXCLUDED_CATEGORIES = (
    AttributionCategory.PAUSE.value,
    AttributionCategory.MATERIAL_CORRECTION.value,
)


@dataclass(frozen=True)
class TimelineEvent:
    kind: str
    occurred_at: dt.datetime
    step_id: str | None = None
    related_step: str | None = None
    reason: str | None = None
    outcome: str | None = None
    sequence: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            'kind': self.kind,
            'occurred_at': self.occurred_at.isoformat(),
            'step_id': self.step_id,
            'related_step': self.related_step,
            'reason': self.reason,
            'outcome': self.outcome,
            'sequence': self.sequence,
        }


@dataclass(frozen=True)
class EvidenceIssue:
    code: str
    message: str
    step_id: str | None = None
    blocking: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            'code': self.code,
            'message': self.message,
            'step_id': self.step_id,
            'blocking': self.blocking,
        }


@dataclass(frozen=True)
class AttributionSegment:
    category: AttributionCategory
    start: dt.datetime
    end: dt.datetime
    step_id: str | None
    related_step: str | None
    working_seconds: int
    calendar_seconds: int
    source: str = ''
    attempt: int = 1
    holiday_seconds: int = 0
    weekend_seconds: int = 0
    off_hours_seconds: int = 0

    @property
    def non_working_seconds(self) -> int:
        return self.holiday_seconds + self.weekend_seconds + self.off_hours_seconds

    def as_dict(self) -> dict[str, Any]:
        return {
            'category': self.category.value,
            'start': self.start.isoformat(),
            'end': self.end.isoformat(),
            'step_id': self.step_id,
            'related_step': self.related_step,
            'working_seconds': self.working_seconds,
            'calendar_seconds': self.calendar_seconds,
            'holiday_seconds': self.holiday_seconds,
            'weekend_seconds': self.weekend_seconds,
            'off_hours_seconds': self.off_hours_seconds,
            'source': self.source,
            'attempt': self.attempt,
        }


@dataclass
class CaseAttribution:
    case_id: str
    service_version: str | None
    batch_id: str | None
    outcome: str | None
    opened_at: dt.datetime | None
    closed_at: dt.datetime | None
    as_of: dt.datetime
    due_at: dt.datetime | None
    due_at_source: str | None
    computed_due_at: dt.datetime | None
    target_seconds: int | None
    verdict: Verdict
    evidence_sufficient: bool
    issues: tuple[EvidenceIssue, ...]
    segments: tuple[AttributionSegment, ...]
    excluded_categories: tuple[str, ...]
    calendar_name: str
    holidays: tuple[str, ...]
    working_seconds_total: int
    chargeable_working_seconds: int
    excluded_working_seconds: int
    calendar_seconds_total: int
    non_working_seconds: int
    non_working_breakdown: Mapping[str, int]
    overdue_seconds: int
    remaining_seconds: int
    by_category: Mapping[str, int]
    by_step: Mapping[str, Mapping[str, int]]
    basis: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            'case_id': self.case_id,
            'service_version': self.service_version,
            'batch_id': self.batch_id,
            'outcome': self.outcome,
            'opened_at': self.opened_at.isoformat() if self.opened_at else None,
            'closed_at': self.closed_at.isoformat() if self.closed_at else None,
            'as_of': self.as_of.isoformat(),
            'due_at': self.due_at.isoformat() if self.due_at else None,
            'due_at_source': self.due_at_source,
            'computed_due_at': self.computed_due_at.isoformat() if self.computed_due_at else None,
            'target_seconds': self.target_seconds,
            'verdict': self.verdict.value,
            'evidence_sufficient': self.evidence_sufficient,
            'evidence_issues': [issue.as_dict() for issue in self.issues],
            'calendar': {
                'name': self.calendar_name,
                'holidays': list(self.holidays),
            },
            'policy': {
                'excluded_categories': list(self.excluded_categories),
                'excluded_working_seconds': self.excluded_working_seconds,
            },
            'working_seconds_total': self.working_seconds_total,
            'chargeable_working_seconds': self.chargeable_working_seconds,
            'calendar_seconds_total': self.calendar_seconds_total,
            'non_working_seconds': self.non_working_seconds,
            'non_working_breakdown': dict(self.non_working_breakdown),
            'overdue_seconds': self.overdue_seconds,
            'remaining_seconds': self.remaining_seconds,
            'by_category': dict(self.by_category),
            'by_step': {step: dict(values) for step, values in self.by_step.items()},
            'segments': [segment.as_dict() for segment in self.segments],
            'basis': list(self.basis),
        }
