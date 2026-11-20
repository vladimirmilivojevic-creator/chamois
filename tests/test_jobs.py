import asyncio

import pytest

from chamois.arnica import JobStatus, QuantumCircuit
from chamois.device import SimulatedDevice
from chamois.jobs import Job, JobManager, QueueFullError
from tests.conftest import bell_circuit


def make_job(circuits: int = 1) -> Job:
    return Job(
        circuits=[QuantumCircuit.model_validate(bell_circuit(5)) for _ in range(circuits)],
        owner="Ana",
        workspace_id="default",
        resource_id="chamois",
    )


def test_cancelling_a_queued_job_drops_it() -> None:
    manager = JobManager(SimulatedDevice(), time_scale=0)
    job = manager.submit(make_job())
    assert manager.cancel(job) and job.status == JobStatus.CANCELLED
    assert job.response().status == "cancelled"
    assert not manager.cancel(job)  # already final


def test_cancelling_an_ongoing_job_stops_after_the_current_circuit() -> None:
    async def scenario() -> Job:
        manager = JobManager(SimulatedDevice(), time_scale=0)
        job = make_job(circuits=3)
        job.status = JobStatus.ONGOING
        manager.cancel(job)  # only sets the flag while ongoing
        job.status = JobStatus.QUEUED
        await manager.execute(job)
        return job

    job = asyncio.run(scenario())
    assert job.status == JobStatus.CANCELLED and job.finished_count == 0


def test_queue_limit_and_history_trimming() -> None:
    manager = JobManager(SimulatedDevice(), time_scale=0, max_queued=2, keep_jobs=2)
    first, second = manager.submit(make_job()), manager.submit(make_job())
    with pytest.raises(QueueFullError):
        manager.submit(make_job())
    manager.cancel(first)
    manager.cancel(second)
    manager.submit(make_job())
    assert first.id not in manager.jobs and len(manager.jobs) == 2


def test_worker_survives_a_crashing_job(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> tuple[Job, Job]:
        manager = JobManager(SimulatedDevice(), time_scale=0, seed=1)
        original = manager.execute

        async def flaky(job: Job) -> None:
            if job.label == "boom":
                raise RuntimeError("simulated crash")
            await original(job)

        monkeypatch.setattr(manager, "execute", flaky)
        bad = make_job()
        bad.label = "boom"
        good = make_job()
        manager.submit(bad)
        manager.submit(good)
        worker = asyncio.create_task(manager.run_forever())
        await asyncio.wait_for(good.done.wait(), timeout=5)
        worker.cancel()
        return bad, good

    bad, good = asyncio.run(scenario())
    assert bad.status == JobStatus.ERROR and bad.message == "internal error while executing the job"
    assert good.status == JobStatus.FINISHED
    assert good.response(include_timing=True).timing_data is not None
