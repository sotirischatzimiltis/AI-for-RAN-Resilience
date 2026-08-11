"""
Experiment 5 — compute-contention sensitivity.

Experiments 1-4 all run the INDEPENDENT-SERVER model (compute_kappa=None): each of the c servers
processes attaches at the full rate mu no matter how many are busy. Real vCU/vDU share a finite
compute pool, so scaling servers has diminishing returns once the pool saturates. This experiment
RE-RUNS the Exp-2 system comparison across a shared-compute-pool axis and asks one question:

    does the ranking of controllers survive realistic compute contention?

Contention model (sim.config.SimConfig): per-attach PROCESSING time inflates by 1/(1 - rho_c), with
rho_c = busy_workers / kappa. kappa is the pool size in attach-workers and MUST exceed c_max (=16);
smaller kappa = a tighter pool = more contention. Propagation delay is unaffected. kappa=None
recovers Exp 1-4 (contention off).

We sweep kappa in {OFF, ...user levels...} on the SAME botnet_event scenario, the SAME six/seven
arms, and the SAME seeds as Exp 2, reusing Exp 2's arm definitions and metric decomposition so the
two experiments are directly comparable. This is a STANDALONE sensitivity study: it does not change
Exp 1-4, which keep the clean independent-server model.

Output: per (kappa, arm) the full Exp-2 metric set (mean + 95% CI over seeds), plus an OFF-vs-ON
table showing how much resilience each arm loses to the shared pool.

Usage (source the shell env for the OpenRouter key first):
    python -m scripts.exp_5_compute_contention --seeds 5 --kappa 48 24 --save --log
    python -m scripts.exp_5_compute_contention --seeds 1 --kappa 24          # quick check
    python -m scripts.exp_5_compute_contention --seeds 5 --kappa 24 --no-llm  # deterministic arms only
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.server import mcp, MCP_HOST, MCP_PORT
from scripts.run import resolve_model
from scripts.exp_1_model_comparison import _Tee, _prevent_sleep, judge_settings
# Reuse Exp 2's arms, prompt, metric decomposition and aggregation UNCHANGED, so every arm here is
# byte-for-byte the Exp-2 arm and the two experiments are directly comparable. Importing this module
# has no side effects beyond Exp 2's own top-level imports (its CLI is guarded by __main__).
from scripts.exp_2_system_comparison import (
    AGENTS, BASELINES, RULE_LABEL, _SYS_PROMPT, _traffic,
    _episode_metrics, _agg,
)
from agents.non_rt_agent import build_non_rt_agent, compose_system_prompt, run_assessment_loop
from agents.near_rt_control_loop import run_control_loop
from agents.rule_based_controller import RuleBasedController
from shared.policy import SharedPolicy, RunStats
from sim.config import SimConfig, open_ran_arch, RRCConfig
from sim.simulator import StormSim
from runtime import UP, host as sim_host, LLM_COMPARE

_EXP_DIR  = Path(__file__).parent.parent / "experiments" / "exp5_compute_contention"
_LOGS_DIR = _EXP_DIR / "logs"

_C_MAX = 16   # the arms' capacity ceiling; kappa MUST exceed it (SimConfig SIM-7 guard)


# ---------------------------------------------------------------------------
# kappa-aware arm runners — the Exp-2 arms with the shared-compute pool threaded in.
# ---------------------------------------------------------------------------
def _run_baseline(factory, c0, seed, parallel, kappa) -> dict:
    """A deterministic static/Lyapunov baseline at pool size `kappa` (None = contention off)."""
    cfg = SimConfig(arch=open_ran_arch(), rrc=RRCConfig(t300_ms=1000, max_attempts=5),
                    c0=c0, c_max=_C_MAX, traffic=_traffic(), seed=seed,
                    parallel_provision=parallel, compute_kappa=kappa)
    sim = StormSim(cfg)
    sim.run(controller=factory())
    return _episode_metrics(sim, sim.cfg.traffic.storm_windows())


def _run_rule(seed, args, kappa) -> dict:
    """The calendar-free rule-based controller at pool size `kappa`."""
    cfg = SimConfig(arch=open_ran_arch(), rrc=RRCConfig(t300_ms=1000, max_attempts=5),
                    c0=2, c_max=_C_MAX, traffic=_traffic(), seed=seed,
                    parallel_provision=args.parallel, compute_kappa=kappa)
    ctrl = RuleBasedController(anticipation=True,
                               assessment_interval=args.assessment_interval, util_p=UP)
    sim = StormSim(cfg)
    sim.run(controller=ctrl)
    return _episode_metrics(sim, sim.cfg.traffic.storm_windows())


async def _run_agentic(model, seed, args, kappa, settings) -> dict:
    """One full agentic episode (LLM judge + fast loop + forecast/calendar tools) at pool size
    `kappa`. Mirrors Exp 2's run_agentic exactly, adding compute_kappa to the sim host."""
    non_rt = build_non_rt_agent(
        model,
        system_prompt=compose_system_prompt(_SYS_PROMPT, calendar_enabled=True, forecast_enabled=True),
    )
    policy = SharedPolicy()
    stats  = RunStats()

    sim_host.forecast_enabled = True
    sim_host.calendar_enabled = True
    sim_host.start(scenario=LLM_COMPARE, seed=seed, c_max=_C_MAX, rt_factor=args.rt_factor,
                   provision_parallel=args.parallel, compute_kappa=kappa)

    stop_event = asyncio.Event()

    async def _watch():
        while not sim_host.is_done:
            await asyncio.sleep(0.5)
        stop_event.set()

    await asyncio.gather(
        _watch(),
        run_control_loop(policy, stop_event, 1.0, stats, memory=None),
        run_assessment_loop(non_rt, policy, stop_event, args.assessment_interval, stats,
                            window_s=args.window_s, model_settings=settings),
    )

    sim = sim_host.sim
    out = _episode_metrics(sim, sim.cfg.traffic.storm_windows(), stats)
    out.update({"att_est": stats.judge_peak_attendance, "reserve_est": stats.judge_peak_reserve,
                "in_tok": stats.llm_input_tokens, "out_tok": stats.llm_output_tokens,
                "errors": stats.non_rt_errors})
    return out


