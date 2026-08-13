"""
Experiment 6, Fold 1, Part 2 — the posture effect (deterministic, no LLM).

The Orchestrator maps an operator intent to a network posture (PRIORITY_VW in agents/orchestrator.py:
qos=(20,1), balanced=(1,1), cost=(1,20) — the full/default postures, symmetric about balanced; a
hedged intent tempers below). Here we show the posture reshapes control: the SAME benign
ramp, run under the three postures, gives three points on the resilience-cost trade-off. Part 1 shows
intent->posture; this is posture->outcome. Caches to exp6_posture.json and renders exp6_posture.pdf.
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np, matplotlib.pyplot as plt
import sys; sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from sim.config import SimConfig, open_ran_arch, RRCConfig, single_ramp_traffic
from sim.simulator import StormSim
from sim.controllers import LyapunovController
from sim.metrics import resilience_multi, benign_success_rate, avg_servers
from runtime import UP

_HERE = Path(__file__).parent
POSTURES = [("Favour QoS", "qos", 20.0, 1.0, "#0072b2"),
            ("Balanced", "balanced", 1.0, 1.0, "#6d6d6d"),
            ("Favour cost", "cost", 1.0, 20.0, "#c1272d")]
SEEDS = range(1, 6)

def _run(V, W, seed):
    cfg = SimConfig(arch=open_ran_arch(), rrc=RRCConfig(t300_ms=1000, max_attempts=5),
                    c0=2, c_max=16, traffic=single_ramp_traffic(peak=200.0, hold=90.0, t_post=60.0),
                    seed=seed, server_provision_delay_s=5.0)
    sim = StormSim(cfg); sim.run(controller=LyapunovController(V=V, W=W, util_p=UP))
    st = sim.cfg.traffic.storm_windows()
    return (resilience_multi(sim.telemetry, sim.mu_single, UP, st)["P_episode"],
            benign_success_rate(sim.stats), avg_servers(sim.telemetry))

def sweep():
    out = []
    for name, key, V, W, col in POSTURES:
        a = np.array([_run(V, W, s) for s in SEEDS])
        m = a.mean(0); ci = 1.96 * a.std(0, ddof=1) / np.sqrt(len(a))
        out.append(dict(name=name, key=key, V=V, W=W, colour=col,
                        P=m[0], P_ci=ci[0], benign=m[1], benign_ci=ci[1],
                        servers=m[2], servers_ci=ci[2]))
        print(f"{name:12s} V={V:<4.0f}W={W:<3.0f} P={m[0]:.3f}±{ci[0]:.3f} "
              f"benign={m[1]:.3f} servers={m[2]:.1f}")
    return out

def plot(data):
    # IEEE single-column (matches exp4/exp5 styling): P bars + avg servers (cost) on a twin axis.
    plt.rc("font", family="serif", size=8)
    plt.rc("axes", titlesize=8, labelsize=8.5, linewidth=0.8)
    plt.rc("legend", fontsize=7); plt.rc("xtick", labelsize=8); plt.rc("ytick", labelsize=8)
    plt.rc("mathtext", fontset="cm")
    fig, ax = plt.subplots(figsize=(3.4, 2.5), constrained_layout=True)
    x = np.arange(len(data))
    P = [d["P"] for d in data]; Pci = [d["P_ci"] for d in data]
    srv = [d["servers"] for d in data]
    ax.bar(x, P, 0.55, yerr=Pci, color=[d["colour"] for d in data], edgecolor="black", lw=0.5,
           error_kw=dict(lw=0.7, capsize=2), zorder=3)
    for xi, d in zip(x, data):                              # benign served inside each bar
        ax.text(xi, 0.06, f"{d['benign']*100:.0f}%\nserved", ha="center", va="bottom",
                fontsize=6.5, color="white")
    ax.set_ylabel(r"Resilience $P$")
    ax.set_ylim(0, 1.0)
    ax.set_xticks(x); ax.set_xticklabels([d["name"] for d in data])
    ax.grid(axis="y", alpha=0.3, lw=0.4)

    ax2 = ax.twinx()                                        # cost axis: avg online servers
    ax2.plot(x, srv, "D--", color="0.15", ms=4, lw=1.0, zorder=4)
    for xi, s in zip(x, srv):
        ax2.annotate(f"{s:.1f}", (xi, s), textcoords="offset points", xytext=(5, 3), fontsize=7,
                     color="0.15")
    ax2.set_ylabel("Avg servers (cost)", color="0.15")
    ax2.set_ylim(0, 8); ax2.tick_params(labelsize=8, colors="0.15")
    ax2.plot([], [], "D--", color="0.15", ms=4, lw=1.0, label="Avg servers")
    ax2.legend(loc="upper right", frameon=False)
    for ext in ("pdf", "png"):
        fig.savefig(_HERE / f"exp6_posture.{ext}", dpi=300, bbox_inches="tight")
    print("saved -> exp6_posture.pdf/.png")


if __name__ == "__main__":
    data = sweep()
    (_HERE / "exp6_posture.json").write_text(json.dumps(data, indent=2))
    print("cached -> exp6_posture.json")
    plot(data)
