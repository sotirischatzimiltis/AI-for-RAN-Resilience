"""
Experiment 2 — system comparison: does the full agentic controller beat non-AI baselines?

Compares six controllers on the SAME scenario — the Exp-1 `botnet_event`: one malicious botnet
ramp plus one benign real-event surge sized to EXP1_EVENT's true attendance — over N seeds:
  • Static (c=1)   — fixed minimal capacity, no LLM
  • Static (c=8)   — fixed mid capacity, no LLM
  • Static (c=16)  — fixed full capacity (= c_max), no LLM
  • Lyapunov       — reactive drift-plus-penalty capacity, no LLM, no filter
  • Deterministic (rules) — the agentic decision tree hardcoded (RuleBasedController): reactive
                     Lyapunov capacity + malicious filter + FORECAST anticipation, but NO calendar
                     (a calendar is human-in-the-loop knowledge the rule has not earned), so it
                     cannot recognize the benign surge and filters it as if it were an attack.
  • Agentic        — the FULL system: the LLM storm judge + malicious filter + BOTH anticipation
                     tools (forecast + calendar), always on. Learning OFF (Exp C), intents OFF (Exp E).

The deterministic arms run in virtual time (fast, no LLM/MCP); the agentic arm runs in real time
(rt_factor=1) with the LLM judge over the same fast loop, and sees the SAME per-seed traffic. Per
arm we report resilience P, benign served, botnet-filtered/blocked, and mean online capacity
(avg_servers) as mean with a 95% CI (Student-t) over seeds; the agentic arm also reports latency.
Static and Lyapunov never filter (botnet-filtered = 0 by construction); the rule and agentic do.

Provisioning is SERIAL by default (one server per delay), matching Exp 1 and making the calendar's
pre-provisioning advantage meaningful; --parallel runs the parallel-provisioning ablation.

Usage (source the shell env for the OpenRouter key first):
    python -m scripts.exp_2_system_comparison --seeds 5 --save --log
    python -m scripts.exp_2_system_comparison --seeds 1              # quick check
    python -m scripts.exp_2_system_comparison --parallel            # parallel-provisioning ablation
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
from scripts.exp_1_model_comparison import _Tee, _prevent_sleep, judge_settings  # reuse logging + no-sleep + LLM settings
# Low-level building blocks — the agentic run is SELF-CONTAINED (like Experiment 1): it drives
# the fast loop + judge loop directly and never touches the Orchestrator / run_episode.
from agents.non_rt_agent import build_non_rt_agent, compose_system_prompt, run_assessment_loop
from agents.near_rt_control_loop import run_control_loop
from shared.policy import SharedPolicy, RunStats
from sim.config import SimConfig, open_ran_arch, RRCConfig, botnet_event_traffic
from sim.simulator import StormSim
from sim.controllers import FixedController, LyapunovController
from agents.rule_based_controller import RuleBasedController
from sim.metrics import (resilience_multi, benign_success_rate, benign_false_positive_rate,
                        malicious_blocked_rate, malicious_filtered_rate, avg_servers,
                        resilience_efficiency, utility_decomposition)
from shared.events import EXP1_EVENT
from runtime import UP, host as sim_host, LLM_COMPARE

# The two judges carried from Experiment 1 (label, slug, reasoning-mode for judge_settings): the
# deployable operating point (gemini) and the reasoning ceiling (gpt-5.4-mini, thinking on).
AGENTS = [
    ("Agentic (gemini)",       "openrouter:google/gemini-3.1-flash-lite", "n/a"),
    ("Agentic (gpt-5.4-mini)", "openrouter:openai/gpt-5.4-mini",          "on"),
]
# ONE scenario — the Exp-1 botnet_event: a malicious botnet ramp (forecast catches it) followed by
# a benign real-event surge sized to EXP1_EVENT's attendance (only the calendar anticipates it, so
# only the agentic arm can pre-provision the reserve). Same scenario the judge was chosen on.
_SCENARIOS = [LLM_COMPARE]

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

# the per-episode metrics we aggregate — the SAME resilience decomposition Exp 1 reports, so every
# arm (baselines, rule, agentic) is scored identically. Baselines have no LLM => llm/asmt latency = 0.
_KEYS = ["P", "P_bot", "P_surge", "benign", "benign_fp", "filtered", "blocked", "servers", "eff",
         "absorb_bot", "adapt_bot", "trec_bot", "absorb_ev", "adapt_ev", "trec_ev",
         "rho_ev", "uA_ev", "uB_ev", "llm_lat", "asmt_lat"]

# Student-t 97.5th percentile by SAMPLE SIZE n (df=n-1); 1.96 fallback for large n. Same as Exp 1/3.
_T95 = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571,
        7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262}


def _traffic(scenario=LLM_COMPARE):
    """The Exp-1 botnet_event traffic (botnet ramp + benign event surge sized to EXP1_EVENT's real
    attendance) — the SAME builder runtime.start uses for the agentic arm, so the deterministic
    arms see identical arrivals at a given seed."""
    return botnet_event_traffic(EXP1_EVENT.surge_peak())


def _episode_metrics(sim, storms, stats=None) -> dict:
    """The per-episode metric set every arm reports, computed from a finished sim — the SAME
    resilience decomposition Exp 1 uses (P + its botnet/event window split, benign served + false
    positives, botnet filtered/blocked, avg capacity, efficiency, and the event-surge utility split
    rho/uA/uB). LLM latency is filled from `stats` for the agentic arm, else 0 (deterministic arms).
    P_win = 0.4*absorption + 0.4*adaptation + 0.2*trec over each storm window."""
    try:
        rm      = resilience_multi(sim.telemetry, sim.mu_single, UP, storms)
        final_P = rm["P_episode"]; per_storm = rm["per_storm"]
        decomp  = utility_decomposition(sim.telemetry, sim.mu_single, UP, storms)
    except Exception:
        final_P, per_storm, decomp = 0.0, [], []

    def _win(seq, i, key, default):
        return seq[i][key] if len(seq) > i else default
    ab_b = _win(per_storm, 0, "absorption", 1.0); ad_b = _win(per_storm, 0, "adaptation", 1.0); tr_b = _win(per_storm, 0, "trec", 1.0)
    ab_e = _win(per_storm, 1, "absorption", 1.0); ad_e = _win(per_storm, 1, "adaptation", 1.0); tr_e = _win(per_storm, 1, "trec", 1.0)
    srv  = avg_servers(sim.telemetry)
    st   = sim.stats
    n    = max(1, stats.non_rt_assessments) if stats else 1
    return {
        "P": round(final_P, 4),
        "P_bot":   round(0.4 * ab_b + 0.4 * ad_b + 0.2 * tr_b, 4),   # botnet-window resilience
        "P_surge": round(0.4 * ab_e + 0.4 * ad_e + 0.2 * tr_e, 4),   # event-window resilience (the discriminator)
        "benign": benign_success_rate(st), "benign_fp": benign_false_positive_rate(st),
        "filtered": malicious_filtered_rate(st), "blocked": malicious_blocked_rate(st),
        "servers": srv, "eff": resilience_efficiency(final_P, srv, 16),
        "absorb_bot": round(ab_b, 4), "adapt_bot": round(ad_b, 4), "trec_bot": round(tr_b, 4),
        "absorb_ev":  round(ab_e, 4), "adapt_ev":  round(ad_e, 4), "trec_ev":  round(tr_e, 4),
        "rho_ev": round(_win(decomp, 1, "rho", 0.0), 4),
        "uA_ev":  round(_win(decomp, 1, "uA", 1.0), 4),
        "uB_ev":  round(_win(decomp, 1, "uB", 1.0), 4),
        "llm_lat":  round(stats.llm_latency_s / n, 3) if stats else 0.0,
        "asmt_lat": round(stats.assessment_latency_s / n, 3) if stats else 0.0,
    }


def run_baseline(factory, c0, scenario, seed, parallel) -> dict:
    """One deterministic episode in virtual time. No filter, so `filtered` (deliberate defense) is
    0; `blocked` may still be >0 from starvation under an inadequate fixed capacity. No LLM =>
    latencies 0. `parallel` = provisioning mode (same as the agentic arm, so the comparison is fair)."""
    cfg = SimConfig(arch=open_ran_arch(), rrc=RRCConfig(t300_ms=1000, max_attempts=5),
                    c0=c0, c_max=16, traffic=_traffic(scenario), seed=seed,
                    parallel_provision=parallel)
    sim = StormSim(cfg)
    sim.run(controller=factory())
    return _episode_metrics(sim, sim.cfg.traffic.storm_windows())


RULE_LABEL = "Deterministic (rules)"


def run_rule_based(scenario, seed, args) -> dict:
    """The agentic decision tree encoded as deterministic rules (RuleBasedController): reactive
    Lyapunov capacity + malicious filter + FORECAST anticipation, but calendar-free. Runs in
    virtual time (no LLM, no MCP), so it is fast. Isolates what the LLM adds over hardcoding its
    own logic. Honours the provisioning mode exactly like the agentic arm."""
    cfg = SimConfig(arch=open_ran_arch(), rrc=RRCConfig(t300_ms=1000, max_attempts=5),
                    c0=2, c_max=16, traffic=_traffic(scenario), seed=seed,
                    parallel_provision=args.parallel)
    ctrl = RuleBasedController(anticipation=True,
                               assessment_interval=args.assessment_interval, util_p=UP)
    sim = StormSim(cfg)
    sim.run(controller=ctrl)
    return _episode_metrics(sim, sim.cfg.traffic.storm_windows())


async def run_agentic(model, scenario, seed, args, model_settings=None) -> dict:
    """One self-contained agentic episode — Experiment 1's structure (no Orchestrator): the LLM
    storm judge over the deterministic fast loop, with the FULL system enabled and ALL tools on:
    Lyapunov capacity, the judge's storm_active / drop filter, and the forecast + calendar tools
    so the judge can pre-provision ahead of the surge. Off: learning (Exp C) and intents (Exp E)."""
    non_rt = build_non_rt_agent(
        model,
        system_prompt=compose_system_prompt(_SYS_PROMPT, calendar_enabled=True, forecast_enabled=True),
    )
    policy = SharedPolicy()
    stats  = RunStats()

    # start() owns the calendar: it registers the event surge so the judge can reason about it.
    sim_host.forecast_enabled = True          # forecast tool always on
    sim_host.calendar_enabled = True          # calendar tool always on
    sim_host.start(scenario=scenario, seed=seed, c_max=16, rt_factor=args.rt_factor,
                   provision_parallel=args.parallel)   # headline = serial (matches Exp 1); --parallel ablation

    stop_event = asyncio.Event()

    async def _watch():
        while not sim_host.is_done:
            await asyncio.sleep(0.5)
        stop_event.set()

    await asyncio.gather(
        _watch(),
        run_control_loop(policy, stop_event, 1.0, stats, memory=None),
        run_assessment_loop(non_rt, policy, stop_event, args.assessment_interval, stats,
                            window_s=args.window_s, model_settings=model_settings),
    )

    sim    = sim_host.sim
    storms = sim.cfg.traffic.storm_windows()
    out    = _episode_metrics(sim, storms, stats)
    # agentic-only extras (ignored by _agg's _KEYS; stored raw): the judge's peak crowd estimate +
    # reserve for the scheduled event (0 if it never anticipated), token totals, and error count.
    out.update({"att_est": stats.judge_peak_attendance, "reserve_est": stats.judge_peak_reserve,
                "in_tok": stats.llm_input_tokens, "out_tok": stats.llm_output_tokens,
                "errors": stats.non_rt_errors})
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


def _save_payload(results, args, seeds, scenarios, path):
    """Write the run's results to its timestamped checkpoint (called after each system finishes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "seeds": seeds, "scenarios": scenarios,
        "agents": [{"label": l, "slug": s, "mode": m} for l, s, m in AGENTS],
        "provisioning": "parallel" if args.parallel else "serial",
        "systems": results,
    }, indent=2))


