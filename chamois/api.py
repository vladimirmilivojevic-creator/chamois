"""FastAPI service: ARNICA-compatible job API, AQT direct-access API, operator API and console."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from chamois import scheduler
from chamois.arnica import (
    JobStatus,
    QuantumCircuit,
    ResourceDetails,
    ResourceStatus,
    ResourceType,
    ResultResponse,
    SubmitJobRequest,
    SubmitJobResponse,
    Workspace,
    WorkspaceResource,
)
from chamois.auth import CurrentUser, Operator
from chamois.calibration import Calibration, CalibrationValues
from chamois.config import Role, Settings, User
from chamois.device import SimulatedDevice
from chamois.jobs import Job, JobManager, QueueFullError
from chamois.timeline import CompileResult

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

try:
    VERSION = version("chamois")
except PackageNotFoundError:  # pragma: no cover - running from a source checkout
    VERSION = "0.0.0"


class JobSummary(BaseModel):
    job_id: UUID
    label: str | None
    owner: str
    source: str
    status: JobStatus
    circuits: int
    shots: int
    finished_count: int
    created_at: datetime
    calibration_version: int | None
    message: str | None


class Health(BaseModel):
    status: str
    version: str
    scheduler: str
    scheduler_version: str | None
    queued_jobs: int
    device_busy: bool


def _summary(job: Job) -> JobSummary:
    return JobSummary(
        job_id=job.id,
        label=job.label,
        owner=job.owner,
        source=job.source,
        status=job.status,
        circuits=len(job.circuits),
        shots=job.shots,
        finished_count=job.finished_count,
        created_at=job.created_at,
        calibration_version=job.calibration_version,
        message=job.message,
    )


def _manager(request: Request) -> JobManager:
    manager: JobManager = request.app.state.jobs
    return manager


def _visible_job(request: Request, job_id: UUID, user: User) -> Job:
    """Researchers only see their own jobs; operators see all of them."""
    job = _manager(request).get(job_id)
    if job is None or (user.role != Role.OPERATOR and job.owner != user.name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job


def _submit(request: Request, job: Job) -> Job:
    try:
        return _manager(request).submit(job)
    except QueueFullError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error


# --- ARNICA-compatible job API -----------------------------------------------------------------
arnica = APIRouter(prefix="/api/v1", tags=["ARNICA job API"])


@arnica.get("/workspaces", response_model=list[Workspace])
def list_workspaces(request: Request, user: CurrentUser) -> list[Workspace]:
    manager = _manager(request)
    device: SimulatedDevice = request.app.state.device
    return [
        Workspace(
            id=request.app.state.settings.workspace_id,
            accepting_job_submissions=manager.accepting,
            jobs_being_processed=manager.current is not None,
            resources=[WorkspaceResource(id=device.resource_id, name=device.name, type=ResourceType.DEVICE)],
        )
    ]


@arnica.get("/resources/{resource_id}", response_model=ResourceDetails)
def get_resource(request: Request, resource_id: str, user: CurrentUser) -> ResourceDetails:
    device: SimulatedDevice = request.app.state.device
    if resource_id != device.resource_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")
    return ResourceDetails(
        id=device.resource_id,
        name=device.name,
        type=ResourceType.DEVICE,
        status=ResourceStatus.ONLINE,
        available_qubits=sum(q.enabled for q in device.calibration.qubits),
        status_updated_at=device.online_since,
    )


@arnica.post("/submit/{workspace}/{resource}", response_model=SubmitJobResponse)
def submit_job(
    request: Request, workspace: str, resource: str, body: SubmitJobRequest, user: CurrentUser
) -> SubmitJobResponse:
    device: SimulatedDevice = request.app.state.device
    if workspace != request.app.state.settings.workspace_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Workspace not available.")
    if resource != device.resource_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not available in workspace.")
    job = _submit(
        request,
        Job(
            circuits=body.payload.circuits,
            owner=user.name,
            workspace_id=workspace,
            resource_id=resource,
            label=body.label,
        ),
    )
    return SubmitJobResponse(job=job.metadata())


@arnica.get("/result/{job_id}", response_model=ResultResponse)
def get_result(request: Request, job_id: UUID, user: CurrentUser, include_timing_data: bool = False) -> ResultResponse:
    job = _visible_job(request, job_id, user)
    return ResultResponse(job=job.metadata(), response=job.response(include_timing_data))


@arnica.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_job(request: Request, job_id: UUID, user: CurrentUser) -> Response:
    job = _visible_job(request, job_id, user)
    if not _manager(request).cancel(job):
        return Response(status_code=status.HTTP_208_ALREADY_REPORTED)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- CHAMOIS operator / researcher extensions ----------------------------------------------------
console_api = APIRouter(prefix="/api/v1", tags=["CHAMOIS extensions"])


@console_api.get("/me", response_model=User)
def me(user: CurrentUser) -> User:
    return user


@console_api.get("/jobs", response_model=list[JobSummary])
def list_jobs(request: Request, user: CurrentUser, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> Any:
    jobs = [
        job for job in reversed(_manager(request).jobs.values()) if user.role == Role.OPERATOR or job.owner == user.name
    ]
    return [_summary(job) for job in jobs[:limit]]


@console_api.get("/jobs/{job_id}/schedule", response_model=CompileResult)
def job_schedule(request: Request, job_id: UUID, user: CurrentUser, circuit: int = 0) -> CompileResult:
    """The pulse timeline the scheduler compiled for one circuit of a job."""
    job = _visible_job(request, job_id, user)
    if circuit < 0 or circuit >= len(job.compiled):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "circuit not compiled yet")
    return job.compiled[circuit]


@console_api.post("/schedule", response_model=CompileResult)
def preview_schedule(request: Request, circuit: QuantumCircuit, user: CurrentUser) -> CompileResult:
    """Compile a circuit with the current calibration without running it."""
    return scheduler.compile_circuit(circuit, request.app.state.device.calibration)


@console_api.get("/calibration", response_model=Calibration)
def get_calibration(request: Request, user: CurrentUser) -> Calibration:
    calibration: Calibration = request.app.state.device.calibration
    return calibration


@console_api.put("/calibration", response_model=Calibration)
def put_calibration(request: Request, values: CalibrationValues, user: Operator) -> Calibration:
    device: SimulatedDevice = request.app.state.device
    return device.update_calibration(values, updated_by=user.name)


@console_api.get("/crosscheck")
async def crosscheck(request: Request, user: CurrentUser) -> list[dict[str, Any]]:
    """Bell/GHZ on CHAMOIS vs AQT's offline simulator (cached per calibration version)."""
    if not request.app.state.settings.crosscheck_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "cross-check is disabled on this server")
    try:
        from chamois import crosscheck as cc
    except ImportError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "install the 'qiskit' extra") from error
    calibration: Calibration = request.app.state.device.calibration
    cache: dict[int, list[dict[str, Any]]] = request.app.state.crosscheck_cache
    if calibration.version not in cache:
        rows = await asyncio.to_thread(cc.run, calibration)
        cache.clear()
        cache[calibration.version] = [row.model_dump() for row in rows]
    return cache[calibration.version]


