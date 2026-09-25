"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class MentorConfirmPayload(BaseModel):
    checkin_event_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal[
        "checkin", "mentor_confirm", "leave_correction", "roster_confirm"
    ]
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventOut(BaseModel):
    event_id: str
    plan_version: str
    event_type: str
    student_id: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    held_seconds: int = 0
    released_seconds: int = 0
    academic_days: list[dict[str, Any]]
    released_academic_days: list[dict[str, Any]] = []


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str


class StudentProgressOut(BaseModel):
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    held_seconds: int = 0
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    held_lesson_units: int = 0
    meets_requirement: bool
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]
    schedule_version: str | None = None
    capacity_versions: dict[str, str] = {}
    overages: list[dict[str, Any]] = []


class FreezeIn(BaseModel):
    pass


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    student_changes: list[dict[str, Any]]
    students_affected: int


class VenueIn(BaseModel):
    venue_id: str = Field(..., min_length=1, max_length=128)
    name: str = Field("", max_length=256)


class VenueOut(BaseModel):
    venue_id: str
    name: str


class CapacityIn(BaseModel):
    capacity_version: str = Field(..., min_length=1, max_length=128)
    capacity: int = Field(..., ge=0)
    effective_from: datetime

    @field_validator("effective_from")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("effective_from must be timezone-aware (RFC 3339)")
        return v


class CapacityOut(BaseModel):
    venue_id: str
    capacity_version: str
    capacity: int
    effective_from_utc: str


class ScheduleEntryIn(BaseModel):
    activity_id: str = Field(..., min_length=1, max_length=128)
    venue_id: str = Field(..., min_length=1, max_length=128)
    start_at: datetime
    end_at: datetime

    @field_validator("start_at", "end_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class ScheduleIn(BaseModel):
    schedule_version: str = Field(..., min_length=1, max_length=128)
    entries: list[ScheduleEntryIn]
    set_active: bool = True


class ScheduleActivateIn(BaseModel):
    pass


class ScheduleEntryOut(BaseModel):
    activity_id: str
    venue_id: str
    start_at_utc: str
    end_at_utc: str


class ScheduleOut(BaseModel):
    plan_version: str
    schedule_version: str
    active: bool
    entries: list[ScheduleEntryOut]


class ConflictPreviewIn(BaseModel):
    schedule_version: str | None = None
    # what-if：按场地临时假设容量，不落库
    what_if_capacity: dict[str, int] | None = None


class ConflictPreviewOut(BaseModel):
    plan_version: str
    schedule_version: str
    what_if_capacity: dict[str, int]
    activities_total: int
    activities_with_overage: int
    overages: list[dict[str, Any]]
    held_lesson_units_by_student: dict[str, int]


class RosterConfirmIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    activity_id: str = Field(..., min_length=1, max_length=128)
    student_ids: list[str] = Field(..., min_length=1)
    actor_id: str = Field(..., min_length=1, max_length=128)
    partial: bool = False


class RosterConfirmOut(BaseModel):
    plan_version: str
    event_id: str
    activity_id: str
    confirmed_student_ids: list[str]
    partial: bool
    released_student_ids: list[str]
    still_held_student_ids: list[str]


class ImpactQueryOut(BaseModel):
    plan_version: str
    schedule_version: str | None
    activities_with_overage: list[dict[str, Any]]
    students_with_held_hours: list[dict[str, Any]]


class ScheduleImpactOut(BaseModel):
    plan_version: str
    old_schedule_version: str
    new_schedule_version: str
    activities_affected: int
    activities_unaffected: int
    affected_activities: list[dict[str, Any]]
    students_affected: int
    students: list[dict[str, Any]]
