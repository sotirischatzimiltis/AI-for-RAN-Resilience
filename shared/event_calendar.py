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
    name:     str            # human label, e.g. "stadium egress" or "England v Brazil, friendly"
    severity: str = "high"   # qualitative load expectation: low | medium | high (bare events)
    # Rich descriptor (reserve-sizing events): the venue and whether it sold out. When `venue`
    # is set, the summary names the event + venue + sold-out status (free text the judge reasons
    # over to estimate the crowd) INSTEAD of the coarse severity. Attendance is NEVER included.
    venue:    str = ""
    sold_out: bool | None = None


def _describe(e: ScheduledEvent, provisioned: bool = False) -> str:
    """Free-text line for one event, naming its ABSOLUTE scheduled time (sim seconds). Rich
    (name + venue + sold-out) when a venue is given; else the coarse severity form. When
    `provisioned`, appends a note that the reserve is already committed for this event, so the
    judge stops re-estimating it — but the event still shows, so its surge stays classified benign."""
    at = f"scheduled at t={e.t_start:.0f}s"
    if e.venue:
        so = "" if e.sold_out is None else (", sold out" if e.sold_out else ", not sold out")
        line = f"'{e.name}, at {e.venue}{so}' — {at}"
    else:
        line = f"'{e.name}' — {at} (severity: {e.severity})"
    if provisioned:
        line += (" [reserve already provisioned — do NOT re-estimate this event; leave "
                 "expected_attendance at 0 for it. It remains this benign event, so once its "
                 "scheduled time arrives keep the filter OFF for its surge]")
    return line


def summarize_calendar(
    events:    list[ScheduledEvent] | None,
    t_now:     float,
    past_grace_s: float = 120.0,
    committed: set | None = None,
) -> str:
    """Calendar summary for the model, with the CURRENT time and each event's ABSOLUTE scheduled
    time (sim seconds) — no relative countdown. Events are visible for the whole episode (until
    ~past_grace_s after they start), so the judge can plan ahead and decide from the clock WHEN to
    act. The system pre-provisions the reserve just in time, so the judge need only supply the
    estimate and the event's time, not race the clock. `committed` holds the t_start of events the
    judge has already provisioned; those are annotated so it acts on each event ONCE (see _describe)."""
    if not events:
        return f"Current time t={t_now:.0f}s. No scheduled events on the calendar."

    committed = committed or set()
    shown = [e for e in events if e.t_start >= t_now - past_grace_s]   # upcoming + recently started
    if not shown:
        return f"Current time t={t_now:.0f}s. No scheduled events on the calendar."
    lines = "; ".join(_describe(e, round(e.t_start, 1) in committed)
                      for e in sorted(shown, key=lambda e: e.t_start))
    return f"Current time t={t_now:.0f}s. SCHEDULED EVENTS: {lines}"
