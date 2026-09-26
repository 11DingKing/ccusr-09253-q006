"""场地容量联动核心逻辑测试：跨午夜、并发签到、排期版本切换、部分确认。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.capacity import (
    ActivitySchedule,
    VenueCapacity,
)
from app.core.replay import Event, EventType, replay


def _checkin(
    eid: str,
    student: str,
    start: str,
    end: str,
    *,
    activity: str = "A1",
    activity_type: str = "regular",
    plan_version: str = "P1",
) -> Event:
    return Event(
        event_id=eid,
        plan_version=plan_version,
        event_type=EventType.CHECKIN,
        student_id=student,
        payload={
            "activity_id": activity,
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
        created_at=datetime.now(timezone.utc),
    )


def _roster(
    eid: str,
    schedule_id: str,
    *,
    released: tuple[str, ...] = (),
    excluded: tuple[str, ...] = (),
) -> Event:
    return Event(
        event_id=eid,
        plan_version="P1",
        event_type=EventType.ROSTER_CONFIRM,
        student_id="admin",
        payload={
            "schedule_id": schedule_id,
            "released": list(released),
            "excluded": list(excluded),
        },
        created_at=datetime.now(timezone.utc),
    )


def test_concurrent_checkins_over_capacity_are_held_with_deterministic_tiebreak():
    # 容量 2，3 名学生同一时刻签到；并发不影响裁决，按签到事件号稳定排序。
    events = [
        _checkin("E-10", "S3", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin("E-05", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
    ]
    schedules = [
        ActivitySchedule(
            schedule_id="S-01",
            version="v1",
            activity_id="A1",
            venue_id="V1",
            start_utc=datetime.fromisoformat("2024-03-15T08:00:00+08:00").astimezone(timezone.utc),
            end_utc=datetime.fromisoformat("2024-03-15T10:00:00+08:00").astimezone(timezone.utc),
        )
    ]
    capacities = {"V1": VenueCapacity("V1", "v1", 2)}

    state = replay(
        list(reversed(events)),  # 导入顺序打乱
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        schedules=schedules,
        capacities=capacities,
    )
    assert state.students["S1"].confirmed_seconds == 7200
    assert state.students["S2"].confirmed_seconds == 7200
    assert state.students["S3"].confirmed_seconds == 0
    assert state.students["S3"].held_seconds == 7200
    assert len(state.overruns) == 1
    assert state.overruns[0].held_student_ids == ("S3",)
    assert state.overruns[0].headcount == 3

    # 再放一次乱序，结果必须一致。
    state_b = replay(
        [events[2], events[0], events[1]],
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        schedules=schedules,
        capacities=capacities,
    )
    assert state_b.students["S3"].held_seconds == 7200
    assert state_b.students["S1"].confirmed_seconds == 7200


def test_cross_midnight_activity_marks_held_per_academic_day():
    # 上海时区 22:00 到次日 02:00 的跨午夜集中实训，容量 1，两人全程在场。
    schedules = [
        ActivitySchedule(
            schedule_id="S-NIGHT",
            version="v1",
            activity_id="A1",
            venue_id="V1",
            start_utc=datetime.fromisoformat("2024-03-15T22:00:00+08:00").astimezone(timezone.utc),
            end_utc=datetime.fromisoformat("2024-03-16T02:00:00+08:00").astimezone(timezone.utc),
        )
    ]
    capacities = {"V1": VenueCapacity("V1", "v1", 1)}
    events = [
        _checkin("E-01", "S1", "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00"),
        _checkin("E-02", "S2", "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00"),
    ]
    state = replay(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        schedules=schedules,
        capacities=capacities,
    )
    s2 = state.students["S2"]
    assert s2.held_seconds == 4 * 3600
    assert s2.confirmed_seconds == 0
    # 超额区间跨越 UTC 14:00 -> 18:00（本地跨午夜），且按本地日切分后 S1 的
    # 已释放学时分别落在两个教学日。
    s1 = state.students["S1"]
    days = {d.academic_day: d.seconds for d in s1.daily}
    assert days == {"2024-03-15": 2 * 3600, "2024-03-16": 2 * 3600}
    assert state.overruns[0].start_utc.isoformat() == "2024-03-15T14:00:00+00:00"
    assert state.overruns[0].end_utc.isoformat() == "2024-03-15T18:00:00+00:00"


def test_schedule_version_switch_recomputes_differently():
    # v1：场地 V1 容量 1；v2 排期改到 V2（容量 5），超额消失。
    common = dict(
        schedule_id="S-01",
        activity_id="A1",
    )
    v1 = [
        ActivitySchedule(
            **common,
            version="v1",
            venue_id="V1",
            start_utc=datetime.fromisoformat("2024-03-15T08:00:00+08:00").astimezone(timezone.utc),
            end_utc=datetime.fromisoformat("2024-03-15T10:00:00+08:00").astimezone(timezone.utc),
        )
    ]
    v2 = [
        ActivitySchedule(
            **common,
            version="v2",
            venue_id="V2",
            start_utc=datetime.fromisoformat("2024-03-15T08:00:00+08:00").astimezone(timezone.utc),
            end_utc=datetime.fromisoformat("2024-03-15T10:00:00+08:00").astimezone(timezone.utc),
        )
    ]
    caps_v1 = {"V1": VenueCapacity("V1", "v1", 1)}
    caps_v2 = {"V2": VenueCapacity("V2", "v1", 5)}
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin("E-02", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
    ]
    kwargs = dict(
        plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    )
    old = replay(events, schedules=v1, capacities=caps_v1, **kwargs)
    new = replay(events, schedules=v2, capacities=caps_v2, **kwargs)
    assert old.students["S2"].held_seconds == 7200
    assert len(old.overruns) == 1
    assert new.students["S2"].confirmed_seconds == 7200
    assert new.students["S2"].held_seconds == 0
    assert new.overruns == []


def test_partial_roster_confirmation_releases_in_determined_order():
    schedules = [
        ActivitySchedule(
            schedule_id="S-01",
            version="v1",
            activity_id="A1",
            venue_id="V1",
            start_utc=datetime.fromisoformat("2024-03-15T08:00:00+08:00").astimezone(timezone.utc),
            end_utc=datetime.fromisoformat("2024-03-15T12:00:00+08:00").astimezone(timezone.utc),
        )
    ]
    capacities = {"V1": VenueCapacity("V1", "v1", 2)}
    checkins = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"),
        _checkin("E-02", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"),
        _checkin("E-03", "S3", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"),
        _checkin("E-04", "S4", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"),
    ]
    # 管理员先按 [S4] 释放一个名额，并排除 S3；S4 插队入座，S2 被挤出暂缓。
    events = checkins + [
        _roster("E-10", "S-01", released=("S4",), excluded=("S3",)),
    ]
    state = replay(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        schedules=schedules,
        capacities=capacities,
    )
    assert state.students["S1"].confirmed_seconds == 4 * 3600
    assert state.students["S4"].confirmed_seconds == 4 * 3600
    assert state.students["S2"].held_seconds == 4 * 3600
    assert state.students["S3"].excluded_seconds == 4 * 3600
    assert state.students["S3"].confirmed_seconds == 0

    # 第二次确认给出完整名单顺序 [S2, S4]：按新顺序入座。
    events.append(
        _roster("E-11", "S-01", released=("S2", "S4"))
    )
    state = replay(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        schedules=schedules,
        capacities=capacities,
    )
    assert state.students["S2"].confirmed_seconds == 4 * 3600
    assert state.students["S4"].confirmed_seconds == 4 * 3600
    assert state.students["S1"].held_seconds == 4 * 3600
    # S3 未在新裁决中被提及，沿用 E-10 的排除裁决（部分确认不影响未提及者）。
    assert state.students["S3"].excluded_seconds == 4 * 3600
    assert state.students["S3"].held_seconds == 0

    # 第三次确认把 S3 列入释放名单，解除其排除状态。
    events.append(
        _roster("E-12", "S-01", released=("S2", "S3"))
    )
    state = replay(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        schedules=schedules,
        capacities=capacities,
    )
    assert state.students["S2"].confirmed_seconds == 4 * 3600
    assert state.students["S3"].confirmed_seconds == 4 * 3600
    assert state.students["S4"].held_seconds == 4 * 3600
    assert state.students["S3"].excluded_seconds == 0


def test_roster_decisions_are_order_independent_on_input():
    # 与上一场景等价的裁决，但事件以任意顺序喂入，replay 按 event_id 排序。
    schedules = [
        ActivitySchedule(
            schedule_id="S-01",
            version="v1",
            activity_id="A1",
            venue_id="V1",
            start_utc=datetime.fromisoformat("2024-03-15T08:00:00+08:00").astimezone(timezone.utc),
            end_utc=datetime.fromisoformat("2024-03-15T10:00:00+08:00").astimezone(timezone.utc),
        )
    ]
    capacities = {"V1": VenueCapacity("V1", "v1", 1)}
    events = [
        _roster("E-09", "S-01", released=("S9",)),
        _checkin("E-02", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
    ]
    state = replay(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        schedules=schedules,
        capacities=capacities,
    )
    # S9 不在签到名单中不影响；S1 先到先得。
    assert state.students["S1"].confirmed_seconds == 7200
    assert state.students["S2"].held_seconds == 7200


def test_checkin_outside_schedule_window_still_counts():
    # 签到比排期场次长：排期覆盖之外的时段不受容量限制，正常计入学时。
    schedules = [
        ActivitySchedule(
            schedule_id="S-01",
            version="v1",
            activity_id="A1",
            venue_id="V1",
            start_utc=datetime.fromisoformat("2024-03-15T09:00:00+08:00").astimezone(timezone.utc),
            end_utc=datetime.fromisoformat("2024-03-15T10:00:00+08:00").astimezone(timezone.utc),
        )
    ]
    capacities = {"V1": VenueCapacity("V1", "v1", 1)}
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T11:00:00+08:00"),
        _checkin("E-02", "S2", "2024-03-15T09:00:00+08:00", "2024-03-15T10:00:00+08:00"),
    ]
    state = replay(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        schedules=schedules,
        capacities=capacities,
    )
    s2 = state.students["S2"]
    # S2 在排期窗口内被暂缓 1 小时，窗外没有签到，总学时 0。
    assert s2.held_seconds == 3600
    assert s2.confirmed_seconds == 0
    s1 = state.students["S1"]
    # S1 窗外 2 小时照算，窗内 1 小时入座（容量 1），共 3 小时。
    assert s1.confirmed_seconds == 3 * 3600
    assert s1.held_seconds == 0
