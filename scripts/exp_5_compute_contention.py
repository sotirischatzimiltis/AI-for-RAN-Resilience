"""
Experiment 5, Part B — compute-contention arm comparison at one over-limit storm.

Part A (experiments/exp5_compute_contention/exp5_envelope.py) sweeps storm intensity with a fixed,
instantly-provisioned pool and shows the resilience cliff shift LEFT under contention, opening a
"collapse window" the dedicated pool rides out but the shared pool does not. A malicious storm would be
FILTERED and never fill the pool, so Part B instead plants ONE benign SCHEDULED-EVENT surge the judge
must actually carry (event_heavy, the Exp-1 England-v-Brazil crowd, ~279 UEs/s). A realistic single
event caps near the a=0.5 cliff (321), so the lever is contention SEVERITY: we sweep a in {0.5,0.75,1.0}
(matching Part A). The judge reads the calendar, estimates the crowd, and pre-provisions the reserve
using NOMINAL capacity. As a rises the cliff drops below the surge, so that same correct reserve delivers
less effective capacity than planned, the surge overruns it, and benign QoS collapses — the controller
did everything right and still cannot escape contention it cannot observe. Runs the full Exp-2 arm set.

Experiments 1-4 all run the INDEPENDENT-SERVER model (compute_kappa=None): each of the c servers
processes attaches at the full rate mu no matter how many are busy. Real vCU/vDU share a finite
compute pool. Contention model (sim/contention.py): per-attach PROCESSING time is flat below an
occupancy threshold omega_th and then rises linearly, bounded, s(omega)=1+a*(omega-omega_th)/(1-omega_th)
for omega=busy/kappa>omega_th. The pool is sized to the max servers, kappa=c_max. kappa=None recovers
Exp 1-4.

Contention is an EXOGENOUS stressor: the controller plans against the nominal mu (a site cannot
observe the occupancy of a pool it shares with other workloads), so this is a ROBUSTNESS study, not a
blind-vs-aware comparison. Reuses Exp 2's arm definitions and metric decomposition unchanged, so the
arms are byte-for-byte comparable. Standalone: it does not change Exp 1-4.

Output: per (kappa, arm) the full Exp-2 metric set (mean + 95% CI over seeds), plus an OFF-vs-ON table
showing how much resilience each arm loses to the shared pool. The result the collapse window predicts:
every arm that fills the pool takes the same hit — you cannot control your way out of contention.

Usage (source the shell env for the OpenRouter key first):
    python -m scripts.exp_5_compute_contention --seeds 5 --save --log       # OFF vs ON (kappa=c_max)
    python -m scripts.exp_5_compute_contention --seeds 1 --no-llm           # quick deterministic check
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
from scripts.exp_1_model_comparison import _Tee, _prevent_sleep, judge_settings
# Reuse Exp 2's arms, prompt, metric decomposition and aggregation UNCHANGED, so every arm here is
# byte-for-byte the Exp-2 arm and the two experiments are directly comparable. Importing this module
# has no side effects beyond Exp 2's own top-level imports (its CLI is guarded by __main__).
from scripts.exp_2_system_comparison import (
    AGENTS, BASELINES, RULE_LABEL, _SYS_PROMPT,
    _episode_metrics, _agg, _T95,
)
from agents.non_rt_agent import build_non_rt_agent, compose_system_prompt, run_assessment_loop
from agents.near_rt_control_loop import run_control_loop
from agents.rule_based_controller import RuleBasedController
from shared.policy import SharedPolicy, RunStats
from sim.config import SimConfig, open_ran_arch, RRCConfig, event_surge_traffic
from sim.simulator import StormSim
from shared.events import EXP1_EVENT
from runtime import UP, host as sim_host

_EXP_DIR  = Path(__file__).parent.parent / "experiments" / "exp5_compute_contention"
_LOGS_DIR = _EXP_DIR / "logs"

_C_MAX = 16                          # the arms' capacity ceiling; kappa = c_max sizes the shared pool
_SURGE = EXP1_EVENT.surge_peak()     # ~279 UEs/s: the Exp-1 benign event the judge pre-provisions for.
                                     # A realistic single event caps near ~300, just under the a=0.5
                                     # cliff (321), so contention is exposed by the SEVERITY sweep (a):
                                     # once a>=0.75 the cliff drops below the surge and the same correct
                                     # reserve is eroded. See Part A envelope.
# event traffic geometry MUST match the host's "event_heavy" branch so agentic and deterministic
# arms see the identical scenario. lead=140 gives serial provisioning enough runway to bring the
# reserve online before the t=140 surge even for a judge that consults the calendar late (~t=55-80);
# post=60 is ample since the queue drains in seconds once load falls back to baseline.
_EV_KW = dict(surge_rate=_SURGE, lead=140.0, surge_dur=60.0, post=60.0)


def _ev_traffic():
    """The benign scheduled-event surge Part B runs (no botnet). Malicious load would be FILTERED and
    never fill the pool, so contention needs LEGITIMATE load the controller must actually carry."""
    return event_surge_traffic(**_EV_KW)


# Agentic-only fields _episode_metrics adds but Exp 2's _agg does NOT aggregate (its _KEYS omit them).
# We aggregate them here so the judge's crowd estimate and reserve are PERSISTED in the checkpoint,
# not just recoverable from the log. Exp 2 is untouched.
_EST_KEYS = ["att_est", "reserve_est", "in_tok", "out_tok", "errors"]


def _agg_estimates(rows: list[dict]) -> dict:
    """Mean / std / 95% CI (Student-t, matching _agg) + raw per-seed values for each estimate field
    present on the agentic rows. Keys absent (deterministic arms) are skipped."""
    out = {}
    for k in _EST_KEYS:
        vals = [r[k] for r in rows if k in r]
        if not vals:
            continue
        n = len(vals)
        m = statistics.mean(vals)
        sd = statistics.stdev(vals) if n > 1 else 0.0
        ci = _T95.get(n, 1.96) * sd / math.sqrt(n) if n > 1 else 0.0
        out[f"{k}_mean"] = m; out[f"{k}_std"] = sd
        out[f"{k}_ci95"] = ci; out[f"{k}_seeds"] = vals
    return out


# ---------------------------------------------------------------------------
# arm runners — the Exp-2 arms with the shared-compute pool (kappa) and severity (a) threaded in.
# ---------------------------------------------------------------------------
def _run_baseline(factory, c0, seed, parallel, kappa, a) -> dict:
    """A deterministic static/Lyapunov baseline at pool size `kappa` (None = off), severity `a`."""
    cfg = SimConfig(arch=open_ran_arch(), rrc=RRCConfig(t300_ms=1000, max_attempts=5),
                    c0=c0, c_max=_C_MAX, traffic=_ev_traffic(), seed=seed,
                    parallel_provision=parallel, compute_kappa=kappa, compute_slowdown=a)
    sim = StormSim(cfg)
    sim.run(controller=factory())
    return _episode_metrics(sim, sim.cfg.traffic.storm_windows())


def _run_rule(seed, args, kappa, a) -> dict:
    """The calendar-free rule-based controller at pool size `kappa`, severity `a`."""
    cfg = SimConfig(arch=open_ran_arch(), rrc=RRCConfig(t300_ms=1000, max_attempts=5),
                    c0=2, c_max=_C_MAX, traffic=_ev_traffic(), seed=seed,
                    parallel_provision=args.parallel, compute_kappa=kappa, compute_slowdown=a)
    ctrl = RuleBasedController(anticipation=True,
                               assessment_interval=args.assessment_interval, util_p=UP)
    sim = StormSim(cfg)
    sim.run(controller=ctrl)
    return _episode_metrics(sim, sim.cfg.traffic.storm_windows())


async def _run_agentic(model, seed, args, kappa, a, settings) -> dict:
    """One full agentic episode (LLM judge + fast loop + forecast/calendar tools) at pool size
    `kappa`, severity `a`. Mirrors Exp 2's run_agentic exactly, adding contention to the sim host."""
    non_rt = build_non_rt_agent(
        model,
        system_prompt=compose_system_prompt(_SYS_PROMPT, calendar_enabled=True, forecast_enabled=True),
    )
    policy = SharedPolicy()
    stats  = RunStats()

    sim_host.forecast_enabled = True
    sim_host.calendar_enabled = True
    sim_host.start(scenario="event_heavy", storm=_SURGE, seed=seed, c_max=_C_MAX,
                   rt_factor=args.rt_factor, provision_parallel=args.parallel,
                   compute_kappa=kappa, compute_slowdown=a)

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
def _levels(args) -> list[tuple[str, float | None, float]]:
    """Contention axis (label, kappa, a): OFF (independent servers, the Exp 1-4 model) then one
    shared-pool (kappa=c_max) level per severity in --severity. Severity is the lever here because a
    realistic event sits just under the a=0.5 cliff, so contention only bites as a rises."""
    return [("off", None, 0.0)] + [(f"a={a:g}", float(_C_MAX), float(a)) for a in args.severity]


