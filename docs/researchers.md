# For researchers

You can reach CHAMOIS in three ways: the web console, the ARNICA-style REST API, or Qiskit.

## 1. Qiskit, through AQT's own provider

CHAMOIS implements the small *direct-access* API that `qiskit-aqt-provider` uses for on-premise devices,
so standard Qiskit circuits run unchanged through AQT's official client code:

```python
from qiskit import QuantumCircuit, transpile
from chamois.qiskit_bridge import chamois_backend

backend = chamois_backend("https://chamois-api.onrender.com", "researcher-demo-token")

bell = QuantumCircuit(2)
bell.h(0)
bell.cx(0, 1)
bell.measure_all()

native = transpile(bell, backend, optimization_level=3)  # -> R, RZ, RXX
print(backend.run(native, shots=200).result().get_counts())  # {'00': ~100, '11': ~100}
```

`chamois_backend()` is a one-liner around `AQTProvider().direct_access().get_resource(url + "/direct", token)`.
The full script is in `examples/qiskit_on_chamois.py`.

## 2. ARNICA-style REST API

The routes, field names and limits follow AQT's public ARNICA API (v1). Angles are in units of π.

```bash
TOKEN=researcher-demo-token
URL=https://chamois-api.onrender.com

curl -s -X POST "$URL/api/v1/submit/default/chamois" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d @examples/bell.json
# {"job": {"job_id": "…", "resource_id": "chamois", …}, "response": {"status": "queued"}}

curl -s "$URL/api/v1/result/<job_id>?include_timing_data=true" -H "Authorization: Bearer $TOKEN"
```

| Gate | Fields | Range |
| --- | --- | --- |
| `R` | `qubit`, `theta`, `phi` | θ ∈ [0, 1], φ ∈ [0, 2] |
| `RZ` | `qubit`, `phi` | any finite φ (virtual, no pulse) |
| `RXX` | `qubits` (two different), `theta` | θ ∈ [0, 0.5]; 0.5 is maximally entangling |
| `MEASURE` | — | exactly once, last; measures all qubits |

Limits: up to 50 circuits per job, 2000 shots and 2000 operations per circuit, 10 qubits on this device.

A finished job returns one list of shots per circuit; each shot is `[bit of qubit 0, bit of qubit 1, …]`:

```json
{"status": "finished", "result": {"0": [[0, 0], [1, 1], [1, 1], [0, 0]]}}
```

If the scheduler rejects a circuit (for example a sequence longer than the coherence budget), the job ends
in `error` with a readable message, and `GET /api/v1/jobs/{id}/schedule` shows the timeline and the
violations.

## 3. Web console

Open the live demo, log in as *Researcher*, pick a preset and press **Submit job**. Click the job to see its
pulse timeline per laser channel and the measured outcomes. **Preview pulse schedule** compiles a circuit
without running it.
