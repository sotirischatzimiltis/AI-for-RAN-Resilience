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
    """Immutable, atomic snapshot of policy state for the fast loop to read."""
    escalation_threshold: float
    malicious_drop_prob:      float
    storm_active:         bool
    queue_hold_threshold: int
    lyapunov_V:           float
    lyapunov_W:           float
    last_P:               float
    last_updated:         float


@dataclass
class SharedPolicy:
    """
    Policy the Non-RT-Agent maintains for the fast loop.

    storm_active         — the Non-RT judge's current storm-vs-noise verdict.
                           Gates the absorption lever (drop_prob) in the fast loop.
    malicious_drop_prob      — drop probability to apply while a storm is active.
    escalation_threshold — longer-horizon tuning knob (queue/(c*mu) sensitivity).
    queue_hold_threshold — the fast loop refuses to scale servers DOWN while
                           queue_len is at/above this. Higher = hold capacity
                           longer during drain; lower = scale down sooner.
    lyapunov_V           — Lyapunov utility/performance weight (raw scale ~1000).
                           Higher V favours QoS → the loop provisions MORE servers.
    lyapunov_W           — Lyapunov server-cost weight (raw scale ~1). Higher W
                           penalises servers → the loop provisions FEWER. Use it to
                           pre-tune posture for a forecast storm / mass event.
    """
    escalation_threshold: float = 0.6
    malicious_drop_prob:      float = 0.0
    storm_active:         bool  = False
    queue_hold_threshold: int   = 10
    lyapunov_V:          float = 1000.0
    lyapunov_W:          float = 1.0

    last_P:       float = field(default=0.0, repr=False)
    last_updated: float = field(default=0.0, repr=False)

    def __post_init__(self):
        self._lock = threading.Lock()

    def update(
        self,
        *,
        storm_active:         bool,
        malicious_drop_prob:      float,
        resilience_P:         float,
        escalation_threshold: float | None = None,
        queue_hold_threshold: int | None = None,
        lyapunov_V:           float | None = None,
        lyapunov_W:           float | None = None,
        tighten:              bool = False,
    ) -> None:
        """
        Write a new policy atomically.

        storm_active and malicious_drop_prob are the operational levers and are always
        written. escalation_threshold, queue_hold_threshold, lyapunov_V and
        lyapunov_W are slow tuning knobs and only move when `tighten` is set
        (avoids the fast loop's behaviour changing on every assessment).
        """
        with self._lock:
            self.storm_active    = storm_active
            self.malicious_drop_prob = malicious_drop_prob
            if tighten:
                if escalation_threshold is not None:
                    self.escalation_threshold = escalation_threshold
                if queue_hold_threshold is not None:
                    self.queue_hold_threshold = max(1, int(queue_hold_threshold))
                if lyapunov_V is not None:
                    self.lyapunov_V = max(0.0, float(lyapunov_V))
                if lyapunov_W is not None:
                    self.lyapunov_W = max(0.0, float(lyapunov_W))
            self.last_P       = resilience_P
            self.last_updated = time.monotonic()

    def snapshot(self) -> PolicyView:
        """Return an immutable, consistent view of all policy fields at once."""
        with self._lock:
            return PolicyView(
                escalation_threshold=self.escalation_threshold,
                malicious_drop_prob=self.malicious_drop_prob,
                storm_active=self.storm_active,
                queue_hold_threshold=self.queue_hold_threshold,
                lyapunov_V=self.lyapunov_V,
                lyapunov_W=self.lyapunov_W,
                last_P=self.last_P,
                last_updated=self.last_updated,
            )

    def context_str(self) -> str:
        with self._lock:
            age_str = ""
            if self.last_updated:
                age = time.monotonic() - self.last_updated
                age_str = f", last Non-RT update {age:.0f}s ago"
            return (
                f"Policy: storm_active={self.storm_active}, "
                f"escalation_threshold={self.escalation_threshold:.2f}, "
                f"malicious_drop_prob={self.malicious_drop_prob:.2f}, "
                f"queue_hold_threshold={self.queue_hold_threshold}, "
                f"lyapunov_V={self.lyapunov_V:.0f}, lyapunov_W={self.lyapunov_W:.2f}"
                f"{age_str}."
            )


@dataclass
class EpisodeStats:
    """Lightweight counters accumulated during an episode run."""
    near_rt_steps:      int = 0
    near_rt_errors:     int = 0
    non_rt_assessments: int = 0
    non_rt_errors:      int = 0
    intents_routed:     int = 0
