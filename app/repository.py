"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .core.capacity import ActivitySchedule, VenueCapacity
from .models import Event as EventModel
from .models import (
    ActivityScheduleVersion,
    Freeze,
    Plan,
    Venue,
    VenueCapacityVersion,
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


# ---------------------------------------------------------------------------
# 场地、容量版本与活动排期版本
# ---------------------------------------------------------------------------


def list_venues(db: Session, plan_version: str) -> list[Venue]:
    stmt = select(Venue).where(Venue.plan_version == plan_version).order_by(Venue.venue_id)
    return list(db.execute(stmt).scalars().all())


def upsert_venue(db: Session, *, plan_version: str, venue_id: str, name: str = "") -> Venue:
    stmt = sqlite_insert(Venue).values(
        plan_version=plan_version, venue_id=venue_id, name=name
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version", "venue_id"],
        set_={"name": name},
    )
    db.execute(stmt)
    db.commit()
    venue = db.get(Venue, (plan_version, venue_id))
    assert venue is not None
    return venue


def put_capacity_version(
    db: Session,
    *,
    plan_version: str,
    venue_id: str,
    version: str,
    capacity: int,
    note: str = "",
) -> tuple[VenueCapacityVersion, bool]:
    """容量版本不可变：同版本号已存在时返回旧行与 created=False。"""
    stmt = sqlite_insert(VenueCapacityVersion).values(
        plan_version=plan_version,
        venue_id=venue_id,
        version=version,
        capacity=capacity,
        note=note,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "venue_id", "version"]
    ).returning(VenueCapacityVersion.version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    row = db.get(VenueCapacityVersion, (plan_version, venue_id, version))
    assert row is not None
    return row, inserted is not None


def list_capacity_versions(
    db: Session, plan_version: str, venue_id: str | None = None
) -> list[VenueCapacityVersion]:
    stmt = select(VenueCapacityVersion).where(
        VenueCapacityVersion.plan_version == plan_version
    )
    if venue_id is not None:
        stmt = stmt.where(VenueCapacityVersion.venue_id == venue_id)
    rows = db.execute(stmt.order_by(VenueCapacityVersion.venue_id, VenueCapacityVersion.version)).scalars().all()
    return list(rows)


def active_capacities(
    rows: Sequence[VenueCapacityVersion],
) -> dict[str, VenueCapacity]:
    """每个场地取版本号最大的容量版本。"""
    latest: dict[str, VenueCapacityVersion] = {}
    for row in rows:
        current = latest.get(row.venue_id)
        if current is None or row.version > current.version:
            latest[row.venue_id] = row
    return {
        venue_id: VenueCapacity(
            venue_id=row.venue_id, version=row.version, capacity=row.capacity
        )
        for venue_id, row in latest.items()
    }


def put_schedule_version(
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
) -> tuple[ActivityScheduleVersion, bool]:
    """排期版本不可变：同 (schedule_id, version) 已存在时返回旧行。"""
    stmt = sqlite_insert(ActivityScheduleVersion).values(
        plan_version=plan_version,
        schedule_id=schedule_id,
        version=version,
        activity_id=activity_id,
        venue_id=venue_id,
        start_at=start_at,
        end_at=end_at,
        note=note,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "schedule_id", "version"]
    ).returning(ActivityScheduleVersion)
    inserted_row = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted_row is not None:
        return inserted_row, True
    lookup = select(ActivityScheduleVersion).where(
        ActivityScheduleVersion.plan_version == plan_version,
        ActivityScheduleVersion.schedule_id == schedule_id,
        ActivityScheduleVersion.version == version,
    )
    existing = db.execute(lookup).scalar_one()
    return existing, False


def list_schedule_versions(
    db: Session,
    plan_version: str,
    *,
    activity_id: str | None = None,
) -> list[ActivityScheduleVersion]:
    stmt = select(ActivityScheduleVersion).where(
        ActivityScheduleVersion.plan_version == plan_version
    )
    if activity_id is not None:
        stmt = stmt.where(ActivityScheduleVersion.activity_id == activity_id)
    rows = db.execute(
        stmt.order_by(
            ActivityScheduleVersion.schedule_id, ActivityScheduleVersion.version
        )
    ).scalars().all()
    return list(rows)


def _row_to_schedule(row: ActivityScheduleVersion) -> ActivitySchedule:
    # SQLite 不保留时区信息，落库统一为 UTC，读回的 naive 值按 UTC 解释。
    start = row.start_at
    end = row.end_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return ActivitySchedule(
        schedule_id=row.schedule_id,
        version=row.version,
        activity_id=row.activity_id,
        venue_id=row.venue_id,
        start_utc=start.astimezone(timezone.utc),
        end_utc=end.astimezone(timezone.utc),
    )


def select_schedules(
    rows: Sequence[ActivityScheduleVersion],
    *,
    version_selection: Mapping[str, str] | None = None,
) -> list[ActivitySchedule]:
    """按 schedule_id 选择排期版本：默认取最新；selection 可钉住指定版本。"""
    chosen: dict[str, ActivityScheduleVersion] = {}
    for row in rows:
        pinned = (version_selection or {}).get(row.schedule_id)
        if pinned is not None:
            if row.version != pinned:
                continue
        else:
            current = chosen.get(row.schedule_id)
            if current is not None and current.version > row.version:
                continue
        chosen[row.schedule_id] = row
    return [_row_to_schedule(row) for row in chosen.values()]
