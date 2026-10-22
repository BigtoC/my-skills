# CLAUDE.md

A portable Agent Skills repo, not an application. Skills follow the open
`SKILL.md` spec (https://agentskills.io/specification) and install via
`npx skills add`. No build, lint or test suite — verify changes by running the
scripts directly (commands below).

The *why* behind every rule here — measurements, post-mortems, history — lives in
`docs/architecture/` (one file per skill plus `cross-cutting.md`). Read the
matching file before changing a rule; update it when the reasoning changes.
`docs/solutions/` holds searchable write-ups of past problems (YAML frontmatter:
`module`, `tags`, `problem_type`).

## Layout

`skills/<name>/SKILL.md` plus optional `references/` (on-demand docs),
`scripts/`, `assets/` (templates, data, state). Adding a skill means a new row
in `README.md`'s skills table. Every `SKILL.md` starts with:

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

`.claude/settings.json` has a `PreToolUse` hook nudging toward
`rust-best-practices` on `.rs` edits — the documented way to force a skill's
auto-invocation.

| skill                 | what it is                                                                                                                                                 |
|-----------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `rust-best-practices` | pure documentation                                                                                                                                         |
| `ai-industry-weekly`  | weekly re-rating of an AI-compute quality table; owns `assets/baseline.md` and `universe.json`                                                             |
| `ai-pullback-daily`   | daily *when-to-buy* over the same universe, every calendar day; per-ticker technicals only on complete trading days, carried forward and labelled otherwise |
| `daily-risk-monitor`  | daily cross-market risk sweep → 7 hard sell thresholds + two-track decision; **standalone**                                                               |

## Commands

Scripts resolve their data via `__file__`, so they run from any cwd.

```sh
W=skills/ai-industry-weekly
python3 $W/scripts/baseline.py show
python3 $W/scripts/baseline.py meta
python3 $W/scripts/baseline.py validate <table.md> --caps /tmp/fundamentals.json
python3 $W/scripts/baseline.py diff <table.md> --caps /tmp/fundamentals.json
python3 $W/scripts/baseline.py write <table.md> --date YYYY-MM-DD --caps /tmp/fundamentals.json
python3 $W/scripts/fetch_fundamentals.py                       # optional: --tickers NVDA,TSM  --json out.json --quiet
python3 $W/scripts/hk_quote.py 0700.HK 1810.HK --json
python3 $W/scripts/etf_holdings.py                             # optional: --tickers LYTE,NCLD  --check  --json out.json --sleep 1

P=skills/ai-pullback-daily
python3 $P/scripts/industry_table.py --check                   # preflight: sibling install, staleness, row count
python3 $P/scripts/technicals.py --json /tmp/tech.json
python3 $P/scripts/perp_quotes.py --spot /tmp/tech.json --json /tmp/perp.json
python3 $P/scripts/neocloud_credit_monitor.py --emit both --no-history   # drop --no-history only in a real run: it rewrites the tracked history file

R=skills/daily-risk-monitor
$R/scripts/fetch_all.sh launch         # start the ten fetch units (--serial without job control)
$R/scripts/fetch_all.sh join           # collect each unit's exit code from <unit>.rc
$R/scripts/fetch_all.sh list           # every unit as a command runnable exactly as printed
python3 $R/scripts/snapshot.py show    # also: diff today.json / write today.json --date YYYY-MM-DD (overwrites last_run.json)
```

Needs `python3` with `requests` and `yfinance` (`neocloud_credit_monitor.py`
also `numpy`); `daily-risk-monitor`'s shell fetchers need bash + curl + awk, and
`cnn_fng.sh` alone needs `jq`. `AV_API_KEYS` (comma-separated, environment only —
never a file in the repo) enables `etf_holdings.py`'s first tier; unset, it starts
at yfinance and the rest of the skill is unaffected.

**Verifying `baseline.py`** (also run from `cd /tmp` with absolute paths, and
check no output contains a local absolute path):

```sh
python3 $W/scripts/baseline.py show > /tmp/t.md
python3 $W/scripts/baseline.py validate /tmp/t.md    # exit 0
python3 $W/scripts/baseline.py diff /tmp/t.md        # 「本周评级无变动」
grep -v '^| ASML ' /tmp/t.md > /tmp/bad.md
python3 $W/scripts/baseline.py diff /tmp/bad.md      # exit 1, 0 bytes on stdout
```

When touching `write`, `md5` `assets/baseline.md` before and after a rejected
write — it must be byte-identical.

## Two-place edits — the worst failure in this repo

Two copies of one rule silently disagreeing means whichever copy ran that day is
what the report states as fact. Change every copy:

- **The two `TH` dicts** — `neocloud_credit_monitor.py` and
  `neocloud_credit_lite.py` (stdlib cloud variant). Different key names, so not
  copy-pasteable. `TH` carries rules the tables don't spell out
  (`L1_primary_window_days = 30`), so also update `references/neocloud-credit.md`.
- **The two `search-contract.md`** (one per daily skill; deliberate — no shared
  file is allowed). The common envelope (§2–§5 + the handoff receipt) is shared;
  group tables, dispatch lists, payload families, per-group caliber notes and
  sentinel sets (three vs four) are per-skill and must not be homogenised.
  Open item: the `daily-risk-monitor` copy lacks `# 数据自身的日期，不是取数日` on
  `as_of`.
- **`bars_fallback.py` vs `market_fallback.py`** — separate on purpose
  (`daily-risk-monitor` may not import from `ai-pullback-daily`).
- **`scrub()` and `rel_display()`** — not deliberate, just unconverged: a fix is
  an N-place edit. Path-scrubbing is the public-repo leak rule, so a missed copy
  publishes a home directory. **Count, never trust a written number:**
  `grep -rl _HOMEISH_RE --include='*.py' skills/` (scrub) and
  `grep -rl 'def rel_display' --include='*.py' skills/`. Converging is fine, but
  `neocloud_credit_lite.py` must gain no import tying it to the rest of the
  script directory.

Things that must stay single-sourced: the industry table (`baseline.md`, written
only by `baseline.py write`), `CASH_DEBT_TH` (only in `fetch_fundamentals.py`;
never quote its numbers in `rating-rules.md`), `fetch_all.sh`'s ten unit argv
lines (never restated in `SKILL.md`), signals 14–18 (`crypto.py` /
`stock_perp.py` only — the deleted `.sh` versions come back via git history, never
into the tree).

## Cross-cutting rules

### Missing data is never quietly safe

Record `N/A` / `⚪️ 数据暂缺`, never estimate. ⚪️ counts toward neither numerator
nor denominator of the 7-threshold tally and carries a staleness figure in weeks.

### Fallback chains — the rule

Eight chains exist; a ninth follows these: (1) fixed, written order; (2) label
the tier in text and `--json`; (3) **Never merge tiers into one table**;
(4) thresholds don't travel between tiers — recalibrate with a date, or N/A;
(5) what a degraded tier can't support is N/A with a reason, never computed from
partial data; (6) falling back is loud; (7) a fresh lower tier beats a stale
higher one — no holdings cache. An *available* tier is not an *authoritative*
one: the issuer's own ETF sheet still has to be pulled.

The one deliberate exception to (4): FCF's TTM and annual tiers share
`CASH_DEBT_TH` (same line, same 12-month window) — not an oversight.

### JSON equivalence — the rule

`--json` carries everything the text output does, as fields; exit codes and
stderr are only redundant channels. (1) `ok` is never a literal — it is false
whenever any self-check failed; (2) a trigger that couldn't be evaluated is
`null` + reason, never `false`; (3) every text-branch prohibition is also a
field; (4) `--quiet` drops human rendering, never warnings — stderr stays live,
plus a top-level `degraded: bool` / `degraded_reasons[]`.

### Retrieval transport layer

The transport side returns readings and provenance only, never a 🟢🟡🔴 state,
threshold comparison or narrative; the schema enforces it. The two
`search-contract.md` are the skills' own contracts (edit normally, unlike other
`references/*.md`), but may only say *how* a caliber is carried, never restate
it. Cite other docs by anchor (`` `file.md`「verbatim phrase」 `` or
`` `file.md` §「heading」 ``), **never by line number**.

