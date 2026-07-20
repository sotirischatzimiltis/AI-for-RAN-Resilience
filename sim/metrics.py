from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence

from .simulator import TelemetrySample

def benign_success_rate(stats) -> float:
    # Fraction of LEGITIMATE users that eventually attached:
    # benign_completed / (benign_completed + benign_failed).
    served = stats.benign_completed + stats.benign_failed
    return stats.benign_completed / served if served > 0 else 1.0

def per_storm_blocked(telemetry, storms) -> list[float]:
    #Fraction of botnet UEs dropped at admission DURING each storm window, from
    # the cumulative counters in telemetry.
    #For each (t0, td): (dropped[td]-dropped[t0]) / (arrivals[td]-arrivals[t0]).
    def counter_value_at(t, field):
        val = 0                          # default: t precedes the first sample -> 0
        for s in telemetry:              # scan snapshots in time order
            if s.t <= t:                 # sample is at/before t...
                val = getattr(s, field)  # ...keep it (later ones overwrite earlier)
            else:
                break                    # first sample past t -> stop; val now holds the answer
        return val

    out = []
    for (t0, td) in storms:
        # cumulative counters, so activity DURING the window = end value - start value
        d = counter_value_at(td, "malicious_dropped") - counter_value_at(t0, "malicious_dropped")
        a = counter_value_at(td, "malicious_arrivals") - counter_value_at(t0, "malicious_arrivals")
        out.append(round(d / a, 4) if a > 0 else 0.0)   # blocked fraction for this storm
    return out

def malicious_blocked_rate(stats) -> float:
    """Fraction of botnet UEs denied service (dropped at admission OR eventually
    failed): (malicious_dropped + malicious_failed) / all malicious outcomes.
    High is good — the attack was absorbed. NOTE: this counts BOTH deliberate filter
    drops and incidental starvation-failures, so a capacity-starved system with no
    filter can score high while also failing benign traffic; pair it with
    benign_success_rate, or use malicious_filtered_rate to isolate the deliberate defense."""
    mal_denied    = stats.malicious_dropped + stats.malicious_failed
    mal_completed = stats.completed - stats.benign_completed   # botnet that got through
    denom = mal_denied + mal_completed
    return mal_denied / denom if denom > 0 else 0.0

def avg_servers(telemetry) -> float:
    """Mean number of ONLINE servers over the episode — a capacity-cost proxy. A static
    controller sits at its fixed count; a dynamic one scales down in calm periods, so a
    lower mean at equal resilience means the same protection for less capacity."""
    cs = [s.c for s in telemetry] # list of server counts over time
    return sum(cs) / len(cs) if cs else 0.0

def resilience_efficiency(P: float, avg_servers: float, c_max: float) -> float:
    """Resilience delivered per unit of capacity used: P / (avg_servers / c_max).

    Interpretation: 1.0 == a controller that burns ALL c_max servers to reach P=1.0.
    Higher is better (more resilience per server); the agent should beat a brute-force
    static controller here even while tying it on P alone. This does NOT change P — it
    is a reporting companion that exposes capacity cost, computed from P and avg_servers.

    WARNING: efficiency rewards frugality, so an under-provisioned low-P controller
    (e.g. Static c=1) can score high on efficiency while being useless. Read it ONLY
    next to P — as a tiebreaker among controllers that already reach an acceptable P,
    never as a standalone ranking."""
    frac = avg_servers / c_max
    return P / frac if frac > 0 else 0.0

def malicious_filtered_rate(stats) -> float:
    """Fraction of botnet UEs DELIBERATELY dropped at admission by the filter (the
    intended defense), out of all botnet outcomes. Unlike malicious_blocked_rate this
    excludes starvation-failures, so a no-filter baseline scores 0 no matter how
    overloaded it is — it isolates 'did the system actively filter the attack?'."""
    mal_completed = stats.completed - stats.benign_completed
    denom = stats.malicious_dropped + stats.malicious_failed + mal_completed
    return stats.malicious_dropped / denom if denom > 0 else 0.0

