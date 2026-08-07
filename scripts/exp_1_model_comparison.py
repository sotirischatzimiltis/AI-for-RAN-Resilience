"""
Experiment 1 — Non-RT-agent LLM comparison: CHOOSE the judge model for every later experiment.

Run each candidate LLM as the Non-RT storm judge inside the COMPLETE system — Lyapunov fast loop
+ malicious filter + the forecast/calendar anticipation tools — on ONE deliberately tricky
scenario, and pick the best. The scenario (config.botnet_event_traffic) has two storms of
comparable arrival VOLUME but opposite nature, so a good score demands reasoning, not
pattern-matching:
  1. a MALICIOUS botnet storm with a RAMP onset — get_forecast catches the unscheduled climb, so
     the judge should filter it and add headroom;
  2. a BENIGN real-event surge with a STEP onset, sized to EXP1_EVENT's true attendance
     (England v Brazil at Wembley — 90k venue, NOT sold out yet 93% full). A step overruns a
     reactive loop, so the ONLY way to serve it is to pre-provision from get_calendar — which
     means the judge must ESTIMATE the crowd behind the named event and size the reserve, WITHOUT
     filtering it. We score that estimate against ground truth (the "how well does it reason" axis).

This module is SELF-CONTAINED: it owns the full-system per-episode runner (`run_agentic`), the
aggregation, the scenario, and the model roster — nothing it needs vanishes when a downstream
experiment file is deleted.

Each model runs at pinned temperature 0 for reproducibility, except reasoning-ON variants (which
providers force to their default) — per-model settings are threaded to each agent.run() call.

The default judge prompt is `prompts/non_rt_agent_system_prompt.md` (the scheduled-reserve prompt,
formerly `_v3`), so no `--prompt` flag is needed; pass one only to A/B an archived version.

Every saved run is written to a TIMESTAMPED file (`model_comparison[_<tag>]_<stamp>.json`) so runs
never overwrite each other; the blessed result the manuscript cites is hand-copied to
`experiments/exp1_model_comparison/model_comparison.json` (the only JSON tracked in git).

Usage (source the shell env for the OpenRouter key first):
    # authoritative run (its result was blessed as model_comparison.json)
    python -m scripts.exp_1_model_comparison --serial --seeds 5 --save --resume --log
    python -m scripts.exp_1_model_comparison --seeds 5 --save --resume --log
    python -m scripts.exp_1_model_comparison --seeds 1 --models gemini-3.1        # smoke test
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
# Full-system building blocks — the agentic run is SELF-CONTAINED (drives the fast loop + judge
# loop directly; never touches the Orchestrator / run_episode).
from agents.non_rt_agent import build_non_rt_agent, compose_system_prompt, run_assessment_loop
from agents.near_rt_control_loop import run_control_loop
from agents.rule_based_controller import RuleBasedController, ShadowRunner
from shared.policy import SharedPolicy, RunStats
from sim.config import single_storm_traffic, mixed_storm_traffic
from sim.metrics import (resilience_multi, benign_success_rate, benign_false_positive_rate,
                        malicious_blocked_rate, malicious_filtered_rate, avg_servers,
                        resilience_efficiency, per_storm_benign_served, utility_decomposition,
                        utility, utility_parts, recovery_report)
from shared.events import EXP1_EVENT
from runtime import UP, host as sim_host, scenario_calendar, LLM_COMPARE

_EXP_DIR   = Path(__file__).parent.parent / "experiments" / "exp1_model_comparison"
_LOGS_DIR  = _EXP_DIR / "logs"        # per-run results: model_comparison[_<tag>]_<timestamp>.json (blessed copy = model_comparison.json)
_TRACE_DIR = _EXP_DIR / "reasoning"   # per-episode reasoning traces (one JSONL per model x seed)

REQUEST_TIMEOUT_S = 60.0
PINNED_TEMP       = 0.0   # greedy decoding for reproducibility (matches the deployed judge)

# ONE scenario: a malicious botnet storm (RAMP -> get_forecast catches it -> filter) followed by
# a benign real-event surge (STEP, sized to EXP1_EVENT's attendance -> get_calendar names it ->
# the judge must estimate the crowd and pre-provision the reserve). The two storms have comparable
# arrival volume, so the judge must REASON (tools), not pattern-match on load.
_SCENARIOS = [LLM_COMPARE]

# Full-system judge prompt (storm detection + filter + anticipation via forecast/calendar).
# --prompt overrides this per run so two prompt versions can be A/B-compared.
_DEFAULT_PROMPT = Path(__file__).parent.parent / "prompts" / "non_rt_agent_system_prompt.md"
_SYS_PROMPT = _DEFAULT_PROMPT.read_text()

# ---------------------------------------------------------------------------------------------
# Candidate roster + prices (canonical home; the retired bare-judge script keeps its own copy).
# (tier, modes, slug); modes = reasoning settings to RUN: "n/a" plain, "on"/"off" toggle.
CANDIDATES = [
    ("small", ["n/a"],       "openrouter:google/gemini-3.1-flash-lite"),
    ("small", ["off"],       "openrouter:qwen/qwen3.7-plus"),
    ("small", ["n/a"],       "openrouter:openai/gpt-4o-mini"),
    ("mid",   ["on", "off"], "openrouter:openai/gpt-5.4-mini"),
    ("mid",   ["n/a"],       "openrouter:anthropic/claude-haiku-4.5"),
]

# OpenRouter prices, USD per 1M tokens (input, output). Unknown -> (0,0).
PRICES = {
    "google/gemini-3.1-flash-lite":  (0.25,  1.50),
    "qwen/qwen3.7-plus":             (0.32,  1.28),
    "openai/gpt-4o-mini":            (0.15,  0.60),
    "openai/gpt-5.4-mini":           (0.75,  4.50),
    "anthropic/claude-haiku-4.5":    (1.00,  5.00),
}

# the per-episode metrics we track. att_est / reserve_est = the judge's peak crowd estimate and the
# reserve it sized for the event (scored against EXP1_EVENT's ground truth — the reasoning signal).
_KEYS = ["P", "benign", "benign_fp", "filtered", "blocked", "servers", "eff", "llm_lat", "asmt_lat",
         "att_est", "reserve_est",
         # P decomposition (diagnostic): per-window timestamps + absorption/adaptation/trec + event split
         "t0_bot", "td_bot", "tr_bot", "absorb_bot", "adapt_bot", "trec_bot",
         "t0_ev", "td_ev", "tr_ev", "absorb_ev", "adapt_ev", "trec_ev",
         "rho_ev", "uA_ev", "uB_ev"]

# Student-t 97.5th percentile by SAMPLE SIZE n (df=n-1); 1.96 fallback for large n.
_T95 = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571,
        7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262}


def select_candidates(models_filter):
    """Optionally restrict CANDIDATES to those whose slug contains any given substring
    (e.g. --models gpt-5.4). None/empty → all candidates."""
    if not models_filter:
        return CANDIDATES
    picked = [c for c in CANDIDATES if any(f in c[2] for f in models_filter)]
    if not picked:
        sys.exit(f"--models {models_filter} matched no candidate slugs: "
                 f"{[c[2].split('/')[-1] for c in CANDIDATES]}")
    return picked


def expand_runs(candidates):
    """Expand each candidate into one run per listed mode. Single-mode models run once; a
    model listing multiple modes (e.g. gpt-5.4-mini on+off) runs once per mode. Returns
    (tier, mode, slug, run_id); run_id gets a ::reasoning=<mode> suffix only for multi-mode."""
    runs = []
    for tier, modes, slug in candidates:
        multi = len(modes) > 1
        for mode in modes:
            run_id = f"{slug.split(':',1)[1]}::reasoning={mode}" if multi else slug.split(":", 1)[1]
            runs.append((tier, mode, slug, run_id))
    return runs


def judge_settings(slug: str, mode: str) -> dict:
    """Per-call LLM settings for one roster run (threaded into each agent.run()). 'n/a'/'off'
    pin temperature for reproducibility; 'on' engages thinking and leaves temperature at the
    provider default (reasoning models reject an explicit one). Reasoning goes through
    OpenRouter's unified `reasoning` body param."""
    base = {"timeout": REQUEST_TIMEOUT_S}
    if mode == "n/a":
        return {**base, "temperature": PINNED_TEMP}
    if mode == "off":
        return {**base, "temperature": PINNED_TEMP, "extra_body": {"reasoning": {"enabled": False}}}
    # on — engage thinking. Anthropic needs an explicit token budget; OpenAI engages via effort.
    is_anthropic = "anthropic/" in slug or "claude" in slug
    reasoning = {"max_tokens": 2000} if is_anthropic else {"effort": "high"}
    return {**base, "max_tokens": 8000, "extra_body": {"reasoning": reasoning}}


