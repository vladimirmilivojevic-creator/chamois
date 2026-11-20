import itertools
from collections import defaultdict

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chamois import scheduler
from chamois.arnica import GateR, GateRXX, GateRZ, Measure, QuantumCircuit
from chamois.calibration import default_calibration
from chamois.reference_scheduler import align_up, to_clock
from chamois.timeline import PulseKind, Timeline
from tests.conftest import BACKENDS, RUST_AVAILABLE
from tests.strategies import circuits

CAL = default_calibration()


def compile_ops(ops: list, n: int, backend: str, calibration=CAL):  # type: ignore[no-untyped-def]
    circuit = QuantumCircuit(repetitions=1, quantum_circuit=[*ops, Measure()], number_of_qubits=n)
    return scheduler.compile_circuit(circuit, calibration, backend=backend)  # type: ignore[arg-type]


def codes(result) -> list[str]:  # type: ignore[no-untyped-def]
    return [v.code for v in result.violations]


@pytest.mark.parametrize("backend", BACKENDS)
def test_bell_schedule(backend: str) -> None:
    result = compile_ops([GateR(qubit=0, theta=0.5, phi=0.5), GateRXX(qubits=[0, 1], theta=0.5)], 2, backend)
    assert result.ok
    pulses = result.timeline.pulses
    assert [p.kind for p in pulses] == ["cool", "r", "ms", "ms", "ms_drive", "detect"]
    assert pulses[1].start_ns == 1_001_000 and pulses[1].duration_ns == 7_504
    assert pulses[2].start_ns == pulses[3].start_ns == pulses[4].start_ns == 1_009_504
    assert {p.channel for p in pulses[2:5]} == {"addr_q0", "addr_q1", "ms_drive"}
    assert result.timeline.duration_ns == pulses[-1].end_ns


@pytest.mark.parametrize("backend", BACKENDS)
def test_rz_is_a_virtual_phase_update(backend: str) -> None:
    result = compile_ops([GateRZ(qubit=0, phi=0.25), GateR(qubit=0, theta=1.0, phi=0.0)], 1, backend)
    pulses = result.timeline.pulses
    assert len(pulses) == 3
    assert pulses[1].phase == pytest.approx(1.75)


