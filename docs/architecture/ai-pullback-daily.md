# ai-pullback-daily — design rationale

> Rationale record. The rules themselves live in the repo-root `CLAUDE.md`;
> this file holds the *why*: measurements, post-mortems and history. It was moved
> out of `CLAUDE.md` verbatim on 2026-10-22. Edit here when a rule's reasoning
> changes, and keep the one-line rule in `CLAUDE.md` in sync.

## Overview and the two `TH` dicts

**`ai-pullback-daily`** — a daily routine over the same universe, answering
*when* to buy rather than *what*. It runs every calendar day (weekends and US
market holidays included): thesis tripwires, 24/7 perp implied moves, and a
four-layer neocloud credit read update daily; per-ticker T1/T2/T3 technicals
update only on complete trading days and are carried forward, clearly labelled,
otherwise. Its `references/*.md` are verbatim migrations from the same kind of
private source document and carry the same edit-only-what-you-must rule.
`scripts/neocloud_credit_monitor.py` owns tripwire ④ outright — the report
quotes its verdict and never second-guesses it.

Its thresholds live in a `TH` dict — but there are **two** of them, with **different key names**, so a recalibration cannot be copy-pasted between them; one in
`neocloud_credit_monitor.py` and one in `neocloud_credit_lite.py` (the
standard-library cloud variant). They must stay in sync: a recalibration is a
**two-place** edit, and the two scripts silently disagreeing about tripwire ④
is the worst failure this skill has, because whichever one ran that day is what
the report quotes as fact. `TH` also carries rules the reference tables do not
spell out — `L1_primary_window_days = 30` caps how long a primary-market
repricing counts toward L1 — so changing one has to be reflected in
`references/neocloud-credit.md` too.

### How the two AI-compute skills couple

The industry rating table is maintained in exactly one place:
`skills/ai-industry-weekly/assets/baseline.md`, written only by
`baseline.py write`. `ai-pullback-daily` reads it (via its
`scripts/industry_table.py`), reads `universe.json` for the ticker set, and
calls `ai-industry-weekly/scripts/hk_quote.py` for HK prices — always read-only,
never writing into the weekly skill. The daily skill therefore hard-depends on
the weekly one being installed as a sibling directory (`AI_INDUSTRY_WEEKLY_DIR`
overrides the lookup), and its preflight `industry_table.py --check` fails
loudly rather than falling back to a stale copy: the rating is the quality gate
in the daily bucketing, so a missing table silently disables half the framework.

That check also warns when the baseline is more than 10 days old. The ratings
are a deliberately slow variable read verbatim each day, so staleness is
invisible at the daily level — nothing errors, the numbers just quietly stop
reflecting the last earnings season. The warning is the only signal that the
weekly run is overdue; it does not block the daily report.

All three daily scripts that need the weekly install (`industry_table.py`,
`technicals.py`, `perp_quotes.py`) resolve it through one shared module,
`skills/ai-pullback-daily/scripts/_weekly.py` — same candidate order, same
probe, same error text. `AI_INDUSTRY_WEEKLY_DIR`, when set but not a valid
weekly install, **always errors out; it never silently falls back** to the
default locations. Three independent lookups drifting apart is how one run's
rating table and ticker list end up coming from two different installs, both
exiting 0, producing a self-contradictory report. Add a new daily script that
needs the sibling and it goes through `_weekly.py` too.

`industry_table.py --check` additionally cross-checks the baseline's **row
count** against `universe.json`'s **ticker count** and warns loudly when they
disagree. Those two numbers come from different files with different writers, so
they drift for a real and common reason: someone edited `universe.json` in the
weekly skill and has not re-run `baseline.py write` yet. In that window the
daily report's step 1 (reads `baseline.md`) and step 2 (`technicals.py` /
`perp_quotes.py`, read `universe.json`) report different ticker counts. Before
this check all three scripts exited 0 and said nothing. The pre-existing
"declared 标的数 vs actual rows" check does not catch it — both of those numbers
live in `baseline.md`, so they are consistent by construction.

Like the staleness warning, the count mismatch and the "baseline date is in the
future" warning are banners, not failures: `--check` still exits 0. All three
describe something broken on the *weekly* side, and the daily skill is read-only
there, so warning is the only thing it can do.

Do not resolve this coupling by copying the table into `ai-pullback-daily`. A
second copy reintroduces exactly the drift the single-writer design removes.
