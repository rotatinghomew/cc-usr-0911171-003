from __future__ import annotations

from collections import Counter, defaultdict

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..intake.service_clock import BusinessCalendar, ClockError
from .calendar import build_calendar, deadline_from, split_working
from .ledger import CaseLedger
from .model import (
    DEFAULT_EXCLUDED_CATEGORIES,
    AttributionCategory,
    AttributionSegment,
    CaseAttribution,
    EvidenceIssue,
    TimelineEvent,
    Verdict,
)

UTC = dt.timezone.utc

# Opening event → the events that close it.  A step attempt may end either in
# ``step_completed`` or in ``step_failed``; both terminate the working interval,
# after which compensation or rework is attributed separately.
OPEN_CLOSE = {
    'step_started': ('step_completed', 'step_failed'),
    'pause_started': ('pause_ended',),
    'correction_requested': ('correction_received',),
    'compensation_started': ('compensation_completed',),
    'rework_started': ('rework_completed',),
}

CATEGORY_BY_OPENING = {
    'step_started': AttributionCategory.HANDLING,
    'pause_started': AttributionCategory.PAUSE,
    'correction_requested': AttributionCategory.MATERIAL_CORRECTION,
    'compensation_started': AttributionCategory.COMPENSATION,
    'rework_started': AttributionCategory.REWORK,
}

OPEN_BY_CLOSE = {closer: opener for opener, closers in OPEN_CLOSE.items() for closer in closers}

# When two attributed intervals overlap, the more specific explanation wins.
# A suspension or material correction therefore takes precedence over the
# generic handling interval it is embedded in, so no working second is
# double-counted and the pause is never hidden inside handling time.
CATEGORY_PRIORITY = {
    AttributionCategory.MATERIAL_CORRECTION: 4,
    AttributionCategory.PAUSE: 4,
    AttributionCategory.COMPENSATION: 3,
    AttributionCategory.REWORK: 3,
    AttributionCategory.HANDLING: 2,
    AttributionCategory.BLOCKED_PREDECESSOR: 2,
    AttributionCategory.QUEUE: 2,
    AttributionCategory.UNATTRIBUTED: 1,
}


def _expected_closers(opener: str) -> str:
    return ' or '.join(OPEN_CLOSE[opener])


@dataclass(frozen=True)
class _Interval:
    category: AttributionCategory
    start: dt.datetime
    end: dt.datetime
    step_id: str | None
    related_step: str | None
    source: str
    attempt: int = 1


@dataclass
class _State:
    issues: list[EvidenceIssue] = field(default_factory=list)
    basis: list[str] = field(default_factory=list)

    def issue(self, code: str, message: str, step_id: str | None = None, blocking: bool = True) -> None:
        self.issues.append(EvidenceIssue(code, message, step_id, blocking))

    def note(self, message: str) -> None:
        self.basis.append(message)


def _ordered(events: Sequence[TimelineEvent]) -> list[TimelineEvent]:
    return sorted(events, key=lambda item: (item.occurred_at, item.sequence, item.kind))


