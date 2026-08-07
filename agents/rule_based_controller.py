"""
RuleBasedController — the Non-RT judge's decision logic as a DETERMINISTIC rule set,
with no LLM.

In effect, a decision tree:
is the load elevated? if so it is unexplained (there is no calendar), so treat it as malicious;
how hard to filter, and how much reactive headroom to add. This controller encodes that tree in
Python, so an experiment can measure what the LLM adds OVER hardcoding its own instructions.

Deliberately calendar-FREE. A calendar entry is human-in-the-loop knowledge (a person knew about
the event and entered it); the agentic arm can populate its own calendar autonomously (a future
web-scraping sub-agent), so handing a calendar to a hardcoded rule would import intelligence it
did not earn. The rule therefore has only the forecast (which it computes itself from telemetry)
to catch a climbing ramp, and NO way to recognize a scheduled benign surge. A step surge it did
not forecast reads as elevated-and-unexplained, so the rule filters it as if it were an attack —
the honest limitation of a controller with no event knowledge.

It plugs into the SAME machinery as everything else: it exposes step(sim, s) (called every
control_dt_s by the simulator, like the baseline controllers) and actuates through the SAME
apply_decision() path the agentic fast loop uses (reactive Lyapunov capacity + the malicious
filter, gated by storm_active). The ONLY difference from the agentic arm is that assess() here is
a rule set instead of an LLM call — so a deterministic-vs-agentic comparison isolates the model's
contribution. It re-runs assess() on the Non-RT cadence (every assessment_interval sim-seconds)
and applies capacity every tick, mirroring the two-timescale split. The forecast is gated by
`anticipation` (off = the --no-anticipation ablation).
"""
from __future__ import annotations

from dataclasses import dataclass

from agents.near_rt_control_loop import apply_decision
from sim.controllers import lyapunov_optimal_c
from sim.metrics import UtilityParams, live_absorption, resting_lam
from shared.forecast import forecast_signals

# --- rule thresholds (the hardcoded knobs the LLM is asked to pick by judgement) ---
_ELEV_FACTOR = 1.8      # LATEST lam must exceed this * resting lam ...
_ELEV_MARGIN = 25.0     # ... AND exceed rest by this many UEs/s, to count as elevated
_FC_CONF     = {"medium", "high"}   # forecast confidence we act on (never on "low")
_FC_RISE     = 1.5      # forecast lam predicted > this * current => a steep rise worth pre-provisioning
_DROP_BASE, _DROP_K, _DROP_MAX = 0.6, 0.04, 0.95   # malicious_drop_prob = BASE + K*severity, capped
_ABSORB_SLIP = 0.7      # absorption below this while elevated => push the filter harder
_V_K, _V_MAX = 1.6, 20.0            # lyapunov_V = 1 + K*severity, capped at V_MAX
_V_ANTICIPATE = 15.0    # lyapunov_V while a forecast ramp is climbing (load present, so V bites)


