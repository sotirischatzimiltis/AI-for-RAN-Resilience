"""
Experiment 4 — Lyapunov weight (V, W) calibration / capacity-cost trade-off.

Sweeps the Near-RT controller's utility weight V and server-cost weight W over a 2-D grid,
with the Non-RT LLM judge REMOVED: the weights are held FIXED at each grid point and only the
deterministic fast loop + simulator run. This isolates the controller (capacity is a controller
property, not the judge's — see Exp 1), and makes the sweep free (no API), fast, and fully
reproducible. The weights enter through lyapunov_optimal_c(...): higher V favours QoS -> more
servers -> more headroom (higher P) at higher capacity cost; higher W penalises servers.

This is a PURE CAPACITY study — no judge, no admission filter, no anticipation. Benign-vs-malicious
is irrelevant here; what matters is the LOAD SHAPE, the weights (V, W), and the provisioning delay.
Two benign single-surge scenarios that differ ONLY in onset shape:
  single_storm  — STEP onset: benign load jumps 20 -> 200 UEs/s instantly, then recovers.
  single_ramp   — RAMP onset: benign load climbs 20 -> 200 over 30 s (a staircase), holds, recovers.
Same peak and same elevated span, so the comparison isolates the load's RATE OF CHANGE.

Per (V, W): resilience P, benign completion, and avg_servers (the capacity-cost proxy). Each
reported as mean + 95% CI (Student-t) over seeds. The output is the resilience-cost Pareto the
operator can later slide along.

A third axis is the server provisioning delay (actuation lag): under a STEP the reactive controller
cannot bring capacity online in time, so at a realistic 5 s delay V/W tuning barely helps — the
delay, not the weights, dominates resilience. Under a RAMP the same controller can track the rising
load, so the delay penalty is far milder. The sweep therefore also varies the delay (0/2/5/10 s).

Usage:
    python -m scripts.exp_4_V_W_tuning --seeds 5 --save          # full V x W x delay{0,2,5,10} sweep
    python -m scripts.exp_4_V_W_tuning --v-grid 1 5 20 --w-grid 1 --provision-delay 0 5
"""

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))   # repo root on the import path

from agents.near_rt_control_loop import run_control_loop      # the deterministic 1 Hz fast loop
from shared.policy import SharedPolicy, RunStats              # weights live here; loop reads them
from runtime import host as sim_host, UP                       # sim host + utility params
from sim.metrics import (resilience_multi, benign_success_rate,
                         malicious_blocked_rate, avg_servers)   # ground-truth scoring

# Default grids. V = utility/QoS weight, W = server-cost weight (both O(1), nominal 1; the
# controller's objective is normalised so V,W actually bind — useful range ~1..20).
V_GRID = [1.0, 2.0, 5.0, 10.0, 20.0]
W_GRID = [1.0, 2.0, 5.0, 10.0, 20.0]

# The two benign single-surge scenarios, keyed to their t_post override (short recovery keeps runs
# fast). They differ ONLY in onset shape (step vs ramp); value = post-surge calm duration (s).
_SCENARIOS = {
    "single_storm": 20.0,   # STEP onset
    "single_ramp":  20.0,   # RAMP onset
}
# No scenario here carries a botnet: this is a pure capacity study, so the admission filter stays OFF.
_BOTNET_SCENARIOS: set[str] = set()

_EXP_DIR  = Path(__file__).parent.parent / "experiments" / "exp4_vw_tuning"
_LOGS_DIR = _EXP_DIR / "logs"

# Student-t 97.5th percentile by SAMPLE SIZE n (df=n-1); 1.96 fallback for large n. Same as Exp 1.
_T95 = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571,
        7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262}


def _agg(vals):
    """(mean, sample-std, 95% CI half-width) for a per-seed metric (Student-t, small n)."""
    n = len(vals)
    mean = statistics.mean(vals)
    if n < 2:
        return mean, 0.0, 0.0
    sd = statistics.stdev(vals)
    return mean, sd, _T95.get(n, 1.96) * sd / math.sqrt(n)


