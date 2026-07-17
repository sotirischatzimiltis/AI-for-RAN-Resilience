"""
Model bake-off — pick the storm-judge LLM used for the rest of the campaign.

Runs a set of candidate models as the Non-RT storm judge under IDENTICAL
conditions and scores them on the full metric set, so the winner is chosen on
evidence, not vibes. Every model runs the BARE judge (no additional
functionalities): telemetry-only detection with the forecast + calendar tools
disabled, no learning, no operator intents, no scheduled pre-provisioning event.
That isolates the model's raw storm-vs-noise judgment; the mechanisms it
normally sits on top of are added back only in the later experiments (A-E).

Scenarios: single_storm (utility only, no botnet) and multi_storm_flat (three
IDENTICAL storms with a botnet — exercises detection + filtering fairly).

Metrics reported per model (mean over seeds):
  resilience   — P (per-storm + episode)
  security     — benign-served rate, botnet-blocked rate
  robustness   — non_rt_errors (a model that loops / emits bad output is gated out)
  cost         — LLM input/output tokens, estimated USD, mean assessment latency

Two modes:
  --probe   1 cheap run per model to confirm the API key reaches it and it emits
            a valid PolicyUpdate. ALWAYS run this first — a dead model slug or a
            model that can't do tool-calling / structured output fails here for
            cents instead of wasting the full sweep.
  (default) the full seed sweep over both scenarios, saved to results/.

Reasoning ablation: each CANDIDATES entry lists the reasoning mode(s) to run. A
model listing on+off runs twice (two scorecard rows, rsn column) — this isolates
whether reasoning effort buys anything on a task this light, at what token/latency
cost. Only models whose OpenRouter reasoning toggle is actually honored are run
both ways (per the --probe evidence); the rest run once in a working mode.

Usage (source the shell env for the OpenRouter key first):
    python -m scripts.experiment_model_comparison --probe
    python -m scripts.experiment_model_comparison --seeds 5 --save
"""

import argparse      # parse the --probe / --seeds / --rt-factor CLI flags
import asyncio        # the sim, MCP server, and judge loop all run as async tasks
import json           # write the scorecard to results/model_comparison.json
import logging        # silence uvicorn's shutdown-noise logger at the end
import statistics     # mean / pstdev of P, tokens, latency across seeds
import sys            # sys.path tweak below so 'python -m scripts.…' finds the repo
import time           # wall-clock timing of runs and the total sweep
from pathlib import Path  # locate the repo root and the MC prompt file

# Put the repo root (this file's grandparent) on the import path so the absolute
# imports below resolve when run as a module from anywhere.
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.server import mcp, MCP_HOST, MCP_PORT   # the MCP server exposing get_episode_stats
from scripts.run import resolve_model                   # turn a model string into a pydantic-ai model
# Low-level building blocks — this experiment runs its OWN self-contained episode
# (below) rather than orchestrator.run_episode, so the full pipeline stays decoupled
# from the bake-off. It reuses only the deterministic loops + the judge loop.
from agents.non_rt_agent import build_non_rt_agent, run_assessment_loop  # build the judge + run its loop
from agents.near_rt_control_loop import run_control_loop                 # the deterministic 1 Hz fast loop
from agents.policy import SharedPolicy, EpisodeStats     # judge↔loop handoff + per-episode counters/usage
from runtime import host as sim_host, UP                 # the sim host (owns the episode) + utility params
from sim.metrics import resilience_multi, benign_success_rate, malicious_blocked_rate  # ground-truth scoring

