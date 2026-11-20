//! Data types shared by the compiler and the validator.
//!
//! The JSON shape matches the Python models in `chamois/scheduler.py`, so a
//! request can cross the PyO3 boundary as one JSON string.

use serde::{Deserialize, Serialize};

/// One AQT native operation, exactly as it appears in an ARNICA circuit.
/// Angles are in units of pi, like in the ARNICA API.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
#[serde(tag = "operation")]
pub enum Op {
    #[serde(rename = "R")]
    R { qubit: usize, theta: f64, phi: f64 },
    #[serde(rename = "RZ")]
    Rz { qubit: usize, phi: f64 },
    #[serde(rename = "RXX")]
    Rxx { qubits: [usize; 2], theta: f64 },
    #[serde(rename = "MEASURE")]
    Measure,
}

/// Per-ion calibration. The position in `Calibration::qubits` is the qubit index.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct QubitCalibration {
    /// Duration of a pi rotation with the addressed 729 nm beam on this ion.
    pub pi_time_ns: f64,
    /// Disabled ions (lost, or being recalibrated) must not be addressed.
    pub enabled: bool,
}

/// Device calibration and timing constraints.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct Calibration {
    pub qubits: Vec<QubitCalibration>,
    /// Duration of a fully entangling MS gate, RXX(theta = 0.5).
    pub ms_gate_ns: f64,
    pub cooling_ns: u64,
    pub detection_ns: u64,
    /// Minimum gap between two pulses on one channel (AOM switching / settling).
    pub settle_ns: u64,
    /// RTIO coarse clock period: every timestamp and duration is a multiple of it.
    pub clock_ns: u64,
    /// Coherence budget: the whole shot must finish within this time.
    pub max_sequence_ns: u64,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PulseKind {
    /// Doppler + sideband cooling at the start of every shot (global 397 nm beam).
    Cool,
    /// Single-ion rotation with the addressed 729 nm beam.
    R,
    /// One leg of a Molmer-Sorensen gate on an addressed beam.
    Ms,
    /// Bichromatic RF drive shared by all MS gates (only one MS gate at a time).
    MsDrive,
    /// State detection of all ions (global 397 nm beam + camera/PMT).
    Detect,
}

/// One timed event on one output channel, like an RTIO event pair (on/off).
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct Pulse {
    pub channel: String,
    pub start_ns: u64,
    pub duration_ns: u64,
    pub kind: PulseKind,
    /// Index of the circuit operation that produced this pulse.
    pub gate: Option<usize>,
    pub qubits: Vec<usize>,
    /// Laser phase in units of pi, already corrected for virtual RZ frames.
    pub phase: f64,
}

impl Pulse {
    pub fn end_ns(&self) -> u64 {
        self.start_ns + self.duration_ns
    }
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct Timeline {
    pub number_of_qubits: usize,
    pub pulses: Vec<Pulse>,
    pub duration_ns: u64,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct Violation {
    pub code: String,
    pub message: String,
}

impl Violation {
    pub fn new(code: &str, message: String) -> Self {
        Self {
            code: code.to_string(),
            message,
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct CompileRequest {
    pub number_of_qubits: usize,
    pub operations: Vec<Op>,
    pub calibration: Calibration,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct CompileResult {
    pub ok: bool,
    pub timeline: Option<Timeline>,
    pub violations: Vec<Violation>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct ValidateRequest {
    pub timeline: Timeline,
    pub calibration: Calibration,
}

/// Channel name of the addressed beam on one ion.
pub fn addressed_channel(qubit: usize) -> String {
    format!("addr_q{qubit}")
}

pub const COOL_CHANNEL: &str = "cool";
pub const MS_DRIVE_CHANNEL: &str = "ms_drive";
pub const DETECT_CHANNEL: &str = "detect";
