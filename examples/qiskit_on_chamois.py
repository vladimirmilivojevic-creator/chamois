"""Run a standard Qiskit circuit on CHAMOIS through AQT's own qiskit-aqt-provider.

pip install -e ".[qiskit]"
python examples/qiskit_on_chamois.py https://chamois-api.onrender.com
"""

import sys

from qiskit import QuantumCircuit, transpile

from chamois.qiskit_bridge import chamois_backend

url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
backend = chamois_backend(url, "researcher-demo-token")
print(f"backend {backend.name}: {backend.num_qubits} qubits")

ghz = QuantumCircuit(4)
ghz.h(0)
for qubit in range(3):
    ghz.cx(qubit, qubit + 1)
ghz.measure_all()

native = transpile(ghz, backend, optimization_level=3)  # R, RZ, RXX
print(f"native gates: {dict(native.count_ops())}")
counts = backend.run(native, shots=500).result().get_counts()
print(counts)  # only '0000' and '1111'
