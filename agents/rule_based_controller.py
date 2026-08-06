"""
RuleBasedController — the Non-RT judge's decision logic as a DETERMINISTIC rule set,
with no LLM.

In effect, a decision tree:
is the load elevated? is it on the calendar (benign) or unexplained (malicious)? how hard
to filter, how much headroom to add, when to pre-provision ahead of a surge. This controller
encodes exactly that tree in Python, so an experiment can measure what the LLM adds OVER
hardcoding its own instructions.

It plugs into the SAME machinery as everything else: it exposes step(sim, s) (called every
control_dt_s by the simulator, like the baseline controllers) and actuates through the SAME
apply_decision() path the agentic fast loop uses (reactive Lyapunov capacity + the malicious
filter, gated by storm_active). The ONLY difference from the agentic arm is that _assess()
here is a rule set instead of an LLM call — so a deterministic-vs-agentic comparison isolates
the model's contribution. It re-runs _assess() on the Non-RT cadence (every
assessment_interval sim-seconds) and applies capacity every tick, mirroring the two-timescale
split. Reads the calendar (passed in) and computes the forecast itself; both are gated by
`anticipation` (off = the --no-anticipation ablation, so the benign surge is unexplained and
gets filtered).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from agents.near_rt_control_loop import apply_decision
from sim.controllers import lyapunov_optimal_c
from sim.metrics import UtilityParams, live_absorption, resting_lam
from shared.forecast import forecast_signals
from shared.event_calendar import ScheduledEvent
from shared.verdict import Verdict

# --- rule thresholds (the hardcoded knobs the LLM is asked to pick by judgement) ---
_ELEV_FACTOR = 1.8      # LATEST lam must exceed this * resting lam ...
_ELEV_MARGIN = 25.0     # ... AND exceed rest by this many UEs/s, to count as elevated
_CAL_HORIZON = 60.0     # a calendar event within +/- this many s => the surge is scheduled/benign
_FC_CONF     = {"medium", "high"}   # forecast confidence we act on (never on "low")
_FC_RISE     = 1.5      # forecast lam predicted > this * current => a steep rise worth pre-provisioning
_DROP_BASE, _DROP_K, _DROP_MAX = 0.6, 0.04, 0.95   # malicious_drop_prob = BASE + K*severity, capped
_ABSORB_SLIP = 0.7      # absorption below this while elevated => push the filter harder
_V_K, _V_MAX = 1.6, 20.0            # lyapunov_V = 1 + K*severity, capped at V_MAX
_V_ANTICIPATE = 15.0    # lyapunov_V while a forecast ramp is climbing (load present, so V bites)
_RESERVE_FLOOR = 10     # servers to hold online ahead of a SCHEDULED surge (a high-severity
                        # calendar event ~10 of c_max=16); the pre-provision lever, since V
                        # cannot add servers before the load actually arrives


@dataclass
class RuleBasedController:
    """Deterministic storm judge + reactive capacity, drop-in for sim.run(controller=...)."""
    calendar:            list[ScheduledEvent] = field(default_factory=list)
    anticipation:        bool  = True          # False => ignore calendar + forecast (ablation)
    assessment_interval: float = 5.0           # Non-RT re-decision cadence (sim-seconds)
    util_p:              UtilityParams | None = None
    queue_hold_threshold: int  = 10
    # --- internal state carried between ticks (the "policy" the fast part reads) ---
    storm_active: bool  = False
    drop:         float = 0.0
    V:            float = 1.0
    reserve:      int   = 1
    _last_assess_t: float | None = None
    _last_verdict:  Verdict | None = None

    def __post_init__(self):
        if self.util_p is None:
            self.util_p = UtilityParams()

    # ---- the Non-RT decision: the prompt's rules, run every assessment_interval ----
    def _calendar_near(self, t_now: float) -> bool:
        """A scheduled event within +/- _CAL_HORIZON of now — the benign discriminator and
        the pre-provision trigger, exactly as summarize_calendar reports it."""
        return any(abs(e.t_start - t_now) <= _CAL_HORIZON for e in self.calendar)

    def _forecast_rising(self, telemetry) -> bool:
        """get_forecast's ALERT condition: arrival rate predicted to rise steeply with
        usable confidence."""
        f = forecast_signals(telemetry)
        lam = f.get("signals", {}).get("lam")
        if not lam:
            return False
        return (lam["trend"] == "rising" and lam["confidence"] in _FC_CONF
                and lam["predicted"] > _FC_RISE * max(lam["current"], 1.0))

    def assess(self, telemetry, mu_single: float) -> Verdict:
        """PURE decision: telemetry + calendar (+ later, previous state) in, a Verdict out.
        No sim mutation, no self mutation — so shadow mode can run it on the agent's telemetry
        each assessment WITHOUT actuating. step() is what commits the verdict and acts."""
        s       = telemetry[-1]                            # newest telemetry sample = "now"
        latest  = s.lam_current                            # arrival rate right now (UEs/s)
        rest    = resting_lam(telemetry)                   # calm baseline over the whole episode
        # severity = how many multiples of rest we sit above rest. e.g. rest 20, latest 200 -> 9.0.
        # (max(rest,1) only guards divide-by-zero; low-rest saturation is a known TODO.)
        sev     = (latest - rest) / max(rest, 1.0)
        # "clearly above rest and holds": latest must beat BOTH a ratio (1.8x rest) and an absolute
        # floor (rest + 25 UEs/s). max() picks whichever is stricter — the margin binds on a quiet
        # cell (rest 20 -> need >45), the ratio binds on a busy one (rest 200 -> need >360) — so a
        # small absolute wobble can't trip a storm call.
        elevated = latest > max(_ELEV_FACTOR * rest, rest + _ELEV_MARGIN)

        cal_near = self.anticipation and self._calendar_near(s.t)
        fc_rise  = self.anticipation and self._forecast_rising(telemetry)
        absorp   = live_absorption(telemetry, mu_single, self.util_p)["absorption"]

        # --- filter: malicious storm only (step 4) ---
        # elevated AND unexplained (not on the calendar) => malicious => filter.
        # a scheduled surge (cal_near) is benign => leave the filter off however high its lam.
        if elevated and not cal_near: 
            storm = True
            drop  = min(_DROP_MAX, _DROP_BASE + _DROP_K * sev)
            if absorp < _ABSORB_SLIP:                     # slipping while elevated => push harder
                drop = min(_DROP_MAX, drop + 0.2)
        else:
            storm, drop = False, 0.0

        # --- capacity: two levers, because V cannot pre-provision an unrisen surge ---
        # lyapunov_V — headroom on load that is PRESENT (elevated, or a climbing forecast).
        if elevated:
            V = min(_V_MAX, float(round(1.0 + _V_K * sev)))
        elif fc_rise:
            V = _V_ANTICIPATE
        else:
            V = 1.0
        # reserve_servers — a capacity FLOOR held ahead of a scheduled surge; V cannot do this
        # at zero load. Held through the event (cal_near spans +/- horizon), then released.
        reserve = _RESERVE_FLOOR if cal_near else 1

        return Verdict(t=s.t, source="rule", storm_active=storm, malicious_drop_prob=drop,
                       lyapunov_V=V, reserve_servers=reserve, tighten=True,
                       latest_lam=latest, resting_lam=rest, severity=sev, elevated=elevated,
                       calendar_hit=cal_near, forecast_rising=fc_rise, absorption=absorp)

    # ---- the fast part: called every control_dt_s, actuates through apply_decision ----
    def step(self, sim, s) -> None:
        # TWO CLOCKS. step() fires every control tick (~1 Hz), but the DECISION only re-runs on
        # the slower Non-RT cadence (every assessment_interval sim-s); between decisions we HOLD
        # the last verdict. (-1e-9 absorbs float rounding so t=5.0000001 still reads as "5s later".)
        if self._last_assess_t is None or (s.t - self._last_assess_t) >= self.assessment_interval - 1e-9:
            v = self.assess(sim.telemetry, sim.mu_single)      # pure decision -> a Verdict
            # commit the verdict to the live state the fast part below reads each tick
            self.storm_active, self.drop = v.storm_active, v.malicious_drop_prob
            self.V, self.reserve = v.lyapunov_V, v.reserve_servers
            self._last_verdict, self._last_assess_t = v, s.t
        # EVERY tick: reactive capacity. Re-solve the Lyapunov optimum for the CURRENT load using
        # the V the decision set (higher V = more headroom); W stays nominal at 1.
        c_star = lyapunov_optimal_c(s, sim.mu_single, sim.cfg.c_max, self.util_p, V=self.V, W=1.0)
        # actuate BOTH levers through the exact same clamps the agentic fast loop uses: capacity in
        # [reserve, c_max] with no scale-down while the queue is backed up, and the drop filter
        # gated by storm_active. reserve = the pre-provision floor (min_servers).
        apply_decision(sim, self.storm_active, c_star, self.drop,
                       self.queue_hold_threshold, current_lam=s.lam_current,
                       memory=None, min_servers=self.reserve)


@dataclass
class ShadowRunner:
    """SHADOW mode: run the rule's decision on the LLM agent's telemetry each assessment,
    WITHOUT actuating, and record the (agent, rule) verdict pair. Lets us measure agreement /
    divergence cheaply alongside the agent's own end-to-end run — no second episode. Give it a
    rule configured exactly like the agent's site (same calendar + anticipation)."""
    rule:    RuleBasedController
    records: list[tuple[Verdict, Verdict]] = field(default_factory=list)

    def observe(self, sim, agent_verdict: Verdict) -> Verdict:
        rule_verdict = self.rule.assess(sim.telemetry, sim.mu_single)   # pure: no actuation
        self.records.append((agent_verdict, rule_verdict))
        return rule_verdict

    def agreement(self) -> dict:
        """Cycle-by-cycle agreement between the two judges over the episode."""
        n = len(self.records)
        if not n:
            return {"n": 0, "storm_agree": 1.0, "filter_agree": 1.0, "full_agree": 1.0}
        storm = sum(a.storm_active == r.storm_active for a, r in self.records) / n
        filt  = sum((a.malicious_drop_prob > 0) == (r.malicious_drop_prob > 0)
                    for a, r in self.records) / n
        full  = sum(a.agrees_with(r) for a, r in self.records) / n
        return {"n": n, "storm_agree": round(storm, 3),
                "filter_agree": round(filt, 3), "full_agree": round(full, 3)}
