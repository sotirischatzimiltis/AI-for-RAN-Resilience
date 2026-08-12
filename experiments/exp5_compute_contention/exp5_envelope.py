"""
Experiment 5, Part A — the compute-contention RESILIENCE ENVELOPE (opens subsection VI-E).

A pure-physics sweep, NO LLM: a fully-provisioned pool (c=16, INSTANT provisioning) is hit with a
benign step storm of growing intensity. With c and the provisioning delay both pinned, the ONLY
thing that can move the resilience cliff is the effective per-server rate mu_eff. So this isolates
the capacity envelope:

  * DEDICATED compute (contention OFF, kappa=None): each server always runs at the nominal rate mu,
    so the M/M/c pool stays stable while lambda < c*mu ~ 459 UEs/s, then P collapses.
  * SHARED compute pool (contention ON, kappa=c_max=16): once occupancy passes the knee the per-attach
    rate drops to mu_eff, so the stability boundary falls to c*mu_eff. The cliff shifts LEFT — the
    shared pool absorbs a ~30% smaller storm (a=0.5) before collapsing.

Between the two cliffs is the COLLAPSE WINDOW: a band of storm intensities the dedicated pool still
rides out (P ~ 0.9) but the shared pool has already fallen off (P ~ 0.6). That band is where Part B
(the over-limit LLM arm comparison) plants its single storm — this figure shows WHY that storm point
is chosen and that contention only bites once the load enters this window.

Deterministic and cached: the sweep is written to exp5_envelope.json; re-run the plot for free, or
pass --refresh to recompute. Run from the repo root:
    python -m experiments.exp5_compute_contention.exp5_envelope            # cached -> figure
    python -m experiments.exp5_compute_contention.exp5_envelope --refresh  # recompute the sweep
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from sim.config import SimConfig, open_ran_arch, RRCConfig, single_storm_traffic
from sim.simulator import StormSim
from sim.controllers import FixedController
from sim.contention import ContentionModel
from sim.metrics import resilience_multi
from runtime import UP

_HERE  = Path(__file__).parent
_CACHE = _HERE / "exp5_envelope.json"

_KAPPA   = 16                                  # shared pool = c_max
_RHO0    = 0.65                                # contention onset knee
_SEEDS   = range(1, 6)
_LOADS   = list(range(220, 521, 15))           # storm intensities swept (UEs/s)
# (label, kappa, severity a, colour) — one dedicated baseline + two contention severities.
_SERIES  = [("Dedicated compute (no contention)", None, 0.0, "#0072b2"),
            (r"Shared pool  $a{=}0.5$",            _KAPPA, 0.5, "#e76f51"),
            (r"Shared pool  $a{=}1.0$",            _KAPPA, 1.0, "#c1272d")]


def _P(storm: float, kappa, a: float, seed: int) -> float:
    """Episode resilience P for one benign step storm, c=16, instant provisioning, given contention."""
    cfg = SimConfig(arch=open_ran_arch(), rrc=RRCConfig(t300_ms=1000, max_attempts=5),
                    c0=16, c_max=16, traffic=single_storm_traffic(storm=storm, t_post=20),
                    seed=seed, compute_kappa=kappa, compute_rho0=_RHO0, compute_slowdown=a,
                    server_provision_delay_s=0.0)
    sim = StormSim(cfg)
    sim.run(controller=FixedController(16))
    return resilience_multi(sim.telemetry, sim.mu_single, UP, cfg.traffic.storm_windows())["P_episode"]


def _sweep() -> dict:
    """Run every (series, load, seed) and reduce to mean + 95% CI per series. Returns a cacheable dict."""
    out = {"loads": _LOADS, "seeds": list(_SEEDS), "series": []}
    for label, kappa, a, col in _SERIES:
        mean, ci = [], []
        for L in _LOADS:
            arr = np.array([_P(L, kappa, a, s) for s in _SEEDS])
            mean.append(float(arr.mean()))
            ci.append(float(1.96 * arr.std(ddof=1) / np.sqrt(len(arr))))
        out["series"].append({"label": label, "kappa": kappa, "a": a, "colour": col,
                              "mean": mean, "ci": ci})
        print(f"[env] {label:34s} P: {mean[0]:.2f} (lo storm) -> {mean[-1]:.2f} (hi storm)")
    return out


def _cliffs() -> dict[float, float]:
    """Stability boundary c*mu_eff(c=16) per series a — where each envelope falls off."""
    arch = open_ran_arch(); ps = arch.proc_total_ms / 1000; pp = (arch.n_ctrl_messages * arch.oneway_delay_ms) / 1000
    out = {0.0: 16 * arch.service_rate()}
    for a in (0.5, 1.0):
        out[a] = 16 * ContentionModel(_KAPPA, ps, pp, _RHO0, a).mu_eff(16)
    return out


def _plot(data: dict) -> None:
    # IEEE single-column: author at the FINAL physical size (~3.5in wide) with proportionate fonts,
    # so the figure is \includegraphics'd at 100% with no shrink that would crush the text.
    cliffs = _cliffs()
    # Match Exp 4's figure styling exactly (experiments/exp4_vw_tuning/make_exp4_figure.py).
    plt.rc("font", family="serif", size=8)
    plt.rc("axes", titlesize=8, labelsize=8.5, linewidth=0.8)
    plt.rc("legend", fontsize=7); plt.rc("xtick", labelsize=8); plt.rc("ytick", labelsize=8)
    plt.rc("mathtext", fontset="cm")
    fig, ax = plt.subplots(figsize=(3.4, 2.35), constrained_layout=True)

    loads = data["loads"]
    for s in data["series"]:
        m, ci = np.array(s["mean"]), np.array(s["ci"])
        lab = "No contention" if s["kappa"] is None else fr"Shared pool $a{{=}}{s['a']:.1f}$"
        ax.plot(loads, m, "-o", color=s["colour"], ms=2.4, lw=1.4, label=lab, zorder=3)
        ax.fill_between(loads, m - ci, m + ci, color=s["colour"], alpha=0.15, lw=0, zorder=1)

    for a, x in cliffs.items():
        col = {0.0: "#0072b2", 0.5: "#e76f51", 1.0: "#c1272d"}[a]
        ax.axvline(x, ls="--", color=col, lw=0.9, alpha=0.7, zorder=2)

    ax.set_xlabel(r"Storm intensity $\lambda$ (UEs/s)")
    ax.set_ylabel(r"Resilience $P$")
    ax.set_xlim(min(loads) - 5, max(loads) + 5)
    ax.set_ylim(0.5, 1.02)
    ax.grid(alpha=0.3, lw=0.4)
    ax.legend(loc="lower left", frameon=True, framealpha=0.95, handlelength=1.8,
              borderpad=0.4, labelspacing=0.3)

    for ext in ("pdf", "png"):
        out = _HERE / f"exp5_envelope.{ext}"
        fig.savefig(out, dpi=300, bbox_inches="tight")
        print(f"[env] saved -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Exp 5 Part A: compute-contention resilience envelope")
    ap.add_argument("--refresh", action="store_true", help="recompute the sweep (else use cached JSON)")
    args = ap.parse_args()

    if args.refresh or not _CACHE.exists():
        data = _sweep()
        _CACHE.write_text(json.dumps(data, indent=2))
        print(f"[env] cached -> {_CACHE}")
    else:
        data = json.loads(_CACHE.read_text())
        print(f"[env] loaded cached sweep <- {_CACHE}")
    _plot(data)
