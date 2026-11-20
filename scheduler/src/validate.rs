//! Timeline validator, modelled on the errors ARTIQ's RTIO core raises.
//!
//! ARTIQ rejects events that go back in time on a channel (sequence error).
//! Here the same idea is checked before the device runs anything: overlaps,
//! too-short gaps, timestamps off the RTIO clock grid, disabled ions and
//! shots that exceed the coherence budget.

use std::collections::BTreeMap;

use crate::types::{Calibration, Pulse, PulseKind, Timeline, Violation};

pub fn validate(timeline: &Timeline, calibration: &Calibration) -> Vec<Violation> {
    let mut violations = Vec::new();
    let clock = calibration.clock_ns;
    let mut by_channel: BTreeMap<&str, Vec<&Pulse>> = BTreeMap::new();

    for pulse in &timeline.pulses {
        if pulse.duration_ns == 0 {
            violations.push(Violation::new(
                "empty_pulse",
                format!("{}: pulse at {} ns has zero duration", pulse.channel, pulse.start_ns),
            ));
        }
        if pulse.start_ns % clock != 0 || pulse.duration_ns % clock != 0 {
            violations.push(Violation::new(
                "off_grid",
                format!(
                    "{}: pulse at {} ns is not on the {} ns RTIO clock grid",
                    pulse.channel, pulse.start_ns, clock
                ),
            ));
        }
        if matches!(pulse.kind, PulseKind::R | PulseKind::Ms) {
            for qubit in &pulse.qubits {
                let enabled = calibration.qubits.get(*qubit).is_some_and(|q| q.enabled);
                if !enabled {
                    violations.push(Violation::new(
                        "qubit_disabled",
                        format!("{}: qubit {qubit} is disabled or not calibrated", pulse.channel),
                    ));
                }
            }
        }
        by_channel.entry(&pulse.channel).or_default().push(pulse);
    }

    for (channel, mut pulses) in by_channel {
        pulses.sort_by_key(|pulse| pulse.start_ns);
        for pair in pulses.windows(2) {
            let (previous, next) = (pair[0], pair[1]);
            if next.start_ns < previous.end_ns() {
                violations.push(Violation::new(
                    "overlap",
                    format!(
                        "{channel}: pulse at {} ns starts before the previous pulse ends at {} ns",
                        next.start_ns,
                        previous.end_ns()
                    ),
                ));
            } else if next.start_ns - previous.end_ns() < calibration.settle_ns {
                violations.push(Violation::new(
                    "settle_time",
                    format!(
                        "{channel}: only {} ns between pulses, the AOM needs {} ns",
                        next.start_ns - previous.end_ns(),
                        calibration.settle_ns
                    ),
                ));
            }
        }
    }

    let end = timeline.pulses.iter().map(Pulse::end_ns).max().unwrap_or(0);
    if end > calibration.max_sequence_ns {
        violations.push(Violation::new(
            "coherence_budget",
            format!(
                "one shot takes {:.1} us, the coherence budget is {:.1} us",
                end as f64 / 1000.0,
                calibration.max_sequence_ns as f64 / 1000.0
            ),
        ));
    }
    violations
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::QubitCalibration;

    fn calibration() -> Calibration {
        Calibration {
            qubits: vec![
                QubitCalibration {
                    pi_time_ns: 15_000.0,
                    enabled: true,
                },
                QubitCalibration {
                    pi_time_ns: 15_000.0,
                    enabled: false,
                },
            ],
            ms_gate_ns: 200_000.0,
            cooling_ns: 1_000,
            detection_ns: 1_000,
            settle_ns: 1_000,
            clock_ns: 8,
            max_sequence_ns: 100_000,
        }
    }

    fn pulse(channel: &str, start_ns: u64, duration_ns: u64, qubit: usize) -> Pulse {
        Pulse {
            channel: channel.to_string(),
            start_ns,
            duration_ns,
            kind: PulseKind::R,
            gate: Some(0),
            qubits: vec![qubit],
            phase: 0.0,
        }
    }

    fn codes(pulses: Vec<Pulse>) -> Vec<String> {
        let timeline = Timeline {
            number_of_qubits: 2,
            pulses,
            duration_ns: 0,
        };
        validate(&timeline, &calibration())
            .into_iter()
            .map(|v| v.code)
            .collect()
    }

    #[test]
    fn accepts_a_well_spaced_channel() {
        assert!(codes(vec![pulse("addr_q0", 0, 800, 0), pulse("addr_q0", 1_800, 800, 0)]).is_empty());
    }

    #[test]
    fn detects_overlap_even_when_pulses_are_unsorted() {
        assert_eq!(
            codes(vec![pulse("addr_q0", 800, 800, 0), pulse("addr_q0", 0, 1_600, 0)]),
            vec!["overlap"]
        );
    }

    #[test]
    fn detects_short_gap() {
        assert_eq!(
            codes(vec![pulse("addr_q0", 0, 800, 0), pulse("addr_q0", 1_000, 800, 0)]),
            vec!["settle_time"]
        );
    }

    #[test]
    fn detects_off_grid_timestamps_and_disabled_ions() {
        assert_eq!(codes(vec![pulse("addr_q1", 3, 800, 1)]), vec!["off_grid", "qubit_disabled"]);
    }

    #[test]
    fn detects_empty_pulses_and_coherence_budget() {
        assert_eq!(
            codes(vec![pulse("addr_q0", 0, 0, 0), pulse("addr_q0", 200_000, 8, 0)]),
            vec!["empty_pulse", "coherence_budget"]
        );
    }
}
