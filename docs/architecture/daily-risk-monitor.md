# daily-risk-monitor — design rationale

> Rationale record. The rules themselves live in the repo-root `CLAUDE.md`;
> this file holds the *why*: measurements, post-mortems and history. It was moved
> out of `CLAUDE.md` verbatim on 2026-10-22. Edit here when a rule's reasoning
> changes, and keep the one-line rule in `CLAUDE.md` in sync.

## Overview, per-host headers, state file, missing data

**`daily-risk-monitor`** — a daily cross-market risk sweep (macro/credit, positioning,
crypto, an anti-emotion layer, long-run valuation) feeding 7 hard sell thresholds
and a two-track decision layer. It is **standalone**: no sibling-skill dependency,
its own scripts, its own state file. Its `references/*.md` are verbatim migrations
from a private source document and carry the same edit-only-what-you-must rule.

Three things about it are load-bearing and easy to erode:

- **Fetching is split so each source can be run and debugged alone.** Every fetch
  lives in its own standalone script. **Standalone invocation is the requirement;
  the language is not.** The real constraints are **per-host headers, and they are
  language-independent** (all measured 2026-09-05 on this machine):

  | host    | rule                                         | evidence                                                                                                                                                                                  |
  |---------|----------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
  | FRED    | **never** send a browser User-Agent          | Chrome UA → 25–30s ReadTimeout, from curl *and* `requests`. curl-like or `requests`' default UA → HTTP 200 in ~0.5s                                                                       |
  | Yahoo   | **always** send one                          | a bare `requests.Session` gets throttled (`market.py`「多半是 Yahoo 限流挡了裸 Session」); `stock_perp.py`「Yahoo chart 端点本机实测稳定回」 records a stable 429 from the chart endpoint |
  | CNN F&G | needs full browser UA **+ Referer + Origin** | otherwise HTTP 418 「I'm a teapot. You're a bot.」                                                                                                                                        |

  These do **not** conflict at the language level — only per-host, which one
  program handles with per-host headers. `stock_perp.py` already straddles both
  (Hyperliquid JSON + FRED CSV).

  > An earlier version of this bullet claimed **"FRED must use curl (python
  > `requests` times out in this environment)"**. That is **false**: `requests`
  > returns FRED in 0.50s versus curl's 1.16s. The failing transport is stdlib
  > `urllib`, which hits `CERTIFICATE_VERIFY_FAILED` against FRED, multpl.com
  > *and* CNN alike — this environment's Python has no CA bundle wired to the
  > system store, so it is not a yfinance-specific fault. `urllib` + `certifi`
  > works (0.48s). Do not restore the old claim; it misdirects anyone debugging
  > FRED.

  What the language choice *does* turn on is dependencies: `fred.sh` and
  `cape.sh` need only bash + curl + awk and **no Python packages at all**.
  Porting those two would trade a satisfied dependency for `requests` — a
  portability loss for a repo whose purpose is portable skills. Keep them shell.
  Concurrency is not a reason to port either: `curl --parallel` with
  `-w '%{url_effective} %{http_code}'` fetches six FRED series in 1.08s versus
  3.26s serial **and keeps per-URL status attribution**, so the ⚪️ per-series
  accounting and `fred.sh`'s three-way ordering invariant survive with the
  reduction loop staying serial in the parent.
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

### `fetch_all.sh` owns the ten fetch invocations — and why they left SKILL.md

Step 1.1 used to be a ~20-line block pasted into one Bash call: a `bg()` function
definition, `mkdir` + `rm -f "$RUN"/*.json`, and ten `( … ) &` background
subshells. It is now one command, `scripts/fetch_all.sh launch`, with `join`
and `list` beside it. **The ten argv lines and their rationale live in that
script and nowhere else** — SKILL.md does not restate them, because two copies
of the unit list is the two-`TH`-dicts trap moved to the scheduling layer.

The reason it had to move is a host rule that no configuration can reach, and it
is worth writing down because it is invisible from the docs. Claude Code parses
**the command string the agent types**, not the script it runs. Verified by
reading the shipped binary (`~/.local/share/claude/versions/2.1.274`, 2026-09-17):

- A command whose AST contains a real background operator — a `&` node whose
  parent is not a `binary_expression`, so `&&` and `2>&1` are excluded — is
  forced to `behavior: "ask"` with `circuitBreaker: "backgroundOperator"` and
  `classifierApprovable: false`, **after** rule matching has already returned
  `allow`. So **no `permissions.allow` entry can ever cover it**, and the dialog
  drops the "don't ask again" row (`suppressAlwaysAllowRule`). Its registry
  entry is `bypassImmune: false`, so bypass-permissions mode does skip it —
  which is why this is invisible locally and fires in the cloud container.
- The sibling breaker `dangerousRemoval` (glob/variable `rm` targets) is
  `bypassImmune: true` — **that one asks even in bypass mode**.
- The only escape is `sandbox.autoAllowBashIfSandboxed` (defaults true), which
  is checked *before* the `&` rule — but it needs the command to be sandboxable,
  and these units need broad network egress.

So: **keep `&`, `rm` with globs, shell function definitions and `for`/`while`
loops out of anything SKILL.md tells an agent to type.** Put them in a script.
That is a portability-neutral move — SKILL.md still names a capability, not a
mechanism — and it is why `ai-pullback-daily`'s step 1.1 (one `( … ) &`) and its
join loop have the same defect, still unfixed.

