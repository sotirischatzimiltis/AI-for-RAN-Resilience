"""
Shared policy state — written by the Non-RT-Agent (storm judge), read by the
deterministic fast control loop. No LLM is involved in the handoff.

The Non-RT task writes from its own async task; the 1 Hz fast loop reads every
tick. Reads go through snapshot() so the fast loop always sees a consistent,
atomic view and can never observe a half-applied update.
"""

import threading
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PolicyView:
    """Immutable, atomic snapshot of policy state for the fast loop to read.

    lyapunov_V/W here are the EFFECTIVE weights: an operator override (set via a
    routed intent) takes precedence over the Non-RT judge's autonomous tuning.
    min_servers is an operator SLA capacity floor (default 1)."""
    malicious_drop_prob:  float   # absorption lever: fraction of botnet UEs to drop (when storm_active)
    storm_active:         bool    # judge's current verdict — gates the drop lever in the fast loop
    queue_hold_threshold: int     # fast loop won't scale DOWN while queue_len is at/above this
    lyapunov_V:           float   # EFFECTIVE utility weight (operator override, else judge's)
    lyapunov_W:           float   # EFFECTIVE cost weight (operator override, else judge's)
    min_servers:          int     # operator SLA capacity floor (default 1)
    reserve_servers:      int     # pre-provision floor the judge sized for a scheduled event
    event_time:           float   # sim-time the event surge hits (0 = apply reserve immediately)
    last_updated:         float   # monotonic timestamp of the last write (for staleness/context)


@dataclass
class SharedPolicy:
    """
    Policy the Non-RT-Agent maintains for the fast loop.

    storm_active         — the Non-RT judge's current storm-vs-noise verdict.
                           Gates the absorption lever (drop_prob) in the fast loop.
    malicious_drop_prob  — drop probability to apply while a storm is active.
    queue_hold_threshold — the fast loop refuses to scale servers DOWN while
                           queue_len is at/above this. Higher = hold capacity
                           longer during drain; lower = scale down sooner.
    lyapunov_V           — Lyapunov utility/performance weight (NORMALISED O(1) scale;
                           nominal 1, load-tracking). Higher V favours QoS → the loop
                           provisions MORE servers; raise toward ~20 to pre-provision.
    lyapunov_W           — Lyapunov server-cost weight (O(1); nominal 1). Higher W
                           penalises servers → the loop provisions FEWER.
    """
    # --- operational levers: written EVERY assessment by the judge ---
    malicious_drop_prob:  float = 0.0
    storm_active:         bool  = False
    # --- slow tuning knobs: only move when the judge sets tighten=True ---
    queue_hold_threshold: int   = 10
    lyapunov_V:          float = 1.0    # normalised O(1) scale (was 1000); nominal load-tracking
    lyapunov_W:          float = 1.0
    # Pre-provisioning floor: servers to hold online AHEAD of a surge, regardless of current
    # load. Raising lyapunov_V does nothing until load arrives (the loop sizes to what it sees),
    # so this is the lever that actually pre-provisions a scheduled/forecast surge. 1 = none.
    reserve_servers:     int   = 1
    # When the event surge is expected (sim seconds). The fast loop applies `reserve_servers`
    # only from (event_time - ramp_time) onward, so servers are online BY the event without
    # pre-provisioning too early. 0 = no schedule -> apply the reserve immediately (rule/operator).
    event_time:          float = 0.0

    # Operator overrides, set by a routed intent via set_operator(). When present
    # they OUTRANK the Non-RT judge's autonomous tuning (an operator command wins).
    operator_V:          float | None = None   # None = no override -> use the judge's lyapunov_V
    operator_W:          float | None = None
    min_servers:         int = 1
    # A standing instruction the Orchestrator delegated to the Non-RT judge (operational
    # nuance, e.g. "tonight's surge is legitimate"). The judge reads it each assessment.
    operator_note:       str = ""

    last_updated: float = field(default=0.0, repr=False)

    def __post_init__(self):
        # One lock guards every read/write: the judge writes from its async task while
        # the 1 Hz fast loop reads each tick — the lock keeps updates atomic so the loop
        # never sees a half-applied policy.
        self._lock = threading.Lock()

    def set_operator(
        self,
        *,
        lyapunov_V:  float | None = None,
        lyapunov_W:  float | None = None,
        min_servers: int | None = None,
    ) -> None:
        """Apply an operator directive (from a routed intent). These override the
        Non-RT judge until cleared. Pass a value to set it; None leaves it unchanged."""
        with self._lock:
            if lyapunov_V is not None:
                self.operator_V = max(0.0, float(lyapunov_V))   # clamp >= 0
            if lyapunov_W is not None:
                self.operator_W = max(0.0, float(lyapunov_W))
            if min_servers is not None:
                self.min_servers = max(1, int(min_servers))     # never below 1 server
            self.last_updated = time.monotonic()

    def set_operator_note(self, note: str) -> None:
        """Store a standing operator instruction the Non-RT judge will read each assessment."""
        with self._lock:
            self.operator_note = note or ""
            self.last_updated = time.monotonic()

    def get_operator_note(self) -> str:
        with self._lock:
            return self.operator_note

    def update(
        self,
        *,
        storm_active:         bool,
        malicious_drop_prob:  float,
        queue_hold_threshold: int | None = None,
        lyapunov_V:           float | None = None,
        lyapunov_W:           float | None = None,
        reserve_servers:      int | None = None,
        event_time:           float | None = None,
        tighten:              bool = False,
    ) -> None:
        """
        Write a new policy atomically.

        storm_active and malicious_drop_prob are the operational levers and are
        always written. queue_hold_threshold, lyapunov_V and lyapunov_W are slow
        tuning knobs and only move when `tighten` is set (avoids the fast loop's
        behaviour changing on every assessment).
        """
        with self._lock:
            self.storm_active    = storm_active          # levers: always overwritten
            self.malicious_drop_prob = malicious_drop_prob
            if tighten:                                   # slow knobs: only when the judge asks
                if queue_hold_threshold is not None:
                    self.queue_hold_threshold = max(1, int(queue_hold_threshold))
                if lyapunov_V is not None:
                    self.lyapunov_V = max(0.0, float(lyapunov_V))
                if lyapunov_W is not None:
                    self.lyapunov_W = max(0.0, float(lyapunov_W))
                # Event plan (reserve + its time): commit ONCE, then LOCK it so per-cycle
                # re-estimates can't jitter or corrupt it. A stand-down (reserve -> 1) always
                # applies and clears the lock; a fresh schedule (none active yet) applies; a
                # re-estimate while a plan is active is ignored. event_time=0 (rule/operator)
                # is never locked, so their immediate reserve behaves exactly as before.
                if reserve_servers is not None or event_time is not None:
                    new_reserve = max(1, int(reserve_servers)) if reserve_servers is not None else self.reserve_servers
                    new_event   = max(0.0, float(event_time))  if event_time is not None      else self.event_time
                    plan_locked = self.event_time > 0.0 and self.reserve_servers > 1
                    if new_reserve <= 1 or not plan_locked:      # stand-down, or first commit
                        self.reserve_servers = new_reserve
                        self.event_time      = new_event
                    # else: plan locked — keep the committed crowd + time
            self.last_updated = time.monotonic()

    def snapshot(self) -> PolicyView:
        """Return an immutable, consistent view of all policy fields at once.
        Operator overrides take precedence over the Non-RT judge's V/W."""
        with self._lock:
            return PolicyView(
                malicious_drop_prob=self.malicious_drop_prob,
                storm_active=self.storm_active,
                queue_hold_threshold=self.queue_hold_threshold,
                # operator override wins over the judge's tuning when present
                lyapunov_V=self.operator_V if self.operator_V is not None else self.lyapunov_V,
                lyapunov_W=self.operator_W if self.operator_W is not None else self.lyapunov_W,
                # min_servers = the OPERATOR SLA floor only. The judge's pre-provisioning reserve is
                # exposed separately (with its event_time) so the fast loop can apply it on schedule
                # — from event_time - ramp_time — instead of immediately.
                min_servers=self.min_servers,
                reserve_servers=self.reserve_servers,
                event_time=self.event_time,
                last_updated=self.last_updated,
            )

    def context_str(self) -> str:
        # One-line summary of the CURRENT policy — fed to the judge as its "previous
        # verdict" line each assessment (continuity / hysteresis).
        with self._lock:
            age_str = ""
            if self.last_updated:                              # how stale is this verdict?
                age = time.monotonic() - self.last_updated
                age_str = f", last Non-RT update {age:.0f}s ago"
            op = ""
            if self.operator_V is not None or self.operator_W is not None or self.min_servers > 1:
                op = (f" Operator override: "                  # only shown when an override is set
                      f"V={self.operator_V}, W={self.operator_W}, min_servers={self.min_servers}.")
            return (
                f"Policy: storm_active={self.storm_active}, "
                f"malicious_drop_prob={self.malicious_drop_prob:.2f}, "
                f"queue_hold_threshold={self.queue_hold_threshold}, "
                f"lyapunov_V={self.lyapunov_V:.1f}, lyapunov_W={self.lyapunov_W:.2f}, "
                f"reserve_servers={self.reserve_servers}"
                f"{f', event_time={self.event_time:.0f}s' if self.event_time > 0 else ''}"
                f"{age_str}.{op}"
            )


