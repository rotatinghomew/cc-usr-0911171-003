from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..contracts import require_datetime, require_identifier, require_integer
from .model import DEFAULT_EXCLUDED_CATEGORIES, EVENT_KINDS, AttributionCategory, TimelineEvent, TimelinessError


def _categories() -> tuple[AttributionCategory, ...]:
    return tuple(AttributionCategory)


@dataclass(frozen=True)
class CaseLedger:
    """Normalized input for a single accepted case."""

    case_id: str
    opened_at: dt.datetime | None
    closed_at: dt.datetime | None
    as_of: dt.datetime
    outcome: str | None
    target_seconds: int | None
    due_at: dt.datetime | None
    service_version: str | None
    batch_id: str | None
    workflow_steps: tuple[str, ...]
    step_dependencies: Mapping[str, tuple[str, ...]]
    completed_steps: tuple[str, ...]
    events: tuple[TimelineEvent, ...]
    excluded_categories: tuple[str, ...]
    work_periods: tuple[Any, ...] | None
    holidays: frozenset[dt.date]


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = ' '.join(str(value).split())
    return text or None


def _holiday_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def _optional_datetime(value: Any) -> dt.datetime | None:
    if value in (None, ''):
        return None
    return require_datetime(value, 'timestamp')


def _parse_event(raw: Mapping[str, Any], position: int) -> TimelineEvent:
    kind = _optional_text(raw.get('kind') or raw.get('event') or raw.get('type'))
    if kind not in EVENT_KINDS:
        raise TimelinessError(f'unsupported timeline event: {kind!r}')
    occurred_at = _optional_datetime(raw.get('occurred_at') or raw.get('at') or raw.get('timestamp'))
    if occurred_at is None:
        raise TimelinessError(f'event {kind!r} is missing occurred_at')
    step_id = _optional_text(raw.get('step_id') or raw.get('step'))
    related = _optional_text(raw.get('related_step') or raw.get('depends_on'))
    return TimelineEvent(
        kind=kind,
        occurred_at=occurred_at,
        step_id=step_id,
        related_step=related,
        reason=_optional_text(raw.get('reason')),
        outcome=_optional_text(raw.get('outcome')),
        sequence=int(raw.get('sequence', position)),
    )


def normalize_events(raw: Iterable[Mapping[str, Any]] | None) -> tuple[TimelineEvent, ...]:
    if raw is None:
        return ()
    parsed = [_parse_event(item, index) for index, item in enumerate(raw, 1)]
    # Preserve supplied order but make sequence stable and reject exact duplicate
    # timestamps for the same kind/step, since those cannot be ordered safely.
    ordered = tuple(sorted(parsed, key=lambda item: (item.occurred_at, item.sequence)))
    seen: set[tuple[Any, ...]] = set()
    for event in ordered:
        key = (event.kind, event.step_id, event.occurred_at)
        if key in seen:
            raise TimelinessError(f'duplicate event: {event.kind} at {event.occurred_at.isoformat()}')
        seen.add(key)
    return ordered


