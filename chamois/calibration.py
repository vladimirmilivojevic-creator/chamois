"""Per-ion calibration data and device timing constraints.

The numbers are realistic for a 40Ca+ optical-qubit trap (single-qubit pi pulses
of ~15 us, MS gates of ~200 us, 1.5 ms detection), but they are demo values.
"""

from datetime import UTC, datetime

from pydantic import BaseModel, Field

# Small per-ion spread, as the addressed beam intensity differs across the chain.
_PI_TIME_SPREAD_NS = [0, 180, -120, 260, 40, -200, 120, 300, -60, 220]


class QubitCalibration(BaseModel):
    pi_time_ns: float = Field(gt=0, le=1_000_000, description="Duration of a pi rotation on this ion.")
    enabled: bool = Field(default=True, description="Disabled ions are never addressed.")


class CalibrationValues(BaseModel):
    """The part of a calibration an operator can edit."""

    qubits: list[QubitCalibration] = Field(min_length=1, max_length=10)
    ms_gate_ns: float = Field(gt=0, le=10_000_000, description="Duration of RXX(theta=0.5).")
    cooling_ns: int = Field(ge=0, le=100_000_000)
    detection_ns: int = Field(ge=0, le=100_000_000)
    settle_ns: int = Field(ge=0, le=1_000_000, description="Minimum gap between pulses on one channel.")
    clock_ns: int = Field(ge=1, le=1_000, description="RTIO coarse clock period.")
    max_sequence_ns: int = Field(gt=0, le=1_000_000_000, description="Coherence budget for one shot.")


class Calibration(CalibrationValues):
    version: int = 1
    updated_at: datetime
    updated_by: str


def default_calibration(qubits: int = 10) -> Calibration:
    return Calibration(
        qubits=[QubitCalibration(pi_time_ns=15_000 + _PI_TIME_SPREAD_NS[i % 10]) for i in range(qubits)],
        ms_gate_ns=200_000,
        cooling_ns=1_000_000,
        detection_ns=1_500_000,
        settle_ns=1_000,
        clock_ns=8,  # 125 MHz RTIO clock, as on ARTIQ/Sinara Kasli
        max_sequence_ns=10_000_000,
        updated_at=datetime.now(UTC),
        updated_by="factory defaults",
    )