async def sweep(args) -> None:
    seeds  = list(range(1, args.seeds + 1))
    levels = _levels(args)
    # results[level_label][arm_label] = _agg(rows)   (scenario is fixed = event_heavy at the surge)
    results: dict[str, dict] = {}

    if args.resume and args.ckpt_path.exists():
        prev = json.loads(args.ckpt_path.read_text())
        if prev.get("seeds") == seeds and prev.get("levels") == [l for l, _, _ in levels]:
            results = prev.get("systems", {})
            print(f"[cc] resume: {sum(len(v) for v in results.values())} (level,arm) cells "
                  f"from {args.ckpt_path.name}")
        else:
            print("[cc] resume: checkpoint seeds/levels differ — starting fresh")

    def _ckpt():
        if args.save:
            _save_payload(results, args, seeds, levels)

    for level_label, kappa, a in levels:
        results.setdefault(level_label, {})
        cell = results[level_label]
        print(f"\n############  contention: {level_label}  "
              f"({'independent servers' if kappa is None else f'shared pool kappa={kappa:g}, a={a:g}'})  ############")

        # --- deterministic arms (fast, virtual time) ---
        for label, factory, c0 in BASELINES:
            if label in cell:
                continue
            cell[label] = _agg([_run_baseline(factory, c0, s, args.parallel, kappa, a) for s in seeds])
            r = cell[label]
            print(f"[cc] {label:22s} P={r['P_mean']:.3f} P_ev={r['P_bot_mean']:.3f}"
                  f"benign={r['benign_mean']:.3f} servers={r['servers_mean']:.1f}")
        if RULE_LABEL not in cell:
            cell[RULE_LABEL] = _agg([_run_rule(s, args, kappa, a) for s in seeds])
            r = cell[RULE_LABEL]
            print(f"[cc] {RULE_LABEL:22s} P={r['P_mean']:.3f} P_ev={r['P_bot_mean']:.3f}"
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
                        r = await _run_agentic(model, s, args, kappa, a, settings)
                    except Exception as e:
                        print(f"[cc] {label} {level_label} seed={s} ERROR {type(e).__name__}: {e}")
                        continue
                    rows.append(r)
                    print(f"[cc] {label:22s} seed={s}  P={r['P']:.3f} P_ev={r['P_bot']:.3f} "
                          f"benign={r['benign']:.3f} srv={r['servers']:.1f} "
                          f"crowd_est={r['att_est']} reserve={r['reserve_est']:.0f} "
                          f"llm={r['llm_lat']:.1f}s")
                if rows:
                    cell[label] = {**_agg(rows), **_agg_estimates(rows)}   # persist crowd est + reserve
                _ckpt()

    _print_contention(results, levels, seeds, args.no_llm)
    if args.save:
        print(f"\n  saved -> {args.ckpt_path}")


def _save_payload(results, args, seeds, levels) -> None:
    args.ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    args.ckpt_path.write_text(json.dumps({
        "seeds": seeds,
        "levels": [l for l, _, _ in levels],
        "kappa":  {l: k for l, k, _ in levels},
        "severity": {l: a for l, _, a in levels},
        "scenario": f"event_heavy@{_SURGE:g}",
        "provisioning": "parallel" if args.parallel else "serial",
        "agents": [{"label": l, "slug": s, "mode": m} for l, s, m in AGENTS],
        "systems": results,
    }, indent=2))


def _print_contention(results, levels, seeds, no_llm) -> None:
    """Per arm, one row per contention level with P / P_ev / benign / servers and dP vs OFF —
    so the reader sees how much resilience the shared compute pool removes from each controller.
    NOTE: event_heavy has a SINGLE storm window (the benign surge, index 0), so P_bot holds that
    window's resilience; the two-window P_surge (index 1) does not exist here and is not reported."""
    arm_order = [b[0] for b in BASELINES] + [RULE_LABEL] + ([] if no_llm else [a[0] for a in AGENTS])
    off = results.get("off", {})
    print("\n" + "=" * 92)
    print(f"COMPUTE-CONTENTION SENSITIVITY  ({len(seeds)} seeds)   "
          f"P / P_ev / benign / avg-servers (mean ± 95% CI), dP vs OFF")
    print("  OFF = independent servers (the Exp 1-4 model); a = shared-pool severity (kappa=c_max)")
    print("  P_ev = benign event-window resilience (the single storm window)")
    print("=" * 92)
    for arm in arm_order:
        print(f"\n  --- {arm} ---")
        print(f"  {'contention':12s} {'P':>13s} {'dP_off':>8s} {'P_ev':>9s} "
              f"{'benign':>13s} {'servers':>12s}")
        p_off = off.get(arm, {}).get("P_mean")
        for level_label, _, _ in levels:
            s = results.get(level_label, {}).get(arm)
            if not s:
                continue
            dP = "" if (p_off is None or level_label == "off") else f"{s['P_mean'] - p_off:+.3f}"
            print(f"  {level_label:12s} {s['P_mean']:.3f}±{s['P_ci95']:.3f}  {dP:>8s} "
                  f"{s['P_bot_mean']:>7.3f}  {s['benign_mean']:.3f}±{s['benign_ci95']:.3f}  "
                  f"{s['servers_mean']:>5.1f}±{s['servers_ci95']:.1f}")
    print("=" * 92)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Experiment 5 — compute-contention sensitivity")
    p.add_argument("--seeds", type=int, default=5)
    p.add_argument("--severity", nargs="+", type=float, default=[0.5, 0.75, 1.0],
                   help="shared-pool contention severity/severities a to test ON (kappa fixed = "
                        f"c_max={_C_MAX}). Default 0.5 0.75 1.0 (matches the Part-A envelope). OFF "
                        "(no contention) is always included as the baseline.")
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

    bad = [a for a in args.severity if a < 0]
    if bad:
        sys.exit(f"[cc] severity values {bad} must be non-negative (typically 0.5-1.0).")

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
