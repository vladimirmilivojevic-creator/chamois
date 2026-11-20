"""Cross-check CHAMOIS against Qiskit and AQT's offline simulator (needs the `qiskit` extra).

For Bell and GHZ circuits:
1. Qiskit transpiles the circuit for AQT's offline simulator -> native R/RZ/RXX gates.
2. CHAMOIS schedules those gates into pulses and simulates the pulses.
3. Exact check: CHAMOIS probabilities vs Qiskit's ideal Statevector (TVD ~ 0).
4. Sampled check: CHAMOIS shots vs the offline simulator's shots (TVD < 0.1).
"""

from collections import Counter
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel

from chamois import device
from chamois.calibration import CalibrationValues
from chamois.qiskit_bridge import measured_qubits, to_arnica
from chamois.scheduler import compile_circuit

if TYPE_CHECKING:
    from qiskit import QuantumCircuit

EXACT_TOLERANCE = 1e-3  # pulse durations are rounded to the 8 ns clock
SAMPLED_TOLERANCE = 0.1


class CrossCheckRow(BaseModel):
    circuit: str
    qubits: int
    native_gates: int
    pulses: int
    shot_duration_us: float
    tvd_exact: float
    tvd_sampled: float
    chamois_counts: dict[str, int]
    aqt_counts: dict[str, int]
    passed: bool


def tvd(p: dict[str, float], q: dict[str, float]) -> float:
    """Total variation distance between two distributions."""
    keys = set(p) | set(q)
    return 0.5 * sum(abs(p.get(k, 0.0) - q.get(k, 0.0)) for k in keys)


def normalise(counts: dict[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    return {key: value / total for key, value in counts.items()}


def ghz(n: int) -> "QuantumCircuit":
    from qiskit import QuantumCircuit

    circuit = QuantumCircuit(n, name="Bell" if n == 2 else f"GHZ-{n}")
    circuit.h(0)
    for qubit in range(n - 1):
        circuit.cx(qubit, qubit + 1)
    circuit.measure_all()
    return circuit


def run(
    calibration: CalibrationValues, sizes: tuple[int, ...] = (2, 3, 5), shots: int = 2000, seed: int = 7
) -> list[CrossCheckRow]:
    from qiskit import transpile
    from qiskit.quantum_info import Statevector
    from qiskit_aqt_provider import AQTProvider

    backend = AQTProvider().offline.ideal()
    rng = np.random.default_rng(seed)
    rows = []
    for n in sizes:
        logical = ghz(n)
        native = transpile(logical, backend, optimization_level=3, seed_transpiler=seed)
        clbit_to_qubit = measured_qubits(native)

        compiled = compile_circuit(to_arnica(native, shots), calibration)
        if not compiled.ok or compiled.timeline is None:
            raise RuntimeError(f"{logical.name}: {compiled.violations}")
        timeline = compiled.timeline

        def key(bits: list[int], mapping: dict[int, int] = clbit_to_qubit) -> str:
            # Qiskit bitstrings put classical bit 0 on the right.
            return "".join(str(bits[mapping[c]]) for c in sorted(mapping, reverse=True))

        probs = device.probabilities(timeline, calibration)
        chamois_exact: dict[str, float] = {}
        for index, p in enumerate(probs):
            if p > 1e-12:
                k = key(device.index_to_bits(index, timeline.number_of_qubits))
                chamois_exact[k] = chamois_exact.get(k, 0.0) + float(p)
        ideal = Statevector(logical.remove_final_measurements(inplace=False)).probabilities_dict()

        chamois_counts = dict(Counter(key(s) for s in device.sample(timeline, calibration, shots, rng)))
        aqt_counts = backend.run(native, shots=shots, seed_simulator=seed).result().get_counts()

        exact = tvd(chamois_exact, ideal)
        sampled = tvd(normalise(chamois_counts), normalise(aqt_counts))
        rows.append(
            CrossCheckRow(
                circuit=logical.name,
                qubits=n,
                native_gates=sum(1 for i in native.data if i.operation.name in ("r", "rz", "rxx")),
                pulses=len(timeline.pulses),
                shot_duration_us=timeline.duration_ns / 1000,
                tvd_exact=round(exact, 6),
                tvd_sampled=round(sampled, 4),
                chamois_counts=chamois_counts,
                aqt_counts=dict(aqt_counts),
                passed=exact < EXACT_TOLERANCE and sampled < SAMPLED_TOLERANCE,
            )
        )
    return rows
