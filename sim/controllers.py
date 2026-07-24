"""
Baseline controllers acting on the simulator's server count c(t).
- FixedController        : constant c (the prior paper's c in {1,2,4,6,...})
- LyapunovController     : drift-plus-penalty optimum c(t) (eqs. 10-14)
These run inside the simulator's control loop (sim._control_loop), invoked every
control_dt_s.
"""
from __future__ import annotations
from dataclasses import dataclass, replace
from .metrics import UtilityParams, utility
from .simulator import StormSim, TelemetrySample

def lyapunov_optimal_c(
    s:       TelemetrySample,
    mu:      float,
    c_max:   int,
    util_p:  UtilityParams,
    lam:     float | None = None,
    V:       float = 1.0,
    W:       float = 1.0,
) -> int:
    """
    Drift-plus-penalty optimal server count (eq. 14), by integer search:

      min_c  qn*an + 0.5*an^2  -  V*u(c)  +  W*(c/c_max)
      s.t.   1 <= c <= c_max,  c integer

    CTL-2: every term is normalised to O(1) so V and W actually bind — in the raw
    form the drift swung ~1e5 while V*u <= V and W*c <= c_max, leaving V and W inert
    at their nominal values (a V-sweep moved c* only above V~1e4). Here:
      an = lam/mu - c   -- net demand-minus-capacity, in units of SERVERS.
             lam [UEs/s] / mu [UEs/s per server] cancels to a pure server COUNT: the
             "offered load", i.e. how many servers' worth of work is arriving (lam/mu ~ 7
             at a 200 UEs/s storm with mu=28.7). Subtracting c (servers we HAVE) gives the
             net shortfall: an>0 = under-provisioned (queue grows), an<0 = headroom (drains),
             an~0 = balanced. This is the imbalance the objective drives toward zero.
      qn = queue / lq_max  -- normalised backlog in [0, ~1]
      u(c) in [0,1], c/c_max in [0,1]
    so V and W trade off comparable quantities and are on an O(1) scale (try V,W ~ 1-10).
    (The control-step dt cancels out of the normalised drift, so CTL-1 is subsumed here.)

    Pure function shared by LyapunovController and the Near-RT control loop. `lam`
    defaults to the sample's current arrival rate; a forecast variant passes a look-ahead.
    """
    if lam is None:
        lam = s.lam_current
    qn = s.queue_len / util_p.lq_max
    best_c, best_obj = 1, float("inf")
    for c in range(1, c_max + 1):
        an    = (lam / mu) - c    # net shortfall in SERVERS: (work arriving) - (servers we have); >0 short, <0 spare
        # drift = push on the backlog. 0.5*an^2 penalises imbalance in EITHER direction
        # (too few servers grows the queue; too many wastes capacity); qn*an makes an
        # existing backlog + positive shortfall the worst case, pushing c up.
        drift = qn * an + 0.5 * an ** 2
        u     = utility(replace(s, c_online=c, c_target=c), mu, util_p)
        obj   = drift - V * u + W * (c / c_max)
        if obj < best_obj:
            best_obj, best_c = obj, c
    return best_c

@dataclass
class FixedController:
    """Constant server count. CTL-9: only re-commands c when it actually changes,
    saving ~1000 redundant set_servers calls per run."""
    c: int
    _last_c: int = -1

    def step(self, sim: StormSim, s: TelemetrySample):
        if self.c != self._last_c:
            sim.set_servers(self.c)
            self._last_c = self.c

@dataclass
class LyapunovController:
    # Normalised drift-plus-penalty controller (see lyapunov_optimal_c). After CTL-2
    # the weights are on an O(1) scale (was ~1000): V rewards utility/headroom (raise
    # it to pre-provision), W penalises servers. V=1 is a load-tracking reactive baseline.
    V: float = 1.0                       # utility/QoS weight: higher -> provision MORE servers
    W: float = 1.0                       # server-cost weight: higher -> provision FEWER servers
    util_p: UtilityParams | None = None  # utility params for u(c); defaults to UtilityParams() below
    # CTL-3: estimate lambda from REALISED attempts (delta arrivals / delta t, i.e.
    # lambda_eff incl. retries) rather than the exogenous schedule rate. Running both
    # arms and reporting the gap quantifies retry amplification. Default False = the
    # schedule rate (paper-equivalent, blind to amplification).
    use_measured: bool = False

    def __post_init__(self):
        if self.util_p is None:          # no params passed -> use the shared defaults
            self.util_p = UtilityParams()

    # Decide WHICH arrival rate lambda the optimiser plans against this tick — the
    # controller needs a lambda to compute an = lambda/mu - c. Two choices:
    #   schedule rate (s.lam_current) : only the NEW UEs arriving (blind to retries)
    #   measured rate (delta arrivals): actual attempts/s incl. retries = lambda_eff,
    #                                   which is retry-amplified under overload
    # Running both arms and comparing P quantifies retry amplification (CTL-3) — the
    # contribution over the M/M/c model, which had no retries so no gap between them.
    # Isolated here (not inline in step) so the >=2-samples / dt>0 guards live in one
    # place and a future FORECAST controller can override this to plan ahead of a storm.
    def _lambda_estimate(self, sim, s):
        if self.use_measured and len(sim.telemetry) >= 2:  # measured arm + at least 2 samples to difference
            prev = sim.telemetry[-2]                        # the previous telemetry sample
            dt = s.t - prev.t                               # time between the two samples (s)
            if dt > 0:
                return (s.arrivals - prev.arrivals) / dt   # realised attempt rate (incl. retries): lambda_eff
        return s.lam_current                                # schedule rate (blind to retry amplification)

    # Called every control_dt_s by sim._control_loop: pick c* and command it.
    def step(self, sim: StormSim, s: TelemetrySample):
        lam = self._lambda_estimate(sim, s)                 # which lambda to plan against (schedule vs measured)
        best_c = lyapunov_optimal_c(                        # integer search for the drift-plus-penalty optimum
            s, sim.mu_single, sim.cfg.c_max, self.util_p,
            lam=lam, V=self.V, W=self.W,
        )
        sim.set_servers(best_c)                             # actuate: command the chosen server count