**`.claude/agents/search-transport.md` is optional.** `SKILL.md` names the
capability, never the mechanism — no `@agent-name` or `mcp__…` in any `SKILL.md`.
Without it the parent searches itself (same contract, same files). Never add
`isolation: worktree` to it. Announce a fallback in the report body when it
changes the data's caliber; only in run output when the data is byte-identical.

### Commands an agent types

Anything a `SKILL.md` tells it to run: no `&`, no `rm` with globs or variables,
no shell function definitions, no `for`/`while` loops — put them in a script.
Claude Code forces a permission prompt on these that no allow-rule can cover.
Open item: `ai-pullback-daily` still has this defect in step 1.1's `( … ) &` and
in its `while` join loop.

## Per-skill invariants

**`ai-industry-weekly`**
- `universe.json` is the single source of the ticker set *and row order*; never
  hardcode a row count. Flags: `hk_quote` routes price fields to `hk_quote.py`;
  `ratio_distorted` / `currency` mark ADR/FX rows whose PS/PB/cash-flow ratios
  are meaningless. Its `ratings` list sets the rating order, and
  `fetch_fundamentals.py` refuses to run unless it matches the cap rule's four tiers.
- `baseline.md` is its own seed. No `baseline.seed.md`, `history/`, `.bak` or
  snapshots — rollback is git.
