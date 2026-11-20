"""Pulse timeline models, shared by the Rust core and the Python reference scheduler."""

from enum import StrEnum

from pydantic import BaseModel


class PulseKind(StrEnum):
    COOL = "cool"
    R = "r"
    MS = "ms"
    MS_DRIVE = "ms_drive"
    DETECT = "detect"


class Pulse(BaseModel):
    channel: str
    start_ns: int
    duration_ns: int
    kind: PulseKind
    gate: int | None = None
    qubits: list[int]
    phase: float = 0.0  # units of pi, already corrected for virtual RZ frames

    @property
    def end_ns(self) -> int:
        return self.start_ns + self.duration_ns


class Timeline(BaseModel):
    number_of_qubits: int
    pulses: list[Pulse]
    duration_ns: int

    def channels(self) -> list[str]:
        """Channels in display order: cooling, addressed beams, MS drive, detection."""
        order = {"cool": 0, "ms_drive": 2, "detect": 3}

        def key(channel: str) -> tuple[int, int]:
            if channel.startswith("addr_q"):
                return (1, int(channel.removeprefix("addr_q")))
            return (order.get(channel, 4), 0)

        return sorted({pulse.channel for pulse in self.pulses}, key=key)


class Violation(BaseModel):
    code: str
    message: str


class CompileResult(BaseModel):
    ok: bool
    timeline: Timeline | None
    violations: list[Violation]


def addressed_channel(qubit: int) -> str:
    return f"addr_q{qubit}"


COOL_CHANNEL = "cool"
MS_DRIVE_CHANNEL = "ms_drive"
DETECT_CHANNEL = "detect"
