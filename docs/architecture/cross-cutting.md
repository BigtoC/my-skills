# Cross-cutting rules — rationale

> Rationale record. The rules themselves live in the repo-root `CLAUDE.md`;
> this file holds the *why*: measurements, post-mortems and history. It was moved
> out of `CLAUDE.md` verbatim on 2026-10-22. Edit here when a rule's reasoning
> changes, and keep the one-line rule in `CLAUDE.md` in sync.

### Known duplication in the daily scripts — not yet converged

Only *sibling-skill location* has been pulled into `_weekly.py`. The other
shared helpers are still copy-pasted, and that is the current state of the code,
not an oversight waiting to be discovered:

- **`scrub()`** (folds `$HOME`-ish absolute paths out of error text) is defined
  **fourteen times** (measured 2026-09-15 by `grep -rl _HOMEISH_RE --include='*.py' skills/`
  — not a remembered number; re-run the grep):
  `etf_holdings.py`, `bars_fallback.py`, `industry_table.py`,
  `neocloud_credit_lite.py`, `neocloud_credit_monitor.py`, `perp_quotes.py`,
  `run_state.py`, `source_probe.py`, `technicals.py`, `crypto.py`,
  `market_fallback.py`, `market.py`, `snapshot.py`, `stock_perp.py`.
- **`rel_display()`** exists in **six** copies: the shared one in `_weekly.py`
  (imported by the scripts that need the weekly install) plus private
  definitions in `neocloud_credit_lite.py`, `neocloud_credit_monitor.py`,
  `run_state.py`, `market.py` and `snapshot.py`. The credit scripts do not import `_weekly` at
  all — they read only this skill's own `assets/`, so they have no reason to
  depend on the weekly lookup.

Treat these as fourteen and six separate implementations: a fix to path scrubbing
(the public-repo leak rule below) is an N-place edit, and grepping for
`_HOMEISH_RE` finds every copy.

⚠️ **Count the grep, do not trust this list.** It said five and three until
2026-09-07 and ten and five until 2026-09-15, by which point the real numbers were
fourteen and six — each time, new scripts had been added without updating it. Path
scrubbing is the public-repo leak rule, so a missed copy publishes a home
directory into a Slack-pushed report; a stale count here is the most expensive
kind of documentation rot in this file. Converging them is fine, but `neocloud_credit_lite.py`
is the standard-library cloud variant and must not gain an import that ties it
to the rest of the script directory.

## Fallback chains — the rule

Several data points in this repo have no single reliable source, so they are
fetched through an ordered chain. Eight exist today: HK prices (`hk_quote.py` →
yfinance, derived indicators only, never the price), VIX (FRED `VIXCLS` →
yfinance `^VIX`), funding rates (Binance → Hyperliquid → coinglass search), BTC
dominance (CoinGecko → CoinPaprika), ETF holdings (Alpha Vantage → yfinance
→ issuer page), and **daily OHLCV bars** (`technicals.py`: yfinance →
`bars_fallback.py` — stockanalysis.com for US, Naver siseJson for KR; HK stays on
yfinance + `hk_quote.py`; indices have **no** second tier and go N/A).

The sixth was added 2026-09-11 for a reason worth stating, because it is the only
one where changing the *transport* cannot help: yfinance is **structurally**
unreachable from the Routines container. Its data host is hardcoded
(`yfinance/const.py: _BASE_URL_ = query2.finance.yahoo.com`, used by `base.py` and
`scrapers/history.py`), and that container's egress proxy kills every query2 tunnel
(~1.8KB ClientHello out, 39 bytes back, code 1006 at ~6s). Both engines —
`requests.Session` and `curl_cffi` — hit that same blocked host, so only a
different **upstream** fixes it. Its two tiers share a price caliber
(split-adjusted, dividend-unadjusted; verified across 7 in-window splits, and
across all 46 tickers **0 differing T1/T2/T3 verdicts**) but **not** a volume
caliber, so fallback rows carry `vol_ratio_comparable: false` and the
「放量 ≥1.5x」 label is suppressed on them — rule 4 in action.

A **seventh** chain, added the same day, is `daily-risk-monitor`'s
`market.py` → `market_fallback.py` (SPY/RSP/TLT/GLD/UUP → stockanalysis;
`^GSPC` → Tencent `us.INX`; `BTC-USD` → Binance `BTCUSDT`). It is a **separate
implementation from `bars_fallback.py` on purpose** — `daily-risk-monitor` is
standalone by design and must not import from `ai-pullback-daily`, so this is a
two-place edit in the same sense as the two `TH` dicts. Two things about it are
load-bearing:

- **It refuses two tickers rather than failing on them.** `DX-Y.NYB` has no
  same-magnitude substitute (`market.py`'s own docstring already forbids
  `DTWEXBGS`), and Tencent's `usDX` is a **name-collision trap** — it returns
  「德尼克斯投资」(DX.N), a company, not the dollar index. `GC=F` has none either
  (GLD is an ETF with fees and premium/discount, PAXG is tokenized gold; swapping
  the numerator redefines the Gold/SPX ratio). Both are refused *with the reason
  written into the module*, so nobody later wires up a wrong-magnitude source.
- **Its tiers have different as-of dates.** Measured: stockanalysis's ETFs lag
  Tencent/Binance by one trading day, so signals 21/22 land on D-1 while 19/26
  land on D. Each signal is internally date-aligned (`anchor` + `pct_change_on`),
  but cross-signal comparison is not, so the fallback emits an explicit
  date-skew warning into `degraded_reasons`. This is rule 3 in its sharpest form.

An **eighth**, added 2026-10-22, is `fetch_fundamentals.py`'s FCF margin for the
cash-flow/leverage rating caps: cash-flow-statement TTM (four quarter-ends aligned
with the income statement) → latest annual statement → N/A. Both tiers are the
same line (`Free Cash Flow` / revenue) over a 12-month window and differ only in
as-of date, which is why the same `CASH_DEBT_TH` thresholds apply to both — the
one place rule 4 is deliberately *not* invoked, and the reason is written here so
it is not mistaken for an oversight. Every verdict carries the tier and period
end, a period older than `max_period_age_days` is refused on either tier (rule 7),
and the TTM-unavailable reason is recorded in `fcf_fallback_notes` (rule 6).

They were each written separately; these constraints are common
to all of them, and a ninth chain should follow them rather than reinvent one.

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

## JSON equivalence — the rule

A script's `--json` must carry everything its human output carries. Every
warning, prohibition, verdict and caliber declaration that the text branch
prints must exist as a **field**. Exit codes and stderr are *redundant*
channels, never the only one: any narrow read, any handoff between steps, and
any concurrent run sees stdout alone.

This was audited across all 16 scripts (2026-09-05) and 9 of them failed. The
defects came in two shapes, and the first is the dangerous one because it looks
fine:

- **Self-contradictory** — the obvious field lies while the truth sits in a
  less obvious one. `cnn_fng.sh` emitted
  `triggers.hard_threshold_4_greed_burst: false` when it could not evaluate the
  trigger at all (only `peak.value: null` gave it away) — and that is *hard
  threshold #4*, where ⚪️ must be deducted from the denominator (`N = 7 − M`)
  and `false` is not. `fred.sh`, `cape.sh` and `stock_perp.sh` (the since-deleted
  shell version — this bullet is the 2026-09-05 audit record, kept as written)
  all hardcoded `"ok":true` while a conditional `sanity.pass` said otherwise.
- **Genuinely absent** — no field at all. `neocloud_credit_monitor.py` popped
  the `cfg` key its own "no live CDS data (this layer's biggest blind spot)"
  line derives from; `snapshot.py show --json` dumped raw state and skipped
  every derived banner, making `--json` strictly *less* than `show`.

Four rules follow:

1. **`ok` is never a literal.** It is false whenever a self-check failed.
2. **A trigger that could not be evaluated is `null`, never `false`.** `false`
   means "checked, did not fire". Pair it with a reason string. This is the
   JSON form of the ⚪️ / ❌ distinction the reference docs already insist on.
3. **Every prohibition in the text branch is also a field.** If the human
   output says "do not divide these two", the JSON says so too.
4. **`--quiet` means "no human-readable rendering on stdout". It never means
   "no warnings".** stderr stays live, and every degradation is additionally a
   structured field plus a top-level `degraded: bool` / `degraded_reasons[]`.

Rule 4 matters most under concurrency, which is exactly what provokes
throttling: if `--quiet` silenced the ⚠ lines and a caller read only a summary
field, a throttled run would become a silently normalised one — the failure
mode fallback-rule 6 and the ⚪️ accounting exist to prevent, triggered by the
change meant to make things faster.

Design record: `docs/superpowers/specs/2026-09-05-skill-handoff-and-concurrency-design.md`.

## Retrieval transport layer

The two daily routines fetch a large part of their signals with WebSearch /
web_fetch. Raw search results are prose, ads and stale reposts — tens of KB per
item — and letting them into the context that writes the report both drags the
verdict toward the snippet's own wording and dilutes the source and as-of date
that `output-format.md` requires on every line. So retrieval is split into
**transport** and **judgement**: the transport side returns readings and
provenance as structured fields, never a 🟢🟡🔴 state, a threshold comparison, a
「一句話解讀」, or narrative. The shape is enforced by the schema having no such
fields, not by a prompt asking nicely.

### The contract is deliberately duplicated — a TWO-PLACE EDIT

The contract lives in **two files, one per skill**:

- `skills/ai-pullback-daily/references/search-contract.md`
- `skills/daily-risk-monitor/references/search-contract.md`