def _episode_cost_usd(slug: str, in_tok: int, out_tok: int) -> float:
    """USD for one episode's tokens at the model's OpenRouter price (per 1M tokens)."""
    name = slug.split(":", 1)[1] if ":" in slug else slug
    p_in, p_out = PRICES.get(name, (0.0, 0.0))
    return in_tok / 1e6 * p_in + out_tok / 1e6 * p_out


# ---------------------------------------------------------------------------------------------
# Logging + no-sleep helpers (owned here so exp_1 depends on no sibling experiment file).
class _Tee:
    """Fan every write out to several streams (terminal + log file); proxies stream attrs to
    the first (real) stream so uvicorn's isatty() checks keep working."""
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
    """macOS: hold a power assertion for the life of THIS process so a long run isn't paused by
    idle sleep. `caffeinate -w <pid>` exits automatically when we do. No-op elsewhere."""
    if sys.platform != "darwin":
        return
    import os
    import subprocess
    try:
        subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())])
        print("[exp1] caffeinate held — system won't idle-sleep during this run")
    except (FileNotFoundError, OSError):
        pass


# ---------------------------------------------------------------------------------------------
def _traffic(scenario):
    """Traffic config matching what the agentic runs build."""
    if scenario == "single_storm":
        return single_storm_traffic(t_post=20.0)
    return mixed_storm_traffic(ramped=("ramp" in scenario), increasing=("inc" in scenario))


