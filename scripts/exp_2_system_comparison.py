"""
Experiment 2 — system comparison: does the full agentic controller beat non-AI baselines?

Compares six controllers on the SAME scenarios/seeds:
  • Static (c=1)   — fixed minimal capacity, no LLM
  • Static (c=8)   — fixed mid capacity, no LLM
  • Static (c=16)  — fixed full capacity (= c_max), no LLM
  • Lyapunov       — dynamic drift-plus-penalty capacity, no LLM, no filter
  • Deterministic (rules) — the FULL system minus the LLM: the agentic decision tree from
                     the judge prompt hardcoded as rules (RuleBasedController), over the same
                     Lyapunov capacity + malicious filter + calendar/forecast anticipation.
                     Isolates what the LLM adds over hardcoding its own instructions.
  • Agentic        — the FULL system: gemini storm judge + malicious filter +
                     anticipation tools (forecast + calendar) enabled. Learning OFF
                     (Experiment C) and operator intents OFF (Experiment E).

The deterministic baselines run in virtual time (fast, no LLM, no MCP); the agentic system
runs in real time (rt_factor=1) with the LLM judge. Per system per scenario we report
resilience P, benign completion, botnet-filtered (the deliberate filter's own contribution),
botnet-blocked, and mean online capacity (avg_servers), as mean with a 95% CI (Student-t)
over seeds; the agentic arm also reports judge latency. Only the agentic system has a
security mechanism, so a baseline's botnet-filtered is 0 by construction — that is the point.

Provisioning is PARALLEL by default (all pending servers spin up at once) so the headline
shows the framework's best-case capability; serial provisioning and the anticipation tools
are peeled back in the ablations (VI-C), not here.

Usage (source the shell env for the OpenRouter key first):
    python -m scripts.exp_2_system_comparison --seeds 5 --save --log
    python -m scripts.exp_2_system_comparison --seeds 2 --scenario mixed_flat_step  # quick check
    python -m scripts.exp_2_system_comparison --no-anticipation                       # ablation: judge+filter only
    python -m scripts.exp_2_system_comparison --serial                                # ablation: serial provisioning
"""

import argparse
import asyncio
import json
import logging
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.server import mcp, MCP_HOST, MCP_PORT
from scripts.run import resolve_model
from scripts.exp1_model_comparison_non_rt import _Tee, _prevent_sleep   # reuse logging + no-sleep
# Low-level building blocks — the agentic run is SELF-CONTAINED (like Experiment 1): it drives
# the fast loop + judge loop directly and never touches the Orchestrator / run_episode.
from agents.non_rt_agent import build_non_rt_agent, compose_system_prompt, run_assessment_loop
from agents.near_rt_control_loop import run_control_loop
from shared.policy import SharedPolicy, RunStats
from sim.config import (SimConfig, open_ran_arch, RRCConfig,
                        single_storm_traffic, mixed_storm_traffic)
from sim.simulator import StormSim
from sim.controllers import FixedController, LyapunovController
from agents.rule_based_controller import RuleBasedController, ShadowRunner
from sim.metrics import (resilience_multi, benign_success_rate,
                        malicious_blocked_rate, malicious_filtered_rate, avg_servers,
                        resilience_efficiency)
from runtime import UP, host as sim_host, scenario_calendar

AGENT_MODEL = "openrouter:google/gemini-3.1-flash-lite"   # winner of Experiment 1
# ONE mixed scenario (two botnet storms bracketing one benign surge), in four versions over
# two axes: onset step/ramp (step -> forecast blind, only the calendar anticipates the benign
# surge; ramp -> forecast fires too) x intensity flat/increasing (equal storms vs each larger
# than the last).
_SCENARIOS = ["mixed_flat_step", "mixed_flat_ramp", "mixed_inc_step", "mixed_inc_ramp"]

# Full-system judge prompt (storm detection + filter + anticipation via forecast/calendar).
_SYS_PROMPT = (Path(__file__).parent.parent / "prompts" / "non_rt_agent_system_prompt.md").read_text()

