"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from .capacity import ActivitySchedule, VenueCapacity
from .replay import (
    CheckinRecord,
    Event,
    ReplayState,
    StudentProgress,
    explain_checkin,
    replay,
)


@dataclass
class Snapshot:
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_version": self.plan_version,
            "freeze_id": self.freeze_id,
            "timezone": self.timezone,
            "required_seconds": self.required_seconds,
            "generated_at": self.generated_at,
            "event_cutoff_id": self.event_cutoff_id,
            "students": self.students,
            "schedule_versions": self.schedule_versions,
            "venue_versions": self.venue_versions,
            "overruns": self.overruns,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Snapshot":
        return cls(
            plan_version=data["plan_version"],
            freeze_id=data.get("freeze_id"),
            timezone=data["timezone"],
            required_seconds=data["required_seconds"],
            generated_at=data["generated_at"],
            event_cutoff_id=data.get("event_cutoff_id"),
            students=list(data.get("students", [])),
            schedule_versions=data.get("schedule_versions"),
            venue_versions=data.get("venue_versions"),
            overruns=data.get("overruns"),
        )


def _student_to_dict(progress: StudentProgress, tz_name: str) -> dict[str, Any]:
    return {
        "student_id": progress.student_id,
        "confirmed_seconds": progress.confirmed_seconds,
        "pending_seconds": progress.pending_seconds,
        "adjustment_seconds": progress.adjustment_seconds,
        "total_seconds": progress.total_seconds,
        "lesson_units": progress.lesson_units,
        "pending_lesson_units": progress.pending_lesson_units,
        "meets_requirement": progress.meets_requirement,
        "held_seconds": progress.held_seconds,
        "excluded_seconds": progress.excluded_seconds,
        "daily": [
            {"academic_day": d.academic_day, "seconds": d.seconds}
            for d in progress.daily
        ],
        "checkins": [explain_checkin(c, tz_name) for c in progress.checkins],
        "adjustments": [
            {
                "event_id": a.event_id,
                "seconds": a.seconds,
                "reason": a.reason,
            }
            for a in progress.adjustments
        ],
        "capacity_holds": [
            {
                "checkin_event_id": h.checkin_event_id,
                "activity_id": h.activity_id,
                "schedule_id": h.schedule_id,
                "venue_id": h.venue_id,
                "start_utc": h.start_utc.isoformat().replace("+00:00", "Z"),
                "end_utc": h.end_utc.isoformat().replace("+00:00", "Z"),
                "seconds": h.seconds,
                "state": h.state,
                "decision_event_id": h.decision_event_id,
            }
            for h in progress.capacity_holds
        ],
    }


def _overrun_to_dict(window: Any) -> dict[str, Any]:
    return {
        "venue_id": window.venue_id,
        "start_utc": window.start_utc.isoformat().replace("+00:00", "Z"),
        "end_utc": window.end_utc.isoformat().replace("+00:00", "Z"),
        "seconds": int((window.end_utc - window.start_utc).total_seconds()),
        "capacity": window.capacity,
        "headcount": window.headcount,
        "schedule_ids": list(window.schedule_ids),
        "held_student_ids": list(window.held_student_ids),
        "excluded_student_ids": list(window.excluded_student_ids),
    }


def build_snapshot(
    events: list[Event],
    *,
    plan_version: str,
    timezone_name: str,
    required_seconds: int,
    freeze_id: str | None = None,
    event_cutoff_id: str | None = None,
    generated_at: datetime | None = None,
    schedules: Sequence[ActivitySchedule] | None = None,
    capacities: dict[str, VenueCapacity] | None = None,
) -> Snapshot:
    """执行确定性的业务处理。"""
    state: ReplayState = replay(
        events,
        plan_version=plan_version,
        timezone_name=timezone_name,
        required_seconds=required_seconds,
        up_to_event_id=event_cutoff_id,
        schedules=schedules,
        capacities=capacities,
    )
    if generated_at is None:
        generated_at = datetime.now(timezone.utc)
    generated_at = generated_at.astimezone(timezone.utc)

    students = [
        _student_to_dict(state.students[sid], timezone_name)
        for sid in sorted(state.students)
    ]

    schedule_versions = (
        {s.schedule_id: s.version for s in schedules} if schedules is not None else None
    )
    venue_versions = (
        {vid: cap.version for vid, cap in capacities.items()}
        if capacities is not None
        else None
    )
    overruns = (
        [_overrun_to_dict(w) for w in state.overruns]
        if capacities is not None
        else None
    )

    return Snapshot(
        plan_version=plan_version,
        freeze_id=freeze_id,
        timezone=timezone_name,
        required_seconds=required_seconds,
        generated_at=generated_at.isoformat().replace("+00:00", "Z"),
        event_cutoff_id=event_cutoff_id,
        students=students,
        schedule_versions=schedule_versions,
        venue_versions=venue_versions,
        overruns=overruns,
    )


def _index_students(snapshot: Snapshot) -> dict[str, dict[str, Any]]:
    return {s["student_id"]: s for s in snapshot.students}


def diff_snapshots(old: Snapshot, new: Snapshot) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    old_map = _index_students(old)
    new_map = _index_students(new)
    all_ids = sorted(set(old_map) | set(new_map))

    student_changes: list[dict[str, Any]] = []
    for sid in all_ids:
        before = old_map.get(sid)
        after = new_map.get(sid)
        if before is None and after is not None:
            student_changes.append(
                {
                    "student_id": sid,
                    "change_type": "added",
                    "before": None,
                    "after": {
                        "total_seconds": after["total_seconds"],
                        "lesson_units": after["lesson_units"],
                        "meets_requirement": after["meets_requirement"],
                    },
                }
            )
            continue
        if after is None and before is not None:
            student_changes.append(
                {
                    "student_id": sid,
                    "change_type": "removed",
                    "before": {
                        "total_seconds": before["total_seconds"],
                        "lesson_units": before["lesson_units"],
                        "meets_requirement": before["meets_requirement"],
                    },
                    "after": None,
                }
            )
            continue

        assert before is not None and after is not None
        fields = (
            "confirmed_seconds",
            "pending_seconds",
            "adjustment_seconds",
            "total_seconds",
            "lesson_units",
            "pending_lesson_units",
            "meets_requirement",
            "held_seconds",
            "excluded_seconds",
        )
        changed_fields = {}
        for field_name in fields:
            if before.get(field_name) != after.get(field_name):
                changed_fields[field_name] = {
                    "before": before.get(field_name),
                    "after": after.get(field_name),
                }
        if changed_fields:
            student_changes.append(
                {
                    "student_id": sid,
                    "change_type": "modified",
                    "fields": changed_fields,
                }
            )

    return {
        "plan_version": old.plan_version,
        "old_freeze_id": old.freeze_id,
        "new_freeze_id": new.freeze_id,
        "old_generated_at": old.generated_at,
        "new_generated_at": new.generated_at,
        "old_event_cutoff_id": old.event_cutoff_id,
        "new_event_cutoff_id": new.event_cutoff_id,
        "student_changes": student_changes,
        "students_affected": len(student_changes),
    }


def explain_student(
    snapshot: Snapshot, student_id: str
) -> dict[str, Any] | None:
    """执行确定性的业务处理。"""
    for student in snapshot.students:
        if student["student_id"] == student_id:
            return student
    return None
