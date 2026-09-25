"""场地容量、排期版本、名单确认与影响查询的 API 测试。"""

from __future__ import annotations

import threading

from app import services
from tests.conftest import TestSessionLocal
from tests.conftest import SHANGHAI_PLAN

PLAN = SHANGHAI_PLAN["plan_version"]
TZ = "+08:00"


def _setup_plan(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text


def _setup_venue(client, venue_id="V-AUD", cap=2):
    assert client.post("/api/venues", json={"venue_id": venue_id, "name": "礼堂"}).status_code == 201
    resp = client.post(
        f"/api/venues/{venue_id}/capacity-versions",
        json={
            "capacity_version": "C-INITIAL",
            "capacity": cap,
            "effective_from": "2000-01-01T00:00:00+00:00",
        },
    )
    assert resp.status_code == 201, resp.text


def _checkin_event(eid, sid, start, end, activity="ACT-1"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": sid,
        "payload": {
            "activity_id": activity,
            "activity_type": "regular",
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _post_checkins(client, events):
    resp = client.post(f"/api/plans/{PLAN}/events", json={"events": events})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_capacity_configuration_and_conflict_preview_flags_overage(client):
    _setup_plan(client)
    _setup_venue(client)

    # 排期：北京 22:00 - 次日 02:00（跨午夜），容量 2。
    resp = client.post(
        f"/api/plans/{PLAN}/schedule-versions",
        json={
            "schedule_version": "SV-1",
            "entries": [
                {
                    "activity_id": "ACT-1",
                    "venue_id": "V-AUD",
                    "start_at": f"2024-03-15T22:00:00{TZ}",
                    "end_at": f"2024-03-16T02:00:00{TZ}",
                }
            ],
            "set_active": True,
        },
    )
    assert resp.status_code == 201, resp.text

    # 并发签到：三名学生完全同一时间签到。
    _post_checkins(
        client,
        [
            _checkin_event(
                "E-01", "S1",
                f"2024-03-15T22:00:00{TZ}", f"2024-03-16T02:00:00{TZ}",
            ),
            _checkin_event(
                "E-02", "S2",
                f"2024-03-15T22:00:00{TZ}", f"2024-03-16T02:00:00{TZ}",
            ),
            _checkin_event(
                "E-03", "S3",
                f"2024-03-15T22:00:00{TZ}", f"2024-03-16T02:00:00{TZ}",
            ),
        ],
    )

    preview = client.post(
        f"/api/plans/{PLAN}/conflict-preview", json={}
    ).json()
    assert preview["activities_with_overage"] == 1
    overage = preview["overages"][0]
    assert overage["venue_id"] == "V-AUD"
    assert overage["peak_occupancy"] == 3
    assert overage["held_student_ids"] == ["S3"]
    assert overage["held_event_ids"] == ["E-03"]
    assert preview["held_lesson_units_by_student"] == {"S3": 5}  # 4h // 45min

    # 学员进度：S3 的 4 学时全部暂缓。
    progress_s3 = client.get(
        f"/api/plans/{PLAN}/students/S3/progress"
    ).json()
    assert progress_s3["held_seconds"] == 4 * 3600
    assert progress_s3["confirmed_seconds"] == 0
    assert progress_s3["total_seconds"] == 0
    assert progress_s3["checkins"][0]["status"] == "HELD"

    # what-if 容量调到 3 后冲突消失（不落库）。
    what_if = client.post(
        f"/api/plans/{PLAN}/conflict-preview",
        json={"what_if_capacity": {"V-AUD": 3}},
    ).json()
    assert what_if["activities_with_overage"] == 0
    # 实际状态未变。
    assert (
        client.get(f"/api/plans/{PLAN}/students/S3/progress").json()["held_seconds"]
        == 4 * 3600
    )


def test_partial_roster_confirmation_releases_in_deterministic_order(client):
    _setup_plan(client)
    _setup_venue(client)
    client.post(
        f"/api/plans/{PLAN}/schedule-versions",
        json={
            "schedule_version": "SV-1",
            "entries": [
                {
                    "activity_id": "ACT-1",
                    "venue_id": "V-AUD",
                    "start_at": "2024-03-15T08:00:00+00:00",
                    "end_at": "2024-03-15T12:00:00+00:00",
                }
            ],
        },
    )
    _post_checkins(
        client,
        [
            _checkin_event(
                "E-01", "S1",
                "2024-03-15T08:00:00+00:00", "2024-03-15T12:00:00+00:00",
            ),
            _checkin_event(
                "E-02", "S2",
                "2024-03-15T08:00:00+00:00", "2024-03-15T12:00:00+00:00",
            ),
            _checkin_event(
                "E-03", "S3",
                "2024-03-15T08:00:00+00:00", "2024-03-15T12:00:00+00:00",
            ),
        ],
    )

    # 部分确认：只确认 S3。
    resp = client.post(
        f"/api/plans/{PLAN}/roster-confirmations",
        json={
            "event_id": "E-10",
            "activity_id": "ACT-1",
            "student_ids": ["S3"],
            "actor_id": "admin-7",
            "partial": True,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["released_student_ids"] == ["S3"]
    assert body["still_held_student_ids"] == ["S2"]

    s3 = client.get(f"/api/plans/{PLAN}/students/S3/progress").json()
    assert s3["total_seconds"] == 4 * 3600
    s2 = client.get(f"/api/plans/{PLAN}/students/S2/progress").json()
    assert s2["held_seconds"] == 4 * 3600

    # 确认事件幂等：重复 event_id 被拒绝。
    dup = client.post(
        f"/api/plans/{PLAN}/roster-confirmations",
        json={
            "event_id": "E-10",
            "activity_id": "ACT-1",
            "student_ids": ["S3"],
            "actor_id": "admin-7",
            "partial": True,
        },
    )
    assert dup.status_code == 400

    # 影响查询只返回有暂缓学时的学员。
    impact = client.get(f"/api/plans/{PLAN}/impact").json()
    assert len(impact["activities_with_overage"]) == 1
    assert [s["student_id"] for s in impact["students_with_held_hours"]] == ["S2"]
    single = client.get(
        f"/api/plans/{PLAN}/impact", params={"student_id": "S1"}
    ).json()
    assert single["students_with_held_hours"] == []


def test_schedule_version_switch_recomputes_only_affected_and_freezes_stay(client):
    _setup_plan(client)
    _setup_venue(client, cap=10)
    # 第二个小容量场地。
    assert client.post("/api/venues", json={"venue_id": "V-ROOM", "name": "教室"}).status_code == 201
    assert client.post(
        "/api/venues/V-ROOM/capacity-versions",
        json={
            "capacity_version": "C-ROOM-1",
            "capacity": 1,
            "effective_from": "2000-01-01T00:00:00+00:00",
        },
    ).status_code == 201

    # SV-1：两场活动都在大场地。
    client.post(
        f"/api/plans/{PLAN}/schedule-versions",
        json={
            "schedule_version": "SV-1",
            "entries": [
                {
                    "activity_id": "ACT-1",
                    "venue_id": "V-AUD",
                    "start_at": "2024-03-15T08:00:00+00:00",
                    "end_at": "2024-03-15T10:00:00+00:00",
                },
                {
                    "activity_id": "ACT-2",
                    "venue_id": "V-AUD",
                    "start_at": "2024-03-15T10:00:00+00:00",
                    "end_at": "2024-03-15T12:00:00+00:00",
                },
            ],
        },
    )
    _post_checkins(
        client,
        [
            _checkin_event(
                "E-01", "S1",
                "2024-03-15T08:00:00+00:00", "2024-03-15T10:00:00+00:00",
                activity="ACT-1",
            ),
            _checkin_event(
                "E-02", "S2",
                "2024-03-15T08:00:00+00:00", "2024-03-15T10:00:00+00:00",
                activity="ACT-1",
            ),
            _checkin_event(
                "E-03", "S1",
                "2024-03-15T10:00:00+00:00", "2024-03-15T12:00:00+00:00",
                activity="ACT-2",
            ),
            _checkin_event(
                "E-04", "S2",
                "2024-03-15T10:00:00+00:00", "2024-03-15T12:00:00+00:00",
                activity="ACT-2",
            ),
        ],
    )

    # 在 SV-1 下冻结：此时没有超额。
    frozen = client.post(f"/api/plans/{PLAN}/freezes/F-OLD", json={})
    assert frozen.status_code == 201, frozen.text
    frozen_body = frozen.json()
    assert frozen_body["schedule_version"] == "SV-1"
    assert frozen_body["overages"] == []
    frozen_s2 = next(s for s in frozen_body["students"] if s["student_id"] == "S2")
    assert frozen_s2["total_seconds"] == 4 * 3600

    # SV-2：把 ACT-2 更正到只能容纳 1 人的小教室，ACT-1 不变。
    client.post(
        f"/api/plans/{PLAN}/schedule-versions",
        json={
            "schedule_version": "SV-2",
            "entries": [
                {
                    "activity_id": "ACT-1",
                    "venue_id": "V-AUD",
                    "start_at": "2024-03-15T08:00:00+00:00",
                    "end_at": "2024-03-15T10:00:00+00:00",
                },
                {
                    "activity_id": "ACT-2",
                    "venue_id": "V-ROOM",
                    "start_at": "2024-03-15T10:00:00+00:00",
                    "end_at": "2024-03-15T12:00:00+00:00",
                },
            ],
        },
    )

    # 版本切换影响：只重算 ACT-2。
    impact = client.get(
        f"/api/plans/{PLAN}/schedule-versions/SV-1/impact/SV-2"
    ).json()
    assert impact["activities_affected"] == 1
    assert impact["activities_unaffected"] == 1
    assert impact["affected_activities"][0]["activity_id"] == "ACT-2"
    # S1 按确定顺位（签到事件 ID）始终占座，只有 S2 从释放变为暂缓。
    assert [s["student_id"] for s in impact["students"]] == ["S2"]
    s2_impact = impact["students"][0]
    assert s2_impact["held_seconds_before"] == 0
    assert s2_impact["held_seconds_after"] == 2 * 3600

    # 切换生效后当前快照出现超额，仅 ACT-2。
    client.post(f"/api/plans/{PLAN}/schedule-versions/SV-2/activate")
    current = client.get(f"/api/plans/{PLAN}/snapshot").json()
    assert current["schedule_version"] == "SV-2"
    assert [o["activity_id"] for o in current["overages"]] == ["ACT-2"]

    # 已冻结快照保持不变。
    old_frozen = client.get(f"/api/plans/{PLAN}/freezes/F-OLD").json()
    assert old_frozen["schedule_version"] == "SV-1"
    assert old_frozen["overages"] == []
    old_s2 = next(s for s in old_frozen["students"] if s["student_id"] == "S2")
    assert old_s2["total_seconds"] == 4 * 3600
    assert old_s2["held_seconds"] == 0


def test_capacity_configuration_validation(client):
    _setup_plan(client)
    # 未注册场地不能配置容量版本。
    resp = client.post(
        "/api/venues/NOPE/capacity-versions",
        json={
            "capacity_version": "C1",
            "capacity": 1,
            "effective_from": "2024-01-01T00:00:00+00:00",
        },
    )
    assert resp.status_code == 400

    # 重复排期版本被拒绝。
    _setup_venue(client)
    payload = {
        "schedule_version": "SV-DUP",
        "entries": [
            {
                "activity_id": "ACT-1",
                "venue_id": "V-AUD",
                "start_at": "2024-03-15T08:00:00+00:00",
                "end_at": "2024-03-15T10:00:00+00:00",
            }
        ],
    }
    assert client.post(f"/api/plans/{PLAN}/schedule-versions", json=payload).status_code == 201
    assert client.post(f"/api/plans/{PLAN}/schedule-versions", json=payload).status_code == 400


def test_concurrent_checkin_import_is_idempotent_and_deterministic(client):
    _setup_plan(client)
    _setup_venue(client, cap=50)
    client.post(
        f"/api/plans/{PLAN}/schedule-versions",
        json={
            "schedule_version": "SV-1",
            "entries": [
                {
                    "activity_id": "ACT-1",
                    "venue_id": "V-AUD",
                    "start_at": "2024-03-15T08:00:00+00:00",
                    "end_at": "2024-03-15T12:00:00+00:00",
                }
            ],
        },
    )

    # 多个线程并发导入：唯一事件全部入库，重复事件只有一个胜出者。
    barrier = threading.Barrier(4)
    errors: list[Exception] = []

    def worker(prefix: str) -> None:
        session = TestSessionLocal()
        try:
            events = [
                _checkin_event(
                    f"E-{prefix}",
                    f"S-{prefix}",
                    "2024-03-15T08:00:00+00:00",
                    "2024-03-15T12:00:00+00:00",
                )
            ]
            barrier.wait()
            services.import_events(session, plan_version=PLAN, events=events)
        except Exception as exc:  # pragma: no cover - 失败时展示
            errors.append(exc)
        finally:
            session.close()

    # 两个线程抢写同一个 event_id（DUP），另两个写各自的事件。
    def dup_worker() -> None:
        session = TestSessionLocal()
        try:
            barrier.wait()
            services.import_events(
                session,
                plan_version=PLAN,
                events=[
                    _checkin_event(
                        "E-DUP",
                        "S-DUP",
                        "2024-03-15T08:00:00+00:00",
                        "2024-03-15T12:00:00+00:00",
                    )
                ],
            )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            session.close()

    threads = [
        threading.Thread(target=worker, args=("01",)),
        threading.Thread(target=worker, args=("02",)),
        threading.Thread(target=dup_worker),
        threading.Thread(target=dup_worker),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    # 容量为 50，并发签到不产生超额；重复事件只计数一次。
    snap = client.get(f"/api/plans/{PLAN}/snapshot").json()
    students = {s["student_id"]: s for s in snap["students"]}
    assert set(students) == {"S-01", "S-02", "S-DUP"}
    assert snap["overages"] == []
    for student in students.values():
        assert student["total_seconds"] == 4 * 3600

    # 重放结果与导入顺序无关：快照可稳定复现。
    snap_again = client.get(f"/api/plans/{PLAN}/snapshot").json()
    assert snap_again["students"] == snap["students"]