async def run_agentic(model, scenario, seed, args, model_settings=None) -> dict:
    """One self-contained agentic episode — the LLM storm judge over the deterministic fast
    loop, FULL system enabled: Lyapunov capacity, the judge's storm_active / drop filter, and
    (unless --no-anticipation) the forecast + calendar tools so the judge can pre-provision
    ahead of a surge. Off: learning and operator intents. `model_settings` is forwarded to each
    agent.run() (None -> the judge's pinned default)."""
    antic = not args.no_anticipation
    non_rt = build_non_rt_agent(
        model,
        system_prompt=compose_system_prompt(getattr(args, "system_prompt", _SYS_PROMPT),
                                            calendar_enabled=antic, forecast_enabled=antic),
    )
    policy = SharedPolicy()
    stats  = RunStats()

    # start() owns the calendar: registers events for the benign surge and clears it for the
    # storm scenarios, so we don't touch sim_host.calendar here.
    sim_host.forecast_enabled = antic
    sim_host.calendar_enabled = antic
    sim_host.start(scenario=scenario, seed=seed, c_max=16, rt_factor=args.rt_factor,
                   t_post=(20.0 if scenario == "single_storm" else None),
                   provision_parallel=not args.serial)

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
    storms  = []
    per_storm = []      # P's own components per window (absorption/adaptation/trec) — already computed
    decomp    = []      # utility split per window (uA capacity-margin, uB queue-health, rho) — diagnostic
    try:
        storms = sim.cfg.traffic.storm_windows()
        rm     = resilience_multi(sim.telemetry, sim.mu_single, UP, storms)
        final_P   = rm["P_episode"]
        per_storm = rm["per_storm"]
        decomp    = utility_decomposition(sim.telemetry, sim.mu_single, UP, storms)
    except Exception:
        pass

    # storm windows for botnet_event are [botnet, event]; pull P's OWN components (absorption over
    # [t0,td], adaptation over [td,tr], trec) plus the timestamps (t0, td, tr=t0+recovery_time) for
    # each, so the summary shows the FULL P breakdown per window and WHY it lands there (event-surge
    # utility split uA/uB/rho). Reporting only — P itself is unchanged.
    def _win(seq, i, key, default):
        return seq[i][key] if len(seq) > i else default
    t0_bot = _win(per_storm, 0, "t0", 60.0);  td_bot = _win(per_storm, 0, "td", 120.0)
    t0_ev  = _win(per_storm, 1, "t0", 240.0); td_ev  = _win(per_storm, 1, "td", 300.0)
    tr_bot = t0_bot + _win(per_storm, 0, "recovery_time", 0.0)   # recovery timestamp = t0 + recovery_time
    tr_ev  = t0_ev  + _win(per_storm, 1, "recovery_time", 0.0)
    absorb_bot = _win(per_storm, 0, "absorption", 1.0); adapt_bot = _win(per_storm, 0, "adaptation", 1.0)
    trec_bot   = _win(per_storm, 0, "trec", 1.0)
    absorb_ev  = _win(per_storm, 1, "absorption", 1.0); adapt_ev  = _win(per_storm, 1, "adaptation", 1.0)
    trec_ev    = _win(per_storm, 1, "trec", 1.0)
    rho_ev     = _win(decomp, 1, "rho", 0.0)
    uA_ev      = _win(decomp, 1, "uA", 1.0)
    uB_ev      = _win(decomp, 1, "uB", 1.0)

    # RECOVERY DIAGNOSTIC (read-only): the detector's internals per window (u_des, target, whether it
    # confirmed recovery, the best post-storm utility reached) + a per-sample utility trace, so a
    # 'not recovered' verdict (tr censored at episode end) can be checked against the [Fast] logs.
    recov, util_trace = [], []
    try:
        recov = recovery_report(sim.telemetry, sim.mu_single, UP, storms)
        for s in sim.telemetry:
            uA, uB = utility_parts(s, sim.mu_single, UP)
            util_trace.append({"t": round(s.t, 1), "lam": round(s.lam_current, 1),
                               "c_online": s.c_online, "queue": s.queue_len,
                               "uA": round(uA, 4), "uB": round(uB, 4),
                               "u": round(utility(s, sim.mu_single, UP), 4)})
    except Exception:
        pass
    st  = sim.stats
    srv = avg_servers(sim.telemetry)
    n   = max(1, stats.non_rt_assessments)
    out = {"P": round(final_P, 4), "benign": benign_success_rate(st),
           # benign served WITHIN each storm window (botnet then event, for botnet_event) — splits the
           # blended episode figure so we see which window starves. Reported for every experiment.
           "benign_per_storm": per_storm_benign_served(sim.telemetry, storms),
           "benign_fp": benign_false_positive_rate(st),   # benign dropped by the filter (over-filtering collateral)
           "filtered": malicious_filtered_rate(st), "blocked": malicious_blocked_rate(st),
           "servers": srv,
           "eff": resilience_efficiency(final_P, srv, 16),
           # P DECOMPOSITION (diagnostic — reporting only, P itself unchanged). Full per-window
           # breakdown: timestamps t0/td/tr, then absorption ([t0,td]) + adaptation ([td,tr]) + trec;
           # plus the event-surge utility split (rho, uA capacity-margin, uB queue-health) — low uA +
           # high uB + rho~1 = surge served but at thin headroom, not starvation.
           "t0_bot": t0_bot, "td_bot": td_bot, "tr_bot": round(tr_bot, 1),
           "absorb_bot": round(absorb_bot, 4), "adapt_bot": round(adapt_bot, 4), "trec_bot": round(trec_bot, 4),
           "t0_ev": t0_ev, "td_ev": td_ev, "tr_ev": round(tr_ev, 1),
           "absorb_ev": round(absorb_ev, 4), "adapt_ev": round(adapt_ev, 4), "trec_ev": round(trec_ev, 4),
           "rho_ev": round(rho_ev, 4), "uA_ev": round(uA_ev, 4), "uB_ev": round(uB_ev, 4),
           "llm_lat": round(stats.llm_latency_s / n, 3),
           "asmt_lat": round(stats.assessment_latency_s / n, 3),
           # the judge's peak crowd estimate + reserve for the scheduled event (0 if it never anticipated)
           "att_est": stats.judge_peak_attendance, "reserve_est": stats.judge_peak_reserve,
           "in_tok": stats.llm_input_tokens, "out_tok": stats.llm_output_tokens,
           "assessments": stats.non_rt_assessments,   # judge calls this episode (varies with latency)
           "errors": stats.non_rt_errors,
           "traces": stats.traces,                     # per-assessment reasoning trace (dumped per episode)
           "recovery": recov, "util_trace": util_trace}  # recovery internals + per-sample utility (dumped per episode)
    if shadow is not None:
        out["shadow"] = shadow.agreement()
    return out


