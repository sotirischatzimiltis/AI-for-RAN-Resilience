"""
Cross-episode persistence for the Non-RT judge's slow tuning knobs.

Each episode runs in its own process against a fresh simulation, so the posture
the Non-RT agent tuned (queue_hold_threshold, lyapunov_V, lyapunov_W) is normally
lost at exit. This tiny JSON store lets that posture carry over: load it to seed
the next episode's SharedPolicy, save it at episode end. The operational levers
(storm_active, malicious_drop_prob) are deliberately NOT persisted — they are
live verdicts, meaningless across episodes.
"""

from __future__ import annotations

import json
from pathlib import Path

# Only the slow knobs persist (NOT the live levers storm_active / malicious_drop_prob).
_KNOBS = ("queue_hold_threshold", "lyapunov_V", "lyapunov_W")

# Both files live at the REPO ROOT (parent.parent), not in shared/, and are gitignored.
DEFAULT_PATH        = Path(__file__).parent.parent / ".policy_state.json"    # tuned posture
STORM_MEMORY_PATH   = Path(__file__).parent.parent / ".storm_memory.json"    # learned storm signature
# fields of the learned storm signature that persist across episodes
_MEMORY_FIELDS = ("baseline_lam", "engage_threshold", "storm_drop_level",
                  "storms_seen", "learned")


def load_knobs(path: str | Path = DEFAULT_PATH) -> dict | None:
    """Return {queue_hold_threshold, lyapunov_V, lyapunov_W} from the store, or
    None if it is missing or unreadable (caller then falls back to defaults)."""
    p = Path(path)
    if not p.exists():                       # first-ever run: nothing saved yet
        return None
    try:
        data = json.loads(p.read_text())
        # keep only known knob keys; empty dict -> None so the caller uses defaults
        return {k: data[k] for k in _KNOBS if k in data} or None
    except (json.JSONDecodeError, OSError):  # corrupt/unreadable -> fall back to defaults
        return None


def save_knobs(policy, path: str | Path = DEFAULT_PATH) -> None:
    """Persist the slow knobs from a SharedPolicy (or PolicyView) snapshot."""
    view = policy.snapshot() if hasattr(policy, "snapshot") else policy  # accept either type
    data = {
        "queue_hold_threshold": int(view.queue_hold_threshold),
        "lyapunov_V":           float(view.lyapunov_V),
        "lyapunov_W":           float(view.lyapunov_W),
    }
    Path(path).write_text(json.dumps(data, indent=2))   # overwrite the store


def load_storm_memory(path: str | Path = STORM_MEMORY_PATH) -> dict | None:
    """Return the persisted storm-signature fields, or None if absent/unreadable."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return {k: data[k] for k in _MEMORY_FIELDS if k in data} or None
    except (json.JSONDecodeError, OSError):
        return None


def save_storm_memory(memory, path: str | Path = STORM_MEMORY_PATH) -> None:
    """Persist a StormMemory's learned signature (not the toggles)."""
    data = {k: getattr(memory, k) for k in _MEMORY_FIELDS}   # pull each signature field off the object
    Path(path).write_text(json.dumps(data, indent=2))
