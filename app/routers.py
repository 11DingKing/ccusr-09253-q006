"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import capacity_services, services
from .db import get_db
from .schemas import (
    CapacityImpactIn,
    CapacityImpactOut,
    CapacityPreviewIn,
    CapacityPreviewOut,
    CapacityVersionIn,
    CapacityVersionOut,
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    PlanIn,
    PlanOut,
    RosterConfirmIn,
    RosterConfirmOut,
    ScheduleVersionIn,
    ScheduleVersionOut,
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
# 场地容量联动：配置 / 冲突预览 / 名单确认 / 影响查询
# ---------------------------------------------------------------------------


def _capacity_error(exc: Exception) -> HTTPException:
    if isinstance(exc, services.PlanNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


@router.post(
    "/plans/{plan_version}/venues",
    response_model=VenueOut,
    status_code=status.HTTP_201_CREATED,
)
def post_venue(
    plan_version: str, body: VenueIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return capacity_services.create_venue(
            db,
            plan_version=plan_version,
            venue_id=body.venue_id,
            name=body.name,
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put(
    "/plans/{plan_version}/venues/{venue_id}/capacity-versions",
    response_model=CapacityVersionOut,
)
def put_capacity_version_route(
    plan_version: str,
    venue_id: str,
    body: CapacityVersionIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return capacity_services.add_capacity_version(
            db,
            plan_version=plan_version,
            venue_id=venue_id,
            version=body.version,
            capacity=body.capacity,
            note=body.note,
        )
    except (services.PlanNotFoundError, capacity_services.CapacityConfigError) as exc:
        raise _capacity_error(exc) from exc


@router.put(
    "/plans/{plan_version}/schedule-versions",
    response_model=ScheduleVersionOut,
)
def put_schedule_version_route(
    plan_version: str,
    body: ScheduleVersionIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return capacity_services.add_schedule_version(
            db,
            plan_version=plan_version,
            schedule_id=body.schedule_id,
            version=body.version,
            activity_id=body.activity_id,
            venue_id=body.venue_id,
            start_at=body.start_at,
            end_at=body.end_at,
            note=body.note,
        )
    except (services.PlanNotFoundError, capacity_services.CapacityConfigError) as exc:
        raise _capacity_error(exc) from exc


@router.post(
    "/plans/{plan_version}/capacity/preview",
    response_model=CapacityPreviewOut,
)
def post_capacity_preview(
    plan_version: str,
    body: CapacityPreviewIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return capacity_services.preview_conflicts(
            db,
            plan_version,
            schedule_versions=body.schedule_versions or None,
            venue_versions=body.venue_versions or None,
            activity_ids=body.activity_ids,
        )
    except (services.PlanNotFoundError, capacity_services.CapacityConfigError) as exc:
        raise _capacity_error(exc) from exc


@router.post(
    "/plans/{plan_version}/schedules/{schedule_id}/roster-confirmations",
    response_model=RosterConfirmOut,
    status_code=status.HTTP_201_CREATED,
)
def post_roster_confirmation(
    plan_version: str,
    schedule_id: str,
    body: RosterConfirmIn,
    db: Session = Depends(get_db),
) -> Any:
    if body.schedule_id != schedule_id:
        raise HTTPException(
            status_code=422, detail="schedule_id in path and body must match"
        )
    try:
        return capacity_services.confirm_roster(
            db,
            plan_version,
            schedule_id=schedule_id,
            released=body.released,
            excluded=body.excluded,
            reason=body.reason,
        )
    except (services.PlanNotFoundError, capacity_services.CapacityConfigError) as exc:
        raise _capacity_error(exc) from exc


@router.post(
    "/plans/{plan_version}/capacity/impact",
    response_model=CapacityImpactOut,
)
def post_capacity_impact(
    plan_version: str,
    body: CapacityImpactIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return capacity_services.query_impact(
            db,
            plan_version,
            schedule_versions=body.schedule_versions or None,
            venue_versions=body.venue_versions or None,
        )
    except (services.PlanNotFoundError, capacity_services.CapacityConfigError) as exc:
        raise _capacity_error(exc) from exc
