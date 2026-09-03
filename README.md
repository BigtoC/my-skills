# my-skills

Portable Agent Skills repo.

This repository uses the open Agent Skills layout so the skills are portable
across compatible agents and suitable for discovery on services such as
`skills.sh`.

## Layout

Skills live under `skills/<skill-name>/SKILL.md`.

Optional supporting material goes in sibling folders such as:

- `references/` for on-demand documentation
- `scripts/` for helper executables
- `assets/` for templates and static resources

## Skills

| Skill                 | Path                                  | Purpose                                                                                                                                                                                                                                                                                |
|-----------------------|---------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `rust-best-practices` | `skills/rust-best-practices/SKILL.md` | Portable Rust coding, review, refactoring, API design, testing, and performance guidance.                                                                                                                                                                                              |
| `ai-industry-weekly`  | `skills/ai-industry-weekly/SKILL.md`  | Weekly re-rating routine for the AI compute supply-chain quality table: fetch fundamentals, re-rate every ticker, diff against a rolling baseline, and publish the result.                                                                                                             |
| `ai-pullback-daily`   | `skills/ai-pullback-daily/SKILL.md`   | Daily AI-compute pullback entry monitor: thesis tripwires, 24/7 perp implied moves, a four-layer neocloud credit read, and per-ticker T1/T2/T3 triggers, bucketed through quality, thesis, and pacing gates into a two-tier report. Reads its quality table from `ai-industry-weekly`. |
| `daily-risk-monitor`  | `skills/daily-risk-monitor/SKILL.md`  | Daily cross-market risk sweep: 30 signals across macro/credit, positioning, crypto, an anti-emotion layer, and long-run valuation, resolved into 7 hard sell thresholds and a two-track decision layer (strategic baseline × tactical coefficient = target position). Standalone, no sibling skill required. |

## Installation

### Via skills.sh

Install all skills in this repo:

```sh
npx skills add https://github.com/BigtoC/my-skills
```

Install a single skill:

```sh
npx skills add https://github.com/BigtoC/my-skills --skill rust-best-practices
```

### Manual

Copy or vendor the skill directory into your agent's preferred skills path:

```sh
cp -r skills/rust-best-practices ~/.claude/skills/
```

## Runtime state

`rust-best-practices` is pure documentation. The three finance skills are not —
they keep runtime state that changes on every run:

- **Rolling baseline.** `skills/ai-industry-weekly/assets/baseline.md` is
  overwritten by the skill on every run, so each week is compared against the
  previous week rather than a frozen seed table. Seeing it modified in
  `git status` after a run is expected. Commit it to advance the baseline, or
  `git checkout <commit> -- skills/ai-industry-weekly/assets/baseline.md` to
  roll back.
- **Neocloud credit state.** `ai-pullback-daily` keeps runtime state too.
  `skills/ai-pullback-daily/assets/neocloud_credit_history.jsonl` gains one
  record per run (a same-day rerun overwrites its own record), so seeing it
  modified in `git status` is normal and committing it is what preserves the
  history the change-rate checks rely on.
  `skills/ai-pullback-daily/assets/neocloud_bonds.json` is different: no script
  ever writes it. Its `quote` fields are updated **by hand** from bond quotes
  found on the web, and a quote older than five days is treated as stale and
  excluded from the verdict — so a run whose credit layer reads ⚪ usually means
  this file needs a fresh quote, not that the fetch failed.
- **Yesterday's signal tiers.** `skills/daily-risk-monitor/assets/last_run.json`
  **does not ship with the skill.** Before the first run `assets/` holds nothing
  but a `.gitkeep` placeholder; the file is created the first time
  `scripts/snapshot.py write` succeeds, so a first run reporting "no baseline
  from yesterday" is expected rather than a broken install. From then on it
  is rewritten by `scripts/snapshot.py write` at the end of every run — it holds
  each signal's tier plus the two track readings (strategic baseline, tactical
  tier), and it is what the next run's step 0 compares today against. Seeing it
  modified in `git status` after a run is expected. The write lands *before* the
  Slack push, so a failed push never loses the day's tiers. Reading Slack history
  is only the fallback for when this file is missing, so deleting it costs a day
  of tier-to-tier comparison, not the report itself.
- **BTC dominance history.** `skills/daily-risk-monitor/assets/dominance_history.jsonl`
  also **does not ship with the skill**. `scripts/crypto.sh dominance` appends one
  record per day (a same-day rerun overwrites its own record, and the file is
  capped at 90 records), because signal 16's "7d drop > 3%" leg has no free
  same-caliber source: what the free tier lacks is the market-cap *history
  series*, not today's dominance, and swapping in another provider would mix
  denominators (CoinGecko and CoinPaprika measured 59.1% vs 56.9% on the same
  day — about 2pt apart, against a 3% threshold). So the script accumulates its
  own same-source history instead. Until seven days have accumulated the 7d leg
  reads ⚪ "history too short (N days so far)" — that is correct output, not a
  failure, and it must never be read as "not triggered". The script also refuses
  to compare across sources: if the record seven days back came from a different
  provider than today's, it reports ⚪ rather than subtracting two different
  denominators. Seeing the file modified in `git status` is normal, and
  committing it is what preserves the history the 7d leg depends on —
  `git checkout`-ing it away costs seven days of re-accumulation.
  `scripts/crypto.sh dominance --history` prints what has accumulated (no
  network) when you need to check.
- **Cross-skill dependency.** `ai-pullback-daily` does not carry its own quality
  table. It reads the industry ratings from
  `skills/ai-industry-weekly/assets/baseline.md` and the ticker list from that
  skill's `assets/universe.json`, and it calls that skill's
  `scripts/hk_quote.py` for Hong Kong prices. Install both skills side by side
  (or point `AI_INDUSTRY_WEEKLY_DIR` at the weekly skill) — the daily skill's
  preflight check fails loudly if it cannot find them.
- **Slack channel via environment variable.** The optional Slack push reads the
  channel id from an environment variable; nothing is stored in the repo:

  ```sh
  export AI_INDUSTRY_SLACK_CHANNEL_ID=C0XXXXXXXXX   # ai-industry-weekly + ai-pullback-daily
  export RISK_MONITOR_SLACK_CHANNEL_ID=C0XXXXXXXXX  # daily-risk-monitor
  ```

  The two AI-compute skills share `AI_INDUSTRY_SLACK_CHANNEL_ID`.
  `daily-risk-monitor` is a general market-risk routine rather than part of that
  pair, so it reads its own `RISK_MONITOR_SLACK_CHANNEL_ID` — point it at the
  same channel if you want them together. Leave a variable unset and the skill
  still runs and prints its full report — it just skips the Slack push.

## Usage

Once installed, agents that load skill descriptions at startup will
auto-invoke `rust-best-practices` whenever you work on `.rs` files or Rust
code — no extra prompt needed.

You can also invoke it explicitly:

```text
Use the /rust-best-practices skill to review this Rust module and refactor it safely.
```

### Claude Code hook (optional)

For guaranteed auto-invocation in Claude Code regardless of context, see the
[Claude Code Auto-Trigger Setup](skills/rust-best-practices/SKILL.md#claude-code-hook-based-enforcement)
section in the skill's `SKILL.md`.

## Specification

This repo follows the `SKILL.md` format described by the open Agent Skills
specification: https://agentskills.io/specification
