import json
from pathlib import Path

from fastapi.testclient import TestClient

from chamois.api import STATIC_DIR
from tests.conftest import OPERATOR, RESEARCHER, bell_circuit, job, make_client, wait_for


def submit(client: TestClient, body: dict, headers: dict[str, str] = RESEARCHER) -> str:  # type: ignore[type-arg]
    response = client.post("/api/v1/submit/default/chamois", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["response"] == {"status": "queued", "timing_data": None}
    return str(response.json()["job"]["job_id"])


def test_health_and_console(client: TestClient) -> None:
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["scheduler"] in ("rust", "python")
    assert "CHAMOIS" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_requires_a_valid_token(client: TestClient) -> None:
    assert client.get("/api/v1/workspaces").status_code == 401
    assert client.get("/api/v1/workspaces", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/v1/me", headers=OPERATOR).json()["role"] == "operator"


def test_workspaces_and_resource_follow_arnica(client: TestClient) -> None:
    [workspace] = client.get("/api/v1/workspaces", headers=RESEARCHER).json()
    assert workspace["id"] == "default" and workspace["accepting_job_submissions"]
    assert workspace["resources"] == [{"id": "chamois", "name": "CHAMOIS simulated 10-ion trap", "type": "device"}]
    resource = client.get("/api/v1/resources/chamois", headers=RESEARCHER).json()
    assert resource["available_qubits"] == 10 and resource["status"] == "online"
    assert client.get("/api/v1/resources/ibex", headers=RESEARCHER).status_code == 404


def test_bell_job_runs_queued_ongoing_finished(client: TestClient) -> None:
    job_id = submit(client, job(bell_circuit(200), label="bell"))
    body = wait_for(client, job_id)
    assert body["job"]["label"] == "bell" and body["job"]["resource_id"] == "chamois"
    response = body["response"]
    assert response["status"] == "finished"
    assert [s["new_status"] for s in response["timing_data"]] == ["queued", "ongoing", "finished"]
    shots = response["result"]["0"]
    assert len(shots) == 200 and all(a == b for a, b in shots)

    schedule = client.get(f"/api/v1/jobs/{job_id}/schedule", headers=RESEARCHER).json()
    assert schedule["ok"] and len(schedule["timeline"]["pulses"]) == 8
    assert client.get(f"/api/v1/jobs/{job_id}/schedule?circuit=5", headers=RESEARCHER).status_code == 404
    # A finished job cannot be cancelled again (ARNICA answers 208).
    assert client.delete(f"/api/v1/jobs/{job_id}", headers=RESEARCHER).status_code == 208


def test_batch_reports_one_result_per_circuit(client: TestClient) -> None:
    job_id = submit(client, job(bell_circuit(10), bell_circuit(20)))
    result = wait_for(client, job_id)["response"]["result"]
    assert sorted(result) == ["0", "1"] and len(result["1"]) == 20
    [summary] = client.get("/api/v1/jobs", headers=RESEARCHER).json()
    assert summary["finished_count"] == 2 and summary["shots"] == 30 and summary["calibration_version"] == 1


def test_scheduler_rejection_becomes_a_job_error(client: TestClient) -> None:
    deep = json.loads(Path("examples/too_deep.json").read_text(encoding="utf-8"))
    job_id = submit(client, deep)
    response = wait_for(client, job_id)["response"]
    assert response["status"] == "error"
    assert "coherence budget" in response["message"]
    schedule = client.get(f"/api/v1/jobs/{job_id}/schedule", headers=RESEARCHER).json()
    assert schedule["violations"][0]["code"] == "coherence_budget" and schedule["timeline"] is not None


def test_invalid_circuits_are_rejected_with_422(client: TestClient) -> None:
    bad_angle = bell_circuit()
    bad_angle["quantum_circuit"][0]["theta"] = 1.5
    no_measure = bell_circuit()
    no_measure["quantum_circuit"].pop()
    for circuit in (bad_angle, no_measure):
        response = client.post("/api/v1/submit/default/chamois", json=job(circuit), headers=RESEARCHER)
        assert response.status_code == 422
    assert client.post("/api/v1/submit/other/chamois", json=job(bell_circuit()), headers=RESEARCHER).status_code == 403
    assert client.post("/api/v1/submit/default/ibex", json=job(bell_circuit()), headers=RESEARCHER).status_code == 404


def test_researchers_only_see_their_own_jobs(client: TestClient) -> None:
    job_id = submit(client, job(bell_circuit(5)), headers=OPERATOR)
    wait_for(client, job_id, OPERATOR)
    assert client.get(f"/api/v1/result/{job_id}", headers=RESEARCHER).status_code == 404
    assert client.get("/api/v1/jobs", headers=RESEARCHER).json() == []
    assert len(client.get("/api/v1/jobs", headers=OPERATOR).json()) == 1


def test_operator_calibration_changes_scheduling(client: TestClient) -> None:
    calibration = client.get("/api/v1/calibration", headers=RESEARCHER).json()
    calibration["qubits"][1]["enabled"] = False
    assert client.put("/api/v1/calibration", json=calibration, headers=RESEARCHER).status_code == 403
    updated = client.put("/api/v1/calibration", json=calibration, headers=OPERATOR).json()
    assert updated["version"] == 2 and updated["updated_by"] == "Operator (demo)"
    assert client.get("/api/v1/resources/chamois", headers=RESEARCHER).json()["available_qubits"] == 9

    response = wait_for(client, submit(client, job(bell_circuit())))["response"]
    assert response["status"] == "error" and "qubit 1 is disabled" in response["message"]


def test_schedule_preview(client: TestClient) -> None:
    preview = client.post("/api/v1/schedule", json=bell_circuit(), headers=RESEARCHER).json()
    assert preview["ok"] and preview["timeline"]["duration_ns"] > 2_500_000


def test_queue_full_returns_503() -> None:
    with make_client(max_queued_jobs=0) as client:
        response = client.post("/api/v1/submit/default/chamois", json=job(bell_circuit()), headers=RESEARCHER)
        assert response.status_code == 503
        assert not client.get("/api/v1/workspaces", headers=RESEARCHER).json()[0]["accepting_job_submissions"]


def test_direct_access_api(client: TestClient) -> None:
    assert client.get("/direct/status/ions", headers=RESEARCHER).json() == {"num_ions": 10}
    assert client.get("/direct/system/name", headers=RESEARCHER).json() == "chamois"
    job_id = client.put("/direct/circuit", json=bell_circuit(50), headers=RESEARCHER).json()
    result = client.get(f"/direct/circuit/result/{job_id}", headers=RESEARCHER).json()
    assert result["job_id"] == job_id and result["payload"]["status"] == "finished"
    assert len(result["payload"]["result"]) == 50
    [summary] = client.get("/api/v1/jobs", headers=RESEARCHER).json()
    assert summary["source"] == "direct"

    deep = json.loads(Path("examples/too_deep.json").read_text(encoding="utf-8"))["payload"]["circuits"][0]
    failed = client.put("/direct/circuit", json=deep, headers=RESEARCHER).json()
    assert client.get(f"/direct/circuit/result/{failed}", headers=RESEARCHER).json()["payload"] == {"status": "error"}


def test_crosscheck_endpoint(client: TestClient) -> None:
    rows = client.get("/api/v1/crosscheck", headers=RESEARCHER).json()
    assert [r["circuit"] for r in rows] == ["Bell", "GHZ-3", "GHZ-5"] and all(r["passed"] for r in rows)
    assert client.get("/api/v1/crosscheck", headers=RESEARCHER).json() == rows  # cached


def test_crosscheck_can_be_disabled() -> None:
    with make_client(crosscheck_enabled=False) as client:
        assert client.get("/api/v1/crosscheck", headers=RESEARCHER).status_code == 503


def test_console_presets_are_valid_jobs(client: TestClient) -> None:
    presets = json.loads((STATIC_DIR / "presets.json").read_text(encoding="utf-8"))
    outcomes = {}
    for preset in presets:
        job_id = submit(client, preset["job"])
        outcomes[preset["id"]] = wait_for(client, job_id)["response"]
    assert outcomes["deep"]["status"] == "error" and outcomes["outside"]["status"] == "error"
    for name in ("bell", "ghz5", "ghz10", "batch"):
        assert outcomes[name]["status"] == "finished"
    ghz10 = outcomes["ghz10"]["result"]["0"]
    assert {tuple(shot) for shot in ghz10} == {(0,) * 10, (1,) * 10}
