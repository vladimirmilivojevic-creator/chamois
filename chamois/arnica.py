"""Request and response models of AQT's public ARNICA API (v1).

Field names, limits and status values follow the public OpenAPI document at
https://arnica.aqt.eu/api/v1/docs, so clients written for ARNICA can talk to
CHAMOIS unchanged. Angles are in units of pi, as in ARNICA.
"""

import math
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, field_validator, model_validator


class _Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GateR(_Operation):
    """Rotation by theta around an axis in the equatorial plane at angle phi."""

    operation: Literal["R"] = "R"
    qubit: NonNegativeInt
    theta: float = Field(ge=0.0, le=1.0)
    phi: float = Field(ge=0.0, le=2.0)


class GateRZ(_Operation):
    """Rotation around the z axis (virtual: a phase update, no laser pulse)."""

    operation: Literal["RZ"] = "RZ"
    qubit: NonNegativeInt
    phi: float

    @field_validator("phi")
    @classmethod
    def _finite(cls, phi: float) -> float:
        if not math.isfinite(phi):
            raise ValueError("phi must be finite")
        return phi


class GateRXX(_Operation):
    """Molmer-Sorensen entangling gate; theta = 0.5 is maximally entangling."""

    operation: Literal["RXX"] = "RXX"
    qubits: Annotated[list[NonNegativeInt], Field(min_length=2, max_length=2)]
    theta: float = Field(ge=0.0, le=0.5)


class Measure(_Operation):
    """Measures all qubits; must be the last operation."""

    operation: Literal["MEASURE"] = "MEASURE"


Operation = Annotated[GateR | GateRZ | GateRXX | Measure, Field(discriminator="operation")]


class QuantumCircuit(BaseModel):
    repetitions: int = Field(ge=1, le=2000, description="Number of shots.")
    quantum_circuit: Annotated[list[Operation], Field(min_length=1, max_length=2000)]
    number_of_qubits: int = Field(ge=1, le=31)

    @model_validator(mode="after")
    def _single_final_measure(self) -> "QuantumCircuit":
        ops = self.quantum_circuit
        if not isinstance(ops[-1], Measure) or any(isinstance(op, Measure) for op in ops[:-1]):
            raise ValueError("a circuit needs exactly one MEASURE, as its last operation")
        return self


class QuantumCircuits(BaseModel):
    circuits: Annotated[list[QuantumCircuit], Field(min_length=1, max_length=50)]


class SubmitJobRequest(BaseModel):
    job_type: Literal["quantum_circuit"] = "quantum_circuit"
    label: str | None = Field(default=None, max_length=200)
    payload: QuantumCircuits


class JobStatus(StrEnum):
    QUEUED = "queued"
    ONGOING = "ongoing"
    FINISHED = "finished"
    ERROR = "error"
    CANCELLED = "cancelled"


class StatusChange(BaseModel):
    new_status: JobStatus
    timestamp: datetime


class BasicJobMetadata(BaseModel):
    job_id: UUID
    job_type: Literal["quantum_circuit"] = "quantum_circuit"
    label: str | None = None
    resource_id: str
    workspace_id: str


class RRQueued(BaseModel):
    status: Literal["queued"] = "queued"
    timing_data: list[StatusChange] | None = None


class RROngoing(BaseModel):
    status: Literal["ongoing"] = "ongoing"
    timing_data: list[StatusChange] | None = None
    finished_count: NonNegativeInt


class RRFinished(BaseModel):
    status: Literal["finished"] = "finished"
    timing_data: list[StatusChange] | None = None
    result: dict[str, list[list[Annotated[int, Field(ge=0, le=1)]]]]


class RRError(BaseModel):
    status: Literal["error"] = "error"
    timing_data: list[StatusChange] | None = None
    message: str


class RRCancelled(BaseModel):
    status: Literal["cancelled"] = "cancelled"
    timing_data: list[StatusChange] | None = None


JobResponse = RRQueued | RROngoing | RRFinished | RRError | RRCancelled


class SubmitJobResponse(BaseModel):
    job: BasicJobMetadata
    response: RRQueued = RRQueued()


class ResultResponse(BaseModel):
    job: BasicJobMetadata
    response: JobResponse


class ResourceType(StrEnum):
    SIMULATOR = "simulator"
    DEVICE = "device"


class ResourceStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    MAINTENANCE = "maintenance"
    UNAVAILABLE = "unavailable"


class WorkspaceResource(BaseModel):
    id: str
    name: str
    type: ResourceType


class Workspace(BaseModel):
    id: str
    accepting_job_submissions: bool
    jobs_being_processed: bool
    resources: list[WorkspaceResource]


class ResourceDetails(BaseModel):
    id: str
    name: str
    type: ResourceType
    status: ResourceStatus
    available_qubits: int
    status_updated_at: datetime
    characterisation: None = None