def _pair_intervals(events: Sequence[TimelineEvent], horizon_end: dt.datetime,
                    case_closed: bool, state: _State) -> list[_Interval]:
    """Pair opening and closing events into attributed intervals.

    Pairing is per (opening kind, step).  Closing events without a matching
    opening are reported as evidence gaps because the start of that work
    cannot be reconstructed.  Opening events without a closing event are
    extended to the evaluation horizon when the case is still open; on a
    closed case they are a blocking gap.
    """
    pending: dict[tuple[str, str | None], list[TimelineEvent]] = defaultdict(list)
    unopened: list[TimelineEvent] = []
    intervals: list[_Interval] = []
    attempts: Counter[tuple[str, str | None]] = Counter()
    last_failed: dict[str | None, TimelineEvent] = {}
    recovered_steps: set[str | None] = set()
    saw_events = False

    for event in _ordered(events):
        saw_events = True
        if event.kind in OPEN_CLOSE:
            if event.kind == 'step_started':
                attempts[(event.kind, event.step_id)] += 1
            pending[(event.kind, event.step_id)].append(event)
            continue
        if event.kind == 'step_failed':
            last_failed[event.step_id] = event
        if event.kind in {'rework_completed', 'compensation_completed'}:
            # Only an explicit recovery event proves the failure was dealt with;
            # otherwise the missing ``step_started`` remains a real gap.
            recovered_steps.add(event.step_id)
        opener = OPEN_BY_CLOSE.get(event.kind)
        if opener is None:
            continue
        key = (opener, event.step_id)
        candidates = pending.get(key)
        if not candidates and event.step_id is not None:
            # Fall back to an opening recorded without a step id; this happens
            # with older exports that put the step on only one event.
            candidates = pending.get((opener, None))
            key = (opener, None)
        if not candidates:
            if event.kind == 'step_completed' and event.step_id in recovered_steps:
                # A failed step that was compensated and reworked legitimately
                # ends with ``step_completed``; the recovery intervals already
                # carry its time, so this is documentation, not a new attempt.
                continue
            unopened.append(event)
            continue
        opening = candidates.pop(0)
        related = opening.related_step or event.related_step
        if related is None and opener in {'compensation_started', 'rework_started'}:
            # Compensation and rework belong to the step that failed most
            # recently before they began.
            failed = [item for item in last_failed.values() if item.occurred_at <= opening.occurred_at]
            if failed:
                related = max(failed, key=lambda item: item.occurred_at).step_id
        intervals.append(_Interval(
            category=CATEGORY_BY_OPENING[opener],
            start=opening.occurred_at,
            end=event.occurred_at,
            step_id=opening.step_id or event.step_id,
            related_step=related,
            source=f'{opener}->{event.kind}',
            attempt=attempts.get((opener, opening.step_id), 1),
        ))

    for event in unopened:
        opener = OPEN_BY_CLOSE[event.kind]
        state.issue(
            'missing_open:' + opener,
            f'{event.kind} at {event.occurred_at.isoformat()} has no preceding {opener}; '
            'its start cannot be attributed',
            event.step_id,
        )

    for (opener, step_id), values in sorted(pending.items()):
        for opening in values:
            category = CATEGORY_BY_OPENING[opener]
            intervals.append(_Interval(
                category=category,
                start=opening.occurred_at,
                end=horizon_end,
                step_id=step_id,
                related_step=opening.related_step,
                source=f'{opener}-><open>',
                attempt=attempts.get((opener, step_id), 1),
            ))
            if case_closed:
                state.issue(
                    'missing_close:' + opener,
                    f'{opener} at {opening.occurred_at.isoformat()} has no matching '
                    f'{_expected_closers(opener)} before closure',
                    step_id,
                )
            else:
                state.issue(
                    'pending_close:' + opener,
                    f'{opener} at {opening.occurred_at.isoformat()} is still open at the evaluation time',
                    step_id,
                    blocking=False,
                )
    if not saw_events and events:
        state.issue('unreadable_timeline', 'timeline contains no recognised events')
    return intervals


def _clip(intervals: Iterable[_Interval], start: dt.datetime, end: dt.datetime) -> list[_Interval]:
    result = []
    for interval in intervals:
        left = max(interval.start, start)
        right = min(interval.end, end)
        if right > left:
            result.append(_Interval(interval.category, left, right, interval.step_id,
                                    interval.related_step, interval.source, interval.attempt))
    return sorted(result, key=lambda item: (item.start, item.end))


def _subtract(base: tuple[dt.datetime, dt.datetime],
              cuts: Sequence[tuple[dt.datetime, dt.datetime]]) -> list[tuple[dt.datetime, dt.datetime]]:
    pieces = [base]
    for cut_start, cut_end in cuts:
        next_pieces = []
        for left, right in pieces:
            if cut_end <= left or cut_start >= right:
                next_pieces.append((left, right))
                continue
            if left < cut_start:
                next_pieces.append((left, cut_start))
            if cut_end < right:
                next_pieces.append((cut_end, right))
        pieces = next_pieces
    return [(left, right) for left, right in pieces if right > left]


