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

### How the two skills couple

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
```

Requires `python3` with `yfinance` and `requests`, plus outbound network.

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

`BigtoC/my-skills` is public. The Slack channel id comes from the
`AI_INDUSTRY_SLACK_CHANNEL_ID` environment variable; the skill intentionally
ships no config file and contains no channel or routine ids. With the variable
unset the skill still runs and prints its full report, skipping only the push.

`git push` uses the SSH host alias `github.com-personal`, while `gh` may be
authenticated as a different account — check `gh auth status` before assuming
PR creation will work.
