//! CHAMOIS pulse scheduler.
//!
//! Compiles AQT native gates (R, RZ, RXX, MEASURE) into a nanosecond-timed
//! pulse timeline per laser channel and rejects timelines that break the
//! device's timing rules. The Python bindings exchange JSON strings: the
//! Python side validates with Pydantic, the Rust side with serde, and neither
//! depends on the other's object model.

pub mod compile;
pub mod types;
pub mod validate;

pub use compile::compile;
pub use types::{CompileRequest, CompileResult, Timeline, ValidateRequest, Violation};
pub use validate::validate;

/// Compiles a JSON `CompileRequest` and returns a JSON `CompileResult`.
pub fn compile_json(request: &str) -> Result<String, serde_json::Error> {
    let request: CompileRequest = serde_json::from_str(request)?;
    serde_json::to_string(&compile(&request))
}

/// Validates a JSON `ValidateRequest` and returns a JSON list of violations.
pub fn validate_json(request: &str) -> Result<String, serde_json::Error> {
    let request: ValidateRequest = serde_json::from_str(request)?;
    serde_json::to_string(&validate(&request.timeline, &request.calibration))
}

#[cfg(feature = "python")]
use pyo3::{exceptions::PyValueError, prelude::*};

/// Python: `compile_json(request: str) -> str`. Releases the GIL while compiling.
#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "compile_json")]
fn py_compile_json(py: Python<'_>, request: &str) -> PyResult<String> {
    py.allow_threads(|| compile_json(request))
        .map_err(|error| PyValueError::new_err(error.to_string()))
}

/// Python: `validate_json(request: str) -> str`.
#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "validate_json")]
fn py_validate_json(py: Python<'_>, request: &str) -> PyResult<String> {
    py.allow_threads(|| validate_json(request))
        .map_err(|error| PyValueError::new_err(error.to_string()))
}

#[cfg(feature = "python")]
#[pymodule]
fn chamois_scheduler(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(py_compile_json, module)?)?;
    module.add_function(wrap_pyfunction!(py_validate_json, module)?)?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_boundary_accepts_arnica_operations() {
        let request = r#"{
            "number_of_qubits": 2,
            "operations": [
                {"operation": "RZ", "qubit": 0, "phi": 0.5},
                {"operation": "R", "qubit": 1, "phi": 0.25, "theta": 0.5},
                {"operation": "RXX", "qubits": [0, 1], "theta": 0.5},
                {"operation": "MEASURE"}
            ],
            "calibration": {
                "qubits": [
                    {"pi_time_ns": 15000.0, "enabled": true},
                    {"pi_time_ns": 15200.0, "enabled": true, "label": "ignored extra field"}
                ],
                "ms_gate_ns": 200000.0, "cooling_ns": 1000000, "detection_ns": 1500000,
                "settle_ns": 1000, "clock_ns": 8, "max_sequence_ns": 10000000
            }
        }"#;
        let result: CompileResult = serde_json::from_str(&compile_json(request).unwrap()).unwrap();
        assert!(result.ok);
        assert_eq!(result.timeline.unwrap().pulses.len(), 6);
    }

    #[test]
    fn json_boundary_keeps_floats_exact() {
        // Without serde_json's float_roundtrip feature this parses as -4.0.
        let op: types::Op =
            serde_json::from_str(r#"{"operation": "RZ", "qubit": 0, "phi": -3.9999999999999996}"#)
                .unwrap();
        assert_eq!(
            op,
            types::Op::Rz {
                qubit: 0,
                phi: -3.9999999999999996
            }
        );
    }

    #[test]
    fn json_boundary_reports_malformed_input() {
        assert!(compile_json(r#"{"operations": "nope"}"#).is_err());
    }
}
