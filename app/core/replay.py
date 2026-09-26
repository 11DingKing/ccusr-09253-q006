"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable, Sequence

from .capacity import (
    ActivitySchedule,
    EXCLUDED,
    HELD,
    RosterDecision,
    SEATED,
    VenueCapacity,
    build_attendance_clips,
    scan_capacity,
)
from .clock import (
    academic_day,
    elapsed_seconds,
    merge_intervals,
    split_by_academic_day,
    to_utc,
    union_seconds,
)


class EventType(StrEnum):
    CHECKIN = "checkin"
    MENTOR_CONFIRM = "mentor_confirm"
    LEAVE_CORRECTION = "leave_correction"
    ROSTER_CONFIRM = "roster_confirm"


class CheckinStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PENDING = "PENDING"


INTERNSHIP_TYPE = "internship"


@dataclass(frozen=True)
class Event:
    """封装领域状态与业务约束。"""

    event_id: str
    plan_version: str
    event_type: EventType
    student_id: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass
class CheckinRecord:
    event_id: str
    student_id: str
    activity_id: str
    activity_type: str
    start_utc: datetime
    end_utc: datetime
    status: CheckinStatus
    seated_intervals: list[tuple[datetime, datetime]] = field(default_factory=list)
    held_intervals: list[tuple[datetime, datetime]] = field(default_factory=list)
    excluded_intervals: list[tuple[datetime, datetime]] = field(default_factory=list)
    capacity_evaluated: bool = False

    @property
    def seconds(self) -> int:
        return elapsed_seconds(self.start_utc, self.end_utc)

    @property
    def counts(self) -> bool:
        if self.status != CheckinStatus.CONFIRMED:
            return False
        if not self.capacity_evaluated:
            return True
        return bool(self.seated_intervals)

    @property
    def seated_seconds(self) -> int:
        return union_seconds(self.seated_intervals)

    @property
    def held_seconds(self) -> int:
        return union_seconds(self.held_intervals)

    @property
    def excluded_seconds(self) -> int:
        return union_seconds(self.excluded_intervals)


@dataclass
class Adjustment:
    event_id: str
    student_id: str
    seconds: int
    reason: str


@dataclass
class DayTotal:
    academic_day: str
    seconds: int


@dataclass
class CapacityHold:
    checkin_event_id: str
    activity_id: str
    schedule_id: str
    venue_id: str
    start_utc: datetime
    end_utc: datetime
    seconds: int
    state: str
    decision_event_id: str | None = None


@dataclass
class StudentProgress:
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DayTotal] = field(default_factory=list)
    checkins: list[CheckinRecord] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)
    held_seconds: int = 0
    excluded_seconds: int = 0
    capacity_holds: list[CapacityHold] = field(default_factory=list)


@dataclass
class OverrunWindow:
    venue_id: str
    start_utc: datetime
    end_utc: datetime
    capacity: int
    headcount: int
    schedule_ids: tuple[str, ...]
    held_student_ids: tuple[str, ...]
    excluded_student_ids: tuple[str, ...]


@dataclass
class ReplayState:
    plan_version: str
    timezone: str
    required_seconds: int
    students: dict[str, StudentProgress]
    overruns: list[OverrunWindow] = field(default_factory=list)


def _parse_checkin(
    event: Event, tz_name: str
) -> CheckinRecord:
    start = to_utc(datetime.fromisoformat(event.payload["check_in_at"]))
    end = to_utc(datetime.fromisoformat(event.payload["check_out_at"]))
    activity_type = event.payload.get("activity_type", "regular")
    requires_confirmation = activity_type == INTERNSHIP_TYPE
    status = (
        CheckinStatus.PENDING if requires_confirmation else CheckinStatus.CONFIRMED
    )
    return CheckinRecord(
        event_id=event.event_id,
        student_id=event.student_id,
        activity_id=event.payload.get("activity_id", ""),
        activity_type=activity_type,
        start_utc=start,
        end_utc=end,
        status=status,
    )