async def sweep(args):
    seeds = list(range(1, args.seeds + 1))
    scenarios = _SCENARIOS
    results: dict[str, dict] = {}

    # Resume: reload the newest matching checkpoint and keep systems already finished — but only if
    # it was produced under the SAME seeds+scenarios, else the pooled stats would not be comparable.
    if args.resume and args.ckpt_path.exists():
        prev = json.loads(args.ckpt_path.read_text())
        if prev.get("seeds") == seeds and prev.get("scenarios") == scenarios:
            results = prev.get("systems", {})
            print(f"[sys] resume: {len(results)} system(s) from {args.ckpt_path.name}: {sorted(results)}")
        else:
            print("[sys] resume: checkpoint seeds/scenarios differ — starting fresh")

    def _done(label):
        return label in results and all(scn in results[label] for scn in scenarios)

    def _ckpt():
        if args.save:
            _save_payload(results, args, seeds, scenarios, args.ckpt_path)

    # --- deterministic baselines (fast, virtual time) ---
    for label, factory, c0 in BASELINES:
        if _done(label):
            continue
        results[label] = {}
        for scn in scenarios:
            results[label][scn] = _agg([run_baseline(factory, c0, scn, s, args.parallel) for s in seeds])
            r = results[label][scn]
            print(f"[sys] {label:22s} P={r['P_mean']:.3f} P_surge={r['P_surge_mean']:.3f} "
                  f"benign={r['benign_mean']:.3f} filtered={r['filtered_mean']:.3f} servers={r['servers_mean']:.1f}")
        _ckpt()

    # --- deterministic rules (fast, virtual time, no LLM): the agentic decision tree hardcoded ---
    if not _done(RULE_LABEL):
        results[RULE_LABEL] = {}
        for scn in scenarios:
            results[RULE_LABEL][scn] = _agg([run_rule_based(scn, s, args) for s in seeds])
            r = results[RULE_LABEL][scn]
            print(f"[sys] {RULE_LABEL:22s} P={r['P_mean']:.3f} P_surge={r['P_surge_mean']:.3f} "
                  f"benign={r['benign_mean']:.3f} benign_fp={r['benign_fp_mean']:.3f} "
                  f"filtered={r['filtered_mean']:.3f} servers={r['servers_mean']:.1f}")
        _ckpt()

    # --- agentic arms (slow, real-time, LLM): the two judges carried from Exp 1 ---
    for label, slug, mode in AGENTS:
        if _done(label):
            continue
        model    = resolve_model(slug)
        settings = judge_settings(slug, mode)
        results[label] = {}
        for scn in scenarios:
            rows = []
            for s in seeds:
                try:
                    r = await run_agentic(model, scn, s, args, model_settings=settings)
                except Exception as e:
                    print(f"[sys] {label} {scn} seed={s} ERROR {type(e).__name__}: {e}")
                    continue
                rows.append(r)
                print(f"[sys] {label:22s} seed={s}  P={r['P']:.3f} P_surge={r['P_surge']:.3f} "
                      f"benign={r['benign']:.3f} fp={r['benign_fp']:.3f} filt={r['filtered']:.3f} "
                      f"srv={r['servers']:.1f} llm={r['llm_lat']:.1f}s")
            if rows:
                results[label][scn] = _agg(rows)
        _ckpt()

    _print_table(results, scenarios, seeds, [a[0] for a in AGENTS])
    if args.save:
        print(f"\n  saved -> {args.ckpt_path}")


