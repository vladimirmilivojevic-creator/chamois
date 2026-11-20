"""Scheduler facade: uses the Rust core when it is installed, else the Python reference.

The Rust extension (`scheduler/`, built with maturin) receives one JSON string
and returns one JSON string, so the boundary is easy to test and debug.
"""

import importlib
import json
import logging
from collections.abc import Sequence
from types import ModuleType
from typing import Literal

from chamois import reference_scheduler
from chamois.arnica import Operation, QuantumCircuit
from chamois.calibration import CalibrationValues
from chamois.timeline import CompileResult, Timeline, Violation

logger = logging.getLogger(__name__)

Backend = Literal["rust", "python"]

_rust: ModuleType | None
try:
    _rust = importlib.import_module("chamois_scheduler")
except ImportError:  # pragma: no cover - depends on the environment
    _rust = None


def available_backend() -> Backend:
    return "rust" if _rust is not None else "python"


def rust_version() -> str | None:
    return getattr(_rust, "__version__", None) if _rust is not None else None


def _calibration_json(calibration: CalibrationValues) -> dict[str, object]:
    return calibration.model_dump(mode="json", include=set(CalibrationValues.model_fields))


def compile_operations(
    number_of_qubits: int,
    operations: Sequence[Operation],
    calibration: CalibrationValues,
    backend: Backend | None = None,
) -> CompileResult:
    backend = backend or available_backend()
    if backend == "python":
        return reference_scheduler.compile_operations(number_of_qubits, operations, calibration)
    if _rust is None:
        raise RuntimeError("the Rust scheduler (chamois_scheduler) is not installed")
    request = {
        "number_of_qubits": number_of_qubits,
        "operations": [op.model_dump() for op in operations],
        "calibration": _calibration_json(calibration),
    }
    return CompileResult.model_validate_json(_rust.compile_json(json.dumps(request)))


def compile_circuit(
    circuit: QuantumCircuit, calibration: CalibrationValues, backend: Backend | None = None
) -> CompileResult:
    return compile_operations(circuit.number_of_qubits, circuit.quantum_circuit, calibration, backend)


def validate_timeline(
    timeline: Timeline, calibration: CalibrationValues, backend: Backend | None = None
) -> list[Violation]:
    backend = backend or available_backend()
    if backend == "python":
        return reference_scheduler.validate_timeline(timeline, calibration)
    if _rust is None:
        raise RuntimeError("the Rust scheduler (chamois_scheduler) is not installed")
    request = {"timeline": timeline.model_dump(mode="json"), "calibration": _calibration_json(calibration)}
    raw = json.loads(_rust.validate_json(json.dumps(request)))
    return [Violation.model_validate(item) for item in raw]