# ---------------------------------------------------------------------------
# Candidate models — MID + SMALL only (the deploy tier; the judge task is light,
# so no frontier). Two clean within-provider size pairs (Google, Qwen) let us see
# size-scaling with provider held fixed; a mixed Anthropic+Tencent pair adds
# provider diversity. OpenRouter slugs; edit freely. The --probe pass tells you
# which are actually reachable before you spend on the full sweep.
# ---------------------------------------------------------------------------
# (tier, modes, slug). `modes` lists the reasoning settings to RUN for this model:
#   "n/a" = plain call (no reasoning param);  "on"/"off" = OpenRouter reasoning enabled/disabled.
# Choices are grounded in the --probe evidence:
#   • gpt-5.4-mini — reasoning toggle demonstrably works → run on+off (the reasoning ablation).
#   • qwen3.7-plus — thinking mode rejects tool_choice=required (400 error) and our structured
#     output NEEDS it; also thinking may be its default, so we PIN it to "off" (which works).
#   • claude-haiku-4.5 — thinking WON'T engage via OpenRouter: enabled/effort/max_tokens
#     budget all left tokens unchanged (probe: on==off), because Anthropic thinking is
#     incompatible with the tool_choice=required our structured output forces (same root
#     cause as qwen's 400, but silent). Run ONCE in default mode.
#   • gemini-flash-lite, gpt-4o-mini — not reasoning models → single plain run.
CANDIDATES = [
    ("small", ["n/a"],       "openrouter:google/gemini-3.1-flash-lite"),
    ("small", ["off"],       "openrouter:qwen/qwen3.7-plus"),
    ("small", ["n/a"],       "openrouter:openai/gpt-4o-mini"),
    ("mid",   ["on", "off"], "openrouter:openai/gpt-5.4-mini"),
    ("mid",   ["n/a"],       "openrouter:anthropic/claude-haiku-4.5"),
]

# OpenRouter prices, USD per 1M tokens (input, output). Confirmed from the model
# pages (2026-07). Used to turn measured tokens into $ / episode. Unknown -> (0,0).
PRICES = {
    "google/gemini-3.1-flash-lite":  (0.25,  1.50),
    "qwen/qwen3.7-plus":             (0.32,  1.28),
    "openai/gpt-4o-mini":            (0.15,  0.60),
    "openai/gpt-5.4-mini":           (0.75,  4.50),
    "anthropic/claude-haiku-4.5":    (1.00,  5.00),
}

# scenario -> t_post (single-storm only; multi is fixed-horizon)
_SCENARIOS = {
    "single_storm":     20.0,
    "multi_storm_flat": None,
}


class _Tee:
    """Duplicate every write to several streams (e.g. the terminal AND a log file),
    so a run is captured to disk without changing any of the print() calls or piping
    through `tee` on the command line. Installed on sys.stdout/stderr in __main__.

    Proxies isatty() and any other stream attribute to the FIRST (real) stream, so
    libraries that introspect the stream — e.g. uvicorn's logging setup calling
    sys.stdout.isatty() — keep working. (Without this the MCP server fails to start
    and every judge tool-call ConnectErrors.)"""
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
        # only reached for attributes not defined above; proxy to the real stream
        return getattr(self._streams[0], name)


def _prevent_sleep():
    """macOS: hold a power assertion for the life of THIS process so a long run isn't
    paused by idle sleep (the system-sleep timer can be as low as 1 min, and a
    low-CPU/network-bound run doesn't keep the system 'busy'). `caffeinate -w <pid>`
    exits automatically when we do, so there is nothing to clean up. No-op elsewhere."""
    if sys.platform != "darwin":
        return
    import os
    import subprocess
    try:
        subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())])
        print("[bakeoff] caffeinate held — system won't idle-sleep during this run")
    except (FileNotFoundError, OSError):
        pass


def _usd(model_str: str, in_tok: float, out_tok: float) -> float:
    slug = model_str.split(":", 1)[1] if ":" in model_str else model_str
    pin, pout = PRICES.get(slug, (0.0, 0.0))
    return (in_tok * pin + out_tok * pout) / 1e6


