# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A portable Agent Skills repo, not an application. Skills follow the open
`SKILL.md` spec (https://agentskills.io/specification) so they work across
compatible agents and are installable via `npx skills add`.

There is no build, no lint, and no test suite. Nothing compiles. Changes are
verified by running the skills' scripts directly (see below).

## Layout and conventions

Skills live at `skills/<skill-name>/SKILL.md`, with optional siblings:
`references/` (on-demand docs), `scripts/` (helper executables), `assets/`
(templates and data).

Every `SKILL.md` starts with frontmatter in this shape — match it when adding a
skill:

```yaml
name: <dir name, must match>
description: <written so an agent auto-invokes it; pack in trigger words>
license: MIT
compatibility: <runtime requirements, or "no script/package/network requirements">
metadata:
  author: BigtoC
  version: "0.1.0"
  tags: "comma,separated"
  triggers: "optional glob hints"
```

`README.md` has a skills table that must gain a row when a skill is added.

`.claude/settings.json` holds a `PreToolUse` hook that nudges toward the
`rust-best-practices` skill on `.rs` edits. Adding a similar hook is the
documented way to force auto-invocation regardless of context.

## Skills

**`rust-best-practices`** — pure documentation, no runtime.

**`ai-industry-weekly`** — a weekly equity-research routine with scripts and
persistent state. Most of what follows concerns it.

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

**`daily-risk-monitor`** — a daily cross-market risk sweep (macro/credit, positioning,
crypto, an anti-emotion layer, long-run valuation) feeding 7 hard sell thresholds
and a two-track decision layer. It is **standalone**: no sibling-skill dependency,
its own scripts, its own state file. Its `references/*.md` are verbatim migrations
from a private source document and carry the same edit-only-what-you-must rule.

Three things about it are load-bearing and easy to erode:

- **Fetching is split by transport, deliberately.** All curl fetches live in
  standalone shell scripts (`fred.sh`, `cnn_fng.sh`, `crypto.sh`, `stock_perp.sh`,
  `cape.sh`) so they can be run and debugged alone; only yfinance work is Python
  (`market.py`). This is not stylistic drift — **FRED must use curl** (python
  `requests` times out in this environment) while **yfinance must use
  `requests.Session` + UA** (urllib fails SSL verification). The two requirements
  point opposite ways; "unifying" them breaks one side silently.
- **`assets/last_run.json` is the day-over-day baseline.** `snapshot.py write`
  rewrites it each run with every signal's tier plus both track readings, and it
  lands *before* the Slack push so a failed push cannot lose the tiers. Reading
  Slack history is only the fallback path when the file is absent — the skill
  must stay usable with no Slack at all.
- **Missing data is never quietly safe.** `⚪️ 数据暂缺` counts toward neither the
  numerator nor the denominator of the 7-threshold tally, and every one of them
  must carry a staleness figure in weeks. Signals 3 (BofA Bull & Bear) and 12
  (insider buy/sell) have no stable source yet are thresholds 6 and 7, so a
  fabricated number there moves the strategic position baseline directly.

It shares no code with `ai-pullback-daily` even though both read Hyperliquid's
`xyz` pool — each keeps its own fetcher, and they must not be merged.

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

### Known duplication in the daily scripts — not yet converged

Only *sibling-skill location* has been pulled into `_weekly.py`. The other
shared helpers are still copy-pasted, and that is the current state of the code,
not an oversight waiting to be discovered:

- **`scrub()`** (folds `$HOME`-ish absolute paths out of error text) is defined
  **five times** — once in each of `industry_table.py`, `technicals.py`,
  `perp_quotes.py`, `neocloud_credit_monitor.py`, `neocloud_credit_lite.py`.
- **`rel_display()`** exists in **three** copies: the shared one in `_weekly.py`
  (imported by the three scripts that need the weekly install) plus private
  definitions in `neocloud_credit_monitor.py` and `neocloud_credit_lite.py`,
  which do not import `_weekly` at all — the credit scripts read only this
  skill's own `assets/`, so they have no reason to depend on the weekly lookup.

Treat these as five and three separate implementations: a fix to path scrubbing
(the public-repo leak rule below) is an N-place edit, and grepping for
`_HOMEISH_RE` finds every copy. Converging them is fine, but `neocloud_credit_lite.py`
is the standard-library cloud variant and must not gain an import that ties it
to the rest of the script directory.

## Fallback chains — the rule

Several data points in this repo have no single reliable source, so they are
fetched through an ordered chain. Five exist today: HK prices (`hk_quote.py` →
yfinance, derived indicators only, never the price), VIX (FRED `VIXCLS` →
yfinance `^VIX`), funding rates (Binance → Hyperliquid → coinglass search), BTC
dominance (CoinGecko → CoinPaprika), and ETF holdings (Alpha Vantage → yfinance
→ issuer page). They were each written separately; these constraints are common
to all of them, and a sixth chain should follow them rather than reinvent one.

1. **The order is fixed and written down.** Not chosen at call time, not
   "whichever answers first".
2. **Label which tier produced the value** — in the human output and in
   `--json`. A number whose source is unknown cannot be checked later.
3. **Never merge tiers into one table.** Different tiers are different
   snapshots at different times under different definitions. Two of them side
   by side in one row is the error every caliber rule here exists to prevent.
4. **Thresholds do not travel between tiers.** A threshold calibrated on tier
   one (GuruFocus's `0.17`, CBOE's put/call bands) cannot be compared against a
   tier-two number. Either recalibrate and record the switch date, or record
   N/A. Reusing it silently is the single most expensive mistake available here.
5. **Whatever a degraded tier cannot support, record as N/A with the reason** —
   never compute it from partial data. yfinance returns only top-N holdings, so
   a cash/T-bill total derived from it would read 0.00% and look entirely
   normal while reproducing the exact "36% of T-bills counted as holdings"
   trap that `rating-rules.md` documents.
6. **Falling back must be loud.** A silent downgrade records "don't know" as
   "checked, fine" — the same failure the ⚪️ accounting rules exist to stop.
7. **A fresh lower tier beats a stale higher one.** This is why
   `etf_holdings.py` keeps no cache: holdings move (the three Roundhill funds
   are actively managed), so a stale snapshot of the preferred source is more
   dangerous than a fresh reading from a weaker one. Do not reintroduce a
   holdings cache.

One corollary worth stating separately: a tier being *available* does not make
it *authoritative*. `rating-rules.md` requires ETF holdings to come from the
issuer's own sheet. Alpha Vantage is convenient, not authoritative — so the
issuer sheet must still be pulled on its own schedule, otherwise "official wins
on conflict" can never fire, because nothing is ever there to conflict with.

## ai-industry-weekly architecture

A weekly re-rating of an AI-compute supply-chain quality table. Three files
carry the design and none makes sense alone:

- **`assets/universe.json`** is the single source of truth for the ticker set
  *and the row order*. Row count is never hardcoded — everything derives from
  `tickers` length. Adding or removing a ticker means editing only this file.
  Per-ticker flags drive behavior: `hk_quote` routes price fields to
  `hk_quote.py`, `ratio_distorted` and `currency` mark ADR/FX rows whose
  PS/PB/cash-flow ratios are meaningless.
- **`assets/baseline.md`** is rolling state, rewritten by every run. It is
  *also* its own seed — there is deliberately no `baseline.seed.md` and no
  `history/` directory. Rollback is git alone. Do not add backup, snapshot, or
  `.bak` files; that was an explicit decision.
- **`scripts/baseline.py`** is the guardrail that makes automatic overwriting
  safe.

The run order matters and is enforced by `SKILL.md`: fetch → re-rate → validate
→ diff → write the full report body → `write` the new baseline → Slack push.
The baseline write lands before the Slack push so a Slack failure cannot lose
the rolled baseline.

### baseline.py invariants — do not weaken these

- `write` runs full validation first and, on any failure, exits 1 without
  touching `baseline.md`. There is **no `--force`** and none should be added.
- `diff` also validates before comparing. This is not redundant: an unvalidated
  diff silently emitted a plausible-but-wrong change summary (a dropped row read
  as a deliberate delisting) that would be published one step before `write`
  caught it. Malformed input exits 1 with **zero bytes on stdout**.
- Validation covers header, row count, ticker set *and order* against
  `universe.json`, rating vocabulary, empty cells, and lazy placeholders like
  「见 Slack thread」.
- `universe.json` is validated too, with errors that name the manifest.
  Duplicate or ticker-less entries otherwise create an unbreakable loop: add a
  row → "duplicate" → remove it → "missing row".
- `write` renders the table itself in compact `| a | b |` form rather than
  echoing the caller's layout. Preserving caller formatting made one week's
  padded table force a full 46-row rewrite the next week — a 96-line diff for
  one row of real change, which destroys the git-based audit the design rests on.
- Table parsing is deliberately lenient: it takes every line starting with `|`
  and ignores prose and `<<<产业表开始>>>` markers, so a report body can be piped
  in directly. The cost is that a temp file must contain only *one* table.
- Paths in output are relativized (`rel_path`). Script output is pasted into the
  report body and pushed to Slack, so a local absolute path would leak the
  machine's username.

### Other non-obvious constraints

- `references/*.md` are **verbatim migrations** from a private source document.
  They encode hard-won caveats (per-ticker rating special cases, ETF holdings
  traps, HK price adjustment pitfalls). Edit only the specific line you must;
  never paraphrase or "tidy" them.
- HK tickers must take price / 52-week-high / drawdown from `hk_quote.py`, never
  from yfinance — yfinance's `history()` dividend-adjusts and corrupts those
  fields. This is a correctness rule, not a preference.
- `fetch_fundamentals.py` imports `yfinance` lazily inside `main()` (not in the
  top import block) so `--help` works without the package and a missing install
  yields a hint instead of a traceback. It must use `requests.Session`, not
  yfinance's default curl_cffi engine, which fails TLS behind a proxy and
  silently returns all-null `.info`.
- Missing data is recorded as `N/A` and never estimated.
- `etf_holdings.py` fetches ETF holdings, weights, expense ratio and inception
  date for the `etf: true` rows in `universe.json` through a **three-tier
  fallback: Alpha Vantage `ETF_PROFILE` → yfinance `funds_data.top_holdings` →
  the issuer's own holdings page**. It **caches nothing** — every run re-fetches.
  That is deliberate: holdings change (the three Roundhill funds are actively
  managed and rebalance quarterly), so a stale snapshot is more dangerous than
  no snapshot, and falling back to a fresher second-choice source beats falling
  back to an expired first-choice one. Do not reintroduce a holdings cache file.
  **The tiers do not share a caliber**, and that is the thing to get right when
  touching this: Alpha Vantage returns the *full* holdings list, yfinance
  returns *only the top N* (measured 2026-09-03: LYTE 0, NCLD 0, DRAM 5, SMH 10,
  SOXX 10), the issuer page is authoritative. Under a top-N tier the "top three
  combined" figure is still meaningful, but "top ten combined" needs N≥10, and
  "swap total", "non-US holdings total" and "cash/T-bill total" are **not
  computable at all** — they must be recorded as `N/A`. Computing them from a
  top-N list reproduces the exact trap `references/rating-rules.md` warns about
  (a marketing page counting 36% T-bills as holdings), which only the full list
  exposes. For the same reason numbers from different tiers must never be
  combined into one table, and every output carries a `source` label that report
  text has to quote alongside the fetch date. Keys come from `AV_API_KEYS`
  (comma-separated; the script rotates to the next key when one is rate limited)
  and never from a file in the repo. **Rate limiting comes back as HTTP 200 with
  a `{"Information": "...spreading out your free API requests..."}` body** —
  this is the easiest thing to get wrong here: branching on the status code
  reads a throttle as real data, so every response body has to be inspected
  before it is trusted. The free tier also caps at 25 calls a day, worded
  differently again — and the per-second message *also* contains "per day" and
  "rate limit", so **classification must test the burst wording first and the
  daily wording second**; reversed, one per-second throttle is read as an
  exhausted quota and burns every key at once. **An invalid key and an exhausted
  key return an identical message** (Alpha Vantage echoes the key back in both),
  so a "daily quota exhausted" verdict cannot distinguish "used up" from
  "mistyped" — the wording must say so, and every key failing on its first call
  means suspect the key, not the quota. Alpha Vantage and yfinance are both
  convenience sources, **not** the issuer's official holdings sheet — where they
  disagree the official sheet wins, and any holdings figure quoted in a report
  must name its source. The whole step is optional: with `AV_API_KEYS` unset the
  skill starts at the yfinance tier, records what it cannot get as `N/A`, and
  still prints its full report.

## Working on ai-industry-weekly

Scripts resolve their own data via `__file__`, so they run from any cwd — but
the *invocation* still needs a real path:

```sh
S=skills/ai-industry-weekly

python3 $S/scripts/baseline.py show                    # current table (header + separator + rows)
python3 $S/scripts/baseline.py meta                    # {date, updated_at, count, path}
python3 $S/scripts/baseline.py validate <table.md>     # check only; exit 1 lists every problem
python3 $S/scripts/baseline.py diff <table.md>         # rating changes + field drift
python3 $S/scripts/baseline.py write <table.md> --date YYYY-MM-DD

python3 $S/scripts/fetch_fundamentals.py                      # all tickers
python3 $S/scripts/fetch_fundamentals.py --tickers NVDA,TSM   # subset, for debugging
python3 $S/scripts/fetch_fundamentals.py --json out.json --quiet
python3 $S/scripts/hk_quote.py 0700.HK 1810.HK 0941.HK --json

python3 $S/scripts/etf_holdings.py                            # every `etf: true` row
python3 $S/scripts/etf_holdings.py --tickers LYTE,NCLD        # subset, for debugging
python3 $S/scripts/etf_holdings.py --check                    # pre-fetch self-check
python3 $S/scripts/etf_holdings.py --json out.json --sleep 1
```

Requires `python3` with `yfinance` and `requests`, plus outbound network.
`etf_holdings.py` uses `AV_API_KEYS` for its first tier; without it that tier is
skipped, the fetch starts at yfinance (top-N only, and nothing at all for LYTE /
NCLD), and the rest of the skill is unaffected.

### Verifying changes to these scripts

There is no test suite, so exercise them live rather than reading the code:

```sh
# round-trip must be lossless
python3 $S/scripts/baseline.py show > /tmp/t.md
python3 $S/scripts/baseline.py validate /tmp/t.md     # expect exit 0
python3 $S/scripts/baseline.py diff /tmp/t.md         # expect "本周评级无变动"

# every malformed shape must be refused with empty stdout
grep -v '^| ASML ' /tmp/t.md > /tmp/bad.md
python3 $S/scripts/baseline.py diff /tmp/bad.md       # expect exit 1, 0 bytes on stdout
```

When touching `write`, prove `assets/baseline.md` is byte-identical after a
rejected write (`md5` before and after) — a partially written baseline poisons
every later week. Test from a foreign cwd (`cd /tmp`, absolute paths) and check
no output contains a local absolute path.

## Public repo

`BigtoC/my-skills` is public. The skills intentionally ship no config file and
contain no channel or routine ids — Slack channel ids come from the environment,
and no real channel id, absolute home path, or username may appear in a report
body either.

**One variable for every skill that notifies: `NOTIFICATION_SLACK_CHANNEL_ID`.**
All three notifying skills (`ai-industry-weekly`, `ai-pullback-daily`,
`daily-risk-monitor`) read that single name, and a new skill that pushes to Slack
reuses it rather than minting its own. With it unset a skill still runs and
prints its full report, skipping only the push.

It replaced a pair grouped by routine family — `AI_INDUSTRY_SLACK_CHANNEL_ID` for
the two AI-compute skills, `RISK_MONITOR_SLACK_CHANNEL_ID` for
`daily-risk-monitor`, which was deliberately kept off the `AI_INDUSTRY_` prefix
because it is not part of that pair. Neither name may come back. Grouping by
family read as tidy and cost real usability: the notification target is a
property of the *operator*, not of the routine, so configuring a skill meant
first knowing which family it belonged to, and a skill fitting no existing family
meant another export pointing at the same channel. A skill that genuinely needs a
different destination is a deliberate exception to argue for, not a default to
reach for.

The rule covers Slack notification targets only. Non-notification variables keep
their own descriptive names — `AI_INDUSTRY_WEEKLY_DIR` (sibling-skill location),
`AV_API_KEYS` (Alpha Vantage keys) — and renaming those is not part of it.

`git push` uses the SSH host alias `github.com-personal`, while `gh` may be
authenticated as a different account — check `gh auth status` before assuming
PR creation will work.
