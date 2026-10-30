from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pytest
from pulsara_agent.scheduling.contracts import (
    next_occurrence,
    validate_rule,
    ScheduledTaskError,
)
from dateutil import rrule, tz

UTC = timezone.utc


def rule(kind, **fields):
    return {"contract": 'scheduled-rule:v1', "kind": kind, **fields}


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.parametrize(
    ("value", "after", "expected"),
    [
        (
            rule("once", run_at_utc="2026-10-06T00:00:00Z"),
            "2026-10-05T00:00:00Z",
            "2026-10-06T00:00:00Z",
        ),
        (
            rule("interval", anchor_at_utc="2020-01-01T00:00:00Z", seconds=60),
            "2050-01-01T00:00:00Z",
            "2050-01-01T00:01:00Z",
        ),
        (
            rule("daily", start_date="2020-01-01", time="08:00"),
            "2026-10-05T00:00:00Z",
            "2026-10-06T00:00:00Z",
        ),
        (
            rule("weekly", start_date="2020-01-01", time="08:00", weekdays=[1, 5]),
            "2026-10-05T00:00:00Z",
            "2026-10-09T00:00:00Z",
        ),
        (
            rule("monthly", start_date="2020-01-01", time="08:00", day=31),
            "2026-01-31T00:00:00Z",
            "2026-03-31T00:00:00Z",
        ),
        (
            rule("monthly", start_date="2020-01-01", time="08:00", day=29),
            "2028-01-29T00:00:00Z",
            "2028-02-29T00:00:00Z",
        ),
        (
            rule("daily", start_date="2050-07-02", time="08:00"),
            "2026-10-05T00:00:00Z",
            "2050-07-02T00:00:00Z",
        ),
    ],
)
def test_next(value, after, expected):
    assert next_occurrence(value, "Asia/Shanghai", instant(after)) == instant(expected)


def test_dst_gap_and_fold_are_compared_in_utc():
    spring = rule("daily", start_date="2020-01-01", time="02:30")
    assert next_occurrence(
        spring, "America/New_York", instant("2026-03-08T06:00:00Z")
    ) == instant("2026-03-09T06:30:00Z")
    fall = rule("daily", start_date="2020-01-01", time="01:30")
    first = next_occurrence(fall, "America/New_York", instant("2026-11-01T05:00:00Z"))
    assert first == instant("2026-11-01T05:30:00Z")
    assert next_occurrence(fall, "America/New_York", first) == instant(
        "2026-11-02T06:30:00Z"
    )
    assert next_occurrence(
        fall, "America/New_York", instant("2026-11-01T06:15:00Z")
    ) == instant("2026-11-02T06:30:00Z")


@pytest.mark.parametrize("kind", ["daily", "weekly", "monthly"])
def test_seek_matches_dependency_original_sequence_across_years(kind):
    zone = ZoneInfo("America/New_York")
    kwargs = dict(start_date="1999-07-13", time="02:30")
    if kind == "weekly":
        kwargs["weekdays"] = [1, 4, 7]
    if kind == "monthly":
        kwargs["day"] = 31
    value = rule(kind, **kwargs)
    opts = dict(
        dtstart=datetime(1999, 7, 13, tzinfo=zone),
        byhour=2,
        byminute=30,
        bysecond=0,
        cache=False,
        wkst=rrule.MO,
    )
    if kind == "weekly":
        opts["byweekday"] = (0, 3, 6)
    if kind == "monthly":
        opts["bymonthday"] = 31
    dependency = rrule.rrule(
        {"daily": rrule.DAILY, "weekly": rrule.WEEKLY, "monthly": rrule.MONTHLY}[kind],
        **opts,
    )
    after = instant("2026-03-07T12:00:00Z")
    for _ in range(40):
        expected = next(
            d.replace(fold=0).astimezone(UTC)
            for d in dependency.xafter(after.astimezone(zone), inc=False)
            if tz.datetime_exists(d) and d.replace(fold=0).astimezone(UTC) > after
        )
        assert next_occurrence(value, zone.key, after) == expected
        after = expected


@pytest.mark.parametrize(
    "value",
    [
        rule([], seconds=1),
        rule("interval", anchor_at_utc="2026-10-05T00:00:00Z", seconds=True),
        rule("weekly", start_date="2026-10-05", time="08:00", weekdays=[1, 1]),
        rule("monthly", start_date="2026-10-05", time="08:00", day=32),
        rule("daily", start_date="2026-10-05", time="8:00"),
        rule("once", run_at_utc="2026-10-05T00:00:00+08:00"),
    ],
)
def test_closed_invalid_rules(value):
    with pytest.raises(ScheduledTaskError):
        validate_rule(value, "Asia/Shanghai")


def test_seek_does_not_visit_old_occurrences(monkeypatch):
    original = rrule.rrule.__init__
    captured = []

    def spy(*args, **kwargs):
        captured.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(rrule.rrule, "__init__", spy)
    assert next_occurrence(
        rule("monthly", start_date="1900-01-01", time="08:00", day=31),
        "UTC",
        instant("9000-01-31T08:00:00Z"),
    ) == instant("9000-03-31T08:00:00Z")
    assert captured[0]["dtstart"] == datetime(9000, 1, 1, tzinfo=UTC)
    assert captured[0]["cache"] is False