@dataclass
class RuleBasedController:
    """Deterministic storm judge + reactive capacity, drop-in for sim.run(controller=...)."""
    anticipation:        bool  = True          # False => ignore the forecast tool (ablation)
    assessment_interval: float = 5.0           # Non-RT re-decision cadence (sim-seconds)
    util_p:              UtilityParams | None = None
    queue_hold_threshold: int  = 10
    # --- internal state carried between ticks (the "policy" the fast part reads) ---
    storm_active: bool  = False
    drop:         float = 0.0
    V:            float = 1.0
    _last_assess_t: float | None = None

    def __post_init__(self):
        if self.util_p is None:
            self.util_p = UtilityParams()

    # ---- the Non-RT decision: the prompt's rules, run every assessment_interval ----
    def _forecast_rising(self, telemetry) -> bool:
        """get_forecast's ALERT condition: arrival rate predicted to rise steeply with
        usable confidence."""
        f = forecast_signals(telemetry)
        lam = f.get("signals", {}).get("lam")
        if not lam:
            return False
        return (lam["trend"] == "rising" and lam["confidence"] in _FC_CONF
                and lam["predicted"] > _FC_RISE * max(lam["current"], 1.0))

    def assess(self, telemetry, mu_single: float) -> tuple[bool, float, float]:
        """PURE decision from telemetry (+ the self-computed forecast): returns
        (storm_active, malicious_drop_prob, lyapunov_V). No sim or self mutation, so it is safe to
        call for a dry read; step() is what commits the result and actuates."""
        latest  = telemetry[-1].lam_current                # arrival rate right now (UEs/s)
        rest    = resting_lam(telemetry)                   # calm baseline over the whole episode
        # severity = how many multiples of rest we sit above rest. e.g. rest 20, latest 200 -> 9.0.
        # (max(rest,1) only guards divide-by-zero; low-rest saturation is a known TODO.)
        sev     = (latest - rest) / max(rest, 1.0)
        # "clearly above rest and holds": latest must beat BOTH a ratio (1.8x rest) and an absolute
        # floor (rest + 25 UEs/s). max() picks whichever is stricter — the margin binds on a quiet
        # cell (rest 20 -> need >45), the ratio binds on a busy one (rest 200 -> need >360) — so a
        # small absolute wobble can't trip a storm call.
        elevated = latest > max(_ELEV_FACTOR * rest, rest + _ELEV_MARGIN)

        # Any elevated load is unexplained (no calendar) => malicious => filter, with V headroom
        # sized to severity. The rule cannot tell a benign flash crowd from an attack, so it filters
        # every elevated surge — including the benign event's — the honest cost of no calendar.
        if elevated:
            drop = min(_DROP_MAX, _DROP_BASE + _DROP_K * sev)
            if live_absorption(telemetry, mu_single, self.util_p)["absorption"] < _ABSORB_SLIP:
                drop = min(_DROP_MAX, drop + 0.2)          # slipping while elevated => push harder
            return True, drop, min(_V_MAX, float(round(1.0 + _V_K * sev)))
        # not elevated: no filter. Add pre-emptive headroom only if the forecast sees a ramp
        # climbing; otherwise idle. No reserve floor — the rule has nothing scheduled to provision.
        if self.anticipation and self._forecast_rising(telemetry):
            return False, 0.0, _V_ANTICIPATE
        return False, 0.0, 1.0

    # ---- the fast part: called every control_dt_s, actuates through apply_decision ----
    def step(self, sim, s) -> None:
        # TWO CLOCKS. step() fires every control tick (~1 Hz), but the DECISION only re-runs on
        # the slower Non-RT cadence (every assessment_interval sim-s); between decisions we HOLD
        # the last verdict. (-1e-9 absorbs float rounding so t=5.0000001 still reads as "5s later".)
        if self._last_assess_t is None or (s.t - self._last_assess_t) >= self.assessment_interval - 1e-9:
            # pure decision -> commit to the live state the fast part below reads each tick
            self.storm_active, self.drop, self.V = self.assess(sim.telemetry, sim.mu_single)
            self._last_assess_t = s.t
        # EVERY tick: reactive capacity. Re-solve the Lyapunov optimum for the CURRENT load using
        # the V the decision set (higher V = more headroom); W stays nominal at 1.
        c_star = lyapunov_optimal_c(s, sim.mu_single, sim.cfg.c_max, self.util_p, V=self.V, W=1.0)
        # actuate BOTH levers through the exact same clamps the agentic fast loop uses: capacity in
        # [1, c_max] with no scale-down while the queue is backed up, and the drop filter gated by
        # storm_active. min_servers=1 — the rule holds no reserve (no scheduled-surge knowledge).
        apply_decision(sim, self.storm_active, c_star, self.drop,
                       self.queue_hold_threshold, current_lam=s.lam_current,
                       memory=None, min_servers=1)