- Run order: fetch → re-rate → validate → diff → report body → `write` → Slack.
- `baseline.py`: `write` validates first and on failure exits 1 untouched; **no
  `--force`, ever**. `diff` validates too; malformed input → exit 1, empty stdout
  (`--json` instead emits `ok:false` / `summary_produced:false` / `result:null`).
  Validation covers header, row count, ticker set and order, rating vocabulary,
  empty cells, lazy placeholders (「见 Slack thread」), and `universe.json` itself.
  `write` renders compact `| a | b |` itself. Parsing takes every `|` line, so a
  temp file holds one table. Output paths are relativized.
- Rating caps: `fetch_fundamentals.py` emits `cash_debt.caps`; `baseline.py
  --caps` refuses ratings above cap and caps files >7 days old or future-dated;
  without `--caps` it says so (`caps_checked: false`). `--caps` is repeatable:
  per ticker the most recently fetched file wins, and a missing ticker is judged
  against the union. `cap: null` means "no cap" only when `evaluated == "full"`;
  an applicable ticker not fully evaluated has an *unknown* cap (listed in
  `caps_unevaluated` + `degraded_reasons`) and may not be 🟢 if its FCF leg wasn't
  evaluated. The step-1 preview reads `baseline.md` only through `baseline.py`'s
  `Table` / `column_index` — never a second parser.