# deterministic baselines: (label, controller factory, initial server count c0)
BASELINES = [
    ("Static (c=1)",  lambda: FixedController(1), 1),    # minimal fixed capacity
    ("Static (c=8)",  lambda: FixedController(8), 8),    # half of c_max
    ("Static (c=16)", lambda: FixedController(16), 16),  # fully provisioned (= c_max)
    # util_p=UP so the baseline optimises the SAME utility the agentic fast loop uses and
    # that P is scored on (default UtilityParams differs) — fair + optimal.
    ("Lyapunov",      lambda: LyapunovController(V=1, W=1, util_p=UP), 1),
]

_EXP_DIR  = Path(__file__).parent.parent / "experiments" / "exp2_system_comparison"
_LOGS_DIR = _EXP_DIR / "logs"

# the per-episode metrics we track (baselines have no LLM → llm/assess latency = 0)
_KEYS = ["P", "benign", "filtered", "blocked", "servers", "eff", "llm_lat", "asmt_lat"]

# Student-t 97.5th percentile by SAMPLE SIZE n (df=n-1); 1.96 fallback for large n. Same as Exp 1/3.
_T95 = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571,
        7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262}


def _traffic(scenario):
    """Traffic config matching what the agentic runs build."""
    if scenario == "single_storm":
        return single_storm_traffic(t_post=20.0)
    return mixed_storm_traffic(ramped=("ramp" in scenario), increasing=("inc" in scenario))


def run_baseline(factory, c0, scenario, seed, parallel) -> dict:
    """One deterministic episode in virtual time. No filter, so `filtered` (deliberate
    defense) is 0; `blocked` may still be >0 from starvation-failures under an inadequate
    fixed capacity. No LLM → latencies are 0. `parallel` = provisioning mode (same as the
    agentic arm, so the comparison is fair)."""
    cfg = SimConfig(arch=open_ran_arch(), rrc=RRCConfig(t300_ms=1000, max_attempts=5),
                    c0=c0, c_max=16, traffic=_traffic(scenario), seed=seed,
                    parallel_provision=parallel)
    sim = StormSim(cfg)
    sim.run(controller=factory())
    storms = sim.cfg.traffic.storm_windows()
    P   = resilience_multi(sim.telemetry, sim.mu_single, UP, storms)["P_episode"]
    srv = avg_servers(sim.telemetry)
    return {"P": P,
            "benign": benign_success_rate(sim.stats),
            "filtered": malicious_filtered_rate(sim.stats),
            "blocked": malicious_blocked_rate(sim.stats),
            "servers": srv,
            "eff": resilience_efficiency(P, srv, 16),   # resilience per unit capacity (read next to P)
            "llm_lat": 0.0, "asmt_lat": 0.0}


RULE_LABEL = "Deterministic (rules)"


def run_rule_based(scenario, seed, args) -> dict:
    """The FULL system minus the LLM: the agentic decision tree encoded as deterministic
    rules (RuleBasedController), over the SAME Lyapunov capacity + malicious filter +
    calendar/forecast anticipation. Runs in virtual time (no LLM, no MCP), so it is fast.
    Isolates what the LLM adds over hardcoding its own prompt. Honours --serial and
    --no-anticipation exactly like the agentic arm."""
    antic   = not args.no_anticipation
    traffic = _traffic(scenario)
    cfg = SimConfig(arch=open_ran_arch(), rrc=RRCConfig(t300_ms=1000, max_attempts=5),
                    c0=2, c_max=16, traffic=traffic, seed=seed,
                    parallel_provision=not args.serial)
    cal  = scenario_calendar(scenario, traffic) if antic else []
    ctrl = RuleBasedController(calendar=cal, anticipation=antic,
                               assessment_interval=args.assessment_interval, util_p=UP)
    sim = StormSim(cfg)
    sim.run(controller=ctrl)
    storms = sim.cfg.traffic.storm_windows()
    P   = resilience_multi(sim.telemetry, sim.mu_single, UP, storms)["P_episode"]
    srv = avg_servers(sim.telemetry)
    return {"P": round(P, 4), "benign": benign_success_rate(sim.stats),
            "filtered": malicious_filtered_rate(sim.stats), "blocked": malicious_blocked_rate(sim.stats),
            "servers": srv, "eff": resilience_efficiency(P, srv, 16),
            "llm_lat": 0.0, "asmt_lat": 0.0}


