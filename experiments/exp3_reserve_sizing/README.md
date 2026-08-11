# Experiment 3 — Event-Portfolio Reserve Sizing

The one place a language model should beat a hardcoded controller is reading a free-text calendar
entry and **reasoning to the crowd it implies**. This experiment asks whether that skill
**generalises** across a whole portfolio of real events, not just the single event used to pick the
judge in Experiment 1.

## The four arms
Each arm sizes the pre-provisioning **reserve** (server count) for each of the 12 real events in
[`../../docs/event_portfolio.md`](../../docs/event_portfolio.md).

| Arm | What it sees | How it sizes the reserve |
|---|---|---|
| Flat rule | "an event is scheduled" | a fixed reserve (10 servers), no estimate |
| Formula rule | structured fields `{capacity, sold_out}` | `sold_out → capacity`, else `0.85 × capacity`. The honest strong baseline (parsing conceded); it cannot infer the turnout fraction from context |
| LLM (gemini-3.1-flash-lite) | the **free-text** calendar line only | the real Non-RT judge reads the line and commits a reserve — the deployable operating point |
| LLM (gpt-5.4-mini, reasoning on) | the **free-text** calendar line only | same judge, the reasoning ceiling |

The calendar line names the event, venue, and sold-out status but **never the attendance**, so the
LLM must infer the turnout itself.

## Method
Each event is one benign surge sized by its **ground-truth attendance**. Each of the 10 seeds is an
independent episode that runs the deployed judge's short assessment loop until it **commits** a
reserve; we read the peak committed crowd/reserve exactly as Experiment 1 does (`judge_peak_*`), so a
model that lapses to "no action" on one cycle still engages on a later one. A seed where it never
commits is a non-engagement (kept out of the estimate mean, reported as an engage rate). The chosen
reserve is then pre-provisioned and the surge simulated, scoring **benign QoS** (penalises
under-reserve) and **servers used** (penalises over-reserve), split by sold-out vs not-sold-out — the
arms should tie on sold-out (the flag suffices) and separate on the rest.

## Reproduce
```bash
python -m scripts.exp_3_reserve_sizing --llm --seeds 10 --save
python experiments/exp3_reserve_sizing/plot_reserve_sizing.py   # forest + scatter figures
```
Timestamped run logs and the exploratory `_5seed` figures are git-ignored; the README, blessed
`reserve_sizing.json`, plot script, and the 10-seed figures are tracked.

## Results (10 seeds, mean)
Reserve error is the mean absolute difference from the ideal reserve, in servers.

| Arm | Reserve error, all 12 | Reserve error, 7 not-sold-out | Benign served (all) | Servers held (all) |
|---|---|---|---|---|
| Flat rule | — (no estimate) | — | 0.959 | 10.00 |
| Formula rule | — (context-blind) | — | 1.000 | 7.83 |
| **LLM (gemini)** | **1.08** | **1.61** | 0.982 | 7.45 |
| **LLM (gpt-5.4-mini)** | **0.78** | **1.06** | 0.994 | 7.58 |

### Findings
- **The estimate generalises across the portfolio**, not just the Exp-1 event. On the scatter figure
  both LLMs sit near the `y=x` diagonal across two orders of magnitude of attendance.
- **Sold-out events are easy for everyone** — the flag pins the crowd to capacity, so all arms tie.
  The separation is on the **not-sold-out** events, where the turnout fraction must be reasoned from
  the free-text context.
- **The LLM reserves are close to ideal** (gpt-5.4-mini within ~0.8 servers on average, gemini within
  ~1.1), while the flat rule wastes capacity (10 servers everywhere, including 1k-attendance events)
  and the formula rule cannot read context beyond the sold-out flag.
- **gpt-5.4-mini (reasoning on) is the ceiling** and gemini the deployable operating point, the same
  two judges carried from Experiment 1.

## Figures
- `exp3_reserve_sizing_forest.{pdf,png}` — per-event true attendance and each arm's estimate (95% CI).
- `exp3_reserve_sizing_scatter.{pdf,png}` — estimated vs true attendance, non-uniform axis so the
  crowded high-attendance events stay legible.