@pytest.mark.parametrize("backend", BACKENDS)
def test_zero_angle_gates_emit_no_pulse(backend: str) -> None:
    result = compile_ops([GateR(qubit=0, theta=0.0, phi=0.3), GateRXX(qubits=[0, 1], theta=0.0)], 2, backend)
    assert [p.kind for p in result.timeline.pulses] == ["cool", "detect"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_rejections(backend: str) -> None:
    bad = compile_ops([GateRXX(qubits=[1, 1], theta=0.5), GateR(qubit=4, theta=0.5, phi=0.0)], 2, backend)
    assert codes(bad) == ["same_qubit", "qubit_out_of_range"] and bad.timeline is None

    too_wide = compile_ops([], 12, backend)
    assert codes(too_wide) == ["too_many_qubits"]

    disabled = CAL.model_copy(deep=True)
    disabled.qubits[1].enabled = False
    result = compile_ops([GateRZ(qubit=1, phi=0.5), GateRXX(qubits=[0, 1], theta=0.5)], 2, backend, disabled)
    assert codes(result) == ["qubit_disabled"]  # RZ on a disabled ion is fine, the MS gate is not

    deep = compile_ops([GateRXX(qubits=[0, 1], theta=0.5)] * 48, 2, backend)
    assert codes(deep) == ["coherence_budget"]
    assert deep.timeline is not None  # kept so operators can see why


@pytest.mark.parametrize("backend", BACKENDS)
def test_measure_must_be_last(backend: str) -> None:
    circuit = QuantumCircuit.model_construct(
        repetitions=1, number_of_qubits=1, quantum_circuit=[Measure(), GateR(qubit=0, theta=0.5, phi=0.0)]
    )
    result = scheduler.compile_circuit(circuit, CAL, backend=backend)  # type: ignore[arg-type]
    assert codes(result) == ["measure_position", "measure_position"]


def test_clock_helpers() -> None:
    assert to_clock(7_500, 8) == 7_504
    assert to_clock(4, 8) == 8
    assert to_clock(3.9, 8) == 0
    assert align_up(1_001, 8) == 1_008


def assert_rtio_rules(timeline: Timeline) -> None:
    """The properties the hardware needs, checked directly (not through the validator)."""
    by_channel = defaultdict(list)
    for pulse in timeline.pulses:
        assert pulse.duration_ns > 0
        assert pulse.start_ns % CAL.clock_ns == 0 and pulse.duration_ns % CAL.clock_ns == 0
        assert 0.0 <= pulse.phase < 2.0
        by_channel[pulse.channel].append(pulse)
    for pulses in by_channel.values():
        pulses.sort(key=lambda p: p.start_ns)
        for previous, following in itertools.pairwise(pulses):
            assert following.start_ns >= previous.end_ns + CAL.settle_ns
    gates = [p for p in timeline.pulses if p.kind not in (PulseKind.COOL, PulseKind.DETECT)]
    cool, detect = timeline.pulses[0], timeline.pulses[-1]
    assert all(cool.end_ns < p.start_ns and p.end_ns < detect.start_ns for p in gates)


@pytest.mark.parametrize("backend", BACKENDS)
@settings(max_examples=150, deadline=None)
@given(circuit=circuits())
def test_every_compiled_timeline_obeys_the_rtio_rules(backend: str, circuit: QuantumCircuit) -> None:
    result = scheduler.compile_circuit(circuit, CAL, backend=backend)  # type: ignore[arg-type]
    assert set(codes(result)) <= {"coherence_budget"}
    assert result.timeline is not None
    assert_rtio_rules(result.timeline)
    assert scheduler.validate_timeline(result.timeline, CAL, backend=backend) == result.violations  # type: ignore[arg-type]


@pytest.mark.skipif(not RUST_AVAILABLE, reason="Rust scheduler not built")
@settings(max_examples=300, deadline=None)
@given(circuit=circuits(max_qubits=10, max_ops=40))
def test_rust_core_matches_python_reference(circuit: QuantumCircuit) -> None:
    rust = scheduler.compile_circuit(circuit, CAL, backend="rust")
    python = scheduler.compile_circuit(circuit, CAL, backend="python")
    assert codes(rust) == codes(python)
    if rust.timeline is None:
        assert python.timeline is None
        return
    assert python.timeline is not None
    assert rust.timeline.duration_ns == python.timeline.duration_ns
    for a, b in zip(rust.timeline.pulses, python.timeline.pulses, strict=True):
        assert a.model_dump(exclude={"phase"}) == b.model_dump(exclude={"phase"})
        assert a.phase == pytest.approx(b.phase, abs=1e-9)


@pytest.mark.parametrize("backend", BACKENDS)
@settings(max_examples=100, deadline=None)
@given(circuit=circuits(), data=st.data())
def test_validator_catches_a_broken_timeline(backend: str, circuit: QuantumCircuit, data: st.DataObject) -> None:
    """Move one gate pulse onto its predecessor on the same channel: the validator must notice."""
    timeline = scheduler.compile_circuit(circuit, CAL, backend=backend).timeline  # type: ignore[arg-type]
    assert timeline is not None
    gate_pulses = [i for i, p in enumerate(timeline.pulses) if p.kind in (PulseKind.R, PulseKind.MS)]
    if not gate_pulses:
        return
    index = data.draw(st.sampled_from(gate_pulses))
    broken = timeline.model_copy(deep=True)
    pulse = broken.pulses[index]
    shift = data.draw(st.sampled_from(["onto_cooling_channel", "off_grid"]))
    if shift == "off_grid":
        pulse.start_ns += 3
        expected = "off_grid"
    else:
        pulse.channel = "cool"
        pulse.start_ns = 0
        expected = "overlap"
    found = {v.code for v in scheduler.validate_timeline(broken, CAL, backend=backend)}  # type: ignore[arg-type]
    assert expected in found


@pytest.mark.parametrize("backend", BACKENDS)
def test_validator_reports_each_rule(backend: str) -> None:
    timeline = compile_ops([GateR(qubit=0, theta=1.0, phi=0.0), GateR(qubit=0, theta=1.0, phi=0.0)], 1, backend)
    assert timeline.timeline is not None
    broken = timeline.timeline.model_copy(deep=True)
    first, second = broken.pulses[1], broken.pulses[2]
    second.start_ns = first.end_ns + 8  # gap shorter than the 1 us settle time
    broken.pulses[-1].duration_ns = 0  # empty detection pulse
    disabled = CAL.model_copy(deep=True)
    disabled.qubits[0].enabled = False
    found = [v.code for v in scheduler.validate_timeline(broken, disabled, backend=backend)]  # type: ignore[arg-type]
    assert found.count("qubit_disabled") == 2
    assert "empty_pulse" in found and "settle_time" in found


def test_missing_rust_backend_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler, "_rust", None)
    assert scheduler.available_backend() == "python"
    assert scheduler.rust_version() is None
    circuit = QuantumCircuit(repetitions=1, quantum_circuit=[Measure()], number_of_qubits=1)
    with pytest.raises(RuntimeError):
        scheduler.compile_circuit(circuit, CAL, backend="rust")
    timeline = Timeline(number_of_qubits=1, pulses=[], duration_ns=0)
    with pytest.raises(RuntimeError):
        scheduler.validate_timeline(timeline, CAL, backend="rust")
