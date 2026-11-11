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

## Run modes and the pre-market layer

The routine can run more than once per calendar day. Each run sets
`RUN_MODE` from `session.US.state`, using the same one-liner in three places
in `SKILL.md`: `intraday` maps to `intraday`, and every other state
(`pre_market`, `pre_open`, `post_close`, `closed`) maps to `postclose`.
`postclose` runs are authoritative. They write the credit history, a
`run_state.py` record and the git commit. `intraday` runs do none of these and
only add the ⏳ intraday overlay.

**A US pre-market run is `postclose` plus a ⏳ pre-market overlay. It is not a
third mode.** This was the user's decision (2026-11-11). Before the open, the
D-1 close is final, so every side effect a `postclose` run produces is still
correct. The git history also shows why a third mode would cost real data:

- DST ended on 11-01. Since then the morning slot (14:1x UTC) lands at 09:1x
  ET, which is pre-market.
- On 11-02 and 11-03 that pre-market run was the only one committed. On 11-05,
  11-06 and 11-09 nothing was committed.
- A non-writing pre-market mode would drop about half the days from
  `neocloud_credit_history.jsonl`, and with them the L1 30-day rate check.

A pre-market run followed by a post-close run on the same day is safe, and it
has already happened (11-04: 2dfa0a8 at 14:11 UTC, 242bd39 at 22:09 UTC).
`run_state.py` replaces the same `(date, postclose)` key. The credit history
overwrites the same-date record. The gate baseline is the latest postclose run
dated strictly before today, so the morning run cannot take the evening run's
alert banner. `run_state.py` `MODES` and the three one-liners stay unchanged.

What the user asked for is in the report. During pre-market the **latest
pre-market trade is the headline price**: drawdown from the 52-week and 20-day
highs, distance to T1/T2, MA position, and the perp-vs-premarket basis.
**T1/T2/T3 and the buckets stay on the last complete close.** Pre-market
crossings go into a separate 「🔶 盘前触及 · 未确认」 list (the name the code's
`live.touched_title` prints). This is the intraday
overlay model, field for field (`live_kind = "premarket"`), so the hard rule
「绝不谎报新收盘数据、绝不制造新触发」 needs no exception.

The plain touch check (`touched_live`: price at or below the trigger) cannot
feed that list. In the 06:08 ET audit run on 2026-11-11 it listed NVDA, whose
T1 and T2 had already triggered at the D-1 close, as a pre-market touch, so
the report presented a confirmed trigger as new and unconfirmed. TSM had
triggered T1 at the close but was trading back above it pre-market, and
nothing flagged it. The overlay therefore splits touches in two, **per
trigger level** (a name triggered only on T1 at the close that newly crosses T2
is a new T2 touch: an escalation, not a repeat). The split is shared code, so
the intraday overlay gets it too:
- new touches, not triggered at the close (`t1_new_touch_live`,
  `t2_new_touch_live`, `new_touched_live`), which feed 🔶;
- reclaims, triggered at the close but back above the trigger
  (`t1_reclaimed_live`, `t2_reclaimed_live`, `reclaimed_live`), which go in
  their own 「已收回 · 未确认」 list.

Names already triggered at the close stay in their buckets and are not
announced again.

### Detecting pre-market: a clock window, not `state`

`session.US.premarket_window` is true only on a weekday from 04:00 ET until
the open. `premarket_active` records whether any pre-market trade dated today
was actually found. The overlay is gated on these two fields, not on `state`.
Measured 2026-11-11 05:38–05:40 ET:

- Equity daily bars dated today are NaN before the open, and `frame_for` drops
  them. DXY and VIX do have real daily bars dated today. Those two bars are
  what turns `state` into `pre_market`. Without them it would be `pre_open`.
  Neither state says anything about equity pre-market trades. The old overlay,
  which read the unfinished daily bar, therefore attached nothing.
- `market_session()` had no weekday check. Weekend runs before 16:00 ET came
  out `pre_open`. The weekday check now makes weekends `closed`, which
  `SKILL.md` already claimed.
- There is still no holiday calendar. On a weekday holiday, `premarket_window`
  is true but no trades are found (`premarket_active = false`, plus a ⚠
  reason). So a holiday never gets an overlay, but it is reported as degraded
  rather than passing silently. The title still says 「盘前」 unless the writer
  has confirmed that the market is closed for a holiday, in which case it says
  「休市」. Outside the window the state does not settle the question either:
  on a holiday, a DXY or VIX bar dated today can make the state `pre_market`,
  or `intraday` after 09:30.