async def run_agentic(model, scenario, seed, args, model_settings=None) -> dict:
    """One self-contained agentic episode — Experiment 1's structure (no Orchestrator): the
    gemini storm judge over the deterministic fast loop, with the FULL system enabled. On:
    Lyapunov capacity, the judge's storm_active / drop filter, and (unless --no-anticipation)
    the forecast + calendar tools so the judge can pre-provision ahead of a surge. Off:
    learning (Experiment C) and operator intents (Experiment E)."""
    antic = not args.no_anticipation
    non_rt = build_non_rt_agent(
        model,
        system_prompt=compose_system_prompt(_SYS_PROMPT, calendar_enabled=antic, forecast_enabled=antic),
    )
    policy = SharedPolicy()
    stats  = RunStats()

    # start() owns the calendar: it registers events for benign_surge and clears it for the
    # storm scenarios, so we don't touch sim_host.calendar here.
    sim_host.forecast_enabled = antic         # forecast tool gated on
    sim_host.calendar_enabled = antic         # calendar tool gated on (returns "disabled" when off)
    sim_host.start(scenario=scenario, seed=seed, c_max=16, rt_factor=args.rt_factor,
                   t_post=(20.0 if scenario == "single_storm" else None),
                   provision_parallel=not args.serial)   # headline = parallel (best); --serial for the ablation

    # SHADOW: the deterministic rule, configured exactly like this site (same calendar +
    # anticipation), scored against the agent each assessment WITHOUT actuating.
    shadow = None
    if args.shadow:
        rule = RuleBasedController(calendar=scenario_calendar(scenario, _traffic(scenario)),
                                   anticipation=antic, assessment_interval=args.assessment_interval,
                                   util_p=UP)
        shadow = ShadowRunner(rule)

    stop_event = asyncio.Event()

    async def _watch():
        while not sim_host.is_done:
            await asyncio.sleep(0.5)
        stop_event.set()

    await asyncio.gather(
        _watch(),
        run_control_loop(policy, stop_event, 1.0, stats, memory=None),
        run_assessment_loop(non_rt, policy, stop_event, args.assessment_interval, stats,
                            window_s=args.window_s, shadow=shadow, model_settings=model_settings),
    )

    sim = sim_host.sim
    final_P = 0.0
    try:
        storms  = sim.cfg.traffic.storm_windows()
        final_P = resilience_multi(sim.telemetry, sim.mu_single, UP, storms)["P_episode"]
    except Exception:
        pass
    st  = sim.stats
    srv = avg_servers(sim.telemetry)
    n   = max(1, stats.non_rt_assessments)
    out = {"P": round(final_P, 4), "benign": benign_success_rate(st),
           "filtered": malicious_filtered_rate(st), "blocked": malicious_blocked_rate(st),
           "servers": srv,
           "eff": resilience_efficiency(final_P, srv, 16),   # resilience per unit capacity (read next to P)
           "llm_lat": round(stats.llm_latency_s / n, 3),
           "asmt_lat": round(stats.assessment_latency_s / n, 3),
           # token totals for the model sweep (Exp 1) to price each judge; ignored by _agg's _KEYS.
           "in_tok": stats.llm_input_tokens, "out_tok": stats.llm_output_tokens,
           "errors": stats.non_rt_errors}
    if shadow is not None:
        out["shadow"] = shadow.agreement()   # rule-vs-agent agreement over this episode
    return out


def _agg(rows: list[dict]) -> dict:
    """Aggregate per-episode metric dicts into mean, SAMPLE std, 95% CI (Student-t), and the
    raw per-seed array for each key, so every statistic is reproducible from what's stored."""
    out = {}
    for k in _KEYS:
        vals = [r[k] for r in rows]
        n = len(vals)
        m = statistics.mean(vals)
        sd = statistics.stdev(vals) if n > 1 else 0.0
        ci = _T95.get(n, 1.96) * sd / math.sqrt(n) if n > 1 else 0.0
        out[f"{k}_mean"] = m; out[f"{k}_std"] = sd
        out[f"{k}_ci95"] = ci; out[f"{k}_seeds"] = vals
    return out


