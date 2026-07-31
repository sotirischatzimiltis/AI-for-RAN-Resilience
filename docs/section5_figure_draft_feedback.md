# Section V Figure — Feedback on First Draft (to fix in coming days)

Review of the user's first abstract draft of Fig. 5 (Agentic Resilience Framework).
Draft layout: Operator → SMO (Agentic AI Coordinator) → [site box: LLM ──MCP── Tools
(Episode Stats / Short-term Forecast / Calendar) → Shared Policy + Memory & Persistence →
fast loop (Compute Optimal Number of Servers | Set Drop Malicious UEs) → RAN (UEs +
antenna + RU–vDU–vCU)].

## What's already correct (keep)
- Tier order: SMO/Coordinator → LLM+Tools (judge) → Shared Policy + Memory → fast loop
  (servers + drop) → RAN. Matches the code.
- Judge: LLM ──MCP── Tools (Episode Stats / Forecast / Calendar). MCP tools correctly on the judge.
- Two levers = c* (Compute Optimal Number of Servers) and p_drop (Set Drop Malicious UEs).
- Coordinator → judge (standing instruction #7) + Coordinator → Shared Policy (posture/SLA #2).
- Memory & Persistence ↔ fast loop bidirectional = evolution (observe / auto-engage).
- Shared Policy + Memory at same middle tier; RAN = RU–vDU–vCU disaggregation.

## MUST FIX (these misrepresent the system)
1. **No telemetry going UP.** Every arrow points down → reads as open-loop top-down control.
   The system is driven by observations rising from the RAN: fast loop reads telemetry each
   tick, judge reads a telemetry window, Episode Stats/Forecast read the sim. ADD an upward
   telemetry/observation rail: RAN → fast loop → judge/tools. This is the closed loop.
2. **"Set Drop Malicious UEs" has no arrow to the RAN.** Only "Compute Servers" connects down.
   Both levers actuate the RAN; the drop is enforced at RAN ADMISSION (on incoming malicious
   UEs). ADD arrow: Set Drop → RAN (point it at the RU/UE admission side).

## OPTIONAL polish (nice-to-have, or cover in prose)
- Label tiers: LLM box = Non-RT storm judge (rApp); servers+drop box = Near-RT deterministic
  loop (xApp); outer box = AI-RAN Site. Ties abstraction to the O-RAN hierarchy.
- UEs → RU attach-request arrow (λ) — the load source (currently implied by position only).
- Coordinator → Calendar (schedule event) if operator-driven anticipation path should be
  visible; else fold into coordinator→judge arrow + cover in text.
- Cross-episode persistence: dashed self-loop on Memory & Persistence ("across episodes");
  live Memory↔loop arrow already carries the main story.

## Naming reminder for the text
"Set Drop Malicious UEs" is fine as a label, but in prose: the loop SETS the drop probability
p_drop; the RAN ADMISSION does the actual dropping. Arrow to RAN = "set p_drop"; filtering
happens there.

## Bottom line
Two must-fixes: (1) upward telemetry arrow, (2) drop→RAN actuation arrow. With those, the
figure is faithful to the implementation; the rest is optional polish coverable in prose.

See also: [section5_figure_arrows.md](section5_figure_arrows.md) (full edge list) and the
memory note project_section5_framework_facts (per-tier ground truth).
