"""
Short-term telemetry forecast — data-driven extrapolation of the UNKNOWN.

Where the calendar (event_calendar.py) answers "what load is SCHEDULED?", the
forecast answers "where is the telemetry HEADING in the next ~20s?". It fits an
ordinary least-squares line to the last ~30s of each signal and projects the
slope forward, so the Non-RT-Agent can pre-provision on a PREDICTED ramp before
a storm is confirmed — the predictive complement to the calendar's scheduled
pre-provisioning.

Signals (from host.sim.telemetry):
  lam        — instantaneous arrival rate            [LEADING]
  retry_rate — d(retries)/dt, from cumulative counts [LEADING]
  fail_rate  — d(failed)/dt,  from cumulative counts [LEADING, QoS breach]
  queue_len  — attempts waiting                       [LAGGING]

Method note: this is a LINEAR fit. arrival rate is really a step, not a line, so
over a sustained plateau the projection will overshoot — the reported R^2 /
confidence exists so the agent can discount a poor fit. Short horizon keeps the
error bounded; a saturating model can replace the fit later without changing this
interface.
"""

from __future__ import annotations


def _ols(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Ordinary least-squares fit y = m*x + b. Returns (slope, intercept, r2).

    r2 is the coefficient of determination in [0, 1]; 0.0 when y is flat (no
    variance to explain) or the fit is degenerate.
    """
    n = len(xs)
    if n < 2:
        return 0.0, (ys[-1] if ys else 0.0), 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return 0.0, my, 0.0
    m = sxy / sxx
    b = my - m * mx
    syy = sum((y - my) ** 2 for y in ys)
    if syy == 0:
        return m, b, 0.0
    ss_res = sum((y - (m * x + b)) ** 2 for x, y in zip(xs, ys))
    r2 = max(0.0, 1.0 - ss_res / syy)
    return m, b, r2


def _rate_series(win, field: str) -> tuple[list[float], list[float]]:
    """Derivative series of a cumulative counter: rate between consecutive
    samples, stamped at the later time. Returns (times, rates)."""
    ts, rs = [], []
    for a, b in zip(win, win[1:]):
        dt = b.t - a.t
        if dt <= 0:
            continue
        ts.append(b.t)
        rs.append((getattr(b, field) - getattr(a, field)) / dt)
    return ts, rs


def _confidence(r2: float) -> str:
    if r2 >= 0.7:
        return "high"
    if r2 >= 0.3:
        return "medium"
    return "low"


def _trend(slope: float, horizon_s: float, eps: float) -> str:
    """Rising / falling / flat, judged by the projected change over the horizon."""
    change = slope * horizon_s
    if change > eps:
        return "rising"
    if change < -eps:
        return "falling"
    return "flat"


# per-signal (times, values) extractor and a "flat" threshold on the projected change
_SIGNALS = {
    "lam":        (lambda w: ([s.t for s in w], [s.lam_current for s in w]), 15.0),
    "queue_len":  (lambda w: ([s.t for s in w], [float(s.queue_len) for s in w]), 20.0),
    "retry_rate": (lambda w: _rate_series(w, "retries"), 2.0),
    "fail_rate":  (lambda w: _rate_series(w, "failed"), 1.0),
}


def forecast_signals(telemetry, window_s: float = 30.0, horizon_s: float = 20.0) -> dict:
    """Fit each signal over the last window_s and project horizon_s ahead.

    Returns {t_now, window_s, horizon_s, signals: {name: {current, slope_per_s,
    predicted, trend, confidence}}}. Predictions are clamped to >= 0.
    """
    if not telemetry:
        return {"error": "no telemetry yet — episode may not have started"}
    t_now = telemetry[-1].t
    win = [s for s in telemetry if s.t >= t_now - window_s]
    if len(win) < 3:
        return {"error": f"only {len(win)} sample(s) in window — need more history"}

    signals: dict[str, dict] = {}
    for name, (extract, eps) in _SIGNALS.items():
        ts, ys = extract(win)
        if len(ts) < 2:
            continue
        m, _b, r2 = _ols(ts, ys)
        current = ys[-1]
        # project from where we actually are at the fitted slope (nowcast), so
        # current -> predicted is always consistent with the reported trend.
        predicted = max(0.0, current + m * horizon_s)
        signals[name] = {
            "current":      round(current, 2),
            "slope_per_s":  round(m, 3),
            "predicted":    round(predicted, 2),
            "trend":        _trend(m, horizon_s, eps),
            "confidence":   _confidence(r2),
        }

    return {
        "t_now":     round(t_now, 1),
        "window_s":  round(win[-1].t - win[0].t, 1),
        "horizon_s": horizon_s,
        "signals":   signals,
    }


def summarize_forecast(telemetry, window_s: float = 30.0, horizon_s: float = 20.0) -> str:
    """One-line-per-signal forecast summary for the model, with a headline when a
    steep arrival-rate rise is predicted with usable confidence."""
    f = forecast_signals(telemetry, window_s, horizon_s)
    if "error" in f:
        return f["error"]

    sig = f["signals"]
    lines = [f"FORECAST (+{f['horizon_s']:.0f}s, from {f['window_s']:.0f}s of history):"]
    for name in ("lam", "retry_rate", "fail_rate", "queue_len"):
        s = sig.get(name)
        if not s:
            continue
        lines.append(
            f"  {name}: {s['current']:.0f} -> ~{s['predicted']:.0f} "
            f"({s['trend']}, {s['slope_per_s']:+.1f}/s, {s['confidence']} conf)"
        )

    lam = sig.get("lam")
    if lam and lam["trend"] == "rising" and lam["confidence"] != "low" \
            and lam["predicted"] > 1.5 * max(lam["current"], 1.0):
        lines.insert(1, "  ALERT: arrival rate ramping — consider pre-provisioning "
                        "(tighten=true, raise lyapunov_V) ahead of confirmation.")
    return "\n".join(lines)
