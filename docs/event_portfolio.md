# Event portfolio — reserve-sizing ground truth (Non-RT justification experiment)

Purpose: a portfolio of **real** scheduled events whose turnout a fixed rule cannot guess, so
the Non-RT judge's job becomes *sizing the pre-provision reserve from context*. The LLM reasons
over the event (domain, venue, artist/opponent, sold-out flag) to estimate attendance; a rule can
only apply a fixed heuristic. This is the one axis where the LLM's judgement should beat a
hardcoded controller (binary storm/filter calls, we already showed, a rule matches ~99%).

Code: `shared/events.py` (model + `PORTFOLIO`), `scripts/exp_3_reserve_sizing.py` (the 3 arms).

## Surge / reserve model
```
surge_peak (UEs/s) = attendance / 300          # /300 = cell-share x 1/peak-window, a calibration
                                               #        constant mapping real crowds onto the sim
                                               #        scale (baseline 20, c_max=16 ~ 456/s).
ideal_reserve (servers) = ceil(surge_peak / (mu * rho_target)),   mu ~= 28.7 UEs/s/server,
                                               rho_target = 0.8,  floored 1, capped 16
```
Reserve is sized with **headroom** (rho_target=0.8), not to bare capacity: a reserve loaded to
rho=1 still fails QoS (queue delay explodes as rho->1), so even a perfect crowd estimate would
score poorly. Sizing the surge to fill only 80% of the reserve leaves slack — the same
"never park at rho=1" principle the Lyapunov loop uses. The SAME map converts every arm's
estimate (LLM `expected_attendance` included), so the comparison stays purely on the estimate.
A surge below ~baseline (attendance ~< 6,000) is a **non-event** -> reserve 1. Recognising *that*
is a clean LLM win: a rule that reserves for "an event on the calendar" burns capacity on a
614-person women's game.

## What the agent sees (the qualitative calendar — free text, no numbers)
The LLM gets `"{name}, at {venue}"` (+ ", sold out" when true). It must bring the venue capacity
from world knowledge and estimate the turnout fraction. The rules get the structured
`{capacity, sold_out}` (parsing conceded, so the fight is on judgement not NLP).

| # | Event | Venue | Sold out? |
|---|---|---|---|
| 1 | Taylor Swift, concert | Wembley Stadium | yes |
| 2 | Man Utd v Liverpool, Premier League | Old Trafford | yes |
| 3 | Arsenal Women v Man Utd, WSL | Emirates Stadium | yes |
| 4 | Liverpool v Real Madrid, UCL knockout | Anfield | yes |
| 5 | Adele, concert | The O2 Arena | yes |
| 6 | England v Brazil, international friendly | Wembley Stadium | **no** |
| 7 | Chelsea v Man Utd, Women's FA Cup final | Wembley Stadium | **no** |
| 8 | West Ham v FK TSC, Europa League group | London Stadium | no |
| 9 | West Ham v Viborg, Conference League | London Stadium | no |
| 10 | Aston Villa Women v Man Utd, WSL | Villa Park | no |
| 11 | Man Utd Women v Brighton, WSL | Leigh Sports Village | no |
| 12 | West Ham Women v Everton, WSL | Chigwell/Victoria Road (Dagenham) | no |

## Hidden ground truth (drives the sim + scores the arms)
`mu = 28.7`, `rho_target = 0.8`; reserve = ceil(attendance/300 / (mu*rho_target)), floored 1.
`src` keys index the sources below.

| # | Venue cap | ⟨attendance⟩ | ~fill | surge/s | **ideal reserve** | src (att / cap) |
|---|---|---|---|---|---|---|
| 1 | 90,000 | 89,000 | 99% | 297 | **13** | A1 / C1 |
| 2 | 74,310 | 73,738 | 99% | 246 | **11** | A2 / C2 |
| 3 | 60,704 | 60,160 | 99% | 201 | **9** | A3 / C3 |
| 4 | 53,394 | 52,337 | 98% | 174 | **8** | A4 / C4 |
| 5 | 20,000 | 18,750 | 94% | 63 | **3** | A5 / C5 |
| 6 | 90,000 | 83,664 | **93%** | 279 | **13** | A6 / C1 |
| 7 | 90,000 | 77,390 | **86%** | 258 | **12** | A7 / C1 |
| 8 | 62,500 | 41,374 | 66% | 138 | **7** | A8 / C6 |
| 9 | 62,500 | 30,230 | 48% | 101 | **5** | A9 / C6 |
| 10 | 42,785 | 12,533 | 29% | 42 | **2** | A10 / C7 |
| 11 | 12,000 | 4,060 | 34% | 14 | **1** | A11 / C8 |
| 12 | 6,078 | 614 | 10% | 2 | **1** | A12 / C9 |

## The structure that gives it teeth
The portfolio deliberately **breaks the sold-out <-> high-fill correlation**:
- **Sold out (rows 1–5):** flag ⇒ ≈full ⇒ both arms size it. **Control group; they tie.**
- **Surprise-high (rows 6–7):** NOT sold out yet ~90% full. A fixed *low* fraction **under-reserves**
  these ⇒ **benign QoS failures**.