# --- AQT direct-access API (used by qiskit-aqt-provider's DirectAccessResource) -----------------
direct = APIRouter(prefix="/direct", tags=["AQT direct-access API"])


class NumIons(BaseModel):
    num_ions: int


@direct.get("/status/ions", response_model=NumIons)
def direct_ions(request: Request, user: CurrentUser) -> NumIons:
    return NumIons(num_ions=sum(q.enabled for q in request.app.state.device.calibration.qubits))


@direct.get("/system/name")
def direct_name(request: Request, user: CurrentUser) -> str:
    name: str = request.app.state.device.resource_id
    return name


@direct.put("/circuit")
def direct_submit(request: Request, circuit: Annotated[QuantumCircuit, Body()], user: CurrentUser) -> UUID:
    settings: Settings = request.app.state.settings
    device: SimulatedDevice = request.app.state.device
    job = Job(
        circuits=[circuit],
        owner=user.name,
        workspace_id=settings.workspace_id,
        resource_id=device.resource_id,
        label="qiskit direct access",
        source="direct",
    )
    return _submit(request, job).id


@direct.get("/circuit/result/{job_id}")
async def direct_result(request: Request, job_id: UUID, user: CurrentUser) -> dict[str, Any]:
    """Long poll: answers when the job reaches a final state."""
    job = _visible_job(request, job_id, user)
    await job.done.wait()
    if job.status == JobStatus.FINISHED:
        payload: dict[str, Any] = {"status": "finished", "result": job.result["0"]}
    else:
        payload = {"status": "error"}
    return {"job_id": str(job.id), "payload": payload}


# --- app ------------------------------------------------------------------------------------------
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    if settings.require_rust and scheduler.available_backend() != "rust":
        raise RuntimeError("CHAMOIS_REQUIRE_RUST is set but the Rust scheduler is not installed")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        worker = asyncio.create_task(app.state.jobs.run_forever())
        logger.info("CHAMOIS %s started, scheduler backend: %s", VERSION, scheduler.available_backend())
        yield
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker

    app = FastAPI(
        title="CHAMOIS",
        version=VERSION,
        summary="Mini control stack for a simulated trapped-ion quantum computer.",
        description=(
            "ARNICA-compatible job API for AQT native gates (R, RZ, RXX, MEASURE), "
            "a Rust pulse scheduler and a simulated 10-ion device. "
            "Demo tokens: `researcher-demo-token`, `operator-demo-token`."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.device = SimulatedDevice()
    app.state.jobs = JobManager(
        app.state.device,
        time_scale=settings.time_scale,
        max_queued=settings.max_queued_jobs,
        keep_jobs=settings.keep_jobs,
        seed=settings.seed,
    )
    app.state.crosscheck_cache = {}

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["Authorization", "Content-Type"],
        )

    @app.get("/health", response_model=Health, tags=["ops"])
    def health() -> Health:
        manager: JobManager = app.state.jobs
        return Health(
            status="ok",
            version=VERSION,
            scheduler=scheduler.available_backend(),
            scheduler_version=scheduler.rust_version(),
            queued_jobs=manager.queued,
            device_busy=manager.current is not None,
        )

    @app.get("/", include_in_schema=False)
    def console() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.include_router(arnica)
    app.include_router(console_api)
    app.include_router(direct)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