async def sweep(args):
    seeds = list(range(1, args.seeds + 1))
    scenarios = _SCENARIOS if args.scenario == "both" else [args.scenario]
    results: dict[str, dict] = {}

    # --- deterministic baselines (fast, virtual time) ---
    for label, factory, c0 in BASELINES:
        results[label] = {}
        for scn in scenarios:
            rows = [run_baseline(factory, c0, scn, s, not args.serial) for s in seeds]
            results[label][scn] = _agg(rows)
            r = results[label][scn]
            print(f"[sys] {label:16s} {scn:16s}  P={r['P_mean']:.3f} "
                  f"benign={r['benign_mean']:.3f} filtered={r['filtered_mean']:.3f} "
                  f"servers={r['servers_mean']:.1f}")

    # --- deterministic rules (fast, virtual time, no LLM): the agentic decision tree hardcoded ---
    results[RULE_LABEL] = {}
    for scn in scenarios:
        rows = [run_rule_based(scn, s, args) for s in seeds]
        results[RULE_LABEL][scn] = _agg(rows)
        r = results[RULE_LABEL][scn]
        print(f"[sys] {RULE_LABEL:16s} {scn:16s}  P={r['P_mean']:.3f} "
              f"benign={r['benign_mean']:.3f} filtered={r['filtered_mean']:.3f} "
              f"servers={r['servers_mean']:.1f}")

    # --- agentic (slow, real-time, LLM) ---
    model = resolve_model(AGENT_MODEL)
    label = "Agentic (gemini)" if not args.no_anticipation else "Agentic (no-antic)"
    results[label] = {}
    shadow_rows: dict[str, list] = {}          # per-scenario rule-vs-agent agreement records
    for scn in scenarios:
        rows = []
        for s in seeds:
            try:
                r = await run_agentic(model, scn, s, args)
            except Exception as e:
                print(f"[sys] agentic {scn} seed={s} ERROR {type(e).__name__}: {e}")
                continue
            rows.append(r)
            if "shadow" in r:
                shadow_rows.setdefault(scn, []).append(r["shadow"])
            print(f"[sys] {label:18s} {scn:16s} seed={s}  P={r['P']:.3f} benign={r['benign']:.3f} "
                  f"filtered={r['filtered']:.3f} servers={r['servers']:.1f} "
                  f"llm={r['llm_lat']:.1f}s asmt={r['asmt_lat']:.1f}s")
        if rows:
            results[label][scn] = _agg(rows)

    _print_table(results, scenarios, seeds, label)
    shadow_summary = _shadow_summary(shadow_rows) if shadow_rows else None
    if shadow_summary:
        _print_shadow(shadow_summary)
    if args.save:
        out = _EXP_DIR / "system_comparison.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {"seeds": seeds, "scenarios": scenarios,
                   "agent_model": AGENT_MODEL, "anticipation": not args.no_anticipation,
                   "provisioning": "serial" if args.serial else "parallel",
                   "systems": results}
        if shadow_summary:
            payload["shadow_agreement"] = shadow_summary
        out.write_text(json.dumps(payload, indent=2))
        print(f"\n  saved -> {out}")


def _shadow_summary(shadow_rows: dict[str, list]) -> dict:
    """Mean rule-vs-agent agreement per scenario (over seeds) + a pooled overall."""
    def _mean(vals):
        return round(sum(vals) / len(vals), 3) if vals else 0.0
    out, all_rows = {}, []
    for scn, recs in shadow_rows.items():
        all_rows += recs
        out[scn] = {"assessments": sum(r["n"] for r in recs),
                    "storm_agree":  _mean([r["storm_agree"]  for r in recs]),
                    "filter_agree": _mean([r["filter_agree"] for r in recs]),
                    "full_agree":   _mean([r["full_agree"]   for r in recs])}
    out["overall"] = {"assessments": sum(r["n"] for r in all_rows),
                      "storm_agree":  _mean([r["storm_agree"]  for r in all_rows]),
                      "filter_agree": _mean([r["filter_agree"] for r in all_rows]),
                      "full_agree":   _mean([r["full_agree"]   for r in all_rows])}
    return out


