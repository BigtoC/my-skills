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

| Skill | Path | Purpose |
| --- | --- | --- |
| `rust-best-practices` | `skills/rust-best-practices/SKILL.md` | Portable Rust coding, review, refactoring, API design, testing, and performance guidance. |
| `ai-industry-weekly` | `skills/ai-industry-weekly/SKILL.md` | Weekly re-rating routine for the AI compute supply-chain quality table: fetch fundamentals, re-rate every ticker, diff against a rolling baseline, and publish the result. |

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

Most skills here are pure documentation. `ai-industry-weekly` is not — it keeps
runtime state:

- **Rolling baseline.** `skills/ai-industry-weekly/assets/baseline.md` is
  overwritten by the skill on every run, so each week is compared against the
  previous week rather than a frozen seed table. Seeing it modified in
  `git status` after a run is expected. Commit it to advance the baseline, or
  `git checkout <commit> -- skills/ai-industry-weekly/assets/baseline.md` to
  roll back.
- **Slack channel via environment variable.** The optional Slack push reads the
  channel id from `AI_INDUSTRY_SLACK_CHANNEL_ID`; nothing is stored in the repo:

  ```sh
  export AI_INDUSTRY_SLACK_CHANNEL_ID=C0XXXXXXXXX
  ```

  Leave it unset and the skill still runs and prints its full report — it just
  skips the Slack push.

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
