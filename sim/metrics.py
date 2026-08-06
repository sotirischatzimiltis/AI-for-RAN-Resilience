from __future__ import annotations
import math
from collections import Counter
from dataclasses import dataclass
from typing import List, Sequence
from .simulator import TelemetrySample

def resting_lam(telemetry, bin_size: float = 10.0, min_frac: float = 0.05) -> float:
    """The cell's calm-baseline arrival rate — the reference a storm is judged against.
    Returns the lowest arrival-rate bin the cell actually dwells in (sparse transition bins
    ignored), so it stays valid even when the whole window is inside a storm. SINGLE source of
    truth, imported by both the LLM judge (non_rt_agent) and the deterministic rule controller
    so the two can never drift apart."""
    lams = [s.lam_current for s in telemetry]
    if not lams:
        return 0.0
    binned = Counter(round(l / bin_size) * bin_size for l in lams)  # snap each lam to a bin, count occupancy
    floor  = max(1.0, min_frac * max(binned.values()))             # ignore sparse (<min_frac of busiest) bins
    return float(min(b for b, c in binned.items() if c >= floor))   # lowest bin the cell dwells in = rest

def benign_success_rate(stats) -> float:
    # Fraction of LEGITIMATE users that eventually attached. Includes, completed, failed and dropped at admission.
    outcomes = stats.benign_completed + stats.benign_failed + getattr(stats, "benign_dropped", 0)
    return stats.benign_completed / outcomes if outcomes > 0 else 1.0

def benign_false_positive_rate(stats) -> float:
    """Fraction of LEGITIMATE users dropped at admission by the malicious filter — the
    filter's collateral. On a benign-only surge (no botnet, e.g. single_storm) this is
    pure OVER-FILTERING: 0.0 means the judge correctly withheld the filter, >0 means it
    wrongly treated a benign surge as an attack. Isolates the deliberate filter drop from
    capacity-starvation failures (which benign_success_rate also folds in)."""
    dropped  = getattr(stats, "benign_dropped", 0)
    outcomes = stats.benign_completed + stats.benign_failed + dropped
    return dropped / outcomes if outcomes > 0 else 0.0

def per_storm_blocked(telemetry, storms) -> list[float]:
    # Fraction of botnet UEs dropped at admission DURING each storm window, from the
    # cumulative counters in telemetry. For each (t0, td):
    #   (dropped[td] - dropped[t0]) / (arrivals[td] - arrivals[t0]).
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


def _counter_at(telemetry, t, field):
    """Cumulative counter value at time t (last sample at/before t); 0 before the first sample."""
    val = 0
    for s in telemetry:
        if s.t <= t:
            val = getattr(s, field, 0)
        else:
            break
    return val


def per_storm_benign_served(telemetry, storms) -> list[float]:
    """Fraction of benign UEs that ATTACHED during each storm window, from the cumulative
    benign counters in telemetry: (completed[td]-completed[t0]) / (arrivals[td]-arrivals[t0]).
    Reported per experiment so the blended episode benign-served can be split by window (e.g. a
    model that serves the botnet window fully but starves the event surge). Minor boundary effect:
    a UE arriving near td may complete just after it (attach lag), same approximation as
    per_storm_blocked. A window with no benign arrivals returns 1.0 (nothing to serve)."""
    out = []
    for (t0, td) in storms:
        c = _counter_at(telemetry, td, "benign_completed") - _counter_at(telemetry, t0, "benign_completed")
        a = _counter_at(telemetry, td, "benign_arrivals")  - _counter_at(telemetry, t0, "benign_arrivals")
        out.append(round(c / a, 4) if a > 0 else 1.0)
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
    """Mean number of ONLINE servers over the episode — a capacity-cost proxy. 
    lower mean at equal resilience means the same protection for less capacity."""
    cs = [s.c_online for s in telemetry] # list of server counts over time
    return sum(cs) / len(cs) if cs else 0.0

def _percentile(vals: Sequence[float], p: float) -> float: # calculate the p-th percentile of a list of values using 
    # PERCENTILE IS A RANKED THRESHOLD. IS THE VALUE THAT P% OF THE DATA IS AT OR BELOW. 
    # linear-interpolation percentile (numpy's default method). vals need not be sorted.
    if not vals:
        return 0.0
    xs = sorted(vals) # sort the values to find the percentile buildin function returns sorted list 
    if len(xs) == 1:
        return xs[0]
    k    = (len(xs) - 1) * (p / 100.0)   # fractional rank of the p-th percentile
    lo   = int(k)                        # lower bracketing index
    hi   = min(lo + 1, len(xs) - 1)      # upper bracketing index
    frac = k - lo                        # how far between them
    return xs[lo] + (xs[hi] - xs[lo]) * frac   # interpolate

