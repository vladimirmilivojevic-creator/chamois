"""Job queue: queued -> ongoing -> finished / error / cancelled.

One worker task drives the single device, like one QPU serving a queue. Jobs
live in memory; a restart clears the queue (see "Known limitations" in the docs).
"""

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import numpy as np

from chamois import device as device_sim
from chamois.arnica import (
    BasicJobMetadata,
    JobResponse,
    JobStatus,
    QuantumCircuit,
    RRCancelled,
    RRError,
    RRFinished,
    RROngoing,
    RRQueued,
    StatusChange,
)
from chamois.device import SimulatedDevice
from chamois.scheduler import compile_circuit
from chamois.timeline import CompileResult

logger = logging.getLogger(__name__)

FINAL_STATES = {JobStatus.FINISHED, JobStatus.ERROR, JobStatus.CANCELLED}


class QueueFullError(Exception):
    pass


@dataclass
class Job:
    circuits: list[QuantumCircuit]
    owner: str
    workspace_id: str
    resource_id: str
    label: str | None = None
    source: str = "arnica"  # "arnica" job API or "direct" access API
    id: UUID = field(default_factory=uuid4)
    status: JobStatus = JobStatus.QUEUED
    timing: list[StatusChange] = field(default_factory=list)
    finished_count: int = 0
    result: dict[str, list[list[int]]] = field(default_factory=dict)
    message: str | None = None
    compiled: list[CompileResult] = field(default_factory=list)
    calibration_version: int | None = None
    cancel_requested: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self._record(JobStatus.QUEUED)

    def _record(self, status: JobStatus) -> None:
        self.status = status
        self.timing.append(StatusChange(new_status=status, timestamp=datetime.now(UTC)))
        if status in FINAL_STATES:
            self.done.set()

    @property
    def created_at(self) -> datetime:
        return self.timing[0].timestamp

    @property
    def shots(self) -> int:
        return sum(circuit.repetitions for circuit in self.circuits)

    def metadata(self) -> BasicJobMetadata:
        return BasicJobMetadata(
            job_id=self.id, label=self.label, resource_id=self.resource_id, workspace_id=self.workspace_id
        )

    def response(self, include_timing: bool = False) -> JobResponse:
        timing = self.timing if include_timing else None
        match self.status:
            case JobStatus.QUEUED:
                return RRQueued(timing_data=timing)
            case JobStatus.ONGOING:
                return RROngoing(finished_count=self.finished_count, timing_data=timing)
            case JobStatus.FINISHED:
                return RRFinished(result=self.result, timing_data=timing)
            case JobStatus.ERROR:
                return RRError(message=self.message or "unknown error", timing_data=timing)
            case _:
                return RRCancelled(timing_data=timing)


class JobManager:
    def __init__(
        self,
        device: SimulatedDevice,
        *,
        time_scale: float = 1.0,
        max_queued: int = 50,
        keep_jobs: int = 200,
        seed: int | None = None,
    ) -> None:
        self.device = device
        self.time_scale = time_scale
        self.max_queued = max_queued
        self.keep_jobs = keep_jobs
        self.jobs: OrderedDict[UUID, Job] = OrderedDict()
        self._queue: asyncio.Queue[UUID] = asyncio.Queue()
        self._rng = np.random.default_rng(seed)
        self.current: UUID | None = None

    @property
    def queued(self) -> int:
        return sum(1 for job in self.jobs.values() if job.status == JobStatus.QUEUED)

    @property
    def accepting(self) -> bool:
        return self.queued < self.max_queued

    def submit(self, job: Job) -> Job:
        if not self.accepting:
            raise QueueFullError(f"the queue already holds {self.max_queued} jobs")
        self.jobs[job.id] = job
        self._forget_old_jobs()
        self._queue.put_nowait(job.id)
        return job

    def get(self, job_id: UUID) -> Job | None:
        return self.jobs.get(job_id)

    def cancel(self, job: Job) -> bool:
        """Queued jobs are dropped; an ongoing job stops after its current circuit (as in ARNICA)."""
        if job.status in FINAL_STATES:
            return False
        if job.status == JobStatus.QUEUED:
            job._record(JobStatus.CANCELLED)
        else:
            job.cancel_requested = True
        return True

    def _forget_old_jobs(self) -> None:
        while len(self.jobs) > self.keep_jobs:
            oldest_id, oldest = next(iter(self.jobs.items()))
            if oldest.status not in FINAL_STATES:
                break
            del self.jobs[oldest_id]

    async def run_forever(self) -> None:
        while True:
            job_id = await self._queue.get()
            job = self.jobs.get(job_id)
            if job is None or job.status != JobStatus.QUEUED:
                continue
            self.current = job.id
            try:
                await self.execute(job)
            except Exception:  # the worker must survive any single bad job
                logger.exception("job %s crashed", job.id)
                job.message = "internal error while executing the job"
                job._record(JobStatus.ERROR)
            finally:
                self.current = None

    async def execute(self, job: Job) -> None:
        calibration = self.device.calibration  # one calibration snapshot for the whole job
        job.calibration_version = calibration.version
        job._record(JobStatus.ONGOING)
        for index, circuit in enumerate(job.circuits):
            if job.cancel_requested:
                job._record(JobStatus.CANCELLED)
                return
            compiled = compile_circuit(circuit, calibration)
            job.compiled.append(compiled)
            if not compiled.ok or compiled.timeline is None:
                reasons = "; ".join(v.message for v in compiled.violations)
                job.message = f"circuit {index} rejected by the scheduler: {reasons}"
                job._record(JobStatus.ERROR)
                return
            timeline = compiled.timeline
            samples = await asyncio.to_thread(device_sim.sample, timeline, calibration, circuit.repetitions, self._rng)
            # Real devices spend shots x shot duration; keep that visible in the demo.
            await asyncio.sleep(device_sim.shot_seconds(timeline) * circuit.repetitions * self.time_scale)
            job.result[str(index)] = samples
            job.finished_count = index + 1
        job._record(JobStatus.FINISHED)