class _Tee:
    """Mirror writes to several streams (terminal + log file) — the --log capture."""
    def __init__(self, *streams):
        self._streams = streams
    def write(self, data):
        for s in self._streams:
            s.write(data)
        return len(data)
    def flush(self):
        for s in self._streams:
            s.flush()
    def isatty(self):
        return self._streams[0].isatty()
    def __getattr__(self, name):
        return getattr(self._streams[0], name)


def _prevent_sleep():
    """macOS: hold a power assertion for the life of this process so a long sweep isn't
    idle-slept. `caffeinate -w <pid>` self-exits with us. No-op elsewhere."""
    if sys.platform != "darwin":
        return
    import os
    import subprocess
    try:
        subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())])
        print("[vw] caffeinate held — system won't idle-sleep during this run")
    except (FileNotFoundError, OSError):
        pass


async def _fixed_weights_run(V, W, scenario, seed, provision_delay, parallel, args) -> dict:
    """One episode at FIXED (V, W), provisioning delay, and provisioning MODE, no LLM: the
    deterministic fast loop over the sim.

    The weights are set on the SharedPolicy and never change (no judge, no tighten). For a
    botnet scenario the filter is pinned ON so capacity provisioning is the only moving part;
    for the benign scenario the filter stays OFF (a pure capacity study). `parallel` selects
    serial (one server per delay) vs parallel (all pending servers up after one delay).
    """
    policy = SharedPolicy(lyapunov_V=float(V), lyapunov_W=float(W))   # fixed weights, read every tick
    if scenario in _BOTNET_SCENARIOS:
        policy.update(storm_active=True, malicious_drop_prob=1.0)     # pin the filter (levers only; V/W untouched)
    stats = RunStats()

    # bare capacity config: no anticipation tools, no learning (they don't apply without the judge)
    sim_host.calendar = []
    sim_host.forecast_enabled = False
    sim_host.calendar_enabled = False
    sim_host.start(scenario=scenario, seed=seed, c_max=16,
                   rt_factor=args.rt_factor, t_post=_SCENARIOS[scenario],
                   provision_delay=provision_delay,   # swept: 0=instant .. 10s; the actuation-lag knob
                   provision_parallel=parallel)       # serial vs parallel spin-up

    stop_event = asyncio.Event()
    async def _watch():
        while not sim_host.is_done:
            await asyncio.sleep(0.05)
        stop_event.set()

    # poll = 1/rt_factor keeps the loop at ~1 sim-second per tick (same cadence as real time)
    poll = max(0.02, 1.0 / args.rt_factor)
    await asyncio.gather(
        _watch(),
        run_control_loop(policy, stop_event, poll, stats, memory=None),
    )

    sim = sim_host.sim
    final_P = 0.0
    try:
        storms = sim.cfg.traffic.storm_windows()
        final_P = resilience_multi(sim.telemetry, sim.mu_single, UP, storms)["P_episode"]
    except Exception:
        pass
    st = sim.stats
    return {
        "final_P":                round(final_P, 4),
        "benign_success_rate":    round(benign_success_rate(st), 4),
        "malicious_blocked_rate": round(malicious_blocked_rate(st), 4),
        "avg_servers":            round(avg_servers(sim.telemetry), 3),   # capacity-cost proxy
    }


async def _run_grid(v_grid, w_grid, delays, scenarios, seeds, parallel, args) -> dict:
    """Full delay×scenario×V×W grid for ONE provisioning mode. Returns delay->scenario->cell."""
    out: dict[str, dict] = {}
    for delay in delays:
        for scenario in scenarios:
            per_cell: dict[str, dict] = {}
            for V in v_grid:
                for W in w_grid:
                    P, ben, blk, srv = [], [], [], []
                    for seed in seeds:
                        try:
                            r = await _fixed_weights_run(V, W, scenario, seed, delay, parallel, args)
                        except Exception as e:
                            print(f"[vw] d={delay} V={V} W={W} {scenario} seed={seed} ERROR "
                                  f"{type(e).__name__}: {e}")
                            continue
                        P.append(r["final_P"]); ben.append(r["benign_success_rate"])
                        blk.append(r["malicious_blocked_rate"]); srv.append(r["avg_servers"])
                    if not P:
                        continue
                    P_m, P_sd, P_ci = _agg(P)
                    b_m, b_sd, b_ci = _agg(ben)
                    s_m, s_sd, s_ci = _agg(srv)
                    per_cell[f"V={V},W={W}"] = {
                        "V": V, "W": W,
                        "P_mean": P_m,   "P_std": P_sd,   "P_ci95": P_ci,   "P_seeds": P,
                        "benign_mean": b_m, "benign_std": b_sd, "benign_ci95": b_ci, "benign_seeds": ben,
                        "blocked_mean": statistics.mean(blk),
                        "avg_servers_mean": s_m, "avg_servers_std": s_sd,
                        "avg_servers_ci95": s_ci, "avg_servers_seeds": srv,
                    }
                    print(f"[vw] {'par' if parallel else 'ser'} d={delay:4.1f}s {scenario:16s} "
                          f"V={V:5.1f} W={W:5.1f}  P={P_m:.3f}  benign={b_m:.3f}  "
                          f"blocked={statistics.mean(blk):.3f}  avg_servers={s_m:.2f}")
            out.setdefault(f"delay={delay}", {})[scenario] = per_cell
    return out


