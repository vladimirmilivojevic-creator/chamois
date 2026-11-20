"""Python reference ("golden model") of the Rust scheduler in `scheduler/src`.

It implements the same rules line by line. Hypothesis tests compare both on
thousands of random circuits, and it lets the API run on machines without a
Rust toolchain. Production and CI use the Rust core (CHAMOIS_REQUIRE_RUST=1).
"""

import itertools
import math
from collections.abc import Sequence

from chamois.arnica import GateR, GateRXX, GateRZ, Measure, Operation
from chamois.calibration import CalibrationValues
from chamois.timeline import (
    COOL_CHANNEL,
    DETECT_CHANNEL,
    MS_DRIVE_CHANNEL,
    CompileResult,
    Pulse,
    PulseKind,
    Timeline,
    Violation,
    addressed_channel,
)


def to_clock(duration_ns: float, clock_ns: int) -> int:
    """Nearest RTIO clock tick, halves round up (same formula as the Rust core)."""
    ticks = math.floor(duration_ns / clock_ns + 0.5)
    return 0 if ticks <= 0 else ticks * clock_ns


def align_up(time_ns: int, clock_ns: int) -> int:
    return -(-time_ns // clock_ns) * clock_ns


def wrap_phase(phase: float) -> float:
    """Phase in [0, 2) units of pi.

    `x % 2.0` alone can return 2.0: for x = -1e-178 the exact result 2 - 1e-178
    rounds to 2.0. Hypothesis found this; the Rust core has the same guard.
    """
    wrapped = phase % 2.0
    return 0.0 if wrapped >= 2.0 else wrapped


def _qubits_of(op: Operation) -> list[int]:
    if isinstance(op, GateR | GateRZ):
        return [op.qubit]
    if isinstance(op, GateRXX):
        return list(op.qubits)
    return []


def check_operations(
    number_of_qubits: int, operations: Sequence[Operation], calibration: CalibrationValues
) -> list[Violation]:
    violations: list[Violation] = []
    n = number_of_qubits
    calibrated = len(calibration.qubits)
    if n > calibrated:
        violations.append(
            Violation(code="too_many_qubits", message=f"circuit uses {n} qubits, the device has {calibrated}")
        )
    last = len(operations) - 1
    for index, op in enumerate(operations):
        if isinstance(op, Measure):
            if index != last:
                violations.append(
                    Violation(
                        code="measure_position",
                        message=f"operation {index}: MEASURE must be the last operation",
                    )
                )
            continue
        if isinstance(op, GateRXX) and op.qubits[0] == op.qubits[1]:
            violations.append(
                Violation(
                    code="same_qubit",
                    message=f"operation {index}: RXX needs two different qubits, got {op.qubits[0]} twice",
                )
            )
        for qubit in _qubits_of(op):
            if qubit >= n:
                violations.append(
                    Violation(
                        code="qubit_out_of_range",
                        message=f"operation {index}: qubit {qubit} is outside the {n}-qubit register",
                    )
                )
            elif qubit < calibrated and not calibration.qubits[qubit].enabled and not isinstance(op, GateRZ):
                violations.append(
                    Violation(
                        code="qubit_disabled",
                        message=f"operation {index}: qubit {qubit} is disabled in the calibration",
                    )
                )
    if not operations or not isinstance(operations[-1], Measure):
        violations.append(Violation(code="measure_position", message="the circuit must end with MEASURE"))
    return violations


def compile_operations(
    number_of_qubits: int, operations: Sequence[Operation], calibration: CalibrationValues
) -> CompileResult:
    violations = check_operations(number_of_qubits, operations, calibration)
    if violations:
        return CompileResult(ok=False, timeline=None, violations=violations)

    clock, settle, n = calibration.clock_ns, calibration.settle_ns, number_of_qubits
    pulses: list[Pulse] = []
    free_at: dict[str, int] = {}  # channel -> earliest start of its next pulse
    frame = [0.0] * n  # virtual RZ phase per ion

    def earliest_start(channels: list[str], not_before: int) -> int:
        return align_up(max([not_before, *(free_at.get(c, 0) for c in channels)]), clock)

    cooling = to_clock(calibration.cooling_ns, clock)
    pulses.append(
        Pulse(channel=COOL_CHANNEL, start_ns=0, duration_ns=cooling, kind=PulseKind.COOL, qubits=list(range(n)))
    )
    gates_start = cooling + settle

    for index, op in enumerate(operations):
        if isinstance(op, GateR):
            duration = to_clock(op.theta * calibration.qubits[op.qubit].pi_time_ns, clock)
            if duration == 0:
                continue
            channel = addressed_channel(op.qubit)
            start = earliest_start([channel], gates_start)
            free_at[channel] = start + duration + settle
            pulses.append(
                Pulse(
                    channel=channel,
                    start_ns=start,
                    duration_ns=duration,
                    kind=PulseKind.R,
                    gate=index,
                    qubits=[op.qubit],
                    phase=wrap_phase(op.phi - frame[op.qubit]),
                )
            )
        elif isinstance(op, GateRZ):
            frame[op.qubit] += op.phi
        elif isinstance(op, GateRXX):
            duration = to_clock(op.theta / 0.5 * calibration.ms_gate_ns, clock)
            if duration == 0:
                continue
            a, b = op.qubits
            channels = [addressed_channel(a), addressed_channel(b), MS_DRIVE_CHANNEL]
            start = earliest_start(channels, gates_start)
            for channel in channels:
                free_at[channel] = start + duration + settle
            for qubit in (a, b):
                pulses.append(
                    Pulse(
                        channel=addressed_channel(qubit),
                        start_ns=start,
                        duration_ns=duration,
                        kind=PulseKind.MS,
                        gate=index,
                        qubits=[qubit],
                        phase=wrap_phase(-frame[qubit]),
                    )
                )
            pulses.append(
                Pulse(
                    channel=MS_DRIVE_CHANNEL,
                    start_ns=start,
                    duration_ns=duration,
                    kind=PulseKind.MS_DRIVE,
                    gate=index,
                    qubits=[a, b],
                )
            )
        else:
            break  # MEASURE

    detect_start = align_up(max([gates_start, *free_at.values()]), clock)
    detection = to_clock(calibration.detection_ns, clock)
    pulses.append(
        Pulse(
            channel=DETECT_CHANNEL,
            start_ns=detect_start,
            duration_ns=detection,
            kind=PulseKind.DETECT,
            qubits=list(range(n)),
        )
    )
    timeline = Timeline(number_of_qubits=n, pulses=pulses, duration_ns=detect_start + detection)
    violations = validate_timeline(timeline, calibration)
    return CompileResult(ok=not violations, timeline=timeline, violations=violations)


def validate_timeline(timeline: Timeline, calibration: CalibrationValues) -> list[Violation]:
    violations: list[Violation] = []
    clock = calibration.clock_ns
    by_channel: dict[str, list[Pulse]] = {}

    for pulse in timeline.pulses:
        if pulse.duration_ns == 0:
            violations.append(
                Violation(
                    code="empty_pulse",
                    message=f"{pulse.channel}: pulse at {pulse.start_ns} ns has zero duration",
                )
            )
        if pulse.start_ns % clock or pulse.duration_ns % clock:
            violations.append(
                Violation(
                    code="off_grid",
                    message=f"{pulse.channel}: pulse at {pulse.start_ns} ns is not on the {clock} ns RTIO clock grid",
                )
            )
        if pulse.kind in (PulseKind.R, PulseKind.MS):
            for qubit in pulse.qubits:
                if not (qubit < len(calibration.qubits) and calibration.qubits[qubit].enabled):
                    violations.append(
                        Violation(
                            code="qubit_disabled",
                            message=f"{pulse.channel}: qubit {qubit} is disabled or not calibrated",
                        )
                    )
        by_channel.setdefault(pulse.channel, []).append(pulse)

    for channel in sorted(by_channel):
        ordered = sorted(by_channel[channel], key=lambda p: p.start_ns)
        for previous, following in itertools.pairwise(ordered):
            if following.start_ns < previous.end_ns:
                violations.append(
                    Violation(
                        code="overlap",
                        message=(
                            f"{channel}: pulse at {following.start_ns} ns starts before "
                            f"the previous pulse ends at {previous.end_ns} ns"
                        ),
                    )
                )
            elif following.start_ns - previous.end_ns < calibration.settle_ns:
                violations.append(
                    Violation(
                        code="settle_time",
                        message=(
                            f"{channel}: only {following.start_ns - previous.end_ns} ns between pulses, "
                            f"the AOM needs {calibration.settle_ns} ns"
                        ),
                    )
                )

    end = max((p.end_ns for p in timeline.pulses), default=0)
    if end > calibration.max_sequence_ns:
        violations.append(
            Violation(
                code="coherence_budget",
                message=(
                    f"one shot takes {end / 1000:.1f} us, "
                    f"the coherence budget is {calibration.max_sequence_ns / 1000:.1f} us"
                ),
            )
        )
    return violations
