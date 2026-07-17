"""
Experiment A — headline comparison: does the agentic system beat non-AI baselines?

Compares three controllers on the SAME scenarios/seeds:
  • Static (c=8)   — fixed capacity, no LLM
  • Lyapunov       — dynamic drift-plus-penalty capacity, no LLM
  • Agentic        — the full system: gemini storm judge + malicious filter + release
                     valve + anticipation tools (forecast/calendar), learning OFF
                     (learning is Experiment C), no operator intents (Experiment E)

The two deterministic baselines run in virtual time (fast, no LLM, no MCP); the agentic
system runs in real time (rt_factor=1) with the LLM judge. Reported per system per
scenario: resilience P, benign-served, and botnet-blocked (mean ± std over seeds). The
baselines have no security mechanism, so their blocked-rate is 0 by construction — that
is the point: only the agentic system defends against the botnet, at no cost to P/benign.

Usage (source the shell env for the OpenRouter key first):
    python -m scripts.experiment_phaseA_headline --seeds 10 --save --log
    python -m scripts.experiment_phaseA_headline --seeds 2 --scenario multi_storm_flat  # quick check
"""

import argparse
import asyncio
import json
import logging
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.server import mcp, MCP_HOST, MCP_PORT
from scripts.run import resolve_model
from scripts.experiment_model_comparison import _Tee, _prevent_sleep   # reuse logging + no-sleep
# Low-level building blocks — the agentic run is SELF-CONTAINED (like Experiment 1's bare
# judge): it drives the fast loop + judge loop directly and never touches the Orchestrator
# / run_episode, so no orchestrator tier is involved in Phase A.
from agents.non_rt_agent import build_non_rt_agent, run_assessment_loop
from agents.near_rt_control_loop import run_control_loop
from agents.policy import SharedPolicy, EpisodeStats
from sim.config import (SimConfig, open_ran_arch, RRCConfig,
                        single_storm_traffic, multi_storm_flat_traffic)
from sim.simulator import StormSim
from sim.controllers import FixedController, LyapunovController
from sim.metrics import (resilience_multi, benign_success_rate,
                        malicious_blocked_rate, malicious_filtered_rate, avg_servers)
from runtime import UP, host as sim_host

AGENT_MODEL = "openrouter:google/gemini-3.1-flash-lite"   # winner of Experiment 1
LQMAX = 1500.0
_SCENARIOS = ["single_storm", "multi_storm_flat"]

# Dedicated Phase A judge prompt: storm detection + filter only (anticipation is
# demonstrated in a separate experiment, so it is off here — the full system for the
# headline is Lyapunov capacity + LLM security judge + release valve).
_PHASEA_PROMPT = (Path(__file__).parent.parent / "prompts" / "prompts_phaseA_non_rt.md").read_text()

# deterministic baselines: (label, controller factory, initial server count c0)
BASELINES = [
    ("Static (c=1)",  lambda: FixedController(1), 1),    # minimal fixed capacity
    ("Static (c=8)",  lambda: FixedController(8), 8),    # half of c_max
    ("Static (c=16)", lambda: FixedController(16), 16),  # fully provisioned (= c_max)
    # util_p=UP so the baseline optimises the SAME utility the agentic fast loop uses and
    # that P is scored on (default UtilityParams differs: lq_max 7000 vs 1500) — fair + optimal.
    ("Lyapunov",      lambda: LyapunovController(V=1000, W=1, util_p=UP), 1),
]


def _traffic(scenario):
    """Traffic config matching what run_episode builds for the agentic runs."""
    if scenario == "single_storm":
        return single_storm_traffic(t_post=20.0)
    return multi_storm_flat_traffic()


# the per-episode metrics we track (baselines have no LLM → llm/assess latency = 0)
_KEYS = ["P", "benign", "filtered", "blocked", "servers", "llm_lat", "asmt_lat"]