async def sweep(args) -> None:
    seeds = list(range(1, args.seeds + 1))
    scenarios = list(_SCENARIOS) if "both" in args.scenario else args.scenario
    v_grid = args.v_grid if args.v_grid else V_GRID
    w_grid = args.w_grid if args.w_grid else W_GRID
    delays = args.provision_delay                       # list: swept as the actuation-lag axis
    modes  = ["serial", "parallel"] if args.provision_mode == "both" else [args.provision_mode]
    both   = len(modes) > 1
    t_start = time.monotonic()

    total = len(modes) * len(delays) * len(v_grid) * len(w_grid) * len(scenarios) * len(seeds)
    print(f"[vw] modes={modes}, delays={delays}s, grid V={v_grid} x W={w_grid}, "
          f"scenarios={scenarios}, seeds={len(seeds)}  -> {total} runs (no API cost)\n")

    # results: with a single mode -> delay->scenario->cell (backward compatible with vw_tuning.json);
    # with both modes -> "mode=<m>" -> delay->scenario->cell (the serial-vs-parallel comparison).
    results: dict[str, dict] = {}
    for mode in modes:
        grid = await _run_grid(v_grid, w_grid, delays, scenarios, seeds, mode == "parallel", args)
        if both:
            results[f"mode={mode}"] = grid
        else:
            results = grid

    elapsed = time.monotonic() - t_start
    if both:
        for mode in modes:
            print(f"\n############  PROVISIONING: {mode.upper()}  ############")
            _print_scorecard(results[f"mode={mode}"], delays, scenarios, v_grid, w_grid, seeds, 0.0)
        print(f"\n  wall time: {elapsed:.0f}s")
    else:
        _print_scorecard(results, delays, scenarios, v_grid, w_grid, seeds, elapsed)

    if args.save:
        # both modes -> vw_modes.json (mode comparison); single mode -> vw_tuning.json (delay sweep)
        out = _EXP_DIR / ("vw_modes.json" if both else "vw_tuning.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {"seeds": seeds, "v_grid": v_grid, "w_grid": w_grid,
                   "delays": delays, "scenarios": scenarios, "results": results}
        if both:
            payload["modes"] = modes
        out.write_text(json.dumps(payload, indent=2))
        print(f"\n  saved -> {out}")


def _pareto(cells: dict) -> set:
    """Return the keys on the resilience-cost Pareto frontier: no other cell has both
    higher P AND lower avg_servers (we want MORE P for FEWER servers)."""
    items = [(k, v["P_mean"], v["avg_servers_mean"]) for k, v in cells.items()]
    front = set()
    for k, P, srv in items:
        dominated = any(P2 >= P and srv2 <= srv and (P2 > P or srv2 < srv)
                        for _, P2, srv2 in items)
        if not dominated:
            front.add(k)
    return front


def _print_scorecard(results, delays, scenarios, v_grid, w_grid, seeds, elapsed) -> None:
    print("\n" + "=" * 78)
    print(f"V/W TUNING SCORECARD  ({len(seeds)} seeds, no LLM)")
    print("=" * 78)
    for delay in delays:
        dkey = f"delay={delay}"
        print(f"\n############  provision_delay = {delay}s  ############")
        for scenario in scenarios:
            _print_one(results.get(dkey, {}).get(scenario, {}), scenario, v_grid, w_grid)
    print(f"\n  wall time: {elapsed:.0f}s")
    print("=" * 78)
    print("  Pick a default (V,W): a knee on the Pareto front — most P per server. The delay")
    print("  axis shows whether V/W can recover QoS lost to actuation lag (spoiler: only anticipation can).")