- On a weekday from 00:00 to 04:00 ET the window is false, but `state` can
  already be `pre_market`, and then its label is 「盘前」. That run has no
  overlay, so `SKILL.md` titles it 「开盘前」 and does not copy the label.

### The source and its caliber

The source is `yf.download(..., period="1d", interval="1m", prepost=True,
auto_adjust=False)`. Measured facts:

- It returns 1-minute bars from 04:00 ET. NVDA's last pre-market trade was
  177.95 at 05:38, against a 168.89 close.
- Volume is reported as 0, so a pre-market volume ratio is null with a reason.
- The index is tz-aware America/New_York. The daily index is tz-naive, so the
  two are never unioned (that raises a TypeError).
- The last row can be the minute still in progress.
- `^GSPC`, `^NDX` and `^TNX` have no pre-market bars. A 1m request for them
  returns only the previous session, so they are N/A. SPY, QQQ, ES and NQ are
  never substituted for them: those are different instruments.
- In a batch that mixes markets, HK and KR 1m bars are converted to ET and come
  out dated today. Rows are therefore filtered by market and by the
  04:00–open window, not by date alone.
- Locally, the tier-1 `requests.Session` path got a 429 and the tier-2
  curl_cffi path worked. The 1m call uses the same tier chain as the daily
  download, with its own fallback note.

Comparing a pre-market trade with daily-history thresholds is a caliber
crossing. The trade is unadjusted. The 52-week high, 20-day high and MAs come
from the split-restored, dividend-unadjusted daily series. Each overlay row
states this in `live_price_caliber` and carries `threshold_comparable: false`.

Rows whose daily bars came from a fallback tier (`bars_source != "yfinance"`)
get N/A instead of a pre-market price. Pairing a Yahoo pre-market price with
stockanalysis thresholds would merge tiers (fallback rules 3 and 4).

T3 is null, because RSI is defined on closes. The macro drivers (SMH−QQQ
±1.5pt, the discount-rate signal) and the quiet rule
「大盘当日涨幅>1.5%」 are calibrated on whole-day moves. Pre-market readings
never feed them. They live in their own top-level `premarket` block, never in
`macro.*` or `indices.*`.

### Perps in a pre-market run

The perp gap is still measured against the last complete close, and the
|≥2%| / |≥5%| thresholds still apply: the window is still close-to-now. Only
the wording changes, to 「若**今日**以此价开盘」 for US rows.

The mode follows `premarket_active`, not the window. This keeps holidays safe,
but there is a cost. When the window is open and `premarket_active` is not
true, the perp falls back to overnight mode and its US forecasts say
「若明日以此价开盘」. That happens when the 1m fetch failed or
`--no-live-overlay` was passed (`premarket_active` is then false or null). On
an ordinary trading day the next open is today. Without a holiday calendar the
code cannot tell the two cases apart. So `perp.json` carries a `mode_note`,
and `SKILL.md` tells the writer not to state 「明日」 for US rows unless a
holiday has been confirmed.

KR (and any other non-USD) forecasts used to compare the USD perp price with
T1/T2 in KRW. Every KR name that had not triggered got a false forecast,
printed with a `$` sign. The comparison now converts the perp price to local
currency with the row's `fx_rate` before comparing.

The pre-market trade and the perp are different instruments. They are shown
side by side (`premarket_px` / `premarket_pct` and `perp_premarket_basis_pct`)
and are never merged into the implied figure. No threshold applies to them.

`perp_quotes.py` reads the mode only from `tech.json` (`premarket_active`) and
never from the clock: the spot baseline and the session must come from the
same fetch. Loading `--spot` before the perp fetch also fixed an older crash.
The fetch-failure JSON path used `intraday` before it was assigned, which
raised UnboundLocalError and exited 1 with no JSON.

### Bond quotes in a pre-market run

The main bond-quote source is a set of European venues, which open around
02:00 ET. A quote dated today and found pre-market is an intraday indicative
price. So even though the run is `postclose`, `quote_kind: "close_clean"` is
written only when `as_of` is earlier than RUN_DATE (the ET calendar day).
Otherwise it is `intraday_indicative`. `quote_kind` is sticky, so a mistake
lasts. An intraday price written as `close_clean` can become the L1 baseline 30
days later. A clean price written as `intraday_indicative` is kept out of the
30-day baseline for good.
