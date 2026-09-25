"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.capacity import ActivitySchedule, VenueCapacity
from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import (
    CapacityVersion,
    Event as EventModel,
    Freeze,
    Plan,
    ScheduleBaseline,
    ScheduleVersion,
    Venue,
)


def get_plan(db: Session, plan_version: str) -> Plan | None:
    return db.get(Plan, plan_version)


def upsert_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> Plan:
    stmt = sqlite_insert(Plan).values(
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "iana_timezone": iana_timezone,
            "required_seconds": required_seconds,
        },
    )
    db.execute(stmt)
    db.commit()
    plan = db.get(Plan, plan_version)
    assert plan is not None
    return plan


def _to_core_event(row: EventModel) -> CoreEvent:
    return CoreEvent(
        event_id=row.event_id,
        plan_version=row.plan_version,
        event_type=EventType(row.event_type),
        student_id=row.student_id,
        payload=dict(row.payload),
        created_at=row.created_at,
    )


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """执行确定性的业务处理。"""
    accepted: list[str] = []
    duplicates: list[str] = []
    for e in events:
        stmt = sqlite_insert(EventModel).values(
            event_id=e["event_id"],
            plan_version=plan_version,
            student_id=e["student_id"],
            event_type=e["event_type"],
            payload=e["payload"],
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["event_id", "plan_version"]
        ).returning(EventModel.id)
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            accepted.append(e["event_id"])
        else:
            duplicates.append(e["event_id"])
    db.commit()
    return accepted, duplicates


def load_events(db: Session, plan_version: str) -> list[CoreEvent]:
    stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def load_events_up_to(
    db: Session, plan_version: str, max_event_id: str
) -> list[CoreEvent]:
    """执行确定性的业务处理。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id <= max_event_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def max_event_id(db: Session, plan_version: str) -> str | None:
    stmt = (
        select(EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.event_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> Freeze | None:
    return db.get(Freeze, (plan_version, freeze_id))


def insert_freeze(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
) -> Freeze | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Freeze).values(
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "freeze_id"]
    ).returning(Freeze.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def upsert_venue(db: Session, *, venue_id: str, name: str) -> None:
    stmt = sqlite_insert(Venue).values(venue_id=venue_id, name=name)
    stmt = stmt.on_conflict_do_update(
        index_elements=["venue_id"], set_={"name": name}
    )
    db.execute(stmt)
    db.commit()


def list_venues(db: Session) -> list[Venue]:
    rows = db.execute(select(Venue).order_by(Venue.venue_id)).scalars().all()
    return list(rows)


def insert_capacity_version(
    db: Session,
    *,
    venue_id: str,
    capacity_version: str,
    capacity: int,
    effective_from: datetime,
) -> bool:
    """幂等写入容量版本；同版本已存在则返回 False。"""
    stmt = sqlite_insert(CapacityVersion).values(
        venue_id=venue_id,
        capacity_version=capacity_version,
        capacity=capacity,
        effective_from=_as_utc(effective_from),
    ).on_conflict_do_nothing(
        index_elements=["venue_id", "capacity_version"]
    ).returning(CapacityVersion.venue_id)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    return inserted is not None


def _capacity_to_core(row: CapacityVersion) -> VenueCapacity:
    return VenueCapacity(
        venue_id=row.venue_id,
        capacity_version=row.capacity_version,
        capacity=row.capacity,
        effective_from=_as_utc(row.effective_from),
    )


def list_capacity_versions(
    db: Session, *, venue_id: str | None = None
) -> list[VenueCapacity]:
    stmt = select(CapacityVersion)
    if venue_id is not None:
        stmt = stmt.where(CapacityVersion.venue_id == venue_id)
    rows = db.execute(
        stmt.order_by(
            CapacityVersion.venue_id,
            CapacityVersion.effective_from,
            CapacityVersion.capacity_version,
        )
    ).scalars().all()
    return [_capacity_to_core(r) for r in rows]


def insert_schedule_version(
    db: Session,
    *,
    schedule_version: str,
    plan_version: str,
    entries: list[dict[str, Any]],
) -> bool:
    """幂等写入整版排期；同版本号已有内容时返回 False。"""
    existing = db.execute(
        select(ScheduleVersion.schedule_version)
        .where(ScheduleVersion.schedule_version == schedule_version)
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return False
    for entry in entries:
        db.add(
            ScheduleVersion(
                schedule_version=schedule_version,
                activity_id=entry["activity_id"],
                plan_version=plan_version,
                venue_id=entry["venue_id"],
                start_utc=_as_utc(entry["start"]),
                end_utc=_as_utc(entry["end"]),
            )
        )
    db.commit()
    return True


def _schedule_to_core(row: ScheduleVersion) -> ActivitySchedule:
    return ActivitySchedule(
        activity_id=row.activity_id,
        schedule_version=row.schedule_version,
        venue_id=row.venue_id,
        start=_as_utc(row.start_utc),
        end=_as_utc(row.end_utc),
    )


def list_schedule_versions(
    db: Session, *, plan_version: str, schedule_version: str
) -> list[ActivitySchedule]:
    rows = db.execute(
        select(ScheduleVersion)
        .where(ScheduleVersion.plan_version == plan_version)
        .where(ScheduleVersion.schedule_version == schedule_version)
        .order_by(ScheduleVersion.activity_id)
    ).scalars().all()
    return [_schedule_to_core(r) for r in rows]


def list_all_schedule_versions(
    db: Session, *, plan_version: str
) -> dict[str, list[ActivitySchedule]]:
    rows = db.execute(
        select(ScheduleVersion)
        .where(ScheduleVersion.plan_version == plan_version)
        .order_by(ScheduleVersion.schedule_version, ScheduleVersion.activity_id)
    ).scalars().all()
    grouped: dict[str, list[ActivitySchedule]] = {}
    for row in rows:
        grouped.setdefault(row.schedule_version, []).append(_schedule_to_core(row))
    return grouped


def get_schedule_baseline(db: Session, plan_version: str) -> str | None:
    row = db.get(ScheduleBaseline, plan_version)
    return row.schedule_version if row is not None else None


def set_schedule_baseline(
    db: Session, *, plan_version: str, schedule_version: str
) -> None:
    stmt = sqlite_insert(ScheduleBaseline).values(
        plan_version=plan_version, schedule_version=schedule_version
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={"schedule_version": schedule_version},
    )
    db.execute(stmt)
    db.commit()