@dataclass
class RunStats:
    """The agentic tiers' run meter — LLM cost (tokens, requests) + latency + step/error
    counters accumulated across an episode. Distinct from the get_episode_stats MCP tool,
    which returns the SIMULATOR's world truth (resilience P, arrivals, retries, queue)."""
    near_rt_steps:      int = 0
    near_rt_errors:     int = 0
    non_rt_assessments: int = 0
    non_rt_errors:      int = 0
    intents_routed:     int = 0
    # LLM usage / cost accounting (accumulated across all assessments + intents)
    llm_requests:       int   = 0
    llm_input_tokens:   int   = 0
    llm_output_tokens:  int   = 0
    llm_latency_s:      float = 0.0   # cumulative wall time inside agent.run() (pure LLM + tool calls)
    assessment_latency_s: float = 0.0 # cumulative wall time for the WHOLE assessment
                                       # (telemetry summary + prompt build + LLM + policy write)
    # Peak pre-provisioning the judge committed this episode (largest crowd estimate + the reserve
    # it mapped to). Used by exp_1 to score how well each LLM reasoned about the scheduled event.
    judge_peak_attendance: int = 0
    judge_peak_reserve:    int = 0
    # Per-assessment reasoning trace: one record per judge cycle (the telemetry window it saw, its
    # articulated reasoning, the decision, and the plan the policy actually held). Lets us replay
    # HOW each LLM reasoned, not just the aggregate score. Filled in _do_assessment; dumped per
    # episode by the experiment runner. Empty by default so nothing changes for callers that ignore it.
    traces:               list = field(default_factory=list, repr=False)