def _merge_ranges(ranges: Sequence[tuple[dt.datetime, dt.datetime]]) -> list[tuple[dt.datetime, dt.datetime]]:
    merged: list[tuple[dt.datetime, dt.datetime]] = []
    for left, right in sorted(ranges):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


def _completed_steps(events: Sequence[TimelineEvent]) -> list[tuple[dt.datetime, str | None]]:
    return [(event.occurred_at, event.step_id) for event in events if event.kind == 'step_completed']


def _in_recovery(events: Sequence[TimelineEvent], start: dt.datetime, end: dt.datetime) -> str | None:
    """Return the step under recovery if ``[start, end)`` sits inside it.

    A failed step is in recovery from the failure until its next completed
    attempt.  Time in that window is compensation or rework, never a
    predecessor wait, even when the individual recovery events are missing.
    """
    for event in _ordered(events):
        if event.kind != 'step_failed':
            continue
        if event.occurred_at > start:
            break
        recovered = any(
            other.step_id == event.step_id
            and other.kind in {'rework_completed', 'step_completed', 'compensation_completed'}
            and other.occurred_at >= end
            for other in events
        )
        if recovered:
            return event.step_id
    return None


def _first_pending_step(ledger: CaseLedger, completed: set[str | None]) -> str | None:
    # Only steps completed *by the gap start* count here; using the ledger's
    # current progress would wrongly treat later completions as already done.
    for step in ledger.workflow_steps:
        if step not in {item for item in completed if item}:
            return step
    return None


def _blocking_predecessor(ledger: CaseLedger, step: str | None,
                          completed: set[str | None]) -> str | None:
    """Return the prerequisite that has completed but whose hand-off is late.

    This names the step the pending step is waiting on, which is what a
    supervisor needs in order to chase the right desk.
    """
    if step is None:
        return None
    done = {item for item in completed if item}
    for dependency in ledger.step_dependencies.get(step, ()):
        if dependency in done:
            return dependency
    return None


def _classify_gap(ledger: CaseLedger,
                  completed_before: Sequence[tuple[dt.datetime, str | None]],
                  failed_before: Sequence[tuple[dt.datetime, str | None]]) -> tuple[AttributionCategory, str | None, str | None]:
    """Give an otherwise unexplained gap an explainable owner.

    Gaps between recorded steps are waiting time.  When a step has completed
    and another step is still pending, the wait is charged to the pending step
    and cross-referenced to the completed step it is waiting on.  A gap that
    follows a failure without a compensation or rework event is attributed to
    recovery of that step instead of pretending a predecessor is blocking it.
    With no progress yet the case is queued for its first step.
    """
    completed_names = {step for _, step in completed_before if step}
    next_step = _first_pending_step(ledger, completed_names)
    last_completed = completed_before[-1][1] if completed_before else None
    if failed_before:
        failed_at, failed_step = failed_before[-1]
        recovered = any(
            item[0] > failed_at and item[1] == failed_step for item in completed_before
        )
        if not recovered and (last_completed is None or failed_at >= completed_before[-1][0]):
            return AttributionCategory.COMPENSATION, failed_step, failed_step
    if next_step is not None and last_completed is not None:
        predecessor = _blocking_predecessor(ledger, next_step, completed_names) or last_completed
        return AttributionCategory.BLOCKED_PREDECESSOR, next_step, predecessor
    if next_step is not None:
        return AttributionCategory.QUEUE, next_step, None
    return AttributionCategory.HANDLING, last_completed, None