@dataclass
class UtilityParams:
    wA: float = 0.5
    wB: float = 0.5
    kA: float = 0.5            # steepness on arrival-rate term
    kB: float = 0.01           # steepness on queue-length term
    mfracA: float = 0.75       # midpoint fraction of c*mu
    lq_max: float = 7000.0
    mfracB: float = 0.5        # midpoint fraction of lq_max

    def __post_init__(self):
        # utility is the convex combination wA*uA + wB*uB; it only stays in [0,1]
        # if the two weights partition 1. Fail loud on a bad weighting rather than
        # silently returning a utility (and hence resilience P) outside [0,1].
        if abs(self.wA + self.wB - 1.0) > 1e-9:
            raise ValueError(f"wA + wB must equal 1 (got {self.wA} + {self.wB} = {self.wA + self.wB})")
        
# method to calculate utility of a single telemetry sample, given the single-server service rate and utility parameters
def utility(sample: TelemetrySample, mu_single: float, p: UtilityParams) -> float:
    """u(t) in [0,1]; higher = more stable/resilient"""
    mA = sample.c * mu_single * p.mfracA
    uA = 1.0 / (1.0 + math.exp(p.kA * (sample.lam_current - mA)))
    mB = p.lq_max * p.mfracB
    uB = 1.0 / (1.0 + math.exp(p.kB * (sample.queue_len - mB)))
    return p.wA * uA + p.wB * uB

# compute the utility time series for a sequence of telemetry samples
def utility_series(telemetry: Sequence[TelemetrySample],mu_single: float, p: UtilityParams) -> List[float]:
    return [utility(s, mu_single, p) for s in telemetry]

@dataclass
class ResilienceWeights:
    w1: float = 0.4   # absorption
    w2: float = 0.4   # adaptation
    w3: float = 0.2   # time-to-recovery

    def __post_init__(self):
        # P = w1*absorption + w2*adaptation + w3*trec is a convex blend of three
        # components each in [0,1]; it only stays in [0,1] if the weights partition 1.
        # Fail loud on a bad weighting rather than silently returning P outside [0,1].
        if abs(self.w1 + self.w2 + self.w3 - 1.0) > 1e-9:
            raise ValueError(f"w1 + w2 + w3 must equal 1 (got {self.w1} + {self.w2} + {self.w3} = {self.w1 + self.w2 + self.w3})")

# compute the area under a curve defined by (xs, ys) using the trapezoidal rule (integration)
# here it integrates utility u(t) over time: xs = timestamps, ys = utility values.
# each adjacent pair of samples forms a trapezoid; we sum their areas.
def _trapz(ys: Sequence[float], xs: Sequence[float]) -> float:
    s = 0.0                                  # running total of the area
    for i in range(1, len(ys)):              # walk each adjacent pair (i-1, i)
        avg_height = 0.5 * (ys[i] + ys[i - 1])   # mean of the two endpoint values
        width      = xs[i] - xs[i - 1]           # gap between the two timestamps (Δt)
        s += avg_height * width              # area of this one trapezoid slice
    return s                                 # total area = integral over [xs[0], xs[-1]]

