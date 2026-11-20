"""Qiskit integration (needs the `qiskit` extra).

Two paths:
* `chamois_backend()` returns the official qiskit-aqt-provider backend
  (`DirectAccessResource`) pointed at a CHAMOIS server, so standard Qiskit
  circuits run on CHAMOIS through AQT's own client code.
* `to_arnica()` converts a circuit that is already in AQT native gates into
  an ARNICA circuit, used by the cross-check.
"""

import math
from typing import TYPE_CHECKING

from chamois.arnica import GateR, GateRXX, GateRZ, Measure, Operation, QuantumCircuit

if TYPE_CHECKING:
    import qiskit
    from qiskit_aqt_provider._direct.resource import DirectAccessResource


def chamois_backend(base_url: str, token: str) -> "DirectAccessResource":
    """Qiskit backend for a CHAMOIS server, e.g. chamois_backend("http://127.0.0.1:8000", token)."""
    from qiskit_aqt_provider import AQTProvider

    return AQTProvider().direct_access().get_resource(f"{base_url.rstrip('/')}/direct", token)


def active_qubits(circuit: "qiskit.QuantumCircuit") -> dict[int, int]:
    """Transpiled circuits span the whole backend (e.g. 20 qubits); keep only the used ones.

    Returns physical qubit -> compact index 0..k-1.
    """
    used = {
        circuit.find_bit(q).index
        for instruction in circuit.data
        if instruction.operation.name != "barrier"
        for q in instruction.qubits
    }
    return {physical: index for index, physical in enumerate(sorted(used))}


def to_arnica(circuit: "qiskit.QuantumCircuit", shots: int) -> QuantumCircuit:
    """Native-gate Qiskit circuit (r, rz, rxx, measure) -> ARNICA circuit; angles in units of pi."""
    compact = active_qubits(circuit)
    ops: list[Operation] = []
    for instruction in circuit.data:
        name = instruction.operation.name
        qubits = [compact[circuit.find_bit(q).index] for q in instruction.qubits if name != "barrier"]
        params = [float(p) / math.pi for p in instruction.operation.params]
        if name == "r":
            theta, phi = params
            if theta < 0:  # R(-t, p) == R(t, p + 1)
                theta, phi = -theta, phi + 1
            ops.append(GateR(qubit=qubits[0], theta=theta, phi=phi % 2))
        elif name == "rz":
            ops.append(GateRZ(qubit=qubits[0], phi=params[0]))
        elif name == "rxx":
            ops.append(GateRXX(qubits=qubits, theta=params[0]))
        elif name not in ("measure", "barrier"):
            raise ValueError(f"'{name}' is not an AQT native gate; transpile for an AQT backend first")
    ops.append(Measure())
    return QuantumCircuit(repetitions=shots, quantum_circuit=ops, number_of_qubits=len(compact))


def measured_qubits(circuit: "qiskit.QuantumCircuit") -> dict[int, int]:
    """Classical bit -> compact qubit index (see `active_qubits`), from the measure instructions."""
    compact = active_qubits(circuit)
    mapping: dict[int, int] = {}
    for instruction in circuit.data:
        if instruction.operation.name == "measure":
            qubit = compact[circuit.find_bit(instruction.qubits[0]).index]
            clbit = circuit.find_bit(instruction.clbits[0]).index
            mapping[clbit] = qubit
    return mapping
