import datetime as dt
import unittest

from one_stop_cases.intake.service_clock import BusinessCalendar, Pause, ServiceClock, WorkPeriod
from one_stop_cases.timeliness import (
    AttributionCategory,
    DEFAULT_EXCLUDED_CATEGORIES,
    TimelinessError,
    Verdict,
    analyze_document,
    attribute_case,
    build_calendar,
    parse_case,
    split_working,
)

UTC = dt.timezone.utc


def moment(day, hour=9, minute=0):
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def work_calendar(holidays=()):
    return BusinessCalendar(
        'gov',
        [WorkPeriod(day, dt.time(9), dt.time(17)) for day in range(5)],
        holidays=holidays,
        timezone=UTC,
    )


def case(**overrides):
    base = {
        'case_id': 'case-1',
        'opened_at': '2026-11-16T09:00:00+00:00',
        'closed_at': '2026-11-17T15:00:00+00:00',
        'target_seconds': 8 * 3600,
        'outcome': 'completed',
        'service_version': 'v1',
        'batch_id': 'batch-1',
        'steps': [
            {'id': 'intake', 'requires': []},
            {'id': 'review', 'requires': ['intake']},
        ],
        'timeline': [
            {'kind': 'step_started', 'step_id': 'intake', 'occurred_at': '2026-11-16T09:00:00+00:00'},
            {'kind': 'step_completed', 'step_id': 'intake', 'occurred_at': '2026-11-16T10:00:00+00:00'},
            {'kind': 'step_started', 'step_id': 'review', 'occurred_at': '2026-11-16T13:00:00+00:00'},
            {'kind': 'pause_started', 'step_id': 'review', 'occurred_at': '2026-11-16T14:00:00+00:00'},
            {'kind': 'pause_ended', 'step_id': 'review', 'occurred_at': '2026-11-16T16:00:00+00:00'},
            {'kind': 'step_completed', 'step_id': 'review', 'occurred_at': '2026-11-17T15:00:00+00:00'},
        ],
    }
    base.update(overrides)
    return base


class CalendarParityTests(unittest.TestCase):
    def test_split_matches_service_clock_working_seconds(self):
        calendar = work_calendar({dt.date(2026, 11, 18)})
        start, end = moment(17, 10), moment(20, 11, 30)
        self.assertEqual(
            split_working(calendar, start, end).working_seconds,
            calendar.working_seconds(start, end),
        )

    def test_split_counts_whole_holiday_and_weekend_dates(self):
        calendar = work_calendar({dt.date(2026, 11, 18)})
        split = split_working(calendar, moment(17, 9), moment(21, 9))
        # Window: Tue 17 09:00 -> Sat 21 09:00.  Wed 18 is a holiday, and only
        # Sat 21 00:00-09:00 falls on the weekend.  Working days inside the
        # window are Tue/Thu/Fri, 8h each.
        self.assertEqual(split.holiday_seconds, 24 * 3600)
        self.assertEqual(split.weekend_seconds, 9 * 3600)
        self.assertEqual(split.working_seconds, 24 * 3600)

    def test_split_buckets_cover_every_calendar_second(self):
        calendar = work_calendar({dt.date(2026, 11, 18)})
        split = split_working(calendar, moment(16, 8), moment(21, 20))
        self.assertEqual(
            split.working_seconds + split.non_working_seconds,
            split.calendar_seconds,
        )
        self.assertEqual(
            split.non_working_seconds,
            split.holiday_seconds + split.weekend_seconds + split.off_hours_seconds,
        )

    def test_inside_single_workday_has_no_non_working_part(self):
        calendar = work_calendar()
        split = split_working(calendar, moment(16, 9), moment(16, 17))
        self.assertEqual(split.off_hours_seconds, 0)
        self.assertEqual(split.working_seconds, 8 * 3600)

    def test_default_calendar_matches_service_clock_deadline(self):
        calendar = work_calendar({dt.date(2026, 11, 18)})
        pauses = [Pause(moment(16, 12), moment(16, 14), 'external')]
        expected = ServiceClock(calendar).deadline('case-1', moment(16, 9), 8 * 3600, pauses).due_at
        ledger = parse_case(
            case(timeline=[
                {'kind': 'opened', 'occurred_at': '2026-11-16T09:00:00+00:00'},
                {'kind': 'pause_started', 'step_id': 'review', 'occurred_at': '2026-11-16T12:00:00+00:00'},
                {'kind': 'pause_ended', 'step_id': 'review', 'occurred_at': '2026-11-16T14:00:00+00:00'},
                {'kind': 'closed', 'occurred_at': '2026-11-17T15:00:00+00:00'},
            ]),
            as_of=moment(20, 9), default_holidays={dt.date(2026, 11, 18)},
        )
        attribution = attribute_case(ledger, calendar)
        self.assertEqual(attribution.due_at, expected)

    def test_calendar_rejects_naive_moments(self):
        calendar = work_calendar()
        with self.assertRaises(Exception):
            calendar.working(dt.datetime(2026, 11, 16, 10))


