"""
StormMemory — the fast loop's learned storm signature.

The malicious-drop filter normally waits on the slow LLM judge to declare a storm,
so the FIRST storm is met late and little of the botnet is blocked. StormMemory
lets the fast loop LEARN the storm signature from experience: once it has weathered
a storm it knows the benign baseline and roughly where a storm sits, so it can
auto-engage the filter itself — no LLM lag — the moment arrival rate crosses a
learned threshold.

Two independent timescales, each toggleable:
  • within-episode : learn from storm 1, then handle storms 2..N faster.
  • across-episode : persist the signature so the next episode starts primed
                     (even storm 1 is met fast).

Learning completes when the first observed storm ENDS, so within-episode the first
storm is the honest "cold" baseline and later storms show the improvement.
"""

from __future__ import annotations

from dataclasses import dataclass

STORM_FACTOR   = 3.0   # lam > STORM_FACTOR * baseline  => a storm is happening
ENGAGE_FACTOR  = 3.0   # once learned, auto-engage the filter above this * baseline
BASELINE_EMA   = 0.1   # smoothing for the benign-baseline estimate


@dataclass
class StormMemory:
    """Learned storm signature + the switches that gate its use."""
    learn_within: bool = False      # calibrate during this episode
    learn_across: bool = False      # persist / load the signature across episodes

    baseline_lam:     float = 20.0
    engage_threshold: float | None = None   # set once learned; None => not yet learned
    storm_drop_level: float = 0.8
    storms_seen:      int   = 0
    learned:          bool  = False

    _in_storm: bool = False         # internal edge-detection state

    def observe(self, lam: float, storm_active: bool) -> None:
        """Update the signature from one telemetry tick. Call every fast-loop cycle
        when within-episode learning is enabled."""
        if not self._in_storm:
            # track the benign baseline only while calm
            if lam <= STORM_FACTOR * self.baseline_lam:
                self.baseline_lam = (1 - BASELINE_EMA) * self.baseline_lam + BASELINE_EMA * lam

        in_storm_now = lam > STORM_FACTOR * self.baseline_lam
        if in_storm_now and not self._in_storm:
            self._in_storm = True
            self.storms_seen += 1
        elif not in_storm_now and self._in_storm:
            self._in_storm = False
            # first storm just ended -> lock in the signature (within-episode)
            if self.learn_within and not self.learned:
                self.engage_threshold = ENGAGE_FACTOR * self.baseline_lam
                self.learned = True

    def should_engage(self, lam: float) -> bool:
        """True if the fast loop should auto-engage the filter for this arrival rate."""
        return self.learned and self.engage_threshold is not None and lam > self.engage_threshold
