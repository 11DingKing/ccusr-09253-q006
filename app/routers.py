"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import services
from .db import get_db
from .schemas import (
    CapacityIn,
    CapacityOut,
    ConflictPreviewIn,
    ConflictPreviewOut,
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImpactQueryOut,
    ImportResult,
    PlanIn,
    PlanOut,
    RosterConfirmIn,
    RosterConfirmOut,
    ScheduleImpactOut,
    ScheduleIn,
    ScheduleOut,
    SnapshotOut,
    StudentProgressOut,
    VenueIn,
    VenueOut,
)

router = APIRouter(prefix="/api")


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(body: PlanIn, db: Session = Depends(get_db)) -> Any:
    return services.ensure_plan(
        db,
        plan_version=body.plan_version,
        iana_timezone=body.iana_timezone,
        required_seconds=body.required_seconds,
    )


@router.get("/plans/{plan_version}", response_model=PlanOut)
def read_plan(plan_version: str, db: Session = Depends(get_db)) -> Any:
    plan = services.get_plan_plain(db, plan_version)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@router.post(
    "/plans/{plan_version}/events",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_events(
    plan_version: str, body: EventBatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.import_events(
            db,
            plan_version=plan_version,
            events=[e.model_dump() for e in body.events],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/snapshot",
    response_model=SnapshotOut,
)
def get_snapshot(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        snap = services.current_snapshot(db, plan_version)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/progress",
    response_model=StudentProgressOut,
)
def get_progress(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = services.student_progress(db, plan_version, student_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze(
    plan_version: str,
    freeze_id: str,
    body: FreezeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap, _ = services.freeze_semester(
            db, plan_version=plan_version, freeze_id=freeze_id
        )
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
)
def get_freeze(
    plan_version: str, freeze_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        snap = services.get_frozen_snapshot(db, plan_version, freeze_id)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.FreezeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/explain/{student_id}",
    response_model=StudentProgressOut,
)
def explain_freeze_student(
    plan_version: str,
    freeze_id: str,
    student_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.explain_frozen_student(
            db, plan_version, freeze_id, student_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/diff/{other_freeze_id}",
    response_model=DiffOut,
)
def get_diff(
    plan_version: str,
    freeze_id: str,
    other_freeze_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.diff_freezes(
            db, plan_version, freeze_id, other_freeze_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 场地与容量版本
# ---------------------------------------------------------------------------


@router.post("/venues", response_model=VenueOut, status_code=status.HTTP_201_CREATED)
def post_venue(body: VenueIn, db: Session = Depends(get_db)) -> Any:
    return services.configure_venue(db, venue_id=body.venue_id, name=body.name)


@router.get("/venues", response_model=list[VenueOut])
def get_venues(db: Session = Depends(get_db)) -> Any:
    return services.list_venues(db)


@router.post(
    "/venues/{venue_id}/capacity-versions",
    response_model=CapacityOut,
    status_code=status.HTTP_201_CREATED,
)
def post_capacity(
    venue_id: str, body: CapacityIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.configure_capacity(
            db,
            venue_id=venue_id,
            capacity_version=body.capacity_version,
            capacity=body.capacity,
            effective_from=body.effective_from,
        )
    except services.CapacityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/venues/{venue_id}/capacity-versions", response_model=list[CapacityOut])
def get_capacity_versions(
    venue_id: str, db: Session = Depends(get_db)
) -> Any:
    return services.list_capacities(db, venue_id=venue_id)


# ---------------------------------------------------------------------------
# 活动排期版本
# ---------------------------------------------------------------------------


@router.post(
    "/plans/{plan_version}/schedule-versions",
    response_model=ScheduleOut,
    status_code=status.HTTP_201_CREATED,
)
def post_schedule(
    plan_version: str, body: ScheduleIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.configure_schedule(
            db,
            plan_version=plan_version,
            schedule_version=body.schedule_version,
            entries=[e.model_dump() for e in body.entries],
            set_active=body.set_active,
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.CapacityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/schedule-versions",
    response_model=list[ScheduleOut],
)
def get_schedules(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.list_schedules(db, plan_version)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/schedule-versions/{schedule_version}/activate",
    response_model=ScheduleOut,
)
def activate_schedule_route(
    plan_version: str,
    schedule_version: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.activate_schedule(
            db, plan_version=plan_version, schedule_version=schedule_version
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.CapacityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 冲突预览、名单确认与影响查询
# ---------------------------------------------------------------------------


@router.post(
    "/plans/{plan_version}/conflict-preview",
    response_model=ConflictPreviewOut,
)
def post_conflict_preview(
    plan_version: str, body: ConflictPreviewIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.preview_conflicts(
            db,
            plan_version=plan_version,
            schedule_version=body.schedule_version,
            what_if_capacity=body.what_if_capacity,
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.CapacityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/roster-confirmations",
    response_model=RosterConfirmOut,
    status_code=status.HTTP_201_CREATED,
)
def post_roster_confirm(
    plan_version: str, body: RosterConfirmIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.confirm_roster(
            db,
            plan_version=plan_version,
            event_id=body.event_id,
            activity_id=body.activity_id,
            student_ids=body.student_ids,
            actor_id=body.actor_id,
            partial=body.partial,
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.CapacityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/impact",
    response_model=ImpactQueryOut,
)
def get_impact(
    plan_version: str,
    student_id: str | None = None,
    activity_id: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.impact_query(
            db,
            plan_version=plan_version,
            student_id=student_id,
            activity_id=activity_id,
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/schedule-versions/{schedule_version}/impact/{other_version}",
    response_model=ScheduleImpactOut,
)
def get_schedule_impact(
    plan_version: str,
    schedule_version: str,
    other_version: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.schedule_version_impact(
            db,
            plan_version=plan_version,
            old_version=schedule_version,
            new_version=other_version,
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.CapacityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
