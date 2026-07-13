"""
Resilience scoring: utility function u(t) and the A3RT resilience metric P,
reconstructed from Chatzimiltis et al. (arXiv:2505.00605), eqs (8) and (9).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence

from .simulator import TelemetrySample


def benign_success_rate(stats) -> float:
    """Fraction of LEGITIMATE users that eventually attached:
    benign_completed / (benign_completed + benign_failed).

    This is the admission metric that matters. The aggregate completed/failed is
    misleading because `failed` also counts every botnet UE the filter blocks —
    so an effective filter would look like it is 'failing' users. Only the benign
    figures answer 'did real users get served?'. Report this ALONGSIDE P.
    """
    served = stats.benign_completed + stats.benign_failed
    return stats.benign_completed / served if served > 0 else 1.0


def malicious_blocked_rate(stats) -> float:
    """Fraction of botnet UEs denied service (dropped at admission OR eventually
    failed): (malicious_dropped + malicious_failed) / all malicious outcomes.
    High is good — the attack was absorbed."""
    mal_denied    = stats.malicious_dropped + stats.malicious_failed
    mal_completed = stats.completed - stats.benign_completed   # botnet that got through
    denom = mal_denied + mal_completed
    return mal_denied / denom if denom > 0 else 0.0


@dataclass
class UtilityParams:
    wA: float = 0.5
    wB: float = 0.5
    kA: float = 0.5            # steepness on arrival-rate term
    kB: float = 0.01           # steepness on queue-length term
    mfracA: float = 0.75       # midpoint fraction of c*mu
    lq_max: float = 7000.0
    mfracB: float = 0.5        # midpoint fraction of lq_max


def utility(sample: TelemetrySample, mu_single: float, p: UtilityParams) -> float:
    """u(t) in [0,1]; higher = more stable/resilient (eq. 9)."""
    mA = sample.c * mu_single * p.mfracA
    uA = 1.0 / (1.0 + math.exp(p.kA * (sample.lam_current - mA)))
    mB = p.lq_max * p.mfracB
    uB = 1.0 / (1.0 + math.exp(p.kB * (sample.queue_len - mB)))
    return p.wA * uA + p.wB * uB


def utility_series(telemetry: Sequence[TelemetrySample],
                  mu_single: float, p: UtilityParams) -> List[float]:
    return [utility(s, mu_single, p) for s in telemetry]


@dataclass
class ResilienceWeights:
    w1: float = 0.4   # absorption
    w2: float = 0.4   # adaptation
    w3: float = 0.2   # time-to-recovery


def _trapz(ys: Sequence[float], xs: Sequence[float]) -> float:
    s = 0.0
    for i in range(1, len(ys)):
        s += 0.5 * (ys[i] + ys[i - 1]) * (xs[i] - xs[i - 1])
    return s


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
    ts = [s.t for s in telemetry]
    us = utility_series(telemetry, mu_single, util_p)

    if u_des is None:
        pre = [u for t, u in zip(ts, us) if t < t0]
        u_des = (sum(pre) / len(pre)) if pre else 1.0

    # detect recovery time tr : first t >= td where u rises above target and
    # holds for a sustained window (robust to brief late dips)
    tr = ts[-1]
    target = recovery_frac * u_des
    hold_window = 30.0  # seconds u must stay recovered to count as recovered
    for i, t in enumerate(ts):
        if t >= td and us[i] >= target:
            held = [u for tt, u in zip(ts, us) if t <= tt <= t + hold_window]
            if held and min(held) >= target:
                tr = t
                break

    # absorption term over [t0, td]
    seg1 = [(t, u) for t, u in zip(ts, us) if t0 <= t <= td]
    # adaptation term over [td, tr]
    seg2 = [(t, u) for t, u in zip(ts, us) if td <= t <= tr]

    def _ratio(seg):
        # Fraction of the DESIRED utility that was actually maintained over the
        # segment, capped at 1.0: maintaining >= u_des is perfectly resilient, and
        # over-provisioning above the pre-storm baseline must not earn P > 1.
        if len(seg) < 2:
            return 1.0
        xs = [t for t, _ in seg]
        ys = [u for _, u in seg]
        num = _trapz(ys, xs)
        den = u_des * (xs[-1] - xs[0])
        return min(1.0, num / den) if den > 0 else 1.0

    absorption = _ratio(seg1)
    adaptation = _ratio(seg2)
    span = tr - t0
    trec = 1.0 if span <= dt_des else dt_des / span

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
    ts = [s.t for s in telemetry]
    us = utility_series(telemetry, mu_single, util_p)

    per = []
    for (t0, td) in storms:
        pre = [u for t, u in zip(ts, us) if t0 - baseline_lookback_s <= t < t0]
        u_des = (sum(pre) / len(pre)) if pre else None   # None -> core auto-calibrates
        r = resilience_score(telemetry, mu_single, util_p, t0=t0, td=td,
                             u_des=u_des, weights=weights)
        per.append({"t0": t0, "td": td, **{k: r[k] for k in
                    ("P", "absorption", "adaptation", "trec", "recovery_time")}})

    p_episode = sum(s["P"] for s in per) / len(per) if per else 0.0
    return {"P_episode": p_episode, "per_storm": per, "n_storms": len(per)}
