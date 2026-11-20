"""Simulated trapped-ion device: runs a pulse timeline on a statevector.

The device does not see gates, only pulses. The rotation angle of every pulse
is derived from its duration and the calibrated pi time, exactly like on real
hardware. If the scheduler produced a wrong duration or phase, the results
would be wrong, and the cross-check against AQT's offline simulator would fail.
"""

from datetime import UTC, datetime

import numpy as np
import numpy.typing as npt

from chamois.calibration import Calibration, CalibrationValues, default_calibration
from chamois.timeline import PulseKind, Timeline

MAX_QUBITS = 10  # 2**10 amplitudes: tiny, but realistic for a demo trap

State = npt.NDArray[np.complex128]

_IDENTITY_2 = np.eye(2, dtype=np.complex128)
_IDENTITY_4 = np.eye(4, dtype=np.complex128)


def sigma(phase: float) -> State:
    """cos(phi) X + sin(phi) Y, with phi in units of pi."""
    e = np.exp(1j * np.pi * phase)
    return np.array([[0, np.conj(e)], [e, 0]], dtype=np.complex128)


def r_matrix(theta: float, phase: float) -> State:
    """R(theta, phi) = exp(-i theta/2 * sigma_phi), theta and phi in units of pi."""
    half = np.pi * theta / 2
    return np.asarray(np.cos(half) * _IDENTITY_2 - 1j * np.sin(half) * sigma(phase), dtype=np.complex128)


def ms_matrix(theta: float, phase_a: float, phase_b: float) -> State:
    """MS gate exp(-i theta/2 * sigma_a (x) sigma_b); RXX(theta) when both phases are 0."""
    half = np.pi * theta / 2
    matrix = np.cos(half) * _IDENTITY_4 - 1j * np.sin(half) * np.kron(sigma(phase_a), sigma(phase_b))
    return np.asarray(matrix, dtype=np.complex128)


def _apply(state: State, matrix: State, qubits: list[int], n: int) -> State:
    """Applies a 1- or 2-qubit matrix. Tensor axis i is qubit i."""
    k = len(qubits)
    tensor = state.reshape([2] * n)
    gate = matrix.reshape([2] * (2 * k))
    tensor = np.tensordot(gate, tensor, axes=(list(range(k, 2 * k)), qubits))
    tensor = np.moveaxis(tensor, list(range(k)), qubits)
    return tensor.reshape(-1)


def final_state(timeline: Timeline, calibration: CalibrationValues) -> State:
    n = timeline.number_of_qubits
    if n > MAX_QUBITS:
        raise ValueError(f"the simulator supports up to {MAX_QUBITS} qubits")
    state = np.zeros(2**n, dtype=np.complex128)
    state[0] = 1.0  # cooling + optical pumping prepare |0...0>

    # MS legs carry the per-ion phase; the drive pulse marks when the gate happens.
    leg_phase = {(p.gate, p.qubits[0]): p.phase for p in timeline.pulses if p.kind == PulseKind.MS}
    # Pulses on the same ion never overlap (the validator guarantees it), so
    # ordering by start time gives the correct operator order.
    for pulse in sorted(timeline.pulses, key=lambda p: (p.start_ns, p.channel)):
        if pulse.kind == PulseKind.R:
            qubit = pulse.qubits[0]
            theta = pulse.duration_ns / calibration.qubits[qubit].pi_time_ns
            state = _apply(state, r_matrix(theta, pulse.phase), [qubit], n)
        elif pulse.kind == PulseKind.MS_DRIVE:
            a, b = pulse.qubits
            theta = 0.5 * pulse.duration_ns / calibration.ms_gate_ns
            matrix = ms_matrix(theta, leg_phase[(pulse.gate, a)], leg_phase[(pulse.gate, b)])
            state = _apply(state, matrix, [a, b], n)
    return state


def probabilities(timeline: Timeline, calibration: CalibrationValues) -> npt.NDArray[np.float64]:
    """Probability of each basis state; index bit (n-1-i) is qubit i."""
    probs = np.abs(final_state(timeline, calibration)) ** 2
    return np.asarray(probs / probs.sum(), dtype=np.float64)


def index_to_bits(index: int, n: int) -> list[int]:
    """Basis-state index -> [bit of qubit 0, bit of qubit 1, ...] (ARNICA shot order)."""
    return [(index >> (n - 1 - qubit)) & 1 for qubit in range(n)]


def sample(timeline: Timeline, calibration: CalibrationValues, shots: int, rng: np.random.Generator) -> list[list[int]]:
    probs = probabilities(timeline, calibration)
    outcomes = rng.choice(len(probs), size=shots, p=probs)
    return [index_to_bits(int(index), timeline.number_of_qubits) for index in outcomes]


def shot_seconds(timeline: Timeline) -> float:
    """Wall-clock time of one shot on the device."""
    return timeline.duration_ns * 1e-9


class SimulatedDevice:
    """The single device behind the API, with its live calibration."""

    resource_id = "chamois"
    name = "CHAMOIS simulated 10-ion trap"

    def __init__(self, calibration: Calibration | None = None) -> None:
        self.calibration = calibration or default_calibration()
        self.online_since = datetime.now(UTC)

    @property
    def qubits(self) -> int:
        return len(self.calibration.qubits)

    def update_calibration(self, values: CalibrationValues, updated_by: str) -> Calibration:
        self.calibration = Calibration(
            **values.model_dump(),
            version=self.calibration.version + 1,
            updated_at=datetime.now(UTC),
            updated_by=updated_by,
        )
        return self.calibration
