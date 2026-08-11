"""
Plot Experiment 4 — Lyapunov (V, W) x provisioning-delay sweep.

Reads experiments/exp4_vw_tuning/vw_tuning.json (written by exp_4_V_W_tuning.py --save) and
renders, per scenario, three figures under experiments/exp4_vw_tuning/:

  1. vw_delay_lines.png  — resilience P and benign completion vs provisioning delay, one line per V
     (W=1 slice, 95% CI bands). The headline: QoS collapses as the delay grows, and V cannot
     recover it — only anticipation (provision before the surge) can.
  2. vw_heatmaps.png     — P over the V x W grid, one panel per delay: the weight sensitivity at
     each actuation lag (near-flat at high delay, strong at delay 0).
  3. vw_pareto.png       — P vs avg_servers (capacity cost), one series per delay: the
     resilience-cost frontier and how the delay shifts it.

Usage (run from this folder, next to its data + outputs):
    python plot_vw_tuning.py
"""

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")                 # headless: write PNGs, no display
import matplotlib.pyplot as plt
import numpy as np

_JSON = Path(__file__).parent / "vw_tuning.json"   # this script lives in the exp4 folder, next to its data + outputs

# pretty scenario labels for figure titles (the two onset shapes)
_SCN_LABEL = {"single_storm": "Step onset", "single_ramp": "Ramp onset"}


def _scn(name):
    return _SCN_LABEL.get(name, name)


def _load():
    if not _JSON.exists():
        sys.exit(f"No results at {_JSON} — run scripts.exp_4_V_W_tuning --save first.")
    return json.loads(_JSON.read_text())


def _cell(res, delay, scn, V, W):
    return res.get(f"delay={delay}", {}).get(scn, {}).get(f"V={V},W={W}")


def plot_delay_lines(data, out):
    """P and benign vs provisioning delay, a line per V at the W=1 slice."""
    res, delays, v_grid = data["results"], data["delays"], data["v_grid"]
    scenarios, W0 = data["scenarios"], data["w_grid"][0]
    fig, axes = plt.subplots(len(scenarios), 2, figsize=(11, 4.2 * len(scenarios)), squeeze=False)
    fig.suptitle(f"V/W tuning vs provisioning delay  (W={W0:g}, {len(data['seeds'])} seeds, 95% CI)",
                 fontweight="bold")
    cmap = plt.cm.viridis(np.linspace(0, 0.9, len(v_grid)))
    for r, scn in enumerate(scenarios):
        for col, (key, ci, label) in enumerate(
                [("P_mean", "P_ci95", "Resilience P"),
                 ("benign_mean", "benign_ci95", "Benign completion")]):
            ax = axes[r][col]
            for vi, V in enumerate(v_grid):
                ys, es = [], []
                for d in delays:
                    c = _cell(res, d, scn, V, W0)
                    ys.append(c[key] if c else np.nan)
                    es.append(c.get(ci, 0.0) if c else 0.0)
                ys, es = np.array(ys), np.array(es)
                ax.plot(delays, ys, "-o", color=cmap[vi], label=f"V={V:g}")
                ax.fill_between(delays, ys - es, ys + es, color=cmap[vi], alpha=0.15)
            ax.set_title(f"{_scn(scn)} — {label}")
            ax.set_xlabel("provisioning delay (s)")
            ax.set_ylabel(label)
            ax.grid(alpha=0.3)
            ax.set_ylim(0, 1.05)
            if col == 1 and r == 0:
                ax.legend(title="utility weight", fontsize=8)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out, dpi=130)
    print(f"saved -> {out}")


def plot_heatmaps(data, out):
    """P over the V x W grid, one column per delay, one row per scenario."""
    res, delays = data["results"], data["delays"]
    v_grid, w_grid, scenarios = data["v_grid"], data["w_grid"], data["scenarios"]
    fig, axes = plt.subplots(len(scenarios), len(delays),
                             figsize=(3.2 * len(delays), 3.2 * len(scenarios)), squeeze=False)
    fig.suptitle("Resilience P over the V×W grid, per provisioning delay", fontweight="bold")
    for r, scn in enumerate(scenarios):
        for c, d in enumerate(delays):
            ax = axes[r][c]
            M = np.array([[(_cell(res, d, scn, V, W) or {}).get("P_mean", np.nan)
                           for W in w_grid] for V in v_grid])
            im = ax.imshow(M, origin="lower", aspect="auto", cmap="magma", vmin=0.5, vmax=1.0)
            ax.set_title(f"{_scn(scn)}\ndelay={d:g}s", fontsize=9)
            ax.set_xticks(range(len(w_grid))); ax.set_xticklabels([f"{w:g}" for w in w_grid], fontsize=7)
            ax.set_yticks(range(len(v_grid))); ax.set_yticklabels([f"{v:g}" for v in v_grid], fontsize=7)
            if c == 0:
                ax.set_ylabel("V (utility)")
            if r == len(scenarios) - 1:
                ax.set_xlabel("W (cost)")
            for i in range(len(v_grid)):        # annotate each cell
                for j in range(len(w_grid)):
                    if not np.isnan(M[i, j]):
                        ax.text(j, i, f"{M[i, j]:.2f}", ha="center", va="center",
                                color="white" if M[i, j] < 0.8 else "black", fontsize=6)
    fig.colorbar(im, ax=axes, shrink=0.6, label="P")
    fig.savefig(out, dpi=130)
    print(f"saved -> {out}")


