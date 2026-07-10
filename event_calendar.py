"""
Operator calendar — KNOWN scheduled load events.

A ScheduledEvent is a planned future load event (a stadium egress, a product
drop, a planned mass registration) — the kind of thing on an operator's calendar.
The Non-RT-Agent reads the calendar (via the get_calendar MCP tool) so it can
PRE-TUNE policy (e.g. raise lyapunov_V to pre-provision) BEFORE the event hits,
rather than only reacting to telemetry after it starts.

NOTE: this is deterministic KNOWN schedule information. Short-term *prediction*
of arrival/retry/fail values (regression on recent telemetry) is a separate
concern, handled by forecast.py / the get_forecast tool — not this module.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScheduledEvent:
    t_start:  float          # simulated time (seconds) the event is expected to begin
    name:     str            # human label, e.g. "stadium egress"
    severity: str = "high"   # qualitative load expectation: low | medium | high


def summarize_calendar(
    events:    list[ScheduledEvent] | None,
    t_now:     float,
    horizon_s: float = 60.0,
) -> str:
    """
    One-line calendar summary for the model: events within +/- horizon_s of now,
    so it sees both an imminent event and one that has just begun.
    """
    if not events:
        return "No scheduled events on the calendar."

    near = []
    for e in events:
        dt = e.t_start - t_now
        if -horizon_s <= dt <= horizon_s:
            when = (f"in {dt:.0f}s"          if dt > 1 else
                    f"started {-dt:.0f}s ago" if dt < -1 else
                    "starting now")
            near.append((e.t_start, f"'{e.name}' {when} (severity: {e.severity})"))

    if not near:
        return "No scheduled events within the calendar horizon."
    return "SCHEDULED EVENTS: " + "; ".join(txt for _, txt in sorted(near))