class AttributionTests(unittest.TestCase):
    def analyze(self, calendar=None, **overrides):
        as_of = overrides.pop('as_of', moment(20, 9))
        return attribute_case(
            parse_case(case(**overrides), as_of=as_of),
            calendar or build_calendar(periods=[{'weekday': day, 'start': '09:00', 'end': '17:00'} for day in range(5)]),
        )

    def test_handling_and_predecessor_wait_are_separate(self):
        attribution = self.analyze()
        self.assertEqual(attribution.by_category['handling'], 9 * 3600)
        self.assertEqual(attribution.by_category['blocked_predecessor'], 3 * 3600)
        self.assertEqual(attribution.by_category['pause'], 2 * 3600)

    def test_pause_is_excluded_and_extends_due_time(self):
        attribution = self.analyze()
        # 8h target + 2h pause ends at 2026-11-17 11:00 (the pause spanned the
        # close of 2026-11-16), and the case closed at 15:00 the same day.
        self.assertEqual(attribution.due_at, moment(17, 11))
        self.assertEqual(attribution.excluded_working_seconds, 2 * 3600)
        self.assertEqual(attribution.chargeable_working_seconds, 12 * 3600)
        self.assertEqual(attribution.verdict, Verdict.OVERDUE)
        self.assertEqual(attribution.overdue_seconds, 4 * 3600)

    def test_on_time_close_produces_remaining_business_time(self):
        attribution = self.analyze(closed_at='2026-11-17T10:00:00+00:00')
        self.assertEqual(attribution.due_at, moment(17, 11))
        self.assertEqual(attribution.verdict, Verdict.WITHIN_TARGET)
        self.assertEqual(attribution.overdue_seconds, 0)
        self.assertEqual(attribution.remaining_seconds, 1 * 3600)

    def test_overdue_spans_holiday_free_business_days(self):
        attribution = self.analyze(closed_at='2026-11-18T15:00:00+00:00')
        self.assertEqual(attribution.due_at, moment(17, 11))
        # 2026-11-17 11:00 -> 17:00 (6h) plus 2026-11-18 09:00 -> 15:00 (6h).
        self.assertEqual(attribution.verdict, Verdict.OVERDUE)
        self.assertEqual(attribution.overdue_seconds, 12 * 3600)

    def test_holiday_not_charged_and_extends_due(self):
        calendar = build_calendar(periods=[{'weekday': day, 'start': '09:00', 'end': '17:00'} for day in range(5)], holidays=['2026-11-17'])
        attribution = attribute_case(parse_case(case(), as_of=moment(20, 9)), calendar)
        # The whole holiday date inside the window is reported separately.
        self.assertEqual(attribution.non_working_breakdown['holiday_seconds'], 15 * 3600)
        self.assertEqual(attribution.due_at, moment(18, 11))
        self.assertEqual(attribution.verdict, Verdict.WITHIN_TARGET)
        self.assertEqual(attribution.overdue_seconds, 0)

    def test_material_correction_attributed_and_excluded(self):
        attribution = self.analyze(timeline=[
            {'kind': 'step_started', 'step_id': 'intake', 'occurred_at': '2026-11-16T09:00:00+00:00'},
            {'kind': 'step_completed', 'step_id': 'intake', 'occurred_at': '2026-11-16T10:00:00+00:00'},
            {'kind': 'correction_requested', 'step_id': 'intake', 'occurred_at': '2026-11-16T10:00:00+00:00'},
            {'kind': 'correction_received', 'step_id': 'intake', 'occurred_at': '2026-11-16T13:00:00+00:00'},
            {'kind': 'step_started', 'step_id': 'review', 'occurred_at': '2026-11-16T13:00:00+00:00'},
            {'kind': 'step_completed', 'step_id': 'review', 'occurred_at': '2026-11-17T15:00:00+00:00'},
        ])
        self.assertEqual(attribution.by_category['material_correction'], 3 * 3600)
        self.assertEqual(attribution.excluded_working_seconds, 3 * 3600)

    def test_compensation_and_rework_are_chargeable(self):
        attribution = self.analyze(timeline=[
            {'kind': 'step_started', 'step_id': 'intake', 'occurred_at': '2026-11-16T09:00:00+00:00'},
            {'kind': 'step_failed', 'step_id': 'intake', 'occurred_at': '2026-11-16T10:00:00+00:00'},
            {'kind': 'compensation_started', 'step_id': 'intake', 'occurred_at': '2026-11-16T10:00:00+00:00'},
            {'kind': 'compensation_completed', 'step_id': 'intake', 'occurred_at': '2026-11-16T12:00:00+00:00'},
            {'kind': 'rework_started', 'step_id': 'intake', 'occurred_at': '2026-11-16T12:00:00+00:00'},
            {'kind': 'rework_completed', 'step_id': 'intake', 'occurred_at': '2026-11-16T14:00:00+00:00'},
            {'kind': 'step_completed', 'step_id': 'intake', 'occurred_at': '2026-11-16T14:00:00+00:00'},
            {'kind': 'step_started', 'step_id': 'review', 'occurred_at': '2026-11-16T14:00:00+00:00'},
            {'kind': 'step_completed', 'step_id': 'review', 'occurred_at': '2026-11-17T15:00:00+00:00'},
        ])
        self.assertEqual(attribution.by_category['compensation'], 2 * 3600)
        self.assertEqual(attribution.by_category['rework'], 2 * 3600)
        self.assertEqual(attribution.evidence_sufficient, True)
        # Both recovery buckets stay chargeable (not in the default exclusions).
        self.assertNotIn('compensation', attribution.excluded_categories)
        self.assertNotIn('rework', attribution.excluded_categories)

    def test_attempt_numbers_track_reopened_steps(self):
        attribution = self.analyze(closed_at='2026-11-16T12:00:00+00:00', timeline=[
            {'kind': 'step_started', 'step_id': 'intake', 'occurred_at': '2026-11-16T09:00:00+00:00'},
            {'kind': 'step_failed', 'step_id': 'intake', 'occurred_at': '2026-11-16T10:00:00+00:00'},
            {'kind': 'step_started', 'step_id': 'intake', 'occurred_at': '2026-11-16T11:00:00+00:00'},
            {'kind': 'step_completed', 'step_id': 'intake', 'occurred_at': '2026-11-16T12:00:00+00:00'},
        ])
        handling = [segment for segment in attribution.segments if segment.category is AttributionCategory.HANDLING]
        self.assertEqual([segment.attempt for segment in handling], [1, 2])
        self.assertEqual(sum(segment.working_seconds for segment in handling), 2 * 3600)

    def test_overlapping_pause_wins_over_handling_without_double_count(self):
        attribution = self.analyze(closed_at='2026-11-16T13:00:00+00:00', timeline=[
            {'kind': 'step_started', 'step_id': 'intake', 'occurred_at': '2026-11-16T09:00:00+00:00'},
            {'kind': 'pause_started', 'step_id': 'intake', 'occurred_at': '2026-11-16T10:00:00+00:00'},
            {'kind': 'pause_ended', 'step_id': 'intake', 'occurred_at': '2026-11-16T12:00:00+00:00'},
            {'kind': 'step_completed', 'step_id': 'intake', 'occurred_at': '2026-11-16T13:00:00+00:00'},
        ])
        self.assertEqual(attribution.by_category['pause'], 2 * 3600)
        self.assertEqual(attribution.by_category['handling'], 2 * 3600)
        self.assertEqual(attribution.working_seconds_total, 4 * 3600)

    def test_missing_timeline_marks_evidence_insufficient(self):
        attribution = self.analyze(timeline=[])
        self.assertEqual(attribution.verdict, Verdict.EVIDENCE_INSUFFICIENT)
        self.assertFalse(attribution.evidence_sufficient)
        self.assertIn('missing_timeline', {issue.code for issue in attribution.issues})

    def test_missing_opening_timestamp_is_not_assumed_on_time(self):
        attribution = self.analyze(opened_at=None)
        self.assertEqual(attribution.verdict, Verdict.EVIDENCE_INSUFFICIENT)
        self.assertIn('missing_opened_at', {issue.code for issue in attribution.issues})

    def test_unclosed_step_on_closed_case_is_blocking(self):
        attribution = self.analyze(timeline=[
            {'kind': 'step_started', 'step_id': 'intake', 'occurred_at': '2026-11-16T09:00:00+00:00'},
        ])
        self.assertEqual(attribution.verdict, Verdict.EVIDENCE_INSUFFICIENT)
        self.assertIn('missing_close:step_started', {issue.code for issue in attribution.issues})

    def test_unclosed_step_on_open_case_is_pending_not_blocking(self):
        attribution = self.analyze(closed_at=None, outcome='in_progress', timeline=[
            {'kind': 'step_started', 'step_id': 'intake', 'occurred_at': '2026-11-16T09:00:00+00:00'},
        ])
        codes = {issue.code for issue in attribution.issues}
        self.assertIn('pending_close:step_started', codes)
        self.assertNotIn('missing_close:step_started', codes)
        self.assertIn(attribution.verdict, {Verdict.OVERDUE, Verdict.IN_PROGRESS})

    def test_closing_without_opening_is_blocking(self):
        attribution = self.analyze(timeline=[
            {'kind': 'step_completed', 'step_id': 'intake', 'occurred_at': '2026-11-16T10:00:00+00:00'},
        ])
        self.assertEqual(attribution.verdict, Verdict.EVIDENCE_INSUFFICIENT)
        self.assertIn('missing_open:step_started', {issue.code for issue in attribution.issues})

    def test_recorded_due_at_is_authoritative_but_mismatch_flagged(self):
        attribution = self.analyze(due_at='2026-11-17T18:00:00+00:00')
        self.assertEqual(attribution.due_at_source, 'recorded')
        self.assertEqual(attribution.due_at, moment(17, 18))
        self.assertEqual(attribution.computed_due_at, moment(17, 11))
        self.assertEqual(attribution.verdict, Verdict.WITHIN_TARGET)

    def test_due_at_mismatch_is_reported(self):
        attribution = self.analyze(due_at='2026-11-20T18:00:00+00:00')
        self.assertEqual(attribution.due_at_source, 'recorded')
        self.assertIn('due_at_mismatch', {issue.code for issue in attribution.issues})
        self.assertTrue(attribution.evidence_sufficient)

    def test_missing_due_basis_is_blocking(self):
        attribution = self.analyze(target_seconds=None, due_at=None)
        self.assertEqual(attribution.verdict, Verdict.EVIDENCE_INSUFFICIENT)
        self.assertIn('missing_due_basis', {issue.code for issue in attribution.issues})

    def test_cancelled_case_is_not_overdue_or_on_time(self):
        attribution = self.analyze(outcome='cancelled', closed_at='2026-11-17T15:00:00+00:00')
        self.assertEqual(attribution.verdict, Verdict.CANCELLED)
        self.assertEqual(attribution.overdue_seconds, 0)

    def test_open_case_past_due_reports_overdue(self):
        attribution = self.analyze(closed_at=None, outcome='in_progress', as_of='2026-11-19T15:00:00+00:00')
        self.assertEqual(attribution.verdict, Verdict.OVERDUE)
        # 2026-11-17 11:00 -> 17:00, then two full business days (18th, 19th)
        # up to 15:00 on the 19th: 6h + 8h + 6h.
        self.assertEqual(attribution.overdue_seconds, 20 * 3600)

    def test_case_without_batch_or_version_is_still_attributed(self):
        attribution = self.analyze(service_version=None, batch_id=None)
        self.assertIsNone(attribution.service_version)
        self.assertIsNone(attribution.batch_id)
        self.assertTrue(attribution.evidence_sufficient)

    def test_duplicate_events_rejected(self):
        with self.assertRaises(TimelinessError):
            parse_case(case(timeline=[
                {'kind': 'step_started', 'step_id': 'intake', 'occurred_at': '2026-11-16T09:00:00+00:00'},
                {'kind': 'step_started', 'step_id': 'intake', 'occurred_at': '2026-11-16T09:00:00+00:00'},
            ]), as_of=moment(20))

    def test_unknown_event_kind_rejected(self):
        with self.assertRaises(TimelinessError):
            parse_case(case(timeline=[
                {'kind': 'teleported', 'occurred_at': '2026-11-16T09:00:00+00:00'},
            ]), as_of=moment(20))

    def test_segments_are_contiguous_and_non_overlapping(self):
        attribution = self.analyze()
        segments = sorted(attribution.segments, key=lambda item: item.start)
        for left, right in zip(segments, segments[1:]):
            self.assertLessEqual(left.end, right.start)

    def test_business_seconds_total_matches_segments(self):
        attribution = self.analyze()
        self.assertEqual(
            attribution.working_seconds_total,
            sum(segment.working_seconds for segment in attribution.segments),
        )
        self.assertEqual(
            attribution.chargeable_working_seconds + attribution.excluded_working_seconds,
            attribution.working_seconds_total,
        )


