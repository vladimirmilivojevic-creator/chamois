# For operators

Operators see the whole queue, cancel any job and edit the device calibration. In the demo, use the token
`operator-demo-token`.

## Operator CLI

```bash
pip install -e ".[plot]"          # the CLI ships with the package
export CHAMOIS_URL=https://chamois-api.onrender.com
export CHAMOIS_TOKEN=operator-demo-token

chamois queue                      # all jobs, newest first
chamois calibration                # current calibration and version
chamois calibration --qubit 3 --pi-time 15100   # new pi time for ion 3, in ns
chamois calibration --disable 7    # stop addressing ion 7 (e.g. ion lost)
chamois calibration --enable 7
chamois timeline 3f2a91c0 --png timeline.png    # pulse timeline of a job (short id is fine)
chamois submit examples/ghz5.json --wait
chamois crosscheck                 # Bell/GHZ vs AQT offline simulator, runs locally
```

`chamois timeline` prints an ASCII plot (one row per channel) and, with `--png`, saves a matplotlib chart:

```text
gate window 1001.0 - 1225.6 us (one shot: 2726.6 us)
     cool |c                                                                       |
  addr_q0 |#####===============================================================### |
  addr_q1 |     ===============================================================####|
 ms_drive |     ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~   |
   detect |                                                                       d|
legend: # R pulse   = MS leg   ~ MS drive   c cooling   d detection
```

## Calibration

| Field | Meaning | Default |
| --- | --- | --- |
| `qubits[i].pi_time_ns` | π rotation time of the addressed 729 nm beam on ion *i* | 14.8–15.3 µs |
| `qubits[i].enabled` | disabled ions are never addressed | `true` |
| `ms_gate_ns` | duration of a fully entangling MS gate, `RXX(0.5)` | 200 µs |
| `cooling_ns` | Doppler + sideband cooling at the start of each shot | 1000 µs |
| `detection_ns` | state detection at the end of each shot | 1500 µs |
| `settle_ns` | minimum gap between two pulses on one channel (AOM switching) | 1 µs |
| `clock_ns` | RTIO coarse clock: every timestamp is a multiple of it | 8 ns |
| `max_sequence_ns` | coherence budget: one shot must finish within it | 10 ms |

Every save creates a new version (`version`, `updated_by`, `updated_at`). A job uses one calibration
snapshot from start to end, and the job summary records which version it ran with.

## What can go wrong

| Message | Cause | What to do |
| --- | --- | --- |
| `coherence_budget` | the shot is longer than `max_sequence_ns` | shorten the circuit, or check the budget |
| `qubit_disabled` | a gate addresses a disabled ion | re-enable the ion or remap the circuit |
| `qubit_out_of_range` / `too_many_qubits` | the circuit does not fit the register | fix `number_of_qubits` |
| `overlap`, `settle_time`, `off_grid` | the timeline breaks the RTIO rules | scheduler bug: the validator stopped it before the device ran |

## Health and deployment

* `GET /health` returns the version, the scheduler backend (`rust` in production) and the queue size.
* The Docker image sets `CHAMOIS_REQUIRE_RUST=1`, so the server refuses to start without the Rust build.
* Jobs live in memory: a restart clears the queue (see *Known limitations* in the README).