def build_judge_model(model_str: str, mode: str):
    """Build the judge model, optionally forcing OpenRouter reasoning on/off.

    mode: 'n/a' → passthrough (resolve_model); 'on'/'off' → set OpenRouter's
    unified `reasoning` body param on the model's default settings, so every
    agent.run() inherits it without threading through run_episode. Only applied to
    'openrouter:' models.

    'on'  → {"effort": "high"}: engages real thinking on BOTH OpenAI (gpt-5.4-mini)
            and Anthropic (claude-haiku) — `enabled:true` alone was a no-op for
            Claude (probe: identical tokens), so we use effort, which OpenRouter
            maps to a thinking budget per provider.
    'off' → {"enabled": false}: disables thinking. (For a pure reasoning model this
            is the provider's minimal effort — some can't fully disable it.)
    The probe's token/latency (ON => more output tokens + higher latency) confirms
    whether the toggle actually took effect.
    """
    if mode == "n/a" or not model_str.startswith("openrouter:"):
        return resolve_model(model_str)
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openrouter import OpenRouterProvider
    name = model_str.split(":", 1)[1]
    if mode == "off":
        settings = {"extra_body": {"reasoning": {"enabled": False}}}
    else:  # on — engage thinking. Anthropic ignored enabled/effort, so give it an
           # explicit reasoning-token BUDGET; OpenAI already engages via effort.
           # The response max_tokens must exceed the thinking budget or it can't fit.
        is_anthropic = "anthropic/" in name or "claude" in name
        reasoning = {"max_tokens": 2000} if is_anthropic else {"effort": "high"}
        settings = {"max_tokens": 8000, "extra_body": {"reasoning": reasoning}}
    return OpenAIChatModel(name, provider=OpenRouterProvider(), settings=settings)


def select_candidates(models_filter):
    """Optionally restrict CANDIDATES to those whose slug contains any given
    substring (e.g. --models gpt-5.4). None/empty → all candidates."""
    if not models_filter:
        return CANDIDATES
    picked = [c for c in CANDIDATES if any(f in c[2] for f in models_filter)]
    if not picked:
        sys.exit(f"--models {models_filter} matched no candidate slugs: "
                 f"{[c[2].split('/')[-1] for c in CANDIDATES]}")
    return picked


def expand_runs(candidates):
    """Expand each candidate into one run per listed mode. Models with a single mode
    run once; a model listing multiple modes (e.g. gpt-5.4-mini on+off) runs once per
    mode. Returns (tier, mode, slug, run_id); run_id gets a ::reasoning=<mode> suffix
    only when the model has more than one mode, so the variants stay distinct."""
    runs = []
    for tier, modes, slug in candidates:
        multi = len(modes) > 1
        for mode in modes:
            run_id = f"{slug}::reasoning={mode}" if multi else slug
            runs.append((tier, mode, slug, run_id))
    return runs


# Trimmed bare-judge system prompt (telemetry-only detection + filter calibration,
# no anticipation tools) — the frozen prompt under test for every model.
_MC_PROMPT = (Path(__file__).parent.parent / "prompts" / "prompts_mc_non_rt.md").read_text()


async def _bare_judge_run(model_obj, scenario, seed, args) -> dict:
    """Self-contained bare-judge episode — the LLM storm judge over the deterministic
    fast loop, with EVERY add-on off. Deliberately does NOT call
    orchestrator.run_episode, so the full pipeline stays decoupled from this
    experiment. Off: anticipation tools (forecast/calendar), learning, the code-side
    release valve, operator intents. On: only Lyapunov capacity (the base controller)
    + the model's own storm_active / drop calibration. That isolates raw model judgment.
    """
    non_rt = build_non_rt_agent(model_obj, system_prompt=_MC_PROMPT)
    policy = SharedPolicy()
    stats  = EpisodeStats()

    # bare configuration: telemetry-only detection (the MCP tools read these gates)
    sim_host.calendar = []
    sim_host.forecast_enabled = False
    sim_host.calendar_enabled = False
    sim_host.start(scenario=scenario, seed=seed, c_max=16,
                   rt_factor=args.rt_factor, t_post=_SCENARIOS[scenario]) # start the simulation host with the given scenario and seed

    stop_event = asyncio.Event() # shared flag that means "the episode is done" (set by the sim host, read by the loops)

    async def _watch(): # coroutine that checks the sim host's is_done flag every 0.5 seconds, and sets the stop_event when done
        while not sim_host.is_done:
            await asyncio.sleep(0.5)
        stop_event.set() 

    await asyncio.gather( # run the three coroutines concurrently: the sim host's watch, the control loop, and the assessment loop
        _watch(),
        run_control_loop(policy, stop_event, 1.0, stats, memory=None, release_valve=False),
        run_assessment_loop(non_rt, policy, stop_event, args.assessment_interval, stats,
                            window_s=args.window_s),
    )

    # ground-truth metrics, computed once at episode end
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
        "non_rt_assessments":     stats.non_rt_assessments,
        "non_rt_errors":          stats.non_rt_errors,
        "llm_requests":           stats.llm_requests,
        "llm_input_tokens":       stats.llm_input_tokens,
        "llm_output_tokens":      stats.llm_output_tokens,
        "mean_assessment_latency_s": round(stats.llm_latency_s / max(1, stats.non_rt_assessments), 2),
    }


