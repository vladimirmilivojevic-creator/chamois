"""Standard Qiskit circuits through AQT's own qiskit-aqt-provider, against a real CHAMOIS server."""

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from qiskit import QuantumCircuit, transpile

from chamois import crosscheck
from chamois.api import create_app
from chamois.calibration import default_calibration
from chamois.config import Settings
from chamois.qiskit_bridge import chamois_backend, to_arnica


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(Settings(time_scale=0, seed=2)), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_official_aqt_provider_runs_on_chamois(server_url: str) -> None:
    backend = chamois_backend(server_url, "researcher-demo-token")
    assert backend.name == "chamois" and backend.num_qubits == 10

    bell = QuantumCircuit(2)
    bell.h(0)
    bell.cx(0, 1)
    bell.measure_all()
    counts = backend.run(transpile(bell, backend, optimization_level=3), shots=300).result().get_counts()
    assert set(counts) <= {"00", "11"} and sum(counts.values()) == 300


def test_crosscheck_against_aqt_offline_simulator() -> None:
    rows = crosscheck.run(default_calibration(), sizes=(2, 4, 10), shots=2000)
    for row in rows:
        assert row.passed, row
        assert set(row.chamois_counts) == {"0" * row.qubits, "1" * row.qubits}


def test_to_arnica_rejects_non_native_gates() -> None:
    circuit = QuantumCircuit(1)
    circuit.h(0)
    with pytest.raises(ValueError, match="native"):
        to_arnica(circuit, shots=10)


def test_to_arnica_normalises_negative_theta() -> None:
    circuit = QuantumCircuit(1)
    circuit.r(-1.0, 0.0, 0)
    circuit.measure_all()
    gate = to_arnica(circuit, shots=1).quantum_circuit[0]
    assert gate.model_dump() == pytest.approx(
        {"operation": "R", "qubit": 0, "theta": 1 / 3.141592653589793, "phi": 1.0}
    )


def test_tvd() -> None:
    assert crosscheck.tvd({"0": 1.0}, {"1": 1.0}) == 1.0
    assert crosscheck.tvd({"0": 0.5, "1": 0.5}, {"0": 0.5, "1": 0.5}) == 0.0
