"""
Simulation runtime — the single in-process home of the running episode.

An entry point calls `host.start(...)` to launch a StormSim episode in a
background thread. Everything else then acts on `host.sim` DIRECTLY, in-process:
  - the fast control loop reads telemetry and calls sim.set_servers / set_drop,
  - the Non-RT agent reads the telemetry window,
  - the MCP server (mcp_server/server.py) reads this same host for get_episode_stats.

No MCP round-trip is involved in owning or actuating the sim; MCP is only used by
the Non-RT agent for the get_episode_stats tool.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sim.config import (
    SimConfig, open_ran_arch, RRCConfig,
    single_storm_traffic, multi_storm_traffic, multi_storm_flat_traffic,
)
from sim.simulator import StormSim
from sim.metrics import UtilityParams

# Utility-function parameters, shared by the fast loop (Lyapunov c_star) and the
# resilience score (get_episode_stats).
LQMAX = 1500.0
UP    = UtilityParams(lq_max=LQMAX, kB=0.004)


class SimHost:
    """Owns one StormSim episode, running in a background thread."""

    def __init__(self):
        self.sim:     StormSim | None          = None
        self._thread: threading.Thread | None  = None
        self._done    = threading.Event()
        self.t0: float = 50.0   # storm onset  (single_storm default)
        self.td: float = 110.0  # storm end    (single_storm default)
        self.calendar: list = []  # scheduled load events (read by the get_calendar MCP tool)
        # Ablation gates for the anticipation MCP tools. When False the tool still
        # exists (the agent can call it) but returns a "disabled" payload, so the
        # agent gets no forecast / calendar signal — a clean information ablation.
        self.forecast_enabled: bool = True
        self.calendar_enabled: bool = True

    def start(
        self,
        scenario:  str   = "single_storm",
        seed:      int   = 3,
        c_max:     int   = 16,
        rt_factor: float = 1.0,
        t_post:    float | None = None,   # override post-storm duration (single_storm only)
        compute_kappa:   float | None = None,  # shared-compute contention (None = off)
        provision_delay: float = 0.0,          # server warm-up delay (s, 0 = instant)
    ) -> str:
        if self._thread and self._thread.is_alive():
            return "episode already running — call ignored"

        if scenario == "multi_storm":
            traffic          = multi_storm_traffic()
            self.t0, self.td = 60.0, 120.0
        elif scenario == "multi_storm_flat":
            traffic          = multi_storm_flat_traffic()
            self.t0, self.td = 60.0, 120.0
        else:
            kw               = {"t_post": t_post} if t_post is not None else {}
            traffic          = single_storm_traffic(**kw)
            self.t0, self.td = 50.0, 110.0

        cfg = SimConfig(
            arch=open_ran_arch(),
            rrc=RRCConfig(t300_ms=1000, max_attempts=5),
            traffic=traffic,
            c0=1, c_max=c_max, lq_max=LQMAX,
            sample_dt_s=0.5, seed=seed,
            realtime=True, rt_factor=rt_factor,
            compute_kappa=compute_kappa,
            server_provision_delay_s=provision_delay,
        )
        self.sim = StormSim(cfg)
        self._done.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        horizon = traffic.horizon()   # actual end of the last phase (all scenarios)
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


# The single process-wide sim host. Entry points call host.start(...); actors
# import this object and act on host.sim directly.
host = SimHost()
