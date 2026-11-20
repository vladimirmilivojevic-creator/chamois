import os
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chamois import scheduler
from chamois.api import create_app
from chamois.config import Settings

RESEARCHER = {"Authorization": "Bearer researcher-demo-token"}
OPERATOR = {"Authorization": "Bearer operator-demo-token"}

# CI sets CHAMOIS_REQUIRE_RUST=1: then the Rust tests must run (and fail if the build is missing).
RUST_REQUIRED = os.environ.get("CHAMOIS_REQUIRE_RUST") == "1"
RUST_AVAILABLE = scheduler.available_backend() == "rust"
BACKENDS = ["python", "rust"] if (RUST_AVAILABLE or RUST_REQUIRED) else ["python"]


def make_client(**overrides: Any) -> TestClient:
    settings = Settings(time_scale=0, seed=1, **overrides)
    return TestClient(create_app(settings))


@pytest.fixture
def client() -> Iterator[TestClient]:
    with make_client() as test_client:
        yield test_client


def wait_for(client: TestClient, job_id: str, headers: dict[str, str] = RESEARCHER) -> dict[str, Any]:
    """Polls the ARNICA result endpoint until the job reaches a final state."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        body: dict[str, Any] = client.get(f"/api/v1/result/{job_id}?include_timing_data=true", headers=headers).json()
        if body["response"]["status"] in ("finished", "error", "cancelled"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def bell_circuit(shots: int = 200) -> dict[str, Any]:
    return {
        "repetitions": shots,
        "number_of_qubits": 2,
        "quantum_circuit": [
            # H + CNOT as transpiled by qiskit-aqt-provider
            {"operation": "R", "qubit": 0, "theta": 1.0, "phi": 0.0},
            {"operation": "RXX", "qubits": [0, 1], "theta": 0.5},
            {"operation": "RZ", "qubit": 0, "phi": -0.5},
            {"operation": "R", "qubit": 0, "theta": 0.5, "phi": 1.0},
            {"operation": "R", "qubit": 1, "theta": 0.5, "phi": 1.0},
            {"operation": "MEASURE"},
        ],
    }


def job(*circuits: dict[str, Any], label: str = "test") -> dict[str, Any]:
    return {"job_type": "quantum_circuit", "label": label, "payload": {"circuits": list(circuits)}}
