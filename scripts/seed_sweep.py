"""
Seed sweep — a STABLE resilience-P baseline for the control policy.

Live agentic runs make P noisy: the malicious filter engages a few sim-seconds
late (LLM-gated) and the exact lag shifts run-to-run with model latency. To tune
anything we first need a reference that is free of that noise.

This runs the DETERMINISTIC control policies (no realtime pacing, no LLM, no MCP)
across many seeds and both scenarios, and reports P as mean ± std. The fast
control loop's capacity choice is exactly lyapunov_optimal_c, so the
'Lyapunov V=1000' row is the agentic system's control-side baseline; 'Fixed'
rows bracket it. Compare live agent runs against these numbers, not against each
other.

Usage:
    python -m scripts.seed_sweep [--seeds 10] [--scenario single_storm|multi_storm|both]
"""

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sim.config import (
    SimConfig, open_ran_arch, RRCConfig,
    single_storm_traffic, multi_storm_traffic,
)
from sim.simulator import StormSim
from sim.controllers import FixedController, LyapunovController, ForecastLyapunov
from sim.metrics import UtilityParams, resilience_score, success_rate

LQMAX = 1500.0
UP    = UtilityParams(lq_max=LQMAX, kB=0.004)

# scenario -> (traffic factory, storm start t0, storm end td)
SCENARIOS = {
    "single_storm": (single_storm_traffic, 50.0, 110.0),
    "multi_storm":  (multi_storm_traffic,  60.0, 120.0),
}

# label -> (controller factory, initial server count c0)
CONTROLLERS = {
    "Fixed c=2":       (lambda: FixedController(2), 2),
    "Fixed c=8":       (lambda: FixedController(8), 8),
    "Lyapunov V=1000": (lambda: LyapunovController(V=1000, W=1), 1),
    "Lyap+Forecast":   (lambda: ForecastLyapunov(V=1000, W=1, horizon_s=8), 1),
}


def run_one(controller_factory, c0: int, scenario: str, seed: int) -> tuple[float, int, float]:
    traffic_fn, t0, td = SCENARIOS[scenario]
    cfg = SimConfig(
        arch=open_ran_arch(),
        rrc=RRCConfig(t300_ms=1000, max_attempts=5),
        c0=c0, c_max=16, lq_max=LQMAX,
        traffic=traffic_fn(), seed=seed,
    )
    sim = StormSim(cfg)
    sim.run(controller=controller_factory())
    r = resilience_score(sim.telemetry, sim.mu_single, UP, t0=t0, td=td)
    succ = success_rate(sim.stats.completed, sim.stats.failed)
    return r["P"], sim.stats.failed, succ


def sweep(scenario: str, seeds: list[int]) -> None:
    print(f"\n=== {scenario}  ({len(seeds)} seeds: {seeds[0]}..{seeds[-1]}) ===")
    print(f"{'controller':18s}  {'P mean':>7s} {'± std':>7s}   "
          f"{'succ%':>6s}   {'fails(mean)':>11s}   note")
    for label, (factory, c0) in CONTROLLERS.items():
        ps, fails, succs = [], [], []
        for s in seeds:
            p, f, sr = run_one(factory, c0, scenario, s)
            ps.append(p)
            fails.append(f)
            succs.append(sr)
        mean = statistics.mean(ps)
        sd   = statistics.pstdev(ps) if len(ps) > 1 else 0.0
        succ = statistics.mean(succs)
        # flag the P-looks-great-but-drops-users case
        note = "P MISLEADING (low admission)" if (mean >= 0.9 and succ < 0.9) else ""
        print(f"{label:18s}  {mean:7.3f} {sd:7.3f}   "
              f"{succ*100:5.1f}%   {statistics.mean(fails):11.0f}   {note}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Deterministic seed sweep for a stable P baseline")
    ap.add_argument("--seeds", type=int, default=10, help="number of seeds (1..N)")
    ap.add_argument("--scenario", default="both",
                    choices=["single_storm", "multi_storm", "both"])
    args = ap.parse_args()

    seeds = list(range(1, args.seeds + 1))
    scenarios = list(SCENARIOS) if args.scenario == "both" else [args.scenario]

    print("Deterministic resilience-P baseline (no LLM, no realtime)")
    for sc in scenarios:
        sweep(sc, seeds)
    print("\nCompare live agent runs against these mean±std, not against single runs.")


if __name__ == "__main__":
    main()
