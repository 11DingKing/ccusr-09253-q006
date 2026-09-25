"""场地容量核算的纯函数核心测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.capacity import (
    ActivitySchedule,
    VenueCapacity,
    allocate_activity,
    schedule_impact,
)
from app.core.replay import (
    CheckinRecord,
    CheckinStatus,
    Event,
    EventType,
    replay,
)

UTC = timezone.utc


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _record(eid, sid, start, end, activity="A1"):
    return CheckinRecord(
        event_id=eid,
        student_id=sid,
        activity_id=activity,
        activity_type="regular",
        start_utc=_dt(start),
        end_utc=_dt(end),
        status=CheckinStatus.CONFIRMED,
    )


def _schedule(
    activity="A1",
    venue="V1",
    start="2024-03-15T14:00:00+00:00",
    end="2024-03-15T18:00:00+00:00",
    version="SV1",
):
    return ActivitySchedule(
        activity_id=activity,
        schedule_version=version,
        venue_id=venue,
        start=_dt(start),
        end=_dt(end),
    )


def _capacity(venue="V1", version="C1", cap=2, effective="2000-01-01T00:00:00+00:00"):
    return VenueCapacity(
        venue_id=venue,
        capacity_version=version,
        capacity=cap,
        effective_from=_dt(effective),
    )


def test_concurrent_checkins_deterministic_hold_order():
    # 三人同一时刻签到，容量为 2：按 (签到时间, 事件 ID) 确定末位者。
    records = [
        _record("E-01", "S1", "2024-03-15T14:00:00+00:00", "2024-03-15T18:00:00+00:00"),
        _record("E-02", "S2", "2024-03-15T14:00:00+00:00", "2024-03-15T18:00:00+00:00"),
        _record("E-03", "S3", "2024-03-15T14:00:00+00:00", "2024-03-15T18:00:00+00:00"),
    ]
    alloc = allocate_activity(_schedule(), records, [_capacity(cap=2)])
    assert alloc.peak_occupancy == 3
    assert alloc.held_student_ids == ["S3"]
    assert alloc.held_event_ids == ["E-03"]

    # 管理员确认 S3 实际在场后，S3 优先占座，末位顺延为 S2。
    confirmed = allocate_activity(
        _schedule(), records, [_capacity(cap=2)], {"S3"}
    )
    assert confirmed.held_student_ids == ["S2"]


def test_earlier_checkin_wins_over_later_one():
    records = [
        _record("E-09", "S9", "2024-03-15T14:05:00+00:00", "2024-03-15T18:00:00+00:00"),
        _record("E-01", "S1", "2024-03-15T14:00:00+00:00", "2024-03-15T18:00:00+00:00"),
    ]
    alloc = allocate_activity(_schedule(), records, [_capacity(cap=1)])
    assert alloc.held_student_ids == ["S9"]


def test_capacity_version_switch_within_window_holds_only_first_half():
    # 容量在活动窗口中途（16:00Z）由 2 升到 3，超额只发生在前半段。
    schedule = _schedule(
        start="2024-03-15T14:00:00+00:00", end="2024-03-15T18:00:00+00:00"
    )
    records = [
        _record("E-01", "S1", "2024-03-15T14:00:00+00:00", "2024-03-15T18:00:00+00:00"),
        _record("E-02", "S2", "2024-03-15T14:00:00+00:00", "2024-03-15T18:00:00+00:00"),
        _record("E-03", "S3", "2024-03-15T14:00:00+00:00", "2024-03-15T18:00:00+00:00"),
    ]
    capacities = [
        _capacity(version="C1", cap=2, effective="2000-01-01T00:00:00+00:00"),
        _capacity(version="C2", cap=3, effective="2024-03-15T16:00:00+00:00"),
    ]
    alloc = allocate_activity(schedule, records, capacities)
    overage = alloc.intervals and [s for s in alloc.intervals if s.held]
    assert len(overage) == 1
    assert overage[0].start == _dt("2024-03-15T14:00:00+00:00")
    assert overage[0].end == _dt("2024-03-15T16:00:00+00:00")
    assert alloc.held_by_student["S3"][0] == (
        _dt("2024-03-15T14:00:00+00:00"),
        _dt("2024-03-15T16:00:00+00:00"),
    )


def test_replay_marks_overage_and_holds_hours_across_midnight():
    # 北京时间 22:00 到次日 02:00；前两小时容量 2，后两小时容量 3。
    def checkin(eid, sid):
        return Event(
            event_id=eid,
            plan_version="P1",
            event_type=EventType.CHECKIN,
            student_id=sid,
            payload={
                "activity_id": "A1",
                "activity_type": "regular",
                "check_in_at": "2024-03-15T22:00:00+08:00",
                "check_out_at": "2024-03-16T02:00:00+08:00",
            },
            created_at=datetime.now(UTC),
        )

    events = [checkin("E-01", "S1"), checkin("E-02", "S2"), checkin("E-03", "S3")]
    schedules = [
        _schedule(
            start="2024-03-15T14:00:00+00:00",
            end="2024-03-15T18:00:00+00:00",
        )
    ]
    capacities = [
        _capacity(version="C1", cap=2, effective="2000-01-01T00:00:00+00:00"),
        _capacity(version="C2", cap=3, effective="2024-03-15T16:00:00+00:00"),
    ]
    state = replay(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        capacities=capacities,
        schedules=schedules,
    )
    s3 = state.students["S3"]
    assert s3.held_seconds == 2 * 3600
    assert s3.confirmed_seconds == 2 * 3600
    # 被暂缓的前两小时落在第一个教学日，释放的两小时落在次日。
    assert {d.academic_day: d.seconds for d in s3.daily} == {
        "2024-03-16": 2 * 3600,
    }
    s1 = state.students["S1"]
    assert {d.academic_day: d.seconds for d in s1.daily} == {
        "2024-03-15": 2 * 3600,
        "2024-03-16": 2 * 3600,
    }
    assert state.overages[0]["held_student_ids"] == ["S3"]


def _roster_event(eid, students, activity="A1"):
    return Event(
        event_id=eid,
        plan_version="P1",
        event_type=EventType.ROSTER_CONFIRM,
        student_id="admin-1",
        payload={"activity_id": activity, "student_ids": students, "partial": True},
        created_at=datetime.now(UTC),
    )


def test_partial_roster_confirmations_union_and_release_in_order():
    def checkin(eid, sid):
        return Event(
            event_id=eid,
            plan_version="P1",
            event_type=EventType.CHECKIN,
            student_id=sid,
            payload={
                "activity_id": "A1",
                "activity_type": "regular",
                "check_in_at": "2024-03-15T14:00:00+00:00",
                "check_out_at": "2024-03-15T18:00:00+00:00",
            },
            created_at=datetime.now(UTC),
        )

    checkins = [checkin("E-01", "S1"), checkin("E-02", "S2"), checkin("E-03", "S3")]
    schedules = [_schedule()]
    capacities = [_capacity(cap=2)]

    held_state = replay(
        checkins,
        plan_version="P1",
        timezone_name="UTC",
        required_seconds=0,
        capacities=capacities,
        schedules=schedules,
    )
    assert held_state.students["S3"].checkins[0].status == CheckinStatus.HELD

    # 第一次部分确认只确认 S3：S3 被释放，S2 顺延暂缓。
    partial = replay(
        checkins + [_roster_event("E-10", ["S3"])],
        plan_version="P1",
        timezone_name="UTC",
        required_seconds=0,
        capacities=capacities,
        schedules=schedules,
    )
    assert partial.students["S3"].held_seconds == 0
    assert partial.students["S3"].total_seconds == 4 * 3600
    assert partial.students["S2"].held_seconds == 4 * 3600

    # 第二次部分确认再确认 S2（累积并集）：S2、S3 占座，S1 顺延暂缓，
    # 即确认并不能突破物理容量，只能决定谁优先。
    full = replay(
        checkins + [_roster_event("E-10", ["S3"]), _roster_event("E-11", ["S2"])],
        plan_version="P1",
        timezone_name="UTC",
        required_seconds=0,
        capacities=capacities,
        schedules=schedules,
    )
    assert full.students["S1"].held_seconds == 4 * 3600
    assert full.students["S2"].checkins[0].status == CheckinStatus.CONFIRMED
    assert full.students["S3"].held_seconds == 0

    # 容量版本上调到 3 后无需再确认，三人全部释放。
    raised = replay(
        checkins + [_roster_event("E-10", ["S3"])],
        plan_version="P1",
        timezone_name="UTC",
        required_seconds=0,
        capacities=[_capacity(version="C2", cap=3)],
        schedules=schedules,
    )
    assert all(s.held_seconds == 0 for s in raised.students.values())
    assert raised.overages == []


def test_schedule_impact_only_recomputes_changed_activities():
    records = [
        _record(
            "E-01", "S1",
            "2024-03-15T14:00:00+00:00", "2024-03-15T18:00:00+00:00",
            activity="A1",
        ),
        _record(
            "E-02", "S2",
            "2024-03-15T14:00:00+00:00", "2024-03-15T18:00:00+00:00",
            activity="A2",
        ),
    ]
    old = [
        _schedule(activity="A1", venue="BIG", version="SV1"),
        _schedule(activity="A2", venue="BIG", version="SV1"),
    ]
    new = [
        _schedule(activity="A1", venue="BIG", version="SV2"),
        _schedule(activity="A2", venue="SMALL", version="SV2"),
    ]
    capacities = [
        _capacity(venue="BIG", cap=10),
        _capacity(venue="SMALL", cap=0),
    ]
    impact = schedule_impact(records, old, new, capacities)
    assert impact["activities_affected"] == 1
    assert impact["activities_unaffected"] == 1
    assert impact["affected_activities"][0]["activity_id"] == "A2"
    assert impact["students_affected"] == 1
    assert impact["students"][0]["student_id"] == "S2"
    assert impact["students"][0]["held_seconds_before"] == 0
    assert impact["students"][0]["held_seconds_after"] == 4 * 3600
