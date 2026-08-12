"""Exp 5 candidate B: resilience envelope vs storm intensity, family over provisioned servers c.
Each c gives a stability cliff at c*mu (OFF) / c*mu_eff(c) (ON). Low c runs the pool cool -> no
contention; high c runs it hot -> the cliff shifts left and the ON cliffs SATURATE (adding servers
past the knee barely extends the envelope). Deterministic, no LLM."""
import numpy as np, matplotlib.pyplot as plt
import sys; sys.path.insert(0, ".")
from sim.config import SimConfig, open_ran_arch, RRCConfig, single_storm_traffic
from sim.simulator import StormSim
from sim.controllers import FixedController
from sim.metrics import resilience_multi
from runtime import UP

SEEDS = range(1, 4)
LOADS = list(range(180, 521, 15))
def P_at(storm, c, kappa, seed):
    cfg = SimConfig(arch=open_ran_arch(), rrc=RRCConfig(t300_ms=1000, max_attempts=5),
                    c0=c, c_max=16, traffic=single_storm_traffic(storm=storm, t_post=20),
                    seed=seed, compute_kappa=kappa, compute_rho0=0.65, compute_slowdown=0.5,
                    server_provision_delay_s=0.0)
    sim = StormSim(cfg); sim.run(controller=FixedController(c))
    st = sim.cfg.traffic.storm_windows()
    return resilience_multi(sim.telemetry, sim.mu_single, UP, st)["P_episode"]

def curve(c, kappa):
    return np.array([np.mean([P_at(L, c, kappa, s) for s in SEEDS]) for L in LOADS])

plt.rc("font", family="serif", size=10)
fig, ax = plt.subplots(figsize=(5.6, 3.6), constrained_layout=True)
C_VALS = [8, 12, 16]
cmap = {8:"#2a9d8f", 12:"#e9a100", 16:"#c1272d"}
for c in C_VALS:
    ax.plot(LOADS, curve(c, None), "-",  color=cmap[c], lw=1.9, label=f"$c={c}$ dedicated")
    ax.plot(LOADS, curve(c, 16),  "--", color=cmap[c], lw=1.9, label=f"$c={c}$ shared pool")
ax.set_xlabel("Storm intensity (UEs/s)"); ax.set_ylabel("Resilience $P$")
ax.set_title("Resilience envelope vs provisioning, dedicated (solid) vs shared (dashed)")
ax.grid(alpha=0.3); ax.set_ylim(0.5, 1.02)
ax.legend(ncol=3, fontsize=7.5, loc="lower left")
out = "/private/tmp/claude-501/-Users-admin-Desktop-Mac-AI-for-RAN-Resilience-Paper-System/790f4069-42bf-4f83-ad51-d4a4feead05d/scratchpad/exp5_envelope_c.png"
fig.savefig(out, dpi=150, bbox_inches="tight"); print("saved", out)