def _resolve_due(ledger: CaseLedger, calendar: BusinessCalendar,
                 extensions: Sequence[tuple[dt.datetime, dt.datetime]],
                 state: _State) -> tuple[dt.datetime | None, str | None, dt.datetime | None, int | None]:
    computed = None
    if ledger.opened_at is not None and ledger.target_seconds is not None:
        try:
            computed = deadline_from(calendar, ledger.case_id, ledger.opened_at,
                                     ledger.target_seconds, extensions)
        except ClockError as exc:
            state.issue('due_computation_failed', str(exc))
    if ledger.due_at is not None:
        if computed is not None and computed != ledger.due_at:
            state.issue(
                'due_at_mismatch',
                f'recorded due_at {ledger.due_at.isoformat()} does not match recomputed '
                f'{computed.isoformat()} from the business calendar',
                blocking=False,
            )
            state.note('due_at taken from the acceptance record; business-calendar recomputation differs')
        else:
            state.note('due_at taken from the acceptance record')
        return ledger.due_at, 'recorded', computed, ledger.target_seconds
    if computed is not None:
        state.note('due_at recomputed from target_seconds and excluded periods using the business calendar')
        return computed, 'computed', computed, ledger.target_seconds
    return None, None, None, ledger.target_seconds


def attribute_case(ledger: CaseLedger, calendar: BusinessCalendar | None = None) -> CaseAttribution:
    calendar = calendar or build_calendar(periods=ledger.work_periods, holidays=ledger.holidays)
    state = _State()
    state.note(
        f"business calendar {calendar.name!r}: {len(calendar.periods)} work periods, "
        f"{len(calendar.holidays)} holiday(s)"
    )

    outcome = (ledger.outcome or ('completed' if ledger.closed_at else 'in_progress')).lower()
    if ledger.outcome is None:
        state.note('outcome inferred from the presence of closed_at')

    if ledger.opened_at is None:
        state.issue('missing_opened_at', 'case has no opening timestamp; elapsed time cannot be attributed')
    if not ledger.events:
        state.issue('missing_timeline',
                    'case has no step timeline; per-step time cannot be reviewed')
    if ledger.closed_at is not None and ledger.opened_at is not None and ledger.closed_at < ledger.opened_at:
        state.issue('closed_before_opened', 'closed_at precedes opened_at')
    if ledger.target_seconds is None and ledger.due_at is None:
        state.issue('missing_due_basis', 'neither target_seconds nor due_at is recorded')
    if outcome in {'cancelled', 'canceled', 'withdrawn'} and ledger.closed_at is None:
        state.issue('cancelled_without_timestamp', 'case is cancelled but has no closing timestamp')

    if ledger.opened_at is None:
        horizon_end = ledger.closed_at or ledger.as_of
    else:
        horizon_end = ledger.closed_at if ledger.closed_at is not None else ledger.as_of
        if horizon_end < ledger.opened_at:
            state.issue('invalid_window', 'evaluation horizon precedes the opening timestamp')
            horizon_end = ledger.opened_at

    raw_intervals = _pair_intervals(ledger.events, horizon_end, ledger.closed_at is not None, state)

    excluded = set(ledger.excluded_categories) | set(DEFAULT_EXCLUDED_CATEGORIES)
    # Pauses and material corrections extend the promise by their full
    # business-calendar duration, exactly as ServiceClock.deadline does.  Only
    # the start is clipped at the opening timestamp; an interval that ran past
    # the evaluation horizon legitimately still extends the due time.
    extension_intervals = _merge_ranges([
        (max(interval.start, ledger.opened_at or interval.start), interval.end)
        for interval in raw_intervals
        if interval.category.value in excluded and interval.end > interval.start
    ])
    if any(interval.category.value in excluded and interval.end >= horizon_end
           for interval in raw_intervals) and ledger.closed_at is None:
        state.issue(
            'open_excluded_period',
            'an excluded period is still open; the recomputed due time is a lower bound',
            blocking=False,
        )
    due_at, due_source, computed_due, target_seconds = _resolve_due(ledger, calendar, extension_intervals, state)

    segments: list[AttributionSegment] = []
    non_working = {'holiday_seconds': 0, 'weekend_seconds': 0, 'off_hours_seconds': 0}
    working_total = 0
    calendar_total = 0
    if ledger.opened_at is not None and horizon_end >= ledger.opened_at:
        ordered = _clip(raw_intervals, ledger.opened_at, horizon_end)
        # Resolve overlaps by specificity: a pause or correction opened while a
        # step is running wins for the overlap, then compensation/rework, then
        # ordinary handling.  This keeps every working second attributed once.
        claimed: list[tuple[dt.datetime, dt.datetime]] = []
        ordered.sort(key=lambda item: (-CATEGORY_PRIORITY.get(item.category, 0), item.start, item.end))
        for interval in ordered:
            for piece_start, piece_end in _subtract((interval.start, interval.end), claimed):
                split = split_working(calendar, piece_start, piece_end)
                working_total += split.working_seconds
                calendar_total += split.calendar_seconds
                non_working['holiday_seconds'] += split.holiday_seconds
                non_working['weekend_seconds'] += split.weekend_seconds
                non_working['off_hours_seconds'] += split.off_hours_seconds
                segments.append(AttributionSegment(
                    category=interval.category,
                    start=piece_start,
                    end=piece_end,
                    step_id=interval.step_id,
                    related_step=interval.related_step,
                    working_seconds=split.working_seconds,
                    calendar_seconds=split.calendar_seconds,
                    source=interval.source,
                    attempt=interval.attempt,
                    holiday_seconds=split.holiday_seconds,
                    weekend_seconds=split.weekend_seconds,
                    off_hours_seconds=split.off_hours_seconds,
                ))
            claimed = _merge_ranges(claimed + [(interval.start, interval.end)])

        completed_events = _completed_steps(ledger.events)
        failed_events = [(event.occurred_at, event.step_id) for event in ledger.events if event.kind == 'step_failed']
        for piece_start, piece_end in _subtract((ledger.opened_at, horizon_end), claimed):
            in_recovery = _in_recovery(ledger.events, piece_start, piece_end)
            if in_recovery is not None:
                category, step_id = AttributionCategory.COMPENSATION, in_recovery
                related = in_recovery
            elif ledger.events:
                completed_before = [item for item in completed_events if item[0] <= piece_start]
                failed_before = [item for item in failed_events if item[0] <= piece_start]
                category, step_id, related = _classify_gap(ledger, completed_before, failed_before)
            else:
                category, step_id, related = AttributionCategory.UNATTRIBUTED, None, None
            split = split_working(calendar, piece_start, piece_end)
            working_total += split.working_seconds
            calendar_total += split.calendar_seconds
            non_working['holiday_seconds'] += split.holiday_seconds
            non_working['weekend_seconds'] += split.weekend_seconds
            non_working['off_hours_seconds'] += split.off_hours_seconds
            segments.append(AttributionSegment(
                category=category,
                start=piece_start,
                end=piece_end,
                step_id=step_id,
                related_step=related,
                working_seconds=split.working_seconds,
                calendar_seconds=split.calendar_seconds,
                source='inferred_gap',
                holiday_seconds=split.holiday_seconds,
                weekend_seconds=split.weekend_seconds,
                off_hours_seconds=split.off_hours_seconds,
            ))
        segments.sort(key=lambda item: (item.start, item.end))
        state.note(
            'working time uses the same work periods and holidays as ServiceClock; '
            'weekends, holidays and off-hours are reported separately and never charged'
        )

    by_category: dict[str, int] = {}
    by_step: dict[str, dict[str, int]] = {}
    excluded_working = 0
    for segment in segments:
        name = segment.category.value
        by_category[name] = by_category.get(name, 0) + segment.working_seconds
        bucket = by_step.setdefault(segment.step_id or '<case>', {'working_seconds': 0, 'calendar_seconds': 0})
        bucket['working_seconds'] += segment.working_seconds
        bucket['calendar_seconds'] += segment.calendar_seconds
        if name in excluded:
            excluded_working += segment.working_seconds
    chargeable = max(0, working_total - excluded_working)

    verdict, overdue_seconds, remaining_seconds = _verdict(
        ledger, calendar, state, due_at, horizon_end, outcome, chargeable
    )

    return CaseAttribution(
        case_id=ledger.case_id,
        service_version=ledger.service_version,
        batch_id=ledger.batch_id,
        outcome=outcome,
        opened_at=ledger.opened_at,
        closed_at=ledger.closed_at,
        as_of=ledger.as_of,
        due_at=due_at,
        due_at_source=due_source,
        computed_due_at=computed_due,
        target_seconds=target_seconds,
        verdict=verdict,
        evidence_sufficient=not any(issue.blocking for issue in state.issues),
        issues=tuple(state.issues),
        segments=tuple(segments),
        excluded_categories=tuple(sorted(excluded)),
        calendar_name=calendar.name,
        holidays=tuple(sorted(day.isoformat() for day in calendar.holidays)),
        working_seconds_total=working_total,
        chargeable_working_seconds=chargeable,
        excluded_working_seconds=excluded_working,
        calendar_seconds_total=calendar_total,
        non_working_seconds=sum(non_working.values()),
        non_working_breakdown=non_working,
        overdue_seconds=overdue_seconds,
        remaining_seconds=remaining_seconds,
        by_category=by_category,
        by_step=by_step,
        basis=tuple(state.basis),
    )


