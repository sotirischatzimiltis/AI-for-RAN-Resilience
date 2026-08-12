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
    single_storm_traffic, single_ramp_traffic, multi_storm_flat_traffic,
    multi_storm_ramp_traffic, mixed_storm_traffic, botnet_event_traffic, event_surge_traffic,
    BENIGN_SURGE_IDX,
)
from sim.simulator import StormSim
from sim.metrics import UtilityParams
from shared.event_calendar import ScheduledEvent
from shared.events import EXP1_EVENT

# Utility-function parameters, shared by the fast loop (Lyapunov c_star) and the
# resilience score (get_episode_stats).
LQMAX = 1500.0
UP    = UtilityParams(lq_max=LQMAX, kB=0.004)

_MIXED_SCENARIOS = ("mixed_flat_step", "mixed_flat_ramp", "mixed_inc_step", "mixed_inc_ramp")
LLM_COMPARE = "llm_compare"          # exp_1: one botnet storm + one real-event benign surge


def scenario_calendar(scenario: str, traffic) -> list:
    """The operator calendar for a scenario — the single source of truth shared by
    runtime.start (agentic/LLM path) and the experiments (deterministic path). The mixed
    scenarios put a coarse 'stadium egress' on the benign surge; llm_compare puts the RICH
    EXP1_EVENT (venue + sold-out, no attendance) on its event surge so the judge must reason
    the crowd; every other scenario is empty."""
    if scenario == LLM_COMPARE:
        t_event = traffic.storm_windows()[1][0]                 # start of storm-2 (the event surge)
        return [ScheduledEvent(t_event, EXP1_EVENT.name, "high", EXP1_EVENT.venue, EXP1_EVENT.sold_out)]
    if scenario in _MIXED_SCENARIOS:
        t_surge = traffic.storm_windows()[BENIGN_SURGE_IDX][0]   # start of the benign surge
        return [ScheduledEvent(t_surge, "stadium egress", "high")]
    return []


class SimHost:
    """Owns one StormSim episode, running in a background thread."""

    def __init__(self):
        self.sim:     StormSim | None          = None
        self._thread: threading.Thread | None  = None
        self._done    = threading.Event()
        self.t0: float = 50.0   # storm onset  (single_storm default)
        self.td: float = 110.0  # storm end    (single_storm default)
        self.calendar: list = []  # scheduled load events (read by the get_calendar MCP tool)
        # t_start of events the judge has ALREADY provisioned a reserve for this episode. get_calendar
        # annotates these "reserve committed — do not re-estimate" so the judge acts on each event ONCE,
        # while the event stays visible (so its surge is still classified benign). Reset every episode.
        self.calendar_committed: set = set()
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
        storm:     float | None = None,   # override surge peak (single_storm / event_heavy; Exp 5)
        compute_kappa:   float | None = None,  # shared-compute contention (None = off)
        compute_slowdown: float = 0.5,         # contention severity a (Exp 5; used only when kappa set)
        provision_delay: float = 5.0,          # server warm-up delay (s); matches CFG-1 default. 0 = instant
        provision_parallel: bool = False,      # False = serial (one server per delay); True = all pending together
    ) -> str:
        if self._thread and self._thread.is_alive():
            return "episode already running — call ignored"

        # Scenario owns the calendar: only the mixed scenarios register an event (below);
        # every other scenario runs with an empty calendar, so a prior mixed run on this
        # shared host cannot leak events into a later storm run. Committed-marks reset too, so a
        # prior episode's "already provisioned" flags never carry over — the calendar is pristine.
        self.calendar = []
        self.calendar_committed = set()
        if scenario == LLM_COMPARE:
            # exp_1: botnet storm (forecast) + real-event benign surge (calendar). The surge is
            # sized to EXP1_EVENT's true attendance, so the judge's crowd estimate drives QoS.
            traffic          = botnet_event_traffic(EXP1_EVENT.surge_peak())
            self.t0, self.td = 60.0, 120.0
            self.calendar    = scenario_calendar(scenario, traffic)
        elif scenario == "multi_storm_flat":
            traffic          = multi_storm_flat_traffic()
            self.t0, self.td = 60.0, 120.0
        elif scenario == "multi_storm_ramp":
            traffic          = multi_storm_ramp_traffic()   # ramped storms (forecast can anticipate)
            self.t0, self.td = 60.0, 120.0
        elif scenario == "single_ramp":
            # benign single-surge with a RAMP onset — the twin of single_storm (STEP); Exp 4
            # sweeps both to isolate load-shape x V/W x provisioning delay (no botnet, filter off).
            kw               = {"t_post": t_post} if t_post is not None else {}
            traffic          = single_ramp_traffic(**kw)
            self.t0, self.td = 50.0, 110.0
        elif scenario == "event_heavy":
            # Exp 5 Part B: a benign scheduled-event surge ONLY (no botnet), STEP onset, sized to a
            # real crowd. `storm` overrides the surge peak (default = EXP1_EVENT's ~279 UEs/s). The
            # judge reads the calendar, estimates the crowd, and pre-provisions the reserve ahead of
            # the surge, entering the contention regime — then pays the contention cost.
            surge            = storm if storm is not None else EXP1_EVENT.surge_peak()
            traffic          = event_surge_traffic(surge_rate=surge, lead=140.0, surge_dur=60.0, post=60.0)
            self.t0, self.td = 140.0, 200.0
            t_event          = traffic.storm_windows()[0][0]     # onset of the (only) surge window
            self.calendar    = [ScheduledEvent(t_event, EXP1_EVENT.name, "high",
                                               EXP1_EVENT.venue, EXP1_EVENT.sold_out)]
        elif scenario in _MIXED_SCENARIOS:
            # one mixed scenario, four versions over two axes: onset step/ramp (step = calendar
            # is the only anticipation signal; ramp = forecast can fire too) x intensity
            # flat/increasing. One calendar event, always on the benign surge (storm-2).
            traffic          = mixed_storm_traffic(ramped=("ramp" in scenario),
                                                   increasing=("inc" in scenario))
            self.t0, self.td = 60.0, 120.0
            self.calendar    = scenario_calendar(scenario, traffic)
        else:
            kw               = {"t_post": t_post} if t_post is not None else {}
            if storm is not None:            # Exp 5 Part B: benign step over the stability limit
                kw["storm"] = storm
            traffic          = single_storm_traffic(**kw)
            self.t0, self.td = 50.0, 110.0

        cfg = SimConfig(
            arch=open_ran_arch(),
            rrc=RRCConfig(t300_ms=1000, max_attempts=5),
            traffic=traffic,
            c0=2, c_max=c_max, seed=seed,
            realtime=True, rt_factor=rt_factor,
            compute_kappa=compute_kappa, compute_slowdown=compute_slowdown,
            server_provision_delay_s=provision_delay,
            parallel_provision=provision_parallel,
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

    def mark_event_committed(self, event_time: float, tol: float = 30.0) -> None:
        """Flag the scheduled event nearest `event_time` as already provisioned. get_calendar then
        annotates it 'reserve committed — do not re-estimate', so the judge acts on the event ONCE
        instead of re-estimating the crowd every cycle. The event stays VISIBLE (not removed), so
        its surge is still classified benign and the filter stays off. Idempotent; reset by start()."""
        if not self.calendar:
            return
        match = min(self.calendar, key=lambda e: abs(e.t_start - event_time))
        if abs(match.t_start - event_time) <= tol:
            self.calendar_committed.add(round(match.t_start, 1))

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
