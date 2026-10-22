# ai-industry-weekly — design rationale

> Rationale record. The rules themselves live in the repo-root `CLAUDE.md`;
> this file holds the *why*: measurements, post-mortems and history. It was moved
> out of `CLAUDE.md` verbatim on 2026-10-22. Edit here when a rule's reasoning
> changes, and keep the one-line rule in `CLAUDE.md` in sync.

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
  caught it. Malformed input exits 1 with **zero bytes on stdout** — with one
  deliberate exception added 2026-09-07: `diff --json` on malformed input exits
  1 and writes an error object carrying `ok: false` / `summary_produced: false`
  / `result: null`. That is not a diff result and cannot be mistaken for one,
  and it exists because a caller narrow-reading stdout could not otherwise tell
  "refused" from "ran and found nothing". The plain (non-`--json`) branch is
  still byte-for-byte silent.
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

### Cash-flow / leverage rating caps (added 2026-10-22)

Low free cash flow and high debt now cap a ticker's rating. Five things about it
are load-bearing:

- **The thresholds live in exactly one place**: `CASH_DEBT_TH` in
  `fetch_fundamentals.py`. The rule body in `rating-rules.md` describes the rule's
  shape and deliberately quotes no threshold numbers; the script prints the dict
  into every run's 「现金流与负债 · 评级上限」 section and into `--json`
  `cash_debt.criteria`. This is the two-`TH`-dicts trap avoided by construction —
  do not copy the numbers into the rule body. Dated editor notes may record the
  *readings* a calibration was made on, never the thresholds themselves.
- **The cap is enforced, not requested.** The script emits `cash_debt.caps`
  (`{ticker: {cap, applicable, evaluated, fcf_evaluated, reasons, na_reasons}}`) and
  `baseline.py validate/diff/write --caps <fundamentals.json>` refuses any row
  rated above its cap, refuses a caps file older than 7 days, dated in the
  future or missing a ticker, and without `--caps` says so on stdout, stderr and
  in JSON (`caps_checked: false` plus a `degraded_reasons` entry). That JSON is
  the contract between the two scripts. `--caps` may be given more than once —
  a full fetch plus a `--tickers` rerun written to a *separate* file — and per
  ticker the entry from the most recently fetched file wins; a missing ticker is
  judged against the union. `cap: null` is ambiguous on its own — it
  means "no cap" only when `evaluated` is `"full"`; an applicable ticker that was
  not fully evaluated has an *unknown* cap, is listed in `caps_unevaluated` /
  `degraded_reasons`, and may not be rated 🟢 if its FCF leg was not evaluated —
  every cap seen at calibration came from the FCF leg, so a net-cash leverage
  reading cannot stand in for it. The rating order comes from `universe.json`
  `ratings`; `fetch_fundamentals.py` refuses to run if that list stops matching
  the four tiers the rule is written for. Its step-1 preview of rows that must
  come down reads `baseline.md` through `baseline.py`'s own `Table` /
  `column_index`, so the preview and the step-3 refusal cannot parse the table
  two different ways.
- **Statement retries must use a fresh `yf.Ticker`.** yfinance caches an empty
  frame from a failed fetch on the Ticker object, so retrying on the same object
  re-reads the cache without a single new request. Measured 2026-10-22: one
  transient failure moved INTC from TTM to annual FCF and its cap from 🔵 to 🟡.
  `_frame(t, attr, renew)` therefore re-instantiates on every retry.
- **FCF never comes from `.info` `freeCashflow`.** Measured 2026-10-22 it differs
  from the cash-flow statement's TTM `Free Cash Flow` in both directions
  (NVDA 13.8% vs 41.9% of revenue, NET 27.5% vs 12.5%) — probably Yahoo's levered
  FCF. The `FCF` field of mapping `F` is kept untouched (hard constraint 3) but
  is reference only. TTM must take the **same** four quarter-ends from the
  cash-flow statement and the income statement; picking each table's own "first
  four non-empty columns" misaligned MU's revenue to 0.68× of the real TTM. The
  order is TTM → latest annual (either rejected if older than `max_period_age_days`) →
  N/A, and the tier is labelled — HK rows and MRAAY have no quarterly cash flow.
- **Leverage is net debt / EBITDA, not 资产负债率.** Liabilities-to-assets counts
  customer prepayments and deferred revenue as debt (NET 75% while net cash,
  VRT 70%, ASML 57%) and would have downgraded VRT and ASML. It and D/E are
  printed as reference only.

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