def run_baseline(factory, c0, scenario, seed) -> dict:
    """One deterministic episode in virtual time. No filter, so `filtered` (deliberate
    defense) is 0; `blocked` may still be >0 from starvation-failures under an inadequate
    fixed capacity. No LLM → latencies are 0."""
    cfg = SimConfig(arch=open_ran_arch(), rrc=RRCConfig(t300_ms=1000, max_attempts=5),
                    c0=c0, c_max=16, lq_max=LQMAX, traffic=_traffic(scenario), seed=seed)
    sim = StormSim(cfg)
    sim.run(controller=factory())
    storms = sim.cfg.traffic.storm_windows()
    return {"P": resilience_multi(sim.telemetry, sim.mu_single, UP, storms)["P_episode"],
            "benign": benign_success_rate(sim.stats),
            "filtered": malicious_filtered_rate(sim.stats),
            "blocked": malicious_blocked_rate(sim.stats),
            "servers": avg_servers(sim.telemetry),
            "llm_lat": 0.0, "asmt_lat": 0.0}


async def run_agentic(model, scenario, seed, args) -> dict:
    """One self-contained agentic episode — exactly Experiment 1's bare-judge structure
    (no Orchestrator / run_episode): the gemini storm judge over the deterministic fast
    loop. Off: anticipation (forecast/calendar), learning, release valve. On: Lyapunov
    capacity + the judge's storm_active / drop. Also reports the judge's mean LLM-call
    time and mean full-assessment time (summary+prompt+LLM+policy write)."""
    non_rt = build_non_rt_agent(model, system_prompt=_PHASEA_PROMPT)
    policy = SharedPolicy()
    stats  = EpisodeStats()

    sim_host.calendar = []
    sim_host.forecast_enabled = False   # anticipation OFF (its own experiment)
    sim_host.calendar_enabled = False
    sim_host.start(scenario=scenario, seed=seed, c_max=16, rt_factor=args.rt_factor,
                   t_post=(20.0 if scenario == "single_storm" else None))

    stop_event = asyncio.Event()

    async def _watch():
        while not sim_host.is_done:
            await asyncio.sleep(0.5)
        stop_event.set()

    await asyncio.gather(
        _watch(),
        run_control_loop(policy, stop_event, 1.0, stats, memory=None, release_valve=False),
        run_assessment_loop(non_rt, policy, stop_event, args.assessment_interval, stats,
                            window_s=args.window_s),
    )

    sim = sim_host.sim
    final_P = 0.0
    try:
        storms  = sim.cfg.traffic.storm_windows()
        final_P = resilience_multi(sim.telemetry, sim.mu_single, UP, storms)["P_episode"]
    except Exception:
        pass
    st = sim.stats
    n  = max(1, stats.non_rt_assessments)
    return {"P": round(final_P, 4), "benign": benign_success_rate(st),
            "filtered": malicious_filtered_rate(st), "blocked": malicious_blocked_rate(st),
            "servers": avg_servers(sim.telemetry),
            "llm_lat": round(stats.llm_latency_s / n, 3),
            "asmt_lat": round(stats.assessment_latency_s / n, 3)}


def _agg(rows: list[dict]) -> dict:
    """Aggregate a list of per-episode metric dicts into mean/std per key."""
    _sd = lambda v: statistics.pstdev(v) if len(v) > 1 else 0.0
    out = {}
    for k in _KEYS:
        vals = [r[k] for r in rows]
        out[f"{k}_mean"], out[f"{k}_std"] = statistics.mean(vals), _sd(vals)
    return out