def resilience_score(telemetry: Sequence[TelemetrySample],
                     mu_single: float,
                     util_p: UtilityParams,
                     t0: float, td: float,
                     u_des: float = None,
                     dt_des: float = 60.0,
                     recovery_frac: float = 0.95,
                     weights: ResilienceWeights = ResilienceWeights()) -> dict:
    """
    A3RT resilience metric P (eq. 8).

      t0  : storm start (begin absorption window)
      td  : storm end   (begin adaptation/recovery window)
      tr  : detected recovery time (u returns to recovery_frac*u_des and holds)
      dt_des : desired recovery-time threshold for the trec term.
      u_des  : desired/ideal utility. If None, auto-calibrated to the mean
               pre-storm baseline utility over [0, t0] (recommended).

    Returns dict with P and its components.
    """
    # unpack telemetry into parallel lists: timestamps and the utility u(t) at each
    ts = [s.t for s in telemetry]
    us = utility_series(telemetry, mu_single, util_p)

    # u_des = the "ideal" utility the storm is scored against. If not given, calibrate
    # it to the mean utility during the calm PRE-storm window [0, t0] (the system's own
    # healthy baseline), so P measures recovery back to normal, not to a fixed 1.0.
    if u_des is None:
        pre = [u for t, u in zip(ts, us) if t < t0]
        u_des = (sum(pre) / len(pre)) if pre else 1.0

    # ---- recovery time tr: when did utility climb back and STAY back? ----
    # tr defaults to the last sample (i.e. "never recovered within the episode").
    tr = ts[-1]
    target = recovery_frac * u_des   # counts as recovered once u reaches 95% of baseline
    hold_window = 30.0               # ...and stays there for this many seconds (ignore brief dips)
    for i, t in enumerate(ts):
        if t >= td and us[i] >= target:                 # after storm end, first time u hits target
            held = [u for tt, u in zip(ts, us) if t <= tt <= t + hold_window]  # the next 30s of u
            if held and min(held) >= target:            # if u never dips below target in that window
                tr = t                                  # ...this is a genuine recovery -> record it
                break

    # slice the utility curve into the two scored windows:
    seg1 = [(t, u) for t, u in zip(ts, us) if t0 <= t <= td]   # absorption: during the storm [t0, td]
    seg2 = [(t, u) for t, u in zip(ts, us) if td <= t <= tr]   # adaptation: recovery phase [td, tr]

    def _ratio(seg):
        # Fraction of the DESIRED utility that was actually maintained over the
        # segment, capped at 1.0: maintaining >= u_des is perfectly resilient, and
        # over-provisioning above the pre-storm baseline must not earn P > 1.
        if len(seg) < 2:                 # too few points to integrate -> treat as perfect
            return 1.0
        xs = [t for t, _ in seg]
        ys = [u for _, u in seg]
        num = _trapz(ys, xs)             # actual area under u(t) over the window
        den = u_des * (xs[-1] - xs[0])   # ideal area = flat u_des across the same span
        return min(1.0, num / den) if den > 0 else 1.0   # achieved fraction, capped at 1

    absorption = _ratio(seg1)            # how well utility held up DURING the storm
    adaptation = _ratio(seg2)            # how well it recovered AFTER the storm
    span = tr - t0                       # total time from storm onset to recovery
    # trec: fast recovery (<= dt_des) scores 1.0; slower recovery decays as dt_des/span
    trec = 1.0 if span <= dt_des else dt_des / span

    # final resilience P = weighted blend of the three components (weights sum to 1)
    P = weights.w1 * absorption + weights.w2 * adaptation + weights.w3 * trec
    return {
        "P": P,
        "absorption": absorption,
        "adaptation": adaptation,
        "trec": trec,
        "tr": tr,
        "recovery_time": tr - t0,
    }

def resilience_multi(telemetry: Sequence[TelemetrySample],
                     mu_single: float,
                     util_p: UtilityParams,
                     storms: Sequence[tuple[float, float]],
                     baseline_lookback_s: float = 50.0,
                     weights: ResilienceWeights = ResilienceWeights()) -> dict:
    """Per-storm resilience plus a whole-episode aggregate, for multi-storm runs.

    Each storm (t0, td) is scored against its OWN local pre-storm baseline —
    u_des = mean utility over [t0 - baseline_lookback_s, t0] — so storm 2 isn't
    judged against storm 1's degraded state. This also captures the evolution
    story: the per-storm P should climb as the agent tunes its posture.

    The whole-episode P is the mean of the per-storm P (each storm weighted
    equally). Falls back to the single-window score when there is one storm.

    Returns {P_episode, per_storm: [{t0, td, P, absorption, adaptation, trec,
    recovery_time}], n_storms}.
    """
    # utility curve for the whole episode (all storms share the same telemetry)
    ts = [s.t for s in telemetry]
    us = utility_series(telemetry, mu_single, util_p)

    per = []                                 # one result dict per storm
    for (t0, td) in storms:                  # storms = [(start, end), ...] from storm_windows()
        # LOCAL baseline: mean utility over the 50s of calm just BEFORE this storm.
        # Scoring each storm against its own recent-normal (not the global start) means
        # storm 2 is judged fresh, not penalised for storm 1's leftover degradation.
        pre = [u for t, u in zip(ts, us) if t0 - baseline_lookback_s <= t < t0]
        u_des = (sum(pre) / len(pre)) if pre else None   # None -> resilience_score auto-calibrates
        # score this one storm with its own u_des, reusing the single-window scorer
        r = resilience_score(telemetry, mu_single, util_p, t0=t0, td=td,
                             u_des=u_des, weights=weights)
        # keep just the reportable fields, tagged with this storm's window
        per.append({"t0": t0, "td": td, **{k: r[k] for k in
                    ("P", "absorption", "adaptation", "trec", "recovery_time")}})

    # whole-episode score = plain mean of the per-storm P (every storm weighted equally)
    p_episode = sum(s["P"] for s in per) / len(per) if per else 0.0
    return {"P_episode": p_episode, "per_storm": per, "n_storms": len(per)}