def _print_one(cells, scenario, v_grid, w_grid) -> None:
    """P grid + avg_servers grid + Pareto frontier for one (delay, scenario) block."""
    if not cells:
        return
    front = _pareto(cells)
    print(f"\n  --- {scenario} ---   (P grid, rows=V  cols=W;  * = Pareto-optimal P/cost)")
    print("   V\\W " + "".join(f"{w:>9.1f}" for w in w_grid))
    for V in v_grid:
        row = f"  {V:5.1f}"
        for W in w_grid:
            c = cells.get(f"V={V},W={W}")
            if c is None:
                row += f"{'--':>9}"
            else:
                mark = "*" if f"V={V},W={W}" in front else " "
                row += f"{c['P_mean']:>8.3f}{mark}"
        print(row)
    print(f"  avg_servers (capacity cost) grid:")
    print("   V\\W " + "".join(f"{w:>9.1f}" for w in w_grid))
    for V in v_grid:
        row = f"  {V:5.1f}"
        for W in w_grid:
            c = cells.get(f"V={V},W={W}")
            row += f"{c['avg_servers_mean']:>9.2f}" if c else f"{'--':>9}"
        print(row)
    pts = sorted((cells[k] for k in front), key=lambda c: c["P_mean"], reverse=True)
    print("  Pareto (P vs avg_servers): " + "  ".join(
        f"[V={c['V']:.0f},W={c['W']:.0f} P={c['P_mean']:.3f} srv={c['avg_servers_mean']:.1f}]" for c in pts))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Lyapunov V/W tuning sweep (no LLM)")
    p.add_argument("--scenario", nargs="*", default=["both"],
                   choices=["both", "single_storm", "single_ramp"],
                   help="onset shapes to run (default: both = step + ramp). "
                        "single_storm = STEP, single_ramp = RAMP")
    p.add_argument("--seeds", type=int, default=5)
    p.add_argument("--provision-mode", default="serial", dest="provision_mode",
                   choices=["serial", "parallel", "both"],
                   help="server spin-up: serial (one per delay, default), parallel (all at once), "
                        "or both (serial-vs-parallel comparison -> vw_modes.json)")
    p.add_argument("--v-grid", nargs="*", type=float, default=None, dest="v_grid",
                   help=f"V values to sweep (default {V_GRID})")
    p.add_argument("--w-grid", nargs="*", type=float, default=None, dest="w_grid",
                   help=f"W values to sweep (default {W_GRID})")
    p.add_argument("--rt-factor", type=float, default=20.0, dest="rt_factor",
                   help="sim seconds per wall second; higher = faster (no LLM latency to pace). "
                        "Control cadence stays ~1 sim-s/tick.")
    p.add_argument("--provision-delay", nargs="*", type=float, default=[0.0, 2.0, 5.0, 10.0],
                   dest="provision_delay",
                   help="server provisioning delays (s) to sweep as the actuation-lag axis "
                        "(default: 0 2 5 10). 0 = instant; 5 = the realistic value.")
    p.add_argument("--save", action="store_true",
                   help="cache results to experiments/exp4_vw_tuning/vw_tuning.json")
    p.add_argument("--log", nargs="?", const="AUTO", default=None,
                   help="tee output to a file; bare --log auto-names under exp4_vw_tuning/logs/")
    args = p.parse_args()

    _logfile = None
    if args.log is not None:
        from datetime import datetime
        if args.log == "AUTO":
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = _LOGS_DIR / f"log_vw_tuning_{stamp}.txt"
        else:
            log_path = Path(args.log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _logfile = open(log_path, "w")
        sys.stdout = _Tee(sys.__stdout__, _logfile)
        sys.stderr = _Tee(sys.__stderr__, _logfile)
        print(f"[vw] logging this run to {log_path}")

    _prevent_sleep()
    try:
        asyncio.run(sweep(args))
    finally:
        if _logfile is not None:
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
            _logfile.close()