def _agg(rows: list[dict]) -> dict:
    """Aggregate per-episode metric dicts into mean, sample std, 95% CI (Student-t), and the
    raw per-seed array for each key, so every statistic is reproducible from what's stored."""
    out = {}
    for k in _KEYS:
        vals = [r[k] for r in rows]
        n = len(vals)
        m = statistics.mean(vals)
        sd = statistics.stdev(vals) if n > 1 else 0.0
        # 95% CI half-width. A Student-t interval only makes sense at n>=3 (df>=2). At n=2 the df=1
        # multiplier is t=12.706, which yields an absurd out-of-range half-width (e.g. +/-3.9 on a
        # [0,1] rate) that reflects nothing but the tiny sample — NOT a real bug, just a degenerate
        # statistic. For n<3 report the seed HALF-RANGE instead (mean +/- h spans exactly [min,max]);
        # use the proper t-CI at n>=3, which the real >=5-seed runs hit. The raw per-seed values are
        # always stored (_seeds) so nothing is hidden — high variance still shows.
        if n >= 3:
            ci = _T95.get(n, 1.96) * sd / math.sqrt(n)
        elif n == 2:
            ci = (max(vals) - min(vals)) / 2.0
        else:
            ci = 0.0
        out[f"{k}_mean"] = m; out[f"{k}_std"] = sd
        out[f"{k}_ci95"] = ci; out[f"{k}_seeds"] = vals
    return out


