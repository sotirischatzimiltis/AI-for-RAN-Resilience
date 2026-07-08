"""
FastMCP server wrapping StormSim.

In the decoupled design the fast control loop actuates the sim directly
(in-process), so the server no longer exposes actuator/telemetry tools. It hosts
the running episode (SimHost) and exposes a single tool for the Non-RT judge:

  get_episode_stats — cumulative counters + A3RT resilience score P

start/stop of an episode is driven in-process via `host.start(...)`.
"""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastmcp import FastMCP

from sim.config import (
    SimConfig, open_ran_arch, RRCConfig,
    single_storm_traffic, multi_storm_traffic,
)
from sim.simulator import StormSim
from sim.metrics import UtilityParams, resilience_score

MCP_HOST = "127.0.0.1"
MCP_PORT = 8000

LQMAX  = 1500.0
UP     = UtilityParams(lq_max=LQMAX, kB=0.004)

mcp = FastMCP("StormSim MCP Server")


# ---------------------------------------------------------------------------
# SimHost — owns the running StormSim instance
# ---------------------------------------------------------------------------
class SimHost:
    """Manages one StormSim episode, running in a background thread."""

    def __init__(self):
        self.sim:    StormSim | None        = None
        self._thread: threading.Thread | None = None
        self._done   = threading.Event()
        self.t0: float = 50.0   # storm onset  (single_storm default)
        self.td: float = 110.0  # storm end    (single_storm default)

    def start(
        self,
        scenario:  str   = "single_storm",
        seed:      int   = 3,
        c_max:     int   = 16,
        rt_factor: float = 1.0,
        t_post:    float | None = None,   # override post-storm duration (single_storm only)
    ) -> str:
        if self._thread and self._thread.is_alive():
            return "episode already running — call ignored"

        if scenario == "multi_storm":
            traffic      = multi_storm_traffic()
            self.t0, self.td = 60.0, 120.0
        else:
            kw      = {"t_post": t_post} if t_post is not None else {}
            traffic = single_storm_traffic(**kw)
            self.t0, self.td = 50.0, 110.0

        cfg = SimConfig(
            arch=open_ran_arch(),
            rrc=RRCConfig(t300_ms=1000, max_attempts=5),
            traffic=traffic,
            c0=1, c_max=c_max, lq_max=LQMAX,
            sample_dt_s=0.5, seed=seed,
            realtime=True, rt_factor=rt_factor,
        )
        self.sim = StormSim(cfg)
        self._done.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        t_post_actual = t_post if t_post is not None else 900.0
        horizon = 50.0 + 60.0 + t_post_actual if scenario != "multi_storm" else 1100.0
        return (
            f"episode started | scenario={scenario} seed={seed} "
            f"c_max={c_max} rt_factor={rt_factor} "
            f"horizon={horizon:.0f}s (~{horizon/rt_factor:.0f}s wall-clock)"
        )

    def _run(self):
        # the fast control loop actuates the sim directly (in-process)
        self.sim.run(controller=None)
        self._done.set()

    @property
    def is_done(self) -> bool:
        return self._done.is_set()


host = SimHost()


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------
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
