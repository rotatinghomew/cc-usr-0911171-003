from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..intake.service_clock import BusinessCalendar, ClockError, Pause, ServiceClock, WorkPeriod

UTC = dt.timezone.utc

DEFAULT_WORK_PERIODS = tuple(WorkPeriod(day, dt.time(9), dt.time(17)) for day in range(5))


class TimelinessCalendarError(ValueError):
    pass


def parse_periods(raw: Any) -> tuple[WorkPeriod, ...]:
    if raw in (None, ()):
        return DEFAULT_WORK_PERIODS
    periods = []
    for item in raw:
        if isinstance(item, WorkPeriod):
            periods.append(item)
            continue
        if not isinstance(item, Mapping):
            raise TimelinessCalendarError('work period must be a mapping')
        try:
            weekday = int(item['weekday'])
            start = dt.time.fromisoformat(str(item['start']))
            end = dt.time.fromisoformat(str(item['end']))
            periods.append(WorkPeriod(weekday, start, end))
        except (KeyError, TypeError, ValueError) as exc:
            raise TimelinessCalendarError('invalid work period') from exc
    if not periods:
        raise TimelinessCalendarError('at least one work period is required')
    return tuple(periods)


def parse_holidays(values: Any) -> frozenset[dt.date]:
    if values in (None, ()):
        return frozenset()
    result = set()
    for value in values:
        if isinstance(value, dt.datetime):
            result.add(value.date())
        elif isinstance(value, dt.date):
            result.add(value)
        else:
            try:
                result.add(dt.date.fromisoformat(str(value)))
            except ValueError as exc:
                raise TimelinessCalendarError(f'invalid holiday: {value!r}') from exc
    return frozenset(result)


def build_calendar(config: Mapping[str, Any] | None = None, *, name: str | None = None,
                   periods: Any = None, holidays: Any = None,
                   timezone: dt.tzinfo = UTC) -> BusinessCalendar:
    config = dict(config or {})
    calendar_name = str(config.get('name') or name or 'default')
    weekday_periods = parse_periods(config.get('periods', periods))
    holiday_dates = parse_holidays(config.get('holidays', holidays))
    raw_timezone = config.get('timezone')
    if raw_timezone not in (None, '', 'UTC', 'utc'):
        try:
            from zoneinfo import ZoneInfo
            timezone = ZoneInfo(str(raw_timezone))
        except Exception as exc:  # pragma: no cover - depends on tz database
            raise TimelinessCalendarError(f'invalid timezone: {raw_timezone!r}') from exc
    try:
        return BusinessCalendar(calendar_name, weekday_periods, holiday_dates, timezone=timezone)
    except ClockError as exc:
        raise TimelinessCalendarError(str(exc)) from exc


@dataclass(frozen=True)
class WorkingSplit:
    working_seconds: int
    calendar_seconds: int
    holiday_seconds: int
    weekend_seconds: int
    off_hours_seconds: int

    @property
    def non_working_seconds(self) -> int:
        return self.holiday_seconds + self.weekend_seconds + self.off_hours_seconds


def split_working(calendar: BusinessCalendar, start: dt.datetime, end: dt.datetime) -> WorkingSplit:
    """Split ``[start, end]`` into working and non-working business-calendar buckets.

    The working total always equals ``calendar.working_seconds(start, end)`` so
    any deadline computed elsewhere with the same calendar stays consistent.
    Every calendar second is allocated to exactly one bucket:

    * ``holiday_seconds`` -- a whole holiday date inside the window.
    * ``weekend_seconds`` -- a whole weekend date inside the window.
    * ``off_hours_seconds`` -- time on a working weekday outside the work
      periods.
    * ``working_seconds`` -- time inside the configured work periods.

    The four buckets therefore add up to ``calendar_seconds``, which makes the
    "holiday / weekend / after-hours" explanation of a delay auditable.
    """
    left = calendar.local(start)
    right = calendar.local(end)
    if right < left:
        raise TimelinessCalendarError('end precedes start')
    working = 0
    holiday = 0
    weekend = 0
    off_hours = 0
    day = left.date()
    while day <= right.date():
        day_start = dt.datetime.combine(day, dt.time.min, calendar.timezone)
        day_end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, calendar.timezone)
        window_start = max(left, day_start)
        window_end = min(right, day_end)
        if window_end > window_start:
            total = int((window_end - window_start).total_seconds())
            if day in calendar.holidays:
                holiday += total
            elif day.weekday() >= 5:
                weekend += total
            else:
                day_working = 0
                for opening, closing in calendar.intervals(day):
                    overlap_start = max(window_start, opening)
                    overlap_end = min(window_end, closing)
                    if overlap_end > overlap_start:
                        day_working += int((overlap_end - overlap_start).total_seconds())
                working += day_working
                off_hours += total - day_working
        day += dt.timedelta(days=1)
    return WorkingSplit(
        working_seconds=working,
        calendar_seconds=int((right - left).total_seconds()),
        holiday_seconds=holiday,
        weekend_seconds=weekend,
        off_hours_seconds=off_hours,
    )


def build_clock(calendar: BusinessCalendar) -> ServiceClock:
    return ServiceClock(calendar)


def pauses_from(intervals: Iterable[tuple[dt.datetime, dt.datetime]]) -> tuple[Pause, ...]:
    return tuple(Pause(start, end, 'attribution') for start, end in intervals)


def deadline_from(calendar: BusinessCalendar, case_id: str, opened_at: dt.datetime,
                  target_seconds: int, intervals: Sequence[tuple[dt.datetime, dt.datetime]]) -> dt.datetime:
    clock = ServiceClock(calendar)
    return clock.deadline(case_id, opened_at, target_seconds, pauses_from(intervals)).due_at
