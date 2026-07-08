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
    drop_prob_floor:      float
    storm_active:         bool
    last_P:               float
    last_updated:         float


@dataclass
class SharedPolicy:
    """
    Policy the Non-RT-Agent maintains for the fast loop.

    storm_active         — the Non-RT judge's current storm-vs-noise verdict.
                           Gates the absorption lever (drop_prob) in the fast loop.
    drop_prob_floor      — drop probability to apply while a storm is active.
    escalation_threshold — longer-horizon tuning knob (queue/(c*mu) sensitivity).
    """
    escalation_threshold: float = 0.6
    drop_prob_floor:      float = 0.0
    storm_active:         bool  = False

    last_P:       float = field(default=0.0, repr=False)
    last_updated: float = field(default=0.0, repr=False)

    def __post_init__(self):
        self._lock = threading.Lock()

    def update(
        self,
        *,
        storm_active:         bool,
        drop_prob_floor:      float,
        resilience_P:         float,
        escalation_threshold: float | None = None,
        tighten:              bool = False,
    ) -> None:
        """
        Write a new policy atomically.

        storm_active and drop_prob_floor are the operational levers and are always
        written. escalation_threshold is the slow tuning knob and only moves when
        `tighten` is set (avoids oscillating the long-horizon threshold).
        """
        with self._lock:
            self.storm_active    = storm_active
            self.drop_prob_floor = drop_prob_floor
            if tighten and escalation_threshold is not None:
                self.escalation_threshold = escalation_threshold
            self.last_P       = resilience_P
            self.last_updated = time.monotonic()

    def snapshot(self) -> PolicyView:
        """Return an immutable, consistent view of all policy fields at once."""
        with self._lock:
            return PolicyView(
                escalation_threshold=self.escalation_threshold,
                drop_prob_floor=self.drop_prob_floor,
                storm_active=self.storm_active,
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
                f"drop_prob_floor={self.drop_prob_floor:.2f}{age_str}."
            )


@dataclass
class EpisodeStats:
    """Lightweight counters accumulated during an episode run."""
    near_rt_steps:      int = 0
    near_rt_errors:     int = 0
    non_rt_assessments: int = 0
    non_rt_errors:      int = 0
    intents_routed:     int = 0
