from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..contracts import require_datetime
from ..intake.service_clock import BusinessCalendar
from .attribution import attribute_case
from .calendar import build_calendar
from .ledger import parse_case, parse_cases
from .model import CaseAttribution, TimelinessError
from .summary import TimelinessReport, summarize


def _document_calendar(document: Mapping[str, Any]) -> BusinessCalendar:
    """Build the business calendar declared by a fixture/document.

    Both the inline ``{"calendar": {...}}`` form and flat ``work_periods`` /
    ``holidays`` keys are accepted.  The calendar is a real
    ``intake.service_clock.BusinessCalendar``, so the working-time and holiday
    definition is shared with the existing due-date calculation.
    """
    config = document.get('calendar')
    if isinstance(config, Mapping):
        return build_calendar(config)
    return build_calendar(
        name=document.get('calendar_name'),
        periods=document.get('work_periods'),
        holidays=document.get('holidays'),
    )


def _as_of(document: Mapping[str, Any], fallback: dt.datetime | str | None) -> dt.datetime | None:
    """Resolve the evaluation moment, letting an explicit argument win.

    An operator who asks "as of date X" must get date X even when the fixture
    carries its own ``as_of``; otherwise re-running an archived fixture could
    silently report a different moment than the one requested.
    """
    if fallback not in (None, ''):
        return require_datetime(fallback, 'as_of') if isinstance(fallback, str) else fallback
    value = document.get('as_of') or document.get('evaluated_at')
    if value not in (None, ''):
        return require_datetime(value, 'as_of')
    return None