def plot_pareto(data, out):
    """P vs avg_servers (capacity cost); one series per delay, all V,W points."""
    res, delays, scenarios = data["results"], data["delays"], data["scenarios"]
    v_grid, w_grid = data["v_grid"], data["w_grid"]
    fig, axes = plt.subplots(1, len(scenarios), figsize=(6 * len(scenarios), 5), squeeze=False)
    fig.suptitle("Resilience–cost trade-off (each point a V,W setting)", fontweight="bold")
    cmap = plt.cm.plasma(np.linspace(0, 0.85, len(delays)))
    for s, scn in enumerate(scenarios):
        ax = axes[0][s]
        for di, d in enumerate(delays):
            xs, ys = [], []
            for V in v_grid:
                for W in w_grid:
                    c = _cell(res, d, scn, V, W)
                    if c:
                        xs.append(c["avg_servers_mean"]); ys.append(c["P_mean"])
            ax.scatter(xs, ys, color=cmap[di], s=30, label=f"delay={d:g}s", alpha=0.8)
        ax.set_title(_scn(scn)); ax.set_xlabel("avg_servers (capacity cost)"); ax.set_ylabel("Resilience P")
        ax.grid(alpha=0.3); ax.legend(fontsize=8)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(out, dpi=130)
    print(f"saved -> {out}")


def plot_modes(data, out):
    """Serial vs parallel provisioning: P and benign vs V, one line per mode, at the
    realistic delay slice. From vw_modes.json (results keyed mode -> delay -> scenario)."""
    res, modes, delays = data["results"], data["modes"], data["delays"]
    v_grid, scenarios, W0 = data["v_grid"], data["scenarios"], data["w_grid"][0]
    d0 = 5.0 if 5.0 in delays else delays[0]          # realistic-delay slice
    colors = {"serial": "#e76f51", "parallel": "#2a9d8f"}
    fig, axes = plt.subplots(len(scenarios), 2, figsize=(11, 4.2 * len(scenarios)), squeeze=False)
    fig.suptitle(f"Serial vs parallel provisioning  (delay={d0:g}s, W={W0:g}, "
                 f"{len(data['seeds'])} seeds, 95% CI)", fontweight="bold")
    for r, scn in enumerate(scenarios):
        for col, (key, ci, label) in enumerate(
                [("P_mean", "P_ci95", "Resilience P"),
                 ("benign_mean", "benign_ci95", "Benign completion")]):
            ax = axes[r][col]
            for mode in modes:
                cells = res[f"mode={mode}"][f"delay={d0}"][scn]
                ys = np.array([cells.get(f"V={v},W={W0}", {}).get(key, np.nan) for v in v_grid])
                es = np.array([cells.get(f"V={v},W={W0}", {}).get(ci, 0.0) for v in v_grid])
                ax.plot(v_grid, ys, "-o", color=colors.get(mode), label=mode)
                ax.fill_between(v_grid, ys - es, ys + es, color=colors.get(mode), alpha=0.15)
            ax.set_title(f"{_scn(scn)} — {label}")
            ax.set_xlabel("utility weight V"); ax.set_ylabel(label)
            ax.set_xscale("log"); ax.grid(alpha=0.3); ax.set_ylim(0, 1.05)
            if col == 1 and r == 0:
                ax.legend(title="provisioning")
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out, dpi=130)
    print(f"saved -> {out}")


def main():
    d = _JSON.parent
    if _JSON.exists():                                    # the V/W x delay sweep
        data = json.loads(_JSON.read_text())
        plot_delay_lines(data, d / "vw_delay_lines.png")
        plot_heatmaps(data, d / "vw_heatmaps.png")
        plot_pareto(data, d / "vw_pareto.png")
    modes_path = d / "vw_modes.json"
    if modes_path.exists():                               # the serial-vs-parallel comparison
        plot_modes(json.loads(modes_path.read_text()), d / "vw_provisioning_modes.png")
    if not _JSON.exists() and not modes_path.exists():
        sys.exit(f"No results in {d} — run scripts.exp_4_V_W_tuning --save first.")


if __name__ == "__main__":
    main()
