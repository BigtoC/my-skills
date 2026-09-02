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

`rust-best-practices` is pure documentation. The two finance skills are not —
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
- **Cross-skill dependency.** `ai-pullback-daily` does not carry its own quality
  table. It reads the industry ratings from
  `skills/ai-industry-weekly/assets/baseline.md` and the ticker list from that
  skill's `assets/universe.json`, and it calls that skill's
  `scripts/hk_quote.py` for Hong Kong prices. Install both skills side by side
  (or point `AI_INDUSTRY_WEEKLY_DIR` at the weekly skill) — the daily skill's
  preflight check fails loudly if it cannot find them.
- **Slack channel via environment variable.** The optional Slack push reads the
  channel id from `AI_INDUSTRY_SLACK_CHANNEL_ID`; nothing is stored in the repo:

  ```sh
  export AI_INDUSTRY_SLACK_CHANNEL_ID=C0XXXXXXXXX
  ```

  Both skills read the same variable. Leave it unset and they still run and
  print their full reports — they just skip the Slack push.

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
