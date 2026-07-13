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

# Only the slow knobs persist.
_KNOBS = ("queue_hold_threshold", "lyapunov_V", "lyapunov_W")

DEFAULT_PATH = Path(__file__).parent / ".policy_state.json"


def load_knobs(path: str | Path = DEFAULT_PATH) -> dict | None:
    """Return {queue_hold_threshold, lyapunov_V, lyapunov_W} from the store, or
    None if it is missing or unreadable (caller then falls back to defaults)."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return {k: data[k] for k in _KNOBS if k in data} or None
    except (json.JSONDecodeError, OSError):
        return None


def save_knobs(policy, path: str | Path = DEFAULT_PATH) -> None:
    """Persist the slow knobs from a SharedPolicy (or PolicyView) snapshot."""
    view = policy.snapshot() if hasattr(policy, "snapshot") else policy
    data = {
        "queue_hold_threshold": int(view.queue_hold_threshold),
        "lyapunov_V":           float(view.lyapunov_V),
        "lyapunov_W":           float(view.lyapunov_W),
    }
    Path(path).write_text(json.dumps(data, indent=2))