- **Low-fill (rows 10–12):** NOT sold out and nearly empty (10–34%). A fixed *high* fraction
  **over-reserves** these ⇒ wasted servers.

No single fraction serves both 6–7 and 10–12, so the formula rule (its best fit, fraction 0.85
under headroom sizing) is forced into a dilemma: it still under-reserves the surprise-high (row 6
formula 12 vs ideal 13) while over-reserving the low-fill rows. Outcome table below is regenerated
each `exp_3_reserve_sizing` run (the pre-headroom numbers are stale — refresh on the next full run):

| arm | group | benign QoS | avg servers |
|---|---|---|---|
| flat (reserve 10) | not sold-out | — | 10.0 (waste on low-fill) |
| formula (frac 0.85) | not sold-out | — (under-reserves 6–7) | — |

The LLM's target: benign ~1.0 (avoid the QoS failures) **and** low servers (avoid the waste) — a
Pareto win from one policy. Two naive heuristics the real data also kills: "cup tie ⇒ low" (Man
City v Chelsea Carabao R3 was 98% full) and "women's ⇒ low" (Arsenal Women sold out the 60k Emirates).

## Caveats / tunables
- `/300` and `mu` are calibration constants; they set absolute reserve, not the ordering. For
  paper-defensible provenance, split `/300` into explicit `cell_share x attendance / window_s`.
- Capacities are contemporaneous (Anfield is pre-Anfield-Road-expansion 53,394 for the Feb-2023 game).
- Concert `capacity` = the venue's concert configuration (sold-out shows ≈ that number).

## Sources
Attendances:
- A1 Taylor Swift, Wembley 2024 (~89k/night): https://www.wembleystadium.com/events/2024/Taylor-Swift-The-Eras-Tour
- A2 Man Utd 0-3 Liverpool, 1 Sep 2024 (73,738): https://www.espn.co.uk/football/match/_/gameId/704305/liverpool-manchester-united
- A3 Arsenal Women v Man Utd, Feb 2024 (60,160, WSL record): https://www.arsenal.com/news/arsenal-set-new-wsl-attendance-record-59042 ; https://feeds.bbci.co.uk/sport/football/68243268
- A4 Liverpool 2-5 Real Madrid, 21 Feb 2023 (52,337): https://www.espn.co.uk/football/match/_/gameId/656856/real-madrid-liverpool
- A5 Adele Live 2016, The O2 (8 sellouts, >150,000 ≈ 18,750/night): https://en.wikipedia.org/wiki/Adele_Live_2016
- A6 England 0-1 Brazil, 23 Mar 2024 (83,664): https://www.espn.com/soccer/match/_/gameId/689956/brazil-england
- A7 Chelsea v Man Utd, Women's FA Cup final, 14 May 2023 (77,390): https://en.wikipedia.org/wiki/2023_Women%27s_FA_Cup_final
- A8 West Ham v FK TSC, 21 Sep 2023 (41,374): https://en.wikipedia.org/wiki/2023%E2%80%9324_West_Ham_United_F.C._season
- A9 West Ham v Viborg, 18 Aug 2022 (30,230): https://www.hammers.news/match-preview/london-stadium-close-to-half-empty-for-west-hams-europa-league-opener-with-over-20000-tickets-unsold-for-clash-against-serbian-minnows/
- A10 Aston Villa Women v Man Utd, Villa Park, 1 Oct 2023 (12,533): https://en.wikipedia.org/wiki/2023%E2%80%9324_Aston_Villa_W.F.C._season
- A11 Man Utd Women v Brighton, 4 Feb 2024 (4,060): https://en.wikipedia.org/wiki/2023%E2%80%9324_Manchester_United_W.F.C._season
- A12 West Ham Women v Everton, 10 Dec 2023 (614): https://en.wikipedia.org/wiki/Women%27s_Super_League_records_and_statistics

Capacities:
- C1 Wembley (90,000): https://en.wikipedia.org/wiki/Wembley_Stadium
- C2 Old Trafford (74,310): https://en.wikipedia.org/wiki/Old_Trafford
- C3 Emirates (60,704): https://en.wikipedia.org/wiki/Emirates_Stadium
- C4 Anfield, Feb 2023 (~53,394, pre-expansion): https://en.wikipedia.org/wiki/Anfield
- C5 The O2 Arena (~20,000): https://www.theo2.co.uk/do-more-at-the-o2/the-o2-arena
- C6 London Stadium (62,500): https://en.wikipedia.org/wiki/London_Stadium
- C7 Villa Park (42,785): https://en.wikipedia.org/wiki/Villa_Park
- C8 Leigh Sports Village (12,000): https://en.wikipedia.org/wiki/Leigh_Sports_Village
- C9 Victoria Road / Chigwell Construction Stadium, Dagenham (6,078): https://en.wikipedia.org/wiki/Victoria_Road_(Dagenham)
