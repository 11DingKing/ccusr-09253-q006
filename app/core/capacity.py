"""场地容量与超额区间分配。

本模块为纯函数、无 IO，重放结果完全由输入决定：

* ``VenueCapacity`` 是场地的容量版本，按 ``effective_from`` 时序生效，
  活动窗口跨越版本切换点时，各时段使用当时有效的容量；
* ``ActivitySchedule`` 是某一排期版本中的一场活动（场地 + UTC 窗口）；
* 重放时按扫描线切分初等时间片，统计每个时间片内的实到学员，
  超过容量的学员其学时进入 HELD（暂缓）状态；
* 学员的确定性顺位为 ``(最早签到时间, 签到事件 ID)``。管理员确认实际
  名单后，已确认学员优先，其余学员再按同一顺位补齐座位，因此部分确认
  与事件到达顺序都不会破坏可重放性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from .clock import elapsed_seconds, merge_intervals, to_utc


@dataclass(frozen=True)
class VenueCapacity:
    venue_id: str
    capacity_version: str
    capacity: int
    effective_from: datetime

    def __post_init__(self) -> None:
        if self.capacity < 0:
            raise ValueError("capacity must be non-negative")
        object.__setattr__(self, "effective_from", to_utc(self.effective_from))


@dataclass(frozen=True)
class ActivitySchedule:
    activity_id: str
    schedule_version: str
    venue_id: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = to_utc(self.start)
        end = to_utc(self.end)
        if end <= start:
            raise ValueError("schedule end must be after start")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)


@dataclass(frozen=True)
class _Attendance:
    event_id: str
    student_id: str
    start: datetime
    end: datetime
    check_in_at: datetime


@dataclass(frozen=True)
class OccupiedInterval:
    start: datetime
    end: datetime
    capacity: int | None  # None 表示该场地此时没有任何生效容量版本，不限制
    present: tuple[str, ...]
    admitted: tuple[str, ...]
    held: tuple[str, ...]


@dataclass
class ActivityAllocation:
    schedule: ActivitySchedule
    intervals: list[OccupiedInterval] = field(default_factory=list)
    admitted_by_student: dict[str, list[tuple[datetime, datetime]]] = field(
        default_factory=dict
    )
    held_by_student: dict[str, list[tuple[datetime, datetime]]] = field(
        default_factory=dict
    )
    events_by_student: dict[str, list[str]] = field(default_factory=dict)
    attendance_by_event: dict[str, _Attendance] = field(default_factory=dict)
    peak_occupancy: int = 0

    @property
    def has_overage(self) -> bool:
        return any(slice_.held for slice_ in self.intervals)

    @property
    def held_student_ids(self) -> list[str]:
        ids = {
            sid
            for slice_ in self.intervals
            for sid in slice_.held
        }
        return sorted(ids)

    @property
    def held_event_ids(self) -> list[str]:
        """与任一超额区间相交的签到事件。"""
        ids: set[str] = set()
        for event_id, att in self.attendance_by_event.items():
            for start, end in self.held_by_student.get(att.student_id, []):
                if _intersect(att.start, att.end, start, end) is not None:
                    ids.add(event_id)
                    break
        return sorted(ids)


def _intersect(
    start: datetime, end: datetime, other_start: datetime, other_end: datetime
) -> tuple[datetime, datetime] | None:
    left = max(start, other_start)
    right = min(end, other_end)
    return (left, right) if left < right else None


def intersect_intervals(
    intervals: Sequence[tuple[datetime, datetime]],
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, datetime]]:
    """返回 intervals 与 [start, end) 的交集（已合并）。"""
    pieces: list[tuple[datetime, datetime]] = []
    for seg_start, seg_end in intervals:
        piece = _intersect(start, end, seg_start, seg_end)
        if piece is not None:
            pieces.append(piece)
    return merge_intervals(pieces)


def subtract_interval(
    start: datetime,
    end: datetime,
    other_start: datetime,
    other_end: datetime,
) -> list[tuple[datetime, datetime]]:
    """从 [start, end) 中减去与排期窗口重叠的部分。"""
    piece = _intersect(start, end, other_start, other_end)
    if piece is None:
        return [(start, end)]
    overlap_start, overlap_end = piece
    result: list[tuple[datetime, datetime]] = []
    if start < overlap_start:
        result.append((start, overlap_start))
    if overlap_end < end:
        result.append((overlap_end, end))
    return result


def _capacity_at(
    versions: Sequence[VenueCapacity], moment: datetime
) -> int | None:
    applicable = [v for v in versions if v.effective_from <= moment]
    if not applicable:
        return None
    winner = max(applicable, key=lambda v: (v.effective_from, v.capacity_version))
    return winner.capacity


def _student_key(attendances: Iterable[_Attendance]) -> dict[str, tuple[datetime, str]]:
    keys: dict[str, tuple[datetime, str]] = {}
    for att in attendances:
        candidate = (att.check_in_at, att.event_id)
        current = keys.get(att.student_id)
        if current is None or candidate < current:
            keys[att.student_id] = candidate
    return keys


def allocate_activity(
    schedule: ActivitySchedule,
    records: Iterable[Any],
    capacities: Iterable[VenueCapacity],
    confirmed_student_ids: Iterable[str] = (),
) -> ActivityAllocation:
    """计算一场活动在给定容量版本与名单下的座位分配。

    ``records`` 为鸭子类型的签到记录（含 ``event_id``、``student_id``、
    ``start_utc``、``end_utc``）；签到区间会先与排期窗口求交。
    """
    versions = sorted(
        (c for c in capacities if c.venue_id == schedule.venue_id),
        key=lambda c: (c.effective_from, c.capacity_version),
    )
    confirmed = set(confirmed_student_ids)

    attendances: list[_Attendance] = []
    for record in records:
        piece = _intersect(
            record.start_utc, record.end_utc, schedule.start, schedule.end
        )
        if piece is None:
            continue
        attendances.append(
            _Attendance(
                event_id=record.event_id,
                student_id=record.student_id,
                start=piece[0],
                end=piece[1],
                check_in_at=record.start_utc,
            )
        )

    allocation = ActivityAllocation(schedule=schedule)
    if not attendances:
        return allocation

    keys = _student_key(attendances)
    events_by_student: dict[str, set[str]] = {}
    for att in attendances:
        events_by_student.setdefault(att.student_id, set()).add(att.event_id)
    allocation.events_by_student = {
        sid: sorted(event_ids) for sid, event_ids in events_by_student.items()
    }
    allocation.attendance_by_event = {att.event_id: att for att in attendances}

    boundaries = {schedule.start, schedule.end}
    for att in attendances:
        boundaries.add(att.start)
        boundaries.add(att.end)
    for version in versions:
        if schedule.start < version.effective_from < schedule.end:
            boundaries.add(version.effective_from)

    raw_slices: list[OccupiedInterval] = []
    ordered = sorted(boundaries)
    for t0, t1 in zip(ordered, ordered[1:]):
        present = sorted(
            {
                att.student_id
                for att in attendances
                if att.start <= t0 and att.end >= t1
            },
            key=lambda sid: keys[sid],
        )
        if not present:
            continue
        capacity = _capacity_at(versions, t0)
        if capacity is None:
            admitted = tuple(present)
            held: tuple[str, ...] = ()
        else:
            confirmed_first = [sid for sid in present if sid in confirmed]
            others = [sid for sid in present if sid not in confirmed]
            winners = (confirmed_first + others)[:capacity]
            admitted = tuple(winners)
            held = tuple(sid for sid in present if sid not in winners)
        raw_slices.append(
            OccupiedInterval(
                start=t0, end=t1, capacity=capacity,
                present=tuple(present), admitted=admitted, held=held,
            )
        )

    # 合并相邻且判定结果相同的时间片，输出更紧凑。
    merged: list[OccupiedInterval] = []
    for slice_ in raw_slices:
        if merged:
            prev = merged[-1]
            if (
                prev.end == slice_.start
                and prev.admitted == slice_.admitted
                and prev.held == slice_.held
                and prev.capacity == slice_.capacity
            ):
                merged[-1] = OccupiedInterval(
                    start=prev.start,
                    end=slice_.end,
                    capacity=slice_.capacity,
                    present=tuple(sorted(
                        set(prev.present) | set(slice_.present),
                        key=lambda sid: keys[sid],
                    )),
                    admitted=slice_.admitted,
                    held=slice_.held,
                )
                continue
        merged.append(slice_)
    allocation.intervals = merged
    allocation.peak_occupancy = max(
        (len(slice_.present) for slice_ in raw_slices), default=0
    )

    admitted_acc: dict[str, list[tuple[datetime, datetime]]] = {}
    held_acc: dict[str, list[tuple[datetime, datetime]]] = {}
    for slice_ in merged:
        for sid in slice_.admitted:
            admitted_acc.setdefault(sid, []).append((slice_.start, slice_.end))
        for sid in slice_.held:
            held_acc.setdefault(sid, []).append((slice_.start, slice_.end))
    allocation.admitted_by_student = {
        sid: merge_intervals(parts) for sid, parts in admitted_acc.items()
    }
    allocation.held_by_student = {
        sid: merge_intervals(parts) for sid, parts in held_acc.items()
    }
    return allocation


def _held_seconds(
    allocation: ActivityAllocation | None,
) -> dict[str, int]:
    if allocation is None:
        return {}
    return {
        sid: sum(elapsed_seconds(s, e) for s, e in intervals)
        for sid, intervals in allocation.held_by_student.items()
    }


def _same_schedule(
    old: ActivitySchedule | None, new: ActivitySchedule | None
) -> bool:
    if old is None or new is None:
        return old is new
    return (
        old.venue_id == new.venue_id
        and old.start == new.start
        and old.end == new.end
    )


def schedule_impact(
    records: Iterable[Any],
    old_schedules: Iterable[ActivitySchedule],
    new_schedules: Iterable[ActivitySchedule],
    capacities: Iterable[VenueCapacity],
    confirmed_student_ids_by_activity: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    """排期更正影响：只重算内容发生变化的活动。

    返回受影响活动的两个版本对比（峰值、各学员暂缓秒数）以及学员级
    学时变动汇总；未变化的活动不参与重算。
    """
    old_map = {s.activity_id: s for s in old_schedules}
    new_map = {s.activity_id: s for s in new_schedules}
    records_by_activity: dict[str, list[Any]] = {}
    for record in records:
        records_by_activity.setdefault(record.activity_id, []).append(record)

    candidate_ids = set(old_map) | set(new_map)
    affected_id_set = {
        aid for aid in candidate_ids if not _same_schedule(old_map.get(aid), new_map.get(aid))
    }
    affected_id_set &= set(records_by_activity)  # 无签到的活动无学时影响

    confirmations = confirmed_student_ids_by_activity or {}
    affected: list[dict[str, Any]] = []
    student_deltas: dict[str, dict[str, int]] = {}

    for aid in sorted(affected_id_set):
        old_schedule = old_map.get(aid)
        new_schedule = new_map.get(aid)
        old_alloc = (
            allocate_activity(
                old_schedule,
                records_by_activity.get(aid, []),
                capacities,
                confirmations.get(aid, set()),
            )
            if old_schedule is not None
            else None
        )
        new_alloc = (
            allocate_activity(
                new_schedule,
                records_by_activity.get(aid, []),
                capacities,
                confirmations.get(aid, set()),
            )
            if new_schedule is not None
            else None
        )
        held_before = _held_seconds(old_alloc)
        held_after = _held_seconds(new_alloc)
        for sid in sorted(set(held_before) | set(held_after)):
            before = held_before.get(sid, 0)
            after = held_after.get(sid, 0)
            entry = student_deltas.setdefault(
                sid, {"held_seconds_before": 0, "held_seconds_after": 0}
            )
            entry["held_seconds_before"] += before
            entry["held_seconds_after"] += after

        if old_schedule is None or new_schedule is None:
            change_type = "added" if old_schedule is None else "removed"
        else:
            change_type = "modified"
        affected.append(
            {
                "activity_id": aid,
                "change_type": change_type,
                "venue_before": old_schedule.venue_id if old_schedule else None,
                "venue_after": new_schedule.venue_id if new_schedule else None,
                "peak_before": old_alloc.peak_occupancy if old_alloc else 0,
                "peak_after": new_alloc.peak_occupancy if new_alloc else 0,
                "held_seconds_by_student_before": held_before,
                "held_seconds_by_student_after": held_after,
            }
        )

    for entry in student_deltas.values():
        entry["held_seconds_delta"] = (
            entry["held_seconds_after"] - entry["held_seconds_before"]
        )

    scheduled_with_attendance = len(candidate_ids & set(records_by_activity))
    return {
        "activities_affected": len(affected),
        "activities_unaffected": scheduled_with_attendance - len(affected),
        "affected_activities": affected,
        "students_affected": len(student_deltas),
        "students": [
            {"student_id": sid, **student_deltas[sid]}
            for sid in sorted(student_deltas)
        ],
    }


def _iso(value: datetime) -> str:
    return to_utc(value).isoformat().replace("+00:00", "Z")


def allocation_to_dict(allocation: ActivityAllocation) -> dict[str, Any]:
    schedule = allocation.schedule
    return {
        "activity_id": schedule.activity_id,
        "venue_id": schedule.venue_id,
        "schedule_version": schedule.schedule_version,
        "window_start_utc": _iso(schedule.start),
        "window_end_utc": _iso(schedule.end),
        "peak_occupancy": allocation.peak_occupancy,
        "held_student_ids": allocation.held_student_ids,
        "held_event_ids": allocation.held_event_ids,
        "intervals": [
            {
                "start_utc": _iso(slice_.start),
                "end_utc": _iso(slice_.end),
                "seconds": elapsed_seconds(slice_.start, slice_.end),
                "capacity": slice_.capacity,
                "occupancy": len(slice_.present),
                "present_student_ids": list(slice_.present),
                "admitted_student_ids": list(slice_.admitted),
                "held_student_ids": list(slice_.held),
            }
            for slice_ in allocation.intervals
            if slice_.held
        ],
    }