def _raw_cases(document: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    for key in ('cases', 'items'):
        values = document.get(key)
        if values is None:
            continue
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise TimelinessError(f'{key} must be a list')
        return values
    return ()


def _attribute(cases: Sequence[Mapping[str, Any]], calendar: BusinessCalendar,
               as_of: dt.datetime | None, excluded: Sequence[str]) -> tuple[CaseAttribution, ...]:
    ledgers = parse_cases(
        cases,
        as_of=as_of,
        default_excluded=tuple(excluded),
        default_periods=calendar.periods,
        default_holidays=calendar.holidays,
    )
    return tuple(attribute_case(ledger, calendar) for ledger in ledgers)


def analyze_document(document: Mapping[str, Any],
                     as_of: dt.datetime | None = None) -> TimelinessReport:
    """Analyze a fixture/document that carries accepted cases and timelines.

    The document may define its business calendar inline as
    ``{"calendar": {"periods": ..., "holidays": ..., "timezone": ...}}`` or via
    flat ``work_periods`` / ``holidays`` keys.
    """
    if not isinstance(document, Mapping):
        raise TimelinessError('document must be a mapping')
    calendar = _document_calendar(document)
    moment = _as_of(document, as_of)
    attributions = _attribute(
        _raw_cases(document), calendar, moment,
        document.get('excluded_categories') or (),
    )
    resolved = moment or (attributions[0].as_of if attributions else None)
    return TimelinessReport(
        as_of=resolved.isoformat() if resolved else None,
        summary=summarize(attributions, int(document.get('minimum_bottleneck_cases', 1))),
        cases=attributions,
    )


def analyze_cases(cases: Iterable[Mapping[str, Any]], *,
                  calendar: BusinessCalendar | None = None,
                  as_of: dt.datetime | str | None = None,
                  excluded_categories: Sequence[str] = ()) -> TimelinessReport:
    calendar = calendar or build_calendar()
    moment = require_datetime(as_of, 'as_of') if isinstance(as_of, str) else as_of
    attributions = _attribute(tuple(cases), calendar, moment, excluded_categories)
    resolved = moment or (attributions[0].as_of if attributions else None)
    return TimelinessReport(
        as_of=resolved.isoformat() if resolved else None,
        summary=summarize(attributions),
        cases=attributions,
    )


def analyze_case(case: Mapping[str, Any], *, calendar: BusinessCalendar | None = None,
                 as_of: dt.datetime | None = None) -> CaseAttribution:
    calendar = calendar or build_calendar(
        periods=case.get('work_periods'),
        holidays=case.get('holidays'),
    )
    ledger = parse_case(
        case,
        as_of=as_of,
        default_periods=calendar.periods,
        default_holidays=calendar.holidays,
    )
    return attribute_case(ledger, calendar)


def analyze_records(records: Iterable[Mapping[str, Any]], *,
                    as_of: dt.datetime | None = None,
                    calendar: BusinessCalendar | None = None) -> TimelinessReport:
    """Build a timeliness report from stored case records.

    Records produced by the orchestration repository carry an ``attributes``
    payload; the richer timeline can be placed under
    ``attributes['timeliness']`` (or directly in ``attributes``).  Records
    without the needed timestamps are reported as evidence insufficient
    instead of being assumed to be on time.
    """
    cases = []
    for record in records:
        attributes = record.get('attributes') or {}
        if not isinstance(attributes, Mapping):
            attributes = {}
        nested = attributes.get('timeliness')
        detail = nested if isinstance(nested, Mapping) else attributes
        cases.append({
            'case_id': record.get('case_id') or record.get('record_id') or record.get('id'),
            'service_version': detail.get('service_version') or record.get('category'),
            'batch_id': detail.get('batch_id'),
            'opened_at': detail.get('opened_at'),
            'closed_at': detail.get('closed_at'),
            'outcome': detail.get('outcome') or ('completed' if record.get('status') == 'completed' else None),
            'target_seconds': detail.get('target_seconds'),
            'due_at': detail.get('due_at'),
            'steps': detail.get('steps') or [],
            'completed': detail.get('completed') or [],
            'timeline': detail.get('timeline') or detail.get('events') or [],
            'work_periods': detail.get('work_periods'),
            'holidays': detail.get('holidays'),
            'excluded_categories': detail.get('excluded_categories'),
        })
    return analyze_cases(cases, as_of=as_of, calendar=calendar)


@dataclass
class TimelinessAnalyzer:
    """Convenience facade over the attribution functions.

    ``analyze`` accepts a fixture document (a mapping with ``cases`` or
    ``items``), a single case mapping, or a plain sequence of case payloads,
    which makes it usable from the CLI, reporting and tests alike.
    """

    calendar: BusinessCalendar | None = None
    excluded_categories: tuple[str, ...] = ()

    def analyze(self, source: Any, *, as_of: dt.datetime | None = None) -> TimelinessReport:
        if isinstance(source, Mapping) and ('cases' in source or 'items' in source):
            calendar = self.calendar or _document_calendar(source)
            attributions = _attribute(
                _raw_cases(source), calendar, _as_of(source, as_of),
                self.excluded_categories or source.get('excluded_categories') or (),
            )
            moment = _as_of(source, as_of) or (attributions[0].as_of if attributions else None)
            return TimelinessReport(
                as_of=moment.isoformat() if moment else None,
                summary=summarize(
                    attributions,
                    int(source.get('minimum_bottleneck_cases', 1)),
                ),
                cases=attributions,
            )
        if isinstance(source, Mapping):
            case = analyze_case(source, calendar=self.calendar, as_of=as_of)
            return TimelinessReport(as_of=case.as_of.isoformat(), summary=summarize([case]), cases=(case,))
        return analyze_cases(
            source, calendar=self.calendar, as_of=as_of,
            excluded_categories=self.excluded_categories,
        )

    def analyze_case(self, case: Mapping[str, Any], *, as_of: dt.datetime | None = None) -> CaseAttribution:
        return analyze_case(case, calendar=self.calendar, as_of=as_of)

    def analyze_records(self, records: Iterable[Mapping[str, Any]], *,
                        as_of: dt.datetime | None = None) -> TimelinessReport:
        return analyze_records(records, as_of=as_of, calendar=self.calendar)

    def summarize(self, report: TimelinessReport, minimum_bottleneck_cases: int = 1) -> dict[str, Any]:
        return summarize(report.cases, minimum_bottleneck_cases)


# Existing package style exposes a ``*Service`` for each capability; keep an
# alias so either naming convention discovers the same implementation.
TimelinessService = TimelinessAnalyzer
