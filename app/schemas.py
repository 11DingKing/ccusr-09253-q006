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
    academic_days: list[dict[str, Any]]


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str


class StudentProgressOut(BaseModel):
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    held_seconds: int = 0
    excluded_seconds: int = 0
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]
    capacity_holds: list[dict[str, Any]] = Field(default_factory=list)


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]
    schedule_versions: dict[str, str] | None = None
    venue_versions: dict[str, str] | None = None
    overruns: list[dict[str, Any]] | None = None


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


# ---------------------------------------------------------------------------
# 场地容量联动
# ---------------------------------------------------------------------------


class VenueIn(BaseModel):
    venue_id: str = Field(..., min_length=1, max_length=128)
    name: str = Field("", max_length=256)


class VenueOut(BaseModel):
    plan_version: str
    venue_id: str
    name: str


class CapacityVersionIn(BaseModel):
    version: str = Field(..., min_length=1, max_length=128)
    capacity: int = Field(..., ge=0)
    note: str = Field("", max_length=512)


class CapacityVersionOut(BaseModel):
    plan_version: str
    venue_id: str
    version: str
    capacity: int
    note: str
    created: bool


class ScheduleVersionIn(BaseModel):
    schedule_id: str = Field(..., min_length=1, max_length=128)
    version: str = Field(..., min_length=1, max_length=128)
    activity_id: str = Field(..., min_length=1, max_length=128)
    venue_id: str = Field(..., min_length=1, max_length=128)
    start_at: datetime
    end_at: datetime
    note: str = Field("", max_length=512)

    @model_validator(mode="after")
    def _check_order(self) -> "ScheduleVersionIn":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self

    @field_validator("start_at", "end_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class ScheduleVersionOut(BaseModel):
    plan_version: str
    schedule_id: str
    version: str
    activity_id: str
    venue_id: str
    start_at: datetime
    end_at: datetime
    note: str
    created: bool


class CapacityPreviewIn(BaseModel):
    # 缺省使用各场地最新容量版本、各排期最新版本。
    schedule_versions: dict[str, str] = Field(
        default_factory=dict,
        description="按 schedule_id 钉住的排期版本号，用于排期版本切换预览",
    )
    venue_versions: dict[str, str] = Field(
        default_factory=dict,
        description="按 venue_id 钉住的容量版本号",
    )
    activity_ids: list[str] | None = Field(
        None, description="只预览这些活动；缺省为全部"
    )


class ExcessSegmentOut(BaseModel):
    venue_id: str
    start_utc: str
    end_utc: str
    seconds: int
    capacity: int
    headcount: int
    schedule_ids: list[str]
    held_student_ids: list[str]
    excluded_student_ids: list[str]


class CapacityHoldOut(BaseModel):
    checkin_event_id: str
    activity_id: str
    schedule_id: str
    venue_id: str
    start_utc: str
    end_utc: str
    seconds: int
    state: str
    decision_event_id: str | None


class CapacityPreviewOut(BaseModel):
    plan_version: str
    schedule_versions: dict[str, str]
    venue_versions: dict[str, str]
    excess_segments: list[ExcessSegmentOut]
    held_seconds: int
    excluded_seconds: int = 0
    held_checkins: int
    affected_students: list[str]
    holds: list[CapacityHoldOut]


class RosterConfirmIn(BaseModel):
    schedule_id: str = Field(..., min_length=1, max_length=128)
    released: list[str] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)
    reason: str = Field("", max_length=512)

    @model_validator(mode="after")
    def _disjoint(self) -> "RosterConfirmIn":
        overlap = set(self.released) & set(self.excluded)
        if overlap:
            raise ValueError(f"student cannot be both released and excluded: {sorted(overlap)}")
        if len(self.released) != len(set(self.released)):
            raise ValueError("released contains duplicate students")
        if len(self.excluded) != len(set(self.excluded)):
            raise ValueError("excluded contains duplicate students")
        return self


class RosterConfirmOut(BaseModel):
    event_id: str
    plan_version: str
    schedule_id: str
    released: list[str]
    excluded: list[str]
    reason: str
    # 确认后重放的即时效果
    newly_seated_seconds: int
    still_held_seconds: int


class CapacityImpactIn(BaseModel):
    schedule_versions: dict[str, str] = Field(default_factory=dict)
    venue_versions: dict[str, str] = Field(default_factory=dict)


class CapacityImpactOut(BaseModel):
    plan_version: str
    changed_schedule_ids: list[str]
    changed_capacity_venues: list[str]
    affected_activities: list[str]
    affected_students: list[str]
    affected_checkins: list[str]
    current: dict[str, Any]
    proposed: dict[str, Any]