class ExecutionImmutabilityTests(unittest.TestCase):
    def test_attribution_does_not_change_executability(self):
        from one_stop_cases.orchestration.engine import WorkflowEngine

        subject = {
            'id': 'case-exec',
            'steps': [{'id': 'intake', 'requires': []}, {'id': 'review', 'requires': ['intake']}],
            'completed': ['intake'],
        }
        before = WorkflowEngine().evaluate(subject, {})
        analyze_document({'cases': [case()], 'as_of': '2026-11-20T09:00:00+00:00'})
        after = WorkflowEngine().evaluate(subject, {})
        self.assertEqual(before, after)

    def test_unknown_dependency_still_blocks_after_analysis(self):
        from one_stop_cases.orchestration.engine import WorkflowEngine

        subject = {'id': 'case-bad', 'steps': [{'id': 'x', 'requires': ['missing']}], 'completed': []}
        result = WorkflowEngine().evaluate(subject, {})
        self.assertIn('blocked:x:unknown_dependency', result.reasons)


class SummaryTests(unittest.TestCase):
    def document(self):
        return {
            'as_of': '2026-11-20T16:00:00+00:00',
            'calendar': {
                'name': 'gov',
                'periods': [{'weekday': day, 'start': '09:00', 'end': '17:00'} for day in range(5)],
                'holidays': ['2026-11-18'],
            },
            'cases': [
                case(case_id='case-a', service_version='v1', batch_id='b1',
                     closed_at='2026-11-17T10:00:00+00:00'),
                case(case_id='case-b', service_version='v1', batch_id='b1', closed_at='2026-11-18T15:00:00+00:00'),
                case(case_id='case-c', service_version='v2', batch_id='b2', timeline=[]),
                case(case_id='case-d', service_version='v2', batch_id='b2', outcome='cancelled'),
            ],
        }

    def test_groups_by_version_and_batch(self):
        report = analyze_document(self.document())
        self.assertEqual(set(report.summary['by_service_version']), {'v1', 'v2'})
        self.assertEqual(set(report.summary['by_batch']), {'b1', 'b2'})
        self.assertEqual(report.summary['by_service_version']['v1']['cases'], 2)
        self.assertEqual(report.summary['by_batch']['b2']['cases'], 2)

    def test_verdict_counts(self):
        report = analyze_document(self.document())
        verdicts = report.summary['verdicts']
        self.assertEqual(verdicts['within_target'], 1)
        self.assertEqual(verdicts['overdue'], 1)
        self.assertEqual(verdicts['evidence_insufficient'], 1)
        self.assertEqual(verdicts['cancelled'], 1)
        self.assertEqual(report.summary['evidence_insufficient_cases'], 1)

    def test_bottleneck_ranking(self):
        report = analyze_document(self.document())
        bottlenecks = report.summary['bottlenecks']
        self.assertTrue(bottlenecks)
        self.assertEqual(bottlenecks[0]['step_id'], 'review')
        self.assertGreater(bottlenecks[0]['chargeable_wait_seconds'], 0)

    def test_case_lookup_and_serialization(self):
        report = analyze_document(self.document())
        payload = report.case('case-a').as_dict()
        self.assertEqual(payload['case_id'], 'case-a')
        self.assertIn('basis', payload)
        self.assertTrue(payload['segments'])
        self.assertEqual(report.as_dict(include_segments=False)['cases'][0].get('segments'), None)

    def test_evidence_insufficient_not_counted_as_bottleneck(self):
        report = analyze_document(self.document())
        rows = {row['step_id']: row for row in report.summary['bottlenecks']}
        self.assertNotIn('<case>', rows)

    def test_source_document_is_not_mutated(self):
        document = self.document()
        snapshot = repr(document)
        analyze_document(document)
        self.assertEqual(repr(document), snapshot)



class AcceptanceRecordTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from one_stop_cases.storage import Database
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'db.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def process(self, **overrides):
        from one_stop_cases.service import ApplicationService
        payload = {
            'id': 'case-rec',
            'owner_id': 'person-1',
            'steps': [{'id': 'intake', 'requires': []}, {'id': 'review', 'requires': ['intake']}],
            'completed': ['intake'],
            'demand': 1,
            'opened_at': '2026-11-16T09:00:00+00:00',
            'closed_at': '2026-11-17T15:00:00+00:00',
            'target_seconds': 28800,
            'service_version': 'v1',
            'batch_id': 'b1',
            'outcome': 'completed',
            'timeline': [
                {'kind': 'step_started', 'step_id': 'intake', 'occurred_at': '2026-11-16T09:00:00+00:00'},
                {'kind': 'step_completed', 'step_id': 'intake', 'occurred_at': '2026-11-16T10:00:00+00:00'},
                {'kind': 'step_started', 'step_id': 'review', 'occurred_at': '2026-11-16T13:00:00+00:00'},
                {'kind': 'step_completed', 'step_id': 'review', 'occurred_at': '2026-11-17T15:00:00+00:00'},
            ],
        }
        payload.update(overrides)
        return ApplicationService(self.db).process(payload, {}, 'request-1')

    def test_executability_result_is_unchanged(self):
        from one_stop_cases.orchestration.engine import WorkflowEngine

        expected = WorkflowEngine().evaluate(
            {'id': 'case-rec', 'steps': [{'id': 'intake', 'requires': []}, {'id': 'review', 'requires': ['intake']}],
             'completed': ['intake']}, {})
        response = self.process()
        self.assertEqual(response['accepted'], expected.accepted)
        self.assertEqual(response['value'], str(expected.value))
        self.assertEqual(response['facts'], dict(expected.facts))

    def test_timeline_is_kept_on_the_case_record(self):
        self.process()
        record = self.db.get_record('case', 'case-rec')
        detail = record['attributes']['timeliness']
        self.assertEqual(detail['service_version'], 'v1')
        self.assertEqual(detail['batch_id'], 'b1')
        self.assertEqual(len(detail['timeline']), 4)

    def test_reporter_attributes_record(self):
        from one_stop_cases.reporting import Reporter

        self.process()
        summary = Reporter(self.db).timeliness(as_of='2026-11-20T09:00:00+00:00')['summary']
        self.assertEqual(summary['verdicts']['overdue'], 1)
        self.assertEqual(summary['by_service_version']['v1']['cases'], 1)
        self.assertEqual(summary['by_batch']['b1']['cases'], 1)
        self.assertEqual(summary['by_category']['blocked_predecessor'], 3 * 3600)

    def test_reporter_exposes_case_basis(self):
        from one_stop_cases.reporting import Reporter

        self.process()
        payload = Reporter(self.db).timeliness_case('case-rec', as_of='2026-11-20T09:00:00+00:00')
        self.assertEqual(payload['case_id'], 'case-rec')
        self.assertTrue(payload['basis'])
        self.assertTrue(payload['segments'])
        self.assertEqual(payload['due_at'], '2026-11-16T17:00:00+00:00')

    def test_record_without_timeline_is_evidence_insufficient(self):
        from one_stop_cases.reporting import Reporter
        from one_stop_cases.storage import Database

        other = Database(self.path())
        with other.transaction() as connection:
            other.put_record(connection, 'case', {
                'case_id': 'case-bare', 'owner_id': 'person-2', 'category': 'workflow',
                'status': 'completed', 'effective_from': '2026-11-16', 'effective_to': '2026-11-16',
                'quantity': 0, 'amount': '0', 'attributes': {},
            })
        summary = Reporter(other).timeliness(as_of='2026-11-20T09:00:00+00:00')['summary']
        self.assertEqual(summary['verdicts']['evidence_insufficient'], 1)
        self.assertEqual(summary['verdicts']['within_target'], 0)
        self.assertEqual(summary['verdicts']['overdue'], 0)

    def path(self):
        return str(__import__('pathlib').Path(self.temp.name) / 'second.sqlite3')

    def test_missing_case_raises_instead_of_reading_as_compliant(self):
        from one_stop_cases.reporting import Reporter
        from one_stop_cases.storage import NotFoundError

        with self.assertRaises(NotFoundError):
            Reporter(self.db).timeliness_case('missing-case')


