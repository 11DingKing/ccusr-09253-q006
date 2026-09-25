"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable

from .capacity import (
    ActivityAllocation,
    ActivitySchedule,
    VenueCapacity,
    allocate_activity,
    allocation_to_dict,
    intersect_intervals,
    subtract_interval,
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
    HELD = "HELD"


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
    held_intervals: list[tuple[datetime, datetime]] = field(default_factory=list)

    @property
    def seconds(self) -> int:
        return elapsed_seconds(self.start_utc, self.end_utc)

    @property
    def counts(self) -> bool:
        return self.status == CheckinStatus.CONFIRMED

    @property
    def released_intervals(self) -> list[tuple[datetime, datetime]]:
        """剔除全部超额区间后的可计学时区间。"""
        remaining: list[tuple[datetime, datetime]] = [
            (self.start_utc, self.end_utc)
        ]
        for held_start, held_end in merge_intervals(self.held_intervals):
            next_remaining: list[tuple[datetime, datetime]] = []
            for start, end in remaining:
                next_remaining.extend(
                    subtract_interval(start, end, held_start, held_end)
                )
            remaining = next_remaining
        return merge_intervals(remaining)

    @property
    def held_seconds(self) -> int:
        return union_seconds(self.held_intervals)


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
class StudentProgress:
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    held_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    held_lesson_units: int
    meets_requirement: bool
    daily: list[DayTotal] = field(default_factory=list)
    checkins: list[CheckinRecord] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)


@dataclass
class ReplayState:
    plan_version: str
    timezone: str
    required_seconds: int
    students: dict[str, StudentProgress]
    schedule_version: str | None = None
    capacity_refs: dict[str, str] = field(default_factory=dict)
    overages: list[dict[str, Any]] = field(default_factory=list)
    allocations: dict[str, ActivityAllocation] = field(default_factory=dict)


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