# ---------------------------------------------------------------------------
# the contention sweep
# ---------------------------------------------------------------------------
def _levels(args) -> list[tuple[str, float | None]]:
    """Contention axis: OFF (independent servers, the Exp 1-4 model) then each --kappa level."""
    return [("off", None)] + [(f"kappa={int(k)}", float(k)) for k in args.kappa]


async def sweep(args) -> None:
    seeds  = list(range(1, args.seeds + 1))
    levels = _levels(args)
    # results[level_label][arm_label] = _agg(rows)   (scenario is fixed = botnet_event)
    results: dict[str, dict] = {}

    if args.resume and args.ckpt_path.exists():
        prev = json.loads(args.ckpt_path.read_text())
        if prev.get("seeds") == seeds and prev.get("levels") == [l for l, _ in levels]:
            results = prev.get("systems", {})
            print(f"[cc] resume: {sum(len(v) for v in results.values())} (level,arm) cells "
                  f"from {args.ckpt_path.name}")
        else:
            print("[cc] resume: checkpoint seeds/levels differ — starting fresh")

    def _ckpt():
        if args.save:
            _save_payload(results, args, seeds, levels)

    for level_label, kappa in levels:
        results.setdefault(level_label, {})
        cell = results[level_label]
        print(f"\n############  contention: {level_label}  "
              f"({'independent servers' if kappa is None else f'shared pool kappa={kappa:g}'})  ############")

        # --- deterministic arms (fast, virtual time) ---
        for label, factory, c0 in BASELINES:
            if label in cell:
                continue
            cell[label] = _agg([_run_baseline(factory, c0, s, args.parallel, kappa) for s in seeds])
            r = cell[label]
            print(f"[cc] {label:22s} P={r['P_mean']:.3f} P_surge={r['P_surge_mean']:.3f} "
                  f"benign={r['benign_mean']:.3f} servers={r['servers_mean']:.1f}")
        if RULE_LABEL not in cell:
            cell[RULE_LABEL] = _agg([_run_rule(s, args, kappa) for s in seeds])
            r = cell[RULE_LABEL]
            print(f"[cc] {RULE_LABEL:22s} P={r['P_mean']:.3f} P_surge={r['P_surge_mean']:.3f} "
                  f"benign={r['benign_mean']:.3f} filtered={r['filtered_mean']:.3f} servers={r['servers_mean']:.1f}")
        _ckpt()

        # --- agentic arms (slow, real-time, LLM) ---
        if not args.no_llm:
            for label, slug, mode in AGENTS:
                if label in cell:
                    continue
                model    = resolve_model(slug)
                settings = judge_settings(slug, mode)
                rows = []
                for s in seeds:
                    try:
                        r = await _run_agentic(model, s, args, kappa, settings)
                    except Exception as e:
                        print(f"[cc] {label} {level_label} seed={s} ERROR {type(e).__name__}: {e}")
                        continue
                    rows.append(r)
                    print(f"[cc] {label:22s} seed={s}  P={r['P']:.3f} P_surge={r['P_surge']:.3f} "
                          f"benign={r['benign']:.3f} srv={r['servers']:.1f} llm={r['llm_lat']:.1f}s")
                if rows:
                    cell[label] = _agg(rows)
                _ckpt()

    _print_contention(results, levels, seeds, args.no_llm)
    if args.save:
        print(f"\n  saved -> {args.ckpt_path}")


def _save_payload(results, args, seeds, levels) -> None:
    args.ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    args.ckpt_path.write_text(json.dumps({
        "seeds": seeds,
        "levels": [l for l, _ in levels],
        "kappa":  {l: k for l, k in levels},
        "scenario": LLM_COMPARE,
        "provisioning": "parallel" if args.parallel else "serial",
        "agents": [{"label": l, "slug": s, "mode": m} for l, s, m in AGENTS],
        "systems": results,
    }, indent=2))