This is not drift waiting to be cleaned up. `daily-risk-monitor` is standalone
by design — the rule above says it shares no code and no sibling-skill
dependency with `ai-pullback-daily` — so the contract **cannot** be hoisted into
one shared file without creating exactly the dependency that rule forbids.

Treat it the way the two `TH` threshold dicts are treated: **a change to the
common envelope is a two-place edit.** The common part is §2 (the envelope
fields), §3 (`threshold_comparable`, `carry_forward_policy`), §4 (the sentinel
is chosen by the caller, not the fetcher), §5 (what is excluded), and the
one-line handoff receipt. The per-skill parts — the group tables, the dispatch
lists, the payload families actually used, the per-group caliber notes — are
genuinely different and must not be homogenised. Two copies silently disagreeing
about the envelope is the same failure mode as the two `TH` dicts disagreeing
about tripwire ④: whichever copy the day's run happened to read is what the
report presents as fact.

Note the copies use **different sentinel sets** (`daily-risk-monitor` has three,
`ai-pullback-daily` four, including `（无）`), so a sentinel change is a real
per-skill decision, not a copy-paste.

Unlike `references/*.md`, these two files are **not** verbatim migrations from
the private source document — they are the skills' own handoff contracts and can
be edited normally. But a contract may only say **how** a caliber is carried,
never restate or amend the caliber itself; every rule it cites has to be checked
back against the migrated `signals-*.md` / `tripwires.md` / `neocloud-credit.md`
files that own it.

Those citations are written as **anchors, never line numbers**:
`` `file.md`「verbatim phrase」 `` or `` `file.md` §「heading」 ``. Both quote bytes
that exist in the target, so every citation in the file can be re-checked by
grepping for it — which is the same "count the grep, do not trust the list"
discipline the `scrub()` / `rel_display()` counts above are under.

This was not free advice. On 2026-09-17 `daily-risk-monitor`'s contract held 59
line-number citations and **19 of them had rotted** — `SKILL.md:140` and `:174`
pointed at blank lines, and `signals-a-macro.md:40-41` was cited for 「无历史基准」
while actually pointing at the HY/IG/BBB caliber trap. Most had been wrong since
before that day's edits; a rewrite of `SKILL.md` step 1.1 then moved several more.
A rotted line number is the expensive kind of wrong because it **looks exactly
like a correct one**: the reader jumps, lands on something unrelated, and
concludes they looked in the wrong place rather than that the document is wrong.
The sibling `ai-pullback-daily` contract never used line numbers and so never
rotted — that is the form to copy.

One scoping note for anyone repeating this: it was a **one-place** edit despite
the two-place rule, because the 依据 citation column exists only in the
`daily-risk-monitor` copy, and the §2 envelope field list is identical in both
set and order across the two files. Verify that before assuming either way.
Still open, and genuinely two-place: the `daily-risk-monitor` copy's `as_of`
field has lost the comment `# 数据自身的日期，不是取数日` that the
`ai-pullback-daily` copy still carries.

### `.claude/agents/` is optional and must stay that way

`.claude/agents/search-transport.md` is the Claude Code mechanism that executes
one group of the contract. It is a **Claude Code-only optional enhancement**:
it is not part of the portable `SKILL.md` layer, and the README's own documented
install path (`cp -r skills/<name> ~/.claude/skills/`) does not carry it.

- **`SKILL.md` names the capability, never the mechanism.** Keep the repo's
  existing neutral wording (`WebSearch / web_fetch`, `a Slack MCP server`,
  `python3 + yfinance`, `jq`). The moment an `@agent-name` or an `mcp__…`
  identifier appears in a `SKILL.md`, the portable layer is dead. Do not add
  this agent to any `SKILL.md` by name.
- **Degrading means the parent searches for itself** — same contract, same JSON
  file, same report bytes.
- **`isolation: worktree` is forbidden in that definition**, and the reason is in
  a comment there: the daily state files have inconsistent git status.
  `last_run.json` and `dominance_history.jsonl` are **untracked**, so a worktree
  shows a "first run" world; `neocloud_credit_history.jsonl` **is tracked**, so a
  worktree shows the last committed state and the day's append evaporates with
  the tree. The tracked one is the more dangerous of the two failures precisely
  because it looks normal.

### Where a missing transport layer gets announced — a deliberate exception

The absence of the transport layer goes into **run output, not the report body**.
That deviates from the repo's "falling back must be loud" rule, so the criterion
is written down here to stop it eroding into "no fallback needs announcing":

> **Announce a fallback in the report body when it changes the data's caliber;
> announce it only in run output when the data is byte-identical.**

`AV_API_KEYS` being unset changes the caliber (full holdings → top-N), so it must
appear in the body. The transport layer being absent changes only *where* the
searching happened, so putting it in the body is noise. Every other fallback in
this repo changes caliber and therefore still goes in the body.

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