Two properties of `fetch_all.sh` are load-bearing and easy to erode:

- **`list` must print commands that are runnable exactly as printed** — the real
  redirections (`>"$RUN/<unit>.json"`, `2>"$RUN/<unit>.err"`,
  `echo $? >"$RUN/<unit>.rc"`) plus the `SCRIPTS=` / `RUN=` lines that bind them.
  Those three redirections *are* the implementation of 守则 1–3, and `list` is
  the only remaining place a reader can see them now that SKILL.md does not carry
  the block. Print a prettified summary instead and the documented
  "run one unit alone to debug" and "serial by hand" paths silently stop working.
- **The units table's 2nd and 3rd columns are different on purpose.** Column 2 is
  the stdout target, column 3 is the file that carries the payload. They differ
  only for `market` (`market.out` vs `market.json`), and `join` verifies column 3.
  Merge them and a run where `market.py` wrote nothing still passes the
  "file exists and is non-empty" check on a one-line 「已写入 …」 stub.

One measured caveat for anyone promising fewer prompts: clicking "don't ask
again" on a **path-invoked script** saves the *exact full command line*, not a
`:*` prefix — measured across this machine's own accumulated rules
(`~/Documents/env/futu/.claude/settings.local.json` holds five separate exact
rules for one `get_kline.py`, differing only in flags, while `cargo build:*` and
`git add:*` in the same files are wildcarded because those are recognized tools).
So `launch` and `join` persist as two separate rules unless a wildcard rule is
hand-written. `.claude/settings.local.json` is now in this repo's own
`.gitignore` — it was previously covered only by the operator's machine-local
`~/.config/git/ignore`, which a fresh clone or CI does not have, and any rule in
it contains an absolute home path.

### crypto / stock_perp are Python now — the shell versions are gone

`scripts/crypto.py` and `scripts/stock_perp.py` are the **only** implementations
of signals 14–18. `scripts/crypto.sh` and `scripts/stock_perp.sh` were deleted
on 2026-09-07 when `SKILL.md` step 1.1 switched its two `bg` lines to
`python3 "$SKILL_DIR/scripts/crypto.py" all --json` and
`python3 "$SKILL_DIR/scripts/stock_perp.py" --from-fred --json`. **Rollback is
git alone** — the deleted files are in history; do not restore them into the
tree. Two implementations silently disagreeing about the same verdict is the
worst failure this skill family has (see the neocloud credit twins, whose two
`TH` dicts make every recalibration a two-place edit), and a second copy of
`crypto` / `stock_perp` would be that trap over signals 14–18.

The switch moved `jq` off the critical path: **`cnn_fng.sh` is now the only
script that needs it** (measured: 10 invocations; `fred.sh` and `cape.sh` have
zero — their sources are CSV and HTML). `crypto.py` / `stock_perp.py` need
`python3` + `requests` and no `jq` / `awk` / `curl`.

Where the language split falls, and why — settled, not to be re-litigated:

- **`fred.sh` and `cape.sh` stay shell.** They need no `jq` and **no Python
  packages at all**, and this environment's stdlib `urllib` cannot reach these
  hosts, so porting them would add `requests` for no gain — a portability loss
  in a repo whose purpose is portable skills.
- **Concurrency is not a reason to port.** `curl --parallel` with
  `-w '%{url_effective} %{http_code}'` keeps **per-URL status attribution**, so
  the ⚪️ per-series accounting and `fred.sh`'s ordering invariant survive in
  shell.
- **What Python uniquely bought is two things curl cannot express.**
  *Heterogeneous-block parallelism*: `crypto.py` fires its four blocks at four
  different hosts, fences each future separately, and reduces `OKCOUNT` /
  `MISSING` in fixed submission order — one bad field can no longer discard
  three good blocks. And *named fields* replacing the old positional TSV, whose
  one row was addressed under **three incompatible index bases** (awk
  `$1…$16`, jq `.[0]…[15]`, and a `read -r` variable list).

The divergence that made lingering untenable is now closed. When Hyperliquid's
`assetCtxs` array is shorter than `universe`, the shell version let `jq`'s
`transpose` pad with null, the empty fields survived into the `--json` branch's
`tonumber`, and `set -euo pipefail` propagated jq's exit **5** — a code this
repo does not reserve — with unparseable JSON on stdout. `stock_perp.py` records
that market as `MissingMarket` with a reason, keeps both markets in the output,
and exits **3**. That defect is therefore gone rather than fixed; the record of
it stays in `skills/daily-risk-monitor/references/known-traps.md`, marked
resolved-by-migration, because it documents a failure that really happened.

**A migration oracle is not sufficient evidence to switch. Run a live
interleaved comparison first.** The oracle — frozen upstream payloads plus a
golden stdout / stderr / exit code per case, replayed offline through the
`RISK_FIXTURE_DIR` / `RISK_FIXTURE_NOW` hook both versions carry — reached
119/130 green while `stock_perp.py` was **100% broken against the real
network**: its `http_request` returned `False` on success, but the contract is
"a non-`None` `fail_reason` means failure", so every successful fetch was read
as a transport-layer failure. The fixture path returned `None` there, so the
oracle never exercised it. The oracle covers *parsing and rendering*; it cannot
cover the transport layer it replaces. So for any future port: alternate
`sh → py → sh → py` against live upstreams and diff every decision-relevant
field before deleting anything. The oracle still **currently lives outside the
repo, in scratch space** — no fixture or golden file is checked in, so a fresh
clone cannot replay it.
