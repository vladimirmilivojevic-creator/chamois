//! Gate -> pulse compiler.
//!
//! Every channel has its own cursor (`free_at`), like the RTIO "now" cursor in
//! ARTIQ: an operation starts as soon as all channels it needs are free, so
//! gates on different ions run in parallel and gates on the same ion queue up.

use std::collections::HashMap;

use crate::types::{
    addressed_channel, CompileRequest, CompileResult, Op, Pulse, PulseKind, Timeline, Violation,
    COOL_CHANNEL, DETECT_CHANNEL, MS_DRIVE_CHANNEL,
};
use crate::validate::validate;

/// Rounds a duration to the nearest RTIO clock tick (halves round up).
/// The Python reference uses the same formula, so both produce identical timelines.
pub fn to_clock(duration_ns: f64, clock_ns: u64) -> u64 {
    let ticks = (duration_ns / clock_ns as f64 + 0.5).floor();
    if ticks <= 0.0 {
        0
    } else {
        ticks as u64 * clock_ns
    }
}

/// Moves a timestamp forward to the next clock edge.
pub fn align_up(time_ns: u64, clock_ns: u64) -> u64 {
    time_ns.div_ceil(clock_ns) * clock_ns
}

/// Phases are kept in [0, 2) units of pi.
///
/// `rem_euclid` alone can return 2.0: for -1e-178 the exact result 2 - 1e-178
/// rounds to 2.0. Hypothesis found this in the Python reference.
pub fn wrap_phase(phase: f64) -> f64 {
    let wrapped = phase.rem_euclid(2.0);
    if wrapped >= 2.0 {
        0.0
    } else {
        wrapped
    }
}

/// Input checks that make a circuit impossible to schedule on this device.
fn check_operations(request: &CompileRequest) -> Vec<Violation> {
    let mut violations = Vec::new();
    let n = request.number_of_qubits;
    let calibrated = request.calibration.qubits.len();
    if n > calibrated {
        violations.push(Violation::new(
            "too_many_qubits",
            format!("circuit uses {n} qubits, the device has {calibrated}"),
        ));
    }
    let last = request.operations.len().saturating_sub(1);
    for (index, op) in request.operations.iter().enumerate() {
        let qubits: Vec<usize> = match op {
            Op::R { qubit, .. } | Op::Rz { qubit, .. } => vec![*qubit],
            Op::Rxx { qubits, .. } => qubits.to_vec(),
            Op::Measure => {
                if index != last {
                    violations.push(Violation::new(
                        "measure_position",
                        format!("operation {index}: MEASURE must be the last operation"),
                    ));
                }
                continue;
            }
        };
        if let Op::Rxx { qubits: [a, b], .. } = op {
            if a == b {
                violations.push(Violation::new(
                    "same_qubit",
                    format!("operation {index}: RXX needs two different qubits, got {a} twice"),
                ));
            }
        }
        for qubit in qubits {
            if qubit >= n {
                violations.push(Violation::new(
                    "qubit_out_of_range",
                    format!("operation {index}: qubit {qubit} is outside the {n}-qubit register"),
                ));
            } else if let Some(calibration) = request.calibration.qubits.get(qubit) {
                if !calibration.enabled && !matches!(op, Op::Rz { .. }) {
                    violations.push(Violation::new(
                        "qubit_disabled",
                        format!("operation {index}: qubit {qubit} is disabled in the calibration"),
                    ));
                }
            }
        }
    }
    if !matches!(request.operations.last(), Some(Op::Measure)) {
        violations.push(Violation::new(
            "measure_position",
            "the circuit must end with MEASURE".to_string(),
        ));
    }
    violations
}

/// Earliest clock-aligned start at which all `channels` are free.
fn earliest_start(
    free_at: &HashMap<String, u64>,
    channels: &[&str],
    not_before: u64,
    clock_ns: u64,
) -> u64 {
    let time = channels
        .iter()
        .map(|channel| free_at.get(*channel).copied().unwrap_or(0))
        .fold(not_before, u64::max);
    align_up(time, clock_ns)
}