FIXTURE = str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'fixtures' / 'timeliness.json')


class CliTests(unittest.TestCase):
    def run_cli(self, arguments):
        import contextlib
        import io
        from one_stop_cases.cli import main

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = main(arguments)
        return code

    def test_timeliness_command_writes_case_basis(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'out.json'
            code = self.run_cli(['timeliness', '--fixture', FIXTURE,
                                 '--case', 'case-minute', '--output', str(output)])
            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(payload['case_id'], 'case-minute')
            self.assertEqual(payload['verdict'], 'overdue')
            self.assertTrue(payload['segments'])

    def test_as_of_argument_overrides_the_document(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'override.json'
            code = self.run_cli(['timeliness', '--fixture', FIXTURE,
                                 '--as-of', '2026-11-20T16:00:00+00:00', '--output', str(output)])
            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(payload['as_of'], '2026-11-20T16:00:00+00:00')

    def test_timeliness_command_summary_by_version_and_batch(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'summary.json'
            code = self.run_cli(['timeliness', '--fixture', FIXTURE,
                                 '--output', str(output)])
            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding='utf-8'))
            self.assertIn('v1', payload['summary']['by_service_version'])
            self.assertIn('batch-A', payload['summary']['by_batch'])
            self.assertTrue(payload['summary']['bottlenecks'])
            self.assertNotIn('segments', payload['cases'][0])


class DependencyLinkTests(unittest.TestCase):
    def test_wait_names_the_actual_predecessor(self):
        attribution = attribute_case(
            parse_case({
                'case_id': 'case-dep',
                'opened_at': '2026-11-16T09:00:00+00:00',
                'closed_at': '2026-11-16T15:00:00+00:00',
                'target_seconds': 28800,
                'outcome': 'completed',
                'steps': [
                    {'id': 'a', 'requires': []},
                    {'id': 'b', 'requires': ['a']},
                    {'id': 'c', 'requires': ['b']},
                ],
                'timeline': [
                    {'kind': 'step_started', 'step_id': 'a', 'occurred_at': '2026-11-16T09:00:00+00:00'},
                    {'kind': 'step_completed', 'step_id': 'a', 'occurred_at': '2026-11-16T10:00:00+00:00'},
                    {'kind': 'step_started', 'step_id': 'b', 'occurred_at': '2026-11-16T12:00:00+00:00'},
                    {'kind': 'step_completed', 'step_id': 'b', 'occurred_at': '2026-11-16T14:00:00+00:00'},
                    {'kind': 'step_started', 'step_id': 'c', 'occurred_at': '2026-11-16T14:00:00+00:00'},
                    {'kind': 'step_completed', 'step_id': 'c', 'occurred_at': '2026-11-16T15:00:00+00:00'},
                ],
            }, as_of=moment(17, 9)),
            build_calendar(),
        )
        waits = [s for s in attribution.segments if s.category is AttributionCategory.BLOCKED_PREDECESSOR]
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0].step_id, 'b')
        self.assertEqual(waits[0].related_step, 'a')
        self.assertEqual(waits[0].working_seconds, 2 * 3600)

    def test_gap_after_failure_without_recovery_is_compensation(self):
        attribution = attribute_case(
            parse_case({
                'case_id': 'case-fail',
                'opened_at': '2026-11-16T09:00:00+00:00',
                'closed_at': '2026-11-16T13:00:00+00:00',
                'target_seconds': 28800,
                'outcome': 'completed',
                'steps': [{'id': 'a', 'requires': []}],
                'timeline': [
                    {'kind': 'step_started', 'step_id': 'a', 'occurred_at': '2026-11-16T09:00:00+00:00'},
                    {'kind': 'step_failed', 'step_id': 'a', 'occurred_at': '2026-11-16T10:00:00+00:00'},
                    {'kind': 'rework_started', 'step_id': 'a', 'occurred_at': '2026-11-16T12:00:00+00:00'},
                    {'kind': 'rework_completed', 'step_id': 'a', 'occurred_at': '2026-11-16T13:00:00+00:00'},
                    {'kind': 'step_completed', 'step_id': 'a', 'occurred_at': '2026-11-16T13:00:00+00:00'},
                ],
            }, as_of=moment(17, 9)),
            build_calendar(),
        )
        self.assertEqual(attribution.by_category['compensation'], 2 * 3600)
        self.assertEqual(attribution.by_category['rework'], 1 * 3600)
        self.assertTrue(attribution.evidence_sufficient)


