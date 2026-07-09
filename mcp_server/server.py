"""
FastMCP server — exposes a single tool for the Non-RT judge:

  get_episode_stats — cumulative counters + A3RT resilience score P

The simulation itself is owned by runtime.host (not by this server); this module
only reads it. Start/stop of an episode is driven in-process via runtime.host.start().
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastmcp import FastMCP

from sim.metrics import resilience_score
from runtime import host, UP
from event_calendar import summarize_calendar

MCP_HOST = "127.0.0.1"
MCP_PORT = 8000

mcp = FastMCP("StormSim MCP Server")


@mcp.tool()
def get_calendar() -> dict:
    """Return KNOWN scheduled load events on the operator's calendar near the
    current sim time (e.g. a stadium egress or planned mass registration).

    Use this to pre-provision BEFORE a known event: if a high-severity event is
    imminent, raise the Lyapunov utility weight so the fast loop runs more servers
    ahead of the demand. (This is deterministic schedule info, not a prediction.)
    """
    t_now = host.sim.telemetry[-1].t if (host.sim and host.sim.telemetry) else 0.0
    return {"t_now": round(t_now, 1), "calendar": summarize_calendar(host.calendar, t_now)}


@mcp.tool()
def get_episode_stats() -> dict:
    """Return cumulative counters and the A3RT resilience score P for this episode.

    P = w1*absorption + w2*adaptation + w3*trec  (weights 0.4 / 0.4 / 0.2).
    u_des is auto-calibrated to the mean pre-storm utility baseline.
    """
    if host.sim is None or len(host.sim.telemetry) < 4:
        return {"error": "insufficient data — episode may not have started yet"}
    try:
        r = resilience_score(
            host.sim.telemetry, host.sim.mu_single, UP,
            t0=host.t0, td=host.td,
        )
    except Exception:
        r = {"P": 0.0, "absorption": 0.0, "adaptation": 0.0,
             "trec": 0.0, "recovery_time": 0.0}
    return {
        "completed":     host.sim.stats.completed,
        "failed":        host.sim.stats.failed,
        "retries":       host.sim.stats.retries,
        "arrivals":      host.sim.stats.arrivals,
        "resilience_P":  round(r["P"], 4),
        "absorption":    round(r["absorption"], 4),
        "adaptation":    round(r["adaptation"], 4),
        "trec":          round(r["trec"], 4),
        "recovery_time": round(r["recovery_time"], 1),
        "episode_done":  host.is_done,
    }


# ---------------------------------------------------------------------------
# Entry point (run as a standalone process if needed)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="http", host=MCP_HOST, port=MCP_PORT)
