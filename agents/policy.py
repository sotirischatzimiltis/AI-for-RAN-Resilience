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
    malicious_drop_prob:  float
    storm_active:         bool
    queue_hold_threshold: int
    lyapunov_V:           float
    lyapunov_W:           float
    min_servers:          int
    last_updated:         float


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
    lyapunov_V           — Lyapunov utility/performance weight (raw scale ~1000).
                           Higher V favours QoS → the loop provisions MORE servers.
    lyapunov_W           — Lyapunov server-cost weight (raw scale ~1). Higher W
                           penalises servers → the loop provisions FEWER. Use it to
                           pre-tune posture for a forecast storm / mass event.
    """
    malicious_drop_prob:  float = 0.0
    storm_active:         bool  = False
    queue_hold_threshold: int   = 10
    lyapunov_V:          float = 1000.0
    lyapunov_W:          float = 1.0

    # Operator overrides, set by a routed intent via set_operator(). When present
    # they OUTRANK the Non-RT judge's autonomous tuning (an operator command wins).
    operator_V:          float | None = None
    operator_W:          float | None = None
    min_servers:         int = 1
    # A standing instruction the Orchestrator delegated to the Non-RT judge (operational
    # nuance, e.g. "tonight's surge is legitimate"). The judge reads it each assessment.
    operator_note:       str = ""

    last_updated: float = field(default=0.0, repr=False)

    def __post_init__(self):
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
                self.operator_V = max(0.0, float(lyapunov_V))
            if lyapunov_W is not None:
                self.operator_W = max(0.0, float(lyapunov_W))
            if min_servers is not None:
                self.min_servers = max(1, int(min_servers))
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
            self.storm_active    = storm_active
            self.malicious_drop_prob = malicious_drop_prob
            if tighten:
                if queue_hold_threshold is not None:
                    self.queue_hold_threshold = max(1, int(queue_hold_threshold))
                if lyapunov_V is not None:
                    self.lyapunov_V = max(0.0, float(lyapunov_V))
                if lyapunov_W is not None:
                    self.lyapunov_W = max(0.0, float(lyapunov_W))
            self.last_updated = time.monotonic()

    def snapshot(self) -> PolicyView:
        """Return an immutable, consistent view of all policy fields at once.
        Operator overrides take precedence over the Non-RT judge's V/W."""
        with self._lock:
            return PolicyView(
                malicious_drop_prob=self.malicious_drop_prob,
                storm_active=self.storm_active,
                queue_hold_threshold=self.queue_hold_threshold,
                lyapunov_V=self.operator_V if self.operator_V is not None else self.lyapunov_V,
                lyapunov_W=self.operator_W if self.operator_W is not None else self.lyapunov_W,
                min_servers=self.min_servers,
                last_updated=self.last_updated,
            )

    def context_str(self) -> str:
        with self._lock:
            age_str = ""
            if self.last_updated:
                age = time.monotonic() - self.last_updated
                age_str = f", last Non-RT update {age:.0f}s ago"
            op = ""
            if self.operator_V is not None or self.operator_W is not None or self.min_servers > 1:
                op = (f" Operator override: "
                      f"V={self.operator_V}, W={self.operator_W}, min_servers={self.min_servers}.")
            return (
                f"Policy: storm_active={self.storm_active}, "
                f"malicious_drop_prob={self.malicious_drop_prob:.2f}, "
                f"queue_hold_threshold={self.queue_hold_threshold}, "
                f"lyapunov_V={self.lyapunov_V:.0f}, lyapunov_W={self.lyapunov_W:.2f}"
                f"{age_str}.{op}"
            )


@dataclass
class EpisodeStats:
    """Lightweight counters accumulated during an episode run."""
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
