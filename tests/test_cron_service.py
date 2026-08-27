"""Tests for CronService and _compute_next_run."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from nanobot.cron.service import CronService, _compute_next_run
from nanobot.cron.types import CronJob, CronPayload, CronSchedule, CronJobState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _make_every_schedule(every_ms: int = 60_000) -> CronSchedule:
    return CronSchedule(kind="every", every_ms=every_ms)


def _make_at_schedule(offset_ms: int = 60_000) -> CronSchedule:
    return CronSchedule(kind="at", at_ms=_now_ms() + offset_ms)


# ---------------------------------------------------------------------------
# _compute_next_run
# ---------------------------------------------------------------------------

class TestComputeNextRun:

    def test_every_returns_now_plus_interval(self) -> None:
        now = _now_ms()
        sched = CronSchedule(kind="every", every_ms=5000)
        result = _compute_next_run(sched, now)
        assert result == now + 5000

    def test_every_zero_returns_none(self) -> None:
        sched = CronSchedule(kind="every", every_ms=0)
        assert _compute_next_run(sched, _now_ms()) is None

    def test_every_none_returns_none(self) -> None:
        sched = CronSchedule(kind="every", every_ms=None)
        assert _compute_next_run(sched, _now_ms()) is None

    def test_at_future_returns_at_ms(self) -> None:
        future = _now_ms() + 10_000
        sched = CronSchedule(kind="at", at_ms=future)
        assert _compute_next_run(sched, _now_ms()) == future

    def test_at_past_returns_none(self) -> None:
        past = _now_ms() - 10_000
        sched = CronSchedule(kind="at", at_ms=past)
        assert _compute_next_run(sched, _now_ms()) is None

    def test_at_none_returns_none(self) -> None:
        sched = CronSchedule(kind="at", at_ms=None)
        assert _compute_next_run(sched, _now_ms()) is None

    def test_cron_invalid_expr_returns_none(self) -> None:
        sched = CronSchedule(kind="cron", expr="not a valid expression")
        assert _compute_next_run(sched, _now_ms()) is None

    def test_cron_no_expr_returns_none(self) -> None:
        sched = CronSchedule(kind="cron", expr=None)
        assert _compute_next_run(sched, _now_ms()) is None


# ---------------------------------------------------------------------------
# CronService.add_job / list_jobs / remove_job
# ---------------------------------------------------------------------------

class TestCronServiceCrud:

    def test_add_job_persists_to_disk(self, tmp_path: Path) -> None:
        svc = CronService(store_path=tmp_path / "jobs.json")
        svc.add_job("daily-check", _make_every_schedule(), "check inbox")

        assert (tmp_path / "jobs.json").exists()
        data = json.loads((tmp_path / "jobs.json").read_text())
        assert len(data["jobs"]) == 1
        assert data["jobs"][0]["name"] == "daily-check"

    def test_list_jobs_returns_enabled_by_default(self, tmp_path: Path) -> None:
        svc = CronService(store_path=tmp_path / "jobs.json")
        svc.add_job("job-a", _make_every_schedule(), "msg a")
        svc.add_job("job-b", _make_every_schedule(), "msg b")
        svc.enable_job(svc.list_jobs(include_disabled=True)[0].id, False)

        active = svc.list_jobs()
        assert len(active) == 1
        assert active[0].name == "job-b"

    def test_list_jobs_include_disabled_returns_all(self, tmp_path: Path) -> None:
        svc = CronService(store_path=tmp_path / "jobs.json")
        svc.add_job("a", _make_every_schedule(), "msg")
        svc.add_job("b", _make_every_schedule(), "msg")
        svc.enable_job(svc.list_jobs()[0].id, False)

        assert len(svc.list_jobs(include_disabled=True)) == 2

    def test_remove_job_deletes_it(self, tmp_path: Path) -> None:
        svc = CronService(store_path=tmp_path / "jobs.json")
        job = svc.add_job("temp", _make_every_schedule(), "do thing")

        removed = svc.remove_job(job.id)

        assert removed is True
        assert svc.list_jobs(include_disabled=True) == []

    def test_remove_unknown_returns_false(self, tmp_path: Path) -> None:
        svc = CronService(store_path=tmp_path / "jobs.json")
        assert svc.remove_job("nonexistent-id") is False

    def test_enable_job_toggles_state(self, tmp_path: Path) -> None:
        svc = CronService(store_path=tmp_path / "jobs.json")
        job = svc.add_job("toggle", _make_every_schedule(), "msg")

        svc.enable_job(job.id, False)
        assert svc.list_jobs(include_disabled=True)[0].enabled is False

        svc.enable_job(job.id, True)
        assert svc.list_jobs(include_disabled=True)[0].enabled is True

    def test_enable_unknown_job_returns_none(self, tmp_path: Path) -> None:
        svc = CronService(store_path=tmp_path / "jobs.json")
        assert svc.enable_job("no-such-id") is None


# ---------------------------------------------------------------------------
# CronService.run_job
# ---------------------------------------------------------------------------

class TestCronServiceRunJob:

    def test_run_job_calls_callback(self, tmp_path: Path) -> None:
        executed: list[str] = []

        async def callback(job: CronJob) -> str:
            executed.append(job.name)
            return "ok"

        svc = CronService(store_path=tmp_path / "jobs.json", on_job=callback)
        job = svc.add_job("my-job", _make_every_schedule(), "run me")

        result = asyncio.run(svc.run_job(job.id))

        assert result is True
        assert executed == ["my-job"]

    def test_run_job_sets_last_status_ok(self, tmp_path: Path) -> None:
        async def callback(job: CronJob) -> str:
            return "done"

        svc = CronService(store_path=tmp_path / "jobs.json", on_job=callback)
        job = svc.add_job("check", _make_every_schedule(), "msg")
        asyncio.run(svc.run_job(job.id))

        updated = svc.list_jobs(include_disabled=True)[0]
        assert updated.state.last_status == "ok"
        assert updated.state.last_run_at_ms is not None

    def test_run_job_sets_error_status_on_exception(self, tmp_path: Path) -> None:
        async def failing_callback(job: CronJob) -> str:
            raise RuntimeError("db unavailable")

        svc = CronService(store_path=tmp_path / "jobs.json", on_job=failing_callback)
        job = svc.add_job("bad-job", _make_every_schedule(), "msg")
        asyncio.run(svc.run_job(job.id))

        updated = svc.list_jobs(include_disabled=True)[0]
        assert updated.state.last_status == "error"
        assert "db unavailable" in updated.state.last_error

    def test_run_job_unknown_id_returns_false(self, tmp_path: Path) -> None:
        svc = CronService(store_path=tmp_path / "jobs.json")
        result = asyncio.run(svc.run_job("no-such-id"))
        assert result is False

    def test_run_job_disabled_requires_force(self, tmp_path: Path) -> None:
        async def callback(job: CronJob) -> str:
            return "ok"

        svc = CronService(store_path=tmp_path / "jobs.json", on_job=callback)
        job = svc.add_job("job", _make_every_schedule(), "msg")
        svc.enable_job(job.id, False)

        assert asyncio.run(svc.run_job(job.id, force=False)) is False
        assert asyncio.run(svc.run_job(job.id, force=True)) is True


# ---------------------------------------------------------------------------
# CronService — one-shot (at) jobs
# ---------------------------------------------------------------------------

class TestCronServiceOneShotJobs:

    def test_at_job_disabled_after_run(self, tmp_path: Path) -> None:
        async def callback(job: CronJob) -> str:
            return "ok"

        svc = CronService(store_path=tmp_path / "jobs.json", on_job=callback)
        job = svc.add_job(
            "one-shot",
            CronSchedule(kind="at", at_ms=_now_ms() + 60_000),
            "fire once",
        )
        asyncio.run(svc.run_job(job.id))

        updated = svc.list_jobs(include_disabled=True)[0]
        assert updated.enabled is False
        assert updated.state.next_run_at_ms is None

    def test_at_job_deleted_after_run_when_flag_set(self, tmp_path: Path) -> None:
        async def callback(job: CronJob) -> str:
            return "ok"

        svc = CronService(store_path=tmp_path / "jobs.json", on_job=callback)
        job = svc.add_job(
            "delete-me",
            CronSchedule(kind="at", at_ms=_now_ms() + 60_000),
            "fire and forget",
            delete_after_run=True,
        )
        asyncio.run(svc.run_job(job.id, force=True))

        assert svc.list_jobs(include_disabled=True) == []


# ---------------------------------------------------------------------------
# CronService — JSON persistence roundtrip
# ---------------------------------------------------------------------------

class TestCronServicePersistence:

    def test_jobs_survive_reload(self, tmp_path: Path) -> None:
        store_path = tmp_path / "jobs.json"

        svc1 = CronService(store_path=store_path)
        svc1.add_job("persist-me", _make_every_schedule(120_000), "hello world")

        # New service instance reads same file
        svc2 = CronService(store_path=store_path)
        jobs = svc2.list_jobs()

        assert len(jobs) == 1
        assert jobs[0].name == "persist-me"
        assert jobs[0].payload.message == "hello world"

    def test_empty_store_path_starts_with_empty_jobs(self, tmp_path: Path) -> None:
        svc = CronService(store_path=tmp_path / "missing.json")
        assert svc.list_jobs(include_disabled=True) == []

    def test_reload_detects_external_modification(self, tmp_path: Path) -> None:
        store_path = tmp_path / "jobs.json"
        svc = CronService(store_path=store_path)
        svc.add_job("original", _make_every_schedule(), "msg")

        # Simulate external write: add another job directly to JSON
        data = json.loads(store_path.read_text())
        data["jobs"].append({
            "id": "ext-1",
            "name": "external",
            "enabled": True,
            "schedule": {"kind": "every", "everyMs": 10000},
            "payload": {"kind": "agent_turn", "message": "external msg"},
            "state": {},
            "createdAtMs": 0,
            "updatedAtMs": 0,
            "deleteAfterRun": False,
        })
        store_path.write_text(json.dumps(data))

        # Invalidate mtime cache by touching with different mtime
        import os
        os.utime(store_path, (store_path.stat().st_atime, store_path.stat().st_mtime + 1))
        svc._last_mtime = 0.0  # force reload

        jobs = svc.list_jobs(include_disabled=True)
        assert len(jobs) == 2
        names = {j.name for j in jobs}
        assert "external" in names


# ---------------------------------------------------------------------------
# CronService.status
# ---------------------------------------------------------------------------

class TestCronServiceStatus:

    def test_status_returns_job_count(self, tmp_path: Path) -> None:
        svc = CronService(store_path=tmp_path / "jobs.json")
        svc.add_job("a", _make_every_schedule(), "msg")
        svc.add_job("b", _make_every_schedule(), "msg")

        s = svc.status()
        assert s["jobs"] == 2

    def test_status_running_false_before_start(self, tmp_path: Path) -> None:
        svc = CronService(store_path=tmp_path / "jobs.json")
        assert svc.status()["enabled"] is False
