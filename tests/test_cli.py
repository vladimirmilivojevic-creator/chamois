import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chamois import cli
from chamois.arnica import QuantumCircuit
from chamois.calibration import default_calibration
from chamois.scheduler import compile_circuit
from tests.conftest import bell_circuit, make_client


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Routes the CLI's HTTP calls into an in-process app."""
    with make_client() as client:
        client.headers["Authorization"] = "Bearer operator-demo-token"
        monkeypatch.setattr(cli, "_client", lambda args: contextlib.nullcontext(client))
        yield client


def test_ascii_timeline_has_one_row_per_channel() -> None:
    timeline = compile_circuit(QuantumCircuit.model_validate(bell_circuit()), default_calibration()).timeline
    assert timeline is not None
    text = cli.ascii_timeline(timeline)
    for channel in ("cool", "addr_q0", "addr_q1", "ms_drive", "detect"):
        assert f"{channel:>9} |" in text
    assert "#" in text and "=" in text and "~" in text


def test_submit_queue_timeline_and_calibration(
    server: TestClient, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cli.main(["submit", "examples/bell.json", "--wait"])
    out = capsys.readouterr().out
    assert "submitted" in out and "finished" in out

    cli.main(["queue"])
    queue = capsys.readouterr().out
    assert "Bell pair" in queue and "finished" in queue
    short_id = queue.splitlines()[2].split()[0]

    png = tmp_path / "timeline.png"
    cli.main(["timeline", short_id, "--png", str(png)])
    assert "addr_q0" in capsys.readouterr().out and png.stat().st_size > 1000

    cli.main(["calibration", "--qubit", "2", "--pi-time", "15500", "--disable", "9"])
    calibration = capsys.readouterr().out
    assert "calibration v2" in calibration and "15.500 us" in calibration and "NO" in calibration
    cli.main(["calibration", "--enable", "9"])
    assert "calibration v3" in capsys.readouterr().out


def test_rejected_job_prints_violations(server: TestClient, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["submit", "examples/too_deep.json", "--wait"])
    assert "coherence budget" in capsys.readouterr().out
    cli.main(["queue"])
    job_id = capsys.readouterr().out.splitlines()[2].split()[0]
    cli.main(["timeline", job_id])
    assert "VIOLATION coherence_budget" in capsys.readouterr().out


def test_errors_exit_cleanly(server: TestClient, tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="no unique job"):
        cli.main(["timeline", "ffff"])
    bad = tmp_path / "bad.json"
    bad.write_text('{"payload": {"circuits": []}}', encoding="utf-8")
    with pytest.raises(SystemExit, match="422"):
        cli.main(["submit", str(bad)])


def test_unreachable_server(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="cannot reach"):
        cli.main(["--url", "http://127.0.0.1:9", "queue"])


def test_empty_queue(server: TestClient, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["queue"])
    assert "queue empty" in capsys.readouterr().out


def test_http_errors_are_reported(server: TestClient) -> None:
    server.headers["Authorization"] = "Bearer researcher-demo-token"
    # Depending on the httpx version, TestClient raises httpx's or its own HTTPStatusError:
    # the CLI turns the first into SystemExit, so only the status code is checked here.
    with pytest.raises(BaseException, match="403"):
        cli.main(["calibration", "--disable", "1"])


def test_crosscheck_command(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["crosscheck", "--shots", "500"])
    assert capsys.readouterr().out.count("True") == 3


def test_table_formatting() -> None:
    assert cli.table([["a", "bb"]], ["x", "y"]).splitlines() == ["x  y ", "-  --", "a  bb"]
    assert isinstance(cli.build_parser().parse_args(["queue"]).url, str)
