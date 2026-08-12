"""Visualise the shared-compute contention model (sim/contention.py): the slowdown factor and the
resulting pool throughput, across severities a, at kappa=c_max=16, rho0=0.8. Helps calibrate a."""
import numpy as np, matplotlib.pyplot as plt
from pathlib import Path
import sys; sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from sim.contention import ContentionModel, proc_slowdown
from sim.config import open_ran_arch

arch = open_ran_arch(); ps = arch.proc_total_ms/1000; pp = (arch.n_ctrl_messages*arch.oneway_delay_ms)/1000
mu0 = arch.service_rate(); KAPPA, RHO0 = 16, 0.65
ARMS = [(10, "reactive\n~10"), (13, "reserve\n13"), (16, "static\n16")]   # where the arms provision
A_VALS = [0.3, 0.5, 0.7, 2.0]
COL = {0.3:"#2a9d8f", 0.5:"#0072b2", 0.7:"#e76f51", 2.0:"#c1272d"}

plt.rc("font", family="serif", size=9)
fig, ax = plt.subplots(1, 3, figsize=(11, 3.4), constrained_layout=True)

# (1) slowdown s vs rho_c
r = np.linspace(0, 1, 200)
for a in A_VALS:
    ax[0].plot(r, [proc_slowdown(x, RHO0, a) for x in r], color=COL[a], lw=1.8, label=f"a={a}")
ax[0].axvline(RHO0, ls=":", color="0.5"); ax[0].text(RHO0-0.02, 2.6, f"knee {RHO0:g}", ha="right", fontsize=8, color="0.4")
ax[0].set_xlabel(r"compute occupancy  $\rho_c$ = busy/16"); ax[0].set_ylabel("processing slowdown  s")
ax[0].set_title("(a) slowdown factor"); ax[0].legend(fontsize=8); ax[0].grid(alpha=0.3)

# (2) total pool throughput c*mu_eff(c) vs c, with the blind (nominal) expectation
c = np.arange(1, 17)
ax[1].plot(c, c*mu0, "k--", lw=1.4, label="blind expects (c·μ)")
for a in A_VALS:
    m = ContentionModel(KAPPA, ps, pp, RHO0, a)
    ax[1].plot(c, [ci*m.mu_eff(ci) for ci in c], color=COL[a], lw=1.8, marker="o", ms=3, label=f"a={a}")
ax[1].axvline(RHO0*KAPPA, ls=":", color="0.5"); ax[1].text(RHO0*KAPPA-0.2, 40, f"knee ({RHO0*KAPPA:.1f})", ha="right", fontsize=8, color="0.4")
for cc, lbl in ARMS:                        # mark where each controller provisions
    ax[1].axvline(cc, ls="-", color="0.7", lw=0.8, alpha=0.6)
    ax[1].text(cc, 470, lbl, ha="center", va="top", fontsize=7, color="0.35")
ax[1].set_xlabel("servers c"); ax[1].set_ylabel("total throughput  c·μ_eff  (UEs/s)")
ax[1].set_title("(b) pool throughput vs servers"); ax[1].legend(fontsize=7.5); ax[1].grid(alpha=0.3)

# (3) effective per-server rate mu_eff vs c
for a in A_VALS:
    m = ContentionModel(KAPPA, ps, pp, RHO0, a)
    ax[2].plot(c, [m.mu_eff(ci) for ci in c], color=COL[a], lw=1.8, marker="o", ms=3, label=f"a={a}")
ax[2].axhline(mu0, ls="--", color="k", lw=1.0); ax[2].text(1.2, mu0+0.4, f"unloaded μ={mu0:.1f}", fontsize=8)
ax[2].axvline(RHO0*KAPPA, ls=":", color="0.5")
ax[2].set_xlabel("servers c"); ax[2].set_ylabel("per-server rate  μ_eff  (UEs/s)")
ax[2].set_title("(c) effective per-server rate"); ax[2].legend(fontsize=8); ax[2].grid(alpha=0.3)

fig.suptitle(f"Shared-compute contention model  (kappa = c_max = 16, knee rho0 = {RHO0:g})", fontweight="bold")
out = Path(__file__).parent / "contention_model_curves.png"
fig.savefig(out, dpi=150, bbox_inches="tight"); print("saved ->", out)