def test_local_once_rejects_gap_and_selects_first_fold():
    import asyncio
    from pulsara_agent.scheduling.service import ScheduledTaskService

    async def scenario():
        service = ScheduledTaskService(None)
        with pytest.raises(ScheduledTaskError, match="不存在"):
            await service.local_once(
                run_at_local="2026-03-08T02:30", timezone="America/New_York"
            )
        value = await service.local_once(
            run_at_local="2026-11-01T01:30", timezone="America/New_York"
        )
        assert value["schedule"]["run_at_utc"] == "2026-11-01T05:30:00+00:00"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "value, after, expected_fold",
    [
        (
            rule("daily", start_date="2026-10-01", time="01:30"),
            "2026-11-01T05:00:00Z",
            0,
        ),
        (rule("once", run_at_utc="2026-11-01T06:30:00Z"), "2026-11-01T05:00:00Z", 1),
        (
            rule("interval", anchor_at_utc="2026-11-01T05:30:00Z", seconds=3600),
            "2026-11-01T05:30:00Z",
            1,
        ),
        (rule("once", run_at_utc="2026-11-02T06:30:00Z"), "2026-11-01T05:00:00Z", None),
    ],
)
def test_preview_reports_actual_ambiguous_fold(
    value, after, expected_fold, monkeypatch
):
    import asyncio
    from pulsara_agent.scheduling.service import ScheduledTaskService

    async def scenario():
        service = ScheduledTaskService(None)

        async def database_now(method):
            assert method == "scheduled_next_time"
            return instant(after), None

        monkeypatch.setattr(service, "_read", database_now)
        preview = await service.preview(schedule=value, timezone="America/New_York")
        assert preview["local_time_fold"] == expected_fold
        assert set(preview) == {"next_run_at", "local_time_fold"}

    asyncio.run(scenario())


def test_timer_tick_drains_runtime_delay_without_collapsing_occurrences(monkeypatch):
    import asyncio
    from datetime import timedelta
    from types import SimpleNamespace
    from pulsara_agent.identity import derive_id
    from pulsara_agent.scheduling.service import ScheduledTaskService

    async def scenario():
        service = ScheduledTaskService(SimpleNamespace())
        service._run_started_at = instant("2030-01-01T00:00:00Z")
        now = instant("2030-01-01T00:03:30Z")
        row = {
            "id": derive_id("schedule", "test", "runtime-delayed"),
            "session_id": derive_id("session", "test", "runtime-delayed"),
            "revision": 1, "next_run_at": instant("2030-01-01T00:01:00Z"),
            "status": "ACTIVE", "prompt": "identical saved task", "permission_mode": "ask-permissions",
        }
        submitted = []

        async def read(method, **kwargs):
            if method == "list_scheduled_tasks":
                return [dict(row)], None
            if method == "read_scheduled_task":
                return dict(row)
            if method == "scheduled_pending_sessions":
                return []
            raise AssertionError(method)

        async def manage(method, **kwargs):
            assert method == "observe_scheduled_due"
            return "PREPARE" if kwargs["admission"].due_at <= now else "STALE"

        async def resume(*args, **kwargs):
            return SimpleNamespace(session=object())

        async def submit(_host, admission):
            submitted.append(admission)
            row["next_run_at"] += timedelta(minutes=1)
            return SimpleNamespace(status="ACCEPTED")

        monkeypatch.setattr(service, "_read", read)
        monkeypatch.setattr(service, "_manage", manage)
        monkeypatch.setattr(service, "_submit", submit)
        service.sessions.resume_session = resume
        await service.tick()
        assert [a.due_at for a in submitted] == [
            instant("2030-01-01T00:01:00Z"), instant("2030-01-01T00:02:00Z"), instant("2030-01-01T00:03:00Z"),
        ]
        assert all(a.prompt == "identical saved task" for a in submitted)
        assert service._run_started_at == instant("2030-01-01T00:00:00Z")

    asyncio.run(scenario())


def test_timer_restart_uses_one_fixed_cutoff_before_attempting_submission(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from pulsara_agent.identity import derive_id
    from pulsara_agent.scheduling.service import ScheduledTaskService

    async def scenario():
        service = ScheduledTaskService(SimpleNamespace())
        service._run_started_at = instant("2030-01-01T00:03:30Z")
        calls = []

        async def manage(method, **kwargs):
            calls.append((method, kwargs))
            return "ADVANCED"

        async def forbidden(*args, **kwargs):
            raise AssertionError("shutdown occurrence must not reach prompt submission")

        monkeypatch.setattr(service, "_manage", manage)
        monkeypatch.setattr(service, "_submit", forbidden)
        await service._dispatch({
            "id": derive_id("schedule", "test", "missed"),
            "session_id": derive_id("session", "test", "missed"),
            "revision": 1, "next_run_at": instant("2030-01-01T00:01:00Z"),
            "status": "ACTIVE", "prompt": "missed", "permission_mode": "ask-permissions",
        })
        assert [method for method, _ in calls] == ["retire_missed_scheduled_due"]
        assert calls[0][1]["run_started_at"] == service._run_started_at

    asyncio.run(scenario())
