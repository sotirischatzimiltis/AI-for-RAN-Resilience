"""
Plot the model bake-off (Experiment 1) results.

Reads results/model_comparison.json (written by experiment_model_comparison.py
--save) and renders a 4-panel figure to results/model_comparison.png:

  (a) Resilience P per model, grouped by scenario, with ±std error bars.
  (b) Security per model on the botnet scenario: benign-served + botnet-blocked.
  (c) Cost per model: $ / episode (and mean assessment latency as a label).
  (d) Quality-vs-cost: P vs $/episode — the deployability trade-off.

Run AFTER a sweep:
    python -m scripts.plot_model_comparison
    python -m scripts.plot_model_comparison --json results/model_comparison.json
"""

import argparse
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")           # headless: write a PNG, no display needed
import matplotlib.pyplot as plt
import numpy as np

_DEFAULT_JSON = Path(__file__).parent.parent / "results" / "model_comparison.json"
# scenario used for the security panel (the only one with a botnet)
_SEC_SCENARIO = "multi_storm_flat"


def _label(run_id: str, d: dict) -> str:
    """Short model label; distinguish the reasoning variants when present."""
    slug = d["slug"].split("/")[-1]
    return f"{slug}\n(rsn {d['reasoning']})" if "::reasoning=" in run_id else slug


def main(args) -> None:
    path = Path(args.json)
    if not path.exists():
        sys.exit(f"No results at {path} — run a sweep with --save first.")
    data = json.loads(path.read_text())
    models, scenarios, seeds = data["models"], data["scenarios"], data["seeds"]

    items = list(models.items())                 # [(run_id, d), ...] preserves sweep order
    labels = [_label(rid, d) for rid, d in items]
    x = np.arange(len(items))

    fig, axes = plt.subplots(2, 2, figsize=(15, 10))
    fig.suptitle(f"Model bake-off — {len(seeds)} seeds, bare judge  "
                 f"(mean ± std)", fontsize=14, fontweight="bold")

    # (a) Resilience P, grouped by scenario -------------------------------------
    ax = axes[0, 0]
    w = 0.8 / max(1, len(scenarios))
    for si, scn in enumerate(scenarios):
        P   = [d["scenarios"].get(scn, {}).get("P_mean", np.nan)  for _, d in items]
        err = [d["scenarios"].get(scn, {}).get("P_std", 0.0)      for _, d in items]
        ax.bar(x + si * w, P, w, yerr=err, capsize=3, label=scn)
    ax.set_title("(a) Resilience P")
    ax.set_ylabel("P (episode)")
    ax.set_xticks(x + w * (len(scenarios) - 1) / 2)
    ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
    ax.legend(fontsize=8); ax.grid(axis="y", alpha=0.3); ax.set_ylim(0, 1.05)

    # (b) Security on the botnet scenario ---------------------------------------
    ax = axes[0, 1]
    sec = _SEC_SCENARIO if _SEC_SCENARIO in scenarios else scenarios[-1]
    benign   = [d["scenarios"].get(sec, {}).get("benign_mean", np.nan)  for _, d in items]
    blocked  = [d["scenarios"].get(sec, {}).get("blocked_mean", np.nan) for _, d in items]
    blk_err  = [d["scenarios"].get(sec, {}).get("blocked_std", 0.0)     for _, d in items]
    ax.bar(x - 0.2, benign,  0.4, label="benign served", color="#2a9d8f")
    ax.bar(x + 0.2, blocked, 0.4, yerr=blk_err, capsize=3, label="botnet blocked", color="#e76f51")
    ax.set_title(f"(b) Security — {sec}")
    ax.set_ylabel("rate")
    ax.set_xticks(x); ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
    ax.legend(fontsize=8); ax.grid(axis="y", alpha=0.3); ax.set_ylim(0, 1.05)

    # (c) Cost: $ / episode (averaged over scenarios) ---------------------------
    ax = axes[1, 0]
    usd = [np.mean([s["usd_per_episode"] for s in d["scenarios"].values()]) for _, d in items]
    lat = [np.mean([s["mean_latency_s"]  for s in d["scenarios"].values()]) for _, d in items]
    bars = ax.bar(x, usd, 0.6, color="#264653")
    for xi, (u, l) in enumerate(zip(usd, lat)):
        ax.text(xi, u, f"{l:.0f}s", ha="center", va="bottom", fontsize=7)
    ax.set_title("(c) Cost — $/episode  (label = mean assessment latency)")
    ax.set_ylabel("USD / episode")
    ax.set_xticks(x); ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
    ax.grid(axis="y", alpha=0.3)

    # (d) Quality vs cost: P (mean over scenarios) vs $/episode -----------------
    ax = axes[1, 1]
    Pall = [np.mean([s["P_mean"] for s in d["scenarios"].values()]) for _, d in items]
    ax.scatter(usd, Pall, s=60, zorder=3)
    for xi, lab in enumerate(labels):
        ax.annotate(lab.replace("\n", " "), (usd[xi], Pall[xi]),
                    fontsize=7, xytext=(5, 3), textcoords="offset points")
    ax.set_title("(d) Quality vs cost  (top-left = best value)")
    ax.set_xlabel("USD / episode"); ax.set_ylabel("P (mean over scenarios)")
    ax.grid(alpha=0.3)

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    out = path.with_suffix(".png")
    fig.savefig(out, dpi=130)
    print(f"saved -> {out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Plot the model bake-off results")
    p.add_argument("--json", default=str(_DEFAULT_JSON), help="path to model_comparison.json")
    main(p.parse_args())