def _print_shadow(summary: dict):
    print("\n" + "=" * 72)
    print("SHADOW: rule-vs-agent agreement (rule run on the agent's telemetry, no actuation)")
    print("  storm = same storm call; filter = same filter on/off; full = both")
    print("=" * 72)
    print(f"  {'scenario':18s} {'assessments':>12s} {'storm':>8s} {'filter':>8s} {'full':>8s}")
    for scn, a in summary.items():
        print(f"  {scn:18s} {a['assessments']:>12d} {a['storm_agree']:>8.2f} "
              f"{a['filter_agree']:>8.2f} {a['full_agree']:>8.2f}")
    print("=" * 72)


def _print_table(results, scenarios, seeds, agent_label):
    order = [b[0] for b in BASELINES] + [RULE_LABEL, agent_label]
    print("\n" + "=" * 112)
    print(f"SYSTEM COMPARISON  ({len(seeds)} seeds)   P / benign / botnet-filtered / avg-servers / efficiency / latency (mean ± 95% CI)")
    print("  botnet-filtered = deliberate filter drops (only the agentic system filters);")
    print("  avg-servers = mean online capacity (lower at equal P = same protection, less cost);")
    print("  eff = resilience per unit capacity, P*c_max/avg_servers (read ONLY next to P — a")
    print("        low-P under-provisioned controller scores high but is useless);")
    print("  llm_lat = mean LLM-call time per assessment (agentic only)")
    print("=" * 112)
    for scn in scenarios:
        print(f"\n  --- {scn} ---")
        print(f"  {'system':21s} {'P':>15s} {'benign':>15s} {'filtered':>15s} {'servers':>12s} {'eff':>6s} {'llm_lat':>8s}")
        for label in order:
            s = results.get(label, {}).get(scn)
            if not s:
                continue
            print(f"  {label:21s} {s['P_mean']:.3f}±{s['P_ci95']:.3f}  "
                  f"{s['benign_mean']:.3f}±{s['benign_ci95']:.3f}  "
                  f"{s['filtered_mean']:.3f}±{s['filtered_ci95']:.3f}  "
                  f"{s['servers_mean']:>5.1f}±{s['servers_ci95']:.1f}  "
                  f"{s['eff_mean']:>6.2f} "
                  f"{s['llm_lat_mean']:>7.2f}s")
    print("=" * 112)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Experiment 2 — system comparison: baselines vs agentic")
    p.add_argument("--seeds", type=int, default=5)
    p.add_argument("--scenario", default="both",
                   choices=["both", "mixed_flat_step", "mixed_flat_ramp",
                            "mixed_inc_step", "mixed_inc_ramp", "single_storm"])
    p.add_argument("--rt-factor", type=float, default=1.0, dest="rt_factor")
    p.add_argument("--assessment-interval", type=float, default=5.0, dest="assessment_interval")
    p.add_argument("--window", type=float, default=15.0, dest="window_s",
                   help="telemetry-window seconds the judge sees (15 = validated Exp 1 config)")
    p.add_argument("--no-anticipation", action="store_true",
                   help="disable the forecast/calendar tools (agentic = judge + filter only)")
    p.add_argument("--serial", action="store_true",
                   help="serial provisioning (one server per delay). Default is PARALLEL "
                        "(best-case; all pending servers spin up at once) for the headline result.")
    p.add_argument("--shadow", action="store_true",
                   help="run the deterministic rule in SHADOW on the agent's telemetry each "
                        "assessment (no actuation) and report rule-vs-agent agreement.")
    p.add_argument("--save", action="store_true",
                   help="cache to experiments/exp2_system_comparison/system_comparison.json")
    p.add_argument("--log", nargs="?", const="AUTO", default=None,
                   help="tee output to a file (bare --log auto-names it under exp2_system_comparison/logs/)")
    args = p.parse_args()

    _logfile = None
    if args.log is not None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = (_LOGS_DIR / f"log_system_comparison_{stamp}.txt"
                    if args.log == "AUTO" else Path(args.log))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _logfile = open(log_path, "w")
        sys.stdout = _Tee(sys.__stdout__, _logfile)
        sys.stderr = _Tee(sys.__stderr__, _logfile)
        print(f"[sys] logging this run to {log_path}")

    _prevent_sleep()

    async def _main():
        print(f"[sys] Starting MCP server on {MCP_HOST}:{MCP_PORT} ...")
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