def _dump_traces(run_id, scenario, seed, traces, tag=""):
    """Write one episode's per-assessment reasoning trace to JSONL (one record per judge cycle):
    what the LLM saw, how it reasoned, what it decided, and the plan the policy held. One file per
    model x seed so a re-run overwrites cleanly. `tag` keeps a v1/v3 A/B in separate filenames."""
    if not traces:
        return
    _TRACE_DIR.mkdir(parents=True, exist_ok=True)
    safe = run_id.replace("/", "_").replace(":", "_").replace("=", "").replace(" ", "")
    stem = f"{tag}__{safe}__{scenario}__seed{seed}" if tag else f"{safe}__{scenario}__seed{seed}"
    path = _TRACE_DIR / f"{stem}.jsonl"
    with path.open("w") as f:
        for rec in traces:
            f.write(json.dumps({"run_id": run_id, "scenario": scenario, "seed": seed, **rec}) + "\n")
    return path


def _dump_util(run_id, scenario, seed, util_trace, recovery, tag=""):
    """Write one episode's utility trace + recovery internals to JSON so a 'not recovered' verdict
    can be inspected against the logs: recovery lists per-window u_des/target/recovered/post_peak_u;
    samples is the per-second u/uA/uB/c_online/queue/lam. One file per model x seed; overwrites."""
    if not util_trace:
        return
    _TRACE_DIR.mkdir(parents=True, exist_ok=True)
    safe = run_id.replace("/", "_").replace(":", "_").replace("=", "").replace(" ", "")
    stem = f"{tag}__{safe}__{scenario}__seed{seed}" if tag else f"{safe}__{scenario}__seed{seed}"
    path = _TRACE_DIR / f"{stem}__util.json"
    path.write_text(json.dumps({"run_id": run_id, "scenario": scenario, "seed": seed,
                                "recovery": recovery, "samples": util_trace}, indent=2))
    return path


