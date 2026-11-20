# CHAMOIS

**CHAMOIS** is a mini control stack for a simulated trapped-ion quantum computer. It follows the path a
circuit takes on a real machine, from the user-facing API down to timed laser pulses:

1. An **ARNICA-style job API** accepts circuits in AQT's native gates (`R`, `RZ`, `RXX`, `MEASURE`) and
   moves jobs through `queued → ongoing → finished / error / cancelled`.
2. A **Rust scheduler** (PyO3 bindings) compiles the gates into a nanosecond-timed pulse timeline per
   laser channel, using per-ion calibration data, and rejects overlaps and timing violations, following
   the RTIO model of ARTIQ.
3. A **simulated 10-ion device** runs the timeline on a statevector. It only sees pulses, so the rotation
   angles come from pulse durations and calibrated π times, as on hardware.
4. Results for Bell and GHZ circuits are **cross-checked** against Qiskit's statevector and against the
   offline simulator of AQT's `qiskit-aqt-provider`.

!!! note "Not real hardware"
    CHAMOIS is a learning and demonstration project. The device is simulated, the calibration numbers are
    realistic for a 40Ca+ optical-qubit trap but invented, and there is no noise model yet.

## Who reads what

| You are | Start with |
| --- | --- |
| A researcher who wants to run circuits | [For researchers](researchers.md) |
| An operator who runs the device | [For operators](operators.md) |
| Curious how gates become pulses | [Timing model](timing-model.md) |
| Reviewing the code | [Architecture](architecture.md), [Development](development.md) |

## Live demo

* Console: <https://chamois-api.onrender.com>
* OpenAPI docs: <https://chamois-api.onrender.com/docs>
* Demo tokens: `researcher-demo-token`, `operator-demo-token`

The free server sleeps when idle; the first request can take up to a minute.
