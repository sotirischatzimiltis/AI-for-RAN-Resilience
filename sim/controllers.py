"""
Baseline controllers acting on the simulator's server count c(t).

- FixedController        : constant c (the prior paper's c in {1,2,4,6,...})
- LyapunovController     : drift-plus-penalty optimum c(t) (eqs. 10-14)
- ForecastLyapunov       : Lyapunov + a 1-step arrival-rate forecast (anticipation
                           ablation rung: same single lever, but pre-warms).

These run inside the simulator's control loop (sim._control_loop), invoked every
sample_dt_s. The agentic system will later replace/augment these via MCP tools.
"""

from __future__ import annotations

from dataclasses import dataclass

from .metrics import UtilityParams, utility
from .simulator import StormSim, TelemetrySample


def lyapunov_optimal_c(
    s:       TelemetrySample,
    mu:      float,
    c_max:   int,
    util_p:  UtilityParams,
    lam:     float | None = None,
    V:       float = 1000.0,
    W:       float = 1.0,
) -> int:
    """
    Drift-plus-penalty optimal server count (eq. 14), by integer search:

      min_c  Lq*(lam - c*mu) + 0.5*(lam - c*mu)^2 - V*u(c) + W*c
      s.t.   1 <= c <= c_max,  c integer

    Pure function shared by LyapunovController and the Near-RT control loop.
    `lam` defaults to the sample's current arrival rate; a forecast variant may
    pass a look-ahead estimate. (NOTE: V is on the raw scale ~1000s — rescaling
    the weights to a smaller range is parked for later.)
    """
    if lam is None:
        lam = s.lam_current
    best_c, best_obj = 1, float("inf")
    for c in range(1, c_max + 1):
        drift = s.queue_len * (lam - c * mu) + 0.5 * (lam - c * mu) ** 2
        probe = TelemetrySample(**{**s.__dict__, "c": c})
        u     = utility(probe, mu, util_p)
        obj   = drift - V * u + W * c
        if obj < best_obj:
            best_obj, best_c = obj, c
    return best_c


class FixedController:
    def __init__(self, c: int):
        self.c = c

    def step(self, sim: StormSim, s: TelemetrySample):
        sim.set_servers(self.c)


@dataclass
class LyapunovController:
    """
    Chooses c(t) minimising the drift-plus-penalty objective
    (see lyapunov_optimal_c). Weights V and W are on the raw scale.
    """
    V: float = 1000.0
    W: float = 1.0
    util_p: UtilityParams = None

    def __post_init__(self):
        if self.util_p is None:
            self.util_p = UtilityParams()

    def _lambda_estimate(self, sim, s):
        return s.lam_current

    def step(self, sim: StormSim, s: TelemetrySample):
        lam = self._lambda_estimate(sim, s)
        best_c = lyapunov_optimal_c(
            s, sim.mu_single, sim.cfg.c_max, self.util_p,
            lam=lam, V=self.V, W=self.W,
        )
        sim.set_servers(best_c)


@dataclass
class ForecastLyapunov(LyapunovController):
    """Lyapunov but using a 1-step-ahead arrival-rate forecast (pre-warming)."""
    horizon_s: float = 5.0

    def _lambda_estimate(self, sim, s):
        # peek the traffic schedule horizon_s into the future (idealised forecast;
        # in the agentic system this is replaced by a real predictor MCP tool)
        b, m = sim.cfg.traffic.rates_at(s.t + self.horizon_s)
        return max(s.lam_current, b + m)