async def probe(args) -> None:
    """One cheap run per model to confirm reachability + valid structured output.
    Each run is a real single-seed single_storm episode, so the end-of-run summary
    doubles as an early (indicative) model comparison + a reasoning on/off check.
    NOTE: single_storm has NO botnet, so botnet-blocked isn't exercised here — that
    arrives with multi_storm_flat in the full sweep."""
    runs = expand_runs(select_candidates(args.models))
    print(f"[probe] Validating {len(runs)} run(s) — single_storm, seed=1, short horizon.\n")
    rows = []
    for tier, mode, slug, _run_id in runs:
        row = {"tier": tier, "mode": mode, "slug": slug, "status": "FAIL", "err": ""}
        try:
            model_obj = build_judge_model(slug, mode)
            t0 = time.monotonic()
            r = await _bare_judge_run(model_obj, "single_storm", 1, args)
            dt = time.monotonic() - t0
            reachable = r["llm_requests"] > 0 and r["non_rt_errors"] < r["non_rt_assessments"]
            row.update(status="OK" if reachable else "SUSPECT",
                       P=r["final_P"], benign=r["benign_success_rate"],
                       assess=r["non_rt_assessments"], errors=r["non_rt_errors"],
                       tin=r["llm_input_tokens"], tout=r["llm_output_tokens"], lat=dt)
            print(f"[probe] {row['status']:7s} [{tier:5s} rsn={mode:4s}] {slug.split(':')[-1]:38s}  "
                  f"assess={r['non_rt_assessments']} errors={r['non_rt_errors']} "
                  f"tok={r['llm_input_tokens']}/{r['llm_output_tokens']} {dt:.0f}s")
        except Exception as e:
            row["err"] = f"{type(e).__name__}: {str(e)[:70]}"
            print(f"[probe] FAIL    [{tier:5s} rsn={mode:4s}] {slug.split(':')[-1]:38s}  {row['err']}")
        rows.append(row)

    _print_probe_summary(rows)


def _print_probe_summary(rows) -> None:
    ok = [r for r in rows if r["status"] == "OK"]
    print("\n" + "=" * 94)
    print(f"PROBE SUMMARY  ({len(ok)}/{len(rows)} runs OK)  — single seed, single_storm: INDICATIVE only")
    print("=" * 94)
    print(f"  {'model':30s} {'rsn':>5s} {'status':>7s} {'P':>6s} {'benign':>7s} "
          f"{'err':>4s} {'in/out tok':>13s} {'lat_s':>6s}")
    for r in rows:
        name = r["slug"].split("/")[-1]
        if r["status"] == "FAIL":
            print(f"  {name:30s} {r['mode']:>5s} {'FAIL':>7s}   {r['err']}")
            continue
        toks = f"{r['tin']}/{r['tout']}"
        print(f"  {name:30s} {r['mode']:>5s} {r['status']:>7s} {r['P']:>6.3f} {r['benign']:>7.3f} "
              f"{r['errors']:>4d} {toks:>13s} {r['lat']:>6.0f}")

    # reasoning on vs off — did the toggle actually change token use / latency?
    print("\n  reasoning on vs off (toggle sanity check):")
    by_slug: dict[str, dict] = {}
    for r in rows:
        if r["status"] != "FAIL" and r["mode"] in ("on", "off"):
            by_slug.setdefault(r["slug"], {})[r["mode"]] = r
    pairs = 0
    for slug, d in by_slug.items():
        if "on" in d and "off" in d:
            pairs += 1
            on_t, off_t = d["on"]["tout"], d["off"]["tout"]
            dtok = on_t - off_t
            dlat = d["on"]["lat"] - d["off"]["lat"]
            # genuine thinking shows a SUBSTANTIAL output-token jump, not a few tokens
            engaged = dtok > 100 or on_t >= 1.3 * max(1, off_t)
            verdict = "toggle WORKS" if engaged else "NO real effect (thinking didn't engage)"
            print(f"    {slug.split('/')[-1]:28s} out-tok Δ={dtok:+d} ({on_t}/{off_t}), "
                  f"lat Δ={dlat:+.0f}s  ->  {verdict}")
    if pairs == 0:
        print("    (no reasoning on/off pair completed)")

    print("=" * 94)
    bad = sorted({r["slug"] for r in rows if r["status"] == "FAIL"})
    if bad:
        print(f"  FIX these slugs before the full sweep: {bad}")
    else:
        print("  All runs reachable — ready for the full sweep.")