async def sweep(args):
    seeds = list(range(1, args.seeds + 1))
    scenarios = _SCENARIOS if args.scenario == "both" else [args.scenario]
    results: dict[str, dict] = {}

    # --- deterministic baselines (fast, virtual time) ---
    for label, factory, c0 in BASELINES:
        results[label] = {}
        for scn in scenarios:
            rows = [run_baseline(factory, c0, scn, s) for s in seeds]
            results[label][scn] = _agg(rows)
            r = results[label][scn]
            print(f"[phaseA] {label:16s} {scn:16s}  P={r['P_mean']:.3f} "
                  f"benign={r['benign_mean']:.3f} filtered={r['filtered_mean']:.3f} "
                  f"servers={r['servers_mean']:.1f}")

    # --- agentic (slow, real-time, LLM) ---
    model = resolve_model(AGENT_MODEL)
    label = "Agentic (gemini)"
    results[label] = {}
    for scn in scenarios:
        rows = []
        for s in seeds:
            try:
                r = await run_agentic(model, scn, s, args)
            except Exception as e:
                print(f"[phaseA] agentic {scn} seed={s} ERROR {type(e).__name__}: {e}")
                continue
            rows.append(r)
            print(f"[phaseA] {label:16s} {scn:16s} seed={s}  P={r['P']:.3f} benign={r['benign']:.3f} "
                  f"filtered={r['filtered']:.3f} servers={r['servers']:.1f} "
                  f"llm={r['llm_lat']:.1f}s asmt={r['asmt_lat']:.1f}s")
        if rows:
            results[label][scn] = _agg(rows)

    _print_table(results, scenarios, seeds)
    if args.save:
        out = Path(__file__).parent.parent / "results" / "phaseA_headline.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps({"seeds": seeds, "scenarios": scenarios,
                                   "agent_model": AGENT_MODEL, "systems": results}, indent=2))
        print(f"\n  saved -> {out}")


def _print_table(results, scenarios, seeds):
    order = [b[0] for b in BASELINES] + ["Agentic (gemini)"]
    print("\n" + "=" * 112)
    print(f"PHASE A — HEADLINE  ({len(seeds)} seeds)   P / benign / botnet-filtered / avg-servers / judge latency (mean ± std)")
    print("  botnet-filtered = deliberate filter drops (only the agentic system filters);")
    print("  avg-servers = mean online capacity (lower at equal P = same protection, less cost);")
    print("  llm_lat = mean LLM-call time per assessment; asmt_lat = mean full-assessment time (agentic only)")
    print("=" * 112)
    for scn in scenarios:
        print(f"\n  --- {scn} ---")
        print(f"  {'system':18s} {'P':>13s} {'benign':>13s} {'filtered':>13s} {'servers':>11s} "
              f"{'llm_lat':>8s} {'asmt_lat':>9s}")
        for label in order:
            s = results.get(label, {}).get(scn)
            if not s:
                continue
            print(f"  {label:18s} {s['P_mean']:.3f}±{s['P_std']:.3f}  "
                  f"{s['benign_mean']:.3f}±{s['benign_std']:.3f}  "
                  f"{s['filtered_mean']:.3f}±{s['filtered_std']:.3f}  "
                  f"{s['servers_mean']:>5.1f}±{s['servers_std']:.1f}  "
                  f"{s['llm_lat_mean']:>7.2f}s {s['asmt_lat_mean']:>8.2f}s")
    print("=" * 112)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Experiment A — headline: baselines vs agentic")
    p.add_argument("--seeds", type=int, default=10)
    p.add_argument("--scenario", default="both",
                   choices=["both", "single_storm", "multi_storm_flat"])
    p.add_argument("--rt-factor", type=float, default=1.0, dest="rt_factor")
    p.add_argument("--assessment-interval", type=float, default=5.0, dest="assessment_interval")
    p.add_argument("--window", type=float, default=15.0, dest="window_s",
                   help="telemetry-window seconds the judge sees (15 = validated Exp 1 config)")
    p.add_argument("--save", action="store_true", help="cache to results/phaseA_headline.json")
    p.add_argument("--log", nargs="?", const="AUTO", default=None,
                   help="tee output to a file (bare --log auto-names it in results/)")
    args = p.parse_args()

    _logfile = None
    if args.log is not None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = (Path(__file__).parent.parent / "results" / f"log_phaseA_{stamp}.txt"
                    if args.log == "AUTO" else Path(args.log))
        log_path.parent.mkdir(exist_ok=True)
        _logfile = open(log_path, "w")
        sys.stdout = _Tee(sys.__stdout__, _logfile)
        sys.stderr = _Tee(sys.__stderr__, _logfile)
        print(f"[phaseA] logging this run to {log_path}")

    _prevent_sleep()

    async def _main():
        print(f"[phaseA] Starting MCP server on {MCP_HOST}:{MCP_PORT} ...")
        server_task = asyncio.create_task(
            mcp.run_http_async(host=MCP_HOST, port=MCP_PORT, show_banner=False, log_level="warning"))
        await asyncio.sleep(1.5)
        try:
            await sweep(args)
        finally:
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
