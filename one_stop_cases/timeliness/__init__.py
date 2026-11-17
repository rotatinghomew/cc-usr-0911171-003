"""Timeliness attribution for accepted one-stop cases.

This package answers the supervision question "why was this case closed later
than promised?" without changing any workflow executability decision.  It
consumes the same ``BusinessCalendar`` used by ``intake.service_clock`` so the
working-time and holiday definition is identical to the one behind existing
due dates.

Terminology used throughout the package:

``handling``
    Working time in which a step is actively being worked.
``blocked_predecessor``
    Wait time after a step completed while the next step had not started.
``queue``
    Wait time before the first step of the case started.
``pause``
    A suspension that extends the committed due time.
``material_correction``
    Waiting for corrected materials; also extends the due time because the case
    cannot progress without them.
``compensation``
    Time spent undoing or re-driving a failed step; chargeable.
``rework``
    Time spent redoing a step after compensation; chargeable.
``unattributed``
    Time that cannot be tied to an event and therefore cannot support an
    on-time or overdue claim.
"""

from .analysis import (
    TimelinessAnalyzer,
    TimelinessService,
    analyze_case,
    analyze_cases,
    analyze_document,
    analyze_records,
)
from .attribution import attribute_case, attribute_cases
from .calendar import build_calendar, build_clock, split_working
from .ledger import CaseLedger, ledger_from_snapshot, normalize_events, parse_case, parse_cases
from .model import (
    DEFAULT_EXCLUDED_CATEGORIES,
    EVENT_KINDS,
    AttributionCategory,
    AttributionSegment,
    CaseAttribution,
    EvidenceIssue,
    TimelineEvent,
    TimelinessError,
    Verdict,
)
from .summary import TimelinessReport, summarize

__all__ = [
    'AttributionCategory',
    'AttributionSegment',
    'CaseAttribution',
    'CaseLedger',
    'DEFAULT_EXCLUDED_CATEGORIES',
    'EVENT_KINDS',
    'EvidenceIssue',
    'TimelinessAnalyzer',
    'TimelinessError',
    'TimelinessReport',
    'TimelinessService',
    'TimelineEvent',
    'Verdict',
    'analyze_case',
    'analyze_cases',
    'analyze_document',
    'analyze_records',
    'attribute_case',
    'attribute_cases',
    'build_calendar',
    'build_clock',
    'ledger_from_snapshot',
    'normalize_events',
    'parse_case',
    'parse_cases',
    'split_working',
    'summarize',
]
