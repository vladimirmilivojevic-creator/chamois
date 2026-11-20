"""Operator CLI: queue, calibration, pulse timeline, submit, cross-check.

    chamois queue
    chamois calibration [--qubit 3 --pi-time 15100 | --disable 3 | --enable 3]
    chamois timeline <job-id> [--circuit 0] [--png timeline.png]
    chamois submit examples/bell.json [--wait]
    chamois crosscheck
    chamois serve

Server URL and token come from --url / --token or CHAMOIS_URL / CHAMOIS_TOKEN.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from chamois.timeline import PulseKind, Timeline

DEFAULT_URL = "http://127.0.0.1:8000"
DEFAULT_TOKEN = "operator-demo-token"
FINAL = {"finished", "error", "cancelled"}
# ASCII so it prints on every terminal, including the Windows console.
SYMBOL = {PulseKind.COOL: "c", PulseKind.R: "#", PulseKind.MS: "=", PulseKind.MS_DRIVE: "~", PulseKind.DETECT: "d"}


def _client(args: argparse.Namespace) -> httpx.Client:
    return httpx.Client(base_url=args.url.rstrip("/"), headers={"Authorization": f"Bearer {args.token}"}, timeout=60)


def _get(args: argparse.Namespace, path: str) -> Any:
    with _client(args) as client:
        response = client.get(path)
        response.raise_for_status()
        return response.json()


def table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows, strict=False)]
    lines = ["  ".join(str(cell).ljust(width) for cell, width in zip(row, widths, strict=False)) for row in rows]
    header = "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=False))
    return "\n".join([header, "  ".join("-" * w for w in widths), *lines])


def cmd_queue(args: argparse.Namespace) -> None:
    jobs = _get(args, "/api/v1/jobs")
    rows = [
        [
            job["job_id"][:8],
            job["status"],
            f"{job['finished_count']}/{job['circuits']}",
            str(job["shots"]),
            job["owner"],
            job["label"] or "",
            job["created_at"][11:19],
        ]
        for job in jobs
    ]
    print(table(rows, ["job", "status", "circuits", "shots", "owner", "label", "created"]) if rows else "queue empty")


def cmd_calibration(args: argparse.Namespace) -> None:
    calibration = _get(args, "/api/v1/calibration")
    changed = False
    if args.qubit is not None and args.pi_time is not None:
        calibration["qubits"][args.qubit]["pi_time_ns"] = args.pi_time
        changed = True
    for qubit, enabled in ((args.disable, False), (args.enable, True)):
        if qubit is not None:
            calibration["qubits"][qubit]["enabled"] = enabled
            changed = True
    if changed:
        with _client(args) as client:
            response = client.put("/api/v1/calibration", json=calibration)
            response.raise_for_status()
            calibration = response.json()
    print(f"calibration v{calibration['version']} by {calibration['updated_by']} at {calibration['updated_at'][:19]}")
    print(
        f"MS gate {calibration['ms_gate_ns'] / 1000:.1f} us | cooling {calibration['cooling_ns'] / 1000:.0f} us"
        f" | detection {calibration['detection_ns'] / 1000:.0f} us | settle {calibration['settle_ns']} ns"
        f" | RTIO clock {calibration['clock_ns']} ns | budget {calibration['max_sequence_ns'] / 1000:.0f} us"
    )
    rows = [
        [str(i), f"{q['pi_time_ns'] / 1000:.3f} us", "yes" if q["enabled"] else "NO"]
        for i, q in enumerate(calibration["qubits"])
    ]
    print(table(rows, ["qubit", "pi time", "enabled"]))


def ascii_timeline(timeline: Timeline, width: int = 72) -> str:
    """One row per channel over the gate window (cooling and detection are shortened)."""
    gates = [p for p in timeline.pulses if p.kind not in (PulseKind.COOL, PulseKind.DETECT)]
    start = min((p.start_ns for p in gates), default=0)
    end = max((p.end_ns for p in gates), default=start + 1)
    span = max(end - start, 1)
    lines = [f"gate window {start / 1000:.1f} - {end / 1000:.1f} us (one shot: {timeline.duration_ns / 1000:.1f} us)"]
    for channel in timeline.channels():
        row = [" "] * width
        for pulse in (p for p in timeline.pulses if p.channel == channel):
            if pulse.kind == PulseKind.COOL:
                row[0] = "c"
                continue
            if pulse.kind == PulseKind.DETECT:
                row[-1] = "d"
                continue
            first = int((pulse.start_ns - start) / span * (width - 1))
            last = max(first, int((pulse.end_ns - start) / span * (width - 1)))
            for i in range(first, last + 1):
                row[i] = SYMBOL[pulse.kind]
        lines.append(f"{channel:>9} |{''.join(row)}|")
    lines.append("legend: # R pulse   = MS leg   ~ MS drive   c cooling   d detection")
    return "\n".join(lines)


def plot_png(timeline: Timeline, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"cool": "#64748b", "r": "#2563eb", "ms": "#d97706", "ms_drive": "#f59e0b", "detect": "#16a34a"}
    channels = timeline.channels()
    fig, ax = plt.subplots(figsize=(11, 0.45 * len(channels) + 1.2))
    for row, channel in enumerate(channels):
        bars = [(p.start_ns / 1000, p.duration_ns / 1000) for p in timeline.pulses if p.channel == channel]
        kinds = [p.kind for p in timeline.pulses if p.channel == channel]
        ax.broken_barh(bars, (row - 0.35, 0.7), facecolors=[colors[k] for k in kinds])
    ax.set_yticks(range(len(channels)), channels)
    ax.invert_yaxis()
    ax.set_xlabel("time in shot (us)")
    ax.set_title("CHAMOIS pulse timeline")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def cmd_timeline(args: argparse.Namespace) -> None:
    job_id = args.job_id
    if len(job_id) < 36:  # allow the short id printed by `chamois queue`
        matches = [j["job_id"] for j in _get(args, "/api/v1/jobs?limit=200") if j["job_id"].startswith(job_id)]
        if len(matches) != 1:
            sys.exit(f"no unique job starts with {job_id!r}")
        job_id = matches[0]
    result = _get(args, f"/api/v1/jobs/{job_id}/schedule?circuit={args.circuit}")
    for violation in result["violations"]:
        print(f"VIOLATION {violation['code']}: {violation['message']}")
    if result["timeline"] is None:
        return
    timeline = Timeline.model_validate(result["timeline"])
    print(ascii_timeline(timeline))
    if args.png:
        plot_png(timeline, Path(args.png))
        print(f"saved {args.png}")


def cmd_submit(args: argparse.Namespace) -> None:
    body = json.loads(Path(args.file).read_text(encoding="utf-8"))
    with _client(args) as client:
        response = client.post(f"/api/v1/submit/{args.workspace}/{args.resource}", json=body)
        if response.status_code >= 400:
            sys.exit(f"{response.status_code}: {response.text}")
        job_id = response.json()["job"]["job_id"]
        print(f"submitted {job_id}")
        while args.wait:
            state = client.get(f"/api/v1/result/{job_id}").json()["response"]
            print(f"  {state['status']}")
            if state["status"] in FINAL:
                if state["status"] == "error":
                    print(f"  {state['message']}")
                break
            time.sleep(0.5)


def cmd_crosscheck(args: argparse.Namespace) -> None:
    from chamois import crosscheck
    from chamois.calibration import default_calibration

    rows = crosscheck.run(default_calibration(), shots=args.shots)
    print(
        table(
            [
                [r.circuit, str(r.qubits), str(r.pulses), f"{r.tvd_exact:.6f}", f"{r.tvd_sampled:.4f}", str(r.passed)]
                for r in rows
            ],
            ["circuit", "qubits", "pulses", "TVD vs Statevector", "TVD vs AQT offline", "passed"],
        )
    )
    if not all(r.passed for r in rows):
        sys.exit(1)


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("chamois.api:create_app", factory=True, host=args.host, port=args.port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chamois", description="CHAMOIS operator CLI")
    parser.add_argument("--url", default=os.environ.get("CHAMOIS_URL", DEFAULT_URL))
    parser.add_argument("--token", default=os.environ.get("CHAMOIS_TOKEN", DEFAULT_TOKEN))
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("queue", help="show the job queue").set_defaults(func=cmd_queue)

    calibration = commands.add_parser("calibration", help="show or edit the calibration (operator)")
    calibration.add_argument("--qubit", type=int)
    calibration.add_argument("--pi-time", type=float, help="new pi time in ns for --qubit")
    calibration.add_argument("--disable", type=int, metavar="QUBIT")
    calibration.add_argument("--enable", type=int, metavar="QUBIT")
    calibration.set_defaults(func=cmd_calibration)

    timeline = commands.add_parser("timeline", help="plot the pulse timeline of a job")
    timeline.add_argument("job_id")
    timeline.add_argument("--circuit", type=int, default=0)
    timeline.add_argument("--png", help="also save a PNG plot (needs the 'plot' extra)")
    timeline.set_defaults(func=cmd_timeline)

    submit = commands.add_parser("submit", help="submit an ARNICA job JSON file")
    submit.add_argument("file")
    submit.add_argument("--workspace", default="default")
    submit.add_argument("--resource", default="chamois")
    submit.add_argument("--wait", action="store_true")
    submit.set_defaults(func=cmd_submit)

    crosscheck = commands.add_parser("crosscheck", help="Bell/GHZ vs AQT offline simulator (local)")
    crosscheck.add_argument("--shots", type=int, default=2000)
    crosscheck.set_defaults(func=cmd_crosscheck)

    serve = commands.add_parser("serve", help="run the API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except httpx.HTTPStatusError as error:
        sys.exit(f"{error.response.status_code}: {error.response.text}")
    except httpx.TransportError as error:
        sys.exit(f"cannot reach {args.url}: {error}")


if __name__ == "__main__":
    main()
