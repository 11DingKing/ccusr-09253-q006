"""场地容量联动的应用服务：配置、冲突预览、名单确认、影响查询。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .core.capacity import (
    ActivitySchedule,
    VenueCapacity,
    diff_capacity_context,
)
from .core.clock import elapsed_seconds
from .core.replay import EventType, ReplayState, replay
from .repository import (
    active_capacities,
    get_plan,
    insert_events,
    list_capacity_versions,
    list_schedule_versions,
    list_venues,
    load_events,
    put_capacity_version,
    put_schedule_version,
    upsert_venue,
)
from .services import PlanNotFoundError
from .models import Event as EventModel, Venue


class CapacityConfigError(ValueError):
    """容量/排期配置引用了不存在的版本或场地。"""


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def _require_venue(db: Session, plan_version: str, venue_id: str):
    if db.get(Venue, (plan_version, venue_id)) is None:
        raise CapacityConfigError(
            f"venue '{venue_id}' is not registered for plan '{plan_version}'"
        )


def create_venue(db: Session, *, plan_version: str, venue_id: str, name: str) -> dict[str, Any]:
    _require_plan(db, plan_version)
    venue = upsert_venue(db, plan_version=plan_version, venue_id=venue_id, name=name)
    return {
        "plan_version": venue.plan_version,
        "venue_id": venue.venue_id,
        "name": venue.name,
    }


def list_venue_ids(db: Session, plan_version: str) -> list[str]:
    return [v.venue_id for v in list_venues(db, plan_version)]


def add_capacity_version(
    db: Session,
    *,
    plan_version: str,
    venue_id: str,
    version: str,
    capacity: int,
    note: str = "",
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    _require_venue(db, plan_version, venue_id)
    row, created = put_capacity_version(
        db,
        plan_version=plan_version,
        venue_id=venue_id,
        version=version,
        capacity=capacity,
        note=note,
    )
    if not created and row.capacity != capacity:
        raise CapacityConfigError(
            f"capacity version '{version}' for venue '{venue_id}' already exists "
            f"with capacity {row.capacity}; versions are immutable, publish a new version"
        )
    return {
        "plan_version": row.plan_version,
        "venue_id": row.venue_id,
        "version": row.version,
        "capacity": row.capacity,
        "note": row.note,
        "created": created,
    }


def add_schedule_version(
    db: Session,
    *,
    plan_version: str,
    schedule_id: str,
    version: str,
    activity_id: str,
    venue_id: str,
    start_at: datetime,
    end_at: datetime,
    note: str = "",
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    _require_venue(db, plan_version, venue_id)
    start_utc = start_at.astimezone(timezone.utc)
    end_utc = end_at.astimezone(timezone.utc)
    row, created = put_schedule_version(
        db,
        plan_version=plan_version,
        schedule_id=schedule_id,
        version=version,
        activity_id=activity_id,
        venue_id=venue_id,
        start_at=start_utc,
        end_at=end_utc,
        note=note,
    )
    existing_start = row.start_at
    existing_end = row.end_at
    if existing_start.tzinfo is None:
        existing_start = existing_start.replace(tzinfo=timezone.utc)
    if existing_end.tzinfo is None:
        existing_end = existing_end.replace(tzinfo=timezone.utc)
    if not created and (
        row.activity_id != activity_id
        or row.venue_id != venue_id
        or existing_start != start_utc
        or existing_end != end_utc
    ):
        raise CapacityConfigError(
            f"schedule version '{version}' for schedule '{schedule_id}' already exists "
            "with different content; versions are immutable, publish a new version"
        )
    return {
        "plan_version": row.plan_version,
        "schedule_id": row.schedule_id,
        "version": row.version,
        "activity_id": row.activity_id,
        "venue_id": row.venue_id,
        "start_at": existing_start,
        "end_at": existing_end,
        "note": row.note,
        "created": created,
    }


def _capacities_for(
    db: Session, plan_version: str, venue_versions: dict[str, str] | None
) -> dict[str, VenueCapacity]:
    rows = list_capacity_versions(db, plan_version)
    if not venue_versions:
        return active_capacities(rows)
    by_venue: dict[str, dict[str, int]] = {}
    for row in rows:
        by_venue.setdefault(row.venue_id, {})[row.version] = row.capacity
    result: dict[str, VenueCapacity] = {}
    for venue_id, version in venue_versions.items():
        versions = by_venue.get(venue_id, {})
        if version not in versions:
            raise CapacityConfigError(
                f"venue '{venue_id}' has no capacity version '{version}'"
            )
        result[venue_id] = VenueCapacity(
            venue_id=venue_id, version=version, capacity=versions[version]
        )
    # 未显式钉住版本的场地仍取最新版本参与计算。
    for venue_id, cap in active_capacities(rows).items():
        result.setdefault(venue_id, cap)
    return result


def _schedules_for(
    db: Session,
    plan_version: str,
    schedule_versions: dict[str, str] | None,
    activity_ids: set[str] | None = None,
) -> list[ActivitySchedule]:
    rows = list_schedule_versions(db, plan_version)
    if activity_ids is not None:
        rows = [r for r in rows if r.activity_id in activity_ids]
    from .repository import select_schedules

    schedules = select_schedules(rows, version_selection=schedule_versions or None)
    available: dict[str, set[str]] = {}
    for row in rows:
        available.setdefault(row.schedule_id, set()).add(row.version)
    for schedule_id, version in (schedule_versions or {}).items():
        if version not in available.get(schedule_id, set()):
            raise CapacityConfigError(
                f"schedule '{schedule_id}' has no version '{version}'"
            )
    return schedules


def _load_context(
    db: Session,
    plan_version: str,
    *,
    schedule_versions: dict[str, str] | None = None,
    venue_versions: dict[str, str] | None = None,
    activity_ids: set[str] | None = None,
):
    schedules = _schedules_for(
        db, plan_version, schedule_versions, activity_ids=activity_ids
    )
    capacities = _capacities_for(db, plan_version, venue_versions)
    return schedules, capacities


def _replay(
    db: Session,
    plan,
    schedules: Sequence[ActivitySchedule],
    capacities: dict[str, VenueCapacity],
) -> ReplayState:
    events = load_events(db, plan.plan_version)
    return replay(
        events,
        plan_version=plan.plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        schedules=schedules,
        capacities=capacities,
    )


def _student_held_for_schedule(state: ReplayState, schedule_id: str) -> dict[str, int]:
    """每个学生在指定排期场次上被暂缓（held）的秒数。"""
    held_by_student: dict[str, int] = {}
    for student in state.students.values():
        total = sum(
            hold.seconds
            for hold in student.capacity_holds
            if hold.schedule_id == schedule_id and hold.state == "held"
        )
        if total:
            held_by_student[student.student_id] = total
    return held_by_student


def preview_conflicts(
    db: Session,
    plan_version: str,
    *,
    schedule_versions: dict[str, str] | None = None,
    venue_versions: dict[str, str] | None = None,
    activity_ids: list[str] | None = None,
) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    schedules, capacities = _load_context(
        db,
        plan_version,
        schedule_versions=schedule_versions,
        venue_versions=venue_versions,
        activity_ids=set(activity_ids) if activity_ids else None,
    )
    state = _replay(db, plan, schedules, capacities)

    selected_schedule_versions = {s.schedule_id: s.version for s in schedules}
    selected_venue_versions = {vid: cap.version for vid, cap in capacities.items()}

    held_total = 0
    excluded_total = 0
    held_checkins: set[str] = set()
    affected_students: set[str] = set()
    holds_out: list[dict[str, Any]] = []
    for sid in sorted(state.students):
        student = state.students[sid]
        for hold in student.capacity_holds:
            if hold.state == "held":
                held_total += hold.seconds
                held_checkins.add(hold.checkin_event_id)
            elif hold.state == "excluded":
                excluded_total += hold.seconds
            affected_students.add(sid)
            holds_out.append(
                {
                    "checkin_event_id": hold.checkin_event_id,
                    "activity_id": hold.activity_id,
                    "schedule_id": hold.schedule_id,
                    "venue_id": hold.venue_id,
                    "start_utc": hold.start_utc.isoformat().replace("+00:00", "Z"),
                    "end_utc": hold.end_utc.isoformat().replace("+00:00", "Z"),
                    "seconds": hold.seconds,
                    "state": hold.state,
                    "decision_event_id": hold.decision_event_id,
                }
            )

    segments = [
        {
            "venue_id": w.venue_id,
            "start_utc": w.start_utc.isoformat().replace("+00:00", "Z"),
            "end_utc": w.end_utc.isoformat().replace("+00:00", "Z"),
            "seconds": elapsed_seconds(w.start_utc, w.end_utc),
            "capacity": w.capacity,
            "headcount": w.headcount,
            "schedule_ids": list(w.schedule_ids),
            "held_student_ids": list(w.held_student_ids),
            "excluded_student_ids": list(w.excluded_student_ids),
        }
        for w in state.overruns
    ]

    return {
        "plan_version": plan_version,
        "schedule_versions": selected_schedule_versions,
        "venue_versions": selected_venue_versions,
        "excess_segments": segments,
        "held_seconds": held_total,
        "excluded_seconds": excluded_total,
        "held_checkins": len(held_checkins),
        "affected_students": sorted(affected_students),
        "holds": sorted(
            holds_out, key=lambda h: (h["start_utc"], h["checkin_event_id"])
        ),
    }


def _next_roster_event_id(db: Session, plan_version: str) -> str:
    stmt = select(func.count()).select_from(EventModel).where(
        EventModel.plan_version == plan_version,
        EventModel.event_type == EventType.ROSTER_CONFIRM.value,
    )
    count = db.execute(stmt).scalar_one()
    return f"RC-{count + 1:06d}"


def confirm_roster(
    db: Session,
    plan_version: str,
    *,
    schedule_id: str,
    released: list[str],
    excluded: list[str],
    reason: str = "",
    actor: str = "admin",
) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    schedule_rows = [
        s for s in list_schedule_versions(db, plan_version) if s.schedule_id == schedule_id
    ]
    if not schedule_rows:
        raise CapacityConfigError(f"schedule '{schedule_id}' is not registered")

    schedules, capacities = _load_context(db, plan_version)
    before = _replay(db, plan, schedules, capacities)
    before_held = _student_held_for_schedule(before, schedule_id)

    # 追加一条 roster_confirm 事件；event_id 在 plan 内顺序生成、冲突重试。
    for _ in range(5):
        event_id = _next_roster_event_id(db, plan_version)
        payload = {
            "schedule_id": schedule_id,
            "released": list(released),
            "excluded": list(excluded),
            "reason": reason,
            "actor": actor,
        }
        accepted, _duplicates = insert_events(
            db,
            plan_version=plan_version,
            events=[
                {
                    "event_id": event_id,
                    "event_type": EventType.ROSTER_CONFIRM.value,
                    "student_id": actor,
                    "payload": payload,
                }
            ],
        )
        if accepted:
            break
    else:  # pragma: no cover - 并发极端情况
        raise CapacityConfigError("could not allocate roster confirm event id")

    after = _replay(db, plan, schedules, capacities)
    after_held = _student_held_for_schedule(after, schedule_id)
    newly_seated = sum(
        seconds
        for sid, seconds in before_held.items()
        if after_held.get(sid, 0) < seconds
        for seconds in [seconds - after_held.get(sid, 0)]
    )

    return {
        "event_id": event_id,
        "plan_version": plan_version,
        "schedule_id": schedule_id,
        "released": list(released),
        "excluded": list(excluded),
        "reason": reason,
        "newly_seated_seconds": newly_seated,
        "still_held_seconds": sum(after_held.values()),
    }


def _summary(state: ReplayState) -> dict[str, Any]:
    held = excluded = confirmed = 0
    for student in state.students.values():
        held += student.held_seconds
        excluded += student.excluded_seconds
        confirmed += student.confirmed_seconds
    return {
        "held_seconds": held,
        "excluded_seconds": excluded,
        "confirmed_seconds": confirmed,
        "overrun_segments": len(state.overruns),
    }


def query_impact(
    db: Session,
    plan_version: str,
    *,
    schedule_versions: dict[str, str] | None = None,
    venue_versions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """对比"当前生效版本(current)"与"拟议版本选择(proposed)"，给出影响面。"""
    plan = _require_plan(db, plan_version)

    current_schedules, current_capacities = _load_context(db, plan_version)
    proposed_schedules, proposed_capacities = _load_context(
        db,
        plan_version,
        schedule_versions=schedule_versions,
        venue_versions=venue_versions,
    )

    scope = diff_capacity_context(
        current_schedules, proposed_schedules, current_capacities, proposed_capacities
    )
    affected_activities = scope["affected_activities"]

    current_state = _replay(db, plan, current_schedules, current_capacities)
    proposed_state = _replay(db, plan, proposed_schedules, proposed_capacities)

    # 受影响学生/签到：落在受影响活动上的已确认签到。
    affected_students: set[str] = set()
    affected_checkins: set[str] = set()
    for student in proposed_state.students.values():
        for checkin in student.checkins:
            if checkin.activity_id in affected_activities:
                affected_students.add(student.student_id)
                affected_checkins.add(checkin.event_id)

    return {
        "plan_version": plan_version,
        "changed_schedule_ids": sorted(scope["changed_schedule_ids"]),
        "changed_capacity_venues": sorted(scope["changed_capacity_venues"]),
        "affected_activities": sorted(affected_activities),
        "affected_students": sorted(affected_students),
        "affected_checkins": sorted(affected_checkins),
        "current": _summary(current_state),
        "proposed": _summary(proposed_state),
    }
