"""
FastMCP server — the READ tools the Non-RT judge can call during an assessment.

Three tools: get_episode_stats (cumulative resilience so far), get_calendar (known
scheduled events) and get_forecast (short-term load prediction). The server does NOT
own the simulation — it only READS the live episode via runtime.host; start/stop is
driven in-process by runtime.host.start(). Tools disabled by ablation are hidden from
the schema by AblationMiddleware, so a bare-judge site (Exp 1) exposes only
get_episode_stats.
"""

import sys
from pathlib import Path

# Put the repo root (this file's grandparent) on the import path so 'sim', 'runtime',
# 'shared' resolve when this module is imported/run from anywhere.
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastmcp import FastMCP                              # the MCP server framework
from fastmcp.server.middleware import Middleware         # request hooks (used to gate tools)

from sim.metrics import live_absorption, benign_success_rate, malicious_blocked_rate  # real-time scoring
from runtime import host, UP                             # the live episode (SimHost) + utility params
from shared.event_calendar import summarize_calendar     # backs get_calendar
from shared.forecast import forecast_signals, summarize_forecast  # backs get_forecast

MCP_HOST = "127.0.0.1"      # loopback — the judge connects over local HTTP
MCP_PORT = 8000

mcp = FastMCP("StormSim MCP Server")   # the server; @mcp.tool() registers each tool below


class AblationMiddleware(Middleware):
    """Hide ablated anticipation tools from the tool SCHEMA (not just return
    "disabled" when called), so the judge never even sees a tool it can't use — the
    bare-judge configuration (Exp 1) truly exposes only get_episode_stats. Evaluated
    per request against the live host flags, so the same server serves both the full
    system (all tools) and an ablated site."""

    # tool name -> the host flag that must be True for it to be visible. A tool not
    # listed here (get_episode_stats) is always allowed.
    _GATED = {"get_calendar": "calendar_enabled", "get_forecast": "forecast_enabled"}

    def _allowed(self, name: str) -> bool:
        flag = self._GATED.get(name)                     # None for ungated tools
        return flag is None or getattr(host, flag, True)  # read the live flag (default: on)

    async def on_list_tools(self, context, call_next):
        # Runs on every "what tools exist?" request. call_next() gets the full list;
        # we drop the ones disabled by the current ablation flags before returning it.
        tools = await call_next(context)
        return [t for t in tools if self._allowed(t.name)]

    async def on_call_tool(self, context, call_next):
        # Runs on every tool CALL. Safety net: if a client somehow calls a hidden tool
        # (e.g. it cached an older tool list), refuse instead of running it.
        if not self._allowed(context.message.name):
            from fastmcp.exceptions import ToolError
            raise ToolError(f"{context.message.name} is disabled at this site (ablation)")
        return await call_next(context)                  # allowed -> run the tool


mcp.add_middleware(AblationMiddleware())   # install the gate on the server


@mcp.tool()
def get_calendar() -> dict:
    """Return KNOWN scheduled load events on the operator's calendar near the
    current sim time (e.g. a stadium egress or planned mass registration).

    Use this to pre-provision BEFORE a known event: if a high-severity event is
    imminent, raise the Lyapunov utility weight so the fast loop runs more servers
    ahead of the demand. (This is deterministic schedule info, not a prediction.)
    """
    if not host.calendar_enabled:  # ablation guard (also hidden by middleware)
        return {"disabled": "calendar unavailable at this site (ablation)"}
    t_now = host.sim.telemetry[-1].t if (host.sim and host.sim.telemetry) else 0.0 # current time
    return {"t_now": round(t_now, 1), "calendar": summarize_calendar(host.calendar, t_now)} # return calendar summary 


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
    if not host.forecast_enabled:       # ablation guard (also hidden by middleware)
        return {"disabled": "forecast unavailable at this site (ablation)"}
    if host.sim is None or len(host.sim.telemetry) < 3:  # need >=3 points to fit a trend
        return {"error": "insufficient data — episode may not have started yet"}
    tel = host.sim.telemetry # obtain telemetry data 
    out = forecast_signals(tel)      # per-signal trend/slope/confidence from a least-squares fit
    out["summary"] = summarize_forecast(tel)     # one-line human-readable version for the prompt
    return out


@mcp.tool()
def get_episode_stats() -> dict:
    """Cumulative episode counters + a REAL-TIME filtering-effectiveness signal.

    Everything here is computable ONLINE from telemetry — no knowledge of when storms
    start or end (unlike the offline resilience_multi used to score the run). The key
    field is `absorption`: over the recent window, how well utility is being held near
    the calm baseline. High = the filter is coping; falling while load stays high = push
    harder. `recovered` = utility back at baseline and holding (the disturbance passed).
    """
    if host.sim is None or len(host.sim.telemetry) < 4:  # need a few samples before scoring is meaningful
        return {"error": "insufficient data — episode may not have started yet"}
    # Real-time absorption: rolling recent window vs the opening-calm baseline. No storm
    # windows / t0 / td — a deployed agent could compute exactly this from live telemetry.
    live = live_absorption(host.sim.telemetry, host.sim.mu_single, UP)
    return {
        # --- raw cumulative counters (whole episode, straight off the sim) ---
        "completed":     host.sim.stats.completed,
        "failed":        host.sim.stats.failed,
        "retries":       host.sim.stats.retries,
        "arrivals":      host.sim.stats.arrivals,
        # --- security rates (whole episode) ---
        "benign_success_rate":    round(benign_success_rate(host.sim.stats), 4),
        "malicious_blocked_rate": round(malicious_blocked_rate(host.sim.stats), 4),
        # --- real-time filtering feedback (recent window vs calm baseline; NO oracle) ---
        "absorption":    live["absorption"],   # utility held near baseline lately (1=fine, low=degrading)
        "recovered":     live["recovered"],    # utility back at baseline and holding
        "episode_done":  host.is_done,         # True once the episode has finished
    }


# ---------------------------------------------------------------------------
# Entry point — run the server as its OWN process (python -m mcp_server.server).
# In the experiments the runners host it in-process instead, but this lets you start
# it standalone (e.g. for manual tool testing). Serves over local HTTP.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="http", host=MCP_HOST, port=MCP_PORT)
