"""Hypothesis strategies for random native-gate circuits."""

from hypothesis import strategies as st

from chamois.arnica import GateR, GateRXX, GateRZ, Measure, Operation, QuantumCircuit

angles = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)


@st.composite
def operations(draw: st.DrawFn, n: int) -> Operation:
    kinds = ["R", "RZ", "RXX"] if n > 1 else ["R", "RZ"]
    kind = draw(st.sampled_from(kinds))
    qubit = st.integers(min_value=0, max_value=n - 1)
    if kind == "R":
        return GateR(qubit=draw(qubit), theta=draw(angles), phi=draw(st.floats(0.0, 2.0)))
    if kind == "RZ":
        return GateRZ(qubit=draw(qubit), phi=draw(st.floats(-4.0, 4.0)))
    a, b = draw(st.lists(qubit, min_size=2, max_size=2, unique=True))
    return GateRXX(qubits=[a, b], theta=draw(st.floats(0.0, 0.5)))


@st.composite
def circuits(draw: st.DrawFn, max_qubits: int = 6, max_ops: int = 25) -> QuantumCircuit:
    n = draw(st.integers(min_value=1, max_value=max_qubits))
    ops = draw(st.lists(operations(n), min_size=0, max_size=max_ops))
    return QuantumCircuit(repetitions=1, quantum_circuit=[*ops, Measure()], number_of_qubits=n)