def _print_table(results, scenarios, seeds, agent_labels):
    order = [b[0] for b in BASELINES] + [RULE_LABEL] + list(agent_labels)
    print("\n" + "=" * 118)
    print(f"SYSTEM COMPARISON  ({len(seeds)} seeds)   P / P_surge / benign / benign-FP / botnet-filtered / avg-servers / latency (mean ± 95% CI)")
    print("  P_surge = event-window resilience (the discriminator); P_bot / full decomposition are in the JSON;")
    print("  benign-FP = benign users dropped by the filter (the rule over-filters the surge; agentic should not);")
    print("  botnet-filtered = deliberate filter drops (the rule + agentic filter; static/Lyapunov = 0);")
    print("  avg-servers = mean online capacity (lower at equal P = same protection, less cost);")
    print("  llm_lat = mean LLM-call time per assessment (agentic only)")
    print("=" * 118)
    for scn in scenarios:
        print(f"\n  --- {scn} ---")
        print(f"  {'system':21s} {'P':>13s} {'P_surge':>9s} {'benign':>13s} {'benign_fp':>10s} "
              f"{'filtered':>13s} {'servers':>12s} {'llm_lat':>8s}")
        for label in order:
            s = results.get(label, {}).get(scn)
            if not s:
                continue
            print(f"  {label:21s} {s['P_mean']:.3f}±{s['P_ci95']:.3f}  "
                  f"{s['P_surge_mean']:>7.3f}  "
                  f"{s['benign_mean']:.3f}±{s['benign_ci95']:.3f}  "
                  f"{s['benign_fp_mean']:>8.3f}  "
                  f"{s['filtered_mean']:.3f}±{s['filtered_ci95']:.3f}  "
                  f"{s['servers_mean']:>5.1f}±{s['servers_ci95']:.1f}  "
                  f"{s['llm_lat_mean']:>7.2f}s")
    print("=" * 118)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Experiment 2 — system comparison: baselines vs agentic")
    p.add_argument("--seeds", type=int, default=5)
    p.add_argument("--rt-factor", type=float, default=1.0, dest="rt_factor")
    p.add_argument("--assessment-interval", type=float, default=5.0, dest="assessment_interval")
    p.add_argument("--window", type=float, default=15.0, dest="window_s",
                   help="telemetry-window seconds the judge sees (15 = validated Exp 1 config)")
    p.add_argument("--parallel", action="store_true",
                   help="parallel provisioning ablation (all pending servers spin up at once). "
                        "Default is SERIAL (one server per delay), matching Exp 1.")
    p.add_argument("--save", action="store_true",
                   help="write results to a timestamped "
                        "experiments/exp2_system_comparison/system_comparison_<timestamp>.json, "
                        "checkpointed after each system")
    p.add_argument("--resume", action="store_true",
                   help="reload the NEWEST matching timestamped checkpoint and skip systems already "
                        "done (same seeds+scenarios). Use with --save to continue a crashed sweep.")
    p.add_argument("--log", nargs="?", const="AUTO", default=None,
                   help="tee output to a file (bare --log auto-names it under exp2_system_comparison/logs/)")
    args = p.parse_args()

    # Route output to a per-run, TIMESTAMPED file so a new run never overwrites a previous one or the
    # blessed system_comparison.json the manuscript cites. --resume continues the newest matching run.
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.resume:
        prior = sorted(_EXP_DIR.glob("system_comparison_[0-9]*.json"))   # timestamps start with the year digit
        args.ckpt_path = prior[-1] if prior else _EXP_DIR / f"system_comparison_{run_stamp}.json"
    else:
        args.ckpt_path = _EXP_DIR / f"system_comparison_{run_stamp}.json"

    _logfile = None
    if args.log is not None:
        log_path = (_LOGS_DIR / f"log_system_comparison_{run_stamp}.txt"
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
