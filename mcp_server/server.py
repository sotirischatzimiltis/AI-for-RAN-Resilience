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
from fastmcp.server.middleware import Middleware

from sim.metrics import resilience_score, benign_success_rate, malicious_blocked_rate
from runtime import host, UP
from shared.event_calendar import summarize_calendar
from shared.forecast import forecast_signals, summarize_forecast

MCP_HOST = "127.0.0.1"
MCP_PORT = 8000

mcp = FastMCP("StormSim MCP Server")


class AblationMiddleware(Middleware):
    """Hide ablated anticipation tools from the tool SCHEMA (not just return
    "disabled" when called), so the judge never even sees a tool it can't use — the
    bare-judge configuration (Exp 1) truly exposes only get_episode_stats. Evaluated
    per request against the live host flags, so the same server serves both the full
    system (all tools) and an ablated site."""

    _GATED = {"get_calendar": "calendar_enabled", "get_forecast": "forecast_enabled"}

    def _allowed(self, name: str) -> bool:
        flag = self._GATED.get(name)
        return flag is None or getattr(host, flag, True)

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        return [t for t in tools if self._allowed(t.name)]

    async def on_call_tool(self, context, call_next):
        # Safety net: block a call to a hidden tool (e.g. a stale client cache).
        if not self._allowed(context.message.name):
            from fastmcp.exceptions import ToolError
            raise ToolError(f"{context.message.name} is disabled at this site (ablation)")
        return await call_next(context)


mcp.add_middleware(AblationMiddleware())


@mcp.tool()
def get_calendar() -> dict:
    """Return KNOWN scheduled load events on the operator's calendar near the
    current sim time (e.g. a stadium egress or planned mass registration).

    Use this to pre-provision BEFORE a known event: if a high-severity event is
    imminent, raise the Lyapunov utility weight so the fast loop runs more servers
    ahead of the demand. (This is deterministic schedule info, not a prediction.)
    """
    if not host.calendar_enabled:
        return {"disabled": "calendar unavailable at this site (ablation)"}
    t_now = host.sim.telemetry[-1].t if (host.sim and host.sim.telemetry) else 0.0
    return {"t_now": round(t_now, 1), "calendar": summarize_calendar(host.calendar, t_now)}


@mcp.tool()
def get_forecast() -> dict:
    """Short-term forecast of where the telemetry is HEADING (next ~20s), from a
    least-squares fit to recent samples — the data-driven complement to
    get_calendar's known schedule.

    Projects the leading signals (arrival rate, retry-rate, fail-rate) and the
    lagging queue forward. Each carries a trend, a per-second slope and a
    confidence from the fit quality. Use a rising arrival-rate forecast to
    PRE-PROVISION (raise lyapunov_V, tighten=true) before a storm is confirmed;
    discount low-confidence projections.
    """
    if not host.forecast_enabled:
        return {"disabled": "forecast unavailable at this site (ablation)"}
    if host.sim is None or len(host.sim.telemetry) < 3:
        return {"error": "insufficient data — episode may not have started yet"}
    tel = host.sim.telemetry
    out = forecast_signals(tel)
    out["summary"] = summarize_forecast(tel)
    return out


@mcp.tool()
def get_episode_stats() -> dict:
    """Return cumulative counters and the A3RT resilience score P for the CURRENT
    storm — the active one, or the most recent if we're now recovering.

    Scoping P to the current storm (rather than a fixed early window) is what makes
    absorption a LIVE feedback signal in a multi-storm episode: it reflects the storm
    the judge is handling now, not a frozen first-storm score.
    P = w1*absorption + w2*adaptation + w3*trec  (weights 0.4 / 0.4 / 0.2).
    u_des is auto-calibrated to the mean pre-storm utility baseline.
    """
    if host.sim is None or len(host.sim.telemetry) < 4:
        return {"error": "insufficient data — episode may not have started yet"}
    # Pick the current storm window: the most recent storm that has STARTED (so an
    # active storm gives partial-window feedback, a passed one gives its final score).
    # Fall back to host.t0/td only if the schedule exposes no storm windows.
    #
    # NOTE (real-world caveat): storm_windows() is derived from the KNOWN traffic
    # schedule — a simulation oracle. In a real deployment the storm boundaries are NOT
    # given; t0/td would have to be DETECTED online from telemetry — e.g. a change-point
    # / threshold on lam vs the resting baseline for onset, and lam settling back to rest
    # for the end — or bootstrapped from the judge's own storm_active True->False
    # transitions. The scoring window is the one piece that currently assumes ground truth.
    t_now   = host.sim.telemetry[-1].t
    windows = host.sim.cfg.traffic.storm_windows()
    started = [w for w in windows if w[0] <= t_now]
    t0, td  = started[-1] if started else (windows[0] if windows else (host.t0, host.td))
    try:
        r = resilience_score(
            host.sim.telemetry, host.sim.mu_single, UP,
            t0=t0, td=td,
        )
    except Exception:
        r = {"P": 0.0, "absorption": 0.0, "adaptation": 0.0,
             "trec": 0.0, "recovery_time": 0.0}
    return {
        "completed":     host.sim.stats.completed,
        "failed":        host.sim.stats.failed,
        "retries":       host.sim.stats.retries,
        "arrivals":      host.sim.stats.arrivals,
        "benign_success_rate":    round(benign_success_rate(host.sim.stats), 4),
        "malicious_blocked_rate": round(malicious_blocked_rate(host.sim.stats), 4),
        "resilience_P":  round(r["P"], 4),
        "absorption":    round(r["absorption"], 4),
        "adaptation":    round(r["adaptation"], 4),
        "trec":          round(r["trec"], 4),
        "recovery_time": round(r["recovery_time"], 1),
        "storm_window":  [round(t0, 1), round(td, 1)],   # which storm this score is for
        "episode_done":  host.is_done,
    }


# ---------------------------------------------------------------------------
# Entry point (run as a standalone process if needed)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="http", host=MCP_HOST, port=MCP_PORT)