def replay(
    events: Iterable[Event],
    *,
    plan_version: str,
    timezone_name: str,
    required_seconds: int,
    up_to_event_id: str | None = None,
    capacities: Iterable[VenueCapacity] | None = None,
    schedules: Iterable[ActivitySchedule] | None = None,
) -> ReplayState:
    """执行确定性的业务处理。

    当提供 ``capacities`` 与 ``schedules`` 时，重放会对排期场地做容量
    核算：超出场地承载量的时间片被标记为 HELD，相关学时暂缓计入，直到
    ``roster_confirm`` 事件确认实际名单后按确定顺序释放。
    """
    sorted_events = sorted(
        (e for e in events if e.plan_version == plan_version),
        key=lambda e: e.event_id,
    )
    if up_to_event_id is not None:
        sorted_events = [e for e in sorted_events if e.event_id <= up_to_event_id]

    checkins_by_student: dict[str, list[CheckinRecord]] = {}
    checkin_index: dict[str, CheckinRecord] = {}
    adjustments_by_student: dict[str, list[Adjustment]] = {}
    roster_by_activity: dict[str, set[str]] = {}
    schedule_versions: dict[str, str] = {}

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
            activity_id = str(event.payload.get("activity_id", ""))
            student_ids = {str(sid) for sid in event.payload.get("student_ids", [])}
            roster_by_activity.setdefault(activity_id, set()).update(student_ids)

    allocations: dict[str, ActivityAllocation] = {}
    overages: list[dict[str, Any]] = []
    capacity_refs: dict[str, str] = {}
    if capacities is not None and schedules is not None:
        schedules_by_activity: dict[str, list[ActivitySchedule]] = {}
        for schedule in schedules:
            schedules_by_activity.setdefault(schedule.activity_id, []).append(schedule)
        capacities_by_venue: dict[str, list[VenueCapacity]] = {}
        for version in capacities:
            capacities_by_venue.setdefault(version.venue_id, []).append(version)

        records_by_activity: dict[str, list[CheckinRecord]] = {}
        for records in checkins_by_student.values():
            for record in records:
                records_by_activity.setdefault(record.activity_id, []).append(record)

        for activity_id, activity_schedules in schedules_by_activity.items():
            # 每个活动在同一排期版本中只有一场；如出现多版本，取版本号
            # 最大者（调用方通常已按生效版本过滤）。
            schedule = max(
                activity_schedules, key=lambda s: s.schedule_version
            )
            schedule_versions[activity_id] = schedule.schedule_version
            allocation = allocate_activity(
                schedule,
                records_by_activity.get(activity_id, []),
                capacities_by_venue.get(schedule.venue_id, []),
                roster_by_activity.get(activity_id, set()),
            )
            allocations[activity_id] = allocation
            if schedule.venue_id in capacities_by_venue:
                active = max(
                    capacities_by_venue[schedule.venue_id],
                    key=lambda c: (c.effective_from, c.capacity_version),
                )
                capacity_refs[schedule.venue_id] = active.capacity_version
            if allocation.has_overage:
                overages.append(allocation_to_dict(allocation))
            for event_id, attendance in allocation.attendance_by_event.items():
                record = checkin_index[event_id]
                held_parts = intersect_intervals(
                    allocation.held_by_student.get(attendance.student_id, []),
                    attendance.start,
                    attendance.end,
                )
                if held_parts:
                    record.held_intervals.extend(held_parts)
                    if record.counts and not record.released_intervals:
                        record.status = CheckinStatus.HELD

    all_students = set(checkins_by_student) | set(adjustments_by_student)
    students: dict[str, StudentProgress] = {}
    for student_id in all_students:
        records = checkins_by_student.get(student_id, [])
        adjustments = adjustments_by_student.get(student_id, [])

        confirmed_intervals = [
            interval
            for r in records
            if r.counts
            for interval in r.released_intervals
        ]
        pending_intervals = [
            (r.start_utc, r.end_utc)
            for r in records
            if r.status == CheckinStatus.PENDING
        ]
        held_intervals = [
            interval
            for r in records
            for interval in merge_intervals(r.held_intervals)
        ]

        confirmed_seconds = union_seconds(confirmed_intervals)
        pending_seconds = union_seconds(pending_intervals)
        held_seconds = union_seconds(held_intervals)
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

        students[student_id] = StudentProgress(
            student_id=student_id,
            confirmed_seconds=confirmed_seconds,
            pending_seconds=pending_seconds,
            held_seconds=held_seconds,
            adjustment_seconds=adjustment_seconds,
            total_seconds=total_seconds,
            lesson_units=total_seconds // (45 * 60),
            pending_lesson_units=pending_seconds // (45 * 60),
            held_lesson_units=held_seconds // (45 * 60),
            meets_requirement=total_seconds >= required_seconds,
            daily=daily,
            checkins=sorted(records, key=lambda r: r.start_utc),
            adjustments=sorted(adjustments, key=lambda a: a.event_id),
        )

    active_schedule_version: str | None = None
    if schedule_versions:
        active_schedule_version = max(schedule_versions.values())

    return ReplayState(
        plan_version=plan_version,
        timezone=timezone_name,
        required_seconds=required_seconds,
        students=students,
        schedule_version=active_schedule_version,
        capacity_refs=capacity_refs,
        overages=sorted(overages, key=lambda item: item["activity_id"]),
        allocations=allocations,
    )


def explain_checkin(record: CheckinRecord, tz_name: str) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    segments = split_by_academic_day(record.start_utc, record.end_utc, tz_name)
    released_segments: list[dict[str, Any]] = []
    for start, end in record.released_intervals:
        for day, seg_start, seg_end in split_by_academic_day(start, end, tz_name):
            released_segments.append(
                {
                    "day": day.isoformat(),
                    "start_utc": seg_start.isoformat().replace("+00:00", "Z"),
                    "end_utc": seg_end.isoformat().replace("+00:00", "Z"),
                    "seconds": elapsed_seconds(seg_start, seg_end),
                }
            )
    return {
        "event_id": record.event_id,
        "activity_id": record.activity_id,
        "activity_type": record.activity_type,
        "status": record.status.value,
        "counts": record.counts,
        "check_in_at_utc": record.start_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "check_out_at_utc": record.end_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "raw_seconds": record.seconds,
        "held_seconds": record.held_seconds,
        "released_seconds": union_seconds(record.released_intervals),
        "academic_days": [
            {
                "day": day.isoformat(),
                "start_utc": seg_start.isoformat().replace("+00:00", "Z"),
                "end_utc": seg_end.isoformat().replace("+00:00", "Z"),
                "seconds": elapsed_seconds(seg_start, seg_end),
            }
            for day, seg_start, seg_end in segments
        ],
        "released_academic_days": released_segments,
    }
