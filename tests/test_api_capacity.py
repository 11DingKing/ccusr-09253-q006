"""场地容量联动 API 测试。"""

from __future__ import annotations

import threading

from tests.conftest import SHANGHAI_PLAN, TestSessionLocal


def _setup_plan(client, capacity=2):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text
    pv = SHANGHAI_PLAN["plan_version"]
    assert client.post(f"/api/plans/{pv}/venues", json={"venue_id": "V1", "name": "lab"}).status_code == 201
    r = client.put(
        f"/api/plans/{pv}/venues/V1/capacity-versions",
        json={"version": "v1", "capacity": capacity},
    )
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True
    r = client.put(
        f"/api/plans/{pv}/schedule-versions",
        json={
            "schedule_id": "S-01",
            "version": "v1",
            "activity_id": "A1",
            "venue_id": "V1",
            "start_at": "2024-03-15T08:00:00+08:00",
            "end_at": "2024-03-15T12:00:00+08:00",
        },
    )
    assert r.status_code == 200, r.text
    return pv


def _checkin(eid, student, start, end, activity="A1"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": activity,
            "activity_type": "regular",
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _three_students(client, pv, *, start="2024-03-15T08:00:00+08:00", end="2024-03-15T12:00:00+08:00"):
    events = [
        _checkin("E-01", "S1", start, end),
        _checkin("E-02", "S2", start, end),
        _checkin("E-03", "S3", start, end),
    ]
    r = client.post(f"/api/plans/{pv}/events", json={"events": events})
    assert r.status_code == 201, r.text


def test_capacity_versions_are_immutable(client):
    pv = _setup_plan(client, capacity=2)
    # 同版本号不同容量 -> 409，必须发布新版本。
    r = client.put(
        f"/api/plans/{pv}/venues/V1/capacity-versions",
        json={"version": "v1", "capacity": 3},
    )
    assert r.status_code == 409
    r = client.put(
        f"/api/plans/{pv}/venues/V1/capacity-versions",
        json={"version": "v2", "capacity": 3},
    )
    assert r.status_code == 200
    assert r.json()["version"] == "v2"
    # 重复写入同版本同内容 -> 幂等 200，created=False。
    again = client.put(
        f"/api/plans/{pv}/venues/V1/capacity-versions",
        json={"version": "v2", "capacity": 3},
    )
    assert again.json()["created"] is False


def test_capacity_on_unknown_venue_is_rejected(client):
    pv = _setup_plan(client)
    r = client.put(
        f"/api/plans/{pv}/schedule-versions",
        json={
            "schedule_id": "S-X",
            "version": "v1",
            "activity_id": "A1",
            "venue_id": "NOPE",
            "start_at": "2024-03-15T08:00:00+08:00",
            "end_at": "2024-03-15T10:00:00+08:00",
        },
    )
    assert r.status_code == 409


def test_preview_marks_over_capacity_and_holds_hours(client):
    pv = _setup_plan(client, capacity=2)
    _three_students(client, pv)

    preview = client.post(f"/api/plans/{pv}/capacity/preview", json={}).json()
    assert preview["held_seconds"] == 4 * 3600
    assert preview["held_checkins"] == 1
    assert preview["affected_students"] == ["S3"]
    segment = preview["excess_segments"][0]
    assert segment["venue_id"] == "V1"
    assert segment["capacity"] == 2
    assert segment["headcount"] == 3
    assert segment["held_student_ids"] == ["S3"]
    assert segment["seconds"] == 4 * 3600

    s3 = client.get(f"/api/plans/{pv}/students/S3/progress").json()
    assert s3["total_seconds"] == 0
    assert s3["held_seconds"] == 4 * 3600
    assert s3["capacity_holds"][0]["state"] == "held"
    assert s3["capacity_holds"][0]["schedule_id"] == "S-01"
    s1 = client.get(f"/api/plans/{pv}/students/S1/progress").json()
    assert s1["total_seconds"] == 4 * 3600

    snap = client.get(f"/api/plans/{pv}/snapshot").json()
    assert snap["venue_versions"] == {"V1": "v1"}
    assert snap["schedule_versions"] == {"S-01": "v1"}
    assert len(snap["overruns"]) == 1


def test_cross_midnight_checkin_is_held_across_both_days(client):
    pv = _setup_plan(client, capacity=1)
    # 22:00 -> 次日 02:00 的跨午夜排期。
    client.put(
        f"/api/plans/{pv}/schedule-versions",
        json={
            "schedule_id": "S-NIGHT",
            "version": "v1",
            "activity_id": "A2",
            "venue_id": "V1",
            "start_at": "2024-03-15T22:00:00+08:00",
            "end_at": "2024-03-16T02:00:00+08:00",
        },
    )
    # 旧的 S-01 (A1) 不影响夜间场次。
    events = [
        _checkin("E-10", "S1", "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00", activity="A2"),
        _checkin("E-11", "S2", "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00", activity="A2"),
    ]
    client.post(f"/api/plans/{pv}/events", json={"events": events})

    preview = client.post(f"/api/plans/{pv}/capacity/preview", json={}).json()
    assert preview["held_seconds"] == 4 * 3600
    segment = preview["excess_segments"][0]
    assert segment["start_utc"] == "2024-03-15T14:00:00Z"
    assert segment["end_utc"] == "2024-03-15T18:00:00Z"

    s1 = client.get(f"/api/plans/{pv}/students/S1/progress").json()
    assert {d["academic_day"]: d["seconds"] for d in s1["daily"]} == {
        "2024-03-15": 2 * 3600,
        "2024-03-16": 2 * 3600,
    }
    s2 = client.get(f"/api/plans/{pv}/students/S2/progress").json()
    assert s2["held_seconds"] == 4 * 3600
    assert s2["daily"] == []


def test_roster_confirmation_partial_then_full_releases_hours(client):
    pv = _setup_plan(client, capacity=2)
    _three_students(client, pv)

    # 部分确认：仅释放 S3，S3 凭管理员顺序插队入座，S2 暂缓。
    r = client.post(
        f"/api/plans/{pv}/schedules/S-01/roster-confirmations",
        json={"schedule_id": "S-01", "released": ["S3"], "reason": "verified"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["event_id"] == "RC-000001"
    assert body["newly_seated_seconds"] == 4 * 3600
    assert body["still_held_seconds"] == 4 * 3600

    s3 = client.get(f"/api/plans/{pv}/students/S3/progress").json()
    assert s3["total_seconds"] == 4 * 3600
    s2 = client.get(f"/api/plans/{pv}/students/S2/progress").json()
    assert s2["total_seconds"] == 0
    assert s2["held_seconds"] == 4 * 3600

    # 排除 S1：S1 学时不再计入（既不暂缓也不确认），S2 获得空出的座位。
    r = client.post(
        f"/api/plans/{pv}/schedules/S-01/roster-confirmations",
        json={"schedule_id": "S-01", "excluded": ["S1"], "reason": "not present"},
    )
    assert r.status_code == 201, r.text
    s1 = client.get(f"/api/plans/{pv}/students/S1/progress").json()
    assert s1["excluded_seconds"] == 4 * 3600
    assert s1["total_seconds"] == 0
    s2 = client.get(f"/api/plans/{pv}/students/S2/progress").json()
    assert s2["total_seconds"] == 4 * 3600
    s3 = client.get(f"/api/plans/{pv}/students/S3/progress").json()
    assert s3["total_seconds"] == 4 * 3600

    # 预览中不再有暂缓，只剩排除记录。
    preview = client.post(f"/api/plans/{pv}/capacity/preview", json={}).json()
    assert preview["held_seconds"] == 0
    assert preview["excluded_seconds"] == 4 * 3600
    assert all(h["state"] == "excluded" for h in preview["holds"])


def test_roster_confirmation_rejects_overlap_and_unknown_schedule(client):
    pv = _setup_plan(client)
    r = client.post(
        f"/api/plans/{pv}/schedules/S-01/roster-confirmations",
        json={"schedule_id": "S-01", "released": ["S1"], "excluded": ["S1"]},
    )
    assert r.status_code == 422
    r = client.post(
        f"/api/plans/{pv}/schedules/NOPE/roster-confirmations",
        json={"schedule_id": "NOPE", "released": ["S1"]},
    )
    assert r.status_code == 409


def test_concurrent_roster_confirmations_get_distinct_event_ids(client):
    pv = _setup_plan(client, capacity=10)
    _three_students(client, pv)

    from app import capacity_services

    errors: list[Exception] = []

    def _confirm():
        session = TestSessionLocal()
        try:
            capacity_services.confirm_roster(
                session,
                pv,
                schedule_id="S-01",
                released=[],
                excluded=[],
                reason="concurrent",
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=_confirm) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    # 四次并发确认必须产生 4 个不同的、连续的 roster 事件 ID。
    from app.models import Event as EventModel
    from sqlalchemy import select

    session = TestSessionLocal()
    try:
        rows = session.execute(
            select(EventModel).where(EventModel.event_type == "roster_confirm")
        ).scalars().all()
    finally:
        session.close()
    ids = sorted(r.event_id for r in rows)
    assert ids == ["RC-000001", "RC-000002", "RC-000003", "RC-000004"]

    preview = client.post(f"/api/plans/{pv}/capacity/preview", json={}).json()
    assert preview["held_seconds"] == 0


def test_schedule_correction_only_recomputes_affected_freeze_unchanged(client):
    pv = _setup_plan(client, capacity=2)
    _three_students(client, pv)
    # 先冻结：S3 学时为 0。
    f1 = client.post(f"/api/plans/{pv}/freezes/F-01", json={}).json()
    frozen_s3 = next(s for s in f1["students"] if s["student_id"] == "S3")
    assert frozen_s3["total_seconds"] == 0
    assert f1["schedule_versions"] == {"S-01": "v1"}

    # 排期更正：活动 A1 改到大容量场地 V2，发布 S-01 v2。
    client.post(f"/api/plans/{pv}/venues", json={"venue_id": "V2", "name": "hall"})
    client.put(
        f"/api/plans/{pv}/venues/V2/capacity-versions",
        json={"version": "v1", "capacity": 50},
    )
    r = client.put(
        f"/api/plans/{pv}/schedule-versions",
        json={
            "schedule_id": "S-01",
            "version": "v2",
            "activity_id": "A1",
            "venue_id": "V2",
            "start_at": "2024-03-15T08:00:00+08:00",
            "end_at": "2024-03-15T12:00:00+08:00",
        },
    )
    assert r.status_code == 200

    # 实时快照只按 v2 重算，超额消失，S3 学时释放。
    s3 = client.get(f"/api/plans/{pv}/students/S3/progress").json()
    assert s3["total_seconds"] == 4 * 3600
    assert s3["held_seconds"] == 0

    # 已冻结快照保持不变（仍按 v1/V1 记录）。
    frozen = client.get(f"/api/plans/{pv}/freezes/F-01").json()
    frozen_s3 = next(s for s in frozen["students"] if s["student_id"] == "S3")
    assert frozen_s3["total_seconds"] == 0
    assert frozen["schedule_versions"] == {"S-01": "v1"}

    # 影响查询：对比当前(v2)与钉住 v1，受影响活动只有 A1。
    impact = client.post(
        f"/api/plans/{pv}/capacity/impact",
        json={"schedule_versions": {"S-01": "v1"}},
    ).json()
    assert impact["changed_schedule_ids"] == ["S-01"]
    assert impact["affected_activities"] == ["A1"]
    assert impact["affected_students"] == ["S1", "S2", "S3"]
    assert set(impact["affected_checkins"]) == {"E-01", "E-02", "E-03"}
    assert impact["current"]["held_seconds"] == 0  # 当前 v2 已无超额
    assert impact["proposed"]["held_seconds"] == 4 * 3600  # 切回 v1 有超额


def test_unrelated_activity_is_not_recomputed_on_correction(client):
    pv = _setup_plan(client, capacity=2)
    # 另一个活动 A9 同时段在同一场地，容量紧张：两人都签 A9，无超额。
    client.put(
        f"/api/plans/{pv}/schedule-versions",
        json={
            "schedule_id": "S-09",
            "version": "v1",
            "activity_id": "A9",
            "venue_id": "V1",
            "start_at": "2024-03-15T14:00:00+08:00",
            "end_at": "2024-03-15T16:00:00+08:00",
        },
    )
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin("E-01", "S1", "2024-03-15T14:00:00+08:00", "2024-03-15T16:00:00+08:00", activity="A9"),
                _checkin("E-02", "S2", "2024-03-15T14:00:00+08:00", "2024-03-15T16:00:00+08:00", activity="A9"),
            ]
        },
    )
    # 更正不相关的 A1 排期（改场地）。
    client.post(f"/api/plans/{pv}/venues", json={"venue_id": "V2"})
    client.put(
        f"/api/plans/{pv}/venues/V2/capacity-versions",
        json={"version": "v1", "capacity": 99},
    )
    client.put(
        f"/api/plans/{pv}/schedule-versions",
        json={
            "schedule_id": "S-01",
            "version": "v2",
            "activity_id": "A1",
            "venue_id": "V2",
            "start_at": "2024-03-15T08:00:00+08:00",
            "end_at": "2024-03-15T12:00:00+08:00",
        },
    )
    impact = client.post(
        f"/api/plans/{pv}/capacity/impact",
        json={"schedule_versions": {"S-01": "v1"}},
    ).json()
    assert impact["affected_activities"] == ["A1"]
    # A9 场次在 V1 的 14:00-16:00，与更正窗口 08:00-12:00 不相交，不受波及。
    assert impact["affected_checkins"] == []