class AggregationDetailTests(unittest.TestCase):
    def test_by_step_counts_each_case_once(self):
        from one_stop_cases.timeliness import parse_case, attribute_case, summarize

        ledger = parse_case({
            'case_id': 'case-multi',
            'opened_at': '2026-11-16T09:00:00+00:00',
            'closed_at': '2026-11-16T14:00:00+00:00',
            'target_seconds': 28800,
            'outcome': 'completed',
            'steps': [{'id': 'a', 'requires': []}],
            # Two separate handling spells for the same step.
            'timeline': [
                {'kind': 'step_started', 'step_id': 'a', 'occurred_at': '2026-11-16T09:00:00+00:00'},
                {'kind': 'pause_started', 'step_id': 'a', 'occurred_at': '2026-11-16T10:00:00+00:00'},
                {'kind': 'pause_ended', 'step_id': 'a', 'occurred_at': '2026-11-16T11:00:00+00:00'},
                {'kind': 'step_completed', 'step_id': 'a', 'occurred_at': '2026-11-16T14:00:00+00:00'},
            ],
        }, as_of=moment(17, 9))
        summary = summarize([attribute_case(ledger, build_calendar())])
        attribution = attribute_case(ledger, build_calendar())
        self.assertEqual(summary['by_step']['a']['cases'], 1)
        # by_step reports all working time recorded at the step: 4h handling
        # plus the 1h pause that interrupted it.  The category split, not the
        # step total, is what separates chargeable time from exclusions.
        self.assertEqual(summary['by_step']['a']['working_seconds'], 5 * 3600)
        self.assertEqual(summary['by_step']['a']['working_seconds'],
                         attribution.by_step['a']['working_seconds'])
        self.assertEqual(attribution.by_category['pause'], 1 * 3600)

    def test_document_can_pin_the_exclusion_policy(self):
        document = {
            'as_of': '2026-11-20T09:00:00+00:00',
            'excluded_categories': ['pause'],
            'cases': [case(timeline=[
                {'kind': 'step_started', 'step_id': 'intake', 'occurred_at': '2026-11-16T09:00:00+00:00'},
                {'kind': 'correction_requested', 'step_id': 'intake', 'occurred_at': '2026-11-16T10:00:00+00:00'},
                {'kind': 'correction_received', 'step_id': 'intake', 'occurred_at': '2026-11-16T12:00:00+00:00'},
                {'kind': 'step_completed', 'step_id': 'intake', 'occurred_at': '2026-11-16T15:00:00+00:00'},
            ])],
        }
        report = analyze_document(document)
        attribution = report.cases[0]
        self.assertIn('pause', attribution.excluded_categories)
        self.assertIn('material_correction', attribution.excluded_categories)
        # Material correction stays excluded because the documented policy is
        # unioned with the conservative defaults rather than replacing them.
        self.assertEqual(attribution.excluded_working_seconds, 2 * 3600)

    def test_cancelled_cases_are_excluded_from_bottleneck(self):
        from one_stop_cases.timeliness import parse_case, attribute_case, summarize

        ledger = parse_case(case(outcome='cancelled'), as_of=moment(20))
        summary = summarize([attribute_case(ledger, build_calendar())])
        self.assertEqual(summary['bottlenecks'], [])


if __name__ == '__main__':
    unittest.main()
