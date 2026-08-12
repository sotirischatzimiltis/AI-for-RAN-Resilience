"""
Shared-compute contention model (Experiment 5).

vCU/vDU share a finite compute pool. When many attach-workers are busy the per-attach PROCESSING
time inflates; the propagation (F1 / O-FH link) component is fixed physics and never inflates.

We use a GENTLE, BOUNDED, dead-zone model rather than the M/M/1 processor-sharing pole 1/(1-rho_c),
which punishes far too hard (3x slowdown already at 2/3 occupancy) and would make adding servers
REDUCE total throughput. Here:

    rho_c = busy / kappa                         # compute-pool occupancy (NOT the queue utilisation)
    s(rho_c) = 1                                  for rho_c <= rho0        (no slowdown below the knee)
    s(rho_c) = 1 + a * (rho_c - rho0)/(1 - rho0)  for rho_c >  rho0        (linear, bounded by 1+a)

with rho0 the onset knee (default 0.65 ~ 2/3, since shared compute realistically contends from about
two thirds occupancy) and a the severity (default 0.5, a mild ~12% penalty for running the pool to
full; no backfire cliff). The natural setting is kappa = c_max, so rho_c = busy/c_max is the fraction
of the maximum capacity that is busy: contention engages once more than rho0 of the servers are
active (busy > 0.65*16 = 10.4 at c_max=16). The anticipatory reserve (~13 servers, rho_c ~ 0.81) sits
inside this regime while a lean reactive controller (~10 servers, rho_c ~ 0.63) sits just below it.
The model is bounded (no pole), so kappa = c_max is safe (at full occupancy rho_c = 1, s = 1 + a).

NOTE the two distinct rho's this experiment keeps separate:
  * rho_c = busy/kappa                 -- compute-pool occupancy; drives the slowdown here.
  * rho   = lam/(c*mu)                 -- queue utilisation; drives the utility knee (uA=0.5 at 0.9).
Contention couples them: under load the utility must use the SLOWED rate mu_eff (see mu_eff below),
so the controller/score see the true utilisation rho_eff = lam/(c*mu_eff) rather than lam/(c*mu).
"""

from __future__ import annotations
from dataclasses import dataclass


def proc_slowdown(rho_c: float, rho0: float = 0.80, severity: float = 2.0) -> float:
    """Processing-time slowdown factor s(rho_c) >= 1. Flat 1.0 below the knee rho0, then linear up
    to (1 + severity) as the pool saturates (rho_c -> 1). Bounded, no pole."""
    if rho_c <= rho0:
        return 1.0
    x = min((rho_c - rho0) / (1.0 - rho0), 1.0)   # normalised excess above the knee, in (0, 1]
    return 1.0 + severity * x


@dataclass(frozen=True)
class ContentionModel:
    """The compute-pool contention as seen by BOTH the simulator (physical service time) and the
    controller (planning). `proc_s` / `prop_s` are the processing / propagation split of one attach
    in seconds. kappa=None disables contention (the Exp 1-4 model: mu_eff == the unloaded rate)."""
    kappa:    float | None
    proc_s:   float
    prop_s:   float
    rho0:     float = 0.65
    severity: float = 0.5

    @classmethod
    def from_cfg(cls, cfg) -> "ContentionModel | None":
        """Build from a SimConfig, or None when contention is off (kappa unset)."""
        if cfg.compute_kappa is None or cfg.compute_kappa <= 0:
            return None
        return cls(kappa=cfg.compute_kappa,
                   proc_s=cfg.arch.proc_total_ms / 1000.0,
                   prop_s=(cfg.arch.n_ctrl_messages * cfg.arch.oneway_delay_ms) / 1000.0,
                   rho0=cfg.compute_rho0, severity=cfg.compute_slowdown)

    def service_time(self, busy: float) -> float:
        """Mean attach service time (s) with `busy` workers active — the slowed processing plus the
        fixed propagation."""
        if self.kappa is None:
            return self.proc_s + self.prop_s
        s = proc_slowdown(busy / self.kappa, self.rho0, self.severity)
        return s * self.proc_s + self.prop_s

    def mu_eff(self, busy: float) -> float:
        """Effective per-server attach rate (UEs/s) at occupancy `busy` = 1 / service_time(busy).
        The controller passes a candidate capacity c as `busy` (all c servers busy under load), so
        mu_eff(c) captures diminishing returns: more servers raise rho_c and slow each attach."""
        return 1.0 / self.service_time(busy)