def attach_latency_stats(stats, storms=None, benign_only: bool = True) -> dict:
    """End-to-end attach latency (ms) of successful UEs — mean / p50 / p95 / count.
    Latency is measured from a UE's ORIGINAL arrival to completion, so it includes
    every T300 timeout, retry and queue wait — the real user-experienced attach time.
    - storms=None            -> whole episode.
    - storms=[(t0,td),...]   -> ONLY UEs that COMPLETED inside a storm window
                                (the 'latency-under-storm' view; needs completion_times).
    - benign_only=True       -> exclude botnet UEs that slipped through, so this reflects
                                REAL users' experience (needs completion_benign).

    Returns {"n", "mean_ms", "p50_ms", "p95_ms"}; zeros if no matching completions."""
    # the three index-aligned per-success lists (see Stats): delays[i], times[i] and
    # benign[i] all describe the SAME completed UE. getattr(...) tolerates older stats
    # objects that predate times/benign (falls back to empty, disabling those filters).
    delays = list(stats.completion_delays)
    times  = list(getattr(stats, "completion_times", []) or [])
    benign = list(getattr(stats, "completion_benign", []) or [])
    n = len(delays)

    idx = list(range(n))                                 # candidate completions: start with all, then narrow
    if benign_only and len(benign) == n:                 # drop bots that slipped through (len check = class was recorded)
        idx = [i for i in idx if benign[i]]
    if storms and len(times) == n:                       # drop completions outside every storm window
        def _in_storm(t):                                # True if t falls inside any (t0, td) storm
            return any(t0 <= t <= td for (t0, td) in storms)
        idx = [i for i in idx if _in_storm(times[i])]

    sample = [delays[i] for i in idx]                    # the surviving delays we actually report on
    if not sample:                                       # nothing matched the filters
        return {"n": 0, "mean_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0}
    return {
        "n":       len(sample),                          # how many completions this stat is over
        "mean_ms": round(sum(sample) / len(sample), 1),  # average attach time
        "p50_ms":  round(_percentile(sample, 50), 1),    # median — the typical user's experience
        "p95_ms":  round(_percentile(sample, 95), 1),    # tail — what the worst-served 5% felt
    }

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
    # Defaults match runtime.UP (the operative params), so a bare UtilityParams() agrees with the
    # deployed config — the controller's utility (LyapunovController) and the resilience score share
    # ONE utility (they must, else the system is judged against a yardstick it did not optimise for).
    # uA is UTILISATION-based: uA = 1/(1+exp(kA*(rho - mfracA))), rho = lam/(c*mu). Because rho already
    # normalises load by capacity, the steepness kA is c-INDEPENDENT (the old lam-based form had an
    # effective steepness kA*c*mu that turned into a cliff at high c).
    wA: float = 0.5
    wB: float = 0.5
    kA: float = 10.0           # uA steepness in UTILISATION space (kappa); c-independent
    kB: float = 0.004          # steepness on queue-length term (matches UP)
    mfracA: float = 0.90       # utilisation KNEE: uA = 0.5 at rho = mfracA (rho = lam/(c*mu))
    lq_max: float = 1500.0     # queue scale (matches UP); mB = 750 is reachable
    mfracB: float = 0.5        # midpoint fraction of lq_max

    def __post_init__(self):
        # utility is the convex combination wA*uA + wB*uB; it only stays in [0,1]
        # if the two weights partition 1. Fail loud on a bad weighting rather than
        # silently returning a utility (and hence resilience P) outside [0,1].
        if abs(self.wA + self.wB - 1.0) > 1e-9:
            raise ValueError(f"wA + wB must equal 1 (got {self.wA} + {self.wB} = {self.wA + self.wB})")

def _clamp_exp(x: float) -> float:
    # guard math.exp against OverflowError for extreme queue lengths (math.exp raises
    # above ~709). Clamping to +/-700 saturates the logistic to 0/1 as intended.
    return max(-700.0, min(700.0, x))

# the two utility components BEFORE weighting — exposed so reporting can decompose u without
# changing it. uA = capacity-margin term (UTILISATION vs the knee); uB = queue-health term.
def utility_parts(sample: TelemetrySample, mu_single: float, p: UtilityParams) -> tuple[float, float]:
    """(uA, uB) for one sample. utility() == p.wA*uA + p.wB*uB. A low uA with a high uB means the
    load was SERVED (queue short) but at high utilisation (little headroom) — thin margin, not
    starvation; that is the split that explains why a well-served surge can still score u<1.

    uA is UTILISATION-based: rho = lam/(c*mu) is the fraction of serving capacity in use, and
    uA = 1/(1+exp(kA*(rho - mfracA))) falls from ~1 (ample headroom) through 0.5 (at the knee
    rho=mfracA) to ~0 (saturated/overloaded). kA is the steepness in rho-space, so uA means the
    same thing at every capacity c (unlike the old lam-based form, whose sharpness scaled with c)."""
    cap = sample.c_online * mu_single                       # total serving capacity (servers * per-server rate)
    rho = (sample.lam_current / cap) if cap > 0 else float("inf")   # utilisation = offered load / capacity
    uA = 1.0 / (1.0 + math.exp(_clamp_exp(p.kA * (rho - p.mfracA))))
    mB = p.lq_max * p.mfracB
    uB = 1.0 / (1.0 + math.exp(_clamp_exp(p.kB * (sample.queue_len - mB))))
    return uA, uB

# method to calculate utility of a single telemetry sample, given the single-server service rate and utility parameters
def utility(sample: TelemetrySample, mu_single: float, p: UtilityParams) -> float:
    """u(t) in [0,1]; higher = more stable/resilient"""
    uA, uB = utility_parts(sample, mu_single, p)
    return p.wA * uA + p.wB * uB

# compute the utility time series for a sequence of telemetry samples
def utility_series(telemetry: Sequence[TelemetrySample],mu_single: float, p: UtilityParams) -> List[float]:
    return [utility(s, mu_single, p) for s in telemetry]

@dataclass(frozen=True)   # MET-7: frozen so the shared default-arg instance can't be mutated
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
                     hold_window: float = 30.0,          # MET-8: promoted from a hardcoded constant
                     t_limit: float = float("inf"),      # MET-4: cap the recovery scan (usually next storm's t0)
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
    # MET-4: only scan up to t_limit (the next storm's onset), so recovery from one
    # storm is never "found" inside the next one. If none is confirmed by then, tr is
    # CENSORED at scan_end rather than measured.
    scan_end = min(t_limit, ts[-1])
    tr = scan_end                    # default: not recovered within [td, scan_end]
    target = recovery_frac * u_des   # counts as recovered once u reaches 95% of baseline
    for i, t in enumerate(ts):
        if td <= t <= scan_end and us[i] >= target:     # after storm end, first time u hits target
            w_hi = min(t + hold_window, scan_end)       # ...and holds for hold_window (clamped to scan_end)
            held = [u for tt, u in zip(ts, us) if t <= tt <= w_hi]
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
    for k, (t0, td) in enumerate(storms):    # storms = [(start, end), ...] from storm_windows()
        # LOCAL baseline: mean utility over the calm just BEFORE this storm, measured at the cell's
        # REST capacity — NOT while a scheduled reserve is pre-provisioning. A serial reserve ramp
        # lands in [t0-lookback, t0] (servers spin up AHEAD of the event), driving c_online far above
        # what the resting load needs; utilisation rho = lam/(c*mu) collapses and utility is
        # transiently inflated, which would set an UNREACHABLE recovery bar (the cell returns to its
        # true rest utility, below 0.95*inflated-baseline, so recovery is never confirmed). So average
        # only the calm samples that are NOT over-provisioned (rho at/above a rest floor). Walk back a
        # little further than the nominal lookback in case the ramp fills the whole window; fall back
        # to the plain window mean if no rest sample is found. Scoring each storm against its own
        # recent rest-normal (not the global start) still means storm 2 is judged fresh.
        rho_rest_floor = 0.5                              # rest rho ~0.7; any pre-provisioned c gives rho<0.4
        max_lookback   = baseline_lookback_s + 60.0       # reach past a serial ramp that fills the window
        rest = [(s.t, u) for s, u in zip(telemetry, us)
                if t0 - max_lookback <= s.t < t0
                and s.c_online * mu_single > 0
                and s.lam_current / (s.c_online * mu_single) >= rho_rest_floor]
        recent = [u for t, u in rest if t >= t0 - baseline_lookback_s]   # prefer rest within the nominal window
        if recent:
            u_des = sum(recent) / len(recent)
        elif rest:
            u_des = sum(u for _, u in rest) / len(rest)                  # ramp filled the window -> older rest
        else:
            pre   = [u for t, u in zip(ts, us) if t0 - baseline_lookback_s <= t < t0]
            u_des = (sum(pre) / len(pre)) if pre else None               # no rest at all -> plain mean / auto
        # MET-4: cap this storm's recovery scan at the NEXT storm's onset (inf for the last).
        t_next = storms[k + 1][0] if k + 1 < len(storms) else float("inf")
        # score this one storm with its own u_des, reusing the single-window scorer
        r = resilience_score(telemetry, mu_single, util_p, t0=t0, td=td,
                             u_des=u_des, t_limit=t_next, weights=weights)
        # keep just the reportable fields, tagged with this storm's window
        per.append({"t0": t0, "td": td, **{k: r[k] for k in
                    ("P", "absorption", "adaptation", "trec", "recovery_time")}})

    # whole-episode score = plain mean of the per-storm P (every storm weighted equally)
    p_episode = sum(s["P"] for s in per) / len(per) if per else 0.0
    return {"P_episode": p_episode, "per_storm": per, "n_storms": len(per)}


def utility_decomposition(telemetry: Sequence[TelemetrySample],
                          mu_single: float,
                          util_p: UtilityParams,
                          storms: Sequence[tuple[float, float]]) -> list[dict]:
    """Per-storm-window breakdown of the resilience utility — DIAGNOSTIC only, does not change P.
    For each (t0, td) reports the mean capacity-margin term uA, the mean queue-health term uB, and
    the mean utilisation rho = lam/(c_online*mu). Reading it: a window with low uA + high uB + rho
    near 1 was SERVED (queue short) but ran with little headroom, so its absorption (hence P) is
    limited by MARGIN, not by users failing — the context that keeps a P<1 from being misread as
    starvation. Windows with no samples report the neutral (1, 1, 0)."""
    out = []
    for (t0, td) in storms:
        seg = [s for s in telemetry if t0 <= s.t <= td]
        if not seg:
            out.append({"t0": t0, "td": td, "uA": 1.0, "uB": 1.0, "rho": 0.0})
            continue
        uAs, uBs, rhos = [], [], []
        for s in seg:
            uA, uB = utility_parts(s, mu_single, util_p)
            uAs.append(uA); uBs.append(uB)
            cap = s.c_online * mu_single
            rhos.append(s.lam_current / cap if cap > 0 else 0.0)
        n = len(seg)
        out.append({"t0": t0, "td": td,
                    "uA": sum(uAs) / n, "uB": sum(uBs) / n, "rho": sum(rhos) / n})
    return out

def recovery_report(telemetry: Sequence[TelemetrySample],
                    mu_single: float,
                    util_p: UtilityParams,
                    storms: Sequence[tuple[float, float]],
                    baseline_lookback_s: float = 50.0,
                    recovery_frac: float = 0.95,
                    hold_window: float = 30.0) -> list[dict]:
    """DIAGNOSTIC: reproduce the recovery detector from resilience_score EXACTLY, but return its
    internals per storm window so a 'not recovered' verdict can be checked against the logs instead
    of trusted blind. For each (t0, td) reports:
      u_des        : the local pre-storm baseline it compares to (mean u over [t0-lookback, t0])
      target       : recovery_frac * u_des (the 0.95 line u must cross AND hold 30s)
      recovered    : did u cross target and hold for hold_window before the scan end?
      tr           : recovery time if recovered, else the censoring scan_end
      post_peak_u  : the BEST utility reached after the storm (if < target, it literally never
                     climbed back to the baseline — baseline likely inflated or backlog draining)
      post_peak_t  : when that peak occurred
      frac_above   : fraction of post-storm samples already at/above target (how close it was)
      calm_u_end   : mean u over the last 20s (where the system actually settled)
    Read-only; does not touch P."""
    ts = [s.t for s in telemetry]
    us = utility_series(telemetry, mu_single, util_p)
    out = []
    for k, (t0, td) in enumerate(storms):
        # baseline at REST, skipping the pre-provisioning ramp — identical rule to resilience_multi
        # so this diagnostic reports the SAME u_des the score uses (see resilience_multi for the why).
        rest = [(s.t, u) for s, u in zip(telemetry, us)
                if t0 - (baseline_lookback_s + 60.0) <= s.t < t0
                and s.c_online * mu_single > 0
                and s.lam_current / (s.c_online * mu_single) >= 0.5]
        recent = [u for t, u in rest if t >= t0 - baseline_lookback_s]
        if recent:
            u_des = sum(recent) / len(recent)
        elif rest:
            u_des = sum(u for _, u in rest) / len(rest)
        else:
            pre   = [u for t, u in zip(ts, us) if t0 - baseline_lookback_s <= t < t0]
            u_des = (sum(pre) / len(pre)) if pre else 1.0
        target = recovery_frac * u_des
        t_next = storms[k + 1][0] if k + 1 < len(storms) else float("inf")
        scan_end = min(t_next, ts[-1])
        tr = None
        for i, t in enumerate(ts):
            if td <= t <= scan_end and us[i] >= target:
                w_hi = min(t + hold_window, scan_end)
                held = [u for tt, u in zip(ts, us) if t <= tt <= w_hi]
                if held and min(held) >= target:
                    tr = t
                    break
        post = [(t, u) for t, u in zip(ts, us) if td <= t <= scan_end]
        peak_t, peak_u = max(post, key=lambda x: x[1]) if post else (td, 0.0)
        frac  = (sum(1 for _, u in post if u >= target) / len(post)) if post else 0.0
        tail  = [u for t, u in zip(ts, us) if t >= ts[-1] - 20.0]
        calm_u_end = (sum(tail) / len(tail)) if tail else 0.0
        out.append({"window_t0": t0, "window_td": td, "u_des": round(u_des, 4),
                    "target": round(target, 4), "recovered": tr is not None,
                    "tr": round(tr if tr is not None else scan_end, 1),
                    "post_peak_u": round(peak_u, 4), "post_peak_t": round(peak_t, 1),
                    "frac_above": round(frac, 3), "calm_u_end": round(calm_u_end, 4)})
    return out


def live_absorption(telemetry: Sequence[TelemetrySample],
                    mu_single: float,
                    util_p: UtilityParams,
                    baseline_s: float = 40.0,
                    window_s:   float = 30.0,
                    recovery_frac: float = 0.95,
                    hold_window:   float = 30.0) -> dict:
    """REAL-TIME filtering-effectiveness signal — computable online, NO storm-window
    oracle (unlike resilience_score, which needs t0/td). This is what the running judge
    should see; the offline resilience_multi keeps using the known windows for scoring.

      absorption : fraction of the calm baseline utility maintained over the RECENT
                   window [now - window_s, now], capped at 1. Tracks the storm being
                   handled NOW — high = utility held (filter working), low = degrading.
      u_des      : the calm baseline — mean utility over the episode's OPENING calm
                   [t0, t0 + baseline_s] (episodes start calm; a real cell would use a
                   rolling estimate from recent quiet).
      recovered  : True if utility has been >= recovery_frac*u_des for the last
                   hold_window seconds — the same "back-to-baseline and holding" test as
                   the offline recovery detector, but self-triggered (looks backward from
                   now) instead of anchored to a known storm end.
    """
    ts = [s.t for s in telemetry]
    us = utility_series(telemetry, mu_single, util_p)
    if len(ts) < 2:
        return {"absorption": 1.0, "u_des": 0.0, "recovered": True}
    t_now = ts[-1]

    # opening-calm baseline u_des: mean utility over the first baseline_s of the episode
    base  = [u for t, u in zip(ts, us) if t <= ts[0] + baseline_s]
    u_des = (sum(base) / len(base)) if base else 1.0

    # absorption over the recent window: actual area under u(t) / ideal area at u_des
    recent = [(t, u) for t, u in zip(ts, us) if t >= t_now - window_s]
    if len(recent) < 2 or u_des <= 0:
        absorption = 1.0                      # too little data (or flat baseline) -> treat as fine
    else:
        xs  = [t for t, _ in recent]
        ys  = [u for _, u in recent]
        den = u_des * (xs[-1] - xs[0])        # ideal: hold u_des across the window
        absorption = min(1.0, _trapz(ys, xs) / den) if den > 0 else 1.0

    # recovered-now: utility held >= 95% of baseline for the last hold_window seconds
    target = recovery_frac * u_des
    held   = [u for t, u in zip(ts, us) if t >= t_now - hold_window]
    recovered = bool(held) and min(held) >= target

    return {"absorption": round(absorption, 4),
            "u_des":      round(u_des, 4),
            "recovered":  recovered}
