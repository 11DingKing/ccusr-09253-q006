"""场地容量联动的纯领域逻辑。

本模块不依赖数据库：replay 把"已确认签到"与"活动排期、容量版本、名单
确认"汇集到这里，扫描同一时段同一空间内的占用，输出超额区间以及每条
签到片段的裁决（seated / held / excluded）。

裁决规则（确定性）：

* 名单确认事件按 event_id 排序，同一学生在同一排期上被多次提及，以
  event_id 最大（最新）的裁决为准；
* 被 excluded 的学生不占容量，其签到片段记为 excluded，不计学时；
* 其余学生按 released 名单中的顺序（decision event_id + 位次）优先入座，
  管理员未裁决的学生按签到事件号排在后面；
* 超过容量的学生片段记为 held，学时暂缓，等待后续名单确认释放。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

from .clock import elapsed_seconds, merge_intervals, to_utc

SEATED = "seated"
HELD = "held"
EXCLUDED = "excluded"


@dataclass(frozen=True)
class VenueCapacity:
    venue_id: str
    version: str
    capacity: int


@dataclass(frozen=True)
class ActivitySchedule:
    schedule_id: str
    version: str
    activity_id: str
    venue_id: str
    start_utc: datetime
    end_utc: datetime


@dataclass(frozen=True)
class RosterDecision:
    event_id: str
    schedule_id: str
    released: tuple[str, ...]
    excluded: tuple[str, ...]


@dataclass(frozen=True)
class AttendanceClip:
    """某条签到与某个排期场次相交的时间片。"""

    student_id: str
    checkin_event_id: str
    activity_id: str
    schedule_id: str
    venue_id: str
    start_utc: datetime
    end_utc: datetime


@dataclass(frozen=True)
class ClipMark:
    student_id: str
    checkin_event_id: str
    schedule_id: str
    venue_id: str
    start_utc: datetime
    end_utc: datetime
    state: str
    decision_event_id: str | None = None


@dataclass(frozen=True)
class ExcessSegment:
    venue_id: str
    start_utc: datetime
    end_utc: datetime
    capacity: int
    headcount: int
    schedule_ids: tuple[str, ...]
    held_student_ids: tuple[str, ...]
    excluded_student_ids: tuple[str, ...]


@dataclass
class CapacityScan:
    marks: list[ClipMark] = field(default_factory=list)
    excess_segments: list[ExcessSegment] = field(default_factory=list)

    def marks_for_checkin(self, checkin_event_id: str) -> list[ClipMark]:
        return [m for m in self.marks if m.checkin_event_id == checkin_event_id]

    def state_intervals(
        self, checkin_event_id: str, state: str
    ) -> list[tuple[datetime, datetime]]:
        return merge_intervals(
            [
                (m.start_utc, m.end_utc)
                for m in self.marks
                if m.checkin_event_id == checkin_event_id and m.state == state
            ]
        )


def build_attendance_clips(
    checkins,  # Sequence[CheckinRecord]，仅传入 status == CONFIRMED 的记录
    schedules: Sequence[ActivitySchedule],
) -> list[AttendanceClip]:
    """把每条签到与同活动且时间相交的排期场次求交，生成占用片段。"""
    by_activity: dict[str, list[ActivitySchedule]] = {}
    for schedule in schedules:
        by_activity.setdefault(schedule.activity_id, []).append(schedule)

    clips: list[AttendanceClip] = []
    for record in checkins:
        for schedule in by_activity.get(record.activity_id, []):
            start = max(record.start_utc, schedule.start_utc)
            end = min(record.end_utc, schedule.end_utc)
            if start < end:
                clips.append(
                    AttendanceClip(
                        student_id=record.student_id,
                        checkin_event_id=record.event_id,
                        activity_id=record.activity_id,
                        schedule_id=schedule.schedule_id,
                        venue_id=schedule.venue_id,
                        start_utc=to_utc(start),
                        end_utc=to_utc(end),
                    )
                )
    return clips


def _decision_maps(
    decisions: Iterable[RosterDecision],
) -> tuple[
    dict[tuple[str, str], tuple[str, int]],
    dict[tuple[str, str], str],
]:
    """返回 ((schedule, student) -> 释放排序键, (schedule, student) -> 排除事件)。"""
    released: dict[tuple[str, str], tuple[str, int]] = {}
    excluded: dict[tuple[str, str], str] = {}
    # replay 已按 event_id 排序，后写入的即最新裁决。
    for decision in decisions:
        for pos, student_id in enumerate(decision.released):
            released[(decision.schedule_id, student_id)] = (decision.event_id, pos)
            excluded.pop((decision.schedule_id, student_id), None)
        for student_id in decision.excluded:
            excluded[(decision.schedule_id, student_id)] = decision.event_id
            released.pop((decision.schedule_id, student_id), None)
    return released, excluded


def scan_capacity(
    clips: Sequence[AttendanceClip],
    capacities: dict[str, VenueCapacity],
    decisions: Sequence[RosterDecision] = (),
) -> CapacityScan:
    """按场地扫描占用片段，输出每个片段的裁决与超额区间。"""
    released_map, excluded_map = _decision_maps(decisions)

    by_venue: dict[str, list[AttendanceClip]] = {}
    for clip in clips:
        by_venue.setdefault(clip.venue_id, []).append(clip)

    all_marks: list[ClipMark] = []
    excess: list[ExcessSegment] = []

    for venue_id in sorted(by_venue):
        venue_clips = by_venue[venue_id]
        capacity = capacities.get(venue_id)
        if capacity is None:
            # 没有容量版本的场地不做限制，全部入座。
            all_marks.extend(
                ClipMark(
                    student_id=c.student_id,
                    checkin_event_id=c.checkin_event_id,
                    schedule_id=c.schedule_id,
                    venue_id=venue_id,
                    start_utc=c.start_utc,
                    end_utc=c.end_utc,
                    state=SEATED,
                )
                for c in venue_clips
            )
            continue

        boundaries = sorted(
            {to_utc(b) for c in venue_clips for b in (c.start_utc, c.end_utc)}
        )
        for t0, t1 in zip(boundaries, boundaries[1:]):
            active = [c for c in venue_clips if c.start_utc <= t0 and c.end_utc >= t1]
            if not active:
                continue

            # 同一学生可能通过多个排期场次进入同一空间，先按学生聚合。
            per_student: dict[str, list[AttendanceClip]] = {}
            for clip in active:
                per_student.setdefault(clip.student_id, []).append(clip)

            excluded_students: set[str] = set()
            present: dict[str, list[AttendanceClip]] = {}
            release_key: dict[str, tuple[str, int]] = {}
            for student_id, student_clips in per_student.items():
                live = [
                    c
                    for c in student_clips
                    if (c.schedule_id, student_id) not in excluded_map
                ]
                if not live:
                    excluded_students.add(student_id)
                    continue
                present[student_id] = live
                keys = [
                    released_map[(c.schedule_id, student_id)]
                    for c in live
                    if (c.schedule_id, student_id) in released_map
                ]
                if keys:
                    release_key[student_id] = min(keys)

            for student_id in excluded_students:
                decision_id = next(
                    excluded_map[(c.schedule_id, student_id)]
                    for c in per_student[student_id]
                )
                for clip in per_student[student_id]:
                    all_marks.append(
                        ClipMark(
                            student_id=student_id,
                            checkin_event_id=clip.checkin_event_id,
                            schedule_id=clip.schedule_id,
                            venue_id=venue_id,
                            start_utc=t0,
                            end_utc=t1,
                            state=EXCLUDED,
                            decision_event_id=decision_id,
                        )
                    )

            # 释放优先级：最近一次提及该学生的裁决优先（事件号倒序），
            # 同一裁决内按管理员给定的名单位次；未裁决学生按签到事件号排在后面。
            release_rank: dict[str, int] = {}
            grouped: dict[str, list[str]] = {}
            for student_id in release_key:
                grouped.setdefault(release_key[student_id][0], []).append(student_id)
            rank = 0
            for decision_id in sorted(grouped, reverse=True):
                for student_id in sorted(
                    grouped[decision_id], key=lambda s: release_key[s][1]
                ):
                    release_rank[student_id] = rank
                    rank += 1

            def _order_key(item: tuple[str, list[AttendanceClip]]):
                student_id, student_clips = item
                if student_id in release_rank:
                    return (0, release_rank[student_id], student_id)
                earliest = min(c.checkin_event_id for c in student_clips)
                return (1, earliest, 0, student_id)

            ordered = sorted(present.items(), key=_order_key)
            seated_students = {sid for sid, _ in ordered[: capacity.capacity]}
            over = len(ordered) > capacity.capacity

            for student_id, student_clips in ordered:
                state = SEATED if student_id in seated_students else HELD
                decision_id = release_key.get(student_id, (None, -1))[0]
                for clip in student_clips:
                    all_marks.append(
                        ClipMark(
                            student_id=student_id,
                            checkin_event_id=clip.checkin_event_id,
                            schedule_id=clip.schedule_id,
                            venue_id=venue_id,
                            start_utc=t0,
                            end_utc=t1,
                            state=state,
                            decision_event_id=decision_id,
                        )
                    )

            if over:
                held_ids = tuple(
                    sorted(sid for sid, _ in ordered[capacity.capacity :])
                )
                schedule_ids = tuple(sorted({c.schedule_id for c in active}))
                excess.append(
                    ExcessSegment(
                        venue_id=venue_id,
                        start_utc=t0,
                        end_utc=t1,
                        capacity=capacity.capacity,
                        headcount=len(ordered),
                        schedule_ids=schedule_ids,
                        held_student_ids=held_ids,
                        excluded_student_ids=tuple(sorted(excluded_students)),
                    )
                )

    return CapacityScan(marks=all_marks, excess_segments=_merge_excess(excess))


def _merge_excess(segments: Sequence[ExcessSegment]) -> list[ExcessSegment]:
    """合并相邻且裁决构成相同的超额小区间，便于展示。"""
    merged: list[ExcessSegment] = []
    for segment in sorted(segments, key=lambda s: (s.venue_id, s.start_utc)):
        if merged:
            prev = merged[-1]
            if (
                prev.venue_id == segment.venue_id
                and prev.end_utc == segment.start_utc
                and prev.capacity == segment.capacity
                and prev.headcount == segment.headcount
                and prev.schedule_ids == segment.schedule_ids
                and prev.held_student_ids == segment.held_student_ids
                and prev.excluded_student_ids == segment.excluded_student_ids
            ):
                merged[-1] = ExcessSegment(
                    venue_id=prev.venue_id,
                    start_utc=prev.start_utc,
                    end_utc=segment.end_utc,
                    capacity=prev.capacity,
                    headcount=prev.headcount,
                    schedule_ids=prev.schedule_ids,
                    held_student_ids=prev.held_student_ids,
                    excluded_student_ids=prev.excluded_student_ids,
                )
                continue
        merged.append(segment)
    return merged


def interval_seconds(intervals: Sequence[tuple[datetime, datetime]]) -> int:
    return sum(elapsed_seconds(start, end) for start, end in merge_intervals(list(intervals)))


def active_schedule_map(schedules: Iterable[ActivitySchedule]) -> dict[str, ActivitySchedule]:
    """同一 schedule_id 只保留 version 最大的一条（活动版本切换的取数约定）。"""
    chosen: dict[str, ActivitySchedule] = {}
    for schedule in schedules:
        current = chosen.get(schedule.schedule_id)
        if current is None or schedule.version > current.version:
            chosen[schedule.schedule_id] = schedule
    return chosen


def diff_capacity_context(
    old_schedules: Sequence[ActivitySchedule],
    new_schedules: Sequence[ActivitySchedule],
    old_capacities: dict[str, VenueCapacity],
    new_capacities: dict[str, VenueCapacity],
) -> dict[str, object]:
    """比较两套排期/容量版本，给出排期更正实际波及的范围。

    受影响活动 = 自身版本发生变化的活动 + 与变化场次同场地同时段共用
    容量的其他活动（一个场地换人或调时间会改变同时段的竞争关系）。
    """
    old_map = active_schedule_map(old_schedules)
    new_map = active_schedule_map(new_schedules)

    changed_schedule_ids: set[str] = set()
    windows_by_venue: dict[str, list[tuple[datetime, datetime]]] = {}

    for schedule_id in sorted(set(old_map) | set(new_map)):
        before = old_map.get(schedule_id)
        after = new_map.get(schedule_id)
        if before is None or after is None or (
            before.venue_id, before.start_utc, before.end_utc
        ) != (after.venue_id, after.start_utc, after.end_utc):
            changed_schedule_ids.add(schedule_id)
            for schedule in (before, after):
                if schedule is not None:
                    windows_by_venue.setdefault(schedule.venue_id, []).append(
                        (schedule.start_utc, schedule.end_utc)
                    )

    changed_capacity_venues = {
        venue_id
        for venue_id in set(old_capacities) | set(new_capacities)
        if (
            venue_id not in old_capacities
            or venue_id not in new_capacities
            or old_capacities[venue_id].capacity != new_capacities[venue_id].capacity
        )
    }
    # 容量变更波及该场地的全部场次。
    for schedule in new_map.values():
        if schedule.venue_id in changed_capacity_venues:
            windows_by_venue.setdefault(schedule.venue_id, []).append(
                (schedule.start_utc, schedule.end_utc)
            )

    affected_activities: set[str] = set()
    for schedule_id in changed_schedule_ids:
        target = new_map.get(schedule_id) or old_map.get(schedule_id)
        if target is not None:
            affected_activities.add(target.activity_id)

    for venue_id, windows in windows_by_venue.items():
        merged_windows = merge_intervals([(to_utc(s), to_utc(e)) for s, e in windows])
        for schedule in new_map.values():
            if schedule.venue_id != venue_id:
                continue
            if any(
                schedule.start_utc < window_end and schedule.end_utc > window_start
                for window_start, window_end in merged_windows
            ):
                affected_activities.add(schedule.activity_id)

    return {
        "changed_schedule_ids": changed_schedule_ids,
        "changed_capacity_venues": changed_capacity_venues,
        "affected_activities": affected_activities,
    }
