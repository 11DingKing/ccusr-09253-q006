"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .core.capacity import (
    ActivitySchedule,
    VenueCapacity,
    schedule_impact,
)
from .core.replay import EventType, replay
from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .repository import (
    get_freeze,
    get_plan,
    get_schedule_baseline,
    insert_capacity_version,
    insert_events,
    insert_freeze,
    insert_schedule_version,
    list_all_schedule_versions,
    list_capacity_versions,
    list_schedule_versions,
    load_events,
    load_events_up_to,
    max_event_id,
    set_schedule_baseline,
    upsert_plan,
    upsert_venue,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


class CapacityError(Exception):
    pass


def get_plan_plain(db: Session, plan_version: str) -> dict[str, Any] | None:
    plan = get_plan(db, plan_version)
    if plan is None:
        return None
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def ensure_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> dict[str, Any]:
    plan = upsert_plan(
        db,
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def import_events(
    db: Session, *, plan_version: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    accepted, duplicates = insert_events(
        db, plan_version=plan_version, events=events
    )
    return {
        "accepted": len(accepted),
        "duplicates": duplicates,
        "rejected": [],
    }


def _active_schedule_version(db: Session, plan_version: str) -> str | None:
    return get_schedule_baseline(db, plan_version)


def _capacity_context(
    db: Session, plan_version: str
) -> tuple[list[VenueCapacity], list[ActivitySchedule], str | None]:
    """返回当前生效的全部容量版本与启用排期。"""
    capacities = list_capacity_versions(db)
    schedule_version = _active_schedule_version(db, plan_version)
    schedules: list[ActivitySchedule] = []
    if schedule_version is not None:
        schedules = list_schedule_versions(
            db, plan_version=plan_version, schedule_version=schedule_version
        )
    return capacities, schedules, schedule_version


def current_snapshot(db: Session, plan_version: str) -> Snapshot:
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    capacities, schedules, _ = _capacity_context(db, plan_version)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        capacities=capacities,
        schedules=schedules,
    )


def student_progress(
    db: Session, plan_version: str, student_id: str
) -> dict[str, Any] | None:
    snap = current_snapshot(db, plan_version)
    return explain_student(snap, student_id)


def freeze_semester(
    db: Session, *, plan_version: str, freeze_id: str
) -> tuple[Snapshot, bool]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    existing = get_freeze(db, plan_version, freeze_id)
    if existing is not None:
        return Snapshot.from_dict(existing.snapshot), False

    cutoff = max_event_id(db, plan_version)
    events = load_events(db, plan_version)
    capacities, schedules, _ = _capacity_context(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
        capacities=capacities,
        schedules=schedules,
    )
    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff,
    )
    if row is None:
        existing = get_freeze(db, plan_version, freeze_id)
        assert existing is not None
        return Snapshot.from_dict(existing.snapshot), False
    return snap, True


def get_frozen_snapshot(
    db: Session, plan_version: str, freeze_id: str
) -> Snapshot:
    _require_plan(db, plan_version)
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise FreezeNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    return Snapshot.from_dict(row.snapshot)


def explain_frozen_student(
    db: Session, plan_version: str, freeze_id: str, student_id: str
) -> dict[str, Any] | None:
    snap = get_frozen_snapshot(db, plan_version, freeze_id)
    return explain_student(snap, student_id)


def diff_freezes(
    db: Session, plan_version: str, old_freeze_id: str, new_freeze_id: str
) -> dict[str, Any]:
    old = get_frozen_snapshot(db, plan_version, old_freeze_id)
    new = get_frozen_snapshot(db, plan_version, new_freeze_id)
    return diff_snapshots(old, new)


# ---------------------------------------------------------------------------
# 场地容量、活动排期与名单确认
# ---------------------------------------------------------------------------


def _parse_aware(value: Any) -> datetime:
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    if moment.tzinfo is None:
        raise CapacityError("timestamps must be timezone-aware (RFC 3339)")
    return moment.astimezone(timezone.utc)


def configure_venue(
    db: Session, *, venue_id: str, name: str
) -> dict[str, Any]:
    upsert_venue(db, venue_id=venue_id, name=name)
    return {"venue_id": venue_id, "name": name}


def list_venues(db: Session) -> list[dict[str, Any]]:
    from .repository import list_venues as _list

    return [{"venue_id": v.venue_id, "name": v.name} for v in _list(db)]


def _venue_exists(db: Session, venue_id: str) -> bool:
    from .repository import list_venues as _list

    return any(v.venue_id == venue_id for v in _list(db))


def configure_capacity(
    db: Session,
    *,
    venue_id: str,
    capacity_version: str,
    capacity: int,
    effective_from: datetime,
) -> dict[str, Any]:
    if not _venue_exists(db, venue_id):
        raise CapacityError(f"venue '{venue_id}' is not registered")
    created = insert_capacity_version(
        db,
        venue_id=venue_id,
        capacity_version=capacity_version,
        capacity=capacity,
        effective_from=effective_from,
    )
    if not created:
        raise CapacityError(
            f"capacity version '{capacity_version}' for venue '{venue_id}' already exists"
        )
    return {
        "venue_id": venue_id,
        "capacity_version": capacity_version,
        "capacity": capacity,
        "effective_from_utc": effective_from.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def list_capacities(db: Session, *, venue_id: str | None = None) -> list[dict[str, Any]]:
    versions = list_capacity_versions(db, venue_id=venue_id)
    return [
        {
            "venue_id": v.venue_id,
            "capacity_version": v.capacity_version,
            "capacity": v.capacity,
            "effective_from_utc": v.effective_from.isoformat().replace("+00:00", "Z"),
        }
        for v in versions
    ]


def configure_schedule(
    db: Session,
    *,
    plan_version: str,
    schedule_version: str,
    entries: list[dict[str, Any]],
    set_active: bool,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        activity_id = str(entry["activity_id"])
        if activity_id in seen:
            raise CapacityError(
                f"duplicate activity_id '{activity_id}' in schedule '{schedule_version}'"
            )
        seen.add(activity_id)
        start = _parse_aware(entry["start_at"])
        end = _parse_aware(entry["end_at"])
        if end <= start:
            raise CapacityError(
                f"schedule for activity '{activity_id}' must end after it starts"
            )
        parsed.append(
            {
                "activity_id": activity_id,
                "venue_id": str(entry["venue_id"]),
                "start": start,
                "end": end,
            }
        )
    for item in parsed:
        if not _venue_exists(db, item["venue_id"]):
            raise CapacityError(f"venue '{item['venue_id']}' is not registered")
    created = insert_schedule_version(
        db,
        schedule_version=schedule_version,
        plan_version=plan_version,
        entries=parsed,
    )
    if not created:
        raise CapacityError(
            f"schedule version '{schedule_version}' already registered"
        )
    if set_active:
        set_schedule_baseline(
            db, plan_version=plan_version, schedule_version=schedule_version
        )
    return _schedule_version_payload(
        db,
        plan_version=plan_version,
        schedule_version=schedule_version,
        active=get_schedule_baseline(db, plan_version) == schedule_version,
    )


def _schedule_entry_to_dict(entry: ActivitySchedule) -> dict[str, Any]:
    return {
        "activity_id": entry.activity_id,
        "venue_id": entry.venue_id,
        "start_at_utc": entry.start.isoformat().replace("+00:00", "Z"),
        "end_at_utc": entry.end.isoformat().replace("+00:00", "Z"),
    }


def _schedule_version_payload(
    db: Session, *, plan_version: str, schedule_version: str, active: bool
) -> dict[str, Any]:
    entries = list_schedule_versions(
        db, plan_version=plan_version, schedule_version=schedule_version
    )
    return {
        "plan_version": plan_version,
        "schedule_version": schedule_version,
        "active": active,
        "entries": [_schedule_entry_to_dict(e) for e in sorted(entries, key=lambda e: e.activity_id)],
    }


def list_schedules(db: Session, plan_version: str) -> list[dict[str, Any]]:
    _require_plan(db, plan_version)
    active = get_schedule_baseline(db, plan_version)
    grouped = list_all_schedule_versions(db, plan_version=plan_version)
    return [
        _schedule_version_payload(
            db,
            plan_version=plan_version,
            schedule_version=version,
            active=version == active,
        )
        for version in sorted(grouped)
    ]


def activate_schedule(
    db: Session, *, plan_version: str, schedule_version: str
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    entries = list_schedule_versions(
        db, plan_version=plan_version, schedule_version=schedule_version
    )
    if not entries:
        raise CapacityError(
            f"schedule version '{schedule_version}' does not exist for plan '{plan_version}'"
        )
    set_schedule_baseline(
        db, plan_version=plan_version, schedule_version=schedule_version
    )
    return _schedule_version_payload(
        db,
        plan_version=plan_version,
        schedule_version=schedule_version,
        active=True,
    )


def _records_for_impact(db: Session, plan_version: str):
    from .core.replay import _parse_checkin

    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    records = []
    for event in events:
        if event.event_type == EventType.CHECKIN:
            records.append(_parse_checkin(event, plan.iana_timezone))
    return plan, events, records


def _confirmed_rosters(
    events: list,
) -> dict[str, set[str]]:
    rosters: dict[str, set[str]] = {}
    for event in events:
        if event.event_type == EventType.ROSTER_CONFIRM:
            aid = str(event.payload.get("activity_id", ""))
            rosters.setdefault(aid, set()).update(
                str(sid) for sid in event.payload.get("student_ids", [])
            )
    return rosters


def preview_conflicts(
    db: Session,
    *,
    plan_version: str,
    schedule_version: str | None = None,
    what_if_capacity: dict[str, int] | None = None,
) -> dict[str, Any]:
    """冲突预览：按指定（默认当前启用）排期与容量模拟超额。"""
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    version = schedule_version or get_schedule_baseline(db, plan_version)
    if version is None:
        raise CapacityError("no active schedule version; pass schedule_version")
    schedules = list_schedule_versions(
        db, plan_version=plan_version, schedule_version=version
    )
    if not schedules:
        raise CapacityError(
            f"schedule version '{version}' does not exist for plan '{plan_version}'"
        )

    capacities = list_capacity_versions(db)
    if what_if_capacity:
        capacities = [
            c for c in capacities if c.venue_id not in what_if_capacity
        ]
        synthetic_version = "__preview__"
        # 容量版本按 effective_from 生效；what-if 视为从远古起生效。
        base = datetime(2000, 1, 1, tzinfo=timezone.utc)
        for venue_id, cap in what_if_capacity.items():
            capacities.append(
                VenueCapacity(
                    venue_id=venue_id,
                    capacity_version=synthetic_version,
                    capacity=cap,
                    effective_from=base,
                )
            )

    state = replay(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        capacities=capacities,
        schedules=schedules,
    )
    return {
        "plan_version": plan_version,
        "schedule_version": version,
        "what_if_capacity": what_if_capacity or {},
        "activities_total": len(schedules),
        "activities_with_overage": len(state.overages),
        "overages": state.overages,
        "held_lesson_units_by_student": {
            sid: progress.held_lesson_units
            for sid, progress in sorted(state.students.items())
            if progress.held_seconds > 0
        },
    }


def _replay_current(db: Session, plan_version: str):
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    capacities, schedules, schedule_version = _capacity_context(db, plan_version)
    state = replay(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        capacities=capacities,
        schedules=schedules,
    )
    return plan, state, schedule_version


def confirm_roster(
    db: Session,
    *,
    plan_version: str,
    event_id: str,
    activity_id: str,
    student_ids: list[str],
    actor_id: str,
    partial: bool,
) -> dict[str, Any]:
    """管理员确认活动实际名单；事件 append-only，多次确认取并集。"""
    _require_plan(db, plan_version)

    before_state = None
    before_allocation = None
    if get_schedule_baseline(db, plan_version) is not None:
        _, before_state, _ = _replay_current(db, plan_version)
        before_allocation = before_state.allocations.get(activity_id)
    previously_held = (
        set(before_allocation.held_student_ids)
        if before_allocation is not None
        else set()
    )

    events = [
        {
            "event_id": event_id,
            "event_type": EventType.ROSTER_CONFIRM.value,
            "student_id": actor_id,
            "payload": {
                "activity_id": activity_id,
                "student_ids": list(student_ids),
                "partial": bool(partial),
                "confirmed_by": actor_id,
            },
        }
    ]
    accepted, duplicates = insert_events(
        db, plan_version=plan_version, events=events
    )
    if duplicates:
        raise CapacityError(f"roster confirmation event '{event_id}' already exists")

    _, state, _ = _replay_current(db, plan_version)
    allocation = state.allocations.get(activity_id)
    still_held = list(allocation.held_student_ids) if allocation else []
    present = (
        set(allocation.admitted_by_student) | set(allocation.held_by_student)
        if allocation is not None
        else set()
    )
    # 本次确认后占座、且确认前正处于暂缓状态的学员即为新释放者。
    released = sorted((set(student_ids) & present) & previously_held - set(still_held))
    return {
        "plan_version": plan_version,
        "event_id": event_id,
        "activity_id": activity_id,
        "confirmed_student_ids": sorted(set(student_ids)),
        "partial": bool(partial),
        "released_student_ids": released,
        "still_held_student_ids": still_held,
    }


def impact_query(
    db: Session,
    *,
    plan_version: str,
    student_id: str | None = None,
    activity_id: str | None = None,
) -> dict[str, Any]:
    """影响查询：当前容量核算下的超额区间与暂缓学时。"""
    _, state, schedule_version = _replay_current(db, plan_version)
    overages = state.overages
    if activity_id is not None:
        overages = [o for o in overages if o["activity_id"] == activity_id]

    students_payload: list[dict[str, Any]] = []
    for sid in sorted(state.students):
        if student_id is not None and sid != student_id:
            continue
        progress = state.students[sid]
        if progress.held_seconds <= 0:
            continue
        students_payload.append(
            {
                "student_id": sid,
                "held_seconds": progress.held_seconds,
                "held_lesson_units": progress.held_lesson_units,
                "confirmed_seconds": progress.confirmed_seconds,
                "total_seconds": progress.total_seconds,
            }
        )
    return {
        "plan_version": plan_version,
        "schedule_version": schedule_version,
        "activities_with_overage": overages,
        "students_with_held_hours": students_payload,
    }


def schedule_version_impact(
    db: Session,
    *,
    plan_version: str,
    old_version: str,
    new_version: str,
) -> dict[str, Any]:
    """排期版本切换影响：只重算两个版本间发生变化的活动。"""
    plan, events, records = _records_for_impact(db, plan_version)
    old_schedules = list_schedule_versions(
        db, plan_version=plan_version, schedule_version=old_version
    )
    new_schedules = list_schedule_versions(
        db, plan_version=plan_version, schedule_version=new_version
    )
    if not old_schedules:
        raise CapacityError(f"schedule version '{old_version}' does not exist")
    if not new_schedules:
        raise CapacityError(f"schedule version '{new_version}' does not exist")
    capacities = list_capacity_versions(db)
    result = schedule_impact(
        records,
        old_schedules,
        new_schedules,
        capacities,
        _confirmed_rosters(events),
    )
    result.update(
        {
            "plan_version": plan_version,
            "old_schedule_version": old_version,
            "new_schedule_version": new_version,
        }
    )
    return result