- Cap FCF: from the cash-flow statement, never `.info` `freeCashflow` (mapping
  `F`'s `FCF` field stays untouched, reference only). TTM uses the *same* four
  quarter-ends as the income statement; TTM → latest annual → N/A, each verdict
  carrying tier and period end; either tier older than `max_period_age_days` is
  refused; the TTM-miss reason goes in `fcf_fallback_notes`. Retry statements on
  a **fresh** `yf.Ticker` (yfinance caches empty frames). Leverage is net debt /
  EBITDA, not liabilities-to-assets.
- HK price / 52w high / drawdown come from `hk_quote.py`, never yfinance
  (dividend-adjusted).
- `fetch_fundamentals.py` imports yfinance lazily in `main()` and uses
  `requests.Session`, not curl_cffi.
- `etf_holdings.py`: Alpha Vantage → yfinance (top-N only) → issuer page, no
  cache. Under top-N, swap / non-US / cash-T-bill totals are N/A. Any holdings
  figure in a report quotes its `source` and fetch date; the issuer sheet wins on
  conflict. AV throttling is HTTP 200 with an `Information` body — inspect every
  body; classify burst wording before daily wording. Invalid and exhausted keys
  look identical, so the quota message must admit a mistyped key, and every key
  failing on its first call means suspect the key.
- `references/*.md` are verbatim migrations: edit only the line you must.

**`ai-pullback-daily`**
- Reads the weekly skill read-only (`baseline.md`, `universe.json`,
  `hk_quote.py`); never writes there and never copies the table.
- Every script needing the weekly install resolves it through `_weekly.py`.
  `AI_INDUSTRY_WEEKLY_DIR` set but invalid always errors, never falls back.
- `industry_table.py --check` fails loudly if the table is missing; it warns
  (exit 0) on >10 days stale, row count ≠ ticker count, or future date.
- `neocloud_credit_monitor.py` owns tripwire ④ outright; the report quotes its
  verdict and never second-guesses it.
- `technicals.py` bars: yfinance → `bars_fallback.py` (US / KR; HK stays on
  yfinance + `hk_quote.py`; indices → N/A). Fallback rows carry
  `vol_ratio_comparable: false` and suppress 「放量 ≥1.5x」.
- `references/*.md` (except `search-contract.md`) are verbatim migrations.

**`daily-risk-monitor`**
- Standalone: no sibling dependency. Shares no code with `ai-pullback-daily`,
  even though both read Hyperliquid's `xyz` pool — never merge the fetchers.
- Every fetch is its own standalone script. Per-host headers: FRED **never** a
  browser UA; Yahoo **always** one; CNN F&G browser UA + Referer + Origin. stdlib
  `urllib` fails TLS here; `requests` reaches FRED fine.
- `fred.sh` / `cape.sh` stay shell (no Python packages needed); parallelism uses
  `curl --parallel`. Don't port them.
- `fetch_all.sh list` must print commands runnable exactly as printed (with the
  `SCRIPTS=` / `RUN=` lines and real redirections). The units table's columns 2
  (stdout target) and 3 (payload file) differ only for `market` — `join` checks
  column 3; don't merge them.
- `market_fallback.py` refuses `DX-Y.NYB` and `GC=F` (→ N/A): never substitute
  `DTWEXBGS`, Tencent `usDX` (a company), GLD or PAXG. Its tiers land on
  different dates (signals 21/22 on D-1, 19/26 on D), so it must emit a date-skew
  warning into `degraded_reasons`.
- `stock_perp.py`: a short `assetCtxs` gives that market `MissingMarket` + exit 3,
  never an abort. Keep the resolved entry in `references/known-traps.md`.
- `assets/last_run.json` holds every signal's tier plus both tracks, written by
  `snapshot.py write` *before* the Slack push. Slack history is only a fallback;
  the skill must stay usable with no Slack at all.
- Signals 3 and 12 (thresholds 6 and 7) have no stable source — never fabricate.
- Before deleting any ported script, run a live interleaved `sh → py → sh → py`
  comparison; a fixture oracle doesn't cover the transport layer. Open item: the
  oracle (replayed via `RISK_FIXTURE_DIR` / `RISK_FIXTURE_NOW`) is not checked
  in, so a fresh clone cannot replay it.
- `references/*.md` (except `search-contract.md`) are verbatim migrations.

## Public repo

`BigtoC/my-skills` is public. Skills ship no config file; Slack channel ids come
only from the environment. No channel or routine ids in code, and no real channel
id, absolute home path or username in a report body. Every notifying skill reads
one variable, `NOTIFICATION_SLACK_CHANNEL_ID` (unset → full report, push
skipped); new skills reuse it. `AI_INDUSTRY_SLACK_CHANNEL_ID` and
`RISK_MONITOR_SLACK_CHANNEL_ID` must not come back. `.claude/settings.local.json`
is gitignored and must stay so.

`git push` uses the SSH alias `github.com-personal`; `gh` may be a different
account — check `gh auth status` before opening a PR.

## Commits and pull requests

- **Commits: [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/)** —
  `<type>[(scope)][!]: <description>`, blank line, body, blank line, footers.
  Types: `feat`, `fix`, `docs`, `chore`, `refactor`, `style` (`perf` / `test` /
  `ci` / `build` only when genuinely that). Scope is the skill directory, `claude`
  for `CLAUDE.md` / `.claude/`, `readme` for `README.md`; comma-separate for two.
  Routine data commits: `chore(<skill>): daily run YYYY-MM-DD`. Breaking (a CLI,
  `--json` shape or state-file format a caller would trip over) takes `!` and/or a
  `BREAKING CHANGE:` footer (that token uppercase). Footers are git trailers
  (`Token: value`), so `Co-Authored-By:` goes after a blank line.
- **PR titles name the problem, not the change**: `Problem: <the problem>` or
  `Problem(<skill>): …`. GitHub appends `(#NN)` — don't add it.
- **Each commit leaves every script runnable** (`python3 -m py_compile` plus the
  area's verification commands). Split by concern; when two files can't change
  apart, keep them in one commit and say so in the body.