def _verdict(ledger: CaseLedger, calendar: BusinessCalendar, state: _State,
             due_at: dt.datetime | None, horizon_end: dt.datetime, outcome: str,
             chargeable: int) -> tuple[Verdict, int, int]:
    if outcome in {'cancelled', 'canceled', 'withdrawn'}:
        return Verdict.CANCELLED, 0, 0
    if any(issue.blocking for issue in state.issues):
        return Verdict.EVIDENCE_INSUFFICIENT, 0, 0
    if due_at is None and ledger.target_seconds is None:
        return Verdict.EVIDENCE_INSUFFICIENT, 0, 0

    if ledger.closed_at is None:
        # Open case: no final on-time/overdue verdict is possible, but an
        # already-passed due time is a fact and is reported as such.
        if due_at is not None:
            overdue = calendar.working_seconds(due_at, ledger.as_of) if ledger.as_of > due_at else 0
            if overdue > 0:
                return Verdict.OVERDUE, overdue, 0
            return Verdict.IN_PROGRESS, 0, calendar.working_seconds(ledger.as_of, due_at)
        return Verdict.IN_PROGRESS, 0, max(0, (ledger.target_seconds or 0) - chargeable)

    if due_at is not None:
        if ledger.closed_at <= due_at:
            return Verdict.WITHIN_TARGET, 0, calendar.working_seconds(ledger.closed_at, due_at)
        overdue = calendar.working_seconds(due_at, ledger.closed_at)
        # A case closed after its due time entirely during non-working hours
        # (a holiday or after-hours) consumed no committed business time and is
        # therefore still within target.
        if overdue == 0:
            return Verdict.WITHIN_TARGET, 0, 0
        return Verdict.OVERDUE, overdue, 0
    overdue = max(0, chargeable - (ledger.target_seconds or 0))
    remaining = max(0, (ledger.target_seconds or 0) - chargeable)
    return (Verdict.OVERDUE if overdue else Verdict.WITHIN_TARGET), overdue, remaining


def attribute_cases(ledgers: Iterable[CaseLedger],
                    calendars: Mapping[tuple[Any, ...], BusinessCalendar] | None = None) -> tuple[CaseAttribution, ...]:
    cache: dict[tuple[Any, ...], BusinessCalendar] = dict(calendars or {})
    results = []
    for ledger in ledgers:
        key = _calendar_key(ledger)
        calendar = cache.get(key)
        if calendar is None:
            calendar = build_calendar(periods=ledger.work_periods, holidays=ledger.holidays)
            cache[key] = calendar
        results.append(attribute_case(ledger, calendar))
    return tuple(results)


def _calendar_key(ledger: CaseLedger) -> tuple[Any, ...]:
    periods = tuple(
        (getattr(item, 'weekday', None), str(getattr(item, 'start', item)), str(getattr(item, 'end', '')))
        for item in (ledger.work_periods or ())
    )
    return periods, tuple(sorted(ledger.holidays))