def _subtract_intervals(
    base: tuple[datetime, datetime],
    covered: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """从 base 中扣除 covered 区间，返回剩余片段。"""
    start, end = to_utc(base[0]), to_utc(base[1])
    pieces: list[tuple[datetime, datetime]] = []
    cursor = start
    for cov_start, cov_end in merge_intervals(covered):
        cov_start = max(to_utc(cov_start), start)
        cov_end = min(to_utc(cov_end), end)
        if cov_start > cursor:
            pieces.append((cursor, min(cov_start, end)))
        if cov_end > cursor:
            cursor = cov_end
    if cursor < end:
        pieces.append((cursor, end))
    return pieces


def replay(
    events: Iterable[Event],
    *,
    plan_version: str,
    timezone_name: str,
    required_seconds: int,
    up_to_event_id: str | None = None,
    schedules: Sequence[ActivitySchedule] | None = None,
    capacities: dict[str, VenueCapacity] | None = None,
) -> ReplayState:
    """执行确定性的业务处理。"""
    sorted_events = sorted(
        (e for e in events if e.plan_version == plan_version),
        key=lambda e: e.event_id,
    )
    if up_to_event_id is not None:
        sorted_events = [e for e in sorted_events if e.event_id <= up_to_event_id]

    checkins_by_student: dict[str, list[CheckinRecord]] = {}
    checkin_index: dict[str, CheckinRecord] = {}
    adjustments_by_student: dict[str, list[Adjustment]] = {}
    decisions: list[RosterDecision] = []

    for event in sorted_events:
        if event.event_type == EventType.CHECKIN:
            record = _parse_checkin(event, timezone_name)
            checkins_by_student.setdefault(event.student_id, []).append(record)
            checkin_index[event.event_id] = record
        elif event.event_type == EventType.MENTOR_CONFIRM:
            target_id = event.payload.get("checkin_event_id")
            target = checkin_index.get(target_id)
            if target is not None and target.student_id == event.student_id:
                target.status = CheckinStatus.CONFIRMED
        elif event.event_type == EventType.LEAVE_CORRECTION:
            seconds = int(event.payload.get("adjustment_seconds", 0))
            adjustments_by_student.setdefault(event.student_id, []).append(
                Adjustment(
                    event_id=event.event_id,
                    student_id=event.student_id,
                    seconds=seconds,
                    reason=str(event.payload.get("reason", "")),
                )
            )
        elif event.event_type == EventType.ROSTER_CONFIRM:
            decisions.append(
                RosterDecision(
                    event_id=event.event_id,
                    schedule_id=str(event.payload["schedule_id"]),
                    released=tuple(event.payload.get("released", [])),
                    excluded=tuple(event.payload.get("excluded", [])),
                )
            )

    # 容量扫描：只有导师状态已确认的签到才实际占用场地。
    overruns: list[OverrunWindow] = []
    capacity_context = bool(schedules) and capacities is not None
    if capacity_context:
        confirmed_records = [
            record
            for records in checkins_by_student.values()
            for record in records
            if record.status == CheckinStatus.CONFIRMED
        ]
        clips = build_attendance_clips(confirmed_records, list(schedules or []))
        scan = scan_capacity(clips, capacities or {}, decisions)

        marks_by_checkin: dict[str, list] = {}
        for mark in scan.marks:
            marks_by_checkin.setdefault(mark.checkin_event_id, []).append(mark)

        for record in confirmed_records:
            marks = marks_by_checkin.get(record.event_id, [])
            covered = merge_intervals([(m.start_utc, m.end_utc) for m in marks])
            # 没有任何排期覆盖的时段视为不受容量限制，正常计入学时。
            uncovered = _subtract_intervals(
                (record.start_utc, record.end_utc), covered
            )
            seated = uncovered + [
                (m.start_utc, m.end_utc) for m in marks if m.state == SEATED
            ]
            held = [
                (m.start_utc, m.end_utc) for m in marks if m.state == HELD
            ]
            excluded = [
                (m.start_utc, m.end_utc) for m in marks if m.state == EXCLUDED
            ]
            record.seated_intervals = merge_intervals(seated)
            record.held_intervals = merge_intervals(held)
            record.excluded_intervals = merge_intervals(excluded)
            record.capacity_evaluated = True

        overruns = [
            OverrunWindow(
                venue_id=segment.venue_id,
                start_utc=segment.start_utc,
                end_utc=segment.end_utc,
                capacity=segment.capacity,
                headcount=segment.headcount,
                schedule_ids=segment.schedule_ids,
                held_student_ids=segment.held_student_ids,
                excluded_student_ids=segment.excluded_student_ids,
            )
            for segment in scan.excess_segments
        ]

    all_students = set(checkins_by_student) | set(adjustments_by_student)
    students: dict[str, StudentProgress] = {}
    for student_id in all_students:
        records = checkins_by_student.get(student_id, [])
        adjustments = adjustments_by_student.get(student_id, [])

        if capacity_context:
            confirmed_intervals = [
                interval
                for record in records
                if record.status == CheckinStatus.CONFIRMED
                for interval in record.seated_intervals
            ]
            held_intervals = [
                interval for record in records for interval in record.held_intervals
            ]
            excluded_intervals = [
                interval
                for record in records
                for interval in record.excluded_intervals
            ]
        else:
            confirmed_intervals = [
                (r.start_utc, r.end_utc)
                for r in records
                if r.status == CheckinStatus.CONFIRMED
            ]
            held_intervals = []
            excluded_intervals = []
        pending_intervals = [
            (r.start_utc, r.end_utc)
            for r in records
            if r.status == CheckinStatus.PENDING
        ]

        confirmed_seconds = union_seconds(confirmed_intervals)
        pending_seconds = union_seconds(pending_intervals)
        held_seconds = union_seconds(held_intervals)
        excluded_seconds = union_seconds(excluded_intervals)
        adjustment_seconds = sum(a.seconds for a in adjustments)
        total_seconds = confirmed_seconds + adjustment_seconds
        if total_seconds < 0:
            total_seconds = 0

        day_totals: dict[str, int] = {}
        for start, end in merge_intervals(confirmed_intervals):
            for day, seg_start, seg_end in split_by_academic_day(
                start, end, timezone_name
            ):
                key = day.isoformat()
                day_totals[key] = day_totals.get(key, 0) + elapsed_seconds(
                    seg_start, seg_end
                )
        daily = [
            DayTotal(academic_day=day, seconds=secs)
            for day, secs in sorted(day_totals.items())
        ]

        holds: list[CapacityHold] = []
        if capacity_context:
            for record in records:
                for state_name, state_intervals in (
                    (HELD, record.held_intervals),
                    (EXCLUDED, record.excluded_intervals),
                ):
                    marks = marks_by_checkin.get(record.event_id, [])
                    for seg_start, seg_end in state_intervals:
                        match = next(
                            (
                                m
                                for m in marks
                                if m.state == state_name
                                and m.start_utc <= seg_start < m.end_utc
                            ),
                            None,
                        )
                        holds.append(
                            CapacityHold(
                                checkin_event_id=record.event_id,
                                activity_id=record.activity_id,
                                schedule_id=match.schedule_id if match else "",
                                venue_id=match.venue_id if match else "",
                                start_utc=seg_start,
                                end_utc=seg_end,
                                seconds=elapsed_seconds(seg_start, seg_end),
                                state=state_name,
                                decision_event_id=(
                                    match.decision_event_id if match else None
                                ),
                            )
                        )

        students[student_id] = StudentProgress(
            student_id=student_id,
            confirmed_seconds=confirmed_seconds,
            pending_seconds=pending_seconds,
            adjustment_seconds=adjustment_seconds,
            total_seconds=total_seconds,
            lesson_units=total_seconds // (45 * 60),
            pending_lesson_units=pending_seconds // (45 * 60),
            meets_requirement=total_seconds >= required_seconds,
            daily=daily,
            checkins=sorted(records, key=lambda r: r.start_utc),
            adjustments=sorted(adjustments, key=lambda a: a.event_id),
            held_seconds=held_seconds,
            excluded_seconds=excluded_seconds,
            capacity_holds=holds,
        )

    return ReplayState(
        plan_version=plan_version,
        timezone=timezone_name,
        required_seconds=required_seconds,
        students=students,
        overruns=overruns,
    )


def explain_checkin(record: CheckinRecord, tz_name: str) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    segments = split_by_academic_day(record.start_utc, record.end_utc, tz_name)

    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    explanation = {
        "event_id": record.event_id,
        "activity_id": record.activity_id,
        "activity_type": record.activity_type,
        "status": record.status.value,
        "counts": record.counts,
        "check_in_at_utc": _iso(record.start_utc),
        "check_out_at_utc": _iso(record.end_utc),
        "raw_seconds": record.seconds,
        "academic_days": [
            {
                "day": day.isoformat(),
                "start_utc": _iso(seg_start),
                "end_utc": _iso(seg_end),
                "seconds": elapsed_seconds(seg_start, seg_end),
            }
            for day, seg_start, seg_end in segments
        ],
    }
    if record.seated_intervals or record.held_intervals or record.excluded_intervals:
        explanation["capacity"] = {
            "seated_seconds": record.seated_seconds,
            "held_seconds": record.held_seconds,
            "excluded_seconds": record.excluded_seconds,
            "seated": [
                {"start_utc": _iso(s), "end_utc": _iso(e)}
                for s, e in record.seated_intervals
            ],
            "held": [
                {"start_utc": _iso(s), "end_utc": _iso(e)}
                for s, e in record.held_intervals
            ],
            "excluded": [
                {"start_utc": _iso(s), "end_utc": _iso(e)}
                for s, e in record.excluded_intervals
            ],
        }
    return explanation