async def sweep(args) -> None:
    seeds = list(range(1, args.seeds + 1))
    scenarios = list(_SCENARIOS) if args.scenario == "both" else [args.scenario]
    results: dict[str, dict] = {}
    t_start = time.monotonic()

    for tier, mode, slug, run_id in expand_runs(select_candidates(args.models)):
        model_obj = build_judge_model(slug, mode)
        per_scn: dict[str, dict] = {}
        for scenario in scenarios:
            P, benign, blocked, errs = [], [], [], []
            in_tok, out_tok, lat = [], [], []
            for seed in seeds:
                try:
                    r = await _bare_judge_run(model_obj, scenario, seed, args)
                except Exception as e:
                    print(f"[bakeoff] {run_id} {scenario} seed={seed} ERROR {type(e).__name__}: {e}")
                    continue
                P.append(r["final_P"]); benign.append(r["benign_success_rate"])
                blocked.append(r["malicious_blocked_rate"]); errs.append(r["non_rt_errors"])
                in_tok.append(r["llm_input_tokens"]); out_tok.append(r["llm_output_tokens"])
                lat.append(r["mean_assessment_latency_s"])
                print(f"[bakeoff] [{tier:5s} rsn={mode:4s}] {slug.split(':')[-1]:30s} {scenario:16s} "
                      f"seed={seed}  P={r['final_P']:.3f} benign={r['benign_success_rate']:.3f} "
                      f"blocked={r['malicious_blocked_rate']:.3f} err={r['non_rt_errors']} "
                      f"tok={r['llm_input_tokens']}/{r['llm_output_tokens']}")
            if not P:
                continue
            mean_in, mean_out = statistics.mean(in_tok), statistics.mean(out_tok)
            _sd = lambda v: statistics.pstdev(v) if len(v) > 1 else 0.0   # noqa: E731
            per_scn[scenario] = {
                "P_mean": statistics.mean(P),           "P_std": _sd(P),
                "benign_mean": statistics.mean(benign), "benign_std": _sd(benign),
                "blocked_mean": statistics.mean(blocked), "blocked_std": _sd(blocked),
                "errors_total": sum(errs),
                "in_tokens_mean": mean_in, "out_tokens_mean": mean_out,
                "usd_per_episode": _usd(slug, mean_in, mean_out),
                "mean_latency_s": statistics.mean(lat),
            }
        results[run_id] = {"tier": tier, "reasoning": mode, "slug": slug, "scenarios": per_scn}

    elapsed = time.monotonic() - t_start
    _print_scorecard(results, scenarios, seeds, elapsed)

    if args.save:
        out = Path(__file__).parent.parent / "results" / "model_comparison.json"
        out.parent.mkdir(exist_ok=True)
        # MERGE into any existing file so partial runs (e.g. gpt-5.4 first, then the
        # rest) accumulate into one JSON. Keep --seeds consistent across partial runs
        # or the merged per-model stats won't be comparable.
        if out.exists():
            prev = json.loads(out.read_text())
            merged = prev.get("models", {})
            merged.update(results)                              # new/re-run models overwrite
            all_scn = sorted(set(prev.get("scenarios", [])) | set(scenarios))
            if prev.get("seeds") != seeds:
                print(f"  [warn] existing file used seeds={prev.get('seeds')}, "
                      f"this run seeds={seeds} — merged stats mix seed counts.")
        else:
            merged, all_scn = results, scenarios
        out.write_text(json.dumps({"seeds": seeds, "scenarios": all_scn, "models": merged}, indent=2))
        print(f"\n  saved -> {out}  ({len(merged)} model-runs total in file)")