/// Compiles a native-gate circuit into a timed pulse timeline and validates it.
pub fn compile(request: &CompileRequest) -> CompileResult {
    let violations = check_operations(request);
    if !violations.is_empty() {
        return CompileResult {
            ok: false,
            timeline: None,
            violations,
        };
    }

    let calibration = &request.calibration;
    let clock = calibration.clock_ns;
    let settle = calibration.settle_ns;
    let n = request.number_of_qubits;
    let all_qubits: Vec<usize> = (0..n).collect();

    let mut pulses = Vec::new();
    // channel -> earliest time its next pulse may start (previous end + settle time)
    let mut free_at: HashMap<String, u64> = HashMap::new();
    // Virtual Z: RZ is not a pulse, it shifts the phase of every later pulse on that ion.
    let mut frame = vec![0.0_f64; n];

    let cooling = to_clock(calibration.cooling_ns as f64, clock);
    pulses.push(Pulse {
        channel: COOL_CHANNEL.to_string(),
        start_ns: 0,
        duration_ns: cooling,
        kind: PulseKind::Cool,
        gate: None,
        qubits: all_qubits.clone(),
        phase: 0.0,
    });
    let gates_start = cooling + settle;

    for (index, op) in request.operations.iter().enumerate() {
        match *op {
            Op::R { qubit, theta, phi } => {
                let duration = to_clock(theta * calibration.qubits[qubit].pi_time_ns, clock);
                if duration == 0 {
                    continue; // R(0, phi) is the identity
                }
                let channel = addressed_channel(qubit);
                let start = earliest_start(&free_at, &[&channel], gates_start, clock);
                free_at.insert(channel.clone(), start + duration + settle);
                pulses.push(Pulse {
                    channel,
                    start_ns: start,
                    duration_ns: duration,
                    kind: PulseKind::R,
                    gate: Some(index),
                    qubits: vec![qubit],
                    phase: wrap_phase(phi - frame[qubit]),
                });
            }
            Op::Rz { qubit, phi } => frame[qubit] += phi,
            Op::Rxx {
                qubits: [a, b],
                theta,
            } => {
                let duration = to_clock(theta / 0.5 * calibration.ms_gate_ns, clock);
                if duration == 0 {
                    continue;
                }
                let (channel_a, channel_b) = (addressed_channel(a), addressed_channel(b));
                let channels = [channel_a.as_str(), channel_b.as_str(), MS_DRIVE_CHANNEL];
                let start = earliest_start(&free_at, &channels, gates_start, clock);
                for channel in channels {
                    free_at.insert(channel.to_string(), start + duration + settle);
                }
                for (qubit, channel) in [(a, channel_a), (b, channel_b)] {
                    pulses.push(Pulse {
                        channel,
                        start_ns: start,
                        duration_ns: duration,
                        kind: PulseKind::Ms,
                        gate: Some(index),
                        qubits: vec![qubit],
                        phase: wrap_phase(-frame[qubit]),
                    });
                }
                pulses.push(Pulse {
                    channel: MS_DRIVE_CHANNEL.to_string(),
                    start_ns: start,
                    duration_ns: duration,
                    kind: PulseKind::MsDrive,
                    gate: Some(index),
                    qubits: vec![a, b],
                    phase: 0.0,
                });
            }
            Op::Measure => break,
        }
    }

    let gates_end = free_at.values().copied().fold(gates_start, u64::max);
    let detect_start = align_up(gates_end, clock);
    let detection = to_clock(calibration.detection_ns as f64, clock);
    pulses.push(Pulse {
        channel: DETECT_CHANNEL.to_string(),
        start_ns: detect_start,
        duration_ns: detection,
        kind: PulseKind::Detect,
        gate: None,
        qubits: all_qubits,
        phase: 0.0,
    });

    let timeline = Timeline {
        number_of_qubits: n,
        pulses,
        duration_ns: detect_start + detection,
    };
    // Defence in depth: the compiler should never produce an invalid timeline,
    // but the validator checks it independently before the device runs it.
    let violations = validate(&timeline, calibration);
    CompileResult {
        ok: violations.is_empty(),
        timeline: Some(timeline),
        violations,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{Calibration, QubitCalibration};

    fn calibration(qubits: usize) -> Calibration {
        Calibration {
            qubits: (0..qubits)
                .map(|i| QubitCalibration {
                    pi_time_ns: 15_000.0 + 200.0 * i as f64,
                    enabled: true,
                })
                .collect(),
            ms_gate_ns: 200_000.0,
            cooling_ns: 1_000_000,
            detection_ns: 1_500_000,
            settle_ns: 1_000,
            clock_ns: 8,
            max_sequence_ns: 10_000_000,
        }
    }

    fn request(n: usize, operations: Vec<Op>) -> CompileRequest {
        CompileRequest {
            number_of_qubits: n,
            operations,
            calibration: calibration(10),
        }
    }

    fn bell() -> Vec<Op> {
        vec![
            Op::R {
                qubit: 0,
                theta: 0.5,
                phi: 0.5,
            },
            Op::Rxx {
                qubits: [0, 1],
                theta: 0.5,
            },
            Op::Measure,
        ]
    }

    #[test]
    fn bell_circuit_has_cooling_gates_and_detection() {
        let result = compile(&request(2, bell()));
        assert!(result.ok, "{:?}", result.violations);
        let timeline = result.timeline.unwrap();
        let kinds: Vec<PulseKind> = timeline.pulses.iter().map(|p| p.kind).collect();
        assert_eq!(
            kinds,
            vec![
                PulseKind::Cool,
                PulseKind::R,
                PulseKind::Ms,
                PulseKind::Ms,
                PulseKind::MsDrive,
                PulseKind::Detect
            ]
        );
        let r = &timeline.pulses[1];
        assert_eq!(r.start_ns, 1_001_000); // after cooling + settle time
        assert_eq!(r.duration_ns, 7_504); // pi/2 of 15 us, rounded to the 8 ns clock
        let ms = &timeline.pulses[2];
        assert_eq!(ms.start_ns, align_up(r.end_ns() + 1_000, 8));
        assert_eq!(ms.duration_ns, 200_000);
        assert_eq!(timeline.duration_ns, timeline.pulses[5].end_ns());
    }

    #[test]
    fn rz_is_virtual_and_shifts_later_phases() {
        let ops = vec![
            Op::Rz {
                qubit: 0,
                phi: 0.25,
            },
            Op::R {
                qubit: 0,
                theta: 1.0,
                phi: 0.0,
            },
            Op::Measure,
        ];
        let timeline = compile(&request(1, ops)).timeline.unwrap();
        assert_eq!(timeline.pulses.len(), 3); // cool, R, detect: no pulse for RZ
        assert!((timeline.pulses[1].phase - 1.75).abs() < 1e-12);
    }

    #[test]
    fn single_qubit_gates_on_different_ions_run_in_parallel() {
        let ops = vec![
            Op::R {
                qubit: 0,
                theta: 1.0,
                phi: 0.0,
            },
            Op::R {
                qubit: 1,
                theta: 1.0,
                phi: 0.0,
            },
            Op::Measure,
        ];
        let timeline = compile(&request(2, ops)).timeline.unwrap();
        assert_eq!(timeline.pulses[1].start_ns, timeline.pulses[2].start_ns);
    }

    #[test]
    fn ms_gates_share_one_drive_and_never_overlap() {
        let ops = vec![
            Op::Rxx {
                qubits: [0, 1],
                theta: 0.5,
            },
            Op::Rxx {
                qubits: [2, 3],
                theta: 0.5,
            },
            Op::Measure,
        ];
        let timeline = compile(&request(4, ops)).timeline.unwrap();
        let drives: Vec<&Pulse> = timeline
            .pulses
            .iter()
            .filter(|p| p.kind == PulseKind::MsDrive)
            .collect();
        assert_eq!(drives.len(), 2);
        assert!(drives[1].start_ns >= drives[0].end_ns() + 1_000);
    }

    #[test]
    fn rejects_bad_qubits_before_scheduling() {
        let ops = vec![
            Op::Rxx {
                qubits: [1, 1],
                theta: 0.5,
            },
            Op::R {
                qubit: 5,
                theta: 0.5,
                phi: 0.0,
            },
            Op::Measure,
        ];
        let result = compile(&request(2, ops));
        assert!(!result.ok);
        assert!(result.timeline.is_none());
        let codes: Vec<&str> = result.violations.iter().map(|v| v.code.as_str()).collect();
        assert_eq!(codes, vec!["same_qubit", "qubit_out_of_range"]);
    }

    #[test]
    fn rejects_disabled_qubit() {
        let mut req = request(2, bell());
        req.calibration.qubits[1].enabled = false;
        let result = compile(&req);
        assert_eq!(result.violations[0].code, "qubit_disabled");
    }

    #[test]
    fn rejects_sequences_longer_than_the_coherence_budget() {
        let mut ops: Vec<Op> = (0..50)
            .map(|_| Op::Rxx {
                qubits: [0, 1],
                theta: 0.5,
            })
            .collect();
        ops.push(Op::Measure);
        let result = compile(&request(2, ops));
        assert!(!result.ok);
        assert!(result.timeline.is_some()); // kept so the operator can see why
        assert_eq!(result.violations[0].code, "coherence_budget");
    }

    #[test]
    fn measure_must_be_last() {
        let ops = vec![
            Op::Measure,
            Op::R {
                qubit: 0,
                theta: 0.5,
                phi: 0.0,
            },
        ];
        let codes: Vec<String> = compile(&request(1, ops))
            .violations
            .into_iter()
            .map(|v| v.code)
            .collect();
        assert_eq!(codes, vec!["measure_position", "measure_position"]);
    }

    #[test]
    fn phase_wrapping_stays_below_two() {
        assert_eq!(wrap_phase(-1e-178), 0.0);
        assert!((wrap_phase(-0.5) - 1.5).abs() < 1e-12);
        assert_eq!(wrap_phase(4.0), 0.0);
    }

    #[test]
    fn clock_rounding_matches_the_python_reference() {
        assert_eq!(to_clock(7_500.0, 8), 7_504);
        assert_eq!(to_clock(4.0, 8), 8); // half a tick rounds up
        assert_eq!(to_clock(3.9, 8), 0);
        assert_eq!(align_up(1_001, 8), 1_008);
    }
}