def _save(summary, seeds, scenarios, path, prompt="", winner=None):
    """Write the results/checkpoint file. Called after EVERY model completes (winner=None,
    an in-progress checkpoint) and once more at the end (winner set), so a crash mid-sweep
    keeps every model finished so far — re-run with --resume to continue. `path` is per-prompt
    (see --tag) so a v1 and v2 A/B don't clobber each other."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"seeds": seeds, "scenarios": scenarios, "prompt": prompt,
         "winner": winner, "models": summary}, indent=2))


async def sweep(args):
    seeds     = list(range(1, args.seeds + 1))
    scenarios = _SCENARIOS
    runs      = expand_runs(select_candidates(args.models))
    ckpt      = args.ckpt_path
    summary: dict[str, dict] = {}

    # Resume: reload the checkpoint and skip models already done — but only if it was produced
    # under the SAME seeds+scenarios, else the pooled stats wouldn't be comparable.
    if args.resume and ckpt.exists():
        prev = json.loads(ckpt.read_text())
        if prev.get("seeds") == seeds and prev.get("scenarios") == scenarios:
            summary = prev.get("models", {})
            print(f"[exp1] resume: {len(summary)} model(s) from checkpoint: {sorted(summary)}")
        else:
            print("[exp1] resume: checkpoint seeds/scenarios differ — starting fresh")

    for tier, mode, slug, run_id in runs:
        if run_id in summary:
            print(f"\n[exp1] skip {run_id} — already in checkpoint")
            continue
        model    = resolve_model(slug)
        settings = judge_settings(slug, mode)
        print(f"\n[exp1] === {run_id}  (tier={tier}, reasoning={mode}) ===")

        model_rows, per_scn = [], {}
        tin = tout = terr = tass = 0
        for scn in scenarios:
            rows = []
            for s in seeds:
                try:
                    r = await run_agentic(model, scn, s, args, model_settings=settings)
                except Exception as e:
                    print(f"[exp1] {run_id} {scn} seed={s} ERROR {type(e).__name__}: {e}")
                    continue
                rows.append(r); model_rows.append(r)
                _dump_traces(run_id, scn, s, r.get("traces"), tag=args.tag)   # save this episode's reasoning
                _dump_util(run_id, scn, s, r.get("util_trace"), r.get("recovery"), tag=args.tag)  # + utility/recovery trace
                tin += r["in_tok"]; tout += r["out_tok"]; terr += r.get("errors", 0)
                tass += r.get("assessments", 0)
                print(f"[exp1] {run_id[:34]:34s} {scn:16s} seed={s}  P={r['P']:.3f} "
                      f"benign={r['benign']:.3f} filtered={r['filtered']:.3f} "
                      f"servers={r['servers']:.1f} llm={r['llm_lat']:.1f}s "
                      f"tok={r['in_tok']}/{r['out_tok']}")
            if rows:
                per_scn[scn] = _agg(rows)

        if not model_rows:
            print(f"[exp1] {run_id}: no successful episodes — skipping")
            continue
        n_ep = len(model_rows)
        # element-wise mean of per-window benign served across this model's episodes (botnet, event)
        bps_rows = [r["benign_per_storm"] for r in model_rows if r.get("benign_per_storm")]
        bps_mean = [round(sum(col) / len(col), 4) for col in zip(*bps_rows)] if bps_rows else []
        summary[run_id] = {
            "tier": tier, "reasoning": mode, "slug": slug,
            "overall": _agg(model_rows),          # pooled over all scenario x seed episodes
            "by_scenario": per_scn,
            "benign_per_storm": bps_mean,         # [botnet-window served, event-window served]
            "episodes": n_ep, "assessments": tass,
            "in_tok": tin, "out_tok": tout, "errors": terr,
            "cost_per_ep_usd":   round(_episode_cost_usd(slug, tin, tout) / n_ep, 5),
            # per-decision cost — normalises out how many calls fit in an episode (slow models fit fewer)
            "cost_per_asmt_usd": round(_episode_cost_usd(slug, tin, tout) / max(1, tass), 6),
        }
        if args.save:
            _save(summary, seeds, scenarios, ckpt, prompt=str(args.prompt))   # checkpoint
            print(f"[exp1]   checkpoint: {len(summary)} model(s) done -> {ckpt.name}")

    _print_table(summary, scenarios, seeds)
    winner = _downselect(summary)
    if args.save and summary:
        _save(summary, seeds, scenarios, ckpt, prompt=str(args.prompt), winner=winner)   # final write
        print(f"\n  saved -> {ckpt}")


def _print_table(summary, scenarios, seeds):
    print("\n" + "=" * 118)
    print(f"EXPERIMENT 1 — LLM JUDGES IN THE FULL AGENTIC LOOP  ({len(seeds)} seeds x {len(scenarios)} scenarios)")
    if len(seeds) < 3:
        print(f"  NOTE: n={len(seeds)} seeds — ± is the seed HALF-RANGE (a Student-t CI needs n>=3 to be meaningful)")
    print("  mean over ALL scenario x seed episodes.  P = resilience; benign = benign completion;")
    print("  filtered = deliberate malicious-drop; servers = mean online capacity; eff = P*c_max/servers;")
    print("  llm_lat = mean judge-call time; $/asmt = cost per judge decision; $/ep = per episode; err = failed")
    print("=" * 122)
    hdr = (f"  {'model':34s} {'P':>13s} {'benign':>13s} {'filtered':>11s} "
           f"{'servers':>11s} {'eff':>6s} {'llm_lat':>8s} {'$/asmt':>9s} {'$/ep':>8s} {'err':>4s}")
    print(hdr)
    for run_id, s in sorted(summary.items(), key=lambda kv: kv[1]["overall"]["P_mean"], reverse=True):
        o = s["overall"]
        print(f"  {run_id[:34]:34s} "
              f"{o['P_mean']:.3f}±{o['P_ci95']:.3f}  "
              f"{o['benign_mean']:.3f}±{o['benign_ci95']:.3f}  "
              f"{o['filtered_mean']:.3f}±{o['filtered_ci95']:.2f}  "
              f"{o['servers_mean']:>5.1f}±{o['servers_ci95']:.1f}  "
              f"{o['eff_mean']:>6.2f} {o['llm_lat_mean']:>7.2f}s "
              f"{s.get('cost_per_asmt_usd', 0):>9.5f} {s['cost_per_ep_usd']:>8.4f} {s['errors']:>4d}")
    print("=" * 122)
    # P DECOMPOSITION — reporting only (P itself is unchanged). Per-storm P = 0.4*absorption +
    # 0.4*adaptation + 0.2*trec; P_episode = mean over storms. absorb is scored over the storm [t0,td],
    # adapt over recovery [td,tr], trec from the recovery span (tr-t0). One row per storm window, so
    # all three components AND their timestamps are visible. The event split (rho/uA/uB) then shows WHY
    # absorb.event is what it is: low uA + high uB + rho~1 = surge served (queue short) but thin margin.
    print("\n  RESILIENCE DECOMPOSITION — per-storm P = 0.4*absorb + 0.4*adapt + 0.2*trec ; P_episode = mean over storms")
    print("  absorb = mean utility over storm [t0,td] | adapt = mean utility over recovery [td,tr]")
    print("  tr = first t after td where u >= 0.95*(pre-storm baseline) and holds 30s (scan capped at next storm)")
    print("  trec = 1.0 if (tr-t0) <= 60s else 60/(tr-t0)")
    print(f"  {'model':26s} {'window':7s} {'t0':>4s} {'td':>4s} {'tr':>5s} "
          f"{'absorb':>7s} {'adapt':>7s} {'trec':>6s} {'P_win':>6s}")
    for run_id, s in sorted(summary.items(), key=lambda kv: kv[1]["overall"]["P_mean"], reverse=True):
        o = s["overall"]
        for wname, p in (("botnet", "bot"), ("event", "ev")):
            ab, ad, tc = o.get(f"absorb_{p}_mean", 0), o.get(f"adapt_{p}_mean", 0), o.get(f"trec_{p}_mean", 0)
            p_win = 0.4 * ab + 0.4 * ad + 0.2 * tc
            print(f"  {run_id[:26]:26s} {wname:7s} "
                  f"{o.get(f't0_{p}_mean', 0):>4.0f} {o.get(f'td_{p}_mean', 0):>4.0f} {o.get(f'tr_{p}_mean', 0):>5.0f} "
                  f"{ab:>7.3f} {ad:>7.3f} {tc:>6.3f} {p_win:>6.3f}")
    print("\n  event-surge utility split (why absorb.event lands where it does):")
    print(f"  {'model':26s} {'rho.event':>10s} {'uA.event':>9s} {'uB.event':>9s}   "
          f"(low uA + high uB + rho~1 = served but thin margin, not starvation)")
    for run_id, s in sorted(summary.items(), key=lambda kv: kv[1]["overall"]["P_mean"], reverse=True):
        o = s["overall"]
        print(f"  {run_id[:26]:26s} {o.get('rho_ev_mean', 0):>10.3f} "
              f"{o.get('uA_ev_mean', 0):>9.3f} {o.get('uB_ev_mean', 0):>9.3f}")
    print("=" * 122)
    # Benign outcomes split the completion number into WHERE users were lost: served (attached),
    # starved (no capacity -> under-reserve/under-provision), or filter-FP (wrongly dropped as
    # malicious). served + starved + fp = 1. This separates a reserve-reasoning failure (starved)
    # from an over-aggressive filter (fp) — the blended 'benign' column hides which.
    print("\n  BENIGN OUTCOMES (served + starved + filter-FP = 1):")
    print(f"  {'model':34s} {'served':>14s} {'starved':>14s} {'filter-FP':>14s}")
    for run_id, s in sorted(summary.items(), key=lambda kv: kv[1]["overall"]["benign_mean"], reverse=True):
        o = s["overall"]
        served, fp = o["benign_mean"], o.get("benign_fp_mean", 0.0)   # .get: pre-column checkpoints
        starved = max(0.0, 1.0 - served - fp)
        print(f"  {run_id[:34]:34s} {served:>13.3f} {starved:>14.3f} {fp:>14.3f}")
    print("=" * 118)
    # Benign served WITHIN each storm window (botnet, then event). The blended figure above is
    # dominated by the event surge (~68% of benign volume); this shows the botnet window is served
    # fine by all, and the starvation is concentrated in the event surge — the reasoning-sensitive one.
    print("\n  BENIGN SERVED PER WINDOW (botnet storm | event surge):")
    print(f"  {'model':34s} {'botnet':>14s} {'event':>14s}")
    for run_id, s in sorted(summary.items(), key=lambda kv: kv[1]["overall"]["benign_mean"], reverse=True):
        bps = s.get("benign_per_storm") or []
        cells = " ".join(f"{v:>14.3f}" for v in bps) if bps else f"{'—':>14s}"
        print(f"  {run_id[:34]:34s} {cells}")
    print("=" * 118)
    # Event reasoning: how well each judge estimated the crowd behind the named event and sized
    # the reserve, vs ground truth. This is the discriminating signal — a model that under-reasons
    # the "not sold out" England v Brazil crowd under-reserves and its event-surge QoS suffers.
    true_att = EXP1_EVENT.attendance
    ideal    = EXP1_EVENT.ideal_reserve()
    print(f"\n  EVENT REASONING — {EXP1_EVENT.name}, {EXP1_EVENT.venue}")
    print(f"  ground truth: {true_att:,} attendees  ->  ideal reserve {ideal} servers "
          f"(the judge sees only the name + venue + 'not sold out', no number)")
    print(f"  {'model':34s} {'est. crowd':>12s} {'reserve':>10s} {'reserve err':>12s}")
    for run_id, s in sorted(summary.items(), key=lambda kv: kv[1]["overall"]["P_mean"], reverse=True):
        o = s["overall"]
        est, res = o["att_est_mean"], o["reserve_est_mean"]
        print(f"  {run_id[:34]:34s} {est:>12,.0f} {res:>10.1f} {res - ideal:>+12.1f}")
    print("=" * 118)


def _downselect(summary) -> str | None:
    """Pick the model to carry into Exp 2–4: highest mean resilience P across scenarios,
    ties broken by lower avg servers then lower cost."""
    if not summary:
        return None
    run_id, s = max(summary.items(),
                    key=lambda kv: (kv[1]["overall"]["P_mean"],
                                    -kv[1]["overall"]["servers_mean"],
                                    -kv[1]["cost_per_ep_usd"]))
    print(f"\n  >>> WINNER: {run_id}  (mean P = {s['overall']['P_mean']:.3f}, "
          f"servers = {s['overall']['servers_mean']:.1f}, ${s['cost_per_ep_usd']:.4f}/ep)")
    print("      -> carry this model into Exp 2–4.")
    return run_id


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Experiment 1 — LLM judges in the full agentic loop")
    p.add_argument("--seeds", type=int, default=5)
    p.add_argument("--models", nargs="*", default=None,
                   help="restrict the roster by slug substring (e.g. --models gpt-5.4 gemini)")
    p.add_argument("--prompt", default=str(_DEFAULT_PROMPT),
                   help="path to the judge system-prompt file to test (default: v1). Use with --tag "
                        "to A/B a second prompt version without clobbering the first run's output.")
    p.add_argument("--tag", default="",
                   help="label folded into this run's output files (e.g. --tag v2 -> "
                        "model_comparison_v2_<timestamp>.json). Every run is timestamped, so a fresh "
                        "run never overwrites a prior one or the blessed model_comparison.json.")
    p.add_argument("--rt-factor", type=float, default=1.0, dest="rt_factor")
    p.add_argument("--assessment-interval", type=float, default=5.0, dest="assessment_interval")
    p.add_argument("--window", type=float, default=15.0, dest="window_s",
                   help="telemetry-window seconds the judge sees (15 = validated config)")
    p.add_argument("--no-anticipation", action="store_true",
                   help="disable forecast/calendar (debug only; the headline runs WITH tools)")
    p.add_argument("--serial", action="store_true", help="serial provisioning (default: parallel)")
    p.add_argument("--shadow", action="store_true", help="also run the rule in shadow (off by default)")
    p.add_argument("--save", action="store_true",
                   help="write results + a per-model checkpoint to a timestamped "
                        "experiments/exp1_model_comparison/model_comparison[_<tag>]_<timestamp>.json")
    p.add_argument("--resume", action="store_true",
                   help="reload the NEWEST matching timestamped checkpoint and skip models already "
                        "done (same seeds+scenarios). Use with --save to continue a crashed sweep.")
    p.add_argument("--log", nargs="?", const="AUTO", default=None,
                   help="tee output to a file (bare --log auto-names it under exp1_model_comparison/logs/)")
    args = p.parse_args()

    # Load the chosen judge prompt and route output to a per-run, TIMESTAMPED file so a new run
    # never overwrites a previous one or the blessed, hand-picked `model_comparison.json` that the
    # manuscript cites. `--resume` continues the NEWEST matching run instead of starting a new file.
    args.system_prompt = Path(args.prompt).read_text()
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"model_comparison_{args.tag}" if args.tag else "model_comparison"
    if args.resume:
        prior = sorted(_EXP_DIR.glob(f"{base}_[0-9]*.json"))   # timestamps start with the year digit
        args.ckpt_path = prior[-1] if prior else _EXP_DIR / f"{base}_{run_stamp}.json"
    else:
        args.ckpt_path = _EXP_DIR / f"{base}_{run_stamp}.json"
    print(f"[exp1] prompt: {Path(args.prompt).name}   ->  results: {args.ckpt_path.name}")

    _logfile = None
    if args.log is not None:
        _slug = f"{args.tag}_{run_stamp}" if args.tag else run_stamp
        log_path = (_LOGS_DIR / f"log_model_comparison_{_slug}.txt"
                    if args.log == "AUTO" else Path(args.log))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _logfile = open(log_path, "w")
        sys.stdout = _Tee(sys.__stdout__, _logfile)
        sys.stderr = _Tee(sys.__stderr__, _logfile)
        print(f"[exp1] logging this run to {log_path}")

    _prevent_sleep()

    async def _main():
        print(f"[exp1] Starting MCP server on {MCP_HOST}:{MCP_PORT} ...")
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