def _print_scorecard(results, scenarios, seeds, elapsed) -> None:
    print("\n" + "=" * 100)
    print(f"MODEL BAKE-OFF SCORECARD  ({len(seeds)} seeds, bare judge, scenarios={scenarios})")
    print("=" * 100)
    for scenario in scenarios:
        print(f"\n  --- {scenario} ---")
        print(f"  {'model':40s} {'rsn':>6s} {'P':>12s} {'benign':>7s} {'blocked':>7s} {'err':>4s} "
              f"{'$/ep':>8s} {'lat_s':>6s}")
        rows = [(m, d) for m, d in results.items() if scenario in d["scenarios"]]
        rows.sort(key=lambda md: md[1]["scenarios"][scenario]["P_mean"], reverse=True)
        for _, d in rows:
            s = d["scenarios"][scenario]
            print(f"  {d['slug'].split(':')[-1]:40s} {d['reasoning']:>6s} {s['P_mean']:.3f}±{s['P_std']:.3f} "
                  f"{s['benign_mean']:>7.3f} {s['blocked_mean']:>7.3f} {s['errors_total']:>4d} "
                  f"{s['usd_per_episode']:>8.4f} {s['mean_latency_s']:>6.1f}")
    print(f"\n  wall time: {elapsed:.0f}s")
    print("=" * 100)
    print("  Pick: gate out any model with errors > 0; then rank on P, break ties on")
    print("  blocked-rate, then $/episode and latency. Report the full table in the paper.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="LLM bake-off for the storm judge")
    p.add_argument("--probe", action="store_true", help="cheap reachability check, 1 run per model")
    p.add_argument("--scenario", default="both",
                   choices=["both", "single_storm", "multi_storm_flat"])
    p.add_argument("--seeds", type=int, default=5)
    p.add_argument("--models", nargs="*", default=None,
                   help="run only candidates whose slug contains these substrings "
                        "(e.g. --models gpt-5.4). Default: all. --save merges into the file.")
    p.add_argument("--rt-factor", type=float, default=1.0, dest="rt_factor",
                   help="sim seconds per wall second; 1.0 = real time (LLM latency realistically paced)")
    p.add_argument("--assessment-interval", type=float, default=5.0, dest="assessment_interval",
                   help="seconds between judge assessments")
    p.add_argument("--window", type=float, default=15.0, dest="window_s",
                   help="telemetry-window seconds the judge sees each assessment")
    p.add_argument("--save", action="store_true", help="cache results to results/model_comparison.json")
    p.add_argument("--log", nargs="?", const="AUTO", default=None,
                   help="tee all output to a file. Bare --log auto-names it "
                        "results/log_model_comparison_<timestamp>.txt; or give a path.")
    args = p.parse_args()

    # Optional: mirror all stdout/stderr to a log file for the whole run.
    _logfile = None
    if args.log is not None:
        from datetime import datetime
        if args.log == "AUTO":
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = Path(__file__).parent.parent / "results" / f"log_model_comparison_{stamp}.txt"
        else:
            log_path = Path(args.log)
        log_path.parent.mkdir(exist_ok=True)
        _logfile = open(log_path, "w")
        sys.stdout = _Tee(sys.__stdout__, _logfile)
        sys.stderr = _Tee(sys.__stderr__, _logfile)
        print(f"[bakeoff] logging this run to {log_path}")

    _prevent_sleep()   # keep the machine awake for the whole run (macOS)

    async def _main():
        print(f"[bakeoff] Starting MCP server on {MCP_HOST}:{MCP_PORT} ...")
        server_task = asyncio.create_task(
            mcp.run_http_async(host=MCP_HOST, port=MCP_PORT, show_banner=False, log_level="warning")
        )
        await asyncio.sleep(1.5)
        try:
            await (probe(args) if args.probe else sweep(args))
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
