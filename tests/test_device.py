import math

import numpy as np
import pytest
from hypothesis import given, settings
from qiskit import QuantumCircuit as QiskitCircuit
from qiskit.circuit.library import RGate, RXXGate
from qiskit.quantum_info import Operator, Statevector

from chamois import device
from chamois.arnica import GateR, GateRXX, GateRZ, QuantumCircuit
from chamois.calibration import CalibrationValues, default_calibration
from chamois.scheduler import compile_circuit
from tests.strategies import circuits

# A 1 ns clock makes pulse-duration rounding negligible, so physics can be compared exactly.
FINE = CalibrationValues(**{**default_calibration().model_dump(), "clock_ns": 1})


def test_matrices_match_qiskit_definitions() -> None:
    theta, phi = 0.37, 1.21
    assert np.allclose(device.r_matrix(theta, phi), Operator(RGate(theta * math.pi, phi * math.pi)).data)
    # Qiskit is little-endian (qubit 0 is the last tensor factor); RXX is symmetric, so this matches.
    assert np.allclose(device.ms_matrix(0.5, 0, 0), Operator(RXXGate(0.5 * math.pi)).data)
    for matrix in (device.r_matrix(theta, phi), device.ms_matrix(0.3, 0.2, 1.7)):
        assert np.allclose(matrix @ matrix.conj().T, np.eye(len(matrix)))


def to_qiskit(circuit: QuantumCircuit) -> QiskitCircuit:
    qc = QiskitCircuit(circuit.number_of_qubits)
    for op in circuit.quantum_circuit:
        if isinstance(op, GateR):
            qc.r(op.theta * math.pi, op.phi * math.pi, op.qubit)
        elif isinstance(op, GateRZ):
            qc.rz(op.phi * math.pi, op.qubit)
        elif isinstance(op, GateRXX):
            qc.rxx(op.theta * math.pi, *op.qubits)
    return qc


@settings(max_examples=150, deadline=None)
@given(circuit=circuits(max_qubits=5, max_ops=15))
def test_pulse_simulation_matches_qiskit_statevector(circuit: QuantumCircuit) -> None:
    """The device only sees pulses (durations, phases): this checks the whole compile -> run path."""
    result = compile_circuit(circuit, FINE)
    assert result.timeline is not None
    if not result.ok:
        return
    ours = device.probabilities(result.timeline, FINE)
    n = circuit.number_of_qubits
    ideal = Statevector(to_qiskit(circuit)).probabilities()  # index bit i = qubit i (little-endian)
    reordered = np.array([ideal[int("".join(map(str, device.index_to_bits(i, n)))[::-1], 2)] for i in range(2**n)])
    assert ours.sum() == pytest.approx(1.0)
    assert 0.5 * np.abs(ours - reordered).sum() < 1e-3


def test_bell_pair_is_perfectly_correlated() -> None:
    cal = default_calibration()
    circuit = QuantumCircuit(
        repetitions=500,
        number_of_qubits=2,
        quantum_circuit=[
            GateR(qubit=0, theta=1.0, phi=0.0),
            GateRXX(qubits=[0, 1], theta=0.5),
            GateRZ(qubit=0, phi=-0.5),
            GateR(qubit=0, theta=0.5, phi=1.0),
            GateR(qubit=1, theta=0.5, phi=1.0),
            {"operation": "MEASURE"},  # type: ignore[list-item]
        ],
    )
    timeline = compile_circuit(circuit, cal).timeline
    assert timeline is not None
    shots = device.sample(timeline, cal, 500, np.random.default_rng(3))
    assert all(a == b for a, b in shots)
    assert 150 < sum(a for a, _ in shots) < 350


def test_simulator_refuses_more_than_ten_qubits() -> None:
    timeline = device.Timeline(number_of_qubits=11, pulses=[], duration_ns=0)
    with pytest.raises(ValueError, match="up to 10"):
        device.final_state(timeline, default_calibration())


def test_calibration_updates_are_versioned() -> None:
    sim = device.SimulatedDevice()
    values = CalibrationValues(**sim.calibration.model_dump())
    values.qubits[3].enabled = False
    updated = sim.update_calibration(values, updated_by="Ana")
    assert updated.version == 2 and updated.updated_by == "Ana"
    assert sim.qubits == 10 and not sim.calibration.qubits[3].enabled