def parse_case(raw: Mapping[str, Any], *, as_of: dt.datetime | None = None,
               default_excluded: Sequence[str] = (),
               default_periods: Sequence[Any] | None = None,
               default_holidays: Iterable[dt.date] = ()) -> CaseLedger:
    if not isinstance(raw, Mapping):
        raise TimelinessError('case payload must be a mapping')
    identifier = _optional_text(raw.get('case_id') or raw.get('id'))
    if identifier is None:
        raise TimelinessError('case_id is required')
    case_id = require_identifier(identifier, 'case_id')
    delivery = raw.get('delivery') if isinstance(raw.get('delivery'), Mapping) else {}
    workflow = raw.get('workflow') if isinstance(raw.get('workflow'), Mapping) else {}
    steps_raw = raw.get('steps') or workflow.get('steps') or ()
    workflow_steps: list[str] = []
    step_dependencies: dict[str, tuple[str, ...]] = {}
    for step in steps_raw:
        if isinstance(step, Mapping):
            value = _optional_text(step.get('id'))
            requires = tuple(str(item) for item in (step.get('requires') or ()) if item is not None)
        else:
            value = _optional_text(step)
            requires = ()
        if value:
            workflow_steps.append(value)
            step_dependencies[value] = requires
    completed_raw = raw.get('completed')
    if completed_raw is None:
        completed_raw = workflow.get('completed', ())
    completed_steps = tuple(sorted({str(item) for item in completed_raw if item is not None}))
    outcome = _optional_text(raw.get('outcome') or delivery.get('outcome'))
    opened_at = _optional_datetime(raw.get('opened_at') or raw.get('accepted_at') or delivery.get('opened_at'))
    closed_at = _optional_datetime(raw.get('closed_at') or delivery.get('closed_at'))
    target_raw = raw.get('target_seconds')
    if target_raw is None:
        target_raw = delivery.get('target_seconds')
    target_seconds = None if target_raw in (None, '') else require_integer(target_raw, 'target_seconds', 1, 10**9)
    due_raw = raw.get('due_at') or delivery.get('due_at')
    due_at = _optional_datetime(due_raw)
    excluded = raw.get('excluded_categories')
    if excluded is None:
        excluded = default_excluded
    if excluded is None:
        excluded = DEFAULT_EXCLUDED_CATEGORIES
    excluded_categories = tuple(sorted({str(item) for item in excluded}))
    unknown = sorted(set(excluded_categories) - {category.value for category in _categories()})
    if unknown:
        raise TimelinessError('unknown excluded category: ' + ','.join(unknown))
    periods = raw.get('work_periods') or default_periods
    holiday_values = raw.get('holidays')
    holiday_dates = frozenset(
        _holiday_date(value)
        for value in (holiday_values if holiday_values is not None else default_holidays)
    )
    events = normalize_events(raw.get('timeline') or raw.get('events'))
    if opened_at is None:
        opened_at = next((event.occurred_at for event in events if event.kind == 'opened'), None)
    if closed_at is None:
        closed_at = next((event.occurred_at for event in events if event.kind == 'closed'), None)
    if outcome is None:
        closing = next((event for event in reversed(events) if event.kind == 'closed'), None)
        if closing is not None:
            outcome = closing.outcome
    moment = require_datetime(as_of or raw.get('as_of') or dt.datetime.now(dt.timezone.utc), 'as_of')
    if closed_at is not None and moment < closed_at:
        moment = closed_at
    references = {
        'service_version': _optional_text(raw.get('service_version') or workflow.get('version') or delivery.get('service_version')),
        'batch_id': _optional_text(raw.get('batch_id') or raw.get('acceptance_batch') or delivery.get('batch_id')),
    }
    return CaseLedger(
        case_id=case_id,
        opened_at=opened_at,
        closed_at=closed_at,
        as_of=moment,
        outcome=outcome,
        target_seconds=target_seconds,
        due_at=due_at,
        service_version=references['service_version'],
        batch_id=references['batch_id'],
        workflow_steps=tuple(workflow_steps),
        step_dependencies=step_dependencies,
        completed_steps=completed_steps,
        events=events,
        excluded_categories=excluded_categories,
        work_periods=tuple(periods) if periods else None,
        holidays=holiday_dates,
    )


def parse_cases(items: Iterable[Mapping[str, Any]], **defaults: Any) -> tuple[CaseLedger, ...]:
    return tuple(parse_case(item, **defaults) for item in items)


def ledger_from_snapshot(snapshot: Mapping[str, Any]) -> tuple[CaseLedger, ...]:
    cases = snapshot.get('cases') if isinstance(snapshot, Mapping) else None
    if cases is None:
        cases = []
    return parse_cases(
        cases,
        as_of=snapshot.get('as_of'),
        default_excluded=snapshot.get('excluded_categories') or (),
        default_periods=snapshot.get('work_periods'),
        default_holidays=snapshot.get('holidays') or (),
    )