def _print_contention(results, levels, seeds, no_llm) -> None:
    """Per arm, one row per contention level with P / P_surge / benign / servers and dP vs OFF —
    so the reader sees how much resilience the shared compute pool removes from each controller."""
    arm_order = [b[0] for b in BASELINES] + [RULE_LABEL] + ([] if no_llm else [a[0] for a in AGENTS])
    off = results.get("off", {})
    print("\n" + "=" * 92)
    print(f"COMPUTE-CONTENTION SENSITIVITY  ({len(seeds)} seeds)   "
          f"P / P_surge / benign / avg-servers (mean ± 95% CI), dP vs OFF")
    print("  OFF = independent servers (the Exp 1-4 model); kappa = shared vCU/vDU pool (smaller = tighter)")
    print("=" * 92)
    for arm in arm_order:
        print(f"\n  --- {arm} ---")
        print(f"  {'contention':12s} {'P':>13s} {'dP_off':>8s} {'P_surge':>9s} "
              f"{'benign':>13s} {'servers':>12s}")
        p_off = off.get(arm, {}).get("P_mean")
        for level_label, _ in levels:
            s = results.get(level_label, {}).get(arm)
            if not s:
                continue
            dP = "" if (p_off is None or level_label == "off") else f"{s['P_mean'] - p_off:+.3f}"
            print(f"  {level_label:12s} {s['P_mean']:.3f}±{s['P_ci95']:.3f}  {dP:>8s} "
                  f"{s['P_surge_mean']:>7.3f}  {s['benign_mean']:.3f}±{s['benign_ci95']:.3f}  "
                  f"{s['servers_mean']:>5.1f}±{s['servers_ci95']:.1f}")
    print("=" * 92)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Experiment 5 — compute-contention sensitivity")
    p.add_argument("--seeds", type=int, default=5)
    p.add_argument("--kappa", nargs="+", type=float, default=[48, 24],
                   help=f"shared-compute pool size(s) to test ON (each MUST exceed c_max={_C_MAX}; "
                        "smaller = more contention). OFF is always included. Default: 48 24")
    p.add_argument("--rt-factor", type=float, default=1.0, dest="rt_factor")
    p.add_argument("--assessment-interval", type=float, default=5.0, dest="assessment_interval")
    p.add_argument("--window", type=float, default=15.0, dest="window_s",
                   help="telemetry-window seconds the judge sees (15 = validated Exp 1/2 config)")
    p.add_argument("--parallel", action="store_true",
                   help="parallel provisioning ablation (default SERIAL, matching Exp 1/2)")
    p.add_argument("--no-llm", action="store_true", dest="no_llm",
                   help="deterministic arms only (skip the agentic LLM arms) — fast, no API cost")
    p.add_argument("--save", action="store_true",
                   help="write timestamped results to experiments/exp5_compute_contention/, "
                        "checkpointed after each level")
    p.add_argument("--resume", action="store_true",
                   help="reload the NEWEST matching checkpoint and skip (level,arm) cells already done")
    p.add_argument("--log", nargs="?", const="AUTO", default=None,
                   help="tee output to a file (bare --log auto-names it under exp5_compute_contention/logs/)")
    args = p.parse_args()

    bad = [k for k in args.kappa if k <= _C_MAX]
    if bad:
        sys.exit(f"[cc] kappa values {bad} must exceed c_max={_C_MAX} (the contention pole is at "
                 f"kappa=c_max); use e.g. 24 48 60.")

    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.resume:
        prior = sorted(_EXP_DIR.glob("compute_contention_[0-9]*.json"))
        args.ckpt_path = prior[-1] if prior else _EXP_DIR / f"compute_contention_{run_stamp}.json"
    else:
        args.ckpt_path = _EXP_DIR / f"compute_contention_{run_stamp}.json"

    _logfile = None
    if args.log is not None:
        log_path = (_LOGS_DIR / f"log_compute_contention_{run_stamp}.txt"
                    if args.log == "AUTO" else Path(args.log))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _logfile = open(log_path, "w")
        sys.stdout = _Tee(sys.__stdout__, _logfile)
        sys.stderr = _Tee(sys.__stderr__, _logfile)
        print(f"[cc] logging this run to {log_path}")

    _prevent_sleep()

    async def _main():
        # The agentic arms' judge reads telemetry/forecast/calendar over MCP, so host the server
        # (skipped when --no-llm, since the deterministic arms never touch it).
        server_task = None
        if not args.no_llm:
            print(f"[cc] Starting MCP server on {MCP_HOST}:{MCP_PORT} ...")
            server_task = asyncio.create_task(
                mcp.run_http_async(host=MCP_HOST, port=MCP_PORT, show_banner=False, log_level="warning"))
            await asyncio.sleep(1.5)
        try:
            await sweep(args)
        finally:
            if server_task is not None:
                logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
                server_task.cancel()
                try:
                    await server_task
                except asyncio.CancelledError:
                    pass

    try:
        asyncio.run(_main())
    finally:
        if _logfile is not None:
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
            _logfile.close()
